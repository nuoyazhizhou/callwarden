#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workspace 身份判定回归：同一物理目录不得重复注册。

背景：task-DB `workspaces` 表的旧查重是
`SELECT ... WHERE name = ? OR root_path = ?`，查询侧用 `norm_path`，
写入侧落原始 `self.workspace_root`，两侧格式不一致 ⇒ 同一仓库被反复
注册成 ws1/ws5/ws6 多行（1/5/6 实际是同一个工作区）。

本文件锁定修好后的不变量：
1. `workspace_identity_key` 对不同写法（反斜杠/尾斜杠/盘符大小写）收敛到同一键；
2. `register_workspace` 与 `_init_workspace` 按身份键查重，同目录只留一行。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.config import workspace_identity_key  # noqa: E402


class TestWorkspaceIdentityKey(unittest.TestCase):
    def test_slash_and_trailing_slash_converge(self):
        """正/反斜杠与尾斜杠写法必须归一到同一键。"""
        base = os.path.join(tempfile.gettempdir(), "cw_identity_probe")
        self.assertEqual(
            workspace_identity_key(base + os.sep),
            workspace_identity_key(base),
        )
        self.assertEqual(
            workspace_identity_key(base.replace(os.sep, "/")),
            workspace_identity_key(base),
        )

    @unittest.skipUnless(os.name == "nt", "Windows 文件系统大小写不敏感")
    def test_windows_case_insensitive(self):
        """Windows 下盘符与路径大小写不影响身份键。"""
        base = os.path.join(tempfile.gettempdir(), "cw_identity_probe")
        self.assertEqual(
            workspace_identity_key(base.upper()),
            workspace_identity_key(base.lower()),
        )

    def test_empty_path(self):
        self.assertEqual(workspace_identity_key(""), "")


class TestWorkspaceRegistrationDedupe(unittest.TestCase):
    def setUp(self):
        from callwarden.db.db import CodeGraphDB

        self.tmpdir = tempfile.mkdtemp(prefix="cw_ws_identity_")
        self.db_path = os.path.join(self.tmpdir, "identity.db")
        self.db = CodeGraphDB(db_path=self.db_path, workspace_root=self.tmpdir)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _workspace_rows(self, root_key: str):
        rows = self.db.conn.execute(
            "SELECT id, root_path FROM workspaces"
        ).fetchall()
        return [r for r in rows if workspace_identity_key(r["root_path"] or "") == root_key]

    def test_register_same_root_different_spelling_returns_same_id(self):
        """同目录的三种写法应复用同一 workspace id，不新增行。"""
        root = self.tmpdir
        key = workspace_identity_key(root)
        first = self.db.register_workspace("identity-probe", root)
        second = self.db.register_workspace("identity-probe-2", root + os.sep)
        third = self.db.register_workspace("identity-probe-3", root.replace(os.sep, "/"))

        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(len(self._workspace_rows(key)), 1)

    def test_init_workspace_reuses_row_for_same_root(self):
        """`_init_workspace` 重复初始化不得新增同 root 行。"""
        from callwarden.db.db import CodeGraphDB

        key = workspace_identity_key(self.tmpdir)
        before = len(self._workspace_rows(key))
        for _ in range(3):
            other = CodeGraphDB(
                db_path=self.db_path,
                workspace_root=self.tmpdir + os.sep,
            )
            try:
                other._init_workspace()
            finally:
                other.close()
        self.assertEqual(len(self._workspace_rows(key)), before)


# ---------------------------------------------------------------------------
# C-08（REQ-04）：仓库级身份三级派生矩阵 + 跨语言锚（Python ↔ Rust 互锁）
#
# 与 Rust `rust_ext/src/daemon/workspace.rs` 的 `#[cfg(test)]` 矩阵同格：
# 同一固定输入向量在 Python / Rust 两侧必须产生同一 16 hex id。Rust 侧期望
# 值以常量固化在本文件（不在 pytest 内跑 cargo），避免测试环境依赖 Rust
# 工具链。
#
# 覆盖 `derive_workspace_instance_id`、`normalize_remote_url`、
# `_normalize_identity_root`、`_workspace_snapshot_metadata`。
# ---------------------------------------------------------------------------
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from callwarden.server.daemon_client import (  # noqa: E402
    _normalize_identity_root,
    _workspace_snapshot_metadata,
    derive_workspace_instance_id,
    normalize_remote_url,
)

# Rust workspace.rs 测试固化的跨语言锚向量（owner_uid = "1000"）。
RUST_ANCHOR_TIER1 = "b85559eb88383d60"  # sha256("1000|remote:github.com/u/r")[:16]
RUST_ANCHOR_TIER2 = "a7ebc654ca6a7d8b"  # sha256("1000|root:/var/hr/main")[:16]
RUST_ANCHOR_TIER3 = "3bde7a047470a6ef"  # sha256("1000|/var/hr")[:16]


class TestCrossLanguageAnchors:
    """固定向量：Python 与 Rust 必须逐字一致。"""

    def test_tier1_remote_anchor(self):
        assert (
            derive_workspace_instance_id(
                "/whatever/checkout",
                git_remote_url="https://github.com/u/r.git",
                owner_uid="1000",
            )
            == RUST_ANCHOR_TIER1
        )

    def test_tier2_main_root_anchor(self):
        assert (
            derive_workspace_instance_id(
                "/tmp/wt",
                git_remote_url="",
                git_main_root="/var/hr/main",
                owner_uid="1000",
            )
            == RUST_ANCHOR_TIER2
        )

    def test_tier3_self_root_anchor(self):
        assert (
            derive_workspace_instance_id(
                "/var/hr",
                git_remote_url="",
                git_main_root="",
                owner_uid="1000",
            )
            == RUST_ANCHOR_TIER3
        )


class TestRemoteNormalization:
    """remote 书写变体全部收敛到同一身份键。"""

    VARIANTS = [
        "https://github.com/u/r.git",
        "https://GitHub.com/U/R.git",
        "https://github.com/u/r",
        "https://github.com/u/r/",
        "http://github.com/u/r.git",
        "ssh://git@github.com/u/r.git",
        "git@github.com:u/r.git",
        "git@github.com:u/r",
        "https://token:secret@github.com/u/r.git",
    ]

    def test_all_variants_same_identity(self):
        ids = {
            derive_workspace_instance_id("/tmp/x", git_remote_url=url, owner_uid="1000")
            for url in self.VARIANTS
        }
        assert len(ids) == 1, f"remote 变体未收敛: {ids}"

    def test_credentials_stripped(self):
        norm = normalize_remote_url("https://token:secret@github.com/u/r.git")
        assert norm == "github.com/u/r"
        assert "token" not in norm
        assert "secret" not in norm

    def test_scheme_not_part_of_identity(self):
        assert normalize_remote_url("https://github.com/u/r.git") == normalize_remote_url(
            "ssh://git@github.com/u/r.git"
        )

    @pytest.mark.parametrize(
        "url",
        ["", "   ", "file:///var/hr/repo", "not-a-url", "git@host-only"],
    )
    def test_non_identity_forms_return_none(self, url):
        assert normalize_remote_url(url) is None

    def test_different_remotes_differ(self):
        a = derive_workspace_instance_id(
            "/tmp/x", git_remote_url="https://github.com/u/a.git", owner_uid="1000"
        )
        b = derive_workspace_instance_id(
            "/tmp/x", git_remote_url="https://github.com/u/b.git", owner_uid="1000"
        )
        assert a != b


class TestRootNormalization:
    """root 书写变体：`\\\\?\\` / 分隔符 / 尾斜杠 /（Windows）大小写。"""

    def test_verbatim_prefix_same_identity(self):
        a = derive_workspace_instance_id("C:\\repo", owner_uid="1000")
        b = derive_workspace_instance_id("\\\\?\\C:\\repo", owner_uid="1000")
        assert a == b

    def test_separator_variants_same_identity(self):
        a = derive_workspace_instance_id("C:/repo/sub", owner_uid="1000")
        b = derive_workspace_instance_id("C:\\repo\\sub", owner_uid="1000")
        assert a == b

    def test_trailing_slash_same_identity(self):
        a = derive_workspace_instance_id("C:/repo/", owner_uid="1000")
        b = derive_workspace_instance_id("C:/repo", owner_uid="1000")
        assert a == b

    @pytest.mark.skipif(os.name != "nt", reason="Windows NTFS 大小写不敏感")
    def test_case_variants_same_identity_on_windows(self):
        assert _normalize_identity_root("C:\\Repo") == _normalize_identity_root("c:\\repo")
        assert derive_workspace_instance_id(
            "C:\\Repo", owner_uid="1000"
        ) == derive_workspace_instance_id("c:\\repo", owner_uid="1000")

    def test_different_roots_without_remote_differ(self):
        a = derive_workspace_instance_id("/var/hr/a", owner_uid="1000")
        b = derive_workspace_instance_id("/var/hr/b", owner_uid="1000")
        assert a != b

    def test_main_root_preferred_over_self_root(self):
        tier2 = derive_workspace_instance_id(
            "/tmp/wt", git_main_root="/var/hr/main", owner_uid="1000"
        )
        tier3 = derive_workspace_instance_id("/tmp/wt", owner_uid="1000")
        assert tier2 != tier3
        assert tier2 == RUST_ANCHOR_TIER2

    def test_legacy_no_owner_keeps_historical_payload(self):
        # A-01 兼容：旧调用方不传 owner_uid 时保持历史公式 sha256(norm_root)。
        import hashlib

        expected = hashlib.sha256("/var/hr".encode("utf-8")).hexdigest()[:16]
        assert derive_workspace_instance_id("/var/hr") == expected


def _git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
class TestSnapshotMetadata:
    """真实 git 仓 + worktree / 非 git 目录。"""

    @pytest.fixture()
    def repo_with_worktree(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _git("init", "-b", "main", cwd=repo).returncode == 0
        _git("config", "user.email", "t@example.com", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git("remote", "add", "origin", "https://github.com/u/r.git", cwd=repo)
        (repo / "a.txt").write_text("hello", encoding="utf-8")
        _git("add", "a.txt", cwd=repo)
        assert _git("commit", "-m", "init", cwd=repo).returncode == 0

        wt = tmp_path / "wt"
        result = _git("worktree", "add", "-b", "feat", str(wt), cwd=repo)
        if result.returncode != 0:
            pytest.skip(f"git worktree add 失败: {result.stderr}")
        return repo, wt

    def test_worktree_metadata_has_main_root(self, repo_with_worktree):
        repo, wt = repo_with_worktree
        meta = _workspace_snapshot_metadata(str(wt))
        assert meta.get("git_remote_url") == "https://github.com/u/r.git"
        assert "git_main_root" in meta
        assert Path(meta["git_main_root"]).resolve() == repo.resolve()

    def test_worktree_and_main_share_identity(self, repo_with_worktree):
        repo, wt = repo_with_worktree
        main_meta = _workspace_snapshot_metadata(str(repo))
        wt_meta = _workspace_snapshot_metadata(str(wt))
        assert derive_workspace_instance_id(
            str(wt),
            wt_meta.get("git_remote_url", ""),
            wt_meta.get("git_main_root", ""),
            owner_uid="1000",
        ) == derive_workspace_instance_id(
            str(repo),
            main_meta.get("git_remote_url", ""),
            main_meta.get("git_main_root", ""),
            owner_uid="1000",
        )

    def test_non_git_dir_metadata_is_empty_and_safe(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        meta = _workspace_snapshot_metadata(str(plain))
        assert "git_main_root" not in meta
        assert "git_remote_url" not in meta

    def test_missing_dangling_worktree_is_safe(self, tmp_path):
        # 悬空路径（不存在）→ git 全部失败，metadata 返回空 dict 且不抛异常。
        meta = _workspace_snapshot_metadata(str(tmp_path / "does-not-exist"))
        assert "git_main_root" not in meta
