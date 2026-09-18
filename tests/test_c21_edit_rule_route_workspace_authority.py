"""C-21 回归：edit/rule 写面路由块 workspace 命名空间权威解析（真实隔离 daemon 功能矩阵）。

背景（本卡 `T-1789397153231-f07a8d84`，承接 backlog §W20 F4）：

  - `rust_ext/src/daemon/snapshot_state.rs` 的第二路由块（edit/rule 写面 19 方法，
    arm ~L3284）原以 `owned_workspace(...).workspace_id`（daemon registry
    `daemon_workspaces.workspace_id` 的**代理 ROWID**）作为 handler 的 workspace
    作用域，并以 `open_write(self, ws)` 闭包打开物理库 —— 与 C-17 admin 块同根因
    （代理 ROWID 与物理库 `workspaces.id` 不是同一命名空间；生产实测代理 207 vs 真 id 1）。
  - 修法（本卡 step1，路由层单文件单点）：整块改走同文件既有
    `open_codegraph_db_write`（内部 owned_workspace ACL + resolve_true_workspace_id
    按 client_view_root 匹配物理库 workspaces.root_path 取真 id）；
    方法名列表 / match 臂与全部 handler 零改动。

判别力设计（隔离环境内复现生产「代理≠真 id」分裂，同 C-17 范式）：

  隔离 registry 先插 **dummy** daemon_workspaces 行（rowid=1，client_view_root
  不匹配 ROOT），再插真实行（rowid=2 = PROXY_ID）。旧实现必然解析出代理 2 →
  FK 失败 / WHERE 不命中 / 查根失败；新实现解析 client_view_root → 真 id 1。
  所有落库断言同时锚定 `workspace_id == WS_ID`。

机械判别分类（step0 静态盘点 + step2 A/B 实测双重确认）：

  - 严格判别 ×10（修复前恒失败/恒空，修复后通过且落库真 id）：
      * 类 1（FK 写失败）×3：edit.propose / edit.propose_range_patch →
        file_edit_audit、edit.record_token_savings → token_savings_ledger
        （两表 workspace_id 均 FOREIGN KEY → workspaces(id)，代理 2 不在取值域）；
      * 类 2（WHERE/查根不命中）×7：edit.revert（WHERE workspace_id → edit_not_found）、
        edit.propose_symbol_id_patch / edit.propose_symbol_patch（JOIN
        file_instances.workspace_id → symbol_not_found）、edit.restore_all_comments
        （restored=0）、rule.extract_candidates（file_instances 子查询空 → 0）、
        rule.insert_agents_md_block / rule.sync_agents_md（workspace_root() 按
        `SELECT root_path FROM workspaces WHERE id=代理` → QueryReturnedNoRows）。
  - 不可判别 ×7（SQL 未实际消费 workspace_id：`let _ = workspace_id;` 或写表无
    workspace_id 列；新旧实现均通过）——按卡面要求保留为写面连通性正例 + 落库读回，
    并如实标注：edit.restore_comment、rule.candidate_create、rule.candidate_accept、
    rule.candidate_reject、gate.run_check、rule.seed_bootstrap、guardrail.add_rule。
    （step0 清单曾把 restore_comment / candidate_reject 归入类 2，系按 handler
    家族静态归类；step2 机械复验确认二者 SQL 不消费 workspace_id。）
  - NF1 已知缺陷锚 ×1 → **已由 NF1 卡 T-1789436398881-877c169c 承接迁移为正例**
    （test_gate_resolve_findings_nf1_fixed）：修复 = handler SQL 子查询改
    task_workspace_bindings（C-19 handle_gc_audit_get 先例同款）；夹具补种子
    四行（workspace_authority_captures + tasks + task_workspace_bindings +
    task_gate_decisions，见 _seed_gate_decision）。
  - NF2 已知缺陷锚 ×1 → **已由 NF2 卡 T-1789436399100-948b9498 承接迁移为正例**
    （test_summary_generate_nf2_fixed）：`summary.generate` 的 upsert
    `ON CONFLICT(symbol_hash) DO UPDATE` 在权威 schema（db/schema.py）上无匹配
    PRIMARY KEY/UNIQUE 约束（symbol_summaries 仅非唯一索引 idx_summaries_hash）
    → 一旦 workspace 存在函数符号即 internal_error。修复前该缺陷被路由缺陷
    **双重掩盖**（WHERE 代理 id 恒空 → 从不触达 upsert，静默返回 0）；路由修复
    后真实暴露。修复 = handler 弃 upsert，按 db/db_summary.py generate_summary
    版本化语义重写（UPDATE is_current=0 → version=MAX+1 → INSERT，整批
    unchecked_transaction）；测试用两次调用构造多版本断言。

隔离负例：未知 workspace_instance_id → not_found、他主 workspace → forbidden
（ACL 门禁不受路由修复影响）；edit.revert 缺失 id / 未知符号 → fail-closed 错误码保持。

二进制选择同 C-16/C-17 规则：`CW_DAEMON_BIN` 显式优先（CI/验收必须指向当次构建产物）。
夹具打印实际二进制路径 + SHA256 供证据引用。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from callwarden.db.db_daemon import WORKSPACE_REGISTRY_DDL  # noqa: E402
from callwarden.db.schema import SCHEMA_INDEXES_SQL, SCHEMA_TABLES_SQL  # noqa: E402
from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="隔离 daemon harness 依赖 Windows 进程/HTTP 传输"
)

# ---- 隔离夹具常量 -------------------------------------------------------

WS_ID = 1                           # 物理库 workspaces.id（真 id）
PROXY_ID = 2                        # registry 代理 ROWID（dummy 行占掉 1 后，真实行 = 2）
INSTANCE_ID = "ws-c21-iso"          # 真实 workspace_instance_id（owner 正确）
INSTANCE_DUMMY = "ws-c21-dummy"     # dummy 行（占 rowid=1；root 不匹配）
INSTANCE_FOREIGN = "ws-c21-foreign" # 他主行（ACL 负向）
FOREIGN_UID = 12345
# Windows HTTP 合成 local-owner peer：PeerCredential::new_windows → uid = u32::MAX
PEER_UID = 4294967295 if sys.platform == "win32" else os.getuid()

# ROOT 必须是真实存在的可写目录（rule.insert_agents_md_block 会向 root 写文件）；
# 由模块夹具用 tmp_path_factory 建目录后赋值（registry client_view_root 与物理库
# workspaces.root_path 逐字同值 → 规范化必命中）。
ROOT_STR = ""

FI_ID = 601                          # 活跃 file_instances（symbols / semgrep_findings 的 FK 目标）
FI_ARCHIVED = 602                    # 归档 file_instances（restore_all_comments 判别目标）
SYM_A_ID = 901                       # symbols 行（propose_symbol_id_patch / summary.generate）
SYM_A_HASH = "c21-sym-a"
SYM_B_HASH = "c21-sym-b"
CONTENT_HASH = "c21-content-hash"
AUDIT_SEED_ID = 8801                 # 预置 file_edit_audit 行（edit.revert 判别目标）
RULE_X = "c21-rule-x"                # extract_candidates 的 semgrep rule_id（≥2 次命中）


def _seed_registry(registry_db: str, root: str) -> None:
    """registry：dummy(1) → real(2) → foreign。PROXY_ID≠WS_ID 是判别力来源。"""
    conn = sqlite3.connect(registry_db, timeout=15)
    try:
        conn.executescript(WORKSPACE_REGISTRY_DDL)
        now = time.time()
        for inst, r, uid in [
            (INSTANCE_DUMMY, "C:/c21-dummy-root", PEER_UID),
            (INSTANCE_ID, root, PEER_UID),
            (INSTANCE_FOREIGN, root, FOREIGN_UID),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO daemon_workspaces "
                "(workspace_instance_id, snapshot_id, owner_uid, git_remote_url, "
                " git_head_commit_sha, client_view_root, host_real_root, "
                " toolchain_fingerprint, registered_at, last_active_at, status) "
                "VALUES (?1, ?2, ?3, '', '', ?4, ?4, '', ?5, ?5, 'active')",
                (inst, f"snap-{inst}", uid, r, now),
            )
        conn.commit()
        row = conn.execute(
            "SELECT workspace_id FROM daemon_workspaces WHERE workspace_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()
        assert row and row[0] == PROXY_ID, f"registry 代理 ROWID 应为 {PROXY_ID}，实得 {row}"
    finally:
        conn.close()


def _seed_codegraph(db_path: str, root: str) -> None:
    """物理库（edit/rule handler 的 conn）：权威 schema + C-21 域夹具。

    关键：`workspaces` 只有 id=1 一行（root_path=ROOT）。旧实现拿代理 id=2
    做 FK/WHERE/查根 → 全部失败或静默空；新实现按 root_path 命中 1。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.executescript(SCHEMA_TABLES_SQL)
        conn.executescript(SCHEMA_INDEXES_SQL)
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?1, ?2, ?3, ?4, 1)",
            (WS_ID, "c21-isolated-ws", root, now),
        )
        # file_contents / file_instances（FK 链 + restore_all_comments 的 archived 行）
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, first_seen_at) VALUES (?1, ?2)",
            (CONTENT_HASH, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO file_instances "
            "(id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
            "VALUES (?1, ?2, 'src/c21_main.rs', ?3, ?4, ?5, 'active')",
            (FI_ID, WS_ID, f"{root}/src/c21_main.rs", CONTENT_HASH, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO file_instances "
            "(id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
            "VALUES (?1, ?2, 'src/c21_archived.rs', ?3, ?4, ?5, 'archived')",
            (FI_ARCHIVED, WS_ID, f"{root}/src/c21_archived.rs", CONTENT_HASH, now),
        )
        # symbol_contents / symbols（propose_symbol_* / summary.generate 判别目标）
        for sid, h, name, sig, qname in [
            (SYM_A_ID, SYM_A_HASH, "c21_fn_a", "fn c21_fn_a()", "c21::fn_a"),
            (SYM_A_ID + 1, SYM_B_HASH, "c21_fn_b", "", "c21::fn_b"),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content) "
                "VALUES (?1, ?2, 'function', 'fn placeholder() {}')",
                (h, name),
            )
            conn.execute(
                "INSERT OR REPLACE INTO symbols "
                "(id, file_instance_id, symbol_hash, name, kind, start_line, end_line, "
                " signature, module_path, qualified_name) "
                "VALUES (?1, ?2, ?3, ?4, 'function', 1, 10, ?5, 'c21', ?6)",
                (sid, FI_ID, h, name, sig, qname),
            )
        # semgrep_findings（extract_candidates：同一 rule_id ≥2 次命中）
        for i in (1, 2):
            conn.execute(
                "INSERT OR REPLACE INTO semgrep_findings "
                "(id, file_instance_id, content_hash, rule_id, rule_name, severity, "
                " start_line, end_line, scanned_at) "
                "VALUES (?1, ?2, ?3, ?4, 'C21 rule x', 'WARNING', ?5, ?5, ?6)",
                (910 + i, FI_ID, f"c21-finding-{i}", RULE_X, i, now),
            )
        conn.commit()
        n_ws = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        assert n_ws == 1, f"隔离物理库 workspaces 应只有 1 行，实得 {n_ws}"
        n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        assert n_sym == 2, f"预置 2 个 symbols，实得 {n_sym}"
    finally:
        conn.close()


def _seed_task_db(db_path: str, root: str) -> None:
    """task collab 库（CW_DAEMON_TASK_DB）：daemon 启动需完整 schema。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.executescript(SCHEMA_TABLES_SQL)
        conn.executescript(SCHEMA_INDEXES_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?1, 'c21-taskdb-ws', ?2, ?3, 1)",
            (WS_ID, root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_edit_audit(db_path: str) -> None:
    """测试内种 file_edit_audit 预置行（edit.revert 判别目标；rw 连接）。

    旧实现 revert 走 `WHERE id=? AND workspace_id=代理2` → 恒不命中 →
    edit_not_found；新实现命中真 id 行。
    """
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO file_edit_audit "
            "(id, workspace_id, file_path, operation, file_hash_before, file_hash_after, "
            " symbol_hash, agent_task_id, diff_summary, status, created_at) "
            "VALUES (?1, ?2, 'src/c21_main.rs', 'edit', 'b', 'a', '', 'c21-test', "
            "'seeded proposed edit', 'proposed', ?3)",
            (AUDIT_SEED_ID, WS_ID, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_gate_decision(db_path: str) -> None:
    """NF1 锚种子（NF1 卡 T-1789436398881-877c169c）。

    修复后的 resolve_findings 经 task_workspace_bindings 子查询做 workspace
    作用域（C-19 handle_gc_audit_get 先例同款），须预置绑定 task 的 gate
    decision 行才能命中。四行种子满足 daemon 侧 FK（bindings 复合 FK →
    workspace_authority_captures）：captures + tasks + bindings + decision。
    """
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspace_authority_captures "
            "(workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id, "
            " daemon_workspace_id, workspace_instance_id, capture_canonicalization_version, "
            " capture_canonicalization_rules_hash, registry_identity_payload_json, "
            " registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash, "
            " client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at) "
            "VALUES ('cap-c21-nf1', ?1, 1, NULL, ?1, 'ws-c21-iso', 'v1', 'hash-seed', '{}', "
            " 'hash-seed', '{}', 'hash-seed', 'hash-seed', 'hash-seed', 'nf1-seed', ?2)",
            (WS_ID, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES ('T-C21-NF1', 'c21 nf1 seed', 'open', ?1, ?1)",
            (now,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, "
            " created_by, authoritative_created_at) "
            "VALUES ('T-C21-NF1', ?1, 'wb-c21-nf1', 'cap-c21-nf1', 'nf1-seed', ?2)",
            (WS_ID, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_gate_decisions "
            "(decision_id, task_id, contract_id, contract_revision, contract_hash, "
            " decision, reason, decision_time) "
            "VALUES ('GATE-c21-nf1', 'T-C21-NF1', 'TC-c21-nf1', 1, '', "
            " 'pending', 'c21-nf1-seed', ?1)",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def _find_daemon_binary():
    """定位当次构建的 cw-daemon；`CW_DAEMON_BIN` 显式覆盖优先（同 C-16/C-17 规则）。"""
    candidates = [
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join(_REPO_ROOT, "rust_ext", "target-c21", "release", "cw-daemon.exe"),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join(
            _REPO_ROOT, "rust_ext", "target", "stage-refresh", "release", "cw-daemon.exe"
        ),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe"),
        os.path.join(_REPO_ROOT, "runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _spawn_isolated_daemon(bin_path: str, data_root: str, home_dir: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # codegraph 库 = USERPROFILE/.callwarden/callwarden.db（default_codegraph_db_path）
    env["USERPROFILE"] = home_dir
    return subprocess.Popen(
        [bin_path, "--http-bind=127.0.0.1:0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_manifest(data_root: str, proc: subprocess.Popen, timeout: float = 60.0):
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for name in os.listdir(manifest_dir):
                if name.startswith("http-daemon.") and name.endswith(".manifest.json"):
                    try:
                        m = json.loads(
                            open(os.path.join(manifest_dir, name), encoding="utf-8").read()
                        )
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="module")
def c21_daemon(tmp_path_factory):
    """启动隔离 daemon，yield (client, codegraph_db, bin_path, bin_sha256)。"""
    global ROOT_STR
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")

    bin_sha = hashlib.sha256(open(bin_path, "rb").read()).hexdigest().upper()
    root_dir = tmp_path_factory.mktemp("c21-ws-root")
    ROOT_STR = str(root_dir)
    data_root = str(tmp_path_factory.mktemp("c21") / "data")
    os.makedirs(data_root, exist_ok=True)
    home_dir = os.path.join(data_root, "userhome")
    task_db = os.path.join(data_root, "task.db")
    registry_db = os.path.join(data_root, "registry.db")
    codegraph_db = os.path.join(home_dir, ".callwarden", "callwarden.db")

    # 全部 seed 在 daemon 启动**之前**（registry / 库均为 CREATE IF NOT EXISTS 语义）
    _seed_task_db(task_db, ROOT_STR)
    _seed_registry(registry_db, ROOT_STR)
    _seed_codegraph(codegraph_db, ROOT_STR)
    _seed_edit_audit(codegraph_db)
    _seed_gate_decision(codegraph_db)

    proc = _spawn_isolated_daemon(bin_path, data_root, home_dir)
    try:
        manifest = _wait_manifest(data_root, proc)
        if manifest is None:
            err = b""
            try:
                fd = proc.stderr.fileno()
                os.set_blocking(fd, False)
                err = os.read(fd, 65536)
            except (BlockingIOError, OSError, ValueError):
                pass
            pytest.fail(
                "隔离 daemon 未发布 manifest\n"
                f"returncode={proc.poll()}\nbinary={bin_path}\n"
                f"stderr={err.decode('utf-8', 'replace')}"
            )
        endpoint = manifest["endpoint"]
        assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"

        print(f"[C-21 harness] bin={bin_path}")
        print(f"[C-21 harness] bin_sha256={bin_sha}")
        print(f"[C-21 harness] endpoint={endpoint}")
        print(f"[C-21 harness] codegraph_db={codegraph_db}")
        print(f"[C-21 harness] root={ROOT_STR}")

        client = HttpDaemonRpcClient(
            endpoint=endpoint, verify_health=False, validate_manifest=False
        )
        yield client, codegraph_db, bin_path, bin_sha
    finally:
        _terminate(proc)


def _edit(client, method: str, params: dict):
    """调用 edit/rule 写面方法；返回 (result, error) 二者其一为 None。"""
    try:
        return client.call(method, {"workspace_instance_id": INSTANCE_ID, **params}), None
    except DaemonRemoteError as exc:  # noqa: BLE001
        return None, exc


def _rows(db: str, sql: str, args=()) -> list:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# ACL 门禁（fail-closed 不受修复影响，须保持）
# ----------------------------------------------------------------------

def test_acl_unknown_workspace_fail_closed(c21_daemon):
    """未知 workspace_instance_id → workspace_not_found（不静默）。"""
    client, _, _, _ = c21_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("edit.propose", {
            "workspace_instance_id": "ws-c21-nope",
            "file_path": "src/x.rs", "new_content": "x",
        })
    assert "not_found" in ei.value.code.lower(), f"实得 code={ei.value.code}"


def test_acl_foreign_owner_rejected(c21_daemon):
    """他主 workspace → workspace_forbidden（owner_uid 门禁保持）。"""
    client, _, _, _ = c21_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("edit.propose", {
            "workspace_instance_id": INSTANCE_FOREIGN,
            "file_path": "src/x.rs", "new_content": "x",
        })
    assert "forbidden" in ei.value.code.lower(), f"实得 code={ei.value.code}"


# ----------------------------------------------------------------------
# 类 1：FK 表写入（修复前恒 FOREIGN KEY constraint failed，代理 2 不在 workspaces(id) 取值域）
# ----------------------------------------------------------------------

def test_edit_propose_writes_real_id_fk(c21_daemon):
    """edit.propose：修复后 FK 通过 → file_edit_audit 落库 workspace_id=1。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.propose", {
        "file_path": "src/c21_main.rs", "new_content": "fn c21_new() {}",
        "operation": "edit",
    })
    assert err is None, f"修复后 FK 应通过，实得 {err}"
    assert res.get("ok") is True and res.get("status") == "proposed"
    rows = _rows(
        cg,
        "SELECT workspace_id, file_path, operation, status FROM file_edit_audit WHERE id=?",
        (res["edit_id"],),
    )
    assert rows, "必须落库一行 file_edit_audit"
    assert rows[0] == (WS_ID, "src/c21_main.rs", "edit", "proposed"), f"实得 {rows}"


def test_edit_propose_range_patch_writes_real_id_fk(c21_daemon):
    """edit.propose_range_patch：修复后 FK 通过 → 落库 operation=range_patch。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.propose_range_patch", {
        "file_path": "src/c21_main.rs", "start_line": 10, "end_line": 20,
        "new_content": "patched range",
    })
    assert err is None, f"修复后 FK 应通过，实得 {err}"
    assert res.get("ok") is True and res.get("status") == "proposed"
    assert (res.get("start_line"), res.get("end_line")) == (10, 20)
    rows = _rows(
        cg, "SELECT workspace_id, operation, status FROM file_edit_audit WHERE id=?",
        (res["edit_id"],),
    )
    assert rows and rows[0] == (WS_ID, "range_patch", "proposed"), f"实得 {rows}"


def test_edit_record_token_savings_writes_real_id_fk(c21_daemon):
    """edit.record_token_savings：修复后 FK 通过 → token_savings_ledger 落库真 id + 金额正确。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.record_token_savings", {
        "operation": "rag_context", "original_tokens": 1000, "actual_tokens": 400,
        "detail": "c21-test",
    })
    assert err is None, f"修复后 FK 应通过，实得 {err}"
    assert res.get("ok") is True
    assert res.get("tokens_saved") == 600, f"实得 {res}"
    assert res.get("savings_pct") == 60.0, f"实得 {res}"
    rows = _rows(
        cg,
        "SELECT workspace_id, operation, original_tokens, actual_tokens, tokens_saved "
        "FROM token_savings_ledger WHERE detail='c21-test'",
    )
    assert rows, "必须落库一行 token_savings_ledger"
    assert rows[0] == (WS_ID, "rag_context", 1000, 400, 600), f"实得 {rows}"


# ----------------------------------------------------------------------
# 类 2：WHERE 过滤 / 查根（修复前恒不命中 → edit_not_found / symbol_not_found / restored=0 / 生成 0）
# ----------------------------------------------------------------------

def test_edit_revert_hits_real_id_row(c21_daemon):
    """edit.revert：修复后 WHERE workspace_id=1 命中预置行 → 状态翻转 reverted。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.revert", {"edit_id": AUDIT_SEED_ID})
    assert err is None, f"修复后应命中预置行（代理 2 恒 edit_not_found），实得 {err}"
    assert res.get("ok") is True and res.get("status") == "reverted"
    rows = _rows(
        cg, "SELECT workspace_id, status FROM file_edit_audit WHERE id=?",
        (AUDIT_SEED_ID,),
    )
    assert rows and rows[0] == (WS_ID, "reverted"), f"实得 {rows}"


def test_edit_revert_missing_edit_fails_closed(c21_daemon):
    """缺失 edit_id → edit_not_found（fail-closed 语义不受修复影响）。"""
    client, _, _, _ = c21_daemon
    _, err = _edit(client, "edit.revert", {"edit_id": 999999})
    assert err is not None and "not_found" in err.code.lower(), f"实得 {err}"


def test_edit_propose_symbol_id_patch_resolves_symbol(c21_daemon):
    """edit.propose_symbol_id_patch：修复后 JOIN fi.workspace_id=1 命中 symbols 901。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.propose_symbol_id_patch", {
        "symbol_id": SYM_A_ID, "new_content": "fn c21_fn_a_patched() {}",
    })
    assert err is None, f"修复后应命中符号（代理 2 恒 symbol_not_found），实得 {err}"
    assert res.get("ok") is True and res.get("symbol_id") == SYM_A_ID
    rows = _rows(
        cg, "SELECT workspace_id, symbol_hash, operation FROM file_edit_audit WHERE id=?",
        (res["edit_id"],),
    )
    assert rows and rows[0] == (WS_ID, SYM_A_HASH, "symbol_id_patch"), f"实得 {rows}"


def test_edit_propose_symbol_patch_resolves_symbol(c21_daemon):
    """edit.propose_symbol_patch：修复后按 qualified_name JOIN 真 id 命中。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.propose_symbol_patch", {
        "qualified_name": "c21::fn_b", "new_content": "fn c21_fn_b_patched() {}",
    })
    assert err is None, f"修复后应命中符号（代理 2 恒 symbol_not_found），实得 {err}"
    assert res.get("ok") is True
    rows = _rows(
        cg, "SELECT workspace_id, symbol_hash, operation FROM file_edit_audit WHERE id=?",
        (res["edit_id"],),
    )
    assert rows and rows[0] == (WS_ID, SYM_B_HASH, "symbol_patch"), f"实得 {rows}"


def test_edit_propose_symbol_patch_unknown_symbol_fails_closed(c21_daemon):
    """未知符号 → symbol_not_found（fail-closed 语义保持）。"""
    client, _, _, _ = c21_daemon
    _, err = _edit(client, "edit.propose_symbol_patch", {
        "qualified_name": "c21::nope", "new_content": "x",
    })
    assert err is not None and "not_found" in err.code.lower(), f"实得 {err}"


def test_edit_restore_all_comments_restores_archived(c21_daemon):
    """edit.restore_all_comments：修复后 fi.workspace_id=1 命中 archived 行 → restored=1。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.restore_all_comments", {"file_path": "src/c21_archived.rs"})
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True
    assert res.get("restored_files") == 1, f"修复前恒 0（代理 2 不命中），实得 {res}"
    rows = _rows(cg, "SELECT status FROM file_instances WHERE id=?", (FI_ARCHIVED,))
    assert rows and rows[0] == ("parsed",), f"归档行必须被恢复为 parsed，实得 {rows}"


def test_rule_extract_candidates_extracts_from_real_scope(c21_daemon):
    """rule.extract_candidates：修复后 file_instances 子查询命中 → 提取 ≥1 候选。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "rule.extract_candidates", {"source": "quality_findings"})
    assert err is None, f"实得 {err}"
    assert res.get("candidates_extracted", 0) >= 1, f"修复前恒 0，实得 {res}"
    rows = _rows(
        cg,
        "SELECT title, source, status FROM agent_rule_candidates WHERE title=?",
        (f"semgrep:{RULE_X}",),
    )
    assert rows and rows[0] == (f"semgrep:{RULE_X}", "quality_findings", "pending"), f"实得 {rows}"


def test_rule_insert_agents_md_block_writes_root_file(c21_daemon):
    """rule.insert_agents_md_block：修复后 workspace_root 命中真根 → 写文件 + 同步日志。

    旧实现 workspace_root(代理 2) → QueryReturnedNoRows → internal_error。
    """
    client, cg, _, _ = c21_daemon
    target_path = "AGENTS-c21-block.md"
    res, err = _edit(client, "rule.insert_agents_md_block", {
        "content": "c21 insert block body", "target_path": target_path,
    })
    assert err is None, f"修复后应成功（旧实现查根失败），实得 {err}"
    assert res.get("ok") is True and res.get("target_path") == target_path
    assert res.get("bytes_written", 0) > 0
    written = open(os.path.join(ROOT_STR, target_path), encoding="utf-8").read()
    assert "<!-- cw-agent-rules:start -->" in written and "c21 insert block body" in written
    rows = _rows(
        cg, "SELECT target_path, actor, dry_run FROM agent_rule_sync_log "
            "WHERE actor='rule_insert_agents_md_block'",
    )
    assert rows and rows[0][0] == target_path and rows[0][2] == 0, f"实得 {rows}"


def test_rule_sync_agents_md_uses_real_root(c21_daemon):
    """rule.sync_agents_md：修复后 workspace_root 命中真根 → dry_run 成功 + 日志落库。

    旧实现 workspace_root(代理 2) → QueryReturnedNoRows → internal_error。
    先 seed_bootstrap（不可判别正例）保证 agent_rules 有 3 条 active。
    """
    client, cg, _, _ = c21_daemon
    seed_res, seed_err = _edit(client, "rule.seed_bootstrap", {"source": "seed"})
    assert seed_err is None and seed_res.get("seeded") == 3, f"seed_bootstrap 实得 {seed_res} {seed_err}"
    res, err = _edit(client, "rule.sync_agents_md", {"dry_run": True})
    assert err is None, f"修复后应成功（旧实现查根失败），实得 {err}"
    assert res.get("success") is True and res.get("dry_run") is True
    assert res.get("rule_count") == 3, f"实得 {res}"
    rows = _rows(
        cg, "SELECT target_path, dry_run, actor FROM agent_rule_sync_log "
            "WHERE actor='rule_sync_agents_md'",
    )
    assert rows and rows[0][1] == 1, f"实得 {rows}"


# ----------------------------------------------------------------------
# 不可判别正例（SQL 未消费 workspace_id；新旧实现均通过；卡面指定 + 连通性锚）
# ----------------------------------------------------------------------

def test_edit_restore_comment_writes_comments(c21_daemon):
    """edit.restore_comment（不可判别）：comments 表无 workspace_id 列，handler
    `let _ = workspace_id;`。保留为写面连通性正例 + 落库读回。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "edit.restore_comment", {
        "symbol_hash": SYM_A_HASH, "comment_type": "doc", "content": "c21 restored comment",
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("symbol_hash") == SYM_A_HASH
    rows = _rows(
        cg, "SELECT comment_type, content FROM comments WHERE symbol_hash=? AND content=?",
        (SYM_A_HASH, "c21 restored comment"),
    )
    assert rows and rows[0] == ("doc", "c21 restored comment"), f"实得 {rows}"


def test_rule_candidate_create_writes_candidate(c21_daemon):
    """rule.candidate_create（卡面指定正例；不可判别）：agent_rule_candidates 无
    workspace_id 列。创建后按返回 candidate_id 读回 status=pending。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "rule.candidate_create", {
        "rule": "禁止在库代码中使用 print 调试输出", "title": "c21-no-print",
        "severity": "warning",
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("candidate_id", "").startswith("cand-")
    assert res.get("status") == "pending"
    rows = _rows(
        cg, "SELECT title, rule_text, source, status FROM agent_rule_candidates WHERE id=?",
        (res["candidate_id"],),
    )
    assert rows and rows[0] == ("c21-no-print", "禁止在库代码中使用 print 调试输出",
                                "manual", "pending"), f"实得 {rows}"


def test_rule_candidate_accept_links_rule(c21_daemon):
    """rule.candidate_accept（不可判别）：按 id 更新候选 + 写 agent_rules（无 workspace_id）。"""
    client, cg, _, _ = c21_daemon
    created, cerr = _edit(client, "rule.candidate_create", {
        "rule": "禁止裸 except", "title": f"c21-accept-{time.time_ns()}",
    })
    assert cerr is None, f"实得 {cerr}"
    cid = created["candidate_id"]
    res, err = _edit(client, "rule.candidate_accept", {"candidate_id": cid})
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("status") == "accepted"
    rows = _rows(
        cg, "SELECT status, reviewer FROM agent_rule_candidates WHERE id=?", (cid,),
    )
    assert rows and rows[0] == ("accepted", "daemon"), f"实得 {rows}"
    linked = _rows(
        cg, "SELECT title, status, source_candidate_id FROM agent_rules "
            "WHERE source_candidate_id=?",
        (cid,),
    )
    assert linked and linked[0][2] == cid and linked[0][1] == "active", f"实得 {linked}"


def test_rule_candidate_reject_rejects_pending(c21_daemon):
    """rule.candidate_reject（不可判别）：UPDATE 仅按 id + status 过滤（无 workspace_id）。
    step0 曾按家族归入类 2，机械复验确认新旧实现均通过 —— 如实标注。"""
    client, cg, _, _ = c21_daemon
    created, cerr = _edit(client, "rule.candidate_create", {
        "rule": "拒绝测试规则", "title": f"c21-reject-{time.time_ns()}",
    })
    assert cerr is None, f"实得 {cerr}"
    cid = created["candidate_id"]
    res, err = _edit(client, "rule.candidate_reject", {"candidate_id": cid, "reason": "c21"})
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("status") == "rejected"
    rows = _rows(cg, "SELECT status FROM agent_rule_candidates WHERE id=?", (cid,))
    assert rows and rows[0] == ("rejected",), f"实得 {rows}"


def test_gate_run_check_writes_decision(c21_daemon):
    """gate.run_check（不可判别）：task_gate_decisions 写入不消费 workspace_id。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "gate.run_check", {
        "task_id": "T-C21-GATE", "contract_id": "TC-c21", "contract_revision": 1,
        "config": "c21-config",
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("decision_id", "").startswith("GATE-")
    assert res.get("decision") == "pass"
    rows = _rows(
        cg, "SELECT task_id, contract_id, contract_revision, decision FROM task_gate_decisions "
            "WHERE decision_id=?",
        (res["decision_id"],),
    )
    assert rows and rows[0] == ("T-C21-GATE", "TC-c21", 1, "pass"), f"实得 {rows}"


def test_guardrail_add_rule_writes_rule(c21_daemon):
    """guardrail.add_rule（卡面指定正例；不可判别）：guardrail_rules 无 workspace_id 列。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "guardrail.add_rule", {
        "rule": "c21-guard-*", "category": "security", "severity": "warning",
        "action": "block", "description": "c21 guardrail",
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True and res.get("action") == "block"
    rows = _rows(
        cg, "SELECT category, severity, pattern, action FROM guardrail_rules WHERE pattern=?",
        ("c21-guard-*",),
    )
    assert rows and rows[0] == ("security", "warning", "c21-guard-*", "block"), f"实得 {rows}"


# ----------------------------------------------------------------------
# NF1 / NF2 缺陷锚（均已由独立缺陷卡承接迁移为正例：NF1 卡
# T-1789436398881-877c169c、NF2 卡 T-1789436399100-948b9498）
# ----------------------------------------------------------------------

def test_summary_generate_nf2_fixed(c21_daemon):
    """NF2 锚已迁移为正例（NF2 卡 T-1789436399100-948b9498，2026-09-15）：
    修复 = handle_summary_generate 弃用 `ON CONFLICT(symbol_hash)` upsert
    （symbol_summaries 无任何 UNIQUE/PK 约束 → 真 prepare 失败，缺陷期本测试
    err=internal_error），改按 db/db_summary.py generate_summary 版本化语义
    重写（UPDATE is_current=0 → version=MAX+1 → INSERT，整批
    unchecked_transaction）。两次调用构造多版本：第 2 次调用须把第 1 版
    is_current 压 0，并插入 version=2、is_current=1 的新行（旧行为语义下单表
    恒单行单版本，双版本断言对回归有判别力）。"""
    client, cg, _, _ = c21_daemon
    res1, err1 = _edit(client, "summary.generate", {})
    assert err1 is None, f"NF2 修复失效（第 1 次调用）：实得 {err1}"
    assert res1.get("summaries_generated", 0) >= 2, f"实得 {res1}"
    rows1 = _rows(cg, "SELECT summary FROM symbol_summaries WHERE symbol_hash=?", (SYM_A_HASH,))
    assert rows1 and "c21_fn_a" in rows1[0][0], f"实得 {rows1}"
    # 第 2 次调用：版本化写路径 —— 旧版本压 0，新版本 = MAX+1
    res2, err2 = _edit(client, "summary.generate", {})
    assert err2 is None, f"NF2 修复失效（第 2 次调用）：实得 {err2}"
    rows2 = _rows(
        cg,
        "SELECT version, is_current, summary FROM symbol_summaries "
        "WHERE symbol_hash=? ORDER BY version",
        (SYM_A_HASH,),
    )
    assert len(rows2) == 2, f"应恰好两行（v1+v2），实得 {rows2}"
    assert rows2[0][:2] == (1, 0), f"v1 应被压为 is_current=0，实得 {rows2}"
    assert rows2[1][:2] == (2, 1), f"v2 应 version=2/is_current=1，实得 {rows2}"
    assert "c21_fn_a" in rows2[1][2], f"实得 {rows2}"


def test_gate_resolve_findings_nf1_fixed(c21_daemon):
    """NF1 锚已迁移为正例（NF1 卡 T-1789436398881-877c169c，2026-09-15）：
    修复 = handler SQL 子查询 tasks WHERE workspace_id → task_workspace_bindings
    （C-19 handle_gc_audit_get 先例同款，step0 裁决：同表 workspace_id 过滤因该列
    全部写入路径为 NULL 而排除）。种子见 _seed_gate_decision。NF2 锚
    （test_summary_generate_nf2_fixed）亦已由 NF2 卡
    T-1789436399100-948b9498 迁移为正例。"""
    client, cg, _, _ = c21_daemon
    res, err = _edit(client, "gate.resolve_findings", {
        "gate_id": "GATE-c21-nf1", "resolution": "resolved",
    })
    assert err is None, f"NF1 修复失效：实得 {err}"
    assert res.get("ok") is True, f"实得 {res}"
    rows = _rows(
        cg, "SELECT task_id, reason FROM task_gate_decisions "
            "WHERE decision_id='GATE-c21-nf1'",
    )
    assert rows and rows[0] == ("T-C21-NF1", "resolved"), f"实得 {rows}"
