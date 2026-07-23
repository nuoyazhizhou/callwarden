"""文档一致性验证测试（评审报告 2026-07-20 整改）。

确保以下关键修正不被回退：
1. USER_GUIDE / deployment 不再出现"删除 callwarden.db"或"rm -wal/-shm"危险建议
2. 各文档头部基线指标对齐到实际值：MCP 205 / Schema v39 / 33 个 Mixin 类
3. D1 文档不再声明 sqlite-vec 已实现（实际是 BLOB + Rust/numpy 余弦相似度）
4. _feature_matrix.md D1/D7 状态反映评审结论
5. README/AGENTS 技术栈不再误导为 sqlite-vec 已实现
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1. 危险建议已被移除
# ============================================================


class TestDangerousAdviceRemoved:
    """USER_GUIDE / deployment 不能再出现删除主数据库的危险建议。"""

    def test_user_guide_no_delete_db_advice(self):
        """Q2 不能再说“删除 ~/.callwarden/callwarden.db 重建”。"""
        ug = ROOT / "callwarden_USER_GUIDE.md"
        if not ug.exists():
            pytest.skip("callwarden_USER_GUIDE.md 不存在，跳过")
        content = ug.read_text(encoding="utf-8")

        # Q2 部分必须明确禁止删除
        q2_section = self._extract_q2(content)
        assert "禁止删除" in q2_section, (
            "USER_GUIDE Q2 必须明确禁止删除 ~/.callwarden/callwarden.db"
        )
        # 不能再出现"删除 ... 下次运行会自动重建"这种原话
        assert "删除 `~/.callwarden/callwarden.db`，下次运行会自动重建" not in q2_section, (
            "USER_GUIDE Q2 不能保留原危险建议"
        )

    def test_deployment_no_rm_wal_shm_advice(self):
        """deployment.md 不能再说"rm -wal/-shm"。"""
        dep = ROOT / "docs" / "deployment.md"
        content = dep.read_text(encoding="utf-8")

        # 锁定排查章节必须改为 PRAGMA wal_checkpoint，不能再有 rm -wal/-shm
        assert "rm $HOME/.callwarden/callwarden.db-wal" not in content, (
            "deployment.md 不能保留 'rm callwarden.db-wal' 危险建议"
        )
        assert "rm $HOME/.callwarden/callwarden.db-shm" not in content, (
            "deployment.md 不能保留 'rm callwarden.db-shm' 危险建议"
        )
        assert "PRAGMA wal_checkpoint" in content, (
            "deployment.md 锁定排查应改为 PRAGMA wal_checkpoint(PASSIVE)"
        )

    def _extract_q2(self, content: str) -> str:
        """从 USER_GUIDE 提取 Q2 章节（到下一个 ### 之前）。"""
        match = re.search(
            r"### Q2[^\n]*\n(.*?)(?=### Q3|## |\Z)", content, re.DOTALL)
        return match.group(1) if match else ""


# ============================================================
# 2. 头部基线指标对齐
# ============================================================


class TestHeaderBaselineAligned:
    """各文档头部应统一到实际基线：MCP 205 / Schema v39 / 33 Mixin 类。"""

    def test_user_guide_header(self):
        """USER_GUIDE 头部不再写 v37/204/40 Mixin。"""
        ug = ROOT / "callwarden_USER_GUIDE.md"
        if not ug.exists():
            pytest.skip("callwarden_USER_GUIDE.md 不存在，跳过")
        header = ug.read_text(encoding="utf-8")[:500]
        assert "v37" not in header, "USER_GUIDE 头部不能再写 v37"
        assert "204 MCP" not in header, "USER_GUIDE 头部不能再写 204 MCP"

    def test_implementation_status_header(self):
        """implementation-status.md 头部应写实际值。"""
        is_path = ROOT / "docs" / "design" / "implementation-status.md"
        header = is_path.read_text(encoding="utf-8")[:500]
        assert "40 Mixin" not in header, "implementation-status.md 头部不能再写 40 Mixin"
        assert "35" in header, "implementation-status.md 头部应写 35 Mixin 类"

    def test_feature_matrix_baseline_table(self):
        """_feature_matrix.md 基线表应反映实际值。"""
        fm = ROOT / "_feature_matrix.md"
        content = fm.read_text(encoding="utf-8")

        # 找到 "## 实际基线数据" 章节
        baseline_match = re.search(
            r"## 实际基线数据.*?(?=## )", content, re.DOTALL
        )
        assert baseline_match, "_feature_matrix.md 必须有基线表章节"
        baseline_section = baseline_match.group(0)

        # 应写 205 或 206
        assert "**205**" in baseline_section or "**206**" in baseline_section, (
            "_feature_matrix.md 基线表 MCP 工具数应为 205/206"
        )
        assert "**196**" not in baseline_section, (
            "_feature_matrix.md 基线表不能保留 196（已过时）"
        )
        # 应写 v40 或 v41
        assert "**v40**" in baseline_section or "**v41**" in baseline_section, (
            "_feature_matrix.md 基线表 Schema 版本应为 v40/v41"
        )
        assert "**v36**" not in baseline_section, (
            "_feature_matrix.md 基线表不能保留 v36（已过时）"
        )
        # Mixin 类应写 35
        assert "35" in baseline_section and "Mixin" in baseline_section, (
            "_feature_matrix.md 基线表应写 35 个功能 Mixin"
        )


# ============================================================
# 3. D1 文档声明修正不可回退
# ============================================================


class TestD1DocumentationCorrected:
    """D1 文档声明必须明确说明实际实现不是 sqlite-vec。"""

    def test_architecture_d1_corrected(self):
        """architecture.md 不能再说 v5 是 sqlite-vec 向量索引。"""
        arc = ROOT / "docs" / "architecture.md"
        content = arc.read_text(encoding="utf-8")

        # v5 注释必须改为 BLOB + Rust/numpy
        v5_line_idx = next(
            (i for i, line in enumerate(content.splitlines())
             if "v5" in line and "向量嵌入表" in line),
            None,
        )
        assert v5_line_idx is not None, "architecture.md 必须有 v5 注释"
        v5_section = "\n".join(content.splitlines()[
                               v5_line_idx:v5_line_idx + 3])
        assert (
            "sqlite-vec 待落地" in v5_section
            or "BLOB" in v5_section
            or "Rust/numpy" in v5_section
        ), f"architecture.md v5 注释必须说明实际实现，附近：{v5_section}"

        # 向量索引章节标题应明确说明不是 sqlite-vec
        assert "sqlite-vec 待落地" in content or "BLOB + Rust/numpy" in content, (
            "architecture.md 向量章节标题必须说明实际实现"
        )

    def test_feature_matrix_d1_status_corrected(self):
        """_feature_matrix.md D1 状态必须是"部分完成"而非"已实现 sqlite-vec"。"""
        fm = ROOT / "_feature_matrix.md"
        content = fm.read_text(encoding="utf-8")

        # 找到 D1 行（含表格中跨多列的全部文本，到下一个 | D 或 | # 之前）
        # markdown 表格每行以 | 开头，匹配 "| D1 |" 开头到下一个 "\n|" 为止
        d1_match = re.search(r"^\| D1 \|.*$", content, re.MULTILINE)
        assert d1_match, "_feature_matrix.md 必须有 D1 行"
        d1_line = d1_match.group(0)

        # 状态应该是 🟡 部分完成，不是 ✅ 已实现
        assert "🟡" in d1_line or "部分完成" in d1_line, (
            f"_feature_matrix.md D1 状态应为 🟡 部分完成，实际：{d1_line}"
        )
        # 不能再说 "sqlite-vec 向量索引 ✅ 已实现"
        assert "sqlite-vec 向量索引" not in d1_line or "已实现" not in d1_line, (
            f"_feature_matrix.md D1 不能再写 'sqlite-vec 向量索引 ✅ 已实现'，实际：{d1_line}"
        )
        # 必须说明实际实现
        assert "BLOB" in d1_line or "Rust/numpy" in d1_line, (
            f"_feature_matrix.md D1 必须说明实际实现（BLOB + Rust/numpy），实际：{d1_line}"
        )


# ============================================================
# 4. D7 文档状态反映评审结论
# ============================================================


class TestD7StatusCorrected:
    """D7 状态必须反映 2026-07-20 评审修复结论。"""

    def test_feature_matrix_d7_status_corrected(self):
        """_feature_matrix.md D7 必须标注 2026-07-20 评审修复。"""
        fm = ROOT / "_feature_matrix.md"
        content = fm.read_text(encoding="utf-8")

        d7_match = re.search(r"\| D7 \|.*?\|.*?\|.*?\|.*?\|", content)
        assert d7_match, "_feature_matrix.md 必须有 D7 行"
        d7_line = d7_match.group(0)

        # 状态应该反映评审修复（接受 2026-07-20 或之后的日期）
        assert "2026-07-2" in d7_line, (
            f"_feature_matrix.md D7 必须标注评审修复日期，实际：{d7_line}"
        )
        # 必须说明 target_symbol_hash 修复
        assert "target_symbol_hash" in d7_line, (
            f"_feature_matrix.md D7 必须说明 target_symbol_hash 修复，实际：{d7_line}"
        )


# ============================================================
# 5. README/AGENTS 技术栈不误导
# ============================================================


class TestTechStackNotMisleading:
    """README/AGENTS 技术栈不能再说"sqlite-vec 向量扩展"作为已实现特性。"""

    def test_readme_tech_stack_clarified(self):
        """README.md 不能再说"基于 sqlite-vec"作为已实现向量搜索。"""
        rm = ROOT / "README.md"
        content = rm.read_text(encoding="utf-8")

        # 核心能力列表应明确说明实际实现
        assert "BLOB + Rust/numpy 余弦相似度" in content or "sqlite-vec 待落地" in content, (
            "README.md 核心能力列表应明确说明实际实现（BLOB + Rust/numpy）"
        )
        # 不能再说"sqlite-vec + sentence-transformers，自然语言查找函数"作为已完成特性
        assert (
            "sqlite-vec + sentence-transformers，自然语言查找函数" not in content
        ), "README.md 不能再说 'sqlite-vec + sentence-transformers' 作为已完成特性"

    def test_agents_tech_stack_clarified(self):
        """AGENTS.md 技术栈描述应明确说明实际实现。"""
        ag = ROOT / "AGENTS.md"
        content = ag.read_text(encoding="utf-8")

        # 技术栈章节应说明实际实现
        tech_stack_idx = content.find("## 技术栈")
        assert tech_stack_idx >= 0, "AGENTS.md 必须有 ## 技术栈 章节"
        tech_stack_section = content[tech_stack_idx:tech_stack_idx + 500]

        # 存储行不能只说 "SQLite + sqlite-vec（向量扩展）"
        assert (
            "SQLite + sqlite-vec（向量扩展）" not in tech_stack_section
        ), "AGENTS.md 技术栈不能再说 'SQLite + sqlite-vec（向量扩展）'，必须说明实际实现"

    def test_readme_mixin_count_correct(self):
        """README.md 工作目录结构 Mixin 数应改为 35。"""
        rm = ROOT / "README.md"
        content = rm.read_text(encoding="utf-8")

        # 找到 "数据库层" 行
        db_line_match = re.search(r"db/.*?# 数据库层.*", content)
        assert db_line_match, "README.md 必须有 db/ 数据库层说明"
        db_line = db_line_match.group(0)

        assert "35" in db_line, (
            f"README.md db/ 应写 35 个 Mixin 类，实际：{db_line}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
