# F3 整改证据（T-1795744094756-6b7ea004）

- 关联卡：T-1795744094756-6b7ea004（parent=根修卡 T-1795507699058-41d9fcbc Step 3）
- finding：F3 🟡 需整改 webui role=webui 生产注册未落地
- executor：executor-roofix-alm01 / sess-executor-roofix-alm01-20261125

## 改动与提交

1. step#0 `server/web_gateway.py:main` — commit 5097239（+41/-6）。
   main 在 uvicorn.run 前 register_process(role=webui, pid=os.getpid(),
   endpoint_or_bind=host:port, authority 取自 config)，register_process 内部写
   pid + process_start_time 双键防 PID 复用；finally 中 unregister_process 注销
   （含 KeyboardInterrupt / uvicorn 异常），崩溃残留交 reconcile_stale 标 stale；
   注册失败 fail-open 不阻断观测面但留痕。复用 host_process_registry 既有 API，
   未绕开锁与原子写。承担组件明确为 web_gateway 自身（生产启动钩子）。
2. step#1 `tests/test_host_process_registry.py:webui_registration_integration`
   — commit aacc4e6（+64）。走真实 web_gateway.main，mock uvicorn.run 做运行期
   探针，注册表隔离 tmp_path。

## 实证

- 生产路径临时脚本：uvicorn.run 运行期注册表可见 role=webui 条目
  （pid + process_start_time + endpoint_or_bind=127.0.0.1:18765），main 返回后清空。
- 集成测试三条断言全过：运行期可见 webui 双键条目 / 退出后注销 /
  daemon 重启（reconcile_stale）不误清 webui 条目。
- 全量回归：56 passed / 9 skipped（基线 55 + 新增 1，零破坏）；编译/import OK。

## check items

- 启动钩子注册 role=webui（双键）+ 正常退出注销：满足（step#0）。
- 承担组件书面指定（web_gateway.main 自身）并留痕：满足。
- 集成测试（生产路径可见/退出注销/daemon 重启不误清）：满足（step#1）。

## F7 更正说明（2026-11-28，T-1795831521518-0c880ea4）

F3 step#0 的 commit message（5097239）与 web_gateway.py 原 :403 注释曾称「崩溃残留
由权威 daemon 的 reconcile_stale 周期清理」——**该表述不准确**：F3 交付时
`reconcile_stale` 仅有测试调用、无任何生产调用方，不存在周期清理任务。commit
message 属不可变历史、不改写，此处留痕更正。

实际生产调用方（F7 修复后）：`daemon_autostart.spawn_http_daemon` 在 host 临界区内、
registry upsert 之前调 `reconcile_stale(authority_id)` 一次（启动清理），覆盖
daemon 重启路径；长效运行永不重启场景的残留清理列为 open 项交 reviewer 裁决。
web_gateway.py 的 :402-403 注释已在 F7 step#1 同步更正为上述准确表述。
