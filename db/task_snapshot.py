"""
task_snapshot.py
================

规范化 Workspace_Snapshot 模块（Requirement 6.2, 6.18, 6.9–6.10）。

Workspace_Snapshot 至少由 HEAD commit、规范化 dirty diff 摘要、相关 tracked/untracked
文件内容 hash 构成。仅覆盖 Envelope relevant scope、Actual_Changes 与声明的 verifier
依赖（Req 6.18）；repo-wide hashing 是非默认显式请求。

snapshot_id 是规范化摘要（wsnap:sha256:...），不是时间戳——仅有 commit hash 不能
表示未提交工作区，仅有 mtime 也不可靠（设计文档 §9.1）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 仓库父目录加入 sys.path（与 db_task_contracts.py 同模式）
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from i18n import t


# ============================================
# 常量
# ============================================

SNAPSHOT_ID_PREFIX = "wsnap:sha256:"


# ============================================
# 数据结构
# ============================================

@dataclass
class WorkspaceSnapshot:
    """规范化工作区快照（Req 6.2, 6.18）。

    所有 hash 字段格式为 sha256:hex64。path 键统一为正斜杠规范化。
    snapshot_id 由 compute_snapshot_id 计算，不参与自身 hash 输入。

    Attributes:
        head_commit: HEAD commit hash（未出生仓库为空字符串）
        dirty_diff_hash: 规范化 `git diff` 内容的 sha256（含 --staged 与 unstaged）
        file_hashes: {rel_path: sha256:...}，仅覆盖 relevant scope
        symbol_hashes: {symbol_qname: sha256:...}，从 db symbols 表查询
        graph_refresh_version: 图刷新版本标识（workspace_scan_runs.manifest_hash 或自增 id）
        snapshot_id: 规范化摘要 wsnap:sha256:...
    """

    head_commit: str = ""
    dirty_diff_hash: str = ""
    file_hashes: Dict[str, str] = field(default_factory=dict)
    symbol_hashes: Dict[str, str] = field(default_factory=dict)
    graph_refresh_version: str = ""
    snapshot_id: str = ""

    def to_dict(self, include_snapshot_id: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "head_commit": self.head_commit,
            "dirty_diff_hash": self.dirty_diff_hash,
            "file_hashes": dict(self.file_hashes),
            "symbol_hashes": dict(self.symbol_hashes),
            "graph_refresh_version": self.graph_refresh_version,
        }
        if include_snapshot_id:
            d["snapshot_id"] = self.snapshot_id
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkspaceSnapshot":
        return cls(
            head_commit=str(data.get("head_commit", "")),
            dirty_diff_hash=str(data.get("dirty_diff_hash", "")),
            file_hashes=dict(data.get("file_hashes", {})),
            symbol_hashes=dict(data.get("symbol_hashes", {})),
            graph_refresh_version=str(data.get("graph_refresh_version", "")),
            snapshot_id=str(data.get("snapshot_id", "")),
        )

    def is_empty(self) -> bool:
        """快照是否完全空（无任何绑定信息）。"""
        return (
            not self.head_commit
            and not self.dirty_diff_hash
            and not self.file_hashes
            and not self.symbol_hashes
            and not self.graph_refresh_version
        )

    def __eq__(self, other: object) -> bool:
        """快照相等性比较——基于 snapshot_id（若已计算）或内容字段。

        用于 TOCTOU 防护中 S0 == S1 比较（Req 6.9–6.10）。
        snapshot_id 相同时内容必然相同（sha256 抗碰撞）。
        """
        if not isinstance(other, WorkspaceSnapshot):
            return NotImplemented
        if self.snapshot_id and other.snapshot_id:
            return self.snapshot_id == other.snapshot_id
        # snapshot_id 未计算时按内容比较
        return (
            self.head_commit == other.head_commit
            and self.dirty_diff_hash == other.dirty_diff_hash
            and self.file_hashes == other.file_hashes
            and self.symbol_hashes == other.symbol_hashes
            and self.graph_refresh_version == other.graph_refresh_version
        )


# ============================================
# 路径规范化
# ============================================

def _normalize_path(p: str) -> str:
    """路径规范化为正斜杠（与 db_task_contracts.py 一致）。"""
    if not p:
        return ""
    return p.replace("\\", "/")


# ============================================
# snapshot_id 计算
# ============================================

def compute_snapshot_id(snapshot: WorkspaceSnapshot) -> str:
    """计算规范化 snapshot_id（wsnap:sha256:...）。

    排除 snapshot_id 自身，对剩余字段做确定性 JSON 序列化后 sha256。
    sort_keys=True + ensure_ascii=False + separators=(',', ':') 消除空白差异。
    """
    data = snapshot.to_dict(include_snapshot_id=False)
    # 路径键规范化
    data["file_hashes"] = {
        _normalize_path(k): v for k, v in data["file_hashes"].items()
    }
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return SNAPSHOT_ID_PREFIX + hashlib.sha256(canonical).hexdigest()


# ============================================
# 文件 hash 计算
# ============================================

def compute_file_hash(file_path: str) -> str:
    """计算单个文件的 sha256 hash。

    Args:
        file_path: 绝对路径

    Returns:
        sha256:hex64 格式 hash；文件不存在或读取失败返回空字符串
    """
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except (OSError, IOError):
        return ""


def compute_file_hashes_batch(
    workspace_root: str, rel_paths: Sequence[str]
) -> Dict[str, str]:
    """批量计算文件 hash（Req 6.18: 仅覆盖 relevant scope）。

    Args:
        workspace_root: 工作区根目录绝对路径
        rel_paths: 相对路径列表（路径分隔符不统一也可）

    Returns:
        {normalized_rel_path: sha256:...}；文件不存在的条目值为空字符串
    """
    result: Dict[str, str] = {}
    for rel in rel_paths:
        norm = _normalize_path(rel)
        if not norm:
            continue
        abs_path = os.path.join(workspace_root, norm.replace("/", os.sep))
        result[norm] = compute_file_hash(abs_path)
    return result


# ============================================
# git 命令辅助
# ============================================

def _run_git(workspace_root: str, args: List[str]) -> str:
    """执行 git 命令并返回 stdout（stripped）。失败返回空字符串。"""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def get_head_commit(workspace_root: str) -> str:
    """获取 HEAD commit hash（未出生仓库返回空字符串）。"""
    return _run_git(workspace_root, ["rev-parse", "HEAD"])


def get_dirty_diff_hash(workspace_root: str) -> str:
    """计算规范化 dirty diff 的 sha256（含 staged + unstaged）。

    git diff HEAD 覆盖工作区与 HEAD 的所有差异（含 staged 和 unstaged）。
    """
    diff = _run_git(workspace_root, ["diff", "HEAD"])
    if not diff:
        # 无差异（clean working tree）→ 空内容的 hash
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    return "sha256:" + hashlib.sha256(diff.encode("utf-8")).hexdigest()


# ============================================
# 符号 hash 查询
# ============================================

def query_symbol_hashes(
    conn, symbol_qnames: Sequence[str], workspace_id: Optional[int] = None
) -> Dict[str, str]:
    """从 db symbols 表查询符号 hash（Req 6.2: relevant symbol hashes）。

    Args:
        conn: sqlite3.Connection
        symbol_qnames: 符号限定名列表
        workspace_id: 工作区 ID（可选过滤）

    Returns:
        {symbol_qname: symbol_hash}；未找到的符号不包含在结果中
    """
    if not symbol_qnames:
        return {}

    result: Dict[str, str] = {}
    # 分批查询避免 SQL 参数限制
    batch_size = 500
    for i in range(0, len(symbol_qnames), batch_size):
        batch = symbol_qnames[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))
        sql = (
            f"SELECT qualified_name, symbol_hash FROM symbols "
            f"WHERE qualified_name IN ({placeholders})"
        )
        params: list = list(batch)
        if workspace_id is not None:
            sql += " AND workspace_id = ?"
            params.append(workspace_id)
        try:
            cur = conn.execute(sql, params)
            for row in cur.fetchall():
                qn = row["qualified_name"] if isinstance(row, dict) else row[0]
                h = row["symbol_hash"] if isinstance(row, dict) else row[1]
                if qn and h:
                    result[qn] = h
        except Exception:
            pass  # fail-soft：查询失败返回空 dict
    return result


# ============================================
# 图刷新版本查询
# ============================================

def query_graph_refresh_version(
    conn, workspace_id: Optional[int] = None
) -> str:
    """查询图刷新版本（workspace_scan_runs.manifest_hash 或 id）。

    使用最新完成的 workspace_scan_runs 记录的 manifest_hash 作为图刷新版本标识。
    """
    sql = (
        "SELECT manifest_hash, id FROM workspace_scan_runs "
        "WHERE status = 'completed'"
    )
    params: list = []
    if workspace_id is not None:
        sql += " AND workspace_id = ?"
        params.append(workspace_id)
    sql += " ORDER BY completed_at DESC LIMIT 1"
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return ""
        manifest = row["manifest_hash"] if isinstance(row, dict) else row[0]
        if manifest:
            return manifest
        # manifest_hash 为空时回退到 id
        rid = row["id"] if isinstance(row, dict) else row[1]
        return str(rid) if rid else ""
    except Exception:
        return ""


# ============================================
# 快照捕获
# ============================================

def capture_workspace_snapshot(
    conn,
    workspace_root: str,
    relevant_files: Optional[Sequence[str]] = None,
    relevant_symbols: Optional[Sequence[str]] = None,
    workspace_id: Optional[int] = None,
    repo_wide: bool = False,
) -> WorkspaceSnapshot:
    """捕获规范化 Workspace_Snapshot（Req 6.2, 6.9, 6.18）。

    仅覆盖 relevant scope（Req 6.18）；repo_wide=True 时哈希全仓库文件（非默认）。

    Args:
        conn: sqlite3.Connection（用于查询 symbol hash 和 graph refresh version）
        workspace_root: 工作区根目录
        relevant_files: relevant scope 文件列表（rel path）
        relevant_symbols: relevant scope 符号限定名列表
        workspace_id: 工作区 ID（可选过滤）
        repo_wide: 是否对全仓库做 hashing（非默认显式请求，Req 6.18）

    Returns:
        WorkspaceSnapshot 实例（snapshot_id 已计算）
    """
    # HEAD commit
    head = get_head_commit(workspace_root)

    # dirty diff hash
    diff_hash = get_dirty_diff_hash(workspace_root)

    # file hashes
    if relevant_files is None:
        relevant_files = []
    file_hashes = compute_file_hashes_batch(workspace_root, relevant_files)

    # symbol hashes
    if relevant_symbols is None:
        relevant_symbols = []
    symbol_hashes = query_symbol_hashes(conn, relevant_symbols, workspace_id)

    # graph refresh version
    graph_version = query_graph_refresh_version(conn, workspace_id)

    snapshot = WorkspaceSnapshot(
        head_commit=head,
        dirty_diff_hash=diff_hash,
        file_hashes=file_hashes,
        symbol_hashes=symbol_hashes,
        graph_refresh_version=graph_version,
    )
    snapshot.snapshot_id = compute_snapshot_id(snapshot)
    return snapshot
