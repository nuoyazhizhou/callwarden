# TP-08C CLI 真实树/参数提取合同（冻结版 r1）

阶段 A1 · owner: inventory-cli · 日期 2026-10-12 · 前置：TP-07（已 closed，进程底座可用）

本文冻结 TP-08C 的提取方式、selector 与正负向义务。结果：**11 passed**（Windows + Py3.14 profile）。

## 1. 提取方式（真实树，非静态解析）

| 层 | 手段 |
|---|---|
| 顶层命令集合 | `cli/categories.py::all_command_names()` —— 唯一权威分类源（84 入口 = `_SUBCOMMANDS` 79 + cw.py standalone 5），由 `tests/test_category_source.py` 锁定不重不漏 |
| 每命令 argparse 树 | **进程内捕获真实 parser 对象**：挂钩 `argparse.ArgumentParser.format_help`，经 CLI 自带 help 快路径 `_dispatch_subcommand_help()` 触发。得到 help 文本之外的 `default / choices / nargs / required / type / 互斥组` |
| cw.py 层 5 入口 | install / server / test / setup / daemon 在 `cw.py main()` 内分发，不经 `_dispatch_subcommand`，改用真实子进程 `cw.py <entry> --help` |

**为什么不用 help 文本解析**：argparse help 不保证暴露 default/choices/required（仅当 help 串里手写了才出现）。TP-08C 要求这些字段，只有捕获 parser 对象才如实。

**副作用边界**：help 快路径不初始化 db（`_dispatch_subcommand_help` 传 `db=None`）；提取器直接调该函数，**绕过** `_run_subcommand_mode` 的 lazy auto-setup，因此不写 `AUTO_SETUP_MARKER`。已实证：提取后生产 `~/.callwarden/.auto_setup_marker` 不存在、`callwarden.db` mtime 早于提取。

## 2. 冻结 selector 与命令

```
提取: C:/Python314/python.exe tests/acceptance/inventory/cli/extract_cli_tree.py
      cwd=C:/git_work/callwarden；rc=0；输出 docs/testing/manifests/cli/cli_tree.json
验收: C:/Python314/python.exe -m pytest tests/acceptance/inventory/cli/test_cli_tree.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
      cwd=C:/git_work/callwarden；expected rc=0，11 passed
```

## 3. 产出规模（r1 实测）

- 顶层入口 **84**（含 cw.py 层 5 个 standalone）
- 嵌套子命令 **144**（task 31 / gc 13 / rule 8 / workspace 6 / lease 5 / assignment 3 / …）
- standalone 脚本 3 个（cw-client / cw-agent / cw-daemon）
- 提取错误 **0**
- manifest 390295 bytes，LF + UTF-8

参数契约抽样（`cw task report`）：`task_id`/`step_id` required=True；`--result` default=""；`--fencing-counter` type=int default=-1；`--fail` nargs=0。

## 4. 正向义务

| 义务 | 测试 |
|---|---|
| 84 顶层入口不重不漏 | `test_all_84_top_level_entries_present` |
| 每个入口有分类归属 | `test_every_entry_has_category` |
| 嵌套一层完整展开 | `test_nested_subcommands_expanded` |
| required/type/default/choices 如实落 manifest | `test_parameter_contract_fields_captured` |
| 互斥组关系可读 | `test_mutually_exclusive_groups_recorded` |
| cw.py 层入口有真实 help | `test_standalone_entries_have_real_help` |
| schema + source_revision 绑定 | `test_schema_and_source_binding` |

## 5. 负向义务

| 场景 | 测试 / 期望 |
|---|---|
| 提取写生产目录 | `test_no_production_write_during_extraction`：auto-setup marker 不得出现；生产 db mtime 不得新于 60s |
| 未决入口静默消失 | `test_pending_entries_explicit_not_silent`：原生 Rust CLI 必须显式挂 pending 并带 reason/blocked_by |
| manifest 行尾/编码漂移 | `test_manifest_is_lf_and_utf8`：必须 LF + 严格 UTF-8 + 以 \n 结尾 |
| 捏造占位入口 | `test_no_placeholder_or_fabricated_entries`：禁止 TODO/FIXME/placeholder/`<frozen-selectors>` |

## 6. 未决项（pending_entries，不阻塞本卡）

**原生 Rust CLI（cw.exe）**：TP-06 候选只构建 `--bin cw-daemon`，原生 CLI 未构建；且 `profiles.json` DP-04「原生 Rust CLI 是否在发布范围」= DECISION_REQUIRED。按 TASK_BREAKDOWN，若纳入发布须**按独立命令树分卡**，不把 Python CLI ownership 无限扩展。本卡如实挂 pending（含 cargo 命令），不冒充已提取。

## 7. 边界

- 本卡只提取「入口/参数树」，不跑任何业务命令（那是对 `cw <cmd>` 实际执行的 TP-10C scope）。
- 未创建正式 daemon 任务（§7 入库门禁未过），不伪造 task_id。
- 与旧 `tests/convergence` 隔离：`--noconftest`，不改任何旧 conftest。
