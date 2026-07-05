"""
db_guardrail.py
===============

生产安全护栏 Mixin。

提供 DB/API/Incident 三类可阻断的安全规则扫描能力。
通过 Mixin 模式集成到 CodeGraphDB 主类。
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Dict, List, Optional, Tuple

from ..config import read_file_normalized
from ..i18n import t
from .schema import (
    GUARDRAIL_ACTION_BLOCK,
    GUARDRAIL_ACTION_REQUIRE_REVIEW,
    GUARDRAIL_ACTION_WARN,
    GUARDRAIL_CATEGORY_API_COMPAT,
    GUARDRAIL_CATEGORY_DB_SAFETY,
    GUARDRAIL_CATEGORY_INCIDENT,
    GUARDRAIL_SEVERITY_BLOCK,
    GUARDRAIL_SEVERITY_INFO,
    GUARDRAIL_SEVERITY_WARN,
    GUARDRAIL_STATUS_OPEN,
    GUARDRAIL_STATUS_RESOLVED,
    GUARDRAIL_STATUS_WONTFIX,
)


class GuardrailMixin:
    """生产安全护栏 Mixin

    通过 self.conn 访问数据库，提供三类安全规则扫描：
    - DB Safety: 数据库 schema 变更风险（ALTER/DROP TABLE、字段缩减等）
    - API Compatibility: API 兼容性破坏（可见性降低、参数删除等）
    - Incident Readiness: 事故响应准备度（错误处理、日志、回滚）

    内置 9 条规则（每类 3 条），支持自定义规则扩展。
    通过 Mixin 模式集成到 CodeGraphDB 主类，可访问 self.conn 和 self.active_workspace。
    """

    # ------------------------------------------------------------------
    # 内置规则初始化
    # ------------------------------------------------------------------

    def _init_builtin_rules(self) -> None:
        """初始化内置规则（9 条，每类 3 条，is_builtin=1）

        使用 INSERT OR IGNORE 保证幂等：多次调用不会重复插入。
        应在 scan_guardrails / guardrail_list_rules 等方法中懒加载调用。
        """
        now = time.time()
        rules = [
            # ---- DB Safety 类 ----
            {
                "rule_id": "GR-builtin-db-1",
                "category": GUARDRAIL_CATEGORY_DB_SAFETY,
                "severity": GUARDRAIL_SEVERITY_WARN,
                "pattern": r"\bALTER\s+TABLE\b",
                "action": GUARDRAIL_ACTION_WARN,
                "description": t("cli.messages.guardrail_rule_db_alter", default="Detect ALTER TABLE statements (schema change risk)"),
            },
            {
                "rule_id": "GR-builtin-db-2",
                "category": GUARDRAIL_CATEGORY_DB_SAFETY,
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "pattern": r"\bDROP\s+(TABLE|COLUMN)\b",
                "action": GUARDRAIL_ACTION_BLOCK,
                "description": t("cli.messages.guardrail_rule_db_drop", default="Detect DROP TABLE / DROP COLUMN statements (data loss risk)"),
            },
            {
                "rule_id": "GR-builtin-db-3",
                "category": GUARDRAIL_CATEGORY_DB_SAFETY,
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "pattern": r"VARCHAR\s*\(\s*(\d+)\s*\)\s*(?:→|->)\s*VARCHAR\s*\(\s*(\d+)\s*\)",
                "action": GUARDRAIL_ACTION_BLOCK,
                "description": t("cli.messages.guardrail_rule_db_varchar_shrink", default="Detect VARCHAR length shrinkage (data truncation risk)"),
            },
            # ---- API Compatibility 类 ----
            {
                "rule_id": "GR-builtin-api-1",
                "category": GUARDRAIL_CATEGORY_API_COMPAT,
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "pattern": r"#\s*BREAKING\s+CHANGE",
                "action": GUARDRAIL_ACTION_BLOCK,
                "description": t("cli.messages.guardrail_rule_api_breaking_change", default="Detect BREAKING CHANGE markers (including reduced pub fn visibility)"),
            },
            {
                "rule_id": "GR-builtin-api-2",
                "category": GUARDRAIL_CATEGORY_API_COMPAT,
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "pattern": r"//\s*REMOVED\s+PARAM",
                "action": GUARDRAIL_ACTION_BLOCK,
                "description": t("cli.messages.guardrail_rule_api_removed_param", default="Detect removed function parameters (caller compatibility break)"),
            },
            {
                "rule_id": "GR-builtin-api-3",
                "category": GUARDRAIL_CATEGORY_API_COMPAT,
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "pattern": r"//\s*REMOVED\s+FIELD",
                "action": GUARDRAIL_ACTION_BLOCK,
                "description": t("cli.messages.guardrail_rule_api_removed_field", default="Detect removed pub struct fields (struct compatibility break)"),
            },
            # ---- Incident Readiness 类 ----
            {
                "rule_id": "GR-builtin-inc-1",
                "category": GUARDRAIL_CATEGORY_INCIDENT,
                "severity": GUARDRAIL_SEVERITY_WARN,
                "pattern": r"fn\s+\w+.*\{[^}]*(?:try|catch|unwrap|expect|\?|Result)",
                "action": GUARDRAIL_ACTION_WARN,
                "description": t("cli.messages.guardrail_rule_inc_error_handling", default="Detect functions missing error handling (no try/catch/unwrap/expect/?/Result)"),
            },
            {
                "rule_id": "GR-builtin-inc-2",
                "category": GUARDRAIL_CATEGORY_INCIDENT,
                "severity": GUARDRAIL_SEVERITY_INFO,
                "pattern": r"fn\s+\w+.*\{[^}]*(?:log::|tracing::|println!|print!)",
                "action": GUARDRAIL_ACTION_WARN,
                "description": t("cli.messages.guardrail_rule_inc_logging", default="Detect functions missing logging (no log::/tracing::/println!/print!)"),
            },
            {
                "rule_id": "GR-builtin-inc-3",
                "category": GUARDRAIL_CATEGORY_INCIDENT,
                "severity": GUARDRAIL_SEVERITY_WARN,
                "pattern": r"(?:INSERT|UPDATE|DELETE|write|save|commit).*(?:rollback|transaction|begin|undo)",
                "action": GUARDRAIL_ACTION_WARN,
                "description": t("cli.messages.guardrail_rule_inc_transaction", default="Detect write operations without transaction/rollback logic"),
            },
        ]

        for r in rules:
            self.conn.execute(
                """INSERT OR IGNORE INTO guardrail_rules
                   (rule_id, category, severity, pattern, action, description, is_builtin, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    r["rule_id"], r["category"], r["severity"], r["pattern"],
                    r["action"], r["description"], now,
                ),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def scan_guardrails(self, file_filter: str = "") -> List[Dict]:
        """对指定文件运行规则扫描

        Args:
            file_filter: 文件路径前缀（如 "src/api/"），空字符串表示扫描所有文件

        Returns:
            findings 列表，每个元素包含 id / rule_id / file_path / severity /
            message / detected_at / status / symbol_hash
        """
        # 确保内置规则已初始化（幂等）
        self._init_builtin_rules()

        ws_id = self._get_active_workspace_id()
        normalized_filter = file_filter.replace("\\", "/").strip()

        # 查询符合条件的文件实例
        if normalized_filter:
            cur = self.conn.execute(
                """SELECT id, rel_path, abs_path, current_content_hash
                   FROM file_instances
                   WHERE workspace_id = ? AND rel_path LIKE ?
                   ORDER BY rel_path""",
                (ws_id, normalized_filter + "%"),
            )
        else:
            cur = self.conn.execute(
                """SELECT id, rel_path, abs_path, current_content_hash
                   FROM file_instances
                   WHERE workspace_id = ?
                   ORDER BY rel_path""",
                (ws_id,),
            )

        files = [dict(row) for row in cur]
        all_findings: List[Dict] = []

        for f in files:
            rel_path = f["rel_path"].replace("\\", "/")
            abs_path = f["abs_path"]

            # 从磁盘读取文件内容
            content = self._read_file_content(abs_path)
            if content is None:
                continue  # 文件不存在或无法读取，跳过

            # 运行三类检测器
            findings: List[Dict] = []
            findings.extend(self._detect_db_safety(content, rel_path))
            findings.extend(self._detect_api_compat(content, rel_path))
            findings.extend(self._detect_incident_readiness(content, rel_path))

            # 持久化 findings 到数据库（带去重）
            for finding in findings:
                inserted = self._append_finding(
                    rule_id=finding["rule_id"],
                    file_path=rel_path,
                    symbol_hash=finding.get("symbol_hash", ""),
                    severity=finding["severity"],
                    message=finding["message"],
                )
                if inserted:
                    all_findings.append(inserted)

        self.conn.commit()
        return all_findings

    def check_before_edit(self, file_path: str, proposed_change: str = "") -> Dict:
        """编辑前阻断式检查

        Args:
            file_path: 文件路径（相对路径或绝对路径）
            proposed_change: 拟议的修改内容（可选，为空则读取磁盘上的当前内容）

        Returns:
            {"decision": "block"/"warn"/"pass", "findings": [...], "message": "..."}
            若存在 block 级别 finding，decision 为 block
        """
        normalized_path = file_path.replace("\\", "/").strip()

        # 获取待检查的内容
        if proposed_change:
            content = proposed_change
        else:
            content = self._load_file_content_for_check(normalized_path)
            if content is None:
                return {
                    "decision": "pass",
                    "findings": [],
                    "message": t("cli.messages.guardrail_file_unreadable_skip", default="File does not exist or cannot be read; skipping checks"),
                }

        # 运行三类检测器（不持久化，仅返回结果）
        findings: List[Dict] = []
        findings.extend(self._detect_db_safety(content, normalized_path))
        findings.extend(self._detect_api_compat(content, normalized_path))
        findings.extend(self._detect_incident_readiness(content, normalized_path))

        # 为每个 finding 补充 file_path 字段（便于调用方定位）
        for f in findings:
            f["file_path"] = normalized_path

        # 判定决策：block > warn > pass
        block_count = sum(1 for f in findings if f["severity"] == GUARDRAIL_SEVERITY_BLOCK)
        warn_count = sum(1 for f in findings if f["severity"] == GUARDRAIL_SEVERITY_WARN)

        if block_count > 0:
            decision = "block"
            message = t("cli.messages.guardrail_decision_block", default="Detected {count} blocking issues; edit is not allowed", count=block_count)
        elif warn_count > 0:
            decision = "warn"
            message = t("cli.messages.guardrail_decision_warn", default="Detected {count} warning issues; review before editing", count=warn_count)
        else:
            decision = "pass"
            message = t("cli.messages.guardrail_decision_pass", default="No safety issues detected; edit is allowed")

        return {"decision": decision, "findings": findings, "message": message}

    def guardrail_add_rule(
        self,
        category: str,
        pattern: str,
        severity: str = "warn",
        action: str = "warn",
        description: str = "",
    ) -> str:
        """添加自定义规则

        Args:
            category: 规则类别（db_safety / api_compat / incident）
            pattern: 检测逻辑描述（正则或关键词）
            severity: 严重级别（block / warn / info）
            action: 动作（block / require_review / warn）
            description: 规则描述

        Returns:
            新建规则的 rule_id（格式：GR-custom-{timestamp}-{random4hex}）
        """
        rule_id = f"GR-custom-{int(time.time())}-{random.randint(0, 0xFFFF):04x}"
        now = time.time()
        self.conn.execute(
            """INSERT INTO guardrail_rules
               (rule_id, category, severity, pattern, action, description, is_builtin, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (rule_id, category, severity, pattern, action, description, now),
        )
        self.conn.commit()
        return rule_id

    def guardrail_list_rules(self, category_filter: str = "") -> List[Dict]:
        """列出规则

        Args:
            category_filter: 可选类别过滤（db_safety / api_compat / incident），空字符串列出全部

        Returns:
            规则列表，内置规则优先，按创建时间升序
        """
        # 确保内置规则已初始化（幂等）
        self._init_builtin_rules()

        if category_filter:
            cur = self.conn.execute(
                """SELECT * FROM guardrail_rules
                   WHERE category = ?
                   ORDER BY is_builtin DESC, created_at ASC""",
                (category_filter,),
            )
        else:
            cur = self.conn.execute(
                """SELECT * FROM guardrail_rules
                   ORDER BY is_builtin DESC, created_at ASC"""
            )
        return [dict(row) for row in cur]

    def resolve_finding(self, finding_id: int, resolution: str = "resolved") -> bool:
        """标记 finding 已处理

        Args:
            finding_id: finding 记录 ID
            resolution: 处理结果（resolved / wontfix），非这两值时默认 resolved

        Returns:
            是否成功更新（finding_id 不存在返回 False）
        """
        # 校验 resolution 值，非法值回退为 resolved
        if resolution not in (GUARDRAIL_STATUS_RESOLVED, GUARDRAIL_STATUS_WONTFIX):
            resolution = GUARDRAIL_STATUS_RESOLVED

        now = time.time()
        # 更新 finding 状态和 resolved_at（resolved_at 为处理时间，含 wontfix 情况）
        cur = self.conn.execute(
            """UPDATE guardrail_findings
               SET status = ?, resolved_at = ?
               WHERE id = ?""",
            (resolution, now, finding_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 三类检测器
    # ------------------------------------------------------------------

    def _detect_db_safety(self, content: str, file_path: str) -> List[Dict]:
        """DB Safety 检测器

        检测数据库 schema 变更风险：
        - ALTER TABLE 语句（warn）
        - DROP TABLE / DROP COLUMN 语句（block）
        - VARCHAR 字段长度缩减（block）
        - SQL 文件不在 migrations/ 目录下（warn）

        Args:
            content: 文件内容
            file_path: 文件路径（正斜杠格式）

        Returns:
            findings 列表
        """
        findings: List[Dict] = []

        # 检测 ALTER TABLE（warn）→ GR-builtin-db-1
        for m in re.finditer(r"\bALTER\s+TABLE\b", content, re.IGNORECASE):
            line = content.count("\n", 0, m.start()) + 1
            findings.append({
                "rule_id": "GR-builtin-db-1",
                "severity": GUARDRAIL_SEVERITY_WARN,
                "message": t("cli.messages.guardrail_finding_alter_table", default="Detected ALTER TABLE statement (line {line})", line=line),
                "symbol_hash": "",
            })

        # 检测 DROP TABLE / DROP COLUMN（block）→ GR-builtin-db-2
        for m in re.finditer(r"\bDROP\s+(TABLE|COLUMN)\b", content, re.IGNORECASE):
            line = content.count("\n", 0, m.start()) + 1
            findings.append({
                "rule_id": "GR-builtin-db-2",
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "message": t("cli.messages.guardrail_finding_drop", default="Detected {statement} statement (line {line})", statement=m.group(0).upper(), line=line),
                "symbol_hash": "",
            })

        # 检测 VARCHAR 长度缩减（block）→ GR-builtin-db-3
        # 模式：VARCHAR(n) → VARCHAR(m) 或 VARCHAR(n) -> VARCHAR(m)，其中 m < n
        varchar_re = re.compile(
            r"VARCHAR\s*\(\s*(\d+)\s*\)\s*(?:→|->)\s*VARCHAR\s*\(\s*(\d+)\s*\)",
            re.IGNORECASE,
        )
        for m in varchar_re.finditer(content):
            old_len = int(m.group(1))
            new_len = int(m.group(2))
            if new_len < old_len:
                line = content.count("\n", 0, m.start()) + 1
                findings.append({
                    "rule_id": "GR-builtin-db-3",
                    "severity": GUARDRAIL_SEVERITY_BLOCK,
                    "message": t("cli.messages.guardrail_finding_varchar_shrink", default="VARCHAR length shrank: {old_len} -> {new_len} (line {line})", old_len=old_len, new_len=new_len, line=line),
                    "symbol_hash": "",
                })

        # 检测迁移脚本缺失（warn）→ GR-builtin-db-1
        # SQL 文件不在 migrations/ 目录下
        if file_path.lower().endswith(".sql") and "migrations/" not in file_path:
            findings.append({
                "rule_id": "GR-builtin-db-1",
                "severity": GUARDRAIL_SEVERITY_WARN,
                "message": t("cli.messages.guardrail_sql_not_in_migrations", default="SQL file is not under migrations/ (migration script missing risk)"),
                "symbol_hash": "",
            })

        return findings

    def _detect_api_compat(self, content: str, file_path: str) -> List[Dict]:
        """API Compatibility 检测器

        检测 API 兼容性破坏（简化实现，基于注释标记）：
        - # BREAKING CHANGE 标记（block）→ 可见性降低等
        - // REMOVED PARAM 标记（block）→ 参数删除
        - // REMOVED FIELD 标记（block）→ pub struct 字段删除

        Args:
            content: 文件内容
            file_path: 文件路径（正斜杠格式）

        Returns:
            findings 列表
        """
        findings: List[Dict] = []

        # 检测 # BREAKING CHANGE 标记（block）→ GR-builtin-api-1
        for m in re.finditer(r"#\s*BREAKING\s+CHANGE", content, re.IGNORECASE):
            line = content.count("\n", 0, m.start()) + 1
            # 提取该行剩余内容作为上下文
            line_end = content.find("\n", m.start())
            if line_end == -1:
                line_end = len(content)
            context = content[m.start():line_end].strip()[:80]
            findings.append({
                "rule_id": "GR-builtin-api-1",
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "message": t("cli.messages.guardrail_finding_breaking_change", default="Detected BREAKING CHANGE marker: {context} (line {line})", context=context, line=line),
                "symbol_hash": "",
            })

        # 检测 // REMOVED PARAM 标记（block）→ GR-builtin-api-2
        for m in re.finditer(r"//\s*REMOVED\s+PARAM", content, re.IGNORECASE):
            line = content.count("\n", 0, m.start()) + 1
            findings.append({
                "rule_id": "GR-builtin-api-2",
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "message": t("cli.messages.guardrail_finding_removed_param", default="Detected parameter removal marker // REMOVED PARAM (line {line})", line=line),
                "symbol_hash": "",
            })

        # 检测 // REMOVED FIELD 标记（block）→ GR-builtin-api-3
        for m in re.finditer(r"//\s*REMOVED\s+FIELD", content, re.IGNORECASE):
            line = content.count("\n", 0, m.start()) + 1
            findings.append({
                "rule_id": "GR-builtin-api-3",
                "severity": GUARDRAIL_SEVERITY_BLOCK,
                "message": t("cli.messages.guardrail_finding_removed_field", default="Detected field removal marker // REMOVED FIELD (line {line})", line=line),
                "symbol_hash": "",
            })

        return findings

    def _detect_incident_readiness(self, content: str, file_path: str) -> List[Dict]:
        """Incident Readiness 检测器

        检测事故响应准备度：
        - 函数缺少错误处理：无 try/catch/unwrap/expect/?/Result（warn）
        - 函数缺少日志：无 log::/tracing::/println!/print!（info）
        - 函数有写操作但无事务/回滚逻辑（warn）

        Args:
            content: 文件内容
            file_path: 文件路径（正斜杠格式）

        Returns:
            findings 列表
        """
        findings: List[Dict] = []
        blocks = self._extract_function_blocks(content)

        # 如果没有提取到函数块，以整个文件为一个块进行检测
        if not blocks:
            blocks = [("<file>", content, 1, content.count("\n") + 1)]

        for name, body, start_line, end_line in blocks:
            # 跳过空函数体或极短函数（如单行 return）
            if len(body.strip()) < 10:
                continue

            location = t("cli.messages.guardrail_location_function", default="Function {name} (lines {start}-{end})", name=name, start=start_line, end=end_line)

            # 检测缺少错误处理（warn）→ GR-builtin-inc-1
            if not re.search(r"\b(?:try|catch|unwrap|expect)\b|\?|Result", body):
                findings.append({
                    "rule_id": "GR-builtin-inc-1",
                    "severity": GUARDRAIL_SEVERITY_WARN,
                    "message": t("cli.messages.guardrail_finding_missing_error_handling", default="{location} is missing error handling (no try/catch/unwrap/expect/?/Result)", location=location),
                    "symbol_hash": "",
                })

            # 检测缺少日志（info）→ GR-builtin-inc-2
            if not re.search(r"\blog::|tracing::|println!|print!|eprintln!|warn!|info!|error!|debug!", body):
                findings.append({
                    "rule_id": "GR-builtin-inc-2",
                    "severity": GUARDRAIL_SEVERITY_INFO,
                    "message": t("cli.messages.guardrail_finding_missing_logging", default="{location} is missing logging (no log::/tracing::/println!/print!)", location=location),
                    "symbol_hash": "",
                })

            # 检测有写操作但无事务/回滚逻辑（warn）→ GR-builtin-inc-3
            has_write = bool(
                re.search(r"\b(?:INSERT|UPDATE|DELETE|CREATE|DROP)\b", body, re.IGNORECASE)
                or re.search(r"\.(?:write|save|push|insert|update|delete)\s*\(", body)
            )
            has_safety = bool(
                re.search(r"\b(?:rollback|transaction|begin|undo|abort|commit)\b", body, re.IGNORECASE)
            )
            if has_write and not has_safety:
                findings.append({
                    "rule_id": "GR-builtin-inc-3",
                    "severity": GUARDRAIL_SEVERITY_WARN,
                    "message": t("cli.messages.guardrail_finding_missing_transaction", default="{location} has write operations without transaction/rollback logic", location=location),
                    "symbol_hash": "",
                })

        return findings

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file_content(abs_path: str) -> Optional[str]:
        """从磁盘读取文件内容，失败返回 None

        Args:
            abs_path: 文件绝对路径

        Returns:
            标准化后的文件内容，读取失败返回 None
        """
        try:
            content, _ = read_file_normalized(abs_path)
            return content
        except (OSError, IOError):
            return None

    def _load_file_content_for_check(self, file_path: str) -> Optional[str]:
        """为 check_before_edit 加载文件内容

        按以下优先级尝试读取：
        1. 从 file_instances 表查找 abs_path
        2. 作为绝对路径直接读取
        3. 相对于 workspace_root 拼接后读取

        Args:
            file_path: 文件路径（已标准化为正斜杠）

        Returns:
            文件内容，无法读取返回 None
        """
        # 策略1：从 file_instances 表查找 abs_path
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "SELECT abs_path FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, file_path),
        )
        row = cur.fetchone()
        if row:
            content = self._read_file_content(row["abs_path"])
            if content is not None:
                return content

        # 策略2：作为绝对路径直接读取
        content = self._read_file_content(file_path)
        if content is not None:
            return content

        # 策略3：相对于 workspace_root 拼接后读取
        abs_candidate = os.path.join(self.workspace_root, file_path)
        return self._read_file_content(abs_candidate)

    def _append_finding(
        self,
        rule_id: str,
        file_path: str,
        symbol_hash: str,
        severity: str,
        message: str,
    ) -> Optional[Dict]:
        """插入 finding 到数据库（不 commit，带去重）

        去重规则：若已存在相同的 (rule_id, file_path, message) 且 status=open 的记录，则跳过。

        Args:
            rule_id: 规则 ID
            file_path: 文件路径
            symbol_hash: 符号 hash（可为空）
            severity: 严重级别
            message: finding 描述

        Returns:
            插入的 finding dict（含 id 和 detected_at），重复时返回 None
        """
        now = time.time()

        # 去重：检查是否已存在相同的 open finding
        cur = self.conn.execute(
            """SELECT id FROM guardrail_findings
               WHERE rule_id = ? AND file_path = ? AND message = ? AND status = ?""",
            (rule_id, file_path, message, GUARDRAIL_STATUS_OPEN),
        )
        if cur.fetchone():
            return None

        cur = self.conn.execute(
            """INSERT INTO guardrail_findings
               (rule_id, file_path, symbol_hash, severity, status, message, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rule_id, file_path, symbol_hash, severity, GUARDRAIL_STATUS_OPEN, message, now),
        )
        return {
            "id": cur.lastrowid,
            "rule_id": rule_id,
            "file_path": file_path,
            "symbol_hash": symbol_hash,
            "severity": severity,
            "status": GUARDRAIL_STATUS_OPEN,
            "message": message,
            "detected_at": now,
        }

    @staticmethod
    def _extract_function_blocks(content: str) -> List[Tuple[str, str, int, int]]:
        """提取函数块（简化版）

        支持花括号语言（Rust fn / Go func / JS-TS function）通过花括号匹配提取函数体，
        以及 Python def 通过缩进启发式提取函数体。
        其他语言或不匹配时返回空列表（调用方回退到整文件扫描）。

        Returns:
            [(func_name, body, start_line, end_line), ...]
            行号从 1 开始计数
        """
        blocks: List[Tuple[str, str, int, int]] = []

        # 花括号语言：fn / func / function（含可选 pub/async/unsafe 前缀）
        brace_pattern = re.compile(
            r"\b(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|func|function)\s+(\w+)"
        )
        for m in brace_pattern.finditer(content):
            name = m.group(1)
            start_line = content.count("\n", 0, m.start()) + 1
            # 找到函数体开始的花括号
            brace_start = content.find("{", m.end())
            if brace_start == -1:
                continue
            # 花括号匹配（简化版，不处理字符串/注释内的花括号）
            depth = 1
            i = brace_start + 1
            while i < len(content) and depth > 0:
                ch = content[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth == 0:
                body = content[brace_start + 1:i - 1]
                end_line = content.count("\n", 0, i) + 1
                blocks.append((name, body, start_line, end_line))

        # Python: def name(...): （缩进启发式，函数体到下一个 def/class 或 EOF）
        for m in re.finditer(r"\bdef\s+(\w+)\s*\(", content):
            name = m.group(1)
            start_line = content.count("\n", 0, m.start()) + 1
            colon_pos = content.find(":", m.end())
            if colon_pos == -1:
                continue
            body_start = content.find("\n", colon_pos)
            if body_start == -1:
                continue
            body_start += 1
            # 函数体到下一个 def/class 或 EOF
            next_def = re.search(r"\n(?:def |class )", content[body_start:])
            if next_def:
                body = content[body_start:body_start + next_def.start()]
            else:
                body = content[body_start:]
            end_line = content.count("\n", 0, body_start + len(body)) + 1
            blocks.append((name, body, start_line, end_line))

        return blocks
