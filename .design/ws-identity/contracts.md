# contracts · ws-identity（R3）

> 2026-11-22 · R3 · 卡片全文写进任务单 description，executor 不用回翻设计文档。
> 7 张 must 卡（首批建单范围）+ 1 张 should 卡（第二批）。行号以工作树 Read 为准。

## 卡片总览

| 卡 | 文件 | 符号 | 判定 | REQ | 依赖 |
|---|---|---|---|---|---|
| C-01 | rust_ext/src/daemon/workspace.rs | compute_workspace_instance_id + normalize_remote_url + resolve_main_repo_root | extend | REQ-01 | 无 |
| C-02 | rust_ext/src/daemon/workspace.rs | register_workspace_preferred | extend | REQ-02 | C-01 |
| C-03 | server/daemon_client.py | derive_workspace_instance_id + _workspace_snapshot_metadata | extend | REQ-01/02 | 无（与 C-01 同语义，可并行） |
| C-04 | rust_ext/src/daemon/workspace_migration.rs | migrate_repo_identity | new | REQ-03 | C-01（复用 compute/normalize） |
| C-05 | rust_ext/src/daemon/workspace.rs | handle_workspace_migrate | extend | REQ-03 | C-04 |
| C-06 | rust_ext/src/daemon/dispatch.rs | dispatch（match arm） | extend | REQ-03 | C-05 |
| C-07 | server/cli/main.py | cw workspace migrate | extend | REQ-03 | C-06 |
| C-08 | tests/test_workspace_identity.py | Python 矩阵单测 | new | REQ-04 | C-03 |
| C-09 | tests/test_worktree_identity_e2e.py | 三 worktree 端到端 | new | REQ-05（should） | C-01/02/03/07 |

文件所有权相交提示：C-01/C-02/C-05 同文件（workspace.rs）→ 串行；C-03/C-04 可与 C-01 并行。

---

### C-01 compute_workspace_instance_id（Rust 三级派生）
- **文件/模块**：rust_ext/src/daemon/workspace.rs::daemon::workspace::compute_workspace_instance_id
- **REQ**：REQ-01（must，revision：1）
- **签名**：
  ```rust
  pub fn compute_workspace_instance_id(
      owner_uid: &str,
      host_real_root: &str,
      git_remote_url: &str,
      git_main_root: &str,
  ) -> String
  ```
  将现有形参 `_git_head_commit_sha`（未使用）替换为 `git_main_root`；head 是 provenance，永不参与身份。
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | owner_uid | &str | 非空 | 调用方（register 链已保证） |
  | host_real_root | &str | 非空、绝对路径 | 调用方 |
  | git_remote_url | &str | 可空串（无 remote） | 被调方（空 → 跳过 tier 1） |
  | git_main_root | &str | 可空串（非 worktree / 解析失败） | 被调方（空 → 跳过 tier 2） |
- **出参契约**：
  - 成功：16 hex 小写字符串。三级派生（领域分隔前缀防 remote/路径串同键）：
    1. remote 非空 → `sha256("{owner_uid}|remote:{norm_remote}")[:16]`
    2. 否则 main_root 非空 → `sha256("{owner_uid}|root:{norm_main_root}")[:16]`
    3. 否则回退现状公式 `sha256("{owner_uid}|{norm_host_real_root}")[:16]`（fail-open，与 CR7 语义一致）
  - 失败：无错误分支（纯函数，sha256 不失败）。
- **异常路径**：无 remote / 无 main_root → 逐级下落，最终回退自身路径（现状行为，含 warning 由调用方负责）；**禁止 panic / unwrap**。
- **副作用**：无（纯函数）。
- **依赖符号**：reuse `normalize_identity_root`（CR7 路径归一）/ new `normalize_remote_url`、`resolve_main_repo_root`（同文件叶子 helper，随本卡实现）。
  - `normalize_remote_url(url: &str) -> Option<String>`：trim → 剥离嵌入凭据（`scheme://user:pass@host/...` → `scheme://host/...`，`git@host:path` ssh 形式 → `ssh://host/path`）→ 全 URL ASCII 小写 → 剥离 `.git` 后缀与尾斜杠 → 空/非 http(s)/ssh 形态返回 None。
  - `resolve_main_repo_root(root: &str) -> Option<String>`：读 `<root>/.git`；目录 → 自身即主仓；文件（`gitdir: <path>` 指针）→ 读指针目标的 `commongdir`（缺省即 gitdir 本身）→ 主仓 root = commondir 的父目录。任一步失败返回 None（fail-open）。
- **测试要点（GWT，随卡写进 `#[cfg(test)]`，REQ-04 Rust 侧矩阵在此交付）**：
  - Given https 与 ssh 形式指向同仓库，When compute，Then 同一 id（`.git` 后缀/大小写/尾斜杠/带凭据各变体同值）
  - Given remote 含 `user:token@`，When compute，Then id 与剥离后一致，且 norm_remote 中不含 token（断言字符串不含 `:` 前凭据段）
  - Given 无 remote、worktree 指向主仓，When compute，Then tier 2 = 主仓路径键
  - Given 非 git 目录（`.git` 不存在），When compute，Then tier 3 自身路径键（与现状公式一致）
  - Given 不同 remote，When compute，Then 不同 id
- **验收映射**：AC-01、AC-03（Rust 半边）、AC-05
- **注释要求**：doc comment 写明「决策反转」来源——现设计刻意排除 remote（旧 docstring：remote 是 provenance 非身份），本卡按 R0 用户拍板反转为身份主键；写明三级回退语义与 head 不参与身份的理由。
- **assumptions**：remote URL 全 ASCII 小写可能合并「仅大小写不同的两个仓库」（病态场景，记录为已知取舍）。

---

### C-02 register_workspace_preferred 消费新身份入参
- **文件/模块**：rust_ext/src/daemon/workspace.rs::daemon::workspace::register_workspace_preferred（RPC 入口 `handle_workspace_register` / `handle_workspace_register_preferred` 同步接线）
- **REQ**：REQ-02（must，revision：1）
- **签名**：不变（params 增字段）：
  ```rust
  // handle_workspace_register_preferred params 新增（均可选，缺省空串）：
  //   "git_remote_url": String, "git_main_root": String
  ```
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | params.git_remote_url | Option&lt;String&gt; | 可缺省；客户端上送 `git config --get remote.origin.url` 原值 | 被调方（归一在 compute 内） |
  | params.git_main_root | Option&lt;String&gt; | 可缺省；客户端解析的主仓绝对路径 | 被调方（compute 内归一） |
- **出参契约**：
  - 成功：register 现有返回结构不变（含权威 workspace_instance_id）；同 repo 多 worktree / 多 clone 注册 → 返回同一 instance_id 且命中同一行。
  - 失败：沿用现有 register 错误面，不新增变体。
- **异常路径**：新字段缺失 → 按空串处理 → compute 三级回退（不报错，兼容旧客户端）；归一后与既有行冲突 → 走现有 `find_adoptable_instance` 收编 / upsert 路径（R2 已确认存在）。
- **副作用**：沿用现有（registry 写 + snapshot 传播 + alias 归档），不新增。
- **依赖符号**：extend `compute_workspace_instance_id`（C-01 产物）/ reuse `find_adoptable_instance`、`propagate_snapshot_to_siblings`、`archive_alias_rows`。
- **测试要点**：
  - Given 主仓 + 2 worktree 依次 register（各带上送 remote），When 三次调用，Then 返回同一 instance_id 且 registry 仅一条 active
  - Given 旧客户端（不带新字段）register，When 调用，Then 成功且回退路径键（不 500）
  - Given 同 root 重复 register，When 调用，Then 幂等（同一行，无新行）
- **验收映射**：AC-01、AC-05
- **注释要求**：在接线处注明「remote/main_root 为客户端自述 provenance，身份权威计算在 daemon 侧」。
- **assumptions**：A-01（daemon 权威，已验证）。

---

### C-03 Python derive 同语义 + metadata 增 main_root
- **文件/模块**：server/daemon_client.py::derive_workspace_instance_id、::_workspace_snapshot_metadata
- **REQ**：REQ-01/02（must，revision：1）
- **签名**：
  ```python
  def derive_workspace_instance_id(
      project_root: str, git_remote_url: str = "", git_main_root: str = ""
  ) -> str: ...

  def _workspace_snapshot_metadata(project_root: str) -> Dict[str, str]:
      # 返回 dict 在现有 git_remote_url / git_head_commit_sha 基础上新增：
      #   "git_main_root": <主仓绝对路径，解析失败则不含该键>
  ```
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | project_root | str | 非空绝对路径 | 调用方 |
  | git_remote_url | str | 可空（旧调用方兼容） | 被调方 |
  | git_main_root | str | 可空 | 被调方 |
- **出参契约**：
  - 成功：16 hex；三级派生与 Rust C-01 逐字同语义（领域前缀 `remote:` / `root:`、ASCII 小写、凭据剥离、`.git` 后缀剥离）。
  - 失败：无异常分支（subprocess 失败在 metadata 层吞掉返回空值，现状语义）。
- **异常路径**：`_workspace_snapshot_metadata` 新增 `git rev-parse --path-format=absolute --git-common-dir`（stdin=DEVNULL、timeout=10，沿用现有 `_git` helper），主仓 root = commondir 父目录；git 失败 → 不含 `git_main_root` 键（fail-open）。
- **副作用**：新增一次 subprocess 调用（注册路径，非热路径）；无锁。
- **依赖符号**：extend 本文件两函数 / reuse 现有 `_git` helper 与 CR7 归一逻辑。
  - 调用方接线：L316 fallback 处将 metadata 传入 derive；`cli/main.py:13766`、`entry_cw_agent.py:158` 两处旧调用保持单参（回退语义，不强行对齐——它们是本地模式路径）。
- **测试要点**：
  - Given tmp git 仓 + 其 worktree，When `_workspace_snapshot_metadata(worktree)`，Then 返回含 `git_main_root` = 主仓路径且 `git_remote_url` 正确
  - Given 非 git 目录，When metadata，Then 返回不含 `git_main_root`/`git_remote_url`（不抛异常）
  - Given derive 带 remote 与不带，Then 前者 = Rust 同输入结果（跨语言一致性用例放 C-08）
- **验收映射**：AC-03（Python 半边）、AC-05
- **注释要求**：注明与 Rust `normalize_remote_url` 同语义、双实现需同步演进（矩阵单测双语言互锁）。
- **assumptions**：无。

---

### C-04 migrate_repo_identity（迁移核心）
- **文件/模块**：rust_ext/src/daemon/workspace_migration.rs::daemon::workspace_migration::migrate_repo_identity（新文件）
- **REQ**：REQ-03（must，revision：1）
- **签名**：
  ```rust
  pub struct MigrationEntry { pub root_path: String, pub git_remote_url: String, pub git_main_root: String }
  pub struct MigrationReport {
      pub scanned: usize, pub groups: usize, pub merged_rows: usize,
      pub rewired: std::collections::BTreeMap<String, usize>,  // 表名 -> 行数
      pub grant_conflicts: Vec<String>,                         // "principal=X keep=owner drop=agent"
      pub pl06_cleaned: usize, pub backup_path: String, pub dry_run: bool,
  }
  pub fn migrate_repo_identity(
      conn: &mut Connection,
      owner_uid: &str,
      entries: &[MigrationEntry],
      dry_run: bool,
  ) -> Result<MigrationReport, MigrationError>
  ```
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | conn | &mut Connection | task-DB 权威连接（daemon 持有） | 调用方（C-05） |
  | owner_uid | &str | 非空，与 registry 行 owner 匹配 | 被调方（不匹配行跳过并计入报告） |
  | entries | &amp;[MigrationEntry] | 非空；root_path 绝对路径 | 被调方（空 → 返回空报告，不报错） |
  | dry_run | bool | — | — |
- **出参契约**：
  - 成功：MigrationReport（见上）。
  - 失败：MigrationError，变体 ↔ 失败点：
    | 错误变体 | 触发条件 | 上层如何处理 |
    |---|---|---|
    | Db(e) | SQL 失败 | 事务整体回滚，返回 500 |
    | BackupFailed(path) | VACUUM INTO 失败 | 中止，不进入迁移 |
    | IdentityCalc(root) | 某行三级派生全部失败（理论不可达，防御） | 跳过该行并计入报告 errors |
- **算法（单事务，BEGIN IMMEDIATE）**：
  1. `VACUUM INTO '<task-db 同目录>/pre-ws-migrate-<ts>.db'`（备份先行，报告记路径）；
  2. 对每个 entry 计算 `compute_workspace_instance_id(owner_uid, root, remote, main_root)` → 按 new_identity 分组；
  3. 每组存活行 = 最早注册（复用 `find_adoptable_instance` 偏好语义）；其余行置 merged/archived（复用 `archive_alias_rows` 语义），存活行 upsert 新 identity（复用 register 的 ON CONFLICT 路径语义）；
  4. **rewire 不合并**：对任务/证据/lease/role_runtime_provenance 等引用旧数值 id 的表执行 `UPDATE <t> SET workspace_id=<survivor> WHERE workspace_id=<old>`；表清单 = 实现时从 `db/schema.py` SCHEMA_TABLES_SQL + PKI 表提取全部含 `workspace_id` 列的表，**排除 append-only 三表**：`audit_chain`、`workspace_reconciliation_aliases`、`workspace_authority_captures`；
  5. grants：`workspace_principal_grants` 同步 rewire；PK (workspace_id, principal_id) 冲突按 owner > governance > agent > readonly 保最高、删低，冲突明细进 `grant_conflicts`（A-03）；
  6. 旧 instance 重定向：为每个旧 instance_id **追加** capture 行（workspace_id=survivor，capture_revision+1）——dispatch 的 `resolve_instance_workspace_id` 即读最新 capture，旧客户端 instance 立即解析到存活行；不改写既有 capture 行；
  7. PL-06：同组内 `\\?\` 变体行在第 3 步自然合并，`pl06_cleaned` 计数；
  8. dry_run=true：执行 1-2 与分组统计后回滚，不写任何行。
- **异常路径**：任何 SQL 失败 → 回滚整体事务并返回 Db(e)；备份失败 → BackupFailed 中止；全程无 unwrap / 吞异常。
- **副作用**：备份文件 + 单事务内多表 UPDATE/INSERT；dry_run 零副作用。
- **依赖符号**：reuse `compute_workspace_instance_id`（C-01）、`normalize_identity_root`、`record_reconciliation_alias` 模式 / new 本卡片。
- **测试要点**：
  - Given 同 repo 三行（主仓 + 2 worktree + `\\?\` 变体），When migrate，Then 一条 active + 任务行 rewire 到存活 id + 报告 merged_rows=3
  - Given 迁移后重复执行，When 再 migrate，Then 幂等（零改动，报告全 0）
  - Given grants 同 principal 在两行分别 owner/agent，When migrate，Then 保 owner、grant_conflicts 记录一条
  - Given dry_run，When migrate，Then DB 行数前后一致（无副作用）
  - Given 旧 instance 的 capture，When 迁移后 dispatch 解析，Then 解析到存活数值 id（C-05 联调用例）
- **验收映射**：AC-04
- **注释要求**：模块 doc 写明「一次性迁移 = 用新身份重放 register（复用收编机制），非新写合并逻辑」与 append-only 纪律（不改写历史 binding/capture/audit）。
- **assumptions**：A-03（grants 冲突保最高权限）。

---

### C-05 handle_workspace_migrate（RPC 薄壳）
- **文件/模块**：rust_ext/src/daemon/workspace.rs::daemon::workspace::handle_workspace_migrate（与现有 handle_workspace_register 同 impl 块）
- **REQ**：REQ-03（must，revision：1）
- **签名**：
  ```rust
  pub fn handle_workspace_migrate(
      &mut self, peer: PeerCredential, params: &Value,
  ) -> Result<Value, DaemonRpcError>
  ```
  params：`{ "entries": [{root_path, git_remote_url, git_main_root}], "dry_run": bool }`
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | entries | 数组 | 非空数组；元素字段见 C-04 | 被调方（缺失字段按空串） |
  | dry_run | bool | 缺省 false | 被调方 |
  | peer | PeerCredential | 仅 service / 本地未认证（UDS/管道）放行；mTLS agent principal → E_PRINCIPAL_WORKSPACE_FORBIDDEN | 被调方（fail-closed） |
- **出参契约**：
  - 成功：`{ "report": <MigrationReport 序列化> }`。
  - 失败：MigrationError → DaemonRpcError（Db→internal_error、BackupFailed→internal_error 带 backup 路径、IdentityCalc→invalid_params 带 root）。
- **异常路径**：entries 非数组 / 元素非对象 → invalid_params；权限不足 → E_PRINCIPAL_WORKSPACE_FORBIDDEN；其余透传 C-04 错误。
- **副作用**：透传 C-04（dry_run=false 时写库）。
- **依赖符号**：reuse C-04 `migrate_repo_identity` / new 本卡片（薄壳，不含业务逻辑）。
- **测试要点**：
  - Given mTLS agent principal，When migrate，Then E_PRINCIPAL_WORKSPACE_FORBIDDEN 且无写库
  - Given 本地 service 身份 + 合法 entries，When dry_run，Then 返回报告且库无变化
  - Given entries 缺 root_path 字段，When migrate，Then invalid_params
- **验收映射**：AC-04
- **注释要求**：注明「迁移是一次性维护操作，仅 daemon owner 可触发」。
- **assumptions**：无。

---

### C-06 dispatch 接线 workspace.migrate
- **文件/模块**：rust_ext/src/daemon/dispatch.rs::dispatch（match 表 workspace 管理段）
- **REQ**：REQ-03（must，revision：1）
- **签名**：`"workspace.migrate" => state.handle_workspace_migrate(peer, params),`（一行 match arm）
- **入参契约**：dispatch 现有签名不变。
- **出参契约**：透传 C-05。
- **异常路径**：method_not_found 兜底已有。
- **副作用**：无新增。
- **依赖符号**：extend C-05 / new 本卡片（纯接线）。
- **测试要点**：
  - Given dispatch("workspace.migrate")，When 调用，Then 路由到 handle_workspace_migrate（现有 dispatch 测试模式）
- **验收映射**：AC-04
- **注释要求**：无（一行接线）。
- **assumptions**：无。

---

### C-07 cw workspace migrate CLI
- **文件/模块**：server/cli/main.py::cmd_workspace_migrate（新子命令，挂到现有 workspace 命令组）
- **REQ**：REQ-03（must，revision：1）
- **签名**：
  ```python
  def cmd_workspace_migrate(args) -> int
  # 用法：cw workspace migrate [--dry-run] [--db <path>]
  ```
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | --dry-run | flag | 缺省 false | argparse |
  | entries 来源 | — | `workspace.list` RPC 取本 owner 全部 root_path → 逐 root 本地解析 remote + main_root（复用 `_workspace_snapshot_metadata`） | 被调方（git 失败的 root 按空值带上，daemon 侧 fail-open） |
- **出参契约**：
  - 成功：打印对账表（scanned/groups/merged_rows/rewired/grant_conflicts/pl06_cleaned/backup_path），exit 0。
  - 失败：daemon 不可用 → E_HTTP_DAEMON_UNAVAILABLE 语义退出非 0（fail-closed，不回退本地写）。
- **异常路径**：daemon 不可用 → 报错退出（禁止本地直连 SQLite 改库，C-09 单写点）；部分 root git 解析失败 → 照常上送空值，报告体现。
- **副作用**：daemon RPC 调用（dry_run=false 时触发迁移写库）。
- **依赖符号**：reuse `_workspace_snapshot_metadata`（C-03）、现有 workspace.list 调用 / new 本卡片。
- **测试要点**：
  - Given --dry-run，When 执行，Then 打印报告且 exit 0、库无变化
  - Given daemon 停止，When 执行，Then 非 0 退出且无本地写库
- **验收映射**：AC-04
- **注释要求**：help 文案注明「一次性迁移，幂等可重跑，迁移前自动备份」。
- **assumptions**：无。

---

### C-08 Python 矩阵单测
- **文件/模块**：tests/test_workspace_identity.py（新文件，pytest）
- **REQ**：REQ-04（must，revision：1）
- **签名**：测试模块（无业务签名）；覆盖 `derive_workspace_instance_id`、`_workspace_snapshot_metadata`、Python 侧 `normalize_remote_url`（C-03 随卡实现）。
- **入参契约**：不适用（测试）。
- **出参契约**：不适用（测试）。
- **异常路径**：不适用。
- **副作用**：tmp_path fixture 建临时 git 仓 / worktree（subprocess git init / worktree add）。
- **依赖符号**：reuse C-03 产物。
- **测试要点（矩阵，逐格断言同/异 id）**：
  - 大小写：`C:\repo` vs `c:\repo` → 同 id（tier 3）
  - `\\?\` 前缀 → 同 id；正反斜杠 → 同 id；尾斜杠 → 同 id
  - remote 形式：https / ssh（`git@host:path`）/ `.git` 后缀 / 带凭据 / 大小写 / 尾斜杠 → 全部同 id
  - 同目录不同书写 → 同 id；不同 remote → 不同 id；不同 repo 无 remote → 不同 id
  - 凭据剥离：norm 结果不含 `token`
  - 跨语言锚：固定输入向量（remote/root/owner 三元组）断言 Python 与 Rust 输出同值（Rust 侧期望值以常量固化，避免测试内跑 Rust）
- **验收映射**：AC-03
- **注释要求**：文件头注明「与 Rust workspace.rs #[cfg(test)] 矩阵同格，双语言互锁」。
- **assumptions**：无。

---

### C-09 三 worktree 端到端（should，第二批建单）
- **文件/模块**：tests/test_worktree_identity_e2e.py（新文件，pytest 进程级）
- **REQ**：REQ-05（should，revision：1）
- **签名**：测试模块；起 daemon（测试模式）+ 三客户端（主仓 / worktree-A / worktree-B）。
- **入参契约**：不适用。
- **出参契约**：不适用。
- **异常路径**：daemon 起不来 → 测试 skip 并标注环境原因（不记 fail）。
- **副作用**：临时目录建 git 仓 + 2 worktree；临时 task-DB。
- **依赖符号**：reuse C-01/02/03/07 全链。
- **测试要点**：
  - Given 三客户端依次 register，Then 同一 instance_id（AC-01）
  - Given A 在 worktree 建任务，Then B 在主仓 task_list 可见；两 worktree 对同文件任务 lease.acquire 互斥（AC-02）
  - Given 非 git 目录客户端 register，Then 成功 + warning（AC-05）
- **验收映射**：AC-01、AC-02、AC-05
- **注释要求**：注明「REQ-05 should：本卡为端到端验收载体，证据落 evidence/AC-01.md、AC-02.md、AC-05.md」。
- **assumptions**：无。
