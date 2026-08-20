//! query.file / query.symbol / query.grep RPC handler 辅助：结构化前置校验。
//!
//! M2.1（T-1786519351240-73127ab4）负责 `query.file` 的越界路径结构化拒绝；
//! M2.2（T-1786526643663-594ee010）负责 `query.symbol` 的符号名参数校验；
//! M2.3（T-1786529505247-9d083e54）负责 `query.grep` 的 pattern 参数校验；
//! M2.4（T-1786539379174-90f74174）负责 `query.issues` 的符号名参数校验；
//! M2.5（T-1786584287058-7f712ff4）负责 `query.tests` 的符号名参数校验。
//! query.issues/tests 的真实 handler 位于 `snapshot_state.rs`
//! （`handle_query_issues` / `handle_query_tests`），本模块只补充结构化
//! 前置校验，不重复实现 handler。
//!
//! 背景：`query.file` 的真实生产 handler 位于 `snapshot_state.rs`
//! （`open_query_connection` + `query_local_file_symbols`）。其
//! `normalize_workspace_path` 对绝对路径 strip root 前缀失败时原样保留路径，
//! 相对路径不做 `..` 向上穿越检测——越界请求会命中空结果或 generic 错误，
//! 而非结构化业务错误。M2.1 所有权白名单**不含** `snapshot_state.rs`（不可改），
//! 因此在本模块实现结构化前置校验，并在 dispatch 路由层（dispatch.rs，白名单内）
//! 对 `query.file` 调用：
//!
//! - 空 / 纯空白路径 → `invalid_params`
//! - 含 NUL 字节 → `invalid_params`
//! - 含 `..` 路径段（向上穿越，如 `../x.py`、`a/../x.py`）→ `out_of_bounds`
//!
//! 绝对路径超出 workspace root 的完整结构化拒绝依赖 handler 层 root_path 比对，
//! 需在 M2.2（`snapshot_state.rs` 进入可改范围）时接入；当前超出 root 的绝对路径
//! 由 SQL `fi.rel_path` 精确匹配天然隔离（返回空数组，不泄露数据），fail-safe。

use super::DaemonRpcError;

/// 校验 `query.file` 的 `file_path` 参数。
///
/// 返回规范化后的路径（`\` 统一为 `/`）；失败返回结构化错误：
/// - 空 / 纯空白：`invalid_params`
/// - NUL 字节：`invalid_params`
/// - `..` 向上穿越：`out_of_bounds`
pub fn validate_query_file_path(file_path: &str) -> Result<String, DaemonRpcError> {
    if file_path.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params("file_path 不能为空"));
    }
    if file_path.contains('\0') {
        return Err(DaemonRpcError::invalid_params(
            "file_path 包含 NUL 字节",
        ));
    }
    // 统一分隔符后按路径段检查 `..` 向上穿越
    let normalized = file_path.replace('\\', "/");
    for segment in normalized.split('/') {
        if segment == ".." {
            return Err(DaemonRpcError::new(
                "out_of_bounds",
                format!("file_path 越界：包含 `..` 向上穿越路径段（{file_path}）"),
            ));
        }
    }
    Ok(normalized)
}

/// 校验 `query.symbol` 的 `qualified_name` 参数。
///
/// 返回规范化后的符号名；失败返回结构化错误：
/// - 空 / 纯空白：`invalid_params`
/// - NUL 字节：`invalid_params`
pub fn validate_query_symbol_params(qualified_name: &str) -> Result<String, DaemonRpcError> {
    if qualified_name.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 不能为空",
        ));
    }
    if qualified_name.contains('\0') {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 包含 NUL 字节",
        ));
    }
    Ok(qualified_name.to_string())
}

/// 校验 `query.grep` 的 `patterns` 参数。
///
/// 成功返回 `Ok(())`；失败返回结构化错误：
/// - 空数组：`invalid_params`
/// - 任一 pattern 为空 / 纯空白：`invalid_params`
/// - 任一 pattern 含 NUL 字节：`invalid_params`
///
/// 只校验参数形态，不做正则语法检查（合法正则仍可无匹配，
/// 由 handler 返回空结果；非法正则由 handler 的 rg/fallback 报告）。
pub fn validate_query_grep_params(patterns: &[&str]) -> Result<(), DaemonRpcError> {
    if patterns.is_empty() {
        return Err(DaemonRpcError::invalid_params("grep 至少需要一个 pattern"));
    }
    for pattern in patterns {
        if pattern.trim().is_empty() {
            return Err(DaemonRpcError::invalid_params("grep pattern 不能为空"));
        }
        if pattern.contains('\0') {
            return Err(DaemonRpcError::invalid_params(
                "grep pattern 包含 NUL 字节",
            ));
        }
    }
    Ok(())
}

/// 校验 `query.issues` 的 `qualified_name` 参数。
///
/// 返回规范化后的符号名；失败返回结构化错误：
/// - 空 / 纯空白：`invalid_params`
/// - NUL 字节：`invalid_params`
///
/// M2.4（T-1786539379174-90f74174）：`handle_query_issues` 位于
/// `snapshot_state.rs`（白名单外），真实生产 handler 已经 `require_str_param`
/// 拒绝缺参；本函数在 dispatch 层补充形态校验（空/空白/NUL），
/// 拒绝后不进入 handler，与 M2.2 query.symbol 的约定一致。
pub fn validate_query_issues_params(qualified_name: &str) -> Result<String, DaemonRpcError> {
    if qualified_name.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 不能为空",
        ));
    }
    if qualified_name.contains('\0') {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 包含 NUL 字节",
        ));
    }
    Ok(qualified_name.to_string())
}

/// 校验 `query.tests` 的 `qualified_name` 参数。
///
/// 返回规范化后的符号名；失败返回结构化错误：
/// - 空 / 纯空白：`invalid_params`
/// - NUL 字节：`invalid_params`
///
/// M2.5（T-1786584287058-7f712ff4）：`handle_query_tests` 位于
/// `snapshot_state.rs`（白名单外，其他 agent 在途工作），真实生产 handler
/// 已经 `require_str_param` 拒绝缺参；本函数在 dispatch 层补充形态校验
/// （空/空白/NUL），拒绝后不进入 handler，与 M2.2 query.symbol /
/// M2.4 query.issues 的约定一致。注意：`get_test_coverage`（无参全项目
/// 测试率统计，tools_query.py）与 query.tests（按符号查询）语义不对应，
/// 遵循 M2.4 `get_issue_summary` 先例不迁移 daemon，本校验不覆盖该工具。
pub fn validate_query_tests_params(qualified_name: &str) -> Result<String, DaemonRpcError> {
    if qualified_name.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 不能为空",
        ));
    }
    if qualified_name.contains('\0') {
        return Err(DaemonRpcError::invalid_params(
            "qualified_name 包含 NUL 字节",
        ));
    }
    Ok(qualified_name.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_rejects_empty_or_blank() {
        for path in ["", "   ", "\t"] {
            assert!(
                matches!(
                    validate_query_file_path(path),
                    Err(e) if e.code == "invalid_params"
                ),
                "路径 {path:?} 应被拒绝为 invalid_params"
            );
        }
    }

    #[test]
    fn test_validate_rejects_nul_byte() {
        assert!(matches!(
            validate_query_file_path("a\x00b.py"),
            Err(e) if e.code == "invalid_params"
        ));
    }

    #[test]
    fn test_validate_rejects_parent_traversal() {
        for path in ["../x.py", "a/../x.py", "..\\x.py", "a\\..\\x.py", "/../etc/passwd"] {
            assert!(
                matches!(
                    validate_query_file_path(path),
                    Err(e) if e.code == "out_of_bounds"
                ),
                "路径 {path:?} 应被拒绝为 out_of_bounds"
            );
        }
    }

    #[test]
    fn test_validate_accepts_normal_paths() {
        for path in ["a.py", "src/a.py", "src\\a.py", "C:/repo/a.py", "./a.py"] {
            assert!(
                validate_query_file_path(path).is_ok(),
                "路径 {path:?} 应合法"
            );
        }
        // `\` 统一为 `/`
        assert_eq!(validate_query_file_path("src\\a.py").unwrap(), "src/a.py");
        // `./` 前缀保留原样（无穿越语义）
        assert_eq!(validate_query_file_path("./a.py").unwrap(), "./a.py");
    }

    // ---- M2.2 query.symbol 参数校验 ----

    #[test]
    fn test_validate_symbol_rejects_empty_or_blank() {
        for name in ["", "   ", "\t", "\n"] {
            assert!(
                matches!(
                    validate_query_symbol_params(name),
                    Err(e) if e.code == "invalid_params"
                ),
                "符号名 {name:?} 应被拒绝为 invalid_params"
            );
        }
    }

    #[test]
    fn test_validate_symbol_rejects_nul_byte() {
        assert!(matches!(
            validate_query_symbol_params("crate::mod\x00fn"),
            Err(e) if e.code == "invalid_params"
        ));
    }

    #[test]
    fn test_validate_symbol_accepts_normal_names() {
        for name in ["alpha", "a.alpha", "crate::module::function_name", "mod::Class::method"] {
            assert!(
                validate_query_symbol_params(name).is_ok(),
                "符号名 {name:?} 应合法"
            );
        }
        assert_eq!(
            validate_query_symbol_params("crate::main").unwrap(),
            "crate::main"
        );
    }

    // ---- M2.3 query.grep 参数校验 ----

    #[test]
    fn test_validate_grep_rejects_empty_patterns() {
        assert!(matches!(
            validate_query_grep_params(&[]),
            Err(e) if e.code == "invalid_params"
        ));
    }

    #[test]
    fn test_validate_grep_rejects_blank_or_nul_patterns() {
        for patterns in [
            vec!["", "alpha"],
            vec!["   ", "beta"],
            vec!["\t"],
            vec!["ok\x00pattern"],
            vec!["\n"],
        ] {
            assert!(
                matches!(
                    validate_query_grep_params(&patterns),
                    Err(e) if e.code == "invalid_params"
                ),
                "patterns {patterns:?} 应被拒绝为 invalid_params"
            );
        }
    }

    #[test]
    fn test_validate_grep_accepts_normal_patterns() {
        for patterns in [
            vec!["alpha"],
            vec!["def ", "TODO"],
            vec![r"fn\(", "->"],
            vec!["a b", "c d"],
        ] {
            assert!(
                validate_query_grep_params(&patterns).is_ok(),
                "patterns {patterns:?} 应合法"
            );
        }
    }

    // ---- M2.4 query.issues 参数校验 ----

    #[test]
    fn test_validate_issues_rejects_empty_or_blank() {
        for name in ["", "   ", "\t", "\n"] {
            assert!(
                matches!(
                    validate_query_issues_params(name),
                    Err(e) if e.code == "invalid_params"
                ),
                "符号名 {name:?} 应被拒绝为 invalid_params"
            );
        }
    }

    #[test]
    fn test_validate_issues_rejects_nul_byte() {
        assert!(matches!(
            validate_query_issues_params("crate::mod\x00fn"),
            Err(e) if e.code == "invalid_params"
        ));
    }

    #[test]
    fn test_validate_issues_accepts_normal_names() {
        for name in ["alpha", "crate::module::function_name", "mod::Class::method", "def two_words()"] {
            assert!(
                validate_query_issues_params(name).is_ok(),
                "符号名 {name:?} 应合法"
            );
        }
        assert_eq!(
            validate_query_issues_params("crate::main").unwrap(),
            "crate::main"
        );
    }

    // ---- M2.5 query.tests 参数校验 ----

    #[test]
    fn test_validate_tests_rejects_empty_or_blank() {
        for name in ["", "   ", "\t", "\n"] {
            assert!(
                matches!(
                    validate_query_tests_params(name),
                    Err(e) if e.code == "invalid_params"
                ),
                "符号名 {name:?} 应被拒绝为 invalid_params"
            );
        }
    }

    #[test]
    fn test_validate_tests_rejects_nul_byte() {
        assert!(matches!(
            validate_query_tests_params("crate::mod\x00fn"),
            Err(e) if e.code == "invalid_params"
        ));
    }

    #[test]
    fn test_validate_tests_accepts_normal_names() {
        for name in ["alpha", "crate::module::function_name", "mod::Class::method", "def two_words()"] {
            assert!(
                validate_query_tests_params(name).is_ok(),
                "符号名 {name:?} 应合法"
            );
        }
        assert_eq!(
            validate_query_tests_params("crate::main").unwrap(),
            "crate::main"
        );
    }
}
