//! Phase 6-2: MinHash/LSH clone detection 核心计算
//!
//! 迁移自 Python `db/db_clone_detection.py`，对齐以下关键设计：
//! - **哈希函数**：FNV-1a 32 位（确定性，跨进程稳定，替代 Python 内置 hash()）
//! - **MinHash 系数**：SHA-256 派生 128 个 (a, b)，a 强制奇数（与 2^32 互质），
//!   截断 32 位（避免 numpy uint64 乘法溢出）
//! - **签名公式**：`h_i(x) = (a_i * x + b_i) mod 2^32`
//! - **LSH 参数**：num_perm=128, num_bands=8, rows_per_band=16
//!   阈值 t ≈ (1/num_bands)^(1/rows_per_band) ≈ 0.88
//! - **桶 key 格式**：`"b{band_idx}:{h0}:{h1}:...:{h_{r-1}}"`（与 Python 完全一致）
//! - **大桶保护**：桶中符号数 > MAX_BUCKET_SIZE 时跳过（避免常见模式导致 O(n²)）
//!
//! 性能优化：
//! - rustc-hash（FxHashMap）替代 Python dict
//! - rayon 并行化 MinHash 签名生成（跨符号 par_iter）
//! - 签名矩阵紧凑布局：`Vec<u64>` 连续存储
//! - LSH 分桶批处理：所有符号的签名一次性入桶

use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::PyList;
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use sha2::{Digest, Sha256};

// ============================================
// 常量（与 Python db_clone_detection.py 完全对齐）
// ============================================

/// MinHash 签名长度（哈希函数数量）
pub const NUM_PERM: usize = 128;

/// LSH 带数
pub const NUM_BANDS: usize = 8;

/// 每带行数
pub const ROWS_PER_BAND: usize = 16;

/// 大桶保护阈值：桶中符号数超过此值时跳过该桶
pub const MAX_BUCKET_SIZE: usize = 200;

/// 暴力比较阈值：代表符号数小于此值时直接全配对，不走 LSH
pub const BRUTEFORCE_THRESHOLD: usize = 500;

/// 空集合的签名填充值（与 Python 一致：0xFFFFFFFF）
pub const EMPTY_SIG_FILL: u64 = 0xFFFFFFFF;

// ============================================
// FNV-1a 32 位哈希（与 Python _fnv1a_32 完全一致）
// ============================================

const FNV_OFFSET_BASIS: u32 = 0x811C9DC5;
const FNV_PRIME: u32 = 0x01000193;

/// FNV-1a 32 位哈希（确定性，跨进程稳定）
///
/// 与 Python `_fnv1a_32` 逐字节对齐：
/// ```python
/// h = 0x811C9DC5
/// for b in data:
///     h ^= b
///     h = (h * 0x01000193) & 0xFFFFFFFF
/// ```
#[inline]
fn fnv1a_32(data: &[u8]) -> u32 {
    let mut h: u32 = FNV_OFFSET_BASIS;
    for &b in data {
        h ^= b as u32;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// 计算 token 的稳定哈希（FNV-1a）
///
/// 与 Python `_stable_token_hash` 对齐：
/// - str：直接编码为 UTF-8 后 FNV-1a
/// - tuple（3-gram shingle）：用 `\x1f` 分隔符拼接为字符串再 hash
#[inline]
fn stable_token_hash_str(token: &str) -> u32 {
    fnv1a_32(token.as_bytes())
}

/// 计算 3-gram shingle 的稳定哈希
///
/// 与 Python 对齐：`token = "\x1f".join(str(t) for t in tuple)`
pub fn stable_token_hash_shingle(t0: &str, t1: &str, t2: &str) -> u32 {
    // 拼接为 "t0\x1ft1\x1ft2"，再 FNV-1a
    // 比 format! 更高效：直接写入字节数组
    let total_len = t0.len() + 1 + t1.len() + 1 + t2.len();
    let mut buf = Vec::with_capacity(total_len);
    buf.extend_from_slice(t0.as_bytes());
    buf.push(0x1f);
    buf.extend_from_slice(t1.as_bytes());
    buf.push(0x1f);
    buf.extend_from_slice(t2.as_bytes());
    fnv1a_32(&buf)
}

// ============================================
// MinHash 签名系数（与 Python _HASH_COEFFS 完全对齐）
// ============================================

/// MinHash 哈希族系数：h_i(x) = (a_i * x + b_i) mod 2^32
///
/// 与 Python 对齐：
/// ```python
/// for i in range(128):
///     h = sha256(f"callwarden_minhash_perm_{i}").digest()
///     a = (int.from_bytes(h[:4], "little") | 1) & 0xFFFFFFFF  # 32 位奇数
///     b = int.from_bytes(h[4:8], "little") & 0xFFFFFFFF       # 32 位
/// ```
///
/// 使用 `OnceLock` 延迟初始化（避免 const 上下文计算 SHA-256）。
static HASH_COEFFS: std::sync::OnceLock<Vec<(u32, u32)>> = std::sync::OnceLock::new();

/// 获取 MinHash 系数（延迟初始化，线程安全）
pub fn hash_coeffs() -> &'static [(u32, u32)] {
    HASH_COEFFS.get_or_init(|| {
        (0..NUM_PERM)
            .map(|i| {
                let seed = format!("callwarden_minhash_perm_{}", i);
                let mut hasher = Sha256::new();
                hasher.update(seed.as_bytes());
                let h = hasher.finalize();
                let a_bytes = &h[..4];
                let b_bytes = &h[4..8];
                // int.from_bytes(h[:4], "little") — 小端序
                let a_raw = u32::from_le_bytes([
                    a_bytes[0], a_bytes[1], a_bytes[2], a_bytes[3],
                ]);
                let b_raw = u32::from_le_bytes([
                    b_bytes[0], b_bytes[1], b_bytes[2], b_bytes[3],
                ]);
                // a 强制奇数（与 2^32 互质），截断 32 位
                let a = a_raw | 1;
                let b = b_raw;
                (a, b)
            })
            .collect()
    })
}

// ============================================
// MinHash 签名生成
// ============================================

/// 计算 token 集合的 MinHash 签名
///
/// 与 Python `_minhash_signature` 对齐：
/// - 空 token_set → 返回全 `EMPTY_SIG_FILL`（0xFFFFFFFF）
/// - 每个 token 先 FNV-1a 得 base_hash（u32）
/// - 128 个 perm 通过 `(a*base + b) mod 2^32` 计算所有 hash，再取 min
///
/// 返回 `Vec<u64>`（与 Python tuple[int, ...] 对齐，但用 u64 便于 PyO3 转换）。
/// 注意：实际值 ≤ 2^32-1，高 32 位为 0。
pub fn minhash_signature(token_hashes: &[u32]) -> Vec<u64> {
    let coeffs = hash_coeffs();

    if token_hashes.is_empty() {
        // 空集合：全 0xFFFFFFFF（与 Python 一致）
        return vec![EMPTY_SIG_FILL; NUM_PERM];
    }

    // 对每个 perm 取所有 token hash 的 min
    // 等价于 Python numpy:
    //   all_hashes = (A[:num_perm, None] * base_hashes[None, :] + B[:num_perm, None]) & MASK_32
    //   signature = all_hashes.min(axis=1)
    let mut sig = vec![u32::MAX; NUM_PERM];
    for &base in token_hashes {
        for (i, &(a, b)) in coeffs.iter().enumerate() {
            // (a * base + b) mod 2^32（u32 wrapping 语义天然 mod 2^32）
            let h = a.wrapping_mul(base).wrapping_add(b);
            if h < sig[i] {
                sig[i] = h;
            }
        }
    }

    // 转 u64 便于 PyO3（Python 端 int 无符号位限制）
    sig.into_iter().map(|x| x as u64).collect()
}

// ============================================
// LSH 分桶
// ============================================

/// 将 MinHash 签名分桶
///
/// 与 Python `_lsh_buckets` 完全对齐：
/// ```python
/// for i in range(num_bands):
///     start = i * rows_per_band
///     end = start + rows_per_band
///     band = signature[start:end]
///     bucket_key = f"b{i}:" + ":".join(str(h) for h in band)
///     buckets.append(bucket_key)
/// ```
///
/// 返回 `Vec<String>`（num_bands 个桶 key）
pub fn lsh_buckets(signature: &[u64], num_bands: usize, rows_per_band: usize) -> Vec<String> {
    let mut buckets = Vec::with_capacity(num_bands);
    for i in 0..num_bands {
        let start = i * rows_per_band;
        let end = start + rows_per_band;
        if end > signature.len() {
            // 签名长度不足，跳过剩余 band（与 Python 不发生，因签名固定 128）
            break;
        }
        let band = &signature[start..end];
        // 拼接为 "b{i}:{h0}:{h1}:...:{h_{r-1}}"
        let mut key = String::with_capacity(8 + rows_per_band * 12);
        key.push_str(&format!("b{}:", i));
        for (j, h) in band.iter().enumerate() {
            if j > 0 {
                key.push(':');
            }
            // 与 Python str(int) 对齐：u64 → 十进制字符串
            key.push_str(&h.to_string());
        }
        buckets.push(key);
    }
    buckets
}

// ============================================
// Jaccard 相似度
// ============================================

/// 计算两个集合的 Jaccard 相似度
///
/// 与 Python `_jaccard_similarity` 对齐：
/// - 空集合 → 0.0
/// - |A∩B| / |A∪B|
pub fn jaccard_similarity<T: Eq + std::hash::Hash + Clone>(
    set_a: &HashSet<T>,
    set_b: &HashSet<T>,
) -> f64 {
    if set_a.is_empty() || set_b.is_empty() {
        return 0.0;
    }
    let intersection = set_a.intersection(set_b).count();
    let union = set_a.union(set_b).count();
    if union == 0 {
        return 0.0;
    }
    intersection as f64 / union as f64
}

// ============================================
// PyO3 暴露
// ============================================

/// 计算 token 字符串列表的 MinHash 签名（Python 入口）
///
/// 与 Python `_minhash_signature(token_set, num_perm=128)` 对齐。
/// Python 侧传入 token 字符串列表（或 3-gram tuple 列表），Rust 内部：
/// 1. 对每个 token 计算 FNV-1a 稳定 hash（u32）
/// 2. 128 个 perm 通过 `(a*base + b) mod 2^32` 计算，取 min
///
/// 返回：长度 128 的 `List[int]`（每个元素 ≤ 2^32-1）
#[pyfunction]
#[pyo3(signature = (tokens, num_perm=128))]
pub fn py_minhash_signature<'py>(
    py: Python<'py>,
    tokens: &Bound<'py, PyList>,
    num_perm: usize,
) -> PyResult<Bound<'py, PyList>> {
    if num_perm != NUM_PERM {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "num_perm must be {} (got {})", NUM_PERM, num_perm
        )));
    }

    // 收集 token hash（支持 str 和 tuple 两种形式，与 Python _stable_token_hash 对齐）
    let mut token_hashes: Vec<u32> = Vec::with_capacity(tokens.len());
    for item in tokens.iter() {
        if let Ok(s) = item.extract::<String>() {
            // str token：直接 FNV-1a
            token_hashes.push(stable_token_hash_str(&s));
        } else {
            // tuple token（3-gram shingle）：用 \x1f 拼接
            // 与 Python: token = "\x1f".join(str(t) for t in token) 对齐
            let tup_result = item.extract::<(String, String, String)>();
            match tup_result {
                Ok((t0, t1, t2)) => {
                    token_hashes.push(stable_token_hash_shingle(&t0, &t1, &t2));
                }
                Err(_) => {
                    // 其他类型：转为字符串再 hash
                    let s = item.to_string();
                    token_hashes.push(stable_token_hash_str(&s));
                }
            }
        }
    }

    let sig = minhash_signature(&token_hashes);

    // 返回 List[int]（与 Python tuple[int, ...] 等价）
    PyList::new(py, sig)
}

/// 将 MinHash 签名分桶（Python 入口）
///
/// 与 Python `_lsh_buckets(signature, num_bands=8, rows_per_band=16)` 对齐。
/// Python 侧传入签名 list（或 tuple），Rust 返回桶 key 列表。
#[pyfunction]
#[pyo3(signature = (signature, num_bands=8, rows_per_band=16))]
pub fn py_lsh_buckets<'py>(
    py: Python<'py>,
    signature: &Bound<'py, PyList>,
    num_bands: usize,
    rows_per_band: usize,
) -> PyResult<Bound<'py, PyList>> {
    let mut sig: Vec<u64> = Vec::with_capacity(signature.len());
    for item in signature.iter() {
        sig.push(item.extract::<u64>()?);
    }

    let buckets = lsh_buckets(&sig, num_bands, rows_per_band);

    PyList::new(py, buckets)
}

/// 批量计算多个符号的 MinHash 签名（rayon 并行化）
///
/// 输入：`List[List[str]]`（每个元素是一个符号的 token 列表）
/// 输出：`List[List[int]]`（每个元素是对应符号的 128 长度签名）
///
/// 性能优化：rayon `par_iter` 跨符号并行，适合 10k+ 符号场景。
#[pyfunction]
pub fn py_batch_minhash_signatures<'py>(
    py: Python<'py>,
    token_lists: Vec<Vec<String>>,
) -> PyResult<Bound<'py, PyList>> {
    // rayon 并行计算每个符号的签名
    let signatures: Vec<Vec<u64>> = token_lists
        .par_iter()
        .map(|tokens| {
            let hashes: Vec<u32> = tokens.iter()
                .map(|t| stable_token_hash_str(t))
                .collect();
            minhash_signature(&hashes)
        })
        .collect();

    // 转为 List[List[int]]
    let inner_lists: Vec<Bound<'py, PyList>> = signatures.iter()
        .map(|sig| PyList::new(py, sig.clone()).unwrap())
        .collect();
    PyList::new(py, inner_lists)
}

/// 批量 LSH 分桶（返回候选对索引列表）
///
/// 与 Python `_detect_clone_groups_core` 中的 LSH 分桶逻辑对齐：
/// 1. 对每个符号的签名分桶
/// 2. 同桶的符号组成候选对
/// 3. 大桶保护：桶中符号数 > MAX_BUCKET_SIZE 时跳过
///
/// 输入：signatures — `List[List[int]]`（每个符号的 MinHash 签名）
/// 输出：候选对列表 `List[Tuple[int, int]]`（符号索引对，a < b）
#[pyfunction]
#[pyo3(signature = (signatures, num_bands=8, rows_per_band=16))]
pub fn py_lsh_candidate_pairs<'py>(
    py: Python<'py>,
    signatures: Vec<Vec<u64>>,
    num_bands: usize,
    rows_per_band: usize,
) -> PyResult<Bound<'py, PyList>> {
    let num_symbols = signatures.len();

    // 若符号数小于 BRUTEFORCE_THRESHOLD，直接全配对（与 Python 对齐）
    if num_symbols < BRUTEFORCE_THRESHOLD {
        let pairs: Vec<(usize, usize)> = (0..num_symbols)
            .flat_map(|i| (i + 1..num_symbols).map(move |j| (i, j)))
            .collect();
        // 转为 List[Tuple[int, int]]
        let py_pairs: Vec<Bound<'py, pyo3::types::PyTuple>> = pairs.iter()
            .map(|(a, b)| {
                pyo3::types::PyTuple::new(py, [*a, *b]).unwrap()
            })
            .collect();
        return PyList::new(py, py_pairs);
    }

    // LSH 分桶：bucket_key → 符号索引列表
    let mut buckets: FxHashMap<String, Vec<usize>> = FxHashMap::default();
    for (idx, sig) in signatures.iter().enumerate() {
        for bucket_key in lsh_buckets(sig, num_bands, rows_per_band) {
            buckets.entry(bucket_key).or_default().push(idx);
        }
    }

    // 生成候选对（大桶保护）
    let mut candidate_pairs: HashSet<(usize, usize)> = HashSet::new();
    for indices in buckets.values() {
        if indices.len() < 2 {
            continue;
        }
        if indices.len() > MAX_BUCKET_SIZE {
            continue;
        }
        for i in 0..indices.len() {
            for j in (i + 1)..indices.len() {
                let a = indices[i];
                let b = indices[j];
                let pair = if a < b { (a, b) } else { (b, a) };
                candidate_pairs.insert(pair);
            }
        }
    }

    let pairs: Vec<(usize, usize)> = candidate_pairs.into_iter().collect();
    // 转为 List[Tuple[int, int]]
    let py_pairs: Vec<Bound<'py, pyo3::types::PyTuple>> = pairs.iter()
        .map(|(a, b)| pyo3::types::PyTuple::new(py, [*a, *b]).unwrap())
        .collect();
    PyList::new(py, py_pairs)
}

/// 返回 MinHash/LSH 参数（供 Python 端验证对齐）
#[pyfunction]
pub fn clone_detection_params<'py>(py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("num_perm", NUM_PERM)?;
    dict.set_item("num_bands", NUM_BANDS)?;
    dict.set_item("rows_per_band", ROWS_PER_BAND)?;
    dict.set_item("max_bucket_size", MAX_BUCKET_SIZE)?;
    dict.set_item("bruteforce_threshold", BRUTEFORCE_THRESHOLD)?;
    dict.set_item("empty_sig_fill", EMPTY_SIG_FILL)?;
    dict.set_item("hash_family", "fnv1a_32")?;
    dict.set_item("coeff_seed", "callwarden_minhash_perm_{i}")?;
    Ok(dict)
}

// ============================================
// detect_clones_core 端到端实现
// ============================================

/// 克隆组（与 Python `_detect_clone_groups_core` 返回结构对齐）
#[derive(Clone, Debug)]
pub struct CloneGroupRaw {
    pub clone_type: u8, // 1 / 2 / 3
    pub token_hash: String,
    pub similarity: f64,
    pub members: Vec<i64>, // symbol IDs，第一个为 representative
}

/// 检测统计信息（与 Python stats dict 对齐）
#[derive(Clone, Debug, Default)]
pub struct CloneStats {
    pub scanned_symbols: usize,
    pub skipped_symbols: usize,
    pub total_groups: usize,
    pub type1_groups: usize,
    pub type2_groups: usize,
    pub type3_groups: usize,
}

/// 符号元数据（Rust 内部结构，由 py_detect_clones_core 组装）
#[derive(Clone, Debug)]
struct SymMeta {
    id: i64,
    /// content_hash（来自 DB 的 symbol_hash 字段）
    symbol_hash: String,
    /// 归一化 token 序列的 SHA-256[:16]
    token_hash: String,
    /// 3-gram shingle 集合（用 \x1f 拼接为字符串，与 Python tuple 表示对齐）
    token_set: HashSet<String>,
    /// MinHash 签名（同 token_hash 的符号共享）
    minhash_sig: Vec<u64>,
}

/// 将归一化 token 列表转为 3-gram shingle 集合
///
/// 与 Python 对齐：
/// ```python
/// if len(tokens) >= 3:
///     token_set = set(zip(tokens, tokens[1:], tokens[2:]))
/// else:
///     token_set = set(tokens)
/// ```
///
/// 3-gram tuple (a, b, c) 在 Rust 侧用 "a\x1fb\x1fc" 字符串表示，
/// 与 `stable_token_hash_shingle` 的拼接方式一致。
fn build_token_set(tokens: &[String]) -> HashSet<String> {
    let mut set = HashSet::with_capacity(tokens.len());
    if tokens.len() >= 3 {
        for i in 0..tokens.len() - 2 {
            let mut buf = String::with_capacity(tokens[i].len() + 1 + tokens[i + 1].len() + 1 + tokens[i + 2].len());
            buf.push_str(&tokens[i]);
            buf.push('\x1f');
            buf.push_str(&tokens[i + 1]);
            buf.push('\x1f');
            buf.push_str(&tokens[i + 2]);
            set.insert(buf);
        }
    } else {
        for t in tokens {
            set.insert(t.clone());
        }
    }
    set
}

/// MinHash 估算的 Jaccard 相似度
///
/// 两个签名在相同位置的比例 ≈ Jaccard 相似度。
/// 与 Python 实现对齐（若 Python 有此函数）。
pub fn minhash_jaccard_estimate(sig_a: &[u64], sig_b: &[u64]) -> f64 {
    if sig_a.is_empty() || sig_b.is_empty() || sig_a.len() != sig_b.len() {
        return 0.0;
    }
    let matches = sig_a.iter().zip(sig_b.iter()).filter(|(a, b)| a == b).count();
    matches as f64 / sig_a.len() as f64
}

/// 端到端 clone detection 核心（与 Python `_detect_clone_groups_core` 对齐）
///
/// 输入：每个符号的 (id, symbol_hash, token_hash, normalized_tokens)
/// 输出：(克隆组列表, 统计信息)
///
/// 算法步骤：
/// 1. 构建每个符号的 token_set（3-gram）和 MinHash 签名（按 token_hash 缓存）
/// 2. 按 symbol_hash 分组 → Type-1（完全相同内容）
/// 3. 按 token_hash 分组 → Type-2（重命名克隆）
/// 4. LSH + Jaccard 验证 → Type-3（微调克隆）
pub fn detect_clones_core(
    symbols: Vec<(i64, String, String, Vec<String>)>,
    similarity_threshold: f64,
) -> (Vec<CloneGroupRaw>, CloneStats) {
    let scanned = symbols.len();
    let mut skipped = 0usize;

    // 1. 构建 SymMeta 列表
    let mut sym_meta: Vec<SymMeta> = Vec::with_capacity(symbols.len());
    let mut minhash_cache: FxHashMap<String, Vec<u64>> = FxHashMap::default();

    for (id, symbol_hash, token_hash, tokens) in symbols {
        if tokens.is_empty() {
            skipped += 1;
            continue;
        }
        let token_set = build_token_set(&tokens);
        let minhash_sig = if let Some(sig) = minhash_cache.get(&token_hash) {
            sig.clone()
        } else {
            // 将 token_set 转为 token hash 列表
            let token_hashes: Vec<u32> = token_set
                .iter()
                .map(|s| stable_token_hash_str(s))
                .collect();
            let sig = minhash_signature(&token_hashes);
            minhash_cache.insert(token_hash.clone(), sig.clone());
            sig
        };
        sym_meta.push(SymMeta {
            id,
            symbol_hash,
            token_hash,
            token_set,
            minhash_sig,
        });
    }

    // 2. 按 token_hash 和 symbol_hash 分组
    let mut by_token_hash: FxHashMap<String, Vec<usize>> = FxHashMap::default();
    let mut by_content_hash: FxHashMap<String, Vec<usize>> = FxHashMap::default();
    for (idx, m) in sym_meta.iter().enumerate() {
        by_token_hash.entry(m.token_hash.clone()).or_default().push(idx);
        by_content_hash.entry(m.symbol_hash.clone()).or_default().push(idx);
    }

    let mut groups: Vec<CloneGroupRaw> = Vec::new();

    // 3. Type-1：content_hash 相同（完全相同内容）
    for (_, group_indices) in &by_content_hash {
        if group_indices.len() < 2 {
            continue;
        }
        let members: Vec<i64> = group_indices.iter().map(|&i| sym_meta[i].id).collect();
        groups.push(CloneGroupRaw {
            clone_type: 1,
            token_hash: sym_meta[group_indices[0]].token_hash.clone(),
            similarity: 1.0,
            members,
        });
    }

    // 4. Type-2：token_hash 相同但 content_hash 不同
    let type1_token_hashes: HashSet<String> = groups
        .iter()
        .filter(|g| g.clone_type == 1)
        .map(|g| g.token_hash.clone())
        .collect();

    for (th, group_indices) in &by_token_hash {
        if group_indices.len() < 2 {
            continue;
        }
        if type1_token_hashes.contains(th) {
            // Type-1 已覆盖此 token_hash
            // 检查是否有多个不同 content_hash（Type-2 子组）
            let mut by_ch: FxHashMap<String, Vec<usize>> = FxHashMap::default();
            for &idx in group_indices {
                by_ch.entry(sym_meta[idx].symbol_hash.clone()).or_default().push(idx);
            }
            if by_ch.len() < 2 {
                continue; // 仅一个 content_hash，无 Type-2
            }
            // Type-2 组：所有成员
            let members: Vec<i64> = group_indices.iter().map(|&i| sym_meta[i].id).collect();
            groups.push(CloneGroupRaw {
                clone_type: 2,
                token_hash: th.clone(),
                similarity: 1.0,
                members,
            });
        } else {
            // 不在 Type-1 中，但有多符号同 token_hash → Type-2
            let members: Vec<i64> = group_indices.iter().map(|&i| sym_meta[i].id).collect();
            groups.push(CloneGroupRaw {
                clone_type: 2,
                token_hash: th.clone(),
                similarity: 1.0,
                members,
            });
        }
    }

    // 5. Type-3：相似度 >= 阈值但 < 1.0
    // 5.1 按 token_hash 去重，每组取第一个符号作为代表
    let mut token_hash_to_rep_idx: FxHashMap<String, usize> = FxHashMap::default();
    for (idx, m) in sym_meta.iter().enumerate() {
        token_hash_to_rep_idx.entry(m.token_hash.clone()).or_insert(idx);
    }
    let lsh_rep_indices: Vec<usize> = token_hash_to_rep_idx.values().copied().collect();
    let num_reps = lsh_rep_indices.len();

    // 5.2 候选对生成（小数据集暴力，大数据集 LSH）
    let mut candidate_pairs: HashSet<(usize, usize)> = HashSet::new();
    if num_reps < BRUTEFORCE_THRESHOLD {
        for i in 0..num_reps {
            for j in i + 1..num_reps {
                let a = lsh_rep_indices[i];
                let b = lsh_rep_indices[j];
                let pair = if a < b { (a, b) } else { (b, a) };
                candidate_pairs.insert(pair);
            }
        }
    } else {
        let mut lsh_buckets_map: FxHashMap<String, Vec<usize>> = FxHashMap::default();
        for &idx in &lsh_rep_indices {
            for bucket_key in lsh_buckets(&sym_meta[idx].minhash_sig, NUM_BANDS, ROWS_PER_BAND) {
                lsh_buckets_map.entry(bucket_key).or_default().push(idx);
            }
        }
        for indices in lsh_buckets_map.values() {
            if indices.len() < 2 {
                continue;
            }
            if indices.len() > MAX_BUCKET_SIZE {
                continue;
            }
            for i in 0..indices.len() {
                for j in i + 1..indices.len() {
                    let a = indices[i];
                    let b = indices[j];
                    let pair = if a < b { (a, b) } else { (b, a) };
                    candidate_pairs.insert(pair);
                }
            }
        }
    }

    // 5.3 Jaccard 验证 + 聚类
    // cluster key: (token_hash_a, token_hash_b, sim_bucket_as_i32)
    // 与 Python 对齐：sim_bucket = round(sim, 2)
    // 注意：f64 不实现 Hash/Eq，用 i32 存储 round(sim*100) 作为 key
    let mut type3_clusters: FxHashMap<(String, String, i32), Vec<i64>> = FxHashMap::default();
    let covered_token_hashes: HashSet<String> = groups
        .iter()
        .map(|g| g.token_hash.clone())
        .collect();

    for (a_idx, b_idx) in &candidate_pairs {
        let a = &sym_meta[*a_idx];
        let b = &sym_meta[*b_idx];
        // 跳过同 token_hash（已被 Type-1/2 覆盖）
        if a.token_hash == b.token_hash {
            continue;
        }
        let sim = jaccard_similarity(&a.token_set, &b.token_set);
        if sim >= similarity_threshold && sim < 1.0 {
            // round(sim, 2) → 用 i32 存储 (sim * 100).round() 作为 key
            let sim_bucket_i32 = (sim * 100.0).round() as i32;
            // 与 Python 对齐：key = (a.token_hash, b.token_hash, sim_bucket)
            // 不对 th 排序，保持原始顺序
            let key = (a.token_hash.clone(), b.token_hash.clone(), sim_bucket_i32);
            type3_clusters.entry(key).or_default().extend([a.id, b.id]);
        }
    }
    let _ = covered_token_hashes; // 与 Python 一致：未实际过滤（仅收集）

    for (key, member_ids) in &type3_clusters {
        // 去重 members（保持插入顺序）
        let mut seen: HashSet<i64> = HashSet::new();
        let mut unique_members: Vec<i64> = Vec::new();
        for &id in member_ids {
            if seen.insert(id) {
                unique_members.push(id);
            }
        }
        if unique_members.len() < 2 {
            continue;
        }
        let (th_a, th_b, sim_bucket_i32) = key;
        let sim_val = *sim_bucket_i32 as f64 / 100.0; // 还原 sim_bucket
        groups.push(CloneGroupRaw {
            clone_type: 3,
            token_hash: format!("{}|{}", th_a, th_b),
            similarity: sim_val,
            members: unique_members,
        });
    }

    // 6. 统计信息
    let type1_groups = groups.iter().filter(|g| g.clone_type == 1).count();
    let type2_groups = groups.iter().filter(|g| g.clone_type == 2).count();
    let type3_groups = groups.iter().filter(|g| g.clone_type == 3).count();
    let stats = CloneStats {
        scanned_symbols: scanned,
        skipped_symbols: skipped,
        total_groups: groups.len(),
        type1_groups,
        type2_groups,
        type3_groups,
    };

    (groups, stats)
}

/// PyO3 暴露：端到端 clone detection 核心
///
/// 与 Python `CloneDetectionMixin._detect_clone_groups_core` 对齐。
/// Python 侧负责 DB 查询和 token 归一化，Rust 侧负责：
/// 1. token_set 构建（3-gram）
/// 2. MinHash 签名生成（按 token_hash 缓存）
/// 3. Type-1/2/3 克隆分组
/// 4. LSH + Jaccard 验证
///
/// 输入：`List[Tuple[int, str, str, List[str]]]`
///   - int: 符号 ID
///   - str: symbol_hash（content_hash）
///   - str: token_hash（归一化 token 序列的 SHA-256[:16]）
///   - List[str]: 归一化 token 列表
/// 输出：`(List[Dict], Dict)`
///   - List[Dict]: 克隆组列表，每个 dict 含 clone_type / token_hash / similarity / members
///   - Dict: 统计信息（scanned_symbols / skipped_symbols / total_groups / type1/2/3_groups）
#[pyfunction]
pub fn py_detect_clones_core<'py>(
    py: Python<'py>,
    symbols: Vec<(i64, String, String, Vec<String>)>,
    similarity_threshold: f64,
) -> PyResult<Bound<'py, pyo3::types::PyTuple>> {
    let (groups, stats) = detect_clones_core(symbols, similarity_threshold);

    // 转换 groups 为 List[Dict]
    let groups_list = PyList::new(py, groups.iter().map(|g| -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let dict = pyo3::types::PyDict::new(py);
        dict.set_item("clone_type", g.clone_type)?;
        dict.set_item("token_hash", &g.token_hash)?;
        dict.set_item("similarity", g.similarity)?;
        let members_list = PyList::new(py, &g.members)?;
        dict.set_item("members", members_list)?;
        Ok(dict)
    }).collect::<PyResult<Vec<_>>>()?)?;

    // 转换 stats 为 Dict
    let stats_dict = pyo3::types::PyDict::new(py);
    stats_dict.set_item("scanned_symbols", stats.scanned_symbols)?;
    stats_dict.set_item("skipped_symbols", stats.skipped_symbols)?;
    stats_dict.set_item("total_groups", stats.total_groups)?;
    stats_dict.set_item("type1_groups", stats.type1_groups)?;
    stats_dict.set_item("type2_groups", stats.type2_groups)?;
    stats_dict.set_item("type3_groups", stats.type3_groups)?;

    let tuple = pyo3::types::PyTuple::new(py, [groups_list.into_any(), stats_dict.into_any()])?;
    Ok(tuple)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fnv1a_32_empty() {
        // FNV-1a("") = offset basis = 0x811C9DC5
        assert_eq!(fnv1a_32(b""), FNV_OFFSET_BASIS);
    }

    #[test]
    fn test_fnv1a_32_known_values() {
        // 参考 FNV-1a 测试向量
        // "a" → 0xE40C292C
        assert_eq!(fnv1a_32(b"a"), 0xE40C292C);
        // "foobar" → 0xBF9CF968
        assert_eq!(fnv1a_32(b"foobar"), 0xBF9CF968);
    }

    #[test]
    fn test_stable_token_hash_str() {
        // 相同输入应产生相同输出（确定性）
        let h1 = stable_token_hash_str("hello");
        let h2 = stable_token_hash_str("hello");
        assert_eq!(h1, h2);

        // 不同输入应产生不同输出（极大概率）
        let h3 = stable_token_hash_str("world");
        assert_ne!(h1, h3);
    }

    #[test]
    fn test_stable_token_hash_shingle() {
        // 3-gram shingle 的 hash 应与手动拼接 \x1f 一致
        let h1 = stable_token_hash_shingle("a", "b", "c");
        let manual = fnv1a_32(b"a\x1fb\x1fc");
        assert_eq!(h1, manual);

        // 不同 shingle 应不同
        let h2 = stable_token_hash_shingle("a", "b", "d");
        assert_ne!(h1, h2);
    }

    #[test]
    fn test_minhash_empty_set() {
        let sig = minhash_signature(&[]);
        assert_eq!(sig.len(), NUM_PERM);
        // 空集合：全 0xFFFFFFFF
        for &v in &sig {
            assert_eq!(v, EMPTY_SIG_FILL);
        }
    }

    #[test]
    fn test_minhash_same_set_same_sig() {
        let tokens = vec!["hello".to_string(), "world".to_string(), "foo".to_string()];
        let hashes: Vec<u32> = tokens.iter().map(|t| stable_token_hash_str(t)).collect();

        let sig1 = minhash_signature(&hashes);
        let sig2 = minhash_signature(&hashes);

        // 相同 token 集合 → 相同签名（确定性）
        assert_eq!(sig1, sig2);
        assert_eq!(sig1.len(), NUM_PERM);
    }

    #[test]
    fn test_minhash_different_set_different_sig() {
        let tokens1: Vec<u32> = vec!["hello".to_string(), "world".to_string()]
            .iter().map(|t| stable_token_hash_str(t)).collect();
        let tokens2: Vec<u32> = vec!["hello".to_string(), "world".to_string(), "extra".to_string()]
            .iter().map(|t| stable_token_hash_str(t)).collect();

        let sig1 = minhash_signature(&tokens1);
        let sig2 = minhash_signature(&tokens2);

        // 不同 token 集合 → 不同签名（极大概率）
        assert_ne!(sig1, sig2);
    }

    #[test]
    fn test_lsh_buckets_format() {
        // 签名长度需 >= NUM_BANDS * ROWS_PER_BAND = 128
        let sig: Vec<u64> = (1..=128).collect();
        let buckets = lsh_buckets(&sig, NUM_BANDS, ROWS_PER_BAND);

        assert_eq!(buckets.len(), NUM_BANDS);
        // 验证格式："b{i}:{h0}:{h1}:...:{h15}"
        assert_eq!(buckets[0], "b0:1:2:3:4:5:6:7:8:9:10:11:12:13:14:15:16");
        assert_eq!(buckets[1], "b1:17:18:19:20:21:22:23:24:25:26:27:28:29:30:31:32");
    }

    #[test]
    fn test_lsh_same_sig_same_bucket() {
        let sig = vec![42u64; NUM_PERM];
        let buckets1 = lsh_buckets(&sig, NUM_BANDS, ROWS_PER_BAND);
        let buckets2 = lsh_buckets(&sig, NUM_BANDS, ROWS_PER_BAND);

        // 相同签名 → 相同桶
        assert_eq!(buckets1, buckets2);
    }

    #[test]
    fn test_jaccard_identical_sets() {
        let a: HashSet<i32> = [1, 2, 3].iter().copied().collect();
        let b: HashSet<i32> = [1, 2, 3].iter().copied().collect();
        assert!((jaccard_similarity(&a, &b) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_jaccard_disjoint_sets() {
        let a: HashSet<i32> = [1, 2, 3].iter().copied().collect();
        let b: HashSet<i32> = [4, 5, 6].iter().copied().collect();
        assert!((jaccard_similarity(&a, &b) - 0.0).abs() < 1e-9);
    }

    #[test]
    fn test_jaccard_partial_overlap() {
        let a: HashSet<i32> = [1, 2, 3].iter().copied().collect();
        let b: HashSet<i32> = [2, 3, 4].iter().copied().collect();
        // |A∩B| = 2, |A∪B| = 4 → 0.5
        assert!((jaccard_similarity(&a, &b) - 0.5).abs() < 1e-9);
    }

    #[test]
    fn test_jaccard_empty_sets() {
        let a: HashSet<i32> = HashSet::new();
        let b: HashSet<i32> = [1, 2].iter().copied().collect();
        assert!((jaccard_similarity(&a, &b) - 0.0).abs() < 1e-9);
    }

    #[test]
    fn test_hash_coeffs_odd_a() {
        let coeffs = hash_coeffs();
        assert_eq!(coeffs.len(), NUM_PERM);
        // 所有 a 必须是奇数（与 Python | 1 对齐）
        for &(a, _) in coeffs {
            assert_eq!(a % 2, 1, "a must be odd, got {}", a);
        }
    }

    #[test]
    fn test_hash_coeffs_deterministic() {
        // 两次获取应返回相同引用（OnceLock 初始化一次）
        let c1 = hash_coeffs();
        let c2 = hash_coeffs();
        assert!(std::ptr::eq(c1, c2));
    }

    #[test]
    fn test_build_token_set_3gram() {
        // 3 个以上 token → 3-gram 集合
        let tokens = vec!["a".to_string(), "b".to_string(), "c".to_string(), "d".to_string()];
        let set = build_token_set(&tokens);
        // 4 token → 2 个 3-gram: (a,b,c), (b,c,d)
        assert_eq!(set.len(), 2);
        assert!(set.contains("a\x1fb\x1fc"));
        assert!(set.contains("b\x1fc\x1fd"));
    }

    #[test]
    fn test_build_token_set_short() {
        // 少于 3 个 token → 1-gram 集合
        let tokens = vec!["a".to_string(), "b".to_string()];
        let set = build_token_set(&tokens);
        assert_eq!(set.len(), 2);
        assert!(set.contains("a"));
        assert!(set.contains("b"));
    }

    #[test]
    fn test_minhash_jaccard_estimate_identical() {
        let tokens = vec!["hello".to_string(), "world".to_string(), "foo".to_string()];
        let hashes: Vec<u32> = tokens.iter().map(|t| stable_token_hash_str(t)).collect();
        let sig = minhash_signature(&hashes);

        // 相同签名 → 估算 Jaccard = 1.0
        let est = minhash_jaccard_estimate(&sig, &sig);
        assert!((est - 1.0).abs() < 1e-9);
    }

    #[test]
    fn test_minhash_jaccard_estimate_disjoint() {
        let tokens_a = vec!["alpha".to_string(), "beta".to_string(), "gamma".to_string()];
        let tokens_b = vec!["one".to_string(), "two".to_string(), "three".to_string()];
        let hashes_a: Vec<u32> = tokens_a.iter().map(|t| stable_token_hash_str(t)).collect();
        let hashes_b: Vec<u32> = tokens_b.iter().map(|t| stable_token_hash_str(t)).collect();
        let sig_a = minhash_signature(&hashes_a);
        let sig_b = minhash_signature(&hashes_b);

        // 不相交签名 → 估算 Jaccard 通常 < 0.2（统计性质）
        let est = minhash_jaccard_estimate(&sig_a, &sig_b);
        assert!(est < 0.3, "disjoint sets estimate should be low, got {}", est);
    }

    #[test]
    fn test_detect_clones_core_empty_input() {
        let (groups, stats) = detect_clones_core(vec![], 0.8);
        assert!(groups.is_empty());
        assert_eq!(stats.scanned_symbols, 0);
        assert_eq!(stats.total_groups, 0);
    }

    #[test]
    fn test_detect_clones_core_type1() {
        // 两个完全相同的符号 → Type-1 组
        let tokens = vec!["def".to_string(), "foo".to_string(), "(".to_string(), ")".to_string()];
        let symbols = vec![
            (1i64, "hash_A".to_string(), "th_A".to_string(), tokens.clone()),
            (2i64, "hash_A".to_string(), "th_A".to_string(), tokens.clone()),
        ];
        let (groups, stats) = detect_clones_core(symbols, 0.8);

        assert_eq!(stats.total_groups, 1);
        assert_eq!(stats.type1_groups, 1);
        assert_eq!(groups[0].clone_type, 1);
        assert_eq!(groups[0].similarity, 1.0);
        assert_eq!(groups[0].members.len(), 2);
    }

    #[test]
    fn test_detect_clones_core_type2() {
        // 两个 token_hash 相同但 symbol_hash 不同的符号 → Type-2 组
        let tokens = vec!["def".to_string(), "foo".to_string(), "(".to_string(), ")".to_string()];
        let symbols = vec![
            (1i64, "hash_A".to_string(), "th_shared".to_string(), tokens.clone()),
            (2i64, "hash_B".to_string(), "th_shared".to_string(), tokens.clone()),
        ];
        let (groups, stats) = detect_clones_core(symbols, 0.8);

        // Type-2 组（同 token_hash，不同 symbol_hash）
        assert_eq!(stats.type2_groups, 1);
        assert_eq!(groups[0].clone_type, 2);
        assert_eq!(groups[0].members.len(), 2);
    }

    #[test]
    fn test_detect_clones_core_no_clones() {
        // 三个完全不同的符号 → 无克隆组
        let symbols = vec![
            (1i64, "hash_A".to_string(), "th_A".to_string(),
             vec!["def".to_string(), "foo".to_string(), "(".to_string()]),
            (2i64, "hash_B".to_string(), "th_B".to_string(),
             vec!["def".to_string(), "bar".to_string(), "(".to_string()]),
            (3i64, "hash_C".to_string(), "th_C".to_string(),
             vec!["class".to_string(), "Baz".to_string(), "{".to_string()]),
        ];
        let (groups, stats) = detect_clones_core(symbols, 0.8);

        // 用高阈值 → 应无 Type-3（相似度低于 0.8）
        assert_eq!(stats.total_groups, 0);
        assert!(groups.is_empty());
    }
}
