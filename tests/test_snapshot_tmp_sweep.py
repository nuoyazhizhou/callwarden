"""T-1785824926483: `.cwsnap.*.tmp` 陈旧残留清扫逻辑测试

验证 db/db_base.py `_sweep_stale_snapshot_tmp`：
- 新鲜 .tmp 保留（可能仍在写入）
- 超过 max_age 的陈旧 .tmp 删除
- 不匹配命名模式的文件保留
- 清扫失败 fail-soft（不阻断 GraphStore 加载）
"""
from __future__ import annotations

import os
import tempfile
import time

from callwarden.db.db_base import CodeGraphBase


def _make_instance() -> CodeGraphBase:
    """构造不连库的 CodeGraphBase 实例（仅测方法本身，避免 DB 副作用）。"""
    return object.__new__(CodeGraphBase)


def test_sweep_removes_stale_tmp_only():
    snap_dir = tempfile.mkdtemp(prefix="sweep_test_")
    snap_path = os.path.join(snap_dir, "test.db.cwsnap")
    obj = _make_instance()
    fresh = f"{snap_path}.100.200.abc.tmp"
    stale = f"{snap_path}.300.400.def.tmp"
    other = os.path.join(snap_dir, "unrelated.tmp")
    try:
        for p in (fresh, stale, other):
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
        now = time.time()
        os.utime(fresh, (now, now))
        os.utime(stale, (now - 7200, now - 7200))
        os.utime(other, (now - 7200, now - 7200))

        obj._sweep_stale_snapshot_tmp(snap_path, max_age_seconds=3600)

        assert os.path.exists(fresh), "新鲜 .tmp 不应被清扫"
        assert not os.path.exists(stale), "陈旧 .tmp 必须被清扫"
        assert os.path.exists(other), "无关文件必须保留"
    finally:
        for p in (fresh, stale, other):
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(snap_dir)


def test_sweep_keeps_recent_tmp():
    snap_dir = tempfile.mkdtemp(prefix="sweep_test_")
    snap_path = os.path.join(snap_dir, "test.db.cwsnap")
    obj = _make_instance()
    recent = f"{snap_path}.1.2.xyz.tmp"
    try:
        with open(recent, "w", encoding="utf-8") as f:
            f.write("x")
        now = time.time()
        os.utime(recent, (now - 600, now - 600))  # 10 分钟内，未过期

        obj._sweep_stale_snapshot_tmp(snap_path, max_age_seconds=3600)

        assert os.path.exists(recent), "未过期的 .tmp 必须保留"
    finally:
        if os.path.exists(recent):
            os.remove(recent)
        os.rmdir(snap_dir)


def test_sweep_fail_soft_when_dir_missing():
    """snap_path 所在目录不存在时清扫应静默成功（fail-soft，不阻断加载）。"""
    obj = _make_instance()
    missing = os.path.join(
        tempfile.gettempdir(), "no_such_dir_sweep_xyz", "x.db.cwsnap")
    # 不抛异常即通过
    obj._sweep_stale_snapshot_tmp(missing, max_age_seconds=3600)
