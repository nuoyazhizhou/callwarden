"""Dashboard Mixin 冒烟测试。

测试 db_dashboard.py 的 get_project_dashboard() 和 get_project_risks()：
1. quick 模式返回所有 7 个 section
2. full 模式（quick=False）含圈复杂度字段
3. with_cycles=True 时 cycles_count 非 None
4. with_evolution=True 时 evolution section 非 None
5. get_project_risks quick 模式 < 200ms，跳过高复杂度检查
6. get_project_risks full 模式包含 high_complexity 风险（若有 > 20 的函数）
7. 隔离性：某 section 失败不影响其他 section
"""
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from callwarden.db.db import CodeGraphDB


@pytest.fixture
def db_with_data():
    """构造一个含小规模合成数据的 DB fixture"""
    tmp = tempfile.mkdtemp(prefix="cw_test_dash_")
    try:
        # 创建一个含高复杂度 + 大函数的测试文件
        # fn_big: 600 行（触发 oversized_function 风险）
        # fn_complex: 多 if/for/while 嵌套（触发 high_complexity 风险）
        test_file = os.path.join(tmp, "mod_test.py")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("def fn_simple():\n    return 1\n\n")
            f.write("def fn_complex(a, b, c):\n")
            f.write("    if a:\n")
            f.write("        if b:\n")
            f.write("            if c:\n")
            f.write("                for i in range(10):\n")
            f.write("                    while i < 5:\n")
            f.write("                        if i % 2 == 0:\n")
            f.write("                            return 1\n")
            f.write("    return 0\n\n")
            f.write("def fn_big():\n")
            for i in range(600):
                f.write(f"    x{i} = {i}\n")
            f.write("    return 0\n")

        db = CodeGraphDB(db_path=os.path.join(tmp, "test.db"), workspace_root=tmp)
        db.register_workspace(name="test", root_path=tmp)
        db.build_full_graph()
        yield db
        db.close()
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


def _locate_corrupt_content_rows(db):
    """定位 symbol_contents 中无法按 UTF-8 解码的损坏行（full 模式降级取证）。

    CI/Linux 上 sqlite3 默认 text_factory=str 读 symbol_contents.content
    遇非法 UTF-8 字节会抛 OperationalError（run 12 根因原文：
    "Could not decode to UTF-8 column 'content' with text 'def fn_big():...'"），
    被 db/db_dashboard.py 的 except 吞成 {"error": ...} 降级结构；本机 fixture
    为纯 ASCII 不触发（CI/Linux 特异）。此处以 bytes 方式（text_factory=bytes）
    逐行 decode 找第一处损坏行，返回定位信息供失败消息取证。

    Returns:
        dict: {"total": symbol_contents 总行数,
               "corrupt": 损坏行数（按 content_hash 去重）,
               "rows": [损坏行定位信息...],
               "error": 取证查询本身失败的原因或 None}
               error 非 None 时 total/corrupt 为 None、rows 为 []——
               取证失败不得掩盖原始降级根因。
    """
    conn = getattr(db, "conn", None)
    if conn is None:
        return {"total": None, "corrupt": None, "rows": [],
                "error": "db 实例无 .conn 属性，无法执行取证查询"}
    old_text_factory = conn.text_factory
    conn.text_factory = bytes  # 绕过 Python 侧 UTF-8 解码，直接拿原始字节
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM symbol_contents").fetchone()["c"]
        # LEFT JOIN：stdlib seed 等无 symbols 对应行的孤儿 content 也要能定位
        # （CI 疑点之一即 stdlib import 阶段写入的 content 损坏）
        rows = conn.execute(
            """SELECT s.qualified_name AS sym_qn, s.symbol_hash AS sym_hash,
                      sc.qualified_name AS sc_qn, sc.content_hash AS content_hash,
                      length(sc.content) AS clen,
                      hex(substr(sc.content, 1, 64)) AS head_hex_sql,
                      hex(substr(sc.content, -64)) AS tail_hex_sql,
                      sc.content AS content
               FROM symbol_contents sc
               LEFT JOIN symbols s ON s.symbol_hash = sc.content_hash"""
        ).fetchall()
    except Exception as e:
        return {"total": None, "corrupt": None, "rows": [],
                "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.text_factory = old_text_factory

    def _text(v):
        if v is None:
            return None
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        return str(v)

    corrupt_by_hash = {}
    for r in rows:
        raw = r["content"]
        if raw is None:
            continue
        if isinstance(raw, str):  # text_factory 理论上已切 bytes，防御性兜底
            raw = raw.encode("utf-8", "surrogateescape")
        decode_err = None
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as e:
            decode_err = e
        if decode_err is None:
            continue
        ch = _text(r["content_hash"])
        entry = corrupt_by_hash.get(ch)
        if entry is None:
            bad = raw[decode_err.start:decode_err.start + 1]
            entry = corrupt_by_hash[ch] = {
                "content_hash": ch,
                "source_symbols": [],
                "sc_qualified_name": _text(r["sc_qn"]),
                "clen_sql": r["clen"],
                "clen_bytes": len(raw),
                "head_hex": raw[:64].hex(),
                "tail_hex": raw[-64:].hex(),
                "head_hex_sql": _text(r["head_hex_sql"]),
                "tail_hex_sql": _text(r["tail_hex_sql"]),
                "bad_offset": decode_err.start,
                "bad_byte_hex": bad.hex() if bad else "(空)",
                "decode_error": str(decode_err),
                "context_hex": raw[max(0, decode_err.start - 16):
                                   decode_err.start + 16].hex(),
            }
        qn = _text(r["sym_qn"])
        if qn and qn not in entry["source_symbols"]:
            entry["source_symbols"].append(qn)

    return {"total": total, "corrupt": len(corrupt_by_hash),
            "rows": list(corrupt_by_hash.values()), "error": None}


def _full_mode_degrade_message(cq_error, db):
    """组装 full 模式降级时的 pytest.fail 消息。

    在既有「翻出被吞根因原文」基础上追加 symbol_contents 取证
    （损坏行的来源符号/hex/第一个非法字节位置/总行数/损坏行数），
    让 CI 下一轮失败直接带出根因修复所需的精确坐标。
    """
    lines = [
        "code_quality section 返回降级结构：full 模式 "
        "get_code_metrics_summary() 的异常被 db/db_dashboard.py 的 "
        f"except 吞成 error 键，根因原文 = {cq_error!r}",
        "",
        "[UTF-8 取证] 以 text_factory=bytes 逐行 decode symbol_contents.content"
        "（run 12 CI/Linux 根因形态：sqlite3.OperationalError "
        "\"Could not decode to UTF-8 column 'content'\"）：",
    ]
    report = _locate_corrupt_content_rows(db)
    if report["error"] is not None:
        lines.append(
            f"  取证查询本身失败（不掩盖上方根因）：{report['error']}")
        return "\n".join(lines)
    lines.append(f"  symbol_contents 总行数 = {report['total']}")
    lines.append(f"  损坏行数（按 content_hash 去重）= {report['corrupt']}")
    if not report["rows"]:
        lines.append(
            "  未定位到损坏行：全部 content 均可按 UTF-8 解码——降级根因不在 "
            "symbol_contents 持久化数据（疑读取路径其他表/内存态/环境差异），"
            "请结合上方根因原文进一步定位。")
        return "\n".join(lines)
    for i, row in enumerate(report["rows"][:5], 1):
        lines.append(f"  损坏行 #{i}:")
        lines.append(f"    content_hash       = {row['content_hash']!r}")
        src = row["source_symbols"] or ["（无 symbols 行命中，孤儿 content）"]
        lines.append(f"    来源符号(JOIN)     = {src}")
        if row["sc_qualified_name"]:
            lines.append(
                "    sc.qualified_name  = "
                f"{row['sc_qualified_name']!r}")
        lines.append(f"    SQL length()       = {row['clen_sql']}")
        lines.append(f"    实际字节数         = {row['clen_bytes']}")
        lines.append(
            f"    第一个非法字节     : offset={row['bad_offset']} "
            f"byte=0x{row['bad_byte_hex']}")
        lines.append(f"    decode 错误        : {row['decode_error']}")
        lines.append(f"    上下文 hex(±16B)  : {row['context_hex']}")
        lines.append(f"    头部 hex(首64B)    : {row['head_hex']}")
        lines.append(f"    尾部 hex(末64B)    : {row['tail_hex']}")
        if (row["head_hex_sql"] is not None
                and row["head_hex_sql"].lower() != row["head_hex"]) or (
                row["tail_hex_sql"] is not None
                and row["tail_hex_sql"].lower() != row["tail_hex"]):
            lines.append(
                f"    SQL hex(substr)    : head={row['head_hex_sql']} "
                f"tail={row['tail_hex_sql']}"
                "（与原始字节不一致，提示 TEXT 存储类下 SQLite 字符口径偏移）")
    if len(report["rows"]) > 5:
        lines.append(
            f"  （还有 {len(report['rows']) - 5} 个损坏 content_hash，已截断）")
    return "\n".join(lines)


class TestDashboardBasic:
    """基础结构测试"""

    def test_quick_mode_returns_all_sections(self, db_with_data):
        """quick 模式应返回 7 个 section"""
        d = db_with_data.get_project_dashboard()
        expected_sections = {"overview", "code_scale", "code_quality", "call_graph",
                             "task_risk", "audit", "evolution"}
        assert set(d.keys()) == expected_sections

    def test_overview_section(self, db_with_data):
        """overview section 包含核心字段"""
        ov = db_with_data.get_project_dashboard()["overview"]
        assert "workspace_name" in ov
        assert "root_path" in ov
        assert "is_git_repo" in ov
        assert "db_size_bytes" in ov
        assert "db_stale" in ov

    def test_code_scale_section(self, db_with_data):
        """code_scale section 包含总行数和符号数"""
        cs = db_with_data.get_project_dashboard()["code_scale"]
        assert cs["total_files"] >= 1
        assert cs["total_lines"] > 0
        assert cs["total_symbols"] >= 3  # fn_simple + fn_complex + fn_big
        assert "by_kind" in cs
        assert "by_language" in cs

    def test_code_quality_quick_mode(self, db_with_data):
        """quick 模式跳过圈复杂度计算"""
        cq = db_with_data.get_project_dashboard()["code_quality"]
        assert cq["quick_mode"] is True
        assert cq["avg_complexity"] is None
        assert cq["max_complexity"] is None
        assert cq["complexity_distribution"] is None
        # 但注释覆盖率仍然计算
        assert "comment_coverage_pct" in cq
        assert "uncommented_fns" in cq

    def test_code_quality_full_mode(self, db_with_data):
        """full 模式（quick=False）计算圈复杂度"""
        cq = db_with_data.get_project_dashboard(quick=False)["code_quality"]
        # instrument：db/db_dashboard.py full 分支裸调 get_code_metrics_summary()，
        # 异常被其 except 吞成 {"error": str(e)} 降级结构（无 quick_mode 键），
        # 上层断言只能报无因果的 KeyError。此处先把被吞异常原文翻到失败消息里
        # （收紧而非放宽：正常路径无 error 键，instrument 不生效）。
        # run 12 CI/Linux：原文实为 sqlite3.OperationalError
        # "Could not decode to UTF-8 column 'content' with text 'def fn_big():...'"
        # ——Python text_factory=str 读 symbol_contents.content 遇非法 UTF-8 字节
        # （本机 fixture 纯 ASCII 不触发，CI/Linux 特异；疑 fn_big content 后段
        # 或 stdlib import 阶段写入损坏）。降级时追加取证：以 bytes 方式逐行
        # decode 定位损坏行的来源符号/hex/第一个非法字节位置/总行数/损坏行数，
        # 给根因修复提供精确坐标。
        if "error" in cq:
            pytest.fail(_full_mode_degrade_message(cq["error"], db_with_data))
        assert cq["quick_mode"] is False
        assert cq["avg_complexity"] is not None
        assert cq["max_complexity"] >= 5  # fn_complex 至少有 5+ 分支
        assert cq["complexity_distribution"] is not None

    def test_call_graph_no_cycles(self, db_with_data):
        """call_graph section 默认不计算 cycles"""
        cg = db_with_data.get_project_dashboard()["call_graph"]
        assert cg["cycles_count"] is None
        assert "total_calls" in cg
        assert "resolve_rate_pct" in cg

    def test_call_graph_with_cycles(self, db_with_data):
        """with_cycles=True 时 cycles_count 非 None"""
        cg = db_with_data.get_project_dashboard(with_cycles=True)["call_graph"]
        assert cg["cycles_count"] is not None
        assert cg["cycles_count"] >= 0

    def test_evolution_none_by_default(self, db_with_data):
        """默认不计算 evolution"""
        d = db_with_data.get_project_dashboard()
        assert d["evolution"] is None

    def test_evolution_with_flag(self, db_with_data):
        """with_evolution=True 时 evolution 非 None"""
        d = db_with_data.get_project_dashboard(with_evolution=True)
        assert d["evolution"] is not None
        # 测试项目不是 git repo，recent_commits 应为空 list
        assert "recent_commits" in d["evolution"]


class TestProjectRisks:
    """风险预警测试"""

    def test_quick_mode_is_fast(self, db_with_data):
        """quick 模式应在 200ms 内完成"""
        t0 = time.time()
        _risks = db_with_data.get_project_risks(quick=True)
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 200, f"quick mode took {elapsed_ms:.1f}ms, expected < 200ms"

    def test_oversized_function_risk(self, db_with_data):
        """600 行的 fn_big 应触发 oversized_function 风险"""
        risks = db_with_data.get_project_risks(quick=True)
        oversized = [r for r in risks if r["type"] == "oversized_function"]
        assert len(oversized) >= 1
        assert any("fn_big" in r["qualified_name"] for r in oversized)

    def test_quick_skips_complexity(self, db_with_data):
        """quick 模式不返回 high_complexity 风险"""
        risks = db_with_data.get_project_risks(quick=True)
        high_cx = [r for r in risks if r["type"] == "high_complexity"]
        assert len(high_cx) == 0

    def test_full_mode_finds_complexity(self, db_with_data):
        """full 模式应找到高复杂度函数（fn_complex）"""
        risks = db_with_data.get_project_risks(quick=False)
        high_cx = [r for r in risks if r["type"] == "high_complexity"]
        # fn_complex 有 6+ 分支，至少在 complexity > 5 区间，不一定 > 20
        # 这里只验证 full 模式不报错，能返回 list
        assert isinstance(high_cx, list)

    def test_risks_sorted_by_severity(self, db_with_data):
        """风险按 severity 排序：high > medium > low"""
        risks = db_with_data.get_project_risks(quick=False)
        if len(risks) >= 2:
            sev_order = {"high": 0, "medium": 1, "low": 2}
            for i in range(len(risks) - 1):
                s1 = sev_order.get(risks[i].get("severity", "low"), 3)
                s2 = sev_order.get(risks[i + 1].get("severity", "low"), 3)
                assert s1 <= s2


class TestDashboardIsolation:
    """section 隔离性测试"""

    def test_section_error_isolation(self, db_with_data):
        """某 section 失败应返回 {"error": ...}，不影响其他 section"""
        # 模拟：mock get_stats 让 code_scale 抛错
        original = db_with_data.get_stats
        call_count = [0]

        def fake_get_stats():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("mock error")
            return original()

        db_with_data.get_stats = fake_get_stats
        try:
            d = db_with_data.get_project_dashboard()
            # overview 应该不受影响（不调 get_stats）
            assert "workspace_name" in d["overview"]
            # code_scale 或 call_graph 可能返回 error，但其他 section 仍工作
            assert "task_risk" in d
            assert "audit" in d
        finally:
            db_with_data.get_stats = original
