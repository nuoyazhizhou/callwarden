"""文档门禁：docs/cli_reference.md 首批命令章节四段式完整性。

背景（architecture.md 阶段 3「文档深度完善」）：命令参考章节长期只写用法与
散文式说明，缺少统一参数表/退出码/可复制示例，容易与实际 argparse 选项漂移。
本测试锁定首批 8 个前缀分类（task / build-context / workspace / query /
search / file / status / stats）的每个 ``### `...` `` 章节必须具备四段式：

1. 用法：``bash`` 代码块；
2. 参数：``**参数**`` 标题 + 紧随其后的参数表（``| 参数 | 必填 | 说明 |``）；
3. 退出码：``**退出码**`` 标题 + 含 0/1/2 三行的表格；
4. 示例：``**示例**`` 标题 + ``bash`` 代码块。

同时校验首批 44 个 canonical 命令一个不少、四段各只出现一次（防重复补段）。
其余分类由后续任务卡扩展 ``EXPECTED_COMMANDS`` 与 ``FIRST_BATCH_PREFIXES``。
"""

import os
import re
import sys


# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

CLI_REF = os.path.join(_PKG_PARENT, "docs", "cli_reference.md")

# 首批覆盖的命令前缀（章节 ``### `task create`：...`` 的首个 token）
FIRST_BATCH_PREFIXES = frozenset({
    "task",
    "build-context",
    "workspace",
    "query",
    "search",
    "file",
    "status",
    "stats",
})

# 首批 canonical 命令（去掉 ``<METAVAR>`` 后的章节标题；44 个）
EXPECTED_COMMANDS = frozenset({
    # task（26）
    "task create",
    "task next",
    "task next-action",
    "task prompt",
    "task report",
    "task rollback",
    "task apply",
    "task close",
    "task reopen",
    "task findings",
    "task resolve-finding",
    "task completion-review",
    "task split",
    "task status-tree",
    "task list",
    "task show",
    "task capture-diff",
    "task assignment-status",
    "task assignment-heartbeat",
    "task handoff",
    "task step-resolve",
    "task cascade-close",
    "task claim-recover",
    "task supersede",
    "task governance-projection",
    "task superseded",
    # build-context（8）
    "build-context register",
    "build-context list",
    "build-context show",
    "build-context activate",
    "build-context delete",
    "build-context import-compile-commands",
    "build-context resolve",
    "build-context edges",
    # workspace（5）
    "workspace list",
    "workspace register",
    "workspace set",
    "workspace delete",
    "workspace generate-ignore",
    # 单 token 命令（5）
    "search",
    "file",
    "query",
    "status",
    "stats",
})

_HEADING_RE = re.compile(r"^### `([^`]+)`")
_METAVAR_RE = re.compile(r"<[^>]+>")
_EXIT_CODE_RE = re.compile(r"^\d+$")


def _canonical(key):
    """``search <QUERY>`` → ``search``；``build-context register <ID> <NAME>`` → ``build-context register``"""
    stripped = _METAVAR_RE.sub("", key)
    return re.sub(r"\s+", " ", stripped).strip()


def _read_doc():
    with open(CLI_REF, encoding="utf-8") as f:
        return f.read()


def _iter_sections(text):
    """按 ``### `` 标题切分章节，fence 内的伪标题与 ``## `` 大类边界不计入。

    返回 ``[(canonical_key, segment_lines), ...]``。
    """
    lines = text.splitlines()
    starts = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("## ") and not line.startswith("###"):
            starts.append((i, None))
            continue
        match = _HEADING_RE.match(line)
        if match:
            starts.append((i, match.group(1)))
    sections = []
    for idx, (start, key) in enumerate(starts):
        if key is None:
            continue
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        sections.append((_canonical(key), lines[start:end]))
    return sections


def _find_line(segment, marker):
    for idx, line in enumerate(segment):
        if line.strip() == marker:
            return idx
    return -1


def _count_marker(segment, marker):
    return sum(1 for line in segment if line.strip() == marker)


def _table_after(segment, start):
    """在 ``start`` 之后跳过空行取第一个 markdown 表；非表返回 ``(None, [])``。"""
    idx = start
    while idx < len(segment) and not segment[idx].strip():
        idx += 1
    if idx >= len(segment) or not segment[idx].lstrip().startswith("|"):
        return None, []
    header = segment[idx]
    sep_idx = idx + 1
    if sep_idx >= len(segment):
        return None, []
    separator = segment[sep_idx].lstrip()
    if not separator.startswith("|") or "-" not in separator:
        return None, []
    rows = []
    row_idx = sep_idx + 1
    while row_idx < len(segment) and segment[row_idx].lstrip().startswith("|"):
        rows.append(segment[row_idx])
        row_idx += 1
    return header, rows


def _cells(row):
    """按未转义的 ``|`` 切分表格行（``\\|`` 视为转义，不切分），去掉首尾空单元格。"""
    parts = re.split(r"(?<!\\)\|", row.strip())
    return [cell.strip() for cell in parts[1:-1]]


def _first_fence_before(segment, end_idx):
    for line in segment[:end_idx]:
        if line.lstrip().startswith("```bash"):
            return True
    return False


def _fence_after(segment, start):
    idx = start
    while idx < len(segment) and not segment[idx].strip():
        idx += 1
    if idx >= len(segment) or not segment[idx].lstrip().startswith("```bash"):
        return None
    body = []
    idx += 1
    while idx < len(segment) and not segment[idx].lstrip().startswith("```"):
        body.append(segment[idx])
        idx += 1
    return [line for line in body if line.strip()]


# ============================================
# docs/cli_reference.md 首批章节四段式门禁
# ============================================


class TestCliReferenceSections:
    """首批命令章节必须统一四段式，且不重复补段。"""

    def test_file_exists(self):
        assert os.path.isfile(CLI_REF), f"缺少文件: {CLI_REF}"

    def test_all_first_batch_commands_have_sections(self):
        """首批 44 个 canonical 命令均有章节（章节不丢失）。"""
        text = _read_doc()
        present = {key for key, _ in _iter_sections(text)}
        missing = sorted(EXPECTED_COMMANDS - present)
        assert not missing, f"cli_reference.md 缺少首批命令章节: {missing}"

    def test_every_section_has_usage_fence(self):
        """每个首批章节都有 bash 用法块（位于 ``**参数**`` 之前）。"""
        text = _read_doc()
        bad = []
        for key, segment in _iter_sections(text):
            if key.split()[0] not in FIRST_BATCH_PREFIXES:
                continue
            param_idx = _find_line(segment, "**参数**")
            if param_idx < 0 or not _first_fence_before(segment, param_idx):
                bad.append(key)
        assert not bad, f"缺少 bash 用法块: {sorted(bad)}"

    def test_every_section_has_param_table(self):
        """``**参数**`` 标题后必须紧跟 ``| 参数 | 必填 | 说明 |`` 表格。"""
        text = _read_doc()
        bad = []
        for key, segment in _iter_sections(text):
            if key.split()[0] not in FIRST_BATCH_PREFIXES:
                continue
            param_idx = _find_line(segment, "**参数**")
            if param_idx < 0 or _count_marker(segment, "**参数**") != 1:
                bad.append(key)
                continue
            header, rows = _table_after(segment, param_idx + 1)
            if header is None or not rows:
                bad.append(key)
                continue
            if not all(col in header for col in ("参数", "必填", "说明")):
                bad.append(key)
                continue
            for row in rows:
                cells = _cells(row)
                if len(cells) != 3 or not cells[0] or not cells[2]:
                    bad.append(key)
                    break
                # 无参命令允许 `| — | — | 无参数 |` 占位行
                if cells[1] in ("是", "否"):
                    continue
                if cells[0] == "—" and cells[1] == "—" and cells[2] == "无参数":
                    continue
                bad.append(key)
                break
        assert not bad, f"参数表缺失或格式不合法: {sorted(bad)}"

    def test_every_section_has_exit_code_table(self):
        """``**退出码**`` 表格必须覆盖 0 / 1 / 2 且含义非空。"""
        text = _read_doc()
        bad = []
        for key, segment in _iter_sections(text):
            if key.split()[0] not in FIRST_BATCH_PREFIXES:
                continue
            exit_idx = _find_line(segment, "**退出码**")
            if exit_idx < 0 or _count_marker(segment, "**退出码**") != 1:
                bad.append(key)
                continue
            header, rows = _table_after(segment, exit_idx + 1)
            if header is None or not rows:
                bad.append(key)
                continue
            codes = {}
            for row in rows:
                cells = _cells(row)
                if len(cells) >= 2 and _EXIT_CODE_RE.match(cells[0]):
                    codes[int(cells[0])] = cells[1]
            if not {0, 1, 2}.issubset(codes) or any(not codes[code] for code in (0, 1, 2)):
                bad.append(key)
        assert not bad, f"退出码表缺失或不完整: {sorted(bad)}"

    def test_every_section_has_example_fence(self):
        """``**示例**`` 后必须有非空 bash 示例块。"""
        text = _read_doc()
        bad = []
        for key, segment in _iter_sections(text):
            if key.split()[0] not in FIRST_BATCH_PREFIXES:
                continue
            example_idx = _find_line(segment, "**示例**")
            if example_idx < 0 or _count_marker(segment, "**示例**") != 1:
                bad.append(key)
                continue
            body = _fence_after(segment, example_idx + 1)
            if not body:
                bad.append(key)
        assert not bad, f"示例块缺失或为空: {sorted(bad)}"

    def test_section_order_param_exit_example(self):
        """四段式顺序固定：用法块 → 参数 → 退出码 → 示例。"""
        text = _read_doc()
        bad = []
        for key, segment in _iter_sections(text):
            if key.split()[0] not in FIRST_BATCH_PREFIXES:
                continue
            param_idx = _find_line(segment, "**参数**")
            exit_idx = _find_line(segment, "**退出码**")
            example_idx = _find_line(segment, "**示例**")
            if not (0 <= param_idx < exit_idx < example_idx):
                bad.append(key)
        assert not bad, f"四段式顺序不符: {sorted(bad)}"

    def test_no_unknown_first_batch_commands(self):
        """首批前缀下不得出现未登记命令章节（新增章节须同步 EXPECTED_COMMANDS）。"""
        text = _read_doc()
        present = {key for key, _ in _iter_sections(text) if key.split()[0] in FIRST_BATCH_PREFIXES}
        unexpected = sorted(present - EXPECTED_COMMANDS)
        assert not unexpected, (
            "cli_reference.md 出现未登记的首批命令章节，请补齐四段式后"
            f"同步 EXPECTED_COMMANDS: {unexpected}"
        )
