//! Phase 5 T-1783751519227-18d8: 输入规范化入口
//!
//! 按照 docs/design/parse-input-abi.md §2.1 规范实现：
//! - BOM 检测 + 剥离（UTF-8 / UTF-16 LE / UTF-16 BE）
//! - 流式解码（UTF-16 LE/BE → UTF-8，或 UTF-8 with latin-1 fallback）
//! - CRLF / lone CR → LF 归一化
//! - SHA-256 content_hash（基于 canonical bytes，不基于原始磁盘字节）
//!
//! 设计原则：canonicalize_source 是输入规范化的唯一入口，
//! delta.rs 等调用方必须先 canonicalize 再 parse，禁止直接读文件喂给 parser。

use std::io;

use sha2::{Digest, Sha256};

// ============================================
// 数据结构
// ============================================

/// 源文件元数据（原始文件的编码信息）
#[derive(Clone, Debug)]
pub struct SourceMetadata {
    /// 原始磁盘字节的 SHA-256 hex
    pub raw_hash: String,
    /// 检测到的源编码（"utf-8" / "utf-16-le" / "utf-16-be" / "latin-1"）
    pub source_encoding: String,
    /// BOM 类型（"utf-8" / "utf-16-le" / "utf-16-be" / "none"）
    pub bom_kind: String,
    /// 换行风格（"crlf" / "lf" / "cr" / "none"）
    pub newline_style: String,
}

/// 规范化结果
#[derive(Clone, Debug)]
pub struct CanonicalizeResult {
    /// 规范化后的字节（BOM 剥离 + UTF-8 编码 + LF 换行）
    pub canonical_bytes: Vec<u8>,
    /// sha256(canonical_bytes)，用于 CAS / delta 对比
    pub content_hash: String,
    /// 原始文件元数据
    pub metadata: SourceMetadata,
    /// canonical_bytes.len()
    pub canonical_total: usize,
    /// raw.len()（含 BOM）
    pub raw_total: usize,
}

// ============================================
// 工具函数
// ============================================

/// 计算 SHA-256 hex 字符串
pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let hash = hasher.finalize();
    format!("{:x}", hash)
}

/// BOM 检测 + 剥离
///
/// 返回 (bom_kind, bom_len, bytes_no_bom)：
/// - UTF-8 BOM:    EF BB BF → bom_kind="utf-8", bom_len=3
/// - UTF-16 LE BOM: FF FE   → bom_kind="utf-16-le", bom_len=2
/// - UTF-16 BE BOM: FE FF   → bom_kind="utf-16-be", bom_len=2
/// - 无 BOM:                → bom_kind="none", bom_len=0
fn detect_and_strip_bom(raw: &[u8]) -> (&'static str, usize, &[u8]) {
    // UTF-8 BOM（3 字节）
    if raw.len() >= 3 && raw[0] == 0xEF && raw[1] == 0xBB && raw[2] == 0xBF {
        return ("utf-8", 3, &raw[3..]);
    }
    // UTF-16 LE BOM（2 字节）
    if raw.len() >= 2 && raw[0] == 0xFF && raw[1] == 0xFE {
        return ("utf-16-le", 2, &raw[2..]);
    }
    // UTF-16 BE BOM（2 字节）
    if raw.len() >= 2 && raw[0] == 0xFE && raw[1] == 0xFF {
        return ("utf-16-be", 2, &raw[2..]);
    }
    ("none", 0, raw)
}

// ============================================
// 流式解码
// ============================================

/// 流式解码（parse 路径，不构建 offset_map）
///
/// 返回 (encoding, newline_style, canonical_bytes)：
/// - 根据 BOM 决定解码方式
/// - CRLF 作为整体输入单元 → emit 单个 \n
/// - lone CR → emit \n
/// - lone LF → emit \n
fn streaming_decode(bytes_no_bom: &[u8], bom_kind: &str) -> (String, String, Vec<u8>) {
    // 1. 根据 BOM 决定解码方式，先得到 Unicode scalar 序列
    let (encoding, decoded_chars): (String, Vec<char>) = match bom_kind {
        "utf-16-le" => {
            let chars = decode_utf16_le(bytes_no_bom);
            ("utf-16-le".to_string(), chars)
        }
        "utf-16-be" => {
            let chars = decode_utf16_be(bytes_no_bom);
            ("utf-16-be".to_string(), chars)
        }
        _ => {
            // UTF-8 BOM 或无 BOM：假设 UTF-8，无效序列回退 latin-1
            decode_utf8_or_latin1(bytes_no_bom)
        }
    };

    // 2. CRLF / CR → LF 归一化（按 parse-input-abi.md §2.2 状态机）
    let mut canonical: Vec<u8> = Vec::with_capacity(decoded_chars.len());
    let mut saw_crlf = false;
    let mut saw_lone_cr = false;
    let mut saw_lone_lf = false;
    let mut cr_pending = false;

    for scalar in decoded_chars {
        match scalar {
            '\r' => {
                // CR：先 emit \n（lone CR 或 CRLF 开头）
                canonical.push(b'\n');
                cr_pending = true;
            }
            '\n' => {
                if cr_pending {
                    // CRLF：\r 已 emit \n，这里只推进（不重复 emit）
                    saw_crlf = true;
                    cr_pending = false;
                } else {
                    // lone LF
                    canonical.push(b'\n');
                    saw_lone_lf = true;
                }
            }
            _ => {
                if cr_pending {
                    // \r 后跟非 \n：\r 已 emit \n（lone CR）
                    saw_lone_cr = true;
                    cr_pending = false;
                }
                let mut buf = [0u8; 4];
                let s = scalar.encode_utf8(&mut buf);
                canonical.extend_from_slice(s.as_bytes());
            }
        }
    }
    // 流末尾 cr_pending：最后一个 scalar 是 \r，已 emit \n
    if cr_pending {
        saw_lone_cr = true;
    }

    let newline_style = if saw_crlf {
        "crlf"
    } else if saw_lone_lf {
        "lf"
    } else if saw_lone_cr {
        "cr"
    } else {
        "none"
    };

    (encoding, newline_style.to_string(), canonical)
}

/// UTF-16 LE 解码（含 surrogate pair 处理）
fn decode_utf16_le(bytes: &[u8]) -> Vec<char> {
    let mut chars = Vec::with_capacity(bytes.len() / 2);
    let mut i = 0;
    while i + 1 < bytes.len() {
        let unit = u16::from_le_bytes([bytes[i], bytes[i + 1]]);
        i += 2;

        if (0xD800..=0xDBFF).contains(&unit) {
            // High surrogate，需要后跟 low surrogate
            if i + 1 < bytes.len() {
                let low = u16::from_le_bytes([bytes[i], bytes[i + 1]]);
                if (0xDC00..=0xDFFF).contains(&low) {
                    i += 2;
                    let cp = 0x10000 + ((unit as u32 - 0xD800) << 10) + (low as u32 - 0xDC00);
                    if let Some(c) = char::from_u32(cp) {
                        chars.push(c);
                    }
                } else {
                    // 孤立 high surrogate，跳过
                }
            }
        } else if !(0xDC00..=0xDFFF).contains(&unit) {
            // BMP 字符
            if let Some(c) = char::from_u32(unit as u32) {
                chars.push(c);
            }
        }
        // 孤立 low surrogate，跳过
    }
    chars
}

/// UTF-16 BE 解码（含 surrogate pair 处理）
fn decode_utf16_be(bytes: &[u8]) -> Vec<char> {
    let mut chars = Vec::with_capacity(bytes.len() / 2);
    let mut i = 0;
    while i + 1 < bytes.len() {
        let unit = u16::from_be_bytes([bytes[i], bytes[i + 1]]);
        i += 2;

        if (0xD800..=0xDBFF).contains(&unit) {
            if i + 1 < bytes.len() {
                let low = u16::from_be_bytes([bytes[i], bytes[i + 1]]);
                if (0xDC00..=0xDFFF).contains(&low) {
                    i += 2;
                    let cp = 0x10000 + ((unit as u32 - 0xD800) << 10) + (low as u32 - 0xDC00);
                    if let Some(c) = char::from_u32(cp) {
                        chars.push(c);
                    }
                }
            }
        } else if !(0xDC00..=0xDFFF).contains(&unit) {
            if let Some(c) = char::from_u32(unit as u32) {
                chars.push(c);
            }
        }
    }
    chars
}

/// UTF-8 解码，无效序列回退 latin-1
///
/// 返回 (encoding, chars)：
/// - 全部有效 UTF-8 → ("utf-8", chars)
/// - 存在无效序列 → ("latin-1", chars)（无效字节按 latin-1 单字节解码）
fn decode_utf8_or_latin1(bytes: &[u8]) -> (String, Vec<char>) {
    let mut chars = Vec::with_capacity(bytes.len());
    let mut all_valid_utf8 = true;
    let mut i = 0;

    while i < bytes.len() {
        match decode_one_utf8(&bytes[i..]) {
            Some((c, consumed)) => {
                chars.push(c);
                i += consumed;
            }
            None => {
                all_valid_utf8 = false;
                // latin-1 fallback：单字节解码
                chars.push(bytes[i] as char);
                i += 1;
            }
        }
    }

    let encoding = if all_valid_utf8 { "utf-8" } else { "latin-1" };
    (encoding.to_string(), chars)
}

/// 尝试从字节流头部解码一个 UTF-8 字符
///
/// 返回 Some((char, consumed_bytes)) 或 None（无效 UTF-8 序列）
fn decode_one_utf8(bytes: &[u8]) -> Option<(char, usize)> {
    if bytes.is_empty() {
        return None;
    }
    let b0 = bytes[0];

    // 1-byte sequence（ASCII）
    if b0 < 0x80 {
        return Some((b0 as char, 1));
    }

    // 2-byte sequence
    if (0xC2..=0xDF).contains(&b0) && bytes.len() >= 2 {
        let b1 = bytes[1];
        if (0x80..=0xBF).contains(&b1) {
            let cp = ((b0 as u32 & 0x1F) << 6) | (b1 as u32 & 0x3F);
            if let Some(c) = char::from_u32(cp) {
                return Some((c, 2));
            }
        }
        return None;
    }

    // 3-byte sequence
    if (0xE0..=0xEF).contains(&b0) && bytes.len() >= 3 {
        let b1 = bytes[1];
        let b2 = bytes[2];
        // overlong 检查
        if b0 == 0xE0 && !(0xA0..=0xBF).contains(&b1) {
            return None;
        }
        // surrogate 检查
        if b0 == 0xED && !(0x80..=0x9F).contains(&b1) {
            return None;
        }
        if !(0x80..=0xBF).contains(&b1) || !(0x80..=0xBF).contains(&b2) {
            return None;
        }
        let cp = ((b0 as u32 & 0x0F) << 12) | ((b1 as u32 & 0x3F) << 6) | (b2 as u32 & 0x3F);
        if let Some(c) = char::from_u32(cp) {
            return Some((c, 3));
        }
        return None;
    }

    // 4-byte sequence
    if (0xF0..=0xF4).contains(&b0) && bytes.len() >= 4 {
        let b1 = bytes[1];
        let b2 = bytes[2];
        let b3 = bytes[3];
        // overlong 检查
        if b0 == 0xF0 && !(0x90..=0xBF).contains(&b1) {
            return None;
        }
        // 超出 Unicode 范围检查
        if b0 == 0xF4 && b1 > 0x8F {
            return None;
        }
        if !(0x80..=0xBF).contains(&b1)
            || !(0x80..=0xBF).contains(&b2)
            || !(0x80..=0xBF).contains(&b3)
        {
            return None;
        }
        let cp = ((b0 as u32 & 0x07) << 18)
            | ((b1 as u32 & 0x3F) << 12)
            | ((b2 as u32 & 0x3F) << 6)
            | (b3 as u32 & 0x3F);
        if let Some(c) = char::from_u32(cp) {
            return Some((c, 4));
        }
        return None;
    }

    None
}

// ============================================
// 入口函数
// ============================================

/// canonicalize_source — 输入规范化的唯一入口
///
/// 按照 parse-input-abi.md §2.1 规范：
/// 1. 读取原始字节
/// 2. 计算 raw_hash（SHA-256）
/// 3. BOM 检测 + 剥离
/// 4. 流式解码（UTF-16 → UTF-8，或 UTF-8 with latin-1 fallback）
/// 5. CRLF / CR → LF 归一化
/// 6. 计算 content_hash（基于 canonical bytes）
pub fn canonicalize_source(abs_path: &str) -> Result<CanonicalizeResult, io::Error> {
    // 1. 读取原始字节
    let raw = std::fs::read(abs_path)?;
    let raw_hash = sha256_hex(&raw);
    let raw_total = raw.len();

    // 2. BOM 检测 + 剥离
    let (bom_kind, _bom_len, bytes_no_bom) = detect_and_strip_bom(&raw);

    // 3. 流式解码（不构建 offset_map，O(1) 额外内存）
    let (source_encoding, newline_style, canonical_bytes) =
        streaming_decode(bytes_no_bom, bom_kind);

    let canonical_total = canonical_bytes.len();
    let content_hash = sha256_hex(&canonical_bytes);

    Ok(CanonicalizeResult {
        canonical_bytes,
        content_hash,
        metadata: SourceMetadata {
            raw_hash,
            source_encoding,
            bom_kind: bom_kind.to_string(),
            newline_style,
        },
        canonical_total,
        raw_total,
    })
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::atomic::{AtomicU64, Ordering};

    // 原子计数器，避免并发测试中文件名冲突
    static FILE_COUNTER: AtomicU64 = AtomicU64::new(0);

    /// 辅助：将字节写入临时文件并 canonicalize
    fn canonicalize_bytes(raw: &[u8]) -> CanonicalizeResult {
        let id = FILE_COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!(
            "callwarden_canonicalize_test_{}_{}.tmp",
            std::process::id(),
            id
        ));
        {
            let mut f = std::fs::File::create(&path).unwrap();
            f.write_all(raw).unwrap();
            f.flush().unwrap();
        }
        let path_str = path.to_string_lossy().to_string();
        let result = canonicalize_source(&path_str).unwrap();
        let _ = std::fs::remove_file(&path);
        result
    }

    #[test]
    fn test_sha256_hex_empty() {
        // 空字节的 SHA-256
        let hex = sha256_hex(b"");
        assert_eq!(
            hex,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn test_sha256_hex_abc() {
        let hex = sha256_hex(b"abc");
        assert_eq!(
            hex,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn test_utf8_no_bom() {
        // UTF-8 无 BOM：canonical == input，newline_style == "lf"
        let result = canonicalize_bytes(b"a\nb");
        assert_eq!(result.canonical_bytes, b"a\nb");
        assert_eq!(result.metadata.bom_kind, "none");
        assert_eq!(result.metadata.source_encoding, "utf-8");
        assert_eq!(result.metadata.newline_style, "lf");
        assert_eq!(result.canonical_total, 3);
        assert_eq!(result.raw_total, 3);
    }

    #[test]
    fn test_crlf_to_lf() {
        // CRLF → LF：b"a\r\nb" → canonical b"a\nb"，newline_style == "crlf"
        let result = canonicalize_bytes(b"a\r\nb");
        assert_eq!(result.canonical_bytes, b"a\nb");
        assert_eq!(result.metadata.newline_style, "crlf");
        assert_eq!(result.metadata.bom_kind, "none");
    }

    #[test]
    fn test_utf8_bom_stripped() {
        // UTF-8 BOM 剥离：b"\xef\xbb\xbfa" → canonical b"a"，bom_kind == "utf-8"
        let result = canonicalize_bytes(b"\xef\xbb\xbfa");
        assert_eq!(result.canonical_bytes, b"a");
        assert_eq!(result.metadata.bom_kind, "utf-8");
        assert_eq!(result.metadata.source_encoding, "utf-8");
    }

    #[test]
    fn test_content_hash_is_sha256_of_canonical() {
        // content_hash 是 canonical bytes 的 SHA-256
        let result = canonicalize_bytes(b"hello\n");
        let expected = sha256_hex(b"hello\n");
        assert_eq!(result.content_hash, expected);
    }

    #[test]
    fn test_utf16_le_bom() {
        // UTF-16 LE BOM + "A"：→ canonical b"A"
        let result = canonicalize_bytes(b"\xff\xfe\x41\x00");
        assert_eq!(result.canonical_bytes, b"A");
        assert_eq!(result.metadata.bom_kind, "utf-16-le");
        assert_eq!(result.metadata.source_encoding, "utf-16-le");
    }

    #[test]
    fn test_utf16_be_bom() {
        // UTF-16 BE BOM + "A"：→ canonical b"A"
        let result = canonicalize_bytes(b"\xfe\xff\x00\x41");
        assert_eq!(result.canonical_bytes, b"A");
        assert_eq!(result.metadata.bom_kind, "utf-16-be");
        assert_eq!(result.metadata.source_encoding, "utf-16-be");
    }

    #[test]
    fn test_utf16_le_surrogate_pair() {
        // UTF-16 LE + emoji 😀（U+1F600）：surrogate pair D83D DE00
        let result = canonicalize_bytes(b"\xff\xfe\x3d\xd8\x00\xde");
        assert_eq!(result.canonical_bytes, b"\xf0\x9f\x98\x80");
        assert_eq!(result.metadata.bom_kind, "utf-16-le");
    }

    #[test]
    fn test_mixed_crlf_lf() {
        // \r\n\n：CRLF + lone LF → \n\n
        let result = canonicalize_bytes(b"\r\n\n");
        assert_eq!(result.canonical_bytes, b"\n\n");
        assert_eq!(result.metadata.newline_style, "crlf");
    }

    #[test]
    fn test_lone_cr() {
        // lone CR：a\rb → a\nb
        let result = canonicalize_bytes(b"a\rb");
        assert_eq!(result.canonical_bytes, b"a\nb");
        assert_eq!(result.metadata.newline_style, "cr");
    }

    #[test]
    fn test_no_newline() {
        // 无换行
        let result = canonicalize_bytes(b"hello");
        assert_eq!(result.canonical_bytes, b"hello");
        assert_eq!(result.metadata.newline_style, "none");
    }

    #[test]
    fn test_empty_file() {
        // 空文件
        let result = canonicalize_bytes(b"");
        assert_eq!(result.canonical_bytes, b"");
        assert_eq!(result.canonical_total, 0);
        assert_eq!(result.raw_total, 0);
        assert_eq!(result.metadata.bom_kind, "none");
        assert_eq!(result.metadata.newline_style, "none");
    }

    #[test]
    fn test_cjk_utf8() {
        // CJK 字符 UTF-8 编码：中 = E4 B8 AD
        let result = canonicalize_bytes(b"\xe4\xb8\xad");
        assert_eq!(result.canonical_bytes, b"\xe4\xb8\xad");
        assert_eq!(result.metadata.source_encoding, "utf-8");
    }

    #[test]
    fn test_invalid_utf8_fallback_latin1() {
        // 无效 UTF-8 序列（overlong）：0xC0 0xAF
        let result = canonicalize_bytes(b"\xc0\xaf");
        assert_eq!(result.metadata.source_encoding, "latin-1");
        // latin-1 解码：0xC0 → À, 0xAF → ¯
        assert_eq!(result.canonical_bytes.len(), 4); // À (2 bytes) + ¯ (2 bytes) in UTF-8
    }

    #[test]
    fn test_raw_hash_differs_from_content_hash() {
        // UTF-8 BOM 文件：raw_hash != content_hash（raw 含 BOM，canonical 不含）
        let result = canonicalize_bytes(b"\xef\xbb\xbfhello");
        assert_ne!(result.metadata.raw_hash, result.content_hash);
    }

    #[test]
    fn test_nonexistent_file() {
        let result = canonicalize_source("/nonexistent/path/file.txt");
        assert!(result.is_err());
    }
}
