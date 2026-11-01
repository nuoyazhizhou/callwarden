"""阶段 3「测试覆盖加强」：27 个新增 subcommand handler 的端到端测试。

背景：`docs/architecture.md` 阶段 3 第三项要求「为 27 个新增 subcommand handler
编写端到端测试」。本文件以**真实进程 + 真实 daemon** 验证，而不是进程内直接调
handler：

- 每个用例 spawn `python cw.py <argv>` 子进程（argparse → dispatch → handler →
  daemon RPC → 渲染输出 全链路），断言退出码、结构化输出关键字、无 Python
  traceback；
- 运行环境完全隔离：独立 `data_root` / `USERPROFILE` / task DB / registry DB /
  命名管道 / HTTP 随机端口，退出即 kill 进程，全程不触碰生产库（`~/.callwarden`）；
- 隔离栈装配走既有 `tests/_w3_harness.py`（模式 A USERPROFILE 重定向 +
  `workspace.register` + 空 codegraph `snapshot.publish`），并 seed 一个已绑定
  workspace 的探针任务，使 `lease status` / `assignment show` 能走通权威 resolver。

覆盖清单（27 条，冻结于 `CASES`，`test_frozen_case_count_is_27` 锁定数量）：

| 主分类 | 命令 |
| --- | --- |
| Workspace & Database | `workspace scan` |
| Query & Search | `semantic-search` |
| Call Chain Analysis | `top-callers` / `detect-cycles` / `export-module-graph` |
| Git Integration | `git log` / `git stats` / `git destructive-log` |
| Semgrep | `semgrep list` / `semgrep stats` |
| Coverage | `coverage fn` / `coverage uncovered` |
| Diagnostics | `config explain` / `config paths` |
| Agent Rule Memory | `rule list` / `rule applicable` / `rule extract` / `guardrail rules` |
| GC | `gc status` / `gc archive-list` / `gc audit-list` |
| Clone | `clone stats` |
| Defect | `defect stats` |
| Audit & Bootstrap | `audit verify` / `bootstrap status` |
| Assignment & Lease | `lease status` / `assignment show` |
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CW_PY = REPO_ROOT / "cw.py"
PYTHON = sys.executable

for _d in (str(REPO_ROOT), str(REPO_ROOT / "tests")):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 探针任务：seed 进隔离 task DB 并绑定 workspace，供 lease/assignment 走权威 resolver。
PROBE_TASK = "T-CLI-E2E-27"


# ---------------------------------------------------------------------------
# 冻结清单：27 个新增 subcommand handler（命令 / argv / 结构化输出关键字）
# ---------------------------------------------------------------------------
CASES = [
    # Workspace & Database
    ("workspace scan", ["workspace", "scan"], ["Found 0 subprojects"]),
    # Query & Search
    ("semantic-search", ["semantic-search", "probe"], ["Semantic search: 'probe'"]),
    # Call Chain Analysis
    ("top-callers", ["top-callers"], ["Most called functions (top 0)"]),
    ("detect-cycles", ["detect-cycles"], ["No cycles detected"]),
    ("export-module-graph", ["export-module-graph"], ["flowchart TD"]),
    # Git Integration
    ("git log", ["git", "log"], ["Git commit history (3 entries)"]),
    ("git stats", ["git", "stats"], ["Total commits: 3"]),
    ("git destructive-log", ["git", "destructive-log"], ["No destructive operations recorded"]),
    # Semgrep
    ("semgrep list", ["semgrep", "list"], ["Semgrep findings (all, 0 total"]),
    ("semgrep stats", ["semgrep", "stats"], ["Total findings: 0"]),
    # Coverage
    ("coverage fn", ["coverage", "fn", "probe_fn"], ["Function not found: probe_fn"]),
    ("coverage uncovered", ["coverage", "uncovered"], ["Under-covered functions"]),
    # Diagnostics
    ("config explain", ["config", "explain"], ["N4 分层配置"]),
    ("config paths", ["config", "paths"], ["N4 PlatformPaths"]),
    # Agent Rule Memory
    ("rule list", ["rule", "list"], ["Active Rules (0)"]),
    ("rule applicable", ["rule", "applicable"], ["Applicable Rules (0)"]),
    ("rule extract", ["rule", "extract"], ["Extracted Candidates"]),
    ("guardrail rules", ["guardrail", "rules"], ["Guardrail Rules"]),
    # GC
    ("gc status", ["gc", "status"], ["GC Status"]),
    ("gc archive-list", ["gc", "archive-list"], ["GC Archive List"]),
    ("gc audit-list", ["gc", "audit-list"], ["GC Audit List"]),
    # Clone
    ("clone stats", ["clone", "stats"], ["Clone detection statistics"]),
    # Defect
    ("defect stats", ["defect", "stats"], ["Defect Knowledge Base Stats"]),
    # Audit & Bootstrap
    ("audit verify", ["audit", "verify"], ["Audit Chain Verification"]),
    ("bootstrap status", ["bootstrap", "status"], ["Bootstrap Health Summary"]),
    # Assignment & Lease
    ("lease status", ["lease", "status", PROBE_TASK], ["Lease Status", "status: none"]),
    ("assignment show", ["assignment", "show", PROBE_TASK], ["Active Assignment"]),
]

CASE_IDS = [case[0] for case in CASES]

# 失败标记：CLI 把 daemon/业务错误统一渲染成 "✗ Subcommand '<x>' failed: ..."
# （英文环境为 "Subcommand '<x>' failed: ..."）。成功用例不得出现。
_FAIL_MARKERS = ("Traceback (most recent call last)", "Subcommand '", "执行子命令")


# ---------------------------------------------------------------------------
# 隔离 daemon 装配
# ---------------------------------------------------------------------------
def _terminate(proc: subprocess.Popen) -> None:
    """结束隔离 daemon 进程（terminate → kill 兜底）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 —— teardown 不得因平台差异失败
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _seed_probe_task(task_db: str, root_path: str) -> None:
    """在隔离 task DB 种入 workspace + 已绑定探针任务。

    daemon 启动时已按权威 schema 初始化 task DB，故此处只追加最小行；
    `task_workspace_bindings` 让 `lease status` / `assignment show` 的权威
    resolver 命中绑定（否则 fail-closed 为 E_TASK_WORKSPACE_UNBOUND）。
    """
    conn = sqlite3.connect(task_db, timeout=15)
    try:
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active)"
            " VALUES (1, 'cli-e2e-ws', ?, ?, 1)",
            (root_path, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO tasks"
            " (id, title, description, creator, status, created_at, updated_at, parent_id)"
            " VALUES (?, 'cli e2e probe', '', 'cli-e2e', 'open', ?, ?, '')",
            (PROBE_TASK, now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_workspace_bindings"
            " (task_id, workspace_id, workspace_binding_id, workspace_capture_id,"
            "  created_by, authoritative_created_at)"
            " VALUES (?, 1, ?, ?, 'cli-e2e', '0')",
            (PROBE_TASK, f"tb-{PROBE_TASK}", f"wc-{PROBE_TASK}"),
        )
        conn.commit()
    finally:
        conn.close()


def _task_rows(task_db: str) -> list:
    """只读快照 `tasks` + `task_workspace_bindings`，用于只读性断言。"""
    uri = "file:{}?mode=ro".format(task_db.replace("\\", "/"))
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        tasks = [tuple(r) for r in conn.execute(
            "SELECT id, status, updated_at FROM tasks ORDER BY id")]
        bindings = [tuple(r) for r in conn.execute(
            "SELECT task_id, workspace_id FROM task_workspace_bindings ORDER BY task_id")]
        return [tasks, bindings]
    finally:
        conn.close()


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory):
    """隔离 daemon + 权威栈 + 子进程环境，yield dict(env/root/task_db/before_rows)。"""
    from _prereq_guard import (  # noqa: PLC0415 —— 依赖 tests/ 在同路径
        read_proc_stderr,
        require_daemon_binary,
        skip_if_daemon_exited_before_manifest,
        skip_if_isolated_daemon_pipe_conflict,
    )
    import _w3_harness as harness  # noqa: PLC0415

    data_root = str(tmp_path_factory.mktemp("cli_e2e") / "data")
    os.makedirs(data_root, exist_ok=True)
    root_path = os.path.join(data_root, "repo")
    os.makedirs(root_path, exist_ok=True)
    task_db = os.path.join(data_root, "task.db")

    bin_path = require_daemon_binary(harness.find_daemon_binary())
    proc, daemon_env = harness.spawn_isolated_daemon(bin_path, data_root)
    try:
        manifest, _manifest_file = harness.wait_manifest(data_root, proc, timeout=60)
        if manifest is None:
            stderr_text = read_proc_stderr(proc)
            skip_if_daemon_exited_before_manifest(proc, stderr_text)
            pytest.fail(
                "隔离 daemon 未发布 manifest（进程仍存活 → 视为卡死，保持红）"
                f"\nbinary={bin_path}\nstderr={stderr_text}"
            )
        skip_if_isolated_daemon_pipe_conflict(proc)
        endpoint = manifest["endpoint"]
        assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"

        from server.daemon_client import HttpDaemonRpcClient  # noqa: PLC0415

        client = HttpDaemonRpcClient(
            endpoint=endpoint, verify_health=False, validate_manifest=False
        )
        ws = client.call("workspace.register", {"client_view_root": root_path})
        instance_id = ws["workspace_instance_id"]

        _seed_probe_task(task_db, root_path)

        codegraph_db = os.path.join(data_root, "userhome", ".callwarden", "callwarden.db")
        os.makedirs(os.path.dirname(codegraph_db), exist_ok=True)
        harness.build_empty_codegraph(codegraph_db, root_path, ws_id=1)
        client.call("snapshot.publish",
                    {"workspace_instance_id": instance_id, "db_path": codegraph_db})

        child_env = os.environ.copy()
        child_env.update({
            "USERPROFILE": os.path.join(data_root, "userhome"),
            "HOME": os.path.join(data_root, "userhome"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "CW_DAEMON_TRANSPORT": "http",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "CALLWARDEN_LANG": "en_US",
        })
        for key in ("CW_DAEMON_DATA_ROOT", "CW_DAEMON_TASK_DB",
                    "CW_DAEMON_REGISTRY_DB", "CW_DAEMON_SOCKET"):
            if key in daemon_env:
                child_env[key] = daemon_env[key]

        yield {
            "env": child_env,
            "root": root_path,
            "task_db": task_db,
            "endpoint": endpoint,
            "before_rows": _task_rows(task_db),
        }
    finally:
        _terminate(proc)


def _run(argv: list, env: dict, root: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    """spawn `python -u cw.py <argv>`，返回 CompletedProcess（UTF-8 解码）。"""
    return subprocess.run(
        [PYTHON, "-u", str(CW_PY)] + argv,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _combined(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "") + "\n" + (result.stderr or "")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label,argv,tokens", CASES, ids=CASE_IDS)
def test_cli_leaf_end_to_end(cli_env, label, argv, tokens):
    """27 个 handler：子进程真实调用成功、输出结构化、无 traceback / 失败标记。"""
    result = _run(argv, cli_env["env"], cli_env["root"])
    combined = _combined(result)

    assert result.returncode == 0, f"{label} rc={result.returncode}\n{combined[:1500]}"
    for marker in _FAIL_MARKERS:
        assert marker not in combined, f"{label} 出现失败标记 {marker!r}\n{combined[:1500]}"
    for token in tokens:
        assert token in result.stdout, (
            f"{label} 缺少结构化输出 {token!r}\n{result.stdout[:1500]}")


def test_frozen_case_count_is_27():
    """清单冻结：保持 27 条，新增/删减须同步 backlog §22 与本表。"""
    assert len(CASES) == 27
    assert len(CASE_IDS) == len(set(CASE_IDS)), "用例 id 重复"


def test_missing_required_arg_exits_2(cli_env):
    """argparse 层退出码：必填参数缺失 → rc=2 且打印 usage（不经 handler）。"""
    result = _run(["collab", "publish"], cli_env["env"], cli_env["root"])
    assert result.returncode == 2, f"rc={result.returncode}\n{_combined(result)[:800]}"
    assert "the following arguments are required" in result.stderr


def test_leaf_calls_are_read_only(cli_env):
    """27 条叶子命令只读：跑完后 `tasks` / `task_workspace_bindings` 不变。

    该用例依赖定义顺序（在全部 parametrize 用例之后执行）。
    """
    after = _task_rows(cli_env["task_db"])
    assert after == cli_env["before_rows"], "只读叶子命令不得改写 task DB"
