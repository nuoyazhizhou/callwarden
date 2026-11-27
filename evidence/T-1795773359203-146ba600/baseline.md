# REQ-07 step① 基线固定（T-1795773359203-146ba600）

- 日期：2026-11-27
- 基线 commit：dfabf6d（master 链顶，本单开工前）
- 语料：r2_corpus_big（2530 个 .rs = src × 10 副本，与 R2 V3 轮同源；victim 文件 cas.rs / canonicalize.rs 每轮前重置）
- 协议：与 R2 V3 相同——`cargo test --release --lib r2_full_build_probe -- --ignored --nocapture`，
  `R2_PROBE_PHASE=full`（冷库全量 build + 删 2 文件触发删除感知 + 二次 build），
  外加本单新增的 phase-2 专项计时（单线程重建分片 → 独立空库单独跑 import_shards，冷库语义）。

## 实测数据（三轮）

| 轮 | 端到端 FIRST_BUILD | phase-2（import_shards） | SECOND_BUILD | PEAK_PRIVATE_MB | 统计 |
|---|---|---|---|---|---|
| 1 | 288.06s | 69.36s | 1.94s | 597 | symbols=61369 calls=714989 resolved=77451 |
| 2 | 220.13s | 56.36s | 2.01s | 598 | 同上 |
| **3（基线）** | **165.00s** | **43.19s** | **1.72s** | **598** | 同上 |

统计三轮完全一致（61369/714989/77451/3780），差异纯耗时。

## 取值依据

- 轮 3 与 R2 V3 轮（fd9fefe，同语料同协议）的 159.498924s 相差 3.4%——正常波动区间，
  判定为无后台干扰轮；轮 1/2 受机器后台负载拖累（288→220→165 单调收敛，与后台任务退出时间吻合）。
- **基线固定为轮 3：端到端 165.00s、phase-2 43.19s**。后续优化后对比同协议、同语料、
  同取值规则（多轮取无干扰轮）。

## 占比结构（对验收指标的先行校验）

- phase-2 占端到端 43.19/165.00 = **26.2%**（与卡面背景「DB 写占 ~75%」的 Amdahl 反推不一致——
  该 75% 是由 1.26x 实测反推的理论值，phase-2 专项实测给出直接证据：DB 写主导的导入只占 ~1/4）。
- 含义：端到端 ≥1.2x（165→≤137.5s，需降 ≥27.5s）要求 phase-2 降幅 ≥64%
  （27.5/43.19），远高于结构指标 25%。prepare_cached + 批量写入能否达到，
  以 step② 后的中间实测为准；若达不到，带本数据向 planner 反馈裁决。

## 尺子说明（probe 扩展，产品代码零改动）

`r2_full_build_probe` 在 full 协议末尾新增 phase-2 专项（R2_PROBE_PHASE=full 时自动执行）：
1. 单线程复刻阶段一（scan_files → 逐文件 sha256/行数/mtime → parse_file_to_events(prev=None)
   → ShardWriter 写单分片，不碰 DB）；
2. 独立空库（`{R2_PROBE_DB}.phase2.db`，先删后建）单独跑 import_shards，
   输出 `R2_PROBE_PHASE2_SECONDS` / `R2_PROBE_PHASE2_RESULT`；
3. 跑完删分片。与端到端库解耦（同库会带 DELETE 旧行开销，冷库语义失真）。

## 复现命令

```
cd /c/git_work/callwarden/rust_ext
R2_PROBE_ROOT=C:/Users/wanpi/AppData/Local/Temp/r2_corpus_big/rust_ext \
R2_PROBE_DB=C:/Users/wanpi/AppData/Local/Temp/r7_baseline.db \
R2_PROBE_PHASE=full cargo test --release --lib r2_full_build_probe -- --ignored --nocapture
```

输出关键字段：`R2_PROBE_FIRST_BUILD_SECONDS`（端到端）、`R2_PROBE_PHASE2_SECONDS`（phase-2）。
