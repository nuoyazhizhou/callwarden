//! B-PoC: 查询 + 存储下沉 Rust
//!
//! 实现 CSR 邻接表 + 内存索引 + rusqlite 加载，验证查询性能 vs Python SQL。
//!
//! 设计要点：
//! - SymbolTable: by_id (Vec) + 名称排序数组 + 字符串池
//! - CallGraph: CSR 压缩稀疏行邻接表，forward/backward 双份排序
//! - 加载: 从现有 SQLite (symbols + calls 表) 一次性读入内存
//! - 查询: 纯内存遍历，零 SQL，零磁盘 I/O
//!
//! 不做（避免过度工程化）：
//! - 不做 Staging 表 / Replicator（Phase 2，等 PoC 验证后）
//! - 不做 Watcher 增量更新
//! - 不做 MCP 协议层（保留 Python MCP Server）
//! - 不替换现有 Python 查询（旁路验证，对比性能后再决定）

use std::collections::{HashSet, VecDeque};
use std::sync::Arc;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::Connection;
// P6 优化：FxHashMap 比 HashMap（SipHash）快 5-10x，非加密哈希无 DoS 防护开销
use rustc_hash::FxHashMap;

// ============================================
// 数据结构
// ============================================

/// 符号种类枚举（P1 优化：String → enum，省 ~200MB / 200万符号）
/// 覆盖所有 parser 产出的 kind 值：fn/test_fn/method/class/struct/enum/union/
/// interface/trait/const/module
///
/// P5 优化：repr(u32) 而非 repr(u8)，避免 GraphSymbol 内 3 字节 padding
/// （bytemuck::Pod 不允许 padding）。多耗费 3 字节/符号 = 6MB/200万符号，可接受。
/// P5 注：bytemuck derive 不支持 #[repr(u32)] enum，需手动 unsafe impl Pod/Zeroable
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
#[repr(u32)]
pub enum SymbolKind {
    #[default] Unknown = 0,
    Fn = 1,
    TestFn = 2,
    Method = 3,
    Class = 4,
    Struct = 5,
    Enum = 6,
    Union = 7,
    Interface = 8,
    Trait = 9,
    Const = 10,
    Module = 11,
}

// P5: bytemuck derive 不支持 #[repr(u32)] enum，手动实现 Pod/Zeroable
// 安全性：SymbolKind 是 fieldless enum，#[repr(u32)] 保证布局与 u32 一致
unsafe impl bytemuck::Pod for SymbolKind {}
unsafe impl bytemuck::Zeroable for SymbolKind {}

impl SymbolKind {
    /// 从数据库 kind 字符串解析为枚举
    pub fn from_db_str(s: &str) -> Self {
        match s {
            "fn" => SymbolKind::Fn,
            "test_fn" => SymbolKind::TestFn,
            "method" => SymbolKind::Method,
            "class" => SymbolKind::Class,
            "struct" => SymbolKind::Struct,
            "enum" => SymbolKind::Enum,
            "union" => SymbolKind::Union,
            "interface" => SymbolKind::Interface,
            "trait" => SymbolKind::Trait,
            "const" => SymbolKind::Const,
            "module" => SymbolKind::Module,
            _ => SymbolKind::Unknown,
        }
    }

    /// 转回字符串（用于 Python 接口返回）
    pub fn as_str(&self) -> &'static str {
        match self {
            SymbolKind::Unknown => "unknown",
            SymbolKind::Fn => "fn",
            SymbolKind::TestFn => "test_fn",
            SymbolKind::Method => "method",
            SymbolKind::Class => "class",
            SymbolKind::Struct => "struct",
            SymbolKind::Enum => "enum",
            SymbolKind::Union => "union",
            SymbolKind::Interface => "interface",
            SymbolKind::Trait => "trait",
            SymbolKind::Const => "const",
            SymbolKind::Module => "module",
        }
    }
}

/// 图存储用的符号信息（含 id，用于 CSR 邻接表索引）
///
/// P7 优化：name/qualified_name/module_path 改为 (offset, len) 指向全局 string pool
/// - struct 从 96 字节 → 48 字节
/// - 消除 200万 × 3 = 600万次 String 堆分配（每次至少 24 字节元数据）
/// - 总省 ~400MB / 200万符号
///
/// P5 优化：实现 Pod/Zeroable，支持 dump/load 零拷贝
#[derive(Clone, Copy, Debug, bytemuck::Pod, bytemuck::Zeroable)]
#[repr(C)]
pub struct GraphSymbol {
    pub id: u32,
    pub file_instance_id: u32,
    pub kind: SymbolKind,
    // P7: string pool offset + len，替代独立 String
    pub name_offset: u32,
    pub name_len: u32,
    pub qname_offset: u32,
    pub qname_len: u32,
    pub module_offset: u32,
    pub module_len: u32,
    pub start_line: u32,
    pub end_line: u32,
    pub depth: i32,
}

/// 符号表：紧凑存储 + 多维索引
pub struct SymbolTable {
    /// id → GraphSymbol（Vec 紧凑存储，O(1) 索引访问）
    pub by_id: Vec<GraphSymbol>,
    /// qualified_name → symbol_id
    /// P4: 删除 by_qname_keys: Vec<String> 和 by_qname_values: Vec<u32>
    /// 改用排序的 symbol_id 数组，从 qname_pool 切片比较，消除 100万 String 堆分配（省 56MB）
    pub by_qname_sorted_ids: Vec<u32>,
    /// 按 simple_name 排序的 symbol_id，同名符号形成连续区间
    pub by_simple_name_sorted_ids: Vec<u32>,
    /// file_instance_id → rel_path（P3 优化：替代 GraphSymbol.file_rel_path）
    /// P4: 改为 pool + offsets，消除 20万 String 堆分配（省 11MB）
    pub file_paths_pool: String,
    pub file_paths_offsets: Vec<u32>,
    /// P7: 全局 string pool，存所有符号的 name
    pub name_pool: String,
    /// P7: 全局 string pool，存所有符号的 qualified_name
    pub qname_pool: String,
    /// P7: 全局 string pool，存所有符号的 module_path
    pub module_pool: String,
    /// P2: 搜索索引 — 所有 name + qname 的小写版本，\0 分隔
    /// memchr SIMD 一次扫描整个池，替代 N 次子串搜索
    pub search_pool_lower: String,
    /// P2: 每个条目在 search_pool_lower 中的起始偏移（排序，用于二分查找）
    pub search_entry_offsets: Vec<u32>,
    /// P2: 每个条目对应的 symbol_id
    pub search_entry_sym_ids: Vec<u32>,
}

impl SymbolTable {
    /// 通过 file_instance_id 获取 rel_path（P3 辅助方法）
    /// P4: 从 file_paths_pool 切片读取，消除 Vec<String> 堆分配
    #[inline]
    pub fn file_rel_path(&self, file_instance_id: u32) -> &str {
        let i = file_instance_id as usize;
        if i + 1 < self.file_paths_offsets.len() {
            let start = self.file_paths_offsets[i] as usize;
            let end = self.file_paths_offsets[i + 1] as usize;
            self.file_paths_pool.get(start..end).unwrap_or("")
        } else {
            ""
        }
    }

    /// P7: 获取符号 name（从 string pool 读取）
    #[inline]
    pub fn sym_name(&self, sym: &GraphSymbol) -> &str {
        let start = sym.name_offset as usize;
        let end = start + sym.name_len as usize;
        self.name_pool.get(start..end).unwrap_or("")
    }

    /// P7: 获取符号 qualified_name（从 string pool 读取）
    #[inline]
    pub fn sym_qname(&self, sym: &GraphSymbol) -> &str {
        let start = sym.qname_offset as usize;
        let end = start + sym.qname_len as usize;
        self.qname_pool.get(start..end).unwrap_or("")
    }

    /// P7: 获取符号 module_path（从 string pool 读取）
    #[inline]
    pub fn sym_module(&self, sym: &GraphSymbol) -> &str {
        let start = sym.module_offset as usize;
        let end = start + sym.module_len as usize;
        self.module_pool.get(start..end).unwrap_or("")
    }

    /// P4: by_qname 排序数组查找（从 qname_pool 切片比较，消除 100万 String 堆分配）
    #[inline]
    pub fn qname_get(&self, qname: &str) -> Option<u32> {
        self.by_qname_sorted_ids
            .binary_search_by(|sid| {
                let sym = &self.by_id[*sid as usize];
                let s = self.sym_qname(sym);
                s.cmp(qname)
            })
            .ok()
            .map(|i| self.by_qname_sorted_ids[i])
    }

    /// 返回 simple_name 对应的连续 symbol_id 区间。
    #[inline]
    pub fn simple_name_ids(&self, name: &str) -> &[u32] {
        let start = self.by_simple_name_sorted_ids.partition_point(|sid| {
            self.sym_name(&self.by_id[*sid as usize]) < name
        });
        let end = self.by_simple_name_sorted_ids.partition_point(|sid| {
            self.sym_name(&self.by_id[*sid as usize]) <= name
        });
        &self.by_simple_name_sorted_ids[start..end]
    }
}

/// 调用边（紧凑 Copy 类型，提升缓存命中）
///
/// P8 优化：is_cross_file 塞到 call_line 高位，struct 从 20 → 16 字节
/// - call_line 实际只用到 ~20 位（行号通常 < 100万），高 1 位存 is_cross_file
/// - 1400万边 × 省 4 字节 = 56 MB
#[derive(Clone, Copy, Debug, bytemuck::Pod, bytemuck::Zeroable)]
#[repr(C)]
pub struct CallEdge {
    pub caller_id: u32,
    pub callee_id: u32,         // 0 表示未解析（外部符号）
    /// P8: call_line 高 1 位存 is_cross_file，低 31 位存实际行号
    pub call_line_packed: u32,
    /// callee_name 在 edges 中的索引（用于反向查询时获取 callee 名）
    pub callee_name_idx: u32,
}

impl CallEdge {
    /// 获取实际 call_line（低 31 位）
    #[inline]
    pub fn call_line(&self) -> u32 {
        self.call_line_packed & 0x7FFF_FFFF
    }

    /// 获取 is_cross_file（最高位）
    #[inline]
    pub fn is_cross_file(&self) -> bool {
        (self.call_line_packed >> 31) & 1 == 1
    }

    /// 打包 call_line + is_cross_file
    #[inline]
    pub fn pack_call_line(call_line: u32, is_cross_file: bool) -> u32 {
        (call_line & 0x7FFF_FFFF) | ((is_cross_file as u32) << 31)
    }
}

/// G7-T4: 调用链 BFS 边的结果信息（daemon 序列化为 JSON 返回给客户端）
///
/// 与 PyO3 `get_call_chain_down` 返回的 PyDict 字段对齐：
/// depth / caller_name / callee_name / callee_id / call_line / is_cross_file
#[derive(Clone, Debug)]
pub struct CallChainEdgeInfo {
    pub depth: usize,
    pub caller_name: String,
    pub callee_name: String,
    pub callee_id: u32,
    pub call_line: u32,
    pub is_cross_file: bool,
}

/// 调用图：CSR 压缩稀疏行邻接表
pub struct CallGraph {
    /// 所有调用边（按 caller_id 升序排序）
    pub forward_edges: Vec<CallEdge>,
    /// CSR 偏移：forward_offsets[i..i+1] 给出 caller_id=i 的边范围
    /// 长度 = max_symbol_id + 2
    pub forward_offsets: Vec<usize>,

    /// `forward_edges` 位置，按对应边的 callee_id 升序排列。
    /// 反向索引不再重复存储 callee_id/caller_id，每边从 8 字节降为 4 字节。
    pub backward_positions: Vec<u32>,
    /// CSR 偏移：backward_offsets[i..i+1] 给出 callee_id=i 的边范围
    pub backward_offsets: Vec<usize>,

    /// callee_name_idx → forward_edges 位置的 CSR 索引。
    pub callee_position_offsets: Vec<u32>,
    pub callee_positions: Vec<u32>,

    /// callee 名字池（P5 优化：紧凑存储，所有 name 拼接在一个 String 中）
    /// 替代 Vec<String>，消除 N 个 String 堆分配开销
    pub callee_names_pool: String,
    /// callee name 偏移表：offsets[i]..offsets[i+1] 给出第 i 个 name 的字节范围
    pub callee_names_offsets: Vec<u32>,

    /// 按名称排序的 callee_name_idx，查询时从字符串池二分。
    pub callee_name_sorted_idxs: Vec<u32>,

    /// 顶层节点（无 caller 的函数，用于拓扑排序）
    pub roots: Vec<u32>,
}

impl CallGraph {
    /// 通过 callee_name_idx 获取 name（P5 优化：从 string pool 读取）
    #[inline]
    pub fn callee_name(&self, idx: u32) -> &str {
        let i = idx as usize;
        if i + 1 < self.callee_names_offsets.len() {
            let start = self.callee_names_offsets[i] as usize;
            let end = self.callee_names_offsets[i + 1] as usize;
            &self.callee_names_pool[start..end]
        } else {
            ""
        }
    }

    /// 从紧凑字符串池二分查找 callee_name_idx。
    #[inline]
    pub fn callee_name_idx(&self, name: &str) -> Option<u32> {
        self.callee_name_sorted_idxs
            .binary_search_by(|idx| self.callee_name(*idx).cmp(name))
            .ok()
            .map(|i| self.callee_name_sorted_idxs[i])
    }

    /// 返回指定 callee_name_idx 对应的 forward_edges 位置。
    #[inline]
    pub fn positions_for_callee_name(&self, idx: u32) -> &[u32] {
        let i = idx as usize;
        if i + 1 >= self.callee_position_offsets.len() {
            return &[];
        }
        let start = self.callee_position_offsets[i] as usize;
        let end = self.callee_position_offsets[i + 1] as usize;
        &self.callee_positions[start..end]
    }
}

// ============================================
// GraphStore: PyO3 类，封装加载 + 查询
// ============================================

/// P10: 单个 caller 查询结果（纯 Rust Copy 结构体，零 Python 开销）
/// 4 × u32 = 16 字节/结果，58660 结果 = 938 KB
#[derive(Clone, Copy)]
struct CallerResult {
    caller_id: u32,
    callee_id: u32,
    callee_name_idx: u32,
    call_line_packed: u32,
}

/// PyO3 暴露的图存储类
/// 用法：
///   store = callwarden_core.GraphStore()
///   store.load_from_sqlite("~/.callwarden/xxx/callwarden.db")
///   callers = store.get_callers("function_name")
#[pyclass]
pub struct GraphStore {
    symbols: Option<Arc<SymbolTable>>,
    calls: Option<Arc<CallGraph>>,
}

#[pymethods]
impl GraphStore {
    /// 创建空 store
    #[new]
    pub fn new() -> Self {
        GraphStore { symbols: None, calls: None }
    }

    /// 从 SQLite 数据库加载 symbols + calls 到内存
    /// 返回加载的符号数 / 边数
    ///
    /// P0-2 整改（2026-07-22 复审整改-v2）：新增 `workspace_id` 参数过滤
    /// 用户级单库多 workspace 数据。`workspace_id=0` 表示不过滤（兼容旧测试）。
    /// 生产路径（daemon / db_base）必须传 >0 的 workspace_id，避免 snapshot
    /// 混入其他 workspace 的符号。
    #[pyo3(signature = (db_path, workspace_id=0))]
    pub fn load_from_sqlite(
        &mut self,
        py: Python<'_>,
        db_path: &str,
        workspace_id: i64,
    ) -> PyResult<(usize, usize)> {
        py.detach(|| self._load_from_sqlite_stage(db_path, workspace_id, true))
    }

    /// 仅加载文件和符号索引，不读取 calls 表。
    ///
    /// 用于分级冷启动：调用方可先发布 symbols-ready store，
    /// 后台构建完整图后再原子替换。
    #[pyo3(signature = (db_path, workspace_id=0))]
    pub fn load_symbols_from_sqlite(
        &mut self,
        py: Python<'_>,
        db_path: &str,
        workspace_id: i64,
    ) -> PyResult<usize> {
        py.detach(|| self._load_from_sqlite_stage(db_path, workspace_id, false))
            .map(|(symbols, _)| symbols)
    }

    /// 创建一个共享当前符号层的新 store，供后台仅构建调用图。
    pub fn fork_symbols(&self) -> PyResult<Self> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("symbols not ready"))?;
        Ok(Self {
            symbols: Some(Arc::clone(symbols)),
            calls: None,
        })
    }

    /// 复用已加载的符号层，仅从 SQLite 加载 calls 并构建 CSR。
    #[pyo3(signature = (db_path, workspace_id=0))]
    pub fn load_calls_from_sqlite(
        &mut self,
        py: Python<'_>,
        db_path: &str,
        workspace_id: i64,
    ) -> PyResult<usize> {
        py.detach(|| self._load_calls_from_sqlite(db_path, workspace_id))
    }

    /// 返回当前加载阶段，供 Python/daemon 选择查询路径。
    pub fn load_state(&self) -> &'static str {
        if self.calls.is_some() {
            "graph_ready"
        } else if self.symbols.is_some() {
            "symbols_ready"
        } else {
            "empty"
        }
    }

    /// 内部共享加载路径，`include_calls=false` 时在符号索引完成后返回。
    ///
    /// P0-2 整改（2026-07-22 复审整改-v2）：`workspace_id` 参数过滤用户级
    /// 单库多 workspace 数据。`workspace_id=0` 不过滤（兼容旧测试和单 workspace DB），
    /// `workspace_id>0` 时在 SQL 层用 `WHERE workspace_id = ?` 过滤 file_instances
    /// 和 symbols，避免 snapshot 混入其他 workspace 的符号。
    fn _load_from_sqlite_stage(
        &mut self,
        db_path: &str,
        workspace_id: i64,
        include_calls: bool,
    ) -> PyResult<(usize, usize)> {
        let conn = open_immutable_db(db_path)?;

        // P0-2: 动态构建 WHERE 条件——workspace_id>0 时过滤，=0 时不过滤（兼容）
        let ws_filter = if workspace_id > 0 {
            format!("AND workspace_id = {}", workspace_id)
        } else {
            String::new()
        };

        // 1a. 先加载 file_paths（P3 优化：file_instance_id → rel_path 独立表）
        // P4: 改为 pool + offsets，消除 20万 String 堆分配（省 11MB）
        let mut file_paths_pool = String::new();
        let mut file_paths_offsets: Vec<u32> = Vec::new();
        {
            let sql_files = format!(
                "SELECT id, rel_path FROM file_instances WHERE status != 'archived' {} ORDER BY id",
                ws_filter
            );
            let mut stmt_files = conn.prepare(&sql_files)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("prepare file_instances query failed: {}", e)))?;
            let file_iter = stmt_files.query_map([], |row| {
                Ok((row.get::<_, i64>(0)? as u32, row.get::<_, String>(1)?))
            }).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("query file_instances failed: {}", e)))?;
            // 用临时 Vec 收集 (fid, rel_path)，然后构建 pool
            let mut file_list: Vec<(u32, String)> = Vec::new();
            for row in file_iter {
                let (fid, rel_path) = row.map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("read file_instance row failed: {}", e)))?;
                file_list.push((fid, rel_path));
            }
            // 找到 max fid 确定 offsets 数组大小
            let max_fid = file_list.iter().map(|(fid, _)| *fid).max().unwrap_or(0);
            file_paths_offsets.resize(max_fid as usize + 2, 0);
            for (fid, rel_path) in &file_list {
                let offset = file_paths_pool.len() as u32;
                file_paths_pool.push_str(rel_path);
                file_paths_offsets[*fid as usize] = offset;
            }
            // 构建末尾哨兵 + 填充空洞（fid 不连续时空洞用前一个 offset）
            let mut last_offset = 0u32;
            for i in 0..file_paths_offsets.len() {
                if file_paths_offsets[i] == 0 && i > 0 {
                    file_paths_offsets[i] = last_offset;
                } else if file_paths_offsets[i] > 0 {
                    last_offset = file_paths_offsets[i];
                }
            }
            // 末尾哨兵：最后一个元素指向 pool 末尾，供 file_rel_path 切片访问
            let sentinel = file_paths_pool.len() as u32;
            let last_idx = file_paths_offsets.len() - 1;
            file_paths_offsets[last_idx] = sentinel;
        }

        // 1b. 加载符号（不再 JOIN file_instances，file_rel_path 从 file_paths 查）
        // P7: name/qualified_name/module_path 改为 string pool，消除 600万 String 堆分配
        let mut by_id = Vec::new();
        // P7: 3 个全局 string pool
        let mut name_pool = String::new();
        let mut qname_pool = String::new();
        let mut module_pool = String::new();

        let sql_symbols = format!(
            "SELECT s.id, s.file_instance_id, s.kind, s.name, s.qualified_name,
                    s.module_path, s.start_line, s.end_line, s.depth
             FROM symbols s
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE fi.status != 'archived' {}",
            ws_filter
        );
        let mut stmt = conn.prepare(&sql_symbols)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("prepare symbols query failed: {}", e)))?;

        let symbol_iter = stmt.query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)? as u32,      // id
                row.get::<_, i64>(1)? as u32,      // file_instance_id
                row.get::<_, String>(2)?,          // kind
                row.get::<_, String>(3)?,          // name
                row.get::<_, String>(4)?,          // qualified_name
                row.get::<_, String>(5)?,          // module_path
                row.get::<_, i64>(6)? as u32,      // start_line
                row.get::<_, i64>(7)? as u32,      // end_line
                row.get::<_, i64>(8)? as i32,      // depth
            ))
        }).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("query symbols failed: {}", e)))?;

        for row in symbol_iter {
            let (id, fid, kind_str, name, qname, module, start_line, end_line, depth) = row
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("read symbol row failed: {}", e)))?;

            // P7: 追加到 string pool，记录 offset + len
            let name_offset = name_pool.len() as u32;
            let name_len = name.len() as u32;
            name_pool.push_str(&name);

            let qname_offset = qname_pool.len() as u32;
            let qname_len = qname.len() as u32;
            qname_pool.push_str(&qname);

            let module_offset = module_pool.len() as u32;
            let module_len = module.len() as u32;
            module_pool.push_str(&module);

            let sym = GraphSymbol {
                id, file_instance_id: fid, kind: SymbolKind::from_db_str(&kind_str),
                name_offset, name_len, qname_offset, qname_len,
                module_offset, module_len, start_line, end_line, depth,
            };

            if id as usize >= by_id.len() {
                by_id.resize(id as usize + 1, GraphSymbol {
                    id: 0, file_instance_id: 0, kind: SymbolKind::Unknown,
                    name_offset: 0, name_len: 0, qname_offset: 0, qname_len: 0,
                    module_offset: 0, module_len: 0, start_line: 0, end_line: 0, depth: -1,
                });
            }
            by_id[id as usize] = sym;
        }

        let symbol_count = by_id.len();

        // 直接排序 symbol_id 并从 pool 比较，加载期不再复制百万份 String。
        let mut by_qname_sorted_ids: Vec<u32> = by_id.iter()
            .filter(|sym| sym.id != 0 || sym.name_len != 0)
            .map(|sym| sym.id)
            .collect();
        by_qname_sorted_ids.sort_unstable_by(|a, b| {
            let sa = &by_id[*a as usize];
            let sb = &by_id[*b as usize];
            let qa = &qname_pool[sa.qname_offset as usize..(sa.qname_offset + sa.qname_len) as usize];
            let qb = &qname_pool[sb.qname_offset as usize..(sb.qname_offset + sb.qname_len) as usize];
            qa.cmp(qb)
        });
        let mut by_simple_name_sorted_ids = by_qname_sorted_ids.clone();
        by_simple_name_sorted_ids.sort_unstable_by(|a, b| {
            let sa = &by_id[*a as usize];
            let sb = &by_id[*b as usize];
            let na = &name_pool[sa.name_offset as usize..(sa.name_offset + sa.name_len) as usize];
            let nb = &name_pool[sb.name_offset as usize..(sb.name_offset + sb.name_len) as usize];
            na.cmp(nb).then_with(|| a.cmp(b))
        });

        // P2: 构建搜索索引 — 所有 name + qname 的小写版本，\0 分隔
        // memchr SIMD 一次扫描整个池，替代 N 次子串搜索
        let mut search_pool_lower = String::new();
        let mut search_entry_offsets: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        let mut search_entry_sym_ids: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        for sym in &by_id {
            if sym.id == 0 && sym.name_len == 0 { continue; }
            // name 条目
            let name = name_pool.get(
                sym.name_offset as usize..(sym.name_offset + sym.name_len) as usize
            ).unwrap_or("");
            if !name.is_empty() {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in name.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');  // \0 分隔符，防止跨条目误匹配
            }
            // qname 条目（可能与 name 相同，但搜索时需要匹配 qualified_name）
            let qname = qname_pool.get(
                sym.qname_offset as usize..(sym.qname_offset + sym.qname_len) as usize
            ).unwrap_or("");
            if !qname.is_empty() && qname != name {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in qname.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');
            }
        }

        let symbols = Arc::new(SymbolTable {
            by_id, by_qname_sorted_ids, by_simple_name_sorted_ids,
            file_paths_pool, file_paths_offsets,
            name_pool, qname_pool, module_pool,
            search_pool_lower, search_entry_offsets, search_entry_sym_ids,
        });

        if !include_calls {
            self.symbols = Some(symbols);
            self.calls = None;
            return Ok((symbol_count, 0));
        }

        let (calls, edge_count) = load_call_graph(&conn, symbols.as_ref(), workspace_id)?;

        self.symbols = Some(symbols);
        self.calls = Some(Arc::new(calls));

        Ok((symbol_count, edge_count))
    }

    fn _load_calls_from_sqlite(&mut self, db_path: &str, workspace_id: i64) -> PyResult<usize> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("symbols not ready"))?;
        let conn = open_immutable_db(db_path)?;
        let (calls, edge_count) = load_call_graph(&conn, symbols.as_ref(), workspace_id)?;
        self.calls = Some(Arc::new(calls));
        Ok(edge_count)
    }

    /// 查询谁调用了这个函数（对齐 Python db_query.get_callers）
    /// 入参：callee_name（简名，对齐 Python 接口）
    /// 入参：qualified_name（可选，P28 新增，大规模下避免短名跨模块误匹配）
    ///
    /// P10 优化：返回 CallersBatch PyClass（懒转换）
    /// 加载阶段只收集 u32 id（零 String clone，零 PyDict），访问时按需转换
    #[pyo3(signature = (callee_name, qualified_name=None))]
    fn get_callers(slf: &Bound<Self>, py: Python<'_>, callee_name: &str, qualified_name: Option<&str>) -> PyResult<CallersBatch> {
        let self_ref = slf.borrow();
        let symbols = self_ref.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self_ref.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

        let mut results: Vec<CallerResult> = Vec::new();

        // P28：传入 qualified_name 时，先解析 callee_id 用于精确过滤
        let qname_filter_id: Option<u32> = match qualified_name {
            Some(qname) => symbols.qname_get(qname),
            None => None,
        };
        // 传了 qname 但查不到对应符号 → 空结果（避免短名误匹配）
        if qualified_name.is_some() && qname_filter_id.is_none() {
            let py_self = slf.clone().unbind();
            return Ok(CallersBatch { results, store: py_self });
        }

        // 从排序名称下标二分查找 callee_name_idx。
        let callee_name_idx = match calls.callee_name_idx(callee_name) {
            Some(idx) => idx,
            None => {
                let py_self = slf.clone().unbind();
                return Ok(CallersBatch { results, store: py_self });
            }
        };

        // CSR 连续区间返回所有 callee_name 匹配的边。
        for &pos in calls.positions_for_callee_name(callee_name_idx) {
                let edge = &calls.forward_edges[pos as usize];
                // P28：传入 qname 时，跳过 callee_id 不匹配的边
                if let Some(filter_id) = qname_filter_id {
                    if edge.callee_id != filter_id {
                        continue;
                    }
                }
                // P10：只收集 id，不构造 PyDict
                if symbols.by_id.get(edge.caller_id as usize).is_some() {
                    results.push(CallerResult {
                        caller_id: edge.caller_id,
                        callee_id: edge.callee_id,
                        callee_name_idx: edge.callee_name_idx,
                        call_line_packed: edge.call_line_packed,
                    });
                }
        }

        let py_self = slf.clone().unbind();
        Ok(CallersBatch { results, store: py_self })
    }

    /// 查询这个函数调用了谁（对齐 Python db_query.get_callees）
    /// 入参：caller_name（简名）
    /// 入参：qualified_name（可选，P28 新增，精确到唯一符号避免跨模块误匹配）
    ///
    /// P28：传入 qualified_name 时，直接 qname→caller_id 走 CSR（O(1) 定位 + O(degree) 遍历），
    ///      跳过简名多候选遍历，避免大规模下多个模块同名函数误匹配
    #[pyo3(signature = (caller_name, qualified_name=None))]
    fn get_callees<'py>(&self, py: Python<'py>, caller_name: &str, qualified_name: Option<&str>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

        let mut results = Vec::new();

        // P28：传入 qualified_name 时，直接定位唯一 caller_id（O(1)）
        let caller_ids: Vec<u32> = match qualified_name {
            Some(qname) => match symbols.qname_get(qname) {
                Some(id) => vec![id],
                None => return Ok(results),  // qname 查不到 → 空结果
            },
            None => symbols.simple_name_ids(caller_name).to_vec(),
        };

        for caller_id in caller_ids {
            // CSR forward 遍历：caller_id 的所有边
            let start = calls.forward_offsets.get(caller_id as usize)
                .copied().unwrap_or(0);
            let end = calls.forward_offsets.get(caller_id as usize + 1)
                .copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.forward_edges[i];
                let callee_name = calls.callee_name(edge.callee_name_idx);
                // 已解析边：从 callee 符号表取完整信息
                let (callee_qname, callee_file, callee_module) = if edge.callee_id != 0 {
                    symbols.by_id.get(edge.callee_id as usize)
                        .map(|s| (symbols.sym_qname(s),
                                  symbols.file_rel_path(s.file_instance_id),
                                  symbols.sym_module(s)))
                        .unwrap_or(("", "", ""))
                } else { ("", "", "") };

                let dict = PyDict::new(py);
                dict.set_item("callee_name", callee_name)?;
                dict.set_item("callee_id", edge.callee_id)?;
                dict.set_item("callee_qualified", callee_qname)?;
                dict.set_item("callee_file", callee_file)?;
                dict.set_item("callee_module", callee_module)?;
                dict.set_item("call_line", edge.call_line())?;
                dict.set_item("is_cross_file", edge.is_cross_file())?;
                results.push(dict.into_any());
            }
        }

        Ok(results)
    }

    /// 通过 qualified_name 获取符号详情
    fn get_symbol<'py>(&self, py: Python<'py>, qualified_name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        if let Some(id) = symbols.qname_get(qualified_name) {
            if let Some(sym) = symbols.by_id.get(id as usize) {
                let dict = PyDict::new(py);
                dict.set_item("id", sym.id)?;
                dict.set_item("name", symbols.sym_name(sym))?;
                dict.set_item("kind", sym.kind.as_str())?;
                dict.set_item("qualified_name", symbols.sym_qname(sym))?;
                dict.set_item("module_path", symbols.sym_module(sym))?;
                dict.set_item("start_line", sym.start_line)?;
                dict.set_item("end_line", sym.end_line)?;
                dict.set_item("depth", sym.depth)?;
                dict.set_item("file_rel_path", symbols.file_rel_path(sym.file_instance_id))?;
                return Ok(Some(dict.into_any()));
            }
        }
        Ok(None)
    }

    /// 搜索符号（子串匹配，PoC 简化版，未上 FTS5）
    /// 对齐 Python db_query.search_symbols
    ///
    /// P2 优化：删除预计算的 name_lower/qname_lower，查询时用零分配的
    /// ASCII 大小写不敏感子串匹配，省 ~400MB / 200万符号
    /// P11 优化：memchr SIMD 加速 + 返回 SymbolSearchBatch（懒转换）
    #[pyo3(signature = (query, kind=None, limit=None))]
    fn search_symbols(slf: &Bound<Self>, py: Python<'_>, query: &str, kind: Option<&str>, limit: Option<usize>) -> PyResult<SymbolSearchBatch> {
        let self_ref = slf.borrow();
        let symbols = self_ref.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;

        let limit = limit.unwrap_or(50);
        let query_lower: Vec<u8> = query.chars().map(|c| c.to_ascii_lowercase() as u8).collect();

        // P2 优化：用预构建的 search_pool_lower + memchr SIMD 一次扫描整个池
        // 替代原来 N 次线性扫描 + contains_ascii_ci 子串匹配
        // search_pool_lower 中所有 name/qname 用 \0 分隔，不会跨条目误匹配
        let mut symbol_ids: Vec<u32> = Vec::new();
        let mut seen: HashSet<u32> = HashSet::new();
        let pool = symbols.search_pool_lower.as_bytes();
        let offsets = &symbols.search_entry_offsets;
        let sym_ids = &symbols.search_entry_sym_ids;

        if query_lower.is_empty() || offsets.is_empty() {
            let py_self = slf.clone().unbind();
            return Ok(SymbolSearchBatch { symbol_ids, store: py_self });
        }

        // memchr::memmem SIMD 加速子串搜索
        let mut search_start = 0;
        loop {
            if symbol_ids.len() >= limit { break; }
            let remaining = &pool[search_start..];
            let rel_pos = match memchr::memmem::find(remaining, &query_lower) {
                None => break,
                Some(p) => p,
            };
            let pos = (search_start + rel_pos) as u32;
            search_start = search_start + rel_pos + 1;
            // 二分查找：找到最大的 entry_offset <= pos
            let entry_idx = match offsets.binary_search(&pos) {
                Ok(idx) => idx,
                Err(idx) if idx > 0 => idx - 1,
                Err(_) => continue,
            };
            let sym_id = sym_ids[entry_idx];

            // 去重
            if !seen.insert(sym_id) { continue; }

            // kind 过滤
            if let Some(k) = kind {
                if let Some(sym) = symbols.by_id.get(sym_id as usize) {
                    if sym.kind != SymbolKind::from_db_str(k) { continue; }
                }
            }

            symbol_ids.push(sym_id);
        }

        let py_self = slf.clone().unbind();
        Ok(SymbolSearchBatch { symbol_ids, store: py_self })
    }

    /// 向下调用链（BFS，对齐 Python db_query.get_call_chain_down）
    /// 入参：qualified_name + max_depth
    ///
    /// P3 优化：
    /// 1. visited: HashSet<u32> → Vec<bool>，O(1) 索引访问，消除哈希计算
    /// 2. queue: VecDeque<(u32, usize)> → Vec<u32> + Vec<usize>（SoA 布局，缓存友好）
    fn get_call_chain_down<'py>(&self, py: Python<'py>, qualified_name: &str, max_depth: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

        let mut results = Vec::new();
        let start_id = match symbols.qname_get(qualified_name) {
            Some(id) => id,
            None => return Ok(results),  // 起点不存在，空结果
        };

        // P3: Vec<bool> 替代 HashSet，O(1) 索引访问，无哈希开销
        let sym_count = symbols.by_id.len();
        let mut visited: Vec<bool> = vec![false; sym_count];
        // P3: SoA 布局 — queue_sym_ids 和 queue_depths 分开存储，缓存友好
        let mut queue_sym_ids: Vec<u32> = Vec::with_capacity(256);
        let mut queue_depths: Vec<usize> = Vec::with_capacity(256);
        let mut queue_head: usize = 0;

        visited[start_id as usize] = true;
        queue_sym_ids.push(start_id);
        queue_depths.push(0);

        while queue_head < queue_sym_ids.len() {
            let sym_id = queue_sym_ids[queue_head];
            let depth = queue_depths[queue_head];
            queue_head += 1;

            if depth >= max_depth { continue; }

            // CSR forward 遍历 callees
            let start = calls.forward_offsets.get(sym_id as usize)
                .copied().unwrap_or(0);
            let end = calls.forward_offsets.get(sym_id as usize + 1)
                .copied().unwrap_or(0);

            let caller_name = symbols.by_id.get(sym_id as usize)
                .map(|s| symbols.sym_name(s))
                .unwrap_or("");

            for i in start..end {
                let edge = &calls.forward_edges[i];
                let callee_name = calls.callee_name(edge.callee_name_idx);

                let dict = PyDict::new(py);
                dict.set_item("depth", depth)?;
                dict.set_item("caller_name", caller_name)?;
                dict.set_item("callee_name", callee_name)?;
                dict.set_item("callee_id", edge.callee_id)?;
                dict.set_item("call_line", edge.call_line())?;
                dict.set_item("is_cross_file", edge.is_cross_file())?;
                results.push(dict.into_any());

                // 继续向下 BFS（仅已解析边）
                if edge.callee_id != 0
                    && (edge.callee_id as usize) < sym_count
                    && !visited[edge.callee_id as usize]
                {
                    visited[edge.callee_id as usize] = true;
                    queue_sym_ids.push(edge.callee_id);
                    queue_depths.push(depth + 1);
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
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

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
            if symbols.by_id[i].id == 0 && symbols.by_id[i].name_len == 0 { continue; }
            if in_degree[i] == 0 {
                queue.push_back(i);
            }
        }

        let mut order = Vec::with_capacity(n);
        while let Some(idx) = queue.pop_front() {
            let sym = &symbols.by_id[idx];
            if sym.id == 0 && sym.name_len == 0 { continue; }
            order.push(symbols.sym_qname(sym).to_string());

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
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

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
                            cycle.push(symbols.sym_qname(sym).to_string());
                        }
                        cur = parent[cur as usize];
                    }
                    if let Some(sym) = symbols.by_id.get(v) {
                        cycle.push(symbols.sym_qname(sym).to_string());
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
            if sym.id == 0 && sym.name_len == 0 { continue; }
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
            dict.set_item("qname_index_size", symbols.by_qname_sorted_ids.len())?;
            dict.set_item("simple_name_index_size", symbols.by_simple_name_sorted_ids.len())?;
            dict.set_item("file_index_size", symbols.file_paths_offsets.len().saturating_sub(1))?;
            // P4: 暴露 pool size 用于内存分析
            dict.set_item("name_pool_size", symbols.name_pool.len())?;
            dict.set_item("qname_pool_size", symbols.qname_pool.len())?;
            dict.set_item("module_pool_size", symbols.module_pool.len())?;
            dict.set_item("search_pool_size", symbols.search_pool_lower.len())?;
            dict.set_item("search_entry_count", symbols.search_entry_offsets.len())?;
        } else {
            dict.set_item("symbol_count", 0)?;
            dict.set_item("qname_index_size", 0)?;
            dict.set_item("simple_name_index_size", 0)?;
            dict.set_item("file_index_size", 0)?;
            dict.set_item("name_pool_size", 0)?;
            dict.set_item("qname_pool_size", 0)?;
            dict.set_item("module_pool_size", 0)?;
            dict.set_item("search_pool_size", 0)?;
            dict.set_item("search_entry_count", 0)?;
        }
        if let Some(calls) = &self.calls {
            let resolved = calls.forward_edges.iter().filter(|e| e.callee_id != 0).count();
            dict.set_item("edge_count", calls.forward_edges.len())?;
            dict.set_item("resolved_edge_count", resolved)?;
            dict.set_item("forward_offsets_size", calls.forward_offsets.len())?;
            dict.set_item("backward_offsets_size", calls.backward_offsets.len())?;
            dict.set_item("callee_name_pool_size", calls.callee_names_pool.len())?;
            dict.set_item("callee_name_count", calls.callee_names_offsets.len().saturating_sub(1))?;
            dict.set_item("callee_name_to_idx_size", calls.callee_name_sorted_idxs.len())?;
            dict.set_item("root_count", calls.roots.len())?;
        } else {
            dict.set_item("edge_count", 0)?;
        }
        Ok(dict.into_any())
    }

    /// 按容器 capacity 返回 GraphStore 已知堆内存，供容量回归测试使用。
    fn memory_breakdown<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        use std::mem::size_of;

        let dict = PyDict::new(py);
        let mut total = 0usize;
        macro_rules! add_bytes {
            ($name:expr, $bytes:expr) => {{
                let bytes = $bytes;
                dict.set_item($name, bytes)?;
                total += bytes;
            }};
        }

        if let Some(symbols) = &self.symbols {
            add_bytes!("symbols_by_id", symbols.by_id.capacity() * size_of::<GraphSymbol>());
            add_bytes!("qname_sorted_ids", symbols.by_qname_sorted_ids.capacity() * size_of::<u32>());
            add_bytes!("simple_name_sorted_ids", symbols.by_simple_name_sorted_ids.capacity() * size_of::<u32>());
            add_bytes!("file_paths_pool", symbols.file_paths_pool.capacity());
            add_bytes!("file_paths_offsets", symbols.file_paths_offsets.capacity() * size_of::<u32>());
            add_bytes!("name_pool", symbols.name_pool.capacity());
            add_bytes!("qname_pool", symbols.qname_pool.capacity());
            add_bytes!("module_pool", symbols.module_pool.capacity());
            add_bytes!("search_pool_lower", symbols.search_pool_lower.capacity());
            add_bytes!("search_entry_offsets", symbols.search_entry_offsets.capacity() * size_of::<u32>());
            add_bytes!("search_entry_sym_ids", symbols.search_entry_sym_ids.capacity() * size_of::<u32>());
        }
        if let Some(calls) = &self.calls {
            add_bytes!("forward_edges", calls.forward_edges.capacity() * size_of::<CallEdge>());
            add_bytes!("forward_offsets", calls.forward_offsets.capacity() * size_of::<usize>());
            add_bytes!("backward_positions", calls.backward_positions.capacity() * size_of::<u32>());
            add_bytes!("backward_offsets", calls.backward_offsets.capacity() * size_of::<usize>());
            add_bytes!("callee_position_offsets", calls.callee_position_offsets.capacity() * size_of::<u32>());
            add_bytes!("callee_positions", calls.callee_positions.capacity() * size_of::<u32>());
            add_bytes!("callee_names_pool", calls.callee_names_pool.capacity());
            add_bytes!("callee_names_offsets", calls.callee_names_offsets.capacity() * size_of::<u32>());
            add_bytes!("callee_name_sorted_idxs", calls.callee_name_sorted_idxs.capacity() * size_of::<u32>());
            add_bytes!("roots", calls.roots.capacity() * size_of::<u32>());
        }
        dict.set_item("known_heap_total", total)?;
        Ok(dict.into_any())
    }

    /// 全量计算所有函数的拓扑深度（Kahn BFS，复用 CSR 索引）
    ///
    /// 算法：从叶子节点（无 callee）开始 BFS 向上传播，
    /// depth = max(所有 callee 的 depth) + 1，环中节点 depth=0。
    ///
    /// 返回 Vec<(symbol_id, depth)>，Python 侧可直接 executemany 批量 UPDATE。
    ///
    /// 内存占用（千万符号）：
    /// - pending_callee_count: Vec<usize> ~80MB
    /// - depth: Vec<i32> ~40MB
    /// - queue: VecDeque<u32> ~40MB
    /// 总计 ~160MB（Rust），远小于 Python defaultdict 的 2GB+
    fn compute_depth_all(&self) -> PyResult<Vec<(u32, i32)>> {
        use std::collections::VecDeque;

        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(
                "symbols not loaded, call load_from_sqlite first"
            ))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(
                "calls not loaded, call load_from_sqlite first"
            ))?;

        let max_id = symbols.by_id.len();

        // 1. 构建每个 fn 的未处理 callee 计数（CSR forward edges）
        let mut pending_callee_count = vec![0usize; max_id];
        for edge in &calls.forward_edges {
            if edge.callee_id > 0 && (edge.caller_id as usize) < max_id {
                pending_callee_count[edge.caller_id as usize] += 1;
            }
        }

        // 2. depth 数组：-1 = 未计算
        let mut depth: Vec<i32> = vec![-1; max_id];

        // 3. 从叶子节点（无 callee 且 kind 是 fn/test_fn）开始 BFS
        let mut queue: VecDeque<u32> = VecDeque::new();
        for sym in &symbols.by_id {
            if sym.id == 0 {
                continue;
            }
            // 只计算 fn 和 test_fn（与 Python 侧一致）
            if sym.kind != SymbolKind::Fn && sym.kind != SymbolKind::TestFn {
                continue;
            }
            let id = sym.id as usize;
            if pending_callee_count[id] == 0 {
                depth[id] = 0;
                queue.push_back(sym.id);
            }
        }

        // 4. BFS 向上传播
        while let Some(fn_id) = queue.pop_front() {
            let fn_depth = depth[fn_id as usize];

            // 遍历所有 caller（通过 CSR backward edges）
            let b_start = calls.backward_offsets.get(fn_id as usize)
                .copied().unwrap_or(0);
            let b_end = calls.backward_offsets.get(fn_id as usize + 1)
                .copied().unwrap_or(0);

            for i in b_start..b_end {
                let caller_id = calls.forward_edges[calls.backward_positions[i] as usize].caller_id;
                if caller_id == 0 || (caller_id as usize) >= max_id {
                    continue;
                }
                if depth[caller_id as usize] != -1 {
                    continue;
                }
                pending_callee_count[caller_id as usize] -= 1;
                if pending_callee_count[caller_id as usize] == 0 {
                    // 所有 callee 都已处理，计算 depth = max(callee depths) + 1
                    let f_start = calls.forward_offsets.get(caller_id as usize)
                        .copied().unwrap_or(0);
                    let f_end = calls.forward_offsets.get(caller_id as usize + 1)
                        .copied().unwrap_or(0);
                    let max_callee_depth = calls.forward_edges[f_start..f_end]
                        .iter()
                        .filter(|e| e.callee_id > 0 && (e.callee_id as usize) < max_id)
                        .map(|e| depth[e.callee_id as usize])
                        .filter(|d| *d >= 0)
                        .max()
                        .unwrap_or(0);
                    depth[caller_id as usize] = max_callee_depth + 1;
                    queue.push_back(caller_id);
                }
            }
        }

        // 5. 收集结果（环中节点 depth=0）
        let mut result = Vec::with_capacity(max_id);
        for sym in &symbols.by_id {
            if sym.id == 0 {
                continue;
            }
            if sym.kind != SymbolKind::Fn && sym.kind != SymbolKind::TestFn {
                continue;
            }
            let d = if depth[sym.id as usize] == -1 { 0 } else { depth[sym.id as usize] };
            result.push((sym.id, d));
        }

        Ok(result)
    }

    /// P5: dump GraphStore 内存数据到快照文件
    ///
    /// 序列化 Vec 和 String pool 到单文件，load_from_file 时 mmap 零拷贝。
    /// HashMap 部分不 dump（load 时从 Vec 重建，约 1-2s）。
    ///
    /// 用法：
    ///   store.dump_to_file("/path/to/snapshot.cwsnap")
    pub fn dump_to_file(&self, py: Python<'_>, path: &str) -> PyResult<()> {
        py.detach(|| self._dump_to_file(path))
    }

    /// 内部 snapshot 写入实现，公开入口在执行期间释放 GIL。
    fn _dump_to_file(&self, path: &str) -> PyResult<()> {
        use std::io::Write;
        let symbols = self.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = self.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

        // 计算 symbol_count（排除空槽位）
        let symbol_count = symbols.by_id.iter()
            .filter(|s| !(s.id == 0 && s.name_len == 0))
            .count() as u32;
        let edge_count = calls.forward_edges.len() as u32;
        // callee_name_count：callee_names_offsets.len() - 1（最后一个为末尾哨兵）
        let callee_name_count = calls.callee_names_offsets.len().saturating_sub(1) as u32;
        // P4: file_paths 已是 pool + offsets 结构，直接复用
        // file_path_count：offsets 数组容量（含空洞填充 + 末尾哨兵）
        let file_path_count = symbols.file_paths_offsets.len().saturating_sub(1) as u32;

        // 计算 sections 布局
        let by_id_bytes = bytemuck::cast_slice::<GraphSymbol, u8>(&symbols.by_id);
        let name_pool_bytes = symbols.name_pool.as_bytes();
        let qname_pool_bytes = symbols.qname_pool.as_bytes();
        let module_pool_bytes = symbols.module_pool.as_bytes();
        let forward_edges_bytes = bytemuck::cast_slice::<CallEdge, u8>(&calls.forward_edges);
        let backward_positions_bytes = bytemuck::cast_slice::<u32, u8>(&calls.backward_positions);
        let forward_offsets_bytes = bytemuck::cast_slice::<usize, u8>(&calls.forward_offsets);
        let backward_offsets_bytes = bytemuck::cast_slice::<usize, u8>(&calls.backward_offsets);
        let callee_names_pool_bytes = calls.callee_names_pool.as_bytes();
        let callee_names_offsets_bytes = bytemuck::cast_slice::<u32, u8>(&calls.callee_names_offsets);
        // P4: 直接引用 symbols.file_paths_pool / offsets，无需再扁平化
        let file_paths_pool_bytes = symbols.file_paths_pool.as_bytes();
        let file_paths_offsets_bytes = bytemuck::cast_slice::<u32, u8>(&symbols.file_paths_offsets);

        // P5 修复：section 起始偏移必须对齐到元素类型的 align_of，否则 load 时
        // pod_align_to 会把 prefix 字节丢弃，导致丢失元素（u32 usize 等 Pod 类型）
        // 例如 callee_names_pool 长度若非 4 的倍数，下一个 section（u32 offsets）
        // 起始偏移便非 4 对齐，pod_align_to 的 prefix 会吃掉首个 u32 的前几字节。
        // 修复：dump 时对齐每个 section 起始偏移，并在 buffer 中加入 padding 字节。
        let section_bytes_arr: [&[u8]; SECTION_COUNT] = [
            by_id_bytes, name_pool_bytes, qname_pool_bytes, module_pool_bytes,
            forward_edges_bytes, backward_positions_bytes,
            forward_offsets_bytes, backward_offsets_bytes,
            callee_names_pool_bytes, callee_names_offsets_bytes,
            file_paths_pool_bytes, file_paths_offsets_bytes,
        ];
        // 每个 section 元素类型的对齐要求（bytes sections = 1）
        let section_aligns: [u64; SECTION_COUNT] = [
            std::mem::align_of::<GraphSymbol>() as u64,  // SEC_BY_ID
            1, 1, 1,                                      // 3 个 string pool
            std::mem::align_of::<CallEdge>() as u64,      // SEC_FORWARD_EDGES
            std::mem::align_of::<u32>() as u64,          // SEC_BACKWARD_POSITIONS
            std::mem::align_of::<usize>() as u64,        // SEC_FWD_OFFSETS
            std::mem::align_of::<usize>() as u64,        // SEC_BWD_OFFSETS
            1,                                             // SEC_CALLEE_NAMES_POOL
            std::mem::align_of::<u32>() as u64,          // SEC_CALLEE_NAMES_OFFSETS
            1,                                             // SEC_FILE_PATHS_POOL
            std::mem::align_of::<u32>() as u64,          // SEC_FILE_PATHS_OFFSETS
        ];

        let mut cur_offset = HEADER_SIZE as u64;
        let mut sections = [SectionEntry { offset: 0, len: 0 }; SECTION_COUNT];
        for i in 0..SECTION_COUNT {
            // 对齐 cur_offset 到 section_aligns[i]
            let align = section_aligns[i];
            let padding = (align - (cur_offset % align)) % align;
            cur_offset += padding;
            sections[i] = SectionEntry {
                offset: cur_offset,
                len: section_bytes_arr[i].len() as u64,
            };
            cur_offset += section_bytes_arr[i].len() as u64;
        }

        // 构造 header
        let header = SnapshotHeader {
            magic: SNAPSHOT_MAGIC,
            version: SNAPSHOT_VERSION,
            symbol_count,
            edge_count,
            callee_name_count,
            file_path_count,
            reserved: [0u8; 32],
            sections,
        };

        // 写文件（一次性 buffer 全部内容，包含 section 间 padding）
        let total_size = cur_offset as usize;
        let mut buffer = Vec::with_capacity(total_size);
        buffer.extend_from_slice(bytemuck::bytes_of(&header));
        for i in 0..SECTION_COUNT {
            // 加入 padding 字节（对齐到 section_aligns[i]）
            let align = section_aligns[i];
            let cur_buf_len = buffer.len() as u64;
            let padding = ((align - (cur_buf_len % align)) % align) as usize;
            if padding > 0 {
                buffer.extend(std::iter::repeat(0u8).take(padding));
            }
            buffer.extend_from_slice(section_bytes_arr[i]);
        }

        let mut file = std::fs::File::create(path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("create snapshot file failed: {}", e)))?;
        file.write_all(&buffer)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("write snapshot failed: {}", e)))?;
        // fsync 确保落盘
        file.sync_all()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("fsync failed: {}", e)))?;

        Ok(())
    }

    /// P5: 从快照文件 mmap 加载（紧凑 Vec + String pool）
    ///
    /// 名称排序数组和 callee 位置 CSR 在 load 后重建。
    ///
    /// 用法：
    ///   store.load_from_file("/path/to/snapshot.cwsnap")
    pub fn load_from_file(
        &mut self,
        py: Python<'_>,
        path: &str,
    ) -> PyResult<(usize, usize)> {
        py.detach(|| self._load_from_file(path))
    }

    /// 内部 snapshot 加载实现，公开入口在执行期间释放 GIL。
    fn _load_from_file(&mut self, path: &str) -> PyResult<(usize, usize)> {
        use std::fs::OpenOptions;

        let file = OpenOptions::new()
            .read(true)
            .open(path)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("open snapshot failed: {}", e)))?;

        // mmap 文件（只读）
        let mmap = unsafe {
            memmap2::Mmap::map(&file)
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("mmap failed: {}", e)))?
        };
        let bytes = &mmap[..];

        // 解析 header
        if bytes.len() < HEADER_SIZE {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("snapshot file too small"));
        }
        let header: &SnapshotHeader = bytemuck::from_bytes(&bytes[..HEADER_SIZE]);
        if header.magic != SNAPSHOT_MAGIC {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("invalid snapshot magic: 0x{:X} (expected 0x{:X})", header.magic, SNAPSHOT_MAGIC)));
        }
        if header.version != SNAPSHOT_VERSION {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                format!("unsupported snapshot version: {} (expected {})", header.version, SNAPSHOT_VERSION)));
        }

        // 从 mmap 切片重建 Vec（这里必须 copy，因为 mmap 生命周期 < store）
        // P5 注：用 pod_align_to 而非 cast_slice，因为 mmap 返回的 &[u8] 对齐为 1，
        // 但目标类型（如 usize）可能需要 8 字节对齐。pod_align_to 自动处理对齐。
        macro_rules! read_section {
            ($idx:expr, $ty:ty) => {{
                let sec = &header.sections[$idx];
                let start = sec.offset as usize;
                let end = start + sec.len as usize;
                if end > bytes.len() {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        format!("section {} out of bounds", $idx)));
                }
                // pod_align_to 返回 (prefix, aligned_middle, suffix)
                // aligned_middle 是对齐后的目标类型切片
                let (_, middle, _) = bytemuck::pod_align_to::<u8, $ty>(&bytes[start..end]);
                middle.to_vec()
            }};
        }
        macro_rules! read_string {
            ($idx:expr) => {{
                let sec = &header.sections[$idx];
                let start = sec.offset as usize;
                let end = start + sec.len as usize;
                if end > bytes.len() {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        format!("section {} out of bounds", $idx)));
                }
                // 从 bytes 直接构造 String（一次 copy）
                String::from_utf8_lossy(&bytes[start..end]).into_owned()
            }};
        }

        let by_id: Vec<GraphSymbol> = read_section!(SEC_BY_ID, GraphSymbol);
        let name_pool: String = read_string!(SEC_NAME_POOL);
        let qname_pool: String = read_string!(SEC_QNAME_POOL);
        let module_pool: String = read_string!(SEC_MODULE_POOL);
        let forward_edges: Vec<CallEdge> = read_section!(SEC_FORWARD_EDGES, CallEdge);
        let backward_positions: Vec<u32> = read_section!(SEC_BACKWARD_POSITIONS, u32);
        let forward_offsets: Vec<usize> = read_section!(SEC_FWD_OFFSETS, usize);
        let backward_offsets: Vec<usize> = read_section!(SEC_BWD_OFFSETS, usize);
        if backward_positions.len() != forward_edges.len()
            || backward_positions.iter().any(|&position| position as usize >= forward_edges.len())
        {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "invalid backward positions section",
            ));
        }
        // P5-v2：新增 4 个 section
        let callee_names_pool: String = read_string!(SEC_CALLEE_NAMES_POOL);
        let callee_names_offsets: Vec<u32> = read_section!(SEC_CALLEE_NAMES_OFFSETS, u32);
        let file_paths_pool: String = read_string!(SEC_FILE_PATHS_POOL);
        let file_paths_offsets: Vec<u32> = read_section!(SEC_FILE_PATHS_OFFSETS, u32);

        let symbol_count = header.symbol_count as usize;
        let edge_count = header.edge_count as usize;

        // 从字符串池直接重建排序 ID 索引，不复制 String。
        let mut by_qname_sorted_ids: Vec<u32> = by_id.iter()
            .filter(|sym| sym.id != 0 || sym.name_len != 0)
            .map(|sym| sym.id)
            .collect();
        by_qname_sorted_ids.sort_unstable_by(|a, b| {
            let sa = &by_id[*a as usize];
            let sb = &by_id[*b as usize];
            let qa = &qname_pool[sa.qname_offset as usize..(sa.qname_offset + sa.qname_len) as usize];
            let qb = &qname_pool[sb.qname_offset as usize..(sb.qname_offset + sb.qname_len) as usize];
            qa.cmp(qb)
        });
        let mut by_simple_name_sorted_ids = by_qname_sorted_ids.clone();
        by_simple_name_sorted_ids.sort_unstable_by(|a, b| {
            let sa = &by_id[*a as usize];
            let sb = &by_id[*b as usize];
            let na = &name_pool[sa.name_offset as usize..(sa.name_offset + sa.name_len) as usize];
            let nb = &name_pool[sb.name_offset as usize..(sb.name_offset + sb.name_len) as usize];
            na.cmp(nb).then_with(|| a.cmp(b))
        });

        let (callee_name_sorted_idxs, callee_position_offsets, callee_positions) =
            build_callee_name_index(&forward_edges, &callee_names_pool, &callee_names_offsets);

        // 5. roots（从 backward positions 重建）
        let mut has_caller = vec![false; by_id.len()];
        for &position in &backward_positions {
            let e = &forward_edges[position as usize];
            if e.callee_id != 0 && (e.callee_id as usize) < has_caller.len() {
                has_caller[e.callee_id as usize] = true;
            }
        }
        let mut roots: Vec<u32> = Vec::new();
        for (idx, sym) in by_id.iter().enumerate() {
            if sym.id == 0 && sym.name_len == 0 { continue; }
            if !has_caller[idx] && sym.kind == SymbolKind::Fn {
                roots.push(idx as u32);
            }
        }

        // 6. P4: file_paths 已是 pool + offsets 结构，直接赋值，无需重建 Vec<String>
        //    （offsets 末尾的哨兵由 dump 时写入，保证 file_rel_path(i) 切片访问安全）

        // P2: 从已加载的 name_pool/qname_pool 构建搜索索引（与 load_from_sqlite 相同逻辑）
        let mut search_pool_lower = String::new();
        let mut search_entry_offsets: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        let mut search_entry_sym_ids: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        for sym in &by_id {
            if sym.id == 0 && sym.name_len == 0 { continue; }
            let name = name_pool.get(
                sym.name_offset as usize..(sym.name_offset + sym.name_len) as usize
            ).unwrap_or("");
            if !name.is_empty() {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in name.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');
            }
            let qname = qname_pool.get(
                sym.qname_offset as usize..(sym.qname_offset + sym.qname_len) as usize
            ).unwrap_or("");
            if !qname.is_empty() && qname != name {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in qname.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');
            }
        }

        self.symbols = Some(Arc::new(SymbolTable {
            by_id,
            by_qname_sorted_ids,
            by_simple_name_sorted_ids,
            file_paths_pool,
            file_paths_offsets,
            name_pool,
            qname_pool,
            module_pool,
            search_pool_lower,
            search_entry_offsets,
            search_entry_sym_ids,
        }));

        self.calls = Some(Arc::new(CallGraph {
            forward_edges,
            forward_offsets,
            backward_positions,
            backward_offsets,
            callee_position_offsets,
            callee_positions,
            callee_names_pool,
            callee_names_offsets,
            callee_name_sorted_idxs,
            roots,
        }));

        Ok((symbol_count, edge_count))
    }
}

// ============================================
// 内部 Rust 方法（非 PyO3，供 diff 模块直接调用）
// ============================================

impl GraphStore {
    /// 创建共享符号层和调用图的查询视图，不复制大表。
    pub(crate) fn fork_shared(&self) -> Self {
        Self {
            symbols: self.symbols.as_ref().map(Arc::clone),
            calls: self.calls.as_ref().map(Arc::clone),
        }
    }

    /// F11 方案 A：从已构建的 SymbolTable + CallGraph 创建 GraphStore
    /// 供 lib.rs::build_graph_from_c_files 调用，跳过 SQLite 加载阶段
    pub fn new_with_data(symbols: Arc<SymbolTable>, calls: Arc<CallGraph>) -> Self {
        GraphStore {
            symbols: Some(symbols),
            calls: Some(calls),
        }
    }

    /// 返回已加载的文件数量（来自 file_paths_offsets，最后一个为 sentinel）
    /// 对应 stats_rust 中的 file_index_size 字段
    pub fn file_count(&self) -> usize {
        self.symbols
            .as_ref()
            .map(|s| s.file_paths_offsets.len().saturating_sub(1))
            .unwrap_or(0)
    }

    /// Rust 内部调用的阻塞加载入口，不需要 Python token。
    ///
    /// P0-2 整改：`workspace_id` 必传，过滤用户级单库多 workspace 数据。
    pub(crate) fn load_from_sqlite_blocking(
        &mut self,
        db_path: &str,
        workspace_id: i64,
    ) -> PyResult<(usize, usize)> {
        self._load_from_sqlite_stage(db_path, workspace_id, true)
    }

    /// 通过 qualified_name 获取符号引用（内部 Rust 接口，零 Python 开销）
    pub fn get_symbol_ref(&self, qualified_name: &str) -> Option<&GraphSymbol> {
        let symbols = self.symbols.as_ref()?;
        let id = symbols.qname_get(qualified_name)?;
        symbols.by_id.get(id as usize)
    }

    /// 获取指定符号的所有已解析 caller symbol_id 集合
    /// （谁调用了这个函数，仅已解析边）
    pub fn get_caller_ids(&self, callee_id: u32) -> Vec<u32> {
        let calls = match self.calls.as_ref() {
            Some(c) => c,
            None => return vec![],
        };
        // CSR backward 遍历：callee_id 的所有入边
        let start = calls.backward_offsets.get(callee_id as usize)
            .copied().unwrap_or(0);
        let end = calls.backward_offsets.get(callee_id as usize + 1)
            .copied().unwrap_or(0);
        let mut callers = Vec::new();
        for i in start..end {
            let edge = &calls.forward_edges[calls.backward_positions[i] as usize];
            if edge.caller_id != 0 {
                callers.push(edge.caller_id);
            }
        }
        callers
    }

    /// 获取指定符号的所有已解析 callee symbol_id 集合
    /// （这个函数调用了谁，仅已解析边）
    pub fn get_callee_ids(&self, caller_id: u32) -> Vec<u32> {
        let calls = match self.calls.as_ref() {
            Some(c) => c,
            None => return vec![],
        };
        // CSR forward 遍历：caller_id 的所有出边
        let start = calls.forward_offsets.get(caller_id as usize)
            .copied().unwrap_or(0);
        let end = calls.forward_offsets.get(caller_id as usize + 1)
            .copied().unwrap_or(0);
        let mut callees = Vec::new();
        for i in start..end {
            let edge = &calls.forward_edges[i];
            if edge.callee_id != 0 {
                callees.push(edge.callee_id);
            }
        }
        callees
    }

    // ============================================
    // G7-T4: 高级图遍历（native Rust 版，供 daemon 直接调用，避免 PyO3 GIL）
    // 对齐 #[pymethods] 中的 get_call_chain_down / get_topological_order / detect_cycles
    // ============================================

    /// 向下调用链 BFS（native 版本，返回扁平结构供 daemon 序列化为 JSON）
    ///
    /// 对齐 PyO3 `get_call_chain_down`：从 `qualified_name` 起点出发，BFS 遍历
    /// 最大 `max_depth` 层，返回每条边的详情（depth / caller_name / callee_name /
    /// callee_id / call_line / is_cross_file）。
    ///
    /// 性能优化同 PyO3 版本：
    /// - visited: Vec<bool> 替代 HashSet（O(1) 索引，无哈希开销）
    /// - SoA 布局：queue_sym_ids + queue_depths 分开存储，缓存友好
    pub fn call_chain_down_rust(
        &self,
        qualified_name: &str,
        max_depth: usize,
    ) -> Vec<CallChainEdgeInfo> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return Vec::new(),
        };
        let calls = match self.calls.as_ref() {
            Some(c) => c,
            None => return Vec::new(),
        };

        let mut results: Vec<CallChainEdgeInfo> = Vec::new();
        let start_id = match symbols.qname_get(qualified_name) {
            Some(id) => id,
            None => return results,
        };

        let sym_count = symbols.by_id.len();
        let mut visited: Vec<bool> = vec![false; sym_count];
        let mut queue_sym_ids: Vec<u32> = Vec::with_capacity(256);
        let mut queue_depths: Vec<usize> = Vec::with_capacity(256);
        let mut queue_head: usize = 0;

        visited[start_id as usize] = true;
        queue_sym_ids.push(start_id);
        queue_depths.push(0);

        while queue_head < queue_sym_ids.len() {
            let sym_id = queue_sym_ids[queue_head];
            let depth = queue_depths[queue_head];
            queue_head += 1;

            if depth >= max_depth {
                continue;
            }

            let start = calls
                .forward_offsets
                .get(sym_id as usize)
                .copied()
                .unwrap_or(0);
            let end = calls
                .forward_offsets
                .get(sym_id as usize + 1)
                .copied()
                .unwrap_or(0);

            let caller_name = symbols
                .by_id
                .get(sym_id as usize)
                .map(|s| symbols.sym_name(s))
                .unwrap_or("");

            for i in start..end {
                let edge = &calls.forward_edges[i];
                let callee_name = calls.callee_name(edge.callee_name_idx);

                results.push(CallChainEdgeInfo {
                    depth,
                    caller_name: caller_name.to_string(),
                    callee_name: callee_name.to_string(),
                    callee_id: edge.callee_id,
                    call_line: edge.call_line(),
                    is_cross_file: edge.is_cross_file(),
                });

                if edge.callee_id != 0
                    && (edge.callee_id as usize) < sym_count
                    && !visited[edge.callee_id as usize]
                {
                    visited[edge.callee_id as usize] = true;
                    queue_sym_ids.push(edge.callee_id);
                    queue_depths.push(depth + 1);
                }
            }
        }

        results
    }

    /// 拓扑排序（native 版本，Kahn 算法）
    ///
    /// 对齐 PyO3 `get_topological_order`：按调用深度升序返回 qualified_name 列表
    /// （被调用者在前，根函数在后）。仅统计已解析边。
    pub fn topological_order_rust(&self) -> Vec<String> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return Vec::new(),
        };
        let calls = match self.calls.as_ref() {
            Some(c) => c,
            None => return Vec::new(),
        };

        let n = symbols.by_id.len();
        let mut in_degree = vec![0u32; n];

        for edge in &calls.forward_edges {
            if edge.callee_id != 0 && (edge.callee_id as usize) < n {
                in_degree[edge.callee_id as usize] += 1;
            }
        }

        let mut queue: VecDeque<usize> = VecDeque::new();
        for i in 0..n {
            if symbols.by_id[i].id == 0 && symbols.by_id[i].name_len == 0 {
                continue;
            }
            if in_degree[i] == 0 {
                queue.push_back(i);
            }
        }

        let mut order = Vec::with_capacity(n);
        while let Some(idx) = queue.pop_front() {
            let sym = &symbols.by_id[idx];
            if sym.id == 0 && sym.name_len == 0 {
                continue;
            }
            order.push(symbols.sym_qname(sym).to_string());

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

        order
    }

    /// 检测调用图中的环（native 版本，DFS 三色标记）
    ///
    /// 对齐 PyO3 `detect_cycles`：返回每个环上的 qualified_name 列表。
    /// 无环时返回空 Vec。
    pub fn detect_cycles_rust(&self) -> Vec<Vec<String>> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return Vec::new(),
        };
        let calls = match self.calls.as_ref() {
            Some(c) => c,
            None => return Vec::new(),
        };

        let n = symbols.by_id.len();
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
            color[u] = 1;
            let start = calls.forward_offsets.get(u).copied().unwrap_or(0);
            let end = calls.forward_offsets.get(u + 1).copied().unwrap_or(0);

            for i in start..end {
                let edge = &calls.forward_edges[i];
                if edge.callee_id == 0 {
                    continue;
                }
                let v = edge.callee_id as usize;
                if v >= symbols.by_id.len() {
                    continue;
                }

                if color[v] == 0 {
                    parent[v] = u as i64;
                    dfs(v, symbols, calls, color, parent, cycles);
                } else if color[v] == 1 {
                    let mut cycle = Vec::new();
                    let mut cur = u as i64;
                    while cur != -1 && cur != v as i64 {
                        if let Some(sym) = symbols.by_id.get(cur as usize) {
                            cycle.push(symbols.sym_qname(sym).to_string());
                        }
                        cur = parent[cur as usize];
                    }
                    if let Some(sym) = symbols.by_id.get(v) {
                        cycle.push(symbols.sym_qname(sym).to_string());
                    }
                    cycle.reverse();
                    if cycle.len() > 1 {
                        cycles.push(cycle);
                    }
                }
            }
            color[u] = 2;
        }

        for i in 0..n {
            let sym = &symbols.by_id[i];
            if sym.id == 0 && sym.name_len == 0 {
                continue;
            }
            if color[i] == 0 {
                dfs(i, symbols, calls, &mut color, &mut parent, &mut cycles);
            }
        }

        cycles
    }

    /// 通过 symbol_id 获取符号引用
    pub fn get_symbol_by_id(&self, id: u32) -> Option<&GraphSymbol> {
        let symbols = self.symbols.as_ref()?;
        symbols.by_id.get(id as usize)
    }

    /// 通过 file_instance_id 获取 rel_path（P3 优化：供 diff 模块使用）
    pub fn get_file_rel_path(&self, file_instance_id: u32) -> &str {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return "",
        };
        symbols.file_rel_path(file_instance_id)
    }

    /// P7: 获取符号 name（从 string pool 读取，供外部模块使用）
    pub fn symbol_name(&self, sym: &GraphSymbol) -> &str {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return "",
        };
        symbols.sym_name(sym)
    }

    /// P7: 获取符号 qualified_name（从 string pool 读取，供外部模块使用）
    pub fn symbol_qname(&self, sym: &GraphSymbol) -> &str {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return "",
        };
        symbols.sym_qname(sym)
    }

    /// P7: 获取符号 module_path（从 string pool 读取，供外部模块使用）
    pub fn symbol_module(&self, sym: &GraphSymbol) -> &str {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return "",
        };
        symbols.sym_module(sym)
    }

    /// 获取指定 file_rel_path 的所有符号（供 delta 模块按文件对比）
    pub fn get_symbols_by_file(&self, file_rel_path: &str) -> Vec<&GraphSymbol> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return vec![],
        };
        symbols
            .by_id
            .iter()
            .filter(|s| symbols.file_rel_path(s.file_instance_id) == file_rel_path)
            .collect()
    }

    /// 获取所有符号的简单名称 → qualified_name 映射（供 resolve delta 使用）
    pub fn get_name_to_qnames(&self) -> FxHashMap<String, Vec<String>> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return FxHashMap::default(),
        };
        let mut map: FxHashMap<String, Vec<String>> = FxHashMap::default();
        for sym in &symbols.by_id {
            if sym.id == 0 && sym.name_len == 0 {
                continue;
            }
            let name = symbols.sym_name(sym).to_string();
            let qname = symbols.sym_qname(sym).to_string();
            map.entry(name).or_default().push(qname);
        }
        map
    }

    /// 获取所有符号的 qualified_name 列表，可选按 file_rel_path 或 module_path 过滤
    ///
    /// 供 diff 模块的 compare_snapshots / count_symbols_in_scope 使用，
    /// 避免 diff 模块直接访问 GraphStore 内部字段。
    pub fn get_all_qualified_names(
        &self,
        file_filter: Option<&str>,
        module_filter: Option<&str>,
    ) -> Vec<String> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return vec![],
        };
        symbols
            .by_id
            .iter()
            .filter(|sym| {
                if sym.id == 0 && sym.name_len == 0 {
                    return false;
                }
                if let Some(f) = file_filter {
                    if symbols.file_rel_path(sym.file_instance_id) != f {
                        return false;
                    }
                }
                if let Some(m) = module_filter {
                    if symbols.sym_module(sym) != m {
                        return false;
                    }
                }
                true
            })
            .map(|sym| symbols.sym_qname(sym).to_string())
            .collect()
    }

    // ============================================
    // R6: pub(crate) getter 供 daemon snapshot_state 模块使用
    // ============================================

    /// 获取 SymbolTable 引用（供 daemon 查询 handler 直接访问，避免 PyO3 GIL）
    #[inline]
    pub(crate) fn symbols_table(&self) -> Option<&SymbolTable> {
        self.symbols.as_deref()
    }

    /// 获取 CallGraph 引用（供 daemon 查询 handler 直接访问，避免 PyO3 GIL）
    #[inline]
    pub(crate) fn call_graph(&self) -> Option<&CallGraph> {
        self.calls.as_deref()
    }

    /// Rust 原生 stats（不依赖 Python GIL），返回与 PyO3 stats() 相同字段集
    ///
    /// 供 daemon `query.stats` handler 使用。返回 serde_json::Value::Object。
    pub(crate) fn stats_rust(&self) -> serde_json::Value {
        use serde_json::{Map, Value};
        let mut m = Map::new();
        if let Some(symbols) = &self.symbols {
            m.insert("symbol_count".into(), Value::Number(symbols.by_id.len().into()));
            m.insert(
                "qname_index_size".into(),
                Value::Number(symbols.by_qname_sorted_ids.len().into()),
            );
            m.insert(
                "simple_name_index_size".into(),
                Value::Number(symbols.by_simple_name_sorted_ids.len().into()),
            );
            m.insert(
                "file_index_size".into(),
                Value::Number(symbols.file_paths_offsets.len().saturating_sub(1).into()),
            );
            m.insert("name_pool_size".into(), Value::Number(symbols.name_pool.len().into()));
            m.insert("qname_pool_size".into(), Value::Number(symbols.qname_pool.len().into()));
            m.insert(
                "module_pool_size".into(),
                Value::Number(symbols.module_pool.len().into()),
            );
            m.insert(
                "search_pool_size".into(),
                Value::Number(symbols.search_pool_lower.len().into()),
            );
            m.insert(
                "search_entry_count".into(),
                Value::Number(symbols.search_entry_offsets.len().into()),
            );
        } else {
            for k in [
                "symbol_count",
                "qname_index_size",
                "simple_name_index_size",
                "file_index_size",
                "name_pool_size",
                "qname_pool_size",
                "module_pool_size",
                "search_pool_size",
                "search_entry_count",
            ] {
                m.insert(k.into(), Value::Number(0u32.into()));
            }
        }
        if let Some(calls) = &self.calls {
            let resolved = calls.forward_edges.iter().filter(|e| e.callee_id != 0).count();
            m.insert("edge_count".into(), Value::Number(calls.forward_edges.len().into()));
            m.insert("resolved_edge_count".into(), Value::Number(resolved.into()));
            m.insert(
                "forward_offsets_size".into(),
                Value::Number(calls.forward_offsets.len().into()),
            );
            m.insert(
                "backward_offsets_size".into(),
                Value::Number(calls.backward_offsets.len().into()),
            );
            m.insert(
                "callee_name_pool_size".into(),
                Value::Number(calls.callee_names_pool.len().into()),
            );
            m.insert(
                "callee_name_count".into(),
                Value::Number(calls.callee_names_offsets.len().saturating_sub(1).into()),
            );
            m.insert(
                "callee_name_to_idx_size".into(),
                Value::Number(calls.callee_name_sorted_idxs.len().into()),
            );
            m.insert("root_count".into(), Value::Number(calls.roots.len().into()));
        } else {
            m.insert("edge_count".into(), Value::Number(0u32.into()));
        }
        Value::Object(m)
    }

    /// Rust 原生 search_symbols（不依赖 Python GIL），返回 symbol_id 列表
    ///
    /// 算法与 PyO3 `search_symbols` 一致：memchr SIMD 扫描 search_pool_lower
    /// + 二分查找 entry_offsets + HashSet 去重 + 可选 kind 过滤。
    pub(crate) fn search_symbols_rust(
        &self,
        query: &str,
        kind: Option<&str>,
        limit: usize,
    ) -> Vec<u32> {
        let symbols = match self.symbols.as_ref() {
            Some(s) => s,
            None => return vec![],
        };
        let limit = if limit == 0 { 50 } else { limit };
        let query_lower: Vec<u8> = query.chars().map(|c| c.to_ascii_lowercase() as u8).collect();
        if query_lower.is_empty() || symbols.search_entry_offsets.is_empty() {
            return vec![];
        }
        let mut symbol_ids: Vec<u32> = Vec::with_capacity(limit.min(64));
        let mut seen: HashSet<u32> = HashSet::new();
        let pool = symbols.search_pool_lower.as_bytes();
        let offsets = &symbols.search_entry_offsets;
        let sym_ids = &symbols.search_entry_sym_ids;

        let mut search_start = 0usize;
        loop {
            if symbol_ids.len() >= limit {
                break;
            }
            let remaining = &pool[search_start..];
            let rel_pos = match memchr::memmem::find(remaining, &query_lower) {
                None => break,
                Some(p) => p,
            };
            let pos = (search_start + rel_pos) as u32;
            search_start = search_start + rel_pos + 1;
            let entry_idx = match offsets.binary_search(&pos) {
                Ok(idx) => idx,
                Err(idx) if idx > 0 => idx - 1,
                Err(_) => continue,
            };
            let sym_id = sym_ids[entry_idx];
            if !seen.insert(sym_id) {
                continue;
            }
            if let Some(k) = kind {
                if let Some(sym) = symbols.by_id.get(sym_id as usize) {
                    if sym.kind != SymbolKind::from_db_str(k) {
                        continue;
                    }
                }
            }
            symbol_ids.push(sym_id);
        }
        symbol_ids
    }

    /// 获取调用边的行号（解包 call_line_packed），供 daemon query.callers/callees 返回
    ///
    /// CallEdge.call_line_packed 高 12 位 = file_id，低 20 位 = line
    #[inline]
    pub(crate) fn edge_call_line(&self, edge: &CallEdge) -> u32 {
        edge.call_line_packed & 0xFFFFF
    }
}

// ============================================
// CSR 构建
// ============================================

/// 构建 callee 名称二分索引和 name_idx → edge position 的紧凑 CSR。
/// F11 方案 A 公开包装：构建 callee_name 索引
/// 供 lib.rs::build_graph_from_c_files 调用
pub fn build_callee_name_index_public(
    forward_edges: &[CallEdge],
    names_pool: &str,
    name_offsets: &[u32],
) -> (Vec<u32>, Vec<u32>, Vec<u32>) {
    build_callee_name_index(forward_edges, names_pool, name_offsets)
}

fn build_callee_name_index(
    forward_edges: &[CallEdge],
    names_pool: &str,
    name_offsets: &[u32],
) -> (Vec<u32>, Vec<u32>, Vec<u32>) {
    let name_count = name_offsets.len().saturating_sub(1);
    let mut sorted_idxs: Vec<u32> = (0..name_count as u32).collect();
    sorted_idxs.sort_unstable_by(|a, b| {
        let ai = *a as usize;
        let bi = *b as usize;
        let an = &names_pool[name_offsets[ai] as usize..name_offsets[ai + 1] as usize];
        let bn = &names_pool[name_offsets[bi] as usize..name_offsets[bi + 1] as usize];
        an.cmp(bn)
    });

    let mut offsets = vec![0u32; name_count + 1];
    for edge in forward_edges {
        let idx = edge.callee_name_idx as usize;
        if idx < name_count {
            offsets[idx + 1] += 1;
        }
    }
    for i in 1..offsets.len() {
        offsets[i] += offsets[i - 1];
    }

    let mut positions = vec![0u32; forward_edges.len()];
    let mut cursors = offsets[..name_count].to_vec();
    for (position, edge) in forward_edges.iter().enumerate() {
        let idx = edge.callee_name_idx as usize;
        if idx < name_count {
            let slot = cursors[idx] as usize;
            positions[slot] = position as u32;
            cursors[idx] += 1;
        }
    }
    (sorted_idxs, offsets, positions)
}

/// 从边列表构建 CSR 邻接表
/// forward: 按 caller_id 排序
/// backward: 按 callee_id 排序（用于 get_callers 反向查询）
fn open_immutable_db(db_path: &str) -> PyResult<Connection> {
    let flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
        | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
        | rusqlite::OpenFlags::SQLITE_OPEN_URI;
    let normalized = db_path.replace('\\', "/");
    let uri = if normalized.starts_with("//") || normalized.starts_with("file:") {
        if normalized.contains('?') {
            format!("{}&immutable=1", normalized)
        } else {
            format!("{}?immutable=1", normalized)
        }
    } else {
        let prefix = if normalized.starts_with('/') { "file:" } else { "file:///" };
        format!("{}{}?immutable=1", prefix, normalized)
    };
    Connection::open_with_flags(&uri, flags).map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "open db failed: {} (uri={})", e, uri
        ))
    })
}

fn load_call_graph(conn: &Connection, symbols: &SymbolTable, workspace_id: i64) -> PyResult<(CallGraph, usize)> {
    let mut edges: Vec<CallEdge> = Vec::new();
    let mut callee_names_pool = String::new();
    let mut callee_names_offsets: Vec<u32> = Vec::new();
    let mut name_idx_map: FxHashMap<String, u32> = FxHashMap::default();

    // P0-2 整改：calls 表无 workspace_id 列，通过 JOIN symbols + file_instances 过滤。
    // workspace_id=0 时不过滤（兼容旧测试），>0 时限定本 workspace 的 caller。
    let sql_calls = if workspace_id > 0 {
        format!(
            "SELECT c.caller_id, c.callee_id, c.callee_name, c.call_line, c.is_cross_file
             FROM calls c
             JOIN symbols s ON c.caller_id = s.id
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE fi.workspace_id = {} AND fi.status != 'archived'",
            workspace_id
        )
    } else {
        "SELECT caller_id, callee_id, callee_name, call_line, is_cross_file FROM calls".to_string()
    };
    let mut stmt = conn.prepare(&sql_calls).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(
        format!("prepare calls query failed: {}", e)
    ))?;
    let call_iter = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)? as u32,
            row.get::<_, i64>(1)? as u32,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)? as u32,
            row.get::<_, i64>(4)? != 0,
        ))
    }).map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(
        format!("query calls failed: {}", e)
    ))?;

    for call in call_iter {
        let (caller_id, callee_id, callee_name, call_line, is_cross_file) = call
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(
                format!("read call row failed: {}", e)
            ))?;
        let callee_name_idx = match name_idx_map.get(&callee_name) {
            Some(&idx) => idx,
            None => {
                let idx = callee_names_offsets.len() as u32;
                callee_names_offsets.push(callee_names_pool.len() as u32);
                callee_names_pool.push_str(&callee_name);
                name_idx_map.insert(callee_name, idx);
                idx
            }
        };
        edges.push(CallEdge {
            caller_id,
            callee_id,
            call_line_packed: CallEdge::pack_call_line(call_line, is_cross_file),
            callee_name_idx,
        });
    }

    let edge_count = edges.len();
    callee_names_offsets.push(callee_names_pool.len() as u32);
    let max_id = symbols.by_id.len().max(1) - 1;
    let mut calls = build_csr(edges, callee_names_pool, callee_names_offsets, max_id);

    let mut has_caller = vec![false; symbols.by_id.len()];
    for &position in &calls.backward_positions {
        let edge = &calls.forward_edges[position as usize];
        if edge.callee_id != 0 && (edge.callee_id as usize) < has_caller.len() {
            has_caller[edge.callee_id as usize] = true;
        }
    }
    for (idx, symbol) in symbols.by_id.iter().enumerate() {
        if symbol.id == 0 && symbol.name_len == 0 {
            continue;
        }
        if !has_caller[idx] && symbol.kind == SymbolKind::Fn {
            calls.roots.push(idx as u32);
        }
    }

    let (name_sorted, position_offsets, positions) = build_callee_name_index(
        &calls.forward_edges,
        &calls.callee_names_pool,
        &calls.callee_names_offsets,
    );
    calls.callee_name_sorted_idxs = name_sorted;
    calls.callee_position_offsets = position_offsets;
    calls.callee_positions = positions;
    Ok((calls, edge_count))
}

/// F11 方案 A 公开包装：从边列表构建 CSR 邻接表
/// 供 lib.rs::build_graph_from_c_files 调用
pub fn build_csr_public(
    edges: Vec<CallEdge>,
    callee_names_pool: String,
    callee_names_offsets: Vec<u32>,
    max_id: usize,
) -> CallGraph {
    build_csr(edges, callee_names_pool, callee_names_offsets, max_id)
}

fn build_csr(
    edges: Vec<CallEdge>,
    callee_names_pool: String,
    callee_names_offsets: Vec<u32>,
    max_id: usize,
) -> CallGraph {
    let n = max_id + 1;  // id 范围 [0, max_id]

    // 1. forward: 按 caller_id 排序（edges 移动，不 clone）
    let mut forward_edges = edges;
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

    // 2. backward: 只保留 forward_edges 位置，按 callee_id 排序。
    assert!(
        u32::try_from(forward_edges.len()).is_ok(),
        "call edge count exceeds u32 position range"
    );
    let mut backward_positions: Vec<u32> = (0..forward_edges.len() as u32).collect();
    backward_positions.sort_unstable_by_key(|&position| {
        forward_edges[position as usize].callee_id
    });

    let mut backward_offsets = vec![0usize; n + 1];
    for &position in &backward_positions {
        let e = &forward_edges[position as usize];
        if (e.callee_id as usize) < n {
            backward_offsets[e.callee_id as usize + 1] += 1;
        }
    }
    for i in 1..=n {
        backward_offsets[i] += backward_offsets[i - 1];
    }

    // 名称索引和 roots 在加载函数中补充。
    CallGraph {
        forward_edges,
        forward_offsets,
        backward_positions,
        backward_offsets,
        callee_position_offsets: Vec::new(),
        callee_positions: Vec::new(),
        callee_names_pool,
        callee_names_offsets,
        callee_name_sorted_idxs: Vec::new(),
        roots: Vec::new(),
    }
}

// ============================================
// 辅助函数
// ============================================

/// ASCII 大小写不敏感子串匹配（零分配）
/// P2 优化：替代预计算 name_lower/qname_lower 的方案
/// P11 优化：用 memchr SIMD 加速首字符搜索，后续字符逐字节比较
/// 对 "Class" 查 "func_0"：原版 ~13 次比较，新版 ~2 次（SIMD 一次处理 16-32 字节）
#[inline]
fn contains_ascii_ci(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() { return true; }
    if haystack.len() < needle.len() { return false; }

    // P11：用 memchr2 SIMD 加速首字符搜索
    // 同时搜索 needle[0] 的大写和小写形式
    let first_lower = needle[0].to_ascii_lowercase();
    let first_upper = first_lower.to_ascii_uppercase();
    let needle_tail = &needle[1..];

    for pos in memchr::memchr2_iter(first_lower, first_upper, haystack) {
        // 检查剩余位置是否足够
        if pos + needle.len() > haystack.len() { continue; }
        // 逐字节比较后续字符（大小写不敏感）
        let tail = &haystack[pos + 1..pos + needle.len()];
        let mut found = true;
        for (h, n) in tail.iter().zip(needle_tail.iter()) {
            if h.to_ascii_lowercase() != *n { found = false; break; }
        }
        if found { return true; }
    }
    false
}

// ============================================
// P10: CallersBatch — get_callers 批量结果（PyClass，懒转换）
// ============================================

/// get_callers 的批量结果，持有纯 Rust 数据 + store 引用
/// 加载阶段零 Python 对象、零 String clone，访问时按需转换为 PyDict
#[pyclass]
pub struct CallersBatch {
    /// 纯 Rust 结果列表（每个 16 字节 Copy 结构体）
    results: Vec<CallerResult>,
    /// 持有 GraphStore 引用，用于访问 string pool 解析 name/qname/module
    store: Py<GraphStore>,
}

#[pymethods]
impl CallersBatch {
    /// 结果数量
    fn __len__(&self) -> usize {
        self.results.len()
    }

    /// 按索引获取单个结果（懒转换为 PyDict）
    fn __getitem__<'py>(&self, py: Python<'py>, idx: isize) -> PyResult<Bound<'py, PyAny>> {
        let len = self.results.len() as isize;
        let i = if idx < 0 { len + idx } else { idx };
        if i < 0 || i >= len {
            return Err(pyo3::exceptions::PyIndexError::new_err("index out of range"));
        }
        let r = self.results[i as usize];

        // 从 GraphStore 的 string pool 解析
        let store = self.store.borrow(py);
        let symbols = store.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let calls = store.calls.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("calls not ready"))?;

        let caller = symbols.by_id.get(r.caller_id as usize)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("caller not found"))?;
        let callee_name = calls.callee_name(r.callee_name_idx);
        let (callee_qname, callee_file, callee_module) = if r.callee_id != 0 {
            symbols.by_id.get(r.callee_id as usize)
                .map(|s| (symbols.sym_qname(s),
                          symbols.file_rel_path(s.file_instance_id),
                          symbols.sym_module(s)))
                .unwrap_or(("", "", ""))
        } else { ("", "", "") };

        let dict = PyDict::new(py);
        dict.set_item("caller_name", symbols.sym_name(caller))?;
        dict.set_item("caller_qualified", symbols.sym_qname(caller))?;
        dict.set_item("caller_file", symbols.file_rel_path(caller.file_instance_id))?;
        dict.set_item("caller_module", symbols.sym_module(caller))?;
        dict.set_item("caller_id", r.caller_id)?;
        dict.set_item("call_line", r.call_line_packed & 0x7FFF_FFFF)?;
        dict.set_item("is_cross_file", (r.call_line_packed >> 31) & 1 == 1)?;
        dict.set_item("callee_name", callee_name)?;
        dict.set_item("callee_id", r.callee_id)?;
        dict.set_item("callee_qualified", callee_qname)?;
        dict.set_item("callee_file", callee_file)?;
        dict.set_item("callee_module", callee_module)?;
        Ok(dict.into_any())
    }

    /// 一次性转换为 List[Dict]（兼容旧 API，但失去懒转换优势）
    fn to_list<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let mut list = Vec::with_capacity(self.results.len());
        let len = self.results.len() as isize;
        for i in 0..len {
            list.push(self.__getitem__(py, i)?);
        }
        Ok(list)
    }

    /// 结果数量（显式方法，供 Python 端 len() 之外的访问）
    fn count(&self) -> usize {
        self.results.len()
    }
}

// ============================================
// P11: SymbolSearchBatch — search_symbols 批量结果（PyClass，懒转换）
// ============================================

/// search_symbols 的批量结果，持有纯 Rust symbol_ids + store 引用
/// 加载阶段零 Python 对象、零 String clone，访问时按需转换为 PyDict
#[pyclass]
pub struct SymbolSearchBatch {
    /// 匹配的 symbol_id 列表
    symbol_ids: Vec<u32>,
    /// 持有 GraphStore 引用，用于访问 string pool 解析 name/qname/module
    store: Py<GraphStore>,
}

#[pymethods]
impl SymbolSearchBatch {
    /// 结果数量
    fn __len__(&self) -> usize {
        self.symbol_ids.len()
    }

    /// 按索引获取单个结果（懒转换为 PyDict）
    fn __getitem__<'py>(&self, py: Python<'py>, idx: isize) -> PyResult<Bound<'py, PyAny>> {
        let len = self.symbol_ids.len() as isize;
        let i = if idx < 0 { len + idx } else { idx };
        if i < 0 || i >= len {
            return Err(pyo3::exceptions::PyIndexError::new_err("index out of range"));
        }
        let sym_id = self.symbol_ids[i as usize];

        let store = self.store.borrow(py);
        let symbols = store.symbols.as_ref()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("store not loaded"))?;
        let sym = symbols.by_id.get(sym_id as usize)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("symbol not found"))?;

        let dict = PyDict::new(py);
        dict.set_item("id", sym.id)?;
        dict.set_item("name", symbols.sym_name(sym))?;
        dict.set_item("kind", sym.kind.as_str())?;
        dict.set_item("qualified_name", symbols.sym_qname(sym))?;
        dict.set_item("module_path", symbols.sym_module(sym))?;
        dict.set_item("start_line", sym.start_line)?;
        dict.set_item("end_line", sym.end_line)?;
        dict.set_item("depth", sym.depth)?;
        dict.set_item("file_path", symbols.file_rel_path(sym.file_instance_id))?;
        Ok(dict.into_any())
    }

    /// 一次性转换为 List[Dict]（兼容旧 API）
    fn to_list<'py>(&self, py: Python<'py>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let mut list = Vec::with_capacity(self.symbol_ids.len());
        let len = self.symbol_ids.len() as isize;
        for i in 0..len {
            list.push(self.__getitem__(py, i)?);
        }
        Ok(list)
    }

    /// 结果数量
    fn count(&self) -> usize {
        self.symbol_ids.len()
    }
}

// ============================================
// P5: 快照 dump/load — 零拷贝 mmap 加载
// ============================================

/// 快照文件魔数 "CWSN" (Call Warden Snapshot)
const SNAPSHOT_MAGIC: u32 = 0x4357534E;
/// 快照格式版本
const SNAPSHOT_VERSION: u32 = 2;
/// Section table 数量（P5-v2：扩展为 12 个 section，覆盖 callee_names + file_paths）
const SECTION_COUNT: usize = 12;

/// Section 索引
const SEC_BY_ID: usize = 0;                   // Vec<GraphSymbol>
const SEC_NAME_POOL: usize = 1;               // String bytes (name_pool)
const SEC_QNAME_POOL: usize = 2;              // String bytes (qname_pool)
const SEC_MODULE_POOL: usize = 3;             // String bytes (module_pool)
const SEC_FORWARD_EDGES: usize = 4;           // Vec<CallEdge>
const SEC_BACKWARD_POSITIONS: usize = 5;     // Vec<u32>
const SEC_FWD_OFFSETS: usize = 6;             // Vec<usize>
const SEC_BWD_OFFSETS: usize = 7;             // Vec<usize>
const SEC_CALLEE_NAMES_POOL: usize = 8;       // String bytes (callee_names_pool)
const SEC_CALLEE_NAMES_OFFSETS: usize = 9;    // Vec<u32>
const SEC_FILE_PATHS_POOL: usize = 10;        // String bytes (扁平化 file_paths)
const SEC_FILE_PATHS_OFFSETS: usize = 11;     // Vec<u32>

/// Section 描述：offset + len
#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct SectionEntry {
    offset: u64,
    len: u64,
}

/// 快照文件头
/// P5-v2：用 size_of 动态计算 HEADER_SIZE，避免硬编码错误
#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct SnapshotHeader {
    magic: u32,
    version: u32,
    symbol_count: u32,
    edge_count: u32,
    callee_name_count: u32,
    file_path_count: u32,
    reserved: [u8; 32],  // 填充字段，预留未来扩展
    sections: [SectionEntry; SECTION_COUNT],
}

/// 快照文件头大小（自动等于 sizeof(SnapshotHeader)）
const HEADER_SIZE: usize = std::mem::size_of::<SnapshotHeader>();
