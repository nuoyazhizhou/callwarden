# CI malformed 12 项 —— 模块二分取证记录

## 一、缺陷基线（HEAD a1f1e2e，CI ubuntu py3.13，job 119090925320）

12 failed + 8 errors，**并非签名完全一致**，实测分两类：

| 类别 | 数量 | 签名 |
|---|---|---|
| 直接 malform | 8 | `RuntimeError: Rust batch_resolve_and_save_calls 返回失败，已拒绝 Python 混合回退: batch_resolve_and_save_calls 失败: database disk image is malformed` |
| 二次症状 | 4 | `tests/test_agent_rules.py` 四项：`assert None is not None` / `AssertionError: 符号查询不应失败` |

8 项直接 malform 明细：
- tests/test_h7_ast_cache_activation.py × 5
- tests/test_agent_integration.py × 2
- tests/test_e2e_triangle_demo.py × 1（TestE2ETriangleLinkage::test_full_triangle_linkage_e2e）

8 errors：tests/test_dashboard.py::TestDashboardBasic × 8（fixture 里 build_full_graph 失败）

## 二、12 项是否同点汇聚 —— 是，全部汇聚同一 Rust 函数

两条独立调用路径，终点相同：

- build_full_graph 路径：
  `db_build.py:877 build_full_graph` → `:1680 self._build_call_graph_multi_lang(file_results, only_files=set(...))`
  → `db_build.py:2123 _build_call_graph_multi_lang` → `:2155 _build_call_graph_multi_lang_via_rust`
  → `db_build.py:2244 batch_resolve_and_save_calls(...)` → `:2275 raise RuntimeError`
- refresh_file 路径（agent_rules 走的这条）：
  `tests/test_agent_rules.py:1044 db.refresh_file(test_file)` → `db_build.py:4069 refresh_file`
  → `:4088 _refresh_file_internal` → `:4159 _refresh_file_generic`（.py 走 generic 分支）
  → `:4204 self._build_call_graph_multi_lang({rel_path: result}, only_files={rel_path})`
  → 同上 → `batch_resolve_and_save_calls`

即：12 项（含 4 项二次症状）**全部**经由 `_build_call_graph_multi_lang` → `via_rust` → `batch_resolve_and_save_calls`。
`via_rust` 在 Rust 已加载但事务失败时**直接 raise**（db_build.py:2255 / 2271），不回退 Python 路径。

`build_full_graph` 出现 0 次于 test_agent_rules.py 属实——它走 refresh_file 的另一条路径，
但终点仍是同一个 Rust 函数。

## 三、任务前提的一处错误（需修正）

前提称「E. 连接寿命 | Probe G（run 39543315114 / job 119035769512）仍 12 failed 同签名」。

实测：
- job 119035769512 = **Test on Python 3.13**（普通测试 job）。该 job 日志中
  `CW_RUST_POOL_LIFETIME` 出现 **0 次** —— 即该 env **根本没生效**，
  它跑的就是默认（全池化）路径，失败集自然是 12 failed + 8 errors。
  拿它证明"连接寿命无关"是把「普通基线」误读成「Probe G 结果」。
- 真正跑 Probe G 的是 job **119035769401**（WAL A/B probe job），该 job 内
  `CW_RUST_POOL_LIFETIME: short` 生效（env 块可见）。其结果：
  - **G2（h7 隔离单跑，基线 7 failed 1 passed）→ `........  [100%]` = 8 passed 全绿**
  - **G1（5 文件合跑）→ 仅剩 1 项 `test_code_quality_full_mode` 的 UTF-8 旧错误，非 malform**
  - Probe F（同 job，无 short env）→ h7 隔离 7 failed 1 passed（malform 复现）

⇒ **连接寿命恰恰是 malform 的开关**，前提 E 站不住。

## 四、二分实验记录

| 轮次 | commit | CI run / job | 配置 | h7 隔离结果 | 12 项 malform |
|---|---|---|---|---|---|
| 基线 | a1f1e2e | 39560686193 / 119090925320 | 全池化 | 7 failed 1 passed | 12 failed + 8 errors |
| ① | 9cc8c6b | 39565776243 / 119107567691 | 回退 batch_calls_query.rs 池化（其余 7 模块仍池化） | 仍红 | 11 failed + 8 errors（agent_rules 4 项转绿；h7 +2、incremental_parse +1） |
| ② | 9e3ca8f | 39570134307 / 119121942247 | 模块矩阵（8 模块逐个关池化） | 逐个关均 7 failed 1 passed；**全关 8 passed**；基线 7 failed 1 passed | Rust job 因单测未适配签名而全红 |

## 五、Probe H 模块矩阵结果（run 39570134307 / job 119121942247）

| CW_RUST_POOL_OFF | h7 结果 | 首个失败签名 |
|---|---|---|
| batch_build | 7 failed 1 passed | `BEGIN IMMEDIATE 失败: database disk image is malformed` |
| batch_calls | 7 failed 1 passed | `打开 CodeGraph 数据库失败: database disk image is malformed` |
| batch_file_versions | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...`（混有 `BEGIN IMMEDIATE 失败`） |
| batch_register | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |
| cas_merge | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |
| cas_write | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |
| incremental_build | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |
| manifest | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |
| **全部 8 个** | **8 passed 全绿** | — |
| 不关（基线） | 7 failed 1 passed | `batch_resolve_and_save_calls 失败: ...` |

**判定：单独回退任一模块都不足以消除 malform；必须全部回到 open→drop 语义才绿。**
⇒ 根因是「长活连接」这一性质本身，不是任何单个模块的写路径细节。

**失败签名随被关模块而变**（BEGIN IMMEDIATE 失败 / 打开 DB 失败 / 调用内失败），
说明库文件在被关模块的调用点**之前**就已损坏，各模块只是首个读者。

## 六、池化引入的具体行为差异（逐项核对 c53da76^ → c53da76）

### 6.1 连接寿命
池化前：每次调用 `Connection::open` → 用完 drop（drop 时 SQLite 自动 checkpoint 并删 -wal/-shm）。
池化后：每 db_path 一个长活连接，进程内常驻，**WAL 从不被收尾**。

### 6.2 open 期 checkpoint 频率
池化前：每次调用 open 时都 `wal_checkpoint(PASSIVE)`。
池化后：**每进程每 db_path 只做一次**（open_pooled:122-124）。

### 6.3 PRAGMA 序列被统一（原本各模块不一致）
池化前各模块 `open_readwrite` 的 PRAGMA 集合本就不同：

| 模块 | journal_mode | synchronous | foreign_keys | open 期 checkpoint |
|---|---|---|---|---|
| batch_build_query | ✓ | ✓ | ✓ | ✓ |
| batch_calls_query | ✓ | ✓ | ✓ | ✓ |
| batch_file_versions_query | ✓ | ✓ | ✓ | ✓ |
| cas_merge_query | ✓ | ✓ | ✓ | ✓ |
| manifest_query | ✓ | ✓ | ✗ | ✓ |
| **batch_register_query** | ✗ | ✗ | ✓ | ✓ |
| **incremental_build_query** | ✗ | ✗ | ✓ | ✓ |
| **cas_write_query** | ✓ | ✗ | ✗ | ✗ |

池化后 `open_pooled` 统一为全套（journal_mode=WAL / synchronous=NORMAL / foreign_keys=OFF / wal_checkpoint(PASSIVE)）。
其中 `PRAGMA journal_mode=WAL` 是**写操作**（改 db header）——在已损坏的库上会直接抛
`malformed`（这正是二分①新增失败项的签名 `打开 CodeGraph 数据库失败: 设置 WAL 模式失败: ...`），
即它是**症状放大器**，不是根因。

### 6.4 两处 drop 自动回滚改为闭包内显式 ROLLBACK
batch_register_query / batch_file_versions_query：池连接长活，drop 不再触发回滚，
故改为闭包内显式 ROLLBACK（否则失败事务悬挂到下一次调用）。这是必要的连带改动，非根因。

## 七、机制假设（待 Probe I 判读）

关键对照：`with_short_conn`（模块矩阵里的短连接路径）每次调用后做
`wal_checkpoint(TRUNCATE)` 再 drop 即可全绿；长活池连接**从不**收尾 WAL。

⇒ 假设：长活连接下 WAL 持续累积 → 达到 Python 侧 `wal_autocheckpoint` 默认阈值
（1000 页，本地实测确认）→ Python 主连接触发 autocheckpoint 回写主库 →
与同进程另一 SQLite 实例（Rust bundled rusqlite vs Python 系统 libsqlite3，
POSIX 上 fcntl 锁按进程分配、无法互斥 WAL-index）的写事务交错 → 撕裂页 → malformed。

⇒ 正式修法候选：**保留长活池连接**（池化收益：消除 Rust 侧多实例、消除短连接
drop 的 checkpoint 风暴、保留 c53da76 修好的 11 项），但在每次写调用后
`wal_checkpoint(TRUNCATE)` 收尾 WAL，使 Python autocheckpoint 无帧可搬。

Probe I 判读：
- I1（长活 + 每次调用后 TRUNCATE）→ 8 passed ⇒ 假设成立，可直接实施正式修法
- I1 仍红 ⇒ 根因是长活连接本身持有 WAL 读标记，需改连接架构
- I4（长活 + PASSIVE）→ 区分「必须清空 WAL」还是「回写帧即可」

## 八、环境事实

- CI 自行 `python release/build.py --rust` 构建扩展，pyd 不入库 ⇒ 只需提交 Rust 源码
- Windows 对该缺陷免疫（LockFileEx 按句柄加锁），本地只能确认无新回归
- 仓库根 callwarden_core.pyd 为陈旧构建且被进程占用无法覆盖；
  本地测试须用 PYTHONPATH 前置新构建目录
  （`%TEMP%/cw_new_ext/callwarden_core.cp314-win_amd64.pyd`）+ `C:/Python314/python.exe -P -m pytest`
- `cargo check --lib` **不编译 #[cfg(test)] 代码** —— 改签名后必须加 `--tests`

## 九、后续判读（2026-10-22）

- Probe I 结论推翻了本文第八节候选修法：长活 + 任何 drain 策略均红；
  v1（a80ca63）实施「短连接 + 写后 TRUNCATE drain」治好 h7 族 12 项，
  但 292e684 全套 CI（8138 passed，首次跑全）暴露 v1 遗留：
  **Rust 侧在本进程内的任何 WAL 回收动作（TRUNCATE drain / drop 时
  autocheckpoint / 最后连接删 -shm）都撕裂同进程 Python 长连接快照**——
  p15 族 6 项 + incremental_parse 1 项 + rust_storage 读路径 1 项
  （DID NOT RAISE：过期快照把篡改读成原值）。
- v2 修法（d5cfbeb）：短连接 + **哨兵连接钉住 -shm**
  （每 .db 一个永不关闭的空连接）+ `wal_autocheckpoint=0` + 默认零
  checkpoint（CW_RUST_SENTINEL_CONN / CW_RUST_WAL_DRAIN 诊断门；
  默认 sentinel on / drain off）。WAL 增长交 Python 侧 autocheckpoint
  回收（单实例操作安全）。daemon 纯 Rust 进程不经 with_db_conn，不受影响。
- **v2 翻车（CI run 39761713228）**：5 个 Python 测试 job 全部回退到 c53da76
  时代的 h7 族签名（12 failed / 2729 passed，maxfail 早停，「malformed」×32），
  但机制**不同**：失败发生在 Rust 侧**打开数据库时**——
  `RuntimeError: Rust batch_resolve_and_save_calls 返回失败…打开 CodeGraph
  数据库失败: database disk image is malformed`。归因：v2 删掉了 open 期
  PASSIVE checkpoint，全新的 Rust 短连接面对 Python 侧遗留 `-wal`/`-shm`
  状态时缺少 wal-index 重建，读到过期 wal-index ⇒ 拼出撕裂页。
  ⇒ v1 能治 h7 的承重件不是「写后 TRUNCATE」，而是 **open 期 PASSIVE
  checkpoint 触发的 wal-index 重建**。
- v3 修法（本次提交，「温和 drain」）：短连接 + **open 期 PASSIVE**
  （CW_RUST_OPEN_CHECKPOINT 门，默认开）+ 哨兵 + `wal_autocheckpoint=0`
  + **写后 PASSIVE drain**（CW_RUST_WAL_DRAIN 三态门：off/passive/truncate，
  默认 passive）。PASSIVE 与 TRUNCATE 的判别性差异：不在有读者持有读标记时
  强行清空 WAL，故不撕 p15；又完成了帧回写，h7 不依赖写后任何动作。
  CI 经验矩阵由三条臂钉死：默认（v3，主测试 job 期全绿）/
  Probe B（v1 复现：truncate → p15 撕裂）/ Probe G（v2 复现：off + 关
  open-PASSIVE → h7 打开即 malformed）。
- 本取证文件使命完成：三代受害面漂移与根因已定性，结论固化进
  `rust_ext/src/db_conn.rs` 模块文档；后续回归由 ci.yml Probe F
  （纳入 p15/incremental 5 文件的隔离回归门）与 Probe B / Probe G
  （v1/v2 复现臂）钉死。
