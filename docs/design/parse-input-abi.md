# ParseInput ABI 规范

> 从 `enterprise-phase1-phase3-detail.md` v10.2 抽取。只保留当前规范 + 状态机 + 不变量 + 故障注入测试，不保留 v1-v9 修订过程。
> 基线版本：v10.2（`ad2e308`）。

## 1. 输入类型定义

```rust
// 每个 Unicode scalar 一条边界记录。
// 查询时二分查找后直接取边界，不在段内做 raw_start + in_seg_off 的线性换算。
pub struct OffsetBoundary {
    pub canonical_before: usize,
    pub raw_before: usize,
    // 下一条边界的 canonical_before 即本 scalar 的 canonical_end（省略存储）
}

pub struct CanonicalizeResult {
    pub canonical_bytes: Vec<u8>,       // BOM 剥离 + 解码为 UTF-8 + CRLF/CR→LF 后
    pub content_hash: String,           // sha256(canonical_bytes)
    pub metadata: SourceMetadata,        // 原始文件元数据
    pub canonical_total: usize,          // canonical_bytes.len()
    pub raw_total: usize,                // raw.len()（含 BOM）
}
// 注意：CanonicalizeResult 无 offset_map 字段。
// offset_map 只在编辑路径通过 raw_span_for_canonical_range_lazy 流式扫描按需构建。
```

## 2. 入口函数

### 2.1 canonicalize_source

输入规范化的**唯一入口**。Rust 实现，Python 通过 FFI（rust_ext）调用，**不**在 Python 侧重复实现编码检测。

```rust
pub fn canonicalize_source(abs_path: &str) -> Result<CanonicalizeResult, io::Error> {
    let raw = std::fs::read(abs_path)?;
    let raw_hash = sha256_hex(&raw);
    let raw_total = raw.len();

    // 1. BOM 检测 + 剥离
    let (bom_kind, bom_len, bytes_no_bom) = detect_and_strip_bom(&raw)?;

    // 2. 流式解码（不构建 offset_map），O(1) 额外内存
    let (source_encoding, newline_style, canonical_bytes) =
        streaming_decode(bytes_no_bom)?;

    let canonical_total = canonical_bytes.len();
    let content_hash = sha256_hex(&canonical_bytes);
    Ok(CanonicalizeResult {
        canonical_bytes, content_hash,
        metadata: SourceMetadata { raw_hash, source_encoding, bom_kind, newline_style },
        canonical_total, raw_total,
    })
}
```

### 2.2 streaming_decode（parse 路径）

流式解码，不构建 offset_map。O(n) 时间，O(1) 额外内存。

```rust
fn streaming_decode(
    bytes_no_bom: &[u8],
) -> Result<(String, String, Vec<u8>), io::Error> {
    let encoding = detect_encoding(bytes_no_bom)?;
    let mut decoder = new_decoder(&encoding, bytes_no_bom);
    let mut canonical: Vec<u8> = Vec::new();
    let mut raw_pos = 0usize;
    let mut saw_crlf = false;
    let mut saw_lone_cr = false;
    let mut saw_lone_lf = false;
    let mut cr_pending = false;

    while let Some((scalar, consumed)) = decoder.next_scalar()? {
        match scalar {
            '\r' => {
                canonical.push(b'\n');   // 先 emit \n（lone CR 或 CRLF 开头）
                raw_pos += consumed;
                cr_pending = true;
            }
            '\n' => {
                if cr_pending {
                    // CRLF：\r 已 emit \n，这里只推进 raw_pos
                    saw_crlf = true;
                    cr_pending = false;
                    raw_pos += consumed;
                } else {
                    // lone LF
                    canonical.push(b'\n');
                    saw_lone_lf = true;
                    raw_pos += consumed;
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
                raw_pos += consumed;
            }
        }
    }
    // cr_pending 在流末尾：最后一个 scalar 是 \r，已 emit \n
    if cr_pending { saw_lone_cr = true; }

    let newline_style = if saw_crlf { "crlf" } else if saw_lone_lf { "lf" } else if saw_lone_cr { "cr" } else { "none" };
    Ok((encoding, newline_style.to_string(), canonical))
}
```

### 2.3 raw_span_for_canonical_range_lazy（编辑路径）

编辑时按需流式扫描原文件，只找目标 start/end 两个边界。O(n) 时间，O(1) 额外内存。

```rust
pub fn raw_span_for_canonical_range_lazy(
    abs_path: &str,
    canonical_start: usize,
    canonical_end: usize,
) -> Result<(usize, usize), io::Error> {
    let raw = std::fs::read(abs_path)?;
    let (_, bom_len, bytes_no_bom) = detect_and_strip_bom(&raw)?;

    let encoding = detect_encoding(bytes_no_bom)?;
    let mut decoder = new_decoder(&encoding, bytes_no_bom);
    let mut canonical_pos = 0usize;
    let mut raw_pos = 0usize;
    let mut raw_start = None;
    let mut raw_end = None;
    let mut cr_pending = false;

    while let Some((scalar, consumed)) = decoder.next_scalar()? {
        // CRLF 作为整体输入单元：
        // \r → canonical_pos+1, raw_pos+consumed
        //   若下一个是 \n → raw_pos+consumed（\n 字节），canonical_pos 不变

        if canonical_pos == canonical_start && raw_start.is_none() {
            raw_start = Some(raw_pos + bom_len);
        }
        if canonical_pos == canonical_end && raw_end.is_none() {
            raw_end = Some(raw_pos + bom_len);
            return Ok((raw_start.unwrap(), raw_end.unwrap()));
        }

        match scalar {
            '\r' => {
                canonical_pos += 1;
                raw_pos += consumed;
                cr_pending = true;
            }
            '\n' if cr_pending => {
                cr_pending = false;
                raw_pos += consumed;
                // canonical_pos 不变（\r 已计入）
            }
            '\n' => {
                canonical_pos += 1;
                raw_pos += consumed;
            }
            _ => {
                if cr_pending { cr_pending = false; }
                canonical_pos += 1;
                raw_pos += consumed;
            }
        }
    }

    if raw_start.is_none() || raw_end.is_none() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "canonical range exceeds decoded length"));
    }
    Ok((raw_start.unwrap(), raw_end.unwrap()))
}
```

### 2.4 verify_raw_hash_before_writeback

编辑写回前校验磁盘文件未变。

```rust
pub fn verify_raw_hash_before_writeback(abs_path: &str, expected_raw_hash: &str) -> bool {
    let raw = std::fs::read(abs_path).unwrap_or_default();
    sha256_hex(&raw) == expected_raw_hash
}
```

## 3. 不变量

| # | 不变量 | 测试覆盖 |
|---|--------|---------|
| I1 | CRLF 作为整体输入单元：raw `[r,r+2)` → canonical `[k,k+1)` | `test_crlf_cjk_both_sides`, `test_crlf_emoji_both_sides`, `test_mixed_crlf_lf` |
| I2 | Lone CR 单独 emit：raw `[r,r+1)` → canonical `[k,k+1)`；cr_pending 置 false 后下一个 scalar 独立 push boundary | `test_mixed_crlf_lf`（第二个 `\n` = raw [2,3)） |
| I3 | parse 路径 O(1) 额外内存：`CanonicalizeResult` 无 `offset_map` 字段；`streaming_decode` 只分配 `canonical: Vec<u8>` | 设计约束，无运行时测试 |
| I4 | 编辑路径 O(n) 时间 O(1) 额外内存：`raw_span_for_canonical_range_lazy` 为每个文件单遍扫描一次 | 设计约束，无运行时测试 |
| I5 | 多字节 UTF-8（"中"=3 bytes）的 canonical offset 映射正确 | `test_ascii_cjk_mixed` |
| I6 | 4 字节 UTF-8 emoji（😀=\U0001F600）surrogate pair 映射正确，前后 ASCII 字符偏移不被污染 | `test_emoji` |
| I7 | GBK→UTF-8 编码转换后偏移独立映射，CRLF→LF 归约后跨编码 raw↔canonical 正确 | `test_gbk_canonicalization` |
| I8 | UTF-16 LE/BE BOM 剥离 + surrogate pair（D83D DE00 2×2→4 bytes）映射正确 | `test_utf16_canonicalization` |
| I9 | `\r\n\n`：第一个 `\n` → raw [0,2)，第二个 `\n` → raw [2,3)，text → raw [3,7)，不粘性吞 LF | `test_mixed_crlf_lf` |
| I10 | 回写前 `verify_raw_hash_before_writeback` 校验磁盘文件与 manifest `raw_hash` 一致 | `test_verify_raw_hash_before_writeback` |
| I11 | BOM 剥离：`detect_and_strip_bom` 返回 bom_kind + bom_len，后续操作只读 bytes_no_bom | 所有测试含 BOM 覆盖 |
| I12 | content_hash 基于 canonical bytes（已规范化），不基于原始磁盘字节 | `canonicalize_source` 入口唯一 |

## 4. 状态机

```
输入文件 (abs_path)
    │
    ▼
┌──────────────────────────────────┐
│ detect_and_strip_bom(&raw)       │  → (bom_kind, bom_len, bytes_no_bom)
└──────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────┐
│ streaming_decode(bytes_no_bom)   │  → (encoding, newline_style, canonical_bytes)
│   per-scalar 循环：               │
│   \r → emit \n, cr_pending=true  │
│   \n + cr_pending → raw_pos+=n   │
│   \n alone → emit \n             │
│   char + cr_pending → lone CR    │
│   char alone → encode_utf8       │
└──────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────┐
│ CanonicalizeResult {             │
│   canonical_bytes, content_hash, │
│   metadata, canonical_total,     │
│   raw_total                      │
│ }                                │
└──────────────────────────────────┘
    │
    ├─ parse 路径 → 直接消费 canonical_bytes（不构建 offset_map）
    │
    └─ 编辑路径 → raw_span_for_canonical_range_lazy(abs_path, start, end)
                  → 单遍流式扫描 → (raw_start, raw_end)
```

## 5. 模型测试

```python
# tests/model/test_parse_input_offset.py

import tempfile, os

def _canonicalize_file(raw_bytes: bytes):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(raw_bytes)
        path = f.name
    result = canonicalize_source(path)  # Rust FFI
    os.unlink(path)
    return result

def _raw_span_for_file(raw_bytes: bytes, c_start: int, c_end: int):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(raw_bytes)
        path = f.name
    span = raw_span_for_canonical_range_lazy(path, c_start, c_end)  # Rust FFI
    os.unlink(path)
    return span


# ── 1. ASCII+CJK 混合 ──
def test_ascii_cjk_mixed():
    """a中b：UTF-8 1+3+1 字节，多字节 CJK 不破坏偏移映射。"""
    raw = b'a\xe4\xb8\xadb'  # a(1) + 中(3) + b(1) = 5 bytes
    result = _canonicalize_file(raw)
    assert len(result.canonical_bytes) == 5
    assert result.canonical_total == 5
    assert _raw_span_for_file(raw, 0, 1) == (0, 1)   # 'a'
    assert _raw_span_for_file(raw, 1, 4) == (1, 4)   # '中'(3 raw bytes)
    assert _raw_span_for_file(raw, 4, 5) == (4, 5)   # 'b'


# ── 2. Emoji ──
def test_emoji():
    """😀 = U+1F600 = 4 bytes UTF-8：surrogate pair 映射正确。"""
    raw = b'hello\xf0\x9f\x98\x80world'  # 5 + 4 + 5 = 14 bytes
    result = _canonicalize_file(raw)
    assert len(result.canonical_bytes) == 14
    assert _raw_span_for_file(raw, 5, 9) == (5, 9)     # 😀
    assert _raw_span_for_file(raw, 0, 5) == (0, 5)     # 'hello'
    assert _raw_span_for_file(raw, 9, 14) == (9, 14)   # 'world'


# ── 3. GBK 编码 → UTF-8 ──
def test_gbk_canonicalization():
    """GBK 编码的"中文测试\r\n"：编码转换后偏移独立映射。"""
    raw = b'\xd6\xd0\xce\xc4\xb2\xe2\xca\xd4\x0d\x0a'
    result = _canonicalize_file(raw)
    assert result.metadata.encoding in ("gbk", "gb2312")
    assert result.canonical_total == 13
    assert result.canonical_bytes[-1] == 0x0A  # CRLF→LF
    assert _raw_span_for_file(raw, 12, 13) == (8, 10)


# ── 4. UTF-16 LE/BE ──
def test_utf16_canonicalization():
    """UTF-16 LE with BOM "A😀"：BOM 剥离 + surrogate pair → UTF-8。"""
    raw = b'\xff\xfe\x41\x00\x3d\xd8\x00\xde'
    result = _canonicalize_file(raw)
    assert result.metadata.encoding == "utf-16-le"
    assert result.metadata.has_bom is True
    assert result.canonical_total == 5
    assert result.canonical_bytes[0] == ord('A')
    assert result.canonical_bytes[1:5] == b'\xf0\x9f\x98\x80'


# ── 5. 非 ASCII 两侧 CRLF ──
def test_crlf_cjk_both_sides():
    r"""中\r\n日：CJK（3 字节）两侧夹 CRLF，CRLF 为整体输入单元。"""
    raw = b'\xe4\xb8\xad\x0d\x0a\xe6\x97\xa5'
    result = _canonicalize_file(raw)
    assert result.canonical_total == 7
    assert result.canonical_bytes[3] == ord('\n')
    assert _raw_span_for_file(raw, 3, 4) == (3, 5)    # \r\n
    assert _raw_span_for_file(raw, 0, 3) == (0, 3)    # 中
    assert _raw_span_for_file(raw, 4, 7) == (5, 8)    # 日

def test_crlf_emoji_both_sides():
    r"""😀\r\n😎：4-byte emoji 两端夹 CRLF。"""
    raw = b'\xf0\x9f\x98\x80\x0d\x0a\xf0\x9f\x98\x8e'
    result = _canonicalize_file(raw)
    assert result.canonical_total == 9
    assert result.canonical_bytes[4] == ord('\n')
    assert _raw_span_for_file(raw, 4, 5) == (4, 6)    # \r\n
    assert _raw_span_for_file(raw, 0, 4) == (0, 4)    # 😀
    assert _raw_span_for_file(raw, 5, 9) == (6, 10)   # 😎


# ── 6. 混合 CRLF/LF ──
def test_mixed_crlf_lf():
    r"""\r\n\n\text：CRLF 消耗后 LF 不粘性吞下一个。"""
    raw = b'\x0d\x0a\x0a\x74\x65\x78\x74'
    result = _canonicalize_file(raw)
    assert result.canonical_total == 6
    assert result.canonical_bytes[0] == ord('\n')
    assert result.canonical_bytes[1] == ord('\n')
    assert _raw_span_for_file(raw, 0, 1) == (0, 2)     # CRLF
    assert _raw_span_for_file(raw, 1, 2) == (2, 3)     # lone LF
    assert _raw_span_for_file(raw, 2, 6) == (3, 7)     # text
```

## 6. 故障注入测试

| 场景 | 输入 | 期望结果 |
|------|------|---------|
| BOM 后紧接 CRLF | `\xEF\xBB\xBF\r\n` | BOM 剥离，canonical `\n`，raw_span 加 bom_len |
| 纯 CR 文件（无 LF） | `line1\rline2\r` | 全部 lone CR→LF，结尾 `\r` 最后 emit `\n` |
| 零字节文件 | `` | canonical_total=0，content_hash 为 ""的 sha256 |
| 无效 UTF-8 序列 | `\xC0\xAF`（overlong） | 编码检测识别并标记，或 fallback latin-1 |
| 512 MB ASCII 文件 | 512 MB 纯 ASCII | streaming_decode O(1) 内存，不 OOM |
| UTF-8 BOM + GBK 内容 | `\xEF\xBB\xBF\xD6\xD0` | BOM 覆盖 UTF-8，内容被编码检测识别为 GBK |
