//! P0-C Step 0: 按语言拆分的配置模块
//!
//! 原 multi_lang.rs 中的 15 个 `*_config()` 函数已按语言拆分到本目录下的
//! 单文件模块（python.rs / rust.rs / ...）。本模块仅提供统一调度入口
//! `get_config(lang_id)`，供 `multi_lang::LangConfig::get` 委托调用。
//!
//! 拆分目的：
//! - 降低 multi_lang.rs 体积（配置与框架分离）
//! - 为 P0-D（HCL/Elixir）提供独立可修改的语言模块，避免与 P0-C 抢文件
//! - 每语言一份文件，便于后续按语言补全 signature/visibility 等字段
//!
//! 本步骤为纯重构，不改变任何语言的配置内容（行为等价）。

use crate::multi_lang::LangConfig;

mod python;
mod rust;
mod go;
mod java;
mod typescript;
mod javascript;
mod ruby;
mod php;
mod scala;
mod csharp;
mod cpp;
mod kotlin;
mod swift;
mod elixir;
mod hcl;

/// 按 language_id 获取配置
///
/// 由 `multi_lang::LangConfig::get` 委托调用。匹配原 `LangConfig::get` 的
/// dispatch 表（含 hcl），行为等价。
pub(crate) fn get_config(lang_id: &str) -> Option<LangConfig> {
    let config = match lang_id {
        "python" => python::config(),
        "rust" => rust::config(),
        "go" => go::config(),
        "java" => java::config(),
        "typescript" => typescript::config(),
        "javascript" => javascript::config(),
        "ruby" => ruby::config(),
        "php" => php::config(),
        "scala" => scala::config(),
        "csharp" => csharp::config(),
        "cpp" => cpp::config(),
        "kotlin" => kotlin::config(),
        "swift" => swift::config(),
        "elixir" => elixir::config(),
        "hcl" => hcl::config(),
        _ => return None,
    };
    Some(config)
}
