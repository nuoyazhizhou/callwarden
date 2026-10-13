"""TP-11L 16 语言解析 / 引用独立性 golden 验收 harness。

在完全隔离的 daemon（独立 run 根 + git provenance workspace + RPC 直调）上
验证多语言 tree-sitter 解析面：每种语言的最小 golden 源文件被真正提取出
**具名符号**，且同名字号在不同语言/不同文件下互不串扰。

🔴 关键架构事实（读代码确认，非猜测）：

1. 语言支持面分**两层**，二者不等价：
   - `multi_lang::LangConfig::supported_languages()`（multi_lang.rs:250）列出
     15 个语言 id，且 `languages::get_config`（languages/mod.rs:36）仅为这
     15 个提供解析配置：
     python/rust/go/java/typescript/javascript/ruby/php/scala/csharp/cpp/
     kotlin/swift/elixir/hcl。
   - 扩展名映射 `parser_lang_id`（fs_handlers.rs:279）额外把 `.c/.h` 映射为
     语言 id `"c"`，但 `get_config("c")` 返回 `None` → **C 文件只做文件级
     索引、不出符号**（parse_and_store_symbols 直接 (0,0) 返回）。
     故本域的「16」= 15 个出符号语言 + 1 个已知不出符号的 C 边界。
     另：`.tf` 同映射为 hcl，但 `INDEXABLE_EXTS`（fs_handlers.rs:26）不含
     `tf`，故 fixture 一律用 `.hcl`；`mjs/cjs` 同理不在白名单，JS fixture
     用 `.js`。

2. qualified_name 构造（`make_qualified`，lib.rs:829）：
   `parent_qualified` > `module_path` > 裸名；`module_path` 由
   `module_path_of`（fs_handlers.rs:1119）取 rel_path 去扩展名（保留 `/`）。
   故 `golden/golden_python.py` 里的顶层函数 `alpha_probe` 的
   qualified_name = `golden/golden_python.alpha_probe`——**文件名与目录都
   进 qualified_name**，这正是引用独立性的判别依据。

3. 读面入口（`dispatch.rs` 实测 method 名）：
   - `query.file`（workspace_instance_id + file_path）→ 该文件符号列表；
     走 `open_query_connection`，无需 snapshot。
   - `query.symbol`（workspace_instance_id + qualified_name）→ 符号详情；
     **需要先 snapshot.publish**（否则 `snapshot_not_ready`）。
   - `query.search`（workspace_instance_id + query [+ kind] [+ limit]）→
     符号数组；同样需要先 snapshot.publish。
   - `query.symbol_location`（workspace_instance_id + name + file_path）。

4. `workspace.build_graph` 只是治理写面名单（dispatch.rs:2381）里的条目，
   **没有 dispatch arm**；建图走 `GovernanceEnv.build_graph()`（`cw.py
   refresh --all`，TP-10C 已验证配方）。

零生产触碰：fixture 源码全部写在隔离 run 根的 workspace 目录内，
绝不指向真实任务/受跟踪源文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
_ISO_DIR = _HERE.parent / "isolation"
for _d in (str(_HERE), str(_GOV_DIR), str(_ISO_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 复用 TP-11I 底座（GovernanceEnv + run 根自清洁 wipe_run_root）
from isolation_harness import IsolationEnv  # noqa: E402

# TP-11L 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_LANG = Path(
    os.environ.get("CW_TP11L_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11L"))

# fixture 相对目录（workspace 根之下）。所有 golden 源文件平铺在此，
# 使 module_path = "golden/<basename>"，qualified_name 形如
# "golden/golden_python.alpha_probe"。
GOLDEN_DIR = "golden"

# 跨语种统一探针符号名：每个语言文件都定义同名的 `alpha_probe`，用于验证
# 「同名字号在不同语言/不同文件下互不串扰」。
PROBE_NAME = "alpha_probe"


# ===================================================================
# 16 语种 golden fixture 源码
# ===================================================================
# 每项：(lang_id, 文件名, 源码, 期望符号名集合, 期望 kind 映射)
# 期望值在下述 golden 表中以「具体符号名」固化（非空即可式的占位断言一律不写）。

LANG_FIXTURES: Dict[str, str] = {
    # ---- 15 个有解析配置的语言 ----
    "python": "golden_python.py",
    "rust": "golden_rust.rs",
    "go": "golden_go.go",
    "java": "golden_java.java",
    "typescript": "golden_ts.ts",
    "javascript": "golden_js.js",
    "ruby": "golden_ruby.rb",
    "php": "golden_php.php",
    "scala": "golden_scala.scala",
    "csharp": "golden_cs.cs",
    "cpp": "golden_cpp.cpp",
    "kotlin": "golden_kt.kt",
    "swift": "golden_swift.swift",
    "elixir": "golden_ex.ex",
    "hcl": "golden_hcl.hcl",
    # ---- 边界：扩展名映射到 "c"，但无解析配置 → 只索引不出符号 ----
    "c": "golden_c.c",
}

LANG_SOURCES: Dict[str, str] = {
    "python": (
        "def alpha_probe():\n"
        "    return 1\n"
        "\n"
        "\n"
        "class BetaProbe:\n"
        "    def gamma_probe(self):\n"
        "        return alpha_probe()\n"
    ),
    "rust": (
        "pub fn alpha_probe() -> i32 {\n"
        "    1\n"
        "}\n"
        "\n"
        "pub struct BetaProbe {\n"
        "    pub v: i32,\n"
        "}\n"
    ),
    "go": (
        "package golden\n"
        "\n"
        "func alpha_probe() int {\n"
        "    return 1\n"
        "}\n"
        "\n"
        "type BetaProbe struct {\n"
        "    V int\n"
        "}\n"
    ),
    "java": (
        "package golden;\n"
        "\n"
        "public class BetaProbe {\n"
        "    public int alpha_probe() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    ),
    "typescript": (
        "export function alpha_probe(): number {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "export class BetaProbe {\n"
        "    gamma_probe(): number {\n"
        "        return alpha_probe();\n"
        "    }\n"
        "}\n"
    ),
    "javascript": (
        "function alpha_probe() {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "class BetaProbe {\n"
        "    gamma_probe() {\n"
        "        return alpha_probe();\n"
        "    }\n"
        "}\n"
    ),
    "ruby": (
        "def alpha_probe\n"
        "  1\n"
        "end\n"
        "\n"
        "class BetaProbe\n"
        "  def gamma_probe\n"
        "    alpha_probe\n"
        "  end\n"
        "end\n"
    ),
    "php": (
        "<?php\n"
        "function alpha_probe() {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "class BetaProbe {\n"
        "    public function gamma_probe() {\n"
        "        return alpha_probe();\n"
        "    }\n"
        "}\n"
    ),
    "scala": (
        "package golden\n"
        "\n"
        "class BetaProbe {\n"
        "  def alpha_probe(): Int = {\n"
        "    1\n"
        "  }\n"
        "}\n"
    ),
    "csharp": (
        "namespace Golden {\n"
        "    public class BetaProbe {\n"
        "        public int alpha_probe() {\n"
        "            return 1;\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "cpp": (
        "int alpha_probe() {\n"
        "    return 1;\n"
        "}\n"
        "\n"
        "class BetaProbe {\n"
        "public:\n"
        "    int gamma_probe() {\n"
        "        return alpha_probe();\n"
        "    }\n"
        "};\n"
    ),
    "kotlin": (
        "package golden\n"
        "\n"
        "fun alpha_probe(): Int {\n"
        "    return 1\n"
        "}\n"
        "\n"
        "class BetaProbe {\n"
        "    fun gamma_probe(): Int {\n"
        "        return alpha_probe()\n"
        "    }\n"
        "}\n"
    ),
    "swift": (
        "func alpha_probe() -> Int {\n"
        "    return 1\n"
        "}\n"
        "\n"
        "class BetaProbe {\n"
        "    func gamma_probe() -> Int {\n"
        "        return alpha_probe()\n"
        "    }\n"
        "}\n"
    ),
    # 🔴 Elixir 必须写 `def name() do`（带括号）：Rust 端 elixir::config 的
    # `def` 规则用 NameStrategy::CallArgName{container:"arguments",
    # child_kind:"call", name_kind:"identifier"}，零参无括号写法
    # (`def alpha_probe do`) 的 arguments 直接挂 identifier、没有中间 call
    # 节点 → 名字提取失败 → 该文件 0 符号（实测，见 _smoke_probe 记录）。
    "elixir": (
        "defmodule GoldenEx do\n"
        "  def alpha_probe() do\n"
        "    1\n"
        "  end\n"
        "\n"
        "  def beta_probe() do\n"
        "    alpha_probe()\n"
        "  end\n"
        "end\n"
    ),
    "hcl": (
        'resource "aws_instance" "alpha_probe" {\n'
        '  ami = "ami-123"\n'
        "}\n"
    ),
    "c": (
        "int alpha_probe(void) {\n"
        "    return 1;\n"
        "}\n"
    ),
}


# ===================================================================
# golden 断言表（实测固化，见 _smoke_probe3 的完整 dump）
# ===================================================================
# 每项：`lang_id -> (符号短名, kind, qualified_name 后缀)` 三元组列表。
# qualified_name 完整形态 = `<workspace_dir 小写正斜杠>/golden/<后缀>`。
#
# 🔴 kind 取 `query.search` 读面返回值（不是 symbols 表里的值——两个管线
# 对 ruby/js/ts/cpp/hcl/elixir 的顶层函数给出的 kind 不同：store 侧为
# `unknown`，DB 侧为 `function`）。golden 固化 store 侧实测值；若将来统一，
# 本表随之失效并显式暴露，而不是静默放行。
GOLDEN_EXPECTATIONS: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "python": (
        ("alpha_probe", "fn", "golden_python.alpha_probe"),
        ("BetaProbe", "class", "golden_python.BetaProbe"),
        ("gamma_probe", "fn", "golden_python.BetaProbe.gamma_probe"),
    ),
    "rust": (
        ("alpha_probe", "fn", "golden_rust.alpha_probe"),
        ("BetaProbe", "struct", "golden_rust.BetaProbe"),
    ),
    "go": (
        ("alpha_probe", "fn", "golden_go.alpha_probe"),
        ("BetaProbe", "struct", "golden_go.BetaProbe"),
    ),
    "java": (
        ("BetaProbe", "class", "golden_java.BetaProbe"),
        ("alpha_probe", "method", "golden_java.BetaProbe.alpha_probe"),
    ),
    "typescript": (
        ("alpha_probe", "unknown", "golden_ts.alpha_probe"),
        ("BetaProbe", "class", "golden_ts.BetaProbe"),
        ("gamma_probe", "method", "golden_ts.BetaProbe.gamma_probe"),
    ),
    "javascript": (
        ("alpha_probe", "unknown", "golden_js.alpha_probe"),
        ("BetaProbe", "class", "golden_js.BetaProbe"),
        ("gamma_probe", "method", "golden_js.BetaProbe.gamma_probe"),
    ),
    "ruby": (
        ("alpha_probe", "unknown", "golden_ruby.alpha_probe"),
        ("BetaProbe", "class", "golden_ruby.BetaProbe"),
        ("gamma_probe", "method", "golden_ruby.BetaProbe.gamma_probe"),
    ),
    "php": (
        ("alpha_probe", "fn", "golden_php.alpha_probe"),
        ("BetaProbe", "class", "golden_php.BetaProbe"),
        ("gamma_probe", "method", "golden_php.BetaProbe.gamma_probe"),
    ),
    "scala": (
        ("BetaProbe", "class", "golden_scala.BetaProbe"),
        ("alpha_probe", "method", "golden_scala.BetaProbe.alpha_probe"),
    ),
    "csharp": (
        ("BetaProbe", "class", "golden_cs.BetaProbe"),
        ("alpha_probe", "method", "golden_cs.BetaProbe.alpha_probe"),
    ),
    "cpp": (
        ("alpha_probe", "unknown", "golden_cpp.alpha_probe"),
        ("BetaProbe", "class", "golden_cpp.BetaProbe"),
        ("gamma_probe", "method", "golden_cpp.BetaProbe.gamma_probe"),
    ),
    "kotlin": (
        ("alpha_probe", "fn", "golden_kt.alpha_probe"),
        ("BetaProbe", "class", "golden_kt.BetaProbe"),
        ("gamma_probe", "fn", "golden_kt.BetaProbe.gamma_probe"),
    ),
    "swift": (
        ("alpha_probe", "method", "golden_swift.alpha_probe"),
        ("BetaProbe", "class", "golden_swift.BetaProbe"),
        ("gamma_probe", "method", "golden_swift.BetaProbe.gamma_probe"),
    ),
    "elixir": (
        ("GoldenEx", "module", "golden_ex.GoldenEx"),
        ("alpha_probe", "unknown", "golden_ex.GoldenEx.alpha_probe"),
        ("beta_probe", "unknown", "golden_ex.GoldenEx.beta_probe"),
    ),
    "hcl": (
        ("aws_instance.alpha_probe", "unknown",
         "golden_hcl.aws_instance.alpha_probe"),
    ),
    # C：扩展名映射到 lang_id "c"，但无解析配置 → 只索引、不出符号
    "c": (),
}

# 有解析配置、必须出符号的 15 个语言（C 除外）
SYMBOL_LANGS: Tuple[str, ...] = tuple(
    lang for lang in LANG_FIXTURES if GOLDEN_EXPECTATIONS[lang])

# golden 符号总数（15 个语种合计 38 个，用于统计面下界断言）
GOLDEN_SYMBOL_TOTAL = sum(len(v) for v in GOLDEN_EXPECTATIONS.values())


# ===================================================================
# 边界 fixture（与 golden 同批写入、同一次建图，避免多代 snapshot 叠加）
# ===================================================================
# 每项：`rel_path -> (内容, 期望符号名集合)`。
# bytes 内容用于非 UTF-8 场景（latin-1 字节序列）。
EDGE_FIXTURES: Dict[str, Tuple[Any, Tuple[str, ...]]] = {
    # 不支持的扩展名：不在 INDEXABLE_EXTS（fs_handlers.rs:26）→ 不入索引
    "golden/edge_unsupported.txt": ("not a source file at all\n", ()),
    # 空文件：可索引但 0 符号；不得炸掉建图
    "golden/edge_empty.py": ("", ()),
    # 含非 ASCII 标识符 + 注释（UTF-8）
    "golden/edge_unicode.py": (
        "def unicode_probe():\n    # 中文注释 emoji\n    return 1\n",
        ("unicode_probe",)),
    # BOM 前缀（canonicalize_source 负责剥离）
    "golden/edge_bom.py": (
        "\ufeffdef bom_probe():\n    return 1\n",
        ("bom_probe",)),
    # CRLF 行尾（canonicalize 归一为 LF）
    "golden/edge_crlf.py": (
        "def crlf_probe():\r\n    return 1\r\n",
        ("crlf_probe",)),
    # 非 UTF-8 字节（latin-1）：canonicalize 后仍须出符号
    "golden/edge_latin1.py": (
        b"def latin1_probe():\n    return '\xe9'\n",
        ("latin1_probe",)),
}


class LanguagesEnv(IsolationEnv):
    """TP-11L 多语言解析执行环境：TP-11 底座 + 解析面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11l-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_LANG,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- golden fixture 播种 ----------

    def write_golden_sources(self, langs: Optional[List[str]] = None
                             ) -> Dict[str, str]:
        """把 golden 源文件写入 workspace 内 `golden/` 目录。

        返回 {lang_id: 该文件的 workspace 相对路径（POSIX）}。
        """
        d = self.workspace_dir / GOLDEN_DIR
        d.mkdir(parents=True, exist_ok=True)
        out: Dict[str, str] = {}
        for lang in (langs or list(LANG_FIXTURES)):
            fname = LANG_FIXTURES[lang]
            rel = f"{GOLDEN_DIR}/{fname}"
            (d / fname).write_text(LANG_SOURCES[lang], encoding="utf-8")
            out[lang] = rel
        return out

    def write_edge_fixtures(self) -> Dict[str, str]:
        """写边界 fixture（不支持扩展名 / 空文件 / BOM / CRLF / 非 UTF-8）。"""
        out: Dict[str, str] = {}
        for rel, (content, _expected) in EDGE_FIXTURES.items():
            self.write_extra_file(rel, content)
            out[rel] = rel
        return out

    def write_extra_file(self, rel_path: str, content: Any) -> str:
        """在 workspace 内写任意边界 fixture（不支持扩展名 / 空文件 / 特殊字符）。"""
        p = self.workspace_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return rel_path

    # ---------- 解析读面 ----------

    def _inst(self, instance_id: str = "") -> str:
        return instance_id or self.setup_workspace()[1]

    def query_file(self, file_path: str, instance_id: str = "") -> Any:
        """query.file：取某文件的符号列表（无需 snapshot）。"""
        return self.rpc("query.file", {
            "workspace_instance_id": self._inst(instance_id),
            "file_path": file_path,
        })

    def query_symbol(self, qualified_name: str, instance_id: str = "") -> Any:
        """query.symbol：按 qualified_name 取符号详情（需先 snapshot.publish）。"""
        return self.rpc("query.symbol", {
            "workspace_instance_id": self._inst(instance_id),
            "qualified_name": qualified_name,
        })

    def query_search(self, query: str, kind: str = "", limit: int = 50,
                     instance_id: str = "") -> Any:
        """query.search：符号名模糊检索（需先 snapshot.publish）。"""
        params: Dict[str, Any] = {
            "workspace_instance_id": self._inst(instance_id),
            "query": query,
            "limit": limit,
        }
        if kind:
            params["kind"] = kind
        return self.rpc("query.search", params)

    def query_symbol_location(self, name: str, file_path: str,
                              instance_id: str = "") -> Any:
        return self.rpc("query.symbol_location", {
            "workspace_instance_id": self._inst(instance_id),
            "name": name,
            "file_path": file_path,
        })

    def query_stats(self, instance_id: str = "") -> Any:
        return self.rpc("query.stats",
                        {"workspace_instance_id": self._inst(instance_id)})

    # ---------- 结果规整 ----------

    def qname_prefix(self) -> str:
        """golden 符号 qualified_name 的公共前缀。

        实测形态：`<workspace_dir 小写 + 正斜杠>/golden/<文件名去扩展名>.<符号名>`。
        workspace_dir 是 `C:/.../TP-11L/ws`，盘符与全路径均小写。
        """
        return str(self.workspace_dir).replace("\\", "/").rstrip("/").lower() \
            + f"/{GOLDEN_DIR}/"

    def collect_symbols(self, queries: Optional[List[str]] = None,
                        limit: int = 200) -> Dict[str, Dict[str, Any]]:
        """按若干检索词取符号并集，返回 {qualified_name: 符号行}。

        `query.search` 是大小写不敏感的子串匹配（实测 `probe` 命中
        `BetaProbe`），故用少数几个检索词即可捞出 fixture 全部符号。
        响应里 `file_path` 已由 normalize_search_results 补齐。
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for q in (queries or ["probe", "GoldenEx", "aws_instance"]):
            r = self.query_search(q, limit=limit)
            assert r.ok, f"query.search({q!r}) 失败：{r!r}"
            for row in self.symbol_rows(r.result):
                qn = str(row.get("qualified_name") or "")
                if qn:
                    merged[qn] = row
        return merged

    @staticmethod
    def symbol_names(payload: Any) -> List[str]:
        """从 query.file / query.search 的响应里抽符号名列表。

        query.file 返回 list（或 {"symbols": [...]}）；query.search 返回 list
        或 {"results": [...]}。此处两种都收敛为 name 列表。
        """
        if isinstance(payload, dict):
            for key in ("symbols", "results", "items", "data"):
                v = payload.get(key)
                if isinstance(v, list):
                    payload = v
                    break
            else:
                return []
        if not isinstance(payload, list):
            return []
        out: List[str] = []
        for s in payload:
            if isinstance(s, dict):
                n = s.get("name") or s.get("symbol_name")
                if n:
                    out.append(str(n))
        return out

    @staticmethod
    def symbol_rows(payload: Any) -> List[Dict[str, Any]]:
        """同上，但返回完整行（供断言 qualified_name / kind / file_path）。"""
        if isinstance(payload, dict):
            for key in ("symbols", "results", "items", "data"):
                v = payload.get(key)
                if isinstance(v, list):
                    payload = v
                    break
            else:
                return []
        if not isinstance(payload, list):
            return []
        return [s for s in payload if isinstance(s, dict)]
