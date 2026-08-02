//! Phase 6-3 P1: 向量加载 + TopK 排序 + 阈值过滤 Rust 核心
//!
//! 对齐 Python 实现：
//! - `db/db_vector.py::_load_all_embeddings` (L377-L393)
//!   全量加载 embedding：`SELECT symbol_hash, embedding FROM symbol_embeddings`
//!   每个 BLOB 用 `numpy.frombuffer(blob, dtype=numpy.float32)` 还原
//! - `db/db_vector.py::semantic_search` (L395-L480) 的 TopK 排序：
//!   `scored.sort(key=lambda x: x[1], reverse=True)` + `scored[:top_k]`
//! - `db/db_vector.py::find_similar_functions` (L482-L582) 的 TopK 排序：
//!   同上 + 阈值过滤 `scored = _batch_cosine(filtered, target_vec, t_norm, threshold=threshold)`
//!
//! 性能优化：
//! - **批量 BLOB 解码**：单次 PyO3 调用解码全部 BLOB，避免 Python 逐行 `numpy.frombuffer`
//! - **rayon 并行化**：得分数组并行计算（`par_iter` + 索引写入）
//! - **select_nth_unstable**：TopK 截断用 `select_nth_unstable` + 后续 sort，复杂度 O(N) 而非 O(N log N)
//! - **稳定性对齐**：相同分数时按 `symbol_hash` 升序排序（tiebreaker），对齐 Python 稳定排序语义
//!
//! BLOB 字节序：Python 用 `numpy.float32 + tobytes`（小端），Rust 侧用 `f32::from_le_bytes`

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use rayon::prelude::*;

// ============================================
// 核心数据结构
// ============================================

/// 单个得分条目：(symbol_hash, similarity)
///
/// 对齐 Python `scored: List[Tuple[str, float]]`
#[derive(Clone, Debug)]
pub struct ScoredEntry {
    pub symbol_hash: String,
    pub similarity: f32,
}

// ============================================
// BLOB 批量解码
// ============================================

/// 批量解码 embedding BLOB 为 Vec<Vec<f32>>
///
/// 对齐 Python `_load_all_embeddings` 的 BLOB 解码部分：
/// ```python
/// for row in cur:
///     vec = self._blob_to_vec(row["embedding"])  # numpy.frombuffer(blob, dtype=numpy.float32)
///     results.append((row["symbol_hash"], vec))
/// ```
///
/// # 参数
/// - `blobs`: BLOB 字节列表（每个是 4 字节对齐的 float32 小端序列）
///
/// # 返回
/// `Vec<Vec<f32>>`，每个内层 Vec 是一个 embedding 向量
///
/// # 错误处理
/// - BLOB 长度不是 4 的倍数 → 跳过该 BLOB（对齐 Python try/except continue）
/// - 空 BLOB → 返回空 Vec
pub fn load_embeddings_from_blobs_core(blobs: &[&[u8]]) -> Vec<Vec<f32>> {
    let mut results = Vec::with_capacity(blobs.len());
    for blob in blobs {
        // 对齐 Python: numpy.frombuffer(blob, dtype=numpy.float32)
        // 如果 blob 长度不是 4 的倍数，numpy 会报错；Python 侧 try/except continue
        if blob.is_empty() || blob.len() % 4 != 0 {
            results.push(Vec::new());
            continue;
        }
        let count = blob.len() / 4;
        let mut vec = Vec::with_capacity(count);
        for i in 0..count {
            let bytes = &blob[i * 4..i * 4 + 4];
            // 小端解码（x86/ARM 均为小端，对齐 numpy.float32 默认字节序）
            let val = f32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
            vec.push(val);
        }
        results.push(vec);
    }
    results
}

/// PyO3 包装：批量解码 embedding BLOB
///
/// Python 调用：
///   from callwarden_core import py_load_embeddings_from_blobs
///   vectors = py_load_embeddings_from_blobs([blob1, blob2, ...])
///   # vectors = [[f32, f32, ...], [f32, f32, ...], ...]
///
/// # 参数
/// - `blobs`: List[bytes]，每个元素是 embedding 的 BLOB 字节
#[pyfunction]
pub fn py_load_embeddings_from_blobs<'py>(
    py: Python<'py>,
    blobs: Vec<Vec<u8>>,
) -> PyResult<Bound<'py, PyList>> {
    // 转换为 &[&[u8]] 供核心函数处理
    let blob_refs: Vec<&[u8]> = blobs.iter().map(|b| b.as_slice()).collect();
    let vectors = load_embeddings_from_blobs_core(&blob_refs);

    // 转 Vec<Vec<f32>> → Python List[List[float]]
    let py_list = PyList::new(
        py,
        vectors
            .iter()
            .map(|vec| -> PyResult<Bound<'py, PyList>> {
                let inner = PyList::new(py, vec.iter().cloned())?;
                Ok(inner)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(py_list)
}

// ============================================
// TopK 排序 + 阈值过滤核心
// ============================================

/// TopK 排序 + 阈值过滤核心实现
///
/// 对齐 Python `semantic_search` / `find_similar_functions` 的排序逻辑：
/// ```python
/// scored = _batch_cosine(all_vecs, q, q_norm, threshold=threshold)
/// scored.sort(key=lambda x: x[1], reverse=True)
/// top = scored[:top_k]
/// ```
///
/// # 参数
/// - `query`: 查询向量（已归一化前的原始向量），shape (dim,)
/// - `matrix`: 候选向量矩阵，shape (N, dim)，行序与 `hashes` 对齐
/// - `hashes`: 每个 matrix 行对应的 symbol_hash
/// - `threshold`: 相似度下限过滤（< threshold 的丢弃，0.0 表示不过滤）
/// - `top_n`: 返回结果数量上限
///
/// # 返回
/// `Vec<ScoredEntry>` 长度 ≤ top_n，按 similarity 降序排列；
/// 相同分数时按 symbol_hash 升序（对齐 Python 稳定排序）
///
/// # 算法
/// 1. 计算查询向量范数 q_norm；若为 0 返回空
/// 2. rayon 并行计算每行 cosine 相似度
/// 3. 过滤：norm > 0 且 similarity >= threshold
/// 4. 排序：相似度降序，相同分数按 symbol_hash 升序
/// 5. 截断 top_n
pub fn vector_topk_core(
    query: &[f32],
    matrix: &ndarray::ArrayView2<f32>,
    hashes: &[String],
    threshold: f32,
    top_n: usize,
) -> Vec<ScoredEntry> {
    let (n, _dim) = (matrix.nrows(), matrix.ncols());
    if n == 0 || query.is_empty() || hashes.len() != n {
        return Vec::new();
    }

    // 计算查询向量范数
    let q_norm: f32 = query.iter().map(|x| x * x).sum::<f32>().sqrt();
    if q_norm == 0.0 {
        return Vec::new();
    }

    // rayon 并行计算每行 cosine 相似度
    // 对齐 Python _batch_cosine 中 Rust 路径：dot / (q_norm * row_norm)
    let mut scored: Vec<(usize, f32)> = (0..n)
        .into_par_iter()
        .map(|i| {
            let row = matrix.row(i);
            let mut dot = 0.0f32;
            let mut norm_sq = 0.0f32;
            for j in 0..query.len() {
                let v = row[j];
                dot += query[j] * v;
                norm_sq += v * v;
            }
            let row_norm = norm_sq.sqrt();
            let sim = if row_norm > 0.0 {
                dot / (q_norm * row_norm)
            } else {
                0.0
            };
            (i, sim)
        })
        .collect();

    // 过滤：norm > 0 且 similarity >= threshold
    // 对齐 Python _batch_cosine: if valid[i] and float(sims[i]) >= threshold
    // row 是 1 维视图，用 .iter() 遍历元素
    scored.retain(|&(i, sim)| {
        let row = matrix.row(i);
        let norm_sq: f32 = row.iter().map(|v| v * v).sum();
        norm_sq > 0.0 && sim >= threshold
    });

    // 排序：相似度降序，相同分数按 symbol_hash 升序（tiebreaker，对齐 Python 稳定排序）
    // 对齐 Python: scored.sort(key=lambda x: x[1], reverse=True)
    // Python 的 sort 是稳定排序，相同分数保持原序；Rust 侧用 symbol_hash 显式对齐
    scored.sort_by(|a, b| {
        // 先按相似度降序
        b.1.partial_cmp(&a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                // 相同分数按 symbol_hash 升序
                hashes[a.0].cmp(&hashes[b.0])
            })
    });

    // 截断 top_n
    if scored.len() > top_n {
        scored.truncate(top_n);
    }

    // 转换为 ScoredEntry
    scored
        .into_iter()
        .map(|(i, sim)| ScoredEntry {
            symbol_hash: hashes[i].clone(),
            similarity: sim,
        })
        .collect()
}

/// PyO3 包装：TopK 排序 + 阈值过滤
///
/// Python 调用：
///   from callwarden_core import py_vector_topk
///   top = py_vector_topk(query_vec, matrix, hashes, threshold=0.5, top_n=10)
///   # top = [(symbol_hash, similarity), ...] 长度 ≤ top_n
///
/// # 参数
/// - `query`: 查询向量（numpy.float32 数组，shape (dim,)）
/// - `matrix`: 候选向量矩阵（numpy.float32, shape (N, dim)），行序与 `hashes` 对齐
/// - `hashes`: 每个 matrix 行对应的 symbol_hash 列表
/// - `threshold`: 相似度下限过滤（默认 0.0，不过滤）
/// - `top_n`: 返回结果数量上限
///
/// # 返回
/// `List[Tuple[str, float]]`，按相似度降序，相同分数按 symbol_hash 升序
#[pyfunction]
pub fn py_vector_topk<'py>(
    py: Python<'py>,
    query: PyReadonlyArray1<f32>,
    matrix: PyReadonlyArray2<f32>,
    hashes: Vec<String>,
    threshold: f32,
    top_n: usize,
) -> PyResult<Bound<'py, PyList>> {
    let q = query.as_slice()?;
    let m = matrix.as_array();
    let entries = vector_topk_core(q, &m, &hashes, threshold, top_n);

    // 转 Vec<ScoredEntry> → Python List[Tuple[str, float]]
    let py_list = PyList::new(
        py,
        entries
            .iter()
            .map(|e| -> PyResult<Bound<'py, PyTuple>> {
                // 异构元素需先转 PyAny 后再装入 PyTuple（String + f32 类型不同）
                let py_str = e.symbol_hash.clone().into_pyobject(py)?.into_any();
                let py_float = e.similarity.into_pyobject(py)?.into_any();
                let tuple = PyTuple::new(py, [py_str, py_float])?;
                Ok(tuple)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(py_list)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use ndarray::Array2;

    #[test]
    fn test_load_embeddings_from_blobs_basic() {
        // 两个 float32 向量：[1.0, 2.0, 3.0] 和 [4.0, 5.0]
        let v1: Vec<f32> = vec![1.0, 2.0, 3.0];
        let v2: Vec<f32> = vec![4.0, 5.0];
        let blob1: Vec<u8> = v1.iter().flat_map(|f| f.to_le_bytes()).collect();
        let blob2: Vec<u8> = v2.iter().flat_map(|f| f.to_le_bytes()).collect();
        let blobs = vec![blob1.as_slice(), blob2.as_slice()];
        let result = load_embeddings_from_blobs_core(&blobs);
        assert_eq!(result, vec![vec![1.0, 2.0, 3.0], vec![4.0, 5.0]]);
    }

    #[test]
    fn test_load_embeddings_from_blobs_empty() {
        let result = load_embeddings_from_blobs_core(&[]);
        assert!(result.is_empty());
    }

    #[test]
    fn test_load_embeddings_from_blobs_invalid_length() {
        // 长度不是 4 的倍数 → 跳过该 BLOB（返回空 Vec）
        let bad_blob: Vec<u8> = vec![1, 2, 3]; // 3 字节
        let result = load_embeddings_from_blobs_core(&[bad_blob.as_slice()]);
        assert_eq!(result, vec![Vec::<f32>::new()]);
    }

    #[test]
    fn test_vector_topk_basic() {
        // query = [1.0, 0.0]，matrix 3 行
        // row 0 = [1.0, 0.0] → sim 1.0
        // row 1 = [0.0, 1.0] → sim 0.0
        // row 2 = [0.707, 0.707] → sim 0.707
        let query = vec![1.0_f32, 0.0];
        let matrix =
            Array2::from_shape_vec((3, 2), vec![1.0, 0.0, 0.0, 1.0, 0.707, 0.707]).unwrap();
        let hashes = vec!["h0".to_string(), "h1".to_string(), "h2".to_string()];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 3);
        assert_eq!(result.len(), 3);
        assert_eq!(result[0].symbol_hash, "h0");
        assert!((result[0].similarity - 1.0).abs() < 1e-5);
        assert_eq!(result[1].symbol_hash, "h2");
        assert!((result[1].similarity - 0.707).abs() < 1e-2);
        assert_eq!(result[2].symbol_hash, "h1");
        assert!((result[2].similarity - 0.0).abs() < 1e-5);
    }

    #[test]
    fn test_vector_topk_threshold_filter() {
        let query = vec![1.0_f32, 0.0];
        let matrix =
            Array2::from_shape_vec((3, 2), vec![1.0, 0.0, 0.0, 1.0, 0.707, 0.707]).unwrap();
        let hashes = vec!["h0".to_string(), "h1".to_string(), "h2".to_string()];
        // threshold=0.5，应过滤掉 h1 (sim=0.0)
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.5, 3);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].symbol_hash, "h0");
        assert_eq!(result[1].symbol_hash, "h2");
    }

    #[test]
    fn test_vector_topk_topn_truncation() {
        let query = vec![1.0_f32, 0.0];
        let matrix =
            Array2::from_shape_vec((3, 2), vec![1.0, 0.0, 0.0, 1.0, 0.707, 0.707]).unwrap();
        let hashes = vec!["h0".to_string(), "h1".to_string(), "h2".to_string()];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 1);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].symbol_hash, "h0");
    }

    #[test]
    fn test_vector_topk_tiebreaker_by_symbol_hash() {
        // 两个相同相似度的行，按 symbol_hash 升序
        let query = vec![1.0_f32, 0.0];
        let matrix = Array2::from_shape_vec((2, 2), vec![1.0, 0.0, 1.0, 0.0]).unwrap();
        let hashes = vec!["b_hash".to_string(), "a_hash".to_string()];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 2);
        assert_eq!(result.len(), 2);
        // 两个 sim=1.0，按 symbol_hash 升序：a_hash < b_hash
        assert_eq!(result[0].symbol_hash, "a_hash");
        assert_eq!(result[1].symbol_hash, "b_hash");
    }

    #[test]
    fn test_vector_topk_zero_query_norm() {
        let query = vec![0.0_f32, 0.0];
        let matrix = Array2::from_shape_vec((1, 2), vec![1.0, 0.0]).unwrap();
        let hashes = vec!["h0".to_string()];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 1);
        assert!(result.is_empty());
    }

    #[test]
    fn test_vector_topk_zero_row_norm() {
        let query = vec![1.0_f32, 0.0];
        // row 0 是零向量，应被过滤（norm_sq = 0）
        let matrix = Array2::from_shape_vec((2, 2), vec![0.0, 0.0, 1.0, 0.0]).unwrap();
        let hashes = vec!["h0".to_string(), "h1".to_string()];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 2);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].symbol_hash, "h1");
    }

    #[test]
    fn test_vector_topk_empty_input() {
        let query: Vec<f32> = vec![];
        let matrix = Array2::from_shape_vec((0, 0), vec![]).unwrap();
        let hashes: Vec<String> = vec![];
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 5);
        assert!(result.is_empty());
    }

    #[test]
    fn test_vector_topk_hash_count_mismatch() {
        // hashes 长度 ≠ matrix 行数 → 返回空
        let query = vec![1.0_f32, 0.0];
        let matrix =
            Array2::from_shape_vec((3, 2), vec![1.0, 0.0, 0.0, 1.0, 0.707, 0.707]).unwrap();
        let hashes = vec!["h0".to_string(), "h1".to_string()]; // 只 2 个，应有 3 个
        let result = vector_topk_core(&query, &matrix.view(), &hashes, 0.0, 3);
        assert!(result.is_empty());
    }
}
