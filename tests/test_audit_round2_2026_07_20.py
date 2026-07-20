"""二轮评审整改验证测试（评审报告 2026-07-20 第二轮）。

覆盖首轮测试未涉及的状态修正，确保以下修正不被回退：
1. _feature_matrix.md M4/M5/M6/M8/M10 状态为 🟡 部分完成
2. _feature_matrix.md N3/N5/N6/N7/N8 状态为 ❌ 声明不成立，N4 为 🟡
3. _feature_matrix.md N 章节标题反映实际状态（"脚本骨架存在/产物未落地"）
4. _feature_matrix.md I7 反映 D7 修复后状态（不再是 🟡）
5. _feature_matrix.md I17 标注 USER_GUIDE L118 修复点
6. _feature_matrix.md I20-I38 二轮评审新增条目存在
7. USER_GUIDE.md L118 MCP 工具数 = 205
8. implementation-status.md Prometheus 状态为 ❌ 未实现
9. 各文档 Mixin 数 = 33（CONTRIBUTING / architecture / naming-report / implementation-status / _health_check_report / history README）
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1. M 章节状态修正
# ============================================================


class TestMatrixMSectionCorrected:
    """_feature_matrix.md M 章节 Rust 扩展 5 个条目状态修正。"""

    @pytest.fixture
    def matrix_content(self):
        return (ROOT / "_feature_matrix.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("mid,expected_keyword", [
        ("M4", "🟡"),
        ("M5", "🟡"),
        ("M6", "🟡"),
        ("M8", "🟡"),
        ("M10", "🟡"),
    ])
    def test_m_entries_partial_complete(self, matrix_content, mid, expected_keyword):
        """M4/M5/M6/M8/M10 必须是 🟡 部分完成状态。"""
        line_match = re.search(rf"^\| {mid} \|.*$", matrix_content, re.MULTILINE)
        assert line_match, f"_feature_matrix.md 必须有 {mid} 行"
        line = line_match.group(0)
        assert expected_keyword in line, (
            f"{mid} 状态应为 {expected_keyword}（部分完成），实际：{line}"
        )
        assert "评审 2026-07-20" in line, (
            f"{mid} 必须标注评审日期，实际：{line}"
        )

    @pytest.mark.parametrize("mid", ["M1", "M2", "M3", "M7", "M9"])
    def test_m_entries_still_complete(self, matrix_content, mid):
        """M1/M2/M3/M7/M9 仍应为 ✅ 已实现。"""
        line_match = re.search(rf"^\| {mid} \|.*$", matrix_content, re.MULTILINE)
        assert line_match, f"_feature_matrix.md 必须有 {mid} 行"
        line = line_match.group(0)
        assert "✅" in line, f"{mid} 状态应保持 ✅，实际：{line}"


# ============================================================
# 2. N 章节状态修正
# ============================================================


class TestMatrixNSectionCorrected:
    """_feature_matrix.md N 章节跨平台打包状态修正。"""

    @pytest.fixture
    def matrix_content(self):
        return (ROOT / "_feature_matrix.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("mid", ["N3", "N5", "N6", "N7", "N8"])
    def test_n_entries_false_claim(self, matrix_content, mid):
        """N3/N5/N6/N7/N8 必须是 ❌ 声明不成立。"""
        line_match = re.search(rf"^\| {mid} \|.*$", matrix_content, re.MULTILINE)
        assert line_match, f"_feature_matrix.md 必须有 {mid} 行"
        line = line_match.group(0)
        assert "❌" in line, f"{mid} 状态应为 ❌ 声明不成立，实际：{line}"
        assert "评审 2026-07-20" in line, (
            f"{mid} 必须标注评审日期，实际：{line}"
        )

    def test_n4_partial_complete(self, matrix_content):
        """N4 必须是 🟡 部分完成。"""
        line_match = re.search(r"^\| N4 \|.*$", matrix_content, re.MULTILINE)
        assert line_match, "_feature_matrix.md 必须有 N4 行"
        line = line_match.group(0)
        assert "🟡" in line, f"N4 状态应为 🟡 部分完成，实际：{line}"
        assert "评审 2026-07-20" in line

    def test_n_section_title_reflects_reality(self, matrix_content):
        """N 章节标题应反映"产物未落地"的实际情况。"""
        # 找到 "## N." 开头的标题
        title_match = re.search(r"^## N\..*$", matrix_content, re.MULTILINE)
        assert title_match, "_feature_matrix.md 必须有 N 章节标题"
        title = title_match.group(0)
        # 不能再说"完整实现"
        assert "完整实现" not in title, (
            f"N 章节标题不能再说'完整实现'，实际：{title}"
        )
        # 应该反映实际状态
        assert "脚本骨架" in title or "产物未落地" in title or "部分实施" in title, (
            f"N 章节标题应反映实际状态，实际：{title}"
        )


# ============================================================
# 3. I7 / I17 状态更新
# ============================================================


class TestMatrixI7I17Updated:
    """_feature_matrix.md I7/I17 在二轮评审后状态更新。"""

    def test_i7_reflects_d7_fix(self):
        """I7 应反映 D7 修复后状态，不再停留在'建议撤销'文字层面。"""
        fm = ROOT / "_feature_matrix.md"
        content = fm.read_text(encoding="utf-8")

        i7_match = re.search(r"^\| I7 \|.*$", content, re.MULTILINE)
        assert i7_match, "_feature_matrix.md 必须有 I7 行"
        i7_line = i7_match.group(0)

        # I7 应该提到 D7 修复和 target_symbol_hash
        assert "target_symbol_hash" in i7_line or "D7" in i7_line, (
            f"I7 应反映 D7 修复，实际：{i7_line}"
        )
        # 应该标注 2026-07-20 更新
        assert "2026-07-20" in i7_line, (
            f"I7 应标注 2026-07-20 更新日期，实际：{i7_line}"
        )

    def test_i17_mentions_user_guide_l118_fix(self):
        """I17 应标注 USER_GUIDE L118 204→205 的补修复。"""
        fm = ROOT / "_feature_matrix.md"
        content = fm.read_text(encoding="utf-8")

        i17_match = re.search(r"^\| I17 \|.*$", content, re.MULTILINE)
        assert i17_match, "_feature_matrix.md 必须有 I17 行"
        i17_line = i17_match.group(0)

        # I17 应该提到 USER_GUIDE L118 修复
        assert "L118" in i17_line or "USER_GUIDE" in i17_line, (
            f"I17 应标注 USER_GUIDE L118 修复，实际：{i17_line}"
        )


# ============================================================
# 4. I20-I38 二轮评审新增条目
# ============================================================


class TestMatrixI20PlusEntriesExist:
    """_feature_matrix.md I20-I38 二轮评审新增条目必须存在。"""

    @pytest.fixture
    def matrix_content(self):
        return (ROOT / "_feature_matrix.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("iid,expected_keyword", [
        ("I20", "A14"),
        ("I21", "A15"),
        ("I22", "A19"),
        ("I23", "A23"),
        ("I24", "C10"),
        ("I25", "D5"),
        ("I26", "F11"),
        ("I27", "F19"),
        ("I28", "G1"),
        ("I29", "G10"),
        ("I30", "G13"),
        ("I31", "G14"),
        ("I32", "G23"),
        ("I33", "H5"),
        ("I34", "H6"),
        ("I35", "L1"),
        ("I36", "M4"),
        ("I37", "N3"),
        ("I38", "Mixin"),
    ])
    def test_entry_exists(self, matrix_content, iid, expected_keyword):
        """I20-I38 条目必须存在并标注对应章节。"""
        line_match = re.search(rf"^\| {iid} \|.*$", matrix_content, re.MULTILINE)
        assert line_match, f"_feature_matrix.md 必须有 {iid} 行"
        line = line_match.group(0)
        # 必须标注 2026-07-20 二轮评审
        assert "2026-07-20" in line, (
            f"{iid} 必须标注 2026-07-20 二轮评审日期，实际：{line}"
        )
        # 必须包含对应章节关键词
        assert expected_keyword in line, (
            f"{iid} 应包含关键词 {expected_keyword}，实际：{line}"
        )


# ============================================================
# 5. USER_GUIDE L118 MCP 数 = 205
# ============================================================


class TestUserGuideMcpCountAligned:
    """USER_GUIDE.md 项目结构中 MCP 工具数必须 = 205。"""

    def test_user_guide_mcp_count(self):
        """USER_GUIDE.md L118 mcp_server.py 注释必须写 205 个工具。"""
        ug = ROOT / "callwarden_USER_GUIDE.md"
        content = ug.read_text(encoding="utf-8")

        # 找到 mcp_server.py 行
        mcp_line_match = re.search(r"mcp_server\.py.*MCP 服务器.*", content)
        assert mcp_line_match, "USER_GUIDE.md 必须有 mcp_server.py 项目结构行"
        mcp_line = mcp_line_match.group(0)

        # 不能再写 204 个工具
        assert "204 个工具" not in mcp_line, (
            f"USER_GUIDE.md mcp_server.py 不能再写 204 个工具，实际：{mcp_line}"
        )
        # 必须写 205 个工具
        assert "205 个工具" in mcp_line, (
            f"USER_GUIDE.md mcp_server.py 必须写 205 个工具，实际：{mcp_line}"
        )


# ============================================================
# 6. implementation-status.md Prometheus 状态 = ❌ 未实现
# ============================================================


class TestImplementationStatusPrometheusCorrected:
    """implementation-status.md Prometheus 状态必须为 ❌ 未实现。"""

    def test_prometheus_status_false(self):
        """implementation-status.md Prometheus 应标 ❌ 未实现。"""
        is_path = ROOT / "docs" / "design" / "implementation-status.md"
        content = is_path.read_text(encoding="utf-8")

        # 找到 Prometheus 相关行
        prometheus_idx = content.find("Prometheus")
        assert prometheus_idx >= 0, (
            "implementation-status.md 必须有 Prometheus 条目"
        )
        # Prometheus 周边 500 字符应包含 ❌ 未实现
        nearby = content[prometheus_idx:prometheus_idx + 500]
        assert "❌" in nearby or "未实现" in nearby, (
            "implementation-status.md Prometheus 应标 ❌ 未实现"
        )
        # 不能再标 ⚠️ 部分
        assert "⚠️ 部分" not in nearby or "Prometheus" not in nearby.split("⚠️ 部分")[0][-100:], (
            "implementation-status.md Prometheus 不能再标 ⚠️ 部分"
        )


# ============================================================
# 7. 各文档 Mixin 数 = 33
# ============================================================


class TestMixinCountConsistent:
    """关键文档 Mixin 数必须统一为 33（不能回退到 40）。"""

    @pytest.mark.parametrize("rel_path", [
        "CONTRIBUTING.md",
        "docs/architecture.md",
        "docs/naming-analysis-report.md",
        "docs/design/implementation-status.md",
        "tests/_health_check_report.md",
        "docs/history/README.md",
    ])
    def test_no_40_mixin(self, rel_path):
        """文档中不能再写 40 个 Mixin。"""
        path = ROOT / rel_path
        if not path.exists():
            pytest.skip(f"{rel_path} 不存在，跳过")
        content = path.read_text(encoding="utf-8")

        # 不能再写"40 个 Mixin"或"40 Mixin"
        assert "40 个 Mixin" not in content, (
            f"{rel_path} 不能再写 '40 个 Mixin'"
        )
        assert "40 Mixin" not in content, (
            f"{rel_path} 不能再写 '40 Mixin'"
        )

    @pytest.mark.parametrize("rel_path", [
        "CONTRIBUTING.md",
        "docs/architecture.md",
        "docs/design/implementation-status.md",
    ])
    def test_33_mixin_present(self, rel_path):
        """关键文档必须写 33 个 Mixin 类。"""
        path = ROOT / rel_path
        if not path.exists():
            pytest.skip(f"{rel_path} 不存在，跳过")
        content = path.read_text(encoding="utf-8")

        # 必须写 33（Mixin 数）
        assert "33" in content and "Mixin" in content, (
            f"{rel_path} 必须写 33 Mixin 类"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
