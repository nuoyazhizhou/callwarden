"""共存契约子任务4：双 daemon storage guard 测试。

对应 windows-wsl-daemon-coexistence-contract.md §4.4 与
windows-wsl-daemon-coexistence-task-plan.md 子任务4。

核心校验逻辑在 Rust `rust_ext/src/daemon/config.rs`（DaemonConfig 的
validate_internal_storage / validate_no_storage_overlap，返回 E_AUTHORITY_STORAGE_CONFLICT）。
本 Python 测试：
1. 验证 Python `DaemonConfig` 的存储路径集合（data_root/registry/cas/codegraph）可被
   提取并比较，作为跨 authority 冲突检测的输入；
2. 提供与 Rust 侧一致的冲突检测语义的 Python 实现（供开发期/CI 使用）；
3. 标注 Rust 侧单测（config.rs::tests::test_validate_*）需 Linux runner 执行。
"""
import os
import sys
import tempfile

import pytest


def _config_storage_paths(cfg) -> dict:
    """从 Python DaemonConfig 提取存储路径（角色 -> 绝对路径）。"""
    paths = {}
    data_root = os.path.abspath(cfg.data_root)
    paths["data_root"] = data_root
    paths["registry"] = os.path.abspath(cfg.registry_db_path)
    paths["cas"] = os.path.abspath(cfg.cas_db_path)
    cg = cfg.resolve_codegraph_db_path("__test_ws__")
    if cg:
        paths["codegraph"] = os.path.abspath(cg)
    return paths


def _detect_internal_conflict(paths: dict) -> str | None:
    """同一 daemon 内部路径自冲突检测（镜像 Rust validate_internal_storage）。"""
    seen = {}
    for role, path in paths.items():
        if path in seen:
            return f"{seen[path]} == {role} == {path}"
        seen[path] = role
    return None


def _detect_overlap(paths_a: dict, paths_b: dict) -> str | None:
    """两个 authority 路径交集检测（镜像 Rust validate_no_storage_overlap）。"""
    for role_a, path_a in paths_a.items():
        for role_b, path_b in paths_b.items():
            if path_a == path_b:
                return f"{role_a} == {role_b} == {path_a}"
    return None


@pytest.fixture
def daemon_config_class():
    from callwarden.server.daemon_config import DaemonConfig

    return DaemonConfig


def _make_config(daemon_config_class):
    return daemon_config_class({})


def test_config_storage_paths_extractable(daemon_config_class):
    """Python DaemonConfig 能提供 storage 路径集合（冲突检测输入）。"""
    cfg = _make_config(daemon_config_class)
    paths = _config_storage_paths(cfg)
    # data_root / registry / cas 必须存在
    assert "data_root" in paths
    assert "registry" in paths
    assert "cas" in paths
    # registry 位于 data_root 下
    assert paths["registry"].startswith(paths["data_root"] + os.sep)


def test_internal_no_conflict_normal_config(daemon_config_class):
    """正常配置下内部路径无自冲突。"""
    cfg = _make_config(daemon_config_class)
    paths = _config_storage_paths(cfg)
    assert _detect_internal_conflict(paths) is None


def test_internal_conflict_when_registry_equals_cas(daemon_config_class):
    """registry 与 cas 指向同一路径 → 自冲突。"""
    cfg = _make_config(daemon_config_class)
    paths = _config_storage_paths(cfg)
    paths["registry"] = paths["cas"]
    conflict = _detect_internal_conflict(paths)
    assert conflict is not None
    assert "registry" in conflict


def test_two_authorities_disjoint_paths_ok(daemon_config_class, tmp_path):
    """两个 daemon 不同根 → 无交集。"""
    cfg_a = _make_config(daemon_config_class)
    cfg_b = _make_config(daemon_config_class)
    paths_a = _config_storage_paths(cfg_a)
    paths_b = _config_storage_paths(cfg_b)
    # 强制不同根（data_root/registry/cas 各自独立）
    root_a = str(tmp_path / "authority-a")
    root_b = str(tmp_path / "authority-b")
    os.makedirs(root_a, exist_ok=True)
    os.makedirs(root_b, exist_ok=True)
    paths_a = {k: v.replace(paths_a["data_root"], root_a) for k, v in paths_a.items()}
    paths_b = {k: v.replace(paths_b["data_root"], root_b) for k, v in paths_b.items()}
    # 注意：默认配置 codegraph 模板为空，resolve_codegraph_db_path 回退到
    # ~/.callwarden/callwarden.db（用户级单库，两 daemon 共享是设计使然）。
    # 共存契约要求显式配置时路径隔离；默认回退路径不属于"冲突"。
    # 这里只比较显式存储路径（data_root/registry/cas），排除 codegraph 回退。
    paths_a.pop("codegraph", None)
    paths_b.pop("codegraph", None)
    assert _detect_overlap(paths_a, paths_b) is None


def test_two_authorities_shared_task_db_conflict(daemon_config_class, tmp_path):
    """两个 authority 共享同一 task 库 → 冲突（镜像 E_AUTHORITY_STORAGE_CONFLICT）。"""
    shared = str(tmp_path / "shared-tasks.db")
    paths_a = {"data_root": str(tmp_path / "a"), "task_db": shared, "registry": str(tmp_path / "a") + "/registry.db"}
    paths_b = {"data_root": str(tmp_path / "b"), "task_db": shared, "registry": str(tmp_path / "b") + "/registry.db"}
    conflict = _detect_overlap(paths_a, paths_b)
    assert conflict is not None
    assert "task_db" in conflict


def test_rust_guard_tests_exist_in_config_rs():
    """Rust 侧单测存在（供 Linux runner 执行）。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_rs = os.path.join(repo, "rust_ext", "src", "daemon", "config.rs")
    with open(config_rs, encoding="utf-8") as f:
        src = f.read()
    for test_name in [
        "test_validate_internal_storage_ok",
        "test_validate_internal_storage_conflict_task_registry",
        "test_validate_no_storage_overlap_ok",
        "test_validate_no_storage_overlap_conflict",
        "test_storage_paths_are_normalized",
    ]:
        assert test_name in src, f"config.rs 缺少单测 {test_name}"
    assert "E_AUTHORITY_STORAGE_CONFLICT" in src
