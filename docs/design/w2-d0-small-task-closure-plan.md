# W2/D0 小任务化收口计划

# 1. 目的

将 W2.3、W2.4、GD、D0 的剩余工作拆成边界明确的小任务，分别处理实现、平台验收、证据归属和独立复审。任何一项失败只阻塞对应子任务，不得用另一项的测试结果替代。

本计划只处理本地收口和可审计证据。GitHub Actions、发布推送和 release tag 不属于本轮完成条件。

# 2. 全局规则

1. 每个子任务只有一个责任范围、一个允许路径集合和一个验收契约。
2. 实现 Agent 只能将自己的子任务推进到 `review`，不得 `apply` 或 `close`。
3. Reviewer 必须基于源码、原始命令输出、环境信息和哈希独立判断，不能只接受汇报摘要。
4. 既有 `UNVERIFIED`、`BLOCKED` 或历史 JSONL 证据只读保留，不覆盖、不改写、不删除。
5. 新证据使用新的文件名或 append-only 记录，并同时保存：命令、退出码、环境、Git HEAD、二进制 SHA-256、原始 stdout/stderr。
6. 测试发生 `skip` 时：若该能力属于当前子任务的必需验收项，则结果是 `UNVERIFIED/BLOCKED`，不是 PASS。
7. 每个代码子任务最多一个聚焦提交；提交前按 AGENTS.md 规则执行 `cw --refresh-all`，并记录刷新结果。
8. 父任务只有在所有子任务均经独立 Reviewer 关闭后才允许关闭。

# 3. 当前已知阻塞

- W2.3 旧证据中存在“真实 daemon”与“未验证环境”两套互相矛盾的记录，需要以新的原始日志和哈希清单为准，旧文件不得覆盖。
- W2.3 P1-B 的部分 `change_audit` 记录过期或未签名，必须重新核对当前文件 SHA-256。
- W2.4 曾有 `change_audit=0` 或仅有叙述性结果的情况，需要补充明确的“无代码变更”归属证据和真实 Linux 双 UID 日志。
- D0 父任务仍有 3.14 子任务处于 `review`，且存在未提交的 CLI/文档修改，不能假设父任务可关闭。
- GD 的 `snapshot.publish` 缺口需要明确区分 Linux 已验证路径与 Windows 未闭合路径，不能笼统标为完成。

# 4. 子任务拆分

## A1：W2.3 P1-B 契约复核

- **范围**：只读核对五类 daemon 查询（file/symbol/grep/issues/tests）的生产注册、ACL、workspace 过滤、成功/拒绝语义。
- **允许路径**：`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/snapshot_state.rs`、`rust_ext/src/daemon/*query*.rs`、已有 W2.3 测试文件。
- **禁止路径**：不修改生产代码，不修改历史证据，不扩大到 W2.4/GD。
- **验收**：输出逐方法契约矩阵；每个方法给出源码位置、请求样例、成功响应、拒绝响应和 workspace 隔离结论。任一生产入口为 stub 或无法定位时标为 BLOCKED。
- **交付物**：只读审计报告，含当前 HEAD 和文件 SHA-256。

## A2：W2.3 Linux UDS 原始证据重跑

- **范围**：在 WSL/Linux 中构建新 daemon，运行五类查询的真实进程级 UDS round-trip。
- **允许路径**：`g0-reviewer-scratch/w2-d0/w2-3-v2/`，必要时只读使用 `tests/test_w2_3_query_uds_e2e.py`。
- **禁止路径**：不覆盖旧 `w2_3_rpc_summary.json`、旧日志或历史报告；不修改生产源码。
- **验收命令**：
  - `CARGO_TARGET_DIR=/tmp/callwarden-review-target cargo build --manifest-path rust_ext/Cargo.toml --no-default-features --bin cw-daemon`
  - `CARGO_TARGET_DIR=/tmp/callwarden-review-target python3 -m pytest -q tests/test_w2_3_query_uds_e2e.py`
- **验收条件**：fresh daemon、真实 IPC、五类查询成功和拒绝路径均通过，`skipped=0`；记录 daemon SHA-256、Rust/Python 版本、WSL 发行版、工作区路径、完整 stdout/stderr。
- **交付物**：原始日志、summary.json、environment.json、binary_sha256.json、命令清单。

## A3：W2.3 任务归属和审计证据

- **范围**：只修复 W2.3 P1-B 当前任务的归属证据，不重写实现历史。
- **允许路径**：当前任务的 `task_steps`、`task_events`、`change_audit`，以及 `g0-reviewer-scratch/w2-d0/w2-3-v2/`。
- **禁止路径**：不修改其他任务的 audit，不用短提交号替代文件内容 SHA-256，不把全局 `audit verify` 当作任务归属证明。
- **验收**：每条 audit 绑定有效 `task_id + step_id + target_file`；`hash_after` 等于当前文件 SHA-256；diff 可由明确基线重建；事件链为 claimed -> reported -> review；旧证据仍保留。
- **交付物**：只读 SQL 导出、hash 对照表、diff 统计、证据文件清单。任何无法归属的记录必须列为缺口。

## A4：W2.4 Linux 双 UID 证据重跑

- **范围**：真实 Linux root/setuid 进程级测试，覆盖双 UID、同仓库不同工作区、clean/dirty、重启恢复。
- **允许路径**：`g0-reviewer-scratch/w2-d0/w2-4-v2/`，只读使用现有 W2.4 测试。
- **禁止路径**：不伪造 UID，不用单 UID in-process 测试替代，不改写旧 W2.4 summary/log。
- **验收命令**：
  - `CARGO_TARGET_DIR=/tmp/callwarden-review-target python3 -m pytest -q -rs tests/test_process_level_e2e_recovery.py`
- **验收条件**：真实两个 UID 均执行；跨 workspace 未授权查询被拒绝；clean/dirty 状态可区分；daemon 重启后 durable log 恢复；`skipped=0`。
- **交付物**：原始日志、UID/权限环境、daemon/hash、workspace 映射、重启前后状态摘要。

## A5：W2.4 任务归属与无代码变更证明

- **范围**：将 A4 的结果绑定到 W2.4 任务；若本轮没有代码变更，必须明确记录“evidence-only”，不能伪造 change diff。
- **允许路径**：W2.4 任务的 steps/events/audit 和 `g0-reviewer-scratch/w2-d0/w2-4-v2/`。
- **禁止路径**：不补写不存在的源码 diff，不复制其他任务的 audit，不关闭任务。
- **验收**：步骤目标文件准确；原始日志 hash 与任务 result 一致；无代码变更时有 evidence-only 记录和 reviewer 说明；任务保持 `review`。
- **交付物**：任务归属报告和 evidence manifest。

## A6：D0 3.14 三文件聚焦收口

- **范围**：只处理已确认的 3 个文件：`cli/main.py`、`docs/design/daemon-deploy-runbook.md`、`docs/design/phase4-4-systemd-dual-uid-container-e2e-contract.md`。
- **允许路径**：上述 3 个文件及 D0 3.14 子任务证据目录。
- **禁止路径**：不得带入 G0 实验文件、其他 D0 子任务、CI workflow 或发布文件。
- **验收**：CLI 只引用真实 `cw-daemon` binary 名称；文档命令与实际入口一致；`python -m py_compile cli/main.py`、聚焦 `git diff --check` 通过；提交前刷新全部修改文件；提交为单一聚焦 commit。
- **交付物**：commit hash、前后 diff、刷新日志、focused test 日志。实现 Agent 只 report `review`。

## A7：D0 父任务状态核对

- **范围**：只读检查 D0 3.14 子任务及父任务的步骤、子任务状态和证据，不修改代码。
- **允许路径**：任务数据库查询结果、`g0-reviewer-scratch/w2-d0/`。
- **禁止路径**：禁止批量 SQL 改状态，禁止因大多数子任务完成而关闭父任务。
- **验收**：逐子任务列出 status、步骤状态、证据、剩余风险；只有所有子任务独立 Reviewer 通过后才提出 close，否则明确阻塞原因。
- **交付物**：状态矩阵和 close/no-close 建议。

## A8：GD gate 与 snapshot.publish 决策矩阵

- **范围**：对 GD 六个子任务和 snapshot.publish 路径做契约级核对，明确 Linux、Windows、macOS 的能力边界。
- **允许路径**：GD 设计文档、对应实现文件、已有证据目录。
- **禁止路径**：不得把 Linux 已验证等同于 Windows 全平台完成；不得删除或改写现有 BLOCKED 结论。
- **验收**：每个 gate 有实现入口、测试、平台状态和最终 decision；Windows publish 若仍缺 Python embed，必须保持 BLOCKED 或单独创建修复任务。
- **交付物**：GD/D0 gate matrix，引用原始证据 hash。

## A9：独立 Reviewer 终审

- **范围**：只读复核 A1-A8 的交付物和当前代码，给出 PASS/BLOCKED/UNVERIFIED。
- **允许路径**：本计划列出的证据目录、任务查询、源码只读检查。
- **禁止路径**：不补代码、不修改证据、不 apply/close 自己审查的任务。
- **验收**：逐子任务检查范围、命令、原始输出、hash、任务归属和状态；任何必需项 skip、证据矛盾或归属缺失都不得给 PASS。
- **交付物**：独立终审报告和明确的下一步任务列表。

# 5. 状态流转

```text
open -> in_progress -> review -> (独立 Reviewer) applied -> closed
```

实现/证据 Agent 只允许执行到 `review`。Reviewer 发现问题时创建或挂载新的 fix 子任务，原任务保持 `review` 或回到 `in_progress`；不得直接修改历史结果使其通过。

# 6. 推荐收口顺序

1. A1 契约复核。
2. A2、A4 平台原始证据重跑。
3. A3、A5 任务归属和证据绑定。
4. A6 D0 三文件聚焦提交。
5. A7 D0 状态核对、A8 GD gate 矩阵。
6. A9 独立 Reviewer 终审。
7. 只有 A9 明确 PASS 后，才由人工/授权 Reviewer 按任务树逐个 apply/close，最后处理父任务。
