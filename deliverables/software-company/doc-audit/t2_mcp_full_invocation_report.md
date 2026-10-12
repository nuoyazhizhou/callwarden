# T2 实测报告：243 个 MCP 工具全参数真实调用

日期：2026-10-13 05:10:36
环境：Windows，隔离 daemon（git HEAD c150807，二进制 f1fa3736a16dbe7d，
endpoint http://127.0.0.1:10917，完全隔离 data_root，**未触碰生产 daemon**），
种子 workspace（build_graph + snapshot publish + codegraph 读面 5 表补齐）。

## 1. 测试目标与方法

对全部 **243 个 MCP 工具**做**全参数真实调用**（create_mcp_server 装配 →
FastMCP.call_tool → route_rpc → daemon RPC），验证工具可路由、daemon 可执行，
定位真实缺陷。分层：READ_ONLY 全调；PROTECTED_MUTATION 非破坏性全调、破坏性 SKIP；
GOVERNANCE_WRITE 需 lease/状态机前置 SKIP。

## 2. 结果汇总

| 分类 | 数量 | 含义 |
| --- | --- | --- |
| PASS | 152 | rc==0 / call_tool 非 error |
| EXPECTED_BUSINESS | 77 | 合理业务拒绝（缺参/not_found/snapshot/identity 等） |
| DEFECT | 0 | 真实缺陷（method_not_found / traceback / internal_error） |
| SKIP | 14 | 破坏性/重操作/阻塞命令 |
| **合计** | **243** | |

## 3. DEFECT 明细（0）

**零 DEFECT**——243 工具全量真实调用无 method_not_found / daemon 不可用 / 代码级 internal_error。
