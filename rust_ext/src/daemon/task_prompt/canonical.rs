//! Role Prompt Compiler v1 — canonical JSON v1 与 SHA-256 形式工具。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md。
//!
//! 职责（RP-04 卡冻结 scope，仅此而已）：
//! - `cw.canonical_json.v1`（spec §9.1）：UTF-8、object key 按 UTF-8 byte
//!   lexical order、无多余空白、integer 无前导零、禁止 float、schema 要求的
//!   null 显式保留、SHA-256 输出小写 `sha256:<64-hex>`；禁止字符串拼接计算
//!   结构化 hash。
//! - SHA-256 wire form 工具（spec §7.1-3 / §9.3）：历史 64-hex 与
//!   `sha256:<64-hex>` 都接受，比较前规范为小写 `sha256:` 前缀形式；其他
//!   算法/长度/字符拒绝。
//!
//! 明确不做：文本裁剪/预算（render.rs）、secret 扫描（redaction.rs）、
//! 路由（route.rs）、bundle 组装（bundle.rs）。

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// canonical JSON v1 序列化失败（float 等 runtime invariant 违例）。
pub const E_TASK_PROMPT_TEMPLATE_INVALID: &str = "E_TASK_PROMPT_TEMPLATE_INVALID";

/// 小写 64-hex SHA-256（不带前缀）。
pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut hex = String::with_capacity(64);
    for b in digest {
        hex.push_str(&format!("{:02x}", b));
    }
    hex
}

/// 小写 `sha256:<64-hex>` 形式（spec §9.1 唯一输出形式）。
pub fn sha256_prefixed(bytes: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(bytes))
}

/// 规范化 SHA-256 wire form（spec §7.1-3）：接受 64-hex（任意大小写）或
/// `sha256:<64-hex>`（任意大小写），返回小写 `sha256:` 形式；其他算法、
/// 长度或字符一律 `None`。
pub fn normalize_sha256_form(input: &str) -> Option<String> {
    let bare = input.strip_prefix("sha256:").unwrap_or(input);
    if bare.len() != 64 || !bare.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    Some(format!("sha256:{}", bare.to_ascii_lowercase()))
}

/// SHA-256 字段预算校验（spec §9.3）：64 hex 或规范化后的 71-byte prefixed
/// form；其他形式失败。
pub fn valid_sha256_form(input: &str) -> bool {
    normalize_sha256_form(input).is_some()
}

/// `cw.canonical_json.v1` 序列化：返回紧凑 UTF-8 bytes。
///
/// - object key 按 UTF-8 byte lexical order 递归排序后以紧凑形式输出
///   （serde_json 紧凑序列化无多余空白）；
/// - float 一律拒绝（spec §9.1：v1 schema 禁止 float）；
/// - 数组保留 authority 顺序（v1 bundle schema 无 set 型数组）；
/// - null 显式保留。
pub fn canonical_json(value: &Value) -> Result<Vec<u8>, String> {
    let sorted = sort_and_check(value)?;
    serde_json::to_vec(&sorted).map_err(|e| format!("canonical serialization failed: {e}"))
}

fn sort_and_check(value: &Value) -> Result<Value, String> {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            // String 的 Ord 即 UTF-8 byte lexical order（spec §9.1）。
            keys.sort();
            let mut out = Map::with_capacity(map.len());
            for key in keys {
                out.insert(key.clone(), sort_and_check(&map[key])?);
            }
            Ok(Value::Object(out))
        }
        Value::Array(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(sort_and_check(item)?);
            }
            Ok(Value::Array(out))
        }
        Value::Number(n) => {
            if n.is_f64() {
                return Err("cw.canonical_json.v1 forbids float values (spec 9.1)".to_string());
            }
            Ok(value.clone())
        }
        _ => Ok(value.clone()),
    }
}

