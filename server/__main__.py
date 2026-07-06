#!/usr/bin/env python3
"""
MCP 服务器启动入口（支持 stdio 传输，适用于多容器共享部署）

使用方式：
  cw server
  cw server --transport stdio  # 默认
  cw server --transport sse    # SSE 模式

多容器共享部署：
  1. 在宿主机安装: pip install callwarden（或直接用此脚本）
  2. 数据库文件放在 $HOME/.callwarden/<16位hash>/callwarden.db（所有容器共享 $HOME）
  3. 每个容器配置 MCP client 指向同一个数据库路径
  4. 多进程安全：SQLite 支持多读者单写者，写入会自动排队

启动时自动同步 AGENTS.md（C2 新增）：
  - 服务器启动时调用 rule_sync_agents_md(dry_run=False)，把当前 active
    的 Agent Rule Memory 同步到 AGENTS.md 标记区
  - 同步失败不阻断启动（fail-soft），仅输出提示到 stderr
  - 标记区不存在时静默跳过（需先运行 `cw rule insert-block` 插入标记块）
  - 同步日志记录到 agent_rule_sync_log 表，actor="mcp_server_startup"
"""

from .mcp_server import main

if __name__ == "__main__":
    main()
