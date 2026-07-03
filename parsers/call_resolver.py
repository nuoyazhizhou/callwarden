"""
call_resolver.py
================

调用关系解析器：将原始调用解析为具体的符号。
"""

from typing import Any, Dict, List, Optional


class CallResolver:
    """调用关系解析器：将原始调用解析为具体的符号"""

    def __init__(self, module_resolver, parser):
        self.module_resolver = module_resolver
        self.parser = parser
        # qualified_name -> symbol_info
        self.all_symbols: Dict[str, Dict[str, Any]] = {}
        # file -> use_stmts
        self.file_uses: Dict[str, List[Dict[str, str]]] = {}
        # crate 名称映射
        self.crate_name: str = "tokenslim"
        self.lib_crate_alias: str = "lib"

    def load_all_symbols(self, file_results: Dict[str, Dict[str, Any]]):
        """加载所有文件的符号"""
        # 第一步：先加载所有原始符号
        for file_path, result in file_results.items():
            for sym in result["symbols"]:
                qname = sym["qualified_name"]
                self.all_symbols[qname] = {
                    **sym,
                    "file": file_path,
                }

            for inline_mod in result["inline_modules"]:
                for sym in inline_mod["symbols"]:
                    qname = sym["qualified_name"]
                    self.all_symbols[qname] = {
                        **sym,
                        "file": file_path,
                    }

            self.file_uses[file_path] = result["use_stmts"]

        # 第二步：处理 pub use 重新导出
        # 遍历每个模块的 pub use，为重新导出的符号添加别名
        for file_path, result in file_results.items():
            module_path = result.get("module_path", "")
            if not module_path:
                continue

            for use_stmt in result["use_stmts"]:
                if not use_stmt.get("is_pub", False):
                    continue

                use_path = use_stmt["path"]
                alias = use_stmt.get("alias", "")

                # 判断 use 语句类型
                if "::{" in use_path:
                    # scoped_use_list: app::{get_plugins, run_cli}
                    self._process_pub_use_list(use_path, module_path)
                elif use_path.endswith("::*"):
                    # use_wildcard: common::*（暂不处理）
                    pass
                else:
                    # 单个符号导出: use xxx::yyy;
                    self._process_pub_use_single(use_path, alias, module_path)

    def _process_pub_use_single(self, use_path: str, alias: str, module_path: str):
        """处理单个符号的 pub use"""
        resolved = self._resolve_use_to_qualified(use_path, module_path)
        if not resolved:
            return

        use_parts = resolved.split("::")
        last_part = use_parts[-1]
        target_name = alias if alias else last_part

        if resolved in self.all_symbols:
            export_qname = f"{module_path}::{target_name}"
            if export_qname not in self.all_symbols:
                self.all_symbols[export_qname] = self.all_symbols[resolved]

    def _process_pub_use_list(self, use_path: str, module_path: str):
        """处理 scoped_use_list 格式的 pub use: app::{get_plugins, run_cli}"""
        # 分割前缀和列表
        idx = use_path.index("::{")
        prefix = use_path[:idx]
        list_str = use_path[idx + 3:-1]  # 去掉 ::{ 和 }

        # 解析列表项（处理换行和空格）
        items = []
        for item in list_str.split(","):
            item = item.strip()
            if item:
                items.append(item)

        # 解析前缀
        resolved_prefix = self._resolve_use_to_qualified(prefix, module_path)
        if not resolved_prefix:
            return

        # 为每个项创建别名
        for item in items:
            # 支持 as 别名: xxx as yyy
            item_parts = item.split(" as ")
            name = item_parts[0].strip()
            item_alias = item_parts[1].strip() if len(item_parts) > 1 else ""

            target_name = item_alias if item_alias else name
            full_qname = f"{resolved_prefix}::{name}"

            if full_qname in self.all_symbols:
                export_qname = f"{module_path}::{target_name}"
                if export_qname not in self.all_symbols:
                    self.all_symbols[export_qname] = self.all_symbols[full_qname]

    def _resolve_use_to_qualified(self, use_path: str, current_module: str) -> Optional[str]:
        """将 use 语句的路径转换为完整限定名"""
        if use_path.startswith("crate::"):
            crate = current_module.split("::")[0]
            return f"{crate}::{use_path[7:]}"

        if use_path.startswith("super::"):
            parent = self._get_parent_module(current_module)
            if parent:
                rest = use_path[7:]  # 去掉 super::
                return f"{parent}::{rest}" if rest else parent
            return None

        if use_path.startswith("self::"):
            rest = use_path[6:]
            return f"{current_module}::{rest}" if rest else current_module

        # 外部 crate: tokenslim::xxx -> lib::xxx
        if self.crate_name and use_path.startswith(f"{self.crate_name}::"):
            rest = use_path[len(self.crate_name) + 2:]
            return f"{self.lib_crate_alias}::{rest}"

        # 相对路径：相对于当前模块
        return f"{current_module}::{use_path}"

    def resolve_call(self, caller_file: str, caller_module: str, callee_name: str,
                     callee_path: str, is_qualified: bool) -> Optional[Dict[str, Any]]:
        """解析一个调用，返回被调用方的完整信息"""
        caller_crate = caller_module.split("::")[0] if "::" in caller_module else caller_module

        if is_qualified:
            # 有路径限定：crate::xxx, super::xxx, self::xxx, or_mod::xxx
            return self._resolve_qualified_call(caller_file, caller_module, callee_name, callee_path)
        else:
            # 无路径限定：可能是当前模块、use 导入、或标准库
            return self._resolve_unqualified_call(caller_file, caller_module, callee_name)

    def _resolve_qualified_call(self, caller_file: str, caller_module: str,
                                 callee_name: str, callee_path: str) -> Optional[Dict[str, Any]]:
        """解析有限定路径的调用"""
        caller_crate = caller_module.split("::")[0] if "::" in caller_module else caller_module

        # 处理 crate:: 前缀
        if callee_path == "crate":
            full_path = f"{caller_crate}::{callee_name}"
            return self.all_symbols.get(full_path)

        # 处理 super:: 前缀
        if callee_path == "super":
            parent = self._get_parent_module(caller_module)
            if parent:
                full_path = f"{parent}::{callee_name}"
                return self.all_symbols.get(full_path)
            return None

        # 处理 self:: 前缀
        if callee_path == "self":
            full_path = f"{caller_module}::{callee_name}"
            return self.all_symbols.get(full_path)

        # 其他路径：可能是同 crate 的模块，或外部 crate
        # 先尝试：如果是当前 crate 的模块，直接拼接
        full_path = f"{caller_crate}::{callee_path}::{callee_name}"
        if full_path in self.all_symbols:
            return self.all_symbols[full_path]

        # 再尝试：外部 crate 转换为内部 lib 路径
        if self.crate_name and callee_path.startswith(f"{self.crate_name}::"):
            rest = callee_path[len(self.crate_name) + 2:]
            full_path = f"{self.lib_crate_alias}::{rest}::{callee_name}"
            if full_path in self.all_symbols:
                return self.all_symbols[full_path]

        # 尝试用 use 语句解析
        use_stmts = self.file_uses.get(caller_file, [])
        for use_stmt in use_stmts:
            use_path = use_stmt["path"]
            use_parts = use_path.split("::")
            callee_parts = callee_path.split("::")

            if use_parts[-1] == callee_parts[0]:
                suffix = "::".join(callee_parts[1:])
                if suffix:
                    resolved = f"{use_path}::{suffix}::{callee_name}"
                else:
                    resolved = f"{use_path}::{callee_name}"

                # 处理 crate:: 前缀
                if resolved.startswith("crate::"):
                    resolved = f"{caller_crate}::{resolved[7:]}"

                # 处理 tokenslim:: -> lib:: 转换
                if self.crate_name and resolved.startswith(f"{self.crate_name}::"):
                    rest = resolved[len(self.crate_name) + 2:]
                    resolved = f"{self.lib_crate_alias}::{rest}"

                if resolved in self.all_symbols:
                    return self.all_symbols[resolved]

        return None

    def _resolve_unqualified_call(self, caller_file: str, caller_module: str,
                                   callee_name: str) -> Optional[Dict[str, Any]]:
        """解析无限定路径的调用"""
        # 1. 先找当前模块
        current_path = f"{caller_module}::{callee_name}"
        if current_path in self.all_symbols:
            return self.all_symbols[current_path]

        # 2. 再找 use 导入的
        use_stmts = self.file_uses.get(caller_file, [])
        for use_stmt in use_stmts:
            use_path = use_stmt["path"]
            alias = use_stmt["alias"]

            use_parts = use_path.split("::")
            last_part = use_parts[-1]

            target_name = alias if alias else last_part

            if target_name == callee_name:
                # use 语句直接导入了这个函数
                # 将 use_path 转换为内部模块路径

                # 情况 1: use crate::xxx -> 转换为 caller_crate::xxx
                resolved = use_path
                if resolved.startswith("crate::"):
                    caller_crate = caller_module.split("::")[0]
                    resolved = f"{caller_crate}::{resolved[7:]}"

                # 情况 2: use tokenslim::xxx -> 转换为 lib::xxx
                if self.crate_name and resolved.startswith(f"{self.crate_name}::"):
                    rest = resolved[len(self.crate_name) + 2:]
                    resolved = f"{self.lib_crate_alias}::{rest}"

                if resolved in self.all_symbols:
                    return self.all_symbols[resolved]

        # 3. 可能是 prelude 或外部 crate，暂不处理
        return None

    def _get_parent_module(self, module_path: str) -> Optional[str]:
        """获取父模块路径"""
        if "::" in module_path:
            return module_path.rsplit("::", 1)[0]
        return None
