"""client 侧 VCS 版本探测测试（2026-09-17 新增）。

验证 probe_vcs_info()：
1. git 仓库：返回 vcs_kind=git + 40-hex head_sha
2. 非 git 目录：返回 vcs_kind=none
3. mtime 缓存：.git/HEAD 未变时不重新 spawn
4. mtime 变化后缓存失效，重新探测
5. 空仓库（无提交）：vcs_kind=git, head_sha=""
6. 探测失败不抛异常（VCS 是增强字段）
7. build_refresh_message / send_refresh_to_daemon 透传 vcs 字段
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from callwarden.server.agent_session import AgentSession
from callwarden.server.agent_protocol import (
    build_refresh_message,
    send_refresh_to_daemon,
    probe_vcs_info,
    clear_vcs_cache,
    AgentProtocolError,
)


def _make_session_with_epoch(ws_id="ws_vcs", epoch=1):
    session = AgentSession.create_in_memory()
    session.register_workspace(ws_id)
    session.set_epoch(ws_id, epoch)
    return session


def _git_available() -> bool:
    """检查 git 可执行文件是否存在。"""
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


GIT_AVAILABLE = _git_available()


def _init_git_repo(root: Path) -> str:
    """在 root 初始化 git 仓库并产生一次提交，返回 head sha。"""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=root, check=True, capture_output=True)
    (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.py"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root,
                   check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ============================================
# 1. probe_vcs_info 基本路径
# ============================================


@pytest.fixture(autouse=True)
def _clear_vcs_cache():
    """每个测试前后清缓存，避免跨用例污染。"""
    clear_vcs_cache()
    yield
    clear_vcs_cache()


class TestProbeVcsInfo:
    """probe_vcs_info 基本行为。"""

    def test_non_git_dir_returns_none(self, tmp_path):
        """非 git 目录返回 vcs_kind=none。"""
        (tmp_path / "file.py").write_text("x\n", encoding="utf-8")
        info = probe_vcs_info(str(tmp_path))
        assert info == {"vcs_kind": "none"}

    def test_nonexistent_dir_returns_none(self, tmp_path):
        """不存在的目录返回 vcs_kind=none（不抛异常）。"""
        info = probe_vcs_info(str(tmp_path / "nope"))
        assert info == {"vcs_kind": "none"}

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git 不可用")
    def test_git_repo_returns_sha(self, tmp_path):
        """git 仓库返回 40-hex head_sha。"""
        sha = _init_git_repo(tmp_path)
        info = probe_vcs_info(str(tmp_path))
        assert info["vcs_kind"] == "git"
        assert info["head_sha"] == sha
        assert len(info["head_sha"]) == 40

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git 不可用")
    def test_empty_git_repo_returns_empty_sha(self, tmp_path):
        """git init 但无提交：head_sha 为空串（不抛异常）。"""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                       capture_output=True)
        info = probe_vcs_info(str(tmp_path))
        assert info["vcs_kind"] == "git"
        assert info["head_sha"] == ""

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git 不可用")
    def test_cache_hits_on_same_mtime(self, tmp_path):
        """.git/HEAD mtime 未变时使用缓存（不重新 spawn）。"""
        _init_git_repo(tmp_path)
        root = str(tmp_path)

        info1 = probe_vcs_info(root)
        assert info1["vcs_kind"] == "git"

        # patch subprocess.run：若缓存命中则不应被调用
        with patch("callwarden.server.agent_protocol.subprocess.run") as mock_run:
            info2 = probe_vcs_info(root)
            mock_run.assert_not_called()
        assert info2 == info1

    @pytest.mark.skipif(not GIT_AVAILABLE, reason="git 不可用")
    def test_cache_invalidates_on_mtime_change(self, tmp_path):
        """新提交改变 .git/HEAD mtime 后缓存失效，重新探测。"""
        _init_git_repo(tmp_path)
        root = str(tmp_path)

        info1 = probe_vcs_info(root)
        sha1 = info1["head_sha"]

        # 产生第二次提交
        (tmp_path / "world.py").write_text("print('world')\n", encoding="utf-8")
        subprocess.run(["git", "add", "world.py"], cwd=root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=root,
                       check=True, capture_output=True)

        # 强制推进 mtime（部分文件系统 mtime 精度低）
        head_file = tmp_path / ".git" / "HEAD"
        import time as _time
        os.utime(head_file, (_time.time() + 2, _time.time() + 2))

        info2 = probe_vcs_info(root)
        assert info2["head_sha"] != sha1
        assert info2["vcs_kind"] == "git"

    def test_probe_failure_returns_none(self, tmp_path):
        """subprocess 异常时降级 vcs_kind=none（不抛异常）。"""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                                encoding="utf-8")
        with patch(
            "callwarden.server.agent_protocol.subprocess.run",
            side_effect=OSError("git not found"),
        ):
            info = probe_vcs_info(str(tmp_path))
        assert info == {"vcs_kind": "none"}


# ============================================
# 2. build_refresh_message 透传 vcs 字段
# ============================================


class TestBuildRefreshVcsFields:
    """build_refresh_message 的 vcs_info 注入。"""

    def test_no_vcs_info_omits_fields(self):
        """不传 vcs_info 时报文不含 vcs 字段（向后兼容）。"""
        session = _make_session_with_epoch()
        msg = build_refresh_message(session, "ws_vcs", "a.py")
        assert "vcs_kind" not in msg
        assert "head_sha" not in msg
        assert msg["monotonic_seq"] == 1

    def test_vcs_info_injected(self):
        """传 vcs_info 时注入 vcs_kind/head_sha。"""
        session = _make_session_with_epoch()
        msg = build_refresh_message(
            session, "ws_vcs", "a.py",
            vcs_info={"vcs_kind": "git", "head_sha": "a" * 40},
        )
        assert msg["vcs_kind"] == "git"
        assert msg["head_sha"] == "a" * 40
        assert msg["rel_path"] == "a.py"

    def test_vcs_info_none_kind(self):
        """vcs_kind=none 也正确透传。"""
        session = _make_session_with_epoch()
        msg = build_refresh_message(
            session, "ws_vcs", "a.py",
            vcs_info={"vcs_kind": "none"},
        )
        assert msg["vcs_kind"] == "none"
        assert msg["head_sha"] == ""


# ============================================
# 3. send_refresh_to_daemon 透传 vcs 字段
# ============================================


class TestSendRefreshVcsFields:
    """send_refresh_to_daemon 的 vcs_info 透传。"""

    def test_small_file_carries_vcs_info(self, tmp_path):
        """小文件路径的 params 包含 vcs 字段。"""
        rpc = MagicMock()
        rpc.call.return_value = {"status": "committed"}
        session = _make_session_with_epoch()

        canonical = b"print('x')\n"
        send_refresh_to_daemon(
            daemon_rpc_client=rpc,
            agent_session=session,
            workspace_instance_id="ws_vcs",
            rel_path="a.py",
            abs_path=str(tmp_path / "a.py"),
            canonical_bytes=canonical,
            content_hash=hashlib.sha256(canonical).hexdigest(),
            vcs_info={"vcs_kind": "git", "head_sha": "b" * 40},
        )

        params = rpc.call.call_args[0][1]
        assert params["vcs_kind"] == "git"
        assert params["head_sha"] == "b" * 40

    def test_no_vcs_info_still_works(self, tmp_path):
        """不传 vcs_info 时行为不变（回归）。"""
        rpc = MagicMock()
        rpc.call.return_value = {"status": "committed"}
        session = _make_session_with_epoch()

        canonical = b"print('x')\n"
        send_refresh_to_daemon(
            daemon_rpc_client=rpc,
            agent_session=session,
            workspace_instance_id="ws_vcs",
            rel_path="a.py",
            abs_path=str(tmp_path / "a.py"),
            canonical_bytes=canonical,
            content_hash=hashlib.sha256(canonical).hexdigest(),
        )

        params = rpc.call.call_args[0][1]
        assert "vcs_kind" not in params
        assert rpc.call.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
