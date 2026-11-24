# supersede 证据清单附录：registry 同根多 workspace 注册歧义（2026-11-24）

## 0. 补录说明（为何用附录而非改原清单）

原证据清单 `docs/reports/supersede-root-fix-evidence-20261124.md`（sha256 `e482f55e659573c101fa8fdb7cee7d0d179ac65d7af99108fd3c3eebe5a53085`）已作为 supersede `SUP-267ca30f6351eb82` 的 `evidence_hash` 参与权威记录，**原文件不得改动**，否则追溯链断裂。

本附录记录 supersede 执行后审计出的新发现（同日 16:05–16:21 GMT+8），内容不覆盖、不修订原清单结论；原清单 5 节结论保持有效。关联任务：successors 卡 `T-1795507699058-41d9fcbc`（根修卡），主题归属 **D3 自愈单实例守卫 / 命名空间消歧**，step `S-1795507699843-70a0bea0`（`fix server/daemon_autostart.py`）。

## 1. 铁证：registry 同一个仓库根有 5 条 active 注册

数据源：`C:/Users/wanpi/.callwarden/registry.db`（只读连接，`daemon_workspaces` 表，`host_real_root LIKE '%allwarden%'`）。全部 5 条 `status=active`、`owner_uid=4294967295`、`git_remote_url=git@github.com:nuoyazhizhou/callwarden.git`：

| registry workspace_id | workspace_instance_id | snapshot_id | git_head_commit_sha | client_view_root | registered_at | 权威 capture |
|---|---|---|---|---|---|---|
| 138 | `65666bdea25673e8` | `144a1565717e4118` | `ba6e9aae326a` | `C:\git_work\callwarden` | 2026-09-02 10:32:58 | ✗ 无 |
| 142 | `723bb365058de6e6` | `144a1565717e4118` | `ba6e9aae326a` | `c:\git_work\callwarden` | 2026-09-02 13:46:20 | ✗ 无 |
| 149 | `eb81cf5375f5446e` | `144a1565717e4118` | `ba6e9aae326a` | `C:\git_work\callwarden` | 2026-09-02 23:12:12 | ✗ 无 |
| **207** | **`4baea3ff12c2ea5c`** | `64e25edc28019f0f` | `ccbb72b8255d` | `C:/git_work/callwarden` | 2026-09-03 14:24:11 | ✓ **有（task DB `workspace_id=1`，revision 1–4）** |
| 208 | `822c031c71488f12` | `144a1565717e4118` | `ba6e9aae326a` | `C:\git_work\callwarden` | 2026-09-07 12:33:29 | ✗ 无 |

补充事实：

- 5 条注册的 `client_view_root` 采用 `C:\` / `c:\` / `C:/` 三种写法，`host_real_root` 为 `\\?\C:\...` 或 `c:/...` —— **注册路径未做规范化**，是同根多注册的直接成因。
- 4 条陈旧注册共享同一个旧 snapshot `144a1565717e4118` 与旧 git_head `ba6e9aae326a`；只有 `4baea3ff…` 持有 `64e25edc28019f0f` / `ccbb72b8255d`。
- 全部 `last_active_at` 都是 2026-09-19（陈旧注册从不被清理，只保留 active 状态）。
- `registry.db` 的 `daemon_state.schema_version = 60`。

## 2. 铁证：只有权威 instance 有 authority capture，其余 4 条命中 fail-closed 分支

数据源：任务库 `C:/Users/wanpi/.callwarden/callwarden.db` 的 `workspace_authority_captures` 表（只读连接）。

- `workspace_instance_id = '4baea3ff12c2ea5c'`：capture revision 1→2→3→4（`wc-4baea3ff12c2ea5c-*`），`workspace_id=1`，`daemon_workspace_id=0`，registry/task-DB 双身份哈希齐全。
- `'ws-1'`：另有 revision 1→16 的 capture 链（`wc-ws-1-*`）。
- `65666bde…` / `723bb365…` / `eb81cf53…` / `822c031c…`：**零条 capture**。

代码坐标：`rust_ext/src/daemon/dispatch.rs:344-352` 注释原文指出，`resolve_instance_workspace_id` 默认实现把未注册实例一律 `Ok(None)`，导致「已 register、尚无 authority capture」的 workspace 首轮调用（`workspace.status` / `activate`）被误判为伪造实例并报 `E_PRINCIPAL_WORKSPACE_REQUIRED`。**4 条陈旧注册正好全部落在这个误判分支里。**

对照：任务库 `workspaces` 表中 callwarden 仓库根只有 `id=1 / root_path='c:/git_work/callwarden' / is_active=1` 一条；另有 22 条 `is_active=0` 的同前缀记录，均为子目录 workspace（daemon/tools/testcode/workbuddy scripts 等），**不是本次歧义源**。歧义源在 registry 侧，不在本地 workspace 表。

## 3. 故障复现与判定

supersede 执行期（16:05–16:12）连续出现同一门禁失败：

```
E_PRINCIPAL_WORKSPACE_REQUIRED: workspace_instance_id 822c031c71488f12 不可解析
```

- 出现位置：`cw lease acquire`（CLI 启动期，尚未进入 lease 子命令）——`cli/main.py:1259-1263` 的 workspace 激活循环取第一个 `root_path == workspace_root` 匹配即 `break`，无规范化、无 capture 前置校验；错误注入点 `cli/main.py:1286-1287`。
- 控制变量：换用两个不同身份（`adjudicator-rofix-01`、`executor-webobs-mvp-01`）均复现 → **与身份无关，是 workspace 解析歧义**。
- 权威路径对照：`cli/main.py:3768-3796` 的 `_resolve_caller_workspace_instance_id` 本身是 fail-closed 设计（禁止本地哈希推导、必须 daemon 权威解析）——设计方向正确，失败来自上游 registry 多注册没有消歧。

绕法（已实测成功）：绕过 CLI 启动期门禁，直接经 daemon RPC `lease.acquire` 显式注入 `workspace_id=1 / workspace_instance_id=4baea3ff12c2ea5c`，取到 fencing=1 的 reviewer lease；随后注册独立 reviewer 身份取到 fencing=2 的合规 lease，supersede 成功（`SUP-267ca30f6351eb82`）。

## 4. 对根修卡的影响与建议

1. **归属 D3**：本问题与 D3「同一台机只存在一个 daemon 实例」「workspace 双命名空间明示」同主题——自愈裸 Popen 产生第二进程，registry 多注册产生第二身份，两者都是「同一实体多个权威表示」。
2. **建议 D3 step 增加验收项**：同一仓库根在 registry 中只允许一条 active 注册；注册前对 `client_view_root` / `host_real_root` 做规范化（大小写、分隔符、尾斜杠、`\\?\` 前缀）；无 authority capture 的陈旧注册视为不可选，不得参与 `workspace_instance_id` 推导。
3. **不要直接删库清理**：4 条陈旧注册是本次审计的一手证据，保留原样；由代码层消歧或另立数据清理卡（需用户确认），避免破坏追溯。
4. **待核差异（不参与本次判定）**：观测面网关 `/api/health` 横幅此前实时显示 `registry_workspace_id=1193`，而本次静态查询 `registry.db` 中 `4baea3ff…` 对应 `workspace_id=207`。两者不一致的原因未定（可能多份 registry 文件或运行时状态未落盘），留给 D3 审计，不写入原清单结论。

证据采集人：本执行会话；采集窗口：2026-11-24 16:05–16:21 GMT+8；采集方式：SQLite 只读 URI 连接（`mode=ro`）+ 源码定位 + CLI/RPC 行为复现，未修改任何 DB 内容。
