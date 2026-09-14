# T-1789365537230-c3f02eb4 独立 Reviewer 复核报告（C-17）

- Reviewer 身份：`reviewer-wb-c17-01` / `inst-rev-wb-c17-01` / `sess-rev-wb-c17-20260914`
  （与执行者 `executor-wb-c17-01` / `inst-exec-wb-c17-01` / `sess-exec-wb-c17-20260914` 完全隔离）
- Reviewer lease：`L-238c778e09e264c6`（fencing=1，review 后已 released）
- Verdict：`V-59ba9c1b4ba50de7efb2f804`（blind_first_pass / overall=pass / findings=4 / event 634）
- view_manifest_hash：`b6f9f163537a5464f91696f6232b635e89e2f145fa860ad09af15e3c8d2f94bb`
  （`get_role_view(task, role=reviewer)`，contract_hash 与 `task_contract_revisions` 一致 = `36535bd0…`）

## 1. 盲审方法

不采信执行者叙述，直接从源码、权威库与部署产物独立复核。复核项 R1-R8：

| # | 复核项 | 命令/手段 | 结果 |
|---|---|---|---|
| R1 | 提交内容与边界 | `git show --stat 186582e` | 仅 2 文件：`snapshot_state.rs` +30/−10、`tests/test_c17_admin_route_workspace_authority.py` +558；前缀 `[T-1789365537230-c3f02eb4]` |
| R2 | 禁止路径 | `git show --name-only \| grep ^(server/\|db/\|scripts/)` | 未命中（rc=1） |
| R3 | diff 卫生 | `git diff --check 186582e~1..186582e` | clean |
| R4 | 修复语义 | 读 `snapshot_state.rs:3186-3245` 现源码 | guard 21 方法统一 `open_codegraph_db_write`（ACL 保留 + `client_view_root` 规范化匹配 `workspaces.root_path` 取真 id），handler 分发逐字核对 |
| R5 | 文件哈希自证 | sha256 | evidence=`F763D6F6…`（== report/handoff 登记）、inventory=`76C7FC1B…` |
| R6 | 部署三方哈希 | sha256 + evidence JSON + `daemon health` | `runtime/current`=`C9C3A5DB…` == 门禁 JSON `daemon_runtime.sha256`；`git_commit=186582e==HEAD`；worker healthy |
| R7 | **对已部署二进制独立复跑** | `CW_DAEMON_BIN=runtime/current pytest …-q` | **18/18 通过** |
| R8 | F1-F3 真实性 | 读 `admin_handlers.rs:138/:176` 与 schema | F1：SQL 引用 schema 不存在的 `tasks.workspace_id`；F2/F3：`:675/:713` INSERT 列清单缺 NOT NULL UNIQUE 的 `action_id`/`revocation_id` —— 三者均为真实缺陷，测试内回归锚如实失败 |

判别力核验：基线二进制 `87C6200…`（不含本修复）跑同套用例 = **10 failed / 8 passed**
（`..FFFFFFFFFF......`），失败条目与 step0 预测逐条吻合（FK 恒失败 / WHERE 不命中静默空 / 无 FK 表脏写代理 id）——用例具判别力。

## 2. 判定：PASS（附 4 条 findings）

1. **IN_SCOPE_TEMPLATE_DIRECTORY_WHITELIST**（范围内，判可接受）：step2/step3 的
   `target_file` 白名单为目录（`tests/`、`rust_ext`），与 `changes[]` 全等比对不兼容
   （`E_CHANGE_PATH_NOT_ALLOWED` 实测复现）。执行者按 C-13 卡先例以 evidence + commit
   + 工作树 diff 留痕，未伪造目录级 `file_path`。建议 C 桶建卡模板改用逐文件白名单。
2. **ADJACENT_F1_GC_AUDIT_MISSING_COLUMN**：`admin.gc_audit_get/list` 引用不存在的
   `tasks.workspace_id`，prepare 恒失败。非命名空间错配，须独立缺陷卡。
3. **ADJACENT_F2_ACTION_IDENTITY_MISSING_NOTNULL**：`record_action_identity` 缺
   NOT NULL UNIQUE 列 `action_id`，恒失败。须独立缺陷卡。
4. **ADJACENT_F3_REVOCATION_MISSING_NOTNULL**：`register_attestation_revocation` 缺
   NOT NULL UNIQUE 列 `revocation_id`，恒失败。须独立缺陷卡。

## 3. 结论

root cause addressed（19 handler 的 workspace 命名空间错配在路由层一次性收口，代理
ROWID 不再进入 handler）、forbidden paths untouched、回归可复现（隔离矩阵 + 生产只读
实测双证）→ **PASS**，交 Adjudicator 终审。
