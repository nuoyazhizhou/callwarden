r"""任务 5D：`task.next_action` 进程级独立验收测试与证据（cw-role-handoff-task-loop.md §6/§7）。

范围（§7 5D）：
- 只新增/维护 §6 的 E2E、并发、ABA、fencing、迁移与零写入验收；
- **不修改** 0C 独占的 `tests/test_task_loop_capability_authority.py`、
  `tests/test_task_loop_gate_order.py`、`rust_ext/src/daemon/task_loop/capability_control_test.rs`；
- **不修改** 5A–5C 生产实现（`next_action.rs` evaluator、CLI adapter、Skill 文档）。

5A 的 `next_action_test.rs` 已在领域层覆盖 14 个 evaluator 单测（READY/CLAIM、
WAITING、REVIEW、REVISE、ADJUDICATE、COMPLETE、BLOCKED、remediation、零写入、
错误码）。5D 不重复单测，而是在**进程级**提供独立证据：

1. **wire 级**：真实 `cw-daemon.exe`（隔离临时 task DB + Named Pipe）上经 RPC
   round-trip 调用 `task.next_action`，禁 mock：
   - task 不存在 → `E_TASK_NOT_FOUND_OR_UNAUTHORIZED`（非泄露、结构化，不是 method_not_found）；
   - 无不可变 binding（CLI 旧路径创建）→ `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；
   - binding 悬空（capture 行缺失）→ `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；
   - 完整 binding+capture+Task Contract+step+Role Contract 链（SQL 播种，与
     next_action_test.rs 同构）→ `READY/CLAIM`，核验全部渲染字段（task_contract /
     role_contract / authorization / allowed_paths / routing / next_session / source）；
   - workspace_instance_id 与 capture 不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`；
   - **任意查询零写入**：`task_events / task_leases / task_verdict_events /
     task_steps / task_workspace_bindings / workspace_authority_captures /
     task_contract_revisions / role_contract_* / task_step_role_contract_bindings`
     行数不变（§6 L612）。
2. **CLI 进程级**：真实 `C:\Python314\python.exe cw.py task next-action` 子进程：
   - `--json` 与人类可读双渲染正确（READY/CLAIM、错误码）；
   - local 模式 fail-closed（无 evaluator，输出 `E_DAEMON_UNAVAILABLE`，不伪造决策）；
   - enterprise 模式 daemon 不可用 fail-closed（不回退本地 SQLite）。
3. **Skill discovery**：5C 交付的 `.agents/skills/cw-task-loop/SKILL.md`（frontmatter +
   只读约束 + 角色卡映射）与 `references/user-guide.md` 存在且结构达标。

前置条件（与 test_lease_gate_empirical.py 一致）：
1. Windows 平台（Named Pipe）；
2. 已构建 `cw-daemon.exe`：`cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon`；
3. 默认管道 `\\.\pipe\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip）。

证据保存：运行本文件时的 pytest 输出 + 隔离 daemon 日志（fixture tmp 内 daemon.log）
即原始证据，供任务 5 交付收尾。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.daemon_client import DaemonUnavailableError

_PY_EXE = r"C:\Python314\python.exe"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")
_SKILL_DIR = os.path.join(_REPO_ROOT, ".agents", "skills", "cw-task-loop")
_SKILL_MD = os.path.join(_SKILL_DIR, "SKILL.md")
_SKILL_USER_GUIDE = os.path.join(_SKILL_DIR, "references", "user-guide.md")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="task.next_action 进程级 E2E 需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)

# 任意 next_action 查询都不得改动的表（§6 L612 零写入）
_ZERO_WRITE_TABLES = [
    "tasks",
    "task_steps",
    "task_events",
    "task_leases",
    "task_verdict_events",
    "task_workspace_bindings",
    "workspace_authority_captures",
    "task_contract_revisions",
    "role_contract_lineages",
    "role_contract_revisions",
    "task_step_role_contract_bindings",
]

# ----------------------------------------------------------------------
# daemon fixture（复用 test_lease_gate_empirical.py 模式：隔离临时库 + Named Pipe）
# ----------------------------------------------------------------------

def _daemon_config(tmp: str) -> dict:
    """生成隔离的 daemon JSON 配置（Windows 管道名由 transport 按 SID 派生）。"""
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 2,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 2,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


@pytest.fixture(scope="module", autouse=True)
def ensure_fresh_binary():
    """P2 门禁：显式构建 cw-daemon，确保二进制由当前源码重建（fresh runtime）。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    build = subprocess.run(
        [cargo, "build", "--release", "--no-default-features",
         "--manifest-path", os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
         "--bin", "cw-daemon"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    if build.returncode != 0:
        pytest.fail("cargo build 失败，二进制无法由当前源码重建：\n" + (build.stdout + build.stderr)[-3000:])
    if not os.path.exists(_DAEMON_BIN):
        pytest.fail(f"cargo build 成功但未产出 {_DAEMON_BIN}")


@pytest.fixture(scope="module")
def lease_env():
    """启动真实隔离 cw-daemon（临时 task DB + 默认 Named Pipe），返回
    (client, tmp, task_db, proc, pipe)。

    与 test_lease_gate_empirical.py 一致：探针默认管道，被其他 daemon 占用则 skip
    （不杀他人进程）；隔离库插入 workspace id=1，使 capture/binding 可绑定。
    测试只用隔离临时任务库，绝不触碰真实用户任务库。
    """
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_task_next_action_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    log = open(os.path.join(tmp, "daemon.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )

    client = UnixDaemonRpcClient(socket_path=pipe, timeout=10)
    deadline = time.time() + 40
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            if client.call("ping").get("status") == "ok":
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ready:
        log.flush()
        pytest.fail("隔离 daemon 未在超时内响应")

    task_db = os.path.join(tmp, "callwarden.db")
    conn = sqlite3.connect(task_db)
    try:
        # 预插多个 workspace：capture 链按 workspace_id 聚合校验（COUNT==MAX），
        # 每个测试用独立 workspace_id 播种，避免互相干扰。
        for ws_id in range(1, 17):
            conn.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?1, ?2, ?3, ?4)",
                (ws_id, f"next-action-e2e-{ws_id}", f"ws-{ws_id}", time.time()),
            )
        conn.commit()
    finally:
        conn.close()

    yield client, tmp, task_db, proc, pipe

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not os.environ.get("CW_KEEP_5D_TMP"):
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# CLI 子进程辅助（复用 test_cli_task_lease_parity.py 模式）
# ----------------------------------------------------------------------

def _cli_env(pipe: str, mode: str = "enterprise", extra: dict = None) -> dict:
    """构造 CLI 子进程环境：路由到指定 daemon、enterprise/auto、autostart 窗口 0。

    - CW_DAEMON_TRANSPORT=named-pipe：显式使用 Named Pipe，避免 stale manifest
      拦截导致测试读到错误的 endpoint。
    - CW_DAEMON_AUTOSTART_WINDOW=0：daemon 连接失败时立即 fail-closed。
    - CALLWARDEN_SKIP_AUTO_SETUP=1：跳过首次自动配置的副作用。
    """
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = mode
    env["CW_DAEMON_ENDPOINT"] = pipe
    env["CW_DAEMON_TRANSPORT"] = "named-pipe"
    env["CW_TASK_WRITE_POLICY"] = "shared"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
    if extra:
        env.update(extra)
    return env


def _run_cw_cli(args, env, cwd, timeout=90):
    r"""真实 `C:\Python314\python.exe cw.py ...` CLI 子进程。"""
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _assert_ok(proc, what: str):
    assert proc.returncode == 0, f"{what} 失败(exit={proc.returncode}):\n{_all_out(proc)}"


# ----------------------------------------------------------------------
# 5D 播种：完整不可变 binding + capture 链 + Task Contract + step + Role Contract
# ----------------------------------------------------------------------

def _ws_instance_for_root(root: str) -> str:
    """与 cli/main.py derive_workspace_instance_id 对齐（SHA-256 前 16 位）。"""
    abs_root = os.path.abspath(root)
    norm_root = abs_root.replace("\\", "/")
    return hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]


def _c14n_payload_json(instance_id: str, view_hash: str, host_hash: str, manifest_hash: str) -> str:
    """复刻 create.rs `c14n_value`：键按 code point 排序、Unicode NFC、紧凑 JSON。"""
    payload = {
        "workspace_instance_id": instance_id,
        "client_view_root_hash": view_hash,
        "host_real_root_hash": host_hash,
        "workspace_manifest_hash": manifest_hash,
    }
    payload = {unicodedata.normalize("NFC", k): v for k, v in payload.items()}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _registry_identity_hash(instance_id: str, view_hash: str, host_hash: str, manifest_hash: str) -> str:
    """复刻 create.rs registry_identity_hash（`sha256:<hex>`）。"""
    canonical = _c14n_payload_json(instance_id, view_hash, host_hash, manifest_hash)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _seed_full_chain(task_db: str, task_id: str, workspace_instance_id: str, workspace_id: int = 1) -> dict:
    """直接播种 §6 READY/CLAIM 所需的最小不可变链（与 next_action_test.rs 同构）。

    写入：task + workspace_authority_captures（rev=1）+ task_workspace_bindings +
    task_contract_revisions（rev=1）+ task_steps（pending）+ role_contract_revisions
    （rev=1，role=implementer→executor）+ task_step_role_contract_bindings（rev=1）。

    workspace_id 必须互不重叠：capture 链按 workspace 聚合校验（COUNT==MAX），
    同 workspace 多行 rev=1 capture 会被判断链。
    """
    now = time.time()
    view_hash = f"client-view-{task_id}"
    host_hash = f"host-root-{task_id}"
    manifest_hash = f"manifest-{task_id}"
    manifest_payload = json.dumps({"kind": "5d", "task": task_id}, ensure_ascii=False)
    identity_hash = _registry_identity_hash(
        workspace_instance_id, view_hash, host_hash, manifest_hash)

    capture_id = f"wc-{task_id}-1"
    binding_id = f"tb-{task_id}"
    lineage_id = f"rcl-{task_id}-executor"
    rcr_id = f"rcr-{task_id}-1"
    step_binding_id = f"sb-{task_id}-1"
    rc_hash = f"sha256:rc-{task_id}"
    rc_c14n_version = "role-contract-c14n/v1"
    rc_rules_hash = f"rules-hash-{task_id}"

    rc_payload = json.dumps({
        "role": "implementer",
        "skill_id": "skill-5d",
        "skill_version": "1.0",
        "prompt_template_id": "pt-5d",
        "prompt_hash": "ph-5d",
        "allowed_paths": ["src/"],
        "forbidden_paths": ["target/"],
        "commands": ["echo"],
        "acceptance_checks": ["pass"],
        "required_evidence": ["log"],
        "handoff_to": "",
        "independence": {
            "different_agent_instance_from": [],
            "different_session_from": ["reviewer"],
            "max_tokens": 100,
        },
    }, ensure_ascii=False)

    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) "
            "VALUES (?1, ?2, '5d seed', '5d-test', 'open', ?3, ?3)",
            (task_id, f"task-{task_id}", now),
        )
        conn.execute(
            "INSERT INTO workspace_authority_captures "
            "(workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id, "
            " daemon_workspace_id, workspace_instance_id, capture_canonicalization_version, "
            " capture_canonicalization_rules_hash, registry_identity_payload_json, "
            " registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash, "
            " client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at) "
            "VALUES (?1, ?9, 1, NULL, 42, ?2, 'workspace-capture-c14n/v1', 'cap-rules-5d', ?3, "
            " ?4, ?5, ?6, ?7, ?8, '5d-test', '0.000000')",
            (
                capture_id,
                workspace_instance_id,
                _c14n_payload_json(workspace_instance_id, view_hash, host_hash, manifest_hash),
                identity_hash,
                manifest_payload,
                manifest_hash,
                view_hash,
                host_hash,
                workspace_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at) "
            "VALUES (?1, ?4, ?2, ?3, '5d-test', '0.000000')",
            (task_id, binding_id, capture_id, workspace_id),
        )
        conn.execute(
            "INSERT INTO task_contract_revisions "
            "(contract_id, revision, contract_hash, profile, task_id, workspace_id, envelope_payload, "
            " created_at, created_by, normalization_version, normalization_rules_hash) "
            "VALUES (?1, 1, ?2, 'review', ?3, ?4, '{\"objective\":\"5d\"}', 0.0, '5d-test', "
            " 'verdict-normalization/v1', 'norm-rules-5d')",
            (f"tc-{task_id}", f"sha256:tc-{task_id}", task_id, workspace_id),
        )
        conn.execute(
            "INSERT INTO task_steps "
            "(id, task_id, step_index, action, target_file, status, result, created_at) "
            "VALUES (?1, ?2, 1, 'implement', 'f.py', 'pending', '', ?3)",
            (f"{task_id}-step-1", task_id, now),
        )
        conn.execute(
            "INSERT INTO role_contract_lineages "
            "(role_contract_lineage_id, task_id, workspace_id, role, created_by, authoritative_created_at) "
            "VALUES (?1, ?2, ?3, 'executor', '5d-test', '0.000000')",
            (lineage_id, task_id, workspace_id),
        )
        conn.execute(
            "INSERT INTO role_contract_revisions "
            "(role_contract_revision_id, role_contract_lineage_id, revision, supersedes_revision_id, "
            " canonical_payload_json, canonicalization_version, canonicalization_rules_hash, "
            " role_contract_hash, created_by, authoritative_created_at) "
            "VALUES (?1, ?2, 1, NULL, ?3, ?4, ?5, ?6, '5d-test', '0.000000')",
            (rcr_id, lineage_id, rc_payload, rc_c14n_version, rc_rules_hash, rc_hash),
        )
        conn.execute(
            "INSERT INTO task_step_role_contract_bindings "
            "(binding_id, workspace_id, task_id, step_id, role_contract_lineage_id, "
            " role_contract_revision_id, role_contract_revision, role_contract_hash, "
            " canonicalization_version, canonicalization_rules_hash, binding_revision, "
            " supersedes_binding_id, created_by, authoritative_created_at) "
            "VALUES (?1, ?9, ?2, ?3, ?4, ?5, 1, ?6, ?7, ?8, 1, NULL, '5d-test', '0.000000')",
            (
                step_binding_id, task_id, f"{task_id}-step-1", lineage_id, rcr_id,
                rc_hash, rc_c14n_version, rc_rules_hash, workspace_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "workspace_instance_id": workspace_instance_id}


def _seed_unbound_task(task_db: str, task_id: str) -> None:
    """播种 task 行但**不写** binding/capture（CLI 旧路径创建的等价物）。"""
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) "
            "VALUES (?1, 'unbound', '', 'cli-legacy', 'open', ?2, ?2)",
            (task_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_dangling_binding(task_db: str, task_id: str, workspace_instance_id: str) -> None:
    """播种 task + binding，但 binding 引用的 capture 行缺失（悬空 binding）。"""
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) "
            "VALUES (?1, 'dangling', '', '5d-test', 'open', ?2, ?2)",
            (task_id, time.time()),
        )
        conn.execute(
            "INSERT INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, created_by, authoritative_created_at) "
            "VALUES (?1, 1, ?2, 'wc-does-not-exist', '5d-test', '0.000000')",
            (task_id, f"tb-{task_id}"),
        )
        conn.commit()
    finally:
        conn.close()


def _table_snapshot(task_db: str) -> dict:
    """零写入验收：读取关注表当前行数（表不存在则跳过）。"""
    conn = sqlite3.connect(task_db)
    try:
        snapshot = {}
        for table in _ZERO_WRITE_TABLES:
            try:
                snapshot[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                continue  # 表未创建则忽略
        return snapshot
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 1) wire 级：真实 daemon 上的 task.next_action（禁 mock）
# ----------------------------------------------------------------------

@requires_binaries
class TestNextActionWireLevel:
    """进程级 wire 验收：真实 cw-daemon.exe + Named Pipe 的 task.next_action。"""

    def test_task_not_found_returns_structured_error(self, lease_env):
        client, tmp, _task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.next_action", {
                "task_id": "T-5D-NOT-EXIST",
                "workspace_instance_id": ws,
            })
        # 非泄露错误：对未证明同 workspace 的 caller 统一该码（§3.2 规则 2）
        assert exc.value.code == "E_TASK_NOT_FOUND_OR_UNAUTHORIZED", exc.value
        assert "不存在" in exc.value.message, exc.value

    def test_task_no_binding_fail_closed(self, lease_env):
        """task 存在但无不可变 binding（CLI 旧路径创建）→ UNAVAILABLE（§3.2 规则 1）。"""
        client, tmp, task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-UNBOUND"
        _seed_unbound_task(task_db, task_id)
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.next_action", {
                "task_id": task_id, "workspace_instance_id": ws,
            })
        assert exc.value.code == "E_WORKSPACE_AUTHORITY_UNAVAILABLE", exc.value
        assert "binding" in exc.value.message, exc.value

    def test_dangling_binding_capture_missing_fail_closed(self, lease_env):
        """binding 引用的 capture 行缺失（悬空）→ UNAVAILABLE（§3.2 规则 1）。"""
        client, tmp, task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-DANGLING"
        _seed_dangling_binding(task_db, task_id, ws)
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.next_action", {
                "task_id": task_id, "workspace_instance_id": ws,
            })
        assert exc.value.code == "E_WORKSPACE_AUTHORITY_UNAVAILABLE", exc.value
        assert "capture" in exc.value.message, exc.value

    def test_workspace_instance_mismatch_fail_closed(self, lease_env):
        """capture 链合法但请求 instance 不匹配 → MISMATCH（§3.2 规则 3）。"""
        client, tmp, task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-MISMATCH"
        _seed_full_chain(task_db, task_id, ws, workspace_id=1)
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.next_action", {
                "task_id": task_id,
                "workspace_instance_id": "ws-inst-other",  # 与 capture 不一致
            })
        assert exc.value.code == "E_WORKSPACE_AUTHORITY_MISMATCH", exc.value
        assert "不一致" in exc.value.message, exc.value

    def test_seeded_full_chain_ready_claim_rendered_fields(self, lease_env):
        """完整不可变链 → READY/CLAIM，核验 §6 L586 的全部渲染字段。"""
        client, tmp, task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-READY-CLAIM"
        _seed_full_chain(task_db, task_id, ws, workspace_id=2)

        result = client.call("task.next_action", {
            "task_id": task_id, "workspace_instance_id": ws,
        })
        assert result["task_id"] == task_id
        assert result["decision"] == "READY"
        assert result["action"] == "CLAIM"
        assert result["required_role"] == "executor"
        assert result["step_id"] == f"{task_id}-step-1"

        # task_contract：id / revision / hash 回显
        tc = result["task_contract"]
        assert tc == {"id": f"tc-{task_id}", "revision": 1, "hash": f"sha256:tc-{task_id}"}, tc

        # role_contract：id/revision/hash/c14n/skill/prompt/handoff 回显
        rc = result["role_contract"]
        assert rc["id"] == f"rcl-{task_id}-executor"
        assert rc["revision_id"] == f"rcr-{task_id}-1"
        assert rc["revision"] == 1
        assert rc["hash"].startswith("sha256:rc-")
        assert rc["canonicalization_version"] == "role-contract-c14n/v1"
        assert rc["skill_id"] == "skill-5d"
        assert rc["skill_version"] == "1.0"
        assert rc["prompt_template_id"] == "pt-5d"
        assert rc["handoff_to"] == ""

        # authorization：acting_role=executor、lease_role=executor、READY 必须 lease+fencing
        auth = result["authorization"]
        assert auth["acting_role"] == "executor"
        assert auth["lease_role"] == "executor"
        assert auth["lease_required"] is True
        assert auth["fencing_required"] is True
        assert auth["different_session_from"] == ["reviewer"]

        # allowed/forbidden paths
        assert result["allowed_paths"] == ["src/"]
        assert result["forbidden_paths"] == ["target/"]

        # eligibility 与 blocking
        assert result["eligibility"]["verdict"] == "not_required_for_claim"
        assert result["blocking_conditions"] == []
        assert result["revision_hint"] is None

        # routing：system_evaluator，next=executor/claim_current_step
        routing = result["routing"]
        assert routing["origin_kind"] == "system_evaluator"
        assert routing["next_role"] == "executor"
        assert routing["next_action"] == "claim_current_step"
        assert any("可领取" in r for r in routing["reason"]), routing

        # next_session：executor 新会话非强制（must_be_new_session=false）
        ns = result["next_session"]
        assert ns == {
            "role": "executor",
            "task_id": task_id,
            "step_id": f"{task_id}-step-1",
            "must_be_new_session": False,
        }, ns

        # source：只读投影（task_status / contract hashes / evaluated_at）
        src = result["source"]
        assert src["task_status"] == "open"
        assert src["task_contract_hash"] == f"sha256:tc-{task_id}"
        assert src["role_contract_hash"].startswith("sha256:rc-")
        assert float(src["evaluated_at"]) > 0

        # 不泄露 from_role（系统查询无角色来源）
        assert "from_role" not in result

    def test_any_query_zero_write(self, lease_env):
        """任意 next_action 查询（成功 + 拒绝）都不得改动任何行（§6 L612）。"""
        client, tmp, task_db, _proc, _pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-ZERO-WRITE"
        _seed_full_chain(task_db, task_id, ws, workspace_id=3)

        before = _table_snapshot(task_db)

        # 成功查询（READY/CLAIM）
        result = client.call("task.next_action", {
            "task_id": task_id, "workspace_instance_id": ws,
        })
        assert result["decision"] == "READY"
        # 重复查询（幂等）
        client.call("task.next_action", {"task_id": task_id, "workspace_instance_id": ws})
        # 拒绝查询（不存在 / 无 binding / 悬空 binding / mismatch）
        for bad in [
            {"task_id": "T-5D-NOT-EXIST", "workspace_instance_id": ws},
            {"task_id": "T-5D-UNBOUND-X", "workspace_instance_id": ws},
            {"task_id": task_id, "workspace_instance_id": "ws-inst-other"},
        ]:
            with pytest.raises(DaemonRemoteError):
                client.call("task.next_action", bad)

        after = _table_snapshot(task_db)
        changed = {t: (before.get(t), after.get(t)) for t in before if before[t] != after.get(t)}
        assert not changed, f"next_action 查询产生了写入: {changed}"


# ----------------------------------------------------------------------
# 2) CLI 进程级：真实 cw.py 子进程
# ----------------------------------------------------------------------

@requires_binaries
class TestNextActionCliProcess:
    r"""真实 `C:\Python314\python.exe cw.py task next-action` 子进程验收。"""

    def test_cli_json_ready_claim(self, lease_env):
        client, tmp, task_db, _proc, pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-CLI-READY"
        _seed_full_chain(task_db, task_id, ws, workspace_id=4)

        proc = _run_cw_cli(["task", "next-action", task_id, "--json"],
                           _cli_env(pipe), tmp)
        _assert_ok(proc, f"next-action --json {task_id}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"--json 输出不是合法 JSON:\n{_all_out(proc)} ({exc})")
        assert data["task_id"] == task_id
        assert data["decision"] == "READY"
        assert data["action"] == "CLAIM"
        assert data["routing"]["origin_kind"] == "system_evaluator"
        assert data["next_session"]["must_be_new_session"] is False
        # CLI 薄壳不得改写 daemon 决策：不输出 from_role、不注入本地补造字段
        assert "from_role" not in data

    def test_cli_human_readable_ready_claim(self, lease_env):
        client, tmp, task_db, _proc, pipe = lease_env
        ws = _ws_instance_for_root(tmp)
        task_id = "T-5D-CLI-HUMAN"
        _seed_full_chain(task_db, task_id, ws, workspace_id=5)

        proc = _run_cw_cli(["task", "next-action", task_id],
                           _cli_env(pipe), tmp)
        _assert_ok(proc, f"next-action 人类可读 {task_id}")
        out = _all_out(proc)
        assert "系统派工" in out, out
        assert "决策: READY" in out, out
        assert "动作: CLAIM" in out, out
        assert "executor" in out, out
        # 人类可读渲染仍源自 daemon：显示路由来源（system_evaluator）
        assert "system_evaluator" in out, out

    def test_cli_json_error_not_found(self, lease_env):
        client, tmp, _task_db, _proc, pipe = lease_env
        proc = _run_cw_cli(["task", "next-action", "T-5D-CLI-NOT-EXIST", "--json"],
                           _cli_env(pipe), tmp)
        _assert_ok(proc, "next-action --json 不存在任务")
        data = json.loads(proc.stdout)
        assert data["ok"] is False
        assert data["code"] == "E_TASK_NOT_FOUND_OR_UNAUTHORIZED", data

    def test_cli_human_error_not_found(self, lease_env):
        client, tmp, _task_db, _proc, pipe = lease_env
        proc = _run_cw_cli(["task", "next-action", "T-5D-CLI-NOT-EXIST"],
                           _cli_env(pipe), tmp)
        _assert_ok(proc, "next-action 人类可读不存在任务")
        out = _all_out(proc)
        assert "E_TASK_NOT_FOUND_OR_UNAUTHORIZED" in out, out
        assert "不存在" in out, out

    def test_cli_local_mode_fail_closed(self):
        """local 模式无 evaluator：fail-closed 输出 E_DAEMON_UNAVAILABLE，不伪造决策。"""
        tmp = tempfile.mkdtemp(prefix="cw_5d_local_")
        try:
            proc = _run_cw_cli(
                ["task", "next-action", "T-5D-LOCAL", "--json"],
                _cli_env(r"\\.\pipe\placeholder-5d", mode="local"), tmp)
            _assert_ok(proc, "local 模式 next-action")
            data = json.loads(proc.stdout)
            assert data["ok"] is False
            assert data["code"] == "E_DAEMON_UNAVAILABLE", data
            assert "daemon" in data["message"], data
            assert "local 模式" in data["message"], data
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_enterprise_daemon_unavailable_fail_closed(self):
        """enterprise 模式 daemon 不可用：fail-closed，不回退本地 SQLite。"""
        tmp = tempfile.mkdtemp(prefix="cw_5d_unavail_")
        try:
            fake_pipe = rf"\\.\pipe\nonexistent-5d-{os.getpid()}"
            proc = _run_cw_cli(
                ["task", "next-action", "T-5D-UNAVAIL", "--json"],
                _cli_env(fake_pipe, mode="enterprise"), tmp)
            _assert_ok(proc, "enterprise 无 daemon next-action")
            data = json.loads(proc.stdout)
            assert data["ok"] is False
            assert data["code"] == "E_DAEMON_UNAVAILABLE", data
            # 若静默回退本地 SQLite，不会出现 daemon 连接失败消息
            assert "连接失败" in data["message"] or "无法连接" in data["message"], data
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# 3) Skill/discovery 独立证据（5C 交付物）
# ----------------------------------------------------------------------

class TestNextActionSkillDiscovery:
    """5C 交付的 Skill/discovery 文件存在性与结构约束（只读检查）。"""

    def test_skill_md_present_with_frontmatter(self):
        assert os.path.isfile(_SKILL_MD), f"缺失 SKILL.md: {_SKILL_MD}"
        with open(_SKILL_MD, encoding="utf-8") as f:
            content = f.read()
        assert content.startswith("---"), "SKILL.md 必须含 YAML frontmatter"
        assert "name: cw-task-loop" in content, "frontmatter 缺 name"
        assert "description:" in content, "frontmatter 缺 description"
        assert "## Fixed Procedure" in content, "缺 Fixed Procedure 节"
        assert "## Role Card Rendering" in content, "缺 Role Card Rendering 节"
        assert "## Forbidden Shortcuts" in content, "缺 Forbidden Shortcuts 节"

    def test_skill_user_guide_present(self):
        assert os.path.isfile(_SKILL_USER_GUIDE), f"缺失 user-guide.md: {_SKILL_USER_GUIDE}"
        with open(_SKILL_USER_GUIDE, encoding="utf-8") as f:
            content = f.read()
        assert len(content.strip()) > 200, "user-guide.md 内容过短"
        assert "next-action" in content, "user-guide 未说明调用方式"
        assert "must_be_new_session" in content or "新会话" in content, \
            "user-guide 未说明窗口/会话独立性"

    def test_skill_read_only_contract(self):
        """Skill 只读约束：不得引导任何 mutation 作为本 Skill 动作。"""
        with open(_SKILL_MD, encoding="utf-8") as f:
            content = f.read()
        for forbidden in ("不得调用任何 mutation", "不得领取步骤", "不得伪造"):
            assert forbidden in content, f"SKILL.md 缺少只读约束: {forbidden}"
        # 每次 report/verdict 后重新查询（不轻信自然语言）
        assert "每次 report 或 verdict 后重新查询" in content
