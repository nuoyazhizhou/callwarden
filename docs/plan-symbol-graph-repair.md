# 修复符号图谱与查询链路

## 子任务 1：复现与故障边界

- Scope: `rust_ext/src/daemon/`, `server/daemon_client.py`, `tests/`
- 验收：区分 Python-free daemon 的发布失败、查询 RPC 路由和本地 SQL fallback。

## 子任务 2：原生图谱发布和符号查询修复

- Scope: `rust_ext/src/graph.rs`, `rust_ext/src/snapshot.rs`, `rust_ext/src/daemon/`, `rust_ext/src/bin/cw_daemon.rs`
- 验收：不依赖嵌入 Python 的 daemon 可以从 SQLite 发布可查询图谱。

## 子任务 3：MCP/客户端回归覆盖

- Scope: `server/daemon_client.py`, `server/tools/`, `tests/`
- 验收：`get_stats`、`get_file_symbols`、`get_callers`、`get_callees` 和无预存图谱的构建路径都有针对性回归。
