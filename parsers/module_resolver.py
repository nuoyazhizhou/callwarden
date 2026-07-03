"""
module_resolver.py
==================

Rust 模块系统解析器：建立模块路径到文件的映射。
"""

import os
from typing import Dict, Optional

from ..config import PROJECT_ROOT, norm_path


class ModuleResolver:
    """Rust 模块系统解析器：建立模块路径到文件的映射"""

    def __init__(self, src_dir: str):
        self.src_dir = src_dir
        # 模块路径 -> 文件相对路径
        self.module_to_file: Dict[str, str] = {}
        # 文件相对路径 -> 模块路径
        self.file_to_module: Dict[str, str] = {}
        # 模块路径 -> 父模块路径
        self.module_parents: Dict[str, str] = {}

    def resolve_all(self, parser):
        """解析整个 src 目录的模块结构"""
        from .rust import RustParser
        # 从 lib.rs 和 main.rs 开始
        crate_roots = []

        lib_rs = os.path.join(self.src_dir, "lib.rs")
        if os.path.exists(lib_rs):
            crate_roots.append(("lib", lib_rs, ""))

        main_rs = os.path.join(self.src_dir, "main.rs")
        if os.path.exists(main_rs):
            crate_roots.append(("main", main_rs, ""))

        # bin 目录下的也是 crate 根
        bin_dir = os.path.join(self.src_dir, "bin")
        if os.path.exists(bin_dir):
            for fname in os.listdir(bin_dir):
                if fname.endswith(".rs"):
                    bin_name = fname[:-3]
                    crate_roots.append((bin_name, os.path.join(bin_dir, fname), ""))

        for crate_name, file_path, mod_path in crate_roots:
            self._resolve_module_recursive(parser, file_path, mod_path, crate_name)

    def _resolve_module_recursive(self, parser, file_path: str, mod_path: str, crate_name: str):
        """递归解析模块"""
        rel_path = os.path.relpath(file_path, PROJECT_ROOT)

        # 注册映射
        full_mod_path = f"{crate_name}::{mod_path}" if mod_path else crate_name
        self.module_to_file[full_mod_path] = rel_path
        self.file_to_module[rel_path] = full_mod_path

        # 解析文件
        result = parser.parse_file(file_path, mod_path)

        # 处理 mod 声明
        for mod_decl in result["mod_decls"]:
            mod_name = mod_decl["name"]
            child_mod_path = f"{mod_path}::{mod_name}" if mod_path else mod_name
            full_child_mod = f"{crate_name}::{child_mod_path}"

            # 查找对应的文件
            child_file = self._find_module_file(file_path, mod_name)
            if child_file and os.path.exists(child_file):
                self.module_parents[full_child_mod] = full_mod_path
                self._resolve_module_recursive(parser, child_file, child_mod_path, crate_name)

    def _find_module_file(self, parent_file: str, mod_name: str) -> Optional[str]:
        """根据父文件和模块名查找子模块文件"""
        parent_dir = os.path.dirname(parent_file)
        parent_base = os.path.basename(parent_file)

        # 如果父文件是 mod.rs，子模块在同目录下
        # 如果父文件是 xxx.rs，子模块在 xxx/ 目录下

        if parent_base == "mod.rs" or parent_base == "lib.rs" or parent_base == "main.rs":
            # 同目录下的 xxx.rs 或 xxx/mod.rs
            candidate1 = os.path.join(parent_dir, f"{mod_name}.rs")
            candidate2 = os.path.join(parent_dir, mod_name, "mod.rs")
        else:
            # xxx.rs 对应的子模块在 xxx/ 目录下
            stem = parent_base[:-3]  # 去掉 .rs
            candidate1 = os.path.join(parent_dir, stem, f"{mod_name}.rs")
            candidate2 = os.path.join(parent_dir, stem, mod_name, "mod.rs")

        if os.path.exists(candidate1):
            return candidate1
        elif os.path.exists(candidate2):
            return candidate2
        return None

    def get_module_file(self, module_path: str) -> Optional[str]:
        """根据模块路径获取文件路径"""
        # 先直接查找，再用规范化路径查找
        if module_path in self.module_to_file:
            return norm_path(self.module_to_file[module_path])
        return None

    def get_file_module(self, file_path: str) -> Optional[str]:
        """根据文件路径获取模块路径"""
        # 先规范化路径，再用两种分隔符尝试查找
        norm = norm_path(file_path)
        # 尝试正斜杠
        if norm in self.file_to_module:
            return self.file_to_module[norm]
        # 尝试反斜杠
        backslash = norm.replace("/", "\\")
        if backslash in self.file_to_module:
            return self.file_to_module[backslash]
        return None
