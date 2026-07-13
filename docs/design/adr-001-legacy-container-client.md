# ADR-001: Legacy 容器客户端策略

> 任务：T-1783952125417-d343 Step #0
> 状态：已决策
> 日期：2026-07-13

## 背景

Ubuntu 14.04 默认 Python 2.7，无法满足 Call Warden Python 3.9+ 要求。
设计文档 §6.2 给出两个选项：

1. 提供 musl 静态链接、兼容 Linux 3.13 的最小 `cw-agent`
2. Agent 固定运行在宿主机，14.04 容器只通过挂载路径和宿主 watcher 被观察

## 决策

**选择方案 2：宿主机 Agent + 容器只被观察**。

理由：
- 维护成本低：不需要为 Ubuntu 14.04 交叉编译静态 Python + Rust
- 架构清晰：agent 始终运行在现代 Python 环境，容器内文件通过 bind mount 暴露给宿主 watcher
- memfd 可选：Linux 3.13 无 memfd，宿主 agent 可使用 unlinked temp FD / streaming fallback
- 安全性：daemon 不读容器内绝对路径，文件内容通过 FD 传递

## 部署模型

```
宿主机 (Ubuntu 22.04+)
├── Enterprise Daemon (callwarden server)
├── User Agent A (cw --watch, Python 3.9+)
├── User Agent B (cw --watch, Python 3.9+)
└── Container Mount Point (/mnt/containers/<name>/...)

容器 (Ubuntu 14.04-24.04)
├── bind mount: /home/user/project → 宿主机 /mnt/containers/<name>/home/user/project
└── 不运行 Python agent
```

## 后果

- 14.04/16.04 容器仅作为文件源，不参与 Python 运行时
- 宿主 agent 通过 watcher 观察 bind mount 路径
- daemon 通过 `client_view_root` 展示信息，内容身份来自 Git/blob 或 FD
- VS Code Remote 场景沿用登录用户 agent，不增加 daemon 对容器 home 的读取权限
