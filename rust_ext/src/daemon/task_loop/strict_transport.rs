//! Strict transport parser（任务 1D2，计划 §3.3 / §8.1.4）。
//!
//! 契约要点：
//! - strict parser **只能**构造 `InvocationClass::ExternalTransport`；
//!   只有 daemon 内不经 `dispatch.rs` 的私有 validation API 能构造
//!   `InvocationClass::InternalValidation`（见 [`validate_internal_envelope`]）。
//! - 客户端无法提交或覆盖 `duplicate_keys_checked` 一类 marker：envelope 字段固定、
//!   不可序列化，marker 不存在于任何 JSON/header/params 字段。
//! - duplicate JSON key 检测**必须在原始 JSON 解析边界**（raw bytes 转成
//!   `serde_json::Value` 前）完成：HTTP body bytes、Named Pipe 与 UDS frame bytes。
//!   命中时返回稳定 `E_DUPLICATE_JSON_KEY`，不进入 route/dedup/dispatch，也不写
//!   任一 ledger（Req 15，AC26）。
//! - 校验失败一律 fail-closed，输出稳定、确定性错误。
//!
//! duplicate-key 检测器是对 raw bytes 的递归下降扫描器（[`scan_json`]），在扫描
//! object 时按 **RFC 8259 §4 解转义后的 key 值**比较（`"\u0061"` 与 `"a"` 视为同一
//! key），任何合法 JSON 都能被完整扫描；语法/结构问题不在此处判定，交由调用方既有
//! 的 `serde_json` 解析路径给出标准解析错误，因此不存在"扫描放过但标准解析接受"的
//! duplicate 绕过。

use std::collections::HashSet;

use crate::daemon::dispatch::DaemonRpcError;

use super::types::{InvocationClass, StrictParsedEnvelope};

/// duplicate key 的稳定错误码（transport 层回显用同一常量，避免字符串漂移）。
pub const ERR_DUPLICATE_JSON_KEY: &str = "E_DUPLICATE_JSON_KEY";

/// strict 扫描结果。
#[derive(Debug)]
enum ScanError {
    /// 命中重复 key（唯一需要 fail-closed 的场景）。
    DuplicateKey { key: String },
    /// 其它结构/语法问题：放行给 serde_json 给出标准解析错误。
    Malformed,
}

/// 对 raw bytes 执行 strict duplicate-key 扫描。
///
/// 返回 `Ok(())` 表示 payload 是（可能仍有语法问题的）合法 JSON 树且**未发现**
/// 重复 key；`Err(DuplicateKey)` 表示命中重复 key（无论 payload 其它部分是否合法，
/// 一律 fail-closed）；`Err(Malformed)` 表示扫描器自身判定结构非法——调用方此时应
/// 交由 `serde_json` 给出标准解析错误（不把 Malformed 当作 duplicate）。
fn scan_json(bytes: &[u8]) -> Result<(), ScanError> {
    StrictScanner::new(bytes).run()
}

/// 严格解析入口：raw bytes → 私有 `StrictParsedEnvelope`。
///
/// - 先做 strict duplicate-key 扫描（命中 → `E_DUPLICATE_JSON_KEY`）；
/// - 再经 `serde_json` 解析为 `Value` 并提取 `(workspace_instance_id,
///   canonical_method, request_id, params)`，构造 `InvocationClass::ExternalTransport`
///   的 envelope。
///
/// 语法错误返回 `E_PARSE_ERROR`；transport 层通常只需对 `E_DUPLICATE_JSON_KEY`
/// fail-closed，其余错误继续走既有 `serde_json` 解析路径以保持原错误语义。
pub fn parse_strict_envelope(
    payload: &[u8],
) -> Result<StrictParsedEnvelope, DaemonRpcError> {
    // 1. raw bytes 上的 strict duplicate-key scan（route/dedup/dispatch 之前）。
    match scan_json(payload) {
        Ok(()) => {}
        Err(ScanError::DuplicateKey { key }) => {
            return Err(DaemonRpcError::new(
                ERR_DUPLICATE_JSON_KEY,
                format!("duplicate JSON key: {}", key),
            ));
        }
        Err(ScanError::Malformed) => {}
    }

    // 2. 标准解析（语法错误交由调用方既有路径给出标准错误；此处只构造 envelope）。
    let value: serde_json::Value = serde_json::from_slice(payload).map_err(|e| {
        DaemonRpcError::new("E_PARSE_ERROR", format!("malformed JSON: {}", e))
    })?;
    let obj = value.as_object().ok_or_else(|| {
        DaemonRpcError::new("E_PARSE_ERROR", "request must be a JSON object")
    })?;

    let request_id = obj
        .get("id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let canonical_method = obj
        .get("method")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let params = obj
        .get("params")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Object(serde_json::Map::new()));
    // 与 HTTP handler 的 workspace 推导一致：`workspace_instance_id` 取自 params。
    let workspace_instance_id = params
        .get("workspace_instance_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    Ok(StrictParsedEnvelope {
        workspace_instance_id,
        canonical_method,
        request_id,
        params,
        invocation_class: InvocationClass::ExternalTransport,
    })
}

/// 私有 validation API 的唯一构造点（保留签名；1D2 前不可达）。
///
/// 只有 daemon 内不经 `dispatch.rs` 的私有 validation API 能构造
/// `InvocationClass::InternalValidation`；客户端/外部 transport 无法经任何
/// JSON 字段构造该类别。
#[allow(dead_code)]
pub(crate) fn validate_internal_envelope(
    _workspace_instance_id: &str,
    _canonical_method: &str,
    _request_id: &str,
    _params: serde_json::Value,
) -> Result<StrictParsedEnvelope, DaemonRpcError> {
    Ok(StrictParsedEnvelope {
        workspace_instance_id: _workspace_instance_id.to_string(),
        canonical_method: _canonical_method.to_string(),
        request_id: _request_id.to_string(),
        params: _params,
        invocation_class: InvocationClass::InternalValidation,
    })
}

// ============================================
// 递归下降 strict 扫描器（raw bytes，零分配于 value；key 解转义用 HashSet）
// ============================================

struct StrictScanner<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> StrictScanner<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        StrictScanner { bytes, pos: 0 }
    }

    fn eof(&self) -> bool {
        self.pos >= self.bytes.len()
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while let Some(b) = self.peek() {
            match b {
                b' ' | b'\t' | b'\n' | b'\r' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn run(&mut self) -> Result<(), ScanError> {
        self.skip_ws();
        if self.eof() {
            return Err(ScanError::Malformed);
        }
        self.scan_value()?;
        // 尾部残留非空白字节属于语法错误，交 serde_json 判定；此时 duplicate
        // 检查已完成，不存在绕过。
        Ok(())
    }

    fn scan_value(&mut self) -> Result<(), ScanError> {
        self.skip_ws();
        match self.peek() {
            None => Err(ScanError::Malformed),
            Some(b'{') => self.scan_object(),
            Some(b'[') => self.scan_array(),
            Some(b'"') => {
                self.skip_string()?;
                Ok(())
            }
            Some(b't') => self.scan_literal(b"true"),
            Some(b'f') => self.scan_literal(b"false"),
            Some(b'n') => self.scan_literal(b"null"),
            Some(b) if b == b'-' || b.is_ascii_digit() => {
                self.skip_number();
                Ok(())
            }
            Some(_) => Err(ScanError::Malformed),
        }
    }

    fn scan_literal(&mut self, lit: &[u8]) -> Result<(), ScanError> {
        if self.bytes.len() - self.pos < lit.len() {
            return Err(ScanError::Malformed);
        }
        if &self.bytes[self.pos..self.pos + lit.len()] == lit {
            self.pos += lit.len();
            Ok(())
        } else {
            Err(ScanError::Malformed)
        }
    }

    /// 宽松跳过 number 字符集；具体合法性由 serde_json 判定。
    fn skip_number(&mut self) {
        while let Some(b) = self.peek() {
            match b {
                b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn scan_array(&mut self) -> Result<(), ScanError> {
        debug_assert_eq!(self.peek(), Some(b'['));
        self.pos += 1;
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(());
        }
        loop {
            self.scan_value()?;
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(());
                }
                _ => return Err(ScanError::Malformed),
            }
        }
    }

    fn scan_object(&mut self) -> Result<(), ScanError> {
        debug_assert_eq!(self.peek(), Some(b'{'));
        self.pos += 1;
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(());
        }
        let mut seen: HashSet<String> = HashSet::new();
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(ScanError::Malformed);
            }
            let key = match self.read_string() {
                Some(k) => k,
                None => return Err(ScanError::Malformed),
            };
            if !seen.insert(key.clone()) {
                return Err(ScanError::DuplicateKey { key });
            }
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(ScanError::Malformed);
            }
            self.pos += 1;
            self.scan_value()?;
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(());
                }
                _ => return Err(ScanError::Malformed),
            }
        }
    }

    /// 跳过开引号后的字符串（不解码）。未闭合/非法转义 → Malformed。
    fn skip_string(&mut self) -> Result<(), ScanError> {
        debug_assert_eq!(self.peek(), Some(b'"'));
        self.pos += 1;
        loop {
            let b = match self.peek() {
                Some(b) => b,
                None => return Err(ScanError::Malformed),
            };
            match b {
                b'"' => {
                    self.pos += 1;
                    return Ok(());
                }
                b'\\' => {
                    self.pos += 1;
                    let esc = match self.peek() {
                        Some(e) => e,
                        None => return Err(ScanError::Malformed),
                    };
                    self.pos += 1;
                    match esc {
                        b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => {}
                        b'u' => {
                            if self.read_hex4().is_none() {
                                return Err(ScanError::Malformed);
                            }
                        }
                        _ => return Err(ScanError::Malformed),
                    }
                }
                _ => self.pos += 1,
            }
        }
    }

    /// 读取开引号后的字符串并返回**解转义后**的值（仅用于 object key 比较）。
    /// 未闭合/非法转义/孤立代理 → None（交 serde_json 报错）。
    fn read_string(&mut self) -> Option<String> {
        debug_assert_eq!(self.peek(), Some(b'"'));
        self.pos += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            let b = self.peek()?;
            match b {
                b'"' => {
                    self.pos += 1;
                    return String::from_utf8(out).ok();
                }
                b'\\' => {
                    self.pos += 1;
                    let esc = self.peek()?;
                    self.pos += 1;
                    match esc {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(0x08),
                        b'f' => out.push(0x0C),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let code = self.read_hex4()?;
                            if (0xD800..=0xDBFF).contains(&code) {
                                // 高代理：必须紧跟 \uDC00-\uDFFF 低代理（RFC 8259 §7）
                                if self.peek() == Some(b'\\') {
                                    self.pos += 1;
                                    if self.peek() == Some(b'u') {
                                        self.pos += 1;
                                        let lo = self.read_hex4()?;
                                        if (0xDC00..=0xDFFF).contains(&lo) {
                                            let cp = 0x10000
                                                + ((code as u32 - 0xD800) << 10)
                                                + (lo as u32 - 0xDC00);
                                            push_codepoint(&mut out, cp);
                                        } else {
                                            return None;
                                        }
                                    } else {
                                        return None;
                                    }
                                } else {
                                    return None;
                                }
                            } else if (0xDC00..=0xDFFF).contains(&code) {
                                // 孤立低代理：非法
                                return None;
                            } else {
                                push_codepoint(&mut out, code as u32);
                            }
                        }
                        _ => return None,
                    }
                }
                _ if b < 0x20 => return None,
                _ => {
                    out.push(b);
                    self.pos += 1;
                }
            }
        }
    }

    /// 读取 4 位十六进制（\uXXXX 的主体），返回码点值。
    fn read_hex4(&mut self) -> Option<u16> {
        if self.pos + 4 > self.bytes.len() {
            return None;
        }
        let mut v: u16 = 0;
        for _ in 0..4 {
            let b = self.bytes[self.pos];
            let d = match b {
                b'0'..=b'9' => (b - b'0') as u16,
                b'a'..=b'f' => (b - b'a' + 10) as u16,
                b'A'..=b'F' => (b - b'A' + 10) as u16,
                _ => return None,
            };
            v = v * 16 + d;
            self.pos += 1;
        }
        Some(v)
    }
}

/// 按 Unicode 标量值向 UTF-8 缓冲追加编码。
fn push_codepoint(out: &mut Vec<u8>, cp: u32) {
    if cp <= 0x7F {
        out.push(cp as u8);
    } else if cp <= 0x7FF {
        out.push(0xC0 | (cp >> 6) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else if cp <= 0xFFFF {
        out.push(0xE0 | (cp >> 12) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    } else {
        out.push(0xF0 | (cp >> 18) as u8);
        out.push(0x80 | ((cp >> 12) & 0x3F) as u8);
        out.push(0x80 | ((cp >> 6) & 0x3F) as u8);
        out.push(0x80 | (cp & 0x3F) as u8);
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn dup_err(payload: &str) -> DaemonRpcError {
        parse_strict_envelope(payload.as_bytes()).unwrap_err()
    }

    fn expect_duplicate(payload: &str) {
        let e = dup_err(payload);
        assert_eq!(e.code, ERR_DUPLICATE_JSON_KEY, "payload: {}", payload);
    }

    fn expect_ok(payload: &str) -> StrictParsedEnvelope {
        match parse_strict_envelope(payload.as_bytes()) {
            Ok(env) => env,
            Err(e) => panic!("payload 应通过 strict parse: {} (err: {:?})", payload, e),
        }
    }

    #[test]
    fn top_level_duplicate_key_rejected() {
        expect_duplicate(r#"{"method":"ping","method":"pong"}"#);
        expect_duplicate(r#"{"a":1,"a":2}"#);
        expect_duplicate(r#"{ "a" : 1 , "a" : 2 }"#);
    }

    #[test]
    fn nested_object_duplicate_rejected() {
        expect_duplicate(r#"{"outer":{"x":1,"x":2}}"#);
        expect_duplicate(r#"{"outer":{"inner":{"k":1,"k":2}}}"#);
    }

    #[test]
    fn array_nested_duplicate_rejected() {
        expect_duplicate(r#"[{"x":1,"x":2}]"#);
        expect_duplicate(r#"{"items":[{"y":1,"y":1}]}"#);
    }

    #[test]
    fn escaped_equivalent_keys_are_duplicates() {
        // RFC 8259：key 比较按解转义后的值——"\u0061" 与 "a" 是同一 key。
        expect_duplicate(r#"{"a":1,"\u0061":2}"#);
        expect_duplicate(r#"{"\u0061":1,"a":2}"#);
    }

    #[test]
    fn distinct_keys_ok() {
        expect_ok(r#"{"a":1,"b":2}"#);
        expect_ok(r#"{"a":1,"A":2}"#); // 大小写不同
        expect_ok(r#"{"a":1,"\u0041":2}"#); // "A" != "a"
        expect_ok(r#"{"outer":{"x":1},"outer2":{"x":2}}"#);
    }

    #[test]
    fn unicode_and_surrogate_keys() {
        // 代理对 key：同样的 emoji 以字面量与 \u 转义表示 → duplicate。
        expect_duplicate(r#"{"😀":1,"\ud83d\ude00":2}"#);
        // 普通非 BMP 单转义被判定非法代理 → 交给 serde_json（此处返回非 duplicate 错误）。
        let e = dup_err(r#"{"\ud83d":1}"#);
        assert_ne!(e.code, ERR_DUPLICATE_JSON_KEY);
    }

    #[test]
    fn same_key_different_values_duplicate() {
        expect_duplicate(r#"{"method":"ping","method":{"deep":true}}"#);
    }

    #[test]
    fn envelope_fields_and_class() {
        let env = expect_ok(
            r#"{"id":"req-1","method":"task.create","params":{"workspace_instance_id":"ws-9"}}"#,
        );
        assert_eq!(env.request_id, "req-1");
        assert_eq!(env.canonical_method, "task.create");
        assert_eq!(env.workspace_instance_id, "ws-9");
        assert_eq!(env.invocation_class, InvocationClass::ExternalTransport);
        assert!(env.params.is_object());
        assert_eq!(env.params["workspace_instance_id"], "ws-9");
    }

    #[test]
    fn envelope_missing_optional_fields() {
        let env = expect_ok(r#"{}"#);
        assert_eq!(env.request_id, "");
        assert_eq!(env.canonical_method, "");
        assert_eq!(env.workspace_instance_id, "");
        assert!(env.params.is_object());
        assert_eq!(env.invocation_class, InvocationClass::ExternalTransport);
    }

    #[test]
    fn malformed_or_non_object_returns_parse_error() {
        let e = dup_err(r#"[1,2,3]"#);
        assert_eq!(e.code, "E_PARSE_ERROR");
        let e = dup_err(r#"{not valid"#);
        assert_eq!(e.code, "E_PARSE_ERROR");
        let e = dup_err(r#""just a string""#);
        assert_eq!(e.code, "E_PARSE_ERROR");
    }

    #[test]
    fn whitespace_and_empty_containers_ok() {
        expect_ok(r#"{ }"#);
        expect_ok(r#"{"a":[]}"#);
        expect_ok(r#"{"a":{},"b":[1,2,{"c":null,"d":true,"e":-1.5e3}]}"#);
    }

    #[test]
    fn internal_validation_class_is_not_external() {
        let env = validate_internal_envelope("ws-1", "task.create", "req-1", serde_json::json!({}))
            .unwrap();
        assert_eq!(env.invocation_class, InvocationClass::InternalValidation);
    }
}
