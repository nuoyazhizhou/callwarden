# Enterprise Phase 1 + Phase 3A + Phase 3B 详细实施设计

状态：Draft v4（评审第三轮 P1 修订版）
日期：2026-07-10
父文档：
- [enterprise-daemon-shared-snapshot-plan.md](enterprise-daemon-shared-snapshot-plan.md)（主架构）
- [enterprise-architecture-evolution.md](enterprise-architecture-evolution.md)（演进背景）

## v4 变更摘要（针对评审第三轮 4 个 P1）

1. **[P1#1] CAS 自包含**：新增 `cas_symbol_contents` 表到 CAS DB（存符号正文 content + start_byte/end_byte），移除跨库 FK（`symbol_hash` 不再指向 workspace DB 的 `symbol_contents`）。CAS 命中后可独立回填 workspace 的 `symbol_contents`。
2. **[P1#2] GC 根集合完整**：GC 遍历 `~/.callwarden/<hash>/` 下**所有** workspace DB 的 manifest，聚合完整 live key 集合。增加 workspace 失联宽限期（不立即视为无引用）。
3. **[P1#3] 统一 symbol identity**：引入 `workspace_symbols` 统一身份表，clean/dirty 都投影到此表。`workspace_resolved_edges.caller_symbol_id` / `callee_symbol_id` 只引用 `workspace_symbols.id`，消除 ID 碰撞。`cas_symbols` 增加 `local_qualified_name`（内容级，含 lexical parent chain），`workspace_symbols.qualified_name` = `module_path + local_qualified_name`。
4. **[P1#4] Dirty overlay 按 rel_path 屏蔽**：文件变 dirty 时在 workspace 事务中删除该 rel_path 的旧 projection（tombstone by rel_path）。Clean 查询 JOIN manifest 校验 `is_dirty = 0`。Overlay 边界改为 rel_path，不是 qualified_name。

**v3 已修复的 5 个 P1**（保留）：CAS 物理存储闭合 / 内容级路径级拆分 / resolved edge store / 多 workspace 隔离扩展 / CAS 原子发布协议。

## v4 P2 修复

- CAS DB 并发：`busy_timeout=5000` + 有界重试 + 重新检查 `state=ready`（不再假设单进程独占）。
- CAS DB schema version：增加 `cas_schema_meta` 表记录 `cas_schema_version` / `min_reader_version`。
- UNIQUE 约束：`cas_raw_calls` 增加 `call_ordinal` 列避免吞掉同行重复调用；resolved edge 同理。
- ATTACH 只读：用 `ATTACH 'file:...?mode=ro' AS cas_db`，JOIN 用 `cas_db.cas_symbols` 全名。
- `enterprise-architecture-evolution.md` 旧章节标记废弃。

## 实施范围（v3 调整后）

- **Phase 1**：Rust 多语言 parse 接入主路径（5-7 天）— ✅ 1.1 已完成
- **Phase 3A**：Local CAS parse cache（per-UID 独立 DB），只承诺减少重复 parse（10-11 天）
- **Phase 3B**：workspace 查询路径迁移 + resolved edge store（10 天）— **建议在 Phase 2 daemon 落地后实施**

Phase 2（daemon skeleton + UDS）、Phase 4+（snapshot sharing / ACL / GraphSnapshot / resolved_edges 共享）按主设计延后。本文档**不承诺**跨用户授权查询、不承诺 resolved_edges 共享、不承诺 snapshot 级 thin DB 复用。

**评审建议的实施顺序**：Phase 1 → Phase 3A（Local CAS）→ **Phase 2 daemon（优先）** → Phase 3B（daemon 内单源实现）。Phase 3B 在 daemon 落地前可做 schema 预留，但不建议抢先实现 Python 查询架构（会被 Rust daemon 替换）。

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
- 与 Python parser 核心字段 alignment test 通过率 ≥ 99%（按归一化 key 比较）。

Phase 3A：
- **单用户** 50 个同 repo clean workspace 中，相同文件 parse 只发生一次（CAS 命中率 ≥ 95%，验收口径 **parse miss = 0**）。
- 第二个 clean workspace 注册后，分别记录 hash / CAS 查询 / 复制 / resolve / 写库耗时，**parse 耗时 = 0**（不再用"低于第一个 10%"作为验收口径，因为复制和 resolve 仍需时间）。
- `resolved_edges` 一律 per-workspace 存储，不共享。
- Dirty workspace 修改不污染共享 CAS。
- CAS 写入原子性：崩溃后 CAS 表无半成品记录（`state=building` 条目被 GC 清理）。

Phase 3B：
- 查询路径走 `workspace_symbol_projection` + `workspace_resolved_edges`，dirty 走 `symbols`/`calls` overlay。
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

# 按 language 分组
rust_langs = set(supported_languages())  # 11 种
rust_files = [x for x in to_parse if x[3] in rust_langs]
non_rust_files = [x for x in to_parse if x[3] not in rust_langs]

# Rust 路径：按语言分组调用 batch_parse_files_lang_pool
if rust_files and _can_use_rust_parse() and not os.environ.get("CW_DISABLE_RUST_PARSE"):
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
        pool = batch_parse_files_lang_pool(rust_args, lang, num_threads=mp_workers)
        
        # 流式回传：逐个 get_at 转 dict 写入 file_results
        for i, (abs_path, module_path, rel_path, file_instance_id) in enumerate(filtered):
            r = pool.get_at(i)
            if r.get("error"):
                failed += 1
                failed_files.append((rel_path, r["error"]))
                continue
            r["abs_path"] = abs_path
            r["file_instance_id"] = file_instance_id
            r["module_path"] = module_path
            r["rel_path"] = rel_path
            r.setdefault("inline_modules", [])
            file_results[rel_path] = r

# 非 Rust 支持语言走原 Python ProcessPoolExecutor
if non_rust_files:
    _python_multiprocess_parse(non_rust_files, mp_workers, file_results, ...)
```

关键点：
- **保留 `batch_parse_c_files_pool`** 作为 C 语言专用快路径（已稳定，不破坏）。
- **新增 `batch_parse_files_lang_pool`** 作为多语言通用路径。
- **小批量用 `parse_file_lang`（单文件）**：文件数 < 阈值时（如 < 50），用单文件 Rust parse 避免线程池开销。大批量（≥ 50）用 `batch_parse_files_lang_pool`。Phase 1.1 实现中已加此阈值，但需补集成测试覆盖小批量路径（v4 Phase 1.1 复核修复）。
- 资源文件预过滤（`_is_resource_file`）仍在 Python 侧执行，避免 Rust 读取大文件。
- 失败 fallback：Rust 扩展不可用、某语言 Rust 不支持、单文件 parse 异常 → 回退 Python parser。需补 **Rust pool 运行时异常**和**单文件 error 后真正回退 Python**的集成测试。

#### 2.2.2 Alignment Tests（Counter 多重集合比较）

新增 `tests/test_rust_python_alignment.py`：

```python
# 对每种语言，准备 5-10 个样本文件，覆盖：
# - 函数/方法/类/结构体/枚举/interface/trait 等所有 symbol kind
# - 嵌套定义（类内方法、impl 块内函数）
# - 调用表达式（普通调用、方法调用、链式调用）
# - import/include 声明
# - 含语法错误的文件（error 字段非空）

from collections import Counter

def normalize_symbols(symbols):
    """按 (qualified_name, kind, start_line, end_line) 归一化为 Counter 多重集合。

    用 Counter 而非 dict：同一行可能有多个相同调用（如 foo(); foo();），
    dict 会吞掉重复，Counter 保留多重性。
    """
    return Counter(
        (s["qualified_name"], s["kind"], s["start_line"], s["end_line"])
        for s in symbols
    )

def normalize_calls(calls):
    """按 (caller_qualified, callee_name, call_line) 归一化为 Counter。"""
    return Counter(
        (c["caller_qualified"], c["callee_name"], c["call_line"])
        for c in calls
    )

@pytest.mark.parametrize("lang", [
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
])
def test_symbol_alignment(lang, sample_files, known_diff_whitelist):
    """Rust parser 与 Python parser 输出的 symbols 核心字段一致（Counter 多重集合比较）。"""
    for path in sample_files[lang]:
        py_result = python_parser.parse_file(path, module_path="")
        rs_result = parse_file_lang(path, module_path="", lang=lang)

        py_syms = normalize_symbols(py_result["symbols"])
        rs_syms = normalize_symbols(rs_result["symbols"])

        # Counter 减法：找出多/少的条目（保留多重性）
        missing_in_rs = py_syms - rs_syms   # Python 有但 Rust 没有
        missing_in_py = rs_syms - py_syms   # Rust 有但 Python 没有

        # 过滤已知差异白名单（显式管理例外，不静默容忍）
        diff_key = (lang, path)
        if diff_key in known_diff_whitelist:
            allowed = known_diff_whitelist[diff_key]
            for k in list(missing_in_rs):
                if k in allowed:
                    del missing_in_rs[k]
            for k in list(missing_in_py):
                if k in allowed:
                    del missing_in_py[k]

        total = max(sum(py_syms.values()), sum(rs_syms.values()), 1)
        diff_count = sum(missing_in_rs.values()) + sum(missing_in_py.values())
        diff_rate = diff_count / total
        assert diff_rate < 0.01, (
            f"{path}: symbol diff rate {diff_rate:.2%} > 1%\n"
            f"  missing_in_rs: {dict(missing_in_rs)}\n"
            f"  missing_in_py: {dict(missing_in_py)}"
        )

        # raw_calls 也纳入对齐
        py_calls = normalize_calls(py_result.get("raw_calls", []))
        rs_calls = normalize_calls(rs_result.get("raw_calls", []))
        # ... 同样集合比较
```

通过率门槛：
- ≥ 99% 的 symbol 核心字段（name/qualified_name/kind/start_line/end_line）一致。
- < 1% 差异进入"已知差异清单"，逐项分析是 Python bug 还是 Rust bug。
- raw_calls / imports 也纳入对齐验收。

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
| Rust parser 与 Python parser 结果不一致 | alignment tests 锁定差异，< 1% 差异进清单 |
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
    cas_key TEXT PRIMARY KEY,          -- sha256(content_hash + language + parser_version + cw_version + extraction_config_version)
    content_hash TEXT NOT NULL,        -- 文件内容 hash（用于快速查找）
    language TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL,
    callwarden_version TEXT NOT NULL,
    extraction_config_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',  -- building / ready（P1#5 原子发布）
    parsed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cas_content_hash ON cas_file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_language ON cas_file_cache(language);

-- CAS 符号内容表（v4 P1#1 修复：CAS 自包含，不再外键指向 workspace DB 的 symbol_contents）
CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,      -- 符号正文内容 hash
    content TEXT NOT NULL,             -- 符号正文文本
    start_byte INTEGER DEFAULT 0,
    end_byte INTEGER DEFAULT 0
);

-- CAS 符号表：内容级符号事实（不含路径信息）
-- qualified_name / module_path 已移除，由 workspace_symbols 生成
CREATE TABLE IF NOT EXISTS cas_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    local_symbol_id INTEGER NOT NULL,  -- 文件内序号（与 cas_raw_calls 关联用）
    symbol_content_hash TEXT NOT NULL,  -- 关联 cas_symbol_contents（CAS DB 内部 FK，自包含）
    name TEXT NOT NULL,                -- 短名（如 "parse_file"）
    local_qualified_name TEXT NOT NULL, -- 内容级限定名（如 "Parser.tokenize"，含 lexical parent chain，不含 module_path）
    lexical_parent_local_id INTEGER DEFAULT 0, -- 词法父符号的 local_symbol_id（0 = 无父）
    kind TEXT NOT NULL,                -- fn / class / method / struct ...
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
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
    caller_local_id INTEGER NOT NULL,  -- 关联 cas_symbols.local_symbol_id
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
```

**v4 P1#1 自包含说明**：
- `cas_symbol_contents` 存储符号正文（与现有 workspace DB 的 `symbol_contents` 表结构一致）。
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
    cas_key TEXT DEFAULT NULL,           -- clean: 指向 cas_file_cache；dirty: NULL
    content_hash TEXT NOT NULL,         -- 文件内容 hash（clean 和 dirty 都填）
    is_dirty INTEGER DEFAULT 0,         -- 1 = dirty overlay，独立 parse
    mtime REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),  -- 允许 NULL
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

### 3.4 CAS Key 设计

```python
def compute_cas_key(content_hash, language, parser_version, callwarden_version, extraction_config_version):
    """计算 CAS key：包含 parser/version，升级后旧条目自然失效"""
    raw = f"{content_hash}|{language}|{parser_version}|{callwarden_version}|{extraction_config_version}"
    return hashlib.sha256(raw.encode()).hexdigest()
```

关键点：
- `content_hash` 是文件内容 hash（已有，`file_contents.content_hash`）。
- `parser_version` 是 tree-sitter grammar 版本（如 `tree-sitter-c v0.24`）。
- `callwarden_version` 是 Call Warden 版本（如 `0.2.0-p29`）。
- `extraction_config_version` 是 SymbolRule/CallRule 配置版本（配置变更时手动 bump）。
- 升级 parser 或配置后，`cas_key` 改变，旧条目自然失效（不删除，等 GC 清理）。

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
   b. 计算 cas_key = sha256(content_hash + language + parser_version + cw_version + config_version)
   c. 查 cas_file_cache WHERE cas_key = ? AND state = 'ready'  ← 只认 ready 状态
   d. 命中 → 跳过 parse，从 CAS 复制到 workspace projection + symbols/calls
   e. 未命中 → CAS 原子发布协议（见下）→ 写 projection + symbols/calls
3. 写 workspace_manifests（clean: cas_key 非空；dirty: cas_key NULL + 走 step 2e 的独立 parse）
4. 解析 resolved_edges（依赖 build context，每 workspace 独立，不共享）
```

#### 3.5.1 CAS 原子发布协议（P1#5 修复）

**问题**：原设计先写 `cas_file_cache`，再写子表。进程中途崩溃会导致"只有父记录、缺少 symbols/calls"的半成品条目被下次误判为命中。

**方案**：单事务原子发布 + `state` 状态机。

```python
def cas_publish(cas_key, parse_result, cas_conn):
    """原子发布 CAS 条目（单事务，崩溃安全）"""
    try:
        cas_conn.execute("BEGIN IMMEDIATE")  # 获取写锁
        # 1. 先插父记录，state=building（不可被命中）
        cas_conn.execute(
            "INSERT OR IGNORE INTO cas_file_cache "
            "(cas_key, content_hash, language, state, parsed_at, ...) VALUES (?, ?, ?, 'building', ?, ...)",
            (cas_key, content_hash, language, time.time(), ...)
        )
        # 检查是否已存在 ready 条目（并发同 key）
        row = cas_conn.execute(
            "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
        ).fetchone()
        if row and row["state"] == "ready":
            cas_conn.execute("ROLLBACK")
            return  # 已有 ready 条目，跳过

        # 2. 写子表（同一事务，UNIQUE 约束防重）
        for sym in parse_result["symbols"]:
            cas_conn.execute(
                "INSERT OR IGNORE INTO cas_symbols "
                "(cas_key, local_symbol_id, name, kind, ...) VALUES (?, ?, ?, ?, ...)",
                (cas_key, sym["local_id"], sym["name"], sym["kind"], ...)
            )
        for call in parse_result["raw_calls"]:
            cas_conn.execute(
                "INSERT OR IGNORE INTO cas_raw_calls "
                "(cas_key, caller_local_id, caller_name, callee_name, call_line) "
                "VALUES (?, ?, ?, ?, ?)",
                (cas_key, call["caller_local_id"], call["caller_name"],
                 call["callee_name"], call["call_line"])
            )

        # 3. 最后发布：state=building → ready（同一事务）
        cas_conn.execute(
            "UPDATE cas_file_cache SET state = 'ready' WHERE cas_key = ?",
            (cas_key,)
        )
        cas_conn.execute("COMMIT")
    except Exception:
        cas_conn.execute("ROLLBACK")
        # 清理 building 条目（下次重试）
        cas_conn.execute(
            "DELETE FROM cas_file_cache WHERE cas_key = ? AND state = 'building'",
            (cas_key,)
        )
        raise
```

**关键点**：
- `state=building` 的条目**不可被命中**（查询条件加 `WHERE state = 'ready'`）。
- 崩溃后 GC 清理 `state=building` 的孤儿条目（见 §3.8）。
- 子表有 `UNIQUE` 约束，并发同 key 写入时 `INSERT OR IGNORE` 幂等。
- 整个发布在**单事务**内完成，要么全写入要么全回滚。

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

### 3.8 GC 策略（v4 P1#2 修复：扫描所有 workspace DB + 宽限期）

**v3 问题**：`gc_cas(cas_conn, ws_conn)` 只读取当前 workspace 的 manifest，却清理整个 per-UID CAS。在 workspace A 运行 GC 会删掉只被 B/C 引用的 key。

**v4 修复**：GC 遍历 `~/.callwarden/<hash>/` 下**所有** workspace DB，聚合完整 live key 集合。失联 workspace 有宽限期。

```python
def gc_cas(cas_conn, grace_period_days=7):
    """清理无引用的 CAS 条目 + 孤儿 building 条目

    v4 P1#2 修复：扫描所有 workspace DB，不只当前 workspace。
    """
    import glob
    cas_conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 遍历 ~/.callwarden/<hash>/ 下所有 workspace DB
        cas_dir = os.path.expanduser("~/.callwarden")
        live_keys = set()
        now = time.time()
        grace_threshold = now - grace_period_days * 86400

        for ws_db_path in glob.glob(os.path.join(cas_dir, "*", "callwarden.db")):
            try:
                ws_conn = sqlite3.connect(f"file:{ws_db_path}?mode=ro", uri=True)
                # 检查是否有 workspace_manifests 表（旧版本可能没有）
                has_table = ws_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_manifests'"
                ).fetchone()
                if not has_table:
                    ws_conn.close()
                    continue
                # 聚合该 workspace 的 live keys
                rows = ws_conn.execute(
                    "SELECT DISTINCT cas_key FROM workspace_manifests WHERE cas_key IS NOT NULL"
                ).fetchall()
                live_keys.update(r["cas_key"] for r in rows)
                ws_conn.close()
            except Exception:
                # DB 被锁或损坏 → 跳过（宽限期保护）
                continue

        # 2. 用临时 mark 表避免 IN 参数上限
        cas_conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gc_live (cas_key TEXT PRIMARY KEY)")
        cas_conn.execute("DELETE FROM _gc_live")
        cas_conn.executemany("INSERT OR IGNORE INTO _gc_live VALUES (?)",
                             [(k,) for k in live_keys])

        # 3. 删除无引用条目（只删 ready，building 由步骤 4 处理）
        cas_conn.execute("""
            DELETE FROM cas_symbol_contents
            WHERE content_hash NOT IN (
                SELECT symbol_content_hash FROM cas_symbols
            )
            AND content_hash NOT IN (
                SELECT sc.content_hash FROM cas_symbols cs
                JOIN cas_symbol_contents sc ON cs.symbol_content_hash = sc.content_hash
                WHERE cs.cas_key IN (SELECT cas_key FROM _gc_live)
            )
        """)
        cas_conn.execute("""
            DELETE FROM cas_symbols WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
        """)
        cas_conn.execute("""
            DELETE FROM cas_raw_calls WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
        """)
        cas_conn.execute("""
            DELETE FROM cas_imports WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
        """)
        cas_conn.execute("""
            DELETE FROM cas_file_cache
            WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
              AND state = 'ready'
        """)

        # 4. 清理孤儿 building 条目（P1#5 崩溃残留）
        cas_conn.execute("DELETE FROM cas_file_cache WHERE state = 'building'")
        cas_conn.execute("""
            DELETE FROM cas_symbols
            WHERE cas_key NOT IN (SELECT cas_key FROM cas_file_cache)
        """)
        cas_conn.execute("""
            DELETE FROM cas_raw_calls
            WHERE cas_key NOT IN (SELECT cas_key FROM cas_file_cache)
        """)

        cas_conn.execute("DROP TABLE _gc_live")
        cas_conn.execute("COMMIT")
    except Exception:
        cas_conn.execute("ROLLBACK")
        raise
```

- GC 命令：`cw gc cas [--grace-days 7]`，手动触发，默认不自动 GC。
- **遍历所有 workspace DB**（`~/.callwarden/<hash>/callwarden.db`），聚合完整 live key 集合。
- **宽限期**：workspace DB 被锁或损坏时跳过（不删除其引用的 key），默认 7 天。
- `cas_symbol_contents` 也参与 GC（清理无符号引用的正文）。
- 清理 `state=building` 孤儿条目（P1#5 崩溃残留）。

### 3.9 迁移策略（v4 P2 修复：CAS schema version）

- **CAS DB**（`~/.callwarden/cas.db`）：首次运行时按 `cas_schema_meta` 中的 `cas_schema_version` 创建。不同 Call Warden 版本/容器并存时，通过 `min_reader_version` 兼容性检查。旧版本 reader 遇到新 schema 时报错提示升级，不静默读取错误数据。
- **Workspace DB**：schema migration 添加 `workspace_symbols` + `workspace_resolved_edges` + `workspace_manifests` 表（不影响现有表），首次 refresh 时回填。
- **Local CAS → Global CAS 迁移**（Phase 2 daemon 落地时）：daemon 启动时将 `~/.callwarden/cas.db` 导入 `/var/lib/callwarden/cas.db`，后续 Local CAS DB 废弃。冷启动策略由 daemon 管理。
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
| CAS 写入崩溃产生半成品 | P1#5 原子发布协议：state=building → ready，GC 清理孤儿 |
| CAS DB 跨进程写锁 | Phase 3A 单用户单进程模型；多进程并发需 Phase 2 daemon |

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
    FOREIGN KEY (caller_symbol_id) REFERENCES workspace_symbols(id),
    FOREIGN KEY (callee_symbol_id) REFERENCES workspace_symbols(id),
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

### 4.3 查询路径改造（v4 P1#4：Dirty overlay 按 rel_path 屏蔽）

**v3 问题**：Clean 查询直接读取 `workspace_symbol_projection`，没有 JOIN manifest 检查 `is_dirty=0`。文件从 clean 变 dirty 后，旧 projection 仍被返回。按 `qualified_name` 让 dirty 优先会误删其他文件中的合法同名符号。

**v4 修复**：
1. 文件变 dirty 时，在 workspace 事务中**删除该 rel_path 的旧 `workspace_symbols`**（tombstone by rel_path）。
2. Clean 查询 JOIN manifest 校验 `wm.is_dirty = 0`（双重保险）。
3. Overlay 边界是 **rel_path**，不是 qualified_name。

```python
def mark_file_dirty(workspace_id, rel_path, ws_conn):
    """文件变 dirty 时，删除旧 clean projection（tombstone by rel_path）"""
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 删除该 rel_path 的旧 workspace_symbols（clean projection）
        ws_conn.execute(
            "DELETE FROM workspace_symbols WHERE workspace_id = ? AND rel_path = ? AND source = 'cas'",
            (workspace_id, rel_path)
        )
        # 2. 删除该 rel_path 的旧 resolved edges（caller 或 callee 指向已删除的 symbol）
        ws_conn.execute("""
            DELETE FROM workspace_resolved_edges
            WHERE workspace_id = ? AND (
                caller_symbol_id IN (
                    SELECT id FROM workspace_symbols WHERE workspace_id = ? AND rel_path = ?
                )
                OR callee_symbol_id IN (
                    SELECT id FROM workspace_symbols WHERE workspace_id = ? AND rel_path = ?
                )
            )
        """, (workspace_id, workspace_id, rel_path, workspace_id, rel_path))
        # 3. 更新 manifest 为 dirty
        ws_conn.execute(
            "UPDATE workspace_manifests SET is_dirty = 1, cas_key = NULL WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path)
        )
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise


def search_symbols(query, kind, limit, workspace_id, ws_conn, cas_attached):
    """查询符号：clean 走 workspace_symbols JOIN cas_symbols，dirty 走 workspace_symbols

    cas_attached: workspace DB 已 ATTACH CAS DB 为 cas_db（mode=ro）
    """
    # Clean 文件：workspace_symbols JOIN cas_db.cas_symbols（ATTACH 只读）
    # JOIN manifest 校验 is_dirty = 0（双重保险，防止 tombstone 未清理的残留）
    sql = """
        SELECT wsym.qualified_name, wsym.module_path, wsym.rel_path as file_path,
               cs.start_line, cs.end_line, cs.depth, csym.name, csym.kind,
               csym.signature, csym.has_comment
        FROM workspace_symbols wsym
        JOIN workspace_manifests wm ON wsym.workspace_id = wm.workspace_id AND wsym.rel_path = wm.rel_path
        JOIN cas_db.cas_symbols csym ON wsym.cas_key = csym.cas_key AND wsym.local_symbol_id = csym.local_symbol_id
        JOIN cas_db.cas_symbol_contents cs ON csym.symbol_content_hash = cs.content_hash
        WHERE wsym.workspace_id = ?
          AND wm.is_dirty = 0
          AND wsym.source = 'cas'
          AND (wsym.name LIKE ? OR wsym.qualified_name LIKE ?)
    """
    params = [workspace_id, f"%{query}%", f"%{query}%"]
    if kind:
        sql += " AND csym.kind = ?"
        params.append(kind)
    sql += " ORDER BY csym.kind, csym.depth DESC, wsym.rel_path, csym.start_line LIMIT ?"
    params.append(limit)
    clean_results = [dict(r) for r in ws_conn.execute(sql, params)]

    # Dirty 文件：workspace_symbols（source='dirty'）
    sql_dirty = """
        SELECT wsym.qualified_name, wsym.module_path, wsym.rel_path as file_path,
               wsym.start_line, wsym.end_line, wsym.depth, wsym.name, wsym.kind,
               s.signature, s.has_comment
        FROM workspace_symbols wsym
        JOIN symbols s ON wsym.symbols_rowid = s.id
        WHERE wsym.workspace_id = ?
          AND wsym.source = 'dirty'
          AND (wsym.name LIKE ? OR wsym.qualified_name LIKE ?)
        ORDER BY wsym.kind, wsym.depth DESC, wsym.rel_path, wsym.start_line LIMIT ?
    """
    dirty_results = [dict(r) for r in ws_conn.execute(sql_dirty, [workspace_id, f"%{query}%", f"%{query}%", limit])]

    # UNION 返回（不需要按 qualified_name 去重，因为 dirty 的 rel_path 已经 tombstone 了 clean）
    return clean_results + dirty_results
```

**v4 关键点**：
- Clean 查询 JOIN `workspace_manifests` 校验 `wm.is_dirty = 0`，防止旧 projection 残留。
- 文件变 dirty 时先删除旧 clean projection（tombstone by rel_path），再插入 dirty projection。
- Overlay 边界是 **rel_path**，不会误删其他文件中的同名符号。
- `workspace_resolved_edges` 的 `source` 字段区分 clean/dirty 边，查询时不需要 UNION，单表查即可。

**跨 DB ATTACH 只读**（v4 P2 修复）：

```python
# ATTACH 时用 mode=ro 确保只读，不引入写锁
ws_conn.execute("ATTACH 'file:{cas_db_path}?mode=ro' AS cas_db")
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
| 11 语言 alignment tests（Counter 多重集合比较） | 单元 | 核心字段一致率 ≥ 99% |
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
