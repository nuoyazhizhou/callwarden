//! Phase 6.1: Toolchain Fingerprint 探测
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 6
//!
//! 在 Rust 层实现 toolchain fingerprint 计算（SHA-256），
//! 供 Python 侧的 db_toolchain.py 调用，避免 Python hashlib 的开销。
//! 同时提供 compiler_type 检测。

use std::collections::HashMap;
use std::path::Path;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use sha2::{Digest, Sha256};

// ============================================
// 工具链类型
// ============================================

/// 从编译器路径推断编译器类型
/// 优先匹配带前缀的交叉编译器
pub fn detect_compiler_type(compiler_path: &str) -> String {
    let basename = Path::new(compiler_path)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_lowercase();

    if basename.contains("arm-none-eabi") {
        return "arm-none-eabi-gcc".to_string();
    }
    if basename.contains("aarch64") {
        return "aarch64-linux-gnu-gcc".to_string();
    }
    if basename.contains("clang") {
        return "clang".to_string();
    }
    if basename.contains("g++") {
        return "g++".to_string();
    }
    if basename.contains("gcc") {
        return "gcc".to_string();
    }
    basename
}

/// 计算 toolchain fingerprint（SHA-256）
///
/// 指纹基于：compiler_path, compiler_type, version, target_triple,
/// sysroot, include_dirs（排序后）, predefined_macros（排序后）
///
/// 排序确保顺序无关
pub fn compute_toolchain_fingerprint(
    compiler_path: &str,
    compiler_type: &str,
    version: &str,
    target_triple: &str,
    sysroot: &str,
    include_dirs: &[String],
    predefined_macros: &HashMap<String, String>,
) -> String {
    // 排序 include_dirs
    let mut sorted_includes: Vec<&String> = include_dirs.iter().collect();
    sorted_includes.sort();

    // 排序 predefined_macros（按 key）
    let mut sorted_macros: Vec<(&String, &String)> = predefined_macros.iter().collect();
    sorted_macros.sort_by(|a, b| a.0.cmp(b.0));

    // 规范化路径
    let normalized_compiler = Path::new(compiler_path)
        .to_string_lossy()
        .replace('\\', "/");
    let normalized_sysroot = if sysroot.is_empty() {
        String::new()
    } else {
        Path::new(sysroot).to_string_lossy().replace('\\', "/")
    };

    // 构建 fingerprint 原文
    let mut hasher = Sha256::new();
    hasher.update(b"toolchain_v1|");
    hasher.update(normalized_compiler.as_bytes());
    hasher.update(b"|");
    hasher.update(compiler_type.as_bytes());
    hasher.update(b"|");
    hasher.update(version.as_bytes());
    hasher.update(b"|");
    hasher.update(target_triple.as_bytes());
    hasher.update(b"|");
    hasher.update(normalized_sysroot.as_bytes());
    hasher.update(b"|");
    for dir in &sorted_includes {
        hasher.update(dir.as_bytes());
        hasher.update(b";");
    }
    hasher.update(b"|");
    for (k, v) in &sorted_macros {
        hasher.update(k.as_bytes());
        hasher.update(b"=");
        hasher.update(v.as_bytes());
        hasher.update(b";");
    }

    let result = hasher.finalize();
    format!("{:x}", result)
}

// ============================================
// PyO3 包装
// ============================================

/// PyO3 暴露的 detect_compiler_type 函数
#[pyfunction]
pub fn detect_compiler_type_py(compiler_path: &str) -> PyResult<String> {
    Ok(detect_compiler_type(compiler_path))
}

/// PyO3 暴露的 compute_toolchain_fingerprint 函数
///
/// 接受 Python dict 作为 predefined_macros，List 作为 include_dirs
#[pyfunction]
#[pyo3(signature = (compiler_path, compiler_type, version, target_triple, sysroot, include_dirs, predefined_macros))]
pub fn compute_toolchain_fingerprint_py(
    compiler_path: &str,
    compiler_type: &str,
    version: &str,
    target_triple: &str,
    sysroot: &str,
    include_dirs: Vec<String>,
    predefined_macros: HashMap<String, String>,
) -> PyResult<String> {
    Ok(compute_toolchain_fingerprint(
        compiler_path,
        compiler_type,
        version,
        target_triple,
        sysroot,
        &include_dirs,
        &predefined_macros,
    ))
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_compiler_type() {
        assert_eq!(detect_compiler_type("/usr/bin/gcc"), "gcc");
        assert_eq!(detect_compiler_type("/usr/bin/g++"), "g++");
        assert_eq!(detect_compiler_type("/usr/bin/clang"), "clang");
        assert_eq!(
            detect_compiler_type("/usr/bin/arm-none-eabi-gcc"),
            "arm-none-eabi-gcc"
        );
        assert_eq!(
            detect_compiler_type("/usr/bin/aarch64-linux-gnu-gcc"),
            "aarch64-linux-gnu-gcc"
        );
    }

    #[test]
    fn test_fingerprint_consistency() {
        let fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &["/usr/include".to_string()],
            &[("__GNUC__".to_string(), "10".to_string())]
                .into_iter()
                .collect(),
        );
        let fp2 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &["/usr/include".to_string()],
            &[("__GNUC__".to_string(), "10".to_string())]
                .into_iter()
                .collect(),
        );
        assert_eq!(fp1, fp2);
    }

    #[test]
    fn test_fingerprint_different_version() {
        let fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &HashMap::new(),
        );
        let fp2 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "11.0",
            "x86_64-linux",
            "",
            &[],
            &HashMap::new(),
        );
        assert_ne!(fp1, fp2);
    }

    #[test]
    fn test_fingerprint_include_dirs_order_independent() {
        let fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &["/usr/include".to_string(), "/usr/local/include".to_string()],
            &HashMap::new(),
        );
        let fp2 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &["/usr/local/include".to_string(), "/usr/include".to_string()],
            &HashMap::new(),
        );
        assert_eq!(fp1, fp2); // 排序后相同
    }

    #[test]
    fn test_fingerprint_macros_order_independent() {
        let mut macros1 = HashMap::new();
        macros1.insert("A".to_string(), "1".to_string());
        macros1.insert("B".to_string(), "2".to_string());

        let mut macros2 = HashMap::new();
        macros2.insert("B".to_string(), "2".to_string());
        macros2.insert("A".to_string(), "1".to_string());

        let fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &macros1,
        );
        let fp2 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &macros2,
        );
        assert_eq!(fp1, fp2); // 排序后相同
    }

    #[test]
    fn test_fingerprint_different_path() {
        let fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &HashMap::new(),
        );
        let fp2 = compute_toolchain_fingerprint(
            "/opt/gcc/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &HashMap::new(),
        );
        assert_ne!(fp1, fp2);
    }

    #[test]
    fn test_fingerprint_is_hex() {
        let fp = compute_toolchain_fingerprint(
            "/usr/bin/gcc",
            "gcc",
            "10.0",
            "x86_64-linux",
            "",
            &[],
            &HashMap::new(),
        );
        assert_eq!(fp.len(), 64); // SHA-256 hex = 64 chars
        assert!(fp.chars().all(|c| c.is_ascii_hexdigit()));
    }
}
