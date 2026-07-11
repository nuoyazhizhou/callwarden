"""
Phase 6.0: Toolchain CAS — 工具链注册与存储

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 6

工具链（toolchain）是编译器 + sysroot + include_dirs + predefined_macros 的组合。
同一工具链可被多个 workspace 复用。
toolchain_fingerprint 用于去重。
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


# ============================================
# Schema
# ============================================

TOOLCHAIN_SCHEMA_DDL = """
-- 工具链表：注册的编译器工具链
CREATE TABLE IF NOT EXISTS toolchains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    compiler_path TEXT NOT NULL,
    compiler_type TEXT NOT NULL,          -- gcc / g++ / clang / arm-none-eabi-gcc / ...
    version TEXT DEFAULT '',
    target_triple TEXT DEFAULT '',
    sysroot TEXT DEFAULT '',
    include_dirs TEXT DEFAULT '[]',        -- JSON array
    predefined_macros TEXT DEFAULT '{}',   -- JSON dict
    fingerprint TEXT UNIQUE NOT NULL,      -- hash of above fields
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    description TEXT DEFAULT ''
);

-- workspace ↔ toolchain 绑定
CREATE TABLE IF NOT EXISTS workspace_toolchains (
    workspace_id INTEGER NOT NULL,
    toolchain_id INTEGER NOT NULL,
    build_context_hash TEXT DEFAULT '',     -- build variant hash
    PRIMARY KEY (workspace_id, toolchain_id, build_context_hash),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (toolchain_id) REFERENCES toolchains(id) ON DELETE CASCADE
);

-- workspace build context：记录每个 workspace 的 build 变体
CREATE TABLE IF NOT EXISTS workspace_build_contexts (
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    name TEXT DEFAULT '',                   -- 人类可读名称（如 "debug", "release"）
    compile_flags TEXT DEFAULT '[]',        -- JSON array of strings
    defines TEXT DEFAULT '{}',              -- JSON dict of macro → value
    include_paths TEXT DEFAULT '[]',        -- JSON array of extra include paths
    is_active INTEGER DEFAULT 0,            -- 每个 workspace 最多一个 active
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, build_context_hash),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);

-- resolved edges：按 build_context_hash 隔离的跨文件调用边
-- 同一源码在不同 sysroot/include_paths 下会产生不同的 resolved edges
CREATE TABLE IF NOT EXISTS resolved_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    caller_symbol_id INTEGER NOT NULL,
    callee_symbol_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL,
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    resolution_method TEXT DEFAULT '',      -- 'exact_match' / 'include_path' / 'sysroot' / 'unresolved'
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id, call_line)
);

CREATE INDEX IF NOT EXISTS idx_toolchain_fingerprint ON toolchains(fingerprint);
CREATE INDEX IF NOT EXISTS idx_workspace_toolchains_ws ON workspace_toolchains(workspace_id);
CREATE INDEX IF NOT EXISTS idx_workspace_toolchains_ctx ON workspace_toolchains(build_context_hash);
CREATE INDEX IF NOT EXISTS idx_build_contexts_ws ON workspace_build_contexts(workspace_id);
CREATE INDEX IF NOT EXISTS idx_build_contexts_active ON workspace_build_contexts(workspace_id, is_active);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_ws_ctx ON resolved_edges(workspace_id, build_context_hash);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_caller ON resolved_edges(caller_symbol_id);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_callee ON resolved_edges(callee_symbol_id);
"""


# ============================================
# 数据结构
# ============================================

@dataclass
class Toolchain:
    """工具链信息"""
    id: int = 0
    name: str = ""
    compiler_path: str = ""
    compiler_type: str = ""           # gcc / g++ / clang / arm-none-eabi-gcc / ...
    version: str = ""
    target_triple: str = ""
    sysroot: str = ""
    include_dirs: List[str] = field(default_factory=list)
    predefined_macros: Dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict"""
        return asdict(self)

    def summary(self) -> str:
        """简要描述"""
        return (
            f"Toolchain(id={self.id}, name={self.name}, "
            f"type={self.compiler_type}, version={self.version}, "
            f"target={self.target_triple})"
        )


@dataclass
class BuildContext:
    """Build context（构建上下文）

    描述一个 workspace 的构建变体：compile flags + defines + include paths。
    同一源码在不同 build context 下会产生不同的 resolved edges。
    """
    workspace_id: int = 0
    build_context_hash: str = ""
    name: str = ""
    compile_flags: List[str] = field(default_factory=list)
    defines: Dict[str, str] = field(default_factory=dict)
    include_paths: List[str] = field(default_factory=list)
    is_active: bool = False
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict"""
        return asdict(self)

    def summary(self) -> str:
        return (
            f"BuildContext(ws={self.workspace_id}, name={self.name}, "
            f"hash={self.build_context_hash[:12]}, active={self.is_active})"
        )


# ============================================
# Schema 初始化
# ============================================

def init_toolchain_schema(conn: sqlite3.Connection):
    """初始化 toolchain schema。"""
    conn.executescript(TOOLCHAIN_SCHEMA_DDL)
    conn.commit()


# ============================================
# 工具链探测
# ============================================

def probe_compiler(compiler_path: str) -> Dict[str, Any]:
    """
    探测编译器信息。

    参数：
        compiler_path: 编译器可执行文件路径

    返回：包含 version, target_triple, include_dirs, predefined_macros 的 dict

    如果探测失败，对应字段返回空值。
    """
    info = {
        "compiler_type": _detect_compiler_type(compiler_path),
        "version": "",
        "target_triple": "",
        "include_dirs": [],
        "predefined_macros": {},
    }

    if not os.path.isfile(compiler_path):
        return info

    # 1. 探测 version
    info["version"] = _probe_version(compiler_path)

    # 2. 探测 target triple
    info["target_triple"] = _probe_target_triple(compiler_path)

    # 3. 探测 include dirs
    info["include_dirs"] = _probe_include_dirs(compiler_path)

    # 4. 探测 predefined macros
    info["predefined_macros"] = _probe_predefined_macros(compiler_path)

    return info


def _detect_compiler_type(compiler_path: str) -> str:
    """从路径推断编译器类型"""
    basename = os.path.basename(compiler_path).lower()
    # 先检查带前缀的交叉编译器
    if "arm-none-eabi" in basename:
        return "arm-none-eabi-gcc"
    if "aarch64" in basename:
        return "aarch64-linux-gnu-gcc"
    if "clang" in basename:
        return "clang"
    if "g++" in basename:
        return "g++"
    if "gcc" in basename:
        return "gcc"
    return basename


def _probe_version(compiler_path: str) -> str:
    """探测编译器版本"""
    try:
        result = subprocess.run(
            [compiler_path, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # 取第一行
            return result.stdout.strip().split("\n")[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _probe_target_triple(compiler_path: str) -> str:
    """探测 target triple"""
    try:
        result = subprocess.run(
            [compiler_path, "-dumpmachine"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _probe_include_dirs(compiler_path: str) -> List[str]:
    """探测系统 include 目录"""
    try:
        result = subprocess.run(
            [compiler_path, "-E", "-x", "c", "-v", "-"],
            input="", capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return _parse_include_dirs(result.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return []


def _parse_include_dirs(stderr_output: str) -> List[str]:
    """从 gcc -v 输出解析 include 目录"""
    dirs = []
    in_include_section = False
    for line in stderr_output.split("\n"):
        if "search starts here:" in line:
            in_include_section = True
            continue
        if "End of search list." in line:
            break
        if in_include_section:
            dir_path = line.strip()
            if dir_path and dir_path not in dirs:
                dirs.append(dir_path)
    return dirs


def _probe_predefined_macros(compiler_path: str) -> Dict[str, str]:
    """探测预定义宏"""
    try:
        result = subprocess.run(
            [compiler_path, "-E", "-dM", "-x", "c", "-"],
            input="", capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return _parse_predefined_macros(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return {}


def _parse_predefined_macros(output: str) -> Dict[str, str]:
    """从 gcc -dM 输出解析预定义宏"""
    macros = {}
    for line in output.split("\n"):
        line = line.strip()
        if not line.startswith("#define "):
            continue
        parts = line[8:].split(None, 1)  # 去掉 "#define " 后按空格分割
        if len(parts) == 1:
            macros[parts[0]] = ""
        elif len(parts) == 2:
            macros[parts[0]] = parts[1]
    return macros


# ============================================
# Fingerprint
# ============================================

def compute_toolchain_fingerprint(
    compiler_path: str,
    compiler_type: str,
    version: str,
    target_triple: str,
    sysroot: str,
    include_dirs: List[str],
    predefined_macros: Dict[str, str],
) -> str:
    """
    计算工具链指纹（SHA-256）。

    指纹基于编译器路径、类型、版本、target、sysroot、include_dirs、macros。
    相同指纹的工具链视为相同（可复用）。

    格式与 Rust 实现（rust_ext/src/toolchain.rs）完全一致，确保跨语言一致。
    """
    # 路径规范化（统一为正斜杠，与 Rust 一致）
    normalized_compiler = compiler_path.replace("\\", "/")
    normalized_sysroot = sysroot.replace("\\", "/") if sysroot else ""

    # 排序 include_dirs 和 macros 以确保顺序无关
    sorted_includes = sorted(include_dirs)
    sorted_macros = sorted(predefined_macros.items())

    # 构建管道分隔的原文（与 Rust compute_toolchain_fingerprint 逐字节一致）
    # 格式: toolchain_v1|<compiler>|<type>|<version>|<target>|<sysroot>|<dir1>;<dir2>;|<k1>=<v1>;<k2>=<v2>;
    includes_str = "".join(f"{d};" for d in sorted_includes)
    macros_str = "".join(f"{k}={v};" for k, v in sorted_macros)
    raw = f"toolchain_v1|{normalized_compiler}|{compiler_type}|{version}|{target_triple}|{normalized_sysroot}|{includes_str}|{macros_str}"

    return hashlib.sha256(raw.encode()).hexdigest()


# ============================================
# 数据库操作
# ============================================

def register_toolchain(
    conn: sqlite3.Connection,
    name: str,
    compiler_path: str,
    sysroot: str = "",
    description: str = "",
    probe: bool = True,
) -> Toolchain:
    """
    注册工具链。

    参数：
        conn: SQLite 连接
        name: 工具链名称（唯一）
        compiler_path: 编译器可执行文件路径
        sysroot: sysroot 路径
        description: 描述
        probe: 是否自动探测编译器信息

    返回：注册的 Toolchain 对象

    如果 fingerprint 已存在，返回已注册的工具链（不重复注册）。
    """
    # 探测编译器信息
    if probe:
        info = probe_compiler(compiler_path)
    else:
        info = {
            "compiler_type": _detect_compiler_type(compiler_path),
            "version": "",
            "target_triple": "",
            "include_dirs": [],
            "predefined_macros": {},
        }

    fingerprint = compute_toolchain_fingerprint(
        compiler_path=compiler_path,
        compiler_type=info["compiler_type"],
        version=info["version"],
        target_triple=info["target_triple"],
        sysroot=sysroot,
        include_dirs=info["include_dirs"],
        predefined_macros=info["predefined_macros"],
    )

    now = time.time()

    # 检查 fingerprint 是否已存在
    existing = conn.execute(
        "SELECT * FROM toolchains WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()

    if existing:
        # 已存在，返回已有的（可能更新名称）
        tc = _row_to_toolchain(existing)
        if tc.name != name:
            # 同一 fingerprint 但不同名称 → 也注册新名称（别名）
            # 简化：直接返回已有的，提示用户
            pass
        return tc

    # 插入新工具链
    cursor = conn.execute(
        """INSERT INTO toolchains
           (name, compiler_path, compiler_type, version, target_triple,
            sysroot, include_dirs, predefined_macros, fingerprint,
            created_at, updated_at, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            os.path.normpath(compiler_path),
            info["compiler_type"],
            info["version"],
            info["target_triple"],
            os.path.normpath(sysroot) if sysroot else "",
            json.dumps(info["include_dirs"]),
            json.dumps(info["predefined_macros"]),
            fingerprint,
            now, now,
            description,
        ),
    )
    conn.commit()

    tc_id = cursor.lastrowid
    tc = Toolchain(
        id=tc_id,
        name=name,
        compiler_path=os.path.normpath(compiler_path),
        compiler_type=info["compiler_type"],
        version=info["version"],
        target_triple=info["target_triple"],
        sysroot=os.path.normpath(sysroot) if sysroot else "",
        include_dirs=info["include_dirs"],
        predefined_macros=info["predefined_macros"],
        fingerprint=fingerprint,
        created_at=now,
        updated_at=now,
        description=description,
    )

    return tc


def get_toolchain(conn: sqlite3.Connection, name_or_id) -> Optional[Toolchain]:
    """
    按 name 或 id 查询工具链。

    参数：
        name_or_id: 工具链名称或 ID

    返回：Toolchain 对象，不存在返回 None
    """
    if isinstance(name_or_id, int):
        row = conn.execute(
            "SELECT * FROM toolchains WHERE id = ?", (name_or_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM toolchains WHERE name = ?", (name_or_id,)
        ).fetchone()

    if row is None:
        return None
    return _row_to_toolchain(row)


def list_toolchains(conn: sqlite3.Connection) -> List[Toolchain]:
    """列出所有工具链"""
    rows = conn.execute(
        "SELECT * FROM toolchains ORDER BY created_at"
    ).fetchall()
    return [_row_to_toolchain(r) for r in rows]


def delete_toolchain(conn: sqlite3.Connection, name_or_id) -> bool:
    """
    删除工具链。

    参数：
        name_or_id: 工具链名称或 ID

    返回：是否删除成功
    """
    if isinstance(name_or_id, int):
        cursor = conn.execute(
            "DELETE FROM toolchains WHERE id = ?", (name_or_id,)
        )
    else:
        cursor = conn.execute(
            "DELETE FROM toolchains WHERE name = ?", (name_or_id,)
        )
    conn.commit()
    return cursor.rowcount > 0


def bind_toolchain_to_workspace(
    conn: sqlite3.Connection,
    workspace_id: int,
    toolchain_id: int,
    build_context_hash: str = "",
) -> bool:
    """
    绑定工具链到 workspace。

    参数：
        workspace_id: workspace ID
        toolchain_id: 工具链 ID
        build_context_hash: build context 哈希（同一 workspace 可有多个 build variant）

    返回：是否绑定成功
    """
    try:
        conn.execute(
            """INSERT OR IGNORE INTO workspace_toolchains
               (workspace_id, toolchain_id, build_context_hash)
               VALUES (?, ?, ?)""",
            (workspace_id, toolchain_id, build_context_hash),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_workspace_toolchains(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str = None,
) -> List[Toolchain]:
    """
    获取 workspace 绑定的工具链列表。

    参数：
        workspace_id: workspace ID
        build_context_hash: 如果指定（含空字符串），只返回该 build context 的工具链；
                           如果为 None，返回所有绑定

    返回：Toolchain 列表
    """
    if build_context_hash is not None:
        rows = conn.execute(
            """SELECT t.* FROM toolchains t
               JOIN workspace_toolchains wt ON t.id = wt.toolchain_id
               WHERE wt.workspace_id = ? AND wt.build_context_hash = ?""",
            (workspace_id, build_context_hash),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT t.* FROM toolchains t
               JOIN workspace_toolchains wt ON t.id = wt.toolchain_id
               WHERE wt.workspace_id = ?""",
            (workspace_id,),
        ).fetchall()

    return [_row_to_toolchain(r) for r in rows]


# ============================================
# 辅助函数
# ============================================

def _row_to_toolchain(row) -> Toolchain:
    """将数据库行转换为 Toolchain 对象"""
    return Toolchain(
        id=row[0],
        name=row[1],
        compiler_path=row[2],
        compiler_type=row[3],
        version=row[4],
        target_triple=row[5],
        sysroot=row[6],
        include_dirs=json.loads(row[7]) if row[7] else [],
        predefined_macros=json.loads(row[8]) if row[8] else {},
        fingerprint=row[9],
        created_at=row[10],
        updated_at=row[11],
        description=row[12] if len(row) > 12 else "",
    )


# ============================================
# Build Context 管理（Phase 6.2）
# ============================================

def compute_build_context_hash(
    compile_flags: List[str],
    defines: Dict[str, str],
    include_paths: List[str],
) -> str:
    """
    计算 build context 哈希。

    参数：
        compile_flags: 编译选项（如 ["-O2", "-g"]）
        defines: 预定义宏（如 {"DEBUG": "1", "VERSION": "1.0"}）
        include_paths: 额外的 include 路径

    返回：SHA-256 hex 字符串

    顺序无关：排序后哈希。
    """
    sorted_flags = sorted(compile_flags)
    sorted_defines = sorted(defines.items())
    sorted_includes = sorted(include_paths)

    # 规范化路径（统一为正斜杠）
    norm_includes = [p.replace("\\", "/") for p in sorted_includes]

    # 构建管道分隔原文
    flags_str = "".join(f"{f};" for f in sorted_flags)
    defines_str = "".join(f"{k}={v};" for k, v in sorted_defines)
    includes_str = "".join(f"{p};" for p in norm_includes)
    raw = f"buildctx_v1|{flags_str}|{defines_str}|{includes_str}"

    return hashlib.sha256(raw.encode()).hexdigest()


def register_build_context(
    conn: sqlite3.Connection,
    workspace_id: int,
    name: str,
    compile_flags: List[str] = None,
    defines: Dict[str, str] = None,
    include_paths: List[str] = None,
    set_active: bool = False,
) -> BuildContext:
    """
    注册 build context。

    参数：
        workspace_id: workspace ID
        name: build context 名称（如 "debug", "release"）
        compile_flags: 编译选项
        defines: 预定义宏
        include_paths: 额外 include 路径
        set_active: 是否设为当前 active context

    返回：BuildContext 对象
    """
    compile_flags = compile_flags or []
    defines = defines or {}
    include_paths = include_paths or []

    bch = compute_build_context_hash(compile_flags, defines, include_paths)
    now = time.time()

    # 如果 set_active，先清除其他 active
    if set_active:
        conn.execute(
            "UPDATE workspace_build_contexts SET is_active = 0 WHERE workspace_id = ?",
            (workspace_id,),
        )

    conn.execute(
        """INSERT OR REPLACE INTO workspace_build_contexts
           (workspace_id, build_context_hash, name, compile_flags, defines,
            include_paths, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (workspace_id, bch, name,
         json.dumps(compile_flags), json.dumps(defines),
         json.dumps(include_paths), int(set_active), now),
    )
    conn.commit()

    return BuildContext(
        workspace_id=workspace_id,
        build_context_hash=bch,
        name=name,
        compile_flags=compile_flags,
        defines=defines,
        include_paths=include_paths,
        is_active=set_active,
        created_at=now,
    )


def get_build_context(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> Optional[BuildContext]:
    """查询 build context。不存在返回 None。"""
    row = conn.execute(
        """SELECT * FROM workspace_build_contexts
           WHERE workspace_id = ? AND build_context_hash = ?""",
        (workspace_id, build_context_hash),
    ).fetchone()
    if row is None:
        return None
    return _row_to_build_context(row)


def list_build_contexts(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> List[BuildContext]:
    """列出 workspace 的所有 build context。"""
    rows = conn.execute(
        """SELECT * FROM workspace_build_contexts
           WHERE workspace_id = ? ORDER BY created_at""",
        (workspace_id,),
    ).fetchall()
    return [_row_to_build_context(r) for r in rows]


def set_active_build_context(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> bool:
    """
    设置 active build context。

    返回：是否设置成功（build context 不存在则返回 False）
    """
    # 检查 build context 是否存在
    exists = conn.execute(
        "SELECT 1 FROM workspace_build_contexts WHERE workspace_id = ? AND build_context_hash = ?",
        (workspace_id, build_context_hash),
    ).fetchone()
    if not exists:
        return False

    # 清除其他 active
    conn.execute(
        "UPDATE workspace_build_contexts SET is_active = 0 WHERE workspace_id = ?",
        (workspace_id,),
    )
    # 设置目标为 active
    conn.execute(
        """UPDATE workspace_build_contexts SET is_active = 1
           WHERE workspace_id = ? AND build_context_hash = ?""",
        (workspace_id, build_context_hash),
    )
    conn.commit()
    return True


def get_active_build_context(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> Optional[BuildContext]:
    """
    获取 workspace 当前 active 的 build context。

    如果没有 active context，返回 None。
    """
    row = conn.execute(
        """SELECT * FROM workspace_build_contexts
           WHERE workspace_id = ? AND is_active = 1""",
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_build_context(row)


def delete_build_context(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> bool:
    """
    删除 build context。

    返回：是否删除成功
    """
    cursor = conn.execute(
        """DELETE FROM workspace_build_contexts
           WHERE workspace_id = ? AND build_context_hash = ?""",
        (workspace_id, build_context_hash),
    )
    conn.commit()
    return cursor.rowcount > 0


def resolve_toolchain(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str = None,
) -> Optional[Toolchain]:
    """
    解析 workspace + build context 对应的 toolchain。

    参数：
        workspace_id: workspace ID
        build_context_hash: 如果为 None，使用当前 active build context

    返回：
        匹配的 Toolchain 对象。
        如果没有绑定 toolchain，返回 None。

    降级策略：
        1. 精确匹配 build_context_hash
        2. 如果未匹配，尝试 active build context
        3. 如果仍未匹配，尝试默认 build context（空 hash）
        4. 最终返回 None（未识别 build context，调用方需处理）
    """
    # 1. 精确匹配
    if build_context_hash is not None:
        tcs = get_workspace_toolchains(conn, workspace_id, build_context_hash)
        if tcs:
            return tcs[0]

    # 2. 使用 active build context
    active = get_active_build_context(conn, workspace_id)
    if active:
        tcs = get_workspace_toolchains(conn, workspace_id, active.build_context_hash)
        if tcs:
            return tcs[0]

    # 3. 尝试默认 context（空 hash）
    tcs = get_workspace_toolchains(conn, workspace_id, "")
    if tcs:
        return tcs[0]

    # 4. 无任何绑定
    return None


def _row_to_build_context(row) -> BuildContext:
    """将数据库行转换为 BuildContext 对象"""
    return BuildContext(
        workspace_id=row[0],
        build_context_hash=row[1],
        name=row[2],
        compile_flags=json.loads(row[3]) if row[3] else [],
        defines=json.loads(row[4]) if row[4] else {},
        include_paths=json.loads(row[5]) if row[5] else [],
        is_active=bool(row[6]),
        created_at=row[7],
    )


# ============================================
# Resolved Edges 管理（Phase 6.3）
# ============================================

@dataclass
class ResolvedEdge:
    """跨文件已解析的调用边

    按 build_context_hash 隔离：同一 workspace 在不同 build context 下
    可能解析到不同的 callee（如不同 sysroot 下的头文件）。
    """
    id: int = 0
    workspace_id: int = 0
    build_context_hash: str = ""
    caller_symbol_id: int = 0
    callee_symbol_id: int = 0
    callee_name: str = ""
    callee_file: str = ""
    call_line: int = 0
    resolution_method: str = ""       # exact_match / include_path / sysroot / unresolved
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"ResolvedEdge(caller={self.caller_symbol_id}, "
            f"callee={self.callee_name}@{self.callee_file}:{self.call_line}, "
            f"method={self.resolution_method})"
        )


def store_resolved_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
    edges: List[Dict[str, Any]],
) -> int:
    """
    批量存储 resolved edges。

    参数：
        workspace_id: workspace ID
        build_context_hash: build context 哈希
        edges: edge dict 列表，每个 dict 包含：
            caller_symbol_id, callee_symbol_id, callee_name,
            callee_file, call_line, resolution_method

    返回：实际插入的行数
    """
    now = time.time()
    count = 0
    for edge in edges:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO resolved_edges
               (workspace_id, build_context_hash, caller_symbol_id,
                callee_symbol_id, callee_name, callee_file, call_line,
                resolution_method, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (workspace_id, build_context_hash,
             edge["caller_symbol_id"], edge["callee_symbol_id"],
             edge.get("callee_name", ""), edge.get("callee_file", ""),
             edge.get("call_line", 0), edge.get("resolution_method", ""),
             now),
        )
        count += cursor.rowcount
    conn.commit()
    return count


def get_resolved_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
    caller_symbol_id: int = None,
) -> List[ResolvedEdge]:
    """
    查询 resolved edges。

    参数：
        workspace_id: workspace ID
        build_context_hash: build context 哈希
        caller_symbol_id: 如果指定，只返回该 caller 的 edges

    返回：ResolvedEdge 列表
    """
    if caller_symbol_id is not None:
        rows = conn.execute(
            """SELECT * FROM resolved_edges
               WHERE workspace_id = ? AND build_context_hash = ?
               AND caller_symbol_id = ?
               ORDER BY call_line""",
            (workspace_id, build_context_hash, caller_symbol_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM resolved_edges
               WHERE workspace_id = ? AND build_context_hash = ?
               ORDER BY caller_symbol_id, call_line""",
            (workspace_id, build_context_hash),
        ).fetchall()

    return [_row_to_resolved_edge(r) for r in rows]


def delete_resolved_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str = None,
) -> int:
    """
    删除 resolved edges。

    参数：
        workspace_id: workspace ID
        build_context_hash: 如果指定，只删除该 context 的 edges；
                           如果为 None，删除该 workspace 的所有 edges

    返回：删除的行数
    """
    if build_context_hash is not None:
        cursor = conn.execute(
            """DELETE FROM resolved_edges
               WHERE workspace_id = ? AND build_context_hash = ?""",
            (workspace_id, build_context_hash),
        )
    else:
        cursor = conn.execute(
            "DELETE FROM resolved_edges WHERE workspace_id = ?",
            (workspace_id,),
        )
    conn.commit()
    return cursor.rowcount


def count_resolved_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> int:
    """统计 resolved edges 数量。"""
    row = conn.execute(
        """SELECT COUNT(*) FROM resolved_edges
           WHERE workspace_id = ? AND build_context_hash = ?""",
        (workspace_id, build_context_hash),
    ).fetchone()
    return row[0]


def list_build_context_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> List[Dict[str, Any]]:
    """列出 workspace 下各 build context 的 edge 统计。"""
    rows = conn.execute(
        """SELECT build_context_hash, COUNT(*) as edge_count
           FROM resolved_edges
           WHERE workspace_id = ?
           GROUP BY build_context_hash""",
        (workspace_id,),
    ).fetchall()
    return [{"build_context_hash": r[0], "edge_count": r[1]} for r in rows]


def _row_to_resolved_edge(row) -> ResolvedEdge:
    """将数据库行转换为 ResolvedEdge 对象"""
    return ResolvedEdge(
        id=row[0],
        workspace_id=row[1],
        build_context_hash=row[2],
        caller_symbol_id=row[3],
        callee_symbol_id=row[4],
        callee_name=row[5],
        callee_file=row[6],
        call_line=row[7],
        resolution_method=row[8],
        created_at=row[9],
    )
