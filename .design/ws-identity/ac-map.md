# ac-map · ws-identity（R3 定稿）

> 快照范围：must = REQ-01..04（C-01..C-08）；指定 should = REQ-05（C-09，第二批建单）。
> 证据统一落 `.design/ws-identity/evidence/<AC-id>.md`。

| AC id | 对应 REQ | 关联卡片 | 验收方式 | 前置条件 | 证据位置 |
|---|---|---|---|---|---|
| AC-01 | REQ-01/02/05 | C-01、C-02、C-03、C-09 | 端到端：主仓 + 2 个 worktree 三客户端依次 `workspace.register` → 返回同一 `workspace_instance_id`；不同仓库注册 → 不同 id | daemon 测试模式可用；临时 git 仓 + 2 worktree | `evidence/AC-01.md`（进程级测试输出） |
| AC-02 | REQ-02/05 | C-02、C-09 | 端到端：A 在 worktree 建任务 → B 在主仓 `task_list` 可见；两 worktree 对同一文件任务 `lease.acquire` 互斥（一方持锁另一方拒绝） | 同 AC-01（同一测试会话） | `evidence/AC-02.md`（进程级测试输出） |
| AC-03 | REQ-01/04 | C-01（Rust 内嵌测试）、C-03、C-08 | 单测矩阵：大小写 / `\\?\` 前缀 / 正反斜杠 / 尾斜杠 / remote 形式（https、ssh、`.git` 后缀、带凭据）/ 同目录不同书写 → 同一 identity；不同 remote、不同主仓 → 不同 identity；凭据剥离断言。Python 与 Rust 两侧同语义用例各一套，跨语言锚用例固定输入向量 | pytest / cargo test 可运行 | `evidence/AC-03.md`（pytest / cargo test 输出） |
| AC-04 | REQ-03 | C-04、C-05、C-06、C-07 | 迁移：旧 id → 新 id 映射可查（capture 重定向）；任务 / 证据 / lease / grants 行 rewire 完整（行数对账）；registry 中同 repo 仅一条 active（PL-06 清理）；`--dry-run` 零副作用；重跑幂等；迁移前自动备份 | 测试库预置同 repo 多 workspace 行 + 任务/lease/grants 样本 | `evidence/AC-04.md`（迁移前后对账 + dry-run + 幂等重跑输出） |
| AC-05 | REQ-01 | C-01、C-02、C-03、C-09 | fail-open：非 git 目录、悬空 worktree（`.git` 指针指向已删主仓）注册成功，回退自身路径哈希 + warning 日志可见 | tmp 非 git 目录 / 悬空 worktree fixture | `evidence/AC-05.md`（测试输出 + 日志摘录） |

判定口径：AC 通过 = 证据文件存在且内容支持该 AC 断言；证据缺失或指向不存在文件 = 未通过（硬规则 5）。
