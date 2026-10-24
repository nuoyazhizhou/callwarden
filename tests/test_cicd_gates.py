"""cicd 门禁脚本单测：check_selector_manifest + check_ci_gates。

缺陷回归锚（2026-10-24 三次独立实证）：pytest 9 的
``pytest <path> --collect-only --no-header``（非 -q 模式）stdout 尾部
仍有一行汇总行「114 tests collected in 0.43s」，被 ``>`` 捕获进
collected 文本；``check_selector_manifest._load()`` 旧实现只滤空行与
``#`` 注释，不滤非 nodeid 行 ⇒ 汇总行被当成未登记 selector ⇒
``unregistered_new=["114 tests collected in 0.43s"]``、``ok=false``、
exit 2 恒红（主理人本机复现：collected=115 vs manifest=114），作为前置
反篡改门的 Gate 1/2 永真，其后的 check_ci_gates（JUnit 判定）永远
到不了。根因：两个门禁脚本零单测，缺陷存活至 CI 实跑才暴露。

本文件锁定修复后的行为——``_load()`` 只保留含 ``::`` 的行（pytest
nodeid 标准形态 ``path/to/test_x.py::Class::test_y``），该规则天然
同时排除汇总行（``^\\d+ tests? collected``）、-q 模式计数行
（``tests/x.py: 4``，无 ``::``）、空行与注释行；并对两个门禁脚本做
正向/负向/边界覆盖，证明修复没有把门改瞎。

全部 fixture 用 tmp_path 临时构建，不落仓库；不走 git 基线路径
（baseline 比对由 workflow 实跑承担，单测保持 hermetic）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # 兼容无 PYTHONPATH 的直接 pytest 调用
    sys.path.insert(0, str(_REPO_ROOT))

from cicd import check_ci_gates, check_selector_manifest  # noqa: E402

# ---------------------------------------------------------------------------
# 共用 fixture 构建助手
# ---------------------------------------------------------------------------

_SEL_NODEIDS = [
    "tests/acceptance/domains/editing/test_editing_revert.py::test_e1_propose_lifecycle",
    "tests/acceptance/domains/editing/test_editing_revert.py::test_e2_revert_semantics",
    "tests/acceptance/domains/storage/test_storage_recovery.py::test_s3_gc_surface",
]

# pytest 9 非 -q 模式 stdout 尾部的真实汇总行（本缺陷的污染源）
_SUMMARY_LINE = "114 tests collected in 0.43s"
# pytest 9 -q 模式每文件计数行（无 `::`，天然被 nodeid 形态过滤排除）
_Q_COUNT_LINE = "tests/acceptance/domains/editing/test_editing_revert.py: 4"


def _write(path: Path, text: str) -> Path:
    """LF 落盘（newline="\\n" 禁 Windows CRLF 翻译），返回路径。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _manifest_text(nodeids) -> str:
    return "# 测试清单注释\n" + "\n".join(nodeids) + "\n"


def _collected_clean(nodeids) -> str:
    """纯净 collected 文本（仅 nodeid 每行一条）。"""
    return "\n".join(nodeids) + "\n"


def _run_manifest_gate(collected: Path, manifest: Path) -> int:
    return check_selector_manifest.main(
        ["--collected", str(collected), "--manifest", str(manifest)])


# ---------------------------------------------------------------------------
# check_selector_manifest：正向（本缺陷回归锚）
# ---------------------------------------------------------------------------

def test_summary_line_and_noise_filtered_exit_ok(tmp_path):
    """回归锚：collected 含汇总行 + 注释 + 空行 + 重复噪声行，与 manifest
    一致 ⇒ exit 0 / ok=true。

    修复前（_load 只滤空行/注释）：汇总行进入 unregistered_new ⇒ exit 2
    恒红（stats.collected=4 而非 3）。注意「重复行」指重复的**噪声行**
    （汇总行/空行可重复出现且不影响判定）；重复 nodeid 是另一个拒绝
    场景，见 test_duplicate_nodeid_rejected。
    """
    collected = _write(tmp_path / "collected.txt",
                       "# 收集输出头注释\n"
                       "\n"
                       + "\n".join(_SEL_NODEIDS) + "\n"
                       "\n"
                       + _SUMMARY_LINE + "\n"
                       "\n"
                       + _SUMMARY_LINE + "\n"
                       "# 尾部注释\n")
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))

    rc = _run_manifest_gate(collected, manifest)
    assert rc == check_selector_manifest.EXIT_OK

    verdict = check_selector_manifest.evaluate(
        str(collected), str(manifest))
    assert verdict["ok"] is True
    assert verdict["stats"]["collected"] == len(_SEL_NODEIDS)
    assert verdict["stats"]["manifest"] == len(_SEL_NODEIDS)
    assert verdict["unregistered_new"] == []
    assert verdict["deleted_or_renamed"] == []
    assert verdict["duplicated_in_collected"] == []


def test_q_mode_count_line_not_counted(tmp_path):
    """负向 C：-q 模式计数行（tests/x.py: 4，无 ::）不计入；
    其余与 manifest 一致 ⇒ 结果仍正确（exit 0 / ok=true）。"""
    collected = _write(tmp_path / "collected.txt",
                       _Q_COUNT_LINE + "\n"
                       + "\n".join(_SEL_NODEIDS) + "\n"
                       + _Q_COUNT_LINE + "\n")
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))

    rc = _run_manifest_gate(collected, manifest)
    assert rc == check_selector_manifest.EXIT_OK

    verdict = check_selector_manifest.evaluate(
        str(collected), str(manifest))
    assert verdict["ok"] is True
    assert verdict["stats"]["collected"] == len(_SEL_NODEIDS)


# ---------------------------------------------------------------------------
# check_selector_manifest：负向（证明修复没把门改瞎）
# ---------------------------------------------------------------------------

def test_manifest_missing_nodeid_rejected(tmp_path):
    """负向 A：manifest 少登记一条 ⇒ exit 2，unregistered_new 精确列出
    该 nodeid（源码有、清单未登记）。"""
    collected = _write(tmp_path / "collected.txt", _collected_clean(_SEL_NODEIDS))
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS[:2]))

    rc = _run_manifest_gate(collected, manifest)
    assert rc == check_selector_manifest.EXIT_INCONSISTENT

    verdict = check_selector_manifest.evaluate(
        str(collected), str(manifest))
    assert verdict["ok"] is False
    assert verdict["unregistered_new"] == [_SEL_NODEIDS[2]]


def test_collected_missing_nodeid_rejected(tmp_path):
    """负向（测试被删/改名方向）：collected 少一条 ⇒ exit 2，
    deleted_or_renamed 精确列出该 nodeid（manifest 有、源码没有）。"""
    collected = _write(tmp_path / "collected.txt", _collected_clean(_SEL_NODEIDS[:2]))
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))

    rc = _run_manifest_gate(collected, manifest)
    assert rc == check_selector_manifest.EXIT_INCONSISTENT

    verdict = check_selector_manifest.evaluate(
        str(collected), str(manifest))
    assert verdict["ok"] is False
    assert verdict["deleted_or_renamed"] == [_SEL_NODEIDS[2]]


def test_extra_fake_nodeid_rejected(tmp_path):
    """负向 B：collected 多一条假 nodeid ⇒ exit 2 且 unregistered_new
    非空——证明节点形态过滤没有把门改成无条件放行。"""
    fake = "tests/acceptance/domains/ghost.py::test_not_registered"
    collected = _write(tmp_path / "collected.txt",
                       _collected_clean(_SEL_NODEIDS + [fake]))
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))

    rc = _run_manifest_gate(collected, manifest)
    assert rc == check_selector_manifest.EXIT_INCONSISTENT

    verdict = check_selector_manifest.evaluate(
        str(collected), str(manifest))
    assert verdict["ok"] is False
    assert verdict["unregistered_new"] == [fake]


# ---------------------------------------------------------------------------
# check_selector_manifest：边界
# ---------------------------------------------------------------------------

def test_empty_comment_only_or_summary_only_input_exit_usage(tmp_path):
    """边界：空文件 / 纯注释 / 仅汇总行（无任何 nodeid）⇒ ManifestError
    ⇒ exit 64（输入用法错，不是 2）。"""
    empty = _write(tmp_path / "empty.txt", "")
    comments = _write(tmp_path / "comments.txt", "# 只有注释\n\n# 仍无 nodeid\n")
    summary_only = _write(tmp_path / "summary_only.txt", "\n" + _SUMMARY_LINE + "\n")
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))

    for bad in (empty, comments, summary_only):
        rc = _run_manifest_gate(bad, manifest)
        assert rc == check_selector_manifest.EXIT_USAGE, bad
        with pytest.raises(check_selector_manifest.ManifestError):
            check_selector_manifest.evaluate(str(bad), str(manifest))


def test_missing_input_file_exit_usage(tmp_path):
    """边界：输入文件不存在 ⇒ exit 64。"""
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))
    rc = _run_manifest_gate(tmp_path / "no_such_collected.txt", manifest)
    assert rc == check_selector_manifest.EXIT_USAGE


def test_duplicate_nodeid_rejected(tmp_path):
    """边界：collected 或 manifest 自身重复 nodeid ⇒ duplicated 检出、
    exit 2（幂等键拒重）。"""
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))
    dup_collected = _write(tmp_path / "collected_dup.txt",
                           _collected_clean(_SEL_NODEIDS + [_SEL_NODEIDS[0]]))

    rc = _run_manifest_gate(dup_collected, manifest)
    assert rc == check_selector_manifest.EXIT_INCONSISTENT
    verdict = check_selector_manifest.evaluate(
        str(dup_collected), str(manifest))
    assert verdict["ok"] is False
    assert verdict["duplicated_in_collected"] == [_SEL_NODEIDS[0]]

    clean_collected = _write(tmp_path / "collected.txt",
                             _collected_clean(_SEL_NODEIDS))
    dup_manifest = _write(tmp_path / "manifest_dup.txt",
                          _manifest_text(_SEL_NODEIDS + [_SEL_NODEIDS[1]]))
    rc2 = _run_manifest_gate(clean_collected, dup_manifest)
    assert rc2 == check_selector_manifest.EXIT_INCONSISTENT
    verdict2 = check_selector_manifest.evaluate(
        str(clean_collected), str(dup_manifest))
    assert verdict2["ok"] is False
    assert verdict2["duplicated_in_manifest"] == [_SEL_NODEIDS[1]]


def test_cli_module_subprocess_roundtrip(tmp_path):
    """端到端：``python -m cicd.check_selector_manifest`` 真实子进程，
    汇总行污染 ⇒ exit 0/ok=true；manifest 删一条 ⇒ exit 2/ok=false
    （锁定 CLI 接线与退出码，不只 evaluate()）。"""
    collected = _write(tmp_path / "collected.txt",
                       "\n".join(_SEL_NODEIDS) + "\n" + _SUMMARY_LINE + "\n")
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_SEL_NODEIDS))
    manifest_minus = _write(tmp_path / "manifest_minus.txt",
                            _manifest_text(_SEL_NODEIDS[:2]))
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}

    ok_run = subprocess.run(
        [sys.executable, "-m", "cicd.check_selector_manifest",
         "--collected", str(collected), "--manifest", str(manifest)],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), env=env)
    assert ok_run.returncode == 0
    assert json.loads(ok_run.stdout)["ok"] is True

    neg_run = subprocess.run(
        [sys.executable, "-m", "cicd.check_selector_manifest",
         "--collected", str(collected), "--manifest", str(manifest_minus)],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), env=env)
    assert neg_run.returncode == 2
    neg_verdict = json.loads(neg_run.stdout)
    assert neg_verdict["ok"] is False
    assert neg_verdict["unregistered_new"] == [_SEL_NODEIDS[2]]


# ---------------------------------------------------------------------------
# check_ci_gates：JUnit fixture（tmp_path 临时构建，不落仓库）
# ---------------------------------------------------------------------------

_CI_NODEIDS = [
    "tests/test_cicd_gates.py::test_one",
    "tests/test_cicd_gates.py::test_two",
]


def _write_junit(path: Path, cases: str, suite_errors: str = "0") -> Path:
    return _write(path,
                  '<?xml version="1.0" encoding="utf-8"?>\n'
                  '<testsuites>\n'
                  f'  <testsuite name="pytest" errors="{suite_errors}" '
                  'failures="0" skipped="0" tests="2">\n'
                  + cases +
                  '  </testsuite>\n'
                  '</testsuites>\n')


def _case(name: str, body: str = "") -> str:
    """一个 <testcase>；无 body 时自闭合，有 body（failure/error/skipped）
    时展开为开闭标签对，避免生成畸形 XML。"""
    head = (f'    <testcase classname="tests.test_cicd_gates" '
            f'name="{name}" file="tests/test_cicd_gates.py"')
    if body:
        return f"{head}>\n      {body}\n    </testcase>\n"
    return f"{head}/>\n"


_PASS_CASES = _case("test_one") + _case("test_two")


def _run_ci_gate(junit: Path, manifest: Path, expect_revision: str = "") -> int:
    argv = ["--junit", str(junit), "--manifest", str(manifest)]
    if expect_revision:
        argv += ["--expect-revision", expect_revision]
    return check_ci_gates.main(argv)


def test_ci_gates_all_pass_ok(tmp_path):
    """check_ci_gates ①：JUnit 全 pass 且与 manifest 一致 ⇒ ok。"""
    junit = _write_junit(tmp_path / "junit.xml", _PASS_CASES)
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_CI_NODEIDS))

    rc = _run_ci_gate(junit, manifest)
    assert rc == check_ci_gates.EXIT_OK

    verdict = check_ci_gates.evaluate([str(junit)], str(manifest))
    assert verdict["ok"] is True
    assert verdict["failures"] == [] and verdict["errors"] == []
    assert verdict["missing"] == [] and verdict["unexpected"] == []


def test_ci_gates_failure_rejected(tmp_path):
    """check_ci_gates ②：含 FAIL ⇒ exit 2，failures 精确列出。"""
    cases = _case("test_one") + _case(
        "test_two",
        '<failure message="assert 1 == 2" type="AssertionError">boom</failure>')
    junit = _write_junit(tmp_path / "junit.xml", cases)
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_CI_NODEIDS))

    rc = _run_ci_gate(junit, manifest)
    assert rc == check_ci_gates.EXIT_GATE_FAILED

    verdict = check_ci_gates.evaluate([str(junit)], str(manifest))
    assert verdict["ok"] is False
    assert verdict["failures"] == ["tests/test_cicd_gates.py::test_two"]


def test_ci_gates_collection_error_rejected(tmp_path):
    """check_ci_gates ③：collection error 形态 ⇒ 拒（§8：collection/中止
    阻断），collection_errors 非空。"""
    cases = _case(
        "test_collect",
        '<error message="collection error" type="collection">'
        'ImportError while collecting</error>')
    junit = _write_junit(tmp_path / "junit.xml", cases, suite_errors="1")
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_CI_NODEIDS))

    rc = _run_ci_gate(junit, manifest)
    assert rc == check_ci_gates.EXIT_GATE_FAILED

    verdict = check_ci_gates.evaluate([str(junit)], str(manifest))
    assert verdict["ok"] is False
    assert verdict["collection_errors"]


def test_ci_gates_skip_and_xfail_rejected(tmp_path):
    """check_ci_gates ④：必需适用 case 存在 SKIP/XFAIL ⇒ 按规则拒，
    skipped_required 精确列出（skip 率本身仅辅助，此处是硬门禁）。"""
    cases = (_case("test_one")
             + _case("test_two", '<skipped type="pytest.skip" message="need net"/>')
             + _case("test_skip_extra",
                     '<skipped type="pytest.xfail" message="known bad"/>'))
    nodeids = _CI_NODEIDS + ["tests/test_cicd_gates.py::test_skip_extra"]
    junit = _write_junit(tmp_path / "junit.xml", cases)
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(nodeids))

    rc = _run_ci_gate(junit, manifest)
    assert rc == check_ci_gates.EXIT_GATE_FAILED

    verdict = check_ci_gates.evaluate([str(junit)], str(manifest))
    assert verdict["ok"] is False
    assert verdict["skipped_required"] == sorted(
        ["tests/test_cicd_gates.py::test_two",
         "tests/test_cicd_gates.py::test_skip_extra"])


def test_ci_gates_expect_revision_freshness(tmp_path, monkeypatch):
    """check_ci_gates ⑤：--expect-revision 传对 ⇒ 通过；传错 ⇒ 过期
    拒绝（新鲜度判定前置于 selector 判定，fail-closed）。"""
    junit = _write_junit(tmp_path / "junit.xml", _PASS_CASES)
    manifest = _write(tmp_path / "manifest.txt", _manifest_text(_CI_NODEIDS))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
        cwd=str(_REPO_ROOT)).stdout.strip()
    if not head:
        pytest.skip("git 不可用，跳过 revision 新鲜度用例")
    # evaluate() 在 Path.cwd() 下执行 git rev-parse HEAD，固定到仓库根
    monkeypatch.chdir(_REPO_ROOT)

    rc_ok = _run_ci_gate(junit, manifest, expect_revision=head)
    assert rc_ok == check_ci_gates.EXIT_OK

    wrong = "0" * 40
    rc_stale = _run_ci_gate(junit, manifest, expect_revision=wrong)
    assert rc_stale == check_ci_gates.EXIT_GATE_FAILED

    verdict = check_ci_gates.evaluate(
        [str(junit)], str(manifest), expect_revision=wrong)
    assert verdict["ok"] is False
    assert verdict["stale"]
    assert verdict["exit_reason"] == "元数据过期/版本不符（证据不属于本次运行）"
