# G0 补实验可执行清单（真实 blind review 批次）

> 目标：让 G0 检查点（`docs/design/tasks.md` §2）在**真实实验数据**上输出 `eligible_for_p1=true`。
> 真相源：`docs/design/requirements.md` Requirement 12（12.1–12.29）、`docs/design/tasks.md` G0 验收标准（L22）、`docs/design/multi-llm-contract-driven-collaboration-design.md` P0 实验协议。
> 硬约束：**禁止人工打标伪造**（Requirement 12.20 为暂停/诚信事件）；实验记录一律标 `non_product_evidence`（12.23），不得称为产品 Evidence。

## 现状缺口

- 旧批次 `B-1785714640010`：有效样本 35（≥30 满足），但 **nontrivial=0/10**、`eligible_for_p1=false`、`status=directional_only`、`insufficient_sample=true`。
- 此前声称的"19 非平凡 / eligible=true"系人工打标注入，不可复现（违反 12.20）。
- 结论：需**创建新批次**积累真实数据，旧批次数据不可复用。

## 归因治理（T-1785854667954-66d5b84e，2026-08-04 已修复）

G0 批次核实发现 change_audit 归因污染，已修复（commit 4fa3384）：
- **排除临时/untracked 文件**：`需求讨论.md`、`_tmp_*`、`_semgrep_out.json` 等不再进入 change_audit
- **去重**：同任务同文件（相同 hash_before/hash_after）只记录一次
- **design/review 任务不捕获代码 diff**：文档任务不误捕获实现任务的代码
- **并发归因修复**：`task_capture_diff_auto` 优先用 source_commit_hash 反查 HEAD 归属任务
- 主库已清理污染（962 条删除，备份表 `change_audit_bak_1785855292` 可恢复）

**重建批次要求**：新批次必须**先回填干净 diff 再 admit**（admit 时盲视图自动带 diff），
且用**清理后的 change_audit** 判定 nontrivial——不得复用被污染的历史 diff。

## G0 验收标准（tasks.md L22，逐条对应 Requirement 12）

1. 至少 30 个有效任务且至少 10 个非平凡 `code_change`（12.9 + 12.26）；
2. Requirements 12.10–12.13 全部满足；
3. Requirements 12.15–12.20 无暂停条件；
4. Requirements 12.27–12.29 灰区观察不得未决；
5. 至少 90% Treatment 可证明 verdict-before-reveal 且 blind view 无禁止字段（12.13 一部分）。

## 第 0 步：前置确认（P0 工具链已就绪）

- [ ] 确认 `cw experiment` 13 个子命令可用：
  `batch-create` / `batch-lock` / `batch-list` / `toggle-set` / `toggle-show` / `admit` / `record-metrics` / `record-verdict` / `record-reveal` / `record-invalid` / `record-incident` / `pause` / `report`
- [ ] 确认 JSONL 记录位置与 `non_product_evidence` 标记（12.23）
- [ ] 确认旧批次 `B-1785714640010` 已关闭/归档（12.21：规则变化必须新批次，不原地改协议）

## 第 1 步：创建并锁定新批次（Requirement 12.3）

- [ ] `cw experiment batch-create`：声明分层随机分配，维度为
  **profile / risk / diff size / language / reviewer-model pair**（五维分层）
- [ ] 声明每个指标的**分子/分母定义、观察窗口、成功阈值、暂停阈值、invalid 排除规则**（12.3、12.22）
- [ ] `cw experiment batch-lock`：首次纳样前**冻结协议**；之后任何规则变化必须再建新批次

## 第 2 步：纳样（关键数量门槛，12.2 / 12.9 / 12.26）

- [ ] 纳入条件（12.2）：任务 profile ∈ {design, code_change, review} + 有可审 diff/设计变更；
  排除紧急直改、纯机械格式化、无法生成最小盲视图的任务
- [ ] `cw experiment admit` 逐任务纳样（自动分组 Control/Treatment + 写最小盲视图）
- [ ] **至少 30 个有效任务**（12.9 下限）
- [ ] **至少 10 个非平凡 code_change**（12.26）：每个任务
  **至少一个 tracked 源文件改动 ≥10 非注释代码行 + `task_symbol_changes` 至少 1 条符号变更**，
  排除纯格式化和生成文件
  - ⚠️ 上次失败的核心：纳样后必须确认每个 code_change 任务的
    `is_nontrivial_code_change=true`（行数 + 符号数**自动判定**，不得手填）
- [ ] 无效样本按 12.8 处理：盲条件破坏 / 快照变化 / reviewer 与 implementer session 标记相同 →
  `cw experiment record-invalid` + 保留原因 + 从效果估计与所有成功/暂停指标分子分母排除（除无效样本率）

## 第 3 步：双盲评审执行 + 指标记录（12.4–12.7）

- [ ] **Control 组**：揭示 Minimal_Blind_View + Implementer_Notes 后再出首轮 verdict（12.4）
- [ ] **Treatment 组**：只揭示 Minimal_Blind_View（不含 Implementer_Notes），
  **首轮 verdict 封存前不得泄露**（12.5）；≥90% Treatment 样本必须可证明 verdict-before-reveal（12.13）
- [ ] reviewer 必须来自**独立 session**（与 implementer 不同，12.8）
- [ ] `cw experiment record-metrics` 记录（12.6）：
  first-pass findings、final findings、verified 真阳性/假阳性/漏报（锁定召回分母）、
  review duration、token usage、Reopen 事件、观察窗口内的 post-apply 缺陷/回滚
- [ ] verdict 封存后 reveal；若 verdict 变化，`cw experiment record-verdict` 记录变化 + 结构化原因（12.7）
- [ ] 泄露/伪造事件：`cw experiment record-incident`（12.18 / 12.20，同时暂停）

## 第 4 步：评估与 G0 判定（12.10–12.13 / 12.15–12.20 / 12.27–12.29）

- [ ] 最小样本：≥30 有效 + ≥10 非平凡（12.9）
- [ ] **12.10 缺陷检测成功**：Treatment 召回率相对提升 ≥15%，或额外发现 ≥2 个确认高危缺陷且无关键遗漏增加
- [ ] **12.11 误报成功**：Treatment FP 率 ≤ Control +10 个百分点（绝对差）
- [ ] **12.12 延迟成功**：Treatment 中位延迟增幅 ≤25%、P90 ≤50%（相对 Control）
- [ ] **12.13 安全与盲化成功**：Treatment 重开/回滚率 ≤ Control，且
  ≥90% Treatment 证明 verdict-before-reveal + 盲视图披露列表排除 Implementer_Notes
- [ ] **12.15–12.20 无暂停触发**：
  - 12.15 Treatment 关键遗漏（缺最小视图事实）
  - 12.16 FP 超 Control +20pp 连续 10 样本
  - 12.17 中位延迟超 +50% 连续两周，或无效率 >30%
  - 12.18 盲视图泄露（Implementer_Notes / 先前 verdict / 敏感推理）
  - 12.19 快照漂移 >20% 样本不可归属
  - 12.20 伪造独立性 / 伪造 Evidence
- [ ] **12.27–12.29 灰区无未决**：
  - FP 超 Control 10–20pp（12.27）或中位延迟增幅 25–50%（12.28）→ 灰区：
    不授权 P1、继续纳样、记录灰区观察；未解决前不得授权（12.29）

## 第 5 步：报告与门禁收尾

- [ ] `cw experiment report`：输出机器可读 G0 决策
  （绝对分子/分母 + 比例 + 置信区间 + 每个 invalid 原因 + 灰区状态，12.22）
- [ ] 确认 `eligible_for_p1=true` 且 `directional_only=false`、`insufficient_sample=false`
- [ ] G0 检查点 gate report（任务 `T-1785574343867-04c2894e`）
- [ ] E. 门禁重验 fix_defect 完成（任务 `T-1785767529978-bacba8f1`）
- [ ] 根任务 verify → 关闭（任务 `T-1785574343859-117469ed`）
- [ ] G3 检查点 review→apply 一并收尾（任务 `T-1785574343992-4d015a97`）

## 需要准备的资源

- 真实 reviewer（独立 session，不能自己审自己）
- 至少 10 个真实的 code_change 任务（每个改动 ≥10 行非注释源码 + 有符号变更）
- 至少 30 个有效任务总样本（含 design / review 类型）

## 参考命令

```powershell
cw experiment batch-create   # 创建并锁定新批次（五维分层 + 指标定义 + 窗口 + 阈值）
cw experiment batch-lock     # 冻结协议
cw experiment admit          # 纳样：任务 + 分组 + 最小盲视图
cw experiment record-metrics # 记录原始评审指标
cw experiment record-verdict # 记录 reveal 前后 verdict 变化
cw experiment record-reveal  # 记录 Implementer_Notes reveal 事件
cw experiment record-invalid # 记录无效样本与原因
cw experiment record-incident# 记录泄露/完整性事件并暂停
cw experiment pause          # 暂停批次并拒绝新纳样
cw experiment report         # 生成机器可读 G0 决策
```
