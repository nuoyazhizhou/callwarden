"""db/ 退休验收③（路线 B）—— 生产侧导入期 purity 回归守卫。

背景
----
db/ 退休验收③ 的验收口径已按**路线 B**重新定义为：
  「生产 purity + `import callwarden` 不加载 db」

P0（摘除 `callwarden/__init__.py` 的 `from .db import CodeGraphDB`）与
P1（23 个生产文件的模块级 db 导入全部改懒加载）已把该口径实现到位：
干净解释器中导入全部生产入口，`sys.modules` 里 `callwarden.db.*` 模块数 = 0。

本测试把该结论**锁死**：任何新加的生产模块级 db 导入（或新 CLI 入口）
都会立刻被本守卫拦下，防止 purity 静默回退。

方法
----
从 `pyproject.toml` 的 `[project.scripts]` 动态派生生产入口模块表
（新增 CLI 入口时自动覆盖，无需手工同步清单），在**干净子解释器**中
逐一导入，断言加载的 `callwarden.db.*` 模块为空。用子解释器是因为
tests/conftest.py 本身会合法地导入 db（测试支持层），同进程快照法
无法区分「生产触发」与「conftest 已加载」。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYPROJECT = os.path.join(_REPO_ROOT, "pyproject.toml")
# callwarden 包根 = 仓根的上一级（包根=仓根：C:/git_work → callwarden/ 包）
_PKG_ROOT = os.path.dirname(_REPO_ROOT)

# server 入口（MCP server，非 CLI，但属生产导入面，须一并覆盖）
_EXTRA_PROD_MODULES = ["callwarden.server.mcp_server"]


def _entry_modules():
    """从 pyproject [project.scripts] 解析生产入口模块（cw = "callwarden.cw:main" → callwarden.cw）。

    优先 tomllib；不可用时回退正则。返回排序去重列表。
    """
    mods = []
    try:
        import tomllib  # py311+
        with open(_PYPROJECT, "rb") as fh:
            scripts = tomllib.load(fh).get("project", {}).get("scripts", {}) or {}
        for entry in scripts.values():
            if ":" in entry:
                mods.append(entry.split(":", 1)[0].strip())
    except ImportError:
        src = open(_PYPROJECT, encoding="utf-8").read()
        for mod, _fn in re.findall(r'^\s*[\w-]+\s*=\s*"([\w\.]+):([\w]+)"\s*$',
                                  src, re.M):
            mods.append(mod)
    mods.extend(_EXTRA_PROD_MODULES)
    return sorted(set(mods))


# 子解释器探测代码：导入全部生产入口，输出已加载的 callwarden.db.* 模块表。
_PROBE = (
    "import importlib, json, sys\n"
    "for m in {mods!r}:\n"
    "    importlib.import_module(m)\n"
    "print(json.dumps(sorted(x for x in sys.modules if x.startswith('callwarden.db'))))\n"
)


class TestDbRetirementProdPurity:
    @pytest.fixture(scope="class")
    def entry_modules(self):
        mods = _entry_modules()
        assert mods, "入口模块表解析为空——pyproject [project.scripts] 解析失败，守卫失效"
        return mods

    def test_entry_modules_cover_all_cli_scripts(self):
        """入口表必须至少含 pyproject 声明的全部 CLI + server 入口（防解析静默失败）。"""
        mods = _entry_modules()
        for expect in ("callwarden.cw", "callwarden.cli.client",
                       "callwarden.cli.agent", "callwarden.cli.daemon",
                       "callwarden.server.mcp_server"):
            assert expect in mods, f"生产入口未被守卫覆盖: {expect}"

    def test_prod_entry_points_load_zero_db_modules(self, entry_modules):
        """干净解释器导入全部生产入口 → 加载的 callwarden.db.* 模块必须为空。"""
        code = _PROBE.format(mods=entry_modules)
        env = dict(os.environ)
        env["PYTHONPATH"] = _PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True,
                           cwd=_PKG_ROOT, env=env, timeout=120)
        assert r.returncode == 0, \
            f"生产入口导入失败 rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
        loaded = json.loads(r.stdout.strip())
        assert loaded == [], (
            f"生产入口导入期加载了 db 模块（purity 回退）: {loaded}\n"
            "检查是否有生产文件新增了模块级 `from callwarden.db ...` 导入。"
        )

    def test_db_package_declared_test_support(self):
        """db/ 必须在 __init__.py 中声明自己为仅测试支持层（路线 B 的文档锚点）。"""
        path = os.path.join(_REPO_ROOT, "db", "__init__.py")
        assert os.path.isfile(path), "db/__init__.py 不存在"
        src = open(path, encoding="utf-8").read()
        assert "仅测试支持" in src or "test-support" in src.lower(), (
            "db/__init__.py 未声明「仅测试支持」——路线 B 要求 db/ 明确标记为"
            "测试支持层，生产侧零导入。"
        )
