"""run_t2_t3_evidence.py —— T2/T3 陈旧证据重跑（任务5②，2026-10-13）。

在**完全隔离**的 daemon（独立 data_root / registry / task DB / 随机 HTTP 端口）
上重跑收敛套件两份全量证据：

  - T2：243 个 MCP 工具全参数真实调用（t2_mcp_runner）
  - T3：234 个 CLI 叶子命令全参数真实调用（t3_cli_runner）

刷新交付物：
  - t2_mcp_result.json / t3_cli_result.json（含 meta：时间 / git HEAD / daemon sha256）
  - t2_mcp_full_invocation_report.md / t3_cli_full_invocation_report.md

**绝对不触碰生产 daemon**：USERPROFILE、CW_DAEMON_TASK_DB、CW_DAEMON_REGISTRY_DB、
CW_DAEMON_SOCKET 全部重定向到临时 data_root；--http-bind=127.0.0.1:0 随机端口。
复用 tests/test_http_daemon_release_acceptance.py 的隔离启动助手与
tests/convergence/conftest.py 的种子 workspace 建库逻辑（同源，避免漂移）。

用法：python deliverables/software-company/doc-audit/run_t2_t3_evidence.py
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

# ---------------------------------------------------------------- sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
# _HERE = <repo>/deliverables/software-company/doc-audit → 三次 dirname 回仓库根
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_TESTS = os.path.join(_REPO_ROOT, "tests")
_CONV = os.path.join(_TESTS, "convergence")
for _p in (_REPO_ROOT, _TESTS, _CONV):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 解释器自检：callwarden_core 是 cp314 PyO3 扩展，只能被 Python 3.14 导入；
# pytest/mcp 等在 3.14 用户级 site-packages（自动启用）。低版本 python 运行
# 本脚本会给出明确提示而非晦涩的 ModuleNotFoundError。
try:
    import pytest  # noqa: F401
    import callwarden_core  # noqa: F401
except ImportError as _e:
    raise SystemExit(
        f"解释器不满足依赖（{type(_e).__name__}: {_e}）。\n"
        "本脚本需要 Python 3.14（callwarden_core 为 cp314 PyO3 扩展，"
        "pytest/mcp 在 3.14 用户级 site-packages）。请用 "
        "C:/Python314/python.exe 运行。"
    )

from test_http_daemon_release_acceptance import (  # noqa: E402
    _backup_http_manifest,
    _isolated_manifest_path,
    _restore_or_clean_http_manifest,
    _spawn_isolated_daemon,
    _terminate,
    _wait_manifest,
)

import param_provider as pp  # noqa: E402
import t2_mcp_runner as t2r  # noqa: E402
import t3_cli_runner as t3r  # noqa: E402

_OUT_DIR = _HERE
_RELEASE_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")
_DEBUG_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")
_SEED_SAMPLE = os.path.join(_CONV, "seed_sample")


def _pick_bin() -> str:
    if os.path.isfile(_RELEASE_BIN):
        return _RELEASE_BIN
    if os.path.isfile(_DEBUG_BIN):
        return _DEBUG_BIN
    raise RuntimeError("cw-daemon 未构建（需 cargo build --bin cw-daemon）")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _copy_seed(root: str) -> None:
    for fname in os.listdir(_SEED_SAMPLE):
        src = os.path.join(_SEED_SAMPLE, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(root, fname))


def _ensure_task_db_workspace(data_root: str, ws_id, name: str, root: str) -> None:
    import sqlite3

    task_db = os.path.join(data_root, "task.db")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_codegraph_seed_tables(data_root: str) -> None:
    """复刻 conftest._ensure_codegraph_seed_tables（隔离 codegraph 库补 5 表）。"""
    import sqlite3

    codegraph_db = os.path.join(data_root, ".callwarden", "callwarden.db")
    ddl = [
        """CREATE TABLE IF NOT EXISTS workspace_build_contexts (
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    name TEXT DEFAULT '',
    compile_flags TEXT DEFAULT '[]',
    defines TEXT DEFAULT '{}',
    include_paths TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, build_context_hash)
)""",
        """CREATE TABLE IF NOT EXISTS resolved_edges (
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
)""",
        "CREATE INDEX IF NOT EXISTS idx_build_contexts_ws ON workspace_build_contexts(workspace_id)",
        "CREATE INDEX IF NOT EXISTS idx_build_contexts_active ON workspace_build_contexts(workspace_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_resolved_edges_ws_ctx ON resolved_edges(workspace_id, build_context_hash)",
        "CREATE INDEX IF NOT EXISTS idx_resolved_edges_caller ON resolved_edges(caller_symbol_id)",
        "CREATE INDEX IF NOT EXISTS idx_resolved_edges_callee ON resolved_edges(callee_symbol_id)",
        """CREATE TABLE IF NOT EXISTS clone_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    group_hash TEXT NOT NULL,
    clone_type INTEGER NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    similarity REAL DEFAULT 0.0,
    representative_symbol_id INTEGER NOT NULL,
    member_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, group_hash)
)""",
        """CREATE TABLE IF NOT EXISTS clone_group_members (
    group_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, symbol_id)
)""",
        """CREATE TABLE IF NOT EXISTS clone_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    symbol_a_id INTEGER NOT NULL,
    symbol_b_id INTEGER NOT NULL,
    clone_type INTEGER NOT NULL,
    similarity REAL NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    lines_a INTEGER DEFAULT 0,
    lines_b INTEGER DEFAULT 0,
    detected_at REAL NOT NULL
)""",
    ]
    conn = sqlite3.connect(codegraph_db, timeout=15)
    try:
        for stmt in ddl:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _build_t2_seed(endpoint: str, data_root: str):
    """镜像 conftest.seed_workspace：注册 → build_graph → 补表 → publish → ctx。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    from param_provider import SeedContext

    root = tempfile.mkdtemp(prefix="cw_t2_ws_")
    _copy_seed(root)
    client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                                 validate_manifest=False, timeout=180)
    reg = client.call("workspace.register", {
        "name": f"t2-evidence-{uuid.uuid4().hex[:8]}",
        "client_view_root": root,
        "description": "T2 evidence re-run seed workspace",
    })
    ws_id = reg.get("workspace_id")
    ws_inst = reg.get("workspace_instance_id")
    _ensure_task_db_workspace(data_root, ws_id, reg.get("name", "t2"), root)

    client.call("workspace.build_graph", {"workspace_instance_id": ws_inst})
    _ensure_codegraph_seed_tables(data_root)

    db_result = client.call("mcp.common.get_db_path_for_daemon", {})
    db_path = db_result.get("db_path") if isinstance(db_result, dict) else None
    stats = None
    known_qnames: list = []
    known_files: list = []
    if db_path:
        try:
            client.call("snapshot.publish", {
                "workspace_instance_id": ws_inst,
                "build_context_hash": "",
                "db_path": db_path,
            })
            stats = client.call("query.stats", {"workspace_instance_id": ws_inst})
            found = client.call("query.search", {
                "workspace_instance_id": ws_inst, "query": "compute", "limit": 20,
            })
            rows = found if isinstance(found, list) else found.get("results", []) \
                if isinstance(found, dict) else []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                qn = r.get("qualified_name") or r.get("name")
                if qn and qn not in known_qnames:
                    known_qnames.append(qn)
                fp = r.get("file_path") or r.get("rel_path")
                if fp and fp not in known_files:
                    known_files.append(fp)
        except Exception:
            pass
    if not known_qnames:
        known_qnames = ["compute", "add", "multiply"]
    if not known_files:
        known_files = ["calc.py", "service.ts"]

    ctx = SeedContext(
        workspace_id=ws_id, workspace_instance_id=ws_inst, root=root,
        known_qualified_names=known_qnames,
        known_callee_names=["add", "multiply"],
        known_file_paths=known_files,
    )
    return {"client": client, "ctx": ctx, "root": root, "ws_id": ws_id,
            "ws_inst": ws_inst, "stats": stats}


def _run_t2(seed, endpoint: str):
    """镜像 mcp_on_isolated_daemon：重定向全局 RPC 单例 → create_mcp_server → run_all。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    from callwarden.server.mcp_server import create_mcp_server

    client = seed["client"]
    saved = HttpDaemonRpcClient._instance
    client.configure_workspace(seed["root"])
    HttpDaemonRpcClient._instance = client
    try:
        mcp = create_mcp_server()
        client.configure_workspace(seed["root"])
        return asyncio.run(t2r.run_all(mcp, seed["ctx"], op_filter="ALL"))
    finally:
        HttpDaemonRpcClient._instance = saved


def _run_t3(endpoint: str, data_root: str):
    """镜像 test_t3 的 seed_cli_workspace：独立 CWD 绑定种子 → run_all。

    CLI 子进程经 CW_DAEMON_HTTP_ENDPOINT 直连隔离 daemon。必须同时重定向
    USERPROFILE=data_root：authority 是 SID 不变，但 manifest 查找目录随
    USERPROFILE（http_manifest_dir），不重定向会命中真实 ~/.callwarden 的生产
    manifest 并校验它，引入假 DEFECT。daemon 侧同样以 USERPROFILE=data_root 启动，
    故 CLI 在重定向后的目录能找到隔离 daemon 自己发布的 authority-scoped manifest。
    """
    root = tempfile.mkdtemp(prefix="cw_t3_ws_")
    _copy_seed(root)
    env = dict(os.environ)
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["CW_DAEMON_HTTP_ENDPOINT"] = endpoint
    env["USERPROFILE"] = data_root
    env["PYTHONUTF8"] = "1"
    r = subprocess.run(
        [sys.executable, "-u", os.path.join(_REPO_ROOT, "cw.py"), "refresh", "--all"],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=env,
    )
    # 补齐隔离 codegraph 库缺的读面 5 表（同 conftest seed_workspace）。
    # T3 须自给自足，不能依赖 T2 先跑过（T2 的 _build_t2_seed 也会建表，
    # 但执行顺序不应成为 T3 零 DEFECT 的隐含前提）。
    _ensure_codegraph_seed_tables(data_root)
    ctx = pp.SeedContext(
        root=root, known_qualified_names=["compute", "add", "multiply"],
        known_callee_names=["add", "multiply"], known_file_paths=["calc.py"],
    )
    # run_one 从 os.environ 拷贝环境，须在进程级注入端点 + USERPROFILE 重定向
    saved = {
        "CW_DAEMON_TRANSPORT": os.environ.get("CW_DAEMON_TRANSPORT"),
        "CW_DAEMON_HTTP_ENDPOINT": os.environ.get("CW_DAEMON_HTTP_ENDPOINT"),
        "USERPROFILE": os.environ.get("USERPROFILE"),
        "PYTHONUTF8": os.environ.get("PYTHONUTF8"),
    }
    os.environ["CW_DAEMON_TRANSPORT"] = "http"
    os.environ["CW_DAEMON_HTTP_ENDPOINT"] = endpoint
    os.environ["USERPROFILE"] = data_root
    os.environ["PYTHONUTF8"] = "1"
    try:
        report = t3r.run_all(ctx, cwd=root, timeout=60)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return report, r.returncode, root


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _md_table_counts(counts: dict) -> str:
    total = sum(counts.values())
    rows = [("PASS", "rc==0 / call_tool 非 error"),
            ("EXPECTED_BUSINESS", "合理业务拒绝（缺参/not_found/snapshot/identity 等）"),
            ("DEFECT", "真实缺陷（method_not_found / traceback / internal_error）"),
            ("SKIP", "破坏性/重操作/阻塞命令")]
    lines = ["| 分类 | 数量 | 含义 |", "| --- | --- | --- |"]
    for k, desc in rows:
        lines.append(f"| {k} | {counts.get(k, 0)} | {desc} |")
    lines.append(f"| **合计** | **{total}** | |")
    return "\n".join(lines)


def main() -> int:
    bin_path = _pick_bin()
    bin_sha = _sha256(bin_path)
    head = _git_head()
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data_root = tempfile.mkdtemp(prefix="cw_evidence_")
    manifest_path = _isolated_manifest_path(data_root)
    backup = _backup_http_manifest(manifest_path)
    proc = _spawn_isolated_daemon(bin_path, data_root)
    try:
        manifest = _wait_manifest(proc, data_root, timeout=30)
        if manifest is None:
            out = (proc.stdout.read(8000).decode("utf-8", "replace") if proc.stdout else "")
            err = (proc.stderr.read(8000).decode("utf-8", "replace") if proc.stderr else "")
            raise RuntimeError(f"隔离 daemon 未发布 manifest\nstdout={out}\nstderr={err}")
        endpoint = manifest["endpoint"]
        print(f"[evidence] 隔离 daemon 就绪: {endpoint} (pid={proc.pid})")
        print(f"[evidence] data_root={data_root}")
        print(f"[evidence] daemon sha256={bin_sha[:16]} git HEAD={head}")

        meta_common = {
            "generated_at": now,
            "git_head": head,
            "daemon_binary": os.path.basename(bin_path),
            "daemon_sha256": bin_sha,
            "daemon_endpoint": endpoint,
            "isolation": "完全隔离 data_root（registry/task DB/manifest/锁），未触碰生产 daemon",
        }

        # ---------------- T2 ----------------
        t0 = time.time()
        seed = _build_t2_seed(endpoint, data_root)
        print(f"[evidence] T2 种子就绪 (ws_id={seed['ws_id']}, "
              f"stats={bool(seed['stats'])}, {time.time()-t0:.1f}s)")
        t2 = _run_t2(seed, endpoint)
        print(f"[evidence] T2 完成: {t2['counts']} ({time.time()-t0:.1f}s)")
        t2_json = {
            "op_filter": "ALL",
            "counts": t2["counts"],
            "results": t2["results"],
            "seed": {
                "workspace_id": seed["ws_id"],
                "workspace_instance_id": seed["ws_inst"],
                "known_qnames": seed["ctx"].known_qualified_names,
                "known_files": seed["ctx"].known_file_paths,
            },
            "meta": dict(meta_common, suite="T2 MCP 243 工具全参数真实调用"),
        }
        _write_json(os.path.join(_OUT_DIR, "t2_mcp_result.json"), t2_json)

        t2_defects = [r for r in t2["results"] if r["verdict"] == "DEFECT"]
        t2_md = f"""# T2 实测报告：243 个 MCP 工具全参数真实调用

日期：{now}
环境：Windows，隔离 daemon（git HEAD {head}，二进制 {bin_sha[:16]}，
endpoint {endpoint}，完全隔离 data_root，**未触碰生产 daemon**），
种子 workspace（build_graph + snapshot publish + codegraph 读面 5 表补齐）。

## 1. 测试目标与方法

对全部 **243 个 MCP 工具**做**全参数真实调用**（create_mcp_server 装配 →
FastMCP.call_tool → route_rpc → daemon RPC），验证工具可路由、daemon 可执行，
定位真实缺陷。分层：READ_ONLY 全调；PROTECTED_MUTATION 非破坏性全调、破坏性 SKIP；
GOVERNANCE_WRITE 需 lease/状态机前置 SKIP。

## 2. 结果汇总

{_md_table_counts(t2['counts'])}

## 3. DEFECT 明细（{len(t2_defects)}）

""" + ("\n".join(
            f"- **{r['name']}** [{r['op']}]: {r['detail']}" for r in t2_defects
        ) if t2_defects else "**零 DEFECT**——243 工具全量真实调用无 method_not_found / "
            "daemon 不可用 / 代码级 internal_error。") + "\n"
        with open(os.path.join(_OUT_DIR, "t2_mcp_full_invocation_report.md"),
                  "w", encoding="utf-8") as f:
            f.write(t2_md)

        # ---------------- T3 ----------------
        t0 = time.time()
        t3, refresh_rc, t3_root = _run_t3(endpoint, data_root)
        print(f"[evidence] T3 完成: {t3['counts']} (refresh rc={refresh_rc}, "
              f"{time.time()-t0:.1f}s)")
        t3_json = {
            "counts": t3["counts"],
            "results": t3["results"],
            "seed": {
                "root": t3_root,
                "refresh_rc": refresh_rc,
                "known_qnames": ["compute"],
                "known_files": ["calc.py"],
            },
            "meta": dict(meta_common, suite="T3 CLI 234 叶子命令全参数真实调用"),
        }
        _write_json(os.path.join(_OUT_DIR, "t3_cli_result.json"), t3_json)

        t3_defects = [r for r in t3["results"] if r["verdict"] == "DEFECT"]
        mn = [r for r in t3_defects if "method_not_found" in r.get("detail", "")]
        tb = [r for r in t3_defects if "Traceback" in r.get("detail", "")]
        t3_md = f"""# T3 实测报告：234 个 CLI 叶子命令全参数真实调用

日期：{now}
环境：Windows，隔离 daemon（git HEAD {head}，二进制 {bin_sha[:16]}，
endpoint {endpoint}，完全隔离 data_root，**未触碰生产 daemon**），
种子 workspace（CWD 绑定 + refresh --all，refresh rc={refresh_rc}）。

## 1. 测试目标与方法

对全部 **234 个 CLI 叶子命令**做**全参数真实调用**（subprocess 执行
`python cw.py <argv>`，cwd=种子 workspace），验证命令可执行、不崩溃，
定位真实缺陷。分层：只读全执行；破坏性/重操作/阻塞 SKIP。

## 2. 结果汇总

{_md_table_counts(t3['counts'])}

## 3. DEFECT 明细（{len(t3_defects)}）

- method_not_found：{len(mn)}
- Python traceback：{len(tb)}
- 其它：{len(t3_defects) - len(mn) - len(tb)}

""" + ("\n".join(
            f"- **{r['cmd']}** ({_cls}): {r['detail'][:160]}"
            for r, _cls in [(r, 'method_not_found' if 'method_not_found' in r.get('detail', '')
                             else 'traceback' if 'Traceback' in r.get('detail', '') else '其它')
                            for r in t3_defects]
        ) if t3_defects else "**零 DEFECT**。") + "\n"
        with open(os.path.join(_OUT_DIR, "t3_cli_full_invocation_report.md"),
                  "w", encoding="utf-8") as f:
            f.write(t3_md)

        print("[evidence] 交付物已刷新：t2/t3 result.json + report.md")
        return 0
    finally:
        _terminate(proc)
        _restore_or_clean_http_manifest(manifest_path, proc.pid, backup)
        shutil.rmtree(data_root, ignore_errors=True)
        os.environ.pop("CW_DAEMON_HTTP_ENDPOINT", None)
        os.environ.pop("CW_DAEMON_TRANSPORT", None)


if __name__ == "__main__":
    raise SystemExit(main())
