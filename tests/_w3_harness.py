"""W3 家族共享隔离 daemon 基建（垂直 slice 验证后横向推广用）。

目标：让 `test_mcp_*_http_rpc.py` 等「需 live daemon」文件在无人值守环境下
不依赖后台常驻 daemon（W8 不稳）即可自建权威 workspace + snapshot 跑通。

权威栈（由浅入深，均已实证）：
1. 隔离 daemon（模式A，USERPROFILE 重定向）→ manifest 落 data_root/userhome/.callwarden
2. workspace.register（registry 侧，client_view_root 需真实存在）
3. task-DB `workspaces` 表 seed 该 workspace id（`resolve_create_authority` 第4步）
4. 构造完整空 codegraph DB（含 `file_instances` 等表）并 `snapshot.publish`
   → 消除 `snapshot_not_ready`；空库对未知 symbol 返回空链（total_upstream=0/all_upstream=[]）

只读 RPC（get_impact 等）因此走合法 authority 返回"空结构"，满足测试的
结构断言，而非绕过 fail-closed。
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import unicodedata

from callwarden.config import get_http_authority_id
from callwarden.server.daemon_client import HttpDaemonRpcClient


def sha256_hex(data: bytes) -> str:
    """复刻 canonicalize::sha256_hex（小写 hex）。"""
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------
# codegraph 空库 schema（镜像 Rust daemon snapshot_state.rs build_codegraph_db）
# ----------------------------------------------------------------------
_EMPTY_CODEGRAPH_DDL = """
CREATE TABLE workspaces (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER NOT NULL,
    description TEXT DEFAULT ''
);
CREATE TABLE file_instances (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    module_path TEXT NOT NULL,
    visibility TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    depth INTEGER NOT NULL
);
CREATE TABLE calls (
    caller_id INTEGER NOT NULL,
    callee_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL,
    call_line INTEGER NOT NULL,
    is_cross_file INTEGER NOT NULL
);
CREATE TABLE file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL
);
CREATE TABLE symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    signature TEXT,
    has_comment INTEGER,
    comment_content TEXT
);
CREATE TABLE file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    is_deleted INTEGER DEFAULT 0
);
CREATE TABLE call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT NOT NULL,
    caller_hash TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0
);
"""


_DAEMON_ONLY_DDL = """
CREATE TABLE IF NOT EXISTS daemon_branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ref_sha TEXT DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, name)
);
"""

# Rust daemon 侧独有表（db.schema.SCHEMA_TABLES_SQL 未覆盖，但 snapshot_state.rs
# build_codegraph_db / 查询 handler 直查）：clone_groups + workspace_build_contexts。
_CLONE_GROUPS_DDL = """
CREATE TABLE IF NOT EXISTS clone_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    group_hash TEXT NOT NULL,
    clone_type INTEGER NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    similarity REAL DEFAULT 0.0,
    representative_symbol_id INTEGER NOT NULL,
    member_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, group_hash),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS clone_group_members (
    group_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, symbol_id),
    FOREIGN KEY (group_id) REFERENCES clone_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);
"""

_WORKSPACE_BUILD_CONTEXTS_DDL = """
CREATE TABLE IF NOT EXISTS workspace_build_contexts (
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    name TEXT DEFAULT '',
    compile_flags TEXT DEFAULT '[]',
    defines TEXT DEFAULT '{}',
    include_paths TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, build_context_hash)
);
CREATE TABLE IF NOT EXISTS resolved_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    caller_symbol_id INTEGER NOT NULL,
    callee_symbol_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL,
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    resolution_method TEXT DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id, call_line)
);
"""

# B 类证据 seed：MCP-002/003（find_evidence / get_freshness_status）的 golden task
# T-1785767529976-1760c608 需 2 条 evidence（verifier=cw-agent）→ 空 verifier_registry
# 下派生 invalid（VERIFIER_NOT_REGISTERED parity）。建库时一并写入，快照发布即生效。
_EVIDENCE_TASK = "T-1785767529976-1760c608"
_EVIDENCE_SEED = [
    ("EV-W3-0001", _EVIDENCE_TASK, "C-w3-seed", 1, "hash-w3-0001", "test_run"),
    ("EV-W3-0002", _EVIDENCE_TASK, "C-w3-seed", 1, "hash-w3-0001", "test_run"),
]


def _codegraph_ddl():
    """权威 codegraph schema：优先 db.schema.SCHEMA_TABLES_SQL（93 表，含
    dependency_edges / agent_rules / task_evidence_events 等全部辅助表），
    失败时回退核心 DDL（_EMPTY_CODEGRAPH_DDL 兼容旧环境）。两条路径都追加
    daemon 侧独有表（daemon_branches / clone_groups / workspace_build_contexts）。"""
    try:
        from callwarden.db.schema import SCHEMA_TABLES_SQL

        return (SCHEMA_TABLES_SQL + _DAEMON_ONLY_DDL + _CLONE_GROUPS_DDL
                + _WORKSPACE_BUILD_CONTEXTS_DDL)
    except Exception:
        return (_EMPTY_CODEGRAPH_DDL + _DAEMON_ONLY_DDL + _CLONE_GROUPS_DDL
                + _WORKSPACE_BUILD_CONTEXTS_DDL)


def build_empty_codegraph(db_path, root_path, ws_id=1):
    """构造完整空 codegraph DB：建全部表 + 一个匹配 root_path 的 workspace 行。

    使 `snapshot.publish` 能成功发布，空库对未知 symbol 返回空调用链。
    同时 seed 少量最小数据，支撑依赖特定表/数据的测试：
    - symbol `handle_task_apply`（fn）→ ask_codebase keyword_fallback 命中；
    - calls 1 行 → resolved_edges.rebuild 有边可复制（build_context 写后可见）；
    - clone_groups / git_commits → list_clone_groups / get_symbol_commit_history。
    db_path 的 `workspaces.root_path` 必须规范化后 == 注册用的 client_view_root，
    否则 `resolve_true_workspace_id` fallback 到 registry rowid（仍可用，但语义为 prod 对齐）。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_codegraph_ddl())
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description)"
            " VALUES (?, ?, ?, 1.0, 1, '')",
            (ws_id, "w3-ws", root_path),
        )
        seed_codegraph_data(conn, ws_id)
        conn.commit()
    finally:
        conn.close()


# keyword_fallback 检索用符号（qualified_name 命中 ask_codebase 查询词）
_W3_FN_SYMBOL = "handle_task_apply"
_W3_FN_HASH = "04d158d2ffa64c0ce362ea4805ae7c44464eba5f3e3d6974d1e60bc26faeea5c"
_GIT_COMMIT = "276829e79f6f55ce0f8d898cb0d292c7dc619648"

# compat_tools_summary::test_ask_codebase_keyword_fallback 查询 "dispatch rpc route"，
# keyword_fallback 分词（>2 字符）为 dispatch/rpc/route → 需一个含 dispatch 的 fn 符号。
_DISPATCH_SYMBOL = "dispatch_rpc_route"
_DISPATCH_HASH = "7a51c2b9f9d0e2f3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a"

# compat_tools_security::test_merge_preview_success_shape 使用固定分支名
# "callwarden"/"test-h9-ws"（查询 workspaces.name）。seed 两个分支 workspace +
# 各自独立符号，使 diff 产生 added（target 有 source 无），merge_preview 返回
# 完整结构（无 calls 边 → affected_symbols=0 / risk_level=low，仍满足形状断言）。
_MERGE_SRC_BRANCH = "callwarden"
_MERGE_TGT_BRANCH = "test-h9-ws"

# compat_tools_security::test_cross_repo_impact_real_hash_shape 使用真实 hash
# 0002e9fe... 并断言 source_workspace=="TokenSlim"。seed 同名 workspace + 该 hash
# 符号，使 source 命中；无 cross_repo_deps → impacted_repos=[] / risk_level=low。
_TOKENSLIM_WS = "TokenSlim"
_CROSS_REPO_HASH = "0002e9fedd5b2ea5e5aa22f7a8d55f0f6fef178189799eff27fc98fb8e980d95"

# compat_tools_task::test_audit_verify_chain_small_limit 断言 total_count==20
# （table_name='' 全表）。seed 20 条 audit_chain（record_signature 与本地重算
# 不匹配 → broken；verified+broken==20 仍成立，兼容已绿 MCP-064 的 limit/过滤断言）。
_AUDIT_CHAIN_SEED_ROWS = 20


def seed_codegraph_data(conn, ws_id):
    """seed 最小 codegraph 数据（各表列按 db.schema.SCHEMA_TABLES_SQL 对齐）。"""
    # file_instance + symbol（fn，命中 ask_codebase keyword_fallback）
    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, mtime, status)"
        " VALUES (1, ?, 'src/w3_seed.py', '/w3/src/w3_seed.py', ?, 'tracked')",
        (ws_id, time.time()),
    )
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,"
        " start_line, end_line, qualified_name)"
        " VALUES (1, 1, ?, ?, 'fn', 'public', 1, 10, ?)",
        (_W3_FN_HASH, _W3_FN_SYMBOL, _W3_FN_SYMBOL),
    )
    # calls 1 行 → resolved_edges.rebuild 从 calls 表复制出边
    conn.execute(
        "INSERT INTO calls (caller_id, caller_name, caller_module, callee_name,"
        " callee_module, callee_id, call_line, is_cross_file)"
        " VALUES (1, ?, 'w3_seed', 'target_fn', 'w3_seed', 2, 5, 0)",
        (_W3_FN_SYMBOL,),
    )
    # dispatch 符号：ask_codebase keyword_fallback（kind='fn' + name LIKE %dispatch%）
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,"
        " start_line, end_line, qualified_name)"
        " VALUES (2, 1, ?, ?, 'fn', 'public', 11, 20, ?)",
        (_DISPATCH_HASH, _DISPATCH_SYMBOL, _DISPATCH_SYMBOL),
    )
    seed_branch_workspaces(conn)
    seed_cross_repo_workspace(conn)
    seed_git_history(conn, ws_id)
    seed_clone_groups(conn, ws_id, 1)
    seed_audit_chain(conn, _AUDIT_CHAIN_SEED_ROWS)


def seed_git_history(conn, ws_id):
    """插入 3 条 git 历史，支持 get_symbol_commit_history 测试（len==3）。

    commit1 为最新（timestamp 最大）：commit_hash=_GIT_COMMIT、change_type='modified'
    → ORDER BY timestamp DESC 首行对齐 MCP-064 断言（commit_hash/change_type/workspace_id）。
    """
    now = time.time()
    commits = [
        # (commit_hash, message, timestamp)
        (_GIT_COMMIT, "feat: add coverage", now - 100),                    # 最新
        ("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", "feat: impl", now - 200),
        ("b0a9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7", "fix: guard", now - 300),
    ]
    conn.executemany(
        "INSERT INTO git_commits (commit_hash, message, author, email, timestamp, workspace_id)"
        " VALUES (?, ?, 'cw-agent', 'agent@example.com', ?, ?)",
        [(h, msg, ts, ws_id) for (h, msg, ts) in commits],
    )
    conn.executemany(
        "INSERT INTO git_symbol_changes (commit_hash, symbol_hash, change_type)"
        " VALUES (?, ?, ?)",
        [(commits[0][0], _W3_FN_HASH, "modified"),
         (commits[1][0], _W3_FN_HASH, "added"),
         (commits[2][0], _W3_FN_HASH, "modified")],
    )


def seed_branch_workspaces(conn):
    """seed merge_preview 需要的两个分支 workspace（按 workspaces.name 查询）。

    query_local_diff_branches 按 workspaces.name 精确匹配 source/target →
    "callwarden"/"test-h9-ws" 各建 workspace + 独立 fn 符号 → diff 产生
    added/removed，merge_preview 返回完整形状（无 calls 边 → affected_symbols=0 /
    risk_level=low，仍满足形状断言）。
    """
    t = time.time()
    rows = [
        # (ws_id, name, root_path, file_id, symbol_id, symbol_hash, fn_name)
        (2, _MERGE_SRC_BRANCH, "/w3/br/callwarden", 2, 3,
         "f0e1d2c3b4a5968778695a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d",
         "callwarden_only_fn"),
        (3, _MERGE_TGT_BRANCH, "/w3/br/test-h9-ws", 3, 4,
         "e0d1c2b3a495867768594a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d",
         "test_h9_ws_only_fn"),
    ]
    for (ws_id, name, root, file_id, sym_id, sym_hash, fn) in rows:
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at, is_active)"
            " VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root, t),
        )
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, mtime, status)"
            " VALUES (?, ?, 'src/branch.py', ?, ?, 'tracked')",
            (file_id, ws_id, root + "/src/branch.py", t),
        )
        conn.execute(
            "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,"
            " start_line, end_line, qualified_name)"
            " VALUES (?, ?, ?, ?, 'fn', 'public', 1, 10, ?)",
            (sym_id, file_id, sym_hash, fn, fn),
        )


def seed_cross_repo_workspace(conn):
    """seed cross_repo_impact 的 source workspace（TokenSlim + 真实 hash 符号）。

    handle_cross_repo_impact 按 symbol_hash 精确匹配 → 命中后 source_workspace
    = w.name。无 cross_repo_deps 行 → impacted_repos=[] / risk_level=low。
    """
    t = time.time()
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active)"
        " VALUES (4, ?, '/w3/br/tokenslim', ?, 1)",
        (_TOKENSLIM_WS, t),
    )
    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, mtime, status)"
        " VALUES (5, 4, 'src/token_slim.py', '/w3/br/tokenslim/src/token_slim.py', ?, 'tracked')",
        (t,),
    )
    conn.execute(
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,"
        " start_line, end_line, qualified_name)"
        " VALUES (5, 5, ?, 'token_slim_fn', 'fn', 'public', 1, 10, 'token_slim_fn')",
        (_CROSS_REPO_HASH,),
    )


def seed_audit_chain(conn, rows):
    """seed rows 条 audit_chain，支持 test_audit_verify_chain_small_limit。

    record_signature 与本地重算不匹配 → 全 broken（verified+broken==rows 成立）。
    table_name='tasks' → table_filter 用例（table_name='tasks' LIMIT 10）
    total_count==10<=10 且 broken_records 全为 tasks。
    """
    t = time.time()
    conn.executemany(
        "INSERT INTO audit_chain (table_name, record_id, payload_hash,"
        " prev_signature, record_signature, signed_at)"
        " VALUES ('tasks', ?, ?, '', ?, ?)",
        [(f"T-{i}", f"payload-hash-{i}", f"invalid-sig-{i}", t) for i in range(1, rows + 1)],
    )


def seed_clone_groups(conn, ws_id, symbol_id):
    """插入模拟克隆组数据，支持 list_clone_groups 测试。"""
    conn.execute(
        "INSERT INTO clone_groups (workspace_id, group_hash, clone_type,"
        " representative_symbol_id, member_count, created_at)"
        " VALUES (?, ?, 1, ?, 2, ?)",
        (ws_id, "group-hash-001", symbol_id, time.time()),
    )
    conn.execute("INSERT INTO clone_group_members (group_id, symbol_id) VALUES (1, ?)",
                 (symbol_id,))
    conn.execute("INSERT INTO clone_group_members (group_id, symbol_id) VALUES (1, ?)",
                 (symbol_id + 1,))


def seed_cli_task_authority(task_db, ws_id, name, root_path, instance_id, now=None):
    """seed task-DB 的 canonical workspace 权威，使 CLI task.create 的
    `resolve_workspace_pair_from_daemon` 拿到唯一 (task_db_id, instance) 配对。

    根因（探针实证）：`workspace.status` 的 task-DB 侧只读
    `workspace_authority_captures`（按 instance），不读 `workspaces` 表；
    fixture 只 seed `workspaces` → status 返回 `task_db_workspace_id: null` →
    resolver matches=0 fail-closed。

    本函数补齐三件事（与 task_collab.rs bind_task_to_workspace 的 create 路径逐字段对齐）：
    1. `workspaces` 行（id=ws_id，root 必须与 register/CLI 归一化一致）。
    2. `canonicalization_rule_sets` 的 workspace_capture c14n rule row
       （create 路径 L268-280 要求可读，否则 capability 未就绪 fail-closed）。
    3. `workspace_authority_captures` rev=1 行：registry_identity_hash 必须与
       create 时重新计算的完全一致（sha256:hex(c14n(payload))，c14n 按键排序 +
       NFC + 紧凑 JSON），否则 task.create 追加 capture 时 identity 校验 mismatch。
    """
    if now is None:
        now = time.time()
    root_hash = sha256_hex(root_path.encode("utf-8"))
    manifest_payload = json.dumps(
        {
            "workspace_id": ws_id,
            "workspace_name": name,
            "root_path_hash": root_hash,
            "manifest_format_version": "workspace-manifest-c14n/v1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    manifest_hash = sha256_hex(manifest_payload.encode("utf-8"))
    identity_payload = {
        "workspace_instance_id": instance_id,
        "client_view_root_hash": root_hash,
        "host_real_root_hash": root_hash,
        "workspace_manifest_hash": manifest_hash,
    }
    registry_payload_json = json.dumps(
        identity_payload, ensure_ascii=False, separators=(",", ":")
    )
    # create.rs registry_identity_hash：c14n_value 键排序 + NFC + 紧凑序列化。
    c14n = json.dumps(
        {unicodedata.normalize("NFC", k): v for k, v in identity_payload.items()},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity_hash = "sha256:" + sha256_hex(c14n.encode("utf-8"))

    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active)"
            " VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root_path, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO canonicalization_rule_sets"
            " (domain, canonicalization_version, rules_hash)"
            " VALUES ('workspace_capture', 'workspace-capture-c14n/v1', 'cli-test-rules-hash')"
        )
        conn.execute(
            "INSERT INTO workspace_authority_captures "
            "(workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id,"
            " daemon_workspace_id, workspace_instance_id, capture_canonicalization_version,"
            " capture_canonicalization_rules_hash, registry_identity_payload_json,"
            " registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash,"
            " client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at)"
            " VALUES (?1, ?2, 1, NULL, 0, ?3, 'workspace-capture-c14n/v1',"
            " 'cli-test-rules-hash', ?4, ?5, ?6, ?7, ?8, ?8, 'cli-test', ?9)",
            (
                f"wc-cli-{instance_id}",
                ws_id,
                instance_id,
                registry_payload_json,
                identity_hash,
                manifest_payload,
                manifest_hash,
                root_hash,
                f"{now:.6f}",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def seed_cli_lifecycle_task(task_db, task_id, ws_id, bind_workspace=True, status="open"):
    """seed task-DB 的治理任务权威，供 CLI-084..088 的 team RPC 直接打隔离 daemon。

    对齐 daemon 真实门禁语义（探针实证）：
    - `task.claim` 在 L97 `task_bound_workspace_id` 处要求 `task_workspace_bindings` 存在，
      否则报 E_TASK_WORKSPACE_UNBOUND（而非走到身份校验）。故 seed `task_workspace_bindings`
      一行（workspace_id=ws_id），使 claim 能通过 UNBOUND 进入后续 policy/身份判断。
    - `task.report` 在 snapshot 校验前报 E_TASK_REPORT_SNAPSHOT_REQUIRED（空 snapshot_id）。
    - `task.rollback`/`task.reopen`/`task.resolve_quality_finding` 无身份门禁，直接执行成功。

    本函数只 seed 最小可查询数据（tasks + 可选 task_workspace_bindings），让目标是
    「python thin client 正确路由 + daemon 权威响应」的负向矩阵在隔离 daemon 上自洽运行。
    """
    now = time.time()
    conn = sqlite3.connect(task_db, timeout=10)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(id, title, description, creator, status, created_at, updated_at, parent_id)"
            " VALUES (?, ?, '', 'cli-matrix', ?, ?, ?, '')",
            (task_id, "cli-matrix " + task_id, status, now, now),
        )
        if bind_workspace:
            bid = f"tb-{task_id}"
            conn.execute(
                "INSERT OR IGNORE INTO task_workspace_bindings "
                "(task_id, workspace_id, workspace_binding_id, created_by, authoritative_created_at)"
                " VALUES (?, ?, ?, 'cli-matrix', ?)",
                (task_id, ws_id, bid, now),
            )
        conn.commit()
    finally:
        conn.close()


def seed_cli_task_agent(task_db, agent_id, agent_name="cli-test-agent",
                        instance_id="", session_id="", role="", model_id=""):
    """seed task-DB 的 agent_registrations，使 CLI claim 携带的 identity 通过
    daemon 的 registered-and-active 校验（否则 E_IDENTITY_UNREGISTERED /
    E_IDENTITY_INACTIVE fail-closed）。

    daemon task.claim legacy 路径按 agent_id 查询 agent_registrations 要求
    status='active'；agent_instance_id 为空则不触发 instance 一致性校验
    （identity 未携带 agent_instance_id 时一致）。
    """
    now = time.time()
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agent_registrations "
            "(agent_id, agent_name, owner_key, capabilities, registered_at,"
            " last_heartbeat, status, agent_instance_id, client_id, provider,"
            " model_id, model_mode, system_fingerprint, runtime_hash, session_id, role)"
            " VALUES (?, ?, 'owner', '[]', ?, ?, 'active', ?, '', '', ?, '', '', '', ?, ?)",
            (agent_id, agent_name, now, now, instance_id, model_id, session_id, role),
        )
        conn.commit()
    finally:
        conn.close()


def seed_task_db_evidence(conn, ws_id):
    """在 daemon task DB 写入证据 seed（find_evidence / get_freshness_status 读此库）。

    MCP-002/003 的 golden task T-1785767529976-1760c608 需 2 条 evidence
    （verifier=cw-agent）→ 空 verifier_registry 下派生 invalid（VERIFIER_NOT_REGISTERED
    parity）。注意：find_evidence 读 daemon 的 task.db（self.conn），不是 codegraph
    快照库——seed 必须落在 task DB。
    """
    conn.executemany(
        "INSERT INTO task_evidence_events "
        "(evidence_id, task_id, contract_id, contract_revision, contract_hash,"
        " evidence_type, event_type, verifier_name, produced_at, workspace_id)"
        " VALUES (?, ?, ?, ?, ?, ?, 'evidence_appended', 'cw-agent', ?, ?)",
        [(eid, tid, cid, rev, ch, etype, time.time(), ws_id)
         for (eid, tid, cid, rev, ch, etype) in _EVIDENCE_SEED],
    )


# ----------------------------------------------------------------------
# daemon 生命周期（模式A：USERPROFILE 重定向）
# ----------------------------------------------------------------------

def _to_windows_path(path):
    """C-18：MSYS 风格路径（/c/Users/...）转 Windows 形式（C:/Users/...）。

    背景（卡 C 实测假阳性）：`CW_DAEMON_BIN=/c/...` 在 Windows Python 下
    `os.path.isfile` 判 False → 候选被静默跳过，回落陈旧 debug 构建。
    非Windows 或非 `/` 开头路径原样返回。
    """
    if os.name != "nt" or not path.startswith("/"):
        return path
    parts = path.split("/")
    if len(parts) > 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return parts[1] + ":/" + "/".join(parts[2:])
    return path


def _candidate_exists(path):
    """C-18：候选存在性判定兼容 MSYS 风格路径（原样 / 转换后任一命中即存在）。"""
    if os.path.isfile(path):
        return os.path.abspath(path)
    win = _to_windows_path(path)
    if win != path and os.path.isfile(win):
        return os.path.abspath(win)
    return None


def find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制。

    多候选按修改时间取最新（release 构建可能晚于 debug），保证路由表覆盖
    P0-COMPAT-v3 全部方法（旧 debug 缺 guardrail_scan 等 → E_COMPAT_METHOD_NOT_FOUND）。
    C-18 收敛：① 存在性判定兼容 MSYS 风格 CW_DAEMON_BIN（不再静默跳过）；
    ② 选中候选必须显式打印最终路径与 sha256（禁止静默回落陈旧构建）。
    """
    candidates = [
        os.path.join("rust_ext", "target", "release", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "release", "cw-daemon"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon"),
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join("runtime", "current", "cw-daemon.exe"),
    ]
    best = None
    for c in candidates:
        if not c:
            continue
        resolved = _candidate_exists(c)
        if resolved is None:
            continue
        try:
            mtime = os.path.getmtime(resolved)
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, resolved)
    if best is None:
        return None
    chosen = best[1]
    try:
        digest = hashlib.sha256(open(chosen, "rb").read()).hexdigest().upper()
    except OSError:
        digest = "unavailable"
    print(f"[w3_harness] find_daemon_binary -> {chosen} sha256={digest}")
    return chosen


def spawn_isolated_daemon(bin_path, data_root, http_bind="127.0.0.1:0"):
    """启动隔离 daemon（模式A）。返回 (proc, env)。"""
    home = os.path.join(data_root, "userhome")
    os.makedirs(os.path.join(home, ".callwarden"), exist_ok=True)
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["USERPROFILE"] = home
    # 主库（codegraph）路径与快照读面同一物理文件（userhome/.callwarden/
    # callwarden.db）：模板支持的二进制 resolve 到此处，不支持的回落默认路径
    # 也是同一文件（USERPROFILE 已重定向）。消除「写后不可见」分裂。
    env["CW_DAEMON_CODEGRAPH_DB_TEMPLATE"] = os.path.join(
        home, ".callwarden", "callwarden.db"
    )
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc, env


def wait_manifest(data_root, proc, timeout=15.0):
    """等待隔离 daemon 落盘 manifest，返回 (manifest, manifest_file_path)。

    返回 manifest 文件路径供客户端显式校验——否则客户端 discover() 会按
    CALLWARDEN_DIR（真实 HOME）读 manifest，命中后台 daemon 回收后残留的
    stale manifest → PID 存活检查失败 E_HTTP_MANIFEST_STALE（本轮实测复现）。
    """
    man_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, None
        if os.path.isdir(man_dir):
            for f in os.listdir(man_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    fp = os.path.join(man_dir, f)
                    try:
                        m = json.load(open(fp, encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m, fp
        time.sleep(0.2)
    return None, None


# ----------------------------------------------------------------------
# workspace authority + snapshot 装配
# ----------------------------------------------------------------------

def setup_w3_client(data_root, root_path, ws_id=1):
    """一站式建立 w3 权威栈，返回 (client, workspace_instance_id, endpoint, proc)。

    依次：起隔离 daemon → manifest 发现 → workspace.register → seed task-DB
    `workspaces` → 空 codegraph+publish snapshot。daemon 二进制缺失时返回
    (None, None, None, None)，由调用方 pytest.skip。
    """
    bin_path = find_daemon_binary()
    if bin_path is None:
        return None, None, None, None
    proc, _ = spawn_isolated_daemon(bin_path, data_root)
    try:
        m, man_file = wait_manifest(data_root, proc)
        if m is None:
            proc.kill()
            return None, None, None, None
        endpoint = m["endpoint"]
        # manifest_path=隔离 manifest：避免 discover() 读真实 HOME 的 stale
        # manifest（后台 daemon 残留 PID → E_HTTP_MANIFEST_STALE）。
        client = HttpDaemonRpcClient(endpoint=endpoint, authority_id=get_http_authority_id(),
                                     manifest_path=man_file)

        os.makedirs(root_path, exist_ok=True)
        ws = client.call("workspace.register", {"client_view_root": root_path})
        inst = ws["workspace_instance_id"]

        # task-DB workspaces 表 seed（resolve_create_authority 第4步）
        conn = sqlite3.connect(os.path.join(data_root, "task.db"), timeout=10)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active)"
                " VALUES (?, ?, ?, ?, 1)",
                (ws_id, f"ws-{ws_id}", root_path, time.time()),
            )
            seed_task_db_evidence(conn, ws_id)
            conn.commit()
        finally:
            conn.close()

        # 空 codegraph + snapshot.publish（消除 snapshot_not_ready）。
        # 主库路径 = USERPROFILE 重定向后的默认单库（userhome/.callwarden/
        # callwarden.db）：daemon 写面（build_context.register / resolved_edges.
        # rebuild，模板缺省时 resolve 到 default_codegraph_db_path()）与快照读面
        # （open_query_connection 打开同一文件）必须指向**同一物理文件**，否则
        # 「写后可见」断言拿不到刚写入的行。模板 env var 同步设同一路径——
        # 支持模板的二进制 resolve 到同一文件，不支持的二进制回落默认路径也
        # 是同一文件（USERPROFILE 已重定向到 data_root/userhome）。
        cg_db = os.path.join(data_root, "userhome", ".callwarden", "callwarden.db")
        os.makedirs(os.path.dirname(cg_db), exist_ok=True)
        build_empty_codegraph(cg_db, root_path, ws_id)
        client.call("snapshot.publish",
                    {"workspace_instance_id": inst, "db_path": cg_db})

        return client, inst, endpoint, proc
    except Exception:
        proc.kill()
        raise


# ----------------------------------------------------------------------
# C-18：assignment_show 家族 well-known 已绑定 task seed（append-only，不改既有行）
# ----------------------------------------------------------------------

W3_ASSIGN_SHOW_BOUND_TASK = "T-W3-ASSIGN-SHOW-BOUND-C18"


def seed_task_workspace_binding(conn, ws_id, task_id=W3_ASSIGN_SHOW_BOUND_TASK):
    """为 W3 家族 assignment_show 用例种一条不可变 task→workspace binding（C-18）。

    背景：C-16 起 handle_assignment_show 走权威 resolver `task_bound_workspace_id`，
    无 binding 的 task 一律 fail-closed（E_TASK_WORKSPACE_UNBOUND）。既有 W3 夹具
    （setup_w3_client）只 seed `workspaces` → 该家族内 task 天然 unbound，
    4+2 例断言「无 active assignment → status=none」变为陈旧（backlog §W19）。

    本函数**只追加**（不改既有行/既有用例语义）：
    - 种一行 tasks（binding 的 FK 目标，隔离 daemon FK 不强制时也无害）；
    - 种一行 task_workspace_bindings（workspace_capture_id 显式给值，
      与卡 C 隔离矩阵 test_c16 同口径）；
    - **不种任何 task_assignments 行**——保留「有 binding 但无 active assignment
      → status=none」的原覆盖语义。

    调用方（测试文件自带 fixture）拿 task.db 连接调用后自行 commit。
    """
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO tasks "
        "(id, title, description, creator, status, created_at, updated_at, parent_id)"
        " VALUES (?, ?, '', 'c18-w3-seed', 'open', ?, ?, '')",
        (task_id, "c18 bound task " + task_id, now, now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_workspace_bindings "
        "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, "
        " created_by, authoritative_created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        (task_id, ws_id, f"tb-{task_id}", f"wc-{task_id}", "c18-w3-seed", "0"),
    )