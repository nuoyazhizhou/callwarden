"""会话/消息全文检索（FTS5）查询面。

数据映射（库内没有独立的 sessions/messages 主表，复用协作主表）：
  - 会话 ← ``tasks``        任务协作单元（title / description / creator）
  - 消息 ← ``task_events``  协作事件流（reason / actor_identity / role / 状态迁移）

索引与同步触发器由 ``db.schema.ensure_session_fts_schema`` 幂等维护
（会话/消息 FTS5 外部内容表 + 插入/更新/删除触发器）。本模块只读：
负责把用户输入转成安全的 MATCH 表达式、按 bm25 排序、用 ``snippet()``
返回带高亮标记的片段。

优雅降级：FTS5 未编译、索引缺失或 MATCH 语法异常时，回退 LIKE 子串匹配，
调用方始终拿到结果列表而非异常（中英文子串均可命中）。

调用链：search_sessions / search_messages → _fts_* → build_match_query
        └─（异常/不可用）→ _like_*（LIKE 子串兜底）
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# snippet() 高亮标记：调用方按需替换为终端颜色或 markdown 粗体
SNIPPET_START = "["
SNIPPET_END = "]"
SNIPPET_ELLIPSIS = "…"
SNIPPET_TOKENS = 16

# FTS5 语法保留字符：直接剔除，避免用户输入被解释为操作符/列过滤
_FTS_STRIP_RE = re.compile(r'["\'*:^(){}\[\]\-+~?\\]')

# unicode61 只按分隔符切词，连续 CJK 会被并为单个 token，无法命中子串。
# 含 CJK 的查询改走 LIKE 子串路径，保证真实无空格中文标题仍可检索。
_CJK_RE = re.compile(
    r'[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF'  # CJK 统一/扩展 A/兼容表意文字
    r'\u3040-\u30FF\uAC00-\uD7AF]'               # 日文假名 / 韩文音节
)

_FTS_TABLES = ("sessions_fts", "messages_fts")


def _has_cjk(text: str) -> bool:
    """查询是否含 CJK 字符（unicode61 不切分 CJK，需 LIKE 子串路径）。"""
    return bool(_CJK_RE.search(text or ""))


def _like_pattern(query: str) -> str:
    """构造 LIKE 模式并转义 %/_/\\，避免用户输入被当成通配符（宽松匹配）。"""
    escaped = (query or "").strip()
    escaped = escaped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _clamp_limit(limit: Any) -> int:
    """limit 收敛到 [1, MAX_LIMIT]，非法值退回默认值。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _rows_to_dicts(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    """把游标结果转为 dict 列表（兼容 Row 与 tuple 两种连接配置）。"""
    columns = [col[0] for col in (cur.description or [])]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def fts_available(conn: sqlite3.Connection) -> bool:
    """会话/消息 FTS5 索引是否就绪（两表都在才认为可用）。"""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('sessions_fts', 'messages_fts')"
        ).fetchall()
    except sqlite3.Error:
        return False
    return len(rows) == len(_FTS_TABLES)


def build_match_query(query: str) -> str:
    """把用户输入转成安全的 FTS5 MATCH 表达式（unicode61 分词）。

    规则：按空白切词 → 剔除 FTS5 语法字符（引号/星号/列过滤/括号等）
    → 每个词用双引号包裹（避免 OR / AND / NOT / NEAR 被当作操作符）
    → 词间隐式 AND。空查询返回空串，调用方应返回空结果。
    """
    tokens = []
    for raw in (query or "").split():
        cleaned = _FTS_STRIP_RE.sub("", raw).strip()
        if cleaned:
            tokens.append('"' + cleaned.replace('"', '""') + '"')
    return " ".join(tokens)


def _manual_snippet(text: str, query: str) -> str:
    """LIKE 降级路径的手工高亮片段：首个命中位置前后截取窗口。"""
    text = text or ""
    needle = (query or "").strip()
    if not text:
        return ""
    if not needle:
        return text[:SNIPPET_TOKENS * 4]
    pos = text.lower().find(needle.lower())
    if pos < 0:
        return text[:SNIPPET_TOKENS * 4]
    left = max(0, pos - 20)
    right = min(len(text), pos + len(needle) + 20)
    fragment = text[left:right]
    marked = text[pos:pos + len(needle)]
    return (
        ("…" if left > 0 else "")
        + fragment.replace(marked, SNIPPET_START + marked + SNIPPET_END, 1)
        + ("…" if right < len(text) else "")
    )


def search_sessions(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """全文检索会话（tasks）。

    返回字段：task_id / title / creator / status / updated_at / snippet / score。
    snippet 为带 ``[ ]`` 高亮标记的原文片段；score 为 bm25 排序分
    （FTS 路径为负值，越接近 0 越相关；LIKE 降级路径固定 0.0）。
    """
    limit = _clamp_limit(limit)
    match = build_match_query(query)
    if not match:
        return []
    # unicode61 把连续 CJK 并为单 token，FTS 无法命中真实中文标题的子串。
    # 含 CJK 查询直接走 LIKE（子串语义），英文/数字仍走 FTS 保留 bm25 排序。
    if _has_cjk(query):
        return _like_sessions(conn, query, limit)
    if fts_available(conn):
        try:
            cur = conn.execute(
                """
                SELECT t.id AS task_id, t.title, t.creator, t.status, t.updated_at,
                       snippet(sessions_fts, -1, ?, ?, ?, ?) AS snippet,
                       bm25(sessions_fts) AS score
                FROM sessions_fts
                JOIN tasks t ON t.rowid = sessions_fts.rowid
                WHERE sessions_fts MATCH ?
                ORDER BY score, t.updated_at DESC
                LIMIT ?
                """,
                (
                    SNIPPET_START, SNIPPET_END, SNIPPET_ELLIPSIS, SNIPPET_TOKENS,
                    match, limit,
                ),
            )
            return _rows_to_dicts(cur)
        except sqlite3.Error:
            pass  # MATCH 语法/索引异常 → LIKE 兜底
    return _like_sessions(conn, query, limit)


def search_messages(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_LIMIT
) -> List[Dict[str, Any]]:
    """全文检索消息（task_events 协作事件流）。

    返回字段：event_id / task_id / actor_identity / role / from_status /
    to_status / authoritative_timestamp / snippet / score。
    """
    limit = _clamp_limit(limit)
    match = build_match_query(query)
    if not match:
        return []
    # 含 CJK 查询走 LIKE 子串路径（理由同 search_sessions）。
    if _has_cjk(query):
        return _like_messages(conn, query, limit)
    if fts_available(conn):
        try:
            cur = conn.execute(
                """
                SELECT e.event_id, e.task_id, e.actor_identity, e.role,
                       e.from_status, e.to_status, e.authoritative_timestamp,
                       snippet(messages_fts, -1, ?, ?, ?, ?) AS snippet,
                       bm25(messages_fts) AS score
                FROM messages_fts
                JOIN task_events e ON e.event_id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                ORDER BY score, e.authoritative_timestamp DESC
                LIMIT ?
                """,
                (
                    SNIPPET_START, SNIPPET_END, SNIPPET_ELLIPSIS, SNIPPET_TOKENS,
                    match, limit,
                ),
            )
            return _rows_to_dicts(cur)
        except sqlite3.Error:
            pass  # MATCH 语法/索引异常 → LIKE 兜底
    return _like_messages(conn, query, limit)


def search_sessions_and_messages(
    conn: sqlite3.Connection, query: str, limit: int = DEFAULT_LIMIT
) -> Dict[str, List[Dict[str, Any]]]:
    """会话 + 消息合并检索视图（供 RPC 层一次调用返回两段结果）。

    每段独立受 limit 约束；返回 ``{"sessions": [...], "messages": [...]}``。
    """
    return {
        "sessions": search_sessions(conn, query, limit),
        "messages": search_messages(conn, query, limit),
    }


def _like_sessions(
    conn: sqlite3.Connection, query: str, limit: int
) -> List[Dict[str, Any]]:
    """会话 LIKE 降级路径（FTS5 不可用）：中英文子串均可命中。"""
    pattern = _like_pattern(query)
    cur = conn.execute(
        """
        SELECT id AS task_id, title, creator, status, updated_at, description
        FROM tasks
        WHERE title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'
              OR creator LIKE ? ESCAPE '\\'
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    )
    results = []
    for row in _rows_to_dicts(cur):
        description = row.pop("description", "") or ""
        text = row["title"] if _icontains(row.get("title"), query) else description
        result = dict(row)
        result["snippet"] = _manual_snippet(text, query)
        result["score"] = 0.0
        results.append(result)
    return results


def _like_messages(
    conn: sqlite3.Connection, query: str, limit: int
) -> List[Dict[str, Any]]:
    """消息 LIKE 降级路径（FTS5 不可用）。"""
    pattern = _like_pattern(query)
    cur = conn.execute(
        """
        SELECT event_id, task_id, actor_identity, role, from_status, to_status,
               authoritative_timestamp, reason
        FROM task_events
        WHERE reason LIKE ? ESCAPE '\\' OR actor_identity LIKE ? ESCAPE '\\'
              OR role LIKE ? ESCAPE '\\'
        ORDER BY authoritative_timestamp DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, limit),
    )
    results = []
    for row in _rows_to_dicts(cur):
        reason = row.pop("reason", "") or ""
        result = dict(row)
        result["snippet"] = _manual_snippet(reason, query)
        result["score"] = 0.0
        results.append(result)
    return results


def _icontains(haystack: Any, needle: str) -> bool:
    """大小写无关子串判断（None 安全）。"""
    return bool(needle) and needle.lower() in str(haystack or "").lower()
