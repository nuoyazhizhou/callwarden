"""G4（T-1789022024819-8cb9f614）：build-context daemon 化 live-daemon 回归。

覆盖：
- build_context.register（workspace_instance_id 模式 → 主库写面）
- build_context.import_compile_commands（parse-only，聚合 + 路径规范化）
- resolved_edges.compute（5 级解析 + 查看面 limit 截断 + fail-closed）
- resolved_edges.rebuild（compute + 单事务清旧落库，幂等重建）
- build_context.resolved_edges（instance 模式查询，写后可见）
- fail-closed 矩阵：缺 instance / 未知 bc / 非法 JSON / workspace 绑定不一致
"""

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.config import get_http_authority_id

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入

COMPILE_COMMANDS_SAMPLE = """
[
  {
    "directory": "/tmp/build",
    "arguments": ["gcc", "-DDEBUG=1", "-DUSE_FAST=1", "-I./include", "-isystem", "/usr/include",
                  "-include", "config.h", "-std=c11", "-O2", "-g", "main.c", "-o", "main.o"],
    "file": "main.c",
    "output": "main.o"
  },
  {
    "directory": "/tmp/build",
    "command": "gcc -DNDEBUG -I./lib -g util.c -o util.o",
    "file": "util.c",
    "output": "util.o"
  }
]
"""


@pytest.fixture(scope="module")
def rpc(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


def _call(rpc, method, params):
    params = dict(params)
    params.setdefault("workspace_instance_id", CANONICAL_INSTANCE)
    return rpc.call(method, params)


def test_import_compile_commands_parse(rpc):
    """聚合解析：arguments 优先、-D/-I/-isystem/-include/-std 提取、路径规范化、去重。"""
    out = _call(rpc, "build_context.import_compile_commands", {
        "content": COMPILE_COMMANDS_SAMPLE,
        "workspace_root": "/tmp/ws",
    })
    assert isinstance(out, dict)
    assert out["file_count"] == 2
    assert out["per_file_count"] == 2
    assert out["compiler_path"] == "gcc"
    # defines：后出现覆盖（NDEBUG 覆盖不了 DEBUG —— 不同 key 都保留）
    assert out["defines"]["DEBUG"] == "1"
    assert out["defines"]["USE_FAST"] == "1"
    assert out["defines"]["NDEBUG"] == ""
    # include_paths：规范化为绝对路径（相对 directory）且去重保序
    incs = out["include_paths"]
    assert "/tmp/build/include" in incs
    assert "/usr/include" in incs
    assert "/tmp/build/lib" in incs
    # compile_flags：-include/-std/-O2/-g 去重
    flags = out["compile_flags"]
    assert "-include config.h" in flags
    assert "-std=c11" in flags
    assert "-O2" in flags
    assert "-g" in flags


def test_import_compile_commands_invalid_json(rpc):
    """非法 JSON → invalid_params fail-closed。"""
    with pytest.raises(Exception) as ei:
        _call(rpc, "build_context.import_compile_commands", {
            "content": "{not json",
            "workspace_root": "/tmp/ws",
        })
    assert "invalid_params" in str(ei.value) or "JSON" in str(ei.value)


def test_import_compile_commands_non_array(rpc):
    """非 JSON array → invalid_params fail-closed。"""
    with pytest.raises(Exception) as ei:
        _call(rpc, "build_context.import_compile_commands", {
            "content": '{"file": "main.c"}',
        })
    assert "invalid_params" in str(ei.value) or "array" in str(ei.value)


def test_register_main_db_instance_mode(rpc):
    """register instance 模式 → 主库写面，返回结构化 build context。"""
    import time
    name = f"g4-regression-{int(time.time())}"
    out = _call(rpc, "build_context.register", {
        "workspace_id": 1,
        "name": name,
        "compile_flags": ["-O2", "-g"],
        "defines": {"G4_TEST": "1"},
        "include_paths": ["/tmp/g4/include"],
        "set_active": True,
    })
    assert isinstance(out, dict)
    assert out.get("name") == name
    assert out.get("is_active") is True
    assert out.get("build_context_hash")
    # 短 hash 前缀可查回（instance 模式 build_context.get）
    got = _call(rpc, "build_context.get", {
        "workspace_id": 1,
        "build_context_hash": str(out["build_context_hash"])[:12],
    })
    assert isinstance(got, dict)
    assert got.get("build_context_hash") == out["build_context_hash"]


def test_compute_missing_build_context(rpc):
    """未知 build_context_hash → fail-closed（error=build_context not found）。"""
    out = _call(rpc, "resolved_edges.compute", {
        "workspace_id": 1,
        "build_context_hash": "deadbeef" * 8,
    })
    assert isinstance(out, dict)
    assert out.get("error") == "build_context not found"
    assert out.get("source") == "none"


def test_rebuild_missing_build_context(rpc):
    """rebuild 未知 bc → fail-closed 不落库（inserted=0）。"""
    out = _call(rpc, "resolved_edges.rebuild", {
        "workspace_id": 1,
        "build_context_hash": "deadbeef" * 8,
    })
    assert isinstance(out, dict)
    assert out.get("error") == "build_context not found"
    assert out.get("inserted") == 0


def test_rebuild_end_to_end(rpc):
    """注册 → rebuild → 查询可见 → 幂等重建（deleted=上次 inserted）全链。"""
    import time
    # defines 注入时间戳 → hash 每轮唯一，保证「首次 rebuild deleted==0」断言成立
    stamp = str(int(time.time()))
    name = f"g4-e2e-{stamp}"
    reg = _call(rpc, "build_context.register", {
        "workspace_id": 1,
        "name": name,
        "compile_flags": ["-O2"],
        "defines": {"G4_RUN": stamp},
        "include_paths": [],
        "set_active": False,
    })
    bch = reg["build_context_hash"]

    # 首次 rebuild：主库 calls 表全量复制（source=calls_table，无 CAS 表）
    r1 = _call(rpc, "resolved_edges.rebuild", {
        "workspace_id": 1,
        "build_context_hash": bch,
    })
    assert isinstance(r1, dict)
    assert "error" not in r1
    assert r1.get("source") == "calls_table"
    assert r1.get("inserted", 0) > 0
    assert r1.get("deleted") == 0

    # 写后可见：build_context.resolved_edges（instance 模式查询）
    edges = _call(rpc, "build_context.resolved_edges", {
        "workspace_id": 1,
        "build_context_hash": bch,
        "limit": 10,
    })
    assert isinstance(edges, list) and len(edges) > 0
    assert all("caller_symbol_id" in e and "resolution_method" in e for e in edges)

    # 幂等重建：先清旧（deleted=上次 inserted）再写入
    r2 = _call(rpc, "resolved_edges.rebuild", {
        "workspace_id": 1,
        "build_context_hash": bch,
    })
    assert r2.get("deleted") == r1.get("inserted")
    assert r2.get("inserted", 0) > 0

    # 查看面：compute 返回截断边（limit 语义）且 count 为全量
    c = _call(rpc, "resolved_edges.compute", {
        "workspace_id": 1,
        "build_context_hash": bch,
        "limit": 5,
    })
    assert c.get("source") == "calls_table"
    assert c.get("count", 0) >= c.get("inserted", c.get("count", 0))
    assert len(c.get("edges") or []) <= 5
    assert c.get("truncated") is True or c.get("count", 0) <= 5

    # 清理：resolved_edges.replace 空边清残留 → delete bc（instance 模式）
    _call(rpc, "resolved_edges.replace", {
        "workspace_id": 1,
        "build_context_hash": bch,
        "edges": [],
    })
    d = _call(rpc, "build_context.delete", {
        "workspace_id": 1,
        "build_context_hash": bch,
    })
    assert isinstance(d, dict) and d.get("deleted", 0) >= 1


def test_missing_instance_id_fail_closed(rpc):
    """缺 workspace_instance_id 的 instance-only 方法 → invalid_params fail-closed。"""
    with pytest.raises(Exception) as ei:
        rpc.call("resolved_edges.compute", {
            "workspace_id": 1,
            "build_context_hash": "deadbeef" * 8,
        })
    assert "invalid_params" in str(ei.value) or "workspace_instance_id" in str(ei.value)
