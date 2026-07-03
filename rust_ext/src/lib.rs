//! Call Warden Core — PyO3 高性能扩展
//!
//! 提供 Python 侧的性能热点加速：
//! - batch_cosine_similarity: 批量余弦相似度（替代 numpy 逐向量循环）
//!
//! 构建方式（需 Rust 工具链 + maturin）:
//!   cd callwarden/rust_ext
//!   pip install maturin
//!   maturin develop --release
//!
//! Python 侧 db_vector.py 会自动加载本扩展，失败时回退到 numpy 向量化路径。

use pyo3::prelude::*;
use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyArray1};

/// 批量余弦相似度：query (dim,) × matrix (N, dim) → scores (N,)
///
/// 对矩阵每一行分别计算与 query 的余弦相似度。
/// 零范数行返回 0.0（避免除零）。
#[pyfunction]
fn batch_cosine_similarity<'py>(
    py: Python<'py>,
    query: PyReadonlyArray1<f32>,
    matrix: PyReadonlyArray2<f32>,
) -> PyResult<Bound<'py, PyArray1<f32>>> {
    let q = query.as_slice()?;
    let m = matrix.as_array();
    let (n, dim) = (m.nrows(), m.ncols());

    // 计算 query 范数
    let q_norm: f32 = q.iter().map(|x| x * x).sum::<f32>().sqrt();
    if q_norm == 0.0 || n == 0 {
        return Ok(PyArray1::zeros(py, [n], false));
    }

    let mut scores = vec![0.0f32; n];
    for i in 0..n {
        let row = m.row(i);
        let mut dot = 0.0f32;
        let mut norm_sq = 0.0f32;
        for j in 0..dim {
            let v = row[j];
            dot += q[j] * v;
            norm_sq += v * v;
        }
        let n = norm_sq.sqrt();
        scores[i] = if n > 0.0 { dot / (q_norm * n) } else { 0.0 };
    }

    Ok(PyArray1::from_vec(py, scores))
}

/// 注册 Python 模块
#[pymodule]
fn callwarden_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(batch_cosine_similarity, m)?)?;
    Ok(())
}
