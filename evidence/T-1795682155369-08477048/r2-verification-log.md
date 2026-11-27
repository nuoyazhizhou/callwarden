# R2 取证日志（T-1795750243195-f6f69ba4）— V1/V3/V4/V5

executor: callwarden-alm-executor-bg2026
基线更正：任务单 (b) 写「4e3f09e~1（f0d9c73）」，实际 git 历史 4e3f09e 的 parent 是
9d58ec7（流水线前实现），f0d9c73 是 4e3f09e 的子提交（含流水线，不能作旧基线）。
本取证按字面 4e3f09e~1 = 9d58ec7 作旧基线；新基线 = HEAD fd9fefe（含 R1/R3/R4/R5 全部整改）。

## (a) 真实语料双路径等价对账单测 — step #0

新增 `test_equivalence_real_repo_sources_old_vs_new`（fs_handlers.rs 测试区）：
- 语料：本仓库真实源文件 include_str! 编译期读入（storage.rs / daemon/fs_handlers.rs /
  daemon/build_journal.rs），覆盖多符号（163）/嵌套 mod tests/跨文件调用。
- 旧路径 parse_and_store_symbols vs 新路径 parse_file_to_events + commit_batch，
  对账维度 symbols/calls(resolved+raw)/file_versions/file_instances status。
- 空符号场景：build_journal.rs 先有符号（旧路径建版本），置空后分别走旧/新路径，
  断言旧行保留一致。

### 验证

```
cargo test --lib test_equivalence_real_repo_sources_old_vs_new -- --nocapture
→ test result: ok. 1 passed; 0 failed (2.71s)
R2_EQUIV_REAL_OLD=(symbols,calls,resolved,raw,versions,status)=(163, 2323, 393, 1930, 3, [("parsed", 3)])
R2_EQUIV_REAL_NEW=(symbols,calls,resolved,raw,versions,status)=(163, 2323, 393, 1930, 3, [("parsed", 3)])
R2_EQUIV_REAL_EMPTY_OLD=(163, 2323, 393, 1930, 3, [("parsed", 3)])
R2_EQUIV_REAL_EMPTY_NEW=(163, 2323, 393, 1930, 3, [("parsed", 3)])
```

结论：旧/新路径在真实语料上逐维度完全一致；空符号场景旧行保留一致（F1 语义验证）。

## (b) 真实仓库级旧/新全量 build SQL 对账 — step #1

基线（更正说明见文首）：
- 旧：4e3f09e~1 = 9d58ec7（流水线前实现），git worktree 挂于 C:/git_work/callwarden-r2-old
- 新：HEAD fd9fefe（主工作树，含 R1/R3/R4/R5 全部整改）
- 语料：r2_corpus（rust_ext 源码副本，239 个 .rs，两基线扫同一份）
- probe：#[ignore] r2_full_build_probe（环境变量 R2_PROBE_ROOT/DB/PHASE），
  full 阶段 = 全量 build + 删 cas.rs/canonicalize.rs + 二次 build（删除感知）

### 验证（命令原文 + 关键输出）

```
# 旧基线
cd C:/git_work/callwarden-r2-old/rust_ext && R2_PROBE_ROOT=...r2_corpus/rust_ext R2_PROBE_DB=r2_old.db R2_PROBE_PHASE=full cargo test --lib r2_full_build_probe -- --ignored --nocapture
R2_PROBE_FIRST_BUILD_SECONDS=42.1341133
R2_PROBE_FIRST_RESULT={"ok":true,"scanned":239,"inserted":239,"deleted":0,"archived":0,"symbols":5715,"calls":66115,"calls_resolved":15955,"calls_ambiguous":3837}
R2_PROBE_SECOND_RESULT={"ok":true,"scanned":237,"unchanged":237,"deleted":2,"archived":0,"calls_resolved":5,"calls_ambiguous":52}

# 新基线
cd C:/git_work/callwarden/rust_ext && R2_PROBE_ROOT=...r2_corpus/rust_ext R2_PROBE_DB=r2_new.db R2_PROBE_PHASE=full cargo test --lib r2_full_build_probe -- --ignored --nocapture
R2_PROBE_FIRST_BUILD_SECONDS=37.092168
R2_PROBE_FIRST_RESULT={"ok":true,"scanned":239,"inserted":239,"deleted":0,"archived":0,"symbols":5715,"calls":66115,"calls_resolved":15955,"calls_ambiguous":3837}
R2_PROBE_SECOND_RESULT={"ok":true,"scanned":237,"unchanged":237,"deleted":2,"archived":0,"calls_resolved":5,"calls_ambiguous":52}

# SQL 对账（python r2_reconcile.py，两库终态）
== R2 (b) 真实仓库级旧/新全量 build SQL 对账 ==
  [OK] symbols: old=5644 new=5644
  [OK] calls_total: old=65750 new=65750
  [OK] calls_resolved: old=15836 new=15836
  [OK] calls_raw: old=49914 new=49914
  [OK] file_versions: old=233 new=233
  [OK] file_instances: old=237 new=237
  [OK] file_contents: old=239 new=239
  [OK] status_dist: old=[('parsed', 237)] new=[('parsed', 237)]
  [OK] symbol name->count map identical: 4526 distinct names
  [OK] parsed rel_path set: 237 files
== 对账结论: PASS（全维度一致） ==
```

结论：真实仓库级旧/新全量 build 后 SQL 全维度一致（含删除感知终态：deleted=2 双方
一致、archived=0；5644 符号 / 65750 调用边 / 15836 resolved / 4526 distinct names /
237 parsed paths 逐一相同）。V1 通过。

## (c) 全量 build 性能 / 内存 / 分片全流程 — step #2

release 模式（cargo test --release），冷库全量，同语料两基线对比：

### 239 文件（rust_ext 单份）
```
旧 9d58ec7:  R2_PROBE_FIRST_BUILD_SECONDS=33.4983451  symbols=5715 calls=66115
新 fd9fefe:  R2_PROBE_FIRST_BUILD_SECONDS=29.2292062  symbols=5715 calls=66115（统计一致）
加速比 = 33.50/29.23 = 1.15x
R2_PROBE_BUILD_TMP_JSONL_LEFTOVER=0（分片生成→导入→删除全流程收口）
R2_PROBE_PEAK_PRIVATE_MB=25
```

### 2530 文件（10 倍语料，src 复制 10 份）
```
旧 9d58ec7:  R2_PROBE_FIRST_BUILD_SECONDS=201.6907511  symbols=61369 calls=714989 resolved=77451
新 fd9fefe:  R2_PROBE_FIRST_BUILD_SECONDS=159.498924  symbols=61369 calls=714989 resolved=77451（统计一致）
加速比 = 201.69/159.50 = 1.26x
R2_PROBE_BUILD_TMP_JSONL_LEFTOVER=0
R2_PROBE_PEAK_PRIVATE_MB=160
```

### V3 结论：≥2.5x 未达成（如实报告）

- 实测加速比：239 文件 1.15x / 2530 文件 1.26x，随规模略升但远低于 plan 目标 2.5x。
- 瓶颈分析：新实现流水线并行化的是「解析」阶段；DB 写入（SQLite 单写者、
  commit_batch 分批事务）新旧同为串行，占全量 build ~75% 时间——解析并行收益
  被 DB 写入稀释（2530 文件下解析约占 25%，4 worker 并行 → 理论 1.23x，与实测 1.26x 吻合）。
- 功能性收益已达成：分片落盘（崩溃恢复）+ 幂等重放 + 导入对账（F3）均为新能力。
- 性能目标 ≥2.5x 需 planner/reviewer 裁决：接受当前 1.26x 或 replan 后续优化
  DB 写入路径（批量 upsert / 预编译语句 / WAL 调优等方向，不在本单 scope）。

### V4 内存
- 239 文件：私有内存峰值 25MB；2530 文件：160MB（进程内 50ms 采样
  GetProcessMemoryInfo PrivateUsage，覆盖 ParseJob 队列 256 + 事件组驻留）。

## (d) 导入中途 kill → 重跑 → SQL 对账 — step #3

### 流程
1. 干净对照：r2_corpus3（230 个 .rs）first 阶段全量 build → r2_kill_ref.db
   （17.12s，5579 符号 / 64999 calls）
2. kill 场景：python r2_kill.py 启动 probe（同语料 → r2_kill.db），轮询库
   file_instances 行数，**导入中途（70/230 行）taskkill /F 测试进程**（cargo exit 1）
3. 重跑：同库同语料 probe first → inserted=230 完成（test ok）
4. 对账：r2_kill.db vs r2_kill_ref.db

### 验证（命令原文 + 关键输出）

```
python C:/Users/wanpi/AppData/Local/Temp/r2_kill.py
→ CARGO_PID=13804
→ KILL_AT_ROWS=70 TESTPID=10332
→ CARGO_EXIT=1
→ KILLED_MID_IMPORT rows=70

# 重跑（同库）
R2_PROBE_ROOT=...r2_corpus3/rust_ext R2_PROBE_DB=r2_kill.db R2_PROBE_PHASE=first \
  R2_PROBE_STRICT_SHARDS=0 cargo test --release --lib r2_full_build_probe -- --ignored --nocapture
→ R2_PROBE_FIRST_RESULT={"ok":true,"scanned":230,"inserted":230,"symbols":5579,"calls":64999,"calls_resolved":15579,"calls_ambiguous":3780}
→ R2_PROBE_BUILD_TMP_JSONL_LEFTOVER=4（旧 runid 孤儿分片，见下）
→ test result: ok. 1 passed

python C:/Users/wanpi/AppData/Local/Temp/r2_kill_reconcile.py
  [OK] symbols: kill_replay=5579 clean_ref=5579
  [OK] calls_total: kill_replay=64999 clean_ref=64999
  [OK] calls_resolved: kill_replay=15579 clean_ref=15579
  [OK] calls_raw: kill_replay=49420 clean_ref=49420
  [OK] file_versions: kill_replay=226 clean_ref=226
  [OK] file_instances: kill_replay=230 clean_ref=230
  [OK] status_dist: identical=True
  [OK] sym_names: identical=True (n=4469)
  [OK] parsed_paths: identical=True (n=230)
== KILL RECOVERY: PASS ==
```

### 行为说明
- kill 后重跑只读**本次 runid** 分片，旧 runid 分片不被重复导入（inserted=230 无重复）——
  幂等重放成立（file_instances 保持 kill 前旧状态 → hash 不匹配重新导入）。
- kill 留下 4 个旧 runid 孤儿分片：cleanup_expired_shards 仅回收 >24h 分片
  （近期分片保留供诊断/恢复，设计行为），probe 断言以 R2_PROBE_STRICT_SHARDS
  区分干净场景（零残留）与 kill 重放场景（允许孤儿分片）。

### V5 结论：PASS——导入中途 kill（70/230 行）→ 重跑 → SQL 全维度与干净对照一致。
