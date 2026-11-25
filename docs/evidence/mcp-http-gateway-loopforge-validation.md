# MCP HTTP 网关对接 LoopForge 模式 A —— 验收证据

- 任务卡：T-1795466083667-eb40402c（步骤 2：用 LoopForge MCPServerDefinition 形状做对接验证）
- 实现来源：origin/master `8f3d7b2`（feat(mcp): MCP HTTP 网关原型对接 LoopForge 模式 A）
- 验证时间：2026-11-24 07:32:17 +0800
- 原始证据：[mcp_http_gateway_evidence.json](../../tests/acceptance/mcp/evidence/mcp_http_gateway_evidence.json)
- 证据 sha256：`fd5c0dba1b0081e72f0abca0045dd2a765510d8c4d0af6ad5cc60c6879657797`

## 1. 对接配置（LoopForge MCPServerDefinition 形状）

```json
{
  "name": "callwarden",
  "transport": "http",
  "url": "http://127.0.0.1:9624/mcp",
  "headers": { "Authorization": "Bearer ***" },
  "timeout_sec": 60
}
```

该配置经 `parse_mcp_server_definition` 形状校验通过（transport=http + url +
Bearer 头 + timeout_sec 齐备），再交给 MCP client 建连。

## 2. 验证结果

| 检查项 | 结果 |
| --- | --- |
| initialize | 成功，server_name=`callwarden-gateway`，protocol_version=`2025-11-25` |
| tools/list（profile=all） | 244 个工具 |
| tools/call search_symbols | 命中 `server/tools/tools_summary.register.review_readiness` |
| tools/call review_readiness | 成功，返回 risk_level=`low`、total_impacted=1、by_layer/coverage/must_test/review_points 齐备 |
| 网关配置 | transport=streamable-http，host=127.0.0.1，profile=all，response_budget_bytes=262144 |

## 3. 复现方式

```powershell
# 仓库根目录执行；随机端口起网关 → 形状校验 → initialize → tools/list → review_readiness
python tests/acceptance/mcp/mcp_http_gateway_evidence.py
# 期望输出：EVIDENCE_OK <evidence.json>; tool_count: 244
```

说明：网关进程使用一次性 HMAC 密钥签发本次专用 Bearer token，token 未落盘
（证据中脱敏为 `Bearer ***`）；daemon 侧 mTLS 凭据保留在网关进程内，不下发
到 harness。
