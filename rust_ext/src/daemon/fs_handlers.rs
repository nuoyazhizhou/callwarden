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

use rusqlite::OptionalExtension;
use serde_json::{json, Map, Value};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};

use super::dispatch::{
    get_int_param_or, get_str_param, get_str_param_or, require_str_param, DaemonRpcError,
    PeerCredential,
};
use super::task_collab::TaskCollabStore;
use super::workspace::{
    owned_workspace, owned_workspace_by_id, validate_owned_path, validate_owned_path_any,
    WorkspaceRegistry,
};

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
///
/// TP-11G P1-C（CI Windows 双测试红根因）：**先 canonicalize 再词法归一化**。
/// 纯词法归一化把同一目录的短名（8.3，如 CI 的 `RUNNER~1`）与长名形态
/// 算作两个键——workspace.register 用一个形态、refresh 的 canonicalize
/// 路径用另一个形态时，`ensure_workspace_row_by_root` 会为同一目录建两行
/// （file_instances 分裂，按 workspace_id 的查询 QueryReturnedNoRows）。
/// canonicalize 把 8.3 短名/符号链接/大小写收敛到同一实体路径；**失败
/// （路径不存在）时退回原词法归一化**，不破坏既有的不存在路径查找语义。
fn normalize_root_for_authority(root: &Path) -> String {
    let lexical = |p: &Path| -> String {
        let s = p.to_string_lossy().replace('\\', "/");
        let s = s.strip_prefix("//?/").map(str::to_string).unwrap_or(s);
        // 仅 Windows 盘符模式（如 `C:/...`）小写首字母；POSIX 路径不受影响
        let bytes = s.as_bytes();
        if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
            let mut owned = s.clone();
            owned[..1].make_ascii_lowercase();
            return owned;
        }
        s.to_string()
    };
    match std::fs::canonicalize(root) {
        Ok(canonical) => lexical(&canonical),
        Err(_) => lexical(root),
    }
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
///
/// 🔴 返回的 root 用于**文件系统操作**（扫描/strip_prefix/建图），不是身份键。
/// registry 行的 `host_real_root` 是 `normalize_identity_root` 的产物
/// （workspace.rs:222：Windows 上全路径 ASCII 小写 + `\`→`/`），只服务于
/// instance-id 哈希/去重。把它当文件系统根用会丢失真实大小写：
/// `handle_build_graph` 以它为扫描种子 → 遍历出的路径前缀全小写 →
/// `strip_prefix(&root_canon)`（canonicalize：真实大小写 + `\\?\` 前缀）
/// 必失败 → rel 退化为**全小写绝对路径**写进 `file_instances.rel_path`，
/// 进而连锁击穿 query.file（L5）、run_semgrep_and_save 的 FK 挂接（X3c）、
/// 以及权威库 `workspaces.root_path` 的大小写。
///
/// 注册时该值已由 `validate_owned_path` 规范化（canonical），此处再
/// `canonicalize` 仅还原大小写与前缀（对已规范路径幂等，无 symlink 旁路）。
/// 失败时回退原值（不阻断只读语义）。
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
    let root = std::fs::canonicalize(&root).unwrap_or(root);
    Ok((workspace_id, root))
}

/// 校验已规范化路径必须位于 workspace 根内（组件级前缀校验，fail-closed）。
///
/// TP-11A（D1/D3/D4）：fs RPC 家族统一根目录限制。file.read 经
/// `resolve_owned_path`、file.grep/file.list 经内联 `starts_with` 此前已有
/// 同款检查，而 file.remove / file.refresh_file / build_directory /
/// build_graph(scan_root) / file.health 只做了 owner 校验
/// （`validate_owned_path[_any]`）——owner ≠ workspace 根：全部本用户拥有
/// 的宿主文件都能过 owner 关，导致越根删除/读取/索引/信息泄露。本 helper
/// 把限制逻辑收敛为一处，根与目标同为 canonicalize 产物（真实大小写 +
/// `\\?\` 前缀），比较语义与 `resolve_owned_path` 完全一致。
fn ensure_root_containment(
    root: &std::path::Path,
    real: &str,
) -> Result<(), DaemonRpcError> {
    let real_root = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let real_root_str = real_root.to_string_lossy().to_string();
    let sep = std::path::MAIN_SEPARATOR.to_string();
    let ok = real == real_root_str || real.starts_with(&format!("{real_root_str}{sep}"));
    if !ok {
        return Err(DaemonRpcError::new(
            "path_escape",
            format!("路径不在 workspace 根内：{real}"),
        ));
    }
    Ok(())
}

/// review F6：daemon 侧 symlink containment（写治理路径）。
///
/// `normalize_write_rel_path` 只做词法归一化（拒绝绝对路径与 `..`），无法
/// 识别 workspace 根内指向根外的 symlink。本 helper 逐级解析 `rel_path` 的
/// **已存在前缀**：任一组件是 symlink 且其 canonical 目标落在根外即
/// fail-closed；不存在的前缀（新建文件/目录）无需检查。
fn ensure_no_symlink_escape(root: &Path, rel_path: &str) -> Result<(), DaemonRpcError> {
    use std::path::Component;
    let root_real = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    let mut cur = root_real.clone();
    for comp in Path::new(rel_path).components() {
        match comp {
            Component::Normal(seg) => cur.push(seg),
            _ => {
                return Err(DaemonRpcError::new(
                    "E_WRITE_PATH_ESCAPE",
                    format!("write path 含非法路径段: {rel_path}"),
                ))
            }
        }
        match std::fs::symlink_metadata(&cur) {
            Ok(meta) if meta.file_type().is_symlink() => {
                let resolved = std::fs::canonicalize(&cur).map_err(|e| {
                    DaemonRpcError::new(
                        "E_WRITE_PATH_ESCAPE",
                        format!("symlink 无法解析（{rel_path}）: {e}"),
                    )
                })?;
                if !resolved.starts_with(&root_real) {
                    return Err(DaemonRpcError::new(
                        "E_WRITE_PATH_ESCAPE",
                        format!("write path 经 symlink 逃逸 workspace: {rel_path}"),
                    ));
                }
                cur = resolved;
            }
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => break,
            Err(e) => {
                return Err(DaemonRpcError::internal_error(format!(
                    "write path symlink 检查失败（{rel_path}）: {e}"
                )))
            }
        }
    }
    Ok(())
}

/// review F6：写路径 containment 组合校验（词法根内 + symlink 解析后根内）。
fn check_write_containment(root: &Path, rel_path: &str) -> Result<(), DaemonRpcError> {
    let root_real = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
    ensure_root_containment(&root_real, &root_real.join(rel_path).to_string_lossy())?;
    ensure_no_symlink_escape(&root_real, rel_path)
}

/// 把路径参数锚定到 workspace 根：相对路径一律 `root.join(path)`。
///
/// F-5（跨 workspace 误写）：daemon 是多 workspace 常驻进程，进程 CWD 是
/// 启动目录（本机为仓库根），与调用方目标 workspace 无关。此前路径参数
/// 直接交给 `validate_owned_path` / `validate_owned_path_any`，其内部
/// `std::fs::canonicalize` 对相对串按**进程 CWD** 解析，导致隔离 workspace
/// 的 `refresh_file("alpha.py")` / `file.remove("alpha.py")` 等实际命中主
/// workspace 的同名文件（删除/覆写真实仓库文件）。现以 `registry
/// workspace_instance_id → host_real_root` 为基准解析；绝对路径保持原样，
/// 后续 owner + `ensure_root_containment` 继续做根内包含校验（越根仍
/// fail-closed 返回 path_escape）。
fn anchor_path_to_root(root: &Path, path: &str) -> String {
    let raw = Path::new(path);
    if raw.is_absolute() {
        path.to_string()
    } else {
        root.join(raw).to_string_lossy().to_string()
    }
}

/// 校验路径必须在 workspace 根内，返回规范化绝对路径（String，validate_owned_path 契约）。
fn resolve_owned_path(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    workspace_instance_id: &str,
    file_path: &str,
) -> Result<String, DaemonRpcError> {
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let anchored = anchor_path_to_root(&root, file_path);
    let real = validate_owned_path(&anchored, peer.uid, true)?;
    ensure_root_containment(&root, &real)?;
    Ok(real)
}

/// 打开 workspace codegraph DB（写路径，daemon 权威库）。
/// codegraph_db 为 None 时 fail-closed（daemon 未配置 codegraph 模板）。
///
/// P0-CR3 修复：写路径打开前必须先 `storage::initialize_or_migrate(path, SCHEMA_VERSION)`
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
    // fail-fast：入口即确保 schema 与 storage::SCHEMA_VERSION 一致（已就绪时为幂等 no-op）
    crate::storage::initialize_or_migrate(path, crate::storage::SCHEMA_VERSION).map_err(|e| {
        DaemonRpcError::new(
            "codegraph_schema_init_failed",
            format!("codegraph schema 初始化/迁移失败（v{}）: {e}", crate::storage::SCHEMA_VERSION),
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
///
/// 🔴 除 `symbols`/`calls`/`symbol_contents` 外，必须**同步镜像**到版本化
/// schema（`file_versions` + `file_symbol_versions`）：`query.symbol` /
/// `query.symbol_location` 等读面走 `query_symbol_detail`
/// （symbol_query.rs:13，JOIN file_symbol_versions ↔ file_versions ↔
/// file_instances），而非 `symbols` 表。P0-CR1 随行落库此前只写 symbols，
/// 导致这些读面对 daemon 建起来的图恒返回 null（L5b 根因）。
fn parse_and_store_symbols(
    conn: &rusqlite::Connection,
    file_instance_id: i64,
    abs_path: &Path,
    rel: &str,
    content_hash: &str,
    mtime: f64,
    total_lines: i64,
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
            // P3-1：先清无 CASCADE 的符号引用行（coverage/clone/test 映射），
            // 否则下面的 DELETE FROM symbols 会被 FK 约束中止——主仓
            // analyzers/compile_commands.py 这类有覆盖率数据的文件必现。
            purge_symbol_references(conn, &old_ids)?;
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

        // 3.5 镜像到版本化 schema（query.symbol 等读面的数据源，见函数文档）。
        //     幂等重建：先清该文件旧版本行（与步骤 2 的 symbols DELETE 对齐），
        //     再插入一条 is_current=1 的新版本 + 其 file_symbol_versions。
        conn.execute(
            "DELETE FROM file_symbol_versions WHERE file_version_id IN \
             (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
            rusqlite::params![file_instance_id],
        )?;
        // 现场库（旧 DDL 创建）的 call_versions 外键是 NO ACTION，而 canonical
        // schema 里是 ON DELETE CASCADE；为兼容两种库形态，显式先删子行，
        // 否则 DELETE FROM file_versions 会报 FOREIGN KEY constraint failed。
        conn.execute(
            "DELETE FROM call_versions WHERE file_version_id IN \
             (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
            rusqlite::params![file_instance_id],
        )?;
        conn.execute(
            "DELETE FROM file_versions WHERE file_instance_id = ?1",
            rusqlite::params![file_instance_id],
        )?;
        conn.execute(
            "INSERT INTO file_versions \
             (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted) \
             VALUES (?1, 1, ?2, ?3, ?4, ?5, 1, 0)",
            rusqlite::params![file_instance_id, content_hash, mtime, total_lines, now_ts_secs()],
        )?;
        let fv_id = conn.last_insert_rowid();
        for s in &result.symbols {
            conn.execute(
                "INSERT OR IGNORE INTO file_symbol_versions \
                 (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0)",
                rusqlite::params![
                    fv_id,
                    s.symbol_hash,
                    s.qualified_name,
                    s.start_line,
                    s.end_line,
                    if s.module_path.is_empty() { &module_path } else { &s.module_path },
                    s.depth,
                ],
            )?;
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

/// 按调用行 id 批量写回已解析边（分块避免 SQLite 变量数上限）。
fn update_calls_by_ids(
    conn: &rusqlite::Connection,
    ids: &[i64],
    callee_id: i64,
    is_cross_file: i64,
) -> Result<usize, rusqlite::Error> {
    const CHUNK: usize = 500;
    let mut updated = 0usize;
    for chunk in ids.chunks(CHUNK) {
        let ph = chunk.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let mut params: Vec<&dyn rusqlite::ToSql> = Vec::with_capacity(chunk.len() + 2);
        params.push(&callee_id);
        params.push(&is_cross_file);
        for id in chunk {
            params.push(id);
        }
        updated += conn.execute(
            &format!(
                "UPDATE calls SET callee_id = ?1, is_cross_file = ?2 \
                 WHERE id IN ({ph}) AND callee_id = 0"
            ),
            params.as_slice(),
        )?;
    }
    Ok(updated)
}

/// P3-1 增量边重算：按名字集合「调用侧驱动」解析（只处理受影响名字）。
///
/// 逐名 `UPDATE calls ... WHERE callee_name = ? AND caller_id IN (<workspace 符号>)`
/// 的查询计划是 `SEARCH calls USING INDEX idx_calls_caller (caller_id=?)`——
/// 每个名字都要遍历一次 workspace 的**全部** caller 符号（主仓 4.3 万符号，
/// 实测单名 ~95ms，数千名字即分钟级，曾把主仓增量建图拖到 HTTP 超时）。
/// 这里反过来驱动：先按名字集合一次性扫 `calls` 收集目标 raw 边行 id
/// （分块 IN，扫描次数 = 名字数/500），再按 rowid 批量 UPDATE；语句与边数
/// 线性、与全库规模解耦。
///
/// 语义与全量路径保持一致：唯一候选全量解析（同文件边 is_cross_file=0、
/// 跨文件=1），多候选只挂同文件边，零候选计入 ambiguous。
fn resolve_filtered_calls(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    filter: &BTreeSet<String>,
    candidate_stmt: &mut rusqlite::Statement<'_>,
) -> Result<(usize, usize), rusqlite::Error> {
    if filter.is_empty() {
        return Ok((0, 0));
    }
    const CHUNK: usize = 500;
    // name -> [(call_id, caller_file_instance_id)]
    let mut raw: HashMap<String, Vec<(i64, i64)>> = HashMap::new();
    let names: Vec<&String> = filter.iter().collect();
    for chunk in names.chunks(CHUNK) {
        let ph = chunk.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let mut params: Vec<&dyn rusqlite::ToSql> = Vec::with_capacity(chunk.len() + 1);
        params.push(&workspace_id);
        for name in chunk {
            params.push(*name);
        }
        let mut stmt = conn.prepare(&format!(
            "SELECT c.id, c.callee_name, cs.file_instance_id FROM calls c \
             JOIN symbols cs ON cs.id = c.caller_id \
             JOIN file_instances cf ON cf.id = cs.file_instance_id \
             WHERE cf.workspace_id = ?1 AND c.callee_id = 0 AND c.callee_name IN ({ph})"
        ))?;
        let rows = stmt.query_map(params.as_slice(), |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
            ))
        })?;
        for row in rows.filter_map(Result::ok) {
            raw.entry(row.1).or_default().push((row.0, row.2));
        }
    }

    let mut resolved = 0usize;
    let mut ambiguous = 0usize;
    for name in filter {
        let candidates: Vec<(i64, i64)> = candidate_stmt
            .query_map(rusqlite::params![workspace_id, name], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
            })?
            .filter_map(Result::ok)
            .collect();
        let Some(rows) = raw.get(name) else {
            if candidates.is_empty() {
                ambiguous += 1;
            }
            continue;
        };
        match candidates.as_slice() {
            [] => ambiguous += 1,
            [(sid, sfid)] => {
                let same: Vec<i64> = rows
                    .iter()
                    .filter(|(_, cfid)| cfid == sfid)
                    .map(|(id, _)| *id)
                    .collect();
                let cross: Vec<i64> = rows
                    .iter()
                    .filter(|(_, cfid)| cfid != sfid)
                    .map(|(id, _)| *id)
                    .collect();
                resolved += update_calls_by_ids(conn, &same, *sid, 0)?;
                resolved += update_calls_by_ids(conn, &cross, *sid, 1)?;
            }
            _ => {
                // 多候选：仅挂接同文件的（每文件取首个候选 id）
                let mut done_files: Vec<i64> = Vec::new();
                for (sid, sfid) in &candidates {
                    if done_files.contains(sfid) {
                        continue;
                    }
                    done_files.push(*sfid);
                    let ids: Vec<i64> = rows
                        .iter()
                        .filter(|(_, cfid)| cfid == sfid)
                        .map(|(id, _)| *id)
                        .collect();
                    resolved += update_calls_by_ids(conn, &ids, *sid, 0)?;
                }
            }
        }
    }
    Ok((resolved, ambiguous))
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
///
/// P3-1：`names_filter` 提供时只处理给定名字（增量建图的边重算裁剪），
/// `None` 表示全库扫描（build_directory / refresh_file 仍走全量语义）。
fn resolve_raw_calls(
    conn: &rusqlite::Connection,
    workspace_id: i64,
) -> Result<(usize, usize), DaemonRpcError> {
    resolve_raw_calls_for_names(conn, workspace_id, None)
}

fn resolve_raw_calls_for_names(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    names_filter: Option<&BTreeSet<String>>,
) -> Result<(usize, usize), DaemonRpcError> {
    (|| -> Result<(usize, usize), rusqlite::Error> {
        let mut candidate_stmt = conn.prepare(
            "SELECT s.id, s.file_instance_id FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.name = ?2",
        )?;

        // P3-1 增量路径：按名字集合走「调用侧驱动」（见 resolve_filtered_calls）。
        if let Some(filter) = names_filter {
            return resolve_filtered_calls(conn, workspace_id, filter, &mut candidate_stmt);
        }

        let names: Vec<String> = {
            let mut stmt = conn.prepare(
                "SELECT DISTINCT c.callee_name FROM calls c \
                 JOIN symbols cs ON c.caller_id = cs.id \
                 JOIN file_instances cf ON cs.file_instance_id = cf.id \
                 WHERE cf.workspace_id = ?1 AND c.callee_id = 0 AND c.callee_name != ''",
            )?;
            let rows = stmt.query_map(rusqlite::params![workspace_id], |r| r.get::<_, String>(0))?;
            let collected: Vec<String> = rows.filter_map(Result::ok).collect();
            collected
        };

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
                    // P3-1：UPDATE 必须限定到本 workspace 的 caller 符号。
                    // 此前只按 callee_name 全库 UPDATE，会把手写其它 workspace 的
                    // raw 边挂到本 workspace 的符号 id 上（跨工作区误挂 + 悬空
                    // 引用），且要求全表扫描全部 calls。
                    let n = conn.execute(
                        "UPDATE calls SET callee_id = ?1, \
                         is_cross_file = CASE WHEN EXISTS ( \
                           SELECT 1 FROM symbols cs WHERE cs.id = calls.caller_id \
                             AND cs.file_instance_id = ?2 \
                         ) THEN 0 ELSE 1 END \
                         WHERE callee_id = 0 AND callee_name = ?3 \
                           AND caller_id IN ( \
                             SELECT cs.id FROM symbols cs \
                             JOIN file_instances cfi ON cs.file_instance_id = cfi.id \
                             WHERE cfi.workspace_id = ?4 \
                           )",
                        rusqlite::params![sid, sfid, name, workspace_id],
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

/// build_graph 单文件落库归类（P3-3 契约：`unchanged` = 可解析且内容未变化）。
#[derive(Debug, PartialEq, Eq)]
enum BuildGraphFileClass {
    /// 首次入库或内容变化 → 重新解析并计入 `inserted`
    Inserted,
    /// 可解析、内容 hash 与库内一致且已解析过 → 计入 `unchanged`
    Unchanged,
    /// 语言无 parser（vue/svelte/cxx 等）→ 计入 `skipped_unparsed`
    SkippedUnparsed,
}

fn classify_build_graph_file(
    parsed: bool,
    stored: Option<(&str, &str)>,
    hash: &str,
) -> BuildGraphFileClass {
    if !parsed {
        return BuildGraphFileClass::SkippedUnparsed;
    }
    match stored {
        Some((stored_hash, status)) if stored_hash == hash && status == "parsed" => {
            BuildGraphFileClass::Unchanged
        }
        _ => BuildGraphFileClass::Inserted,
    }
}

/// P3-1：mtime 快路径命中判定（命中即免文件读取 / sha256 / 解析）。
///
/// 共享 schema（`db/schema.py` 的 `file_instances`）没有 size 列，本步骤不改
/// schema（版本号与 Python 端 DDL 均为外部契约），因此缓存键是
/// 「库内 status='parsed' + 内容 hash 非空 + mtime 与磁盘一致」：
/// - mtime 为 f64 秒（NTFS 精度 100ns），任何写入都会刷新它；
/// - 一旦不等就回落 sha256 全量校验，正确性仍以内容 hash 为准（不是 mtime）；
/// - 同秒内改写且 mtime 恰好不变属极端情形，交由全量重建（`build_graph`
///   单文件 `refresh_file`）兜底。
fn is_mtime_cache_hit(
    stored_status: &str,
    stored_hash: &str,
    stored_mtime: f64,
    disk_mtime: f64,
) -> bool {
    stored_status == "parsed"
        && !stored_hash.is_empty()
        // 0.5ms 容差：吸收 f64 秒与文件系统时间戳的量化误差
        && (stored_mtime - disk_mtime).abs() < 0.0005
}

/// 清理指向给定符号 id 的无 CASCADE 引用行。
///
/// `symbols` 重建（parse_and_store_symbols 的幂等 DELETE）或整文件删除前必须
/// 调用，否则 SQLite 直接报 `FOREIGN KEY constraint failed`
/// （`clone_pairs` / `test_case_relations` / `test_runs` / `coverage_data`
/// 对 `symbols(id)` 都是 RESTRICT 语义）。
fn purge_symbol_references(
    conn: &rusqlite::Connection,
    ids: &[i64],
) -> Result<(), rusqlite::Error> {
    if ids.is_empty() {
        return Ok(());
    }
    let ph = ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let once: Vec<&dyn rusqlite::ToSql> =
        ids.iter().map(|id| id as &dyn rusqlite::ToSql).collect();
    let mut twice: Vec<&dyn rusqlite::ToSql> = Vec::with_capacity(once.len() * 2);
    twice.extend(once.iter().copied());
    twice.extend(once.iter().copied());
    conn.execute(
        &format!("DELETE FROM clone_pairs WHERE symbol_a_id IN ({ph}) OR symbol_b_id IN ({ph})"),
        twice.as_slice(),
    )?;
    conn.execute(
        &format!(
            "DELETE FROM test_case_relations WHERE test_fn_id IN ({ph}) OR tested_fn_id IN ({ph})"
        ),
        twice.as_slice(),
    )?;
    conn.execute(
        &format!("DELETE FROM test_runs WHERE test_fn_id IN ({ph})"),
        once.as_slice(),
    )?;
    conn.execute(
        &format!("DELETE FROM coverage_data WHERE symbol_id IN ({ph})"),
        once.as_slice(),
    )?;
    Ok(())
}

/// P3-1 删除感知：清理磁盘上已消失文件的所有图行。
///
/// 返回「受影响的 callee 名」集合（旧定义名 + 其它文件指向这些符号的已解析
/// 边的名字），供调用方做增量边重算；清理本身按 FK 依赖序进行
/// （与 `file.remove` 对齐，另补 `clone_pairs` / `test_case_relations` /
/// `test_runs` / `coverage_data` / `file_ownership` 这些无 CASCADE 的引用表，
/// 否则删 `symbols` / `file_instances` 会被 FK 约束中止）。
fn purge_missing_file_graph(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    rel: &str,
) -> Result<Vec<String>, DaemonRpcError> {
    let map_err = |e: rusqlite::Error| DaemonRpcError::internal_error(format!("删除感知清理失败: {e}"));
    let fid: Option<i64> = conn
        .query_row(
            "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2",
            rusqlite::params![workspace_id, rel],
            |r| r.get::<_, i64>(0),
        )
        .optional()
        .map_err(map_err)?;
    let Some(fid) = fid else {
        return Ok(Vec::new());
    };

    let affected = purge_file_graph_rows(conn, fid)?;
    for sql in [
        // 现场库的 archived_files / git_file_changes 外键同为 NO ACTION
        "DELETE FROM archived_files WHERE file_instance_id = ?1",
        "DELETE FROM file_instances WHERE id = ?1",
    ] {
        conn.execute(sql, rusqlite::params![fid]).map_err(map_err)?;
    }
    Ok(affected)
}

/// 删除单个文件的图行（symbols/calls/file_versions 等），保留
/// `file_instances` 行本身。返回「受影响的 callee 名」集合。
///
/// 归档路径（[`archive_ignored_file_graph`]）必须保留 file_instances 行：
/// `archived_files.file_instance_id` 是 NO ACTION 外键，行删除后归档记录无法
/// 写入；C1 复用本函数以对齐 `purge_missing_file_graph` 的级联顺序。
fn purge_file_graph_rows(
    conn: &rusqlite::Connection,
    fid: i64,
) -> Result<Vec<String>, DaemonRpcError> {
    let map_err = |e: rusqlite::Error| DaemonRpcError::internal_error(format!("图行清理失败: {e}"));
    let mut affected: Vec<String> = Vec::new();
    let symbol_ids: Vec<i64> = {
        let mut stmt = conn
            .prepare("SELECT id, name FROM symbols WHERE file_instance_id = ?1")
            .map_err(map_err)?;
        let rows = stmt
            .query_map(rusqlite::params![fid], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(map_err)?
            .filter_map(Result::ok)
            .collect::<Vec<(i64, String)>>();
        for (_, name) in &rows {
            affected.push(name.clone());
        }
        rows.into_iter().map(|(id, _)| id).collect()
    };
    if !symbol_ids.is_empty() {
        let ph = symbol_ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let params: Vec<&dyn rusqlite::ToSql> =
            symbol_ids.iter().map(|id| id as &dyn rusqlite::ToSql).collect();
        // 其它文件指向本文件符号的已解析边先降级为 raw（随后也被删除，
        // 但保持「不留悬空 callee_id」的不变量）
        let mut stmt = conn
            .prepare(&format!(
                "SELECT DISTINCT callee_name FROM calls \
                 WHERE callee_id IN ({ph}) AND callee_name != ''"
            ))
            .map_err(map_err)?;
        let names = stmt
            .query_map(params.as_slice(), |r| r.get::<_, String>(0))
            .map_err(map_err)?
            .filter_map(Result::ok)
            .collect::<Vec<String>>();
        affected.extend(names);
        conn.execute(
            &format!("UPDATE calls SET callee_id = 0 WHERE callee_id IN ({ph})"),
            params.as_slice(),
        )
        .map_err(map_err)?;
        purge_symbol_references(conn, &symbol_ids).map_err(map_err)?;
    }
    for sql in [
        "DELETE FROM calls WHERE caller_id IN \
         (SELECT id FROM symbols WHERE file_instance_id = ?1)",
        "DELETE FROM file_symbol_versions WHERE file_version_id IN \
         (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
        "DELETE FROM call_versions WHERE file_version_id IN \
         (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
        "DELETE FROM coverage_data WHERE file_instance_id = ?1",
        "DELETE FROM file_ownership WHERE file_instance_id = ?1",
        "DELETE FROM semgrep_findings WHERE file_instance_id = ?1",
        "DELETE FROM git_file_changes WHERE file_instance_id = ?1",
        "DELETE FROM file_versions WHERE file_instance_id = ?1",
        "DELETE FROM symbols WHERE file_instance_id = ?1",
    ] {
        conn.execute(sql, rusqlite::params![fid]).map_err(map_err)?;
    }
    Ok(affected)
}

/// C1：ignore 契约归档 —— 已入库但当前被剪枝命中的文件行。
///
/// `handle_build_graph` 的删除感知只清理「磁盘上已消失」的行；被
/// `.gitignore`/`.callwardenignore` 剪枝、但文件仍存在于磁盘的旧行（如重构前
/// 误入库的 `build/lib.win-amd64-cpython-314/` 副本）永远不会离开符号图，
/// 导致源码与构建副本重复符号污染 `get_callers`/孤儿符号/统计。
///
/// 这里迁 `archived_files` + 级联清理图行，保留 `file_instances` 行并置
/// `status='archived'`（与 `gc_archive_core` 的归档语义一致）。幂等：已有
/// 归档记录或状态已是 archived 时直接跳过（`archived` 计数不增加）。
///
/// 返回 `None` 表示该 rel_path 无库内行；`Some((是否新归档, 受影响名字))`。
fn archive_ignored_file_graph(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    rel: &str,
    reason: &str,
    now: f64,
) -> Result<Option<(bool, Vec<String>)>, DaemonRpcError> {
    let map_err = |e: rusqlite::Error| DaemonRpcError::internal_error(format!("ignore 归档失败: {e}"));
    let row: Option<(i64, String, String, String)> = conn
        .query_row(
            "SELECT id, abs_path, current_content_hash, status FROM file_instances
             WHERE workspace_id = ?1 AND rel_path = ?2",
            rusqlite::params![workspace_id, rel],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()
        .map_err(map_err)?;
    let Some((fid, abs_path, content_hash, status)) = row else {
        return Ok(None);
    };
    if status == "archived" {
        return Ok(Some((false, Vec::new())));
    }
    let already_archived: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM archived_files WHERE file_instance_id = ?1)",
            rusqlite::params![fid],
            |r| r.get(0),
        )
        .map_err(map_err)?;
    if already_archived {
        return Ok(Some((false, Vec::new())));
    }
    let symbol_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols WHERE file_instance_id = ?1",
            rusqlite::params![fid],
            |r| r.get(0),
        )
        .map_err(map_err)?;
    let call_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM calls WHERE caller_id IN
             (SELECT id FROM symbols WHERE file_instance_id = ?1)",
            rusqlite::params![fid],
            |r| r.get(0),
        )
        .map_err(map_err)?;
    conn.execute(
        "INSERT INTO archived_files (file_instance_id, workspace_id, rel_path, abs_path,
            content_hash, symbol_count, call_count, archive_reason, archived_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        rusqlite::params![
            fid,
            workspace_id,
            rel,
            abs_path,
            content_hash,
            symbol_count,
            call_count,
            reason,
            now
        ],
    )
    .map_err(map_err)?;
    let affected = purge_file_graph_rows(conn, fid)?;
    conn.execute(
        "UPDATE file_instances SET status = 'archived' WHERE id = ?1",
        rusqlite::params![fid],
    )
    .map_err(map_err)?;
    Ok(Some((true, affected)))
}

/// 返回 rel_path 命中的剪枝目录名（只看目录段；剪枝集合来自
/// [`load_ignore_dir_names`]，即 `.gitignore`/`.callwardenignore` 契约与硬编码
/// SKIP_DIRS。**不包含** `scan_files` 的隐藏目录启发式——`.github/` 这类目录
/// 不在 ignore 契约内，Python 管线会正常索引，不能在此归档误伤）。
fn ignored_dir_of(
    rel: &str,
    skip: &std::collections::HashSet<String>,
) -> Option<String> {
    let norm = rel.replace('\\', "/");
    let mut segs: Vec<&str> = norm.split('/').collect();
    segs.pop();
    segs.into_iter()
        .find(|seg| skip.contains(*seg))
        .map(|seg| seg.to_string())
}

/// 收集单个文件的符号名（供增量边重算的受影响名字集合）。
fn collect_file_symbol_names(
    conn: &rusqlite::Connection,
    fid: i64,
    out: &mut BTreeSet<String>,
) -> Result<(), DaemonRpcError> {
    let mut stmt = conn
        .prepare("SELECT name FROM symbols WHERE file_instance_id = ?1")
        .map_err(|e| DaemonRpcError::internal_error(format!("收集符号名失败: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![fid], |r| r.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("收集符号名失败: {e}")))?;
    for name in rows.filter_map(Result::ok) {
        out.insert(name);
    }
    Ok(())
}

/// 收集单个文件本轮产生的 raw 调用边 callee 名（供增量边重算）。
fn collect_file_raw_callee_names(
    conn: &rusqlite::Connection,
    fid: i64,
    out: &mut BTreeSet<String>,
) -> Result<(), DaemonRpcError> {
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT c.callee_name FROM calls c \
             JOIN symbols s ON c.caller_id = s.id \
             WHERE s.file_instance_id = ?1 AND c.callee_id = 0 AND c.callee_name != ''",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("收集 callee 名失败: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params![fid], |r| r.get::<_, String>(0))
        .map_err(|e| DaemonRpcError::internal_error(format!("收集 callee 名失败: {e}")))?;
    for name in rows.filter_map(Result::ok) {
        out.insert(name);
    }
    Ok(())
}

/// `workspace.build_graph` —— 增量重建文件索引（file_instances upsert）。
///
/// P3-1 增量语义：
/// - 快路径：`file_instances.mtime` 与磁盘一致且状态已解析 → 跳过读取/hash/解析
///   （计 `skipped_cached`，同时计入 `unchanged`）；
/// - 内容未变但 mtime 变化（hash 一致）→ 只刷新 file_instances，不重解析符号；
/// - 删除感知：磁盘上已消失的文件同步清理 symbols/calls/file_versions 等
///   （计 `deleted`）；
/// - 边重算裁剪：只对本轮受影响的名字（改动文件的旧/新符号名 + raw callee 名
///   + 删除文件波及的名字）重解析，不再全库扫描 raw 边。
///
/// 计数契约（P3-1 在 P3-3 基础上新增两项）：
/// - `scanned` 本轮访问的可索引文件数；
/// - `inserted` 首次入库或内容变化、已重新解析的文件数；
/// - `unchanged` 可解析且内容未变化的文件数（含快路径命中）；
/// - `skipped_cached` 其中走 mtime 快路径、未读取文件内容的文件数；
/// - `deleted` 磁盘上已消失、本轮清理掉图行的文件数；
/// - `archived` 被 ignore 契约目录剪枝命中但仍存在于磁盘、本轮归档的存量行数
///   （幂等：已归档行不重复计数）；
/// - `skipped_unparsed` 语言无 parser 的文件数；
/// - `symbols` / `calls` 本轮解析产出的符号/调用总数；
/// - `calls_resolved` / `calls_ambiguous` 本轮受影响名字的边重算结果
///   （无改动时恒为 0，不再全库重算）。
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
            // F-5：相对 scan_root 以 workspace 根为基准，不用进程 CWD
            let anchored = anchor_path_to_root(&root, &s);
            PathBuf::from(validate_owned_path_any(&anchored, peer.uid)?)
        }
        _ => root.clone(),
    };
    // TP-11A D3：显式 scan_root 必须限制在 workspace 根内
    // （owner 校验不构成根限制；越根扫描会把本用户任意目录索引进库）
    let scan_root_str = scan_root.to_string_lossy().to_string();
    ensure_root_containment(&root, &scan_root_str)?;
    // rel 计算基于 canonical root（client_view_root 可能与真实路径大小写/前缀不一致）
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    // ignore 契约剪枝（.callwardenignore/.gitignore，workspace 根语义）
    let skip = load_ignore_dir_names(&root);
    let files = scan_files(&scan_root, true, &skip);
    let mut scanned = 0usize;
    let mut inserted = 0usize;
    let mut unchanged = 0usize;
    let mut skipped_unparsed = 0usize;
    let mut skipped_cached = 0usize;
    let mut deleted = 0usize;
    // C1：ignore 契约目录命中但仍在磁盘的存量行归档数
    let mut archived = 0usize;
    let mut symbol_total = 0usize;
    let mut call_total = 0usize;
    let now = now_ts_secs();
    // P3-1：本轮磁盘上现存的可索引文件（删除感知差集基准）
    let mut seen: HashSet<String> = HashSet::new();
    // P3-1：本轮受影响的名字（边重算裁剪输入）
    let mut affected_names: BTreeSet<String> = BTreeSet::new();
    for path in files {
        let rel = path
            .strip_prefix(&root_canon)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        scanned += 1;
        seen.insert(rel.clone());
        // 先取元数据（cheap）：快路径命中时避免读取文件内容
        let meta = std::fs::metadata(&path).ok();
        let mtime = meta
            .as_ref()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(now);
        let parsed = parser_lang_id(&rel).is_some();
        let stored: Option<(String, String, f64)> = conn
            .query_row(
                "SELECT current_content_hash, status, mtime FROM file_instances
                 WHERE workspace_id = ?1 AND rel_path = ?2",
                rusqlite::params![workspace_id, rel],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, f64>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("file_instances 回查失败: {e}")))?;
        // P3-1 快路径：mtime 未变且已解析 → 免读 / 免 hash / 免解析
        if parsed {
            if let Some((stored_hash, status, stored_mtime)) = &stored {
                if is_mtime_cache_hit(status, stored_hash, *stored_mtime, mtime) {
                    unchanged += 1;
                    skipped_cached += 1;
                    continue;
                }
            }
        }
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let hash = sha256_hex(&bytes);
        let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
        // P3-3：`unchanged` 的语义收敛为「可解析且内容未变化」。此前它统计的是
        // 「不可解析」文件（vue/svelte/cxx 等无 parser 的扩展名），首次全建就会
        // 返回 unchanged>0，与 Python 契约（unchanged = 未变化文件）相反。
        // 不可解析文件改计入新的 skipped_unparsed，文件清单仍照常登记。
        let class = classify_build_graph_file(
            parsed,
            stored.as_ref().map(|(h, st, _)| (h.as_str(), st.as_str())),
            &hash,
        );
        // file_contents upsert
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, infer_language(&rel), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
        // 先 upsert file_instances 拿真实 id（语言可解析时标记 parsed），再做符号落库
        let fid = upsert_file_instance(
            &conn, workspace_id, &rel, &path, &hash, mtime, total_lines, parsed,
        )?;
        if parsed && class == BuildGraphFileClass::Inserted {
            // P3-1：重解析前先记录旧符号名——parse_and_store_symbols 内部会把
            // 其它文件指向这些旧 id 的已解析边降级为 raw，这些名字必须参与重算。
            collect_file_symbol_names(&conn, fid, &mut affected_names)?;
            // P0-CR1：符号抽取随建图落库（tree-sitter 管线）
            let parse_result = parse_and_store_symbols(
                &conn, fid, &path, &rel, &hash, mtime, total_lines,
            );
            match parse_result {
                Ok((sym, calls)) => {
                    symbol_total += sym;
                    call_total += calls;
                }
                Err(e) => {
                    // P3-1：解析失败时 file_instances 已被 upsert 成 parsed。
                    // 若不回退，下一轮的 mtime 快路径会把这行当成「已解析」永久
                    // 跳过（旧实现每次全量重解析天然自愈，增量化后必须显式补偿）。
                    let _ = conn.execute(
                        "UPDATE file_instances SET status = 'pending', last_parsed = 0 \
                         WHERE id = ?1",
                        rusqlite::params![fid],
                    );
                    return Err(e);
                }
            }
            // 新定义名（其它文件的 raw 边可能因此可解析）+ 本文件新 raw 边名
            collect_file_symbol_names(&conn, fid, &mut affected_names)?;
            collect_file_raw_callee_names(&conn, fid, &mut affected_names)?;
        }
        match class {
            BuildGraphFileClass::Inserted => inserted += 1,
            BuildGraphFileClass::Unchanged => unchanged += 1,
            BuildGraphFileClass::SkippedUnparsed => skipped_unparsed += 1,
        }
    }
    // P3-1 删除感知：库里存在、磁盘上已消失的文件清理图行。
    // 以「磁盘上是否真的不存在」为准（而非「本轮是否扫描到」），这样
    // scan_root 只扫子目录时不会误删目录外文件，也不会误删被 ignore
    // 规则排除但仍存在于磁盘的文件。
    let stored_rows: Vec<(String, String)> = {
        let mut stmt = conn
            .prepare("SELECT rel_path, abs_path FROM file_instances WHERE workspace_id = ?1")
            .map_err(|e| DaemonRpcError::internal_error(format!("file_instances 列表失败: {e}")))?;
        let rows = stmt
            .query_map(rusqlite::params![workspace_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("file_instances 列表失败: {e}")))?;
        rows.filter_map(Result::ok).collect()
    };
    for (rel, abs_path) in stored_rows {
        if seen.contains(&rel) {
            continue;
        }
        let exists_on_disk = root_canon.join(&rel).exists() || Path::new(&abs_path).exists();
        // C1：被剪枝命中但文件仍在磁盘 → 归档（幂等），否则历史行永久残留
        if let Some(dir) = ignored_dir_of(&rel, &skip) {
            if exists_on_disk {
                let reason = format!("ignored path: {dir}");
                if let Some((newly, names)) =
                    archive_ignored_file_graph(&conn, workspace_id, &rel, &reason, now)?
                {
                    for name in names {
                        affected_names.insert(name);
                    }
                    if newly {
                        archived += 1;
                    }
                    continue;
                }
            }
        }
        if exists_on_disk {
            continue;
        }
        let names = purge_missing_file_graph(&conn, workspace_id, &rel)?;
        for name in names {
            affected_names.insert(name);
        }
        deleted += 1;
    }
    // P3-1 边重算裁剪：只重算本轮受影响的 callee 名（无改动则零成本）。
    // 全覆盖性论证：会改变解析结果的只有「名字被新增/删除定义」或
    // 「该名字的 raw 边被新建/降级」，两类名字都已收进 affected_names。
    let (resolved, ambiguous) = if affected_names.is_empty() {
        (0usize, 0usize)
    } else {
        resolve_raw_calls_for_names(&conn, workspace_id, Some(&affected_names))?
    };
    let mut m = Map::new();
    m.insert("ok".into(), Value::Bool(true));
    m.insert("scanned".into(), Value::Number(scanned.into()));
    m.insert("inserted".into(), Value::Number(inserted.into()));
    m.insert("unchanged".into(), Value::Number(unchanged.into()));
    m.insert("skipped_cached".into(), Value::Number(skipped_cached.into()));
    m.insert("deleted".into(), Value::Number(deleted.into()));
    m.insert("archived".into(), Value::Number(archived.into()));
    m.insert("skipped_unparsed".into(), Value::Number(skipped_unparsed.into()));
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
    // TP-11A D3：叠加根目录限制（与 file.read 同款 path_escape 契约），
    // 阻断越根目录递归扫描与索引
    // F-5：相对 dir_path 以 workspace 根为基准，不用进程 CWD
    let anchored_dir = anchor_path_to_root(&root, dir_path);
    let real_dir = PathBuf::from(validate_owned_path_any(&anchored_dir, peer.uid)?);
    ensure_root_containment(&root, &real_dir.to_string_lossy())?;
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
            let (sym, calls) = parse_and_store_symbols(
                &conn, fid, &path, &rel, &hash, mtime, total_lines,
            )?;
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
    // P0-CR2 同源修复：base 是目录作用域（缺省即 workspace 根，也是目录），
    // 此前误用 require_file=true 导致传子目录一律报 `path_not_found: 不是文件`，
    // file.grep 的 path 剪枝参数从未真正可用。改 require_file=false（要目录）。
    // 根内校验第二层修复：`real` 是 canonicalize 后的（Windows 带 \\?\ 前缀、
    // 反斜杠），与未规范化的 `root_str`（正斜杠）字符串比较恒不匹配 → path_escape
    // 误拒。改用 Path 组件级比较，root 同样 canonicalize（与 build_directory 同式）。
    // 第四层：默认分支（无 path 参数）也必须用 root_canon —— scan_files(base)
    // 产出的路径与 strip_prefix 参照必须同为规范路径。
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            // F-5：相对 path 以 workspace 根为基准，不用进程 CWD
            let anchored = anchor_path_to_root(&root_canon, &p);
            let real = PathBuf::from(validate_owned_path(&anchored, peer.uid, false)?);
            if !real.starts_with(&root_canon) {
                return Err(DaemonRpcError::new(
                    "path_escape",
                    "path 不在 workspace 根内".to_string(),
                ));
            }
            real
        }
        _ => root_canon.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    let head_limit = get_int_param_or(params, "head_limit", 50).max(1) as usize;
    let skip = load_ignore_dir_names(&root);
    // 第三层修复：files 来自 canonicalize 后的 base，用未规范化的 root 做
    // strip_prefix 必失败 → fallback 输出绝对路径（file_path 字段返回绝对路径，
    // 违反契约且让 glob 相对匹配失效）。
    let files = scan_files(&base, true, &skip);
    let needle = pattern.to_lowercase();
    let mut matches: Vec<Value> = Vec::new();
    for path in files {
        if matches.len() >= head_limit {
            break;
        }
        let rel = path
            .strip_prefix(&root_canon)
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
    // P0-CR2 同源修复：base 是目录作用域（缺省即 workspace 根，也是目录），
    // 此前误用 require_file=true 导致传子目录一律报 `path_not_found: 不是文件`，
    // file.list 的 path 剪枝参数从未真正可用。改 require_file=false（要目录）。
    // 根内校验第二层修复：canonicalize 后的 real 与未规范化的 root_str 字符串比较
    // 恒不匹配（Windows \\?\ 前缀 + 斜杠方向）→ path_escape 误拒。改 Path 组件级
    // 比较，root 同样 canonicalize（与 build_directory 同式）。
    // 第四层：默认分支（无 path 参数）也必须用 root_canon —— entry.path() 来自
    // read_dir(base)，base 与 strip_prefix 参照必须同为规范路径，否则 fallback
    // 输出绝对路径（且 Windows 上会丢成全小写）。
    let root_canon = std::fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            // F-5：相对 path 以 workspace 根为基准，不用进程 CWD
            let anchored = anchor_path_to_root(&root_canon, &p);
            let real = PathBuf::from(validate_owned_path(&anchored, peer.uid, false)?);
            if !real.starts_with(&root_canon) {
                return Err(DaemonRpcError::new("path_escape", "path 不在 workspace 根内".to_string()));
            }
            real
        }
        _ => root_canon.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    // 第三层修复：base 是 canonicalize 后的路径，用未规范化的 root 做 strip_prefix
    // 必失败 → fallback 输出绝对路径（rel_path 字段返回绝对路径，违反契约且让
    // glob 相对匹配失效）。
    let mut rows: Vec<Value> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&base) {
        for entry in entries.flatten() {
            let path = entry.path();
            let rel = path
                .strip_prefix(&root_canon)
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
    // F-5：相对 file_path 以 workspace 根为基准（此前按 daemon 进程 CWD 解析，
    // 隔离 workspace 的 remove("alpha.py") 会删掉主 workspace 的同名真实文件）
    let anchored = anchor_path_to_root(&_root, file_path);
    let real = validate_owned_path(&anchored, peer.uid, true)?;
    // TP-11A D1：owner 校验之外必须叠加根目录限制，否则任意本用户文件
    // 可被越根删除（file.read 同款 path_escape 契约）
    ensure_root_containment(&_root, &real)?;
    std::fs::remove_file(Path::new(&real)).map_err(|e| {
        DaemonRpcError::new("file_remove_failed", format!("删除 {real} 失败: {e}"))
    })?;
    if let Some(db) = codegraph_db {
        if let Ok(conn) = rusqlite::Connection::open(db) {
            let workspace_id = ensure_workspace_row_by_root(&conn, _root.as_path()).unwrap_or(registry_ws_id);
            let rel = real.replace('\\', "/");
            // TP-11A D5（S7 实证）：file_instances 被 symbols/file_versions/
            // semgrep_findings 以 FK 引用（daemon 开 foreign_keys=ON），此前
            // 单条 DELETE 恒被 FK 约束中止且 `let _ =` 吞错 → 被删文件的
            // 索引行（含 L5b 版本化镜像）全部残留。按依赖序先清子表：
            // calls→symbols→file_symbol_versions/call_versions→file_versions
            // →semgrep_findings，最后清 file_instances 本身。
            let ids: Vec<i64> = match conn
                .prepare(
                    "SELECT id FROM file_instances \
                     WHERE workspace_id = ?1 AND (rel_path = ?2 OR abs_path = ?3)",
                ) {
                Ok(mut st) => match st.query_map(
                    rusqlite::params![workspace_id, rel, real],
                    |r| r.get::<_, i64>(0),
                ) {
                    Ok(it) => it.filter_map(|v| v.ok()).collect(),
                    Err(_) => Vec::new(),
                },
                Err(_) => Vec::new(),
            };
            for id in &ids {
                let _ = conn.execute(
                    "DELETE FROM calls WHERE caller_id IN \
                     (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                    rusqlite::params![id],
                );
                let _ = conn.execute(
                    "DELETE FROM file_symbol_versions WHERE file_version_id IN \
                     (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
                    rusqlite::params![id],
                );
                let _ = conn.execute(
                    "DELETE FROM call_versions WHERE file_version_id IN \
                     (SELECT id FROM file_versions WHERE file_instance_id = ?1)",
                    rusqlite::params![id],
                );
                let _ = conn.execute(
                    "DELETE FROM file_versions WHERE file_instance_id = ?1",
                    rusqlite::params![id],
                );
                let _ = conn.execute(
                    "DELETE FROM symbols WHERE file_instance_id = ?1",
                    rusqlite::params![id],
                );
                let _ = conn.execute(
                    "DELETE FROM semgrep_findings WHERE file_instance_id = ?1",
                    rusqlite::params![id],
                );
            }
            let _ = conn.execute(
                "DELETE FROM file_instances WHERE workspace_id = ?1 AND (rel_path = ?2 OR abs_path = ?3)",
                rusqlite::params![workspace_id, rel, real],
            );
            // TP-11A D2：destructive_operations 真实列无 target_path
            // （表为 git 系破坏性操作列：local_ref/local_sha/remote_ref/
            // remote_sha/commit_hash/task_id/blocked/message/created_at）；
            // 此前 INSERT 引用不存在的列恒失败且被 `let _ =` 吞错，
            // file.remove 的审计行从未写入。被删路径落 `message`（人类可读描述列）。
            let _ = conn.execute(
                "INSERT INTO destructive_operations (workspace_id, operation_type, message, created_at)
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
    // TP-11A D3：单文件刷新同样必须限制在 workspace 根内
    // （此前仅 owner 校验 → 越根文件内容被读取并落库符号）
    // F-5：相对 file_path 以 workspace 根为基准，不用进程 CWD
    let anchored = anchor_path_to_root(&root, file_path);
    let real = PathBuf::from(validate_owned_path(&anchored, peer.uid, true)?);
    ensure_root_containment(&root, &real.to_string_lossy())?;
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
        parse_and_store_symbols(&conn, fid, &real, &rel, &hash, mtime, total_lines)?
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
    // TP-11A D4：此前 validate_owned_path(require_file=false) 实为"必须是目录"
    // → 文件恒报"不是目录"（文档合同是文件健康检查，且 exists=false 优雅
    // 分支因 canonicalize 先行失败成为死代码）。改 any 变体（存在即可）+
    // 根目录限制：根内文件按合同返回 exists/size/mtime/readable，越根
    // 一律 path_escape（此前越根目录会泄漏 exists/size/mtime）。
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    // F-5：相对 file_path 以 workspace 根为基准，不用进程 CWD
    let anchored = anchor_path_to_root(&root, file_path);
    let real = validate_owned_path_any(&anchored, peer.uid)?;
    ensure_root_containment(&root, &real)?;
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

// ---------------------------------------------------------------------------
// S3（ADR-002 §8.2）：远端写治理 write.grant / write.commit
//
// 不变量：
// - grant 绑定 principal / workspace / task / step / path / base_hash / 过期；
// - 同 (workspace_id, path) 逻辑租约（部分唯一索引，默认拒绝 E_WRITE_LOCKED）；
// - commit 复核 grant 未过期/未消费/principal 匹配/base_hash 仍一致；
// - 拒绝时审计留痕且不更新索引；成功时审计、索引、grant 消费同事务提交；
// - 审计事件写 write_audit_events 并同步进 audit_chain（remote_write_events）。
// ---------------------------------------------------------------------------

/// 与 `db/db_base.py::WRITE_GOVERNANCE_SCHEMA_STATEMENTS` 保持一致。
/// daemon 在每次写连接上幂等补齐，保证旧库/未跑 Python 初始化的库也可用。
pub fn ensure_write_governance_tables(
    conn: &rusqlite::Connection,
) -> Result<(), DaemonRpcError> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS write_grants (
            grant_id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            db_workspace_id INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL,
            task_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            base_hash TEXT NOT NULL,
            intent TEXT NOT NULL DEFAULT '',
            allowed_paths_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','consumed','expired','revoked')),
            consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at REAL NOT NULL DEFAULT 0,
            new_hash TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_write_grants_active_path
            ON write_grants(workspace_id, path)
            WHERE status = 'active' AND consumed = 0;
        CREATE INDEX IF NOT EXISTS idx_write_grants_principal
            ON write_grants(workspace_id, principal_id, status);
        CREATE INDEX IF NOT EXISTS idx_write_grants_task
            ON write_grants(workspace_id, task_id, step_id);
        CREATE INDEX IF NOT EXISTS idx_write_grants_expires
            ON write_grants(workspace_id, expires_at);
        CREATE TABLE IF NOT EXISTS write_audit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            db_workspace_id INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL CHECK(event_type IN ('grant','commit','reject')),
            outcome TEXT NOT NULL CHECK(outcome IN ('allow','deny')),
            grant_id TEXT NOT NULL DEFAULT '',
            principal_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            step_id TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            base_hash TEXT NOT NULL DEFAULT '',
            new_hash TEXT NOT NULL DEFAULT '',
            reason_code TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            audit_record_id TEXT NOT NULL DEFAULT '',
            recorded_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_write_audit_events_grant
            ON write_audit_events(workspace_id, grant_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_write_audit_events_path
            ON write_audit_events(workspace_id, path, event_id);
        CREATE INDEX IF NOT EXISTS idx_write_audit_events_type
            ON write_audit_events(workspace_id, event_type, event_id);
        CREATE TRIGGER IF NOT EXISTS trg_write_audit_events_no_delete
        BEFORE DELETE ON write_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'write_audit_events is append-only');
        END;
        "#,
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("write governance schema 初始化失败: {e}")))?;
    // review F3：write_grants/write_audit_events 的 workspace_id 是 registry 自增 id，
    // 与 file_instances 使用的 codegraph workspace id 不同空间；db_workspace_id 记录
    // 后者作为索引关联键。旧库用 ADD COLUMN 增量补齐（幂等）。
    for (table, column) in [
        ("write_grants", "db_workspace_id"),
        ("write_audit_events", "db_workspace_id"),
    ] {
        let exists = {
            let mut stmt = conn
                .prepare(&format!("PRAGMA table_info({table})"))
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 {table} schema 失败: {e}")))?;
            let mut rows = stmt
                .query([])
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 {table} 列失败: {e}")))?;
            let mut found = false;
            while let Some(row) = rows
                .next()
                .map_err(|e| DaemonRpcError::internal_error(format!("读取 {table} 列失败: {e}")))?
            {
                let name: String = row
                    .get(1)
                    .map_err(|e| DaemonRpcError::internal_error(format!("读取 {table} 列失败: {e}")))?;
                if name == column {
                    found = true;
                    break;
                }
            }
            found
        };
        if !exists {
            conn.execute(
                &format!("ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"),
                [],
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("{table}.{column} 增量补齐失败: {e}"))
            })?;
        }
    }
    // review F5：`audit_record_id` 是写入后一次性回填的链回链列，必须允许单独
    // UPDATE；其余业务列保持 append-only，DELETE 仍整体禁止。触发器放在 ALTER
    // 之后创建，避免旧库缺 db_workspace_id 列时 CREATE TRIGGER 失败。
    conn.execute_batch(
        r#"
        DROP TRIGGER IF EXISTS trg_write_audit_events_no_update;
        CREATE TRIGGER trg_write_audit_events_no_update
        BEFORE UPDATE OF workspace_id, db_workspace_id, event_type, outcome, grant_id,
            principal_id, task_id, step_id, path, base_hash, new_hash, reason_code,
            request_id, detail_json, recorded_at
        ON write_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'write_audit_events is append-only');
        END;
        "#,
    )
    .map_err(|e| {
        DaemonRpcError::internal_error(format!("write audit append-only 触发器初始化失败: {e}"))
    })?;
    Ok(())
}

const WRITE_AUDIT_CHAIN_TABLE: &str = "remote_write_events";
const WRITE_GRANT_DEFAULT_TTL_SECS: f64 = 900.0;
const WRITE_GRANT_MAX_TTL_SECS: f64 = 900.0;

/// 统一授权主语：mTLS SAN principal；loopback 合成 owner 身份。
fn write_caller_principal(peer: &PeerCredential) -> String {
    peer.principal_id()
        .map(str::to_string)
        .unwrap_or_else(|| "local-owner".to_string())
}

fn is_lower_sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// workspace 相对路径归一化（拒绝绝对路径、盘符、UNC 与 `..` 穿越）。
fn normalize_write_rel_path(raw: &str) -> Result<String, DaemonRpcError> {
    let normalized = raw.trim().replace('\\', "/");
    if normalized.is_empty() {
        return Err(DaemonRpcError::new(
            "E_WRITE_PATH_FORBIDDEN",
            "write path 不能为空",
        ));
    }
    if normalized.starts_with('/') || normalized.contains(':') {
        return Err(DaemonRpcError::new(
            "E_WRITE_PATH_FORBIDDEN",
            format!("write path 必须是 workspace 相对路径: {raw}"),
        ));
    }
    let mut parts: Vec<&str> = Vec::new();
    for seg in normalized.split('/') {
        match seg {
            "" | "." => continue,
            ".." => {
                return Err(DaemonRpcError::new(
                    "E_WRITE_PATH_FORBIDDEN",
                    format!("write path 禁止上跳穿越: {raw}"),
                ))
            }
            other => parts.push(other),
        }
    }
    if parts.is_empty() {
        return Err(DaemonRpcError::new(
            "E_WRITE_PATH_FORBIDDEN",
            format!("write path 无有效路径段: {raw}"),
        ));
    }
    Ok(parts.join("/"))
}

#[derive(Debug, Clone)]
struct WriteGrantRow {
    grant_id: String,
    workspace_id: i64,
    db_workspace_id: i64,
    path: String,
    task_id: String,
    step_id: String,
    principal_id: String,
    base_hash: String,
    allowed_paths_json: String,
    status: String,
    consumed: i64,
    created_at: f64,
    expires_at: f64,
}

fn load_write_grant(
    conn: &rusqlite::Connection,
    grant_id: &str,
) -> Result<Option<WriteGrantRow>, DaemonRpcError> {
    conn.query_row(
        "SELECT grant_id, workspace_id, db_workspace_id, path, task_id, step_id, \
                principal_id, base_hash, allowed_paths_json, status, consumed, created_at, \
                expires_at \
         FROM write_grants WHERE grant_id = ?1",
        rusqlite::params![grant_id],
        |row| {
            Ok(WriteGrantRow {
                grant_id: row.get(0)?,
                workspace_id: row.get(1)?,
                db_workspace_id: row.get(2)?,
                path: row.get(3)?,
                task_id: row.get(4)?,
                step_id: row.get(5)?,
                principal_id: row.get(6)?,
                base_hash: row.get(7)?,
                allowed_paths_json: row.get(8)?,
                status: row.get(9)?,
                consumed: row.get(10)?,
                created_at: row.get(11)?,
                expires_at: row.get(12)?,
            })
        },
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("write_grants 查询失败: {e}")))
}

fn write_allowed_paths(grant: &WriteGrantRow) -> Vec<String> {
    serde_json::from_str(&grant.allowed_paths_json).unwrap_or_default()
}

struct WriteAuditEvent<'a> {
    workspace_id: i64,
    db_workspace_id: i64,
    event_type: &'a str,
    outcome: &'a str,
    grant_id: &'a str,
    principal_id: &'a str,
    task_id: &'a str,
    step_id: &'a str,
    path: &'a str,
    base_hash: &'a str,
    new_hash: &'a str,
    reason_code: &'a str,
    request_id: &'a str,
    detail: &'a Value,
    recorded_at: f64,
}

fn append_write_audit(
    conn: &rusqlite::Connection,
    event: &WriteAuditEvent<'_>,
) -> Result<i64, DaemonRpcError> {
    // review F5：事件行与 audit_chain 必须原子写入。SAVEPOINT 在事务内外均可用；
    // 任一步失败都回滚到保存点，杜绝「event 已落库、chain 未写」的哈希链缺口。
    const SP: &str = "cw_write_audit_append";
    conn.execute_batch(&format!("SAVEPOINT {SP}")).map_err(|e| {
        DaemonRpcError::internal_error(format!("write audit savepoint 开启失败: {e}"))
    })?;
    match append_write_audit_inner(conn, event) {
        Ok(event_id) => {
            conn.execute_batch(&format!("RELEASE {SP}")).map_err(|e| {
                DaemonRpcError::internal_error(format!("write audit savepoint 提交失败: {e}"))
            })?;
            Ok(event_id)
        }
        Err(err) => {
            let _ = conn.execute_batch(&format!("ROLLBACK TO {SP}; RELEASE {SP}"));
            Err(err)
        }
    }
}

fn append_write_audit_inner(
    conn: &rusqlite::Connection,
    event: &WriteAuditEvent<'_>,
) -> Result<i64, DaemonRpcError> {
    conn.execute(
        "INSERT INTO write_audit_events(\
             workspace_id, db_workspace_id, event_type, outcome, grant_id, principal_id, \
             task_id, step_id, path, base_hash, new_hash, reason_code, request_id, \
             detail_json, recorded_at\
         ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)",
        rusqlite::params![
            event.workspace_id,
            event.db_workspace_id,
            event.event_type,
            event.outcome,
            event.grant_id,
            event.principal_id,
            event.task_id,
            event.step_id,
            event.path,
            event.base_hash,
            event.new_hash,
            event.reason_code,
            event.request_id,
            event.detail.to_string(),
            event.recorded_at,
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("write_audit_events 写入失败: {e}")))?;
    let event_id = conn.last_insert_rowid();
    // 同步哈希链（S3-09）：payload 与事件行一一对应，可用 audit_verify_chain 回放。
    crate::cli::task::sign_audit_chain(
        conn,
        WRITE_AUDIT_CHAIN_TABLE,
        &event_id.to_string(),
        &json!({
            "event_id": event_id,
            "workspace_id": event.workspace_id,
            "db_workspace_id": event.db_workspace_id,
            "event_type": event.event_type,
            "outcome": event.outcome,
            "grant_id": event.grant_id,
            "principal_id": event.principal_id,
            "task_id": event.task_id,
            "step_id": event.step_id,
            "path": event.path,
            "base_hash": event.base_hash,
            "new_hash": event.new_hash,
            "reason_code": event.reason_code,
            "request_id": event.request_id,
            "recorded_at": event.recorded_at,
        }),
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("write audit_chain 写入失败: {e}")))?;
    // review F5：回填链行 id，形成 event ↔ audit_chain 双向可追溯的硬关联。
    // 触发器只允许更新 audit_record_id，其余业务列仍 append-only。
    let chain_id = conn.last_insert_rowid();
    conn.execute(
        "UPDATE write_audit_events SET audit_record_id = ?2 WHERE event_id = ?1",
        rusqlite::params![event_id, chain_id.to_string()],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("audit_record_id 回填失败: {e}")))?;
    Ok(event_id)
}

/// 拒绝事件审计（fail-closed）：返回调用方应回给客户端的错误。
///
/// review F5：不再 `let _` 吞掉审计失败——append_write_audit 内部以 SAVEPOINT
/// 保证 event+chain 原子，失败时两者都不落库；此时用 `E_WRITE_AUDIT_UNAVAILABLE`
/// 明确覆盖原始错误，避免「拒绝已发生但审计静默丢失」。
#[allow(clippy::too_many_arguments)]
fn audit_write_reject(
    conn: &rusqlite::Connection,
    workspace_id: i64,
    db_workspace_id: i64,
    principal_id: &str,
    task_id: &str,
    step_id: &str,
    grant_id: &str,
    path: &str,
    base_hash: &str,
    new_hash: &str,
    reason_code: &str,
    request_id: &str,
    detail: Value,
    original_code: &str,
    original_message: &str,
) -> DaemonRpcError {
    let now = now_ts_secs();
    let event = WriteAuditEvent {
        workspace_id,
        db_workspace_id,
        event_type: "reject",
        outcome: "deny",
        grant_id,
        principal_id,
        task_id,
        step_id,
        path,
        base_hash,
        new_hash,
        reason_code,
        request_id,
        detail: &detail,
        recorded_at: now,
    };
    match append_write_audit(conn, &event) {
        Ok(_) => DaemonRpcError::new(original_code, original_message.to_string()),
        Err(audit_err) => DaemonRpcError::new(
            "E_WRITE_AUDIT_UNAVAILABLE",
            format!("{original_message}（拒绝审计写入失败: {}）", audit_err.message),
        ),
    }
}

/// task/step 关闭或不属于该 task → E_WRITE_TASK_FORBIDDEN。
fn validate_task_step_for_write(
    store: Option<&TaskCollabStore>,
    task_id: &str,
    step_id: &str,
) -> Result<(), DaemonRpcError> {
    // 精简 state / 测试无 task store 时跳过（与 check_task_owner 语义一致）。
    let Some(store) = store else {
        return Ok(());
    };
    let conn = store
        .conn
        .lock()
        .map_err(|_| DaemonRpcError::internal_error("task_collab_store 锁不可用"))?;
    let task_status: Option<String> = conn
        .query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            rusqlite::params![task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("tasks 查询失败: {e}")))?;
    let task_status = task_status.ok_or_else(|| {
        DaemonRpcError::new(
            "E_WRITE_TASK_FORBIDDEN",
            format!("task 不存在: {task_id}"),
        )
    })?;
    if !matches!(task_status.as_str(), "open" | "in_progress") {
        return Err(DaemonRpcError::new(
            "E_WRITE_TASK_FORBIDDEN",
            format!("task 状态 {task_status} 不允许远端写: {task_id}"),
        ));
    }
    let step: Option<(String, String)> = conn
        .query_row(
            "SELECT task_id, status FROM task_steps WHERE id = ?1",
            rusqlite::params![step_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("task_steps 查询失败: {e}")))?;
    let (step_task_id, step_status) = step.ok_or_else(|| {
        DaemonRpcError::new(
            "E_WRITE_TASK_FORBIDDEN",
            format!("step 不存在: {step_id}"),
        )
    })?;
    if step_task_id != task_id {
        return Err(DaemonRpcError::new(
            "E_WRITE_TASK_FORBIDDEN",
            format!("step {step_id} 不属于 task {task_id}"),
        ));
    }
    if matches!(
        step_status.as_str(),
        "done" | "completed" | "closed" | "cancelled" | "skipped" | "failed"
    ) {
        return Err(DaemonRpcError::new(
            "E_WRITE_TASK_FORBIDDEN",
            format!("step 状态 {step_status} 不允许远端写: {step_id}"),
        ));
    }
    Ok(())
}

/// review F2：principal ↔ workspace ↔ task 三重归属校验（fail-closed）。

/// - 已认证 principal 必须在其 `allowed_workspace_ids` 内（HTTP peer 的 uid 对所有
///   mTLS principal 都是 daemon owner，单靠 uid 无法区分 principal）；
/// - task 绑定 workspace 时，必须与本次请求的 workspace_instance_id 一致，禁止用
///   A workspace 的任务给 B workspace 签 grant。
fn enforce_write_scope(
    peer: &PeerCredential,
    registry_workspace_id: i64,
    workspace_key: &str,
    task_store: Option<&TaskCollabStore>,
    task_id: &str,
) -> Result<(), DaemonRpcError> {
    if peer.principal.is_authenticated()
        && !peer
            .principal
            .workspace_allowed(&registry_workspace_id.to_string())
    {
        return Err(DaemonRpcError::new(
            "E_PRINCIPAL_WORKSPACE_FORBIDDEN",
            format!(
                "principal 未被授权访问 workspace {registry_workspace_id}（write.grant 拒绝）"
            ),
        ));
    }
    if let Some(store) = task_store {
        if let Some(bound) = store.task_workspace_instance_id(task_id)? {
            let bound = bound.trim();
            if !bound.is_empty() && bound != workspace_key {
                return Err(DaemonRpcError::new(
                    "E_WRITE_TASK_FORBIDDEN",
                    format!("task {task_id} 绑定的 workspace 与请求不一致"),
                ));
            }
        }
    }
    Ok(())
}

/// `write.grant`：签发一次性写 grant 并对 (workspace, path) 加逻辑租约。
pub fn handle_write_grant(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
    task_store: Option<&TaskCollabStore>,
) -> Result<Value, DaemonRpcError> {
    let workspace_key = params
        .get("workspace_instance_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            params.get("workspace_id").and_then(|v| match v {
                Value::String(s) if !s.trim().is_empty() => Some(s.trim().to_string()),
                Value::Number(n) => Some(n.to_string()),
                _ => None,
            })
        })
        .ok_or_else(|| {
            DaemonRpcError::invalid_params(
                "write.grant 需要 workspace_instance_id 或 workspace_id",
            )
        })?;
    let task_id = require_str_param(params, "task_id")?.trim().to_string();
    let step_id = require_str_param(params, "step_id")?.trim().to_string();
    let raw_path = require_str_param(params, "path")?;
    let path = normalize_write_rel_path(raw_path)?;
    let base_hash = get_str_param_or(params, "base_hash", "").trim().to_string();
    if !base_hash.is_empty() && !is_lower_sha256_hex(&base_hash) {
        return Err(DaemonRpcError::invalid_params(
            "base_hash 必须为空（新文件）或 64 位小写 hex",
        ));
    }
    let intent = get_str_param_or(params, "intent", "");
    let request_id = get_str_param_or(params, "request_id", "");
    let requested_ttl = params
        .get("ttl_seconds")
        .and_then(Value::as_f64)
        .unwrap_or(WRITE_GRANT_DEFAULT_TTL_SECS);
    if !(requested_ttl > 0.0) {
        return Err(DaemonRpcError::invalid_params("ttl_seconds 必须大于 0"));
    }
    let ttl = requested_ttl.min(WRITE_GRANT_MAX_TTL_SECS);
    let now = now_ts_secs();
    let principal_id = write_caller_principal(peer);

    // workspace ACL / root（fail-closed）
    let (registry_workspace_id, root) = resolve_workspace(registry, peer, &workspace_key)?;

    // 允许路径集合：默认仅本次 path；不在集合内自动补入（避免签发不可用 grant）。
    let mut allowed_paths: Vec<String> = params
        .get("allowed_paths")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .filter_map(|s| normalize_write_rel_path(s).ok())
                .collect()
        })
        .unwrap_or_default();
    if !allowed_paths.iter().any(|p| p == &path) {
        allowed_paths.push(path.clone());
    }
    let allowed_paths_json = serde_json::to_string(&allowed_paths)
        .map_err(|e| DaemonRpcError::internal_error(format!("allowed_paths 序列化失败: {e}")))?;

    let conn = open_codegraph_write(codegraph_db)?;
    ensure_write_governance_tables(&conn)?;
    let db_workspace_id = ensure_workspace_row_by_root(&conn, &root)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 行映射失败: {e}")))?;

    // review F2：principal/workspace/task 归属先于任务状态校验（防止跨 workspace 签发）。
    if let Err(err) =
        enforce_write_scope(peer, registry_workspace_id, &workspace_key, task_store, &task_id)
    {
        return Err(audit_write_reject(
            &conn,
            registry_workspace_id,
            db_workspace_id,
            &principal_id,
            &task_id,
            &step_id,
            "",
            &path,
            &base_hash,
            "",
            &err.code,
            &request_id,
            json!({"phase": "grant", "detail": err.message}),
            &err.code,
            &err.message,
        ));
    }

    // task/step 归属与关闭态校验（S3-08）
    if let Err(err) = validate_task_step_for_write(task_store, &task_id, &step_id) {
        return Err(audit_write_reject(
            &conn,
            registry_workspace_id,
            db_workspace_id,
            &principal_id,
            &task_id,
            &step_id,
            "",
            &path,
            &base_hash,
            "",
            &err.code,
            &request_id,
            json!({"phase": "grant", "detail": err.message}),
            &err.code,
            &err.message,
        ));
    }

    // review F6：授权通过后再做 symlink containment（词法根内 + 解析后根内），
    // 避免向无 workspace 权限的 principal 泄露根内文件系统状态。
    if let Err(err) = check_write_containment(&root, &path) {
        return Err(audit_write_reject(
            &conn,
            registry_workspace_id,
            db_workspace_id,
            &principal_id,
            &task_id,
            &step_id,
            "",
            &path,
            &base_hash,
            "",
            &err.code,
            &request_id,
            json!({"phase": "grant", "detail": err.message}),
            &err.code,
            &err.message,
        ));
    }

    // base_hash 必须与 daemon 当前索引一致；不一致说明已有写入（S3-07 stale）。
    let current_hash: Option<String> = conn
        .query_row(
            "SELECT current_content_hash FROM file_instances \
             WHERE workspace_id = ?1 AND rel_path = ?2",
            rusqlite::params![db_workspace_id, path],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("file_instances 查询失败: {e}")))?;
    let expected_base = current_hash.unwrap_or_default();
    if base_hash != expected_base {
        let err = DaemonRpcError::new(
            "E_WRITE_STALE_BASE",
            format!(
                "base_hash 与 daemon 索引不一致（path={path}，expected={expected_base}，got={base_hash}）"
            ),
        );
        return Err(audit_write_reject(
            &conn,
            registry_workspace_id,
            db_workspace_id,
            &principal_id,
            &task_id,
            &step_id,
            "",
            &path,
            &base_hash,
            "",
            "E_WRITE_STALE_BASE",
            &request_id,
            json!({"phase": "grant", "expected_base_hash": expected_base}),
            &err.code,
            &err.message,
        ));
    }

    // grant_id：16 字节 CSPRNG。
    let mut entropy = [0u8; 16];
    getrandom::fill(&mut entropy).map_err(|e| {
        DaemonRpcError::internal_error(format!("grant_id 熵源不可用: {e}"))
    })?;
    let grant_id = format!("wg_{}", hex::encode(entropy));

    let grant_tx = |conn: &rusqlite::Connection| -> Result<i64, DaemonRpcError> {
        // 过期 grant 先收敛，释放逻辑租约（daemon 是唯一写者，事务内安全）。
        conn.execute(
            "UPDATE write_grants SET status = 'expired' \
             WHERE workspace_id = ?1 AND path = ?2 AND status = 'active' AND consumed = 0 \
               AND expires_at <= ?3",
            rusqlite::params![registry_workspace_id, path, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("过期 grant 收敛失败: {e}")))?;
        let active: Option<String> = conn
            .query_row(
                "SELECT grant_id FROM write_grants \
                 WHERE workspace_id = ?1 AND path = ?2 AND status = 'active' AND consumed = 0 \
                 LIMIT 1",
                rusqlite::params![registry_workspace_id, path],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("活跃 grant 查询失败: {e}")))?;
        if active.is_some() {
            return Err(DaemonRpcError::new(
                "E_WRITE_LOCKED",
                format!("path 已被活跃 grant 租约占用: {path}"),
            ));
        }
        let expires_at = now + ttl;
        conn.execute(
            "INSERT INTO write_grants(\
                 grant_id, workspace_id, db_workspace_id, path, task_id, step_id, principal_id, \
                 base_hash, intent, allowed_paths_json, status, consumed, created_at, expires_at\
             ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,'active',0,?11,?12)",
            rusqlite::params![
                grant_id,
                registry_workspace_id,
                db_workspace_id,
                path,
                task_id,
                step_id,
                principal_id,
                base_hash,
                intent,
                allowed_paths_json,
                now,
                expires_at,
            ],
        )
        .map_err(|e| {
            if e.to_string().contains("UNIQUE constraint failed") {
                DaemonRpcError::new(
                    "E_WRITE_LOCKED",
                    format!("path 已被活跃 grant 租约占用: {path}"),
                )
            } else {
                DaemonRpcError::internal_error(format!("write_grants 写入失败: {e}"))
            }
        })?;
        let detail = json!({
            "phase": "grant",
            "intent": intent,
            "allowed_paths": allowed_paths,
            "expires_at": expires_at,
        });
        append_write_audit(
            conn,
            &WriteAuditEvent {
                workspace_id: registry_workspace_id,
                db_workspace_id,
                event_type: "grant",
                outcome: "allow",
                grant_id: &grant_id,
                principal_id: &principal_id,
                task_id: &task_id,
                step_id: &step_id,
                path: &path,
                base_hash: &base_hash,
                new_hash: "",
                reason_code: "",
                request_id: &request_id,
                detail: &detail,
                recorded_at: now,
            },
        )
    };

    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| DaemonRpcError::internal_error(format!("grant 事务开启失败: {e}")))?;
    match grant_tx(&conn) {
        Ok(audit_event_id) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| DaemonRpcError::internal_error(format!("grant 事务提交失败: {e}")))?;
            Ok(json!({
                "ok": true,
                "grant_id": grant_id,
                "workspace_id": registry_workspace_id,
                "db_workspace_id": db_workspace_id,
                "path": path,
                "task_id": task_id,
                "step_id": step_id,
                "principal_id": principal_id,
                "base_hash": base_hash,
                "allowed_paths": allowed_paths,
                "expires_at": now + ttl,
                "ttl_seconds": ttl,
                "audit_event_id": audit_event_id,
            }))
        }
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(audit_write_reject(
                &conn,
                registry_workspace_id,
                db_workspace_id,
                &principal_id,
                &task_id,
                &step_id,
                "",
                &path,
                &base_hash,
                "",
                &err.code,
                &request_id,
                json!({"phase": "grant", "detail": err.message}),
                &err.code,
                &err.message,
            ))
        }
    }
}

/// `write.commit`：校验 grant 后原子更新索引、写审计并消费 grant。
pub fn handle_write_commit(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
    task_store: Option<&TaskCollabStore>,
) -> Result<Value, DaemonRpcError> {
    let grant_id = require_str_param(params, "grant_id")?.trim().to_string();
    let new_hash = require_str_param(params, "new_hash")?.trim().to_string();
    if !is_lower_sha256_hex(&new_hash) {
        return Err(DaemonRpcError::invalid_params(
            "new_hash 必须为 64 位小写 hex",
        ));
    }
    let request_id = get_str_param_or(params, "request_id", "");
    let content = params.get("content").and_then(Value::as_str);
    let delta = params.get("delta").and_then(Value::as_str);
    let principal_id = write_caller_principal(peer);
    let now = now_ts_secs();

    let conn = open_codegraph_write(codegraph_db)?;
    ensure_write_governance_tables(&conn)?;
    let grant = load_write_grant(&conn, &grant_id)?.ok_or_else(|| {
        DaemonRpcError::new("E_WRITE_GRANT_NOT_FOUND", format!("grant 不存在: {grant_id}"))
    })?;

    let commit_path = match params.get("path").and_then(Value::as_str) {
        Some(raw) if !raw.trim().is_empty() => normalize_write_rel_path(raw)?,
        _ => grant.path.clone(),
    };
    let reject = |reason: &str, code: &str, detail: Value| {
        audit_write_reject(
            &conn,
            grant.workspace_id,
            grant.db_workspace_id,
            &principal_id,
            &grant.task_id,
            &grant.step_id,
            &grant.grant_id,
            &commit_path,
            &grant.base_hash,
            &new_hash,
            reason,
            &request_id,
            detail,
            code,
            reason,
        )
    };

    // ---- 静态 fail-closed 检查（不产生索引副作用）----
    if grant.principal_id != principal_id {
        return Err(reject(
            "E_WRITE_GRANT_PRINCIPAL_MISMATCH",
            "E_WRITE_GRANT_PRINCIPAL_MISMATCH",
            json!({"phase": "commit", "grant_principal": grant.principal_id, "caller": principal_id}),
        ));
    }
    if grant.consumed != 0 || grant.status == "consumed" {
        return Err(reject(
            "E_WRITE_GRANT_CONSUMED",
            "E_WRITE_GRANT_CONSUMED",
            json!({"phase": "commit", "grant_id": grant_id}),
        ));
    }
    if grant.status == "expired" || now > grant.expires_at {
        return Err(reject(
            "E_WRITE_GRANT_EXPIRED",
            "E_WRITE_GRANT_EXPIRED",
            json!({"phase": "commit", "expires_at": grant.expires_at, "now": now}),
        ));
    }
    if grant.status != "active" {
        return Err(reject(
            "E_WRITE_GRANT_CONSUMED",
            "E_WRITE_GRANT_CONSUMED",
            json!({"phase": "commit", "status": grant.status}),
        ));
    }
    let allowed_paths = write_allowed_paths(&grant);
    if commit_path != grant.path || !allowed_paths.iter().any(|p| p == &commit_path) {
        return Err(reject(
            "E_WRITE_PATH_FORBIDDEN",
            "E_WRITE_PATH_FORBIDDEN",
            json!({"phase": "commit", "grant_path": grant.path, "allowed_paths": allowed_paths}),
        ));
    }
    if let Err(err) = validate_task_step_for_write(task_store, &grant.task_id, &grant.step_id) {
        return Err(reject(&err.code, &err.code, json!({"phase": "commit", "detail": err.message})));
    }

    // commit 复核 workspace ACL，避免 grant 签发后 workspace 被移除/归档。
    let workspace = owned_workspace_by_id(registry, peer.uid, grant.workspace_id)?;
    let root = workspace
        .get("host_real_root")
        .and_then(Value::as_str)
        .or_else(|| workspace.get("client_view_root").and_then(Value::as_str))
        .map(PathBuf::from)
        .ok_or_else(|| DaemonRpcError::internal_error("workspace 缺少 host_real_root"))?;
    let root = std::fs::canonicalize(&root).unwrap_or(root);
    let db_workspace_id = ensure_workspace_row_by_root(&conn, &root)
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 行映射失败: {e}")))?;

    // review F6：写前 containment（事务内会再次复核）。
    if let Err(err) = check_write_containment(&root, &commit_path) {
        return Err(reject(
            &err.message,
            &err.code,
            json!({"phase": "commit", "detail": err.message}),
        ));
    }

    let mtime = params
        .get("mtime")
        .and_then(Value::as_f64)
        .unwrap_or(now);
    let delta_detail = json!({
        "phase": "commit",
        "content_supplied": content.is_some(),
        "delta_bytes": delta.map(|d| d.len()).unwrap_or(0),
        "delta_sha256": delta.map(|d| sha256_hex(d.as_bytes())).unwrap_or_default(),
        "request_id": request_id,
    });

    let commit_tx = |conn: &rusqlite::Connection| -> Result<(i64, i64), DaemonRpcError> {
        // 事务内重读，避免与过期/并发路径竞态。
        let grant = load_write_grant(conn, &grant_id)?.ok_or_else(|| {
            DaemonRpcError::new("E_WRITE_GRANT_NOT_FOUND", format!("grant 不存在: {grant_id}"))
        })?;
        if grant.consumed != 0 || grant.status != "active" {
            return Err(DaemonRpcError::new(
                "E_WRITE_GRANT_CONSUMED",
                "grant 已被消费或不再活跃",
            ));
        }
        if now > grant.expires_at {
            return Err(DaemonRpcError::new(
                "E_WRITE_GRANT_EXPIRED",
                "grant 已过期",
            ));
        }
        // review F6：事务内重放静态授权复核，缩小 TOCTOU 窗口（principal /
        // path / workspace 归属 / task 状态 / symlink containment）。
        if grant.principal_id != principal_id {
            return Err(DaemonRpcError::new(
                "E_WRITE_GRANT_PRINCIPAL_MISMATCH",
                "grant principal 与调用方不一致",
            ));
        }
        if grant.path != commit_path {
            return Err(DaemonRpcError::new(
                "E_WRITE_PATH_FORBIDDEN",
                "commit path 与 grant 不一致",
            ));
        }
        if grant.db_workspace_id != db_workspace_id {
            return Err(DaemonRpcError::new(
                "E_WRITE_WORKSPACE_MISMATCH",
                "grant workspace 与当前 workspace 不一致",
            ));
        }
        validate_task_step_for_write(task_store, &grant.task_id, &grant.step_id)?;
        check_write_containment(&root, &commit_path)?;
        let current: Option<(String, i64)> = conn
            .query_row(
                "SELECT current_content_hash, total_lines FROM file_instances \
                 WHERE workspace_id = ?1 AND rel_path = ?2",
                rusqlite::params![db_workspace_id, commit_path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("file_instances 查询失败: {e}")))?;
        let (current_hash, existing_total_lines) = current.unwrap_or_default();
        if current_hash != grant.base_hash {
            let consumed_after: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM write_grants \
                     WHERE db_workspace_id = ?1 AND path = ?2 AND status = 'consumed' \
                       AND consumed_at > ?3",
                    rusqlite::params![grant.db_workspace_id, commit_path, grant.created_at],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            let code = if consumed_after > 0 {
                "E_WRITE_STALE_BASE"
            } else {
                "E_WRITE_BASE_HASH_MISMATCH"
            };
            return Err(DaemonRpcError::new(
                code,
                format!(
                    "base_hash 已变化（grant={}，index={current_hash}）",
                    grant.base_hash
                ),
            ));
        }
        let total_lines = match content {
            Some(text) => text.bytes().filter(|b| *b == b'\n').count() as i64,
            None => existing_total_lines,
        };
        conn.execute(
            "INSERT OR IGNORE INTO file_contents(content_hash, language, total_lines, first_seen_at) \
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![new_hash, infer_language(&commit_path), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents 写入失败: {e}")))?;
        upsert_file_instance(
            conn,
            db_workspace_id,
            &commit_path,
            &root.join(&commit_path),
            &new_hash,
            mtime,
            total_lines,
            false,
        )?;
        let audit_event_id = append_write_audit(
            conn,
            &WriteAuditEvent {
                workspace_id: grant.workspace_id,
                db_workspace_id,
                event_type: "commit",
                outcome: "allow",
                grant_id: &grant_id,
                principal_id: &principal_id,
                task_id: &grant.task_id,
                step_id: &grant.step_id,
                path: &commit_path,
                base_hash: &grant.base_hash,
                new_hash: &new_hash,
                reason_code: "",
                request_id: &request_id,
                detail: &delta_detail,
                recorded_at: now,
            },
        )?;
        let updated = conn
            .execute(
                "UPDATE write_grants SET status = 'consumed', consumed = 1, consumed_at = ?2, \
                        new_hash = ?3, request_id = ?4 \
                 WHERE grant_id = ?1 AND status = 'active' AND consumed = 0",
                rusqlite::params![grant_id, now, new_hash, request_id],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("grant 消费失败: {e}")))?;
        if updated != 1 {
            return Err(DaemonRpcError::new(
                "E_WRITE_GRANT_CONSUMED",
                "grant 已被并发消费",
            ));
        }
        Ok((audit_event_id, db_workspace_id))
    };

    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| DaemonRpcError::internal_error(format!("commit 事务开启失败: {e}")))?;
    match commit_tx(&conn) {
        Ok((audit_event_id, db_workspace_id)) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| DaemonRpcError::internal_error(format!("commit 事务提交失败: {e}")))?;
            Ok(json!({
                "ok": true,
                "grant_id": grant_id,
                "workspace_id": grant.workspace_id,
                "db_workspace_id": db_workspace_id,
                "path": commit_path,
                "base_hash": grant.base_hash,
                "new_hash": new_hash,
                "consumed_at": now,
                "audit_event_id": audit_event_id,
            }))
        }
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(audit_write_reject(
                &conn,
                grant.workspace_id,
                db_workspace_id,
                &principal_id,
                &grant.task_id,
                &grant.step_id,
                &grant_id,
                &commit_path,
                &grant.base_hash,
                &new_hash,
                &err.code,
                &request_id,
                json!({"phase": "commit", "detail": err.message}),
                &err.code,
                &err.message,
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_anchor_path_to_root_relative_and_absolute() {
        let root = std::path::Path::new("C:/git_work/callwarden/.workbuddy/scripts/outputs/wt_iso_ws");
        // 相对路径以 workspace 根为基准（不是 daemon 进程 CWD）
        let anchored = anchor_path_to_root(root, "alpha.py");
        assert_eq!(anchored, root.join("alpha.py").to_string_lossy());
        assert!(std::path::Path::new(&anchored).starts_with(root));
        // 绝对路径原样保留（根内/根外由 ensure_root_containment 判定）
        let abs = "C:/git_work/callwarden/cw.py";
        assert_eq!(anchor_path_to_root(root, abs), abs);
    }

    #[test]
    fn test_anchor_path_keeps_root_containment_fail_closed() {
        let tag = format!("cw_f5_{}", std::process::id());
        let ws = std::env::temp_dir().join(&tag).join("ws");
        let outside = std::env::temp_dir().join(&tag).join("outside");
        std::fs::create_dir_all(&ws).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(ws.join("alpha.py"), "x\n").unwrap();
        std::fs::write(outside.join("alpha.py"), "x\n").unwrap();
        // 相对路径锚定后落在 workspace 根内 → 包含校验通过
        let anchored = anchor_path_to_root(&ws, "alpha.py");
        let real = std::fs::canonicalize(&anchored).unwrap();
        ensure_root_containment(&ws, &real.to_string_lossy()).unwrap();
        // 根外路径即使 owner 合法也必须 path_escape（写面 fail-closed）
        let escaped = anchor_path_to_root(&outside, "alpha.py");
        let escaped_real = std::fs::canonicalize(&escaped).unwrap();
        let err = ensure_root_containment(&ws, &escaped_real.to_string_lossy()).unwrap_err();
        assert_eq!(err.code, "path_escape");
        let _ = std::fs::remove_dir_all(std::env::temp_dir().join(&tag));
    }

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
    fn test_classify_build_graph_file_matches_contract() {
        let hash = sha256_hex(b"def foo(): pass\n");
        // 首次全建：库内无行 → inserted（不再是 unchanged）
        assert_eq!(
            classify_build_graph_file(true, None, &hash),
            BuildGraphFileClass::Inserted
        );
        // 二次重建且内容未变 → unchanged
        assert_eq!(
            classify_build_graph_file(true, Some((hash.as_str(), "parsed")), &hash),
            BuildGraphFileClass::Unchanged
        );
        // 内容变化 → inserted
        assert_eq!(
            classify_build_graph_file(true, Some((hash.as_str(), "parsed")), &sha256_hex(b"x")),
            BuildGraphFileClass::Inserted
        );
        // 库内存在但未解析过（status=pending）→ 仍需解析入库
        assert_eq!(
            classify_build_graph_file(true, Some((hash.as_str(), "pending")), &hash),
            BuildGraphFileClass::Inserted
        );
        // 无 parser 的语言 → skipped_unparsed（不再计入 unchanged）
        assert_eq!(
            classify_build_graph_file(false, Some((hash.as_str(), "pending")), &hash),
            BuildGraphFileClass::SkippedUnparsed
        );
        assert_eq!(
            classify_build_graph_file(false, None, &hash),
            BuildGraphFileClass::SkippedUnparsed
        );
    }

    #[test]
    fn test_is_mtime_cache_hit_contract() {
        // 仅「已解析 + hash 非空 + mtime 一致」才可免读取
        assert!(is_mtime_cache_hit("parsed", "abc", 100.0, 100.0));
        assert!(is_mtime_cache_hit("parsed", "abc", 100.002, 100.0019));
        // 未解析过（pending）→ 必须重新解析
        assert!(!is_mtime_cache_hit("pending", "abc", 100.0, 100.0));
        // mtime 变化 → 回落 sha256 全量校验
        assert!(!is_mtime_cache_hit("parsed", "abc", 100.0, 101.0));
        // hash 为空（异常行）→ 不信任缓存
        assert!(!is_mtime_cache_hit("parsed", "", 100.0, 100.0));
    }

    #[test]
    fn test_purge_missing_file_graph_demotes_and_removes() {
        // P3-1 删除感知：a.py 从磁盘消失后，其 symbols/file_instances 全清，
        // b.py 指向 a.py 的已解析边降级为 raw 且名字回传供重算。
        let tmp = std::env::temp_dir().join(format!(
            "cw_purge_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(&tmp, &conn, ws_id, "a.py", "def helper():\n    return 1\n");
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "b.py",
            "def caller():\n    return helper()\n",
        );
        resolve_raw_calls(&conn, ws_id).unwrap();
        let resolved_before: i64 = conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'helper'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(resolved_before > 0);

        let names = purge_missing_file_graph(&conn, ws_id, "a.py").unwrap();
        assert!(names.iter().any(|n| n == "helper"), "受影响名字须回传: {names:?}");
        // a.py 的符号与文件行被清干净
        let left: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
                 WHERE fi.workspace_id = ?1 AND fi.rel_path = 'a.py'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(left, 0);
        let rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?1 AND rel_path = 'a.py'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rows, 0);
        // b.py 的边降级为 raw，不留悬空 callee_id
        let demoted: i64 = conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'helper'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(demoted, 0);
        let dangling: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM calls WHERE callee_id > 0 AND callee_id NOT IN (SELECT id FROM symbols)",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(dangling, 0);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_archive_ignored_file_graph_is_idempotent() {
        // C1：build/ 等被剪枝目录的存量行走归档路径（迁 archived_files +
        // 清图行 + status=archived），重复执行幂等，且不误伤未忽略文件。
        let tmp = std::env::temp_dir().join(format!(
            "cw_archive_ignored_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join(".gitignore"), "build/\n").unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "build/lib/callwarden/a.py",
            "def helper():\n    return 1\n",
        );
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "callwarden/a.py",
            "def helper():\n    return 1\n",
        );
        let skip = load_ignore_dir_names(&tmp);
        assert_eq!(
            ignored_dir_of("build/lib/callwarden/a.py", &skip),
            Some("build".to_string())
        );
        assert_eq!(ignored_dir_of("callwarden/a.py", &skip), None);
        // 隐藏目录启发式不属于 ignore 契约（`.github/` 会被 Python 管线索引），
        // 不能仅因 scan_files 跳过而归档
        assert_eq!(ignored_dir_of(".github/workflows/x.py", &skip), None);

        let (newly, names) = archive_ignored_file_graph(
            &conn, ws_id, "build/lib/callwarden/a.py", "ignored path: build", 0.0,
        )
        .unwrap()
        .expect("库内应有 build 行");
        assert!(newly, "首次调用应新归档");
        assert!(names.iter().any(|n| n == "helper"), "受影响名字须回传: {names:?}");
        let archived_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM archived_files WHERE workspace_id = ?1",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(archived_rows, 1);
        let build_symbols: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
                 WHERE fi.workspace_id = ?1 AND fi.rel_path LIKE 'build/%'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(build_symbols, 0, "归档后 build 图行必须清零");
        let build_status: String = conn
            .query_row(
                "SELECT status FROM file_instances WHERE workspace_id = ?1
                 AND rel_path = 'build/lib/callwarden/a.py'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(build_status, "archived");

        // 幂等：第二次不再新归档，也不再产生归档记录
        let (newly2, names2) = archive_ignored_file_graph(
            &conn, ws_id, "build/lib/callwarden/a.py", "ignored path: build", 0.0,
        )
        .unwrap()
        .expect("库内应有 build 行");
        assert!(!newly2, "重复执行不得重复归档");
        assert!(names2.is_empty());
        let archived_rows2: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM archived_files WHERE workspace_id = ?1",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(archived_rows2, 1);

        // 未忽略文件不受影响
        let source_symbols: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id
                 WHERE fi.workspace_id = ?1 AND fi.rel_path = 'callwarden/a.py'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        assert!(source_symbols >= 1);
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_resolve_raw_calls_for_names_prunes_unrelated() {
        // P3-1 边重算裁剪：过滤器外的 raw 边保持不变（不被全库重算波及）
        let tmp = std::env::temp_dir().join(format!(
            "cw_resolve_filter_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(&tmp, &conn, ws_id, "a.py", "def helper():\n    return 1\n");
        setup_ws_and_parse(
            &tmp, &conn, ws_id, "b.py",
            "def caller():\n    return helper() + missing()\n",
        );
        let filter: BTreeSet<String> = ["helper".to_string()].into_iter().collect();
        let (resolved, _) =
            resolve_raw_calls_for_names(&conn, ws_id, Some(&filter)).unwrap();
        assert_eq!(resolved, 1, "只应解析被过滤的 helper 边");
        let helper_id: i64 = conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'helper'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(helper_id > 0);
        let missing_id: i64 = conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'missing'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(missing_id, 0, "过滤器外的名字必须保持 raw");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_resolve_raw_calls_scoped_to_workspace() {
        // P3-1：单候选解析不得跨 workspace 挂接 raw 边。
        // ws1 定义 helper，ws2 有一条同名 raw 边；只解析 ws1 时 ws2 必须保持 raw。
        let tmp = std::env::temp_dir().join(format!(
            "cw_resolve_scope_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('w1', 'c:/w1', 0.0, 0, 't'), ('w2', 'c:/w2', 0.0, 0, 't')",
            [],
        )
        .unwrap();
        let ws1: i64 = conn
            .query_row("SELECT id FROM workspaces WHERE root_path = 'c:/w1'", [], |r| r.get(0))
            .unwrap();
        let ws2: i64 = conn
            .query_row("SELECT id FROM workspaces WHERE root_path = 'c:/w2'", [], |r| r.get(0))
            .unwrap();
        setup_ws_and_parse(&tmp, &conn, ws1, "a.py", "def helper():\n    return 1\n");
        setup_ws_and_parse(
            &tmp, &conn, ws2, "b.py",
            "def other():\n    return helper()\n",
        );
        let filter: BTreeSet<String> = ["helper".to_string()].into_iter().collect();
        let (resolved, _) = resolve_raw_calls_for_names(&conn, ws1, Some(&filter)).unwrap();
        assert_eq!(resolved, 0, "ws1 自身没有 raw 边，不应改动任何边");
        let ws2_edge: i64 = conn
            .query_row(
                "SELECT c.callee_id FROM calls c \
                 JOIN symbols s ON c.caller_id = s.id \
                 JOIN file_instances fi ON s.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND c.callee_name = 'helper'",
                rusqlite::params![ws2],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(ws2_edge, 0, "ws2 的 raw 边不得被 ws1 的解析结果挂接");
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn test_reparse_clears_restrictive_symbol_references() {
        // P3-1：重解析前的 DELETE FROM symbols 必须先把 coverage_data 等
        // RESTRICT 引用行清掉，否则主仓这类文件必现 FOREIGN KEY constraint failed。
        let tmp = std::env::temp_dir().join(format!(
            "cw_reparse_fk_test_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let db_path = tmp.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description)
             VALUES ('t', 'c:/t', 0.0, 0, 'test')",
            [],
        )
        .unwrap();
        let ws_id = conn.last_insert_rowid();
        setup_ws_and_parse(&tmp, &conn, ws_id, "a.py", "def helper():\n    return 1\n");
        let fid: i64 = conn
            .query_row(
                "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = 'a.py'",
                rusqlite::params![ws_id],
                |r| r.get(0),
            )
            .unwrap();
        let sid: i64 = conn
            .query_row(
                "SELECT id FROM symbols WHERE file_instance_id = ?1",
                rusqlite::params![fid],
                |r| r.get(0),
            )
            .unwrap();
        conn.execute(
            "INSERT INTO coverage_data \
             (file_instance_id, symbol_id, line_start, line_end, hit_count, report_source, imported_at) \
             VALUES (?1, ?2, 1, 2, 5, 'lcov', ?3)",
            rusqlite::params![fid, sid, now_ts_secs()],
        )
        .unwrap();
        let bytes = std::fs::read(tmp.join("a.py")).unwrap();
        let hash = sha256_hex(&bytes);
        let res = parse_and_store_symbols(
            &conn, fid, &tmp.join("a.py"), "a.py", &hash, now_ts_secs(), 2,
        );
        assert!(res.is_ok(), "重解析不得被 FK 约束中止: {:?}", res.err());
        let left: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM coverage_data WHERE symbol_id = ?1",
                rusqlite::params![sid],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(left, 0, "旧符号的覆盖率引用必须被清理");
        let _ = std::fs::remove_dir_all(&tmp);
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
        // P0-CR3：fresh DB 自动建 schema（与 storage::SCHEMA_VERSION 同步）
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
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
        let (syms, calls) =
            parse_and_store_symbols(&conn, fid, &src, "m.py", &hash, 0.0, 4).unwrap();
        assert!(syms >= 2, "expected >=2 symbols, got {syms}");
        assert!(calls >= 1, "expected >=1 call, got {calls}");
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n as usize, syms);
        // 幂等重建：再跑一次不重复累积
        let (syms2, _calls2) =
            parse_and_store_symbols(&conn, fid, &src, "m.py", &hash, 0.0, 4).unwrap();
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
            parse_and_store_symbols(conn, fid, &src, rel, &hash, 0.0, 0).unwrap();
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
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
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
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
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

    /// TP-11G P1-C 回归守卫：同一目录的 8.3 短名形态与长名形态必须映射到
    /// 同一 workspace 行（canonicalize 收敛）。修复前：纯词法归一化把两种
    /// 形态算作两个键 → ensure 建两行 → file_instances 分裂 → 按
    /// workspace_id 查询 QueryReturnedNoRows（CI Windows 双测试红根因）。
    /// CI 的 TMPDIR 含 RUNNER~1 短名，本地长名 tempdir 不复现——故本测试
    /// 显式构造短名路径复现并锁定。仅 Windows（8.3 短名是 Windows 概念）。
    #[cfg(windows)]
    #[test]
    fn normalize_root_short_and_long_form_share_one_workspace_row() {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::GetShortPathNameW;

        let long_base = std::env::temp_dir().join(format!(
            "cw_shortname_guard_{}",
            std::process::id()
        ));
        std::fs::create_dir_all(&long_base).unwrap();
        let mut short_buf = [0u16; 260];
        let long_wide: Vec<u16> = long_base
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let n = unsafe { GetShortPathNameW(long_wide.as_ptr(), short_buf.as_mut_ptr(), 260) };
        assert!(n > 0, "GetShortPathNameW 失败（卷未启用 8.3 生成时跳过无意义）");
        let short_base =
            std::path::PathBuf::from(String::from_utf16_lossy(&short_buf[..n as usize]));
        assert_ne!(
            long_base.to_string_lossy().to_lowercase(),
            short_base.to_string_lossy().to_lowercase(),
            "短名应与长名不同（否则本测试无意义）"
        );

        // 归一化键必须相同（canonicalize 收敛 8.3 短名）
        assert_eq!(
            normalize_root_for_authority(&short_base),
            normalize_root_for_authority(&long_base),
            "短名/长名必须归一化到同一键"
        );

        // 端到端：两种形态 ensure 必须得到同一行 id（而非两个）
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        crate::daemon::cas_merge::init_codegraph_schema(&conn).unwrap();
        let id_short = ensure_workspace_row_by_root(&conn, &short_base).unwrap();
        let id_long = ensure_workspace_row_by_root(&conn, &long_base).unwrap();
        assert_eq!(id_short, id_long, "同一目录的两种路径形态必须同一 workspace 行");
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM workspaces", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1, "不得为同一目录建第二行");
    }

    #[test]
    fn test_write_governance_schema_idempotent_lease_and_append_only() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        crate::daemon::cas_merge::init_codegraph_schema(&conn).unwrap();
        // 两次 ensure 必须幂等
        ensure_write_governance_tables(&conn).unwrap();
        ensure_write_governance_tables(&conn).unwrap();

        let insert_grant = "INSERT INTO write_grants(grant_id, workspace_id, path, task_id, \
             step_id, principal_id, base_hash, created_at, expires_at) \
             VALUES (?1, 1, 'src/a.py', 'T-1', 'S-1', 'pr-a', 'base', 1.0, 2.0)";
        conn.execute(insert_grant, rusqlite::params!["G-1"]).unwrap();
        assert!(
            conn.execute(insert_grant, rusqlite::params!["G-2"]).is_err(),
            "同 workspace/path 的第二个活跃 grant 必须被逻辑租约拒绝"
        );

        conn.execute(
            "INSERT INTO write_audit_events(workspace_id, event_type, outcome, grant_id, \
             path, base_hash, new_hash, recorded_at) \
             VALUES (1, 'grant', 'allow', 'G-1', 'src/a.py', 'base', '', 1.0)",
            [],
        )
        .unwrap();
        assert!(
            conn.execute(
                "UPDATE write_audit_events SET outcome = 'deny' WHERE event_id = 1",
                [],
            )
            .is_err(),
            "审计事件必须只追加"
        );
        assert!(
            conn.execute("DELETE FROM write_audit_events WHERE event_id = 1", [])
                .is_err(),
            "审计事件必须只追加"
        );
        let events: i64 = conn
            .query_row("SELECT COUNT(*) FROM write_audit_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(events, 1);

        // review F3：grant/审计与 file_instances 的 workspace id 双空间显式记录
        for table in ["write_grants", "write_audit_events"] {
            let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})")).unwrap();
            let cols: Vec<String> = stmt
                .query_map([], |r| r.get::<_, String>(1))
                .unwrap()
                .filter_map(Result::ok)
                .collect();
            assert!(
                cols.iter().any(|c| c == "db_workspace_id"),
                "{table} 缺少 db_workspace_id"
            );
        }
    }

    #[test]
    fn test_write_grant_commit_end_to_end() {
        let root = std::env::temp_dir().join(format!(
            "cw_write_gov_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&root).unwrap();
        let db_path = root.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let workspace = registry
            .register_workspace(
                1000,
                root.to_str().unwrap(),
                root.to_str().unwrap(),
                "",
                "",
                "",
            )
            .unwrap();
        let instance_id = workspace["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let peer = PeerCredential::new_unix(1000, 1000, 1);
        let grant_params = json!({
            "workspace_instance_id": instance_id,
            "task_id": "T-1",
            "step_id": "S-1",
            "path": "src/a.py",
            "base_hash": "",
            "request_id": "r1",
        });
        let grant =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap();
        let grant_id = grant["grant_id"].as_str().unwrap().to_string();
        assert_eq!(grant["base_hash"], "");

        // 同 path 活跃租约 → E_WRITE_LOCKED
        let locked =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap_err();
        assert_eq!(locked.code, "E_WRITE_LOCKED");

        let content = "def f():\n    return 1\n";
        let new_hash = sha256_hex(content.as_bytes());
        let commit = handle_write_commit(
            &registry,
            &peer,
            &json!({
                "grant_id": grant_id,
                "new_hash": new_hash,
                "content": content,
                "request_id": "r2",
                "workspace_instance_id": instance_id,
            }),
            Some(&db_path),
            None,
        )
        .unwrap();
        assert_eq!(commit["ok"], true);

        let conn = rusqlite::Connection::open(&db_path).unwrap();
        let (indexed_hash, grant_status): (String, String) = conn
            .query_row(
                "SELECT fi.current_content_hash, wg.status \
                 FROM file_instances fi, write_grants wg \
                 WHERE fi.rel_path = 'src/a.py' AND wg.grant_id = ?1",
                rusqlite::params![grant_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(indexed_hash, new_hash);
        assert_eq!(grant_status, "consumed");
        let allow_events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM write_audit_events \
                 WHERE outcome = 'allow' AND event_type IN ('grant','commit')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(allow_events, 2, "grant + commit 各一条 allow 审计");
        let chain_records: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM audit_chain WHERE table_name = 'remote_write_events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(chain_records, 3, "grant/locked/commit 均进入审计哈希链");

        // 重放 → E_WRITE_GRANT_CONSUMED
        let replay = handle_write_commit(
            &registry,
            &peer,
            &json!({
                "grant_id": grant_id,
                "new_hash": new_hash,
                "content": content,
            }),
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(replay.code, "E_WRITE_GRANT_CONSUMED");

        // 旧 base_hash 再申请 grant → E_WRITE_STALE_BASE
        let stale =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap_err();
        assert_eq!(stale.code, "E_WRITE_STALE_BASE");

        let _ = std::fs::remove_dir_all(&root);
    }

    /// backlog §38.3 S3-02/03/05/06/08/09 daemon 侧 fail-closed 与审计回放矩阵。
    #[test]
    fn test_write_governance_fail_closed_matrix() {
        let root = std::env::temp_dir().join(format!(
            "cw_write_gov_matrix_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&root).unwrap();
        let db_path = root.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let workspace = registry
            .register_workspace(1000, root.to_str().unwrap(), root.to_str().unwrap(), "", "", "")
            .unwrap();
        let instance_id = workspace["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let peer = PeerCredential::new_unix(1000, 1000, 1);
        let grant_params = json!({
            "workspace_instance_id": instance_id,
            "task_id": "T-1",
            "step_id": "S-1",
            "path": "src/a.py",
            "base_hash": "",
            "request_id": "rm",
        });
        let content = "line1\nline2\n";
        let new_hash = sha256_hex(content.as_bytes());
        let conn = rusqlite::Connection::open(&db_path).unwrap();

        // S3-06 跨 principal → PRINCIPAL_MISMATCH，索引不变
        let grant =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap();
        let grant_id = grant["grant_id"].as_str().unwrap().to_string();
        let real_principal: String = conn
            .query_row(
                "SELECT principal_id FROM write_grants WHERE grant_id = ?1",
                rusqlite::params![grant_id],
                |row| row.get(0),
            )
            .unwrap();
        conn.execute(
            "UPDATE write_grants SET principal_id = 'intruder' WHERE grant_id = ?1",
            rusqlite::params![grant_id],
        )
        .unwrap();
        let mismatch = handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": grant_id, "new_hash": new_hash, "content": content,
                    "path": "src/a.py"}),
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(mismatch.code, "E_WRITE_GRANT_PRINCIPAL_MISMATCH");
        let indexed: Option<String> = conn
            .query_row(
                "SELECT current_content_hash FROM file_instances WHERE rel_path = 'src/a.py'",
                [],
                |row| row.get(0),
            )
            .optional()
            .unwrap();
        assert!(indexed.is_none(), "拒绝路径不得产生索引副作用");

        // S3-05 commit path 越出 grant → PATH_FORBIDDEN
        conn.execute(
            "UPDATE write_grants SET principal_id = ?1 WHERE grant_id = ?2",
            rusqlite::params![real_principal, grant_id],
        )
        .unwrap();
        let forbidden = handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": grant_id, "new_hash": new_hash, "content": content,
                    "path": "src/other.py"}),
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(forbidden.code, "E_WRITE_PATH_FORBIDDEN");

        // S3-03 过期 → GRANT_EXPIRED
        conn.execute(
            "UPDATE write_grants SET expires_at = 1.0 WHERE grant_id = ?1",
            rusqlite::params![grant_id],
        )
        .unwrap();
        let expired = handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": grant_id, "new_hash": new_hash, "content": content,
                    "path": "src/a.py"}),
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(expired.code, "E_WRITE_GRANT_EXPIRED");

        // S3-01 成功一次 → 索引推进
        let ok_grant =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap();
        let ok_gid = ok_grant["grant_id"].as_str().unwrap().to_string();
        handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": ok_gid, "new_hash": new_hash, "content": content,
                    "path": "src/a.py"}),
            Some(&db_path),
            None,
        )
        .unwrap();
        let indexed_hash: String = conn
            .query_row(
                "SELECT current_content_hash FROM file_instances WHERE rel_path = 'src/a.py'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(indexed_hash, new_hash);

        // S3-02 grant 后索引被外部推进（非本次 grant 消费）→ BASE_HASH_MISMATCH
        let second = handle_write_grant(
            &registry,
            &peer,
            &json!({
                "workspace_instance_id": instance_id,
                "task_id": "T-1",
                "step_id": "S-1",
                "path": "src/a.py",
                "base_hash": new_hash,
                "request_id": "rm2",
            }),
            Some(&db_path),
            None,
        )
        .unwrap();
        let second_gid = second["grant_id"].as_str().unwrap().to_string();
        let external_hash = sha256_hex(b"external-writer");
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, \
             first_seen_at) VALUES (?1, 'python', 1, 1.0)",
            rusqlite::params![external_hash],
        )
        .unwrap();
        conn.execute(
            "UPDATE file_instances SET current_content_hash = ?1 WHERE rel_path = 'src/a.py'",
            rusqlite::params![external_hash],
        )
        .unwrap();
        let base_mismatch = handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": second_gid, "new_hash": sha256_hex(b"v2"),
                    "content": "v2", "path": "src/a.py"}),
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(base_mismatch.code, "E_WRITE_BASE_HASH_MISMATCH");
        let after: String = conn
            .query_row(
                "SELECT current_content_hash FROM file_instances WHERE rel_path = 'src/a.py'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(after, external_hash, "失败 commit 必须保持索引不变");

        // S3-08 task closed → TASK_FORBIDDEN（真实 TaskCollabStore 注入）
        let store = TaskCollabStore::from_connection(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        {
            let tconn = store.conn.lock().unwrap();
            tconn
                .execute(
                    "INSERT INTO tasks (id, title, description, creator, status, created_at, \
                     updated_at) VALUES ('T-CLOSED', 't', '', 'agent', 'closed', 1.0, 1.0)",
                    [],
                )
                .unwrap();
            tconn
                .execute(
                    "INSERT INTO task_steps (id, task_id, step_index, action, status, created_at) \
                     VALUES ('S-CLOSED', 'T-CLOSED', 0, 'implement', 'pending', 1.0)",
                    [],
                )
                .unwrap();
        }
        let task_forbidden = handle_write_grant(
            &registry,
            &peer,
            &json!({
                "workspace_instance_id": instance_id,
                "task_id": "T-CLOSED",
                "step_id": "S-CLOSED",
                "path": "src/b.py",
                "base_hash": "",
            }),
            Some(&db_path),
            Some(&store),
        )
        .unwrap_err();
        assert_eq!(task_forbidden.code, "E_WRITE_TASK_FORBIDDEN");

        // S3-09 审计回放：grant/commit/拒绝全部入链且无缺口
        let verify = crate::cli::security::verify_audit_chain(
            &conn,
            "remote_write_events",
            500,
        )
        .unwrap();
        assert!(
            verify.total_count >= 6,
            "审计应覆盖 grant/commit/拒绝，实际 {}",
            verify.total_count
        );
        assert_eq!(
            verify.broken_count, 0,
            "审计链不得有缺口: {:?}",
            verify.broken_records
        );
        assert_eq!(verify.verified_count, verify.total_count);

        drop(conn);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn test_write_path_normalization_rejects_escape() {
        assert_eq!(normalize_write_rel_path(" src/a.py ").unwrap(), "src/a.py");
        assert_eq!(normalize_write_rel_path("./src//a.py").unwrap(), "src/a.py");
        for bad in ["../a.py", "src/../../a.py", "/abs/a.py", "C:/a.py", "//host/share/a.py", ""] {
            assert!(
                normalize_write_rel_path(bad).is_err(),
                "path 应被拒绝: {bad}"
            );
        }
        assert!(is_lower_sha256_hex(&"a".repeat(64)));
        assert!(!is_lower_sha256_hex(&"A".repeat(64)));
        assert!(!is_lower_sha256_hex("short"));
    }

    /// review F2：principal/workspace/task 归属 fail-closed。
    #[test]
    fn test_write_grant_scope_fail_closed() {
        use crate::daemon::peercred::{FixedStr, PrincipalIdentity};

        let root = std::env::temp_dir().join(format!(
            "cw_write_grant_scope_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&root).unwrap();
        let db_path = root.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let workspace = registry
            .register_workspace(1000, root.to_str().unwrap(), root.to_str().unwrap(), "", "", "")
            .unwrap();
        let instance_id = workspace["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let peer = PeerCredential::new_unix(1000, 1000, 1);
        let grant_params = json!({
            "workspace_instance_id": instance_id,
            "task_id": "T-1",
            "step_id": "S-1",
            "path": "src/a.py",
            "base_hash": "",
            "request_id": "scope",
        });

        // (a) 已认证 principal 未被授权该 workspace → 拒绝且不签 grant
        let restricted = peer.clone().with_principal(PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str("pr_agent").unwrap(),
            kind: FixedStr::<16>::from_str("agent").unwrap(),
            allowed_workspace_ids: FixedStr::<512>::from_str("999").unwrap(),
            ..Default::default()
        });
        let denied = handle_write_grant(
            &registry,
            &restricted,
            &grant_params,
            Some(&db_path),
            None,
        )
        .unwrap_err();
        assert_eq!(denied.code, "E_PRINCIPAL_WORKSPACE_FORBIDDEN");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        let grants: i64 = conn
            .query_row("SELECT COUNT(*) FROM write_grants", [], |r| r.get(0))
            .unwrap();
        assert_eq!(grants, 0, "拒绝的 principal 不得产生 grant");
        let (reason, audit_db_ws): (String, i64) = conn
            .query_row(
                "SELECT reason_code, db_workspace_id FROM write_audit_events \
                 WHERE outcome = 'deny' ORDER BY event_id DESC LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(reason, "E_PRINCIPAL_WORKSPACE_FORBIDDEN");
        let capture_ws: i64 = conn
            .query_row(
                "SELECT id FROM workspaces ORDER BY id LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(audit_db_ws, capture_ws, "拒绝审计也应绑定 codegraph workspace id");

        // (b) task 绑定 workspace 与请求不一致 → E_WRITE_TASK_FORBIDDEN
        let store = TaskCollabStore::from_connection(
            rusqlite::Connection::open_in_memory().unwrap(),
        )
        .unwrap();
        {
            let tconn = store.conn.lock().unwrap();
            tconn
                .execute(
                    "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
                     VALUES (1, 'test-ws', '/tmp/test-ws', 1.0, 1)",
                    [],
                )
                .unwrap();
            tconn
                .execute(
                    "INSERT OR IGNORE INTO tasks (id, title, description, creator, status, \
                     created_at, updated_at, parent_id) \
                     VALUES ('T-1', 'test task', '', 'test', 'open', 1.0, 1.0, '')",
                    [],
                )
                .unwrap();
            tconn
                .execute(
                    "INSERT OR IGNORE INTO task_steps (id, task_id, step_index, action, status, \
                     created_at) VALUES ('S-1', 'T-1', 1, 'edit', 'pending', 1.0)",
                    [],
                )
                .unwrap();
            tconn
                .execute(
                    "INSERT INTO workspace_authority_captures(workspace_capture_id, workspace_id, \
                     capture_revision, supersedes_capture_id, daemon_workspace_id, \
                     workspace_instance_id, capture_canonicalization_version, \
                     capture_canonicalization_rules_hash, registry_identity_payload_json, \
                     registry_identity_hash, workspace_manifest_payload_json, \
                     workspace_manifest_hash, client_view_root_hash, host_real_root_hash, \
                     created_by, authoritative_created_at) \
                     VALUES ('cap-other', 1, 1, NULL, 0, 'inst-other', 'workspace-capture-c14n/v1', \
                     'rules-hash', '{}', 'identity-hash', '{}', 'manifest-hash', 'root-hash', \
                     'root-hash', 'test', 1.0)",
                    [],
                )
                .unwrap();
            tconn
                .execute(
                    "INSERT INTO task_workspace_bindings(task_id, workspace_id, \
                     workspace_binding_id, workspace_capture_id, created_by, \
                     authoritative_created_at) \
                     VALUES ('T-1', 1, 'tb-other', 'cap-other', 'test', 1.0)",
                    [],
                )
                .unwrap();
        }
        let mismatch = handle_write_grant(
            &registry,
            &peer,
            &grant_params,
            Some(&db_path),
            Some(&store),
        )
        .unwrap_err();
        assert_eq!(mismatch.code, "E_WRITE_TASK_FORBIDDEN");

        // (c) task 绑定与请求一致 → 放行
        {
            let tconn = store.conn.lock().unwrap();
            tconn
                .execute(
                    "UPDATE workspace_authority_captures SET workspace_instance_id = ?1 \
                     WHERE workspace_capture_id = 'cap-other'",
                    rusqlite::params![instance_id],
                )
                .unwrap();
        }
        let granted = handle_write_grant(
            &registry,
            &peer,
            &grant_params,
            Some(&db_path),
            Some(&store),
        )
        .unwrap();
        assert_eq!(granted["ok"], true);

        drop(conn);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// review F3：grant/审计的 db_workspace_id 必须与 file_instances 的 workspace id 对齐。
    #[test]
    fn test_write_governance_db_workspace_id_correlation() {
        let root = std::env::temp_dir().join(format!(
            "cw_write_gov_corr_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        std::fs::create_dir_all(&root).unwrap();
        let db_path = root.join("codegraph.db");
        crate::storage::initialize_or_migrate(&db_path, crate::storage::SCHEMA_VERSION).unwrap();
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let workspace = registry
            .register_workspace(1000, root.to_str().unwrap(), root.to_str().unwrap(), "", "", "")
            .unwrap();
        let instance_id = workspace["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let peer = PeerCredential::new_unix(1000, 1000, 1);
        let content = "body\n";
        let new_hash = sha256_hex(content.as_bytes());
        let grant_params = json!({
            "workspace_instance_id": instance_id,
            "task_id": "T-1",
            "step_id": "S-1",
            "path": "src/a.py",
            "base_hash": "",
        });
        let grant =
            handle_write_grant(&registry, &peer, &grant_params, Some(&db_path), None).unwrap();
        let grant_id = grant["grant_id"].as_str().unwrap().to_string();
        handle_write_commit(
            &registry,
            &peer,
            &json!({"grant_id": grant_id, "new_hash": new_hash, "content": content,
                    "path": "src/a.py"}),
            Some(&db_path),
            None,
        )
        .unwrap();

        let conn = rusqlite::Connection::open(&db_path).unwrap();
        let index_ws: i64 = conn
            .query_row(
                "SELECT workspace_id FROM file_instances WHERE rel_path = 'src/a.py'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let grant_ws: i64 = conn
            .query_row(
                "SELECT db_workspace_id FROM write_grants WHERE grant_id = ?1",
                rusqlite::params![grant_id],
                |r| r.get(0),
            )
            .unwrap();
        let commit_audit_ws: i64 = conn
            .query_row(
                "SELECT db_workspace_id FROM write_audit_events \
                 WHERE event_type = 'commit' AND outcome = 'allow' \
                 ORDER BY event_id DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(grant_ws, index_ws, "grant 必须记录 codegraph workspace id");
        assert_eq!(
            commit_audit_ws, index_ws,
            "commit 审计必须与 file_instances 同一 workspace id 空间"
        );

        // review F5：审计事件必须回填链行号，且 event 与 audit_chain 一一对应。
        let (record_id, chain_id): (String, i64) = conn
            .query_row(
                "SELECT e.audit_record_id, c.id \
                   FROM write_audit_events e \
                   JOIN audit_chain c ON c.id = CAST(e.audit_record_id AS INTEGER) \
                  WHERE e.event_type = 'commit' AND e.outcome = 'allow' \
                  ORDER BY e.event_id DESC LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(
            !record_id.is_empty(),
            "review F5: audit_record_id 必须回填 audit_chain 行号"
        );
        assert_eq!(record_id, chain_id.to_string());
        let (events, chains): (i64, i64) = conn
            .query_row(
                "SELECT (SELECT COUNT(*) FROM write_audit_events), \
                        (SELECT COUNT(*) FROM audit_chain WHERE table_name = ?1)",
                rusqlite::params![WRITE_AUDIT_CHAIN_TABLE],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            events, chains,
            "review F5: write_audit_events 与 audit_chain 必须一一对应，不得有链缺口"
        );

        drop(conn);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// review F6：写路径 symlink containment（根内 symlink 指向根外必须拒绝）。
    #[test]
    fn test_write_containment_rejects_symlink_escape() {
        let base = std::env::temp_dir().join(format!(
            "cw_write_sym_{}_{}",
            std::process::id(),
            now_ts_secs() as u64
        ));
        let root = base.join("root");
        let outside = base.join("outside");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        let link = root.join("link");
        #[cfg(unix)]
        let created = std::os::unix::fs::symlink(&outside, &link).is_ok();
        #[cfg(windows)]
        let created = std::os::windows::fs::symlink_dir(&outside, &link).is_ok();
        // 无 symlink 权限的环境（如未开开发者模式的 Windows）跳过逃逸断言。
        if created {
            let err = check_write_containment(&root, "link/evil.py").unwrap_err();
            assert_eq!(err.code, "E_WRITE_PATH_ESCAPE");
        }
        assert!(
            check_write_containment(&root, "src/ok.py").is_ok(),
            "根内普通路径（含尚不存在的前缀）必须放行"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}
