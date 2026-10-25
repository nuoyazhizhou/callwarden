"""known-red 隔离注册表（cicd/known_red.py + known_red.json）单测。

背景：T-3 known-red 零隔离（主 CI 恒红 4 failed + 5 errors，红仅文档登记、
CI 无隔离）。本测试锁定隔离机制本身的契约——门禁脚本零单测正是 T-2
（Gate 1/2 汇总行污染）得以长期存活的根因，known-red 机制不得重蹈：

1. schema 缺字段 ⇒ exit 64（防「半登记」流入 CI）；
2. 伪造到期条目 ⇒ exit 2（到期升级是日落条款的牙齿，必须真实生效）；
3. --deselect 输出恰好等于登记的 4 个 nodeid（KR-A；KR-B 已 closed）——
   关闭任何条目都须显式改本测试（有意的摩擦，防「修了不关」假账）；
4. 登记漂移防护：注册表 open 条目的 nodeid 必须真实存在于本机 collect
   （nodeid 禁手抄；重命名/删除测试而注册表不动 ⇒ 本测试红）；
5. kind 契约：kind=test 禁空 nodeids、kind=workflow 禁挂 nodeid；
   status=closed 的条目自动退出 deselect/quarantine 集合。
"""

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# 确保仓库根在 sys.path（脚本直跑/CI 联接两种形态下的硬约束）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cicd import known_red  # noqa: E402 —— sys.path 注入须先于 import

REGISTRY = _ROOT / "docs" / "testing" / "known_red" / "known_red.json"
SCRIPT = _ROOT / "cicd" / "known_red.py"
# 登记漂移防护的 ground truth：collect 实际存在的用例（禁手抄 nodeid）
DRIFT_FILES = (
    "tests/test_srv_019.py",
    "tests/test_mcp_get_token_savings_report_http_rpc.py",
)
EXPECTED_NODEID_COUNT = 4  # KR-A 4（KR-B 已关闭）；关闭条目须同步改此常量


def _load_entries():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["entries"]


def _subprocess_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.setdefault("CW_TEST_MODE", "1")
    return env


def _write_registry(tmp_path, entries):
    path = tmp_path / "known_red.json"
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- schema 负向

def test_schema_missing_field_exit_64(tmp_path):
    """缺任一必填字段 ⇒ exit 64（防半登记流入 CI）。"""
    entries = _load_entries()
    del entries[0]["owner"]  # 删一个必填字段
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_USAGE


def test_schema_bad_kind_exit_64(tmp_path):
    """kind 非法 ⇒ exit 64。"""
    entries = _load_entries()
    entries[0]["kind"] = "pytest"
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_USAGE


def test_schema_test_kind_empty_nodeids_exit_64(tmp_path):
    """kind=test 但 nodeids 为空 ⇒ exit 64（空隔离=没隔离）。"""
    entries = _load_entries()
    entries[0]["nodeids"] = []
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_USAGE


def test_schema_workflow_kind_with_nodeids_exit_64(tmp_path):
    """kind=workflow 挂 nodeid ⇒ exit 64（workflow 级红非 pytest 用例）。"""
    entries = _load_entries()
    entries[2]["nodeids"] = ["tests/test_srv_019.py::test_success_final_gate_passed"]
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_USAGE


def test_schema_bad_date_exit_64(tmp_path):
    """日期非法 ⇒ exit 64。"""
    entries = _load_entries()
    entries[0]["expiry_date"] = "2026/11/23"
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_USAGE


# ---------------------------------------------------------------- 到期升级

def test_expired_entry_exit_2(tmp_path):
    """伪造到期条目（expiry < 今天且 open）⇒ exit 2。"""
    entries = _load_entries()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    entries[0]["expiry_date"] = yesterday
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_EXPIRED


def test_closed_entry_not_expired(tmp_path):
    """到期但 status=closed 的条目不触发 exit 2（闭环即出清）。"""
    entries = _load_entries()
    entries[0]["expiry_date"] = "2020-01-01"
    entries[0]["status"] = "closed"
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_OK


def test_expired_workflow_entry_also_exit_2(tmp_path):
    """到期升级对 kind=workflow 一视同仁（不进 quarantine 跑，但牙齿照常）。"""
    entries = _load_entries()
    entries[2]["expiry_date"] = "2020-01-01"  # KR-C
    entries[2]["status"] = "open"  # 显式构造「到期未关」形态
    path = _write_registry(tmp_path, entries)
    assert known_red.main(["--registry", str(path)]) == known_red.EXIT_EXPIRED


# ---------------------------------------------------------------- deselect 正向

def test_deselect_output_exactly_registered_nodeids():
    """--deselect 输出恰好等于登记的 4 个 nodeid（KR-A；KR-B 已关闭）。

    以真实注册表跑 CLI（非内存构造），同时锁定 CLI 输出格式：
    单行、`--deselect <nodeid>` 成对、顺序与注册表一致。
    """
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--deselect"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_subprocess_env(),
        cwd=str(_ROOT),
    )
    assert out.returncode == 0, out.stderr
    tokens = out.stdout.split()
    registered = known_red.quarantine_nodeids(_load_entries())
    expected = []
    for nid in registered:
        expected.extend(["--deselect", nid])
    assert tokens == expected
    assert len(registered) == EXPECTED_NODEID_COUNT


def test_quarantine_list_one_per_line():
    """--quarantine-list 每行一个 nodeid，共 4 行（KR-A）。"""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--quarantine-list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_subprocess_env(),
        cwd=str(_ROOT),
    )
    assert out.returncode == 0, out.stderr
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert lines == known_red.quarantine_nodeids(_load_entries())
    assert len(lines) == EXPECTED_NODEID_COUNT


def test_closed_entry_excluded_from_deselect():
    """status=closed 的条目自动退出 deselect 集合（关闭即生效）。"""
    entries = _load_entries()
    entries[0]["status"] = "closed"  # KR-A 闭环
    args = known_red.deselect_args(entries)
    assert args == []
    # KR-B 已关闭：其 nodeid 不得再出现在 deselect 中
    assert not any("test_mcp_get_token_savings_report_http_rpc.py::" in a for a in args)


# ---------------------------------------------------------------- 登记漂移防护

def test_registered_nodeids_exist_in_collect():
    """注册表 open 条目的 nodeid 必须真实存在于本机 collect（防登记漂移）。

    nodeid 禁手抄：测试重命名/删除而注册表不动 ⇒ 本测试红。以
    `pytest --collect-only` 为 ground truth（任务规格口径）。
    """
    registered = known_red.quarantine_nodeids(_load_entries())
    assert registered, "注册表无 open test 条目？"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *DRIFT_FILES,
         "--collect-only", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_subprocess_env(),
        cwd=str(_ROOT),
    )
    assert proc.returncode == 0, (
        f"collect 失败（登记漂移 or 导入破损）：\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    collected = {
        ln.strip() for ln in proc.stdout.splitlines() if "::" in ln
    }
    missing = [nid for nid in registered if nid not in collected]
    assert not missing, (
        f"登记的 nodeid 在 collect 中不存在（登记漂移）：{missing}"
    )
