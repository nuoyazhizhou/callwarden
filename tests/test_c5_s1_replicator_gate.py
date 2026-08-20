"""C5 S1 复审整改（P1-1）：daemon_server 层 C2 merge 失败门控测试。

覆盖 Reviewer 复审结论 P1-1：
- daemon_server.py `workspace.file.refresh` handler 在 merge 失败
  （merge_status=error/cas_miss/open_failed）时跳过 replicate、
  返回 snapshot_warning、staging append 为 pending 不 applied。
- P1-3 修复后 cas_miss/open_failed 分支可达（Python 透传 Rust merge_status）。

对比参照：Rust `workspace.rs` L2140-2166（merge 失败 skip committed + replicate）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"),
)


def _make_daemon_service(tmp_path):
    """构造 EnterpriseDaemonService 实例（不启动 UDS server）。"""
    from callwarden.server.daemon_server import EnterpriseDaemonService
    from callwarden.server.snapshot_manager import SnapshotManagerService
    snapshot_service = SnapshotManagerService(max_workspaces=8)
    return EnterpriseDaemonService(
        registry_db=str(tmp_path / "registry.db"),
        snapshot_service=snapshot_service,
    )


def _peer():
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return {"pid": os.getpid(), "uid": uid, "gid": uid}


class TestDaemonHandlerMergeGate:
    """daemon_server.py refresh handler 的 merge 失败门控（C5 C2 / P1-1）。"""

    @pytest.fixture
    def daemon_service(self, tmp_path):
        return _make_daemon_service(tmp_path)

    def _dispatch_refresh(self, daemon_service, ws_id="1", mock_workspace=None):
        """调用 workspace.file.refresh dispatch，绕过 _owned_workspace 校验。"""
        params = {
            "workspace_instance_id": ws_id,
            "rel_path": "main.py",
            "agent_session_id": "s1",
            "session_epoch": 1,
            "monotonic_seq": 1,
            "canonical_bytes_hex": b"x".hex(),
            "language": "python",
        }
        if mock_workspace is None:
            # 用 dict 模拟 workspace row：daemon_server.py L1231 取
            # workspace["workspace_id"] 做 int()，dict 比 MagicMock 更可靠
            mock_workspace = {
                "workspace_id": int(ws_id) if ws_id.isdigit() else ws_id,
                "host_real_root": str(Path.cwd()),
            }
        with patch.object(
            daemon_service, "_owned_workspace", return_value=mock_workspace,
        ):
            return daemon_service.dispatch(
                _peer(), "workspace.file.refresh", params)

    def _patch_resources(self, daemon_service, tmp_path):
        """patch _get_workspace_resources 返回 mock 资源。"""
        mock_ws_conn = MagicMock()
        mock_cas_conn = MagicMock()
        mock_staging_log = MagicMock()
        mock_replicator = MagicMock()
        mock_resources = {
            "ws_conn": mock_ws_conn,
            "cas_conn": mock_cas_conn,
            "staging_log": mock_staging_log,
            "replicator": mock_replicator,
            "codegraph_db_path": str(tmp_path / "cg.db"),
            "ws_db_path": str(tmp_path / "ws.db"),
            "cas_db_path": str(tmp_path / "cas.db"),
        }
        return patch.object(
            daemon_service,
            "_get_workspace_resources",
            return_value=mock_resources,
        ), mock_resources

    @pytest.mark.parametrize("merge_status", ["error", "cas_miss", "open_failed"])
    def test_merge_failure_skips_replicate(
        self, daemon_service, tmp_path, merge_status,
    ):
        """C5 C2 / P1-1：merge 失败（error/cas_miss/open_failed）→
        skip replicate + snapshot_warning + staging pending。"""
        respatch, mock_resources = self._patch_resources(
            daemon_service, tmp_path)
        merge_result = {
            "cas_key": "ck",
            "workspace_id": 1,
            "file_instance_id": 0,
            "symbols_inserted": 0,
            "calls_inserted": 0,
            "merge_status": merge_status,
            "error": f"simulated {merge_status}",
        }
        refresh_result = {
            "status": "committed",
            "generation": "1:1",
            "content_hash": "abc123",
            "merge": merge_result,
        }
        with respatch, patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            return_value=refresh_result,
        ):
            result = self._dispatch_refresh(daemon_service)

        # 1) staging append 一次（pending）
        assert mock_resources["staging_log"].append.call_count == 1, (
            "merge 失败也应 append staging entry（pending）"
        )
        appended = mock_resources["staging_log"].append.call_args[0][0]
        assert appended.status == "pending", (
            f"merge 失败 staging 应为 pending，实际: {appended.status}"
        )
        # 2) replicate 不被调用
        mock_resources["replicator"].replicate.assert_not_called()
        # 3) 返回 snapshot_warning + snapshot_published=false
        repl = result.get("replication") or {}
        assert repl.get("snapshot_published") is False, (
            f"merge 失败不应 publish snapshot，实际: {repl}"
        )
        assert "merge 失败" in repl.get("snapshot_warning", ""), (
            f"应返回 merge 失败 warning，实际: {repl.get('snapshot_warning')}"
        )
        assert repl.get("cas_merge", {}).get("merge_status") == merge_status, (
            "P1-3：merge_status 应透传 Rust 原值"
        )

    def test_merge_success_triggers_replicate(
        self, daemon_service, tmp_path,
    ):
        """对照组：merge 成功（merged）→ replicate 被调用。"""
        respatch, mock_resources = self._patch_resources(
            daemon_service, tmp_path)
        refresh_result = {
            "status": "committed",
            "generation": "1:1",
            "content_hash": "abc123",
            "merge": {
                "cas_key": "ck",
                "workspace_id": 1,
                "file_instance_id": 1,
                "symbols_inserted": 1,
                "calls_inserted": 0,
                "merge_status": "merged",
            },
        }
        with respatch, patch(
            "callwarden.server.replicator.daemon_handle_refresh",
            return_value=refresh_result,
        ):
            # replicate 返回带数值的结果（daemon_server.py L1290 比较 generation > 0）
            repl_result = MagicMock()
            repl_result.success = True
            repl_result.generation = 1
            repl_result.error = None
            mock_resources["replicator"].replicate.return_value = repl_result
            result = self._dispatch_refresh(daemon_service)

        # replicate 被调用
        mock_resources["replicator"].replicate.assert_called_once()
        # 正常路径 snapshot 发布成功，无 merge 失败 warning
        repl = result.get("replication") or {}
        assert repl.get("snapshot_published") is True, (
            f"merge 成功应 publish snapshot，实际: {repl}"
        )
        assert "merge 失败" not in repl.get("snapshot_warning", "")


class TestStagingEntryOperationField:
    """C5 S1 复审整改（P1-2）：StagingEntry operation 字段断言。"""

    def test_operation_defaults_to_refresh(self):
        """构造时未传 operation → 默认 refresh。"""
        from callwarden.server.staging_log import StagingEntry
        entry = StagingEntry(
            lsn=1,
            timestamp=1.0,
            workspace_id="w1",
            file_path="a.py",
            content_hash="h",
            language="python",
        )
        assert entry.operation == "refresh"

    def test_operation_round_trip_preserved(self):
        """to_dict → from_dict 保留 operation（refresh / delete）。"""
        from callwarden.server.staging_log import StagingEntry
        for op in ("refresh", "delete"):
            entry = StagingEntry(
                lsn=2,
                timestamp=2.0,
                workspace_id="w1",
                file_path="a.py",
                content_hash="h",
                language="python",
                operation=op,
            )
            restored = StagingEntry.from_dict(entry.to_dict())
            assert restored.operation == op, (
                f"round-trip 应保留 operation={op}，实际: {restored.operation}"
            )

    def test_from_dict_missing_operation_falls_back_to_refresh(self):
        """旧日志无 operation 字段 → 缺省 refresh（兼容历史数据）。"""
        from callwarden.server.staging_log import StagingEntry
        old_dict = {
            "lsn": 3,
            "timestamp": 3.0,
            "workspace_id": "w1",
            "file_path": "a.py",
            "content_hash": "h",
            "language": "python",
            "status": "pending",
        }
        entry = StagingEntry.from_dict(old_dict)
        assert entry.operation == "refresh"
        assert entry.status == "pending"

    def test_create_staging_entry_operation_passthrough(self):
        """create_staging_entry 支持 operation 参数并写入 entry。"""
        from callwarden.server.staging_log import create_staging_entry
        entry = create_staging_entry(
            workspace_id="w1",
            file_path="a.py",
            content_hash="h",
            language="python",
            operation="delete",
        )
        assert entry.operation == "delete"

        entry2 = create_staging_entry(
            workspace_id="w1",
            file_path="b.py",
            content_hash="h2",
            language="python",
        )
        assert entry2.operation == "refresh"
