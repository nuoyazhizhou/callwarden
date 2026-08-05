# G0 补实验 Reviewer 盲评指引

> 实验批次：`B-1785972239933-194e2caa`（已锁定协议，30 个真实任务已纳样）
> 数据目录：`~/.callwarden/experiments/B-1785972239933-194e2caa.jsonl`
> 协议依据：`docs/design/g0-remediation-checklist.md` + `docs/design/requirements.md` Requirement 12
> 硬约束：reviewer 必须来自**独立 session**（与 implementer 不同，Req 12.8）；禁止人工伪造 nontrivial（12.20，已由自动判定接管）

## 批次概况

- 30 个有效样本：17 Control / 13 Treatment（五维分层随机分组）
- 12 个任务经 `nontrivial_code_change_from_change_audit` 自动判定为非平凡 code_change
  （从 change_audit.diff 非注释新增行 ≥10 + task_symbol_changes ≥1 符号变更）
- 19/30 盲视图含完整 diff（先回填后 admit，修复了旧批次 24/37 缺 diff 的问题）
- 每个样本已生成 Minimal_Blind_View（JSONL 的 blind_view 记录）

## 分组与盲法要求（Req 12.4 / 12.5）

| 组 | 首轮 verdict 前披露 | 说明 |
|---|---|---|
| **Control** | Minimal_Blind_View + **Implementer_Notes** | 可看实现者笔记后再出 verdict |
| **Treatment** | 仅 Minimal_Blind_View（**不含** Implementer_Notes） | 首轮 verdict 封存前**不得**泄露笔记/先前 verdict/敏感推理 |

## Reviewer 操作流程（每个任务）

对每个已纳样任务 `TASK_ID`：

### 1. 查看盲视图
读取 JSONL 中该任务的 `blind_view` 记录，`payload` 字段包含：task_title / task_description / step_targets / change_audit_diffs / symbol_changes / test_runs_status / open_quality_findings。

### 2. 出首轮 verdict（reviewer 独立判断）
- 判断任务变更中是否存在 **blocker/defect 级问题**（逻辑错误、安全漏洞、数据一致性破坏、明显遗漏）
- 记录 finding 数（TP 候选 / FP 候选 / miss）

### 3. Treatment 首轮 verdict 封存
Treatment 任务在 reveal 前先记录 verdict（`record-verdict --changed no` 表示未变）。

### 4. reveal + 记录指标
用 `cw experiment record-metrics` 记录每个任务的评审结果：

```powershell
python cw.py experiment record-metrics <TASK_ID> B-1785972239933-194e2caa ^
  --tp <确认真阳性数> --fp <确认假阳性数> --misses <漏报数> ^
  --duration <评审秒数> --tokens <token数> [--reopen N] [--defects N] [--rollbacks N]
```

> **nontrivial 已自动判定**：`record-metrics` 会从 change_audit.diff + task_symbol_changes 自动计算 `is_nontrivial_code_change`，输出标注 `(auto-detect)`。**不要手填 `--nontrivial`**（12.20）。

### 5. 记录 reveal 事件
```powershell
python cw.py experiment record-reveal <TASK_ID> B-1785972239933-194e2caa --sealed yes
```
（`--sealed yes` = 首轮 verdict 在 reveal 前已封存）

### 6. 记录 verdict 变化（若 reveal 后 verdict 改变）
```powershell
python cw.py experiment record-verdict <TASK_ID> B-1785972239933-194e2caa --changed yes --reason-code <code>
```

## 无效样本处理（Req 12.8）

以下情况标记 invalid（从效果估计排除）：
- 盲条件破坏（reviewer 提前看到 Implementer_Notes）
- 快照变化（评审期间 diff 数据变化）
- reviewer 与 implementer session 标记相同

```powershell
python cw.py experiment record-invalid <TASK_ID> B-1785972239933-194e2caa --reason <code> --detail "..."
```

## 泄露/完整性事件（Req 12.18 / 12.20）

任何泄露或伪造事件立即记录并暂停：
```powershell
python cw.py experiment record-incident <TASK_ID> B-1785972239933-194e2caa --type disclosure --detail "..."
python cw.py experiment pause B-1785972239933-194e2caa --reason "..."
```

## 评估与报告

全部任务评审完成后：
```powershell
python cw.py experiment report B-1785972239933-194e2caa
```
输出机器可读 G0 决策（`eligible_for_p1` / `directional_only` / `insufficient_sample` / 灰区 / 暂停）。

## G0 判定阈值（Requirement 12）

- 最小样本：≥30 有效 + ≥10 非平凡 code_change（12.9 + 12.26）
- 12.10 缺陷检测：Treatment 召回率相对提升 ≥15%，或额外确认 ≥2 高危缺陷且无关键遗漏增加
- 12.11 误报：Treatment FP 率 ≤ Control +10pp（绝对差）
- 12.12 延迟：Treatment 中位延迟增幅 ≤25%、P90 ≤50%
- 12.13 安全盲化：Treatment 重开/回滚率 ≤ Control，且 ≥90% Treatment 证明 verdict-before-reveal
- 12.15-12.20 无暂停触发；12.27-12.29 无未决灰区

## 环境（reviewer 专用 HOME，宿主机可见）

nontrivial 自动判定依赖 DB 的 `change_audit.diff`，因此 reviewer 必须用
**精简 G0 实验 DB**（已回填 diff）而非宿主主库（diff 全空）。

**reviewer 专用 HOME 位置**（宿主机可见挂载）：
- DB：`<g0-reviewer-home>/.callwarden/callwarden.db`（精简 G0 实验 DB，6MB，已回填 239 条 diff）
- 批次数据：`<g0-reviewer-home>/.callwarden/experiments/`（batch_config + B-1785839992016 JSONL）

```powershell
$env:HOME = "<g0-reviewer-home>"   # reviewer 专用 HOME（含精简 G0 DB + 批次数据）
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
cd C:\git_work\callwarden
python cw.py experiment batch-list        # 应看到 B-1785839992016 [active]
```

**验证自动判定**（reviewer 开始前自查）：
```powershell
python -c "import os,sys; os.environ['HOME']='<g0-reviewer-home>'; sys.path.insert(0,'.'); from callwarden.db import CodeGraphDB; from callwarden.experiments.blind_review_views import collect_source_facts_from_db; from callwarden.experiments.blind_review_evaluator import nontrivial_code_change_from_change_audit; db=CodeGraphDB(db_path=os.path.expanduser('~/.callwarden/callwarden.db')); f=collect_source_facts_from_db(db,'T-1785024538955-13bb3191'); print('nontrivial=',nontrivial_code_change_from_change_audit(f.change_audit_diffs,f.symbol_changes))"
```
应输出 `nontrivial=True`。若为 False，说明 DB diff 未就位，先同步再盲评。

> **不要**用宿主机主库（`C:\Users\wanpi\.callwarden\callwarden.db`）跑 experiment——
> 其 `change_audit.diff` 全空（2568 条），自动判定会得到 nontrivial=0，G0 无法过。
