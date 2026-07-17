"""
db_defect_kb.py
===============

缺陷知识库 Mixin。

提供 build_defect_knowledge / defect_pattern_search / suggest_fix /
learn_defect_from_fix / defect_stats 等方法，从历史 Semgrep 扫描和
git 修复中构建缺陷知识库。通过 Mixin 模式集成到 CodeGraphDB 主类。
"""

from __future__ import annotations

import difflib
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from ..config import compute_content_hash


# Semgrep 常见类别关键词（用于从 rule_id 推断类别）
_CATEGORY_KEYWORDS = {
    "security", "correctness", "best-practice", "best-practices",
    "performance", "maintainability", "portability", "accessibility",
}


def _extract_category(rule_id: str) -> str:
    """从 rule_id 推断缺陷类别

    解析策略：
    1. 优先匹配已知类别关键词（security / correctness / performance 等）
    2. 否则取第 3 段（如 "python.lang.security" → "security"）
    3. 退化取最后一段

    Args:
        rule_id: Semgrep 规则 ID（如 "python.lang.security.audit.crypto"）

    Returns:
        类别名（小写，如 "security"）
    """
    if not rule_id:
        return "general"
    # 按点/斜杠分隔，兼容不同命名风格
    parts = re.split(r"[./]", rule_id)
    for part in parts:
        if part.lower() in _CATEGORY_KEYWORDS:
            return part.lower()
    if len(parts) >= 3:
        return parts[2].lower()
    return parts[-1].lower() if parts else "general"


def _normalize_severity(sev: str) -> str:
    """标准化严重度为小写形式

    Semgrep 原始值通常为 ERROR / WARNING / INFO，统一转为小写。
    """
    if not sev:
        return "info"
    return sev.lower().strip()


def _compute_diff(old: str, new: str) -> str:
    """计算 unified diff 格式的简单文本差异

    Args:
        old: 修改前内容
        new: 修改后内容

    Returns:
        unified diff 文本（可能为空字符串）
    """
    if not old and not new:
        return ""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile="before", tofile="after")
    return "".join(diff)


def _gen_custom_pattern_id() -> str:
    """生成自定义模式 ID（用于非 Semgrep 来源的模式）

    格式: DP-custom-{timestamp}-{random8hex}

    后缀 8 位 hex（32 bit，~42 亿种）而非 4 位 hex：
    4 位 hex 在秒内连续生成 100 个 ID 时按生日悖论有 ~7.3% 碰撞概率；
    8 位 hex 将此概率降到 ~10⁻⁶，足以支撑快速循环调用。
    """
    ts = int(time.time())
    rand8 = secrets.token_hex(4)  # 4 字节 = 8 个十六进制字符
    return f"DP-custom-{ts}-{rand8}"


def _snippet_in_content(snippet: str, content: str) -> bool:
    """判断 snippet 是否出现在 content 中（忽略首尾空白）

    Args:
        snippet: Semgrep finding 的代码片段
        content: 符号内容

    Returns:
        是否包含
    """
    if not snippet or not content:
        return False
    # 去除首尾空白后做子串匹配，提升鲁棒性
    return snippet.strip() in content


class DefectKbMixin:
    """缺陷知识库 Mixin

    从历史 Semgrep 扫描和 git 修复中构建缺陷知识库。
    通过 self.conn 访问数据库连接，提供缺陷模式挖掘、搜索、修复推荐、
    从修复中学习及统计能力。
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _ensure_pattern(
        self,
        pattern_id: str,
        category: str,
        description: str,
        detection_rule: str,
        severity: str,
        learned_from: str = "semgrep",
    ) -> bool:
        """确保 defect_patterns 记录存在（不存在则创建）

        Args:
            pattern_id: 模式 ID
            category: 类别
            description: 描述
            detection_rule: 检测规则（rule_id）
            severity: 严重度
            learned_from: 来源标识

        Returns:
            是否为新创建（已存在返回 False）
        """
        cur = self.conn.execute(
            "SELECT pattern_id FROM defect_patterns WHERE pattern_id = ?",
            (pattern_id,),
        )
        if cur.fetchone():
            return False
        self.conn.execute(
            """
            INSERT OR IGNORE INTO defect_patterns
                (pattern_id, category, description, detection_rule, fix_template,
                 severity, learned_from, case_count, created_at)
            VALUES (?, ?, ?, ?, '', ?, ?, 0, ?)
            """,
            (pattern_id, category, description, detection_rule, severity, learned_from, time.time()),
        )
        return True

    def _increment_pattern_case_count(self, pattern_id: str, delta: int = 1):
        """递增模式的 case_count"""
        self.conn.execute(
            "UPDATE defect_patterns SET case_count = case_count + ? WHERE pattern_id = ?",
            (delta, pattern_id),
        )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def build_defect_knowledge(self) -> Dict[str, Any]:
        """从历史 Semgrep 扫描结果中挖掘缺陷模式

        流程：
        1. 查询 semgrep_findings 表，按 rule_id 分组
        2. 对每组提取 category / description / detection_rule / severity / case_count
        3. 为每个独特 rule_id 创建 defect_patterns 记录（pattern_id = "DP-{rule_id}"）
        4. 从 git_symbol_changes 中查找修复提交（change_type="modified" 且后续版本不再有该 finding），
           创建 defect_fixes 记录

        Returns:
            {"patterns_built": N, "fixes_learned": M, "categories": {...}}
        """
        now = time.time()
        patterns_built = 0
        fixes_learned = 0
        categories: Dict[str, int] = {}

        # ---- 1. 按 rule_id 分组挖掘模式 ----
        cur = self.conn.execute(
            """
            SELECT
                rule_id,
                MAX(rule_name) as rule_name,
                MAX(message) as message,
                MAX(severity) as severity,
                COUNT(*) as cnt
            FROM semgrep_findings
            WHERE rule_id IS NOT NULL AND rule_id != ''
            GROUP BY rule_id
            """,
        )
        rule_rows = cur.fetchall()

        for row in rule_rows:
            rule_id = row["rule_id"]
            message = row["message"] or ""
            severity = _normalize_severity(row["severity"] or "info")
            case_count = row["cnt"]
            category = _extract_category(rule_id)
            description = message.strip() if message else (row["rule_name"] or rule_id)
            # 描述过长则截断，避免存储爆炸
            if len(description) > 500:
                description = description[:500] + "..."
            pattern_id = f"DP-{rule_id}"

            # 统计类别分布
            categories[category] = categories.get(category, 0) + 1

            # 插入或更新模式记录
            existing = self.conn.execute(
                "SELECT pattern_id, case_count FROM defect_patterns WHERE pattern_id = ?",
                (pattern_id,),
            ).fetchone()
            if existing:
                # 已存在：累加 case_count，必要时更新描述/严重度
                self.conn.execute(
                    """
                    UPDATE defect_patterns
                    SET case_count = case_count + ?,
                        description = CASE WHEN description = '' THEN ? ELSE description END,
                        severity = ?
                    WHERE pattern_id = ?
                    """,
                    (case_count, description, severity, pattern_id),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO defect_patterns
                        (pattern_id, category, description, detection_rule, fix_template,
                         severity, learned_from, case_count, created_at)
                    VALUES (?, ?, ?, ?, '', ?, 'semgrep', ?, ?)
                    """,
                    (pattern_id, category, description, rule_id, severity, case_count, now),
                )
                patterns_built += 1

        # ---- 2. 从 git_symbol_changes 挖掘修复案例 ----
        # 查找 change_type="modified" 的符号变更，判断是否修复了已知缺陷
        change_cur = self.conn.execute(
            """
            SELECT gsc.symbol_hash, gsc.commit_hash, gsc.old_content, gsc.new_content
            FROM git_symbol_changes gsc
            WHERE gsc.change_type = 'modified'
              AND gsc.old_content IS NOT NULL
              AND gsc.new_content IS NOT NULL
            """,
        )
        changes = change_cur.fetchall()

        # 批量优化：预查询避免 N+1
        # 1. 一次性查所有 symbol_hash → qualified_name 映射
        symbol_hashes = [ch["symbol_hash"] for ch in changes if ch["symbol_hash"]]
        qname_map: Dict[str, str] = {}  # symbol_hash -> qualified_name
        batch_size = 500
        for i in range(0, len(symbol_hashes), batch_size):
            chunk = symbol_hashes[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"SELECT content_hash, qualified_name FROM symbol_contents WHERE content_hash IN ({placeholders})",
                chunk,
            )
            for r in cur.fetchall():
                qname_map[r["content_hash"]] = r["qualified_name"]

        # 2. 一次性查所有相关 semgrep_findings（按 qualified_name 或 content_hash）
        all_qnames = [q for q in qname_map.values() if q]
        findings_by_qname: Dict[str, List] = {}
        findings_by_hash: Dict[str, List] = {}
        for i in range(0, len(all_qnames), batch_size):
            chunk = all_qnames[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"""SELECT id, rule_id, snippet, fix, content_hash, symbol_qualified
                    FROM semgrep_findings WHERE symbol_qualified IN ({placeholders})""",
                chunk,
            )
            for f in cur.fetchall():
                findings_by_qname.setdefault(f["symbol_qualified"], []).append(f)

        # 对没有 qualified_name 的 symbol_hash 也批量查
        hash_only = [h for h in symbol_hashes if h not in qname_map or not qname_map.get(h)]
        for i in range(0, len(hash_only), batch_size):
            chunk = hash_only[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"""SELECT id, rule_id, snippet, fix, content_hash
                    FROM semgrep_findings WHERE content_hash IN ({placeholders})""",
                chunk,
            )
            for f in cur.fetchall():
                findings_by_hash.setdefault(f["content_hash"], []).append(f)

        # 3. 收集所有需要插入的 defect_fixes 行（批量查重后再批量 INSERT）
        pending_fixes = []  # [(pattern_id, symbol_hash, before_hash, after_hash, fix_diff, ...), ...]
        dup_keys = set()  # (pattern_id, symbol_hash, before_hash, after_hash)
        existing_dups: List[tuple] = []
        # 先收集所有候选 dup key
        for ch in changes:
            symbol_hash = ch["symbol_hash"]
            old_content = ch["old_content"] or ""
            new_content = ch["new_content"] or ""
            qualified_name = qname_map.get(symbol_hash, "")
            findings = findings_by_qname.get(qualified_name, []) if qualified_name else findings_by_hash.get(symbol_hash, [])
            for f in findings:
                snippet = f["snippet"] or ""
                in_old = _snippet_in_content(snippet, old_content) if snippet else True
                in_new = _snippet_in_content(snippet, new_content) if snippet else False
                if not in_old or in_new:
                    continue
                rule_id = f["rule_id"]
                pattern_id = f"DP-{rule_id}"
                self._ensure_pattern(
                    pattern_id,
                    _extract_category(rule_id),
                    f["fix"] or snippet or rule_id,
                    rule_id,
                    "info",
                    learned_from="git_fix",
                )
                before_hash = compute_content_hash(old_content)
                after_hash = compute_content_hash(new_content)
                dup_key = (pattern_id, symbol_hash, before_hash, after_hash)
                if dup_key in dup_keys:
                    continue
                dup_keys.add(dup_key)
                existing_dups.append(dup_key)
                fix_diff = _compute_diff(old_content, new_content)
                pending_fixes.append((pattern_id, symbol_hash, before_hash, after_hash, fix_diff, 0.8, now))

        # 4. 一次性查所有 dup（IN 子句按 pattern_id+symbol_hash 批量查）
        if existing_dups:
            # SQLite 不支持 IN 多列，分批查（每批 500 个）
            actual_dups = set()
            for i in range(0, len(existing_dups), batch_size):
                chunk = existing_dups[i:i + batch_size]
                # 用 OR 连接多列查询
                or_clauses = " OR ".join(
                    "(pattern_id = ? AND symbol_hash = ? AND before_hash = ? AND after_hash = ?)"
                    for _ in chunk
                )
                params = []
                for k in chunk:
                    params.extend(k)
                cur = self.conn.execute(
                    f"SELECT pattern_id, symbol_hash, before_hash, after_hash FROM defect_fixes WHERE {or_clauses}",
                    params,
                )
                for r in cur.fetchall():
                    actual_dups.add((r["pattern_id"], r["symbol_hash"], r["before_hash"], r["after_hash"]))

            # 过滤已存在的
            new_fixes = []
            for fix_row in pending_fixes:
                key = (fix_row[0], fix_row[1], fix_row[2], fix_row[3])
                if key not in actual_dups:
                    new_fixes.append(fix_row)

            pending_fixes = new_fixes

        # 5. 批量 INSERT 新的 defect_fixes
        if pending_fixes:
            self.conn.executemany(
                """
                INSERT INTO defect_fixes
                    (pattern_id, symbol_hash, before_hash, after_hash, fix_diff,
                     effectiveness, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                pending_fixes,
            )
            fixes_learned = len(pending_fixes)

        self.conn.commit()

        return {
            "patterns_built": patterns_built,
            "fixes_learned": fixes_learned,
            "categories": categories,
        }

    def defect_pattern_search(
        self,
        category: str = "",
        severity_filter: str = "",
    ) -> List[Dict[str, Any]]:
        """按类别/严重度搜索缺陷模式

        Args:
            category: 类别过滤（支持前缀匹配，如 "sec" 匹配 "security"）
            severity_filter: 严重度过滤（精确匹配，如 "error" / "warning" / "info"）

        Returns:
            模式列表，每个包含 detection_rule / fix_template / case_count 等字段
        """
        sql = "SELECT * FROM defect_patterns WHERE 1=1"
        params: List[Any] = []

        if category:
            # 前缀匹配
            sql += " AND category LIKE ?"
            params.append(category + "%")

        if severity_filter:
            sql += " AND severity = ?"
            params.append(_normalize_severity(severity_filter))

        sql += " ORDER BY case_count DESC, created_at DESC"

        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def suggest_fix(self, symbol_hash: str, finding_id: int = 0) -> Dict[str, Any]:
        """基于缺陷知识库推荐修复方案

        Args:
            symbol_hash: 符号内容哈希
            finding_id: 可选，具体的 semgrep finding ID

        Returns:
            {"pattern_id": ..., "fix_template": ..., "similar_fixes": [...],
             "effectiveness_score": ...}
            - 如果 semgrep_findings 中有 fix 字段，fix_template 直接返回该 fix
            - 否则返回 defect_patterns.fix_template
        """
        rule_id = ""
        finding_fix = ""
        snippet = ""

        # ---- 获取具体的 finding 信息 ----
        if finding_id > 0:
            f_row = self.conn.execute(
                """
                SELECT rule_id, snippet, fix, symbol_qualified
                FROM semgrep_findings WHERE id = ?
                """,
                (finding_id,),
            ).fetchone()
            if f_row:
                rule_id = f_row["rule_id"] or ""
                finding_fix = f_row["fix"] or ""
                snippet = f_row["snippet"] or ""
        else:
            # 通过 symbol_hash 查找相关 finding
            sym_row = self.conn.execute(
                "SELECT qualified_name FROM symbol_contents WHERE content_hash = ?",
                (symbol_hash,),
            ).fetchone()
            qualified_name = sym_row["qualified_name"] if sym_row else ""
            if qualified_name:
                f_row = self.conn.execute(
                    """
                    SELECT rule_id, snippet, fix
                    FROM semgrep_findings
                    WHERE symbol_qualified = ?
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (qualified_name,),
                ).fetchone()
                if f_row:
                    rule_id = f_row["rule_id"] or ""
                    finding_fix = f_row["fix"] or ""
                    snippet = f_row["snippet"] or ""
            else:
                # 退化：直接按 content_hash 匹配 semgrep_findings
                f_row = self.conn.execute(
                    """
                    SELECT rule_id, snippet, fix
                    FROM semgrep_findings
                    WHERE content_hash = ?
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (symbol_hash,),
                ).fetchone()
                if f_row:
                    rule_id = f_row["rule_id"] or ""
                    finding_fix = f_row["fix"] or ""
                    snippet = f_row["snippet"] or ""

        # ---- 通过 rule_id 匹配 defect_patterns ----
        pattern_id = ""
        pattern_fix_template = ""
        if rule_id:
            pattern_id = f"DP-{rule_id}"
            p_row = self.conn.execute(
                "SELECT pattern_id, fix_template, case_count FROM defect_patterns WHERE pattern_id = ?",
                (pattern_id,),
            ).fetchone()
            if p_row:
                pattern_fix_template = p_row["fix_template"] or ""
            else:
                # 模式不存在则置空 pattern_id
                pattern_id = ""

        # ---- 从 defect_fixes 查找类似修复案例 ----
        similar_fixes: List[Dict[str, Any]] = []
        if pattern_id:
            fx_cur = self.conn.execute(
                """
                SELECT pattern_id, symbol_hash, before_hash, after_hash, fix_diff, effectiveness
                FROM defect_fixes
                WHERE pattern_id = ?
                ORDER BY effectiveness DESC, created_at DESC
                LIMIT 5
                """,
                (pattern_id,),
            )
            similar_fixes = [dict(r) for r in fx_cur.fetchall()]

        # ---- 计算有效性分数 ----
        if similar_fixes:
            effectiveness_score = sum(
                f.get("effectiveness", 0.0) for f in similar_fixes
            ) / len(similar_fixes)
        elif pattern_id:
            # 无修复案例时，基于 case_count 给出保守分数
            p_count_row = self.conn.execute(
                "SELECT case_count FROM defect_patterns WHERE pattern_id = ?",
                (pattern_id,),
            ).fetchone()
            case_count = p_count_row["case_count"] if p_count_row else 0
            effectiveness_score = min(0.5, case_count * 0.05)
        else:
            effectiveness_score = 0.0

        # ---- 确定最终推荐的 fix ----
        # 优先使用 semgrep_findings.fix，其次 defect_patterns.fix_template
        recommended_fix = finding_fix if finding_fix else pattern_fix_template

        return {
            "pattern_id": pattern_id,
            "fix_template": recommended_fix,
            "similar_fixes": similar_fixes,
            "effectiveness_score": round(effectiveness_score, 4),
        }

    def learn_defect_from_fix(self, fix_commit_hash: str) -> Dict[str, Any]:
        """从修复 commit 中学习缺陷模式

        流程：
        1. 查询 git_symbol_changes 表，找到该 commit 的所有符号变更
        2. 对每个变更（change_type="modified"）：old_content 是缺陷版本，new_content 是修复版本
        3. 提取 diff（简单文本差异）
        4. 查询该 commit 之前是否有 semgrep_findings（通过 content_hash 关联），关联到 defect_pattern
        5. 创建 defect_fixes 记录

        Args:
            fix_commit_hash: 修复提交的 commit hash

        Returns:
            {"learned_patterns": N, "learned_fixes": M, "details": [...]}
        """
        now = time.time()
        learned_patterns = 0
        learned_fixes = 0
        details: List[Dict[str, Any]] = []

        # 查询该 commit 的所有 modified 符号变更
        change_cur = self.conn.execute(
            """
            SELECT symbol_hash, old_content, new_content
            FROM git_symbol_changes
            WHERE commit_hash = ? AND change_type = 'modified'
            """,
            (fix_commit_hash,),
        )
        changes = change_cur.fetchall()

        for ch in changes:
            symbol_hash = ch["symbol_hash"]
            old_content = ch["old_content"] or ""
            new_content = ch["new_content"] or ""

            if old_content == new_content:
                continue  # 无实质变更，跳过

            # 计算前后内容 hash
            before_hash = compute_content_hash(old_content)
            after_hash = compute_content_hash(new_content)

            # 提取 diff
            fix_diff = _compute_diff(old_content, new_content)

            # 查找关联的 semgrep_findings
            # 策略：先通过 symbol_hash → qualified_name → symbol_qualified 匹配；
            #       再退化通过 content_hash 直接匹配
            sym_row = self.conn.execute(
                "SELECT qualified_name FROM symbol_contents WHERE content_hash = ?",
                (symbol_hash,),
            ).fetchone()
            qualified_name = sym_row["qualified_name"] if sym_row else ""

            related_rule_id = ""
            if qualified_name:
                f_row = self.conn.execute(
                    """
                    SELECT rule_id, snippet FROM semgrep_findings
                    WHERE symbol_qualified = ?
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (qualified_name,),
                ).fetchone()
                if f_row:
                    # 校验 snippet 确实出现在旧版本（确认缺陷存在）
                    snippet = f_row["snippet"] or ""
                    if not snippet or _snippet_in_content(snippet, old_content):
                        related_rule_id = f_row["rule_id"] or ""

            if not related_rule_id:
                # 退化：通过 content_hash 直接匹配
                f_row = self.conn.execute(
                    """
                    SELECT rule_id, snippet FROM semgrep_findings
                    WHERE content_hash = ?
                    ORDER BY scanned_at DESC LIMIT 1
                    """,
                    (before_hash,),
                ).fetchone()
                if f_row:
                    related_rule_id = f_row["rule_id"] or ""

            # 确定 pattern_id
            pattern_id: Optional[str] = None
            if related_rule_id:
                pattern_id = f"DP-{related_rule_id}"
                category = _extract_category(related_rule_id)
                # 确保模式存在（从修复中学习到的模式）
                created = self._ensure_pattern(
                    pattern_id,
                    category,
                    f"从修复 commit {fix_commit_hash[:8]} 学到的缺陷模式",
                    related_rule_id,
                    "info",
                    learned_from="git_fix",
                )
                if created:
                    learned_patterns += 1
                # 递增 case_count
                self._increment_pattern_case_count(pattern_id)

            # 避免重复插入修复记录
            if pattern_id:
                dup = self.conn.execute(
                    """
                    SELECT id FROM defect_fixes
                    WHERE pattern_id = ? AND symbol_hash = ? AND before_hash = ? AND after_hash = ?
                    """,
                    (pattern_id, symbol_hash, before_hash, after_hash),
                ).fetchone()
                if dup:
                    details.append({
                        "symbol_hash": symbol_hash,
                        "pattern_id": pattern_id,
                        "status": "duplicate",
                    })
                    continue

            # 创建 defect_fixes 记录
            self.conn.execute(
                """
                INSERT INTO defect_fixes
                    (pattern_id, symbol_hash, before_hash, after_hash, fix_diff,
                     effectiveness, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pattern_id, symbol_hash, before_hash, after_hash, fix_diff, 1.0, now),
            )
            learned_fixes += 1
            details.append({
                "symbol_hash": symbol_hash,
                "pattern_id": pattern_id or "",
                "rule_id": related_rule_id,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "status": "learned",
            })

        self.conn.commit()

        return {
            "learned_patterns": learned_patterns,
            "learned_fixes": learned_fixes,
            "details": details,
        }

    def defect_stats(self) -> Dict[str, Any]:
        """缺陷知识库统计

        Returns:
            {"total_patterns": N, "total_fixes": M, "by_category": {...},
             "by_severity": {...}, "avg_effectiveness": ..., "top_defects": [...]}
        """
        # 模式总数
        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM defect_patterns")
        total_patterns = cur.fetchone()["cnt"]

        # 修复总数
        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM defect_fixes")
        total_fixes = cur.fetchone()["cnt"]

        # 按类别分布
        cur = self.conn.execute(
            "SELECT category, COUNT(*) as cnt FROM defect_patterns GROUP BY category ORDER BY cnt DESC"
        )
        by_category = {row["category"]: row["cnt"] for row in cur.fetchall()}

        # 按严重度分布
        cur = self.conn.execute(
            "SELECT severity, COUNT(*) as cnt FROM defect_patterns GROUP BY severity ORDER BY cnt DESC"
        )
        by_severity = {row["severity"]: row["cnt"] for row in cur.fetchall()}

        # 平均有效性
        cur = self.conn.execute(
            "SELECT AVG(effectiveness) as avg_eff FROM defect_fixes"
        )
        avg_row = cur.fetchone()
        avg_effectiveness = avg_row["avg_eff"] if avg_row and avg_row["avg_eff"] is not None else 0.0

        # 最常见缺陷 Top 10（按 case_count 降序）
        cur = self.conn.execute(
            """
            SELECT pattern_id, category, description, detection_rule, severity, case_count
            FROM defect_patterns
            ORDER BY case_count DESC
            LIMIT 10
            """
        )
        top_defects = [dict(row) for row in cur.fetchall()]

        return {
            "total_patterns": total_patterns,
            "total_fixes": total_fixes,
            "by_category": by_category,
            "by_severity": by_severity,
            "avg_effectiveness": round(avg_effectiveness, 4),
            "top_defects": top_defects,
        }
