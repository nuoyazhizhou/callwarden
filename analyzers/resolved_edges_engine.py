"""
L5 resolved_edges 计算引擎

将 raw_calls（CAS 层）或 calls（CLI 层）解析为 build-context-aware 的 resolved_edges。

双层调用关系模型（设计文档 enterprise-daemon-shared-snapshot-plan.md §7.4）：
- raw_calls / calls：单文件内直接抽取的调用文本，不感知 build_context
- resolved_edges：按 (workspace_id, build_context_hash) 隔离的解析后调用边

数据源优先级：
1. CAS 模式：从 cas_raw_calls 解析（需要 workspace_manifests 有 cas_key）
2. CLI 降级：从 calls 表复制（resolution_method="from_calls"）

解析策略（CAS 模式，5 级）：
1. exact_match：callee_name 作为 qualified_name 精确匹配
2. simple_name_unique：简名（最后一段）全局唯一匹配（跨文件）
3. same_file：同文件简名匹配
4. include_path/sysroot：基于 build_context 的 include_paths + toolchain sysroot/include_dirs
   消除简名歧义（多 candidate 时，优先匹配在头文件搜索路径下的定义）
5. unresolved：兜底
"""

import json
import sqlite3
from typing import List, Dict, Any, Optional, Tuple


def compute_resolved_edges(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> Dict[str, Any]:
    """
    计算 resolved edges。

    参数：
        conn: 数据库连接
        workspace_id: workspace ID
        build_context_hash: build context 哈希

    返回：
        {
            "edges": List[dict],   # edge dict 列表（含 caller_symbol_id 等）
            "source": str,         # "cas" | "calls_table" | "none"
            "count": int,          # edge 数量
            "skipped": int,        # 跳过的 raw_call 数（caller 无法映射）
        }
    """
    # 验证 build_context 存在（避免对空 hash 计算）
    bc_row = conn.execute(
        """SELECT build_context_hash FROM workspace_build_contexts
           WHERE workspace_id = ? AND build_context_hash = ?""",
        (workspace_id, build_context_hash),
    ).fetchone()
    if bc_row is None:
        return {"edges": [], "source": "none", "count": 0, "skipped": 0,
                "error": "build_context not found"}

    # 优先 CAS 模式
    cas_result = _compute_from_cas(conn, workspace_id, build_context_hash)
    if cas_result is not None:
        return cas_result

    # 降级：从 calls 表复制
    return _compute_from_calls(conn, workspace_id, build_context_hash)


# ============================================
# CAS 模式：从 cas_raw_calls 解析
# ============================================

def _compute_from_cas(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> Optional[Dict[str, Any]]:
    """
    CAS 模式：从 cas_raw_calls 解析。

    需要 workspace_manifests 表有 cas_key。如果 cas_raw_calls 无数据返回 None（降级）。
    """
    # 1. 获取 workspace 的 cas_key → rel_path 映射
    manifest_rows = conn.execute(
        """SELECT rel_path, cas_key FROM workspace_manifests
           WHERE workspace_id = ? AND cas_key IS NOT NULL AND cas_key != ''""",
        (workspace_id,),
    ).fetchall()
    if not manifest_rows:
        return None  # 无 manifest，降级

    cas_to_relpath: Dict[str, str] = {r[1]: r[0] for r in manifest_rows}
    cas_keys = list(cas_to_relpath.keys())

    # 2. 查询 cas_raw_calls
    placeholders = ",".join("?" * len(cas_keys))
    raw_calls = conn.execute(
        f"""SELECT cas_key, caller_local_id, caller_name, callee_name, call_line
            FROM cas_raw_calls
            WHERE cas_key IN ({placeholders})""",
        cas_keys,
    ).fetchall()
    if not raw_calls:
        return None  # cas_raw_calls 为空，降级

    # 3. 构建 caller_symbol_id 映射：
    # (cas_key, caller_local_id) → cas_symbols.local_qualified_name → symbols.id
    caller_map = _build_cas_caller_map(conn, workspace_id, cas_keys, cas_to_relpath)

    # 4. 构建符号索引（symbols 表）
    symbol_index = _build_symbol_index(conn, workspace_id)

    # 4.5 加载 build_context search_paths（include_paths + sysroot + toolchain_include_dirs）
    include_paths, sysroot, tc_include_dirs = _load_build_context_includes(
        conn, workspace_id, build_context_hash
    )
    search_paths = None
    if include_paths or sysroot or tc_include_dirs:
        search_paths = {
            "include_paths": include_paths,
            "sysroot": sysroot,
            "toolchain_include_dirs": tc_include_dirs,
        }

    # 5. 解析每条 raw_call
    edges: List[Dict[str, Any]] = []
    skipped = 0
    for row in raw_calls:
        cas_key, caller_local_id, caller_name, callee_name, call_line = row
        caller_symbol_id = caller_map.get((cas_key, caller_local_id))
        if caller_symbol_id is None:
            # caller_local_id 为 NULL 或无法映射，跳过
            skipped += 1
            continue

        caller_relpath = cas_to_relpath.get(cas_key, "")
        callee_id, callee_file, method = _resolve_callee(
            callee_name, caller_relpath, symbol_index, search_paths
        )
        edges.append({
            "caller_symbol_id": caller_symbol_id,
            "callee_symbol_id": callee_id,
            "callee_name": callee_name,
            "callee_file": callee_file,
            "call_line": call_line,
            "resolution_method": method,
        })

    return {"edges": edges, "source": "cas", "count": len(edges), "skipped": skipped}


def _build_cas_caller_map(
    conn: sqlite3.Connection,
    workspace_id: int,
    cas_keys: List[str],
    cas_to_relpath: Dict[str, str],
) -> Dict[Tuple[str, int], int]:
    """
    构建 caller 映射：(cas_key, caller_local_id) → symbols.id

    路径：cas_symbols.local_qualified_name → symbols.qualified_name → symbols.id
    通过 cas_key → rel_path → file_instance_id 关联。
    """
    # 查询 cas_symbols（cas_key, local_symbol_id, local_qualified_name, name）
    placeholders = ",".join("?" * len(cas_keys))
    cas_syms = conn.execute(
        f"""SELECT cas_key, local_symbol_id, local_qualified_name, name, start_line
            FROM cas_symbols
            WHERE cas_key IN ({placeholders})""",
        cas_keys,
    ).fetchall()

    # 构建 rel_path → file_instance_id 映射
    relpath_to_fiid: Dict[str, int] = {}
    fi_rows = conn.execute(
        """SELECT id, rel_path FROM file_instances
           WHERE workspace_id = ? AND status != 'archived'""",
        (workspace_id,),
    ).fetchall()
    for fi_id, rel_path in fi_rows:
        relpath_to_fiid[rel_path] = fi_id

    # 构建 (qname, file_instance_id) → symbols.id 索引
    # 以及 (name, start_line, file_instance_id) → symbols.id 作为 fallback
    qname_index: Dict[Tuple[str, int], int] = {}
    name_line_index: Dict[Tuple[str, int, int], int] = {}
    sym_rows = conn.execute(
        """SELECT s.id, s.qualified_name, s.name, s.start_line, s.file_instance_id
           FROM symbols s
           JOIN file_instances fi ON s.file_instance_id = fi.id
           WHERE fi.workspace_id = ?""",
        (workspace_id,),
    ).fetchall()
    for s_id, qname, s_name, s_line, fi_id in sym_rows:
        if qname:
            qname_index[(qname, fi_id)] = s_id
        name_line_index[(s_name, s_line, fi_id)] = s_id

    # 构建 caller_map
    caller_map: Dict[Tuple[str, int], int] = {}
    for cas_key, local_id, local_qname, sym_name, sym_line in cas_syms:
        rel_path = cas_to_relpath.get(cas_key)
        if rel_path is None:
            continue
        fi_id = relpath_to_fiid.get(rel_path)
        if fi_id is None:
            continue

        # 优先 qualified_name 匹配
        s_id = qname_index.get((local_qname, fi_id)) if local_qname else None
        # fallback: name + start_line 匹配
        if s_id is None:
            s_id = name_line_index.get((sym_name, sym_line, fi_id))

        if s_id is not None:
            caller_map[(cas_key, local_id)] = s_id

    return caller_map


# ============================================
# 降级模式：从 calls 表复制
# ============================================

def _compute_from_calls(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> Dict[str, Any]:
    """
    降级模式：从 calls 表复制（CLI 模式，CAS 未发布）。

    calls 表已经是名称匹配解析后的结果，不感知 build_context。
    resolution_method 标记为 "from_calls"，后续补齐真正的 build_context 感知解析。
    """
    rows = conn.execute(
        """SELECT c.caller_id, c.callee_id, c.callee_name, c.callee_file, c.call_line
           FROM calls c
           JOIN symbols s ON c.caller_id = s.id
           JOIN file_instances fi ON s.file_instance_id = fi.id
           WHERE fi.workspace_id = ?""",
        (workspace_id,),
    ).fetchall()

    edges: List[Dict[str, Any]] = []
    for caller_id, callee_id, callee_name, callee_file, call_line in rows:
        edges.append({
            "caller_symbol_id": caller_id,
            "callee_symbol_id": callee_id if callee_id else 0,
            "callee_name": callee_name,
            "callee_file": callee_file or "",
            "call_line": call_line,
            "resolution_method": "from_calls",
        })

    return {"edges": edges, "source": "calls_table", "count": len(edges), "skipped": 0}


# ============================================
# 符号索引构建 + callee 解析
# ============================================

def _build_symbol_index(conn: sqlite3.Connection, workspace_id: int) -> Dict[str, Any]:
    """
    构建 callee 解析用的符号索引。

    返回：
        {
            "qname_map": {qualified_name: symbol_id},  # 精确匹配
            "name_index": {simple_name: [symbol_id, ...]},  # 简名匹配
            "file_symbols": {rel_path: {simple_name: symbol_id}},  # 同文件匹配
            "file_for_symbol": {symbol_id: rel_path},  # symbol_id → 文件路径
        }
    """
    rows = conn.execute(
        """SELECT s.id, s.name, s.qualified_name, fi.rel_path
           FROM symbols s
           JOIN file_instances fi ON s.file_instance_id = fi.id
           WHERE fi.workspace_id = ?""",
        (workspace_id,),
    ).fetchall()

    qname_map: Dict[str, int] = {}
    name_index: Dict[str, List[int]] = {}
    file_symbols: Dict[str, Dict[str, int]] = {}
    file_for_symbol: Dict[int, str] = {}

    for s_id, s_name, qname, rel_path in rows:
        if qname:
            qname_map[qname] = s_id
        name_index.setdefault(s_name, []).append(s_id)
        file_symbols.setdefault(rel_path, {})[s_name] = s_id
        file_for_symbol[s_id] = rel_path

    return {
        "qname_map": qname_map,
        "name_index": name_index,
        "file_symbols": file_symbols,
        "file_for_symbol": file_for_symbol,
    }


def _resolve_callee(
    callee_name: str,
    caller_relpath: str,
    symbol_index: Dict[str, Any],
    search_paths: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, str]:
    """
    解析 callee_name → callee_symbol_id。

    返回 (callee_symbol_id, callee_file, resolution_method)。
    解析失败时 callee_symbol_id=0, resolution_method="unresolved"。

    5 级策略：
    1. exact_match：callee_name 作为 qualified_name 精确匹配
    2. simple_name_unique：简名全局唯一
    3. same_file：同文件简名
    4. include_path/sysroot：简名多 candidate 时，按 build_context search_paths 消歧
    5. unresolved：兜底

    参数：
        search_paths: 第 4 级解析所需的 build_context 路径信息，结构：
            {
                "include_paths": [...],           # workspace 相对路径
                "sysroot": "...",                 # 绝对路径
                "toolchain_include_dirs": [...],  # 绝对路径
            }
            为 None 时跳过第 4 级。
    """
    qname_map = symbol_index["qname_map"]
    name_index = symbol_index["name_index"]
    file_symbols = symbol_index["file_symbols"]
    file_for_symbol = symbol_index["file_for_symbol"]

    # 策略 1：qualified_name 精确匹配
    if callee_name in qname_map:
        sid = qname_map[callee_name]
        return sid, file_for_symbol.get(sid, ""), "exact_match"

    # 策略 2：简名全局唯一
    candidates = name_index.get(callee_name, [])
    if len(candidates) == 1:
        sid = candidates[0]
        return sid, file_for_symbol.get(sid, ""), "simple_name_unique"

    # 策略 3：同文件简名
    file_syms = file_symbols.get(caller_relpath, {})
    if callee_name in file_syms:
        sid = file_syms[callee_name]
        return sid, caller_relpath, "same_file"

    # 策略 4：include_path/sysroot 路径匹配（消除简名歧义）
    if search_paths is not None and len(candidates) > 1:
        result = _resolve_callee_include_path(candidates, symbol_index, search_paths)
        if result is not None:
            return result

    # 策略 5：兜底
    return 0, "", "unresolved"


# ============================================
# 第 4 级解析：include_path / sysroot 路径匹配
# ============================================

def _load_build_context_includes(
    conn: sqlite3.Connection,
    workspace_id: int,
    build_context_hash: str,
) -> Tuple[List[str], str, List[str]]:
    """
    加载 build context 的 include 路径信息（第 4 级解析所需）。

    联表查询：
    - workspace_build_contexts.include_paths（workspace 相对路径，JSON array）
    - workspace_toolchains → toolchains.sysroot（绝对路径）
    - workspace_toolchains → toolchains.include_dirs（绝对路径，JSON array）

    返回 (include_paths, sysroot, toolchain_include_dirs)。
    无对应数据时各字段为空。
    """
    # 1. 查 build_context 的 include_paths
    bc_row = conn.execute(
        "SELECT include_paths FROM workspace_build_contexts "
        "WHERE workspace_id = ? AND build_context_hash = ?",
        (workspace_id, build_context_hash),
    ).fetchone()
    include_paths: List[str] = []
    if bc_row and bc_row[0]:
        try:
            include_paths = json.loads(bc_row[0])
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. 查 workspace_toolchains + toolchains（取第一个 toolchain 的 sysroot + include_dirs）
    tc_row = conn.execute(
        "SELECT t.sysroot, t.include_dirs FROM toolchains t "
        "JOIN workspace_toolchains wt ON t.id = wt.toolchain_id "
        "WHERE wt.workspace_id = ? AND wt.build_context_hash = ? "
        "ORDER BY t.id LIMIT 1",
        (workspace_id, build_context_hash),
    ).fetchone()
    sysroot = ""
    toolchain_include_dirs: List[str] = []
    if tc_row:
        sysroot = tc_row[0] or ""
        if tc_row[1]:
            try:
                toolchain_include_dirs = json.loads(tc_row[1])
            except (json.JSONDecodeError, TypeError):
                pass

    return include_paths, sysroot, toolchain_include_dirs


def _resolve_callee_include_path(
    candidates: List[int],
    symbol_index: Dict[str, Any],
    search_paths: Dict[str, Any],
) -> Optional[Tuple[int, str, str]]:
    """
    第 4 级解析：基于 build_context search_paths 消除简名歧义。

    场景：callee_name 有多个 candidate（简名相同但定义在不同文件中），
    通过 build_context 的 include_paths 或 toolchain 的 sysroot/include_dirs
    判定哪个 candidate 是当前构建上下文应解析到的定义。

    匹配规则：
    - include_paths（workspace 相对路径）：candidate 的 rel_path 前缀匹配
    - sysroot + toolchain_include_dirs（绝对路径）：取 basename 前缀匹配 rel_path

    返回：
    - 唯一命中：(callee_id, callee_file, "include_path" | "sysroot")
    - 多匹配或无匹配：None（交由第 5 级 unresolved 处理）
    """
    file_for_symbol = symbol_index["file_for_symbol"]
    include_paths = search_paths.get("include_paths", [])
    sysroot = search_paths.get("sysroot", "")
    toolchain_include_dirs = search_paths.get("toolchain_include_dirs", [])

    # 规范化 search_paths（统一正斜杠 + 去尾部斜杠）
    norm_include_paths = [_norm_search_path(p) for p in include_paths if p]
    norm_sysroot = _norm_search_path(sysroot) if sysroot else ""
    norm_tc_dirs = [_norm_search_path(p) for p in toolchain_include_dirs if p]

    # 分别收集 include_path 命中和 sysroot 命中的 candidates
    include_hits: List[int] = []
    sysroot_hits: List[int] = []

    for sid in candidates:
        rel_path = file_for_symbol.get(sid, "")
        if not rel_path:
            continue
        norm_rel = _norm_search_path(rel_path)

        # 检查 include_paths（workspace 相对路径，前缀匹配）
        hit_include = False
        for inc in norm_include_paths:
            if inc and norm_rel.startswith(inc):
                include_hits.append(sid)
                hit_include = True
                break
        if hit_include:
            continue

        # 检查 sysroot + toolchain_include_dirs（绝对路径，basename 前缀匹配）
        for tc_dir in [norm_sysroot] + norm_tc_dirs:
            if not tc_dir:
                continue
            # 取 basename（如 /usr/include -> include）
            basename = tc_dir.rsplit("/", 1)[-1] if "/" in tc_dir else tc_dir
            if basename and norm_rel.startswith(basename):
                sysroot_hits.append(sid)
                break

    # include_path 优先于 sysroot
    if len(include_hits) == 1:
        sid = include_hits[0]
        return sid, file_for_symbol.get(sid, ""), "include_path"

    if len(sysroot_hits) == 1:
        sid = sysroot_hits[0]
        return sid, file_for_symbol.get(sid, ""), "sysroot"

    # 多匹配或无匹配 → None（交由 unresolved）
    return None


def _norm_search_path(path: str) -> str:
    """规范化 search path：统一正斜杠 + 去尾部斜杠"""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p
