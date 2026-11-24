"""pytest root conftest：包解析重定向（worktree 测试支持）。

两个前置事实：
1. 本仓是"仓库根=callwarden 包根"布局（root/__init__.py），site-packages 的
   editable 安装（__editable___callwarden_*.pth）注入 meta_path finder，把
   callwarden 包钉在安装时的主仓路径上，优先级高于 sys.path；
2. pytest 9 导入 rootdir 的 __init__.py 时按目录名解析包链，要求目录名是合法
   Python 标识符（resolve_package_path 的 isidentifier 约束）。

后果：主仓 checkout（目录名 callwarden）一切正常；worker worktree（目录名
run-<ts>-<hex> 带连字符）下，pytest 以裸名 '__init__' 导入根包会炸相对导入，
且 `import callwarden` 会静默命中主仓代码而非本 worktree。

处理：在收集前把 editable finder 的 MAPPING 重定向到当前仓 root（详见
_editable_rebind.py）。主仓运行时映射相同，no-op。
运行环境硬约束：worktree 目录名必须是合法标识符（用下划线而非连字符），
否则 pytest 9 的根包导入无解（连 conftest 也救不回 Package.setup 路径）。
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from _editable_rebind import rebind_callwarden_mapping  # noqa: E402

rebind_callwarden_mapping()
