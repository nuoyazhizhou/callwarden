# G0 盲评执行手册（Batch B-1785972239933-194e2caa）

> 适用：独立 reviewer session（**不得与任一样本的 implementer 同 session**，Requirement 12.8）。
> 批次：B-1785972239933-194e2caa（30 样本：Control 17 / Treatment 13，协议已锁定，指纹 a3949e5e）。
> 目标：逐样本出 verdict + 记录指标（**必须传 `--duration`**，12.12 依赖），最后 `report` 输出 G0 决策。

## 1. 环境准备（Windows，务必用 USERPROFILE 方案）

```powershell
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'
$env:USERPROFILE='C:\Users\wanpi\.callwarden\g0-reviewer-home'
$env:HOME='C:\Users\wanpi\.callwarden\g0-reviewer-home'
```

> ⚠️ Windows 上 Python `expanduser("~")` 读 **USERPROFILE** 而非 HOME；只设 HOME 不生效。
> 该环境 DB 是精简 G0 实验库（含治理后干净 diff），`record-metrics` 的 nontrivial 自动判定依赖它。

验证环境：
```powershell
python cw.py experiment batch-list          # 应看到 B-1785972239933-194e2caa [active]
```

## 2. 流程总览与数据说明

**Control（17 个）**：打开盲视图 md → 看内容（含 Implementer_Notes 提示）→ 形成 verdict（TP/FP/misses）→ `record-metrics`（**必传 `--duration`**）。

**Treatment（13 个）**：打开盲视图 md → 只看到盲视图 → 首轮 verdict → `record-verdict --changed no`（封存）→ `record-reveal --sealed`（揭示）→ 若 verdict 变化：`record-verdict --changed yes --reason-code <code>` → `record-metrics`（**必传 `--duration`**）。

### 2.1 下一批纳样前置条件

Control 的 Implementer_Notes 必须在纳样时通过 UTF-8 文件显式提供；Treatment 不得提供该参数：

```powershell
cw experiment admit <TASK_ID> <BATCH_ID> --notes-file <IMPLEMENTER_NOTES_UTF8_FILE>
```

若确定性分组结果为 Treatment，带有非空 `--notes-file` 会 fail-closed；若结果为 Control
但没有 notes 文件，纳样也会拒绝。`report` 会再次校验 JSONL 中的首轮视图，防止使用没有
真实披露差异的旧批次。

下一批必须在开始 review 前确认两组都有足够的可检出缺陷覆盖。`TP=0 且 misses=0` 的组其
Recall 分母为 0，属于不可估计，不会被当作 Recall 0 参与相对改善计算；该批次只能失败并
重新纳样，不能通过修改阈值或补写指标解决。

**盲视图 diff 覆盖说明**（如实告知 reviewer）：
- 30 个样本中 **19 个含 diff**（其中 5 个 100% 覆盖、约 4 个 40–67%、若干大任务仅 1–4% 覆盖），**11 个无 diff**。
- 每个盲视图顶部标注 `diff 覆盖: X/Y 文件`。**对含 diff 的样本逐 diff 评审**；对无 diff / 低覆盖样本，基于任务元信息给出判断，如实记录（可给 0 但注明原因）。
- 无 diff 不代表无变更——是回填覆盖限制，评审时以盲视图披露内容为准。

盲视图文件目录：`C:\Users\wanpi\.callwarden\g0-reviewer-home\.callwarden\experiments\blind_package_B-1785972239933\`

## 3. 指标定义（record-metrics 参数）

- `--tp`：Verified true positives（评审确认的真阳性缺陷/问题数）
- `--fp`：Verified false positives（评审确认的误报数）
- `--misses`：Verified misses（评审确认的漏报数，用于锁定召回分母）
- `--duration`：**评审耗时（秒），必填**（12.12 延迟成功条件依赖）
- `--tokens`（可选）：token 用量
- `--reopen` / `--defects` / `--rollbacks`（可选）：观察窗口内 Reopen / 事后缺陷 / 回滚数

> nontrivial 由系统自动判定（基于回填 diff），**不要手填 `--nontrivial`**（Requirement 12.20）。

## 4. Control 样本（17 个，按序执行）

```powershell
# CONTROL 1  fix: pre-existing test failures batch 2
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784219067931-f237 B-1785972239933-194e2caa

# CONTROL 2  fix: 统一升级 ID 生成器熵 4→8 位 hex
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784245605607-5bf1245e B-1785972239933-194e2caa

# CONTROL 3  批次11：P0 运维 RPC 授权
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784597116187-1c028c20 B-1785972239933-194e2caa

# CONTROL 4  分析 GitHub Actions 打包失败
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784968411560-4ba39f88 B-1785972239933-194e2caa

# CONTROL 5  R4 修复 artifact inspector 并完成真实跨平台验收
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785024538957-b4bc9f03 B-1785972239933-194e2caa

# CONTROL 6  Phase 4：Rust daemon 与多用户安全边界
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785148066855-f885798a B-1785972239933-194e2caa

# CONTROL 7  P0-CLI-D1：Rust cw refresh 写路径
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785468161999-4ba39283 B-1785972239933-194e2caa

# CONTROL 8  P0-CLI-D3：Rust cw build-context/toolchain
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785468162025-5c47603b B-1785972239933-194e2caa

# CONTROL 9  P0-CLI-E3：Rust cw task 审核审计闭环
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785484889185-b2883eb8 B-1785972239933-194e2caa

# CONTROL 10  P0-CLI-E4：Rust rule/guardrail/audit 安全链
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785484889187-cf1e9f88 B-1785972239933-194e2caa

# CONTROL 11  多 LLM 契约驱动协同实施计划（根任务）
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785574343859-117469ed B-1785972239933-194e2caa

# CONTROL 12  1.1 实现实验批次与冻结协议模型
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785574343862-5915392a B-1785972239933-194e2caa

# CONTROL 13  3. D0：跨平台 daemon 化
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785574343867-51dc8c7d B-1785972239933-194e2caa

# CONTROL 14  补齐 watcher/recovery 进程级企业验收
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785599952979-5b045e5d B-1785972239933-194e2caa

# CONTROL 15  P4: assignment 与 token/expiry/renew/release/fencing lease
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785741777729-45af3058 B-1785972239933-194e2caa

# CONTROL 16  F. Evidence Gate Stage_Toggle 感知
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785798646156-ead2691a B-1785972239933-194e2caa

# CONTROL 17  修复 cw --refresh-all 多进程 DB 竞争卡死
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785824926483-86b35843 B-1785972239933-194e2caa
```

## 5. Treatment 样本（13 个，按序执行）

每个样本 4 步：读盲视图 → 首轮 verdict 封存 → reveal → 记录变化（如有）→ record-metrics（必传 duration）。

```powershell
# TREATMENT 1  fix(docs): 同步过时统计数字
python cw.py experiment record-verdict --changed no T-1784278382176-563364ad B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784278382176-563364ad B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784278382176-563364ad B-1785972239933-194e2caa

# TREATMENT 2  批次1：文档/描述类修正（约 50 项）
python cw.py experiment record-verdict --changed no T-1784527388572-3aa13284 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784527388572-3aa13284 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784527388572-3aa13284 B-1785972239933-194e2caa

# TREATMENT 3  批次2：实际代码修复（A14/A15/K2/L2/G13）
python cw.py experiment record-verdict --changed no T-1784527388572-6be4a6b2 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784527388572-6be4a6b2 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784527388572-6be4a6b2 B-1785972239933-194e2caa

# TREATMENT 4  P0-3 Linux/macOS 打包脚本修复
python cw.py experiment record-verdict --changed no T-1784677450316-e9fec35b B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784677450316-e9fec35b B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784677450316-e9fec35b B-1785972239933-194e2caa

# TREATMENT 5  P3 发布依赖白名单与 v0.3.1 标签
python cw.py experiment record-verdict --changed no T-1784979928079-e7033874 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784979928079-e7033874 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784979928079-e7033874 B-1785972239933-194e2caa

# TREATMENT 6  P0-C 修复通用语言 Rust parser 语义缺口
python cw.py experiment record-verdict --changed no T-1784986236713-1d233675 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1784986236713-1d233675 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1784986236713-1d233675 B-1785972239933-194e2caa

# TREATMENT 7  R1 可信 ParseFact ABI 与 fail-closed gate
python cw.py experiment record-verdict --changed no T-1785024538955-13bb3191 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785024538955-13bb3191 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785024538955-13bb3191 B-1785972239933-194e2caa

# TREATMENT 8  R0 clean HEAD 可编译与工作区收口
python cw.py experiment record-verdict --changed no T-1785024538955-1858a947 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785024538955-1858a947 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785024538955-1858a947 B-1785972239933-194e2caa

# TREATMENT 9  修复 b89e3d3 复审剩余 3 P0 + 3 P1
python cw.py experiment record-verdict --changed no T-1785076592821-6b0d7555 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785076592821-6b0d7555 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785076592821-6b0d7555 B-1785972239933-194e2caa

# TREATMENT 10  P0-CLI-D2：Rust cw workspace 生命周期
python cw.py experiment record-verdict --changed no T-1785468162023-598bbdfa B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785468162023-598bbdfa B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785468162023-598bbdfa B-1785972239933-194e2caa

# TREATMENT 11  3.14 实现经 daemon 的 CLI 写命令面
python cw.py experiment record-verdict --changed no T-1785574343876-2b298161 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785574343876-2b298161 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785574343876-2b298161 B-1785972239933-194e2caa

# TREATMENT 12  B. P1 collab 集成修复
python cw.py experiment record-verdict --changed no T-1785767529976-1760c608 B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785767529976-1760c608 B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785767529976-1760c608 B-1785972239933-194e2caa

# TREATMENT 13  A. P1 Evidence Gate 完整性修复
python cw.py experiment record-verdict --changed no T-1785767529976-ea90977f B-1785972239933-194e2caa
python cw.py experiment record-reveal --sealed T-1785767529976-ea90977f B-1785972239933-194e2caa
python cw.py experiment record-metrics --tp <TP> --fp <FP> --misses <M> --duration <S> T-1785767529976-ea90977f B-1785972239933-194e2caa
```

## 6. 收尾

全部 30 个样本记录完成后：

```powershell
python cw.py experiment report B-1785972239933-194e2caa
```

确认输出：
- `eligible_for_p1=true` 且 `directional_only=false`、`insufficient_sample=false`（最小样本 + 成功条件 12.10–12.13）
- 无暂停触发（12.15–12.20）、灰区无未决（12.27–12.29）

## 7. 红线（违反即暂停 + 诚信事件）

- **不得与任一 implementer 同 session**（12.8 自审无效样本）
- **不得编造 verdict / TP / FP / misses / duration**（12.20）
- **不得手填 `--nontrivial`**（自动判定，12.26）
- **duration 必须真实填写**（12.12 延迟指标，缺失则该成功条件不可评估）
- 实验记录一律 `non_product_evidence`（12.23），不得称为产品 Evidence
- 若盲视图泄露 Implementer_Notes 或敏感推理（Treatment）：`record-incident` 并暂停（12.18）
