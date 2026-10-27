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
