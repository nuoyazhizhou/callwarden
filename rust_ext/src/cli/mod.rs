//! CLI 模块（Phase 5-1 + 5-3）
//!
//! 对齐 Python `cli/main.py`、`release/config_loader.py` 和 `cli/console.py`：
//! - `config`: 分层配置加载器（TOML + env + CLI 三层优先级）
//! - `readonly`: 只读命令识别（`_is_readonly_command` / `_is_readonly_args`）
//! - `router`: local/enterprise/auto 路由决策（Phase 5-1 B）
//! - `output`: 兼容输出层（彩色文本 + 格式化 + JSON）（Phase 5-3）
//! - `stats`: stats 子命令业务逻辑（Phase 5-1 C 垂直切片）
//!
//! 契约：
//! - docs/design/phase5-1-cli-config-contract.md
//! - docs/design/phase5-1b-router-contract.md
//! - docs/design/phase5-3-output-layer-contract.md
//! - docs/design/phase5-1c-stats-vertical-slice-contract.md

pub mod config;
pub mod file_query;
pub mod output;
pub mod readonly;
pub mod router;
pub mod runtime;
pub mod search;
pub mod stats;
pub mod status;
pub mod symbol;
