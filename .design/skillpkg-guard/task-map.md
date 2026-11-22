# task-map · skillpkg-guard

> R4 产出。三列映射：`task_id` ↔ `REQ` ↔ `AC`。**本表是追溯链第四环**（`REQ-01 → 卡片 → task_id → AC-id → 证据`）。
> 建单时间 2026-11-22；基线 `b6dfbc5`；workspace `1 / 4baea3ff12c2ea5c`。

## 任务单

| task_id | title（符号，非行号） | action | 状态 | 承接 AC |
|---|---|---|---|---|
| `T-1795312692647-c9ff8c00` | `skills/_shared/check_skill_packages.py:新增 implement 技能包结构自检器（五类检查 + 三态退出码）` | `implement` | open | AC-01…06 |
| `T-1795312729595-643ffdec` | `tests/test_skill_packages_guard.py:新增 test 结构自检器单测（AC-01…07 全覆盖）` | `test` | open | AC-01…07 |

## 依赖声明（G6.4 三件套）

1. **建单顺序按拓扑**：implement 先于 test（本表行序即拓扑序）。
2. **`description` 第 2 行**：`blocked-by: T-1795312692647-c9ff8c00`（test 单）。
3. **执行侧按序认领**：implement 未 `closed` 前不领 test 单——脚本不存在时 test 必红，抢跑只会产出假 FAIL。

> ⚠️ 已知限制（G6.4 原文）：`task_create` 无结构化 blocking 字段，`blocked-by` 是**文本约定**，daemon 不强制。本条为首次真实演练该约定，验证点：`blocked-by` 行是否能在 `cw task show` 里原样读回（已确认在 description 第 2 行）。

## 步骤 → 合同卡映射

| task_id | step | target_symbol | 合同卡 | 覆盖违规码 |
|---|---|---|---|---|
| `…c9ff8c00` | #0 | `parse_frontmatter` | C-01 | `FRONTMATTER_*` |
| `…c9ff8c00` | #1 | `check_package` | C-04 | `REFERENCE_MISSING` / `DUPLICATE_GATES` / `SMOKE_BAD_PATH` / `INSTALL_*` |
| `…c9ff8c00` | #2 | `main` | C-06 | `DUPLICATE_PACKAGE_NAME` + 退出码三态 |
| `…643ffdec` | #0 | `test_real_packages_pass` | —（测试） | 全部 9 码 |

> 支撑函数 `load_packages`(C-02) / `referenced_reference_files`(C-03) / `format_report`(C-05) 随所属步骤一并实现，不单独设 step（G5 一文件一任务，步骤按判定层切分而非按函数计数）。

## 已演练的契约项（本轮真实建单的收获）

| 契约 | 是否按预期 | 证据 |
|---|---|---|
| G6.1 `title` 用符号不用行号 | ✅ | 两条 title 均为 `<路径>:新增 <action> <一句话>`，无行号 |
| G6.1 `description` 首行 = `REQ-xx` | ✅ | `REQ-01 \| must`（`cw task show` 原样读回） |
| G6.1 `steps` 非空 | ✅ | implement 3 步 / test 1 步；无"无步骤任务" |
| G6.2 `action` 仅用惯例四值 | ✅ | 用了 `implement` / `test`，未手写 `fix_defect` |
| G6.3 `check_items` 必须字符串 | ✅ | 4 个 step 的 `check_items` 全为 `str`（建单前 `assert isinstance(..., str)` 拦了一道） |
| G6.4 `blocked-by` 文本约定 | ✅（文本层） | test 单 description 第 2 行；daemon 不校验，属**已知弱依赖** |
| 卡片全文进 description | ✅ | 2494 / 3732 字符，executor 无需回翻 `contracts.md` |

## 待回填

- `ac-map.md` 的「证据位置」列：test 单完成后由 test-cw 填 `evidence/AC-xx.md`
- 本表「状态」列：随 `task.close` 更新

---

## 追加（2026-11-22 R5 后期 · code-review 发现 P2 后）

| task_id | title | action | 状态 | 承接 |
|---|---|---|---|---|
| `T-1795313464539-82567a30` | `skills/_shared/check_skill_packages.py:_read_text fix 非 UTF-8 编码致 traceback（CL-02）` | `fix` | review | AC-08 / CL-02 |

### `action:"fix"` 建单路径实测（此前从未演练）

| 契约项 | 结果 |
|---|---|
| `action` 用惯例值 `fix`（非保留值 `fix_defect`） | ✅ 建单成功 |
| `description` 首行 `REQ-01 \| 🟡 P2` | ✅ 原样读回 |
| `description` 含 `blocked-by:` | ✅（指向被审任务） |
| `check_items` 字符串 | ✅ `cw task show` 多行显示未转义 |
| 单步骤 `fix` 认领 → 报账 | ✅ `Action: fix`，报账 success |

### 状态与阻塞（如实记录）

| 任务 | 状态 | 说明 |
|---|---|---|
| `T-1795312692647-c9ff8c00`（implement） | `review` / `workflow=review_pending` | 三步全 `done`；等 reviewer verdict |
| `T-1795313464539-82567a30`（fix） | `review` | 一步 `done`；等 reviewer verdict |
| `T-1795312729595-643ffdec`（test） | `open` | 依赖前两条闭环 |

**reviewer verdict 未入账（阻塞）**：`verdict.submit` 需要 `snapshot_id`，该值只能由
`snapshot.publish` 产生；而 publish 会重写共享快照（`~/.callwarden/callwarden.db`），
影响并行会话，不宜为一条治理仪式在中途随意触发。⇒ 本轮停在「verdict 前一步」，
`apply`/`close` 未执行。

**顺带发现的 daemon 参数契约缺口**（逐个撞出来的，均无文档）：
`lease.acquire` 的 identity 必须**嵌套**在 `identity` 键下（扁平传参报
`E_IDENTITY_INCOMPLETE`）；`verdict.submit` 依次需要 `identity`(嵌套) → `request_id`
→ `view_manifest_hash`（来自 `role_view.get`）→ `phase` 必须是 v1 名
（`blind_first_pass`，`PRE_VERDICT` 被拒）→ `findings` 必须是 **JSON 数组**（不是字符串，
与 `check_items` 相反）→ `snapshot_id`。凡此六项，MCP 工具文档均未写全。
