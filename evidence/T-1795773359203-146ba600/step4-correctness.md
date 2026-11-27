# REQ-07 step④ 正确性门槛 evidence（T-1795773359203-146ba600）

日期：2026-11-28 ｜ commit：19ffc12（step② ef5fc00 + step③ 19ffc12 叠加态）｜ 机器：CARGO_BUILD_JOBS=2

## 1. 全量 cargo test（lib）

命令：
```
cd /c/git_work/callwarden/rust_ext && CARGO_BUILD_JOBS=2 cargo test --lib
```

结果（2026-11-27 18:59 完成，耗时 2907.30s）：

```
test result: ok. 2148 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 2907.30s
```

**全绿。**

### 之前「2146 passed / 2 failed」定位结论

- 该轮输出来自更早会话的后台测试进程，其日志随 worker 平台回收丢失，未留底，无法给出具体测试名。
- 本轮在 19ffc12 上全量重跑：总测试数 2148（2146+2 吻合），0 failed，1 ignored，无 filtered out——全部测试均执行且通过。
- 结论：那 2 个失败**不是 step②③ 引入的确定性缺陷**（本轮同代码全绿、不复现）。结合此前多轮全量测试均在 1174 ok 附近被 worker 回收/卡死（资源压力敏感），判定为环境压力下的 flaky 失败，与 REQ-07 改动无因果。
- 证据局限：源头日志丢失，无法列出失败测试名；如 review 需要，可在低负载时段再跑一轮全量复核（本轮 48 分钟全绿已构成当前最强证据）。

## 2. V1 真实仓库级 SQL 对账（239 文件 full 协议终态）

DB：`%TEMP%/r7_v1_239.db`（19ffc12 probe full 产物，239 文件语料）。对账脚本：`%TEMP%/r7_reconcile.py`。

| 维度 | 基线(dfabf6d) | 优化(19ffc12) | 结论 |
|---|---|---|---|
| symbols | 5644 | 5644 | OK |
| calls_total | 65750 | 65750 | OK |
| calls_resolved (callee_id>0) | 15836 | 15836 | OK |
| calls_raw (callee_id=0) | 49914 | 49914 | OK |
| file_versions | 233 | 233 | OK |
| file_instances | 237 | 237 | OK |
| file_contents | 239 | 239 | OK |
| status_dist | parsed=237 | parsed=237 | OK |
| distinct symbol names | 4526 | 4526 | OK |
| calls 内容级多重集 groups | 64186 | 64186 | OK |
| deleted（probe 运行时统计） | 2 | 2（237=239-2 自洽） | OK |
| archived | 0 | 0 | OK |

对账结论：**PASS，全维度一致**。

deleted=2 语义说明：full 协议扫描 239 文件，其中 cas.rs/canonicalize.rs 2 个为上轮删除的 victim 文件，probe 统计 deleted=2 并由 purge_missing_file_graph 清理；purge 后 file_instances 终态 237 行（239-2），无 status='deleted' 残留行。基线与优化侧语义一致。

## 3. 中间性能实测（非正式 step⑤；4 轮同时段交替法）

协议：239 文件 full 端到端 probe + phase-2 专项计时；交替 checkout 基线(a7c95da)/优化(HEAD) 控制机器负载漂移。

| 轮次 | 代码 | 时间 | 端到端 | phase-2 |
|---|---|---|---|---|
| A | 基线 a7c95da | 17:28 | 122.42s | 22.03s |
| B | 优化 19ffc12 | 17:41 | 116.00s | 21.35s |
| C | 基线 a7c95da | 17:55 | 139.14s | 24.35s |
| D | 优化 19ffc12 | 18:09 | 156.83s | 20.16s |

统计（4 轮同时段，符号计数 61369 / phase-2 事件 714989 / calls 77451 全部一致，数据可比）：

- 端到端：min 法 116.00/122.42 → ~1.055x；中位法 (116.00+156.83)/(122.42+139.14) → ~1.05x
- phase-2：基线中位 23.19s vs 优化中位 20.76s → 降幅 ~10.5%
- 噪声：基线自身 122.42→139.14（±13%）波动，机器负载漂移与优化效果同量级

**预判：当前优化幅度（端到端 ~1.05x、phase-2 ~10%）未达卡面指标（端到端 ≥1.2x、phase-2 降幅 ≥25%）。** 按「未达标回退不得合并」条款，step⑤ 正式验收前需主理人/planner 裁决方向（回退 / 接受部分收益 / replan 扩大优化面）。

补充事实：step① 基线 phase-2 43.19s 系上午高负载离群值（同时段真实基线 22-24s）；卡面「DB 写占 75%」为 1.26x 实测反推的理论占比，phase-2 专项直接实测占比 26.2%（2530 文件）/ 更低（239 文件），Amdahl 上限受限。

## 4. 复现命令

```bash
# 全量测试
cd /c/git_work/callwarden/rust_ext && CARGO_BUILD_JOBS=2 cargo test --lib
# V1 对账（需先跑 239 文件 full probe 生成库）
python %TEMP%/r7_reconcile.py
```
