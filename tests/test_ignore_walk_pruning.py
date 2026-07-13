"""IgnoreMatcher 递归扫描剪枝与子目录规则作用域测试。"""

import os

from pathlib import Path

from callwarden.analyzers.ignore_spec import IgnoreMatcher


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_root_ignore_rule_prunes_entire_subtree(tmp_path: Path):
    _write(tmp_path / ".callwardenignore", "ignored/\n")
    _write(tmp_path / "ignored" / "deep" / ".gitignore", "*.tmp\n")
    _write(tmp_path / "kept" / "deep" / ".gitignore", "*.cache\n")

    matcher = IgnoreMatcher(str(tmp_path))
    matcher.load_workspace_ignores()

    assert "ignored/deep" not in matcher.dir_rules
    assert "kept/deep" in matcher.dir_rules


def test_pruned_subtree_is_never_visited(tmp_path: Path, monkeypatch):
    _write(tmp_path / ".callwardenignore", "ignored/\n")
    _write(tmp_path / "ignored" / "deep" / ".gitignore", "*.tmp\n")
    _write(tmp_path / "kept" / "deep" / ".gitignore", "*.cache\n")

    real_walk = os.walk
    visited = []

    def tracking_walk(root, *args, **kwargs):
        for current, dirs, files in real_walk(root, *args, **kwargs):
            visited.append(Path(current).relative_to(tmp_path).as_posix())
            yield current, dirs, files

    monkeypatch.setattr("callwarden.analyzers.ignore_spec.os.walk", tracking_walk)

    matcher = IgnoreMatcher(str(tmp_path))
    matcher.load_workspace_ignores()

    assert "kept/deep" in visited
    assert not any(path == "ignored" or path.startswith("ignored/") for path in visited)


def test_current_directory_rules_prune_children_before_descent(tmp_path: Path):
    _write(tmp_path / "src" / ".gitignore", "generated/\n")
    _write(tmp_path / "src" / "generated" / "deep" / ".gitignore", "*.tmp\n")
    _write(tmp_path / "src" / "kept" / ".gitignore", "*.cache\n")

    matcher = IgnoreMatcher(str(tmp_path))
    matcher.load_workspace_ignores()

    assert matcher.is_ignored("src/generated/output.py")
    assert "src/generated/deep" not in matcher.dir_rules
    assert "src/kept" in matcher.dir_rules


def test_reincluded_parent_directory_is_traversed(tmp_path: Path):
    _write(tmp_path / ".callwardenignore", "ignored/\n!ignored/\n")
    _write(tmp_path / "ignored" / "deep" / ".gitignore", "*.tmp\n")

    matcher = IgnoreMatcher(str(tmp_path))
    matcher.load_workspace_ignores()

    assert not matcher.is_ignored("ignored", is_dir=True)
    assert "ignored/deep" in matcher.dir_rules


def test_child_negation_does_not_reinclude_ignored_parent(tmp_path: Path):
    _write(tmp_path / ".callwardenignore", "ignored/\n!ignored/keep/\n")
    _write(tmp_path / "ignored" / "keep" / ".gitignore", "*.tmp\n")

    matcher = IgnoreMatcher(str(tmp_path))
    matcher.load_workspace_ignores()

    assert matcher.is_ignored("ignored", is_dir=True)
    assert "ignored/keep" not in matcher.dir_rules
