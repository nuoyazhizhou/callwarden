# Evidence manifest: registry 同根多 workspace 注册歧义（2026-11-24）

- 关联任务：T-1795507699058-41d9fcbc（ROOT FIX 根修卡，supersede T-1795491554078-3616ae10）
- 关联 step：S-1795507699843-70a0bea0（D3 fix server/daemon_autostart.py）
- 原证据清单（哈希已锚定 supersede，不可改）：docs/reports/supersede-root-fix-evidence-20261124.md，sha256 `e482f55e659573c101fa8fdb7cee7d0d179ac65d7af99108fd3c3eebe5a53085`
- 完整附录（本 manifest 的证据正文）：docs/reports/supersede-root-fix-evidence-addendum-20261124.md

## 摘要

registry.db `daemon_workspaces` 中同一 callwarden 仓库根存在 5 条 `status=active` 注册（workspace_id 138/142/149/207/208，instance 65666bde/723bb365/eb81cf53/4baea3ff/822c031c）；路径写法含 `C:\` / `c:\` / `C:/` 三种变体，未做规范化。仅 `4baea3ff…`（task DB workspace_id=1）持有 authority capture（revision 1–4），其余 4 条零 capture，命中 `dispatch.rs:344-352` 的自述误判分支。

CLI `cw lease acquire` 启动期取到陈旧 instance `822c031c71488f12`，报 `E_PRINCIPAL_WORKSPACE_REQUIRED`；换两个不同身份均复现，与身份无关。绕过 CLI 启动门禁、直接 daemon RPC 显式注入权威 workspace 对（1 / 4baea3ff12c2ea5c）成功，supersede 凭证 `SUP-267ca30f6351eb82`。

归属根修卡 D3（自愈单实例守卫 / 命名空间消歧）：自愈裸 Popen 产生第二进程，registry 多注册产生第二身份，同为「同一实体多权威表示」。建议 D3 增加验收：同根仅一条 active 注册、注册前路径规范化、无 capture 的陈旧注册不可选。4 条陈旧注册作为一手证据保留，不直接删库。

## 采集方式

SQLite 只读 URI 连接（mode=ro）+ 源码定位（cli/main.py:1259-1263/1286-1287/3768-3796、dispatch.rs:344-352）+ CLI/RPC 行为复现；未修改任何 DB 内容。
