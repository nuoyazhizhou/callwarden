# -*- coding: utf-8 -*-
"""RP-09 Skill and documentation cutover 契约测试。

覆盖：
1. 仓库实跑：scripts/validate_template_compliance.py（含 --self-test）退出码 0；
2. SKILL/四模板/AGENTS/user-guide 声明 production prompt 唯一来源为 daemon 编译的
   Role Prompt Bundle（RPC `task.prompt.compile`，capability `role_prompt_compiler_v1`）；
3. 受保护回退：capability 未声明时才允许回落只读 next-action，且必须要求 daemon 返回的
   exact workspace instance；
4. 负向：注入「未声明来源 / 本地渲染 / derive fallback / 无门禁回退」四类破坏，
   校验器必须分别报 E_PROMPT_SOURCE_MISSING / E_CLIENT_PROMPT_RENDER /
   E_DERIVE_FALLBACK / E_FALLBACK_NO_GUARD。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "scripts" / "validate_template_compliance.py"
SKILL = REPO_ROOT / ".agents" / "skills" / "cw-task-loop" / "SKILL.md"
USER_GUIDE = REPO_ROOT / ".agents" / "skills" / "cw-task-loop" / "references" / "user-guide.md"
AGENTS = REPO_ROOT / "AGENTS.md"
AGENT_USAGE = REPO_ROOT / "docs" / "agent-usage-guide.md"
TEMPLATES = [
    REPO_ROOT / "docs" / "role-loop-templates" / "Callwarden 无人值守循环启动模板：Planner v1.md",
    REPO_ROOT / "docs" / "role-loop-templates" / "Callwarden 无人值守循环启动模板：Executor v4.md",
    REPO_ROOT / "docs" / "role-loop-templates" / "Callwarden 无人值守循环启动模板：Reviewer v4.md",
    REPO_ROOT / "docs" / "role-loop-templates" / "Callwarden 无人值守循环启动模板：Adjudicator v4.md",
]

PROMPT_METHOD = "task.prompt.compile"
CAPABILITY = "role_prompt_compiler_v1"


def _load_validator():
    spec = importlib.util.spec_from_file_location("vtcompliance", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 解析需要模块已在 sys.modules 中登记，否则取不到 __module__ 命名空间。
    sys.modules["vtcompliance"] = module
    spec.loader.exec_module(module)
    return module


vt = _load_validator()


# --------------------------------------------------------------------- 仓库实跑


def test_validator_passes_on_repository():
    proc = subprocess.run([sys.executable, str(VALIDATOR)], capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_validator_self_test_passes():
    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), "--self-test"], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 失败" in proc.stdout


# ------------------------------------------------------- production prompt 来源


@pytest.mark.parametrize("path", [SKILL, USER_GUIDE, AGENTS, AGENT_USAGE, *TEMPLATES])
def test_docs_declare_daemon_bundle_as_prompt_source(path: Path):
    text = path.read_text(encoding="utf-8")
    assert PROMPT_METHOD in text, f"{path.name} 未声明 RPC {PROMPT_METHOD}"
    assert CAPABILITY in text, f"{path.name} 未声明 capability {CAPABILITY}"
    assert not [v for v in vt.check_prompt_source_discipline(path, text)
                if v.code == "E_PROMPT_SOURCE_MISSING"]


def test_skill_emits_prompt_text_verbatim():
    text = SKILL.read_text(encoding="utf-8")
    assert "prompt.text" in text
    # 逐字输出纪律：不得改写、不得本地补造。
    assert "逐字输出" in text or "原样输出" in text
    assert "不得本地渲染" in text or "不得本地渲染或补造" in text


def test_skill_fallback_requires_exact_daemon_workspace_instance():
    text = SKILL.read_text(encoding="utf-8")
    fallback_section = text.split("## Prompt Source and Capability Gate")[1].split("\n## ")[0]
    assert "next-action" in fallback_section, "回退必须指向既有只读 next-action role card"
    assert "exact workspace instance" in fallback_section or "exact daemon-returned" in fallback_section
    assert CAPABILITY in fallback_section
    assert "fail closed" in fallback_section or "fail-closed" in fallback_section


@pytest.mark.parametrize("path", TEMPLATES)
def test_template_body_retired_from_prompt_source(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "不再是 production prompt 来源" in text, f"{path.name} 未声明模板正文退役"
    # 只有受保护回退骨架：字段必须逐字取自 daemon。
    assert "逐字" in text
    # 不得出现未加否定的本地渲染 / derive fallback 断言。
    codes = {v.code for v in vt.check_prompt_source_discipline(path, text)}
    assert "E_CLIENT_PROMPT_RENDER" not in codes
    assert "E_DERIVE_FALLBACK" not in codes
    assert "E_FALLBACK_NO_GUARD" not in codes


# ------------------------------------------------------------------- 负向注入


def _violations_for_broken(path: Path, broken: str):
    return {v.code for v in vt.check_prompt_source_discipline(path, broken)}


def test_detects_missing_prompt_source():
    raw = (REPO_ROOT / "docs" / "role-loop-templates" / "Callwarden 无人值守循环启动模板：Executor v4.md").read_text(encoding="utf-8")
    broken = re.sub(r"## Prompt 来源（RP-09 cutover）.*?(?=\n## |\Z)", "", raw, flags=re.DOTALL)
    assert "E_PROMPT_SOURCE_MISSING" in _violations_for_broken(Path("executor.md"), broken)


def test_detects_client_local_prompt_rendering():
    raw = SKILL.read_text(encoding="utf-8")
    broken = raw + "\n本入口在客户端本地渲染 production prompt。\n"
    assert "E_CLIENT_PROMPT_RENDER" in _violations_for_broken(Path("SKILL.md"), broken)


def test_detects_derive_workspace_fallback():
    raw = SKILL.read_text(encoding="utf-8")
    broken = raw + "\nworkspace 未知时调用 `derive_workspace_instance_id` 补全即可。\n"
    assert "E_DERIVE_FALLBACK" in _violations_for_broken(Path("SKILL.md"), broken)


def test_detects_unguarded_next_action_fallback():
    raw = SKILL.read_text(encoding="utf-8")
    broken = raw + "\n直接回落 `cw task next-action <task-id>` 即可，不需要其他前提。\n"
    assert "E_FALLBACK_NO_GUARD" in _violations_for_broken(Path("SKILL.md"), broken)
