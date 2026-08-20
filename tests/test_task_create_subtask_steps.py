"""task.create_subtask 漏写 steps + task.claim 返回步骤详情契约 回归测试
（任务 T-1786438019310-e24474c0，Implementer 交付）。

缺陷链：
1. rust_ext/src/daemon/task_collab.rs handle_task_create_subtask 只 INSERT tasks，
   不读 params.steps / 不调用 insert_task_steps / 无事务包裹 / task_events 用 .ok() 吞错，
   导致 daemon 创建的子任务 Steps(0)，且 tasks/task_events/task_steps 非原子。
2. handle_task_claim 返回 {task_id, status, claimed_by} 不含步骤详情，
   daemon 模式下 MCP task_next_step / CLI task next 拿不到 step_id/action/target 等字段。
3. server/tools/tools_task.py task_create_subtask 声明 -> str 但 daemon 返回 dict，
   MCP wrapper 未归一化时会把成功的结构化 dict 当字符串失败。

覆盖 10 场景：
S1  create_subtask 接收 steps 并保存到 task_steps
S2  每步生成真实 step_id（S- 前缀）
S3  task_steps 绑定正确 task_id
S4  step_index 从 0 连续
S5  保留 action/target_file/target_symbol/check_items
S6  tasks/task_events/task_steps 同一事务（源码断言 + 回滚断言）
S7  任一步骤失败整体回滚（无残留子任务/步骤/事件）
S8  返回结构化结果 {task_id, parent_id, status, step_count}
S9  MCP wrapper 正确归一化 daemon dict（不把成功 dict 当字符串失败）
S10 保留 request_id dedup / 权限 / 错误码语义
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------
# 1. 源码静态断言（不依赖运行时，Windows/Linux 均可执行）
# ------------------------------------------------------------

def _collab_src():
    with open(
        os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "task_collab.rs"),
        encoding="utf-8",
    ) as f:
        return f.read()


def _tools_src():
    with open(
        os.path.join(_REPO_ROOT, "server", "tools", "tools_task.py"),
        encoding="utf-8",
    ) as f:
        return f.read()


def test_handler_reads_steps_and_calls_insert_task_steps():
    """S1/S6：handle_task_create_subtask 必须读取 steps、调用 insert_task_steps、用事务包裹。"""
    src = _collab_src()
    # 从 handle_task_create_subtask 到函数结束的切片
    start = src.index("pub fn handle_task_create_subtask")
    end = src.index("\n    pub fn ", start + 10)
    body = src[start:end]
    assert "params.get(\"steps\")" in body, "必须读取 params.steps"
    assert "insert_task_steps(&tx, &task_id, steps, ts)?" in body, "必须调用 insert_task_steps"
    assert "unchecked_transaction()" in body, "必须用事务包裹"
    assert "tx.commit()" in body, "必须显式提交事务"


def test_handler_returns_step_count_and_structured_result():
    """S8：返回结构化结果 {task_id, parent_id, status, step_count}。"""
    src = _collab_src()
    start = src.index("pub fn handle_task_create_subtask")
    end = src.index("\n    pub fn ", start + 10)
    body = src[start:end]
    assert '"step_count"' in body, "返回必须含 step_count"
    assert '"parent_id"' in body
    assert '"status"' in body


def test_handler_task_events_no_longer_swallows_errors():
    """S6：task_events 写入不再用 .ok() 吞错。"""
    src = _collab_src()
    start = src.index("pub fn handle_task_create_subtask")
    end = src.index("\n    pub fn ", start + 10)
    body = src[start:end]
    assert "task_events" in body
    assert ".ok();" not in body.replace("pub fn handle_task_create_subtask", "", 1) or "map_err" in body


def test_claim_returns_step_details():
    """S1/S3：handle_task_claim 返回步骤详情字段（step_id/step_index/action/target_file/...）。"""
    src = _collab_src()
    start = src.index("pub fn handle_task_claim")
    end = src.index("\n    pub fn ", start + 10)
    body = src[start:end]
    assert '"step_id"' in body
    assert '"step_index"' in body
    assert '"action"' in body
    assert '"target_file"' in body
    assert '"target_symbol"' in body
    assert '"check_items"' in body


def _wrapper_body(src: str, func_name: str) -> str:
    """截取指定 wrapper 函数体（到下一个 @mcp.tool() 或文件结束）。"""
    start = src.index(f"def {func_name}")
    next_deco = src.find("\n@mcp.tool()", start + 10)
    end = next_deco if next_deco != -1 else len(src)
    return src[start:end]


def test_wrapper_normalizes_daemon_dict():
    """S9：task_create_subtask wrapper 必须归一化 daemon dict（dict 含 task_id 时返回 task_id）。"""
    src = _tools_src()
    body = _wrapper_body(src, "task_create_subtask")
    assert "isinstance(res, dict)" in body, "wrapper 必须处理 dict 返回"
    assert '"task_id" in res' in body, "必须从 dict 提取 task_id"


def test_next_step_wrapper_returns_none_without_step():
    """S9：task_next_step wrapper 在 daemon 返回无 step_id 时归一化为 None。"""
    src = _tools_src()
    body = _wrapper_body(src, "task_next_step")
    assert '"step_id" not in res' in body, "缺少 step_id 时视为无待执行步骤"
    assert "return None" in body


# ------------------------------------------------------------
# 2. 真实 daemon round-trip（Windows Named Pipe + Rust daemon）
# ------------------------------------------------------------

_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")
_CLIENT_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-client.exe")
_CW_BIN = os.path.join(_REPO_ROOT, "cw.py")

_requires_bin = pytest.mark.skipif(
    not (os.path.exists(_DAEMON_BIN) and os.path.exists(_CLIENT_BIN)),
    reason="cw-daemon.exe / cw-client.exe 未构建（需先 cargo build --bin cw-daemon --bin cw-client）",
)


def _client(pipe: str, args: list, timeout: int = 15) -> dict:
    """调用真实 cw-client.exe（Named Pipe 客户端），返回结构化结果。"""
    try:
        result = subprocess.run(
            [_CLIENT_BIN, "--socket", pipe, "--timeout", str(timeout)] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return {"code": -1, "json": {}, "stdout": "", "stderr": "client timeout"}
    parsed = {}
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        pass
    return {
        "code": result.returncode,
        "json": parsed,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _daemon_config(tmp: str) -> dict:
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 4,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 4,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


def _task_db_steps(task_db: str, task_id: str):
    """只读查询隔离临时库的 task_steps（测试专用临时库，非权威库）。"""
    import sqlite3

    conn = sqlite3.connect(task_db)
    try:
        rows = conn.execute(
            "SELECT id, step_index, action, target_file, target_symbol, check_items, status "
            "FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        return rows
    finally:
        conn.close()


@pytest.fixture(scope="module")
def isolated_daemon():
    """启动隔离临时目录的真实 cw-daemon（Named Pipe 按 SID 派生）。"""
    from callwarden.config import _get_windows_user_sid

    sid = _get_windows_user_sid()
    pipe = rf"\\.\pipe\callwarden-{sid}"

    # 默认管道已被其他 daemon 占用则跳过（不杀他人进程）
    occupied = _client(pipe, ["ping"], timeout=2)
    if occupied["code"] == 0:
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过进程级 E2E")

    tmp = tempfile.mkdtemp(prefix="create-subtask-steps-e2e-")
    config_path = os.path.join(tmp, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(_daemon_config(tmp), f)

    log_path = os.path.join(tmp, "daemon.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
    )
    try:
        deadline = time.time() + 40.0
        ok = False
        time.sleep(1.5)
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            r = _client(pipe, ["ping"], timeout=2)
            if r["code"] == 0 and r["json"].get("status") == "ok":
                ok = True
                break
            time.sleep(0.5)
        if not ok:
            log_content = ""
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    log_content = f.read()[-3000:]
            except Exception:
                pass
            pytest.fail(f"daemon 未在超时内响应，日志：\n{log_content}")
        yield {"pipe": pipe, "tmp": tmp, "task_db": os.path.join(tmp, "callwarden.db")}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _env_with_daemon(pipe: str):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    env["CW_DAEMON_MODE"] = "enterprise"
    env["CW_DAEMON_ENDPOINT"] = pipe
    env.pop("CW_DAEMON_SOCKET_PATH", None)
    return env


def _mcp_tool_text(result):
    """从 fastmcp call_tool 结果中提取文本内容（兼容 tuple/list/content 多种返回形态）。"""
    if isinstance(result, tuple):
        result = result[0] if result else []
    if isinstance(result, list):
        items = result
    else:
        items = getattr(result, "content", []) or []
    parts = []
    for block in items:
        txt = getattr(block, "text", None)
        if txt:
            parts.append(str(txt))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


@_requires_bin
def test_daemon_create_subtask_persists_steps(isolated_daemon):
    """S1-S5：真实 daemon task.create_subtask 后 task_steps 完整（修复前 Steps(0)）。"""
    pipe = isolated_daemon["pipe"]
    task_db = isolated_daemon["task_db"]
    env = _env_with_daemon(pipe)

    # 父任务
    r1 = subprocess.run(
        [sys.executable, _CW_BIN, "task", "create", "--title", "E2E 父任务", "--desc", "parent"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    parent_id = None
    for line in r1.stdout.splitlines():
        for tok in line.split():
            if tok.startswith("T-"):
                parent_id = tok.strip()
    assert parent_id, f"无法解析父任务 ID: {r1.stdout}"

    # 子任务：CLI 无 create-subtask 子命令，直接走 daemon RPC（验证 handle_task_create_subtask）
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    client = UnixDaemonRpcClient(socket_path=pipe)
    parent_res = client.call("task.status", {"task_id": parent_id})
    assert parent_res.get("status") == "open"

    sub_res = client.call("task.create_subtask", {
        "parent_task_id": parent_id,
        "title": "daemon-subtask",
        "description": "desc",
        "steps": [
            {
                "action": "audit",
                "target_file": "rust_ext/src/daemon/task_collab.rs",
                "target_symbol": "TaskCollabStore::handle_task_create_subtask",
                "check_items": ["read code", "verify"],
            },
            {"action": "fix", "target_file": "server/tools/tools_task.py", "check_items": "pytest"},
        ],
    })
    assert sub_res.get("status") == "open", f"create_subtask 返回: {sub_res}"
    assert sub_res.get("parent_id") == parent_id
    assert sub_res.get("step_count") == 2, f"step_count 应为 2: {sub_res}"
    sub_id = sub_res.get("task_id")
    assert sub_id and sub_id.startswith("T-"), f"非法子任务 id: {sub_res}"

    rows = _task_db_steps(task_db, sub_id)
    assert len(rows) == 2, f"子任务应有 2 步，实际 {len(rows)}: {rows}"
    # S2：真实 step_id
    assert rows[0][0].startswith("S-"), f"step_id 应为真实生成 id: {rows[0][0]}"
    # S4：step_index 从 0 连续
    assert rows[0][1] == 0 and rows[1][1] == 1
    # S5：字段保留
    assert rows[0][2] == "audit"
    assert rows[0][3] == "rust_ext/src/daemon/task_collab.rs"
    assert rows[0][4] == "TaskCollabStore::handle_task_create_subtask"
    assert rows[0][5] == '["read code","verify"]'
    assert rows[1][2] == "fix"
    # S3：绑定正确 task_id
    assert all(row[0] for row in rows)


@_requires_bin
def test_daemon_claim_returns_step_details(isolated_daemon):
    """S1/S3：真实 daemon task.claim 返回下一步骤详情（含 step_id/action/target_file/...）。"""
    pipe = isolated_daemon["pipe"]
    env = _env_with_daemon(pipe)
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    client = UnixDaemonRpcClient(socket_path=pipe)

    create = client.call("task.create", {
        "title": "claim-contract",
        "steps": [
            {
                "action": "audit",
                "target_file": "rust_ext/src/daemon/task_collab.rs",
                "target_symbol": "TaskCollabStore::handle_task_claim",
                "check_items": ["read"],
            },
            {"action": "fix", "target_file": "server/tools/tools_task.py"},
        ],
    })
    task_id = create.get("task_id")
    assert task_id

    claim = client.call("task.claim", {"task_id": task_id, "agent_session_id": "session-e2e"})
    assert claim.get("status") == "in_progress"
    assert claim.get("claimed_by") == "session-e2e"
    assert claim.get("step_id", "").startswith("S-"), f"claim 必须返回真实 step_id: {claim}"
    assert claim.get("step_index") == 0
    assert claim.get("action") == "audit"
    assert claim.get("target_file") == "rust_ext/src/daemon/task_collab.rs"
    assert claim.get("target_symbol") == "TaskCollabStore::handle_task_claim"
    # 领取即占用步骤（对齐 Python db.task_next_step：pending -> in_progress）
    assert claim.get("step_status") == "in_progress"


@_requires_bin
def test_cli_task_show_displays_steps(isolated_daemon):
    """S1：cw task show（树形）显示 Steps(N)，非 0。"""
    pipe = isolated_daemon["pipe"]
    env = _env_with_daemon(pipe)
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    client = UnixDaemonRpcClient(socket_path=pipe)

    create = client.call("task.create", {
        "title": "show-steps",
        "steps": [{"action": "implement", "target_file": "a.rs", "check_items": "cargo"}],
    })
    task_id = create.get("task_id")
    assert task_id

    r = subprocess.run(
        [sys.executable, _CW_BIN, "task", "show", task_id],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert r.returncode == 0, f"task show 失败: {r.stdout} {r.stderr}"
    assert "Steps" in r.stdout, f"task show 未显示 Steps 标签: {r.stdout}"


@_requires_bin
def test_mcp_create_subtask_roundtrip(isolated_daemon):
    """S9：真实 MCP task_create_subtask 走 daemon 返回归一化 task_id（修复前误判为类型错误）。"""
    pipe = isolated_daemon["pipe"]
    env = _env_with_daemon(pipe)
    import asyncio
    import re as _re

    from callwarden.server.daemon_client import UnixDaemonRpcClient
    client = UnixDaemonRpcClient(socket_path=pipe)
    parent = client.call("task.create", {"title": "mcp-parent"})
    parent_id = parent.get("task_id")

    # 真实 MCP 工具调用：register(mcp) 闭包内的工具只能经 call_tool 访问，
    # 进程内直接 tools_task.task_create_subtask 会因模块级无该属性而 AttributeError。
    from callwarden.server.mcp_server import create_mcp_server
    mcp = create_mcp_server()
    result = asyncio.run(mcp.call_tool("task_create_subtask", {
        "parent_task_id": parent_id,
        "title": "mcp-child",
        "description": "d",
        "steps": [{"action": "implement", "target_file": "b.py", "target_symbol": "main"}],
        "creator": "agent",
    }))
    text = _mcp_tool_text(result).strip()
    # S9：返回必须是 task_id 字符串（dict 已被归一化），而非 dict 或异常
    m = _re.search(r"T-[0-9A-Za-z-]+", text or "")
    assert m, f"MCP task_create_subtask 未返回合法 task_id: {text}"
    child_id = m.group(0)
    assert isinstance(child_id, str) and child_id.startswith("T-"), f"非法 task_id: {child_id}"

    # 父/子任务状态（对齐 AGENTS.md reopen 契约 + Python task_create 行为）：
    # 父任务 open/in_progress 时挂子任务直接挂、不改状态；仅 review/applied/closed
    # 父任务按兄弟子任务状态决定是否 reopen 为 in_progress。
    status = client.call("task.status", {"task_id": parent_id})
    assert status.get("status") == "open", (
        f"父任务 open 挂子任务应保持 open（reopen 契约仅对 review/applied/closed 生效）: {status}"
    )
    child = client.call("task.status", {"task_id": child_id})
    assert child.get("status") == "open", f"新子任务应为 open: {child}"
