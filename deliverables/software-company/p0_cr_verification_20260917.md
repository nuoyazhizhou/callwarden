# P0-CR1/CR2/CR3 收编卡验证回执（T-1789046208235-309d2008）

验证日期：2026-09-17 ｜ 验证者：executor（独立核验，非采信原汇报）

## 重要披露：commit `79d3c83` 已不在当前历史

卡描述引用的修复提交 `79d3c83` 在当前 git 历史中**不存在**（`git show 79d3c83` →
`unknown revision`）。根因：git 对象库曾发生清扫事故，历史经两次灾难恢复重建：
`53b2958`（以 9855da6 为锚重建 5 个治理提交内容）与 `96fa721`（以 e064853 为锚
重建 b6fbcc0 全部 tracked 内容）。**代码内容在重建中保全，全部落在当前 HEAD 树内**，
仅原始 commit 哈希丢失。本回执以「当前 HEAD 源码 + 行为级实测」为验证依据。

## CR1：multi_lang GenericParser 管线接入（符号/调用不再恒为 0）

- `rust_ext/src/daemon/fs_handlers.rs`：
  - `parse_and_store_symbols`（~7.3KB 函数体）内含 `multi_lang` + `GenericParser`
    + `parse_file` 调用 —— 符号落库走多语言通用解析器
  - `parser_lang_id` 经 `multi_lang`/`GenericParser` 按语言派生 lang_id
  - `handle_build_graph` / `handle_build_directory` / `handle_refresh_file`
    均经 `scan_files` → `parse_and_store_symbols`（+ `parser_lang_id`）落库
- 行为级：workspace `4baea3ff12c2ea5c` 的权威 codegraph
  （`~/.callwarden/codegraph/4baea3ff12c2ea5c/codegraph.db`，只读查询）
  实测 **symbols=358、calls=8749**（非 0），与「symbol/calls 不再恒为 0」一致

## CR2：workspace 目录校验恢复（validate_owned_path_any）

- `rust_ext/src/daemon/workspace.rs`：`validate_owned_path_any` 存在
  （canonicalize + ACL 归一）
- `fs_handlers.rs` 的 `handle_build_graph` / `handle_build_directory` 函数体内
  均调用 `validate_owned_path_any`（目录递归校验随构建路径生效）

## CR3：schema fail-fast + scan 剪枝

- `fs_handlers.rs::open_codegraph_write` 文档与实现明确
  「写路径打开前必须先 `storage::initialize_or_migrate(path, 60)` 确保 schema 就绪」
  （半成品库残留导致 upsert/查询全部报错的旧缺陷修复）
- `handle_build_graph` / `handle_build_directory` 均经 `open_codegraph_write`
  打开写库；`scan_files` 做索引剪枝（`is_indexable_path` + ignore 目录加载）

## 行为级验收（本会话实测）

- `tests/test_p0_bugfixes.py`：**4 passed / 0 failed**（0.32s，
  `.venv_test/Scripts/python.exe`，`CW_TEST_MODE=1`，pin RUSTUP_HOME/CARGO_HOME）
  - 用例：test_b1_import_os / test_b2_issue_rules / test_b3_git_symbol_changes /
    test_end_to_end_with_real_db
- 注：该 4 用例为通用冒烟（不直接引用 CR1/CR2/CR3 符号），故补充上述源码级
  与 graph 库级实证作为等价覆盖

## 结论

收编的裸卡 `T-1788923523728-7a316cc4` 所述三项修复在当前 HEAD 树内**全部落地**
（源码实证 + graph 库非空 + 冒烟 4/4）。验证通过，可入 review。
