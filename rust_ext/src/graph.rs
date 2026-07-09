//! B-PoC: 查询 + 存储下沉 Rust
//!
//! 实现 CSR 邻接表 + 内存索引 + rusqlite 加载，验证查询性能 vs Python SQL。
//!
//! 设计要点：
//! - SymbolTable: by_id (Vec) + by_qname / by_simple_name / by_file (HashMap 索引)
//! - CallGraph: CSR 压缩稀疏行邻接表，forward/backward 双份排序
//! - 加载: 从现有 SQLite (symbols + calls 表) 一次性读入内存
//! - 查询: 纯内存遍历，零 SQL，零磁盘 I/O
//!
//! 不做（避免过度工程化）：
//! - 不做 Staging 表 / Replicator（Phase 2，等 PoC 验证后）
//! - 不做 Watcher 增量更新
//! - 不做 MCP 协议层（保留 Python MCP Server）
//! - 不替换现有 Python 查询（旁路验证，对比性能后再决定）

use std::collections::{HashMap, HashSet, VecDeque};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};
use rusqlite::Connection;

// ============================================
// 数据结构
// ============================================

/// 图存储用的符号信息（含 id，用于 CSR 邻接表索引）
#[derive(Clone, Debug)]
pub struct GraphSymbol {
    pub id: u32,
    pub file_instance_id: u32,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub module_path: String,
    pub start_line: u32,
    pub end_line: u32,
    pub depth: i32,
    pub file_rel_path: String,
}

/// 符号表：紧凑存储 + 多维索引
pub struct SymbolTable {
    /// id → GraphSymbol（Vec 紧凑存储，O(1) 索引访问）
    pub by_id: Vec<GraphSymbol>,
    /// qualified_name → symbol_id
    pub by_qname: HashMap<String, u32>,
    /// simple_name → [symbol_id]（同名可能有多个）
    pub by_simple_name: HashMap<String, Vec<u32>>,
    /// file_instance_id → [symbol_id]
    pub by_file: HashMap<u32, Vec<u32>>,
}

/// 调用边（紧凑 Copy 类型，提升缓存命中）
#[derive(Clone, Copy, Debug)]
pub struct CallEdge {
    pub caller_id: u32,
    pub callee_id: u32,         // 0 表示未解析（外部符号）
    pub call_line: u32,
    pub is_cross_file: bool,
    /// callee_name 在 edges 中的索引（用于反向查询时获取 callee 名）
    /// 注：未解析边需要通过 callee_name 查 by_simple_name 找 callee_id
    pub callee_name_idx: u32,   // 指向 callee_names 数组的索引
}

/// 调用图：CSR 压缩稀疏行邻接表
pub struct CallGraph {
    /// 所有调用边（按 caller_id 升序排序）
    pub forward_edges: Vec<CallEdge>,
    /// CSR 偏移：forward_offsets[i..i+1] 给出 caller_id=i 的边范围
    /// 长度 = max_symbol_id + 2
    pub forward_offsets: Vec<usize>,

    /// 所有调用边（按 callee_id 升序排序，用于反向查询）
    pub backward_edges: Vec<CallEdge>,
    /// CSR 偏移：backward_offsets[i..i+1] 给出 callee_id=i 的边范围
    pub backward_offsets: Vec<usize>,

    /// 未解析边索引：callee_name_idx → [forward_edges 中的位置]
    /// 用于 get_callers 快速查找 callee_id=0 但 callee_name 匹配的边
    /// 避免全扫 forward_edges（O(E) → O(k)，k 为同名未解析边数）
    pub unresolved_by_name: HashMap<u32, Vec<usize>>,

    /// callee 名字池（edges 通过索引引用，避免重复分配 String）
    pub callee_names: Vec<String>,

    /// 反向索引：callee_name → callee_name_idx（O(1) 查找，避免每次扫池）
    pub callee_name_to_idx: HashMap<String, u32>,

    /// 顶层节点（无 caller 的函数，用于拓扑排序）
    pub roots: Vec<u32>,
}

// ============================================
// GraphStore: PyO3 类，封装加载 + 查询
// ============================================

/// PyO3 暴露的图存储类
/// 用法：
///   store = callwarden_core.GraphStore()
///   store.load_from_sqlite("~/.callwarden/xxx/callwarden.db")
///   callers = store.get_callers("function_name")
#[pyclass]
pub struct GraphStore {
    symbols: Option<SymbolTable>,
    calls: Option<CallGraph>,
}

#[pymethods]
impl GraphStore {
    /// 创建空 store
    #[new]
    fn new() -> Self {
        GraphStore { symbols: None, calls: None }
    }

    /// 从 SQLite 数据库加载 symbols + calls 到内存
    /// 返回加载的符号数 / 边数
    fn load_from_sqlite(&mut self, db_path: &str) -> PyResult<(usize, usize)> {
        // 只读 + immutable 模式打开：
        // - READ_ONLY: 不写入
        // - immutable=1: 告知 SQLite 数据库不会被修改，跳过 -wal/-shm 文件创建
        //   避免 WAL 模式下 rusqlite bundled SQLite 尝试创建 -shm 文件被沙箱拦截
        let flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
            | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
            | rusqlite::OpenFlags::SQLITE_OPEN_URI;
        // 转换 Windows 路径为 file: URI（immutable=1 跳过 WAL/SHM）
        let normalized = db_path.replace('\\', "/");
        let uri = if normalized.starts_with("//") || normalized.starts_with("file:") {
            // UNC 路径或已是 URI，直接加 immutable
            if normalized.contains('?') {
                format!("{}&immutable=1", normalized)
            } else {
                format!("{}?immutable=1", normalized)
            }
        } else {
            // 普通路径转 file: URI
            let prefix = if normalized.starts_with('/') { "file:" } else { "file:///" };
            format!("{}{}?immutable=1", prefix, normalized)
        };
        let conn = Connection::open_with_flags(&uri, flags)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("open db failed: {} (uri={})", e, uri)))?;

        // 1. 加载符号（JOIN file_instances 拿 rel_path）
        let mut by_id = Vec::new();
        let mut by_qname: HashMap<String, u32> = HashMap::new();
        let mut by_simple_name: HashMap<String, Vec<u32>> = HashMap::new();
        let mut by_file: HashMap<u32, Vec<u32>> = HashMap::new();

        let mut stmt = conn.prepare(
            "SELECT s.id, s.file_instance_id, s.kind, s.name, s.qualified_name,
                    s.module_path, s.start_line, s.end_line, s.depth, fi.rel_path
             FROM symbols s
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE fi.status != 'archived'"
        ).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("prepare symbols query failed: {}", e)))?;

        let symbol_iter = stmt.query_map([], |row| {
            Ok(GraphSymbol {
                id: row.get::<_, i64>(0)? as u32,
                file_instance_id: row.get::<_, i64>(1)? as u32,
                kind: row.get::<_, String>(2)?,
                name: row.get::<_, String>(3)?,
                qualified_name: row.get::<_, String>(4)?,
                module_path: row.get::<_, String>(5)?,
                start_line: row.get::<_, i64>(6)? as u32,
                end_line: row.get::<_, i64>(7)? as u32,
                depth: row.get::<_, i64>(8)? as i32,
                file_rel_path: row.get::<_, String>(9)?,
            })
        }).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("query symbols failed: {}", e)))?;

        for sym in symbol_iter {
            let sym = sym.map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("read symbol row failed: {}", e)))?;
            let id = sym.id;
            let qname = sym.qualified_name.clone();
            let simple = sym.name.clone();
            let fid = sym.file_instance_id;

            if id as usize >= by_id.len() {
                by_id.resize(id as usize + 1, GraphSymbol {
                    id: 0, file_instance_id: 0, kind: String::new(),
                    name: String::new(), qualified_name: String::new(),
                    module_path: String::new(), start_line: 0, end_line: 0,
                    depth: -1, file_rel_path: String::new(),
                });
            }
            by_id[id as usize] = sym;
            by_qname.insert(qname, id);
            by_simple_name.entry(simple).or_default().push(id);
            by_file.entry(fid).or_default().push(id);
        }

        let symbol_count = by_id.len();
        let symbols = SymbolTable { by_id, by_qname, by_simple_name, by_file };

        // 2. 加载调用关系
        let mut edges: Vec<CallEdge> = Vec::new();
        let mut callee_names: Vec<String> = Vec::new();
        let mut name_idx_map: HashMap<String, u32> = HashMap::new();

        let mut stmt = conn.prepare(
            "SELECT caller_id, callee_id, callee_name, call_line, is_cross_file FROM calls"
        ).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("prepare calls query failed: {}", e)))?;

        let call_iter = stmt.query_map([], |row| {
            let callee_name: String = row.get(2)?;
            Ok((
                row.get::<_, i64>(0)? as u32,   // caller_id
                row.get::<_, i64>(1)? as u32,   // callee_id
                callee_name,                     // callee_name
                row.get::<_, i64>(3)? as u32,   // call_line
                row.get::<_, i64>(4)? != 0,     // is_cross_file
            ))
        }).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("query calls failed: {}", e)))?;

        for call in call_iter {
            let (caller_id, callee_id, callee_name, call_line, is_cross_file) = call
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("read call row failed: {}", e)))?;

            // callee_name 池化去重
            let callee_name_idx = match name_idx_map.get(&callee_name) {
                Some(&idx) => idx,
                None => {
                    let idx = callee_names.len() as u32;
                    callee_names.push(callee_name.clone());
                    name_idx_map.insert(callee_name, idx);
                    idx
                }
            };

            edges.push(CallEdge {
                caller_id, callee_id, call_line, is_cross_file, callee_name_idx,
            });
        }

        let edge_count = edges.len();

        // 3. 构建 CSR 邻接表（forward + backward 双份排序）
        let max_id = symbols.by_id.len().max(1) - 1;
        let mut calls = build_csr(edges, callee_names, max_id);

        // 4. 计算根节点（无 caller 的真实符号）
        // 遍历 backward_edges 中 callee_id != 0 的边，标记被调用的符号
        let mut has_caller = vec![false; symbols.by_id.len()];
        for e in &calls.backward_edges {
            if e.callee_id != 0 && (e.callee_id as usize) < has_caller.len() {
                has_caller[e.callee_id as usize] = true;
            }
        }
        for (idx, sym) in symbols.by_id.iter().enumerate() {
            // 跳过空槽位 + 跳过非函数（只有 fn/test_fn/method 才进调用图）
            if sym.id == 0 && sym.name.is_empty() { continue; }
            if !has_caller[idx] && sym.kind == "fn" {
                calls.roots.push(idx as u32);
            }
        }

        // 5. 构建未解析边索引：callee_name_idx → [forward_edges 位置]
        // 用于 get_callers 快速查找 callee_id=0 但 callee_name 匹配的边
        for (idx, edge) in calls.forward_edges.iter().enumerate() {
            if edge.callee_id == 0 {
                calls.unresolved_by_name.entry(edge.callee_name_idx).or_default().push(idx);
            }
        }

        // 6. 构建反向索引：callee_name → callee_name_idx（O(1) 查找）
        // 复用已构建的 name_idx_map，避免每次 get_callers 扫池
        calls.callee_name_to_idx = name_idx_map;

        self.symbols = Some(symbols);
        self.calls = Some(calls);

        Ok((symbol_count, edge_count))
    }

    /// 查询谁调用了这个函数（对齐 Python db_query.get_callers）
    /// 入参：callee_name（简名，对齐 Python 接口）
    ///
    /// B-P6 优化：O(E) 全扫 → O(degree + k)
    /// - 已解析边：CSR backward_offsets 按 callee_id 定位（O(degree) per callee_id）
    /// - 未解析边：unresolved_by_name 索引按 callee_name_idx 定位（O(k)，k=同名未解析边数）
    /// - callee_name → callee_name_idx：O(1) 反向索引
    fn get_callers<'py>(&self, py: Python<'py>, callee_name: &str) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let mut results = Vec::new();

        // 1. O(1) 查找 callee_name_idx（反向索引，避免扫池）
        let callee_name_idx = calls.callee_name_to_idx.get(callee_name).copied();

        // 2. 已解析边：通过 CSR backward_offsets 按 callee_id 定位 O(degree)
        //    callee_name 可能匹配多个 symbol_id（同名函数）
        let callee_ids: Vec<u32> = symbols.by_simple_name.get(callee_name)
            .cloned()
            .unwrap_or_default();

        for callee_id in &callee_ids {
            let start = calls.backward_offsets.get(*callee_id as usize)
                .copied().unwrap_or(0);
            let end = calls.backward_offsets.get(*callee_id as usize + 1)
                .copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.backward_edges[i];
                // backward_offsets[0] 区段是未解析边，跳过（由 step 3 处理）
                if edge.callee_id == 0 { continue; }

                if let Some(caller) = symbols.by_id.get(edge.caller_id as usize) {
                    let dict = PyDict::new(py);
                    dict.set_item("caller_name", &caller.name)?;
                    dict.set_item("caller_qualified", &caller.qualified_name)?;
                    dict.set_item("caller_file", &caller.file_rel_path)?;
                    dict.set_item("caller_module", &caller.module_path)?;
                    dict.set_item("call_line", edge.call_line)?;
                    dict.set_item("is_cross_file", edge.is_cross_file)?;
                    results.push(dict.into_any());
                }
            }
        }

        // 3. 未解析边：通过 unresolved_by_name 索引按 callee_name_idx 定位 O(k)
        //    即使有已解析的同名符号，未解析边也应返回（对齐原逻辑）
        if let Some(idx) = callee_name_idx {
            if let Some(positions) = calls.unresolved_by_name.get(&idx) {
                for &pos in positions {
                    let edge = &calls.forward_edges[pos];
                    if let Some(caller) = symbols.by_id.get(edge.caller_id as usize) {
                        let dict = PyDict::new(py);
                        dict.set_item("caller_name", &caller.name)?;
                        dict.set_item("caller_qualified", &caller.qualified_name)?;
                        dict.set_item("caller_file", &caller.file_rel_path)?;
                        dict.set_item("caller_module", &caller.module_path)?;
                        dict.set_item("call_line", edge.call_line)?;
                        dict.set_item("is_cross_file", edge.is_cross_file)?;
                        results.push(dict.into_any());
                    }
                }
            }
        }

        Ok(results)
    }

    /// 查询这个函数调用了谁（对齐 Python db_query.get_callees）
    /// 入参：caller_name（简名）
    fn get_callees<'py>(&self, py: Python<'py>, caller_name: &str) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let mut results = Vec::new();

        // caller_name 匹配多个 symbol_id（同名函数）
        let caller_ids = symbols.by_simple_name.get(caller_name)
            .cloned()
            .unwrap_or_default();

        for caller_id in caller_ids {
            // CSR forward 遍历：caller_id 的所有边
            let start = calls.forward_offsets.get(caller_id as usize)
                .copied().unwrap_or(0);
            let end = calls.forward_offsets.get(caller_id as usize + 1)
                .copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.forward_edges[i];
                let callee_name = calls.callee_names.get(edge.callee_name_idx as usize)
                    .map(|s| s.as_str()).unwrap_or("");
                let callee_qname = if edge.callee_id != 0 {
                    symbols.by_id.get(edge.callee_id as usize)
                        .map(|s| s.qualified_name.as_str()).unwrap_or("")
                } else { "" };
                let callee_file = if edge.callee_id != 0 {
                    symbols.by_id.get(edge.callee_id as usize)
                        .map(|s| s.file_rel_path.as_str()).unwrap_or("")
                } else { "" };

                let dict = PyDict::new(py);
                dict.set_item("callee_name", callee_name)?;
                dict.set_item("callee_qualified", callee_qname)?;
                dict.set_item("callee_file", callee_file)?;
                dict.set_item("call_line", edge.call_line)?;
                dict.set_item("is_cross_file", edge.is_cross_file)?;
                results.push(dict.into_any());
            }
        }

        Ok(results)
    }

    /// 通过 qualified_name 获取符号详情
    fn get_symbol<'py>(&self, py: Python<'py>, qualified_name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        if let Some(&id) = symbols.by_qname.get(qualified_name) {
            if let Some(sym) = symbols.by_id.get(id as usize) {
                let dict = PyDict::new(py);
                dict.set_item("id", sym.id)?;
                dict.set_item("name", &sym.name)?;
                dict.set_item("kind", &sym.kind)?;
                dict.set_item("qualified_name", &sym.qualified_name)?;
                dict.set_item("module_path", &sym.module_path)?;
                dict.set_item("start_line", sym.start_line)?;
                dict.set_item("end_line", sym.end_line)?;
                dict.set_item("depth", sym.depth)?;
                dict.set_item("file_rel_path", &sym.file_rel_path)?;
                return Ok(Some(dict.into_any()));
            }
        }
        Ok(None)
    }

    /// 搜索符号（子串匹配，PoC 简化版，未上 FTS5）
    /// 对齐 Python db_query.search_symbols
    #[pyo3(signature = (query, kind=None, limit=None))]
    fn search_symbols<'py>(&self, py: Python<'py>, query: &str, kind: Option<&str>, limit: Option<usize>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let limit = limit.unwrap_or(50);
        let query_lower = query.to_lowercase();
        let mut results = Vec::new();

        // PoC: 遍历 by_id，子串匹配 name 或 qualified_name
        // 优化点：后续上 FTS5 trigram 或 SuffixIndex
        for sym in &symbols.by_id {
            if sym.id == 0 && sym.name.is_empty() { continue; }  // 跳过空槽位
            if let Some(k) = kind {
                if sym.kind != k { continue; }
            }
            let name_match = sym.name.to_lowercase().contains(&query_lower);
            let qname_match = sym.qualified_name.to_lowercase().contains(&query_lower);
            if name_match || qname_match {
                let dict = PyDict::new(py);
                dict.set_item("id", sym.id)?;
                dict.set_item("name", &sym.name)?;
                dict.set_item("kind", &sym.kind)?;
                dict.set_item("qualified_name", &sym.qualified_name)?;
                dict.set_item("file_rel_path", &sym.file_rel_path)?;
                dict.set_item("start_line", sym.start_line)?;
                results.push(dict.into_any());
                if results.len() >= limit { break; }
            }
        }

        Ok(results)
    }

    /// 向下调用链（BFS，对齐 Python db_query.get_call_chain_down）
    /// 入参：qualified_name + max_depth
    fn get_call_chain_down<'py>(&self, py: Python<'py>, qualified_name: &str, max_depth: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let mut results = Vec::new();
        let start_id = match symbols.by_qname.get(qualified_name) {
            Some(&id) => id,
            None => return Ok(results),  // 起点不存在，空结果
        };

        // BFS: (symbol_id, depth)
        let mut visited: HashSet<u32> = HashSet::new();
        let mut queue: VecDeque<(u32, usize)> = VecDeque::new();
        visited.insert(start_id);
        queue.push_back((start_id, 0));

        while let Some((sym_id, depth)) = queue.pop_front() {
            if depth >= max_depth { continue; }

            // CSR forward 遍历 callees
            let start = calls.forward_offsets.get(sym_id as usize)
                .copied().unwrap_or(0);
            let end = calls.forward_offsets.get(sym_id as usize + 1)
                .copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.forward_edges[i];
                let callee_name = calls.callee_names.get(edge.callee_name_idx as usize)
                    .map(|s| s.as_str()).unwrap_or("");

                let dict = PyDict::new(py);
                let caller_sym = symbols.by_id.get(sym_id as usize);
                dict.set_item("depth", depth)?;
                dict.set_item("caller_name", caller_sym.map(|s| s.name.as_str()).unwrap_or(""))?;
                dict.set_item("callee_name", callee_name)?;
                dict.set_item("callee_id", edge.callee_id)?;
                dict.set_item("call_line", edge.call_line)?;
                dict.set_item("is_cross_file", edge.is_cross_file)?;
                results.push(dict.into_any());

                // 继续向下 BFS（仅已解析边）
                if edge.callee_id != 0 && visited.insert(edge.callee_id) {
                    queue.push_back((edge.callee_id, depth + 1));
                }
            }
        }

        Ok(results)
    }

    /// 拓扑排序（Kahn 算法，对齐 Python CLI --topo）
    /// 返回 qualified_name 列表，按调用深度升序（被调用者在前）
    fn get_topological_order(&self) -> PyResult<Vec<String>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let n = symbols.by_id.len();

        // 计算入度（被多少个 caller 调用）
        let mut in_degree = vec![0u32; n];

        // 仅统计已解析边（callee_id > 0）
        for edge in &calls.forward_edges {
            if edge.callee_id != 0 && (edge.callee_id as usize) < n {
                in_degree[edge.callee_id as usize] += 1;
            }
        }

        // 入度为 0 的节点入队（根函数）
        let mut queue: VecDeque<usize> = VecDeque::new();
        for i in 0..n {
            // 跳过空槽位（id 0 或未填充）
            if symbols.by_id[i].id == 0 && symbols.by_id[i].name.is_empty() { continue; }
            if in_degree[i] == 0 {
                queue.push_back(i);
            }
        }

        let mut order = Vec::with_capacity(n);
        while let Some(idx) = queue.pop_front() {
            let sym = &symbols.by_id[idx];
            if sym.id == 0 && sym.name.is_empty() { continue; }
            order.push(sym.qualified_name.clone());

            // 遍历该节点的 callees，减小入度
            let start = calls.forward_offsets.get(idx).copied().unwrap_or(0);
            let end = calls.forward_offsets.get(idx + 1).copied().unwrap_or(0);
            for i in start..end {
                let edge = &calls.forward_edges[i];
                if edge.callee_id != 0 && (edge.callee_id as usize) < n {
                    let ci = edge.callee_id as usize;
                    if in_degree[ci] > 0 {
                        in_degree[ci] -= 1;
                        if in_degree[ci] == 0 {
                            queue.push_back(ci);
                        }
                    }
                }
            }
        }

        Ok(order)
    }

    /// 检测调用图中的环（DFS 三色标记，对齐 Python CLI --detect-cycles）
    /// 返回环上的 qualified_name 列表
    fn detect_cycles(&self) -> PyResult<Vec<Vec<String>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let n = symbols.by_id.len();
        // 0=white(未访问), 1=gray(在栈中), 2=black(已完成)
        let mut color = vec![0u8; n];
        let mut parent: Vec<i64> = vec![-1; n];
        let mut cycles: Vec<Vec<String>> = Vec::new();

        fn dfs(
            u: usize,
            symbols: &SymbolTable,
            calls: &CallGraph,
            color: &mut [u8],
            parent: &mut [i64],
            cycles: &mut Vec<Vec<String>>,
        ) {
            color[u] = 1;  // gray
            let start = calls.forward_offsets.get(u).copied().unwrap_or(0);
            let end = calls.forward_offsets.get(u + 1).copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.forward_edges[i];
                if edge.callee_id == 0 { continue; }
                let v = edge.callee_id as usize;
                if v >= symbols.by_id.len() { continue; }

                if color[v] == 0 {
                    parent[v] = u as i64;
                    dfs(v, symbols, calls, color, parent, cycles);
                } else if color[v] == 1 {
                    // 发现回边，提取环
                    let mut cycle = Vec::new();
                    let mut cur = u as i64;
                    while cur != -1 && cur != v as i64 {
                        if let Some(sym) = symbols.by_id.get(cur as usize) {
                            cycle.push(sym.qualified_name.clone());
                        }
                        cur = parent[cur as usize];
                    }
                    if let Some(sym) = symbols.by_id.get(v) {
                        cycle.push(sym.qualified_name.clone());
                    }
                    cycle.reverse();
                    if cycle.len() > 1 {
                        cycles.push(cycle);
                    }
                }
            }
            color[u] = 2;  // black
        }

        for i in 0..n {
            let sym = &symbols.by_id[i];
            if sym.id == 0 && sym.name.is_empty() { continue; }
            if color[i] == 0 {
                dfs(i, symbols, calls, &mut color, &mut parent, &mut cycles);
            }
        }

        Ok(cycles)
    }

    /// 统计信息（符号数 / 边数 / 已解析边数 / 索引大小）
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let dict = PyDict::new(py);
        if let Some(symbols) = &self.symbols {
            dict.set_item("symbol_count", symbols.by_id.len())?;
            dict.set_item("qname_index_size", symbols.by_qname.len())?;
            dict.set_item("simple_name_index_size", symbols.by_simple_name.len())?;
            dict.set_item("file_index_size", symbols.by_file.len())?;
        } else {
            dict.set_item("symbol_count", 0)?;
            dict.set_item("qname_index_size", 0)?;
            dict.set_item("simple_name_index_size", 0)?;
            dict.set_item("file_index_size", 0)?;
        }
        if let Some(calls) = &self.calls {
            let resolved = calls.forward_edges.iter().filter(|e| e.callee_id != 0).count();
            dict.set_item("edge_count", calls.forward_edges.len())?;
            dict.set_item("resolved_edge_count", resolved)?;
            dict.set_item("forward_offsets_size", calls.forward_offsets.len())?;
            dict.set_item("backward_offsets_size", calls.backward_offsets.len())?;
            dict.set_item("callee_name_pool_size", calls.callee_names.len())?;
            dict.set_item("root_count", calls.roots.len())?;
        } else {
            dict.set_item("edge_count", 0)?;
        }
        Ok(dict.into_any())
    }
}

// ============================================
// CSR 构建
// ============================================

/// 从边列表构建 CSR 邻接表
/// forward: 按 caller_id 排序
/// backward: 按 callee_id 排序（用于 get_callers 反向查询）
fn build_csr(edges: Vec<CallEdge>, callee_names: Vec<String>, max_id: usize) -> CallGraph {
    let n = max_id + 1;  // id 范围 [0, max_id]

    // 1. forward: 按 caller_id 排序
    let mut forward_edges = edges.clone();
    forward_edges.sort_by_key(|e| e.caller_id);

    let mut forward_offsets = vec![0usize; n + 1];
    for e in &forward_edges {
        if (e.caller_id as usize) < n {
            forward_offsets[e.caller_id as usize + 1] += 1;
        }
    }
    // 前缀和
    for i in 1..=n {
        forward_offsets[i] += forward_offsets[i - 1];
    }

    // 2. backward: 按 callee_id 排序（未解析边 callee_id=0 排最前）
    let mut backward_edges = edges;
    backward_edges.sort_by_key(|e| e.callee_id);

    let mut backward_offsets = vec![0usize; n + 1];
    for e in &backward_edges {
        if (e.callee_id as usize) < n {
            backward_offsets[e.callee_id as usize + 1] += 1;
        }
    }
    for i in 1..=n {
        backward_offsets[i] += backward_offsets[i - 1];
    }

    // callee_name_to_idx 在 load_from_sqlite 完成后由调用方补充
    // roots 也在 load_from_sqlite 完成后由调用方补充（需要 symbols 信息）
    CallGraph {
        forward_edges,
        forward_offsets,
        backward_edges,
        backward_offsets,
        unresolved_by_name: HashMap::new(),
        callee_names,
        callee_name_to_idx: HashMap::new(),
        roots: Vec::new(),
    }
}
