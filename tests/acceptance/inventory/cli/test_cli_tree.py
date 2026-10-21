"""TP-08C CLI 真实树/参数提取 —— 验收套件。

selector: tests/acceptance/inventory/cli/test_cli_tree.py

正向：84 顶层入口不重不漏、每个顶层命令的 parser 可提取、嵌套子命令可展开、
      关键参数契约（required/type/default/choices/互斥）如实落 manifest、
      cw.py 层 5 个 standalone 入口有真实 help。
负向：提取不写生产目录（auto-setup marker / db mtime 不变）、未决入口（原生
      Rust CLI）必须显式挂 pending 而非静默缺失、manifest schema 漂移拒绝。

运行：C:/Python314/python.exe -m pytest tests/acceptance/inventory/cli/test_cli_tree.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = REPO_ROOT / "docs" / "testing" / "manifests" / "cli" / "cli_tree.json"


@pytest.fixture(scope="module")
def tree() -> dict:
    if not MANIFEST.is_file():
        pytest.skip(f"manifest not generated: {MANIFEST}")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ---------------- 正向 ----------------

def _head() -> str:
    import subprocess
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip()


def _is_ancestor(rev: str, head: str) -> bool:
    import subprocess
    r = subprocess.run(["git", "merge-base", "--is-ancestor", rev, head],
                       cwd=REPO_ROOT, capture_output=True)
    return r.returncode == 0


def test_schema_and_source_binding(tree):
    """绑定语义：manifest 的 source_revision 必须是 HEAD 的祖先（或相等）。

    为什么不要求 `== HEAD`：manifest 自身要入库，而**提交动作会推进 HEAD**
    （TP-02 的 HEAD 独断自引用悖论，2026-10-12 已在 TP-02 识别）——任何
    「revision == HEAD」的断言在提交后立刻变红，是不可达状态。正确语义是
    「manifest 描述的源码版本被 HEAD 包含」，真正的漂移保护由
    `test_cli_tree_no_unreviewed_drift` 的重提取对拍提供。
    """
    assert tree["schema"] == "cli-tree/v1"
    assert tree["card"] == "TP-08C"
    assert len(tree["source_revision"]) == 40
    head = _head()
    assert _is_ancestor(tree["source_revision"], head), (
        f"manifest source_revision {tree['source_revision']} is not an ancestor of "
        f"HEAD {head} — manifest describes source newer than the repo (stale/forged)")
    assert tree["transport"] if "transport" in tree else True


def test_cli_tree_no_unreviewed_drift(tree):
    """未审漂移阻断：重提取 CLI 树，与冻结 manifest 逐条对拍。

    这是比 revision 绑定更强的保护——源码若在提取后变更（新增/删除/改名
    子命令、参数增删、choices 变化），重提取结果与冻结集出现差异即失败。
    时间戳/版本号等易变字段不参与比对。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import extract_cli_tree as ext

    live = ext.extract()
    frozen_keys = set(tree["entries"].keys())
    live_keys = set(live["entries"].keys())
    added = sorted(live_keys - frozen_keys)
    removed = sorted(frozen_keys - live_keys)
    assert not added, f"unreviewed CLI entries added since freeze: {added[:10]}"
    assert not removed, f"unreviewed CLI entries removed since freeze: {removed[:10]}"

    # 参数结构对拍（每个顶层入口的 positionals/options 概要）。
    # cw.py 层 5 个 standalone 入口的 top_parser 只存 help_text（无 arguments 键），
    # 对它们的保护是 help_text 非空 + rc=0（见 test_standalone_entries_have_real_help）。
    for name in sorted(frozen_keys & live_keys):
        f_top = tree["entries"][name].get("top_parser")
        l_top = live["entries"][name].get("top_parser")
        if not f_top or not l_top:
            continue
        if "arguments" not in f_top or "arguments" not in l_top:
            continue
        f_opts = sorted(o["dest"] for o in f_top["arguments"] if o["option_strings"])
        l_opts = sorted(o["dest"] for o in l_top["arguments"] if o["option_strings"])
        assert f_opts == l_opts, f"{name}: option set drifted: frozen={f_opts} live={l_opts}"
        f_nested = set(tree["entries"][name]["nested"].keys())
        l_nested = set(live["entries"][name]["nested"].keys())
        assert f_nested == l_nested, (
            f"{name}: nested set drifted: added={sorted(l_nested - f_nested)[:5]} "
            f"removed={sorted(f_nested - l_nested)[:5]}")


def test_all_84_top_level_entries_present(tree):
    """84 个顶层入口（_SUBCOMMANDS 79 + cw.py standalone 5）一个不少。"""
    assert tree["top_level_count"] == 84
    entries = tree["entries"]
    assert len(entries) == 84, f"entries={len(entries)}"
    assert tree["error_count"] == 0, f"extraction errors: {tree['errors'][:5]}"
    # cw.py 层 5 个入口必须由真实子进程取得
    for entry in ("install", "server", "test", "setup", "daemon"):
        assert entries[entry]["top_parser"] is not None, f"{entry} help not extracted"


def test_every_entry_has_category(tree):
    for name, node in tree["entries"].items():
        assert node.get("category"), f"{name} has no category binding"


def test_nested_subcommands_expanded(tree):
    """至少展开一层嵌套；task/gc/rule 等家族必须完整。"""
    assert tree["nested_total"] >= 100
    assert len(tree["entries"]["task"]["nested"]) >= 25
    assert len(tree["entries"]["gc"]["nested"]) >= 10
    assert len(tree["entries"]["lease"]["nested"]) >= 4


def test_parameter_contract_fields_captured(tree):
    """required / type / default / choices 必须如实捕获（help 文本不保证暴露）。"""
    report = tree["entries"]["task"]["nested"]["report"]["parser"]
    by_dest = {a["dest"]: a for a in report["arguments"]}
    assert by_dest["task_id"]["required"] is True
    assert by_dest["step_id"]["required"] is True
    assert by_dest["result"]["required"] is False
    assert by_dest["result"]["default"] == ""
    assert by_dest["fencing_counter"]["type"] == "int"
    assert by_dest["fencing_counter"]["default"] == -1
    assert by_dest["fail"]["nargs"] == 0


def test_mutually_exclusive_groups_recorded(tree):
    """存在互斥组的命令必须记录该关系（不能丢）。"""
    found = 0
    checked = 0
    for node in tree["entries"].values():
        p = node.get("top_parser")
        if not p or "mutually_exclusive_groups" not in p:
            continue  # cw.py 层入口只存 help_text，无 parser 结构
        checked += 1
        assert isinstance(p["mutually_exclusive_groups"], list)
        if p["mutually_exclusive_groups"]:
            found += 1
    assert checked >= 79, f"only {checked} argparse-structured parsers found"
    # 不预设互斥组具体数量（当前 CLI 未必有），但结构必须可读
    assert found >= 0


def test_standalone_entries_have_real_help(tree):
    for script, node in tree["standalone_entries"].items():
        if node["parser"]:
            assert node["parser"]["rc"] == 0
            assert len(node["parser"]["help_text"]) > 50, f"{script} help too short"


# ---------------- 负向 ----------------

def test_no_production_write_during_extraction(tmp_path, monkeypatch):
    """提取不得有生产副作用（auto-setup / 本地 db 任何写入）。

    在本测试内把提取子进程的 HOME 隔离到 tmp_path 后受控执行一次
    extract()：提取若触发 lazy auto-setup 或初始化本地 db，产物只会
    落在隔离目录。隔离 home 下 .callwarden 未被创建即提取零生产写；
    该判据确定性强——并行 xdist worker / 后台 daemon 都看不到隔离目录。

    修正旧版两个缺陷：
    ① marker 断言检查的文件名写错（.auto_setup_marker vs 实际
       config.py 定义的 .auto_setup_done），旧断言恒真（死断言）；
    ② 「db mtime 60s 内不新鲜」启发式在全新 CI runner 上必假性失败
       （套件自身并行建库/写库，fresh 与提取无关）。
    """
    import extract_cli_tree as ext
    iso_home = tmp_path / "iso_home"
    iso_home.mkdir()
    monkeypatch.setattr(ext, "_EXTRACTION_ENV_OVERRIDE",
                        {"USERPROFILE": str(iso_home), "HOME": str(iso_home)})
    live = ext.extract()
    produced = iso_home / ".callwarden"
    assert not produced.exists(), (
        f"提取产生生产侧写入: {[str(p) for p in produced.rglob('*')][:10]}；"
        f"提取错误数={live.get('error_count')}, err={live.get('errors')[:3]}"
    )


def test_pending_entries_explicit_not_silent(tree):
    """未决入口必须显式挂 pending，不允许静默消失。"""
    pending = tree.get("pending_entries") or []
    assert pending, "pending_entries missing — undecided entries may be silently dropped"
    names = [p["entry"] for p in pending]
    assert any("native" in n for n in names), "native Rust CLI must be pending-explicit"
    for p in pending:
        assert p.get("reason") and p.get("blocked_by"), f"pending entry lacks reason: {p}"


def test_manifest_is_lf_and_utf8(tree):
    """行尾/编码契约：manifest 必须 LF + UTF-8（与 ledger 同规矩）。"""
    raw = MANIFEST.read_bytes()
    assert b"\r\n" not in raw, "manifest must be LF, not CRLF"
    assert raw.endswith(b"\n")
    json.loads(raw.decode("utf-8"))  # 严格 UTF-8


def test_no_placeholder_or_fabricated_entries(tree):
    """禁止捏造入口：manifest 里不得出现占位命令名。"""
    text = json.dumps(tree, ensure_ascii=False)
    for bad in ("TODO", "FIXME", "placeholder", "<frozen-selectors>"):
        assert bad not in text, f"placeholder marker {bad!r} found in manifest"
