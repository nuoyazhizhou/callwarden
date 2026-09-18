# 2026-09-11 ~ 09-14 提交元数据存档（对象已丢失）
来源：`callwarden-metadata-backup-20260914/logs-HEAD.stale`（74 条 HEAD reflog）。
这 72 个 commit 对象已从对象库消失（不可 `git cat-file` 取回），
但 reflog 备份保留了父提交、作者、时间、message。本文档将其固化入仓，
使提交历史至少以元数据形式可追溯。恢复分支 `recovery/2026-09-14-snapshot`
（`2b97894c`）保存的是 09-14 19:12 的文件快照，不是逐提交历史。

## 汇总

| 项目 | 值 |
|---|---|
| 提交数 | 74 |
| 时间范围 | 2026-09-11 04:08:30 +0800 ~ 2026-09-14 11:12:09 +0800 |
| 链根（工作线真实起点） | `9855da6c08c996b0092ca421b69e7901b3873f71` |
| 链尖（09-14 最后提交） | `b6fbcc091ae8334c9a4de37cd15adac416d1dc90` |

## 提交元数据（时间正序）

| # | 提交 sha | 父 sha | 时间 | message |
|---|---|---|---|---|
| 1 | `e06485368969` | `9855da6c08c9` | 2026-09-11 04:08:30 +0800 | (空) |
| 2 | `9855da6c08c9` | `9855da6c08c9` | 2026-09-11 04:09:19 +0800 | reset: moving to HEAD |
| 3 | `9855da6c08c9` | `53b2958b18c5` | 2026-09-11 04:11:31 +0800 | commit: recovery: 对象库灾难恢复——重建 9855da6 之后全部本地工作（原 5 个治理提交内容保全） |
| 4 | `53b2958b18c5` | `c88c89a548d0` | 2026-09-11 04:16:44 +0800 | commit: ledger: GOV-FIX-06 cascade_close 拒绝语义修复——治理卡全循环闭环 |
| 5 | `c88c89a548d0` | `e936787f20ba` | 2026-09-11 05:57:21 +0800 | commit: ledger: GOV-FIX-05 批次 2——110 张历史遗留卡 supersede 收尾 + 批次 1 根卡 6/7 闭合 |
| 6 | `e936787f20ba` | `cedadf0a8590` | 2026-09-11 07:19:41 +0800 | commit: fix(cli): GOV-FIX-07 daemon 业务拒绝 CLI 层 RC=0 假成功修复 |
| 7 | `cedadf0a8590` | `52c01694d58b` | 2026-09-11 07:27:25 +0800 | commit: ledger: GOV-FIX-07 daemon 业务拒绝 CLI RC=0 假成功修复——治理卡全循环闭环 |
| 8 | `52c01694d58b` | `c98102691f67` | 2026-09-11 08:03:44 +0800 | commit: fix(cli): GOV-FIX-08 socket 传输 error dict 路径 RC=0 假成功统一巡检修复 |
| 9 | `c98102691f67` | `9e3cf1f2bbc5` | 2026-09-11 08:08:51 +0800 | commit: ledger: GOV-FIX-08 socket 传输 error dict 路径 RC=0 假成功统一巡检修复——治理卡全循环闭环 |
| 10 | `9e3cf1f2bbc5` | `9ece8cc36fc4` | 2026-09-11 14:47:21 +0800 | commit: [T-1788871227327-45c94bd8] test: cutover 套件对齐 compat 全量退役终态（step0 stale 分桶） |
| 11 | `9ece8cc36fc4` | `cf3e7f1de1c8` | 2026-09-11 15:18:06 +0800 | commit: [T-1788871227327-45c94bd8] evidence: step0 stale 分桶证据清单（归因 A/B 实证 + 环境因素） |
| 12 | `cf3e7f1de1c8` | `e16d79cfbc77` | 2026-09-11 15:20:47 +0800 | commit: chore(tokenslim): 启用本项目工作区压缩审计 debug.audit（.tokenslim/audit/compression.jsonl） |
| 13 | `e16d79cfbc77` | `306b74411b80` | 2026-09-11 15:45:47 +0800 | commit: [T-1788871227327-45c94bd8] test(A桶): 陈旧断言对齐 daemon authority |
| 14 | `306b74411b80` | `15de3c8f14fd` | 2026-09-11 16:40:11 +0800 | commit: [T-1789139378194-02f1f66c] test(A桶-2): 断言对齐 daemon authority 落点（21→10 FAILED） |
| 15 | `15de3c8f14fd` | `904df983cadf` | 2026-09-11 16:41:07 +0800 | commit: [T-1789139378194-02f1f66c] evidence: A 桶第二批修复清单 + D1/D2 相邻缺陷登记 |
| 16 | `904df983cadf` | `9d4193155509` | 2026-09-12 00:42:05 +0800 | commit: [T-1789139378194-02f1f66c] fix(db): D1 新库首文件注册补 file_contents 占位（FK 修复） |
| 17 | `9d4193155509` | `eb15a7e9d77f` | 2026-09-12 00:42:21 +0800 | commit: [T-1789139378194-02f1f66c] test: file_symbol_content 走 RPC 契约 + legacy v21 构造贴近真实 + D2 回归用例 |
| 18 | `eb15a7e9d77f` | `8211219672dd` | 2026-09-12 00:43:19 +0800 | commit: [T-1789139378194-02f1f66c] evidence: D1/D2 修复证据（含 A/B 反证与回归结果） |
| 19 | `8211219672dd` | `b87f545bfb73` | 2026-09-12 01:10:21 +0800 | commit: [T-1789139378194-02f1f66c] fix(db): v2→v3 迁移空 hash 占位 + 修正过早 DROP（D3） |
| 20 | `b87f545bfb73` | `3e4795039722` | 2026-09-12 01:14:53 +0800 | commit: [T-1789139378194-02f1f66c] test(A桶): task reopen CLI 断言对齐 daemon RPC 契约 |
| 21 | `3e4795039722` | `1061ba3d39bd` | 2026-09-12 01:15:33 +0800 | commit: [T-1789139378194-02f1f66c] evidence: D3 修复 + D4/D5 连带过早 DROP + reopen A 桶证据 |
| 22 | `1061ba3d39bd` | `fd23c89d13aa` | 2026-09-12 01:34:01 +0800 | commit: [T-1789139378194-02f1f66c] fix(db): D3 占位行改为按 NOT NULL 约束动态构造 |
| 23 | `fd23c89d13aa` | `3b4b56578522` | 2026-09-12 05:24:39 +0800 | commit: [T-1788871227327-45c94bd8] escalate(step4): 身份误用(step_id 当 task_id) + db/** 越 scope 提交，待裁决 |
| 24 | `3b4b56578522` | `f82d55feb776` | 2026-09-12 05:55:29 +0800 | commit: [T-1788871227327-45c94bd8] evidence(step4): 全量验收实测——主体失败为环境欠配（153→58，零改码） |
| 25 | `f82d55feb776` | `c4ae6d0017ab` | 2026-09-12 06:00:22 +0800 | commit: [T-1788871227327-45c94bd8] ledger: 更正 9 个提交的 step_id→task_id 前缀误用（含 db/** 越 scope 标注） |
| 26 | `c4ae6d0017ab` | `a470c8a8a9ee` | 2026-09-12 06:10:58 +0800 | commit: [T-1788871227327-45c94bd8] evidence(step4): 逐个解决进度台账 + CLI 信封未解包 finding |
| 27 | `a470c8a8a9ee` | `688fe387fd1d` | 2026-09-12 06:15:05 +0800 | commit: [T-1788871227327-45c94bd8] evidence(step4): 更正 finding#9（信封为陈旧契约，非缺陷）+ 记录分片驱动 |
| 28 | `688fe387fd1d` | `f9705bea3405` | 2026-09-12 12:39:17 +0800 | commit: [T-1788871227327-45c94bd8] test(A桶): defect 读组工具改锁模块级 _route 契约（16 失败→0） |
| 29 | `f9705bea3405` | `443e27461294` | 2026-09-12 12:40:36 +0800 | commit: [T-1788871227327-45c94bd8] test(A桶): cli_081 断言对齐 GOV-FIX-07/08 + 路由层栈（6 失败→0） |
| 30 | `443e27461294` | `593adf67bf7c` | 2026-09-12 12:41:01 +0800 | commit: [T-1788871227327-45c94bd8] evidence(step4): A 桶修复进度（_route 范式，22 例归零） |
| 31 | `593adf67bf7c` | `b543636ba1fa` | 2026-09-12 12:48:23 +0800 | commit: [T-1788871227327-45c94bd8] docs: Step#4 未完成任务交接清单（W1-W11，含环境口径与速查） |
| 32 | `b543636ba1fa` | `ff9bf3acc4b9` | 2026-09-12 13:02:43 +0800 | commit: [T-1788871227327-45c94bd8] test(A桶): build读组转模块级 _route 契约 + CLI 080/090/093 对齐 GOV-FIX-07/08（8 失败→0） |
| 33 | `ff9bf3acc4b9` | `2f31dd4d60f4` | 2026-09-12 13:07:13 +0800 | commit: [T-1788871227327-45c94bd8] test(conftest): autouse 隔离 CALLWARDEN_DIR（manifest/registry 防共享写空 → E_HTTP_MANIFEST_STALE） |
| 34 | `2f31dd4d60f4` | `fb077404810e` | 2026-09-12 13:29:25 +0800 | commit: [T-1788871227327-45c94bd8] test(W3): 隔离 daemon harness 统一为模式A（USERPROFILE 重定向）——test_http_daemon_integration 从 300s 超时降为 17s 跑完 7/10 |
| 35 | `fb077404810e` | `b47f5f507718` | 2026-09-12 13:30:16 +0800 | commit: [T-1788871227327-45c94bd8] docs: 台账更新——W3 隔离 daemon harness 落地（300s→17s 7/10）+ task.create 双库 authority 分裂 evidence；W7 标记 test_http_daemon_integration 已处理 |
| 36 | `b47f5f507718` | `87b08e73b362` | 2026-09-12 13:49:56 +0800 | commit: [T-1788871227327-45c94bd8] test(方案A): 解决 task.create 双库 authority 分裂——test_http_daemon_integration 10/10 全绿（300s 超时→20s） |
| 37 | `87b08e73b362` | `ef309ac4abd4` | 2026-09-12 13:50:37 +0800 | commit: [T-1788871227327-45c94bd8] docs: 台账更新——方案A 解决 task.create 双库 authority 分裂（87b08e7），test_http_daemon_integration 10/10 |
| 38 | `ef309ac4abd4` | `820107ca2a95` | 2026-09-12 14:23:48 +0800 | commit: [T-1788871227327-45c94bd8] docs: W7 定界完——4 个超时文件均为环境性（W4 前 manifest 竞争 + W8 后台不稳），顺序运行全绿无需改码 |
| 39 | `820107ca2a95` | `1920f2ccfb75` | 2026-09-12 14:41:13 +0800 | commit: [T-1788871227327-45c94bd8] test(W3): 横向推广隔离 daemon harness 到 tools_query S2 组 8 文件 |
| 40 | `1920f2ccfb75` | `ad5e4b82fef4` | 2026-09-12 21:02:22 +0800 | commit: test(W3): 横向推广隔离 daemon harness——A/B 类迁移 + compat 家族 5 文件全绿 |
| 41 | `ad5e4b82fef4` | `bfc88313b566` | 2026-09-13 09:53:48 +0800 | commit: [T-1788871227327-45c94bd8] test(step4): 全量 sweep 收口与 acceptance 证据——116 测试补 stale 依据、28 个 rc≠0 全量定界、登记 C-10..C-12 |
| 42 | `bfc88313b566` | `e53de7a7bcfb` | 2026-09-13 10:00:47 +0800 | commit: [T-1788871227327-45c94bd8] docs(step5): parked 披露——acceptance ② 属 planner 计划缺陷 |
| 43 | `e53de7a7bcfb` | `b941bc188218` | 2026-09-13 10:02:41 +0800 | commit: [T-1788871227327-45c94bd8] chore(ledger): 台账登记 step5 parked 披露提交与 report 回执 |
| 44 | `b941bc188218` | `fd0798313318` | 2026-09-13 10:02:55 +0800 | commit: [T-1788871227327-45c94bd8] docs(step5): W16 补记 report 回执与派生 step#6（重复派工活锁） |
| 45 | `fd0798313318` | `18b00bacb740` | 2026-09-13 10:54:25 +0800 | commit: [T-1789274621921-e5464ad8] fix(metrics): 空 workspace 不再崩溃并统一 8 字段 legacy 契约（W12 承接） |
| 46 | `18b00bacb740` | `cb40f2809ce0` | 2026-09-13 10:58:55 +0800 | commit: [T-1789274621921-e5464ad8] chore(ledger): 台账登记 W12 承接卡提交 18b00ba 与 Executor->Reviewer 交接 |
| 47 | `cb40f2809ce0` | `a2c166efd159` | 2026-09-13 11:16:37 +0800 | commit: [T-1789274621921-e5464ad8] chore(ledger): 台账登记 W12 卡 Reviewer PASS(V-47b9fa19cfbd42932dc24bc6) 与 Reviewer->Adjudicator 交接 |
| 48 | `a2c166efd159` | `c66b54cd1ce6` | 2026-09-13 11:21:12 +0800 | commit: [T-1789274621921-e5464ad8] docs(backlog): 登记 W17 cw collab verdict 与 cw lease 传输面 authority 不一致缺陷 |
| 49 | `c66b54cd1ce6` | `f2e726fd4c32` | 2026-09-13 11:25:39 +0800 | commit: chore(ledger): T-1789274621921-e5464ad8 W12 adjudicator finalize -> COMPLETE |
| 50 | `f2e726fd4c32` | `4dfe9594975d` | 2026-09-13 11:26:09 +0800 | commit: docs(backlog): mark W12 承接卡 T-1789274621921-e5464ad8 as closed/COMPLETE |
| 51 | `4dfe9594975d` | `a2d559ea3c74` | 2026-09-13 12:09:09 +0800 | commit: [T-1789301330757-87f33c34] docs(w17): 建立 W17 承接卡合同与幂等建卡脚本 |
| 52 | `a2d559ea3c74` | `f0b726877b74` | 2026-09-13 12:24:24 +0800 | commit: [T-1789301330757-87f33c34] fix(collab): 将 cw collab 治理写命令面迁移到 HTTP authority |
| 53 | `f0b726877b74` | `25ee96b17359` | 2026-09-13 12:25:01 +0800 | commit: [T-1789301330757-87f33c34] chore(ledger): 记录 W17 承接卡 f0b7268 提交关联 |
| 54 | `25ee96b17359` | `cff5167658d7` | 2026-09-13 14:06:49 +0800 | commit: [T-1789301330757-87f33c34] chore(w17): 记录 W17 承接卡治理闭环终态与 adjudicator 收尾证据 |
| 55 | `cff5167658d7` | `d71d1175f772` | 2026-09-13 15:17:14 +0800 | commit: [T-1789290072972-5fad5b5c] fix(rust_ext): 修复 unix/Linux target 编译阻断 C-03（解除 WSL 共存契约阻断） |
| 56 | `d71d1175f772` | `7629ac9ac1ce` | 2026-09-13 15:18:35 +0800 | commit: [T-1789290072972-5fad5b5c] chore(ledger): 记录 C-03 承接卡治理闭环终态与 adjudicator 收尾证据 |
| 57 | `7629ac9ac1ce` | `b2c675288dde` | 2026-09-13 17:34:41 +0800 | commit: [T-1789290073049-6442e268] fix(cli): 修复 i18n 遮蔽与 RPC 契约缺陷 C-04..C-07（含 C-10/C-11/C-12） |
| 58 | `b2c675288dde` | `1bf10649b15a` | 2026-09-13 17:35:33 +0800 | commit: [T-1789290073049-6442e268] chore(ledger): 记录 C-04..C-07 承接卡治理闭环终态与 adjudicator 收尾证据 |
| 59 | `1bf10649b15a` | `740fed0fcfe9` | 2026-09-13 22:54:11 +0800 | commit: [T-1789290073113-6808b1ac] fix(server): 修复 ADMIN_ONLY_METHODS 授权清单与错误 code 契约 C-08/C-09 |
| 60 | `740fed0fcfe9` | `7122be2e0006` | 2026-09-13 22:54:49 +0800 | commit: [T-1789290073113-6808b1ac] chore(ledger): 记录 C-08..C-09 承接卡治理闭环终态与 adjudicator 收尾证据 |
| 61 | `7122be2e0006` | `7e48f8ea0653` | 2026-09-13 23:08:46 +0800 | commit: [T-1789340885170-02a8fe9c] chore(governance): C-13/C-14/C-15 剥离裁决落库并建 2 张承接卡 |
| 62 | `7e48f8ea0653` | `1afabc8e1939` | 2026-09-14 02:29:17 +0800 | commit: [T-1789340885170-02a8fe9c] fix(rust_ext): 修复 semgrep_handlers 未编译接线与 semgrep RPC 路由缺失 C-13 |
| 63 | `1afabc8e1939` | `29e0f99715d7` | 2026-09-14 02:33:27 +0800 | commit: [T-1789340885170-02a8fe9c] chore(governance): 收口 C-13 承接卡终审记录与台账回写 |
| 64 | `29e0f99715d7` | `158432be194c` | 2026-09-14 05:16:08 +0800 | commit: [T-1789340885245-071cb9b4] fix(rust_ext): 修复 daemon assignment create/revoke 契约一致性（assignment_id 单源）C-14/C-15 |
| 65 | `158432be194c` | `5509a4e63906` | 2026-09-14 05:17:44 +0800 | commit: [T-1789340885245-071cb9b4] chore(ledger): 记录 C-14..C-15 承接卡治理闭环终态与 adjudicator 收尾证据 |
| 66 | `5509a4e63906` | `78bb2af46e59` | 2026-09-14 05:17:49 +0800 | commit: [T-1789340885245-071cb9b4] chore(governance): 收口 C-14..C-15 承接卡终审记录与 backlog 回写 |
| 67 | `78bb2af46e59` | `b2056b48eb20` | 2026-09-14 06:00:21 +0800 | commit: [T-1788871227327-45c94bd8] chore(governance): 登记 C-16/C-17 出界 finding 与承接卡建卡回执 |
| 68 | `b2056b48eb20` | `5cfb1589f6df` | 2026-09-14 07:03:58 +0800 | commit: [T-1789365537146-bef4c2e4] fix(rust_ext): assignment_show 走权威 workspace resolver（C-16） |
| 69 | `5cfb1589f6df` | `480829e33ee8` | 2026-09-14 07:31:02 +0800 | commit: [T-1789365537146-bef4c2e4] chore(ledger): 登记 C-16 承接卡提交 5cfb158（assignment_show workspace 权威解析） |
| 70 | `480829e33ee8` | `ed84aa9e69ae` | 2026-09-14 07:31:10 +0800 | commit: [T-1789365537146-bef4c2e4] chore(governance): 收口 C-16 独立复核与终审记录，回写 backlog §W18/§W19 |
| 71 | `ed84aa9e69ae` | `186582ee2f18` | 2026-09-14 08:32:41 +0800 | commit: [T-1789365537230-c3f02eb4] fix(daemon): admin 路由块 workspace 改走 open_codegraph_db_write 权威解析（C-17） |
| 72 | `186582ee2f18` | `105a114b4bea` | 2026-09-14 08:58:08 +0800 | commit: [T-1789365537230-c3f02eb4] chore(ledger): 登记 C-17 承接卡提交 186582e（admin 路由块 workspace 权威解析） |
| 73 | `105a114b4bea` | `b6fbcc091ae8` | 2026-09-14 08:58:18 +0800 | commit: [T-1789365537230-c3f02eb4] chore(governance): 收口 C-17 独立复核与终审记录，backlog 回写 §W18/新增 §W20 |
| 74 | `b6fbcc091ae8` | `96fa721c34b2` | 2026-09-14 11:12:09 +0800 | (空) |
