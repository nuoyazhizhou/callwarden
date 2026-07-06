"""architecture.md 文档完整性测试。

覆盖 A1 任务（T-1783349079753-99bc）：验证架构设计文档与代码现状一致。

测试内容：
- Schema 版本号 v25 与代码一致（db/schema.py SCHEMA_VERSION）
- v14-v25 共 12 个版本的变更记录都出现在文档中
- 25 个 Mixin 都在文档中列出
- 关键表分组章节齐全（v14/v16/v17/v19/v20/v21/v22/v23/v25）
- MCP 工具数与文档声称的数量一致
"""

import os
import re

import pytest

# 从 schema.py 读取当前版本号
from callwarden.db.schema import SCHEMA_VERSION


# architecture.md 路径
ARCH_DOC = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "docs", "architecture.md")
)


def _read_arch_doc():
    """读取 architecture.md 全文。"""
    with open(ARCH_DOC, encoding="utf-8") as f:
        return f.read()


def test_doc_schema_version_matches_code():
    """文档声称的 Schema 版本应与 schema.py 中 SCHEMA_VERSION 一致。"""
    text = _read_arch_doc()
    # 查找 "当前 Schema 版本：**vNN**"
    m = re.search(r"当前 Schema 版本[：:]\s*\*\*v(\d+)\*\*", text)
    assert m, "文档中未找到 '当前 Schema 版本' 声明"
    doc_ver = int(m.group(1))
    assert doc_ver == SCHEMA_VERSION, (
        f"文档 Schema 版本 v{doc_ver} 与代码 SCHEMA_VERSION={SCHEMA_VERSION} 不一致"
    )


def test_doc_covers_v14_to_v25_versions():
    """文档应覆盖 v14-v25 每个版本的变更说明。"""
    text = _read_arch_doc()
    expected_versions = list(range(14, 26))  # v14 .. v25
    missing = []
    for v in expected_versions:
        # 每个版本应出现 vN 前缀行（如 "v14 归档文件表..."）
        pattern = rf"^v{v}\s+\S"
        if not re.search(pattern, text, re.MULTILINE):
            missing.append(f"v{v}")
    assert not missing, f"文档缺少以下版本的变更说明: {', '.join(missing)}"


def test_doc_lists_25_mixins():
    """文档应列出 25 个 Mixin。"""
    text = _read_arch_doc()
    # 检查标题
    assert "25 个 Mixin" in text, "文档未声明 25 个 Mixin"
    # 计算表格中的 Mixin 行数（| N | Name | file | ... |）
    rows = re.findall(r"^\|\s*\d+\s*\|", text, re.MULTILINE)
    # 混合表里可能还有别的编号行，这里用 Mixin 表特定锚点
    # 找 "### 25 个 Mixin 列表" 之后的表格行
    mixin_section = text.split("### 25 个 Mixin 列表", 1)
    assert len(mixin_section) == 2, "未找到 '### 25 个 Mixin 列表' 章节"
    mixin_table = mixin_section[1].split("### ", 1)[0]
    mixin_rows = re.findall(r"^\|\s*\d+\s*\|", mixin_table, re.MULTILINE)
    assert len(mixin_rows) == 25, (
        f"Mixin 列表应有 25 行，实际 {len(mixin_rows)} 行"
    )


def test_doc_has_all_table_group_sections():
    """文档应包含所有 v14-v25 对应的表分组章节。"""
    text = _read_arch_doc()
    expected_sections = [
        "归档与 GC 表",          # v14 + v19 + v20
        "外部依赖表",            # v16 + v18
        "任务-符号变更归因表",   # v17
        "任务质量门禁表",        # v21
        "审计签名链表",          # v22
        "Agent Rule Memory 表", # v23
        "自举闭环表",            # v25
        "守护者架构表",          # v10
        "任务与编辑审计表",      # v7 + v12 + v15 + v24
    ]
    missing = [s for s in expected_sections if s not in text]
    assert not missing, f"文档缺少以下表分组章节: {', '.join(missing)}"


def test_doc_mcp_tool_count_consistent():
    """文档中 MCP 工具数声明应一致（166）。"""
    text = _read_arch_doc()
    # 架构图里声明 "166 个 @mcp.tool() 工具"
    assert "166" in text, "文档未声明 166 个 MCP 工具"


def test_doc_archived_files_documented():
    """v14 archived_files 表应有字段说明。"""
    text = _read_arch_doc()
    assert "archived_files" in text, "文档未提及 archived_files 表"
    assert "归档" in text, "文档未说明归档机制"


def test_doc_gc_tables_documented():
    """v19/v20 gc_policies/gc_runs 表应有说明。"""
    text = _read_arch_doc()
    assert "gc_policies" in text, "文档未提及 gc_policies 表"
    assert "gc_runs" in text, "文档未提及 gc_runs 表"


def test_doc_external_symbols_documented():
    """v16 external_symbols/package_versions 表应有说明。"""
    text = _read_arch_doc()
    assert "external_symbols" in text, "文档未提及 external_symbols 表"
    assert "package_versions" in text, "文档未提及 package_versions 表"


def test_doc_task_symbol_changes_documented():
    """v17 task_symbol_changes 表应有说明。"""
    text = _read_arch_doc()
    assert "task_symbol_changes" in text, "文档未提及 task_symbol_changes 表"


def test_doc_task_quality_findings_documented():
    """v21 task_quality_findings 表应有说明。"""
    text = _read_arch_doc()
    assert "task_quality_findings" in text, "文档未提及 task_quality_findings 表"


def test_doc_audit_chain_documented():
    """v22 audit_chain 表应有说明。"""
    text = _read_arch_doc()
    assert "audit_chain" in text, "文档未提及 audit_chain 表"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
