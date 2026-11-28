# commit b86e87b 归属异常补记

## 事实

`b86e87b`（message：F-3 后续单复核记录 + 四条工具链/流程缺口登记）本意只提交
`.design/preq/task-map.md`（追加 §30）。实际一并带入 7 个 preq 线（T11 / T10）文件：

- `.design/preq/evidence/T-1795837031464-eef45a20-AC.md`
- `.design/preq/evidence/T-1795837031464-eef45a20-evidence-manifest.md`
- `.design/preq/evidence/T-1795837031464-eef45a20-tests-keylines.txt`
- `.design/preq/evidence/T-1795837037942-710955b4-AC.md`
- `.design/preq/evidence/verdict_t10_payload.json`
- `.design/preq/evidence/verdict_t11_payload.json`
- `.design/preq/review-t11.md`

## 成因

这 7 个文件事先已被其它会话 `git add` 进索引（adjudicator 在裁决回报中已披露
「.design/preq 的既有暂存条目仍在索引中」，其选择不清理）。主理人随后执行
`git add -f .design/preq/task-map.md` + `git commit`，索引中既有暂存条目被一并提交。

## 判定

- **内容完整落库，零丢失、零篡改**：7 文件均为 preq T11/T10 线的证据与评审产物，
  本就应入库；本次仅是 commit message 归属不准确，不构成数据损失。
- 与 §29 记录的 `8590c73` 归属异常同类，处置一致：**不回滚历史、不 rewrite/force push**，
  以本文件补记归属，供 preq 线追溯。

## 预防

主理人后续提交前须显式核对 `git status --short` 的暂存区，或在提交命令中使用
`git commit -- <paths>` 限定路径，避免索引中他人暂存条目被带入。
