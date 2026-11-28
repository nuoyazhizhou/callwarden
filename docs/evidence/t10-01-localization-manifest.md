# T10-01 定位取证 Manifest — 全量 `cargo test --lib` 间歇病态（栈溢出/内存膨胀）

## 1 头部

| 项 | 值 |
|---|---|
| 任务 | T-1795847624350-48202e98（批次 3，REQ-GAP，稳定性缺陷定位） |
| 步骤 | S-1795847624369-494b72b4（step 0，action=test） |
| 执行角色 | tester 纪有据（agent_id=tester-t10-01-k7m2p，独立身份注册，与 executor T8 线不共工作树） |
| 工作树 | preq-t10-rerun-j2k7d（detached @ 78d7ddc，与 T10 复跑同一独立树） |
| 判定 | **定位完成（步骤①②③全部执行），病灶未定位到代码层——结论：19–22GB 无界分配不可能出自测试逻辑，病灶在逻辑之下的 OS/环境交互层，需挂起时栈捕获（超 tester 工具能力），交 executor 按 step 1（fix）处置** |
| 时间 | 2026-11-28 14:37–15:20（本地时间） |

## 2 环境指纹

| 项 | 值 |
|---|---|
| OS | Windows（LINKPLAY-SCM），git-bash |
| Rust | cargo（MSVC 工具链，PATH 剥离 /usr/bin 避免 link.exe 遮蔽） |
| 仓库 HEAD | 78d7ddc（preq-t10-rerun-j2k7d，detached） |
| 隔离 | CARGO_TARGET_DIR=%TEMP%/cw_t10_target（与主工作区隔离） |
| 看门狗 | Python runner：15s 采样 `tasklist /FO CSV /NH`（GBK decode），阈值 8GB（8388608KB），超限 `taskkill /PID <cargo_pid> /F /T` |
| daemon | PID 5360，https://127.0.0.1:6374（mTLS），lease L-c5f082d43854d97d（fencing_counter=1） |

## 3 命令原文

步骤①（单线程锁定首挂，带看门狗，8GB 阈值）：

```
cargo test snapshot_state --lib -- --test-threads=1
```

（经 `cw_t10_01_singlethread_runner.py` 承载：Popen 输出重定向至日志，15s 采样 RSS，超 8GB 杀进程树）

步骤②（首挂测试单独跑，最小复现确认，看门狗同上）：

```
cargo test test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot --lib -- --exact
```

步骤②对照（邻居测试单独跑）：

```
cargo test test_query_call_chain_down_requires_qualified_name --lib -- --exact
```

步骤③（只读走读，未运行任何写操作）：

```
# 走读文件（只读）：
#   rust_ext/src/daemon/snapshot_state.rs（make_state:8760 / handle_workspace_register:889 /
#     open_query_connection:262 / handle_query_call_chain_down:2522 / 测试 #12:9866、#10:9921、#11:9941 /
#     register_workspace_for_test:8771）
#   rust_ext/src/daemon/workspace.rs（validate_owned_path_any:1393 / owned_workspace:1782 /
#     handle_workspace_register_preferred:2273）
#   rust_ext/src/daemon/snapshot.rs（SnapshotCache::get:414）
#   rust_ext/src/daemon/dispatch.rs（dispatch:2427）
```

## 4 统计

### 步骤①：单线程定位

- snapshot_state 模块共 **62 个测试**（`--list` 字母序，见 t10-01-snapshot-state-list.txt）
- 单线程跑：**11 个 ok 后，第 12 个测试开跑即 cargo rc=4294967295**（非看门狗击杀，killed=False，峰值 1.69GB——进程在 RSS 尚低时即死于 rc=4294967295，与 T10 全量 attempt1 的栈溢出症状同型）
- 首挂测试（第 12 个，字母序）：`daemon::snapshot_state::tests::test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot`

### 步骤②：最小复现集与确定性

| 轮次 | 结果 | RSS 峰值 | 耗时 | 备注 |
|---|---|---|---|---|
| solo run1 | 挂起→看门狗击杀 | 20121728KB（**19.2GB**） | 17s（1795849950→1795849967） | 测试行未及打印即爆炸 |
| solo run2 | 挂起→看门狗击杀 | 23448936KB（**22.4GB**） | ~84s | **测试行已打印**（确认挂测试内部） |
| 邻居对照（test_query_call_chain_down_requires_qualified_name） | **PASS** | 1809124KB（1.8GB，正常） | 109.79s | 同一测试类、相邻测试，单独跑通过 |

最小复现集 = **该单测试**，确定性 **2/2**（两次独立 solo 均复现 19–22GB 级爆炸）。

### 步骤③：走读结论

注册→查询全路径逐段核过，**均有界、无循环等待**：

| 环节 | 位置 | 结论 |
|---|---|---|
| make_state | snapshot_state.rs:8760 | 内存注册表 + tempdir，无无界分配 |
| register 路径校验 | workspace.rs:1393 validate_owned_path_any | canonicalize + metadata + check_path_owner，有界 |
| owned_workspace | workspace.rs:1782 | 纯注册表查找，有界 |
| workspace.register | snapshot_state.rs:889 → workspace.rs:2273 | 有界 |
| 查询 | snapshot_state.rs:2522 handle_query_call_chain_down | 有界 |
| SnapshotCache::get | snapshot.rs:414 | 平凡 RwLock 读 |
| dispatch | dispatch.rs:2427 | 全测试共用，已排除（其他 61 测试同路径正常） |

## 5 证据清单（11 份，sha256）

| 文件 | sha256 | 大小 |
|---|---|---|
| t10-01-snapshot-state-list.txt | 546aa75f3c77de4b12456053ed04a88e0097096d2b4fe5aef33b8c207e7d47f0 | 48053 |
| t10-01-singlethread.log | 8036bb393bf098f0cd6c532e980904598939f257d032e217b54800d931559c99 | 43789 |
| t10-01-singlethread-watchdog.log | 7cfeedcba7cdba0466b0479499cb24c44332fe7781e2be5c4637f874d8f86440 | 2746 |
| t10-01-firsthang-solo-run1.log | 51318617e0c14a7eac03fefefc9f6c22fe944cbae0afb79493f0eeedd2aafde3 | 42347 |
| t10-01-firsthang-solo-run1-watchdog.log | d9075f27868f3b04712acfff5a4ec01ff3c76a62e1d6187599e6deba56e264df | 232 |
| t10-01-firsthang-solo-run2.log | da61c91778427ecdfd57c4ef8b4bc28854dfe91f5be8f5a21a2fb6309f06503f | 42734 |
| t10-01-firsthang-solo-run2-watchdog.log | 9c1b358480d80cfab3dfbdcb5f2c0afb878679b22281b8775db20e96eef11d9a | 392 |
| t10-01-contrast-neighbor-solo.log | 9459cb35997020bb634576893e0f43acf922ada69bef9086d588884b30bd9d84 | 42668 |
| t10-01-contrast-neighbor-solo-watchdog.log | 12588b21a46c112273d0a395b5f2c7ab7fcb8a5fc6a18cd7d9351c8a558fb8cc | 327 |
| t10-01-firsthang-solo-oom.log | d33ab43cf99c6498b8c7b404166633f13b4d05078ca1c690960728bc2463f856 | 14302 |
| t10-01-orphan-event.md | b3003039783ac2cf4cc6910ca9709ba9520f72e4935f7960a57ace3b06d64e94 | 1088 |

全部位于 `docs/evidence/`。

## 6 关键输出

**步骤①首挂证据**（t10-01-singlethread.log 尾部）：

```
test daemon::snapshot_state::tests::test_query_call_chain_down_requires_qualified_name ... ok
test daemon::snapshot_state::tests::test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot ... CARGO_RC=4294967295
=== end epoch=1795848887 killed=False peak_test_binary_kb=1691432 ===
```

**步骤② solo run1 看门狗**（17s 内 43MB→19.2GB）：

```
1795849950 test_binary_total_kb=43020
1795849967 test_binary_total_kb=20121728
1795849967 WATCHDOG_KILLED threshold_exceeded kb=20121728
```

**步骤② solo run2**（测试行已打印，确认挂测试内部，22.4GB）：

```
test daemon::snapshot_state::tests::test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot ... CARGO_RC=WATCHDOG_KILLED_RSS_OVER_8GB peak_kb=23448936
```

**邻居对照**（PASS 但异常慢）：

```
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 2163 filtered out; finished in 109.79s
CARGO_RC=0
```

## 7 判读

### 7.1 三步结论

- **步骤①**：首挂测试锁定为 `daemon::snapshot_state::tests::test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot`（单线程 11 ok 后第 12 个开跑即 rc=4294967295）。
- **步骤②**：最小复现集 = 该单测试，确定性 2/2（19.2GB / 22.4GB）；邻居测试对照 PASS 但耗时 109.79s（同数量级测试通常 <1s），**异常慢百倍佐证环境交互存在**。
- **步骤③**：注册→查询全路径走读均有界，**19–22GB 无界分配不可能出自测试逻辑**。

### 7.2 病灶层次判断（如实边界）

病灶不在代码逻辑层，在逻辑之下的 **OS/环境交互层**。候选方向（供 executor step 1 参考）：

- tempdir 生命周期 × ACL/canonicalize 交互（register_workspace_for_test 的 tempdir 在函数末清理，若 validate/register 期间路径悬空，Windows 层行为可能与 Unix 不同）
- 分配器 / AV 实时扫描交互（17–22GB 级瞬间膨胀更像分配器或系统层病态，非测试代码可达）

**tester 工具能力边界**：需要挂起时栈捕获（procdump / cdb attach）才能定位到具体分配点，此超出本角色工具能力，未执行。executor 修复时建议先挂起抓栈再改代码。

### 7.3 来源标注（评审 finding 勘误）

`prerequisite-rerun-t6-full-lib-attempt2.log` 中记录的 **14.1GB 终止**：**来源为 tester 外部监控**（任务管理器观察），非看门狗 runner 采样值——attempt2 时 runner v1 因 GBK 解码崩溃、cargo 成孤儿进程，看门狗数据不可用；14.1GB 为外部观察值，特此标注。看门狗采样的权威数值为 attempt3 的 18.6GB 与本文档步骤②的 19.2GB / 22.4GB。

### 7.4 过程事件（不影响结论）

- 第一次 solo run 被 SIGTERM 后测试二进制曾成孤儿进程并膨胀至 17.49GB（机器内存 96%），已 taskkill 清理；证据见 t10-01-orphan-event.md。此后所有 solo 跑一律走看门狗 runner。
- 邻居 solo 对照跑之前曾发生一次 rustc OOM（"memory allocation of 49696512 bytes failed"，孤儿进程占用所致），清理后 redo 成功；证据见 t10-01-firsthang-solo-oom.log。

## 8 附件指针

- 本 manifest：`docs/evidence/t10-01-localization-manifest.md`
- 全量复跑上下文（T10 manifest）：`docs/evidence/prerequisite_rerun_manifest.md`
- 治理轨迹：task.report 事件（daemon RPC，request_id 见治理回报）
