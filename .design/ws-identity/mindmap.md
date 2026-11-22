# mindmap · ws-identity 领域概念

```mermaid
mindmap
  root((workspace 身份))
    身份派生
      remote URL 归一
        https / ssh 形式
        .git 后缀
        大小写 / 尾斜杠
        剥离嵌入凭据
      主仓 root 路径兜底
        worktree .git 指针解析
        realpath 归一
      自身路径兜底（fail-open）
    身份消费方
      workspace 行（registry + task DB）
      任务库（task / REQ 追溯 / ac-map）
      验收证据（evidence）
      lease（全仓互斥）
      principal 授权面
    存量迁移
      workspace 行合并（active 归一）
      任务 / 证据 / lease rewire
      旧 id → 新 id 映射表
      PL-06 \\?\ 重复行清理
    一致性约束
      Python / Rust 同语义
      注册幂等（upsert）
      并发首次注册唯一约束
```

概念要点：

- **身份派生三级**：remote → 主仓 root → 自身路径；前两级失败不阻塞（fail-open）。
- **checkout 只是执行位置**：worktree / clone 不再产生独立身份，任务与证据天然全仓共享。
- **迁移是行 rewire 不是数据合并**：任务、证据、lease 原样保留，只改 workspace_id 归属；旧 id 留映射表审计。