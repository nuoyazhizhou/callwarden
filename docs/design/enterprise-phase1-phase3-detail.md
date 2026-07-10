# Enterprise Phase 1 + Phase 3A + Phase 3B 详细实施设计

状态：Draft v7（评审第六轮 P1 修订版）
日期：2026-07-10
父文档：
- [enterprise-daemon-shared-snapshot-plan.md](enterprise-daemon-shared-snapshot-plan.md)（主架构）
- [enterprise-architecture-evolution.md](enterprise-architecture-evolution.md)（演进背景）

## v7 变更摘要（针对评审第六轮 5 个 P1 + 2 个 P2）

1. **[P1#1] CAS 与 workspace 双库提交原子性**：`refresh_file_with_cas` 把 parse 放在 CAS `BEGIN IMMEDIATE` 内，同 UID 并发刷新长时间串行化；且 workspace DB 先于 CAS DB 提交，崩溃窗口内 manifest 会指向不存在/已回滚的 CAS。v7：parse 移出锁外；refresh 持共享 `flock`、GC 持独占 `flock`；锁内重新检查 CAS 命中；**CAS 先于 workspace manifest 提交**；新增带 TTL 的 `cas_pending_refs` 表保护窗口期内 CAS 不被 GC 删除；补 crash-injection 测试。
2. **[P1#2] local_id 哨兵值冲突**：`local_id` 从 0 开始且 `lexical_parent_local_id=0` 表示"无父节点"，第一个符号无法被引用为父节点。v7：`local_id` 从 **1** 开始（0 保留给 synthetic module symbol）；`lexical_parent_local_id` 改 `NULL`（`Option<u32>`）表示顶层；`caller_local_id` 改 `NULL` 表示顶层裸调用。
3. **[P1#3] ParseInputV1 ABI**：Rust 解析原始字节并用 `DefaultHasher`，Python 解码标准化 UTF-8 后 SHA-256；CRLF/GBK/BOM/UTF-16 文件产生不同 CAS key、byte range、符号内容。v7 新增 Phase 3A.0 §3.0.1 ParseInputV1：canonical bytes（BOM 剥离 + 编码检测 + UTF-8 转换）、`newline_policy="lf"`（CRLF/CR→LF）、`offset_coordinate_space="canonical_utf8_bytes"`、`content_hash=sha256(canonical_bytes)`（Rust 必须用 `sha2::Sha256`，不用 `DefaultHasher`）；`input_abi_version` 纳入 CAS key。
4. **[P1#4] 灰度门漏文件**：伪代码先把放行语言移入 `rust_files`，扩展不可用时这批文件既不走 Rust 也不在 `non_rust_files` 中，最终消失；默认空集合会禁用已稳定放行的 C 快路径。v7：`non_rust_files` 初始包含全部文件，只有 Rust 实际可用时才移出；pool 异常→整组回退 Python；单文件 error→该文件回退 Python；C 快路径独立保留，不受 `RUST_PARSE_ENABLED_LANGS` 影响。
5. **[P1#4 补充] clean→clean 投影替换**：见 v6 P1#4（已修复，v7 保留）。
6. **[P1#5] daemon 文件读取模型**：daemon 固定为 `User=callwarden`，`SO_PEERCRED` 只证明请求者身份，不能让其读取 `0700/0750` 目录。v7 在 [enterprise-architecture-evolution.md](enterprise-architecture-evolution.md) 新增"文件数据面"章节：per-UID helper/worker 进程以 `peer_uid` 身份 fork 运行完成文件 I/O，daemon 对 worker 返回数据重新计算 hash 校验；客户端内容传输和 POSIX ACL 作为降级方案。

## v7 P2 修复

- **[P2#1] CAS key 统一**：§3.4 重新定义的 `compute_cas_key_v1` 漏掉 `abi_version`，与 §3.0 冲突。v7：删除 §3.4 重复定义，全文档（schema 注释、冷启动伪代码、验收测试）统一调用 §3.0.3 的 `compute_cas_key_v1()`，该函数同时包含 `abi_version` 和 `input_abi_version`。
- **[P2#2] edge INSERT 同 workspace 校验**：`foreign_keys=OFF` 时只规定显式 DELETE，没有规定 edge INSERT 的同 workspace 校验，`doctor` 只能事后发现。v7：`workspace_resolved_edges` 写入采用 `INSERT ... SELECT` 约束 caller/callee 的 `workspace_id`；写事务提交前运行局部完整性检查；`doctor --fk-check` 增加 edge workspace 一致性校验。

## v6 变更摘要（针对评审第五轮 6 个 P1 + 6 个 P2）

1. **[P1#1] 跨库 FK 删除 + FK 策略明确**：`workspace_manifests` 删除 `FOREIGN KEY (cas_key) REFERENCES cas_file_cache`（`cas_file_cache` 在独立 CAS DB，普通 FK 无法跨库）。当前 [db_base.py L1614](../../db/db_base.py#L1614) 执行 `PRAGMA foreign_keys=OFF`，组合 FK 和 `ON DELETE CASCADE` 不生效。v6 策略：bulk refresh 期间保持 `foreign_keys=OFF`（性能），应用层显式 DELETE 维护一致性，`cw doctor --fk-check` 作为迁移后校验命令（临时 `foreign_keys=ON` + `PRAGMA foreign_key_check`）。FK 声明保留为**意图文档**，不依赖其运行时强制。
2. **[P1#2] Phase 3A.0 ParseFactV1 ABI**：当前 Rust `SymbolInfo`/`RawCall`（[lib.rs L48](../../rust_ext/src/lib.rs#L48)、[lib.rs L65](../../rust_ext/src/lib.rs#L65)）缺少 `local_id` / `lexical_parent_local_id` / `start_byte` / `end_byte` / `caller_local_id` / `call_ordinal`。新增 Phase 3A.0 定义 ParseFactV1 ABI——Rust/Python parser 统一输出 occurrence ID、parent ID、byte range、call ordinal。ABI 版本纳入 CAS key。Alignment tests 增加这些字段对齐。
3. **[P1#3] GC scan/sweep TOCTOU 修复**：fail-closed 未解决并发——GC 扫描 workspace A 后，refresh 发布新 key 并写入 manifest，GC 根据旧 live set 删除该 key。v6 引入 per-UID GC/refresh gate：refresh 从 CAS lookup 到 manifest commit 持共享锁（`gc_generation` 读锁），GC scan+sweep 持独占锁（`gc_generation` 写锁 + generation 自增）。
4. **[P1#4] clean→clean 投影替换**：`git pull`/commit 切换后文件从 clean 版本 A 变 clean 版本 B，查询只校验 `is_dirty=0` 未校验 `wm.cas_key = wsym.cas_key`。v6 refresh 在一个 workspace 事务中按 `rel_path` 原子替换 projection（删旧 edge → 删旧 symbol → 插新 projection → 更新 manifest），查询额外校验 `wm.cas_key = wsym.cas_key`。
5. **[P1#5] RUST_PARSE_ENABLED_LANGS 灰度门**：Alignment tests 通过不等于可以切换默认 Rust——白名单允许 TypeScript"完全未提取符号"，但生产路径已将 TypeScript 分给 Rust。v6 引入 `RUST_PARSE_ENABLED_LANGS` 环境变量逐语言放行，"整种语言零符号"不进可接受白名单。补充 `test_kind_alignment` / `test_signature_alignment` / `test_visibility_alignment`（之前注释声称存在但实际缺失）。
6. **[P1#6] Global CAS 冷启动重建**：Local CAS（`~/.callwarden/cas.db`）用户可写，直接导入 Global CAS 会被伪造 `cas_key` 污染所有用户。v6：Global CAS 冷启动重建（从源文件重新 parse），若必须 seed 则把 Local CAS 当不可信候选，daemon 重新读源文件、校验 content hash、重新 parse 后才发布。

## v6 P2 修复

- `cas_publish` 回滚路径移除多余的 `DELETE building`（rollback 已回滚整个事务，再执行 DELETE 会报 "transaction within transaction"）。
- Dirty tombstone 的 `IN (?)` 改为子查询（避免大文件符号多时参数上限）。
- `grace_threshold` 真正参与 live set：宽限期内 workspace 的 cas_key 必须加入 live set（已有 pass 但注释修正）；`gc_cas` 在 lock 获取失败时中止（非静默跳过）。
- Clean/dirty 查询分别 `LIMIT` 改为 `UNION ALL + ORDER BY + LIMIT`（全局排序，不返回 `2 × limit`）。
- 架构图"只读共享"改为"daemon-only"，路径统一为 `/var/lib/callwarden/cas.db`。
- 风险表"单用户单进程"更新为"WAL + busy_timeout 多进程重试"；实施章节"需补 fallback 测试"标记为已完成。

## v5 已修复的 5 个 P1（保留）

1. Global CAS daemon-only / 2. GC fail-closed 两阶段 / 3. Dirty tombstone 先删 edge / 4. cas_publish 四阶段 / 5. start_byte/end_byte 移到 cas_symbols

## v5 已修复的 P2（保留）

查询列名 / 组合 FK / ATTACH 参数化 / GC 删除顺序 / Phase 3B 验收 / Alignment whitelist Counter 相减

## v4 已修复的 4 个 P1（保留）

1. CAS 自包含（`cas_symbol_contents`）/ 2. GC 扫描所有 workspace / 3. 统一 symbol identity（`workspace_symbols`）/ 4. Dirty overlay 按 rel_path 屏蔽

## v4 P2 修复（保留）

- CAS DB 并发：`busy_timeout=5000` + 有界重试 + 重新检查 `state=ready`。
- CAS DB schema version：`cas_schema_meta` 表。
- UNIQUE 约束：`call_ordinal` 列。
- ATTACH 只读 + `cas_db.cas_symbols` 全名引用。
- `enterprise-architecture-evolution.md` 旧章节标记废弃。

## 实施范围（v3 调整后）

- **Phase 1**：Rust 多语言 parse 接入主路径（5-7 天）— ✅ 1.1 已完成
- **Phase 3A.0**：ParseFactV1 ABI（parser 输出 local_id/parent/byte range/call_ordinal）— **v6 新增，Phase 3A 前置**
- **Phase 3A**：Local CAS parse cache（per-UID 独立 DB），只承诺减少重复 parse（10-11 天）
- **Phase 3B**：workspace 查询路径迁移 + resolved edge store（10 天）— **建议在 Phase 2 daemon 落地后实施**

Phase 2（daemon skeleton + UDS）、Phase 4+（snapshot sharing / ACL / GraphSnapshot / resolved_edges 共享）按主设计延后。本文档**不承诺**跨用户授权查询、不承诺 resolved_edges 共享、不承诺 snapshot 级 thin DB 复用。

**评审建议的实施顺序**：Phase 1 → Phase 3A.0（ABI）→ Phase 3A（Local CAS）→ **Phase 2 daemon（优先）** → Phase 3B（daemon 内单源实现）。Phase 3B 在 daemon 落地前可做 schema 预留，但不建议抢先实现 Python 查询架构（会被 Rust daemon 替换）。

---

## 1. 背景与目标

### 1.1 触发场景

服务器（一台共享 Linux 固件开发机）现状：

- 14T 磁盘已用 11T，86% 占用。
- 十几个开发者，每人 3-10 个 `A98_stable_5.2_*` 工作目录，每个 70-100G。
- 95%+ 文件在不同工作目录间内容完全相同（同 repo 不同分支、同分支不同 checkout）。
- 架构师为了帮人看不同分支，自己保留 5+ 个分支 checkout，磁盘吃紧。
- 任何人 `git pull` 时，所有工作区各自重新 parse 一遍相同文件。

### 1.2 不解决什么（明确排除）

- 不解决 firmware 源码本身重复 checkout 的磁盘占用（那是 git worktree / sparse-checkout 的事）。
- 不重写 120+ MCP 工具。
- 不做 daemon + UDS + SO_PEERCRED（Phase 2，延后）。
- 不做直接 B-tree 页写入（已否决）。
- **不承诺跨用户授权查询**（Phase 2 daemon 落地前）。
- **不共享 resolved_edges**（Phase 4+ 的事，需要 toolchain fingerprint 闭合）。
- **不承诺 snapshot 级 thin DB 复用**（同上）。

### 1.3 解决什么

| 痛点 | Phase 1 | Phase 3A | Phase 3B |
|------|---------|---------|----------|
| 相同文件被重复 parse | 11 语言统一走 Rust rayon 并行 | **单用户/同 UID 下跨 workspace 按 file_hash 命中 CAS** | — |
| Python parser 与 Rust parser 结果不一致 | alignment tests 锁定差异 | 统一数据源后自然消除 | — |
| symbols/calls 表按 file_instance_id 重复存储 | — | CAS 表去重存储 | **查询走 CAS 表，symbols/calls 仅 dirty overlay** |
| db_build 写入路径 O(N×M) 隐藏扫描 | — | CAS 命中跳过 parse+写入 | resolver 加 workspace 过滤 |
| 架构师需要 checkout 分支才能看代码 | — | — | 单用户/同 UID 下多 workspace 共享查询（不跨 UID） |

### 1.4 共享范围明确（重要）

**Phase 3A/3B 的 CAS 共享范围**：

| 场景 | 是否共享 | 说明 |
|------|---------|------|
| 同一用户的多个 workspace（同 UID） | ✅ 共享 CAS | Local CAS 是 per-UID 独立 DB（`~/.callwarden/cas.db`），同 UID 多 workspace 共享 |
| 同 UID 下多个容器视角的同一 workspace | ✅ 共享 CAS | 通过 workspace 注册时 realpath 解析 |
| 不同 UID 的用户 | ❌ 不共享 | 各自打开自己的 `~/.callwarden/cas.db`，无 daemon 无 SO_PEERCRED，不承诺跨用户授权 |
| 跨机器 | ❌ 不共享 | 不在本文档范围 |

跨用户共享需要 Phase 2 daemon + SO_PEERCRED + 统一权限校验，本文档不涉及。

### 1.5 CAS 物理存储架构（P1#1 修复）

**问题**：当前 [config.py L28](../../config.py#L28) 的 `get_project_db_path()` 按项目根路径生成 per-workspace DB（`~/.callwarden/<hash>/callwarden.db`）。若把 CAS 放进 workspace DB，无法跨 workspace 命中。

**解决方案**：CAS 使用独立的 per-UID DB，与 workspace DB 物理隔离。

```text
~/.callwarden/
├── cas.db                          # Local CAS DB（per-UID，跨 workspace 共享）
│   ├── cas_file_cache              # 文件解析结果主表
│   ├── cas_symbols                 # 内容级符号事实（不含路径信息）
│   ├── cas_raw_calls              # 内容级原始调用（不含 callee_file/qualified）
│   └── cas_imports                # 内容级 import 声明
├── <hash_A>/                       # workspace A 的 DB
│   └── callwarden.db
│       ├── symbols / calls         # workspace 投影 + dirty overlay（Phase 3A 保留写入）
│       ├── workspace_symbols       # 统一身份表（clean/dirty 都投影到此表）
│       ├── workspace_manifests     # workspace 文件清单（cas_key 指向 cas.db）
│       ├── workspace_resolved_edges    # 跨文件解析边（per-workspace）
│       └── ...（现有表）
├── <hash_B>/                       # workspace B 的 DB
│   └── callwarden.db
└── ...
```

**连接管理**（v4 P2 修复：不再假设单进程独占）：
- CAS DB 用独立的 SQLite 连接（`~/.callwarden/cas.db`），WAL 模式 + `busy_timeout=5000`。
- 同 UID 的多个 CLI / hook / VS Code 进程可能同时打开 CAS DB，靠 WAL + `busy_timeout` + 有界重试协调。
- CAS 写入（原子发布）遇到锁时最多等 5 秒，超时后友好提示"数据库正忙，请几秒后重试"（与现有 CLI 写锁策略一致）。
- 原子发布协议中，重试时**重新检查 `state=ready`**（其他进程可能已写入同一 key）。
- Workspace DB 保持现有 per-workspace 连接。
- **跨进程写协调的终极方案**是 Phase 2 daemon（单写者），Phase 3A 用 `busy_timeout` + 重试缓解。

**Enterprise CAS（Phase 2+）**：`/var/lib/callwarden/cas.db`，只能由 daemon 打开，跨 UID 共享。本文档不涉及。

### 1.6 验收标准

Phase 1：
- 支持 11 种语言默认走 Rust `batch_parse_files_lang_pool`，非支持语言回退 Python。
- 大批量解析时父进程 RSS 不再持有全部 Python dict 峰值（沿用 P30 流式 pool）。
- 与 Python parser 核心字段 alignment test：actual_diff - known_diffs == empty（Counter 相减，剩余差异为零；已知差异显式记录在 `KNOWN_SYMBOL_DIFFS` / `KNOWN_CALL_DIFFS` 中）。

Phase 3A：
- **单用户** 50 个同 repo clean workspace 中，相同文件 parse 只发生一次（CAS 命中率 ≥ 95%，验收口径 **parse miss = 0**）。
- 第二个 clean workspace 注册后，分别记录 hash / CAS 查询 / 复制 / resolve / 写库耗时，**parse 耗时 = 0**（不再用"低于第一个 10%"作为验收口径，因为复制和 resolve 仍需时间）。
- `resolved_edges` 一律 per-workspace 存储，不共享。
- Dirty workspace 修改不污染共享 CAS。
- CAS 写入原子性：崩溃后 CAS 表无半成品记录（`state=building` 条目被 GC 清理）。

Phase 3B：
- 查询路径走 `workspace_symbols` + `workspace_resolved_edges`，dirty 走 `symbols`/`calls` overlay。
- 多 workspace 共享 CAS 时，符号查询返回的 `qualified_name` / `content` 跨 workspace 一致。
- `get_callers`/`get_callees` 走 `workspace_resolved_edges`，按 `workspace_id` 过滤。
- `GraphStore` 加载按 `workspace_id` 过滤，不串库。

---

## 2. Phase 1 详细设计：Rust 多语言 parse 接入主路径

### 2.1 现状

- [rust_ext/src/multi_lang.rs](../../rust_ext/src/multi_lang.rs) 已实现 11 种语言的统一 `walk_node` + `SymbolRule`/`CallRule` 配置驱动框架。
- [rust_ext/src/lib.rs](../../rust_ext/src/lib.rs) 已暴露 `parse_file_lang` / `batch_parse_files_lang` / `batch_parse_files_lang_pool` / `supported_languages`。
- [db/db_build.py](../../db/db_build.py) 主路径只对 **C 语言** 接入了 `batch_parse_c_files_pool`（[L1145-L1166](../../db/db_build.py#L1145-L1166)），其余 10 种语言仍走 Python `ProcessPoolExecutor`。
- Rust 扩展不可用时回退 Python parser（`_can_use_rust_parse()`）。
- 环境变量 `CW_DISABLE_RUST_PARSE` 可强制关闭 Rust 路径。

### 2.2 改动范围

#### 2.2.1 db_build.py 主路径改造

当前 [L1114-L1198](../../db/db_build.py#L1114-L1198) 的"C 语言专用 Rust 接入"扩展为"多语言 Rust 接入"：

```python
# 伪代码：替换 L1114-L1198
to_parse.sort(key=lambda x: (x[3], x[0]))  # (lang, idx)

# v7 P1#4 修复：不要在 _can_use_rust_parse() 之前从 non_rust_files 中移除文件。
# 原伪代码先把已放行语言移入 rust_files，扩展不可用时这批文件既不走 Rust 也不在
# non_rust_files 中，最终消失。v7 改为：non_rust_files 初始包含全部文件，
# 只有 Rust 实际可用时才把放行语言移出 non_rust_files。Rust 失败的文件回退 non_rust_files。

# v6 P1#5: 逐语言放行门（RUST_PARSE_ENABLED_LANGS）
# 环境变量为逗号分隔的语言列表（如 "python,c,rust"）。
# 未设置时默认空 → 通用多语言 Rust 路径全部走 Python parser（最安全）。
# 注意：C 语言专用快路径（batch_parse_c_files_pool）独立保留，不受此环境变量影响。
# 放行条件：该语言 alignment tests 零关键差异（kind/signature/visibility 对齐通过），
# 且"整种语言零符号"不进可接受白名单（TypeScript 当前 Rust parser 提取 0 符号，不放行）。
enabled_langs = set(
    os.environ.get("RUST_PARSE_ENABLED_LANGS", "").split(",")
) - {""}

# v7 P1#4: non_rust_files 初始包含全部文件
non_rust_files = list(to_parse)
rust_failed_files = []  # Rust parse 失败的文件，回退 Python

# Rust 路径：按语言分组调用 batch_parse_files_lang_pool
# v7 P1#4: 只有 _can_use_rust_parse() 成功时才从 non_rust_files 移出文件
if _can_use_rust_parse() and not os.environ.get("CW_DISABLE_RUST_PARSE"):
    rust_langs = set(supported_languages()) & enabled_langs  # 交集：Rust 支持且已放行
    rust_files = [x for x in to_parse if x[3] in rust_langs]
    # 从 non_rust_files 中移除将走 Rust 的文件
    rust_rel_paths = {x[1] for x in rust_files}
    non_rust_files = [x for x in non_rust_files if x[1] not in rust_rel_paths]

    # 按 language 分组
    by_lang = defaultdict(list)
    for entry in rust_files:
        by_lang[entry[3]].append(entry)

    for lang, files in by_lang.items():
        # 资源文件预过滤（沿用现有 _is_resource_file）
        # 注意：to_parse 是六元组 (idx, rel_path, abs_path, lang, module_path, file_instance_id)
        filtered = []
        for _idx, rel_path, abs_path, _lang, module_path, file_instance_id in files:
            is_res, reason = _is_resource_file(abs_path)
            if is_res:
                skipped += 1
                failed_files.append((rel_path, f"skip_resource:{reason}"))
                continue
            filtered.append((abs_path, module_path, rel_path, file_instance_id))

        rust_args = [(abs_path, module_path) for abs_path, module_path, _, _ in filtered]
        try:
            pool = batch_parse_files_lang_pool(rust_args, lang, num_threads=mp_workers)
        except Exception as pool_err:
            # v7 P1#4: pool 异常 → 整组回退 Python
            rust_failed_files.extend(files)
            failed_files.append((f"<pool:{lang}>", str(pool_err)))
            continue

        # 流式回传：逐个 get_at 转 dict 写入 file_results
        for i, (abs_path, module_path, rel_path, file_instance_id) in enumerate(filtered):
            r = pool.get_at(i)
            if r.get("error"):
                # v7 P1#4: 单文件 error → 该文件回退 Python（不是整组丢弃）
                rust_failed_files.append((None, rel_path, abs_path, lang, module_path, file_instance_id))
                failed_files.append((rel_path, r["error"]))
                continue
            r["abs_path"] = abs_path
            r["file_instance_id"] = file_instance_id
            r["module_path"] = module_path
            r["rel_path"] = rel_path
            r.setdefault("inline_modules", [])
            file_results[rel_path] = r

# v7 P1#4: Rust 失败的文件 + 未放行语言 → 走原 Python ProcessPoolExecutor
python_files = non_rust_files + rust_failed_files
if python_files:
    _python_multiprocess_parse(python_files, mp_workers, file_results, ...)
```

**v7 P1#4 关键修复**：
- **文件不会消失**：`non_rust_files` 初始包含全部文件，只有 Rust 实际可用且放行时才移出。Rust pool 异常或单文件 error 的文件回退 `non_rust_files`，最终走 Python fallback。
- **C 快路径独立保留**：`batch_parse_c_files_pool`（C 语言专用）不受 `RUST_PARSE_ENABLED_LANGS` 影响，已在 [db_build.py L1145-L1166](../../db/db_build.py#L1145-L1166) 稳定运行。上面的伪代码是**通用多语言 Rust 路径**，与 C 快路径并行。
- **测试覆盖**：需要补"语言已放行但扩展不可用"、"pool 异常回退"、"单文件 error 回退"的集成测试。

关键点：
- **保留 `batch_parse_c_files_pool`** 作为 C 语言专用快路径（已稳定，不破坏，不受 `RUST_PARSE_ENABLED_LANGS` 影响）。
- **新增 `batch_parse_files_lang_pool`** 作为多语言通用路径。
- **小批量用 `parse_file_lang`（单文件）**：文件数 < 阈值时（如 < 50），用单文件 Rust parse 避免线程池开销。大批量（≥ 50）用 `batch_parse_files_lang_pool`。Phase 1.1 实现中已加此阈值，但需补集成测试覆盖小批量路径（v4 Phase 1.1 复核修复）。
- 资源文件预过滤（`_is_resource_file`）仍在 Python 侧执行，避免 Rust 读取大文件。
- **失败 fallback（v7 P1#4 修正）**：Rust 扩展不可用 → `non_rust_files` 保持全部文件走 Python；pool 异常 → 整组回退 Python；单文件 error → 该文件回退 Python。三种情况都不会丢文件。需补集成测试覆盖"语言已放行但扩展不可用"、"pool 异常回退"、"单文件 error 回退"。

#### 2.2.2 Alignment Tests（Counter 多重集合比较 + Counter 相减）

新增 `tests/test_rust_python_alignment.py`（v5 P2 修复：Counter 相减白名单）：

```python
# 对每种语言，准备 5-10 个样本文件，覆盖：
# - 函数/方法/类/结构体/枚举/interface/trait 等所有 symbol kind
# - 嵌套定义（类内方法、impl 块内函数）
# - 调用表达式（普通调用、方法调用、链式调用）
# - import/include 声明
# - 含语法错误的文件（error 字段非空）

from collections import Counter

# v5 P2: 已知差异用 Counter 表示（精确到每个 tuple 的允许次数）
# 格式: Dict[lang, (reason, Counter_of_known_diffs)]
# 断言逻辑：actual_diff - known_diffs == empty（剩余差异必须为零）
#   Counter 相减只减去允许的数量，不会一次放过某 key 的全部差异次数
#   （`del Counter[key]` 的缺陷），也比 `diff_rate < threshold` 更精确——
#   只有显式记录的差异才被允许，任何新差异都会失败。
KNOWN_SYMBOL_DIFFS: dict[str, tuple[str, Counter]] = {
    "typescript": (
        "Rust TypeScript parser 未提取任何符号，Phase 1.4 待修复",
        Counter({
            ("User", 3, 9): 1, ("add", 11, 13): 1,
            ("constructor", 4, 4): 1, ("greet", 6, 8): 1,
            ("main", 15, 20): 1,
        }),
    ),
    "php": (
        "Rust 不提取 PHP property 符号，Phase 1.4 待修复",
        Counter({("value", 7, 7): 1}),
    ),
    # ... 其余已知差异语言
}

def normalize_symbols(symbols):
    """按 (name, start_line, end_line) 归一化为 Counter 多重集合。

    用 Counter 而非 dict：同一行可能有多个相同调用（如 foo(); foo();），
    dict 会吞掉重复，Counter 保留多重性。
    不比较 qualified_name：Rust/Python 的 module_path 策略不同（投影差异）。
    """
    return Counter(
        (s["name"], s["start_line"], s["end_line"])
        for s in symbols
    )

def subtract_known_diffs(diff_counter, known_diffs):
    """从实际 diff 中减去已知差异，返回剩余差异。

    v5 P2 修复：用 Counter 相减而非 `del Counter[key]` 或 `diff_rate < threshold`。
    Counter 相减只减去允许的数量，如果实际差异超过已知数量，剩余非空 → 测试失败。
    """
    return diff_counter - known_diffs

@pytest.mark.parametrize("lang", [
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
])
def test_symbol_alignment(lang, sample_files):
    """Rust parser 与 Python parser 输出的 symbols 核心字段一致。

    断言逻辑：actual_diff - known_diffs == empty
    - 已知差异从 KNOWN_SYMBOL_DIFFS 中减去
    - 剩余差异必须为零（任何未知差异都会失败）
    """
    for path in sample_files[lang]:
        py_result = python_parser.parse_file(path, module_path="test.align")
        rs_result = parse_file_lang(path, module_path="test.align", lang=lang)

        py_syms = normalize_symbols(py_result["symbols"])
        rs_syms = normalize_symbols(rs_result["symbols"])

        # Counter 减法：找出多/少的条目（保留多重性）
        missing_in_rs = py_syms - rs_syms   # Python 有但 Rust 没有
        missing_in_py = rs_syms - py_syms   # Rust 有但 Python 没有
        diff_counter = missing_in_rs + missing_in_py  # 合并总差异

        # v5 P2: 从实际差异中减去已知差异（Counter 相减，保留多重性）
        known = Counter()
        if lang in KNOWN_SYMBOL_DIFFS:
            _, known = KNOWN_SYMBOL_DIFFS[lang]
        remaining = diff_counter - known

        assert not remaining, (
            f"[{lang}] {path}: symbol 对齐发现未知差异（已知差异已减去）\n"
            f"  剩余未知差异: {dict(remaining)}\n"
            f"  missing_in_rs: {dict(missing_in_rs)}\n"
            f"  missing_in_py: {dict(missing_in_py)}\n"
            f"  提示: 若为新增已知差异，请更新 KNOWN_SYMBOL_DIFFS[{lang!r}]"
        )

        # raw_calls 也纳入对齐（同样 Counter 相减逻辑）
        # ... 见 KNOWN_CALL_DIFFS
```

断言逻辑（v5 P2 修复）：
- **已知差异用 Counter 表示**，从实际 diff Counter 中**相减**，要求**剩余差异为零**。
- 不会一次放过某 key 的全部差异次数（`del Counter[key]` 的缺陷），也不允许任意比例的差异（`diff_rate < threshold` 的缺陷）。
- 只有显式记录的差异才被允许，任何新差异都会失败——迫使开发者更新白名单或修复 parser。
- Counters 由 `tests/_discover_alignment_diffs.py` 脚本发现后填入（运行 `python tests/_discover_alignment_diffs.py` 重新生成）。

#### 2.2.2b v6 P1#5 新增对齐测试（kind / signature / visibility）

**问题**：当前 alignment tests 只比较 `(name, start_line, end_line)`，未比较 `kind` / `signature` / `visibility`。测试注释声称有 `test_kind_alignment` 但实际不存在。函数签名是企业版核心需求（CAS 符号内容存 signature，查询结果直接返回给用户），必须对齐。

```python
@pytest.mark.parametrize("lang", [
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
])
def test_kind_alignment(lang, sample_files):
    """v6 P1#5: Rust 与 Python parser 的 symbol kind 必须一致。

    kind 决定符号在 UI 中的展示分类（function/struct/enum/class/interface 等），
    kind 不一致会导致用户看到错误的符号类型。
    """
    for path in sample_files[lang]:
        py_result = python_parser.parse_file(path, module_path="test.align")
        rs_result = parse_file_lang(path, module_path="test.align", lang=lang)

        # 按 (name, start_line, end_line) 配对，比较 kind
        py_map = {(s["name"], s["start_line"], s["end_line"]): s["kind"]
                  for s in py_result["symbols"]}
        rs_map = {(s["name"], s["start_line"], s["end_line"]): s["kind"]
                  for s in rs_result["symbols"]}

        common_keys = set(py_map) & set(rs_map)
        for key in common_keys:
            py_kind = py_map[key]
            rs_kind = rs_map[key]
            assert py_kind == rs_kind, (
                f"[{lang}] {path}: symbol {key} kind 不一致\n"
                f"  Python: {py_kind}  Rust: {rs_kind}"
            )


@pytest.mark.parametrize("lang", [
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
])
def test_signature_alignment(lang, sample_files):
    """v6 P1#5: Rust 与 Python parser 的 symbol signature 必须一致。

    signature 是企业版核心字段——CAS 存储并直接返回给用户查询。
    signature 不一致会导致 CAS 投影返回错误的函数签名。
    """
    for path in sample_files[lang]:
        py_result = python_parser.parse_file(path, module_path="test.align")
        rs_result = parse_file_lang(path, module_path="test.align", lang=lang)

        py_map = {(s["name"], s["start_line"]): s.get("signature", "")
                  for s in py_result["symbols"]}
        rs_map = {(s["name"], s["start_line"]): s.get("signature", "")
                  for s in rs_result["symbols"]}

        common_keys = set(py_map) & set(rs_map)
        for key in common_keys:
            assert py_map[key] == rs_map[key], (
                f"[{lang}] {path}: symbol {key} signature 不一致\n"
                f"  Python: {py_map[key]!r}  Rust: {rs_map[key]!r}"
            )


@pytest.mark.parametrize("lang", [
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
])
def test_visibility_alignment(lang, sample_files):
    """v6 P1#5: Rust 与 Python parser 的 symbol visibility 必须一致。

    visibility（public/private/protected）影响符号的查询过滤逻辑。
    """
    for path in sample_files[lang]:
        py_result = python_parser.parse_file(path, module_path="test.align")
        rs_result = parse_file_lang(path, module_path="test.align", lang=lang)

        py_map = {(s["name"], s["start_line"]): s.get("visibility", "")
                  for s in py_result["symbols"]}
        rs_map = {(s["name"], s["start_line"]): s.get("visibility", "")
                  for s in rs_result["symbols"]}

        common_keys = set(py_map) & set(rs_map)
        for key in common_keys:
            assert py_map[key] == rs_map[key], (
                f"[{lang}] {path}: symbol {key} visibility 不一致\n"
                f"  Python: {py_map[key]!r}  Rust: {rs_map[key]!r}"
            )
```

#### 2.2.2c v6 P1#5 RUST_PARSE_ENABLED_LANGS 灰度放行策略

**问题**：Alignment tests 通过 ≠ 可以切换默认 Rust。白名单允许 TypeScript"完全未提取符号"（Counter 记录了全部 5 个符号的差异），测试因此通过，但生产路径已将 TypeScript 分给 Rust → 写入空图谱。

**放行规则**：

| 条件 | 说明 |
|------|------|
| kind/signature/visibility 对齐通过 | `test_kind_alignment` / `test_signature_alignment` / `test_visibility_alignment` 零差异 |
| symbol/call 差异在白名单内 | `actual_diff - known_diffs == empty`（已有 Counter 相减） |
| **"整种语言零符号"不可接受** | Rust parser 对该语言提取 0 个符号 → **不进白名单**，不放行 |
| 差异属"投影差异"可显式批准 | 如 C++ namespace 这种结构性差异，注明原因后放行 |

**放行流程**：
1. 运行 `python tests/_discover_alignment_diffs.py` 确认当前差异。
2. 确认 `test_kind_alignment` / `test_signature_alignment` / `test_visibility_alignment` 零差异。
3. 确认该语言 Rust parser 提取的符号数 > 0（非"整种语言零符号"）。
4. 在 `RUST_PARSE_ENABLED_LANGS` 环境变量中加入该语言。
5. 生产环境逐步启用：先开发机测试，再推广到共享开发机。

**当前放行状态**（基于 alignment tests 结果）：

| 语言 | symbol 差异 | kind/signature/visibility | Rust 符号数 | 放行状态 |
|------|------------|---------------------------|-------------|---------|
| python | 0 | 需验证 | >0 | 待验证 |
| rust | 0 | 需验证 | >0 | 待验证 |
| go | 0 | 需验证 | >0 | 待验证 |
| java | 0 | 需验证 | >0 | 待验证 |
| javascript | 0 | 需验证 | >0 | 待验证 |
| ruby | 0 | 需验证 | >0 | 待验证 |
| scala | 0（call 有差异） | 需验证 | >0 | 待验证 |
| csharp | 0 | 需验证 | >0 | 待验证 |
| c | 0 | 已验证（C 专用快路径已稳定） | >0 | ✅ 已放行 |
| cpp | 1（namespace 投影差异） | 需验证 | >0 | 待验证 |
| php | 1（property 符号） | 需验证 | >0 | 待验证 |
| typescript | 5（**全部符号**） | N/A | **0** | ❌ 不放行（整种语言零符号） |

#### 2.2.3 性能验证

新增 `tests/bench_rust_vs_python_parse.py`：

| 场景 | 目标 |
|------|------|
| admin 项目（~5K 文件）parse 全量 | Rust 比 Python 快 ≥ 2x |
| firmware 75K 文件 parse 全量 | Rust 父进程 RSS < 1GB（Python 多进程 > 8GB） |
| 单文件增量 parse | Rust < 50ms |

### 2.3 不做的事

- **不删除 Python parser**：保留作为 fallback 和 alignment 基准。
- **不修改 `batch_parse_c_files_pool`**：C 语言专用快路径已稳定。
- **不动 schema**：Phase 1 只改 parse 路径，不改存储结构。
- **不做 CAS**：CAS 是 Phase 3A 的事。

### 2.4 风险与缓解

| 风险 | 缓解 |
|------|------|
| Rust parser 与 Python parser 结果不一致 | alignment tests 锁定差异（Counter 相减，剩余差异为零），已知差异进 `KNOWN_*_DIFFS` 清单 |
| Rust 扩展在某些环境编译失败 | 保留 Python fallback，`CW_DISABLE_RUST_PARSE` 可关闭 |
| tree-sitter grammar 版本差异 | Cargo.toml 锁定版本，alignment tests 持续监控 |
| Rust 多语言 parse 速度反而慢于 Python ProcessPool | benchmark 验证，不达标不切换默认路径 |

### 2.5 工作量预估

| 任务 | 工时 |
|------|------|
| db_build.py 主路径改造（多语言分组 + Rust 接入） | 1 天 |
| Alignment tests（11 语言 × 5-10 样本，归一化比较） | 2-3 天 |
| Benchmark 脚本 | 0.5 天 |
| Rust parser bug 修复（alignment 暴露的差异） | 1-2 天（不可控） |
| 文档更新 | 0.5 天 |
| **合计** | **5-7 天** |

---

## 3. Phase 3A 详细设计：CAS Parse Cache

### 3.0 Phase 3A.0：ParseFactV1 + ParseInputV1 ABI（v6 P1#2 + v7 P1#2/P1#3，Phase 3A 前置）

**问题**：
1. `cas_publish()` 要求 `local_id` / `local_qualified_name` / `lexical_parent_local_id` / `start_byte` / `end_byte` / `caller_local_id` / `call_ordinal`，但当前 Rust `SymbolInfo`（[lib.rs L48](../../rust_ext/src/lib.rs#L48)）和 `RawCall`（[lib.rs L65](../../rust_ext/src/lib.rs#L65)）没有这些字段。Python parser 也未输出。按当前代码，Phase 3A 会在 `sym["local_id"]` 处直接 KeyError。
2. **v7 P1#2 哨兵冲突**：v6 同时规定 `local_id` 从 0 开始、`lexical_parent_local_id=0` 表示"无父"。第一个符号（`local_id=0`）无法被引用为父节点——`lexical_parent_local_id=0` 到底指第一个符号还是"无父"？
3. **v7 P1#3 输入字节/编码 ABI 缺失**：v6 只规定了 `start_byte/end_byte`，没有规定偏移属于原始文件还是标准化 UTF-8。Python（[config.py L993](../../config.py#L993)）解码、标准化换行后再算 SHA-256；Rust（[multi_lang.rs L570](../../rust_ext/src/multi_lang.rs#L570)）直接解析原始字节并用 64-bit `DefaultHasher`。CRLF、GBK/Latin-1、BOM、UTF-16 文件的 CAS key、byte range 和符号内容可能完全不同。固件老代码场景尤其关键。

#### 3.0.1 ParseInputV1 ABI（v7 P1#3 新增）

**ParseInputV1 定义**：parser 的**输入**合约——在 parse 之前，如何把磁盘上的原始文件转换为 tree-sitter 可消费的 canonical bytes。

```python
PARSE_INPUT_ABI_VERSION = "v1"

# ParseInputV1: parser 输入合约（Rust + Python 必须一致执行）
#
# 1. canonical_bytes: 读取原始文件字节后，统一规范化为 UTF-8 编码的 bytes
#    - BOM: 去掉 UTF-8/UTF-16/UTF-32 BOM（如果存在）
#    - 编码检测: UTF-8 → 失败则尝试 chardetng → 失败则 latin-1（不丢失字节）
#    - 编码后: 统一为 UTF-8 bytes（tree-sitter 接受 &[u8]）
#
# 2. newline_policy: "lf"（统一为 \n）
#    - CRLF (\r\n) → LF (\n)
#    - CR (\r) → LF (\n)
#    - 规范化在 canonical_bytes 层完成，tree-sitter 看到的都是 \n
#
# 3. offset_coordinate_space: "canonical_utf8_bytes"
#    - start_byte/end_byte 基于规范化后的 UTF-8 bytes
#    - 不是原始文件字节偏移（原始可能有 CRLF/BOM/UTF-16）
#    - 不是 Unicode codepoint 偏移
#
# 4. content_hash: sha256(canonical_bytes) — 32 字节 hex（不用 DefaultHasher）
#    - Rust 侧必须用 sha2::Sha256，不用 std::hash::DefaultHasher
#    - Python 侧用 hashlib.sha256
#
# 5. total_lines: canonical_bytes 中 \n 的数量 + 1
```

**Rust 侧改动**（[rust_ext/src/multi_lang.rs](../../rust_ext/src/multi_lang.rs)）：

```rust
// v7 P1#3: Rust 侧必须规范化输入，不能用原始字节
fn read_canonical_bytes(abs_path: &str) -> Result<Vec<u8>, io::Error> {
    let raw = std::fs::read(abs_path)?;
    // 1. BOM 检测
    let (encoding, bytes_no_bom) = detect_encoding_and_strip_bom(&raw);
    // 2. 解码为 UTF-8
    let utf8_str = decode_to_utf8(bytes_no_bom, encoding)?;
    // 3. 换行规范化: CRLF/CR → LF
    let normalized: String = utf8_str.replace("\r\n", "\n").replace("\r", "\n");
    Ok(normalized.into_bytes())
}

// v7 P1#3: 必须用 SHA-256，不用 DefaultHasher
fn compute_content_hash(canonical: &[u8]) -> String {
    use sha2::{Sha256, Digest};
    let mut hasher = Sha256::new();
    hasher.update(canonical);
    format!("{:x}", hasher.finalize())
}
```

**Python 侧改动**（[config.py read_file_normalized](../../config.py#L993)）：当前已做 UTF-8 → latin-1 降级。v7 需补齐：BOM 剥离 + CRLF→LF 规范化 + 确保与 Rust 侧 `read_canonical_bytes` 输出字节一致。可参考 [TokenSlim-publish2 encoding_fallback](../../testcode/TokenSlim-publish2/src/core/encoding_fallback/mod.rs)（见 AGENTS.md 第 9 条）。

**ParseInputV1 版本纳入 CAS key**：input_abi_version 与 abi_version 一起纳入 CAS key，编码/换行策略变化后旧条目自然失效。

#### 3.0.2 ParseFactV1 ABI（v6 P1#2 + v7 P1#2 修正）

**ParseFactV1 定义**：Rust/Python parser 统一输出的"解析事实"合约，版本号纳入 CAS key。

```python
PARSE_FACT_ABI_VERSION = "v1"

# SymbolInfo 新增字段（在现有 name/kind/start_line/end_line/... 基础上）：
#   local_id: int              — v7 P1#2: 文件内符号序号（从 1 开始，按 start_line/start_col 排序）
#                                  0 保留给 synthetic module symbol（可选，用于挂载顶层裸调用）
#   local_qualified_name: str  — 内容级限定名（含 lexical parent chain，不含 module_path）
#                                  如 "Parser.tokenize"（Parser 是父，tokenize 是子）
#   lexical_parent_local_id: Option<u32> / SQL NULL
#                              — v7 P1#2: 词法父符号的 local_id
#                                NULL = 顶层符号（无词法父节点）
#                                非 NULL 时引用同一 cas_key 的 local_symbol_id
#                                不再用 0 作为"无父"哨兵，避免与 local_id=0 冲突
#   start_byte: int            — 符号在 canonical_bytes 中的字节偏移（tree-sitter node start_byte）
#                                  v7 P1#3: 基于 ParseInputV1 规范化后的 UTF-8 bytes
#   end_byte: int              — 符号在 canonical_bytes 中的字节偏移（tree-sitter node end_byte）

# RawCall 新增字段（在现有 callee_name/caller_name/call_line/... 基础上）：
#   caller_local_id: Option<u32> / SQL NULL
#                              — v7 P1#2: 调用者符号的 local_id
#                                NULL = 顶层裸调用（无词法容器，如模块级 foo() 不在任何函数内）
#                                非 NULL 时关联 cas_symbols.local_symbol_id
#   call_ordinal: int          — 同行调用序号（0-based，同一 caller_local_id 同一 call_line 的第 N 个调用）
```

**v7 P1#2 哨兵值设计决策**：
- `local_id` 从 **1** 开始（0 保留给 synthetic module symbol，用于挂载顶层裸调用）。
- `lexical_parent_local_id` 用 **NULL**（`Option<u32>` / SQL `NULL`）表示"无父节点"，不用 0。
- `caller_local_id` 用 **NULL** 表示"顶层裸调用"（无词法容器）。顶层裸调用仍保留在 `cas_raw_calls` 中，`caller_name` 为 `"__module__"`（synthetic）或源码中实际出现的表达式。
- SQLite `UNIQUE` 约束对 NULL 视为 distinct，所以多个 `caller_local_id=NULL` 的同行同 callee 调用靠 `call_ordinal` 区分。

**Rust 侧改动**（[rust_ext/src/lib.rs](../../rust_ext/src/lib.rs)）：

```rust
pub struct SymbolInfo {
    // ... 现有字段 ...
    pub local_id: u32,                        // v7 P1#2: 文件内序号（从 1 开始）
    pub local_qualified_name: String,         // v6 P1#2: 内容级限定名
    pub lexical_parent_local_id: Option<u32>, // v7 P1#2: 词法父 local_id（None=顶层）
    pub start_byte: u32,                      // v7 P1#3: canonical UTF-8 bytes 偏移
    pub end_byte: u32,                        // v7 P1#3: 同上
}

pub struct RawCall {
    // ... 现有字段 ...
    pub caller_local_id: Option<u32>,          // v7 P1#2: 调用者 local_id（None=顶层裸调用）
    pub call_ordinal: u32,                    // v6 P1#2: 同行调用序号
}
```

**Python 侧改动**：各语言 parser 的 `parse_file()` 返回 dict 中增加上述字段。`local_id` 在遍历 AST 时按 `start_line, start_col` 排序从 1 开始赋值；`lexical_parent_local_id` 从 AST 父节点追溯（顶层为 `None`）；`start_byte`/`end_byte` 从 tree-sitter node 直接取（基于 canonical bytes）。

**`local_qualified_name` 构造规则**：
- 顶层符号：`local_qualified_name = name`（如 `"parse_file"`）
- 嵌套符号：`local_qualified_name = parent.local_qualified_name + "." + name`（如 `"Parser.tokenize"`）
- `qualified_name`（workspace 级）= `module_path + "." + local_qualified_name`（由 `workspace_symbols` 在 refresh 时生成）

**`call_ordinal` 构造规则**：
- 同一 `caller_local_id`（含 NULL）+ 同一 `call_line` 的多个调用，按 AST 出现顺序赋 0, 1, 2, ...
- 保证 `UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)` 不会吞掉同行重复调用

#### 3.0.3 统一 CAS key 计算（v7 P2#1：唯一版本化函数）

**v7 P2#1 问题**：§3.0 定义了 `compute_cas_key`，§3.4 又重复定义了一个漏掉 `abi_version` 的版本。实施者按 §3.4 编码会在 ABI 升级后错误命中旧条目。

**v7 修复**：全文档**唯一**的 CAS key 计算函数，包含 `abi_version` + `input_abi_version`：

```python
def compute_cas_key_v1(content_hash, language, parser_version, callwarden_version,
                       extraction_config_version, abi_version, input_abi_version):
    """v7 P2#1: 全文档唯一的 CAS key 计算函数。
    包含 abi_version（ParseFactV1）和 input_abi_version（ParseInputV1）。
    任一版本升级后旧 CAS 条目自然失效，不会被错误命中。
    """
    raw = (f"{content_hash}|{language}|{parser_version}|{callwarden_version}|"
           f"{extraction_config_version}|{abi_version}|{input_abi_version}")
    return hashlib.sha256(raw.encode()).hexdigest()
```

> **注意**：文档中所有引用 `compute_cas_key` 的地方（§3.4、§3.9 冷启动、refresh 伪代码、验收测试）必须统一调用此函数。**不得在任何地方重复定义简化版本。**

**Alignment tests 增加 ABI 字段对齐**：

```python
def normalize_symbols_v1(symbols):
    """v6 P1#2 + v7 P1#2: 含 local_id / parent / byte range 的归一化"""
    return Counter(
        (s["name"], s["local_id"], s.get("lexical_parent_local_id"),  # None=顶层
         s["start_line"], s["end_line"], s["start_byte"], s["end_byte"])
        for s in symbols
    )

def normalize_calls_v1(calls):
    """v6 P1#2 + v7 P1#2: 含 caller_local_id / call_ordinal 的归一化"""
    return Counter(
        (c["callee_name"], c["call_line"], c.get("caller_local_id"),  # None=顶层裸调用
         c["call_ordinal"])
        for c in calls
    )
```

**实施步骤**：
1. Rust 实现 `read_canonical_bytes`（BOM + 编码检测 + CRLF→LF），替换 [multi_lang.rs L570](../../rust_ext/src/multi_lang.rs#L570) 的原始字节读取。
2. Rust `content_hash` 改用 `sha2::Sha256`，替换 `DefaultHasher`。
3. Rust `SymbolInfo` / `RawCall` 增加字段（`local_id` 从 1 开始，parent/caller 用 `Option<u32>`），`walk_node` 赋值。
4. Python `read_file_normalized` 补齐 BOM 剥离 + CRLF→LF，确保与 Rust `read_canonical_bytes` 输出字节一致。
5. Python 各 parser 增加 `local_id`（从 1 开始）/ parent / byte range 输出。
6. `_normalize_rust_symbols` 补齐缺失字段的默认值（向后兼容）。
7. Alignment tests 增加 `test_abi_field_alignment`（local_id / parent / byte range / call_ordinal / input_canonical_bytes 一致性）。
8. CAS key 统一调用 `compute_cas_key_v1`，删除所有重复定义。

### 3.1 核心思想（只承诺减少重复 parse）

**Phase 3A 只做一件事**：相同文件内容（同 `cas_key`）在**同一用户**的多个 workspace 中只 parse 一次，结果存入 CAS 表。

**Phase 3A 不做**：
- ❌ 不改查询路径（查询仍走 `symbols`/`calls` 表，CAS 命中后同步写入 `symbols`/`calls`）。
- ❌ 不共享 `resolved_edges`（每 workspace 独立解析）。
- ❌ 不做 snapshot 级 thin DB 复用。
- ❌ 不做跨用户授权查询。

### 3.2 CAS Schema 设计（v4：自包含 + 内容级/路径级分离）

**核心原则**：
- CAS 只存**局部语法事实**（tree-sitter 从文件内容直接提取，不含路径信息）。
- CAS 是**自包含**的：命中后可独立回填 workspace 的 `symbol_contents` + `symbols` + `calls`，不依赖 workspace DB 中的任何表。
- 路径相关字段（`qualified_name` / `module_path`）由 `workspace_symbols` 统一身份表在 refresh 时生成。

CAS 表存放在 **Local CAS DB**（`~/.callwarden/cas.db`），`workspace_symbols` 表存放在 **workspace DB**。

#### 3.2.1 CAS 表（Local CAS DB，内容级，自包含，跨 workspace 共享）

```sql
-- CAS schema 版本管理（v4 P2 修复：跨版本/容器兼容）
CREATE TABLE IF NOT EXISTS cas_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 初始化：cas_schema_version=1, min_reader_version=1

-- CAS 主表：按文件内容 hash 存储单文件解析结果
CREATE TABLE IF NOT EXISTS cas_file_cache (
    cas_key TEXT PRIMARY KEY,          -- v7 P2#1: compute_cas_key_v1() 统一计算（含 abi_version + input_abi_version）
    content_hash TEXT NOT NULL,        -- 文件内容 hash（用于快速查找）
    language TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL,
    callwarden_version TEXT NOT NULL,
    extraction_config_version TEXT NOT NULL,
    abi_version TEXT NOT NULL,         -- v7 P2#1: ParseFactV1 ABI 版本（从 §3.0 compute_cas_key_v1 纳入）
    input_abi_version TEXT NOT NULL,   -- v7 P1#3: ParseInputV1 ABI 版本（编码/换行/offset 坐标系版本）
    state TEXT NOT NULL DEFAULT 'ready',  -- building / ready（P1#5 原子发布）
    parsed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cas_content_hash ON cas_file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_language ON cas_file_cache(language);

-- CAS 符号内容表（v5 P1#5 修复：byte range 移到 cas_symbols，正文表只存 content_hash + content）
CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,      -- 符号正文内容 hash
    content TEXT NOT NULL              -- 符号正文文本
);

-- CAS 符号表：内容级符号事实（不含路径信息）
-- qualified_name / module_path 已移除，由 workspace_symbols 生成
-- v5 P1#5: start_byte/end_byte 从 cas_symbol_contents 移到此处（byte range 是文件位置，非正文属性）
CREATE TABLE IF NOT EXISTS cas_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    local_symbol_id INTEGER NOT NULL,  -- v7 P1#2: 文件内序号，从 1 开始（0 保留给 synthetic module symbol）
    symbol_content_hash TEXT NOT NULL,  -- 关联 cas_symbol_contents（CAS DB 内部 FK，自包含）
    name TEXT NOT NULL,                -- 短名（如 "parse_file"）
    local_qualified_name TEXT NOT NULL, -- 内容级限定名（如 "Parser.tokenize"，含 lexical parent chain，不含 module_path）
    lexical_parent_local_id INTEGER DEFAULT NULL, -- v7 P1#2: 词法父符号的 local_symbol_id（NULL = 顶层符号；非 NULL 时引用同一 cas_key 的 local_symbol_id）
    kind TEXT NOT NULL,                -- fn / class / method / struct ...
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    start_byte INTEGER DEFAULT 0,      -- v5 P1#5: 符号在文件中的字节偏移（从 cas_symbol_contents 移来）
    end_byte INTEGER DEFAULT 0,        -- v5 P1#5: 同上
    visibility TEXT DEFAULT 'private',
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    FOREIGN KEY (symbol_content_hash) REFERENCES cas_symbol_contents(content_hash),
    UNIQUE(cas_key, local_symbol_id)   -- 稳定唯一键
);

CREATE INDEX IF NOT EXISTS idx_cas_symbols_key ON cas_symbols(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_symbols_name ON cas_symbols(name);

-- CAS raw calls：内容级原始调用文本
CREATE TABLE IF NOT EXISTS cas_raw_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    caller_local_id INTEGER DEFAULT NULL, -- v7 P1#2: 关联 cas_symbols.local_symbol_id（NULL = 顶层调用，无词法容器，如模块级裸调用）
    caller_name TEXT NOT NULL,         -- 调用者短名
    callee_name TEXT NOT NULL,         -- 被调用文本（如 "parse", "Parser.tokenize"）
    call_line INTEGER NOT NULL,
    call_ordinal INTEGER DEFAULT 0,    -- v4 P2 修复：同行调用序号，避免 UNIQUE 吞掉同行重复调用
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)  -- 含 call_ordinal
);

CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_key ON cas_raw_calls(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_caller ON cas_raw_calls(caller_name);

-- CAS imports：内容级 import/include 声明
CREATE TABLE IF NOT EXISTS cas_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    import_path TEXT NOT NULL,
    import_kind TEXT DEFAULT 'import',
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, import_path, import_kind)
);

CREATE INDEX IF NOT EXISTS idx_cas_imports_key ON cas_imports(cas_key);

-- v7 P1#1: CAS pending refs——CAS 已提交但 workspace manifest 尚未提交时的引用保护
-- GC 将 pending refs 中的 key 视为 live，避免在窗口期内删除 CAS 条目
-- TTL（expires_at）保证 pending ref 不会永久残留
CREATE TABLE IF NOT EXISTS cas_pending_refs (
    cas_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,          -- Unix timestamp，过期后 GC 清理
    created_at REAL NOT NULL,
    PRIMARY KEY (cas_key, workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_cas_pending_expires ON cas_pending_refs(expires_at);
```

**v4 P1#1 自包含说明 + v5 P1#5 修正**：
- `cas_symbol_contents` 只存 `content_hash + content`（v5 修正：byte range 移到 `cas_symbols`，因为相同正文出现在不同文件偏移不同）。
- `cas_symbols.start_byte/end_byte` 记录符号在文件中的字节偏移。
- CAS 命中后，从 `cas_symbol_contents` 复制到 workspace 的 `symbol_contents`，从 `cas_symbols` + path 信息生成 `workspace_symbols` + `symbols`。
- 不再有任何跨库 FK（`symbol_content_hash` 指向 CAS DB 内部的 `cas_symbol_contents`）。
- `cas_symbols.local_qualified_name`：内容级限定名（如 `Parser.tokenize`），基于 lexical parent chain 拼接。workspace 级 `qualified_name` = `module_path + "." + local_qualified_name`（如 `my_module.Parser.tokenize`）。

#### 3.2.2 Workspace Symbols 统一身份表（v4 P1#3 修复：消除 ID 碰撞）

`workspace_symbols` 是 clean/dirty 的**统一身份表**，`workspace_resolved_edges` 只引用 `workspace_symbols.id`：

```sql
-- Workspace symbols 统一身份表（clean 和 dirty 都投影到此表）
CREATE TABLE IF NOT EXISTS workspace_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,            -- workspace 内相对路径
    local_symbol_id INTEGER NOT NULL,   -- 文件内序号
    cas_key TEXT DEFAULT NULL,          -- clean: 指向 CAS；dirty: NULL
    symbols_rowid INTEGER DEFAULT NULL, -- dirty: 指向 symbols.id；clean: NULL
    name TEXT NOT NULL,                 -- 短名
    local_qualified_name TEXT NOT NULL, -- 内容级限定名（从 cas_symbols 复制，或 dirty 时从 symbols 生成）
    qualified_name TEXT NOT NULL,       -- workspace 级限定名 = module_path + "." + local_qualified_name
    module_path TEXT DEFAULT '',        -- 由 rel_path 推导
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'cas', -- 'cas' = clean 投影，'dirty' = dirty 文件
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    UNIQUE(workspace_id, id),           -- v5 P2: 组合唯一约束，供 workspace_resolved_edges 组合 FK 引用
    UNIQUE(workspace_id, rel_path, local_symbol_id, source)
);

CREATE INDEX IF NOT EXISTS idx_ws_sym_workspace ON workspace_symbols(workspace_id);
CREATE INDEX IF NOT EXISTS idx_ws_sym_qname ON workspace_symbols(workspace_id, qualified_name);
CREATE INDEX IF NOT EXISTS idx_ws_sym_rel_path ON workspace_symbols(workspace_id, rel_path);
CREATE INDEX IF NOT EXISTS idx_ws_sym_cas_key ON workspace_symbols(workspace_id, cas_key);
```

**为什么用统一身份表**：
- `workspace_resolved_edges.caller_symbol_id` 和 `callee_symbol_id` 只引用 `workspace_symbols.id`，**不会 ID 碰撞**（clean 和 dirty 不共用 ID 空间的问题消失）。
- `workspace_symbols.qualified_name` = `module_path + "." + local_qualified_name`，`A.run` 和 `B.run` 的 `local_qualified_name` 都是 `run`，但 workspace 级 `qualified_name` 分别是 `A.run` / `B.run`，不碰撞。
- [multi_lang.rs L690](../../rust_ext/src/multi_lang.rs#L690) 的 Rust parser 用 `module_path` 构造 `qualified_name`，`module_path` 在 workspace 侧生成。

### 3.3 Workspace Manifest 设计（clean/dirty 分离）

现有 `workspaces` 表保留。`workspace_manifests` 表的 `cas_key` **允许 NULL**，dirty 文件不指向 CAS：

```sql
-- Workspace manifest：记录每个 workspace 包含哪些文件
-- clean 文件指向 CAS（cas_key 非空），dirty 文件独立存储（cas_key NULL）
CREATE TABLE IF NOT EXISTS workspace_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    cas_key TEXT DEFAULT NULL,           -- clean: 指向 CAS DB 的 cas_file_cache；dirty: NULL
    content_hash TEXT NOT NULL,         -- 文件内容 hash（clean 和 dirty 都填）
    is_dirty INTEGER DEFAULT 0,         -- 1 = dirty overlay，独立 parse
    mtime REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    -- v6 P1#1: 删除跨库 FK（cas_file_cache 在独立 CAS DB，普通 SQLite FK 无法跨库引用）
    -- cas_key 的一致性由应用层保证：refresh 写入前查 CAS DB 确认 cas_key 存在
    UNIQUE(workspace_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_manifest_workspace ON workspace_manifests(workspace_id);
CREATE INDEX IF NOT EXISTS idx_manifest_cas_key ON workspace_manifests(cas_key);
CREATE INDEX IF NOT EXISTS idx_manifest_dirty ON workspace_manifests(is_dirty);
```

**关键修正**（针对评审 P2）：
- `cas_key` 允许 NULL，dirty 文件 `cas_key = NULL`，不产生无效 FK。
- 不再有 `dirty_content_hash` 字段（dirty 文件的 content_hash 已存在 `content_hash` 列）。
- Dirty 文件独立 parse 后写入**现有 `symbols`/`calls` 表**（不进 CAS），查询时 overlay。

### 3.3.1 Workspace DB FK 策略（v6 P1#1 新增）

**问题**：当前 [db_base.py L1614](../../db/db_base.py#L1614) 执行 `PRAGMA foreign_keys=OFF`（bulk insert 性能优化，避免每次 INSERT 触发引用完整性校验）。因此：
- `workspace_resolved_edges` 的组合 FK + `ON DELETE CASCADE` **不会在运行时生效**。
- `mark_file_dirty` 和 `refresh_clean_to_clean` 必须用**显式 DELETE** 维护 edge 一致性，不能依赖 CASCADE。

**v6 策略（应用层强制 + 迁移后校验）+ v7 P2#2 补充（edge 写入期约束）**：

| 层级 | 策略 | 说明 |
|------|------|------|
| Bulk refresh | `foreign_keys=OFF` | 保持现有性能优化，INSERT 不触发 FK 校验 |
| 应用层 DELETE | 显式 DELETE edge | `mark_file_dirty` / `refresh_clean_to_clean` 先删 edge 再删 symbol |
| 应用层 INSERT（v7 P2#2） | `INSERT ... SELECT` 约束 workspace_id | `workspace_resolved_edges` 写入时 caller/callee 的 workspace_id 由 SELECT 子查询约束，越权 edge 写不进去（见 §4.2.1） |
| 提交前局部检查（v7 P2#2） | edge workspace 一致性扫描 | 写事务 COMMIT 前校验本批次 edge 无 caller/callee workspace_id 不一致，不一致则 ROLLBACK |
| 迁移后校验 | `cw doctor --fk-check` | 临时 `foreign_keys=ON` + `PRAGMA foreign_key_check` + edge workspace 一致性扫描，报错则修复 |
| FK 声明 | 保留为**意图文档** | 声明组合 FK，但不声称运行时强制；校验靠 `doctor --fk-check` |

```python
def doctor_fk_check(ws_conn):
    """v6 P1#1: 迁移后 FK 完整性校验。

    在只读连接上临时启用 foreign_keys，运行 PRAGMA foreign_key_check。
    v7 P2#2: 增加 edge workspace 一致性校验（不依赖 foreign_keys=ON）。
    """
    # 用独立只读连接，不影响正在运行的 refresh
    ro_conn = sqlite3.connect(f"file:{ws_db_path}?mode=ro", uri=True)
    ro_conn.execute("PRAGMA foreign_keys=ON")
    violations = ro_conn.execute("PRAGMA foreign_key_check").fetchall()
    ro_conn.close()
    if violations:
        for table, rowid, fk_table, fk_id in violations:
            print(f"FK violation: {table} rowid={rowid} -> {fk_table} id={fk_id}")
        return False
    # v7 P2#2: edge workspace 一致性校验（见 §4.2.1）
    if not doctor_check_edge_workspace_consistency(ws_conn):
        return False
    return True
```

**为什么不全量启用 `foreign_keys=ON`**：
- Bulk refresh 单文件可能有数百个 symbol + 数千个 call edge，FK 校验每次 INSERT 都查父表，性能下降 20-30%。
- CAS DB 的 FK 是库内部的（`cas_symbols → cas_file_cache`），CAS 连接可以也应当启用 `foreign_keys=ON`（CAS 写入量小，原子发布单事务）。
- Workspace DB 保持 `OFF`，靠应用层显式 DELETE + `doctor --fk-check` 校验。

**CAS DB FK 策略**：CAS DB 连接启用 `PRAGMA foreign_keys=ON`（CAS 写入是原子发布，量小，FK 校验开销可接受）。

### 3.4 CAS Key 设计

> **v7 P2#1 修复**：本节曾重复定义 `compute_cas_key`（漏掉 `abi_version`），与 §3.0.3 冲突。已删除重复定义，全文档统一调用 §3.0.3 的 `compute_cas_key_v1()`。该函数同时包含 `abi_version`（ParseFactV1）和 `input_abi_version`（ParseInputV1），任一版本升级后旧条目自然失效。

CAS key 计算统一调用 [§3.0.3 `compute_cas_key_v1()`](#302-parsefactv1-abiv6-p12--v7-p12-修正)，不在此重复定义。

关键点：
- `content_hash` 是 **canonical bytes** 的 SHA-256（v7 P1#3：不是原始文件字节，见 §3.0.1 ParseInputV1）。
- `parser_version` 是 tree-sitter grammar 版本（如 `tree-sitter-c v0.24`）。
- `callwarden_version` 是 Call Warden 版本（如 `0.2.0-p29`）。
- `extraction_config_version` 是 SymbolRule/CallRule 配置版本（配置变更时手动 bump）。
- `abi_version` 是 ParseFactV1 ABI 版本（v7 P2#1：occurrence ID / parent / byte range / call ordinal 输出格式版本）。
- `input_abi_version` 是 ParseInputV1 ABI 版本（v7 P1#3：canonical bytes / 编码 / 换行 / offset 坐标系版本）。
- 升级 parser、配置、ABI 或输入规范化策略后，`cas_key` 改变，旧条目自然失效（不删除，等 GC 清理）。

### 3.5 Refresh 流程改造（P1#5 原子发布协议）

当前 [db_build.py build()](../../db/db_build.py#L621) 流程：

```text
1. 扫描文件 → to_parse 列表
2. 逐文件 parse（Python 或 Rust）
3. 写入 symbols/calls/file_versions
```

Phase 3A 改造后：

```text
1. 扫描文件 → to_parse 列表
2. 对每个文件：
   a. 计算 content_hash
   b. 计算 cas_key = compute_cas_key_v1(content_hash, lang, parser_version, cw_version, config_version, abi_version, input_abi_version)  # v7 P2#1: 统一调用 §3.0.3
   c. 查 cas_file_cache WHERE cas_key = ? AND state = 'ready'  ← 只认 ready 状态
   d. 命中 → 跳过 parse，从 CAS 复制到 workspace projection + symbols/calls
   e. 未命中 → CAS 原子发布协议（见下）→ 写 projection + symbols/calls
3. 写 workspace_manifests（clean: cas_key 非空；dirty: cas_key NULL + 走 step 2e 的独立 parse）
4. 解析 resolved_edges（依赖 build context，每 workspace 独立，不共享）
```

#### 3.5.1 CAS 原子发布协议（v5 P1#4 重写：四阶段 + busy retry）

**v4 问题**：`cas_publish()` 仍是 v3 写法，未写入 `cas_symbol_contents` / `symbol_content_hash` / `local_qualified_name` / `lexical_parent_local_id` / `call_ordinal` 等 v4 新增字段。当前伪代码无法生成一个满足 v4 schema 的 ready 条目。

**v5 修复**：发布流程改为"正文 payload → symbol occurrence → raw calls/imports → ready"四阶段，补齐全部 v4 字段，并加 busy retry 包装层。

```python
def cas_publish_with_retry(cas_key, parse_result, cas_conn, max_retries=3):
    """带 busy retry 的 CAS 发布包装层。

    CAS DB 可能被同 UID 的其他 CLI / hook / VS Code 进程占用，
    BEGIN IMMEDIATE 遇到 database is locked 时有界重试。
    """
    for attempt in range(max_retries):
        try:
            return _cas_publish_impl(cas_key, parse_result, cas_conn)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))  # 100ms, 200ms, 300ms
                # 重试前重新检查：可能其他进程已经发布了 ready 条目
                row = cas_conn.execute(
                    "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
                ).fetchone()
                if row and row["state"] == "ready":
                    return  # 其他进程已完成发布
                continue
            raise


def _cas_publish_impl(cas_key, parse_result, cas_conn):
    """原子发布 CAS 条目（单事务，崩溃安全）

    v5 P1#4: 四阶段写入——
    1. 正文 payload → cas_symbol_contents（按 content_hash 去重）
    2. symbol occurrence → cas_symbols（含 symbol_content_hash / local_qualified_name /
       lexical_parent_local_id / start_byte / end_byte 等 v4 全部新增字段）
    3. raw calls / imports → cas_raw_calls / cas_imports（含 call_ordinal）
    4. state=building → ready（最后发布）
    """
    cas_conn.execute("BEGIN IMMEDIATE")  # 获取写锁
    try:
        # 0. 检查是否已存在 ready 条目（并发同 key）
        row = cas_conn.execute(
            "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
        ).fetchone()
        if row and row["state"] == "ready":
            cas_conn.execute("ROLLBACK")
            return  # 已有 ready 条目，跳过

        # 1. 插父记录，state=building（不可被命中）
        content_hash = parse_result["content_hash"]
        language = parse_result["language"]
        cas_conn.execute(
            """INSERT OR IGNORE INTO cas_file_cache
               (cas_key, content_hash, language, parser_version,
                callwarden_version, extraction_config_version, state, parsed_at)
               VALUES (?, ?, ?, ?, ?, ?, 'building', ?)""",
            (cas_key, content_hash, language,
             parse_result.get("parser_version", ""),
             parse_result.get("callwarden_version", ""),
             parse_result.get("extraction_config_version", ""),
             time.time())
        )

        # 2. 正文 payload → cas_symbol_contents（按 content_hash 去重）
        for sym in parse_result["symbols"]:
            sym_content_hash = sym.get("content_hash") or compute_content_hash(sym.get("content", ""))
            cas_conn.execute(
                "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) VALUES (?, ?)",
                (sym_content_hash, sym.get("content", ""))
            )

        # 3. symbol occurrence → cas_symbols（含 v4 全部新增字段）
        for sym in parse_result["symbols"]:
            sym_content_hash = sym.get("content_hash") or compute_content_hash(sym.get("content", ""))
            cas_conn.execute(
                """INSERT OR IGNORE INTO cas_symbols
                   (cas_key, local_symbol_id, symbol_content_hash, name,
                    local_qualified_name, lexical_parent_local_id, kind,
                    start_line, end_line, start_col, end_col,
                    start_byte, end_byte, visibility, signature, has_comment, depth)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cas_key, sym["local_id"], sym_content_hash, sym["name"],
                 sym["local_qualified_name"],           # v4: 内容级限定名
                 sym.get("lexical_parent_local_id", 0), # v4: 词法父 ID
                 sym["kind"],
                 sym["start_line"], sym["end_line"],
                 sym.get("start_col", 0), sym.get("end_col", 0),
                 sym.get("start_byte", 0),              # v5 P1#5: 从 cas_symbol_contents 移来
                 sym.get("end_byte", 0),                # v5 P1#5: 同上
                 sym.get("visibility", "private"),
                 sym.get("signature", ""),
                 int(sym.get("has_comment", False)),    # v4: bool → int 归一化
                 sym.get("depth", -1))
            )

        # 4. raw calls → cas_raw_calls（含 call_ordinal）
        for call in parse_result.get("raw_calls", []):
            cas_conn.execute(
                """INSERT OR IGNORE INTO cas_raw_calls
                   (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cas_key, call["caller_local_id"], call["caller_name"],
                 call["callee_name"], call["call_line"],
                 call.get("call_ordinal", 0))           # v4 P2: 同行调用序号
            )

        # 5. imports → cas_imports
        for imp in parse_result.get("imports", []):
            import_path = imp["module"] if isinstance(imp, dict) else imp  # 兼容 List[str]/List[Dict]
            import_kind = imp.get("kind", "import") if isinstance(imp, dict) else "import"
            cas_conn.execute(
                """INSERT OR IGNORE INTO cas_imports
                   (cas_key, import_path, import_kind) VALUES (?, ?, ?)""",
                (cas_key, import_path, import_kind)
            )

        # 6. 最后发布：state=building → ready（同一事务）
        cas_conn.execute(
            "UPDATE cas_file_cache SET state = 'ready' WHERE cas_key = ?",
            (cas_key,)
        )
        cas_conn.execute("COMMIT")
    except Exception:
        cas_conn.execute("ROLLBACK")
        # v6 P2#1: 不再在 rollback 后执行 DELETE building。
        # ROLLBACK 已回滚整个事务（包括 step 1 的 INSERT building），
        # 再执行 DELETE 会触发隐式事务，可能报 "transaction within transaction"。
        # 孤儿 building 条目由 GC 的 building 清理逻辑处理（见 §3.8 子查询清理）。
        raise
```

**关键点**：
- **四阶段顺序**：正文 payload → symbol occurrence → raw calls/imports → ready。每阶段在同一事务内，全部成功后才 `state=ready`。
- `cas_symbol_contents` 按 `content_hash` 去重（`INSERT OR IGNORE`），相同正文只存一份。
- `cas_symbols.symbol_content_hash` 引用 CAS DB 内部的 `cas_symbol_contents`（自包含 FK）。
- `cas_symbols` 写入 `local_qualified_name` / `lexical_parent_local_id` / `start_byte` / `end_byte` 等 v4 新增字段。
- `cas_raw_calls` 写入 `call_ordinal`（v4 P2：同行调用序号）。
- `has_comment` 做 `bool → int` 归一化（Rust parser 返回 bool，Python 期望 int）。
- `imports` 兼容 `List[str]` 和 `List[Dict]` 两种格式（Rust 返回 str，Python 返回 Dict）。
- **busy retry 包装层**：`cas_publish_with_retry` 在 `database is locked` 时有界重试，重试前重新检查 `state=ready`（可能其他进程已完成发布）。
- `state=building` 的条目**不可被命中**（查询条件加 `WHERE state = 'ready'`）。
- 崩溃后 GC 清理 `state=building` 的孤儿条目（见 §3.8）。

收益：
- **CAS 命中时跳过 Rust parse**（最耗时的步骤）。
- **CAS 命中时仍写 `symbols`/`calls` 表**（保持查询路径不变，但写入是从 CAS 表复制，比 parse 快得多）。
- 50 个同 repo workspace，第一个全量 parse，后续 49 个只从 CAS 复制（命中率 ≥ 95%）。
- **崩溃安全**：`state=building` 条目不会被误判为命中。

### 3.6 Resolved Edges 处理（一律 per-workspace）

**关键修正**（针对评审 P1）：

- Phase 3A **不共享 `resolved_edges`**。
- `_resolve_calls_batch` 和 `_write_calls_db` 仍按 `workspace_id` 独立解析和存储。
- `resolved_edges` 依赖 build context（sysroot、include path、宏），在 toolchain fingerprint 闭合前共享不安全。
- Toolchain fingerprint 闭合是 Phase 6 的事，Phase 3A 不涉及。

### 3.7 多 Workspace 隔离改造（P1#4 修复：不只修 resolver）

**问题**：当前多处查询/加载**没有 `workspace_id` 过滤**，多 workspace 放进同一个 DB 会串库。仅修 resolver 不足以验收。

必须改造的 3 个位置：

#### 3.7.1 `_resolve_calls_batch`（db_build.py）

当前 [db_build.py L1757](../../db/db_build.py#L1757) 全表加载 `symbols`：

```python
cur = self.conn.execute("SELECT id, name, qualified_name, file_instance_id FROM symbols")
```

改为按 `workspace_id` 过滤：

```python
cur = self.conn.execute("""
    SELECT s.id, s.name, s.qualified_name, s.file_instance_id
    FROM symbols s
    JOIN file_instances fi ON s.file_instance_id = fi.id
    WHERE fi.workspace_id = ?
""", (workspace_id,))
```

#### 3.7.2 `get_callers` / `get_callees`（db_query.py）

当前 [db_query.py L270](../../db/db_query.py#L270) 的 SQL **没有 `workspace_id` 条件**：

```python
# 现状（有 qualified_name 分支）
cur = self.conn.execute(
    """SELECT c.*, s.name as caller_name, fi.rel_path as caller_file
       FROM calls c
       JOIN symbols s ON c.caller_id = s.id
       JOIN file_instances fi ON s.file_instance_id = fi.id
       JOIN symbols callee ON c.callee_id = callee.id
       WHERE c.callee_name = ? AND callee.qualified_name = ?
       ORDER BY fi.rel_path, c.call_line""",
    (callee_name, qualified_name),
)
```

改为加 `workspace_id` 过滤：

```python
# 修改后：加 fi.workspace_id 条件
cur = self.conn.execute(
    """SELECT c.*, s.name as caller_name, fi.rel_path as caller_file
       FROM calls c
       JOIN symbols s ON c.caller_id = s.id
       JOIN file_instances fi ON s.file_instance_id = fi.id
       JOIN symbols callee ON c.callee_id = callee.id
       WHERE fi.workspace_id = ? AND c.callee_name = ? AND callee.qualified_name = ?
       ORDER BY fi.rel_path, c.call_line""",
    (ws_id, callee_name, qualified_name),
)
```

`get_callees` 同理加 `JOIN file_instances fi` + `WHERE fi.workspace_id = ?`。

#### 3.7.3 GraphStore 加载（graph.rs）

当前 [graph.rs L153](../../rust_ext/src/graph.rs#L153) 加载**全部** symbols/calls，无 workspace 过滤：

```rust
let mut stmt = conn.prepare(
    "SELECT s.id, s.file_instance_id, s.kind, s.name, s.qualified_name,
            s.module_path, s.start_line, s.end_line, s.depth, fi.rel_path
     FROM symbols s
     JOIN file_instances fi ON s.file_instance_id = fi.id
     WHERE fi.status != 'archived'"
).map_err(...)?;
```

改为接收 `workspace_id` 参数：

```rust
let mut stmt = conn.prepare(
    "SELECT s.id, s.file_instance_id, s.kind, s.name, s.qualified_name,
            s.module_path, s.start_line, s.end_line, s.depth, fi.rel_path
     FROM symbols s
     JOIN file_instances fi ON s.file_instance_id = fi.id
     WHERE fi.status != 'archived' AND fi.workspace_id = ?1"
).map_err(...)?;

let symbol_iter = stmt.query_map([workspace_id], |row| { ... })?;
```

calls 加载同理加 `JOIN file_instances` + `WHERE fi.workspace_id = ?`。

**GraphStore 构造签名变更**：

```rust
// 现状：GraphStore::new(db_path, wal_checkpoint)
// 改后：GraphStore::new(db_path, wal_checkpoint, workspace_id)
pub fn new(db_path: &str, wal_checkpoint: bool, workspace_id: i64) -> PyResult<Self>
```

PyO3 绑定（[lib.rs](../../rust_ext/src/lib.rs)）同步更新，Python 侧 `_get_graph_store()` 传入当前 `workspace_id`。

这是 Phase 3A 的**必做项**，否则多 workspace 共享 DB 时：
- resolver 会把其他 workspace 的同名符号误匹配为 callee。
- GraphStore 会把其他 workspace 的调用边加载进 CSR 索引。
- `get_callers`/`get_callees` 会返回其他 workspace 的调用方。

### 3.8 GC 策略（v6 P1#3：GC 独占锁全程 + fail-closed + TOCTOU 修复）

**v5 遗留问题（TOCTOU）**：fail-closed 解决了"读失败后误删"，但事务外扫描与短事务删除之间仍有时间窗口：
1. GC 扫描 workspace A，未看到新 key。
2. refresh 发布该 key，并将其写入 A 的 manifest。
3. GC 根据旧 live set 删除该 key。
4. manifest 指向不存在的 CAS，查询缺数据。

**v6 修复**：GC 全程持有 CAS `BEGIN IMMEDIATE`（独占写锁），refresh 从 CAS lookup 到 manifest commit 也持 CAS 锁。二者互斥，消除 TOCTOU 窗口。

```python
def gc_cas(cas_conn, grace_period_days=7):
    """v6 P1#3: GC 全程持 CAS 独占锁（scan + sweep 原子），消除 TOCTOU。

    refresh 的 refresh_file_with_cas 也持 CAS BEGIN IMMEDIATE 从 lookup 到 manifest commit。
    二者通过 CAS 写锁互斥，busy_timeout=5000 协调。
    GC 是手动命令（cw gc cas），持续时间可接受（扫描 + 删除）。
    """
    import glob

    # 全程持有 CAS BEGIN IMMEDIATE（独占写锁）
    # refresh 的 cas_publish 也需要 BEGIN IMMEDIATE → busy_timeout 等待 GC 完成
    cas_conn.execute("BEGIN IMMEDIATE")
    try:
        # ===== 阶段 1：扫描所有 workspace DB（持锁状态下，fail-closed）=====
        cas_dir = os.path.expanduser("~/.callwarden")
        live_keys = set()
        now = time.time()
        grace_threshold = now - grace_period_days * 86400
        scanned_workspaces = []

        for ws_db_path in glob.glob(os.path.join(cas_dir, "*", "callwarden.db")):
            try:
                ws_conn = sqlite3.connect(f"file:{ws_db_path}?mode=ro", uri=True)
                ws_conn.row_factory = sqlite3.Row
                has_table = ws_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_manifests'"
                ).fetchone()
                if not has_table:
                    ws_conn.close()
                    continue
                # v6 P2: grace_threshold 真正参与 live set
                # 宽限期内（mtime > grace_threshold）的 workspace cas_key 必须加入 live set
                # 宽限期外的 workspace 也加入（保守策略：GC 不删任何被 manifest 引用的 key）
                # grace_threshold 仅影响是否对 workspace 发出"陈旧"警告，不影响 live set
                ws_mtime = os.path.getmtime(ws_db_path)
                rows = ws_conn.execute(
                    "SELECT DISTINCT cas_key FROM workspace_manifests WHERE cas_key IS NOT NULL"
                ).fetchall()
                live_keys.update(r["cas_key"] for r in rows)
                ws_conn.close()
                scanned_workspaces.append((ws_db_path, ws_mtime > grace_threshold))
            except Exception as e:
                # v6 fail-closed：任何 workspace 读取失败即中止 GC（rollback）
                cas_conn.execute("ROLLBACK")
                print(f"GC aborted: workspace DB {ws_db_path} read failed: {e}")
                print("不删除任何 CAS 条目。请确保所有 workspace DB 可读后重试。")
                return False

        # ===== 阶段 2：mark-sweep（同一事务内，先子表后正文表后父表）=====
        cas_conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gc_live (cas_key TEXT PRIMARY KEY)")
        cas_conn.execute("DELETE FROM _gc_live")
        cas_conn.executemany("INSERT OR IGNORE INTO _gc_live VALUES (?)",
                             [(k,) for k in live_keys])

        # 2a. 先删子表
        cas_conn.execute("DELETE FROM cas_symbols WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
        cas_conn.execute("DELETE FROM cas_raw_calls WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
        cas_conn.execute("DELETE FROM cas_imports WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")

        # 2b. 再删正文表（无符号引用的正文）
        cas_conn.execute("""
            DELETE FROM cas_symbol_contents
            WHERE content_hash NOT IN (SELECT DISTINCT symbol_content_hash FROM cas_symbols)
        """)

        # 2c. 最后删父表（只删 ready）
        cas_conn.execute("""
            DELETE FROM cas_file_cache
            WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live) AND state = 'ready'
        """)

        # 2d. 清理孤儿 building 条目（崩溃残留，先子表后父记录）
        cas_conn.execute("DELETE FROM cas_symbols WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        cas_conn.execute("DELETE FROM cas_raw_calls WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        cas_conn.execute("DELETE FROM cas_imports WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
        cas_conn.execute("DELETE FROM cas_symbol_contents WHERE content_hash NOT IN (SELECT DISTINCT symbol_content_hash FROM cas_symbols)")
        cas_conn.execute("DELETE FROM cas_file_cache WHERE state = 'building'")

        cas_conn.execute("DROP TABLE _gc_live")
        cas_conn.execute("COMMIT")
        stale = [p for p, active in scanned_workspaces if not active]
        print(f"GC complete: scanned {len(scanned_workspaces)} workspaces, "
              f"live keys = {len(live_keys)}, stale workspaces = {len(stale)}")
        return True
    except Exception:
        cas_conn.execute("ROLLBACK")
        raise
```

**refresh 侧配合（v6 P1#3 + v7 P1#1：parse 锁外 + flock + CAS 先提交 + pending_refs）**：

**v7 P1#1 问题**：v6 的 `refresh_file_with_cas` 把 parse 放在 CAS `BEGIN IMMEDIATE` 内，同 UID 并发刷新会长时间串行化（parse 耗时可能数秒），5 秒 `busy_timeout` 容易失效。而且 workspace DB 在 CAS DB 之前提交——进程在两次 COMMIT 之间崩溃，manifest 会指向已回滚或不存在的 CAS（SQLite WAL 下两个独立连接不能组成原子事务）。

**v7 修复**：
1. **parse 在锁外完成**——parse 耗时不可控，不持有任何 DB 锁。
2. **flock 协调 GC/refresh**——refresh 获取共享 `flock`（多个 refresh 可并行），GC 获取独占 `flock`（GC 期间所有 refresh 阻塞）。flock 是进程级文件锁，不占用 SQLite 连接。
3. **锁内重新检查 CAS**——获取 flock 后重新查 CAS，可能其他 refresh 已发布同一 key。
4. **CAS 先于 workspace 提交**——先短事务发布 CAS（COMMIT），再提交 workspace manifest。崩溃在两次提交之间时：CAS 多一个未引用条目（GC 清理），workspace manifest 未更新（下次 refresh 重做）——**不会出现 manifest 指向不存在 CAS**。
5. **cas_pending_refs 带 TTL**——CAS 发布后、manifest 提交前，写入 `cas_pending_refs(cas_key, workspace_id, expires_at)`。GC 将 pending key 视为 live，避免在窗口期内删除。manifest 提交后删除 pending ref（或等 TTL 自然过期）。

```python
import fcntl

CAS_FLOCK_PATH = os.path.expanduser("~/.callwarden/cas.flock")

def refresh_file_with_cas(workspace_id, rel_path, abs_path, module_path, lang,
                         ws_conn, cas_conn):
    """v7 P1#1: parse 锁外 → flock 共享锁 → CAS 短事务发布 → manifest 提交。

    崩溃安全性：
    - CAS 先提交，workspace 后提交。两次提交之间崩溃 → CAS 多一个未引用条目
      （GC 清理），manifest 未更新（下次 refresh 重做）。不会出现 manifest 指向
      不存在 CAS。
    - cas_pending_refs 保证 GC 在窗口期内不删除 CAS 条目。
    """
    # 1. 锁外读取文件 + 计算 cas_key（不持有任何锁）
    canonical_bytes = read_canonical_bytes(abs_path)  # v7 P1#3: ParseInputV1
    content_hash = hashlib.sha256(canonical_bytes).hexdigest()
    cas_key = compute_cas_key_v1(content_hash, lang, parser_version,
                                  callwarden_version, extraction_config_version,
                                  abi_version, input_abi_version)

    # 2. 锁外 parse（耗时操作，不持有 DB 锁）
    parse_result = None  # 延迟 parse：CAS 命中则不需要

    # 3. 获取共享 flock（与 GC 独占 flock 互斥）
    flock_fd = os.open(CAS_FLOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(flock_fd, fcntl.LOCK_SH)  # 共享锁，多个 refresh 可并行

        # 4. 锁内重新检查 CAS（可能其他 refresh 已发布）
        row = cas_conn.execute(
            "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
        ).fetchone()
        if row and row["state"] == "ready":
            pass  # 命中，跳过 parse + publish
        else:
            # 5. 未命中：parse（仍在 flock 内，但不在 CAS 事务内）
            if parse_result is None:
                parse_result = parse_file(abs_path, module_path, lang)

            # 6. 短事务发布 CAS（CAS 先提交）
            cas_conn.execute("BEGIN IMMEDIATE")
            try:
                # 再次检查（flock 内但事务外可能有同 UID 并发？flock 保证不会）
                row = cas_conn.execute(
                    "SELECT state FROM cas_file_cache WHERE cas_key = ?",
                    (cas_key,)
                ).fetchone()
                if row and row["state"] == "ready":
                    cas_conn.execute("ROLLBACK")  # 已发布，跳过
                else:
                    _cas_publish_in_transaction(cas_key, parse_result, cas_conn)

                # 写入 pending ref（带 TTL，GC 视为 live）
                cas_conn.execute(
                    "INSERT OR REPLACE INTO cas_pending_refs "
                    "(cas_key, workspace_id, expires_at) VALUES (?, ?, ?)",
                    (cas_key, workspace_id, time.time() + 300)  # 5 分钟 TTL
                )
                cas_conn.execute("COMMIT")  # CAS 先提交
            except Exception:
                cas_conn.execute("ROLLBACK")
                raise

        # 7. workspace manifest 提交（CAS 已提交，崩溃在此 → CAS 多一个未引用条目）
        ws_conn.execute("BEGIN IMMEDIATE")
        try:
            refresh_projection(workspace_id, rel_path, cas_key, ws_conn, cas_conn)
            ws_conn.execute("COMMIT")  # workspace 后提交
        except Exception:
            ws_conn.execute("ROLLBACK")
            # manifest 未提交，CAS 已提交——pending ref TTL 保护 CAS 不被 GC 删除
            raise

        # 8. manifest 提交成功，删除 pending ref
        cas_conn.execute("BEGIN IMMEDIATE")
        try:
            cas_conn.execute(
                "DELETE FROM cas_pending_refs WHERE cas_key = ? AND workspace_id = ?",
                (cas_key, workspace_id)
            )
            cas_conn.execute("COMMIT")
        except Exception:
            cas_conn.execute("ROLLBACK")
            # pending ref 删除失败不影响正确性，TTL 自然过期
    finally:
        fcntl.flock(flock_fd, fcntl.LOCK_UN)
        os.close(flock_fd)
```

**GC 侧配合（v7 P1#1）**：GC 获取独占 `flock`，扫描时将 `cas_pending_refs` 中的 key 也加入 live set：

```python
def gc_cas(cas_conn, grace_period_days=7):
    """v7 P1#1: GC 获取独占 flock，scan + sweep 期间所有 refresh 阻塞。
    pending_refs 中的 key 也视为 live。
    """
    flock_fd = os.open(CAS_FLOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(flock_fd, fcntl.LOCK_EX)  # 独占锁，阻塞所有 refresh

        # ... v6 的 scan + sweep 逻辑 ...

        # live set 额外包含 pending_refs（未完成 manifest 提交的 CAS key）
        pending_keys = cas_conn.execute(
            "SELECT DISTINCT cas_key FROM cas_pending_refs WHERE expires_at > ?",
            (time.time(),)
        ).fetchall()
        live_keys.update(r["cas_key"] for r in pending_keys)

        # 清理过期 pending_refs（TTL 自然过期）
        cas_conn.execute(
            "DELETE FROM cas_pending_refs WHERE expires_at <= ?",
            (time.time(),)
        )

        # ... sweep 逻辑 ...
    finally:
        fcntl.flock(flock_fd, fcntl.LOCK_UN)
        os.close(flock_fd)
```

**crash-injection 测试（v7 P1#1）**：

```python
def test_cas_workspace_commit_crash_safety(tmp_path):
    """v7 P1#1: 模拟 CAS 提交后、workspace 提交前崩溃。

    预期：CAS 有一个未引用条目（pending ref 保护），workspace manifest 未更新。
    下次 refresh 正常完成，不留残留。
    """
    # 1. CAS 提交成功
    # 2. 模拟崩溃（不执行 workspace COMMIT）
    # 3. 重启 refresh
    # 4. 验证：CAS 条目存在且 ready，manifest 已更新，pending_ref 已清理
    ...


def test_gc_does_not_delete_pending_ref(tmp_path):
    """v7 P1#1: pending ref 中的 CAS key 不被 GC 删除。"""
    # 1. 发布 CAS + 写 pending_ref
    # 2. 不提交 workspace manifest
    # 3. 运行 GC
    # 4. 验证：CAS 条目仍存在（pending ref 保护）
    ...
```

**edge INSERT 校验测试（v7 P2#2）**：

```python
def test_edge_insert_rejects_cross_workspace_caller(tmp_path):
    """v7 P2#2: caller 属于其他 workspace 时，INSERT...SELECT 返回 0 行，edge 不写入。"""
    # 1. workspace A 写入 symbol_x（workspace_id=A）
    # 2. workspace B 调用 write_resolved_edges(workspace_id=B, caller_symbol_id=symbol_x.id)
    # 3. 验证：edge 未写入（SELECT 约束 caller.workspace_id=B 但 symbol_x 属于 A）


def test_edge_insert_rejects_cross_workspace_callee(tmp_path):
    """v7 P2#2: callee 属于其他 workspace 时，edge 不写入。"""
    # 1. caller 和 callee 属于不同 workspace
    # 2. 调用 write_resolved_edges
    # 3. 验证：edge 未写入


def test_edge_insert_allows_unresolved_callee(tmp_path):
    """v7 P2#2: callee_symbol_id=NULL（未解析边）允许写入，只校验 caller。"""
    # 1. caller 属于本 workspace
    # 2. callee_symbol_id=None
    # 3. 验证：edge 写入成功


def test_edge_insert_local_integrity_check_rollback(tmp_path):
    """v7 P2#2: 提交前局部完整性检查发现不一致时 ROLLBACK。"""
    # 1. 构造一个绕过 INSERT...SELECT 的场景（如直接 INSERT 或 mock）
    # 2. 触发局部完整性检查
    # 3. 验证：事务 ROLLBACK，抛 RuntimeError


def test_doctor_detects_edge_workspace_inconsistency(tmp_path):
    """v7 P2#2: doctor_check_edge_workspace_consistency 发现历史不一致 edge。"""
    # 1. 从旧 schema 迁移的数据中存在 caller workspace_id 不一致
    # 2. 运行 doctor_check_edge_workspace_consistency
    # 3. 验证：返回 False 并打印违规 edge
```

**v6 + v7 关键改进**：
- **TOCTOU 消除**：flock 保证 GC（独占）与 refresh（共享）互斥。
- **崩溃安全**：CAS 先提交，workspace 后提交。崩溃窗口内 CAS 多一个未引用条目（pending ref 保护），不会出现 manifest 指向不存在 CAS。
- **parse 不持锁**：parse 在 flock 内但不在 CAS 事务内，不阻塞其他 refresh 的 CAS 查询（只阻塞 GC）。
- **fail-closed 保留**：任何 workspace DB 读取失败即中止 GC。
- **`grace_threshold`**：仅影响"陈旧 workspace"警告，不影响 live set（保守策略：所有 manifest 引用的 key + pending refs 都加入 live set）。
- **删除顺序**：先子表 → 正文表 → 父表。
- **building 清理用子查询**：`WHERE cas_key IN (SELECT ...)` 避免参数上限。
- GC 命令：`cw gc cas [--grace-days 7]`，手动触发。
- **并发影响**：GC 期间 refresh 的 `flock(LOCK_SH)` 会阻塞等待（GC 完成后自动继续）。

### 3.9 迁移策略（v4 P2 + v6 P1#6：Global CAS 冷启动重建）

- **CAS DB**（`~/.callwarden/cas.db`）：首次运行时按 `cas_schema_meta` 中的 `cas_schema_version` 创建。不同 Call Warden 版本/容器并存时，通过 `min_reader_version` 兼容性检查。旧版本 reader 遇到新 schema 时报错提示升级，不静默读取错误数据。
- **Workspace DB**：schema migration 添加 `workspace_symbols` + `workspace_resolved_edges` + `workspace_manifests` 表（不影响现有表），首次 refresh 时回填。
- **Local CAS → Global CAS 迁移**（Phase 2 daemon 落地时）：

  **v6 P1#6 修复**：Local CAS（`~/.callwarden/cas.db`）是用户可写的普通 SQLite 文件。用户可以手工修改它，伪造相同 `cas_key` 下的符号结果（如把 `malicious_function` 的 `signature` 改成 `safe_function` 的签名）。若 daemon 直接导入 Local CAS 到 Global CAS（`/var/lib/callwarden/cas.db`），则被伪造的数据会污染**所有用户共享**的 Global CAS。

  **v6 策略：Global CAS 冷启动重建（不导入 Local CAS）**：

  ```python
  def global_cas_cold_start(daemon, global_cas_path, workspace_roots):
      """v6 P1#6: Global CAS 冷启动重建——从源文件重新 parse，不导入 Local CAS。

      Local CAS 是用户可写的普通文件，cas_key 下可挂伪造的符号结果。
      直接导入会污染所有用户共享的 Global CAS。
      """
      global_cas = open_cas_db(global_cas_path, fresh=True)  # 全新空数据库

      for ws_root in workspace_roots:
          for rel_path, abs_path in walk_source_files(ws_root):
              # v7 P1#3: 必须用 ParseInputV1 规范化后的 canonical bytes 计算 content_hash
              canonical_bytes = read_canonical_bytes(abs_path)  # 见 §3.0.1
              content_hash = hashlib.sha256(canonical_bytes).hexdigest()

              # 必须重新读取源文件、计算 content hash，不信任 Local CAS 中的 cas_key
              # v7 P2#1: 统一调用 compute_cas_key_v1（含 abi_version + input_abi_version）
              cas_key = compute_cas_key_v1(content_hash, lang, parser_version, callwarden_version, extraction_config_version, abi_version, input_abi_version)

              # 只有 cas_key 不存在时才重新 parse（避免重复工作）
              if not cas_key_exists(global_cas, cas_key):
                  result = parse_file(abs_path, lang)  # 重新 parse，不信任 Local CAS 缓存
                  cas_publish(global_cas, cas_key, result)  # 原子发布

      return global_cas
  ```

  **若必须 seed（加速冷启动）**：将 Local CAS 当**不可信候选**，daemon 必须对每条 `cas_key` 重新验证：
  1. 从 Local CAS 读取 `cas_key` 对应的 `content_hash` 和 `language`。
  2. 找到对应的源文件，**重新读取文件内容**并计算 `sha256(content)`，校验与 Local CAS 中记录的 `content_hash` 一致。
  3. 用当前 parser_version + callwarden_version + abi_version 重新计算 `cas_key`，校验与 Local CAS 中的 `cas_key` 一致。
  4. 重新 parse 源文件，将结果与 Local CAS 中的 `cas_symbols` / `cas_raw_calls` 逐字段比较。
  5. 全部一致才采纳；任一不一致则丢弃 Local CAS 条目，用重新 parse 的结果发布。

  **不信任理由**：用户可以修改 Local CAS 的 `cas_symbols` 表内容（如改 `signature` 字段），同时保持 `cas_key` 不变——因为 `cas_key` 是 hash，用户无法逆推，但可以复制一个合法 `cas_key` 然后替换其下的符号数据。因此必须重新 parse 验证。

- 回退：`CW_DISABLE_CAS=1` 环境变量关闭 CAS 路径，回退到现有 parse → symbols/calls 写入。

### 3.10 风险与缓解

| 风险 | 缓解 |
|------|------|
| CAS 命中率不达预期（< 95%） | 监控命中率，分析未命中原因（parser 版本不一致、文件实际不同等） |
| Dirty workspace 查询路径复杂 | Phase 3A 不改查询路径，dirty 走现有 symbols/calls 表 |
| CAS 表膨胀（parser 升级后旧条目堆积） | `cw gc cas` 从 manifest 聚合清理无引用条目 |
| 多 workspace 隔离不足（串库） | Phase 3A 必做项：resolver + db_query + graph.rs 全部加 workspace_id 过滤 |
| schema migration 失败 | 保留 `CW_DISABLE_CAS` 回退路径，migration 失败不影响现有功能 |
| resolved_edges 错误共享 | Phase 3A 不共享 resolved_edges，一律 per-workspace |
| CAS 写入崩溃产生半成品 | 原子发布协议：state=building → ready，GC 清理孤儿 |
| CAS DB 跨进程写锁 | v6 更新：Phase 3A 采用 WAL + `busy_timeout=5000` + 有界重试多进程模型（v5 P1#1）。同 UID 多 CLI/hook/VSCode 进程并发写靠 WAL 协调，撞锁时友好提示重试。Phase 2 daemon 为终极单写者方案。（v5 已删除"单用户单进程"描述） |
| Rust parser fallback 未覆盖 | v6 更新：✅ 已完成。Phase 1.1 集成测试覆盖 Rust 不可用、pool 异常、单文件 error 回退 Python（`tests/test_p31_multi_lang.py` 28 passed） |
| Local CAS 数据被篡改 | v6 P1#6：Global CAS 冷启动重建，不导入 Local CAS；若必须 seed 则逐条重新 parse 验证 |

### 3.11 工作量预估

| 任务 | 工时 |
|------|------|
| CAS DB 连接管理 + schema 设计 | 2 天 |
| `db_cas.py` CAS 存储层实现（原子发布 / query / copy_to_projection） | 2.5 天 |
| `db_build.py` refresh 流程改造（CAS 查询 + miss parse + 命中复制 + projection 生成） | 2 天 |
| Workspace manifest 实现（clean/dirty 分离，cas_key 允许 NULL） | 1 天 |
| 多 workspace 隔离改造（resolver + db_query + graph.rs，P1#4） | 1.5 天 |
| GC 命令（临时 mark 表 + 孤儿清理） | 0.5 天 |
| 集成测试（50 workspace 共享验证 + 崩溃恢复测试） | 2 天 |
| 文档更新 | 0.5 天 |
| **合计** | **12 天** |

---

## 4. Phase 3B 详细设计：Resolved Edge Store + 查询路径迁移

> **评审建议**：Phase 3B 建议在 Phase 2 daemon 落地后实施（daemon 内单源实现），避免先建一套很快被 Rust daemon 替换的 Python 查询架构。Phase 3B 的 schema 可在 Phase 3A 阶段预留。

### 4.1 核心思想（v4：统一身份 + resolved edge store）

Phase 3A 只用 CAS 加速 parse，查询仍走 `symbols`/`calls` 表。Phase 3B 把查询路径迁到 `workspace_symbols` 统一身份表 + `workspace_resolved_edges`，dirty 文件走 overlay。

**v3 关键修正**（P1#3）：`get_callers`/`get_callees` 查询的是**已解析到具体符号的边**，不是 raw calls。`cas_raw_calls` 只有调用文本（`callee_name`），无法回答"谁调用了这个符号"。必须引入 `workspace_resolved_edges` 表。

**v4 关键修正**（P1#3 续）：`caller_symbol_id` 只引用 `workspace_symbols.id`（统一身份表），不再歧义地指向 projection.id 或 symbols.id。

**Phase 3B 做**：
- ✅ 符号查询走 `workspace_symbols`（clean 从 `cas_symbols` 复制内容级字段，dirty 从 `symbols` 复制）。
- ✅ 调用图查询走 `workspace_resolved_edges`（已解析的 caller→callee 边，引用 `workspace_symbols.id`）。
- ✅ Dirty 文件走 rel_path tombstone 屏蔽旧 clean projection（P1#4）。
- ✅ 多 workspace 共享 CAS 时，符号查询返回的 `qualified_name` 跨 workspace 一致。

**Phase 3B 不做**：
- ❌ 不共享 `resolved_edges`（仍 per-workspace，依赖 build context）。
- ❌ 不做 snapshot 级 thin DB 复用。
- ❌ 不做跨用户授权查询。

### 4.2 Resolved Edge Store 设计（v4 P1#3：引用统一身份表）

`workspace_resolved_edges` 表存放在 workspace DB，记录已解析的跨文件调用边。**caller/callee 只引用 `workspace_symbols.id`**，不会 ID 碰撞：

```sql
-- Workspace resolved edges：已解析的 caller→callee 边（per-workspace）
-- caller_symbol_id / callee_symbol_id 只引用 workspace_symbols.id（统一身份表）
CREATE TABLE IF NOT EXISTS workspace_resolved_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    caller_symbol_id INTEGER NOT NULL,   -- workspace_symbols.id（统一身份表，clean/dirty 统一）
    callee_symbol_id INTEGER,             -- workspace_symbols.id；NULL = 未解析
    caller_qualified TEXT NOT NULL,       -- 冗余存储，加速查询
    callee_qualified TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_file TEXT DEFAULT '',
    call_line INTEGER NOT NULL,
    call_ordinal INTEGER DEFAULT 0,       -- v4 P2：同行调用序号
    is_cross_file INTEGER DEFAULT 0,
    source TEXT DEFAULT 'cas',            -- 'cas' = clean 文件边，'dirty' = dirty 文件边
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    -- v6 P1#1: 组合 FK 声明为意图文档（workspace DB 保持 foreign_keys=OFF）
    -- ON DELETE CASCADE 不在运行时生效，应用层显式 DELETE edge（见 mark_file_dirty / refresh_clean_to_clean）
    -- cw doctor --fk-check 校验 FK 完整性
    FOREIGN KEY (workspace_id, caller_symbol_id) REFERENCES workspace_symbols(workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, callee_symbol_id) REFERENCES workspace_symbols(workspace_id, id) ON DELETE CASCADE,
    UNIQUE(workspace_id, caller_symbol_id, callee_name, call_line, call_ordinal)  -- 含 call_ordinal
);

CREATE INDEX IF NOT EXISTS idx_edges_workspace ON workspace_resolved_edges(workspace_id);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON workspace_resolved_edges(workspace_id, caller_qualified);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON workspace_resolved_edges(workspace_id, callee_name);
CREATE INDEX IF NOT EXISTS idx_edges_callee_qname ON workspace_resolved_edges(workspace_id, callee_qualified);
```

**写入时机**：refresh 流程中 `_resolve_calls_batch` 解析完跨文件调用后，写入 `workspace_resolved_edges`（clean 文件 `source='cas'`，dirty 文件 `source='dirty'`）。caller/callee 都先在 `workspace_symbols` 中创建或查找对应行，拿到 `workspace_symbols.id`。

**查询路径**：
- `get_callers(callee_name, qualified_name)` → 查 `workspace_resolved_edges WHERE workspace_id = ? AND callee_name = ? [AND callee_qualified = ?]`
- `get_callees(caller_name, qualified_name)` → 查 `workspace_resolved_edges WHERE workspace_id = ? AND caller_qualified = ?`

#### 4.2.1 Edge INSERT 同 workspace 校验（v7 P2#2）

**问题**：`foreign_keys=OFF` 时 v6 只规定了 `mark_file_dirty` / `refresh_clean_to_clean` 的**显式 DELETE**，没有规定 `_resolve_calls_batch` 写入 edge 时的同 workspace 校验。现有 `_write_calls_db`（[db_build.py L2836](../../db/db_build.py#L2836)）用 Python 侧 `qname_id_map` 查找 `caller_id` / `callee_id` 后直接 `INSERT INTO calls VALUES (...)`，没有任何 workspace 边界约束。迁移到 `workspace_resolved_edges` 后若沿用此模式，一个 workspace 的 resolver 误把 caller/callee 解析到另一个 workspace 的 `workspace_symbols.id`（多 workspace 同 DB 时尤其危险），edge 会静默写入，`doctor` 只能事后发现。

**v7 P2#2 策略（写入期约束 + 提交前局部校验 + doctor 兜底）**：

| 层级 | 策略 | 说明 |
|------|------|------|
| Edge 写入 | `INSERT ... SELECT` | caller/callee 的 `workspace_id` 由 SELECT 子查询约束，不信任 Python 侧查到的 id |
| 提交前 | 局部完整性检查 | 写事务 COMMIT 前校验本批次 edge 的 caller/callee 均属同一 workspace |
| 迁移后 | `cw doctor --fk-check` | 增加 edge workspace 一致性校验（见 §3.3 doctor_fk_check 扩展） |

**`INSERT ... SELECT` 写入模式**：

```python
def write_resolved_edges(workspace_id, resolved_calls, ws_conn):
    """v7 P2#2: workspace_resolved_edges 写入，caller/callee 的 workspace_id
    由 SELECT 子查询约束，不信任 Python 侧查到的 id。

    - caller 必须命中 workspace_symbols(workspace_id=?, id=caller_symbol_id)
    - callee 若已解析（callee_symbol_id 非 NULL），必须命中同 workspace 的 symbol
    - callee 未解析（callee_symbol_id=NULL）允许写入（未解析边仍需保留以供查询）
    """
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        for call in resolved_calls:
            caller_symbol_id = call["caller_symbol_id"]
            callee_symbol_id = call.get("callee_symbol_id")  # 可能 None

            if callee_symbol_id is None:
                # callee 未解析：只校验 caller 属于本 workspace
                ws_conn.execute(
                    """INSERT INTO workspace_resolved_edges
                       (workspace_id, caller_symbol_id, callee_symbol_id,
                        caller_qualified, callee_qualified, callee_name,
                        callee_file, call_line, call_ordinal,
                        is_cross_file, source)
                       SELECT ?, caller.id, NULL,
                              ?, ?, ?,
                              ?, ?, ?,
                              ?, ?
                       FROM workspace_symbols caller
                       WHERE caller.workspace_id = ? AND caller.id = ?""",
                    (workspace_id,
                     call["caller_qualified"], call.get("callee_qualified", ""),
                     call["callee_name"], call.get("callee_file", ""),
                     call["call_line"], call.get("call_ordinal", 0),
                     call.get("is_cross_file", 0), call.get("source", "cas"),
                     workspace_id, caller_symbol_id),
                )
            else:
                # callee 已解析：caller 和 callee 都必须属同一 workspace
                ws_conn.execute(
                    """INSERT INTO workspace_resolved_edges
                       (workspace_id, caller_symbol_id, callee_symbol_id,
                        caller_qualified, callee_qualified, callee_name,
                        callee_file, call_line, call_ordinal,
                        is_cross_file, source)
                       SELECT ?, caller.id, callee.id,
                              ?, ?, ?,
                              ?, ?, ?,
                              ?, ?
                       FROM workspace_symbols caller
                       JOIN workspace_symbols callee
                         ON callee.workspace_id = caller.workspace_id
                       WHERE caller.workspace_id = ? AND caller.id = ?
                         AND callee.id = ?""",
                    (workspace_id,
                     call["caller_qualified"], call.get("callee_qualified", ""),
                     call["callee_name"], call.get("callee_file", ""),
                     call["call_line"], call.get("call_ordinal", 0),
                     call.get("is_cross_file", 0), call.get("source", "cas"),
                     workspace_id, caller_symbol_id, callee_symbol_id),
                )
                # SELECT 返回 0 行 = caller 或 callee 不属于本 workspace → 该 edge 被丢弃

        # v7 P2#2: 提交前局部完整性检查
        # 校验本事务写入的 edge 没有 caller/callee workspace_id 不一致的情况
        bad = ws_conn.execute(
            """SELECT COUNT(*) FROM workspace_resolved_edges e
               WHERE e.workspace_id = ?
                 AND (
                   EXISTS (SELECT 1 FROM workspace_symbols s
                           WHERE s.id = e.caller_symbol_id
                             AND s.workspace_id != e.workspace_id)
                   OR (e.callee_symbol_id IS NOT NULL
                       AND EXISTS (SELECT 1 FROM workspace_symbols s
                                   WHERE s.id = e.callee_symbol_id
                                     AND s.workspace_id != e.workspace_id))
                 )""",
            (workspace_id,),
        ).fetchone()[0]
        if bad > 0:
            raise RuntimeError(
                f"edge workspace 一致性检查失败：{bad} 条 edge 的 caller/callee 不属于 workspace {workspace_id}"
            )
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise
```

**关键设计**：

1. **`INSERT ... SELECT` 替代 `INSERT ... VALUES`**：caller/callee 的 `workspace_id` 由 `WHERE caller.workspace_id = ? AND caller.id = ?` 约束。若 Python 侧查到的 id 属于其他 workspace（例如 resolver 误用全局 `qname_id_map` 命中了另一 workspace 的同名符号），SELECT 返回 0 行，edge 不写入——**写入期即拒绝越权 edge**，而非事后由 `doctor` 发现。
2. **callee 未解析仍允许写入**：`callee_symbol_id=NULL` 的边（raw call 未解析到目标）仍需保留，以支持 `get_callees` 返回未解析调用。此时只校验 caller 属于本 workspace。
3. **提交前局部完整性检查**：在 COMMIT 前用一条 SQL 扫描本事务写入的 edge，确认无 caller/callee workspace_id 不一致。发现不一致则 ROLLBACK 并抛异常，避免脏数据落盘。该检查在事务内执行，开销小（仅扫本 workspace 的 edge）。
4. **不依赖 `PRAGMA foreign_keys=ON`**：`INSERT ... SELECT` 的 WHERE 子句本身即 workspace 边界约束，与 FK 是否启用无关。即使 `foreign_keys=OFF`，越权 edge 也无法写入。
5. **批量写入优化**：上述伪代码为单条 INSERT 展示逻辑；实现时可收集批量参数后用 `executemany` + 同一 `INSERT ... SELECT` 模板，减少往返开销。`executemany` 的参数绑定与 `INSERT ... SELECT` 兼容（SQLite 会逐行执行 SELECT 约束）。

**`doctor --fk-check` 扩展（edge workspace 一致性校验）**：

在 §3.3 的 `doctor_fk_check` 基础上增加 edge 一致性扫描（不依赖 `foreign_keys=ON`，直接查 workspace_id 不匹配）：

```python
def doctor_check_edge_workspace_consistency(ws_conn):
    """v7 P2#2: 校验 workspace_resolved_edges 的 caller/callee 都属于同一 workspace。

    与 doctor_fk_check 并行运行，不依赖 PRAGMA foreign_keys=ON。
    """
    # caller workspace_id 不一致
    bad_caller = ws_conn.execute(
        """SELECT e.id, e.workspace_id, e.caller_symbol_id, s.workspace_id AS sym_ws
           FROM workspace_resolved_edges e
           JOIN workspace_symbols s ON s.id = e.caller_symbol_id
           WHERE s.workspace_id != e.workspace_id"""
    ).fetchall()
    # callee workspace_id 不一致（排除 NULL 未解析边）
    bad_callee = ws_conn.execute(
        """SELECT e.id, e.workspace_id, e.callee_symbol_id, s.workspace_id AS sym_ws
           FROM workspace_resolved_edges e
           JOIN workspace_symbols s ON s.id = e.callee_symbol_id
           WHERE e.callee_symbol_id IS NOT NULL
             AND s.workspace_id != e.workspace_id"""
    ).fetchall()
    if bad_caller or bad_callee:
        for row in bad_caller:
            print(f"edge #{row['id']}: workspace={row['workspace_id']} "
                  f"但 caller_symbol {row['caller_symbol_id']} 属 workspace {row['sym_ws']}")
        for row in bad_callee:
            print(f"edge #{row['id']}: workspace={row['workspace_id']} "
                  f"但 callee_symbol {row['callee_symbol_id']} 属 workspace {row['sym_ws']}")
        return False
    return True
```

**与 `INSERT ... SELECT` 的关系**：`INSERT ... SELECT` 是**写入期预防**（越权 edge 写不进去），`doctor` 是**迁移后兜底**（已有数据或从旧 schema 迁移的 edge 可能不一致）。两者互补：新写入靠 `INSERT ... SELECT` 保证一致性，历史数据靠 `doctor` 发现并修复。

### 4.3 查询路径改造（v4 P1#4 + v6 P1#4：Dirty overlay + clean→clean 投影替换）

**v3 问题**：Clean 查询直接读取 `workspace_symbol_projection`，没有 JOIN manifest 检查 `is_dirty=0`。文件从 clean 变 dirty 后，旧 projection 仍被返回。按 `qualified_name` 让 dirty 优先会误删其他文件中的合法同名符号。

**v4 修复 + v5 P1#3 删除顺序修正**：
1. 文件变 dirty 时，在 workspace 事务中**先删 edge 再删 symbol**（v5 修正：v4 先删 symbol 导致 edge 子查询为空）。
2. `workspace_resolved_edges` 的 FK 增加 `ON DELETE CASCADE`（v5 P1#3），即使先删 symbol 也能自动清理 edge。
3. Clean 查询 JOIN manifest 校验 `wm.is_dirty = 0`（双重保险）。
4. Overlay 边界是 **rel_path**，不是 qualified_name。

**v6 P1#4 新增 clean→clean 投影替换**：`git pull` 或 commit 切换后，文件可能从 clean 版本 A 直接变成 clean 版本 B（is_dirty 保持 0，但 cas_key 变了）。若只校验 `is_dirty=0`，旧 `workspace_symbols` 行会残留并继续返回（新版本符号更少时尤甚）。修复：
1. Refresh 在一个 workspace 事务中按 `rel_path` 原子替换 projection（删旧 edge → 删旧 symbol → 插新 projection → 更新 manifest cas_key）。
2. Clean 查询额外校验 `wm.cas_key = wsym.cas_key`，manifest 与 projection 的 cas_key 不一致时不返回。
3. `mark_file_dirty` 和 `refresh_clean_to_clean` 的 edge 删除改用子查询（v6 P2#2），避免大文件符号多时 `IN (?)` 参数上限。

```python
def mark_file_dirty(workspace_id, rel_path, ws_conn):
    """文件变 dirty 时，删除旧 clean projection（tombstone by rel_path）

    v5 P1#3: 先删 edge 再删 symbol（v4 先删 symbol 导致 edge 子查询为空）。
    v6 P2#2: edge 删除改用子查询，避免大文件符号多时 IN(?) 参数上限。
    FK ON DELETE CASCADE 作为兜底，但显式先删 edge 更安全。
    """
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 先删该 rel_path 的旧 resolved edges（caller 或 callee 指向 affected symbols）
        # v6 P2#2: 用子查询替代 IN(?)，不受 SQLite 参数上限影响
        ws_conn.execute("""
            DELETE FROM workspace_resolved_edges
            WHERE workspace_id = ? AND (
                caller_symbol_id IN (
                    SELECT id FROM workspace_symbols
                    WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'
                )
                OR callee_symbol_id IN (
                    SELECT id FROM workspace_symbols
                    WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'
                )
            )
        """, (workspace_id, workspace_id, rel_path, workspace_id, rel_path))

        # 2. 再删该 rel_path 的旧 workspace_symbols（clean projection）
        ws_conn.execute(
            "DELETE FROM workspace_symbols WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'",
            (workspace_id, rel_path)
        )
        # 注：FK ON DELETE CASCADE 也会自动清理 edge，但显式先删更安全（不依赖 PRAGMA foreign_keys=ON）

        # 3. 更新 manifest 为 dirty
        ws_conn.execute(
            "UPDATE workspace_manifests SET is_dirty = 1, cas_key = NULL WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path)
        )
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise


def refresh_clean_to_clean(workspace_id, rel_path, new_cas_key, ws_conn):
    """v6 P1#4: clean→clean 投影替换（git pull / commit 切换后文件从版本 A 变版本 B）

    在一个 workspace 事务中按 rel_path 原子替换 projection：
    1. 删除旧 resolved edges（子查询，避免 IN(?) 参数上限）
    2. 删除旧 workspace_symbols（source='cas'）
    3. 从新 CAS 条目复制 projection 到 workspace_symbols
    4. 更新 workspace_manifests.cas_key 为新 key

    若新版本符号更少，旧 workspace_symbols 行必须被删除，否则会残留并继续返回。
    """
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 删除旧 resolved edges（caller 或 callee 指向旧 projection symbols）
        ws_conn.execute("""
            DELETE FROM workspace_resolved_edges
            WHERE workspace_id = ? AND (
                caller_symbol_id IN (
                    SELECT id FROM workspace_symbols
                    WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'
                )
                OR callee_symbol_id IN (
                    SELECT id FROM workspace_symbols
                    WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'
                )
            )
        """, (workspace_id, workspace_id, rel_path, workspace_id, rel_path))

        # 2. 删除旧 workspace_symbols（clean projection）
        ws_conn.execute(
            "DELETE FROM workspace_symbols WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'",
            (workspace_id, rel_path)
        )

        # 3. 从新 CAS 条目复制 projection 到 workspace_symbols
        # 伪代码：实际由 _copy_cas_to_workspace_symbols 实现（从 cas_symbols 查询后批量 INSERT）
        _copy_cas_to_workspace_symbols(ws_conn, workspace_id, rel_path, new_cas_key)

        # 4. 更新 manifest cas_key（保持 is_dirty=0，因为新版本也是 clean）
        ws_conn.execute(
            "UPDATE workspace_manifests SET cas_key = ?, is_dirty = 0 WHERE workspace_id = ? AND rel_path = ?",
            (new_cas_key, workspace_id, rel_path)
        )
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise


def search_symbols(query, kind, limit, workspace_id, ws_conn, cas_attached):
    """查询符号：clean 走 workspace_symbols JOIN cas_symbols，dirty 走 workspace_symbols

    cas_attached: workspace DB 已 ATTACH CAS DB 为 cas_db（mode=ro）

    v6 P1#4: clean 查询额外校验 wm.cas_key = wsym.cas_key，防止 git pull 后
    clean→clean 替换未完成时旧 projection 残留。
    v6 P2#4: clean/dirty 用 UNION ALL + ORDER BY + LIMIT 统一排序，
    不再分别 LIMIT 拼接（避免返回 2×limit 且非全局排序）。
    """
    # v6 P2#4: 统一 UNION ALL + ORDER BY + LIMIT
    sql = """
        SELECT * FROM (
            -- Clean 文件：workspace_symbols JOIN cas_db.cas_symbols（ATTACH 只读）
            SELECT wsym.qualified_name, wsym.module_path, wsym.rel_path as file_path,
                   csym.start_line, csym.end_line, csym.depth, wsym.name, csym.kind,
                   csym.signature, csym.has_comment
            FROM workspace_symbols wsym
            JOIN workspace_manifests wm ON wsym.workspace_id = wm.workspace_id AND wsym.rel_path = wm.rel_path
            JOIN cas_db.cas_symbols csym ON wsym.cas_key = csym.cas_key AND wsym.local_symbol_id = csym.local_symbol_id
            WHERE wsym.workspace_id = ?
              AND wm.is_dirty = 0
              AND wsym.source = 'cas'
              AND wm.cas_key = wsym.cas_key    -- v6 P1#4: 校验 manifest 与 projection cas_key 一致
              AND (wsym.name LIKE ? OR wsym.qualified_name LIKE ?)
    """
    params = [workspace_id, f"%{query}%", f"%{query}%"]
    if kind:
        sql += "              AND csym.kind = ?"
        params.append(kind)

    sql += """
            UNION ALL
            -- Dirty 文件：workspace_symbols JOIN symbols（overlay）
            SELECT wsym.qualified_name, wsym.module_path, wsym.rel_path as file_path,
                   wsym.start_line, wsym.end_line, s.depth, wsym.name, wsym.kind,
                   s.signature, s.has_comment
            FROM workspace_symbols wsym
            JOIN symbols s ON wsym.symbols_rowid = s.id
            WHERE wsym.workspace_id = ?
              AND wsym.source = 'dirty'
              AND (wsym.name LIKE ? OR wsym.qualified_name LIKE ?)
    """
    params.extend([workspace_id, f"%{query}%", f"%{query}%"])
    if kind:
        sql += "              AND wsym.kind = ?"
        params.append(kind)

    # v6 P2#4: 统一 ORDER BY + LIMIT（不再分别 LIMIT 拼接）
    sql += ") ORDER BY kind, depth DESC, file_path, start_line LIMIT ?"
    params.append(limit)

    return [dict(r) for r in ws_conn.execute(sql, params)]
```

**v4 关键点**：
- Clean 查询 JOIN `workspace_manifests` 校验 `wm.is_dirty = 0`，防止旧 projection 残留。
- 文件变 dirty 时先删除旧 clean projection（tombstone by rel_path），再插入 dirty projection。
- Overlay 边界是 **rel_path**，不会误删其他文件中的同名符号。
- `workspace_resolved_edges` 的 `source` 字段区分 clean/dirty 边，查询时不需要 UNION，单表查即可。

**v6 P1#4 关键点**：
- Clean 查询额外校验 `wm.cas_key = wsym.cas_key`，git pull 后若 `refresh_clean_to_clean` 未完成，旧 projection 不会被返回。
- `refresh_clean_to_clean` 在单个 workspace 事务中原子替换 projection，防止"新版本符号更少，旧行残留"问题。
- `mark_file_dirty` 和 `refresh_clean_to_clean` 的 edge 删除均使用子查询（v6 P2#2），不受 SQLite `SQLITE_MAX_VARIABLE_NUMBER` 限制。
- `search_symbols` 用 `UNION ALL + ORDER BY + LIMIT` 统一排序（v6 P2#4），不再分别 LIMIT 拼接导致返回 `2×limit` 且非全局排序。

**跨 DB ATTACH 只读**（v5 P2: 参数化 ATTACH）：

```python
# v5 P2: 参数化 ATTACH，避免 f-string 花括号被当字面值
# workspace 主连接需启用 URI（sqlite3.connect("file:...", uri=True)）
cas_uri = f"file:{cas_db_path}?mode=ro"
ws_conn.execute("ATTACH DATABASE ? AS cas_db", (cas_uri,))
# 查询用 cas_db.cas_symbols 全名引用
```

### 4.4 工作量预估

| 任务 | 工时 |
|------|------|
| `workspace_symbols` 统一身份表 + 写入逻辑（clean/dirty 投影） | 2 天 |
| `workspace_resolved_edges` schema + 写入逻辑 | 1.5 天 |
| `search_symbols` 改造（JOIN + ATTACH mode=ro + rel_path tombstone） | 2 天 |
| `get_callers`/`get_callees` 改造（走 resolved_edges） | 1.5 天 |
| `get_symbol` 改造 + dirty overlay 逻辑 | 1 天 |
| 集成测试 | 2 天 |
| **合计** | **10 天** |

---

## 5. 测试计划

### 5.1 Phase 1 测试

| 测试 | 类型 | 验收 |
|------|------|------|
| 11 语言 alignment tests（Counter 多重集合比较 + Counter 相减） | 单元 | actual_diff - known_diffs == empty（剩余差异为零） |
| Rust 扩展不可用 fallback | 集成 | 回退 Python parser，不报错 |
| `CW_DISABLE_RUST_PARSE` | 集成 | 强制走 Python |
| 小批量走 `parse_file_lang`（单文件 Rust） | 集成 | 文件数 < 50 时走单文件 Rust，不走 pool |
| 大批量走 `batch_parse_files_lang_pool` | 集成 | 文件数 ≥ 50 时走 pool |
| Rust pool 运行时异常回退 Python | 集成 | pool 抛异常时回退 Python，不中断 |
| 单文件 Rust error 回退 Python | 集成 | 单文件 parse 返回 error 时回退 Python |
| benchmark admin 项目 | 性能 | Rust ≥ 2x Python |
| benchmark firmware 75K 文件 | 性能 | Rust 父进程 RSS < 1GB |

### 5.2 Phase 3A 测试

| 测试 | 类型 | 验收 |
|------|------|------|
| CAS 命中率（50 同 repo workspace，同 UID） | 集成 | ≥ 95%，parse miss = 0 |
| 第二个 workspace refresh 耗时分解 | 性能 | 分别记录 hash/CAS 查询/复制/resolve/写库耗时，**parse 耗时 = 0** |
| Dirty workspace 修改不污染共享 CAS | 集成 | dirty 文件 cas_key NULL |
| resolved_edges per-workspace 隔离 | 集成 | 不同 workspace 的 edges 不共享 |
| Resolver workspace 过滤（v3 P1#4） | 集成 | 不被其他 workspace 同名符号污染 |
| `get_callers`/`get_callees` workspace 隔离（v3 P1#4） | 集成 | 加 workspace_id 过滤后不返回其他 workspace 的边 |
| GraphStore workspace 过滤（v3 P1#4） | 集成 | CSR 索引只含当前 workspace 的 symbols/calls |
| CAS 原子发布 — 崩溃恢复（v3 P1#5） | 集成 | 模拟写入中途崩溃，CAS 表无半成品（state=building 被 GC 清理） |
| CAS 原子发布 — 并发同 key（v3 P1#5） | 集成 | 两个进程同时写同 cas_key，只有一个成功，另一个跳过 |
| CAS 自包含回填（v4 P1#1） | 集成 | CAS 命中后可从 cas_symbol_contents + cas_symbols 独立回填 workspace 的 symbol_contents + workspace_symbols |
| CAS GC 扫描所有 workspace（v4 P1#2） | 集成 | workspace A 运行 GC 不删除只被 B/C 引用的 key |
| CAS GC 宽限期（v4 P1#2） | 集成 | workspace DB 被锁时跳过，不删除其引用的 key |
| workspace_symbols 统一身份（v4 P1#3） | 集成 | resolved_edges caller/callee_id 只引用 workspace_symbols.id，不碰撞 |
| local_qualified_name 不碰撞（v4 P1#3） | 集成 | A.run 和 B.run 的 workspace 级 qualified_name 分别为 A.run / B.run |
| CAS DB schema version 兼容（v4 P2） | 单元 | 旧版本 reader 遇到新 schema 报错提示 |
| CAS DB 并发 busy_timeout（v4 P2） | 集成 | 两个进程同时写 CAS DB，一个等 5 秒后友好提示重试 |
| Schema migration 幂等 | 单元 | 重复执行不报错 |
| `CW_DISABLE_CAS` 回退 | 集成 | 回退到现有 parse 路径 |

### 5.3 Phase 3B 测试

| 测试 | 类型 | 验收 |
|------|------|------|
| 符号查询走 workspace_symbols + ATTACH JOIN（mode=ro） | 集成 | clean 走 workspace_symbols JOIN cas_symbols，dirty 走 workspace_symbols |
| `get_callers`/`get_callees` 走 resolved_edges（v3 P1#3） | 集成 | 查询 workspace_resolved_edges，按 workspace_id 过滤 |
| `get_callers`/`get_callees` 跨 workspace 一致 | 集成 | 相同 cas_key 的符号返回一致 resolved edges |
| `search_symbols` 跨 workspace 一致 | 集成 | 相同 cas_key 的符号返回一致 workspace_symbols |
| Dirty overlay 按 rel_path 屏蔽（v4 P1#4） | 集成 | 文件变 dirty 后旧 clean projection 被 tombstone，不返回 |
| Dirty overlay 不误删同名符号（v4 P1#4） | 集成 | 其他文件中的同名符号不受 dirty overlay 影响 |
| 75K 文件查询无 IN(?) 参数上限（P2） | 集成 | JOIN 替代 IN，不报参数上限错误 |

---

## 6. 实施顺序（v4 调整）

```text
Phase 1（5-7 天）— ✅ 1.1 已完成，需补 fallback 测试
├── 1.1 db_build.py 多语言 Rust 接入 ✅（需补小批量 parse_file_lang + fallback 集成测试）
├── 1.2 Alignment tests（11 语言，Counter 多重集合比较）
├── 1.3 Benchmark 验证
└── 1.4 Rust parser bug 修复

Phase 3A（13 天）— Local CAS parse cache（per-UID 独立 DB）
├── 3A.1 CAS DB 连接管理 + schema（Local CAS DB + cas_symbol_contents + workspace_symbols）
├── 3A.2 db_cas.py CAS 存储层（原子发布 / query / copy_to_workspace_symbols）
├── 3A.3 db_build.py refresh 流程改造（CAS 查询 + miss parse + 命中复制 + workspace_symbols 生成）
├── 3A.4 Workspace manifest（clean/dirty 分离，cas_key 允许 NULL）
├── 3A.5 多 workspace 隔离改造（resolver + db_query get_callers/callees + graph.rs，v3 P1#4）
├── 3A.6 GC 命令（遍历所有 workspace DB + 临时 mark 表 + 孤儿清理 + 宽限期，v4 P1#2）
├── 3A.7 集成测试（50 workspace 共享 + 崩溃恢复 + 并发同 key + GC 跨 workspace + 自包含回填）
└── 3A.8 schema 预留 workspace_resolved_edges（为 Phase 3B 准备）

─── Phase 2 daemon（优先，按主设计） ───

Phase 3B（10 天）— 建议在 daemon 落地后实施（daemon 内单源实现）
├── 3B.1 workspace_symbols 统一身份表写入逻辑（clean/dirty 投影 + rel_path tombstone）
├── 3B.2 workspace_resolved_edges 写入逻辑（引用 workspace_symbols.id）
├── 3B.3 search_symbols 改造（workspace_symbols JOIN cas_symbols + ATTACH mode=ro）
├── 3B.4 get_callers/get_callees 改造（走 resolved_edges）
├── 3B.5 get_symbol 改造 + dirty overlay 逻辑
└── 3B.6 集成测试
```

**实施顺序说明**（v4 调整）：
- Phase 1 → Phase 3A：立即衔接，拿到"减少重复 parse"收益。Phase 1.1 需补 fallback 集成测试。
- Phase 3A → Phase 2 daemon：Phase 3A 完成后**优先做 daemon**，而非 Phase 3B。
- Phase 2 daemon → Phase 3B：Phase 3B 在 daemon 内单源实现，避免先建一套很快被 Rust daemon 替换的 Python 查询架构。
- Phase 3A 阶段可预留 `workspace_resolved_edges` + `workspace_symbols` schema，为 Phase 3B 做准备。

---

## 7. 待决策项（v4 已解决项标记 ✅）

1. ✅ **CAS 存储位置**（v3 P1#1）：Local CAS 使用独立的 per-UID DB（`~/.callwarden/cas.db`）。
2. ✅ **CAS 自包含**（v4 P1#1）：CAS DB 含 `cas_symbol_contents`，不再有跨库 FK。
3. ✅ **CAS GC 根集合**（v4 P1#2）：GC 遍历所有 workspace DB，有宽限期。
4. ✅ **统一 symbol identity**（v4 P1#3）：`workspace_symbols` 统一身份表，resolved edges 只引用其 id。
5. ✅ **Dirty overlay**（v4 P1#4）：按 rel_path tombstone 屏蔽旧 clean projection。
6. **extraction_config_version 如何管理**：硬编码常量还是从配置文件读取？
   - 建议：硬编码在 `config.py`，配置变更时手动 bump。
7. **Phase 3A 的 `symbols`/`calls` 表是否保留**：Phase 3A 命中 CAS 后仍写入 `symbols`/`calls` + `workspace_symbols`，是否可以跳过 `symbols`/`calls` 写入？
   - 建议：Phase 3A 保留写入（保持查询路径不变），Phase 3B 迁查询路径后评估是否可跳过。
8. **Phase 3B 时机**：Phase 3A 完成后是否立即做 Phase 3B？
   - 评审建议：**不立即做**，优先完成 Phase 2 daemon，Phase 3B 在 daemon 内单源实现。
9. **跨 DB ATTACH 性能**：workspace DB ATTACH CAS DB（mode=ro）后 JOIN 查询性能如何？
   - 需在 Phase 3B 实施时 benchmark 验证。不推荐用"Python 侧合并"作为大型仓库 fallback（评审指出应坚持 JOIN）。
10. **enterprise-architecture-evolution.md 旧章节同步**：该文档 L481 仍写 Phase 1 直接使用 `/var/lib/call_warden/global_cache.db`，与 v4 per-UID Local CAS 和 Phase 2 → Global CAS 顺序冲突。需标记旧章节已废弃或同步更新。

---

## 8. 不做的事（明确排除）

- ❌ 不做 daemon + UDS（Phase 2，但 Phase 3B 建议等 daemon 落地后做）
- ❌ 不做 SO_PEERCRED 权限校验（Phase 2）
- ❌ 不做跨用户授权查询（Phase 2 daemon 落地前）
- ❌ 不共享 resolved_edges（Phase 4+，需 toolchain fingerprint 闭合）
- ❌ 不做 snapshot 级 thin DB 复用（Phase 4+）
- ❌ 不做 toolchain CAS（Phase 6）
- ❌ 不做秒级 watcher（Phase 5）
- ❌ 不做直接 B-tree 页写入（已否决）
- ❌ 不重写 120+ MCP 工具
- ❌ 不删除 Python parser（保留 fallback）
- ❌ Phase 3A 不改查询路径（只加速 parse，projection + symbols/calls 并行写入）
- ❌ Phase 3A 不存 `ref_count`（GC 从 manifest 聚合）
- ❌ CAS 不存路径相关字段（qualified_name/module_path 移至 projection，P1#2）
- ❌ CAS 不存 resolved edge 字段（callee_file/callee_qualified/is_cross_file，P1#2）
