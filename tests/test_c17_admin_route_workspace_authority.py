"""C-17 回归：admin 路由块 workspace 命名空间权威解析（真实隔离 daemon 功能矩阵）。

背景（本卡 `T-1789365537230-c3f02eb4`，承接 backlog §W18 / 交接文档卡 D）：

  - `rust_ext/src/daemon/snapshot_state.rs` 的 admin 路由块（guard 21 方法）原以
    `owned_workspace(...).workspace_id`（daemon registry `daemon_workspaces.workspace_id`
    的**代理 ROWID**）作为 handler 的 workspace 作用域，并用 `open_write(self, ws)`
    打开物理库（`USERPROFILE/.callwarden/callwarden.db`）。代理 ROWID 与物理库
    `workspaces.id` **不是同一命名空间**（生产实测：代理 207 vs 真 id 1）。
  - 后果（step0 逐 handler 实测）分三类：
      1. 写带 `FOREIGN KEY (workspace_id) REFERENCES workspaces(id)` 的表 →
         恒 `FOREIGN KEY constraint failed`（gc_archive_import / record_artifact_identity）；
      2. 仅作 WHERE 过滤 → 恒不命中、静默空/误报（gc_archive_inspect/list、gc_retention、
         snapshot_compare、clear_clones、branch_switch）；
      3. 写**无 FK** 的表 → 静默写入非法 workspace_id（branch_register、
         publish_interface、select_interface_provider）。
  - 修法（本卡 step1，路由层单文件）：改走同文件既有、semgrep 写面已采用的
    `open_codegraph_db_write` —— ACL 不变（owned_workspace），再按
    `client_view_root` 规范化匹配物理库 `workspaces.root_path` 取真 id。

判别力设计（隔离环境内复现生产「代理≠真 id」分裂）：

  生产上代理 ROWID(207) ≠ 真 id(1)；隔离 registry 全新时 AUTOINCREMENT 从 1 起，
  会与 `workspaces.id=1` 意外重合、使旧实现「碰巧通过」。因此夹具先插一条
  **dummy** daemon_workspaces 行（rowid=1，client_view_root 不匹配 ROOT），再插
  真实行（rowid=2 = PROXY_ID）。旧实现必然解析出 2 → FK 失败 / WHERE 不命中 /
  写入 2；新实现解析 client_view_root → 1。所有落库断言同时锚定
  `workspace_id == 1`。

已知缺陷回归锚（C-17 卡不修；转绿须由独立缺陷卡承接）：
  - F1 `admin.gc_audit_get/list` 引用不存在的 `tasks.workspace_id` → prepare 失败；
    **已由 C-19 卡 `T-1789392878852-bb9bef18` 承接修复**（改经 task_workspace_bindings
    join 作用域），本文件 F1 锚相应迁移为正确行为正例 + 隔离负例（见 §F1 区段）；
  - F2 `admin.record_action_identity` 缺 NOT NULL 列 `action_identities.action_id`；
    **已由 C-20 卡 `T-1789397153198-ee7baf18` 承接修复**（调用方入参优先、为空时
    Rust 生成 `ACT-<hex16>`），本文件 F2 锚迁移为正例（见 §F2/F3 区段）；
  - F3 `admin.register_attestation_revocation` 缺 NOT NULL 列 `revocation_id`；
    **同由 C-20 承接修复**（内部生成 `REV-<hex16>`，镜像 Python
    `db/db_task_identity.py:609`），F3 锚同样迁移为正例。

  至此 §W20 F1/F2/F3 三处 known-defect 锚均已转绿（C-19 → F1，C-20 → F2/F3）。

二进制选择与 C-16 同规则：`CW_DAEMON_BIN` 显式优先（CI/验收必须指向当次构建产物）。
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

ROOT = "C:/c17-iso-root"            # client_view_root / workspaces.root_path（逐字一致 → 规范化必命中）
WS_ID = 1                           # 物理库 workspaces.id（真 id）
PROXY_ID = 2                        # registry 代理 ROWID（dummy 行占掉 1 后，真实行 = 2）
INSTANCE_ID = "ws-c17-iso"          # 真实 workspace_instance_id（owner 正确）
INSTANCE_DUMMY = "ws-c17-dummy"     # dummy 行（占 rowid=1；root 不匹配）
INSTANCE_FOREIGN = "ws-c17-foreign" # 他主行（ACL 负向）
FOREIGN_UID = 12345
# Windows HTTP 合成 local-owner peer：PeerCredential::new_windows → uid = u32::MAX
PEER_UID = 4294967295 if sys.platform == "win32" else os.getuid()

TASK_C17 = "T-C17-DOC"              # 绑定到 WS_ID 的 task（assignment_create/revoke 用）
FI_ID = 501                         # 预置 file_instances 行（gc_archive_import 的 FK 目标）
SYMBOL_HASH = "c17-sym-hash"
CONTENT_HASH = "c17-content-hash"

# ---- C-19（§W20 F1）change_audit 域夹具 ---------------------------------
# change_audit.task_id 有 FK -> tasks(id)，unbound 负例同样先种 tasks 行；
# 种子经测试内 rw 连接插入（C-17 模块夹具零触碰：不新增 workspaces 行，
# 不破坏 n_ws==1 判别力断言）。外域 workspace 负例因 task_workspace_bindings
# 的复合 FK（workspace_authority_captures）需第二行 workspaces，与 n_ws==1
# 冲突，不种——join-miss 路径已由 unbound 负例覆盖（同一 SQL 分支）。
TASK_C19_UNBOUND = "T-C19-UNBOUND"          # 有 tasks 行、无 binding（join-miss 负例）
AUDIT_C19_BOUND = "CA-c19-bound-1"          # TASK_C17 的 change_audit 行（正例）
AUDIT_C19_UNBOUND = "CA-c19-unbound-1"      # TASK_C19_UNBOUND 的行（不可见负例）

# ---- C-20（§W20 F2+F3）identity / revocation 域夹具 --------------------
# 调用方显式提供的 action_id（非生成形态，用于验证「入参优先」与 UNIQUE 拒绝）
ACT_C20_EXPLICIT = "ACT-c20-explicit-id"


def _seed_change_audit(db_path: str) -> None:
    """测试内种 change_audit 域夹具（rw 连接；daemon 持有的同一物理库）。"""
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?1, 'C-19 unbound task', 'open', ?2, ?2)",
            (TASK_C19_UNBOUND, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO change_audit "
            "(id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp) "
            "VALUES (?1, ?2, NULL, 'src/f1.rs', 'b1', 'a1', 'diff-bound', 'c19-test', ?3)",
            (AUDIT_C19_BOUND, TASK_C17, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO change_audit "
            "(id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp) "
            "VALUES (?1, ?2, NULL, 'src/unbound.rs', 'b2', 'a2', 'diff-unbound', 'c19-test', ?3)",
            (AUDIT_C19_UNBOUND, TASK_C19_UNBOUND, now),
        )
        conn.commit()
    finally:
        conn.close()


def _find_daemon_binary():
    """定位当次构建的 cw-daemon；`CW_DAEMON_BIN` 显式覆盖优先（同 C-16 规则）。"""
    candidates = [
        os.environ.get("CW_DAEMON_BIN", ""),
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


def _seed_registry(registry_db: str) -> None:
    """registry：dummy(1) → real(2) → foreign。PROXY_ID≠WS_ID 是判别力来源。"""
    conn = sqlite3.connect(registry_db, timeout=15)
    try:
        conn.executescript(WORKSPACE_REGISTRY_DDL)
        now = time.time()
        for inst, root, uid in [
            (INSTANCE_DUMMY, "C:/c17-dummy-root", PEER_UID),
            (INSTANCE_ID, ROOT, PEER_UID),
            (INSTANCE_FOREIGN, ROOT, FOREIGN_UID),
        ]:
            conn.execute(
                "INSERT OR REPLACE INTO daemon_workspaces "
                "(workspace_instance_id, snapshot_id, owner_uid, git_remote_url, "
                " git_head_commit_sha, client_view_root, host_real_root, "
                " toolchain_fingerprint, registered_at, last_active_at, status) "
                "VALUES (?1, ?2, ?3, '', '', ?4, ?4, '', ?5, ?5, 'active')",
                (inst, f"snap-{inst}", uid, root, now),
            )
        conn.commit()
        row = conn.execute(
            "SELECT workspace_id FROM daemon_workspaces WHERE workspace_instance_id = ?",
            (INSTANCE_ID,),
        ).fetchone()
        assert row and row[0] == PROXY_ID, f"registry 代理 ROWID 应为 {PROXY_ID}，实得 {row}"
    finally:
        conn.close()


def _seed_codegraph(db_path: str) -> None:
    """物理库（codegraph = admin handler 的 conn）：权威 schema + 域夹具。

    关键：`workspaces` 只有 id=1 一行（root_path=ROOT）。旧实现拿代理 id=2
    做 FK/WHERE/写值 → 全部失败或写脏；新实现按 root_path 命中 1。
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
            (WS_ID, "c17-isolated-ws", ROOT, now),
        )
        # tasks + binding（assignment_create/revoke 走 task_bound_workspace_id）
        conn.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, created_at, updated_at) "
            "VALUES (?1, 'C-17 isolated doc task', 'open', ?2, ?2)",
            (TASK_C17, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, "
            " created_by, authoritative_created_at) VALUES (?1, ?2, ?3, ?4, 'test', 0)",
            (TASK_C17, WS_ID, f"tb-{TASK_C17}", "wc-c17-iso"),
        )
        # file_contents / file_instances（FK 链 + gc_retention/snapshot_compare 的 active 计数）
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, first_seen_at) "
            "VALUES (?1, ?2)",
            (CONTENT_HASH, now),
        )
        for i in (1, 2):
            conn.execute(
                "INSERT OR REPLACE INTO file_instances "
                "(id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) "
                "VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active')",
                (FI_ID if i == 1 else FI_ID + 1, WS_ID, f"src/f{i}.rs",
                 f"{ROOT}/src/f{i}.rs", CONTENT_HASH, now),
            )
        # symbol_contents / symbols（snapshot_compare 的 symbols 计数）
        conn.execute(
            "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content) "
            "VALUES (?1, 'c17_fn', 'function', 'fn c17_fn() {}')",
            (SYMBOL_HASH,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO symbols "
            "(id, file_instance_id, symbol_hash, name, kind, start_line, end_line) "
            "VALUES (701, ?1, ?2, 'c17_fn', 'function', 1, 10)",
            (FI_ID, SYMBOL_HASH),
        )
        # 预置一条旧归档（gc_retention 的 archived_eligible；snapshot_compare 的 archived）
        conn.execute(
            "INSERT OR REPLACE INTO archived_files "
            "(id, file_instance_id, workspace_id, rel_path, abs_path, content_hash, "
            " symbol_count, call_count, archive_reason, archived_at) "
            "VALUES (601, ?1, ?2, 'src/old.rs', ?3, '', 0, 0, 'gc', ?4)",
            (FI_ID, WS_ID, f"{ROOT}/src/old.rs", now - 90 * 86400.0),
        )
        # clone_pairs（clear_clones 判别：真 id 行必须被删）
        conn.execute(
            "INSERT OR REPLACE INTO clone_pairs "
            "(id, workspace_id, symbol_a_id, symbol_b_id, clone_type, similarity, token_hash, detected_at) "
            "VALUES (801, ?1, 701, 701, 1, 0.95, 'c17-th', ?2)",
            (WS_ID, now),
        )
        # agent_rule_sync_log（cleanup_rule_sync_log 判别：旧记录被删）
        conn.execute(
            "INSERT OR REPLACE INTO agent_rule_sync_log "
            "(id, target_path, created_at) VALUES ('c17-log-1', 'AGENTS.md', ?)",
            (now - 86400.0,),
        )
        conn.commit()
        n_ws = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        assert n_ws == 1, f"隔离物理库 workspaces 应只有 1 行，实得 {n_ws}"
    finally:
        conn.close()


def _seed_task_db(db_path: str) -> None:
    """task collab 库（CW_DAEMON_TASK_DB）：daemon 启动需完整 schema。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.executescript(SCHEMA_TABLES_SQL)
        conn.executescript(SCHEMA_INDEXES_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?1, 'c17-taskdb-ws', ?2, ?3, 1)",
            (WS_ID, ROOT, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def c17_daemon(tmp_path_factory):
    """启动隔离 daemon，yield (client, codegraph_db, bin_path, bin_sha256)。"""
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")

    bin_sha = hashlib.sha256(open(bin_path, "rb").read()).hexdigest().upper()
    data_root = str(tmp_path_factory.mktemp("c17") / "data")
    os.makedirs(data_root, exist_ok=True)
    home_dir = os.path.join(data_root, "userhome")
    task_db = os.path.join(data_root, "task.db")
    registry_db = os.path.join(data_root, "registry.db")
    codegraph_db = os.path.join(home_dir, ".callwarden", "callwarden.db")

    # 全部 seed 在 daemon 启动**之前**（registry / 库均为 CREATE IF NOT EXISTS 语义）
    _seed_task_db(task_db)
    _seed_registry(registry_db)
    _seed_codegraph(codegraph_db)

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

        print(f"[C-17 harness] bin={bin_path}")
        print(f"[C-17 harness] bin_sha256={bin_sha}")
        print(f"[C-17 harness] endpoint={endpoint}")
        print(f"[C-17 harness] codegraph_db={codegraph_db}")

        client = HttpDaemonRpcClient(
            endpoint=endpoint, verify_health=False, validate_manifest=False
        )
        yield client, codegraph_db, bin_path, bin_sha
    finally:
        _terminate(proc)


def _admin(client, method: str, params: dict):
    """调用 `admin.*`；返回 (result, error) 二者其一为 None。"""
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

def test_acl_unknown_workspace_fail_closed(c17_daemon):
    """未知 workspace_instance_id → workspace_not_found（不静默）。"""
    client, _, _, _ = c17_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("admin.gc_policy_get", {"workspace_instance_id": "ws-c17-nope"})
    assert "not_found" in ei.value.code.lower(), f"实得 code={ei.value.code}"


def test_acl_foreign_owner_rejected(c17_daemon):
    """他主 workspace → workspace_forbidden（owner_uid 门禁保持）。"""
    client, _, _, _ = c17_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("admin.gc_policy_get", {"workspace_instance_id": INSTANCE_FOREIGN})
    assert "forbidden" in ei.value.code.lower(), f"实得 code={ei.value.code}"


# ----------------------------------------------------------------------
# 第 1 类：FK 表写入（修复前恒 FOREIGN KEY constraint failed）
# ----------------------------------------------------------------------

def test_gc_archive_import_fk_passes_with_real_id(c17_daemon):
    """gc_archive_import：修复后 workspace_id=1 在 FK 取值域内 → 落库成功。"""
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_archive_import", {
        "archive_path": f"{ROOT}/archive/f1", "file_instance_id": FI_ID,
        "rel_path": "src/f1.rs", "content_hash": CONTENT_HASH,
        "archive_reason": "c17-test",
    })
    assert err is None, f"修复后 FK 应通过，实得 {err}"
    assert res.get("ok") is True and res.get("inserted") == 1
    rows = _rows(cg, "SELECT workspace_id, rel_path FROM archived_files WHERE rel_path='src/f1.rs'")
    assert rows and rows[0][0] == WS_ID, f"落库 workspace_id 应为 {WS_ID}，实得 {rows}"


def test_record_artifact_identity_writes_real_id(c17_daemon):
    """record_artifact_identity：修复后 FK 通过 → 落库 workspace_id=1。"""
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.record_artifact_identity", {
        "artifact_id": "ART-c17-iso-1", "task_id": TASK_C17,
        "contract_id": "TC-c17", "contract_revision": 1,
        "artifact_type": "file", "artifact_ref": "src/f1.rs",
    })
    assert err is None, f"修复后应成功，实得 {err}"
    assert res.get("ok") is True
    rows = _rows(cg, "SELECT workspace_id, task_id FROM artifact_identities WHERE artifact_id='ART-c17-iso-1'")
    assert rows and rows[0][0] == WS_ID and rows[0][1] == TASK_C17, f"实得 {rows}"


# ----------------------------------------------------------------------
# 第 2 类：WHERE 过滤（修复前恒不命中 → 空/误报）
# ----------------------------------------------------------------------

def test_gc_archive_inspect_returns_row(c17_daemon):
    client, _, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_archive_inspect", {"archive_path": f"{ROOT}/src/old.rs"})
    assert err is None, f"实得 {err}"
    assert res is not None, "修复前恒 Null（WHERE 代理 id 不命中）；修复后必须命中预置行"


def test_gc_archive_list_nonempty(c17_daemon):
    client, _, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_archive_list", {"limit": 50})
    assert err is None, f"实得 {err}"
    assert isinstance(res, list) and len(res) >= 1, "修复前恒 []；修复后必须返回预置归档"


def test_gc_retention_counts_real_rows(c17_daemon):
    client, _, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_retention", {"retention_days": 30})
    assert err is None, f"实得 {err}"
    assert res.get("archived_eligible", 0) >= 1, f"修复前恒 0，实得 {res}"
    assert res.get("active_files", 0) == 2, f"预置 2 个 active file_instances，实得 {res}"


def test_snapshot_compare_counts_real_rows(c17_daemon):
    client, _, _, _ = c17_daemon
    res, err = _admin(client, "admin.snapshot_compare", {})
    assert err is None, f"实得 {err}"
    assert res.get("archived_files", 0) >= 1, f"实得 {res}"
    assert res.get("active_files", 0) == 2, f"实得 {res}"
    assert res.get("symbols", 0) == 1, f"实得 {res}"


def test_clear_clones_deletes_real_rows(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.clear_clones", {})
    assert err is None, f"实得 {err}"
    assert res.get("deleted_pairs") == 1, f"预置 1 行真 id clone_pairs，实得 {res}"
    left = _rows(cg, "SELECT COUNT(*) FROM clone_pairs WHERE workspace_id=?", (WS_ID,))
    assert left[0][0] == 0


def test_branch_register_then_switch_hits_real_id(c17_daemon):
    """register→switch 回路 + 落库锚定：修复前 register/switch 一致用代理 id 也能
    自洽通过，但落库 workspace_id=2（脏写）；修复后必须落 1。"""
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.branch_register", {"name": "c17-feat", "ref_sha": "abc123"})
    assert err is None, f"register 实得 {err}"
    assert res.get("ok") is True
    res2, err2 = _admin(client, "admin.branch_switch", {"name": "c17-feat"})
    assert err2 is None, f"switch 实得 {err2}"
    assert res2.get("ok") is True and res2.get("is_active") is True
    rows = _rows(cg, "SELECT workspace_id, is_active FROM daemon_branches WHERE name='c17-feat'")
    assert rows and rows[0] == (WS_ID, 1), f"落库应为 ({WS_ID},1)，实得 {rows}"


# ----------------------------------------------------------------------
# 第 3 类：无 FK 表（修复前静默写代理 id → 脏数据）
# ----------------------------------------------------------------------

def test_publish_interface_writes_real_id(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.publish_interface", {
        "interface_name": "c17.IFace", "version": "1.0.0", "provider_task_id": TASK_C17,
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True
    rows = _rows(cg, "SELECT workspace_id, interface_name FROM interface_identities WHERE interface_name='c17.IFace'")
    assert rows and rows[0][0] == WS_ID, f"落库 workspace_id 应为 {WS_ID}，实得 {rows}"


def test_select_interface_provider_writes_real_id(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.select_interface_provider", {
        "interface_name": "c17.IFace", "provider_task_id": TASK_C17,
        "consumer_task_id": TASK_C17, "contract_id": "TC-c17", "contract_revision": 1,
    })
    assert err is None, f"实得 {err}"
    assert res.get("ok") is True
    rows = _rows(cg, "SELECT workspace_id, selected_provider_task_id FROM interface_provider_selections WHERE interface_name='c17.IFace'")
    assert rows and rows[0] == (WS_ID, TASK_C17), f"实得 {rows}"


# ----------------------------------------------------------------------
# 卡 A 已收口方法在 admin 路由连通性（handler 内部走 task_bound_workspace_id）
# ----------------------------------------------------------------------

def test_assignment_create_revoke_roundtrip(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.assignment_create", {
        "task_id": TASK_C17, "role": "implementer",
        "agent_id": "c17-agent", "session_id": "c17-sess", "model_id": "c17-mdl",
    })
    assert err is None, f"实得 {err}"
    asg = res.get("assignment_id")
    assert asg and asg.startswith("ASG-")
    rows = _rows(cg, "SELECT workspace_id, status FROM task_assignments WHERE assignment_id=?", (asg,))
    assert rows and rows[0] == (WS_ID, "active"), f"实得 {rows}"
    res2, err2 = _admin(client, "admin.assignment_revoke", {"assignment_id": asg})
    assert err2 is None, f"revoke 实得 {err2}"
    assert res2.get("ok") is True


# ----------------------------------------------------------------------
# 无 workspace 语义的 handler（修复后仍正常；路由统一走新解析不回归）
# ----------------------------------------------------------------------

def test_gc_policy_set_get_roundtrip(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_policy_set", {"older_than_days": 45})
    assert err is None and res.get("ok") is True and res.get("older_than_days") == 45
    res2, err2 = _admin(client, "admin.gc_policy_get", {})
    assert err2 is None and res2.get("older_than_days") == 45
    rows = _rows(cg, "SELECT older_than_days FROM gc_policies")
    assert rows and rows[0][0] == 45


def test_audit_rotate_key_and_cleanup_rule_sync_log(c17_daemon):
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.audit_rotate_key", {"reason": "c17-test"})
    assert err is None and res.get("ok") is True and res.get("key_id", "").startswith("key-")
    active = _rows(cg, "SELECT COUNT(*) FROM audit_key_rotations WHERE is_active=1")
    assert active[0][0] == 1
    res2, err2 = _admin(client, "admin.cleanup_rule_sync_log", {"before_ts": 0})
    assert err2 is None and res2.get("deleted") == 1, f"预置 1 条旧日志，实得 {res2}"


# ----------------------------------------------------------------------
# 已知缺陷回归锚（F1 已由 C-19 承接修复 → 正例 + 隔离负例；F2/F3 保持锚形态）
# ----------------------------------------------------------------------

def test_gc_audit_get_returns_bound_row(c17_daemon):
    """C-19 修复正例（原 F1 锚迁移）：绑定 task 的 change_audit 行可命中且结构完整。

    旧实现引用不存在的 tasks.workspace_id → 恒 prepare 失败（no such column）；
    新实现经 task_workspace_bindings join 作用域。
    """
    client, cg, _, _ = c17_daemon
    _seed_change_audit(cg)
    res, err = _admin(client, "admin.gc_audit_get", {"audit_id": AUDIT_C19_BOUND})
    assert err is None, f"C-19 修复后应命中绑定行，实得 {err}"
    assert res is not None, "绑定 task 的行必须可见"
    assert res.get("id") == AUDIT_C19_BOUND
    assert res.get("task_id") == TASK_C17
    assert res.get("file_path") == "src/f1.rs"
    assert res.get("hash_before") == "b1" and res.get("hash_after") == "a1"
    assert res.get("diff") == "diff-bound" and res.get("author") == "c19-test"
    assert isinstance(res.get("timestamp"), float)


def test_gc_audit_get_unbound_task_invisible(c17_daemon):
    """C-19 隔离负例：unbound task（无 binding 行）的 change_audit 行不可见。

    与 task_bound_workspace_id 权威模型同源：unbound task 不归属任何 workspace。
    旧实现在此场景同样失败（no such column），新实现返回 null（不报错、不可见）。
    """
    client, cg, _, _ = c17_daemon
    _seed_change_audit(cg)
    res, err = _admin(client, "admin.gc_audit_get", {"audit_id": AUDIT_C19_UNBOUND})
    assert err is None, f"unbound 行不可见不应报错，实得 {err}"
    assert res is None, f"unbound task 的行必须不可见，实得 {res}"


def test_gc_audit_get_missing_id_returns_null(c17_daemon):
    """缺失 id → null（不报错）。"""
    client, _, _, _ = c17_daemon
    res, err = _admin(client, "admin.gc_audit_get", {"audit_id": "CA-c19-nope"})
    assert err is None and res is None, f"实得 res={res} err={err}"


def test_gc_audit_list_scopes_by_binding(c17_daemon):
    """gc_audit_list 只返回本 workspace 绑定 task 的行；unbound 行不可见。"""
    client, cg, _, _ = c17_daemon
    _seed_change_audit(cg)
    res, err = _admin(client, "admin.gc_audit_list", {"limit": 50})
    assert err is None, f"实得 {err}"
    assert isinstance(res, list)
    ids = {r.get("id") for r in res}
    assert AUDIT_C19_BOUND in ids, f"绑定行必须在列，实得 {sorted(ids)}"
    assert AUDIT_C19_UNBOUND not in ids, "unbound task 的行必须被作用域过滤"
    task_ids = {r.get("task_id") for r in res}
    assert task_ids <= {TASK_C17}, f"只应含本 workspace 绑定 task，实得 {task_ids}"
    fields_ok = all(
        {"id", "task_id", "file_path", "author", "timestamp"} <= set(r) for r in res
    )
    assert fields_ok, "行结构必须完整（id/task_id/file_path/author/timestamp）"


def test_record_action_identity_generates_action_id(c17_daemon):
    """C-20 修复正例（原 F2 锚迁移）：调用方未给 action_id → Rust 生成 ACT-<hex16> 并落库。

    旧实现 INSERT 缺 action_id 列 → 恒 NOT NULL constraint failed；
    新实现回退生成 ACT-<hex16>（与 gen_assignment_id 同源熵形，对齐 Python
    db/db_task_identity.py 的 ACT-… 语义），且落库 workspace_id 为真 id（WS_ID）。
    """
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.record_action_identity", {
        "action_type": "contract", "task_id": TASK_C17, "contract_id": "TC-c20",
        "contract_revision": 3, "agent_id": "a", "session_id": "s",
        "model_id": "m", "role": "implementer",
    })
    assert err is None, f"C-20 修复后必须成功，实得 {err}"
    assert res is not None and res.get("ok") is True
    rows = _rows(
        cg,
        "SELECT action_id, workspace_id, action_type, task_id, contract_id, contract_revision,"
        " agent_id, session_id, model_id, role, recorded_at FROM action_identities"
        " ORDER BY recorded_at DESC, id DESC LIMIT 1",
    )
    assert rows, "必须落库一行 action_identities"
    (action_id, ws, a_type, task, contract, revision, agent, sess, model, role, _ts) = rows[0]
    assert action_id.startswith("ACT-"), f"action_id 必须 ACT- 前缀，实得 {action_id}"
    assert len(action_id) == len("ACT-") + 16, f"ACT- 后应为 16 hex，实得 {action_id}"
    assert all(c in "0123456789abcdef" for c in action_id[4:]), f"非 hex 熵：{action_id}"
    assert ws == WS_ID, f"必须写真 workspace id（{WS_ID}），实得 {ws}"
    assert (a_type, task, contract, revision, agent, sess, model, role) == (
        "contract", TASK_C17, "TC-c20", 3, "a", "s", "m", "implementer",
    ), f"字段读回不符：{rows[0]}"


def test_record_action_identity_caller_id_and_uniqueness(c17_daemon):
    """C-20：调用方显式 action_id 优先入列（对齐 Python 契约）；重复 id 撞 UNIQUE 拒绝。

    Python 权威实现 db/db_task_identity.py:170-203 由调用方提供 action_id，
    UNIQUE 冲突 → ERR_IDENTITY_ACTION_DUPLICATE；Rust 侧同语义（冲突即报错）。
    两次自动生成必须得到不同 id。
    """
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.record_action_identity", {
        "action_type": "contract", "task_id": TASK_C17, "action_id": ACT_C20_EXPLICIT,
        "agent_id": "a", "session_id": "s", "model_id": "m", "role": "implementer",
    })
    assert err is None, f"显式 action_id 必须原样入列，实得 {err}"
    hit = _rows(
        cg, "SELECT COUNT(*) FROM action_identities WHERE action_id = ?", (ACT_C20_EXPLICIT,)
    )
    assert hit and hit[0][0] == 1, f"显式 id 必须落库，实得 {hit}"

    _, dup_err = _admin(client, "admin.record_action_identity", {
        "action_type": "contract", "task_id": TASK_C17, "action_id": ACT_C20_EXPLICIT,
        "agent_id": "a", "session_id": "s", "model_id": "m", "role": "implementer",
    })
    assert dup_err is not None, "重复 action_id 必须撞 UNIQUE 拒绝"

    ids = set()
    for _ in range(2):
        _admin(client, "admin.record_action_identity", {
            "action_type": "gate", "task_id": TASK_C17,
            "agent_id": "a", "session_id": "s", "model_id": "m", "role": "implementer",
        })
    for (aid,) in _rows(cg, "SELECT action_id FROM action_identities WHERE action_type='gate'"):
        ids.add(aid)
    assert len(ids) == 2, f"两次自动生成必须得到不同 id，实得 {sorted(ids)}"


def test_register_attestation_revocation_generates_revocation_id(c17_daemon):
    """C-20 修复正例（原 F3 锚迁移）：内部生成 REV-<hex16> 并落库（镜像 Python :609）。

    旧实现 INSERT 缺 revocation_id 列 → 恒 NOT NULL constraint failed；
    新实现每次调用新生成 REV-<hex16>；rows 全字段读回 + 两次调用 id 互不相同。
    """
    client, cg, _, _ = c17_daemon
    res, err = _admin(client, "admin.register_attestation_revocation", {
        "issuer": "c20", "signing_key_id": "k1", "revocation_mode": "rotated",
        "revocation_reason": "c20-test", "initiating_actor": "test",
    })
    assert err is None, f"C-20 修复后必须成功，实得 {err}"
    assert res is not None and res.get("ok") is True
    rows = _rows(
        cg,
        "SELECT revocation_id, workspace_id, issuer, signing_key_id, revocation_mode,"
        " revocation_reason, initiating_actor, revoked_at FROM attestation_revocation_records"
        " ORDER BY revoked_at DESC, id DESC LIMIT 1",
    )
    assert rows, "必须落库一行 attestation_revocation_records"
    (rev_id, ws, issuer, key, mode, reason, actor, _ts) = rows[0]
    assert rev_id.startswith("REV-"), f"revocation_id 必须 REV- 前缀，实得 {rev_id}"
    assert len(rev_id) == len("REV-") + 16, f"REV- 后应为 16 hex，实得 {rev_id}"
    assert all(c in "0123456789abcdef" for c in rev_id[4:]), f"非 hex 熵：{rev_id}"
    assert ws == WS_ID, f"必须写真 workspace id（{WS_ID}），实得 {ws}"
    assert (issuer, key, mode, reason, actor) == (
        "c20", "k1", "rotated", "c20-test", "test",
    ), f"字段读回不符：{rows[0]}"

    _admin(client, "admin.register_attestation_revocation", {
        "issuer": "c20", "signing_key_id": "k1", "revocation_mode": "compromised",
        "revocation_reason": "c20-second", "initiating_actor": "test",
    })
    ids = {r[0] for r in _rows(cg, "SELECT revocation_id FROM attestation_revocation_records")}
    assert len(ids) == 2, f"两次登记必须生成不同 revocation_id，实得 {sorted(ids)}"
