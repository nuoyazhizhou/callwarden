"""L5: compile_commands.json 解析器（clangd compilation database）

从 JSON 格式的编译数据库提取构建上下文：
  - -D 选项 → defines（宏定义）
  - -I 选项 → include_paths（头文件搜索路径）
  - 其他 - 开头的选项 → compile_flags
  - 编译器路径 → 可选注册 toolchain

参考格式（clangd compilation database spec）：
  [
    {
      "directory": "/path/to/build",
      "command": "gcc -DDEBUG=1 -I./include main.c -o main.o",
      "file": "main.c",
      "arguments": ["gcc", "-DDEBUG=1", "-I./include", "main.c", "-o", "main.o"],
      "output": "main.o"
    }
  ]

设计原则：
  - command 和 arguments 二选一，arguments 优先（更可靠，无 shell 分词歧义）
  - 聚合到 workspace 级别 build context（合并所有文件的 -D/-I 去重）
  - 不修改原始文件，只读取
"""

import json
import os
import shlex
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class CompileEntry:
    """单个编译条目"""
    file: str
    directory: str = ""
    command: str = ""
    arguments: List[str] = field(default_factory=list)
    output: str = ""
    # 解析后的字段
    defines: Dict[str, str] = field(default_factory=dict)
    include_paths: List[str] = field(default_factory=list)
    compile_flags: List[str] = field(default_factory=list)
    compiler_path: str = ""

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "directory": self.directory,
            "compiler_path": self.compiler_path,
            "defines": self.defines,
            "include_paths": self.include_paths,
            "compile_flags": self.compile_flags,
            "output": self.output,
        }


@dataclass
class AggregatedBuildContext:
    """聚合后的 workspace 级别构建上下文"""
    defines: Dict[str, str] = field(default_factory=dict)
    include_paths: List[str] = field(default_factory=list)
    compile_flags: List[str] = field(default_factory=list)
    compiler_path: str = ""
    file_count: int = 0
    # 每文件的构建上下文（用于精细查询）
    per_file: Dict[str, CompileEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "defines": self.defines,
            "include_paths": self.include_paths,
            "compile_flags": self.compile_flags,
            "compiler_path": self.compiler_path,
            "file_count": self.file_count,
            "per_file_count": len(self.per_file),
        }


def parse_compile_commands(json_path: str) -> List[CompileEntry]:
    """解析 compile_commands.json 文件

    参数：
        json_path: compile_commands.json 文件路径

    返回：CompileEntry 列表（每文件一条）

    异常：
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 格式错误
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"compile_commands.json should be a JSON array, got {type(data)}")

    entries = []
    for item in data:
        entry = _parse_single_entry(item)
        if entry:
            entries.append(entry)

    return entries


def _parse_single_entry(item: dict) -> Optional[CompileEntry]:
    """解析单条编译记录"""
    file_path = item.get("file", "")
    if not file_path:
        return None

    entry = CompileEntry(
        file=file_path,
        directory=item.get("directory", ""),
        output=item.get("output", ""),
    )

    # 优先用 arguments（数组形式，无分词歧义），回退到 command（字符串形式）
    args = item.get("arguments")
    if args and isinstance(args, list):
        tokens = [str(a) for a in args]
    else:
        command = item.get("command", "")
        if not command:
            return entry
        # 用 shlex 分词（处理引号）
        try:
            tokens = shlex.split(command)
        except ValueError:
            # shlex 解析失败时退化到空格分词
            tokens = command.split()

    _parse_tokens(tokens, entry)
    return entry


def _parse_tokens(tokens: List[str], entry: CompileEntry):
    """解析编译命令 token 列表，提取 defines/includes/flags/compiler"""
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # 第一个非选项 token 通常是编译器
        if i == 0 and not tok.startswith("-"):
            entry.compiler_path = tok
            i += 1
            continue

        # -D DEFINE 或 -DDEFINE
        if tok == "-D":
            if i + 1 < len(tokens):
                _parse_define(tokens[i + 1], entry)
                i += 2
            else:
                i += 1
        elif tok.startswith("-D"):
            _parse_define(tok[2:], entry)
            i += 1

        # -I path 或 -Ipath
        elif tok == "-I":
            if i + 1 < len(tokens):
                entry.include_paths.append(tokens[i + 1])
                i += 2
            else:
                i += 1
        elif tok.startswith("-I"):
            entry.include_paths.append(tok[2:])
            i += 1

        # -isystem path（系统 include 路径）
        elif tok == "-isystem":
            if i + 1 < len(tokens):
                entry.include_paths.append(tokens[i + 1])
                i += 2
            else:
                i += 1

        # -include file.h（强制包含头文件）
        elif tok == "-include":
            if i + 1 < len(tokens):
                entry.compile_flags.append(f"-include {tokens[i + 1]}")
                i += 2
            else:
                i += 1

        # -std=c11 / -std=c++17（语言标准）
        elif tok.startswith("-std="):
            entry.compile_flags.append(tok)
            i += 1

        # -U UNDEFINE（取消定义，记录到 flags）
        elif tok == "-U":
            if i + 1 < len(tokens):
                entry.compile_flags.append(f"-U {tokens[i + 1]}")
                i += 2
            else:
                i += 1
        elif tok.startswith("-U"):
            entry.compile_flags.append(tok)
            i += 1

        # 其他 - 开头的选项（-O2, -g, -fPIC 等）
        elif tok.startswith("-"):
            entry.compile_flags.append(tok)
            i += 1

        else:
            # 非选项 token（源文件、目标文件等），跳过
            i += 1


def _parse_define(token: str, entry: CompileEntry):
    """解析 -D 后的 token，格式 NAME 或 NAME=VALUE"""
    if "=" in token:
        name, value = token.split("=", 1)
        entry.defines[name] = value
    else:
        entry.defines[token] = ""


def aggregate_build_context(entries: List[CompileEntry]) -> AggregatedBuildContext:
    """聚合多个 CompileEntry 为 workspace 级别 build context

    合并策略：
      - defines: 并集（后出现的覆盖先出现的）
      - include_paths: 并集去重（保持顺序）
      - compile_flags: 并集去重
      - compiler_path: 取第一个非空
    """
    agg = AggregatedBuildContext()
    agg.file_count = len(entries)

    seen_includes = set()
    seen_flags = set()

    for entry in entries:
        # 编译器路径（取第一个非空）
        if not agg.compiler_path and entry.compiler_path:
            agg.compiler_path = entry.compiler_path

        # defines 合并
        for k, v in entry.defines.items():
            agg.defines[k] = v

        # include_paths 去重追加
        for p in entry.include_paths:
            if p not in seen_includes:
                seen_includes.add(p)
                agg.include_paths.append(p)

        # compile_flags 去重追加
        for f in entry.compile_flags:
            if f not in seen_flags:
                seen_flags.add(f)
                agg.compile_flags.append(f)

        # per_file 记录
        agg.per_file[entry.file] = entry

    return agg


def import_compile_commands(
    json_path: str,
    workspace_root: str = "",
    normalize_paths: bool = True,
) -> AggregatedBuildContext:
    """一站式导入：解析 compile_commands.json + 聚合 + 路径规范化

    参数：
        json_path: compile_commands.json 文件路径
        workspace_root: workspace 根目录（用于路径规范化）
        normalize_paths: 是否将相对路径转为绝对路径

    返回：聚合后的 AggregatedBuildContext
    """
    entries = parse_compile_commands(json_path)

    # 路径规范化
    if normalize_paths and workspace_root:
        for entry in entries:
            # include_paths 相对路径 → 绝对路径
            entry.include_paths = [
                _normalize_path(p, entry.directory or workspace_root)
                for p in entry.include_paths
            ]
            # file 相对路径 → 绝对路径
            if entry.file and not os.path.isabs(entry.file):
                entry.file = os.path.normpath(
                    os.path.join(entry.directory or workspace_root, entry.file)
                )

    return aggregate_build_context(entries)


def _normalize_path(path: str, base_dir: str) -> str:
    """将相对路径转为绝对路径"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def split_compile_flags(tokens: List[str]) -> Tuple[str, Dict[str, str], List[str], List[str]]:
    """从 token 列表中拆分编译选项

    返回：(compiler_path, defines, include_paths, other_flags)
    独立函数，用于解析单个编译命令。
    """
    entry = CompileEntry(file="")
    _parse_tokens(tokens, entry)
    return (
        entry.compiler_path,
        entry.defines,
        entry.include_paths,
        entry.compile_flags,
    )
