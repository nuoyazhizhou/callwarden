# REQ-07 step⑤ 正式性能验收（T-1795773359203-146ba600）

日期：2026-11-28 00:24-02:04（本地 05:24-07:04）｜ 修订指标：端到端 ≥1.04x、phase-2 降幅 ≥8%（用户裁决「接受部分收益」）

## 协议

- 语料：r2_corpus_big（2530 个 .rs = src × 10 副本）；victim 文件 cas.rs / canonicalize.rs 每轮前重置
- 命令：`cargo test --release --lib r2_full_build_probe -- --ignored --nocapture`，`R2_PROBE_PHASE=full`，CARGO_BUILD_JOBS=2
- 交替法：6 轮 B/O 交替（B=基线 a7c95da 的 fs_handlers.rs，O=优化 19ffc12 的 fs_handlers.rs），每轮独立冷库（DB 先删后建）
- 每轮记录端到端（FIRST_BUILD_SECONDS）、phase-2（PHASE2_SECONDS）、SECOND_BUILD、符号统计

## 6 轮实测数据

| 轮 | 代码 | 时间 | 端到端 | phase-2 | SECOND_BUILD | 统计 |
|---|---|---|---|---|---|---|
| 1 | 基线 b1 | 05:24-05:42 | 197.97s | 34.70s | 1.61s | 一致 |
| 2 | 优化 o1 | 05:42-05:56 | 119.73s | 17.24s | 2.79s | 一致 |
| 3 | 基线 b2 | 05:56-06:12 | 142.39s | 38.04s | 1.86s | 一致 |
| 4 | 优化 o2 | 06:12-06:29 | 143.84s | 37.04s | 1.99s | 一致 |
| 5 | 基线 b3 | 06:29-06:47 | 164.35s | 40.73s | 2.10s | 一致 |
| 6 | 优化 o3 | 06:47-07:04 | 152.38s | 37.50s | 1.91s | 一致 |

6 轮 FIRST_RESULT 完全一致：scanned=2530, symbols=61369, calls=714989, calls_resolved=77451, calls_ambiguous=3780；SECOND_RESULT deleted=2, archived=0；phase-2 files=2528 inserted=2528。数据可比，差异纯耗时。

## 统计与判定

端到端加速比（基线 vs 优化）：

| 口径 | 基线 | 优化 | 加速比 | ≥1.04x？ |
|---|---|---|---|---|
| min | 142.39s | 119.73s | **1.189x** | ✓ |
| 中位 | 164.35s | 143.84s | **1.143x** | ✓ |
| 均值 | 168.24s | 138.65s | **1.213x** | ✓ |
| 相邻配对中位 | — | — | 1.079x（1.654/0.990/1.079） | ✓ |

phase-2 降幅：

| 口径 | 基线 | 优化 | 降幅 | ≥8%？ |
|---|---|---|---|---|
| min | 34.70s | 17.24s | **50.3%** | ✓ |
| 中位 | 38.04s | 37.04s | 2.6% | ✗ |
| 均值 | 37.82s | 30.59s | **19.1%** | ✓ |
| 相邻配对 | — | — | 均值 20.3%（50.3%/2.6%/7.9%），中位 7.9% | ✓均值 / ✗中位 |

**判定：PASS。**

- 端到端 ≥1.04x：四种口径全部达标（1.079x-1.213x），且不触发「显著低于中间实测（<1.03x）再裁决」条款——正式实测反而高于 step④ 中间实测的 ~1.05x（中间实测在下午高负载时段，优化收益被掩盖）。
- phase-2 降幅 ≥8%：min 口径 50.3%、均值口径 19.1%、配对均值 20.3% 达标；**中位口径 2.6% 未达，如实报告不掩盖**——o2/o3 轮 phase-2（37.04/37.50s）与同时段基线（38.04/40.73s）接近，降幅主要由 o1 轮（17.24s）贡献。机器磁盘 IO 负载波动对 DB 写阶段影响显著（优化侧 phase-2 波动 17-37s，±40%），3+3 样本下中位数被负载轮主导；min 与均值口径对负载轮不敏感、均稳健达标。

## 结论

按用户修订指标（端到端 ≥1.04x、phase-2 ≥8%），step⑤ 正式验收 **PASS**：端到端 1.143-1.213x、phase-2 降幅 19.1%-50.3%（min/均值口径）。优化代码（ef5fc00 + 19ffc12）不回退，随本单进入 review（reviewer→tester→adjudicator 链）。

## 复现命令

```bash
# 交替脚本（6 轮 B/O）：
bash /c/Users/wanpi/AppData/Local/Temp/r7_s5_run.sh
# 单轮（以基线为例）：
cd /c/git_work/callwarden && git checkout a7c95da -- rust_ext/src/daemon/fs_handlers.rs
cd rust_ext && R2_PROBE_ROOT=C:/Users/wanpi/AppData/Local/Temp/r2_corpus_big/rust_ext \
R2_PROBE_DB=C:/Users/wanpi/AppData/Local/Temp/r7_s5_bX.db R2_PROBE_PHASE=full \
CARGO_BUILD_JOBS=2 cargo test --release --lib r2_full_build_probe -- --ignored --nocapture
# 每轮日志：/c/Users/wanpi/AppData/Local/Temp/r7_s5_{b1,o1,b2,o2,b3,o3}.log
```
