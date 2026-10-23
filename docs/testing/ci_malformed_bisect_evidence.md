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
- **v3 判读（CI run 39788826909，提交 b8b05ec）：v3 未治好 h7。** 主测试
  job（py3.13）11 failed + 8 errors，全部「Rust 侧打开数据库即 malformed」
  的 h7 签名（h7 7 + agent_integration 2 + e2e_triangle 1 + incremental 1
  + dashboard 8 errors），套件 `--maxfail=10` 早停于 3193 passed，**p15 文件
  未跑到，状态未知**。⇒ v2 的「缺 open 期 PASSIVE」归因被 v3 推翻——
  v1（h7 绿）与 v2/v3（h7 红）的差异集是 **{哨兵 on/off, autocheckpoint
  0/1000, TRUNCATE/PASSIVE drain}**，不只是 open-passive。
- 新的核心嫌疑 = **哨兵连接钉住 -shm**：哨兵使 Rust 后续短连接读到的是
  Python 侧系统 libsqlite3 写入的外来 `-shm` 视图（两个不同 SQLite 实现
  各自维护 -shm 缓存），打开时 wal-index 错位 ⇒ 拼撕裂页。v1 无哨兵时，
  短连接 close 会删除 `-wal`/`-shm`，下次 open 从**干净状态**重建
  wal-index（h7 治愈路径）；代价正是删 `-shm` 撕 Python 长连接（v1 的
  p15 撕裂路径）。**h7 与 p15 需求互斥——哨兵保 p15 即害 h7，删 -shm
  保 h7 即撕 p15，「两族从未同时绿过」在双实例前提下不可逾越。**
- 根本的唯一交集 = **同进程单一 libsqlite3 实例**：Rust 扩展改为链接
  系统库 sqlite3（而非 rusqlite bundled 3.45.0），两侧共用同一 VFS 与
  `-shm` 映射，同进程多连接即为 SQLite 原生支持场景。旁证：Probe D
  （同进程双 **Python** 连接 + WAL + DDL）在 CI 上持续全绿——同库多连接
  合法，问题仅在于**两个不同的 SQLite 构建**。
- **哨兵假说已由 CI 钉死（run 39795760927，a232967）**：Probe H（关哨兵
  + TRUNCATE drain）与 Probe I（关哨兵 + passive drain）**双双转绿**，
  而 Probe F（默认 = 哨兵开）在 h7 首文件即检出 malformed ⇒ **哨兵连接
  是 h7「打开即 malformed」的唯一驱动，与 drain 模式无关**（truncate/
  passive 在无哨兵下等价）。h7（需 close 删 `-wal`/`-shm` 重建 wal-index）
  与 p15（需钉 `-shm` 防删除）在双实例下**互斥不可兼得**——「两族从未
  同时绿过」正式定性为结构性壁垒，而非任一配置缺陷。
- v4 方向（待定）：① 单一 libsqlite3（Linux/macOS 链系统库、Windows 保
  bundled——本地免疫无妨；CI apt 装 libsqlite3-dev）；② 或放弃一族
  （默认回退关哨兵 = h7 绿 p15 红，回到 v1 代价面）。
  - **【后续定案，2026-10-23】** 本条「待定」已被第十节取代：v4 走方向 ①
    并已落地，但**平台限定比此处原述更窄**——实测 `release/build.py` 的
    `_use_system_sqlite()` 门控要求 **Linux 且系统 sqlite ≥ 3.45**；
    macOS 与 Windows 一律回退 bundled（故 macOS 上「链系统库」并未成立，
    双实例前提仍在，撕裂面未排除）；CI 侧 ubuntu-24.04（sqlite 3.45.1）
    命中，ubuntu-22.04（3.37.2）会安全回退 bundled ⇒ 该场景仍是双实例。
- 本取证文件使命完成：三代受害面漂移与根因已定性，结论固化进
  `rust_ext/src/db_conn.rs` 模块文档；后续回归由 ci.yml Probe F
  （纳入 p15/incremental 5 文件的隔离回归门；`set -e` 吞输出的门缺陷
  已修）与 Probe B/G/H/I 四臂（v1/v2 复现 + 哨兵隔离）钉死。

## 十、v4 判读（2026-10-23，CI run 39814732618，提交 69a87f4）—— malformed 根因已根除

### 10.1 切换生效（构建日志实证）

测试 job 与 probe job 的 `python release/build.py --rust` 输出均含：

```
[v4] system libsqlite3 detected -> linking SYSTEM sqlite3
```

（ubuntu-24.04 = sqlite 3.45.1 ≥ rusqlite 0.31 的 3.45 基线，版本门命中；
`--no-default-features --features extension-module` 未带 `sqlite-bundled`。）

### 10.2 主测试 job：malformed 归零，套件首次跑全

| Python | passed | failed | errors | `malformed` 计数 | `database is locked` 计数 | 耗时 |
|---|---|---|---|---|---|---|
| 3.10 | 8144 | 5 | 5 | 0 | 0 | 15:20 |
| 3.11 | 8102 | 5 | 5 | 0 | 0 | 14:13 |
| 3.12 | 8144 | 5 | 4 | 0 | 0 | 18:01 |
| 3.13 | 8148 | 5 | 0 | 0 | 0 | 08:44 |
| 3.14 | 8142 | 5 | 4 | 0 | 0 | 14:07 |

- **5 个版本签名完全一致**：0 malformed、0 locked，`--maxfail=10` 不再早停
  （对比 v3 卡在 3193 passed）。
- 5 项 FAILED 在所有版本逐字相同：`test_srv_019.py` ×4（既有残余面，
  audit 退出码 1）+ `test_rust_storage_service::test_facade_does_not_fallback
  _after_rust_failure` ×1（见 10.5）。
- 4~5 项 ERROR 为 HTTP RPC fixture 抖动，**每版本命中的模块都不同**
  （3.14=test_mcp_get_symbol_history、3.12=test_mcp_get_symbol_commit_history、
  3.11=test_mcp_rule_candidate_list、3.10=test_mcp_get_vulnerability_blast_radius）
  ⇒ 与 v4 无关的环境/端口型抖动。
- 原 292e684 残余面中的 dashboard UTF-8 / FTS stale / metrics **本轮全绿**，
  实际结果优于预期（三者很可能本就是 malformed 同族症状）。

### 10.3 各 probe 臂：h7 与 p15 首次同时全绿

| 臂 | 环境变量 | 文件 | 结果 |
|---|---|---|---|
| p15 臂 | `CW_RUST_WAL_DRAIN=truncate` | p15 ×3 + dashboard | ✅ 54 项全绿 |
| Probe G | `DRAIN=off`, `OPEN_CHECKPOINT=0` | h7 ×4 | ✅ 133 项全绿 |
| Probe H | `SENTINEL=0`, `DRAIN=truncate` | h7 ×4 | ✅ 133 项全绿 |
| Probe I | `SENTINEL=0`, `DRAIN=passive` | h7 ×4 | ✅ 133 项全绿 |
| 双连接复现 | — | `_wal_ab_dual_conn_repro.py` | ✅ 4/4 全绿 |
| 隔离循环 | 逐文件 | h7 + p15 ×9 文件 | ✅ 9/9 全绿 |
| **Probe F** | 默认门 + `-p _wal_ab_probe_plugin` | test_p7_batch_calls | ❌ 7 项 `database is locked`（见 10.4） |

**结论：第九节定性的「h7/p15 结构性互斥」壁垒已被 v4 拆除。** 四臂同时
绿意味着复现臂彻底失去撕裂面——共享 `-shm` 下连 TRUNCATE drain 都安全。

### 10.4 Probe F 的 `database is locked` —— 探针插件自身缺陷（已本地实证，非 v4 回归）

Probe F 设 `CW_WAL_AB_ROLLBACK_RUST_BATCH=1`，激活 `tests/_wal_ab_probe_
plugin.py`。本地复现（Windows，同构 SQL）给出完整链条：

```
PRAGMA foreign_keys = 1                      # CodeGraphDB 启用外键
rollback_config table exists: True
in_transaction BEFORE insert: False
INSERT failed: IntegrityError('FOREIGN KEY constraint failed')
in_transaction AFTER insert attempt: True    # ← 悬空写事务
is_feature_rolled_back: False                # ← 注入未生效，Rust 路径照走
```

机制：插件用 `task_id='wal-ab-probe'` 插 `rollback_config`，该表 `task_id`
有 `REFERENCES tasks(id)` 外键且库启用 FK ⇒ 插入违约抛 `IntegrityError`；
插件 `except Exception: pass` 吞掉，**`commit()` 从未执行**，Python sqlite3
legacy 模式自动开启的隐式事务悬空（持有写锁）。随后 `db_build.py:1207`
的 `is_feature_rolled_back` 因行不存在返回 False ⇒ 走 Rust 路径 ⇒
Rust `BEGIN IMMEDIATE` 撞上这把悬空写锁，`busy_timeout(5s)` 用尽 ⇒
SQLITE_BUSY `database is locked`。7 项 × 5s ≈ 36s，与实测耗时吻合。

**这同时也是 v4 根因模型的第二个独立证据**：该悬空写锁在 v1–v3 一直存在，
只是双实例时代 Rust 侧 fcntl 锁与之互不排斥、直接覆写（⇒ 撕裂 ⇒ malformed）；
v4 单实例下锁被正确承认，静默 corruption 转为诚实的 BUSY。
⇒ Probe F 红属**临时探针自身缺陷**，全套生产路径零 `locked`、零 `malformed`。

### 10.5 遗留：test_facade_does_not_fallback_after_rust_failure（**v1 即存在、v4 未收口**，跟进中）

- 现象：`DID NOT RAISE RuntimeError`，即 Rust 未检出篡改。
- **非 v4 引入**：本文第九节已记录 v1（292e684 全套 8138 passed）时就有
  「rust_storage 读路径 1 项（DID NOT RAISE：过期快照把篡改读成原值）」——
  同一签名、同一机理。v4 只修掉了 corruption，这个「读原值」症状未收口。
- 本地（Windows，bundled）通过；CI（ubuntu）失败。
- 判决链定位到 `rust_ext/src/storage.rs:1365`：`metadata_current == checksum`
  即提前 `Ok` 放行；不 raise 意味着 Rust 的 `open_connection` 读到了**篡改前**
  的 checksum，或 `schema_shape_matches_canonical` 认为 `task_dependencies` 仍在。
- **加固盲区（本轮新发现）**：`rust_ext/src/storage.rs:398` 的
  `open_connection` 是本仓库**第二个、从未加固的 opener**——缺
  `wal_autocheckpoint=0`、缺 open 期 `PRAGMA wal_checkpoint(PASSIVE)`、
  不走哨兵；而 `db_conn.rs:231` 的 `open_db_conn` 三样齐全且已被 v2–v4
  实证为 h7 治愈所需的必要形态。前者被生产路径 storage.rs:592 / 1305
  （= `initialize_or_migrate`，正是失败路径）/ 1519 / 1564 / 1723 使用。
  ⇒ storage 路径从未享受过 v2 以来的 WAL 加固，是「过期快照读原值」
  长期残留的最可能结构性原因。
- 跟进：已派单对齐 `open_connection` 与 `open_db_conn`（保留 storage 侧
  有意的 `foreign_keys=ON`），由 CI 判读三项指标（malformed 仍 0 /
  locked 仍 0 / 该测试是否转绿）。**修改有效性与否由 CI 判定，不预设结论。**

### 10.6 结论

**malformed 根因（同进程双 SQLite 实例撕裂 `-shm`）已由 v4 根除**，判据为
5 个 Python 版本 0 malformed + h7/p15 首次同时全绿 + 四臂复现失去撕裂面。
剩余三项：srv_019×4（既有残余）、HTTP RPC fixture 抖动（环境问题）、
10.5 的 fail-closed 测试失效（v4 特有，需定向探针）。

### 10.7 Probe J 判别（run 39870506204，提交 a28e646）—— 两条假说均被证伪，真凶在测试侧

在 probe job 加判别臂（插在 `Expose repo root` 之后、Probe E 之前，
`continue-on-error`），脱离 pytest 单独复现同一篡改序列，打印原文：

```
STEP1 rust init: {'success': True, 'version': 60, 'error': ''}
[after-tamper] checksum=('tampered',)
[after-tamper] task_dependencies_exists=False
[after-tamper] -wal exists=False size=0
STEP3 CodeGraphDB raised = RuntimeError: MIGRATION_FAILED: schema checksum mismatch
      for v60: stored=tampered, binary=ac345274..., and DB schema shape differs
      from canonical (missing tables)
[after-open] checksum=('tampered',)
VERDICT = 本地/CI 均正常抛错，无需判别
```

⇒ **storage.rs 的判决逻辑本身工作正常**；(a)「读到篡改前旧值」与
(b)「shape 误判」**双双证伪**（`after-open` 的 checksum 仍是 `'tampered'`，
未被策略 A 重写）。c530b21 保留，不再往 storage.rs 方向投入。

### 10.8 真凶：`CW_USE_RUST_STORAGE` 测试侧环境泄漏（非 Rust 缺陷）

`db/db_base.py:3729`：
```python
rust_storage_requested = os.environ.get("CW_USE_RUST_STORAGE", "1").lower() not in {"0","false","no","off"}
```
进程内一旦 `CW_USE_RUST_STORAGE=0`，`_initialize_schema` **整体跳过 Rust
storage**，checksum 校验根本不执行 ⇒ 不抛错。而多个测试在 fixture / 用例体内
**裸写 `os.environ[...] = "0"` 且不还原**，泄漏给同进程后续用例；pytest-xdist
下同一 worker 内测试共享环境 ⇒ 全套跑时该测试拿到 `0`。

这解释了全部现象：standalone 抛错、单文件本地通过、全套 CI 不抛错、
v1 即存在、与 v4 完全无关。

仓库里**已有正确范本**：`tests/test_multi_llm_contract_p4_protected_mutation.py`
已改用 `monkeypatch.setenv`，其注释原文即记录了这条泄漏。

其余违规点（10 处，改法统一为 `monkeypatch.setenv`）：
test_p4_lease_smoke.py:40 / test_p4_authoritative_clock.py:38 /
test_p1_write_path_verdict.py:26 / test_property_lease_fencing.py:31（签名已有
monkeypatch）/ test_multi_llm_contract_p4_lease.py:44 /
test_multi_llm_contract_p4_migration.py:45 / test_p3_task_mutation_smoke.py:34 /
test_p3_gate_smoke.py:38 / test_p3_reviews_smoke.py:21 /
test_p3_identity_smoke.py:28。

## 十一、环境泄漏修复判读（提交 0a6f3da + 4453a7c，run 39873640117）

### 11.1 本地实证（Windows，与平台无关，修复前）

用临时探针直接观测 env：

| 运行方式 | `CW_USE_RUST_STORAGE` | 结果 |
|---|---|---|
| 单独跑探针 | `'<UNSET>'` | 1 passed |
| 先跑 `tests/test_p4_lease_smoke.py` 再跑探针 | **`'0'`** | 泄漏确凿 |

配套行为对照：`test_facade_does_not_fallback_after_rust_failure` 单独跑通过、
与 `test_p4_lease_smoke.py` 同进程跑**转红**（Windows 上主因被 tempdir 清理的
`PermissionError: WinError 32` 掩盖一层，根因同为 env=0 后不走 Rust 路径）。

### 11.2 范围修正：10 处里只有 6 处真会泄漏

实测发现 `test_p3_gate_smoke.py` 之类是**脚本不是 pytest 测试**——env 写在
`def main():` 里，pytest 收集该文件得 **0 项用例**，永不执行、不会泄漏。
故按 `--collect-only` 实测分类后只改 6 个真收集到的文件：
test_p4_lease_smoke / test_p4_authoritative_clock / test_p1_write_path_verdict /
test_property_lease_fencing / test_multi_llm_contract_p4_lease /
test_multi_llm_contract_p4_migration。

### 11.3 CI 判读（0a6f3da）

| 指标 | 结果 |
|---|---|
| ① `test_facade_does_not_fallback_after_rust_failure` | **转绿，已从失败清单消失** ✅ |
| ② `database disk image is malformed` | **0** |
| ③ `database is locked` | **0** |
| ④ 新增失败 | 无；总数 `4 failed, 8146 passed`（基线 5 failed） |

剩余仅既有残余：`srv_019`×4 + 3 个 HTTP RPC fixture ERROR
（本次命中 test_mcp_get_test_coverage_http_rpc，仍为逐版本漂移的抖动）。

⇒ **env 泄漏假说被完整证实并闭环**。该 fail-closed 测试失效自 v1 存在、
与 v4 无关，至此收口。

### 11.4 修复引入的次生问题与收尾（4453a7c）

改为 `monkeypatch.setenv` 后 `import os` 变未使用，CI **Lint check 转红**
（ruff F401 ×5）。已字节级删除该 import（避免 CRLF 行尾归一化）：
test_multi_llm_contract_p4_lease:13 / test_multi_llm_contract_p4_migration:12 /
test_p1_write_path_verdict:12 / test_p4_lease_smoke:14 /
test_property_lease_fencing:11。
验证：`ruff check tests/` → All checks passed；本地 31 项 + 7 项回归全绿。

## 十二、临时探针清理（2026-10-23）

取证使命完成，清理 v1–v4 期间堆出的诊断脚手架：

- 删除 CI probe job 的 9 个 A/B 诊断臂：Probe A（rust storage init off）、
  B（v1 drain=truncate 复现）、C（fts 写短路回滚）、D（双连接最小复现）、
  E×2（metrics pytest/CLI 取证）、G（v2 零 checkpoint 复现）、
  H/I（哨兵隔离）、J（fail-closed 判别）。
- 删除两个自身标注「临时，实验后删除」的探针文件：
  `tests/_wal_ab_probe_plugin.py`（其 INSERT 触发 `rollback_config` 外键
  违约、被 `except: pass` 吞成悬空写事务，是 Probe F `database is locked`
  的真正来源，见 10.4）、`tests/_wal_ab_dual_conn_repro.py`。
- job 改名为 `WAL malformed regression gate (isolated)`，**只保留一个
  逐文件隔离回归门**（9 个历史最脆弱文件、无 xdist 单进程跑，出现
  malformed 即失败）——覆盖主测试 job 的 xdist 条件所不覆盖的场景。
  步骤数 18 → 8。

### 12.1 三个 env 诊断门的调用方核实与处置结论（结论已定：保留，不删）

`rust_ext/src/db_conn.rs` 的三个诊断门 `CW_RUST_SENTINEL_CONN` /
`CW_RUST_WAL_DRAIN` / `CW_RUST_OPEN_CHECKPOINT`——**全仓调用方核实**
（本次实测 grep）：

| 位置 | 命中 |
|---|---|
| `.github/workflows/` | 0 |
| `scripts/` | 0 |
| `tests/` | 0 |
| Python 侧（`db/`、`*.py`） | 0 |
| `rust_ext/` 内的 `std::env::var` 读取点 | 仅 `db_conn.rs`（三处读取）与 `storage.rs`（`CW_RUST_OPEN_CHECKPOINT` 一处） |
| Rust 单测 | **不走 env 路径**，依赖**编程式覆盖 API**：`set_sentinel_for_tests` / `set_wal_drain_for_tests` / `close_sentinel_connections`（均在 `rust_ext/src/db_conn.rs`） |

⇒ **准确表述**：三个 env 门在全仓**无任何调用方**——CI workflow、scripts、
tests、Python 侧全部零匹配；Rust 单测依赖的是**编程式覆盖 API**，不是 env
路径。旧表述「现仅被单测使用」不准确：单测并不经由 env 门，env 门是零调用方。

**处置结论（已定）：保留 env 门，不删。** 理由：

1. **v4 只在 Linux 生效**：Windows/macOS 的 Python ext 构建仍走
   `sqlite-bundled`（`release/build.py` 的 `_use_system_sqlite()` 仅在
   `sys.platform == "linux"` 且系统 sqlite ≥ 3.45 时返回 True；Linux 上
   sqlite < 3.45，如 ubuntu-22.04 的 3.37.2，也会安全回退 bundled）
   ⇒ POSIX 双实例在 macOS 上客观仍存在、撕裂面未排除
   （Windows 按句柄加锁免疫）。非 Linux 上仍需保留 env 门作为诊断/退化
   复现开关。
2. **env 门是唯一的运行时（跨进程）诊断手段**：编程式覆盖 API 只能在
   进程内生效，无法复现「另一个进程 / 另一个 Python 解释器持有同一 `.db`」
   这类跨进程场景；env 门可跨进程生效。
3. **留着零成本、零行为变更**：三个门均以「未设置 = 默认值」读取
   （sentinel on / drain passive / open checkpoint on），不设置即无影响。

### 12.2 事实性错误债登记（注释层，已由并行 worker 同步修正）

本次核查同时发现并登记一项**事实性错误债**：**约 13 个 Rust 文件 +
`db/db_base.py`** 的注释仍在用「v1/v2/v3 双实例撕裂模型」解释现状，其中
**6 处把默认 drain 误写为 `wal_checkpoint(TRUNCATE)`**（审计时点实测命中
`batch_register_query.rs` / `cas_merge_query.rs` / `cas_write_query.rs` /
`manifest_query.rs` 两处 + `db/db_base.py`）——与代码默认 **PASSIVE** 直接矛盾
（`rust_ext/src/db_conn.rs` 的 `with_db_conn` 中
`DRAIN_PASSIVE => { let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);"); }`；
TRUNCATE 仅在诊断复现臂显式开启时才发生）。

该批注释由另一 worker **同步修正为 v4 现状表述**（`rust_ext/` 侧注释 +
`db/db_base.py` 侧注释）。本节只做**登记与交叉引用**，不重复其改动内容。

### 12.3 注释修正的 CI 判读（run 39894991695，提交 6fb7ccb）

改动面：13 文件 +254/-63，**纯注释、零行为变更**（`git diff --cached`
非注释行检查 = 空）。

| job | 结论 |
|---|---|
| Lint check | ✅ success |
| WAL malformed regression gate (isolated) | ✅ success |
| Rust test binaries compile gate | ✅ success |
| Rust unit tests（TP-11G / workspace Windows / security-critical） | ✅ 全 success |
| Security gates (SAST/SCA/secret-scan) | ✅ success |
| Bootstrap Gate Check | ✅ success |
| Test on Python 3.10 / 3.11 / 3.12 / 3.13 / 3.14 | ❌ failure（**失败面 = 既有残余，非本次引入**，见下） |

5 个 Python 版本的失败面逐项核对（`gh api .../jobs/<id>/logs`）：

| 版本 | 总数 | `FAILED` | `ERROR` | malformed | `database is locked` |
|---|---|---|---|---|---|
| 3.10 | 4 failed, 8149 passed | `test_srv_019.py`×4 | 0 | 0 | 0 |
| 3.11 | 4 failed, 8147 passed, 2 errors | `test_srv_019.py`×4 | `test_mcp_guardrail_list_rules_http_rpc`×2 | 0 | 0 |
| 3.12 | 4 failed, 8145 passed, 4 errors | `test_srv_019.py`×4 | `test_mcp_guardrail_check_edit_http_rpc`×4 | 0 | 0 |
| 3.13 | 4 failed, 8147 passed, 2 errors | `test_srv_019.py`×4 | `test_mcp_server_full`×2 | 0 | 0 |
| 3.14 | 4 failed, 8144 passed, 3 errors | `test_srv_019.py`×4 | `test_mcp_get_test_coverage_http_rpc`×3 | 0 | 0 |

- **`FAILED` 面 5 版本逐字一致 = `test_srv_019.py`×4**（audit 脚本域，他人任务
  范围）⇒ 与 v4 基线（run 39889450467）**完全一致**，未新增失败。
- **`ERROR` 面逐版本漂移**（3.11 guardrail_list_rules / 3.12 guardrail_check_edit
  / 3.13 mcp_server_full / 3.14 get_test_coverage），根因**统一为
  `DaemonRemoteError: E_HTTP_MANIFEST_STALE`**（manifest executable 与存活 PID
  不匹配，pid 路径带 `(deleted)`）——CI 环境 daemon 二进制被重编译替换所致，
  **命中模块每版本都不同 ⇒ 环境/端口型抖动**，与本次纯注释改动无因果关系，
  且 v4 基线轮次已出现同类漂移。
- **`malformed` 与 `database is locked` 5 版本全 0** ⇒ v4 根治面未被本次改动
  破坏。
- 结论：**注释修正零行为变更已由 CI 证实**（无新增失败、无新增 malformed/
  locked、非测试 job 全绿）；Python job 的 failure 全部落在既有残余面。

## 十三、macOS 撕裂面裁决（run 39923719831，提交 3bfa649）—— 坐实

### 13.1 裁决手段

v4 只在 Linux 生效（`release/build.py:61-62` 平台门 `if sys.platform !=
"linux": return False`）⇒ macOS 走 **bundled**（`Cargo.toml:46` 默认 features
含 `sqlite-bundled`），同进程存在 Rust bundled SQLite 3.45.0 + Python 系统
libsqlite3 两个不同实现；macOS 的 `fcntl` 锁语义与 POSIX 相同（不像 Windows
按句柄免疫）⇒ 撕裂面**此前未被排除，但也从未被验证**（仓库此前无任何
macOS pytest job / malformed 断言门）。

为此新增平行 job `wal-malformed-gate-macos`（`.github/workflows/ci.yml`）：
`runs-on: macos-latest`、走 bundled 构建（不改 `build.py`）、逐文件隔离单跑
（无 xdist）与 ubuntu `wal-ab-probe` job **完全同口径**的 9 文件门
（`database disk image is malformed` 出现即判失败），并额外**观测**
`database is locked` 出现次数（只观测不判定）。

### 13.2 裁决结果：**macOS 撕裂面存在（已坐实）**

job 结论 = **failure**。观测证据（job id 120314877034 日志）：

| 观测项 | 值 | 判读 |
|---|---|---|
| Python sqlite3 版本 | **3.50.4**（`/Library/Frameworks/Python.framework/...`，系统库） | Python 侧 = 系统 libsqlite3 |
| Rust ext 链接（`otool -L`） | **`(no libsqlite3 in linkage => bundled SQLite, dual-instance precondition holds)`** | Rust 侧 = 静态 bundled SQLite 3.45.0 ⇒ **双实例前提成立** |
| `test_h7_ast_cache_activation.py` | **malformed_hits=14**、`database_is_locked_hits=0` | 首文件即命中 |
| 失败签名 | 7 项 FAILED，逐字为 `RuntimeError: Rust batch_resolve_and_save_calls 返回失败，已拒绝 Python 混合回退: 打开 CodeGraph 数据库失败: database disk image is malformed` | 与 ubuntu **v3 双实例时代**的 h7 族失败签名**完全一致** |

⇒ **第九节定性的「h7/p15 结构性互斥壁垒」在 macOS 上原样复现**：哨兵连接
钉 `-shm` ⇒ Rust 后续短连接读到 Python 侧写入的外来 `-shm` 视图 ⇒ 打开时
wal-index 错位 ⇒ malformed。macOS 的默认配置（哨兵开 + drain PASSIVE +
autocheckpoint=0）与 ubuntu v3 时代的失败配置**完全等价**，本门实测证实
了这一等价不是理论推断。

`database is locked` = 0 说明 macOS 上连「诚实 BUSY」的中间态都没出现 ——
撕裂直接发生（与双实例时代 ubuntu 同形）。

### 13.3 处置含义（决策路径已收窄）

本次裁决之前，可选项包括「维持现状 / macOS 切系统库 / 声明不支持 /
按平台条件化启用防御机制」。**macOS 红之后**：

- 「维持现状（仅注释登记）」**不再是可接受选项** —— 已实证的数据损坏
  风险不可沉默放任。
- 「按平台条件化退役防御机制」（仅 Linux 退役、macOS 保留）也**不成立**
  —— 防御机制（哨兵）在 macOS 双实例下本身就是 h7 的**致害因子**，保留它
  不能治 macOS，只会复现 v3 的失败面。
- 剩余路径：**macOS 也必须切到与 Python 同源的 libsqlite3（v4 扩展到
  darwin）**，或**收口 macOS 支持**。二者都是产品决策，需立项。

### 13.5 路径 A 可行性取证（提交 785266c）—— **结构性不可行**

为给上述决策补上前置事实，把 macOS 门的观测 step 扩为决策取证
（F1/F3/F5 + 解开 P4 矛盾）。下一轮 CI（run 39969046247，job id
120466519237）产出**决定性证据**：

| 观测项 | 实测值 | 推论 |
|---|---|---|
| `sys.executable` | `/Library/Frameworks/Python.framework/Versions/3.13/bin/python` | **P4 矛盾解开**：观测与 pytest 实际用的是 framework Python（python.org 安装版），`actions/setup-python` 报告的 `pythonLocation=hostedtoolcache` 并未上到 PATH 首位 ⇒ 此前「hostedtoolcache vs framework」的疑虑消除，证据链自洽 |
| `_sqlite3` 模块文件 | `.../lib/python3.13/lib-dynload/_sqlite3.cpython-313-darwin.so` | — |
| `otool -L` 该文件的 sqlite 相关行 | **仅文件自身的两个 arch 头，无任何 libsqlite3 依赖行** | Python 的 SQLite 是**静态编译进 `_sqlite3.so` 的自包含副本**，不依赖任何外部/系统 libsqlite3 |
| Python `sqlite3.sqlite_version` | **3.50.4** | 静态副本的版本 |
| `pkg-config --modversion sqlite3` | **3.51.0**（`--libs` = `-lsqlite3`，来自 `PKG_CONFIG_PATH=hostedtoolcache/.../lib/pkgconfig`） | runner 上**唯一**可链接的共享 libsqlite3 属于 hostedtoolcache Python，且**不是运行时 Python 所用的那份** |
| `sqlite3 --version`（CLI） | 3.50.6 | 第三份（CLI 自带） |

**关键推论（路径 A 生死判据）**：

1. **版本不等即独立证明**：若 `_sqlite3` 动态链接了 pkg-config 所指的
   共享库，则 `sqlite3.sqlite_version`（3.50.4）必等于
   `pkg-config --modversion`（3.51.0）。实测不等 ⇒ `_sqlite3` **没有**链
   那份共享库，与 otool 结果互相印证。
2. **不存在「可共用的共享 libsqlite3」**：运行时 Python 用静态 3.50.4；
   runner 上唯一的共享库是 3.51.0（且属另一个 Python）。即使把
   `build.py:61` 的平台门放开到 darwin，ext 也只能链到 3.51.0 那份
   ⇒ 与运行时 Python（3.50.4 静态）**仍是两个 SQLite 实例** ⇒ v4 的
   「同一 .so / 同一 VFS / 同一 `-shm`」前提**不成立**。
3. 且构建期（hostedtoolcache Python 3.13.16 + 其 sqlite3.pc 3.51.0）与
   运行期（framework Python 3.13 + 静态 3.50.4）**根本不是同一个 Python** ⇒
   即使链上共享库，也是「构建时链 A、运行时 B 不加载它」的错配。

⇒ **路径 A（macOS 切系统 libsqlite3）在本 runner 形态下结构性不可行。**
不是「改动没做」，而是「做了也不生效」：macOS 上 Python 的 sqlite3 是
静态自包含的，没有可与之共用的共享库。

**因此 macOS 议题的选项收敛为**：
- **B：收口 macOS 支持**（改动面已全部可枚举：README 无 macOS 字样、
  pyproject 无 OS classifier；需停 pkg/冻结包 + 清 CI matrix 与
  `release/macos/` 引用链）；
- **C2：仅在 macOS 关哨兵**（`CW_RUST_SENTINEL_CONN=0`）——Linux 上
  Probe H/I 实证可治 h7，但会把 p15 族换红，属「换一族失败」而非修复，
  且 macOS 上该 trade-off 尚未实测；
- **E：接受 macOS 为「已知不可用于并发 DB 访问」并显式登记**（最省事但
  风险沉默，与 13.3 第 1 条冲突）。

**注意**：以上结论基于 **GitHub macos-latest runner 形态**（python.org
framework Python 静态 sqlite + hostedtoolcache Python 提供 .pc）。用户
本机若用 Homebrew Python（其 `_sqlite3` 可能动态链接 Homebrew 的
libsqlite3），结论可能不同 —— 属 F1 的延伸待查项，但即便本机可行，
**分发面（PyPI wheel / pkg 要跑在万千用户机器上）仍无解**：无法要求
所有 macOS 用户的 Python 都动态链接同一份 libsqlite3。

### 13.6 本门的边界（不能回答什么）

- 只覆盖**当前默认配置**（哨兵开 + drain PASSIVE + autocheckpoint=0）下
  的 macOS 表现；其它配置组合（如关哨兵）未测。
- 只覆盖 macos-latest（arm64）+ Python 3.13；其它 macOS 架构 / Python
  版本未测。
- Windows 未覆盖（Windows 按句柄加锁免疫，且 Windows 侧一直走 bundled，
  本门不触及）。
- gate 在首个命中文件即 `exit 1`（fail-fast），故只有首文件的观测数据；
  其余 8 文件未跑（设计如此，与 ubuntu 门同口径）。


## 十四、收口裁决（2026-10-23，提交 81852a6 / d8584a0 / 5bca9f6）—— 选项 B 落地

### 14.1 决策

第十三节收敛出的三个选项中，**决策 = B：收口 macOS 支持**。决策输入：

- 路径 A（macOS 切系统 libsqlite3）已被 13.5 取证证明**结构性不可行**（macOS 的 Python sqlite3 静态自包含，runner 上唯一共享库属另一个 Python 且版本不同）；
- C2（仅 macOS 关哨兵）会把 p15 族换红，属「换一族失败」而非修复，且 macOS 上该 trade-off 未实测；
- E（显式登记接受）与 13.3 第 1 条冲突（已实证的数据损坏风险不可沉默放任）。

用户（产品负责人）批准的执行口径（三个决策点）：

| 决策点 | 口径 | 落地位置 |
|---|---|---|
| 1b | 裁决门 `wal-malformed-gate-macos` 改造为**防回归守卫** `no-macos-artifacts-guard`（runs-on: ubuntu-latest，扫描 `.github/workflows/` 无 macOS runner） | `.github/workflows/ci.yml` |
| 2 | 冻结包 / wheel / 离线包 / E2E 门**全停** | `enterprise-release.yml` / `release.yml` / `pyinstaller-build.yml` / `e2e-verify-macos-arm64.yml`（整文件删除） |
| 3b | `release/build.py` 在 darwin 上 **fail-closed**（`_reject_macos_build()` + `_detect_wheel_platform_tag` exit 1） | `release/build.py` |

### 14.2 收口语义（三层）

| 层 | 收口内容 |
|---|---|
| 发布物层 | macOS wheel / pkg / PyInstaller 冻结包 / 离线包 / E2E 验证门全停；`release/macos/` 整目录删除（`build_pkg.sh` 489 行） |
| 配置层 | `release/version.toml` 删 `macos` 平台项；`config_loader.py` / `rust_ext/src/cli/config.rs` 删 darwin 角色支持（Rust/Python 矩阵由 D4 差分测试 `tests/test_phase5_1_cli_diff.py` 锁定一致）；`build.py` / `config_loader.py` / `setup.py` darwin 分支 fail-closed |
| 声明层 | `docs/deployment.md` / `docs/cli_reference.md` / `docs/architecture.md` / `_feature_matrix.md` / `release-notes-rust-only-parser.md` / `cross-platform-packaging-release-plan.md` 标注收口 |

**源码运行时能力代码不删**：UDS `LOCAL_PEERCRED` / file lock / launchd 代码与缺陷无因果关系（缺陷在「bundled SQLite 双实例共享 -wal/-shm」），保留但文档统一标注「源码保留 / 平台已收口 / 不代表受支持」。

### 14.3 守卫设计（为什么翻绿而不是删门）

历史上 macOS matrix 被反复加回 4 处（`ci.yml` / `release.yml` / `pyinstaller-build.yml` / `enterprise-release.yml`）。删掉裁决门会留下真空：未来 PR 可无声加回 macOS runner 而无人发现。故裁决门翻转为守卫 job：

- `no-macos-artifacts-guard`（`.github/workflows/ci.yml`，runs-on: ubuntu-latest）：`grep -rEn` 扫描 `.github/workflows/` 全目录，命中 `macos-latest|macos-1[0-9]` 即 `::error::` + exit 1；
- needle 防自匹配：bash 相邻字符串拼接 `needle="macos""-latest|macos-1[0-9]"`——ci.yml 自身文本不匹配该模式（否则守卫第一个就红自己）。

### 14.4 落地清单与验证

| 提交 | 内容 | 验证 |
|---|---|---|
| 81852a6 | CI：4 workflow 收口 + 守卫 job；`e2e-verify-macos-arm64.yml` 删除 | 4 文件 `yaml.safe_load` 通过；守卫 needle 全域 0 命中；gate4b/gate5b/macos-pkg 零悬空引用；行尾 100% 保持（ci.yml LF、其余 CRLF）；6 files changed, +58/-535 |
| d8584a0 | 发布配置与脚本：`build.py` / `config_loader.py` / `version.toml` / `MANIFEST.in` / `setup.py` / `callwarden.spec` / `entry_cw_agent.py` / `_check_artifacts.py` / `inspect_pyinstaller_bundle.py` + `rust_ext/src/cli/config.rs` + D4 期望翻转 | 全文件 Python AST 通过；`cargo test --lib cli::config` 5/5；行为级：矩阵 linux/win32 intact、darwin 全 False、`PlatformPaths.detect()` exit 1、`fail_closed_unsupported()` exit 2 |
| 5bca9f6 | 测试断言：原「问题 8」build_pkg.sh 功能验证（4 用例，依赖已删除脚本）→ 收口防回归守卫（5 用例）；phase7 / cross-platform 断言改 fail-closed | 三文件回归全绿（1 个既有 Linux-only skip）；D4 差分测试在新建 ext 下 6/6 |

Rust ext 行为级验证（MEMORY 铁律：build 后必须验证二进制含预期改动）：新建 ext（`rust_ext/target/release/callwarden_core.dll`，`PYO3_PYTHON=C:/Python314/python.exe cargo build --lib --release`，36,688,896 bytes）上 `check_role_supported_py` 对全部 5 角色 × {darwin, macos} 返回 False，win32/linux 矩阵 intact。根目录 `callwarden_core.pyd`（36,690,432 bytes，cp314）被运行中进程锁定（Device or resource busy），暂以 sys.modules 预加载方式验证新二进制、未覆盖根 pyd（该文件 gitignored，不影响 CI）。

### 14.5 边界与遗留

- 本结论基于 **GitHub runner 形态**；用户本机 Homebrew Python 形态未测（13.5 已注明：即便本机可行，分发面仍无解——无法要求所有 macOS 用户的 Python 都动态链接同一份 libsqlite3）。
- 冻结包 `callwarden.spec` 的 `_PARSER_GRAMMAR_EXCLUDES` 使 pkg 100% 依赖 Rust ext 解析 ⇒ macOS 上 ext 即双实例 ⇒ pkg 必须与 wheel 一并停（不存在「只停 wheel」的中间态）。
- `release/artifact-manifest.json` 未被 git 跟踪（gitignored），其中 stale macOS 条目不动。
- 合并后 `enterprise-release.yml` 先 `workflow_dispatch`（dry_run=true）验证再打 tag（待办）。
