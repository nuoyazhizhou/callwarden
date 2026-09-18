# NF2 修复盘点：summary.generate upsert ON CONFLICT(symbol_hash) 无匹配 UNIQUE 约束

- 卡：`T-1789436399100-948b9498`（父 PYT 卡 `T-1788871227327-45c94bd8`；C-21 期新发现，backlog §W20 F4 相邻登记）
- 步骤：step0（adjudicate）→ step1（implement）→ step2（test）→ step3（verify，含部署门禁）
- 日期：2026-09-15；git HEAD：`1930892`（NF1 闭环后）

## 1. 缺陷 SQL（本 HEAD 实测行号）

`rust_ext/src/daemon/edit_handlers.rs` L575-621 `handle_summary_generate`；缺陷语句 L612-614：

```sql
INSERT INTO symbol_summaries (symbol_hash, summary, model, version, is_current, created_at)
VALUES (?1, ?2, 'rule-based', 1, 1, ?3)
ON CONFLICT(symbol_hash) DO UPDATE SET summary = excluded.summary, is_current = 1, created_at = excluded.created_at
```

`ON CONFLICT(symbol_hash)` 要求 symbol_hash 上存在 PRIMARY KEY 或 UNIQUE 约束。

## 2. 权威 schema 实证（PRAGMA / sqlite_master，生产库只读实测）

```sql
CREATE TABLE symbol_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_hash TEXT NOT NULL,          -- 仅 NOT NULL，无 UNIQUE
    summary TEXT NOT NULL,
    model TEXT DEFAULT 'manual',
    version INTEGER NOT NULL DEFAULT 1,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
-- 索引：idx_summaries_hash(symbol_hash)（非唯一）、idx_summaries_current(is_current)（非唯一）
```

→ upsert 冲突目标不存在，prepare 期报 `ON CONFLICT clause does not match any PRIMARY KEY or
UNIQUE constraint`，handler 返回 `internal_error: symbol_summaries upsert/summary prepare`。
**与 NF1 的失效模式不同**：NF1 是名字解析导致的静默 gate_not_found；NF2 是真 prepare 失败
（异常显性，但方法自上线从未端到端可用；此前被 C-21 已修的路由缺陷「双重掩盖」——写面路由
臂失活使两类缺陷都不可达）。

## 3. 修法裁决（建卡时已裁定，本步复核维持）

**版本化重写**（与 `db/db_summary.py generate_summary` L56-76 语义同源），**不加 UNIQUE 约束**：

```python
# Python 先例（权威语义）：
UPDATE symbol_summaries SET is_current = 0 WHERE symbol_hash = ?
next_version = MAX(version) WHERE symbol_hash = ? 加 1
INSERT INTO symbol_summaries (symbol_hash, summary, model, version, is_current, created_at)
VALUES (?, ?, ?, next_version, 1, ?)
```

B 选项（加 UNIQUE）排除理由：symbol_summaries 设计语义即**同一符号版本化多行**
（`db/db_base.py` `_migrate_v5_to_v6` docstring 明示 + 版本自增逻辑依赖多行历史）；
全列 UNIQUE 会破坏 Python 侧 `db_summary.py` 的多版本写入，属 schema 破坏性变更。
Rust 实现沿用 daemon 内 `unchecked_transaction` 先例（task_collab.rs L897 等）保证
「置旧 → 取版本 → 插新」的原子性。

## 4. 同型扫描（rust_ext 全部 ON CONFLICT(symbol_hash) 引用）

- `edit_handlers.rs` L611-617：本缺陷（唯一坏点）。
- `job_runner.rs` symbol_embeddings upsert：**合法**——symbol_embeddings 的
  symbol_hash 为 PRIMARY KEY，冲突目标存在，不在本卡范围（建卡时已裁决）。

## 5. NF2 锚（C-21 测试文件 L776-784）

`test_summary_generate_nf2_known_defect`：xfail(strict) 正例体形态——断言
`summary.generate` 无错误 + `summaries_generated >= 2` + symbol_summaries 行含 `c21_fn_a`。
fixture 已预置两个 function 符号（`c21_fn_a`/`c21_fn_b`，hash `c21-sym-a`/`c21-sym-b`，
file_instance 挂 WS_ID=1 active 实例）→ **无需新增种子**（与 NF1 需 4 行 FK 种子不同）。
迁移 = 删除 xfail 标记保留断言 + 增补版本化行为断言（旧行 is_current=0、新行
version ≥ 1 且 is_current=1）。

## 6. 路由层

零触碰（C-21 已修路由臂；本卡仅 handler 内 SQL 语义改写）。

## 7. 行为曲线与 A/B 计划

| 阶段 | 二进制 | summary.generate 行为 |
|---|---|---|
| 修复前 | 生产现役 `26519773…`（NF1 修复版） | internal_error（prepare 失败：冲突目标不存在），锚 xfail 失败成立 |
| 修复后 | target-nf2 release | ok + summaries_generated=2 + 版本化落库，锚转绿 |

- before 腿：现役部署二进制跑 NF2 锚（预期 1F：summary prepare internal_error）。
- after 腿：修复二进制跑 22 测试矩阵（预期 22P/0F/0xf，NF1+NF2 双锚全绿）。
- 判别锚：1F → 0F。

## 8. 验证计划（step2/step3）

1. `cargo check --all-targets`（隔离 target-nf2）零 error；
2. 隔离矩阵（修复二进制）22/0/0/0（NF1 锚保持绿、NF2 锚转绿、xfail 归零）；
3. `cargo test --release --lib` 同集对照（C-21/C-13/NF1 基线 1778P/6F）零新增失败；
4. `git diff --check` → 按 C-21/NF1 时序先提交修复（本卡 task_id 前缀）再
   `refresh_shared_runtime.ps1`（health.git_commit==HEAD、三方 sha256、PID、rollback=false）；
5. 生产只读探针：`summary.generate {"target": "nf2-probe-nonexistent"}`（qualified_name
   精确匹配不命中 → 0 行 → 零写入）证明端点存活。**判别力披露**：对 0 符号命中新旧
   二进制均返回 ok/0（INSERT prepare 只发生在循环体内，0 行不触发）→ 探针不具判别力，
   判别力来自隔离 A/B + 三方 sha256 链；生产不含真实符号摘要写（无合成写）。
