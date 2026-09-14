# T-1789365537230-c3f02eb4 Adjudicator 闭环记录（C-17）

- Adjudicator 身份：`adjudicator-wb-c17-01` / `inst-adj-wb-c17-01` / `sess-adj-wb-c17-20260914`
- Adjudicator lease：`L-464605cf68d24751`（**role=reviewer**，fencing=2，apply/close 后已 released）
- 终态：`lifecycle_status=closed` / `workflow_status=completed` / `decision=COMPLETE` / `next_action=finalize`

## 1. 终审输入

| 项 | 值 |
|---|---|
| 修复 commit | `186582ee2f18dddae41500b4d3556eca267d9a0e`（单 commit，本卡 task_id 前缀） |
| Review verdict | `V-59ba9c1b4ba50de7efb2f804`（blind_first_pass / pass / findings=4 / event 634） |
| source step | `S-1789365537231-c403fca0`（step3 release_verify） |
| snapshot_id | `dfcac6f16b827a30` |
| view_manifest_hash | `b6f9f163537a5464f91696f6232b635e89e2f145fa860ad09af15e3c8d2f94bb` |
| 执行证据 | `T-1789365537230-c3f02eb4-evidence.md`（sha256 `f763d6f6…`） |
| 独立复核 | `T-1789365537230-c3f02eb4-reviewer-review.md`（sha256 `fe935a5d750b7e7625846bc6e78751bf57b16b24ca5bab6aa192e562df84e7b5`） |
| handoff 链 | event 8912（executor→reviewer）→ event 8913（reviewer→adjudicator） |

## 2. 独立终审核验（非复读 reviewer 结论）

1. **影响半径**：commit 仅触及 `rust_ext/src/daemon/snapshot_state.rs` 与
   `tests/test_c17_admin_route_workspace_authority.py`；无 `server/`、`db/**`、
   `scripts/refresh_shared_runtime.ps1` 改动（与 step0 禁止路径裁决一致）。
2. **根因收口面**：路由层 `open_codegraph_db_write` 为 guard 内 19+2 方法**单点**收口，
   handler 层零改动 —— blast radius 最小化，且与 semgrep 写面既有先例同源。
3. **运行验证**：Rust `cargo test --lib task_collab` 150/150；隔离矩阵 18/18（新二进制）
   vs 基线反证 10 failed/8 passed；部署门禁 `passed/rollback=false/git_head==HEAD`；
   生产实例只读实测 `gc_retention` 151/2205、`snapshot_compare` 151/2205/39723、
   `gc_archive_list` 5 行、`inspect` 命中 id=151（修复前全部恒空/0）。
4. **findings 处置**：4 条（1 范围内模板缺陷 + F1/F2/F3 相邻缺陷）均已在 backlog 登记
   并建议承接卡，无未披露偏差；verdict provenance 完整（snapshot/view_manifest/contract
   三元组/reviewer 身份/lease 全链可溯）。

## 3. 受保护 apply/close

```
task apply  → applied  （event 8916，adjudicator 持 reviewer lease，fencing=2）
task close  → closed   （event 8917）
lease release → L-464605cf68d24751 released
```

lease 审计链（无残留空档）：

| lease | role | holder | fencing | 终态 |
|---|---|---|---|---|
| `L-98683177c9bc1072` | implementer | executor-wb-c17-01 | 1 | released |
| `L-238c778e09e264c6` | reviewer | reviewer-wb-c17-01 | 1 | released |
| `L-464605cf68d24751` | reviewer | adjudicator-wb-c17-01 | 2 | released |

## 4. 残余与移交

1. **C 桶残留缺陷（均不在本卡 scope，建议建卡）**：
   - **C-18**（backlog §W19）：MCP-015 `tests/test_mcp_assignment_show_http_rpc.py`
     4 例陈旧断言 + `_w3_harness.find_daemon_binary()` mtime/MSYS 隐患（卡 C 遗留）；
   - **C-19（建议）**：F1 `gc_audit_get/list` 引用不存在的 `tasks.workspace_id`；
   - **C-20（建议）**：F2 `record_action_identity` 缺 `action_id`；F3
     `register_attestation_revocation` 缺 `revocation_id`（可并入一张卡）；
   - **C-21（建议）**：`edit.*/rule.*` 第二路由块（`snapshot_state.rs:3262-3273`）同类
     代理 id 缺陷（step0 已核验存在，本卡登记范围外）；
   - **C-22（建议）**：C 桶建卡模板目录级 `target_file` 白名单与 `changes[]` 全等比对
     不兼容（本卡 verdict finding #1）。
2. **部署披露**：refresh 脚本 post-swap 清理 `genie-trash` 拒绝访问（swap 已完成，既有
   环境缺陷）；脚本拉起的 daemon 随父进程退出（既有缺陷），已前台 exec 重启
   pid 7744 / endpoint 8950 / `git_commit=186582e==HEAD` / healthy。
3. backlog §W18 计数更正：admin 路由块方法名 **21（guard 列表）− 2（卡 A 已收口）= 19**，
   原记 18 系漏计 `_ =>` 兜底臂的 `admin.select_interface_provider`。
