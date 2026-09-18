# -*- coding: utf-8 -*-
"""C 桶建卡模板 step target_file 文件级白名单校验测试。

（C-22 承接卡 T-1789397153261-f23f38b8，backlog §W20 F5；step0 盘点见
deliverables/software-company/c22_remediation_inventory.md）

F5 教训（daemon 全等比对语义，rust_ext/src/daemon/task_collab_lifecycle.rs
L285-290 解析 / L307-316 全等拒绝）：

- step ``target_file``：**文件级全等**白名单 —— daemon 将 ``changes[].file_path``
  与之做逐字符串全等比对（``;``/`,` 拆分 + 反斜杠→正斜杠归一后 ``==`` 判定，
  无目录前缀/通配语义）；目录级条目（``tests/``、``rust_ext``、
  ``deliverables/software-company/`` 等）永远无法全等命中 → 恒
  ``E_CHANGE_PATH_NOT_ALLOWED``，changes 入账缺失、可审计性降级。
- ``executor_allowed``（合同 allowed_paths）：**可目录前缀** —— 仅是 executor
  的工作区边界声明，不参与 changes 全等比对。两者混用是本缺陷根因，勿再混淆。

本测试固化不变量：build_cards() 全部卡的全部 step ``target_file`` 为文件级
token（多文件用 ``;`` 连接、逐段仍为文件级；step0 类「文档尚未存在」场景用
预声明确定性文件名，如 cNN_remediation_inventory.md）。
"""
import importlib.util
import json
import re
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "deliverables" / "software-company" / "create_c_bucket_remediation_tasks.py"
)

# 目录级禁止值（归一后）——覆盖 step0 盘点全部 15 处缺陷原值所在目录/前缀形态
DIRECTORY_VALUES = {
    "rust_ext", "tests", "deliverables", "deliverables/software-company",
    "runtime", "runtime/current", "server", "db", "docs", "scripts",
}


def _load_module():
    """按文件路径加载建卡脚本模块（不污染 sys.path，不触发 main()）。"""
    spec = importlib.util.spec_from_file_location(
        "create_c_bucket_remediation_tasks_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _daemon_tokens(target_file):
    """复刻 daemon 白名单解析语义（task_collab_lifecycle.rs L285-290）：
    ``split([',', ';'])`` → ``trim`` → ``replace('\\', "/")``。"""
    return [tok.strip().replace("\\", "/") for tok in re.split(r"[;,]", target_file)]


def _violations(target_file):
    """返回 [(token, 原因)]；文件级判定 = 非空、非目录值、无尾斜杠、basename 带扩展名。"""
    bad = []
    for tok in _daemon_tokens(target_file):
        if not tok:
            bad.append((tok, "empty token"))
        elif tok in DIRECTORY_VALUES:
            bad.append((tok, "directory-level value"))
        elif tok.endswith("/"):
            bad.append((tok, "trailing slash (directory form)"))
        elif "." not in tok.rsplit("/", 1)[-1]:
            bad.append((tok, "basename without extension (not a file)"))
    return bad


class TestCBucketTemplateTargetFileWhitelist:
    """C-22 验收：模板全部 step target_file 文件级 token 化（F5 防回归）。"""

    def test_build_cards_loads_with_expected_shape(self):
        mod = _load_module()
        cards = mod.build_cards()
        assert len(cards) == 12, "期望 12 张 C 桶承接卡"
        steps = [s for c in cards for s in c.get("steps", [])]
        assert len(steps) == 55, "期望 55 个 step"
        for s in steps:
            assert str(s.get("target_file", "")).strip(), "step target_file 不得为空"

    def test_every_step_target_file_token_is_file_level(self):
        mod = _load_module()
        cards = mod.build_cards()
        offenders = []
        for c in cards:
            for s in c.get("steps", []):
                tf = str(s.get("target_file", ""))
                for tok, why in _violations(tf):
                    offenders.append({
                        "card": c.get("title", "?"), "action": s.get("action"),
                        "target_file": tf, "token": tok, "why": why,
                    })
        assert not offenders, (
            "发现目录级/非文件级 target_file（F5 回归）："
            + json.dumps(offenders, ensure_ascii=False, indent=2))

    def test_multifile_semicolon_joined_tokens_all_file_level(self):
        mod = _load_module()
        cards = mod.build_cards()
        multifile = [
            (c.get("title", "?"), s)
            for c in cards for s in c.get("steps", [])
            if (";" in str(s.get("target_file", "")))
            or ("," in str(s.get("target_file", "")))
        ]
        assert multifile, "期望存在多文件 `;` 连接的 target_file（C-03/C-04..07 先例）"
        for title, s in multifile:
            toks = _daemon_tokens(str(s["target_file"]))
            assert len(toks) > 1, title
            assert not _violations(s["target_file"]), (
                "多文件 target_file 存在非文件级段落：%s" % title)

    def test_directory_level_literals_are_gone(self):
        # 注意（F5 语义区分）：字面值锚只对 step target_file 生效 —— 合同
        # executor_allowed（allowed_paths）允许目录前缀（executor 工作区边界声明），
        # 其中的目录级值（如 C-03 的 "tests/"、"deliverables/software-company/"）
        # 是合法且必需的，不属于本缺陷；故只收集 target_file 字段值做字面断言。
        mod = _load_module()
        cards = mod.build_cards()
        target_files = [
            str(s.get("target_file", "")) for c in cards for s in c.get("steps", [])
        ]
        dumped = json.dumps(target_files, ensure_ascii=False)
        for literal in (
            '"tests/"', '"tests"', '"rust_ext"', '"runtime/"', '"runtime/current"',
            '"deliverables/software-company/"', '"deliverables/software-company"',
        ):
            assert literal not in dumped, (
                "模板 step target_file 中出现目录级字面值 %s（F5 回归）" % literal)

    def test_new_batch_cards_c20_c21_c22_use_predeclared_names(self):
        """C-20/C-21/C-22 三张新卡（第五次追加起范式）亦满足文件级不变量，
        且 step0 类盘点文档用预声明确定性名 cNN_remediation_inventory.md。"""
        mod = _load_module()
        cards = mod.build_cards()
        by_title = {}
        for c in cards:
            for tag in ("C-20", "C-21", "C-22"):
                if tag in str(c.get("title", "")):
                    by_title[tag] = c
        assert sorted(by_title) == ["C-20", "C-21", "C-22"], (
            "三张新卡须全部在列，实际：%s" % sorted(by_title))
        for tag, card in sorted(by_title.items()):
            expected = "deliverables/software-company/c%s_remediation_inventory.md" % tag[2:].lower()
            step0s = [s for s in card.get("steps", []) if s.get("action") == "adjudicate"]
            assert step0s, "%s 须有 step0（adjudicate）步" % tag
            assert any(str(s.get("target_file", "")) == expected for s in step0s), (
                "%s step0 预声明名 %s 不在列" % (tag, expected))
            offenders = [
                (s.get("action"), tok, why)
                for s in card.get("steps", [])
                for tok, why in _violations(str(s.get("target_file", "")))
            ]
            assert not offenders, "%s 存在目录级 target_file：%s" % (tag, offenders)

    def test_docstring_documents_f5_semantics(self):
        mod = _load_module()
        doc = mod.__doc__ or ""
        assert "executor_allowed" in doc, (
            "docstring 须记录 target_file（文件级全等）与 executor_allowed"
            "（合同 allowed_paths，可目录前缀）的语义区分（F5 教训）")
        assert "文件级" in doc, "docstring 须写明 target_file 为文件级全等白名单"
        assert ("E_CHANGE_PATH_NOT_ALLOWED" in doc) or ("全等" in doc), (
            "docstring 须记录全等比对语义与 E_CHANGE_PATH_NOT_ALLOWED 后果")
