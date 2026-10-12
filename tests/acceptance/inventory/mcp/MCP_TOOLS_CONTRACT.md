# TP-08M MCP wire 集合/schema 提取合同（冻结版 r1）

阶段 A1 · owner: inventory-mcp · 日期 2026-10-12 · 前置：TP-07（已 closed）

本文冻结 TP-08M 的提取方式、selector 与正负向义务。结果：**12 passed**（Windows + Py3.14 profile）。

## 1. 提取方式（真实子进程 wire）

| 项 | 手段 |
|---|---|
| 传输 | 真实子进程 `C:/Python314/python.exe cw.py server`（FastMCP stdio），**非进程内 manager** |
| 协议 | mcp 官方客户端 `stdio_client` + `ClientSession`：`initialize` → `list_tools(cursor=…)` 按 `nextCursor` 翻页至穷尽 → 断开 |
| 逐工具字段 | name / title / description / inputSchema / outputSchema / annotations |
| 语言固定 | `CALLWARDEN_LANG=en_US`，提取结果可复现 |
| 产出 | `docs/testing/manifests/mcp/mcp_tools.json`（LF + UTF-8） |

## 2. startup 隔离（本卡最重要的发现）

`daemon_autostart._find_daemon_binary()` 的解析顺序：①`CW_DAEMON_BIN`/`CW_DAEMON_BINARY`
env → ②`<USERPROFILE>/.callwarden/runtime/current/cw-daemon.exe` → ③PATH → ④项目
`rust_ext/target/{release,debug}`。

**🔴 本机 ambient 环境里 `CW_DAEMON_BIN` 已指向生产 `C:\Users\wanpi\.callwarden\runtime\
current\cw-daemon.exe`**（优先级①）。因此只覆盖 USERPROFILE/HOME/CALLWARDEN_HOME 的
「常规隔离」**不够**——MCP server 启动时 daemon_autostart 会按①解析并尝试启动**生产
daemon**。实测确认：未覆盖 `CW_DAEMON_BIN` 时，日志出现
`spawn_http_daemon: 已启动 detached daemon: <生产 runtime 路径>`。

**冻结隔离面**（extract_mcp_tools.py `isolated_env()`）：

| 动作 | 变量 |
|---|---|
| 覆盖到 run 根 | `USERPROFILE` / `HOME` / `CALLWARDEN_HOME` / `CALLWARDEN_DB` / `CALLWARDEN_DAEMON_REGISTRY_DB` |
| 钉死来源 | `CW_DAEMON_BIN` = TP-06 冻结候选 `.../cw-acceptance/TP-06/src/rust_ext/target/release/cw-daemon.exe`（sha256 `6324df12…`） |
| 清除继承 | `CW_DAEMON_BINARY` / `CW_DAEMON_TRANSPORT` / `CW_DAEMON_HTTP_BIND` |

效果实证：修复后提取期无 daemon spawn 日志、run 根无 manifest、生产 daemon 进程
（PID 26656）与生产 manifest 均未变。

## 3. 冻结 selector 与命令

```
提取: C:/Python314/python.exe tests/acceptance/inventory/mcp/extract_mcp_tools.py
      cwd=C:/git_work/callwarden；rc=0；输出 docs/testing/manifests/mcp/mcp_tools.json
验收: C:/Python314/python.exe -m pytest tests/acceptance/inventory/mcp/test_mcp_tools.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
      cwd=C:/git_work/callwarden；expected rc=0，12 passed
```

## 4. 产出规模（r1 实测）

- 工具数 **243**，翻页 **1** 页（单页即穷尽，cursor 逻辑仍走完整循环）
- 重复名 **0**，缺/空 inputSchema **0**
- initialize：`serverName=callwarden` / `serverVersion=1.28.1` / `protocolVersion=2025-11-25`
- manifest 290659 bytes，LF + UTF-8

## 5. 正向义务

| 义务 | 测试 |
|---|---|
| schema + source_revision 绑定、stdio 传输 | `test_schema_and_source_binding` |
| 真实子进程 wire + initialize 握手完整 | `test_real_subprocess_wire_extraction` |
| **wire 工具数 == 源码 `@mcp.tool()` 数**（12 模块合计 243） | `test_wire_count_matches_source_decorators` |
| 无重名 | `test_no_duplicate_tool_names` |
| 每个工具 inputSchema 为 object 且带 properties | `test_every_tool_has_object_input_schema` |
| 翻页至穷尽 | `test_pagination_exhausted` |
| 关键家族不丢（task_list/lease_acquire/assignment_create/task_get_role_prompt 等） | `test_known_tools_present` |

## 6. 负向义务

| 场景 | 测试 / 期望 |
|---|---|
| 重名工具 | `test_no_duplicate_tool_names`：duplicates 必须为空 |
| 缺/空 inputSchema | `test_missing_input_schema_rejected`：必须为空集合 |
| 未知 schema 形状 | `test_every_tool_has_object_input_schema`：type 必须为 object |
| startup 隔离失效 | `test_startup_isolation_pins_daemon_binary`：`CW_DAEMON_BIN` 必须钉在 run 根内候选且 sha256 绑定 TP-06；含 `runtime` 生产路径即失败 |
| 生产 manifest 被覆盖 | `test_production_manifest_untouched`：生产 manifest 的 daemon_executable 必须仍指向 runtime |
| manifest 行尾/编码漂移 | `test_manifest_is_lf_and_utf8` |
| 捏造占位 | `test_no_placeholder_entries` |

## 7. 边界与未决

- 本卡只提取「工具集合 + schema」，不调用任何工具（业务语义是 TP-10M scope）。
- 工具分类维度（`server/tools/_categories.py` 的 17 类）未在 wire annotations 暴露
  （本 MCP 版本 annotations 为 null），跨端分类合并留给 TP-08F。
- 未创建正式 daemon 任务（§7 入库门禁未过），不伪造 task_id。
- 与旧 `tests/convergence` 隔离：`--noconftest`，不改任何旧 conftest。
