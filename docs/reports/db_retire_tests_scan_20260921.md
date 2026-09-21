# tests/ db/ 引用扫描报告（db/ 退休验收③ 前置）

**扫描时间**: 2026-09-21
**扫描器**: `.workbuddy/scripts/scan_db_refs.py`
**明细**: `.workbuddy/scripts/scan_db_refs_report.json`

## 1. tests/ 引用形态分类

| 形态 | 文件数 | 说明 |
|---|---|---|
| `import callwarden.db...` / `from callwarden.db import ...` | **161** | 运行期包导入（backlog 早期快照为 154） |
| `callwarden.db.xxx` 属性访问 | **216** | 含上面 161 的超集（RpcDBProxy / 直接引用） |
| `'db/xxx.py'` 路径字符串 | **11** | 文件存在性断言 / 内容读取 |
| `"db"` 路径组件（os.path.join） | **28** | 同上，Path 组件形态 |
| **总命中文件（任一形态）** | **240** | |
| **运行期依赖 db/ 包（import ∪ attr）** | **216** | 删 db/ 后 import 期即断 |
| **仅路径/组件形态** | **39** | 删 db/ 后断在文件存在性断言 |

**注**：裸 `from db import` / `import db`（无 callwarden 前缀）= **0**；
大量 `db.xxx` 是局部 sqlite 连接变量（`db.conn.execute`），非包引用，已排除。

## 2. 导入目标分布（top）

| 目标模块 | 文件数 |
|---|---|
| `callwarden.db.db` | 95 |
| `callwarden.db`（壳） | 51 |
| `callwarden.db.schema` | 41 |
| `callwarden.db.db_build` | 31 |
| `callwarden.db.db_base` | 26 |
| `callwarden.db.db_workspace_manifest` | 19 |
| `callwarden.db.db_toolchain` | 13 |
| `callwarden.db.db_cas` | 11 |
| 其余 22 个 db_* 子模块 | 各 1–6 |

**符号集中度极高**：`CodeGraphDB`（136 文件）、`SCHEMA_VERSION`（26+）、
`upsert_manifest`(9)、`init_manifest_schema`(6)、`BuildMixin`(6)、
`register_build_context`(6)、`SCHEMA_SQL/SCHEMA_INDEXES_SQL`(各 5)。
共 89 个不同符号。

## 3. ⚠️ 生产侧 purity 并未完成（重要更正）

MEMORY.md 此前记录"phase-3 生产侧已完成（cw.py/replicator/cli_admin/daemon_server
4 文件全断 db）"。**完整复扫（`.workbuddy/scripts/purity_scan_prod.py`，
输出不截断）发现 23 个生产文件 / 29 处仍引用 db**：

**server/ 运行时（12 文件）**

| 文件 | 位置 | 形态 |
|---|---|---|
| `server/mcp_server.py` | L28 | 🔴 模块级，**但经核实为死导入**（全文件仅 1 次出现，`get_db` 实从 `_mcp_common` 导入）→ 可直接删 |
| `server/_mcp_common.py` | L12 | 🔴 模块级 + `get_db()` 内 `CodeGraphDB(...)` 实例化（L81/83/91）— **中枢** |
| `server/job_executor.py` | L43 | 🔴 模块级 `db_jobs` CRUD |
| `server/job_handlers.py` | L149/156/163/168/191/197/202 | 🟡 方法内懒加载 ×7（clone_detection/rollback_config/vector mixin 委托） |
| `server/tools/tools_collab.py` | L36 | 🟡 `_bind_readonly_db` = `object.__new__(CodeGraphDB)` legacy 只读回退 |
| `server/tools/tools_p2_graph.py` | L21 | 🟡 同上 |
| `server/tools/tools_security.py` | L28 | 🟡 同上 |
| `server/tools/tools_semantic.py` | L26 | 🟡 同上 |
| `server/tools/tools_summary.py` | L29 | 🟡 同上 |
| `server/tools/tools_task.py` | L39 | 🟡 同上 |

**cicd/（2 文件）**：`bootstrap_check.py:35`（模块级）、`github_action.py:54`（try 内）

**scripts/（1 文件）**：`test_impact_en.py:20`

**deliverables/ + docs/（10 文件，交付/文档脚本，非运行时）**：
`deliverables/software-company/` 7 个、`docs/_create_subtasks_v2.py`、
`docs/create_r8_r11_subtasks.py`、`docs/design/g0_create_review_task.py`

**.temp/ + .trae-cn/（25 文件 / 40 处，scratch，不计入）**

### 关键架构事实

- `tools_*.py` 的**主路径已是 daemon RPC 薄壳**（docstring 明确"业务校验/落库全在
  Rust daemon，不回退本地 SQLite"）；`_bind_readonly_db`（`object.__new__(CodeGraphDB)`
  注入 worker 只读连接）是 **legacy 只读回退**，非 HTTP 模式下触发。
- `server/_mcp_common.py::get_db()` 是本地 CodeGraphDB 实例化的**唯一中枢**。

### 行为级验证（`__init__.py` 阻塞，P0 已解除）

```
import callwarden
→ callwarden.db.* modules loaded at import time: 47  （P0 前）
→ 0                                                    （P0 后）
```

P0 前**任何 `import callwarden` 都会急切加载 47 个 db 子模块**；
`callwarden.CodeGraphDB` 顶层属性经扫描**无外部使用者**，已安全摘除（P0 完成）。

> **修正记录**：本节首版因 purity 扫描输出经 `head -60` 截断，仅列出 6 个文件，
> 完整复扫更正为 23 文件 / 29 处。

## 4. db/ 现状

- `db/` 目录 **54 个 .py**；`db/__init__.py` 已是兼容壳（`from .db import CodeGraphDB`）。
- `db/db.py` = 30+ Mixin 组合的 `CodeGraphDB` 遗留类（Build/Query/Task/Clone/
  AuditChain/AgentRules/... 全部业务逻辑仍在此 Python 层）。
- `callwarden/__init__.py` 仍把 `CodeGraphDB` 作为核心导出类。

## 5. 处理建议（按阻塞顺序）

**P0 —— 摘除 `__init__.py` 的 db 导入（1 文件，解除硬阻塞）**
删除 `from .db import CodeGraphDB` 与 `__all__` 中的对应项。
验证：`import callwarden` 后 `sys.modules` 不含 `callwarden.db*`。
顶层 `callwarden.CodeGraphDB` 无使用者，安全。

**P1 —— 生产侧 23 文件断 db**
按风险分批：
- **P1-0（死导入，零风险）**：`server/mcp_server.py:28` 删除未使用的 import。
- **P1-1（中枢）**：`server/_mcp_common.py::get_db()` 改走 daemon RPC（fail-closed）。
- **P1-2（legacy 只读回退 ×6）**：`server/tools/tools_*.py` 的 `_bind_readonly_db`
  （`object.__new__(CodeGraphDB)`）改为 daemon 只读 RPC 或 fail-closed。
- **P1-3（jobs 层）**：`server/job_executor.py`（db_jobs CRUD）+
  `job_handlers.py`（×7 mixin 懒加载委托）改 daemon `mcp.job_executor.*` /
  clone/vector RPC。
- **P1-4（cicd/scripts）**：`cicd/bootstrap_check.py`、`cicd/github_action.py`、
  `scripts/test_impact_en.py`。
- **P1-5（非运行时脚本 10 文件）**：`deliverables/software-company/*`、`docs/*`
  —— 改 import 或标记遗留。

**P2 —— tests/ 216 运行期依赖文件**
按导入符号分批，最大批次 = CodeGraphDB（136 文件）。
两条路线：
- **路线 A（RPC 迁移）**：测试改用 daemon RPC。工作量大，每个测试需
  live daemon + workspace 建栈（conftest 已有 W3 隔离 daemon 模式可复用）。
- **路线 B（测试支持层）**：CodeGraphDB 保留为**仅测试支持**的遗留类
  （移入 `tests/_support/` 或保留 db/ 但生产零导入），
  验收③ 重新定义为「生产 purity + import callwarden 不加载 db」。

**P3 —— tests/ 39 路径/组件断言文件**
`db/schema.py`、`db/db_base.py` 等存在性/内容断言，随 db/ 下线一并删除或改断言。

**P4 —— db/ 物理删除（54 文件）**
P0–P3 全部完成后执行；删除后重跑全量 pytest 验证无残留 ImportError。

## 6. 结论

- tests/ 侧的 154→161→216 引用**不是当前唯一阻塞**；
- **真正的硬阻塞是 `__init__.py` 的加载期 db 导入**（1 行，影响全部进程）—— **P0 已解除**；
- 生产 purity 需从"4 文件已断"更正为"**23 文件 / 29 处待断**"（首版扫描输出
  截断导致少报，已用完整扫描更正）；
- 处理顺序 P0 → P1（5 子批）→ P2 tests 216（路线 B）→ P3 39 路径断言 →
  P4 物理删 db/（54 文件）。

## 7. 执行状态（2026-09-21 更新）

| 阶段 | 范围 | 状态 |
| --- | --- | --- |
| **P0** | `__init__.py` 摘除 db 导入 | ✅ 完成（db 模块 47→0） |
| **P1-0** | `server/mcp_server.py` 死导入 | ✅ |
| **P1-1** | `server/_mcp_common.py::get_db()` 懒加载 | ✅ |
| **P1-2** | `server/tools/tools_*.py` ×6 `_bind_readonly_db` | ✅ |
| **P1-3** | `server/job_executor.py` db_jobs（PEP 562 + 入口填充 globals） | ✅ |
| **P1-4** | `cicd/bootstrap_check.py` + `scripts/test_impact_en.py` | ✅ |
| **P1-5** | 非运行时脚本 10 文件 | ✅ |
| **P1-e 验收** | purity 复扫 + 回归 | ✅ **通过**（见下） |
| **P2-a** | 导入期 purity 回归守卫测试 | ✅ 新增 `tests/convergence/test_db_retire_prod_purity.py`（3 用例，含负对照） |
| **P2-b** | db/ 移出生产 wheel（`pyproject.toml` packages） | ✅ clean rebuild 实证 wheel 内 db/ 条目 = 0 |
| **P2-c** | db/ 声明为仅测试支持层 | ✅ `db/__init__.py` docstring 更新 + 守卫测试锚定 |
| **P3** | 39 路径断言 | ⬜ 路线 B 下 db/ 保留，非阻塞（见 §7 末） |
| **P4** | 物理删 db/（54 文件） | ⬜ 路线 B 下 db/ 保留为测试支持层，**已重新定义**（见 §7 末） |

### P1-e 验收证据

1. **导入期 purity（行为级）**：`import callwarden` → `callwarden.db.*`
   已加载模块 **0**（P1 前 47）；继续导入完整 server 栈（`mcp_server` +
   全部 `server.tools.*` + `job_executor` + `job_handlers` +
   `cicd.bootstrap_check`）→ 仍 **0**。
2. **AST 缩进复核**（`.workbuddy/scripts/p1_verify_indent.py`，按真实
   `col_offset`）：生产侧 26 条 db import 语句中 module-level（col==0）
   = **0**；lazy（col>0）= 21。扫描命中的 5 个 col==0 全部位于 gitignored
   非生产目录（`Temp/` @ .gitignore:253；`testcode/` @ .gitignore:138，
   为第三方 vendored 仓引用 `django.db` / flask `db`，与 `callwarden.db` 无关）。
3. **全量收集**：584 测试文件 / **8615** 测试 / **0** 收集错误。
4. **回归**：bootstrap_ci_check 12/12；get_db patch + job_executor 生命周期
   139/139；客户端 purity 收敛套件 + MCP RPC 74/74；11 个 python_compat
   本地 SQL 工具运行时 74/74（`_bind_readonly_db` 懒加载在调用时正常）。
5. **同步修 1 处测试**：`tests/test_bootstrap_ci_check.py` 两处 patch 目标
   `callwarden.cicd.bootstrap_check.CodeGraphDB` → `callwarden.db.CodeGraphDB`
   （懒加载后模块级无该属性，须 patch 源模块，`main()` 调用时取到被替换属性）。
6. **1 个既有失败（非 P1 回归）**：`tests/convergence/test_m1_route_matrix.py::
   test_compat_whitelist_two_side_aligned` 期望 `both_missing` 含 11 项
   KNOWN_DRIFT、实测空集。该用例仅读取 `server/compat_registry.py`、
   `deliverables/.../tool_migration_matrix.json`、`rust_ext/.../http_server.rs`、
   `rust_ext/.../dispatch.rs` 四个文件，**四者均 clean、不在 P1 改动集**，
   纯文件读 → pristine HEAD 同样失败，属已登记的 daemon compat 域漂移
   （该测试 docstring 自述「KNOWN_DRIFT…另卡承接」）。

### 改动清单（未提交）

P1 + P2 合计 **24 个修改文件 + 2 个新增文件**：
- 修改：`__init__.py`、`server/` 9（`_mcp_common` / `job_executor` /
  `mcp_server` + `tools/` 6）、`cicd/bootstrap_check.py`、
  `scripts/test_impact_en.py`、`deliverables/software-company/` ×7、
  `docs/_create_subtasks_v2.py`、`docs/create_r8_r11_subtasks.py`、
  `tests/test_bootstrap_ci_check.py`（P1）+ `db/__init__.py`、
  `pyproject.toml`（P2）；
- 新增：本报告、`tests/convergence/test_db_retire_prod_purity.py`（P2 守卫）。

注：`docs/design/g0_create_review_task.py` 的 P1-5 改动落在
`.gitignore:187` 显式忽略的本地 scratch 脚本，不入库；`cw.py` 的
负对照探针已精确还原到 HEAD（`git diff` 为空）；`.workbuddy/scripts/*`
按项目约定不入库。

### P2 执行状态（路线 B：CodeGraphDB 降级为测试支持层，2026-09-21）

P0+P1 实现了路线 B 的验收口径（生产 purity + `import callwarden` 不加载
db）。P2 的剩余工作是把该结论**锁定并落到打包层面**，使 db/ 名正言顺地
只服务测试：

**P2-a —— 导入期 purity 回归守卫**（`tests/convergence/test_db_retire_prod_purity.py`）

此前唯一的 purity 守卫 `test_m2_client_purity.py` 是**静态 AST** 规则
（禁 sqlite3 业务读写 / 禁 CodeGraphDB 实例化），**不覆盖导入期加载**——
P0/P1 实现的「`import callwarden` 不加载 db」此前**无任何回归守卫**，
新增一行模块级 db 导入不会被发现。新增测试补上该缺口：

- 入口表从 `pyproject.toml` `[project.scripts]` **动态派生**（新增 CLI
  入口自动覆盖，无需手工同步清单），并额外覆盖 `callwarden.server.mcp_server`；
- 在**干净子解释器**中逐一导入全部生产入口，断言 `sys.modules` 中
  `callwarden.db.*` 模块为空。用子解释器是因为 `tests/conftest.py` 本身
  合法导入 db（测试支持层），同进程快照法无法区分「生产触发」与
  「conftest 已加载」；
- 3 个用例：入口表完整性（防解析静默失败）、导入期零 db、
  db/ 已声明测试支持。

**负对照（实证守卫非空转）**：临时给 `cw.py` 追加一行模块级
`from callwarden.db import CodeGraphDB` → 守卫**立刻失败**并报出全部
47 个被加载的 db 模块；回退后 3/3 恢复通过。`cw.py` 已确认回到 HEAD
（`git diff` 为空）。

**P2-b —— db/ 移出生产 wheel**

`pyproject.toml` 的 `[tool.setuptools] packages` 移除 `callwarden.db`
（sdist 仍保留，供测试使用——`MANIFEST.in` 的 `recursive-include db *.py`
不动）。

- setuptools 解析复核：`callwarden.db` 不在已解析 packages。
- **clean rebuild 行为级实证**：首次构建受 `build/lib.win-amd64-cpython-314/`
  陈旧残留污染（含上次构建遗留的 db/，gitignore 内）→ 清空 build 树重 build，
  wheel 内 **db/ 条目 = 0**，`callwarden_core.pyd` 与 13 个 `server/tools/`
  文件在位，`entry_points.txt` 在位。
- 源码树不受影响：`import callwarden.db`（PYTHONPATH=仓根上一级）仍正常，
  `CodeGraphDB` 可用 → 测试期 db 访问不破。

**P2-c —— db/ 声明为仅测试支持层**

`db/__init__.py` docstring 更新：从「cli/server/cicd/tests 兼容层」改为
**仅测试支持层（test-support only）**，明示生产代码不得导入本包，
由 P2-a 守卫锁定（`test_db_package_declared_test_support` 断言 docstring
含「仅测试支持」锚点）。

**P2 回归**：convergence 全套 + bootstrap_ci_check + db_tests +
job_executor 生命周期 = **123 passed / 1 failed**；唯一失败为既有
`test_m1_route_matrix::test_compat_whitelist_two_side_aligned`
（KNOWN_DRIFT，依赖文件全 clean，非本次回归）。

### P3/P4 在路线 B 下的重新定义

路线 B 保留 db/ 作为测试支持层，故：
- **P3（39 路径断言）**：这些断言的是 `db/schema.py` 等文件存在性，
  db/ 保留则断言继续成立，**不再是阻塞项**；
- **P4（物理删 db/）**：路线 B 下 **不再执行物理删除**——db/ 的「退休」
  体现为**生产 wheel 排除 + 生产侧零导入 + 守卫锁定**，而非从仓库消失。
  若未来要彻底删 db/，需先完成 tests/ 216 文件的 CodeGraphDB 迁移
  （即原路线 A），届时 P3/P4 才重新启用。
