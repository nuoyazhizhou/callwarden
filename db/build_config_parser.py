"""
Phase 6.4: 构建系统配置解析器

从 compile_commands.json / Makefile / Kconfig 中提取编译配置，
自动注册为 build context。

三种接入方式：
1. compile_commands.json (Clang compilation database) — 每个编译单元有独立的 flags
2. Makefile — 解析 CFLAGS/CXXFLAGS/INCLUDES/DEFINES 等变量
3. Kconfig (.config) — Linux 内核配置，解析 CONFIG_* 宏
"""

import json
import os
import re
import shlex
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path


# ============================================
# compile_commands.json 解析
# ============================================

def parse_compile_commands(path: str) -> List[Dict[str, Any]]:
    """
    解析 compile_commands.json（Clang compilation database）。

    返回每个编译单元的配置列表：
        [{
            "file": "src/main.c",
            "directory": "/build",
            "arguments": ["gcc", "-c", ...],  # 或 "command"
            "command": "gcc -c ...",
            "compile_flags": ["-O2", "-g"],
            "defines": {"DEBUG": "1"},
            "include_paths": ["/usr/include"],
            "compiler": "gcc",
        }, ...]
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = []
    for entry in data:
        result = _parse_compile_commands_entry(entry)
        results.append(result)

    return results


def _parse_compile_commands_entry(entry: Dict) -> Dict[str, Any]:
    """解析单个 compile_commands 条目"""
    file_path = entry.get("file", "")
    directory = entry.get("directory", "")

    # 获取命令参数
    if "arguments" in entry:
        args = entry["arguments"]
    elif "command" in entry:
        args = shlex.split(entry["command"])
    else:
        args = []

    # 提取编译器
    compiler = args[0] if args else ""

    # 解析 flags
    compile_flags = []
    defines = {}
    include_paths = []

    i = 1
    while i < len(args):
        arg = args[i]

        if arg.startswith("-D"):
            # -DNAME=value 或 -DNAME
            define_str = arg[2:]
            if "=" in define_str:
                k, v = define_str.split("=", 1)
                defines[k] = v
            else:
                defines[define_str] = "1"
        elif arg == "-D" and i + 1 < len(args):
            # -D NAME=value
            i += 1
            define_str = args[i]
            if "=" in define_str:
                k, v = define_str.split("=", 1)
                defines[k] = v
            else:
                defines[define_str] = "1"
        elif arg.startswith("-I"):
            # -I/path 或 -I /path
            inc = arg[2:]
            if inc:
                include_paths.append(_resolve_path(inc, directory))
            elif i + 1 < len(args):
                i += 1
                include_paths.append(_resolve_path(args[i], directory))
        elif arg.startswith("-"):
            # 其他编译选项
            if arg not in ("-c", "-o", "-pipe", "-Wall", "-Wextra"):
                compile_flags.append(arg)
            elif arg in ("-Wall", "-Wextra"):
                compile_flags.append(arg)
        elif arg.endswith(".c") or arg.endswith(".cpp") or arg.endswith(".cc") or arg.endswith(".cxx"):
            pass  # 源文件
        else:
            # 可能是 -o 的参数
            pass

        i += 1

    return {
        "file": file_path,
        "directory": directory,
        "arguments": args,
        "command": entry.get("command", ""),
        "compiler": compiler,
        "compile_flags": compile_flags,
        "defines": defines,
        "include_paths": include_paths,
    }


def _resolve_path(path: str, base_dir: str) -> str:
    """解析相对路径，统一使用正斜杠"""
    if os.path.isabs(path):
        return os.path.normpath(path).replace("\\", "/")
    if base_dir:
        return os.path.normpath(os.path.join(base_dir, path)).replace("\\", "/")
    return path


def compile_commands_to_build_context(
    entries: List[Dict[str, Any]],
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """
    将 compile_commands 条目聚合为 build context 参数。

    取所有编译单元的 flags/defines/includes 的并集。
    """
    all_flags = set()
    all_defines = {}
    all_includes = set()

    for entry in entries:
        all_flags.update(entry.get("compile_flags", []))
        all_defines.update(entry.get("defines", {}))
        all_includes.update(entry.get("include_paths", []))

    return sorted(all_flags), all_defines, sorted(all_includes)


# ============================================
# Makefile 解析
# ============================================

def parse_makefile(path: str) -> Dict[str, Any]:
    """
    解析 Makefile，提取编译配置。

    识别的变量：
        CFLAGS / CXXFLAGS / CPPFLAGS — 编译选项
        DEFINES — 额外宏定义
        INCLUDES / CPPFLAGS — include 路径
        CC / CXX — 编译器

    返回：
        {
            "compiler": "gcc",
            "compile_flags": ["-O2", "-g"],
            "defines": {"DEBUG": "1"},
            "include_paths": ["/usr/include"],
        }
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    result = {
        "compiler": "",
        "compile_flags": [],
        "defines": {},
        "include_paths": [],
    }

    # 提取变量赋值 VAR = value 或 VAR := value 或 VAR += value
    var_pattern = re.compile(r'^([A-Z_][A-Z0-9_]*)\s*([:?+]?=)\s*(.+)$', re.MULTILINE)

    variables = {}
    for match in var_pattern.finditer(content):
        var_name = match.group(1)
        assignment = match.group(2)
        value = match.group(3).strip()

        # 去除注释
        if '#' in value:
            value = value[:value.index('#')].strip()

        # 展开变量引用 $(VAR)
        value = _expand_makefile_vars(value, variables)

        if assignment == '+=' and var_name in variables:
            variables[var_name] = variables[var_name] + ' ' + value
        else:
            variables[var_name] = value

    # 提取编译器
    result["compiler"] = variables.get("CC", variables.get("CXX", ""))

    # 提取编译选项
    cflags = variables.get("CFLAGS", "")
    cxxflags = variables.get("CXXFLAGS", "")
    cppflags = variables.get("CPPFLAGS", "")

    all_flags_str = " ".join([cflags, cxxflags, cppflags])
    result["compile_flags"] = shlex.split(all_flags_str) if all_flags_str.strip() else []

    # 从 flags 中提取 defines 和 includes
    flags = result["compile_flags"]
    remaining_flags = []
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag.startswith("-D"):
            define_str = flag[2:]
            if "=" in define_str:
                k, v = define_str.split("=", 1)
                result["defines"][k] = v
            else:
                result["defines"][define_str] = "1"
        elif flag.startswith("-I"):
            inc = flag[2:]
            if inc:
                result["include_paths"].append(inc)
        else:
            remaining_flags.append(flag)
        i += 1

    result["compile_flags"] = remaining_flags

    # 从 DEFINES 变量提取
    defines_str = variables.get("DEFINES", "")
    if defines_str:
        for token in shlex.split(defines_str):
            if token.startswith("-D"):
                token = token[2:]
            if "=" in token:
                k, v = token.split("=", 1)
                result["defines"][k] = v
            else:
                result["defines"][token] = "1"

    # 从 INCLUDES 变量提取
    includes_str = variables.get("INCLUDES", "")
    if includes_str:
        for token in shlex.split(includes_str):
            if token.startswith("-I"):
                token = token[2:]
            if token:
                result["include_paths"].append(token)

    return result


def _expand_makefile_vars(value: str, variables: Dict[str, str]) -> str:
    """展开 Makefile 变量引用 $(VAR) 或 ${VAR}"""
    def replace_var(match):
        var_name = match.group(1)
        return variables.get(var_name, match.group(0))

    value = re.sub(r'\$\(([A-Z_][A-Z0-9_]*)\)', replace_var, value)
    value = re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}', replace_var, value)
    return value


# ============================================
# Kconfig (.config) 解析
# ============================================

def parse_kconfig(path: str) -> Dict[str, Any]:
    """
    解析 Linux 内核 Kconfig (.config) 文件。

    格式：
        CONFIG_FOO=y       → defines["CONFIG_FOO"] = "y"
        CONFIG_BAR=m       → defines["CONFIG_BAR"] = "m"
        CONFIG_BAZ="text"  → defines["CONFIG_BAZ"] = "text"
        # CONFIG_DISABLED is not set  → 跳过

    返回：
        {
            "defines": {"CONFIG_FOO": "y", ...},
            "compile_flags": [],
            "include_paths": [],
        }
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    result = {
        "defines": {},
        "compile_flags": [],
        "include_paths": [],
    }

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            # 跳过注释和空行
            continue

        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # 去除引号
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]

            result["defines"][key] = value

    return result


# ============================================
# 统一导入接口
# ============================================

def import_build_config(
    conn,
    workspace_id: int,
    config_path: str,
    config_type: str = "auto",
    name: str = "",
    set_active: bool = False,
):
    """
    从构建系统配置文件导入 build context。

    参数：
        conn: SQLite 连接
        workspace_id: workspace ID
        config_path: 配置文件路径
        config_type: "compile_commands" / "makefile" / "kconfig" / "auto"（自动检测）
        name: build context 名称
        set_active: 是否设为 active

    返回：BuildContext 对象
    """
    # 延迟导入，避免循环依赖
    import importlib.util
    _tc_path = Path(__file__).parent / "db_toolchain.py"
    _tc_spec = importlib.util.spec_from_file_location("db_toolchain_imp", str(_tc_path))
    _tc_mod = importlib.util.module_from_spec(_tc_spec)
    _tc_spec.loader.exec_module(_tc_mod)

    register_build_context = _tc_mod.register_build_context

    # 自动检测类型
    if config_type == "auto":
        config_type = _detect_config_type(config_path)

    # 解析配置文件
    if config_type == "compile_commands":
        entries = parse_compile_commands(config_path)
        flags, defines, includes = compile_commands_to_build_context(entries)
    elif config_type == "makefile":
        info = parse_makefile(config_path)
        flags = info["compile_flags"]
        defines = info["defines"]
        includes = info["include_paths"]
    elif config_type == "kconfig":
        info = parse_kconfig(config_path)
        flags = info["compile_flags"]
        defines = info["defines"]
        includes = info["include_paths"]
    else:
        raise ValueError(f"未知的配置类型: {config_type}")

    # 自动生成名称
    if not name:
        name = Path(config_path).stem

    # 注册 build context
    return register_build_context(
        conn, workspace_id, name,
        compile_flags=flags,
        defines=defines,
        include_paths=includes,
        set_active=set_active,
    )


def _detect_config_type(path: str) -> str:
    """根据文件名自动检测配置类型"""
    filename = os.path.basename(path).lower()

    if filename == "compile_commands.json" or filename.endswith(".json"):
        return "compile_commands"
    if filename in ("makefile", "makefile.am", "makefile.in") or filename.endswith(".mk"):
        return "makefile"
    if filename == ".config" or filename.startswith(".config") or "kconfig" in filename:
        return "kconfig"

    # 根据内容检测
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first_line = f.readline()

    if first_line.strip().startswith("[") or first_line.strip().startswith("{"):
        return "compile_commands"
    if "CONFIG_" in first_line:
        return "kconfig"

    return "makefile"
