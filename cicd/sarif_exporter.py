"""sarif_exporter.py
=================

SARIF (Static Analysis Results Interchange Format) 报告导出器。

将 Semgrep / guardrail 扫描产出的 findings 转换为 SARIF 2.1.0 标准格式，
便于 GitHub Code Scanning、Azure DevOps 等 CI 平台消费与可视化。

参考规范：https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

# cicd 是 code_graph 的子包，需要回到上一级才能拿到 config
from ..config import atomic_write_file


# SARIF 工具元信息（驱动名称与版本）
_TOOL_NAME = "code_graph"
_TOOL_VERSION = "1.0"
_SARIF_VERSION = "2.1.0"


def _severity_to_level(severity: str) -> str:
    """将内部 severity 映射为 SARIF level

    SARIF level 合法值：error / warning / note / none

    支持的输入（大小写不敏感）：
    - ERROR / BLOCK  -> error
    - WARNING / WARN -> warning
    - INFO / NOTE    -> note
    - 其余           -> warning（SARIF 未指定时的默认值）
    """
    if not severity:
        return "warning"
    sev = str(severity).strip().upper()
    if sev in ("ERROR", "BLOCK"):
        return "error"
    if sev in ("WARNING", "WARN"):
        return "warning"
    if sev in ("INFO", "NOTE"):
        return "note"
    return "warning"


def _coalesce_str(value: Any, default: str = "") -> str:
    """安全取字符串，None / 非字符串返回默认值"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _coalesce_int(value: Any, default: int = 0) -> int:
    """安全取整数，None / 非数值返回默认值"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_file_path(finding: Dict) -> str:
    """从 finding 中提取文件路径（兼容多种字段名）"""
    for key in ("file_path", "filePath", "file", "path", "uri"):
        val = finding.get(key)
        if val:
            return _coalesce_str(val)
    return ""


def _extract_start_line(finding: Dict) -> int:
    """从 finding 中提取起始行号（兼容多种字段名）"""
    for key in ("start_line", "startLine", "line", "lineNumber"):
        if key in finding:
            return _coalesce_int(finding.get(key))
    return 0


def _extract_rule_id(finding: Dict) -> str:
    """从 finding 中提取规则 ID（兼容多种字段名）"""
    for key in ("rule_id", "ruleId", "rule", "id"):
        val = finding.get(key)
        if val:
            return _coalesce_str(val)
    return ""


def _extract_message(finding: Dict) -> str:
    """从 finding 中提取消息文本（兼容多种字段名）"""
    # message 可能直接是字符串，也可能是 {"text": "..."} 结构
    msg = finding.get("message")
    if isinstance(msg, dict):
        return _coalesce_str(msg.get("text"))
    return _coalesce_str(msg) or _coalesce_str(finding.get("description"))


class SarifExporter:
    """SARIF 2.1.0 报告导出器

    将 Semgrep / guardrail findings 列表转换为 SARIF 2.1.0 兼容的 JSON 结构。
    """

    def export_findings(self, findings: List[Dict]) -> Dict:
        """将 findings 列表导出为 SARIF 2.1.0 字典结构

        Args:
            findings: finding 字典列表，每个元素至少应包含 severity / message /
                      file_path / start_line / rule_id 中的若干字段

        Returns:
            SARIF 2.1.0 完整报告字典，可直接 json.dumps
        """
        results: List[Dict] = []

        for finding in findings or []:
            rule_id = _extract_rule_id(finding)
            level = _severity_to_level(finding.get("severity"))
            message_text = _extract_message(finding) or rule_id or "finding"
            file_path = _extract_file_path(finding)
            start_line = _extract_start_line(finding)

            # 构造 location（行号为 0 时不输出 region）
            physical_location: Dict[str, Any] = {
                "artifactLocation": {"uri": file_path}
            }
            if start_line > 0:
                physical_location["region"] = {"startLine": start_line}

            results.append({
                "ruleId": rule_id,
                "level": level,
                "message": {"text": message_text},
                "locations": [
                    {"physicalLocation": physical_location}
                ],
            })

        return {
            "version": _SARIF_VERSION,
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": _TOOL_NAME,
                            "version": _TOOL_VERSION,
                        }
                    },
                    "results": results,
                }
            ],
        }

    def export_to_file(self, findings: List[Dict], output_path: str) -> str:
        """导出 SARIF 报告到文件（原子写入）

        Args:
            findings: finding 字典列表
            output_path: 输出文件路径

        Returns:
            实际写入的文件路径
        """
        report = self.export_findings(findings)
        # 用 json.dumps 生成 JSON 字符串，再走原子写入
        content = json.dumps(report, ensure_ascii=False, indent=2)
        atomic_write_file(output_path, content, encoding="utf-8")
        return output_path
