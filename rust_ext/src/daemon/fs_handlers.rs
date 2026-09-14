//! 文件/构建面 handler（T02-fs 批次，9 个工具）。
//!
//! 对应 `deliverables/software-company/tool_migration_matrix.json` 中
//! target_backend=rust_native、batch=T02-fs 的 9 个纯本地 SQL 工具：
//! build_graph / build_directory / file_read / file_grep / file_list /
//! file_symbol_content / file_remove / file_health / refresh_file。
//!
//! 安全边界（设计 Q4，daemon handler 强制，Python 无权限逻辑）：
//! 1. `workspace_instance_id` 显式注入且归属当前 peer（owned_workspace ACL）；
//! 2. 路径参数经 `validate_owned_path`（canonicalize + owner_uid），禁止 `..` 穿越；
//! 3. 文件读取仅限 workspace 根内（host_real_root 前缀校验）。
//!
//! 工程决策（记录于交付摘要 §设计歧义处理）：build_graph / build_directory 的
//! 符号级解析仍由既有 `workspace.file.refresh` 深管线承担；本 handler 提供
//! 文件级全量索引重建（扫描 + file_instances upsert + 内容 hash），保证
//! daemon 权威写路径可独立重建文件清单，且不引入 Python 双实现。

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

use super::dispatch::{
    get_int_param_or, get_str_param, require_str_param, DaemonRpcError, PeerCredential,
};
use super::workspace::{owned_workspace, validate_owned_path, validate_owned_path_any, WorkspaceRegistry};

/// 可索引的源文件扩展名（文件面扫描白名单，与 db 层解析面一致）。
const INDEXABLE_EXTS: &[&str] = &[
    "py", "rs", "c", "h", "cpp", "hpp", "cc", "go", "java", "js", "jsx", "ts", "tsx",
    "rb", "php", "scala", "cs", "kt", "swift", "ex", "exs", "hcl", "vue", "svelte",
];

/// 跳过目录（与 db/db_build.py 的 skip_dirs 保持一致的常见噪声目录）。
///
/// 注意：`build` 不在此列。它是歧义目录——Gradle/Maven 项目放编译产物，
/// 但也有很多项目把编译脚本（build.sh / release.py / *.cmake）放这里。
/// 改由 ignore 契约接管：`.gitignore`/`.callwardenignore` 里写了 `build/`
/// 的项目会被 `load_ignore_dir_names` 剪枝；没写的项目里 `build/` 会被
/// 扫描，但只有 `INDEXABLE_EXTS` 白名单内的源码文件才入库（.class/.o
/// 等产物天然被挡）。
const SKIP_DIRS: &[&str] = &[
    ".git", "node_modules", "target", "dist", ".next", "__pycache__",
];

/// 判断 rel_path 是否属于可索引源文件。
pub fn is_indexable_path(rel_path: &str) -> bool {
    let norm = rel_path.replace('\\', "/");
    if norm.starts_with('.') || norm.contains("/.") {
        return false;
    }
    // 任一路径段命中跳过目录 → 不索引（如 node_modules/x.js、dist/app.js）。
    for seg in norm.split('/') {
        if SKIP_DIRS.contains(&seg) {
            return false;
        }
    }
    let lower = norm.to_lowercase();
    let ext = lower.rsplit('.').next().unwrap_or("");
    INDEXABLE_EXTS.contains(&ext)
}

/// 秒级 Unix 时间戳（ISO 8601 UTC 秒级契约的数值表示）。
pub fn now_ts_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 计算文件 sha256 内容哈希（十六进制小写）。
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// 归一化 workspace 根目录为权威单库 root_path 业务键。
///
/// 剥离 Windows verbatim 前缀（`\\?\` → 归一化后为 `//?/`）、反斜杠转正斜杠、
/// 盘符字母小写，与 Python 权威库既有行（如 `c:/git_work/TokenSlim`）对齐。
fn normalize_root_for_authority(root: &Path) -> String {
    let s = root.to_string_lossy().replace('\\', "/");
    let s = s.strip_prefix("//?/").map(str::to_string).unwrap_or(s);
    // 仅 Windows 盘符模式（如 `C:/...`）小写首字母；POSIX 路径不受影响
    let bytes = s.as_bytes();
    if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
        let mut owned = s.clone();
        owned[..1].make_ascii_lowercase();
        return owned;
    }
    s.to_string()
}

/// 将 host 真实根目录映射/登记到权威单库 workspaces 行，返回其自增 id。
///
/// 主机级单库时代，workspace 的业务键是完整路径（root_path UNIQUE）。
/// registry 的 workspace_id 与权威库自增 id 是两个不相干的 ID 空间，
/// 禁止把 registry id 直插权威库（既有 FK 失败，又有 id 撞行时
/// file_instances 错挂到别的 workspace 的数据错乱风险）。
/// name 列有 UNIQUE 约束：撞名时追加 root 哈希后缀降级重试。
pub fn ensure_workspace_row_by_root(
    conn: &rusqlite::Connection,
    root: &Path,
) -> Result<i64, rusqlite::Error> {
    let norm = normalize_root_for_authority(root);
    let select_id = |conn: &rusqlite::Connection| -> Result<i64, rusqlite::Error> {
        conn.query_row(
            "SELECT id FROM workspaces WHERE root_path = ?1",
            rusqlite::params![norm],
            |row| row.get::<_, i64>(0),
        )
    };
    if let Ok(id) = select_id(conn) {
        return Ok(id);
    }
    let base = root
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "workspace".to_string());
    let digest = sha256_hex(norm.as_bytes());
    let candidates = [
        base.clone(),
        format!("{base}-{}", &digest[..8]),
    ];
    for name in candidates {
        let res = conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES (?1, ?2, ?3, 0, 'daemon-managed workspace (by root_path)')
             ON CONFLICT(root_path) DO NOTHING",
            rusqlite::params![name, norm, now_ts_secs()],
        );
        match res {
            Ok(_) => {}
            Err(e) => {
                // 仅 name UNIQUE 冲突时换候选名重试；其余错误原样上抛
                if !e.to_string().contains("workspaces.name") {
                    return Err(e);
                }
                continue;
            }
        }
        if let Ok(id) = select_id(conn) {
            return Ok(id);
        }
    }
    Err(rusqlite::Error::QueryReturnedNoRows)
}

/// 解析 workspace 行并返回 (workspace_id, host_real_root)。
fn resolve_workspace(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    workspace_instance_id: &str,
) -> Result<(i64, PathBuf), DaemonRpcError> {
    let workspace = owned_workspace(registry, peer.uid, workspace_instance_id)?;
    let workspace_id = workspace
        .get("workspace_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string()))?;
    let root = workspace
        .get("host_real_root")
        .and_then(Value::as_str)
        .or_else(|| workspace.get("client_view_root").and_then(Value::as_str))
        .map(PathBuf::from)
        .ok_or_else(|| DaemonRpcError::internal_error("workspace 缺少 host_real_root".to_string()))?;
    Ok((workspace_id, root))
}

/// 校验路径必须在 workspace 根内，返回规范化绝对路径（String，validate_owned_path 契约）。
fn resolve_owned_path(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    workspace_instance_id: &str,
    file_path: &str,
) -> Result<String, DaemonRpcError> {
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let real = validate_owned_path(file_path, peer.uid, true)?;
    let real_root = std::fs::canonicalize(&root).unwrap_or(root);
    let real_root_str = real_root.to_string_lossy().to_string();
    let sep = std::path::MAIN_SEPARATOR.to_string();
    let ok = real == real_root_str || real.starts_with(&format!("{real_root_str}{sep}"));
    if !ok {
        return Err(DaemonRpcError::new(
            "path_escape",
            format!("路径不在 workspace 根内：{real}"),
        ));
    }
    Ok(real)
}

/// 打开 workspace codegraph DB（写路径，daemon 权威库）。
/// codegraph_db 为 None 时 fail-closed（daemon 未配置 codegraph 模板）。
///
/// P0-CR3 修复：写路径打开前必须先 `storage::initialize_or_migrate(path, 60)`
/// 确保 schema 就绪——此前 daemon 写图谱前未接 storage 初始化，半成品库
/// （sqlite_master 为空）残留导致 file_instances upsert 与查询全部报错。
fn open_codegraph_write(
    codegraph_db: Option<&Path>,
) -> Result<rusqlite::Connection, DaemonRpcError> {
    let path = codegraph_db.ok_or_else(|| {
        DaemonRpcError::new(
            "codegraph_db_unconfigured",
            "daemon 未配置 codegraph_db_path_template，无法执行写操作（fail-closed）",
        )
    })?;
    // fail-fast：入口即确保 schema v60（已就绪时为幂等 no-op）
    crate::storage::initialize_or_migrate(path, 60).map_err(|e| {
        DaemonRpcError::new(
            "codegraph_schema_init_failed",
            format!("codegraph schema 初始化/迁移失败（v60）: {e}"),
        )
    })?;
    rusqlite::Connection::open(path).map_err(|e| {
        DaemonRpcError::internal_error(format!("打开 codegraph DB 失败: {e}"))
    })
}

/// 扫描目录（递归），返回可索引文件列表。
///
/// P0-CR3 修复：递归时按目录名剪枝 SKIP_DIRS 与隐藏目录（`.` 开头）。
/// 此前实现先递归后过滤，walk 会深入 target/、node_modules/、.git/ 等
/// 十万级文件目录，全量 build_graph 直接挂死。
/// 从 workspace 根的 `.callwardenignore` / `.gitignore` 提取目录名剪枝集合
/// （合并硬编码 SKIP_DIRS）。Python 侧 ignore_spec 早已尊重这两个契约文件
/// （如本项目 `testcode/` 内嵌 Linux 内核源码，必须排除出默认全量建图）；
/// daemon 侧此前不读 → 全量 build_graph 啃 8 万+ 文件内核树。
/// 近似语义：仅取规则末段目录名（`tests/_gen/` → `_gen`）；跳过注释、
/// 否定（`!`）、通配符与纯文件扩展名规则；文件级精细 ignore 仍归 Python 管线。
fn load_ignore_dir_names(ws_root: &Path) -> std::collections::HashSet<String> {
    let mut set: std::collections::HashSet<String> =
        SKIP_DIRS.iter().map(|s| s.to_string()).collect();
    for fname in [".callwardenignore", ".gitignore"] {
        let p = ws_root.join(fname);
        let Ok(text) = std::fs::read_to_string(&p) else {
            continue;
        };
        for line in text.lines() {
            let l = line.trim();
            if l.is_empty() || l.starts_with('#') || l.starts_with('!') {
                continue;
            }
            let l = l.trim_end_matches('/');
            let l = l.strip_prefix('/').unwrap_or(l);
            let seg = l.rsplit('/').next().unwrap_or(l);
            if seg.is_empty() || seg.contains('*') || seg.contains('?') || seg.contains('.') && !l.contains('/') {
                continue;
            }
            set.insert(seg.to_string());
        }
    }
    set
}

fn scan_files(
    root: &Path,
    recursive: bool,
    skip: &std::collections::HashSet<String>,
) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(entries) = std::fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            let fname = entry.file_name().to_string_lossy().to_string();
            if path.is_dir() {
                // 剪枝：跳过噪声目录、隐藏目录与 ignore 契约目录，不再深入
                if recursive && !fname.starts_with('.') && !skip.contains(&fname) {
                    out.extend(scan_files(&path, true, skip));
                }
            } else if let Some(rel) = path.strip_prefix(root).ok() {
                if is_indexable_path(&rel.to_string_lossy()) {
                    out.push(path);
                }
            }
        }
    }
    out
}

/// 将 rel_path 映射为 multi_lang 解析管线的 language id（与 languages/ 注册表一致）。
/// 不支持解析的扩展名返回 None（仅做文件级索引，不出符号）。
fn parser_lang_id(rel_path: &str) -> Option<&'static str> {
    let ext = rel_path.rsplit('.').next()?.to_lowercase();
    Some(match ext.as_str() {
        "py" => "python",
        "rs" => "rust",
        "c" | "h" => "c",
        "cpp" | "hpp" | "cc" | "cxx" => "cpp",
        "go" => "go",
        "java" => "java",
        "ts" | "tsx" => "typescript",
        "js" | "jsx" | "mjs" | "cjs" => "javascript",
        "rb" => "ruby",
        "php" => "php",
        "scala" => "scala",
        "cs" => "csharp",
        "kt" => "kotlin",
        "swift" => "swift",
        "ex" | "exs" => "elixir",
        "hcl" | "tf" => "hcl",
        _ => return None,
    })
}

/// 单文件 tree-sitter 解析 + 符号/调用落库（P0-CR1 修复核心）。
///
/// 此前 daemon 建图链路只写 file_contents/file_instances，symbols/calls 恒为 0，
/// 图谱查询全链路空转。本函数复用既有 `multi_lang` 解析管线
/// （GenericParser + languages/* 配置，与 Python 权威库 content_hash 对齐的
/// canonicalize_source 规范化），把 ParseResult 持久化到 codegraph DB：
/// 1. symbol_contents upsert（含真实源码 content，供 file_symbol_content 查询）；
/// 2. 该文件旧 calls/symbols 清理 + 新 symbols 写入（捕获 row id 供调用边挂接）；
/// 3. raw calls 写入（先以 callee_id=0 落库，随后由 resolve_raw_calls 在
///    建图收尾时批量解析为已挂接边；同轮重建场景配合 CR12 降级重挂）。
///
/// 返回 (symbols_stored, calls_stored)。语言不支持或解析为空时 (0, 0)。
fn parse_and_store_symbols(
    conn: &rusqlite::Connection,
    file_instance_id: i64,
    abs_path: &Path,
    rel: &str,
) -> Result<(usize, usize), DaemonRpcError> {
    let Some(lang_id) = parser_lang_id(rel) else {
        return Ok((0, 0));
    };
    let Some(config) = crate::multi_lang::LangConfig::get(lang_id) else {
        return Ok((0, 0));
    };
    let parser = crate::multi_lang::GenericParser::new(std::sync::Arc::new(config));
    let module_path = module_path_of(rel);
    let result = parser.parse_file(&abs_path.to_string_lossy(), &module_path);
    if result.symbols.is_empty() {
        // 与 Python _save_symbols_for_version 一致：空符号不清旧数据、不写新数据
        return Ok((0, 0));
    }

    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| DaemonRpcError::internal_error(format!("symbols 事务开启失败: {e}")))?;
    let outcome = (|| -> Result<(usize, usize), rusqlite::Error> {
        // 1. symbol_contents upsert（写入真实 content）
        let mut contents_written = 0usize;
        for s in &result.symbols {
            conn.execute(
                "INSERT INTO symbol_contents \
                 (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, '', ?7) \
                 ON CONFLICT(content_hash) DO UPDATE SET \
                   name = excluded.name, kind = excluded.kind, content = excluded.content, \
                   signature = excluded.signature, has_comment = excluded.has_comment, \
                   qualified_name = excluded.qualified_name",
                rusqlite::params![
                    s.symbol_hash,
                    s.name,
                    s.kind,
                    s.content,
                    s.signature,
                    s.has_comment as i64,
                    s.qualified_name
                ],
            )?;
            contents_written += 1;
        }

        // 2. 清理该文件旧调用边与旧符号（幂等重建）。
        //    CR12：先捕获旧符号 id 集合，把**其它文件**指向这些 id 的已解析
        //    入边降级为 raw（callee_id=0）——重建后旧 id 即成悬空引用；
        //    降级后由 resolve_raw_calls 按 callee_name 重解析到新 id。
        let old_ids: Vec<i64> = {
            let mut stmt = conn
                .prepare("SELECT id FROM symbols WHERE file_instance_id = ?1")?;
            let rows = stmt
                .query_map(rusqlite::params![file_instance_id], |r| r.get::<_, i64>(0))?
                .filter_map(Result::ok)
                .collect();
            rows
        };
        if !old_ids.is_empty() {
            let placeholders = old_ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
            let params: Vec<&dyn rusqlite::ToSql> =
                old_ids.iter().map(|id| id as &dyn rusqlite::ToSql).collect();
            conn.execute(
                &format!(
                    "UPDATE calls SET callee_id = 0 WHERE callee_id IN ({placeholders})"
                ),
                params.as_slice(),
            )?;
            conn.execute(
                "DELETE FROM calls WHERE caller_id IN \
                 (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                rusqlite::params![file_instance_id],
            )?;
        }
        conn.execute(
            "DELETE FROM symbols WHERE file_instance_id = ?1",
            rusqlite::params![file_instance_id],
        )?;

        // 3. 写 symbols，按 local_id 建 file 内 id 映射（供调用边 caller_id 挂接）
        let mut local_id_map: std::collections::HashMap<u32, i64> =
            std::collections::HashMap::new();
        for s in &result.symbols {
            // INSERT OR IGNORE：minified 单行 bundle（如 d3.v7.min.js）常见
            // 同名同 start_line 多符号，会撞 UNIQUE(file_instance_id,name,start_line)；
            // 重复行不落库，调用边挂接到既有首行（图保真对压缩产物本就是有损的）。
            let inserted = conn.execute(
                "INSERT OR IGNORE INTO symbols \
                 (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, \
                  start_col, end_col, signature, has_comment, comment_status, module_path, qualified_name) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0, 0, ?8, ?9, 'pending', ?10, ?11)",
                rusqlite::params![
                    file_instance_id,
                    s.symbol_hash,
                    s.name,
                    s.kind,
                    s.visibility,
                    s.start_line,
                    s.end_line,
                    s.signature,
                    s.has_comment as i64,
                    if s.module_path.is_empty() { &module_path } else { &s.module_path },
                    s.qualified_name
                ],
            )?;
            let row_id = if inserted > 0 {
                conn.last_insert_rowid()
            } else {
                conn.query_row(
                    "SELECT id FROM symbols WHERE file_instance_id = ?1 AND name = ?2 AND start_line = ?3",
                    rusqlite::params![file_instance_id, s.name, s.start_line],
                    |r| r.get::<_, i64>(0),
                )
                .unwrap_or(0)
            };
            if s.local_id > 0 && row_id > 0 {
                local_id_map.insert(s.local_id, row_id);
            }
        }

        // 4. 写 raw calls（caller 必须能挂到本文件符号；顶层裸调用暂缺 synthetic
        //    module 符号，跳过并计数——与"跨文件 resolve 由既有管线承担"边界一致）
        let mut calls_written = 0usize;
        for rc in &result.calls {
            let Some(caller_local) = rc.caller_local_id else {
                continue;
            };
            let Some(&caller_id) = local_id_map.get(&caller_local) else {
                continue;
            };
            conn.execute(
                "INSERT INTO calls \
                 (caller_id, caller_name, caller_module, callee_name, callee_module, \
                  callee_qualified, callee_file, callee_id, call_line, is_cross_file) \
                 VALUES (?1, ?2, ?3, ?4, ?5, '', '', 0, ?6, ?7)",
                rusqlite::params![
                    caller_id,
                    rc.caller_name,
                    result.module_path,
                    rc.callee_name,
                    rc.callee_module,
                    rc.call_line,
                    rc.is_cross_file as i64
                ],
            )?;
            calls_written += 1;
        }
        Ok((contents_written, calls_written))
    })();

    match outcome {
        Ok((symbols, calls)) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| DaemonRpcError::internal_error(format!("symbols 事务提交失败: {e}")))?;
            Ok((symbols, calls))
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(DaemonRpcError::internal_error(format!(
                "符号落库失败（{}）: {}",
                rel, e
            )))
        }
    }
}

/// CR11（review §5.5）：daemon 侧批量调用边解析（batch resolve）。
///
/// 把 workspace 内 callee_id=0 的 raw 边挂接到符号：
/// - 唯一候选（workspace 内该 name 只有一个存活符号）→ 全量解析，
///   is_cross_file 按 caller/callee 是否同文件写 0/1；
/// - 多候选 → 仅解析 caller 与候选同文件的边（保守策略，宁缺勿错）；
/// - 零候选 → 保持 raw（callee_name 面查询仍可用，后续建图可能补齐）。
///
/// 解析输入不含 callee_qualified（当前解析器恒写 ''）；未来解析器带出
/// qualified 精确形式时可在此先行精确解析。
///
/// 返回 (resolved_edges, ambiguous_names)。按 distinct callee_name 逐名
/// 处理，SQL 语句数 ≈ 2×去重名数，与边数解耦。
fn resolve_raw_calls(
    conn: &rusqlite::Connection,
    workspace_id: i64,
) -> Result<(usize, usize), DaemonRpcError> {
    (|| -> Result<(usize, usize), rusqlite::Error> {
        let names: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT DISTINCT c.callee_name FROM calls c \
                 JOIN symbols cs ON c.caller_id = cs.id \
                 JOIN file_instances cf ON cs.file_instance_id = cf.id \
                 WHERE cf.workspace_id = ?1 AND c.callee_id = 0 AND c.callee_name != ''",
            )?;
            let rows = stmt
                .query_map(rusqlite::params![workspace_id], |r| r.get::<_, String>(0))?
                .filter_map(Result::ok)
                .collect();
            rows
        };

        let mut candidate_stmt = conn.prepare(
            "SELECT s.id, s.file_instance_id FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.name = ?2",
        )?;

        let mut resolved = 0usize;
        let mut ambiguous = 0usize;
        for name in &names {
            let candidates: Vec<(i64, i64)> = candidate_stmt
                .query_map(rusqlite::params![workspace_id, name], |r| {
                    Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
                })?
                .filter_map(Result::ok)
                .collect();
            match candidates.len() {
                0 => {
                    ambiguous += 1;
                }
                1 => {
                    let (sid, sfid) = candidates[0];
                    let n = conn.execute(
                        "UPDATE calls SET callee_id = ?1, \
                         is_cross_file = CASE WHEN EXISTS ( \
                           SELECT 1 FROM symbols cs WHERE cs.id = calls.caller_id \
                             AND cs.file_instance_id = ?2 \
                         ) THEN 0 ELSE 1 END \
                         WHERE callee_id = 0 AND callee_name = ?3",
                        rusqlite::params![sid, sfid, name],
                    )?;
                    resolved += n;
                }
                _ => {
                    // 多候选：仅挂接同文件的（每文件取首个候选 id）
                    let mut done_files: Vec<i64> = Vec::new();
                    for (sid, sfid) in &candidates {
                        if done_files.contains(sfid) {
                            continue;
                        }
                        done_files.push(*sfid);
                        let n = conn.execute(
                            "UPDATE calls SET callee_id = ?1, is_cross_file = 0 \
                             WHERE callee_id = 0 AND callee_name = ?2 \
                               AND caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?3)",
                            rusqlite::params![sid, name, sfid],
                        )?;
                        resolved += n;
                    }
                }
            }
        }
        Ok((resolved, ambiguous))
    })()
    .map_err(|e| DaemonRpcError::internal_error(format!("resolve_raw_calls 失败: {e}")))
}

/// file_instances upsert（UNIQUE(workspace_id, rel_path)），返回 file_instance_id。
///
/// `parsed` = true 时写 last_parsed=now + status='parsed'（符号已随行落库）；
/// false 时仅登记文件面（last_parsed=0，符号管线跳过的文件）。
fn upsert_file_instance(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    rel: &str,
    abs_path: &Path,
    hash: &str,
    mtime: f64,
    total_lines: i64,
    parsed: bool,
) -> Result<i64, DaemonRpcError> {
    let now = now_ts_secs();
    // CR13（review §5.5）：不支持语言的文件未解析，status 应为 'pending'
    // （schema 默认语义），与"已解析（零符号也算）"的 'parsed' 可区分。
    let (last_parsed, status) = if parsed {
        (now, "parsed")
    } else {
        (0.0, "pending")
    };
    conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
         ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
           abs_path = excluded.abs_path,
           current_content_hash = excluded.current_content_hash,
           mtime = excluded.mtime,
           total_lines = excluded.total_lines,
           last_parsed = excluded.last_parsed,
           status = excluded.status,
           module_path = excluded.module_path",
        rusqlite::params![
            workspace_id,
            rel,
            abs_path.to_string_lossy(),
            hash,
            mtime,
            total_lines,
            last_parsed,
            status,
            module_path_of(rel)
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("file_instances upsert: {e}")))?;
    conn.query_row(
        "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2",
        rusqlite::params![workspace_id, rel],
        |row| row.get::<_, i64>(0),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("file_instance_id 回查失败: {e}")))
}

/// `workspace.build_graph` —— 全量重建文件索引（file_instances upsert）。
pub fn handle_build_graph(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let (registry_ws_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    // 权威单库按 root_path 映射 workspace 行（registry id 与权威库 id 是两个 ID 空间）
    let workspace_id = ensure_workspace_row_by_root(&conn, &root)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 行映射失败: {e}")))?;
    let _ = registry_ws_id;
    let scan_root: PathBuf = match get_str_param(params, "scan_root") {
        Some(s) if !s.is_empty() => {
            // P0-CR2 修复：scan_root 允许目录（此前 require_file=true 直接报"不是文件"）
            PathBuf::from(validate_owned_path_any(s, peer.uid)?)
        }
        _ => root.clone(),
    };
    // rel 计算基于 canonical root（client_view_root 可能与真实路径大小写/前缀不一致）
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    // ignore 契约剪枝（.callwardenignore/.gitignore，workspace 根语义）
    let skip = load_ignore_dir_names(&root);
    let files = scan_files(&scan_root, true, &skip);
    let mut scanned = 0usize;
    let mut inserted = 0usize;
    let mut unchanged = 0usize;
    let mut symbol_total = 0usize;
    let mut call_total = 0usize;
    let now = now_ts_secs();
    for path in files {
        let rel = path
            .strip_prefix(&root_canon)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let hash = sha256_hex(&bytes);
        let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(now);
        scanned += 1;
        // file_contents upsert
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, infer_language(&rel), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
        // 先 upsert file_instances 拿真实 id（语言可解析时标记 parsed），再做符号落库
        let parsed = parser_lang_id(&rel).is_some();
        let fid = upsert_file_instance(
            &conn, workspace_id, &rel, &path, &hash, mtime, total_lines, parsed,
        )?;
        if parsed {
            // P0-CR1：符号抽取随建图落库（tree-sitter 管线）
            let (sym, calls) = parse_and_store_symbols(&conn, fid, &path, &rel)?;
            symbol_total += sym;
            call_total += calls;
            inserted += 1;
        } else {
            unchanged += 1;
        }
    }
    // CR11：建图收尾批量解析 raw 调用边（含本轮重建降级的重挂）
    let (resolved, ambiguous) = resolve_raw_calls(&conn, workspace_id)?;
    let mut m = Map::new();
    m.insert("ok".into(), Value::Bool(true));
    m.insert("scanned".into(), Value::Number(scanned.into()));
    m.insert("inserted".into(), Value::Number(inserted.into()));
    m.insert("unchanged".into(), Value::Number(unchanged.into()));
    m.insert("symbols".into(), Value::Number(symbol_total.into()));
    m.insert("calls".into(), Value::Number(call_total.into()));
    m.insert("calls_resolved".into(), Value::Number(resolved.into()));
    m.insert("calls_ambiguous".into(), Value::Number(ambiguous.into()));
    Ok(Value::Object(m))
}

/// `workspace.build_directory` —— 重建指定目录文件索引。
pub fn handle_build_directory(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let dir_path = require_str_param(params, "dir_path")?;
    let (registry_ws_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    let workspace_id = ensure_workspace_row_by_root(&conn, &root)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 行映射失败: {e}")))?;
    let _ = registry_ws_id;
    // P0-CR2 修复：dir_path 允许目录（此前 require_file=true 报"path_not_found: 不是文件"，
    // 目录递归建图 RPC 从未真正可用）
    let real_dir = PathBuf::from(validate_owned_path_any(dir_path, peer.uid)?);
    let recursive = params.get("recursive").and_then(Value::as_bool).unwrap_or(false);
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    // ignore 契约剪枝（.callwardenignore/.gitignore，workspace 根语义）
    let skip = load_ignore_dir_names(&root);
    let files = scan_files(&real_dir, recursive, &skip);
    let scanned_total = files.len();
    let mut refreshed = 0usize;
    let mut symbol_total = 0usize;
    let mut call_total = 0usize;
    let now = now_ts_secs();
    for path in files {
        let rel = path
            .strip_prefix(&root_canon)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let hash = sha256_hex(&bytes);
        let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(now);
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, infer_language(&rel), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
        let parsed = parser_lang_id(&rel).is_some();
        let fid = upsert_file_instance(
            &conn, workspace_id, &rel, &path, &hash, mtime, total_lines, parsed,
        )?;
        if parsed {
            let (sym, calls) = parse_and_store_symbols(&conn, fid, &path, &rel)?;
            symbol_total += sym;
            call_total += calls;
        }
        refreshed += 1;
    }
    // CR11：目录建图收尾批量解析 raw 调用边
    let (resolved, ambiguous) = resolve_raw_calls(&conn, workspace_id)?;
    let mut m = Map::new();
    m.insert("ok".into(), Value::Bool(true));
    m.insert("scanned".into(), Value::Number(scanned_total.into()));
    m.insert("refreshed".into(), Value::Number(refreshed.into()));
    m.insert("symbols".into(), Value::Number(symbol_total.into()));
    m.insert("calls".into(), Value::Number(call_total.into()));
    m.insert("calls_resolved".into(), Value::Number(resolved.into()));
    m.insert("calls_ambiguous".into(), Value::Number(ambiguous.into()));
    Ok(Value::Object(m))
}

/// `workspace.file.read` —— 读取文件内容（offset/limit 分页）。
pub fn handle_file_read(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let real = resolve_owned_path(registry, peer, workspace_instance_id, file_path)?;
    let content = std::fs::read_to_string(Path::new(&real)).map_err(|e| {
        DaemonRpcError::new("file_read_failed", format!("读取 {real} 失败: {e}"))
    })?;
    let lines: Vec<&str> = content.lines().collect();
    let offset = get_int_param_or(params, "offset", 0).max(0) as usize;
    let limit = get_int_param_or(params, "limit", 200).max(1) as usize;
    let start = offset.min(lines.len());
    let end = (start + limit).min(lines.len());
    let selected = lines[start..end].join("\n");
    let mut m = Map::new();
    m.insert("file_path".into(), Value::String(file_path.to_string()));
    m.insert("offset".into(), Value::Number(offset.into()));
    m.insert("limit".into(), Value::Number(limit.into()));
    m.insert("total_lines".into(), Value::Number(lines.len().into()));
    m.insert("content".into(), Value::String(selected));
    Ok(Value::Object(m))
}

/// `workspace.file.grep` —— 递归 grep（大小写不敏感子串/简单正则）。
pub fn handle_file_grep(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let pattern = require_str_param(params, "pattern")?;
    if pattern.is_empty() {
        return Err(DaemonRpcError::invalid_params("pattern 不能为空"));
    }
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            let real = PathBuf::from(validate_owned_path(p, peer.uid, true)?);
            let root_str = root.to_string_lossy().to_string();
            if !(real == root || real.starts_with(format!("{root_str}/"))) {
                return Err(DaemonRpcError::new(
                    "path_escape",
                    "path 不在 workspace 根内".to_string(),
                ));
            }
            real
        }
        _ => root.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    let head_limit = get_int_param_or(params, "head_limit", 50).max(1) as usize;
    let skip = load_ignore_dir_names(&root);
    let files = scan_files(&base, true, &skip);
    let needle = pattern.to_lowercase();
    let mut matches: Vec<Value> = Vec::new();
    for path in files {
        if matches.len() >= head_limit {
            break;
        }
        let rel = path
            .strip_prefix(&root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        if !glob.is_empty() && !glob_match(&rel, &glob) {
            continue;
        }
        let Ok(content) = std::fs::read_to_string(&path) else { continue };
        for (idx, line) in content.lines().enumerate() {
            if matches.len() >= head_limit {
                break;
            }
            if line.to_lowercase().contains(&needle) {
                matches.push(json!({
                    "file_path": rel,
                    "line": idx + 1,
                    "text": line,
                }));
            }
        }
    }
    Ok(json!({ "matches": matches, "count": matches.len() }))
}

/// `workspace.file.list` —— 列出目录内容。
pub fn handle_file_list(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            let real = PathBuf::from(validate_owned_path(p, peer.uid, true)?);
            let root_str = root.to_string_lossy().to_string();
            if !(real == root || real.starts_with(format!("{root_str}/"))) {
                return Err(DaemonRpcError::new("path_escape", "path 不在 workspace 根内".to_string()));
            }
            real
        }
        _ => root.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    let mut rows: Vec<Value> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&base) {
        for entry in entries.flatten() {
            let path = entry.path();
            let rel = path
                .strip_prefix(&root)
                .map(|p| p.to_string_lossy().replace('\\', "/"))
                .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
            if !glob.is_empty() && !glob_match(&rel, &glob) {
                continue;
            }
            let is_dir = path.is_dir();
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            rows.push(json!({ "rel_path": rel, "size": size, "is_dir": is_dir }));
        }
    }
    rows.sort_by_key(|r| r["rel_path"].as_str().unwrap_or("").to_string());
    Ok(Value::Array(rows))
}

/// `workspace.file.symbol_content` —— 查询符号内容（symbol_contents 表）。
pub fn handle_file_symbol_content(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let symbol_name = require_str_param(params, "symbol_name")?;
    let conn = open_codegraph_write(codegraph_db)?;
    let mut stmt = conn
        .prepare(
            "SELECT s.qualified_name, sc.content, s.start_line, s.end_line
             FROM symbols s
             JOIN symbol_contents sc ON sc.content_hash = s.symbol_hash
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.rel_path = ?1 AND (s.name = ?2 OR s.qualified_name = ?2)
             ORDER BY s.start_line ASC LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content prepare: {e}")))?;
    let mut rows = stmt
        .query_map(
            rusqlite::params![file_path, symbol_name],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                ))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content query: {e}")))?;
    if let Some(row) = rows.next() {
        let (qualified_name, content, start_line, end_line) =
            row.map_err(|e| DaemonRpcError::internal_error(format!("symbol_content row: {e}")))?;
        return Ok(json!({
            "qualified_name": qualified_name,
            "content": content,
            "start_line": start_line,
            "end_line": end_line,
        }));
    }
    Ok(Value::Null)
}

/// `workspace.file.remove` —— 删除文件（写操作，记录 destructive_operations）。
pub fn handle_file_remove(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let (registry_ws_id, _root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let real = validate_owned_path(file_path, peer.uid, true)?;
    std::fs::remove_file(Path::new(&real)).map_err(|e| {
        DaemonRpcError::new("file_remove_failed", format!("删除 {real} 失败: {e}"))
    })?;
    if let Some(db) = codegraph_db {
        if let Ok(conn) = rusqlite::Connection::open(db) {
            let workspace_id = ensure_workspace_row_by_root(&conn, _root.as_path()).unwrap_or(registry_ws_id);
            let rel = real.replace('\\', "/");
            let _ = conn.execute(
                "DELETE FROM file_instances WHERE workspace_id = ?1 AND (rel_path = ?2 OR abs_path = ?3)",
                rusqlite::params![workspace_id, rel, real],
            );
            let _ = conn.execute(
                "INSERT INTO destructive_operations (workspace_id, operation_type, target_path, created_at)
                 VALUES (?1, 'file_remove', ?2, ?3)",
                rusqlite::params![workspace_id, rel, now_ts_secs()],
            );
        }
    }
    Ok(json!({ "ok": true, "removed": true }))
}

/// `workspace.file.refresh_file` —— 刷新单个文件（文件索引增量更新，MCP
/// `refresh_file(file_path)` 契约：单文件增量更新）。
pub fn handle_refresh_file(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let (registry_ws_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    let workspace_id = ensure_workspace_row_by_root(&conn, &root)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 行映射失败: {e}")))?;
    let _ = registry_ws_id;
    let real = PathBuf::from(validate_owned_path(file_path, peer.uid, true)?);
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    let rel = real
        .strip_prefix(&root_canon)
        .map(|p| p.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| file_path.replace('\\', "/"));
    let bytes = std::fs::read(&real).map_err(|e| {
        DaemonRpcError::new(
            "file_read_failed",
            format!("读取 {} 失败: {e}", real.to_string_lossy()),
        )
    })?;
    let hash = sha256_hex(&bytes);
    let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
    let mtime = std::fs::metadata(&real)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or_else(now_ts_secs);
    let now = now_ts_secs();
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![hash, infer_language(&rel), total_lines, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
    // P0-CR1 修复：refresh_file 同样走符号落库（回归基准：refresh 单文件后 symbol_count>0）
    let parsed = parser_lang_id(&rel).is_some();
    let fid = upsert_file_instance(
        &conn, workspace_id, &rel, &real, &hash, mtime, total_lines, parsed,
    )?;
    let (symbol_count, call_count) = if parsed {
        parse_and_store_symbols(&conn, fid, &real, &rel)?
    } else {
        (0, 0)
    };
    // CR11：单文件刷新收尾也做批量解析（顺带把历史 raw 边随全库数据补挂）
    let (resolved, ambiguous) = resolve_raw_calls(&conn, workspace_id)?;
    Ok(json!({
        "ok": true,
        "file_path": rel,
        "content_hash": hash,
        "total_lines": total_lines,
        "symbols": symbol_count,
        "calls": call_count,
        "calls_resolved": resolved,
        "calls_ambiguous": ambiguous,
    }))
}

/// `workspace.file.health` —— 文件健康检查（存在性/大小/mtime/可读）。
pub fn handle_file_health(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let real = validate_owned_path(file_path, peer.uid, false)?;
    let real_path = Path::new(&real);
    let mut m = Map::new();
    m.insert("file_path".into(), Value::String(file_path.to_string()));
    if !real_path.exists() {
        m.insert("exists".into(), Value::Bool(false));
        m.insert("size".into(), Value::Number(0.into()));
        m.insert("mtime".into(), Value::Null);
        m.insert("readable".into(), Value::Bool(false));
        return Ok(Value::Object(m));
    }
    let meta = std::fs::metadata(real_path).map_err(|e| {
        DaemonRpcError::new("file_health_failed", format!("stat {real} 失败: {e}"))
    })?;
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let readable = std::fs::File::open(real_path).is_ok();
    m.insert("exists".into(), Value::Bool(true));
    m.insert("size".into(), Value::Number(meta.len().into()));
    m.insert("mtime".into(), serde_json::Number::from_f64(mtime).map(Value::Number).unwrap_or(Value::Null));
    m.insert("readable".into(), Value::Bool(readable));
    Ok(Value::Object(m))
}

/// 根据扩展名推断语言（与 db 层 language 字段语义一致）。
fn infer_language(rel_path: &str) -> String {
    let lower = rel_path.to_lowercase();
    let ext = lower.rsplit('.').next().unwrap_or("");
    match ext {
        "py" => "python",
        "rs" => "rust",
        "c" | "h" => "c",
        "cpp" | "hpp" | "cc" | "cxx" => "cpp",
        "go" => "go",
        "java" => "java",
        "js" | "jsx" | "ts" | "tsx" => "typescript",
        "rb" => "ruby",
        "php" => "php",
        "scala" => "scala",
        "cs" => "csharp",
        "kt" => "kotlin",
        "swift" => "swift",
        "ex" | "exs" => "elixir",
        "hcl" => "hcl",
        _ => "unknown",
    }
    .to_string()
}

/// 从 rel_path 派生模块路径（目录路径，去掉扩展名）。
fn module_path_of(rel_path: &str) -> String {
    let norm = rel_path.replace('\\', "/");
    match norm.rfind('.') {
        Some(idx) => norm[..idx].to_string(),
        None => norm,
    }
}

/// 极简 glob 匹配（支持 `*` 与 `**`），用于 file_list/file_grep 的 glob 过滤。
fn glob_match(path: &str, glob: &str) -> bool {
    if glob.is_empty() {
        return true;
    }
    let pattern = glob.replace("**", "\u{0}");
    let pattern = pattern.replace('*', "[^/]*");
    let pattern = pattern.replace('\u{0}', ".*");
    let re = match regex::Regex::new(&format!("^{}$", pattern)) {
        Ok(re) => re,
        Err(_) => return path.contains(&glob),
    };
    re.is_match(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_load_ignore_dir_names() {
        let tmp = std::env::temp_dir().join(format!("cw_ignore_test_{}", std::process::id()));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(
            tmp.join(".callwardenignore"),
            "# 注释\ntestcode/\ntests/_gen/\n*.log\n!keep/\n/build/\n",
        )
        .unwrap();
        let skip = load_ignore_dir_names(&tmp);
        // 硬编码基线（build 已移出 SKIP_DIRS：歧义目录，交给 ignore 契约）
        assert!(skip.contains("node_modules") && skip.contains("target"));
        // 末段目录名提取（tests/_gen/ → _gen）
        assert!(skip.contains("testcode"));
        assert!(skip.contains("_gen"));
        // build 来自 ignore 契约（测试 .callwardenignore 的 /build/），非硬编码
        assert!(skip.contains("build"));
        // 通配符、否定、扩展名规则不入集
        assert!(!skip.contains("*.log"));
        assert!(!skip.contains("keep"));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_is_indexable_path() {
        assert!(is_indexable_path("src/main.rs"));
        assert!(is_indexable_path("app.py"));
        assert!(!is_indexable_path(".git/config"));
        assert!(!is_indexable_path("node_modules/x.js"));
        assert!(!is_indexable_path("README.md"));
        // build/ 不再硬编码跳过：编译脚本（build.sh 旁的 release.py）
        // 与编译产物（.class/.o 不在白名单）应区别对待
        assert!(is_indexable_path("build/release.py"));
        assert!(is_indexable_path("build/scripts/build.py"));
        assert!(!is_indexable_path("build/output.a"));
    }

    #[test]
    fn test_parser_lang_id() {
        assert_eq!(parser_lang_id("cli/main.py"), Some("python"));
        assert_eq!(parser_lang_id("src/lib.rs"), Some("rust"));
        assert_eq!(parser_lang_id("app.tsx"), Some("typescript"));
        assert_eq!(parser_lang_id("app.jsx"), Some("javascript"));
        assert_eq!(parser_lang_id("infra/main.tf"), Some("hcl"));
        assert_eq!(parser_lang_id("README.md"), None);
    }

    #[test]
    fn test_sha256_hex() {
        let h = sha256_hex(b"hello");
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_glob_match() {
        assert!(glob_match("src/a.rs", "src/*.rs"));
        assert!(glob_match("a/b/c.rs", "**/*.rs"));
        assert!(!glob_match("a/b/c.rs", "src/*.rs"));
        assert!(glob_match("", ""));
    }

    #[test]
    fn test_module_path_of() {
        assert_eq!(module_path_of("src/main.rs"), "src/main");
        assert_eq!(module_path_of("a/b.py"), "a/b");
    }

    #[test]
    fn test_upsert_file_instance_and_symbol_store_roundtrip() {
        // 临时库：open_codegraph_write 的 schema fail-fast 等价路径
        let tmp = std::env::temp_dir().join(format!(
            "cw_fs_handlers_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        // P0-CR3：fresh DB 自动建 schema（v60）
        crate::storage::initialize_or_migrate(&db_path, 60).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        // 文件面 + 符号面 roundtrip
        let src = tmp.join("m.py");
        std::fs::write(&src, "def foo():\n    return bar()\n\ndef bar():\n    return 1\n").unwrap();
        let bytes = std::fs::read(&src).unwrap();
        let hash = sha256_hex(&bytes);
        // 生产顺序：file_contents 先于 file_instances（FK current_content_hash）
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, 'python', 4, 0.0)",
            rusqlite::params![hash],
        )
        .unwrap();
        let fid = upsert_file_instance(
            &conn, ws_id, "m.py", &src, &hash, 0.0, 4, true,
        )
        .unwrap();
        assert!(fid > 0);
        let (syms, calls) = parse_and_store_symbols(&conn, fid, &src, "m.py").unwrap();
        assert!(syms >= 2, "expected >=2 symbols, got {syms}");
        assert!(calls >= 1, "expected >=1 call, got {calls}");
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n as usize, syms);
        // 幂等重建：再跑一次不重复累积
        let (syms2, _calls2) = parse_and_store_symbols(&conn, fid, &src, "m.py").unwrap();
        assert_eq!(syms2, syms);
        let n2: i64 = conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n2 as usize, syms);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// 测试助手：临时 DB + workspace + 单文件解析落库
    fn setup_ws_and_parse(
        tmp: &std::path::Path,
        conn: &rusqlite::Connection,
        ws_id: i64,
        rel: &str,
        code: &str,
    ) -> i64 {
        let src = tmp.join(rel.replace('/', "_"));
        std::fs::write(&src, code).unwrap();
        let bytes = std::fs::read(&src).unwrap();
        let hash = sha256_hex(&bytes);
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, 'python', 0, 0.0)",
            rusqlite::params![hash],
        )
        .unwrap();
        let fid = upsert_file_instance(
            conn, ws_id, rel, &src, &hash, 0.0, 0, true,
        )
        .unwrap();
        let (syms, _calls) =
            parse_and_store_symbols(conn, fid, &src, rel).unwrap();
        assert!(syms >= 1, "expected symbols in {rel}");
        fid
    }

    #[test]
    fn test_resolve_raw_calls_cross_file() {
        // CR11：b.py 的 caller() 调用 a.py 的 helper() —— 建图收尾批量解析
        // 后边应挂接 callee_id>0 且 is_cross_file=1。
        let tmp = std::env::temp_dir().join(format!(
            "cw_resolve_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, 60).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "a.py",
            "def helper():\n    return 1\n",
        );
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "b.py",
            "def caller():\n    return helper()\n",
        );
        // 建图收尾批量解析
        let (resolved, _ambiguous) = resolve_raw_calls(&conn, ws_id).unwrap();
        assert!(resolved >= 1, "expected >=1 resolved edge, got {resolved}");
        let (callee_id, cross): (i64, i64) = conn
            .query_row(
                "SELECT callee_id, is_cross_file FROM calls WHERE callee_name = 'helper'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(callee_id > 0, "helper call must resolve to a symbol id");
        assert_eq!(cross, 1, "a.py→b.py call must be marked cross-file");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_rebuild_demotes_dangling_resolved_edges() {
        // CR12：a.py 重建（helper 改名 helper2）后，其它文件指向旧 helper id
        // 的已解析边必须先降级 raw，再重解析；不得残留悬空 callee_id。
        let tmp = std::env::temp_dir().join(format!(
            "cw_dangle_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, 60).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(&tmp, &conn, ws_id, "a.py", "def helper():\n    return 1\n");
        setup_ws_and_parse(&tmp, &conn, ws_id, "b.py", "def caller():\n    return helper()\n");
        resolve_raw_calls(&conn, ws_id).unwrap();
        let old_callee: i64 = conn
            .query_row("SELECT callee_id FROM calls WHERE callee_name = 'helper'", [], |r| r.get(0))
            .unwrap();
        assert!(old_callee > 0);

        // 重建 a.py：helper 改名 helper2（旧 helper 符号行被删除）
        setup_ws_and_parse(&tmp, &conn, ws_id, "a.py", "def helper2():\n    return 2\n");
        // 降级发生在 parse_and_store_symbols 内：此时旧 id 已悬空降级
        let dangling: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM calls WHERE callee_id > 0 AND callee_id NOT IN (SELECT id FROM symbols)",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(dangling, 0, "no dangling resolved edges allowed after rebuild");
        // 重解析：helper 无候选 → 保持 raw；无悬空
        let _ = resolve_raw_calls(&conn, ws_id).unwrap();
        let raw_back: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM calls WHERE callee_name = 'helper' AND callee_id = 0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(raw_back >= 1, "demoted edge must stay raw when callee gone");
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
