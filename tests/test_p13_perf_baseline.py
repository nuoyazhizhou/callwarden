"""P13: perf 回归基线 —— 阶段耗时回归检测。

覆盖：
- _stage_timings 暴露到 CodeGraphDB 实例（build_full_graph 后可读）
- _load_perf_baseline() 容错（缺失/损坏 JSON）
- _print_stage_baseline_comparison() 检测回归（>1.5x 警告）
- _update_perf_baseline() 正确写入 JSON 结构
- --update-baseline CLI 参数存在
- REGRESSION_THRESHOLD = 1.5
"""
import importlib
import inspect
import json
import os
import sys

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# P13: _stage_timings 暴露测试
# ============================================

def test_stage_timings_exposed_after_build(tmp_path):
    """build_full_graph 后 db._stage_timings 应为 dict 并包含所有阶段。"""
    # 准备一个最小可解析文件
    src = tmp_path / "foo.py"
    src.write_text('def bar():\n    pass\n', encoding="utf-8")

    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("t", str(tmp_path), "测试")
    db.build_full_graph()

    timings = getattr(db, "_stage_timings", None)
    assert timings is not None, "build_full_graph 后应设置 _stage_timings"
    assert isinstance(timings, dict)

    # 关键阶段必须存在（值允许为 0）
    expected_keys = {
        "register", "parse", "symbol_write", "stdlib_import",
        "call_resolve_write", "depth", "fts_rebuild", "commit",
        "gc_archive", "total",
    }
    missing = expected_keys - set(timings.keys())
    assert not missing, f"缺少阶段: {missing}"

    # total 应为非负数，且 >= 各阶段之和的近似
    assert timings["total"] >= 0
    # 文件统计字段也应存在
    assert "files_total" in timings
    assert "files_parsed" in timings
    assert "files_unchanged" in timings
    db.close()


def test_stage_timings_unchanged_refresh(tmp_path):
    """二次增量刷新（无变化）后 _stage_timings 也应设置。"""
    src = tmp_path / "foo.py"
    src.write_text('def bar():\n    pass\n', encoding="utf-8")

    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("t", str(tmp_path), "测试")
    db.build_full_graph()
    db.build_full_graph(force=False)  # 二次增量

    timings = getattr(db, "_stage_timings", None)
    assert timings is not None, "增量刷新后也应设置 _stage_timings"
    # 全部 unchanged 时 files_parsed 应为 0
    assert timings["files_unchanged"] >= 1
    assert timings["files_parsed"] == 0
    db.close()


# ============================================
# P13: _load_perf_baseline 容错测试
# ============================================

def test_load_perf_baseline_missing_file(monkeypatch, tmp_path):
    """基线文件不存在时返回 {}。"""
    fake_path = str(tmp_path / "nonexistent_baseline.json")
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", fake_path)

    baseline = perf._load_perf_baseline()
    assert baseline == {}


def test_load_perf_baseline_corrupt_json(monkeypatch, tmp_path):
    """基线文件内容损坏时返回 {}。"""
    bad = tmp_path / "bad.json"
    bad.write_text("not a valid json {{{", encoding="utf-8")
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(bad))

    baseline = perf._load_perf_baseline()
    assert baseline == {}


def test_load_perf_baseline_valid(monkeypatch, tmp_path):
    """有效基线文件正常加载。"""
    good = tmp_path / "good.json"
    payload = {
        "firmware": {
            "stage_timings": {"total": 18.0, "parse": 12.0},
            "elapsed_sec": 18.0,
            "updated_at": "2026-07-08 12:00:00",
        }
    }
    good.write_text(json.dumps(payload), encoding="utf-8")
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(good))

    baseline = perf._load_perf_baseline()
    assert "firmware" in baseline
    assert baseline["firmware"]["stage_timings"]["parse"] == 12.0


# ============================================
# P13: 回归检测阈值测试
# ============================================

def test_regression_threshold_value():
    """REGRESSION_THRESHOLD 应为 1.5。"""
    perf = _import_perf_module()
    assert perf.REGRESSION_THRESHOLD == 1.5


def test_print_stage_baseline_comparison_no_regression(capsys, monkeypatch, tmp_path):
    """耗时 < 1.5x 时不打印回归警告。"""
    baseline = tmp_path / "bl.json"
    payload = {
        "firmware": {
            "stage_timings": {"parse": 10.0, "total": 20.0},
        }
    }
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(baseline))

    # 当前耗时 12s vs 基线 10s = 1.2x，未超阈值
    timings = {"parse": 12.0, "total": 22.0}
    perf._print_stage_baseline_comparison("firmware", timings)
    out = capsys.readouterr().out
    assert "✓" in out
    assert "回归" not in out


def test_print_stage_baseline_comparison_regression(capsys, monkeypatch, tmp_path):
    """耗时 > 1.5x 时打印回归警告。"""
    baseline = tmp_path / "bl.json"
    payload = {
        "firmware": {
            "stage_timings": {"parse": 10.0},
        }
    }
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(baseline))

    # 当前 20s vs 基线 10s = 2.0x，超阈值
    timings = {"parse": 20.0}
    perf._print_stage_baseline_comparison("firmware", timings)
    out = capsys.readouterr().out
    assert "回归" in out or "WARNING" in out


def test_print_stage_baseline_comparison_no_baseline(capsys, monkeypatch, tmp_path):
    """无基线时打印提示信息，不崩溃。"""
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(tmp_path / "none.json"))

    perf._print_stage_baseline_comparison("firmware", {"parse": 10.0})
    out = capsys.readouterr().out
    assert "无基线" in out


# ============================================
# P13: _update_perf_baseline 写入测试
# ============================================

def test_update_perf_baseline_writes_json(monkeypatch, tmp_path):
    """_update_perf_baseline 正确写入 stage_timings。"""
    out = tmp_path / "out_baseline.json"
    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(out))

    results = [
        {
            "repo": "firmware",
            "refresh": {
                "stage_timings": {"parse": 12.0, "total": 18.0},
                "elapsed_sec": 18.0,
            },
        },
        {
            "repo": "admin",
            "refresh": {
                "stage_timings": {"parse": 0.5, "total": 1.0},
                "elapsed_sec": 1.0,
            },
        },
        # 缺少 stage_timings 的结果应被跳过
        {"repo": "empty", "refresh": {}},
    ]
    perf._update_perf_baseline(results)

    assert out.exists(), "基线文件应被创建"
    with open(out, "r", encoding="utf-8") as f:
        saved = json.load(f)

    assert "firmware" in saved
    assert saved["firmware"]["stage_timings"]["parse"] == 12.0
    assert "admin" in saved
    assert "empty" not in saved


def test_update_perf_baseline_merges_existing(monkeypatch, tmp_path):
    """_update_perf_baseline 应保留未在本次结果中的旧基线。"""
    out = tmp_path / "merged.json"
    existing = {"legacy_repo": {"stage_timings": {"total": 99.0}}}
    out.write_text(json.dumps(existing), encoding="utf-8")

    perf = _import_perf_module()
    monkeypatch.setattr(perf, "PERF_BASELINE_PATH", str(out))

    results = [{"repo": "firmware", "refresh": {"stage_timings": {"total": 18.0}}}]
    perf._update_perf_baseline(results)

    with open(out, "r", encoding="utf-8") as f:
        saved = json.load(f)
    # 旧的保留，新的添加
    assert "legacy_repo" in saved
    assert "firmware" in saved


# ============================================
# P13: CLI 参数存在性测试
# ============================================

def test_perf_test_has_update_baseline_flag():
    """--update-baseline 参数应在 argparse 中定义。"""
    perf = _import_perf_module()
    src = inspect.getsource(perf.main)
    assert "--update-baseline" in src
    assert "args.update_baseline" in src


def test_perf_test_main_calls_update_baseline_when_flag_set():
    """args.update_baseline 为 True 时应调用 _update_perf_baseline。"""
    perf = _import_perf_module()
    src = inspect.getsource(perf.main)
    assert "_update_perf_baseline" in src


# ============================================
# 辅助函数
# ============================================

def _import_perf_module():
    """导入 tests/_perf_test.py 模块。

    由于文件名以下划线开头（非合法 Python 标识符），需用 importlib 加载。
    """
    perf_path = os.path.join(os.path.dirname(__file__), "_perf_test.py")
    spec = importlib.util.spec_from_file_location("_perf_test", perf_path)
    mod = importlib.util.module_from_spec(spec)
    # 避免执行 main()，只加载定义
    spec.loader.exec_module(mod)
    return mod
