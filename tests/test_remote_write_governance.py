r"""S3（ADR-002）远端写治理验收矩阵（backlog §38.3 S3-01…S3-10）。

契约：`docs/design/mtls-remote-plane.md` §8.2；实现：
- daemon 权威：`rust_ext/src/daemon/fs_handlers.rs`（handle_write_grant /
  handle_write_commit / ensure_write_governance_tables）
- 客户端编排：`server/daemon_client.py::governed_write_file`
- MCP/CLI 薄壳：`server/tools/tools_workspace.py::file_write`、`cli/main.py::_handle_write`

承载分工（与 S2 验收同款：客户端半在 Python，daemon 半在 Rust）：
| 行 | 客户端半（本文件） | daemon 半（cargo test） |
| S3-01 | 成功流水线/返回体/审计字段 | test_write_grant_commit_end_to_end |
| S3-02 | 错误传播 + 索引不变 + 不回滚语义 | test_write_governance_fail_closed_matrix |
| S3-03 | 同上（E_WRITE_GRANT_EXPIRED） | 同上 |
| S3-04 | 重放（E_WRITE_GRANT_CONSUMED） | test_write_grant_commit_end_to_end |
| S3-05 | 客户端路径归一化零 RPC + 错误传播 | test_write_path_normalization_rejects_escape |
| S3-06 | 跨 principal 错误传播 | test_write_governance_fail_closed_matrix |
| S3-07 | 并发仅一个成功（LOCKED/STALE） | test_write_grant_commit_end_to_end |
| S3-08 | 任务关闭 → 本地文件不变 | test_write_governance_fail_closed_matrix |
| S3-09 | 事件序列无缺口（客户端契约） | verify_audit_chain 回放（Rust） |
| S3-10 | 静态扫描：客户端零 DB 直写、仅 daemon 写 | 全量 Rust 写面 |

stub 边界（与 test_remote_read_plane.py 同款 harness 风格）：本文件用
`_FakeWriteDaemon` 复刻 daemon 契约状态机（grant 绑定 / 逻辑租约 / 过期 /
消费 / principal / 路径集合 / 索引基线 / 拒绝审计），只验证**客户端编排与
错误传播**；daemon 侧真实 SQLite 索引、审计哈希链与并发事务由 Rust 单测
承载，避免用 Python fake 冒充权威实现。
"""

import ast
import hashlib
import json
import os
import re

import pytest

from callwarden.server import daemon_client as dc
from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------
# daemon 契约 stub（状态机镜像 fs_handlers::handle_write_*）
# ----------------------------------------------------------------------

class _FakeWriteDaemon:
    """按 fs_handlers 契约实现的内存 daemon（只服务客户端编排断言）。"""

    def __init__(self, root, principal="pr-a", task_status="in_progress"):
        self._project_root = root
        self.principal = principal
        self.task_status = task_status
        self.steps = {}            # step_id -> (task_id, status)
        self.index = {}            # rel_path -> current_content_hash
        self.grants = {}           # grant_id -> dict
        self.lock_paths = set()    # 活跃 grant 的逻辑租约
        self.audits = []
        self.calls = []
        self.now = 1000.0
        self.on_grant = None       # 签发后钩子（注入并发/过期/外部索引变化）
        self._seq = 0
        self._grant_seq = 0

    # -- harness 接口 ---------------------------------------------------
    def is_remote_profile(self):
        return True

    def _ensure_remote_snapshot(self, db_path=None):
        return "inst-s3"

    # -- 审计（prev-hash 链，模拟 append-only）--------------------------
    def _audit(self, event_type, outcome, grant_id, path, base_hash,
               new_hash, reason_code=""):
        self._seq = len(self.audits) + 1
        body = json.dumps(
            {
                "event_id": self._seq,
                "event_type": event_type,
                "outcome": outcome,
                "grant_id": grant_id,
                "principal_id": self.principal,
                "path": path,
                "base_hash": base_hash,
                "new_hash": new_hash,
                "reason_code": reason_code,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        prev = self.audits[-1]["hash"] if self.audits else ""
        self.audits.append(
            {
                "event_id": self._seq,
                "event_type": event_type,
                "outcome": outcome,
                "grant_id": grant_id,
                "principal_id": self.principal,
                "path": path,
                "base_hash": base_hash,
                "new_hash": new_hash,
                "reason_code": reason_code,
                "prev": prev,
                "hash": hashlib.sha256((prev + body).encode("utf-8")).hexdigest(),
            }
        )

    def _reject(self, code, grant_id, path, base_hash, new_hash):
        self._audit("commit" if grant_id else "grant", "deny", grant_id,
                    path, base_hash, new_hash, code)
        raise DaemonRemoteError(code, code)

    # -- RPC -----------------------------------------------------------
    def call(self, method, params=None, request_id=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "write.grant":
            return self._grant(params)
        if method == "write.commit":
            return self._commit(params)
        raise AssertionError(f"治理写不应发起 {method} RPC")

    def _check_task_step(self, task_id, step_id, grant_id, path, base_hash, new_hash):
        if self.task_status not in ("open", "in_progress"):
            self._reject("E_WRITE_TASK_FORBIDDEN", grant_id, path, base_hash, new_hash)
        step = self.steps.get(step_id)
        if step is None or step[0] != task_id:
            self._reject("E_WRITE_TASK_FORBIDDEN", grant_id, path, base_hash, new_hash)
        if step[1] in ("done", "completed", "closed", "cancelled", "skipped", "failed"):
            self._reject("E_WRITE_TASK_FORBIDDEN", grant_id, path, base_hash, new_hash)

    def _grant(self, params):
        path = params["path"]
        base_hash = params.get("base_hash", "")
        task_id = params["task_id"]
        step_id = params["step_id"]
        self._check_task_step(task_id, step_id, "", path, base_hash, "")

        expected = self.index.get(path, "")
        if base_hash != expected:
            self._reject("E_WRITE_STALE_BASE", "", path, base_hash, "")

        # 过期 grant 收敛后仍是活跃租约 → LOCKED
        if path in self.lock_paths:
            self._reject("E_WRITE_LOCKED", "", path, base_hash, "")

        self._grant_seq += 1
        grant_id = f"wg_{self._grant_seq:08d}"
        allowed = list(params.get("allowed_paths") or [])
        if path not in allowed:
            allowed.append(path)
        ttl = float(params.get("ttl_seconds") or 900.0)
        self.grants[grant_id] = {
            "grant_id": grant_id,
            "path": path,
            "task_id": task_id,
            "step_id": step_id,
            "principal_id": self.principal,
            "base_hash": base_hash,
            "allowed_paths": allowed,
            "status": "active",
            "consumed": 0,
            "created_at": self.now,
            "expires_at": self.now + min(ttl, 900.0),
        }
        self.lock_paths.add(path)
        self._audit("grant", "allow", grant_id, path, base_hash, "")
        if self.on_grant is not None:
            self.on_grant(self.grants[grant_id])
        return {
            "ok": True,
            "grant_id": grant_id,
            "workspace_id": "inst-s3",
            "path": path,
            "task_id": task_id,
            "step_id": step_id,
            "principal_id": self.grants[grant_id]["principal_id"],
            "base_hash": base_hash,
            "allowed_paths": allowed,
            "expires_at": self.grants[grant_id]["expires_at"],
            "ttl_seconds": min(ttl, 900.0),
        }

    def _commit(self, params):
        grant_id = params["grant_id"]
        new_hash = params["new_hash"]
        path = params.get("path") or ""
        grant = self.grants.get(grant_id)
        if grant is None:
            raise DaemonRemoteError("E_WRITE_GRANT_NOT_FOUND", "grant 不存在")
        path = path or grant["path"]

        if grant["principal_id"] != self.principal:
            self._reject("E_WRITE_GRANT_PRINCIPAL_MISMATCH", grant_id, path,
                         grant["base_hash"], new_hash)
        if grant["consumed"] or grant["status"] == "consumed":
            self._reject("E_WRITE_GRANT_CONSUMED", grant_id, path,
                         grant["base_hash"], new_hash)
        if grant["status"] == "expired" or self.now > grant["expires_at"]:
            self._reject("E_WRITE_GRANT_EXPIRED", grant_id, path,
                         grant["base_hash"], new_hash)
        if grant["status"] != "active":
            self._reject("E_WRITE_GRANT_CONSUMED", grant_id, path,
                         grant["base_hash"], new_hash)
        if path != grant["path"] or path not in grant["allowed_paths"]:
            self._reject("E_WRITE_PATH_FORBIDDEN", grant_id, path,
                         grant["base_hash"], new_hash)
        self._check_task_step(grant["task_id"], grant["step_id"], grant_id,
                              path, grant["base_hash"], new_hash)

        current = self.index.get(path, "")
        if current != grant["base_hash"]:
            consumed_after = any(
                g["path"] == path and g["status"] == "consumed"
                and g["consumed_at"] > grant["created_at"]
                for g in self.grants.values()
            )
            code = "E_WRITE_STALE_BASE" if consumed_after else "E_WRITE_BASE_HASH_MISMATCH"
            self._reject(code, grant_id, path, grant["base_hash"], new_hash)

        self.index[path] = new_hash
        grant["status"] = "consumed"
        grant["consumed"] = 1
        grant["consumed_at"] = self.now
        grant["new_hash"] = new_hash
        self.lock_paths.discard(path)
        self._audit("commit", "allow", grant_id, path, grant["base_hash"], new_hash)
        return {
            "ok": True,
            "grant_id": grant_id,
            "workspace_id": "inst-s3",
            "path": path,
            "base_hash": grant["base_hash"],
            "new_hash": new_hash,
            "consumed_at": self.now,
        }


def _install(monkeypatch, daemon):
    monkeypatch.setattr(
        dc.HttpDaemonRpcClient, "get_instance", classmethod(lambda cls: daemon)
    )
    return daemon


@pytest.fixture()
def write_setup(tmp_path, monkeypatch):
    """workspace 根 + 已索引文件 src/a.py（内容 b"old\\n"）。"""
    src = tmp_path / "src"
    src.mkdir()
    target = src / "a.py"
    target.write_bytes(b"old\n")
    daemon = _FakeWriteDaemon(str(tmp_path))
    daemon.index["src/a.py"] = _sha(b"old\n")
    daemon.steps["S-1"] = ("T-1", "in_progress")
    _install(monkeypatch, daemon)
    return daemon, target


def _write(monkeypatch_daemon, content="new\n", path="src/a.py", ws="inst-s3"):
    return dc.governed_write_file(
        path, content, "T-1", "S-1", workspace_instance_id=ws
    )


# ----------------------------------------------------------------------
# S3-01 正常流
# ----------------------------------------------------------------------

class TestS3NormalFlow:
    def test_s3_01_grant_local_write_commit(self, write_setup):
        daemon, target = write_setup
        out = _write(daemon)

        assert out["ok"] is True
        assert out["path"] == "src/a.py"
        assert out["base_hash"] == _sha(b"old\n")
        assert out["new_hash"] == _sha(b"new\n")
        assert out["bytes_written"] == len(b"new\n")
        assert target.read_bytes() == b"new\n"
        # daemon 索引推进 + grant 消费
        assert daemon.index["src/a.py"] == out["new_hash"]
        assert daemon.grants[out["grant_id"]]["consumed"] == 1
        assert "src/a.py" not in daemon.lock_paths

        allow = [e for e in daemon.audits if e["outcome"] == "allow"]
        assert [e["event_type"] for e in allow] == ["grant", "commit"]
        for event in allow:
            assert event["grant_id"] == out["grant_id"]
            assert event["base_hash"] == out["base_hash"]
        assert allow[1]["new_hash"] == out["new_hash"]

    def test_s3_01_rpc_protocol_only_grant_and_commit(self, write_setup):
        daemon, _ = write_setup
        _write(daemon)
        assert [m for m, _ in daemon.calls] == ["write.grant", "write.commit"]
        grant_calls = [p for m, p in daemon.calls if m == "write.grant"]
        assert grant_calls[0]["task_id"] == "T-1"
        assert grant_calls[0]["step_id"] == "S-1"


# ----------------------------------------------------------------------
# S3-02 base_hash 不匹配 / S3-07 并发
# ----------------------------------------------------------------------

class TestS3BaseHash:
    def test_s3_02_local_edit_before_grant_is_stale(self, write_setup):
        """本地先改文件（索引未变）→ grant 侧 fail-closed，索引不变。"""
        daemon, target = write_setup
        target.write_bytes(b"edited-outside\n")   # 本地先改
        index_before = dict(daemon.index)

        with pytest.raises(DaemonRemoteError) as exc:
            _write(daemon)

        assert exc.value.code == "E_WRITE_STALE_BASE"
        assert daemon.index == index_before
        assert not daemon.grants
        assert daemon.audits[-1]["outcome"] == "deny"
        assert daemon.audits[-1]["reason_code"] == "E_WRITE_STALE_BASE"

    def test_s3_02_index_changed_after_grant_is_base_mismatch(self, write_setup):
        daemon, target = write_setup
        other_hash = _sha(b"other-writer\n")

        # 外部写者在 grant 之后推进了索引（无已消费 grant → BASE_HASH_MISMATCH）
        daemon.on_grant = lambda grant: daemon.index.__setitem__(grant["path"], other_hash)

        with pytest.raises(DaemonRemoteError) as exc:
            _write(daemon)

        assert exc.value.code == "E_WRITE_BASE_HASH_MISMATCH"
        # fail-closed：索引保持外部写者的值，不被本次失败 commit 覆盖
        assert daemon.index["src/a.py"] == other_hash
        assert daemon.grants[next(iter(daemon.grants))]["consumed"] == 0
        # 本地文件已原子写（客户端不回滚），错误信息显式声明
        assert target.read_bytes() == b"new\n"
        assert "未自动回滚" in exc.value.message

    def test_s3_07_concurrent_grant_only_one_wins(self, write_setup):
        daemon, _ = write_setup

        first = daemon.call("write.grant", {
            "workspace_instance_id": "inst-s3", "task_id": "T-1",
            "step_id": "S-1", "path": "src/a.py",
            "base_hash": _sha(b"old\n"),
        })
        with pytest.raises(DaemonRemoteError) as exc:
            daemon.call("write.grant", {
                "workspace_instance_id": "inst-s3", "task_id": "T-1",
                "step_id": "S-1", "path": "src/a.py",
                "base_hash": _sha(b"old\n"),
            })
        assert exc.value.code == "E_WRITE_LOCKED"

        # 第一个写者提交成功；第二个写者随后用旧 base 重试 → STALE_BASE
        daemon.call("write.commit", {
            "grant_id": first["grant_id"], "path": "src/a.py",
            "new_hash": _sha(b"new\n"),
        })
        with pytest.raises(DaemonRemoteError) as exc:
            daemon.call("write.grant", {
                "workspace_instance_id": "inst-s3", "task_id": "T-1",
                "step_id": "S-1", "path": "src/a.py",
                "base_hash": _sha(b"old\n"),
            })
        assert exc.value.code == "E_WRITE_STALE_BASE"
        successes = [e for e in daemon.audits
                     if e["event_type"] == "commit" and e["outcome"] == "allow"]
        assert len(successes) == 1


# ----------------------------------------------------------------------
# S3-03 过期 / S3-04 重放
# ----------------------------------------------------------------------

class TestS3GrantLifecycle:
    def test_s3_03_expired_grant_rejected(self, write_setup):
        daemon, target = write_setup
        daemon.on_grant = lambda grant: grant.update(expires_at=daemon.now - 1)

        with pytest.raises(DaemonRemoteError) as exc:
            _write(daemon)

        assert exc.value.code == "E_WRITE_GRANT_EXPIRED"
        assert daemon.index["src/a.py"] == _sha(b"old\n")   # 索引不变
        assert target.read_bytes() == b"new\n"             # 不回滚（显式语义）

    def test_s3_04_replay_second_commit_rejected(self, write_setup):
        daemon, _ = write_setup
        out = _write(daemon)

        with pytest.raises(DaemonRemoteError) as exc:
            daemon.call("write.commit", {
                "grant_id": out["grant_id"], "path": "src/a.py",
                "new_hash": out["new_hash"],
            })

        assert exc.value.code == "E_WRITE_GRANT_CONSUMED"
        assert daemon.index["src/a.py"] == out["new_hash"]


# ----------------------------------------------------------------------
# S3-05 越权路径
# ----------------------------------------------------------------------

class TestS3PathForbidden:
    @pytest.mark.parametrize("bad", [
        "../escape.py", "src/../../x.py", "/abs.py", "C:/win.py",
        "//host/share.py", "", "   ",
    ])
    def test_s3_05_client_normalization_rejects_zero_rpc(self, write_setup, bad):
        daemon, _ = write_setup
        with pytest.raises(DaemonRemoteError) as exc:
            dc.governed_write_file(bad, "x", "T-1", "S-1",
                                   workspace_instance_id="inst-s3")
        assert exc.value.code == "E_WRITE_PATH_FORBIDDEN"
        assert daemon.calls == [], "越权路径必须在客户端 fail-closed，零 RPC"

    def test_s3_05_commit_path_outside_grant_forbidden(self, write_setup):
        daemon, _ = write_setup
        grant = daemon.call("write.grant", {
            "workspace_instance_id": "inst-s3", "task_id": "T-1",
            "step_id": "S-1", "path": "src/a.py",
            "base_hash": _sha(b"old\n"),
            "allowed_paths": ["src/a.py"],
        })
        with pytest.raises(DaemonRemoteError) as exc:
            daemon.call("write.commit", {
                "grant_id": grant["grant_id"], "path": "src/other.py",
                "new_hash": _sha(b"x\n"),
            })
        assert exc.value.code == "E_WRITE_PATH_FORBIDDEN"
        assert daemon.index["src/a.py"] == _sha(b"old\n")


# ----------------------------------------------------------------------
# S3-06 跨 principal
# ----------------------------------------------------------------------

class TestS3CrossPrincipal:
    def test_s3_06_other_principal_cannot_commit(self, write_setup):
        daemon, _ = write_setup
        grant = daemon.call("write.grant", {
            "workspace_instance_id": "inst-s3", "task_id": "T-1",
            "step_id": "S-1", "path": "src/a.py",
            "base_hash": _sha(b"old\n"),
        })

        daemon.principal = "pr-b"    # B 持 A 的 grant
        with pytest.raises(DaemonRemoteError) as exc:
            daemon.call("write.commit", {
                "grant_id": grant["grant_id"], "path": "src/a.py",
                "new_hash": _sha(b"new\n"),
            })

        assert exc.value.code == "E_WRITE_GRANT_PRINCIPAL_MISMATCH"
        assert daemon.index["src/a.py"] == _sha(b"old\n")
        assert daemon.grants[grant["grant_id"]]["consumed"] == 0


# ----------------------------------------------------------------------
# S3-08 任务关闭后写
# ----------------------------------------------------------------------

class TestS3TaskClosed:
    def test_s3_08_closed_task_forbids_grant_and_local_write(self, write_setup):
        daemon, target = write_setup
        daemon.task_status = "closed"

        with pytest.raises(DaemonRemoteError) as exc:
            _write(daemon)

        assert exc.value.code == "E_WRITE_TASK_FORBIDDEN"
        assert not daemon.grants
        assert daemon.index["src/a.py"] == _sha(b"old\n")
        assert target.read_bytes() == b"old\n", "grant 失败必须发生在本地写之前"
        reject = daemon.audits[-1]
        assert reject["outcome"] == "deny"
        assert reject["reason_code"] == "E_WRITE_TASK_FORBIDDEN"

    def test_s3_08_closed_step_forbids_grant(self, write_setup):
        daemon, _ = write_setup
        daemon.steps["S-1"] = ("T-1", "done")
        with pytest.raises(DaemonRemoteError) as exc:
            _write(daemon)
        assert exc.value.code == "E_WRITE_TASK_FORBIDDEN"


# ----------------------------------------------------------------------
# S3-09 审计回放（客户端契约半）
# ----------------------------------------------------------------------

def _replay(audits):
    """重算 prev-hash 链，返回 (events, broken_ids)。"""
    prev = ""
    broken = []
    for event in audits:
        body = json.dumps(
            {k: event[k] for k in (
                "event_id", "event_type", "outcome", "grant_id", "principal_id",
                "path", "base_hash", "new_hash", "reason_code")},
            sort_keys=True, ensure_ascii=False,
        )
        expected = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
        if event["prev"] != prev or event["hash"] != expected:
            broken.append(event["event_id"])
        prev = event["hash"]
    return audits, broken


class TestS3AuditReplay:
    def test_s3_09_allow_and_deny_events_form_gap_free_chain(self, write_setup):
        daemon, _ = write_setup
        _write(daemon)                                   # grant/allow + commit/allow

        daemon2 = daemon
        daemon2.on_grant = lambda grant: grant.update(expires_at=daemon2.now - 1)
        with pytest.raises(DaemonRemoteError):
            _write(daemon2, content="again\n")           # grant/allow + commit/deny(expired)

        events, broken = _replay(daemon.audits)
        assert broken == [], f"审计链存在缺口: {broken}"
        ids = [e["event_id"] for e in events]
        assert ids == list(range(1, len(ids) + 1)), "事件 id 必须连续无缺口"
        types = {(e["event_type"], e["outcome"]) for e in events}
        assert ("grant", "allow") in types
        assert ("commit", "allow") in types
        assert ("commit", "deny") in types
        assert events[-1]["reason_code"] == "E_WRITE_GRANT_EXPIRED"


# ----------------------------------------------------------------------
# S3-10 daemon 唯一写者 + 本地模式 fail-closed
# ----------------------------------------------------------------------

def _source(rel_path):
    with open(os.path.join(_REPO_ROOT, rel_path), encoding="utf-8") as handle:
        return handle.read()


class TestS3DaemonOnlyWriter:
    def test_s3_10_client_write_path_has_no_local_db_access(self):
        tree = ast.parse(_source("server/daemon_client.py"))
        func = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "governed_write_file"
        )
        src = ast.get_source_segment(_source("server/daemon_client.py"), func)
        assert "sqlite3" not in src
        assert ".execute(" not in src
        assert "INSERT" not in src.upper() and "UPDATE " not in src.upper()
        assert set(re.findall(r'client\.call\(\s*"([^"]+)"', src)) == {
            "write.grant", "write.commit",
        }

    def test_s3_10_mcp_and_cli_delegate_to_single_implementation(self):
        tools = _source("server/tools/tools_workspace.py")
        assert "governed_write_file" in tools
        cli = _source("cli/main.py")
        assert "governed_write_file" in cli
        # write 子命令跳过本地 workspace 注册（不产生本地 SQLite 旁路）
        assert "remote_write_cmd" in cli
        # 本地原子写只发生在客户端本机（_atomic_write_bytes）
        assert "_atomic_write_bytes" in _source("server/daemon_client.py")

    def test_s3_10_local_profile_fail_closed(self, write_setup, monkeypatch, tmp_path):
        daemon, _ = write_setup

        class _Loopback(_FakeWriteDaemon):
            def is_remote_profile(self):
                return False

        local = _Loopback(str(tmp_path))
        _install(monkeypatch, local)

        with pytest.raises(DaemonRemoteError) as exc:
            _write(local)
        assert exc.value.code == "E_WRITE_REMOTE_ONLY"
        assert local.calls == [], "loopback 不得有本地旁路写"

    def test_s3_10_route_matrix_registers_authoritative_commit(self):
        matrix = json.loads(_source("deliverables/software-company/tool_migration_matrix.json"))
        entry = next(
            tool for tool in matrix["tools"] if tool["name"] == "file_write"
        )
        assert entry["rpc_method"] == "write.commit"
        assert entry["op_class"] == "PROTECTED_MUTATION"
        assert entry["status"] == "migrated"
