//! Role Prompt Compiler v1 — secret denylist 高置信扫描。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md §8.3。
//!
//! 职责（RP-04 卡冻结 scope，仅此而已）：
//! - compile 前对输入（模板正文、canonical context、不可信块内容）与
//!   compose 后对最终 `prompt.text` 执行高置信 secret 扫描；
//! - 命中返回 `E_TASK_PROMPT_SECRET_DETECTED`，不得只打日志后继续返回，
//!   也不得把 secret 放进 omissions hash；
//! - reason codes 只含高置信类别名，不含 secret 内容本身（错误 details
//!   纪律，spec §10.2）。
//!
//! 误报边界（有意为之的排除）：`sha256:<64-hex>` 形式的 contract/hash 字段
//! 是 authority 数据（spec §9.3 认可的唯一 hash 形式），不是 credential
//! hash；bundle 的 `authorization` 对象键（routing_state 等授权投影）不是
//! HTTP Authorization 头。denylist 模式据此设计为"值形态"匹配而非裸关键词。

use regex::Regex;
use std::sync::OnceLock;

/// 输入或最终输出命中 secret denylist（spec §10.2，retry `never`）。
pub const E_TASK_PROMPT_SECRET_DETECTED: &str = "E_TASK_PROMPT_SECRET_DETECTED";

/// 已知非 secret 的裸 64-hex 固定引用：daemon-owned 资产正文自述的冻结
/// authority SHA（compiler policy §0 自述，build gate §11.1-8 背书）。
/// bare-hex 规则对它们豁免，避免 policy 正文触发误报。
///
/// CR14（review §5.7）：除该硬编码条目外，豁免集还从 role_prompt_assets
/// manifest（编译期 include_str，与模板正文同源 authority）派生全部
/// `content_sha256` hex 核心——新增资产自述 hash 自动豁免，免改码。
/// 匹配时两侧统一 `to_ascii_uppercase` 归一化，消除大小写敏感。
pub const KNOWN_NON_SECRET_HEX_REFS: &[&str] =
    &["95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7"];

const ROLE_PROMPT_ASSETS_MANIFEST: &str =
    include_str!("../../../resources/role_prompts/v1/manifest.json");

/// 归一化豁免集：硬编码条目 + manifest 全部 content_sha256 的 64-hex 核心
/// （统一大写）。懒初始化。`pub(crate)` 供测试断言 manifest 派生生效。
pub(crate) fn non_secret_hex_refs() -> &'static Vec<String> {
    static SET: OnceLock<Vec<String>> = OnceLock::new();
    SET.get_or_init(|| {
        let mut set: Vec<String> = KNOWN_NON_SECRET_HEX_REFS
            .iter()
            .map(|s| s.to_ascii_uppercase())
            .collect();
        if let Ok(manifest) = serde_json::from_str::<serde_json::Value>(
            ROLE_PROMPT_ASSETS_MANIFEST,
        ) {
            collect_manifest_hex(&manifest, &mut set);
        }
        set
    })
}

/// 递归收集 manifest JSON 中所有 `content_sha256` 字段的 64-hex 核心。
fn collect_manifest_hex(value: &serde_json::Value, out: &mut Vec<String>) {
    match value {
        serde_json::Value::Object(map) => {
            for (k, v) in map {
                if k == "content_sha256" {
                    if let Some(s) = v.as_str() {
                        let core = s.trim_start_matches("sha256:");
                        if core.len() == 64 && core.chars().all(|c| c.is_ascii_hexdigit()) {
                            let up = core.to_ascii_uppercase();
                            if !out.contains(&up) {
                                out.push(up);
                            }
                        }
                    }
                } else {
                    collect_manifest_hex(v, out);
                }
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                collect_manifest_hex(item, out);
            }
        }
        _ => {}
    }
}

/// 单条高置信命中；只携带类别 reason code，绝不携带命中内容。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SecretFinding {
    pub reason_code: &'static str,
}

/// 对文本执行高置信 secret 扫描；返回全部命中（空 = 通过）。
pub fn scan_text(text: &str) -> Vec<SecretFinding> {
    let mut findings = Vec::new();
    let mut push = |reason_code: &'static str| {
        if !findings.iter().any(|f: &SecretFinding| f.reason_code == reason_code) {
            findings.push(SecretFinding { reason_code });
        }
    };

    // 1. PEM 私钥块（高置信）。
    if text.contains("-----BEGIN") && text.contains("PRIVATE KEY") {
        push("private_key_pem");
    }

    // 2. Bearer token（Authorization scheme + 16+ token 字符）。
    if bearer_regex().is_match(text) {
        push("bearer_token");
    }

    // 3. Authorization 赋值形态（值必须紧跟 token 字符；`authorization": {`
    //    这类 bundle 授权投影对象键不命中）。
    if auth_assign_regex().is_match(text) {
        push("authorization_header");
    }

    // 4. 本地 credential store / session-store。
    if text.to_ascii_lowercase().contains("credentials.bin") {
        push("local_credential_store");
    }
    if set_cookie_regex().is_match(text) {
        push("cookie_session_secret");
    }

    // 5. lease / fencing secret 字段赋值形态（本系统 lease token 只存在于
    //    daemon lease 槽，绝不应出现在 prompt 数据面）。
    if lease_fencing_regex().is_match(text) {
        push("lease_fencing_secret");
    }

    // 6. 可复用 API key 高置信前缀形态。
    if api_key_regex().is_match(text) {
        push("reusable_api_key");
    }

    // 7. 裸 64-hex（无 `sha256:` 前缀）：credential hash 风险。contract/
    //    prompt hash 的唯一合法形式是 `sha256:` 前缀（spec §9.3），裸 64-hex
    //    不应出现在数据面。已知非 secret 的固定引用（冻结 spec SHA 等
    //    daemon-owned 资产自述 hash）显式豁免。
    for capture in bare_hex64_regex().find_iter(text) {
        let matched = capture.as_str();
        let hex_core = matched
            .trim_matches(|c: char| !c.is_ascii_hexdigit())
            .to_ascii_uppercase();
        if non_secret_hex_refs().contains(&hex_core) {
            continue;
        }
        push("bare_credential_hash");
        break;
    }

    findings
}

/// 便捷封装：扫描通过返回 Ok，命中返回 (code, reason_codes) 形式的错误
/// 消息（不含 secret 内容）。
pub fn ensure_clean(text: &str) -> Result<(), String> {
    let findings = scan_text(text);
    if findings.is_empty() {
        return Ok(());
    }
    let codes: Vec<&'static str> = findings.iter().map(|f| f.reason_code).collect();
    Err(format!(
        "{}|{}",
        E_TASK_PROMPT_SECRET_DETECTED,
        codes.join(",")
    ))
}

fn bearer_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/-]{16,}").expect("valid bearer regex")
    })
}

fn auth_assign_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // 值形态：[=:] 后直接跟引号或 token 字符（`{` 与空白+`{` 为对象键，不命中）。
        Regex::new(r#"(?i)"?authorization"?[ \t]*[=:][ \t]*["']?[A-Za-z0-9]"#)
            .expect("valid auth regex")
    })
}

fn set_cookie_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)\bset-cookie[ \t]*[=:]").expect("valid cookie regex"))
}

fn lease_fencing_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"(?i)"?(lease[_-]?token|fencing[_-]?token|fencing[_-]?secret|lease[_-]?secret)"?[ \t]*[=:][ \t]*["']?[A-Za-z0-9]"#)
            .expect("valid lease regex")
    })
}

fn api_key_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"\b(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xoxb-[0-9A-Za-z-]{20,})")
            .expect("valid api key regex")
    })
}

fn bare_hex64_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // 负向前瞻不可用（rust regex 无 look-around）：先排除 `sha256:` 前缀
        // 形态——匹配以非冒号/hex 分隔的 64-hex 序列。"sha256:<hex>" 中 hex
        // 前面是冒号，用 `(?:^|[^:0-9a-fA-F])` 边界即可让冒号前缀不命中。
        Regex::new(r"(?:^|[^:0-9a-zA-Z])[0-9a-fA-F]{64}(?:$|[^0-9a-zA-Z])")
            .expect("valid hex64 regex")
    })
}

/// CR4 本体（review §5.7 重定性）：写入侧 hash 引用规范化。
///
/// 对任务自由文本（title/description/report note/handoff message 等）中的
/// **裸 64-hex** 引用统一补写 `sha256:` 前缀——spec §9.3 规定 hash 的唯一
/// 合法数据面形式是 `sha256:<64-hex>`，裸形式会被本模块扫描 fail-closed。
/// 与 `scan_text` 的 bare_hex64 规则共享同一套边界语义（冒号前缀形态、
/// 更长 hex 串、65+ 长度均不命中），保证"写入侧规范化过的文本，扫描必过"。
///
/// 只做前缀补写，不改 hex 内容与大小写；豁免集（KNOWN_NON_SECRET_HEX_REFS）
/// 同样被规范化——统一形式对 authority 数据无害。
///
/// 适用边界：仅自由文本字段。结构化 hash 字段（evidence_hash/payload_hash/
/// contract hash）是程序化比对字段，存在裸字符串相等比较（如 gate evidence
/// 匹配），禁止在写入侧改写格式。
pub fn normalize_bare_sha256_refs(text: &str) -> String {
    // 手写扫描（regex replace_all 会因边界字符被相邻匹配消费而漏掉
    // 相邻 hash）：找极大 hex run，按 scanner 同款边界判定。
    let bytes = text.as_bytes();
    let is_hex = |b: u8| b.is_ascii_hexdigit();
    // scanner post 边界：非 [0-9a-zA-Z]；prev 边界额外排除冒号
    // （"sha256:<hex>" 的 hex 前是冒号 → 前缀形态不命中）。
    let is_post_boundary = |b: u8| !b.is_ascii_alphanumeric();
    let is_prev_boundary = |b: u8| !b.is_ascii_alphanumeric() && b != b':';
    let mut out = String::with_capacity(text.len() + 16);
    let mut i = 0usize;
    while i < text.len() {
        if is_hex(bytes[i]) {
            let start = i;
            while i < text.len() && is_hex(bytes[i]) {
                i += 1;
            }
            let run_len = i - start;
            let prev_ok = start == 0 || is_prev_boundary(bytes[start - 1]);
            let next_ok = i == text.len() || is_post_boundary(bytes[i]);
            if run_len == 64 && prev_ok && next_ok {
                out.push_str("sha256:");
            }
            out.push_str(&text[start..i]);
        } else {
            let ch_len = text[i..].chars().next().map(|c| c.len_utf8()).unwrap_or(1);
            out.push_str(&text[i..i + ch_len]);
            i += ch_len;
        }
    }
    out
}

