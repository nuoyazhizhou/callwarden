"""P0-COMPAT-v3（T-1788963104879-2e9e6270）tools_semantic 组 → Rust daemon native。

覆盖方法（5）：get_symbol_commit_history / parse_codeowners /
get_project_dependencies / semantic_search / find_similar_functions。

覆盖 task 要求：success / invalid 参数 / daemon unavailable（fail-closed）矩阵；
Python compat 退役断言（_SEMANTIC_READ_ONLY_METHODS 已清空 + RUST_COMPAT_ROUTE 已
摘除 5 条目）。

语义对照（Python 真相源）：
- get_symbol_commit_history：db_git.get_symbol_commit_history ——
  git_symbol_changes JOIN git_commits，`SELECT gc.*, gsc.change_type`，
  按 timestamp DESC，LIMIT 参数。symbol_hash 空 → 空数组。
- parse_codeowners：db_ownership.parse_codeowners —— 候选路径
  .github/CODEOWNERS / docs/CODEOWNERS / CODEOWNERS；注释与 " #" 行内注释截断，
  owner 只保留含 @ 的 token；文件不存在返回空数组（不报错）。
- get_project_dependencies：db_external.get_project_dependencies ——
  {语言: {包名: 版本约束}}；语言检测按 manifest 存在性（python/rust/java/scala/
  go/typescript/javascript/ruby/php/swift/kotlin/csharp/elixir 顺序）；
  python 走 requirements.txt + pyproject.toml + setup.py 累加，
  typescript/javascript 走 package.json dependencies。
- semantic_search：db_vector.semantic_search —— 库内 embedding 为空或由外部
  模型产出时返回 []（daemon 无嵌入模型运行时，降级契约与 Python 无 embedder 一致）。
- find_similar_functions：db_vector.find_similar_functions —— 目标符号不存在或
  无 embedding → []。

已声明偏离（与 Python 真相源的差异，fail-closed 取向）：
- get_symbol_commit_history limit<0：Python 直传 SQL（负 LIMIT = 不限行数），
  Rust 返回 invalid_params。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入

# 真实数据样本（主机级单库 C:/Users/wanpi/.callwarden/callwarden.db，workspace_id=1）
SYM_HASH = "04d158d2ffa64c0ce362ea4805ae7c44464eba5f3e3d6974d1e60bc26faeea5c"
KNOWN_QUALIFIED_NAME = "analyzers.coverage.CoverageMixin.get_comment_coverage"

ALL_METHODS = [
    "get_symbol_commit_history",
    "parse_codeowners",
    "get_project_dependencies",
    "semantic_search",
    "find_similar_functions",
]


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


# ---------------------------------------------------------------------------
# success 矩阵
# ---------------------------------------------------------------------------
def test_symbol_commit_history_shape(live_daemon):
    """真实 symbol_hash → 变更记录数组，字段对齐 `SELECT gc.*, change_type`。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": SYM_HASH,
        "limit": 3,
    })
    assert isinstance(r, list)
    assert len(r) == 3
    row = r[0]
    assert set(row.keys()) == {
        "id",
        "commit_hash",
        "message",
        "author",
        "email",
        "timestamp",
        "workspace_id",
        "change_type",
    }
    assert row["commit_hash"] == "276829e79f6f55ce0f8d898cb0d292c7dc619648"
    assert row["change_type"] == "modified"
    assert row["workspace_id"] == 1
    assert isinstance(row["timestamp"], float)


def test_symbol_commit_history_limit_one(live_daemon):
    """limit=1 → 只返回 1 条（LIMIT 语义）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": SYM_HASH,
        "limit": 1,
    })
    assert isinstance(r, list)
    assert len(r) == 1


def test_symbol_commit_history_default_limit(live_daemon):
    """不传 limit → 默认 20（Python 签名默认值）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": SYM_HASH,
    })
    assert isinstance(r, list)
    assert len(r) <= 20


def test_symbol_commit_history_empty_hash(live_daemon):
    """symbol_hash 空 → 空数组（不查全表）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": "",
    })
    assert r == []


def test_symbol_commit_history_unknown_hash(live_daemon):
    """未知 symbol_hash → 空数组。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": "no-such-hash-zzz",
        "limit": 5,
    })
    assert r == []


def test_symbol_commit_history_limit_zero(live_daemon):
    """limit=0 → 空数组（SQLite LIMIT 0 语义）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": SYM_HASH,
        "limit": 0,
    })
    assert r == []


def test_symbol_commit_history_limit_clamped(live_daemon):
    """超长 limit 被 QueryBudget 钳制（<=500），不报错。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": SYM_HASH,
        "limit": 100000,
    })
    assert isinstance(r, list)
    assert len(r) <= 500


def test_symbol_commit_history_negative_limit_rejected(live_daemon):
    """limit<0 → invalid_params（fail-closed，取代 Python 的不限行数语义）。"""
    c = live_daemon
    with pytest.raises(Exception) as exc:
        c.call("get_symbol_commit_history", {
            "workspace_instance_id": CANONICAL_INSTANCE,
            "symbol_hash": SYM_HASH,
            "limit": -1,
        })
    assert "invalid_params" in str(exc.value)


def test_parse_codeowners_default_path(live_daemon):
    """file_path 空 → 走 workspace 候选路径；当前仓库无 CODEOWNERS → []。"""
    c = live_daemon
    r = c.call("parse_codeowners", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)


def test_parse_codeowners_missing_file(live_daemon):
    """显式不存在的路径 → 空数组（Python 同款：不报错）。"""
    c = live_daemon
    r = c.call("parse_codeowners", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "file_path": "C:/no/such/CODEOWNERS",
    })
    assert r == []


def test_get_project_dependencies_auto_detect(live_daemon):
    """不传 languages → 自动检测（隔离 root 空库 → 空 dict；契约：dict 嵌套）。"""
    c = live_daemon
    r = c.call("get_project_dependencies", {
        "workspace_instance_id": CANONICAL_INSTANCE,
    })
    assert isinstance(r, dict)
    for lang, deps in r.items():
        assert isinstance(deps, dict)
        assert all(isinstance(v, str) for v in deps.values())


def test_get_project_dependencies_explicit_language(live_daemon):
    """指定 languages=["python"] → 只返回 python 键（无 manifest → 空 dict）。"""
    c = live_daemon
    r = c.call("get_project_dependencies", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "languages": ["python"],
    })
    assert isinstance(r, dict)
    assert set(r.keys()) == {"python"}
    assert isinstance(r["python"], dict)


def test_get_project_dependencies_unknown_language(live_daemon):
    """未知语言 → 空 dict（该语言无 manifest 且无 parser 命中）。"""
    c = live_daemon
    r = c.call("get_project_dependencies", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "languages": ["cobol"],
    })
    assert r == {"cobol": {}}


def test_get_project_dependencies_empty_languages(live_daemon):
    """空数组 → 空 dict（不做自动检测，Python 迭代空列表同形）。"""
    c = live_daemon
    r = c.call("get_project_dependencies", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "languages": [],
    })
    assert r == {}


def test_semantic_search_no_embeddings(live_daemon):
    """库内无 embedding → 空列表（与 Python 无 embedder 的降级契约一致）。"""
    c = live_daemon
    r = c.call("semantic_search", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "query": "code coverage",
        "top_k": 3,
    })
    assert r == []


def test_semantic_search_empty_query(live_daemon):
    """空 query → 空列表。"""
    c = live_daemon
    r = c.call("semantic_search", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "query": "",
    })
    assert r == []


def test_semantic_search_zero_top_k(live_daemon):
    """top_k=0 → 空列表（Python 的 slice [:0] 同形）。"""
    c = live_daemon
    r = c.call("semantic_search", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "query": "auth",
        "top_k": 0,
    })
    assert r == []


def test_find_similar_functions_no_embedding(live_daemon):
    """目标符号存在但库内无 embedding → 空列表。"""
    c = live_daemon
    r = c.call("find_similar_functions", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": KNOWN_QUALIFIED_NAME,
        "threshold": 0.5,
        "top_k": 3,
    })
    assert r == []


def test_find_similar_functions_unknown_symbol(live_daemon):
    """目标符号不存在 → 空列表（Python 查不到 symbol_hash 直接 return []）。"""
    c = live_daemon
    r = c.call("find_similar_functions", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": "no::such::symbol::zzz",
    })
    assert r == []


def test_find_similar_functions_empty_name(live_daemon):
    """空 qualified_name → 空列表。"""
    c = live_daemon
    r = c.call("find_similar_functions", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": "",
    })
    assert r == []


def test_find_similar_functions_zero_top_k(live_daemon):
    """top_k=0 → 空列表。"""
    c = live_daemon
    r = c.call("find_similar_functions", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": KNOWN_QUALIFIED_NAME,
        "top_k": 0,
    })
    assert r == []


# ---------------------------------------------------------------------------
# invalid 矩阵：缺 workspace_instance_id → fail-closed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ALL_METHODS)
def test_methods_missing_workspace_instance_id(live_daemon, method):
    """缺 workspace_instance_id → 报错，不降级本地 SQLite。"""
    c = live_daemon
    with pytest.raises(Exception) as exc:
        c.call(method, {})
    assert "invalid_params" in str(exc.value) or "missing" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ALL_METHODS)
def test_methods_daemon_unavailable_fail_closed(method):
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    # DaemonUnavailableError 异常类型本身即 fail-closed 证据
    with pytest.raises(DaemonUnavailableError):
        c.call(method, {"workspace_instance_id": CANONICAL_INSTANCE})


# ---------------------------------------------------------------------------
# Python compat 退役断言：条目已从只读白名单与 compat 路由表摘除
# ---------------------------------------------------------------------------
def test_python_compat_entries_retired():
    from callwarden.server.tools import tools_semantic
    assert tools_semantic._SEMANTIC_READ_ONLY_METHODS == {}
    # handler 函数保留供追溯（退役不等于删除实现）
    for name in (
        "_h_semantic_search",
        "_h_find_similar_functions",
        "_h_get_symbol_commit_history",
        "_h_parse_codeowners",
        "_h_get_project_dependencies",
    ):
        assert callable(getattr(tools_semantic, name))
    from callwarden.server.compat_registry import RUST_COMPAT_ROUTE
    for m in ALL_METHODS:
        assert m not in RUST_COMPAT_ROUTE
