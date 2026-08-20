"""H3: compat worker / registry 的进程级与帧协议测试。

覆盖（契约 docs/design/http-daemon-mvp-compatibility-contract.md §3.3）：
- 帧编解码 roundtrip / EOF / 超长拒绝
- 帧校验 fail-closed：缺字段、错误协议版本、db_path 禁止、governance_write 禁止
- handle_frame 成功路径：stats_top_files（get_uncommented_symbols 已 W2-1
  迁移 rust_native，见 test_migrated_native_method_not_served_by_worker）
- 异常路径：未知方法、表缺失（E_COMPAT_EXECUTION_ERROR）
- 子进程 main() roundtrip：真实 stdin/stdout 帧协议 + EOF 正常退出
- 子进程协议损坏：垃圾输入回 E_COMPAT_WORKER_PROTOCOL 帧

隔离：in-process 测试 monkeypatch `callwarden.config.DB_PATH`；子进程测试
重定向 `USERPROFILE` 到临时目录（config.py 用 expanduser 解析数据库根目录），
worker 通过 authority 配置解析到临时 DB，不触碰真实用户数据库。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# 仓库根目录（tests/ 的父目录）加入 sys.path，保证 server/ 与 callwarden 包可导入
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server import compat_registry as reg  # noqa: E402
from server.compat_worker import (  # noqa: E402
    ERR_DBPATH_FORBIDDEN,
    ERR_EXECUTION,
    ERR_GOVERNANCE_FORBIDDEN,
    ERR_METHOD_NOT_FOUND,
    ERR_PROTOCOL,
    MAX_FRAME_BYTES,
    WORKER_PROTOCOL_VERSION,
    handle_frame,
    read_frame,
    write_frame,
)
from server.compat_worker import main as worker_main  # noqa: E402

# ---------------------------------------------------------------
# 测试工具：隔离临时 DB
# ---------------------------------------------------------------

# 与 db/schema.py 一致的列子集（仅覆盖 worker 查询所需列；无 FK 以便独立建表）
MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    qualified_name TEXT DEFAULT '',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    has_comment INTEGER DEFAULT 0
);
"""

SAMPLE_ROWS = [
    # (workspace_id, rel_path, name, kind, qualified_name, start_line, end_line, has_comment)
    (1, "src/app.py", "run", "function", "app.run", 1, 5, 0),
    (1, "src/app.py", "helper", "function", "app.helper", 10, 15, 1),
    (1, "src/util.py", "parse", "function", "util.parse", 20, 30, 0),
    (1, "src/util.py", "Token", "class", "util.Token", 40, 60, 0),
    (2, "other/main.py", "main", "function", "main.main", 1, 9, 0),
]


def make_temp_db(tmp_path: Path, workspace_id: int = 1) -> str:
    """建一个含最小 schema + sample 数据的临时用户级 DB，返回路径。"""
    db_path = str(tmp_path / "callwarden.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(MINIMAL_SCHEMA)
        for (ws, rel, name, kind, qn, sl, el, hc) in SAMPLE_ROWS:
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, status) VALUES (?, ?, 'parsed')",
                (ws, rel),
            )
            fi_id = conn.execute(
                "SELECT id FROM file_instances WHERE workspace_id=? AND rel_path=?",
                (ws, rel),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO symbols
                   (file_instance_id, name, kind, qualified_name, start_line, end_line, has_comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fi_id, name, kind, qn, sl, el, hc),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _base_frame(**overrides) -> dict:
    # W2-1（T-1786840097330-dec66710）：默认方法改用仍注册于 worker 的
    # stats_top_files（get_uncommented_symbols 已迁移 rust_native，worker 不再受理）
    frame = {
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "request_id": "req-1",
        "method": "stats_top_files",
        "params": {"limit": 10},
        "workspace_instance_id": "ws-inst-1",
        "workspace_id": 1,
        "operation_class": "read_only",
        "deadline": 123456789,
    }
    frame.update(overrides)
    return frame


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """in-process 测试统一把 config.get_project_db_path 指向临时 DB。

    注意：conftest.py 的 autouse fixture 已把 get_project_db_path 替换为
    tmp_path/test_isolated.db（不创建文件）；本 fixture 叠加 monkeypatch，
    使其指向我们建好的、含 seed 数据的临时 DB（后设置者生效）。
    """
    db_path = make_temp_db(tmp_path)
    import callwarden.config as cfg

    monkeypatch.setattr(cfg, "get_project_db_path", lambda project_root="": db_path)
    yield db_path


# ---------------------------------------------------------------
# 帧编解码
# ---------------------------------------------------------------


def test_frame_roundtrip():
    buf = io.BytesIO()
    frame = _base_frame()
    write_frame(buf, frame)
    buf.seek(0)
    decoded = read_frame(buf)
    assert decoded == frame


def test_read_frame_eof_returns_none():
    assert read_frame(io.BytesIO(b"")) is None


def test_read_frame_rejects_oversized_length():
    buf = io.BytesIO((MAX_FRAME_BYTES + 1).to_bytes(4, byteorder="big"))
    with pytest.raises(ValueError):
        read_frame(buf)


def test_write_frame_rejects_oversized():
    buf = io.BytesIO()
    with pytest.raises(ValueError):
        write_frame(buf, {"payload": "x" * (MAX_FRAME_BYTES + 1)})


# ---------------------------------------------------------------
# 帧校验（fail-closed）
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "override, err_code",
    [
        ({"worker_protocol_version": 2}, ERR_PROTOCOL),
        ({"request_id": ""}, ERR_PROTOCOL),
        ({"request_id": 123}, ERR_PROTOCOL),
        ({"db_path": "/tmp/evil.db"}, ERR_DBPATH_FORBIDDEN),
        ({"operation_class": "governance_write"}, ERR_GOVERNANCE_FORBIDDEN),
        ({"operation_class": "unknown_class"}, ERR_PROTOCOL),
        ({"method": ""}, ERR_PROTOCOL),
        ({"params": [1, 2]}, ERR_PROTOCOL),
    ],
)
def test_frame_validation_fail_closed(override, err_code):
    frame = _base_frame(**override)
    resp = handle_frame(frame)
    assert resp["ok"] is False
    assert resp["error"]["code"] == err_code
    # 协议版本不匹配时 _validate_frame 在 request_id 校验之前就返回，
    # 错误帧 request_id 恒为空串；其余场景 request_id 合法则原样回显。
    if override.get("worker_protocol_version", WORKER_PROTOCOL_VERSION) != WORKER_PROTOCOL_VERSION:
        assert resp["request_id"] == ""
    else:
        expected_rid = frame.get("request_id")
        if isinstance(expected_rid, str):
            assert resp["request_id"] == expected_rid
        else:
            assert resp["request_id"] == ""


def test_frame_missing_required_field():
    frame = _base_frame()
    del frame["deadline"]
    resp = handle_frame(frame)
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_PROTOCOL
    assert "deadline" in resp["error"]["message"]


# ---------------------------------------------------------------
# handle_frame 成功路径
# ---------------------------------------------------------------


def test_migrated_native_method_not_served_by_worker():
    """W2-1：get_uncommented_symbols 已迁移 rust_native，worker 不再受理。

    保留对已迁移方法的引用并断言 fail-closed：handle_frame 返回
    method_not_found（HTTP 模式该 RPC 由 Rust native handler 服务，
    python_compat worker 不重复注册）。
    """
    resp = handle_frame(_base_frame(method="get_uncommented_symbols", params={"limit": 100}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_stats_top_files_respects_limit():
    resp = handle_frame(_base_frame(method="stats_top_files", params={"limit": 2}))
    assert resp["ok"] is True
    assert resp["result"]["count"] == 2


def test_stats_top_files_invalid_limit():
    resp = handle_frame(_base_frame(method="stats_top_files", params={"limit": 999}))
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_EXECUTION


def test_stats_top_files_returns_coverage():
    resp = handle_frame(_base_frame(method="stats_top_files", params={"limit": 10}))
    assert resp["ok"] is True
    result = resp["result"]
    assert result["count"] == 2
    by_path = {f["rel_path"]: f for f in result["files"]}
    # src/app.py: 2 符号 1 注释 → 0.5；src/util.py: 2 符号 0 注释 → 0.0
    assert by_path["src/app.py"]["symbol_count"] == 2
    assert by_path["src/app.py"]["commented_count"] == 1
    assert by_path["src/app.py"]["comment_coverage"] == 0.5
    assert by_path["src/util.py"]["comment_coverage"] == 0.0
    # 按 symbol_count 降序（同 count 时顺序不保证，只校验 Top 集合）
    assert result["files"][0]["symbol_count"] >= result["files"][1]["symbol_count"]


def test_other_workspace_isolated():
    # workspace_id 是顶层帧字段（daemon 注入的显式上下文），不在 params 内；
    # stats_top_files 按注入 workspace_id=2 过滤 → 仅 other/main.py 1 个文件
    resp = handle_frame(_base_frame(workspace_id=2))
    assert resp["ok"] is True
    result = resp["result"]
    assert result["count"] == 1
    assert result["files"][0]["rel_path"] == "other/main.py"


# ---------------------------------------------------------------
# handle_frame 异常路径
# ---------------------------------------------------------------


def test_unknown_method():
    resp = handle_frame(_base_frame(method="no_such_method"))
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_operation_class_mismatch():
    # 注册为 read_only，frame 声称 index_write → 拒绝
    resp = handle_frame(_base_frame(operation_class="index_write"))
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_PROTOCOL


def test_execution_error_on_missing_table(tmp_path, monkeypatch):
    # 空 DB（无表）→ E_COMPAT_EXECUTION_ERROR（可重试 + 恢复指引）
    empty_db = str(tmp_path / "empty.db")
    sqlite3.connect(empty_db).close()
    import callwarden.config as cfg

    monkeypatch.setattr(cfg, "get_project_db_path", lambda project_root="": empty_db)
    resp = handle_frame(_base_frame())
    assert resp["ok"] is False
    assert resp["error"]["code"] == ERR_EXECUTION
    assert resp["error"]["retryable"] is True
    assert "recovery" in resp["error"]


# ---------------------------------------------------------------
# registry 约束
# ---------------------------------------------------------------


def test_registry_rejects_duplicate_and_governance():
    r = reg.CompatRegistry()
    r.register("m1", reg.READ_ONLY, reg.SCOPE_WORKSPACE, "d", lambda ctx: {})
    with pytest.raises(ValueError):
        r.register("m1", reg.READ_ONLY, reg.SCOPE_WORKSPACE, "d", lambda ctx: {})
    with pytest.raises(ValueError):
        r.register("m2", reg.GOVERNANCE_WRITE, reg.SCOPE_WORKSPACE, "d", lambda ctx: {})
    with pytest.raises(ValueError):
        r.register("m3", "bogus", reg.SCOPE_WORKSPACE, "d", lambda ctx: {})


def test_default_registry_has_one_method():
    # W2-1：get_uncommented_symbols 已迁移 rust_native，_build_default_registry()
    # 仅注册 stats_top_files 1 项。注意：get_compat_registry() 单例在 import
    # server.compat_worker 时被工具模块 register_compat_routes 装配至全量
    # 104 项（含 H4C-2/3 方法），故长度断言针对默认注册器本身，单例另验
    # get_uncommented_symbols 已不在其中。
    r = reg._build_default_registry()
    assert len(r) == 1
    assert r.is_compat_method("stats_top_files")
    assert not r.is_compat_method("get_uncommented_symbols")
    assert r.operation_class("stats_top_files") == "read_only"

    full = reg.get_compat_registry()
    assert full.is_compat_method("stats_top_files")
    assert not full.is_compat_method("get_uncommented_symbols")


# ---------------------------------------------------------------
# 子进程 main()（真实 stdin/stdout 帧协议）
# ---------------------------------------------------------------


def _spawn_worker(tmp_path: Path):
    """在隔离 USERPROFILE 下启动真实 compat_worker 子进程。

    重定向 USERPROFILE → tmp_path，使 worker 内 config.USER_HOME 解析到
    tmp_path，再在其下创建 .callwarden/callwarden.db（含 seed 数据）。
    """
    db_file = tmp_path / ".callwarden" / "callwarden.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    make_temp_db(db_file.parent)
    env = dict(os.environ)
    env["USERPROFILE"] = str(tmp_path)
    worker_py = _REPO_ROOT / "server" / "compat_worker.py"
    proc = subprocess.Popen(
        [sys.executable, str(worker_py)],
        cwd=str(_REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def test_subprocess_roundtrip_and_eof(tmp_path):
    proc = _spawn_worker(tmp_path)
    try:
        # W2-1：get_uncommented_symbols 已迁移 rust_native，子进程帧协议验证改用
        # 仍注册于 worker 的默认方法 stats_top_files
        frame = _base_frame(method="stats_top_files", params={"limit": 100})
        write_frame(proc.stdin, frame)  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        resp = read_frame(proc.stdout)  # type: ignore[arg-type]
        assert resp is not None
        assert resp["ok"] is True
        assert resp["result"]["count"] == 2
        assert resp["request_id"] == frame["request_id"]

        # 第二个请求：stats_top_files（limit 变体）
        frame2 = _base_frame(method="stats_top_files", params={"limit": 5})
        write_frame(proc.stdin, frame2)  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        resp2 = read_frame(proc.stdout)  # type: ignore[arg-type]
        assert resp2["ok"] is True
        assert resp2["result"]["count"] == 2

        # 关闭 stdin → worker 应正常退出（exit code 0）
        proc.stdin.close()  # type: ignore[union-attr]
        code = proc.wait(timeout=10)
        assert code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_subprocess_rejects_db_path_frame(tmp_path):
    proc = _spawn_worker(tmp_path)
    try:
        frame = _base_frame(db_path="/tmp/evil.db")
        write_frame(proc.stdin, frame)  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        resp = read_frame(proc.stdout)  # type: ignore[arg-type]
        assert resp is not None
        assert resp["ok"] is False
        assert resp["error"]["code"] == ERR_DBPATH_FORBIDDEN
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_subprocess_returns_protocol_error_on_garbage(tmp_path):
    proc = _spawn_worker(tmp_path)
    try:
        # 写入非法长度头（0 长度）→ worker 回 E_COMPAT_WORKER_PROTOCOL 错误帧
        proc.stdin.write((0).to_bytes(4, byteorder="big"))  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        resp = read_frame(proc.stdout)  # type: ignore[arg-type]
        assert resp is not None
        assert resp["ok"] is False
        assert resp["error"]["code"] == ERR_PROTOCOL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_worker_main_returns_zero_on_eof(monkeypatch):
    """直接驱动 worker_main：EOF（空 stdin）应正常退出 0。"""
    # TextIOWrapper 提供 .buffer（worker_main 读取 sys.stdin.buffer）
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    assert worker_main() == 0
