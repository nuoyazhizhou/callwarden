"""Phase 3-4: StagingLog + ParseRetryLog PyO3 暴露层差分测试

对应：
- Phase 3-4-1: `rust_ext/src/staging_log_query.rs`（9 个 API）
- Phase 3-4-2: `rust_ext/src/parse_retry_log_query.rs`（9 个 API）

差分策略：
- StagingLog：Python `server/staging_log.py:StagingLog` ↔ Rust `staging_log_*` PyO3 API
  做真正的 Python↔Rust 业务行为对照。
- ParseRetryLog：Python 端无对应实现，在测试中创建 `_PyParseRetryLog` 参考实现
  （复用 Rust 的 JSON Lines + LSN + status 语义），与 Rust `parse_retry_log_*` API 对照。

测试场景：
- S1-S10: StagingLog 差分（append/read/read_pending/mark_applied_batch/mark_failed/
  truncate/compact_applied/stats/next_lsn/文件不存在）
- P1-P10: ParseRetryLog 差分（append/read/read_pending/read_retryable/mark_applied/
  mark_exhausted/increment_retry/compact/next_lsn/permanent 不在 retryable）

环境要求：
- Rust 扩展 `callwarden_core` 必须可导入（cp314 wheel 安装到 Python 3.14）
- 不可导入时全部 skip（与 Phase 1/2 差分测试一致）
"""
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest

# ============================================
# Rust 扩展可用性检测
# ============================================

try:
    import callwarden_core  # type: ignore
    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False

# Python StagingLog（server/staging_log.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.staging_log import StagingEntry, StagingLog, create_staging_entry  # type: ignore


# ============================================
# Python 参考实现：ParseRetryLog（用于差分测试）
# ============================================

class _PyParseFailureEntry:
    """ParseFailureEntry Python 参考实现（对齐 Rust ParseFailureEntry 语义）"""

    def __init__(
        self,
        workspace_id: str,
        rel_path: str,
        abs_path: str,
        generation: str,
        language: str,
        parse_status: str,
        cas_state: str,
        reason: str,
        allows_retry: bool,
        lsn: int = 0,
        timestamp: float = 0.0,
        retry_count: int = 0,
        last_retry_at: Optional[float] = None,
        status: Optional[str] = None,
    ):
        self.lsn = lsn
        self.timestamp = timestamp
        self.workspace_id = workspace_id
        self.rel_path = rel_path
        self.abs_path = abs_path
        self.generation = generation
        self.language = language
        self.parse_status = parse_status
        self.cas_state = cas_state
        self.reason = reason
        self.allows_retry = allows_retry
        self.retry_count = retry_count
        self.last_retry_at = last_retry_at
        # allows_retry=true → pending；allows_retry=false → permanent
        self.status = status or ("pending" if allows_retry else "permanent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lsn": self.lsn,
            "timestamp": self.timestamp,
            "workspace_id": self.workspace_id,
            "rel_path": self.rel_path,
            "abs_path": self.abs_path,
            "generation": self.generation,
            "language": self.language,
            "parse_status": self.parse_status,
            "cas_state": self.cas_state,
            "reason": self.reason,
            "allows_retry": self.allows_retry,
            "retry_count": self.retry_count,
            "last_retry_at": self.last_retry_at,
            "status": self.status,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> Optional["_PyParseFailureEntry"]:
        try:
            data = json.loads(line.strip())
            return cls(
                workspace_id=data["workspace_id"],
                rel_path=data["rel_path"],
                abs_path=data.get("abs_path", ""),
                generation=data.get("generation", ""),
                language=data.get("language", ""),
                parse_status=data["parse_status"],
                cas_state=data.get("cas_state", ""),
                reason=data.get("reason", ""),
                allows_retry=data.get("allows_retry", False),
                lsn=data["lsn"],
                timestamp=data.get("timestamp", 0.0),
                retry_count=data.get("retry_count", 0),
                last_retry_at=data.get("last_retry_at"),
                status=data.get("status"),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def is_retryable(self, max_retry: int) -> bool:
        return (
            self.allows_retry
            and self.status == "pending"
            and self.retry_count < max_retry
        )


class _PyParseRetryLog:
    """ParseRetryLog Python 参考实现（对齐 Rust ParseRetryLog 语义）

    用于差分测试：与 Rust `parse_retry_log_*` PyO3 API 行为对照。
    """

    def __init__(self, log_path: str):
        self.log_path = str(log_path)
        self._next_lsn = 1

        # 确保目录存在
        log_dir = os.path.dirname(self.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        self._recover_lsn()

    def _recover_lsn(self):
        if not os.path.exists(self.log_path):
            return
        max_lsn = 0
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    entry = _PyParseFailureEntry.from_json_line(line)
                    if entry and entry.lsn > max_lsn:
                        max_lsn = entry.lsn
        except IOError:
            pass
        self._next_lsn = max_lsn + 1

    def append(self, entry: _PyParseFailureEntry) -> int:
        entry.lsn = self._next_lsn
        if entry.timestamp == 0.0:
            entry.timestamp = time.time()
        self._next_lsn += 1

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(entry.to_json_line() + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry.lsn

    def read(self, since_lsn: int = 0) -> List[_PyParseFailureEntry]:
        entries: List[_PyParseFailureEntry] = []
        if not os.path.exists(self.log_path):
            return entries
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                entry = _PyParseFailureEntry.from_json_line(line)
                if entry and entry.lsn > since_lsn:
                    entries.append(entry)
        return entries

    def read_pending(self) -> List[_PyParseFailureEntry]:
        return [e for e in self.read() if e.status == "pending"]

    def read_retryable(self, max_retry: int) -> List[_PyParseFailureEntry]:
        return [e for e in self.read_pending() if e.is_retryable(max_retry)]

    def mark_applied(self, lsn: int):
        self._update_status(lsn, "applied")

    def mark_exhausted(self, lsn: int):
        self._update_status(lsn, "exhausted")

    def increment_retry(self, lsn: int):
        entries = self.read()
        now = time.time()
        for e in entries:
            if e.lsn == lsn:
                e.retry_count += 1
                e.last_retry_at = now
                break
        self._rewrite(entries)

    def compact(self) -> int:
        entries = self.read()
        total = len(entries)
        kept = [e for e in entries if e.status == "pending"]
        removed = total - len(kept)
        self._rewrite(kept)
        return removed

    def _update_status(self, lsn: int, new_status: str):
        entries = self.read()
        for e in entries:
            if e.lsn == lsn:
                e.status = new_status
                break
        self._rewrite(entries)

    def _rewrite(self, entries: List[_PyParseFailureEntry]):
        tmp_path = self.log_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(e.to_json_line() + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.log_path)


# ============================================
# 辅助函数
# ============================================

def _make_staging_entry_json(
    workspace_id: str = "ws1",
    file_path: str = "a.rs",
    content_hash: str = "abc123",
    language: str = "rust",
    status: str = "pending",
) -> str:
    """构造 StagingEntry JSON（用于 Rust API）"""
    return json.dumps({
        "lsn": 0,
        "timestamp": 0.0,
        "workspace_id": workspace_id,
        "file_path": file_path,
        "content_hash": content_hash,
        "language": language,
        "parse_delta": {},
        "resolve_delta": {},
        "frontier": {},
        "metrics_update": {},
        "status": status,
        "error": None,
    }, ensure_ascii=False)


def _make_parse_failure_entry_json(
    workspace_id: str = "ws1",
    rel_path: str = "a.rs",
    allows_retry: bool = True,
    parse_status: str = "failed",
    reason: str = "test error",
) -> str:
    """构造 ParseFailureEntry JSON（用于 Rust API）"""
    return json.dumps({
        "lsn": 0,
        "timestamp": 0.0,
        "workspace_id": workspace_id,
        "rel_path": rel_path,
        "abs_path": f"/repo/{rel_path}",
        "generation": "1:1",
        "language": "rust",
        "parse_status": parse_status,
        "cas_state": "parse_failed",
        "reason": reason,
        "allows_retry": allows_retry,
        "retry_count": 0,
        "last_retry_at": None,
        "status": "pending" if allows_retry else "permanent",
    }, ensure_ascii=False)


def _make_py_parse_failure_entry(
    workspace_id: str = "ws1",
    rel_path: str = "a.rs",
    allows_retry: bool = True,
) -> _PyParseFailureEntry:
    """构造 Python _PyParseFailureEntry"""
    return _PyParseFailureEntry(
        workspace_id=workspace_id,
        rel_path=rel_path,
        abs_path=f"/repo/{rel_path}",
        generation="1:1",
        language="rust",
        parse_status="failed",
        cas_state="parse_failed",
        reason="test error",
        allows_retry=allows_retry,
    )


# ============================================
# StagingLog 差分测试（S1-S10）
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 不可用（需 cp314 wheel）")
class TestStagingLogDiff:
    """StagingLog Python↔Rust 差分测试"""

    def test_s1_append_and_read_round_trip(self, tmp_path):
        """S1: append 3 条 + read 全部 + read since_lsn"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python 路径
        py_log = StagingLog(py_path)
        py_lsns = []
        for fp in ["a.rs", "b.rs", "c.rs"]:
            entry = create_staging_entry("ws1", fp, "abc123", "rust")
            py_lsns.append(py_log.append(entry))

        # Rust 路径
        for fp in ["a.rs", "b.rs", "c.rs"]:
            callwarden_core.staging_log_append(
                rust_path, _make_staging_entry_json(file_path=fp)
            )

        # 差分：LSN 序列
        assert py_lsns == [1, 2, 3]

        # 差分：read all
        py_all = py_log.read(0)
        rust_all_json = callwarden_core.staging_log_read(rust_path, 0)
        rust_all = json.loads(rust_all_json)
        assert len(py_all) == len(rust_all) == 3
        assert [e.lsn for e in py_all] == [e["lsn"] for e in rust_all]
        assert [e.file_path for e in py_all] == [e["file_path"] for e in rust_all]

        # 差分：read since_lsn=1
        py_since = py_log.read(1)
        rust_since_json = callwarden_core.staging_log_read(rust_path, 1)
        rust_since = json.loads(rust_since_json)
        assert len(py_since) == len(rust_since) == 2
        assert py_since[0].lsn == rust_since[0]["lsn"] == 2

    def test_s2_read_pending(self, tmp_path):
        """S2: read_pending 只返回 status=pending"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # 两端各写 2 条 pending
        py_log = StagingLog(py_path)
        py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))

        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))

        py_pending = py_log.read_pending()
        rust_pending = json.loads(callwarden_core.staging_log_read_pending(rust_path))
        assert len(py_pending) == len(rust_pending) == 2
        assert all(e.status == "pending" for e in py_pending)
        assert all(e["status"] == "pending" for e in rust_pending)

    def test_s3_mark_applied_batch(self, tmp_path):
        """S3: mark_applied_batch 批量标记"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_lsn1 = py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_lsn2 = py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))
        py_log.append(create_staging_entry("ws1", "c.rs", "h3", "rust"))
        py_log.mark_applied_batch([py_lsn1, py_lsn2])

        # Rust
        r_lsn1 = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        r_lsn2 = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="c.rs"))
        callwarden_core.staging_log_mark_applied_batch(rust_path, [r_lsn1, r_lsn2])

        # 差分：pending 应只剩 1 条（lsn=3）
        py_pending = py_log.read_pending()
        rust_pending = json.loads(callwarden_core.staging_log_read_pending(rust_path))
        assert len(py_pending) == len(rust_pending) == 1
        assert py_pending[0].lsn == rust_pending[0]["lsn"] == 3

    def test_s4_mark_failed(self, tmp_path):
        """S4: mark_failed 标记失败 + error 字段"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_lsn = py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.mark_failed(py_lsn, "test failure")

        # Rust
        r_lsn = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_mark_failed(rust_path, r_lsn, "test failure")

        # 差分：pending 应为空
        assert len(py_log.read_pending()) == 0
        assert len(json.loads(callwarden_core.staging_log_read_pending(rust_path))) == 0

        # 差分：read all 检查 status 和 error
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.staging_log_read(rust_path, 0))
        assert py_all[0].status == rust_all[0]["status"] == "failed"
        assert py_all[0].error == rust_all[0]["error"] == "test failure"

    def test_s5_truncate(self, tmp_path):
        """S5: truncate 截断 LSN <= up_to_lsn"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))
        py_log.append(create_staging_entry("ws1", "c.rs", "h3", "rust"))
        py_log.truncate(2)  # 删除 lsn <= 2

        # Rust
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="c.rs"))
        callwarden_core.staging_log_truncate(rust_path, 2)

        # 差分：应只剩 lsn=3
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.staging_log_read(rust_path, 0))
        assert len(py_all) == len(rust_all) == 1
        assert py_all[0].lsn == rust_all[0]["lsn"] == 3

    def test_s6_compact_applied_no_workspace(self, tmp_path):
        """S6: compact_applied(workspace_id=None) 删除所有 applied"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_lsn1 = py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))
        py_log.mark_applied(py_lsn1)
        py_log.compact_applied()  # 删除所有 applied

        # Rust
        r_lsn1 = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))
        callwarden_core.staging_log_mark_applied_batch(rust_path, [r_lsn1])
        callwarden_core.staging_log_compact_applied(rust_path, None)

        # 差分：应只剩 1 条 pending（lsn=2）
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.staging_log_read(rust_path, 0))
        assert len(py_all) == len(rust_all) == 1
        assert py_all[0].lsn == rust_all[0]["lsn"] == 2
        assert py_all[0].status == rust_all[0]["status"] == "pending"

    def test_s7_compact_applied_with_workspace(self, tmp_path):
        """S7: compact_applied(workspace_id=...) 只删除指定 workspace 的 applied"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_lsn1 = py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_lsn2 = py_log.append(create_staging_entry("ws2", "b.rs", "h2", "rust"))
        py_log.mark_applied_batch([py_lsn1, py_lsn2])
        py_log.compact_applied("ws1")  # 只删除 ws1 的 applied

        # Rust
        r_lsn1 = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(workspace_id="ws1", file_path="a.rs"))
        r_lsn2 = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(workspace_id="ws2", file_path="b.rs"))
        callwarden_core.staging_log_mark_applied_batch(rust_path, [r_lsn1, r_lsn2])
        callwarden_core.staging_log_compact_applied(rust_path, "ws1")

        # 差分：应只剩 ws2 的 applied（lsn=2）
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.staging_log_read(rust_path, 0))
        assert len(py_all) == len(rust_all) == 1
        assert py_all[0].lsn == rust_all[0]["lsn"] == 2
        assert py_all[0].workspace_id == rust_all[0]["workspace_id"] == "ws2"
        assert py_all[0].status == rust_all[0]["status"] == "applied"

    def test_s8_stats(self, tmp_path):
        """S8: stats 统计信息"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_lsn = py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))
        py_log.mark_applied(py_lsn)

        # Rust
        r_lsn = callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))
        callwarden_core.staging_log_mark_applied_batch(rust_path, [r_lsn])

        py_stats = py_log.stats()
        rust_stats = json.loads(callwarden_core.staging_log_stats(rust_path))

        # 差分：total=2, pending=1, applied=1, failed=0, next_lsn=3
        assert py_stats["total_entries"] == rust_stats["total_entries"] == 2
        assert py_stats["pending"] == rust_stats["pending"] == 1
        assert py_stats["applied"] == rust_stats["applied"] == 1
        assert py_stats["failed"] == rust_stats["failed"] == 0
        assert py_stats["next_lsn"] == rust_stats["next_lsn"] == 3

    def test_s9_next_lsn_recovery(self, tmp_path):
        """S9: 重新打开 log 时 next_lsn 恢复"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = StagingLog(py_path)
        py_log.append(create_staging_entry("ws1", "a.rs", "h1", "rust"))
        py_log.append(create_staging_entry("ws1", "b.rs", "h2", "rust"))
        # 重新打开（模拟新进程）
        py_log2 = StagingLog(py_path)
        assert py_log2._next_lsn == 3

        # Rust
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="a.rs"))
        callwarden_core.staging_log_append(rust_path, _make_staging_entry_json(file_path="b.rs"))
        # 重新打开（无状态函数模式，每次调用都从文件恢复）
        assert callwarden_core.staging_log_next_lsn(rust_path) == 3

    def test_s10_file_not_exists_read_returns_empty(self, tmp_path):
        """S10: 文件不存在时 read 返回空列表"""
        py_path = str(tmp_path / "nonexistent_py.log")
        rust_path = str(tmp_path / "nonexistent_rust.log")

        # Python：文件不存在时 read 返回空（不报错）
        py_log = StagingLog(py_path)  # __init__ 会创建空文件
        py_all = py_log.read(0)
        assert len(py_all) == 0

        # Rust：文件不存在时 read 返回 "[]"（StagingLog::new 会创建空文件）
        rust_all = json.loads(callwarden_core.staging_log_read(rust_path, 0))
        assert len(rust_all) == 0


# ============================================
# ParseRetryLog 差分测试（P1-P10）
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 不可用（需 cp314 wheel）")
class TestParseRetryLogDiff:
    """ParseRetryLog Python 参考实现 ↔ Rust 差分测试"""

    def test_p1_append_and_read_round_trip(self, tmp_path):
        """P1: append 3 条 + read 全部 + read since_lsn"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsns = []
        for rp in ["a.rs", "b.rs", "c.rs"]:
            entry = _make_py_parse_failure_entry(rel_path=rp)
            py_lsns.append(py_log.append(entry))

        # Rust
        for rp in ["a.rs", "b.rs", "c.rs"]:
            callwarden_core.parse_retry_log_append(
                rust_path, _make_parse_failure_entry_json(rel_path=rp)
            )

        # 差分：LSN 序列
        assert py_lsns == [1, 2, 3]

        # 差分：read all
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.parse_retry_log_read(rust_path, 0))
        assert len(py_all) == len(rust_all) == 3
        assert [e.lsn for e in py_all] == [e["lsn"] for e in rust_all]
        assert [e.rel_path for e in py_all] == [e["rel_path"] for e in rust_all]

        # 差分：read since_lsn=1
        py_since = py_log.read(1)
        rust_since = json.loads(callwarden_core.parse_retry_log_read(rust_path, 1))
        assert len(py_since) == len(rust_since) == 2
        assert py_since[0].lsn == rust_since[0]["lsn"] == 2

    def test_p2_read_pending(self, tmp_path):
        """P2: read_pending 只返回 status=pending（permanent 不在 pending 中）"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_log.append(_make_py_parse_failure_entry(rel_path="a.rs", allows_retry=True))
        py_log.append(_make_py_parse_failure_entry(rel_path="b.rs", allows_retry=True))
        py_log.append(_make_py_parse_failure_entry(rel_path="c.rs", allows_retry=False))

        # Rust
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs", allows_retry=True))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs", allows_retry=True))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="c.rs", allows_retry=False))

        py_pending = py_log.read_pending()
        rust_pending = json.loads(callwarden_core.parse_retry_log_read_pending(rust_path))
        assert len(py_pending) == len(rust_pending) == 2
        assert all(e.status == "pending" for e in py_pending)
        assert all(e["status"] == "pending" for e in rust_pending)

    def test_p3_read_retryable(self, tmp_path):
        """P3: read_retryable 过滤 retry_count >= max_retry"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsn1 = py_log.append(_make_py_parse_failure_entry(rel_path="a.rs"))
        py_lsn2 = py_log.append(_make_py_parse_failure_entry(rel_path="b.rs"))

        # increment lsn1 三次 → retry_count=3，max_retry=3 不可重试
        py_log.increment_retry(py_lsn1)
        py_log.increment_retry(py_lsn1)
        py_log.increment_retry(py_lsn1)

        # Rust
        r_lsn1 = callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs"))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs"))

        callwarden_core.parse_retry_log_increment_retry(rust_path, r_lsn1)
        callwarden_core.parse_retry_log_increment_retry(rust_path, r_lsn1)
        callwarden_core.parse_retry_log_increment_retry(rust_path, r_lsn1)

        # 差分：max_retry=3，retry_count=3 不可重试 → 只剩 lsn2
        py_retryable = py_log.read_retryable(3)
        rust_retryable = json.loads(callwarden_core.parse_retry_log_read_retryable(rust_path, 3))
        assert len(py_retryable) == len(rust_retryable) == 1
        assert py_retryable[0].lsn == rust_retryable[0]["lsn"] == py_lsn2

    def test_p4_mark_applied(self, tmp_path):
        """P4: mark_applied 标记成功"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsn1 = py_log.append(_make_py_parse_failure_entry(rel_path="a.rs"))
        py_log.append(_make_py_parse_failure_entry(rel_path="b.rs"))
        py_log.mark_applied(py_lsn1)

        # Rust
        r_lsn1 = callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs"))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs"))
        callwarden_core.parse_retry_log_mark_applied(rust_path, r_lsn1)

        # 差分：pending 应只剩 1 条
        py_pending = py_log.read_pending()
        rust_pending = json.loads(callwarden_core.parse_retry_log_read_pending(rust_path))
        assert len(py_pending) == len(rust_pending) == 1
        assert py_pending[0].lsn == rust_pending[0]["lsn"] == 2

    def test_p5_mark_exhausted(self, tmp_path):
        """P5: mark_exhausted 标记耗尽"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsn = py_log.append(_make_py_parse_failure_entry(rel_path="a.rs"))
        py_log.mark_exhausted(py_lsn)

        # Rust
        r_lsn = callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs"))
        callwarden_core.parse_retry_log_mark_exhausted(rust_path, r_lsn)

        # 差分：pending 应为空
        assert len(py_log.read_pending()) == 0
        assert len(json.loads(callwarden_core.parse_retry_log_read_pending(rust_path))) == 0

        # 差分：read all 检查 status=exhausted
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.parse_retry_log_read(rust_path, 0))
        assert py_all[0].status == rust_all[0]["status"] == "exhausted"

    def test_p6_increment_retry(self, tmp_path):
        """P6: increment_retry 增加 retry_count + last_retry_at"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsn = py_log.append(_make_py_parse_failure_entry(rel_path="a.rs"))
        py_log.increment_retry(py_lsn)
        py_log.increment_retry(py_lsn)

        # Rust
        r_lsn = callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs"))
        callwarden_core.parse_retry_log_increment_retry(rust_path, r_lsn)
        callwarden_core.parse_retry_log_increment_retry(rust_path, r_lsn)

        # 差分：retry_count=2
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.parse_retry_log_read(rust_path, 0))
        assert py_all[0].retry_count == rust_all[0]["retry_count"] == 2
        # last_retry_at 应为数字（非 None）
        assert py_all[0].last_retry_at is not None
        assert rust_all[0]["last_retry_at"] is not None

    def test_p7_compact(self, tmp_path):
        """P7: compact 删除非 pending（applied/exhausted/permanent）"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_lsn1 = py_log.append(_make_py_parse_failure_entry(rel_path="a.rs", allows_retry=True))
        py_log.append(_make_py_parse_failure_entry(rel_path="b.rs", allows_retry=True))
        py_log.append(_make_py_parse_failure_entry(rel_path="c.rs", allows_retry=False))  # permanent
        py_log.mark_applied(py_lsn1)
        py_removed = py_log.compact()

        # Rust
        r_lsn1 = callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs", allows_retry=True))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs", allows_retry=True))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="c.rs", allows_retry=False))
        callwarden_core.parse_retry_log_mark_applied(rust_path, r_lsn1)
        rust_removed = callwarden_core.parse_retry_log_compact(rust_path)

        # 差分：删除 2 条（applied + permanent），只剩 1 条 pending
        assert py_removed == rust_removed == 2
        py_all = py_log.read(0)
        rust_all = json.loads(callwarden_core.parse_retry_log_read(rust_path, 0))
        assert len(py_all) == len(rust_all) == 1
        assert py_all[0].lsn == rust_all[0]["lsn"] == 2
        assert py_all[0].status == rust_all[0]["status"] == "pending"

    def test_p8_next_lsn_recovery(self, tmp_path):
        """P8: 重新打开 log 时 next_lsn 恢复"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_log.append(_make_py_parse_failure_entry(rel_path="a.rs"))
        py_log.append(_make_py_parse_failure_entry(rel_path="b.rs"))
        py_log2 = _PyParseRetryLog(py_path)
        assert py_log2._next_lsn == 3

        # Rust
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs"))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs"))
        assert callwarden_core.parse_retry_log_next_lsn(rust_path) == 3

    def test_p9_file_not_exists_read_returns_empty(self, tmp_path):
        """P9: 文件不存在时 read 返回空列表"""
        py_path = str(tmp_path / "nonexistent_py.log")
        rust_path = str(tmp_path / "nonexistent_rust.log")

        # Python：文件不存在时 read 返回空（不报错）
        py_log = _PyParseRetryLog(py_path)  # __init__ 会创建空文件
        py_all = py_log.read(0)
        assert len(py_all) == 0

        # Rust：文件不存在时 read 返回 "[]"（ParseRetryLog::new 会创建空文件）
        rust_all = json.loads(callwarden_core.parse_retry_log_read(rust_path, 0))
        assert len(rust_all) == 0

    def test_p10_permanent_not_in_retryable(self, tmp_path):
        """P10: allows_retry=false 的 entry 不在 retryable 中"""
        py_path = str(tmp_path / "py.log")
        rust_path = str(tmp_path / "rust.log")

        # Python
        py_log = _PyParseRetryLog(py_path)
        py_log.append(_make_py_parse_failure_entry(rel_path="a.rs", allows_retry=False))
        py_log.append(_make_py_parse_failure_entry(rel_path="b.rs", allows_retry=True))

        # Rust
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="a.rs", allows_retry=False))
        callwarden_core.parse_retry_log_append(rust_path, _make_parse_failure_entry_json(rel_path="b.rs", allows_retry=True))

        # 差分：retryable 只含 b.rs
        py_retryable = py_log.read_retryable(3)
        rust_retryable = json.loads(callwarden_core.parse_retry_log_read_retryable(rust_path, 3))
        assert len(py_retryable) == len(rust_retryable) == 1
        assert py_retryable[0].rel_path == rust_retryable[0]["rel_path"] == "b.rs"
