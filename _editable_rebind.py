"""editable 安装重定向：worktree 测试的包解析引导。

背景见根 conftest.py 与 tests/test_cli_journal.py 注释。核心事实：
- 本仓"仓库根=callwarden 包根"，site-packages 的 __editable___callwarden_*.pth
  注入 meta_path finder，把 callwarden 钉在安装时的主仓路径上；
- 该 finder 的 find_spec 每次调用动态读模块级 MAPPING 字典，改字典即生效。

本模块同时被两处调用：
- 根 conftest.py（pytest 主进程，惠及全部测试文件）；
- tests/test_cli_journal.py 模块级（multiprocessing spawn 子进程不加载 conftest，
  但会重新导入测试模块，模块级执行本函数可让并发 worker 子进程正确解析包）。
主仓运行时映射相同，no-op。
"""

import os
import sys
from pathlib import Path


def rebind_callwarden_mapping() -> None:
    """把 editable finder 的 MAPPING['callwarden'] 重定向到当前仓 root。"""
    root = str(Path(__file__).resolve().parent)
    for m in sys.meta_path:
        mod = sys.modules.get(getattr(m, "__module__", ""))
        mapping = getattr(mod, "MAPPING", None)
        if not isinstance(mapping, dict) or "callwarden" not in mapping:
            continue
        if os.path.realpath(mapping["callwarden"]).lower() != os.path.realpath(root).lower():
            mapping["callwarden"] = root
            namespaces = getattr(mod, "NAMESPACES", None)
            if isinstance(namespaces, dict):
                namespaces["callwarden.i18n"] = [os.path.join(root, "i18n")]
            # 清掉可能已缓存的错误 sys.modules 条目（finder 指向旧路径时导入的）
            for k in [k for k in sys.modules if k == "callwarden" or k.startswith("callwarden.")]:
                del sys.modules[k]
