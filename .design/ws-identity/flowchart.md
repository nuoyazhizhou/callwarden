# flowchart · ws-identity 端到端数据流（含异常分支）

```mermaid
flowchart TD
    A[客户端 workspace.register<br/>client_view_root] --> B[daemon 解析 checkout]
    B --> C{是 worktree？<br/>.git 指针文件}
    C -- 是 --> D[解析主仓 root<br/>agent_protocol 已有]
    C -- 否 --> E[主仓 root = 自身]
    D --> F{git 解析成功？}
    E --> F
    F -- 否 --> X1[fail-open：回退自身路径哈希<br/>+ warning 日志]
    F -- 是 --> G{有 remote？}
    G -- 是 --> H[remote URL 归一化<br/>https/ssh、.git 后缀、<br/>大小写、尾斜杠、剥离凭据]
    G -- 否 --> I[主仓 root 路径归一化<br/>realpath + 大小写 + 分隔符]
    H --> J[identity = hash 归一结果]
    I --> J
    X1 --> J
    J --> K[daemon upsert workspace 行<br/>identity 唯一约束]
    K --> L{存在旧路径身份行？}
    L -- 是 --> M[迁移：行合并 + 任务/证据/lease rewire<br/>旧 id → 新 id 映射表]
    L -- 否 --> N[返回 workspace_instance_id]
    M --> N
    N --> O[任务 / REQ 追溯 / ac-map / 证据 / lease<br/>全仓共享]

    X1 -.异常分支.-> N
```

异常分支说明：

| 分支 | 触发 | 行为 |
|---|---|---|
| X1 fail-open | 非 git 目录 / 无 remote 且无主仓 / 悬空 worktree | 回退自身路径哈希 + warning，注册不阻塞 |
| 迁移分支 | upsert 时发现同 repo 旧路径身份行 | 行合并 + rewire + 映射表，可重跑幂等 |
| 并发注册 | 两客户端同时首次注册同 repo | identity 唯一约束 + upsert 裁决（R2 确认现有约束） |

状态机视角（workspace 行）：

```mermaid
stateDiagram-v2
    [*] --> active: register（identity 唯一）
    active --> active: 重复 register（幂等 upsert）
    active --> merged: 迁移发现旧路径身份行
    merged --> [*]: 旧 id 进映射表（可审计查询）
```