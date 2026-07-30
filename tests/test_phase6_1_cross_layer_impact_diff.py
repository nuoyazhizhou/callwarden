"""Phase 6-1 P2 差分测试：cross_layer_impact Rust 实现与 Python 一致性验证

**本文件对应 Phase 6-1 P2（cross_layer_impact Rust 迁移）的 D2 差分矩阵。**

差分测试矩阵（D2.1 - D2.8）：
  TestCrossLayerImpactDiff：
    - D2.1: 单文件符号 + SQL 表名提取（content 含 SELECT * FROM users + UPDATE config SET）
    - D2.2: 跨多文件符号（code_layer 有 3 个调用方，来自不同文件/模块）
    - D2.3: API 层检测（source_name="handle_route"，content 含 #[get("/api")]）
    - D2.4: 配置层提取（content 含 env::var("DATABASE_URL") + config.get("timeout")）
    - D2.5: 空 content（各层应为空，code 层保留传入值）
    - D2.6: 多种 SQL 模式混合（FROM + INSERT INTO + DELETE FROM）
    - D2.7: 路由装饰器检测（content 含 @app.route("/users")）
    - D2.8: 无匹配（content 无任何 SQL/API/Config 模式）

预期差异：无
  - Rust 与 Python 在 db / api / config 三层均使用相同正则与相同排序（BTreeSet 等价 sorted(set(...))）
  - code 层为 Python 预查询的调用方列表，Rust 仅原样返回

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - 如果不可加载，本测试套件会显式 skip 并给出修复指引

关联：
  - Python 真相源：db/db_impact.py:ImpactMixin.cross_layer_impact (L553-L700)
  - Rust 真相源：rust_ext/src/impact.rs:cross_layer_impact_core + py_cross_layer_impact
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Tuple

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "请先运行 `maturin develop --manifest-path rust_ext/Cargo.toml --release` "
        "或 `pip install --force-reinstall rust_ext/target/wheels/callwarden_core-*.whl`。"
    )


# ============================================
# Python baseline: cross_layer_impact 纯函数实现
# ============================================
# 从 db/db_impact.py::cross_layer_impact (L553-L700) 提取的纯函数逻辑
# 仅保留 db / api / config 三层正则匹配（code 层为外部传入）

def _py_cross_layer_impact(
    source_qn: str,
    source_name: str,
    content: str,
    code_layer: List[Tuple[str, str, str, str, str, str]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Python baseline: cross_layer_impact 的 db/api/config 三层逻辑

    对齐 db/db_impact.py:ImpactMixin.cross_layer_impact 的 Python 全路径 fallback。
    code 层由调用方预查询后传入（与 Rust 入参完全一致）。
    """
    # ---- DB 层：从 content 中正则提取 SQL 表名 ----
    db_layer: List[Dict[str, Any]] = []
    table_names = set()
    sql_patterns = [
        re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bUPDATE\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.IGNORECASE),
        re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.IGNORECASE),
    ]
    for pat in sql_patterns:
        for m in pat.finditer(content):
            table_names.add(m.group(1))
    for tbl in sorted(table_names):
        db_layer.append({"table": tbl, "source": source_qn})

    # ---- API 层：函数名关键词 + HTTP 注解 + 路由装饰器 ----
    api_layer: List[Dict[str, Any]] = []
    name_lower = source_name.lower()
    is_api_name = (
        "route" in name_lower
        or "handler" in name_lower
        or "endpoint" in name_lower
    )
    http_annotation = re.search(
        r"#\[(?:get|post|put|delete|patch|head|options)\s*\(",
        content,
        re.IGNORECASE,
    )
    route_decorator = re.search(
        r"@\w+\.(?:route|get|post|put|delete|patch)\s*\(",
        content,
        re.IGNORECASE,
    )
    if is_api_name or http_annotation or route_decorator:
        reasons = []
        if is_api_name:
            reasons.append("function_name_keyword")
        if http_annotation:
            reasons.append("http_method_annotation")
        if route_decorator:
            reasons.append("route_decorator")
        api_layer.append({
            "symbol": source_qn,
            "name": source_name,
            "reason": ",".join(reasons),
        })

    # ---- 配置层：从 content 中正则提取配置项引用 ----
    # 注意：此组正则未使用 re.IGNORECASE（对齐 Rust CONFIG_PATTERNS）
    config_layer: List[Dict[str, Any]] = []
    config_keys = set()
    config_patterns = [
        re.compile(r"env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"std::env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"config\.get\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    ]
    for pat in config_patterns:
        for m in pat.finditer(content):
            config_keys.add(m.group(1))
    for key in sorted(config_keys):
        config_layer.append({"config_key": key, "source": source_qn})

    # code 层：把元组列表转为 dict 列表（对齐 Rust 输出格式）
    code_dicts: List[Dict[str, Any]] = [
        {
            "qualified_name": qn,
            "name": n,
            "module_path": mp,
            "visibility": vis,
            "kind": k,
            "file_path": fp,
        }
        for (qn, n, mp, vis, k, fp) in code_layer
    ]

    return {"code": code_dicts, "db": db_layer, "api": api_layer, "config": config_layer}


# ============================================
# 归一化对比工具
# ============================================

def _normalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """归一化结果用于对比

    将每层 dict 转为 sorted tuple 列表，忽略顺序差异。
    """
    return {
        "code": sorted([tuple(sorted(d.items())) for d in result.get("code", [])]),
        "db": sorted([tuple(sorted(d.items())) for d in result.get("db", [])]),
        "api": sorted([tuple(sorted(d.items())) for d in result.get("api", [])]),
        "config": sorted([tuple(sorted(d.items())) for d in result.get("config", [])]),
    }


def _assert_cross_layer_equal(py_result: Dict[str, Any],
                               rust_result: Dict[str, Any]) -> None:
    """断言 Python baseline 与 Rust 输出完全一致"""
    py_norm = _normalize(py_result)
    rust_norm = _normalize(rust_result)
    for layer in ("code", "db", "api", "config"):
        assert py_norm[layer] == rust_norm[layer], (
            f"layer '{layer}' mismatch:\n"
            f"  py={py_norm[layer]}\n  rust={rust_norm[layer]}"
        )


# ============================================
# D2.1: 单文件符号 + SQL 表名提取
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_1:
    """D2.1: 单文件符号 + SQL 表名提取

    content 包含 SELECT * FROM users 和 UPDATE config SET，
    预期 db 层提取出 [config, users]（按字母序）
    """

    def test_d2_1_sql_table_extraction(self):
        source_qn = "mod.query_fn"
        source_name = "query_fn"
        content = """
        fn query_fn() {
            let rows = SELECT * FROM users WHERE id = 1;
            UPDATE config SET value = 'new';
        }
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = [
            ("mod.caller_a", "caller_a", "mod", "public", "fn", "src/caller_a.rs"),
        ]

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 具体断言
        assert len(rust_result["db"]) == 2
        tables = [d["table"] for d in rust_result["db"]]
        assert tables == ["config", "users"], f"got {tables}"
        # source 字段应为 source_qn
        for entry in rust_result["db"]:
            assert entry["source"] == source_qn
        # code 层应保留传入值
        assert len(rust_result["code"]) == 1
        assert rust_result["code"][0]["qualified_name"] == "mod.caller_a"


# ============================================
# D2.2: 跨多文件符号（3 个调用方来自不同文件/模块）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_2:
    """D2.2: 跨多文件符号

    code_layer 有 3 个调用方，来自不同文件/模块，
    预期 code 层完整保留，其他层为空（content 无匹配模式）
    """

    def test_d2_2_cross_file_callers(self):
        source_qn = "svc.process"
        source_name = "process"
        content = "fn process() { /* 无 SQL / API / Config */ }"
        code_layer: List[Tuple[str, str, str, str, str, str]] = [
            ("api.handler", "handler", "api", "public", "fn", "src/api.rs"),
            ("svc.batch", "batch", "svc", "private", "fn", "src/svc/batch.rs"),
            ("main.run", "run", "main", "public", "fn", "src/main.rs"),
        ]

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 验证 code 层保留 3 个调用方
        assert len(rust_result["code"]) == 3
        qnames = {d["qualified_name"] for d in rust_result["code"]}
        assert qnames == {"api.handler", "svc.batch", "main.run"}
        file_paths = {d["file_path"] for d in rust_result["code"]}
        assert file_paths == {"src/api.rs", "src/svc/batch.rs", "src/main.rs"}
        # 其他层应为空
        assert rust_result["db"] == []
        assert rust_result["api"] == []
        assert rust_result["config"] == []


# ============================================
# D2.3: API 层检测（函数名 + HTTP 注解）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_3:
    """D2.3: API 层检测

    source_name="handle_route"（含 "route" 关键词），
    content 含 #[get("/api")]（HTTP 方法注解），
    预期 api 层 reason 同时包含 function_name_keyword 和 http_method_annotation
    """

    def test_d2_3_api_detection(self):
        source_qn = "api.handle_route"
        source_name = "handle_route"
        content = """
        #[get("/api/users")]
        fn handle_route() { /* ... */ }
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = []

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 验证 api 层
        assert len(rust_result["api"]) == 1
        api_entry = rust_result["api"][0]
        assert api_entry["symbol"] == source_qn
        assert api_entry["name"] == source_name
        # reason 应包含两个触发原因（顺序由实现决定，用集合验证）
        reasons = set(api_entry["reason"].split(","))
        assert "function_name_keyword" in reasons
        assert "http_method_annotation" in reasons


# ============================================
# D2.4: 配置层提取
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_4:
    """D2.4: 配置层提取

    content 含 env::var("DATABASE_URL") 和 config.get("timeout")，
    预期 config 层提取出 [DATABASE_URL, timeout]（按字母序）
    """

    def test_d2_4_config_extraction(self):
        source_qn = "mod.init"
        source_name = "init"
        content = """
        fn init() {
            let db = env::var("DATABASE_URL");
            let to = config.get("timeout");
        }
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = []

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 验证 config 层
        assert len(rust_result["config"]) == 2
        keys = [d["config_key"] for d in rust_result["config"]]
        assert keys == ["DATABASE_URL", "timeout"], f"got {keys}"
        for entry in rust_result["config"]:
            assert entry["source"] == source_qn


# ============================================
# D2.5: 空 content
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_5:
    """D2.5: 空 content

    content 为空字符串，预期 db / api / config 层均为空，
    code 层保留传入的调用方
    """

    def test_d2_5_empty_content(self):
        source_qn = "mod.empty"
        source_name = "empty"
        content = ""
        code_layer: List[Tuple[str, str, str, str, str, str]] = [
            ("mod.caller", "caller", "mod", "public", "fn", "src/caller.rs"),
        ]

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 各层应为空
        assert rust_result["db"] == []
        assert rust_result["api"] == []
        assert rust_result["config"] == []
        # code 层保留传入值
        assert len(rust_result["code"]) == 1
        assert rust_result["code"][0]["qualified_name"] == "mod.caller"


# ============================================
# D2.6: 多种 SQL 模式混合
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_6:
    """D2.6: 多种 SQL 模式混合

    content 同时包含 FROM / INSERT INTO / DELETE FROM 三种 SQL 模式，
    预期 db 层提取出全部去重后的表名（按字母序）
    """

    def test_d2_6_mixed_sql_patterns(self):
        source_qn = "mod.db_ops"
        source_name = "db_ops"
        content = """
        SELECT * FROM accounts;
        INSERT INTO logs (msg) VALUES ('x');
        DELETE FROM temp;
        SELECT * FROM accounts;  -- 重复表名，应去重
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = []

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 验证 db 层：3 个唯一表名，去重后按字母序
        assert len(rust_result["db"]) == 3
        tables = [d["table"] for d in rust_result["db"]]
        assert tables == ["accounts", "logs", "temp"], f"got {tables}"


# ============================================
# D2.7: 路由装饰器检测
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_7:
    """D2.7: 路由装饰器检测

    content 含 @app.route("/users")，
    预期 api 层 reason 仅包含 route_decorator
    （source_name 不含 route/handler/endpoint，content 无 HTTP 注解）
    """

    def test_d2_7_route_decorator(self):
        source_qn = "web.list_users"
        source_name = "list_users"  # 不含 route/handler/endpoint
        content = """
        @app.route("/users")
        def list_users():
            return []
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = []

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 验证 api 层
        assert len(rust_result["api"]) == 1
        api_entry = rust_result["api"][0]
        assert api_entry["symbol"] == source_qn
        assert api_entry["name"] == source_name
        # reason 应仅包含 route_decorator
        assert api_entry["reason"] == "route_decorator", f"got {api_entry['reason']}"


# ============================================
# D2.8: 无匹配（content 无任何 SQL/API/Config 模式）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestCrossLayerImpactDiffD2_8:
    """D2.8: 无匹配

    content 是普通函数体，无任何 SQL/API/Config 模式，
    预期 db / api / config 层均为空
    """

    def test_d2_8_no_match(self):
        source_qn = "mod.plain"
        source_name = "plain"
        content = """
        fn plain(x: i32) -> i32 {
            let y = x + 1;
            y * 2
        }
        """
        code_layer: List[Tuple[str, str, str, str, str, str]] = []

        py_result = _py_cross_layer_impact(source_qn, source_name, content, code_layer)
        rust_result = callwarden_core.py_cross_layer_impact(
            source_qn, source_name, content, code_layer
        )

        _assert_cross_layer_equal(py_result, rust_result)

        # 各层应为空
        assert rust_result["db"] == []
        assert rust_result["api"] == []
        assert rust_result["config"] == []
        assert rust_result["code"] == []
