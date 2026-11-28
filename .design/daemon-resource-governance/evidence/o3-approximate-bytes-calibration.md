# O3 口径校准记录：GraphStore::approximate_bytes（T3 step#0）

- 任务卡：T-1795832915536-9e838eec（REQ-RG-02 / C3 / O3）
- 决策人：executor 郑兑现（O3 为 open 未决项，contracts.md 授权 executor 校准并登记假设）
- 日期：2026-11-28

## 采用口径

`approximate_bytes()` = 四项求和，每项取**真实堆分配量**：

1. **by_id 字节**：`by_id.capacity() × size_of::<GraphSymbol>()`（48B POD）
   + 衍生索引数组 `by_qname_sorted_ids` / `by_simple_name_sorted_ids` /
   `search_entry_offsets` / `search_entry_sym_ids`（各 `capacity() × 4B`）
2. **CSR 边字节**：`forward_edges.capacity() × size_of::<CallEdge>()`（16B POD）
   + `forward_offsets` / `backward_offsets`（`capacity() × size_of::<usize>()`）
   + `backward_positions` / `callee_position_offsets` / `callee_positions` /
   `callee_names_offsets` / `callee_name_sorted_idxs` / `roots`（各 ×4B）
3. **字符串池字节**：`name_pool` / `qname_pool` / `module_pool` /
   `search_pool_lower`（各 `capacity()` 字节）
4. **文件路径表字节**：`file_paths_pool.capacity()` +
   `file_paths_offsets.capacity() × 4B`

## 登记的假设

- **capacity 而非 len**：Vec/String 的真实堆分配是 capacity；建图完成后两者
  通常相等（精确 push），但 snapshot 驻留期间以 capacity 为准更接近 RSS。
- **不含**：struct 自身大小、Arc 头部（16B/份）、内存对齐 padding、
  `Vec` 分配器的元数据开销——相对元素字节可忽略（<1%）。
- **衍生索引计入 by_id 项**：排序/搜索数组是符号表的必要索引内存（各
  与符号数同阶），不计会低估 30%+，与 ±20% 目标不符。
- **空 store（symbols/calls 均 None）= 0**。

## 验证

- `approximate_bytes_empty_store_is_zero`：空 store → 0。
- `approximate_bytes_known_scale_within_bounds`：1000 符号/500 边构造，
  断言 `est ≥ 元素原始字节和`（capacity ≥ len 且含全部池）且
  `est < 10 × 元素原始字节和`（辅助数组同阶不越界）。
- `approximate_bytes_scales_with_symbol_count`：规模翻倍 → 估值 ≥1.5×
  （防漏算大头）。

## 上界推导（为何 <10× 成立）

估值/元素原始字节和 ≈ (48 + 4×4 辅助 + 池) / (48 + 池) ≤ 2.5×；
边侧 ≈ (16 + 8×2 + 4×5) / 16 ≈ 3.25×（小规模时 offsets 数组占比高，
随边数增长趋近 1.4×）。全区间 < 10× 有余量。
