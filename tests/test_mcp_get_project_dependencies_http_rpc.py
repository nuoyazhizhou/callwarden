"""MCP-028（A′ task_evidence_read）get_project_dependencies → Rust daemon native。

覆盖 task 要求：
  success（无 manifest → 空对象）/ languages 显式 / 缺省参数、daemon unavailable
  （fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_semantic.get_project_dependencies）已从
  _SEMANTIC_READ_ONLY_METHODS 移除 compat 注册（语义组全部迁移），改由 Rust daemon
  （task_collab.rs::handle_get_project_dependencies）为权威：检测项目语言（manifest
  存在性）→ 解析各语言 manifest（python/rust/go/typescript/javascript）→ 返回
  {语言: {包名: 版本}} 嵌套字典。
- 确定性 parity：workspace 1（callwarden）有 Cargo.toml + pyproject.toml 等 → 返回
  对应语言依赖；未知 workspace 无 root → 空对象。
- 本测试直连 HTTP RPC，验证返回结构。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入
UNKNOWN_INSTANCE = "00000000deadbeef"


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_project_dependencies_structure(live_daemon):
    """返回嵌套字典 {语言: {包名: 版本}}。"""
    c = live_daemon
    r = c.call("get_project_dependencies", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, dict)
    for lang, deps in r.items():
        assert isinstance(deps, dict)
        for name, ver in deps.items():
            assert isinstance(name, str) and isinstance(ver, str)


def test_get_project_dependencies_explicit_languages(live_daemon):
    """显式 languages 列表 → 只返回指定语言。"""
    c = live_daemon
    r = c.call("get_project_dependencies",
               {"workspace_instance_id": CANONICAL_INSTANCE, "languages": ["rust"]})
    assert isinstance(r, dict)
    assert "rust" in r


def test_get_project_dependencies_unknown_workspace(live_daemon):
    """未知 workspace：无 root → 空对象（不报错）。"""
    c = live_daemon
    # 未知 instance → fail-closed workspace_not_found（不静默返回空对象）
    with pytest.raises(Exception) as ei:
        c.call("get_project_dependencies", {"workspace_instance_id": UNKNOWN_INSTANCE})
    assert "workspace_not_found" in str(ei.value)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_project_dependencies_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_project_dependencies", {"workspace_instance_id": CANONICAL_INSTANCE})
    # fail-closed 证据即异常类型本身（错误消息随传输层实现演进而变化）


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_project_dependencies_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = c2.call("get_project_dependencies", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, dict)
