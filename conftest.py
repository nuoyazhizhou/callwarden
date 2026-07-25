"""项目根目录 conftest.py —— 在 tests/conftest.py 之前加载。

package-dir={callwarden=.} 布局下，pip install 后 callwarden.i18n /
callwarden.server 等子包可能解析为 namespace package（无 __init__.py）。
本文件在 pytest 收集测试之前注册子包别名，确保后续导入能找到。
"""
import importlib.util
import os
import sys

_project_root = os.path.dirname(os.path.abspath(__file__))


def _register_subpackage(dir_name: str, module_name: str) -> None:
    """从文件系统加载子包 __init__.py 并注册到 sys.modules。"""
    init_file = os.path.join(_project_root, dir_name, '__init__.py')
    if not os.path.isfile(init_file):
        return
    spec = importlib.util.spec_from_file_location(
        module_name, init_file,
        submodule_search_locations=[os.path.join(_project_root, dir_name)],
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[module_name] = mod


_register_subpackage('i18n', 'callwarden.i18n')
_register_subpackage('server', 'callwarden.server')
