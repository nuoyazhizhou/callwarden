# 诊断：能否用 `python C:/git_work/callwarden/cw.py` 将 TokenSlim 入库

- 日期：2026-09-05
- 目标仓库：`C:\git_work\TokenSlim`
- CLI：`C:\git_work\callwarden\cw.py`（Python 3.13.12，HTTP 传输模式）
- Daemon：`cw-daemon.exe` PID 46844，endpoint `http://127.0.0.1:1537`

## 结论

**不能。** 存在两个互相独立的阻塞点，任何一个不解除都无法完成入库。
两个阻塞点都不局限于 TokenSlim —— **callwarden 自身项目当前也无法入库**，因此这是全局性缺陷，不是 TokenSlim 侧的配置问题。

| # | 阻塞点 | 影响范围 | 证据 |
|---|--------|----------|------|
| A | CLI 工作区上下文硬编码绑定到 callwarden 包目录，`cw workspace set` 完全失效 | 所有非 callwarden 项目 | 传 `175` / `TokenSlim` 均返回 workspace 1194 (`C:\git_work\callwarden`) |
| B | daemon 的 codegraph DB 路径默认为 Linux 硬编码 `/var/lib/callwarden` | **所有项目（含 callwarden 自己）** | `unable to open database file: /var/lib/callwarden/workspaces/<id>/codegraph.db` |

---

## 阻塞点 A：CLI 工作区上下文硬绑定 callwarden

### 现象

```
$ cd C:\git_work\TokenSlim
$ cw workspace set 175        → Switched to active workspace: callwarden (C:\git_work\callwarden)
$ cw workspace set TokenSlim  → Switched to active workspace: callwarden (C:\git_work\callwarden)
$ cw workspace set 1288       → Switched to active workspace: callwarden (C:\git_work\callwarden)
```

无论传什么都不生效，稳定复现。

### 根因链

1. `config.py:35`
   ```python
   PROJECT_ROOT = _MODULE_DIR  # = C:\git_work\callwarden（包自身目录，与 cwd 无关）
   ```

2. `server/daemon_client.py:3887-3889`（`route_rpc` 内，HTTP 模式分支）
   ```python
   if client._project_root is None:
       from callwarden.config import PROJECT_ROOT
       client.configure_workspace(PROJECT_ROOT)   # 永远绑定成 callwarden
   ```
   随后 `_ensure_remote_snapshot()` 把 callwarden 的 `workspace_instance_id` 注入**每一个** RPC 参数。

3. `cli/main.py:1362-1366`
   ```python
   def set_active_workspace(self, workspace_id_or_name) -> bool:
       route_rpc("workspace.activate", {...}, "PROTECTED_MUTATION")
       return True          # 🔴 无条件返回 True，不校验 RPC 实际结果
   ```
   紧随其后的 `get_active_workspace()` 读回的必然是 callwarden，于是打印出误导性的 "Switched to callwarden"。

4. daemon 侧 `workspace.activate` **已不再接受** `workspace_id_or_name`。裸调用（绕过注入）实测：
   ```
   RAW activate(175)       → DaemonRemoteError: invalid_params: 缺少字段: workspace_instance_id
   RAW activate(TokenSlim) → DaemonRemoteError: invalid_params: 缺少字段: workspace_instance_id
   ```
   即该参数是遗留字段，实际生效的是注入的 `workspace_instance_id`。

### 附带影响

`cw refresh --all` → `db.build_full_graph()` 同样经过 `route_rpc`，注入的仍是 callwarden 上下文。
**即使阻塞点 B 被修复，直接跑 `cw refresh` 入库的也是 callwarden，不是 TokenSlim。**

---

## 阻塞点 B：daemon codegraph DB 路径为 Linux 硬编码（真正的入库杀手）

### 现象

绑定 TokenSlim 后刷新单个源文件：

```
bound workspace = C:/git_work/TokenSlim
DaemonRemoteError: internal_error: 打开 codegraph DB 失败:
  unable to open database file: /var/lib/callwarden/workspaces/6108ef48c489cb1b/codegraph.db
```

**对照实验：对 callwarden 自己的项目刷新，报完全相同的错误**（仅 instance id 不同）：
```
bound = C:/git_work/callwarden
DaemonRemoteError: internal_error: 打开 codegraph DB 失败:
  unable to open database file: /var/lib/callwarden/workspaces/822c031c71488f12/codegraph.db
```

旁证：`C:\Users\wanpi\.callwarden\` 下 148 个 instance 目录**没有任何一个**含 `codegraph.db`
（`ls ~/.callwarden/*/codegraph.db` 无输出），说明该库从未被成功创建过。

### 根因链

`rust_ext/src/daemon/config.rs`

```rust
// :25
pub const DEFAULT_DATA_ROOT: &str = "/var/lib/callwarden";   // Linux systemd 约定

// :131-134 （Default for DaemonConfig）
codegraph_db_path_template: format!(
    "{}/workspaces/{{workspace_instance_id}}/codegraph.db",
    DEFAULT_DATA_ROOT
),
```

`apply_env_overrides()` 中的**非对称处理**是缺陷核心：

```rust
// :214-222
if let Ok(v) = std::env::var("CW_DAEMON_DATA_ROOT") {
    if !v.is_empty() {
        self.data_root = PathBuf::from(v);
        // registry 跟随 data_root 重算 ✅
        if self.registry_db_path == default_registry_db_path() {
            self.registry_db_path = self.data_root.join("registry.db");
        }
        // 🔴 codegraph_db_path_template 没有对应的跟随重算
    }
}

// :232-236 —— 只有显式设置该环境变量才会改
if let Ok(v) = std::env::var("CW_DAEMON_CODEGRAPH_DB_TEMPLATE") { ... }
```

结果：daemon 用 Windows 的 `data_root` 启动 → `registry.db` 正确落在
`C:\Users\wanpi\.callwarden\daemon\registry.db`；但 codegraph 模板**仍留在**
`/var/lib/callwarden/...`，Windows 上无法创建 → 任何 refresh / 入库全部失败。

`config.rs` 全文无 `cfg!(windows)` 分支（`default_authority_task_db_path()` 反而正确处理了
`USERPROFILE`，说明 Windows 是被考虑过的，只是这两处遗漏了）。

---

## 附带发现（次要缺陷）

1. **`cw status` 直接崩溃**
   ```
   ✗ Subcommand 'status' failed: 'workspace'
   ```
   `cli/main.py` 的 status 分支对 `workspace.status` 返回结构取 `['workspace']` 键，
   而 daemon 行无该字段 → KeyError。

2. **workspace 注册表严重重复**：81 条记录里 `callwarden` 出现 11+ 次，
   `TokenSlim` 有 2 条（id 175 = `C:\git_work\TokenSlim`，id 1288 = `c:/git_work/TokenSlim`，
   大小写/分隔符不同被判为两个工作区）。

3. **`doctor` 报环境未优化**：`synchronous=2`、`busy_timeout=3000`、`cache_size=-2000`、
   `mmap_size=0` 均偏离期望值；Windows Defender 未加 `C:\Users\wanpi\.callwarden` 排除项
   （可能引发间歇性 `SQLITE_CANTOPEN`）。

---

## 修复方案

### A 的绕过（无需改代码）

在 CLI 进程内**预先**占住 `_project_root`，使 `route_rpc` 的 `if ... is None` 分支不再改写：

```python
import sys
sys.path.insert(0, r"C:\git_work")
from callwarden.server.daemon_client import HttpDaemonRpcClient
HttpDaemonRpcClient.get_instance().configure_workspace(r"C:\git_work\TokenSlim")  # 目标仓库
from callwarden.cli.main import main
sys.argv = ["cw"] + sys.argv[1:]
main()
```

已验证：绑定后 `workspace.status` 返回 `client_view_root = C:/git_work/TokenSlim`，
且 daemon 正确解析出 TokenSlim 的 instance id `6108ef48c489cb1b`。

> 根治方向：让 `_project_root` 回退到进程 cwd（或支持 `CW_PROJECT_ROOT` 环境变量），
> 并让 `set_active_workspace` 校验 RPC 返回值而非无条件 `return True`。

### B 的修复（必需，否则入库必失败）

`cw-daemon.exe` 支持 `--config <JSON>`（优先级：CLI > env > 文件 > 默认）。
写一个 Windows 路径配置后重启 daemon 即可，无需改代码：

```json
{
  "socket_path": "\\\\.\\pipe\\callwarden-<sid>",
  "registry_db_path": "C:\\Users\\wanpi\\.callwarden\\daemon\\registry.db",
  "data_root": "C:\\Users\\wanpi\\.callwarden\\daemon",
  "max_workers": 16,
  "request_timeout_secs": 30,
  "socket_mode": 420,
  "snapshot_cache_capacity": 32,
  "codegraph_db_path_template": "C:\\Users\\wanpi\\.callwarden\\daemon\\workspaces\\{workspace_instance_id}\\codegraph.db",
  "socket_group": "",
  "stage_toggle_db_path": "C:\\Users\\wanpi\\.callwarden\\daemon\\stage_toggle.db",
  "task_db_path": "C:\\Users\\wanpi\\.callwarden\\callwarden.db",
  "audit_db_path": "C:\\Users\\wanpi\\.callwarden\\daemon\\audit.db"
}
```

> 根治方向（推荐）：在 `apply_env_overrides()` 的 `CW_DAEMON_DATA_ROOT` 分支内补齐
> codegraph 模板跟随重算，与 registry 的处理保持对称；并新增 Windows 分支的
> `DEFAULT_DATA_ROOT`（如 `%LOCALAPPDATA%\callwarden` 或 `~/.callwarden/daemon`）。

---

## 修复记录（2026-09-05 同日实施）

按裁决方向修复：**任何平台都不再需要 per-workspace `{workspace_instance_id}` 数据库维度**（那是"每个 workspace 一个数据库"时代的残留；现已是整台主机一个库）。schema 本身早已是单库多项目设计——`workspaces` 表区分项目、`file_instances` 带 `workspace_id`（`UNIQUE(workspace_id, rel_path)`）、`file_contents`/`symbol_contents` 按 content_hash 全局去重共享——per-workspace 文件与 schema 自相矛盾。

改动（`rust_ext/src/daemon/`，未提交，待独立复审）：

| 文件 | 改动 |
|------|------|
| `config.rs` | ① 新增 `default_codegraph_db_path()` → `~/.callwarden/callwarden.db`（USERPROFILE/HOME 跨平台）；② `Default.codegraph_db_path_template` 由 `/var/lib/callwarden/workspaces/{id}/codegraph.db` 改为空串；③ `resolve_codegraph_db_path` 空模板回退单库（与 Python `daemon_config.py:184-205` 语义完全对齐）；④ `storage_paths` 无条件纳入 codegraph 冲突检测；⑤ `validate_internal_storage` 豁免 `task_db`==`codegraph`（同一权威单库的两个角色）；⑥ `apply_env_overrides` 补齐 `stage_toggle_db_path` 跟随 `CW_DAEMON_DATA_ROOT` 重算（与 registry 同构的非对称遗漏） |
| `snapshot_state.rs` | `codegraph_db` 闭包（唯一生产解析点，覆盖 `file.refresh`/`build_graph`/`job_submit`）空模板 → 回退单库；仅 HOME/USERPROFILE 均缺失时 fail-closed |
| `config.rs`（测试） | 更新 `test_resolve_codegraph_db_path_empty_template`；新增 `test_validate_internal_storage_allows_single_authority_db` |

验证：`cargo check` ✓；`daemon::config` 22/22 ✓；`daemon::snapshot_state` 51/51 ✓。

生效条件：重新编译部署 `cw-daemon.exe` 并重启 daemon。**阻塞点 A（CLI 工作区上下文硬绑定）本次未修**，仍需后续处理。

## 第三轮：workspace ID 空间错位修复（2026-09-05 18:20，已部署并实证）

单库修复部署后（runtime 版本 `20260905-174050`），`refresh_file` 不再报 `/var/lib` 打开失败，但暴露第二层缺陷：

```
internal_error: file_instances upsert: FOREIGN KEY constraint failed
```

### 根因：两个不相干的 workspace ID 空间

- daemon registry（`~/.callwarden/daemon/registry.db` 的 `daemon_workspaces`）的 `workspace_id` 是 registry 自增（如 36）；
- 权威单库 `workspaces.id` 是另一套自增（1-11，TokenSlim=10、callwarden=1）。
- `fs_handlers.rs` 的 `refresh_file` / `build_graph` / `build_directory` 拿 registry id 直插 `file_instances`，**从不把 workspace 行同步进单库**（`ensure_workspace_row` 只在 cas_merge publish 路径被调用）——旧设计里被 `/var/lib` 打开失败掩盖。
- 且 cas_merge 的 `ensure_workspace_row` 按 registry id 显式直插（`INSERT OR IGNORE ... id=registry_id`），存在 **id 撞行错挂风险**：若 registry id=10 恰与权威库 id=10（TokenSlim）相撞，file_instances 会静默写到错误工作区。

### 修复（按「workspace 完整路径为业务键」裁决）

| 文件 | 改动 |
|------|------|
| `fs_handlers.rs` | 新增 `normalize_root_for_authority()`（剥离 `\\?\` verbatim 前缀、`\`→`/`、盘符小写，与权威库既有行如 `c:/git_work/TokenSlim` 对齐）+ `ensure_workspace_row_by_root()`（按 root_path SELECT→miss 时 INSERT ON CONFLICT(root_path) DO NOTHING，name UNIQUE 撞名时追加哈希后缀降级重试）；四个消费点（`refresh_file`/`build_graph`/`build_directory`/`file_remove`）全部改用权威 id |
| `cas_merge.rs` | `merge_cas_to_codegraph_impl` 的 workspace 行确保改为按 root_path 映射（inner 闭包返回映射后 id 供 `MergeResult` 使用）；旧 `ensure_workspace_row` 标记废弃 |

测试：`daemon::fs_handlers` 4/4 ✓；`daemon::cas_merge` 20/20 ✓（更新 2 个依赖旧「registry id 直插」语义的用例，改为按 root_path 断言）。

### 部署

`scripts/refresh_shared_runtime.ps1 -Configuration release -RestartMcp`，runtime 版本 `20260905-182105-6fa8570c58da-8606a5e3`。

> 部署脚注：脚本 `Endpoint` 函数的 `(& $PythonExe -c "import config; ...").Trim()` 依赖 **cwd 在仓库根**（`import config` 按 cwd 解析），且 stderr 被 `2>$null` 吞掉——从其他目录发起时表现为「不能对 Null 值表达式调用方法」，证据 JSON 中 `endpoint = null`。从 `C:\git_work\callwarden` 发起即可。

### 实证结果（主机级单库，`~/.callwarden/callwarden.db`）

1. **多项目并行入库** ✓：TokenSlim `src/lib.rs`（hash `d289ea60…`）与 callwarden `config.py`（hash `00a2190e…`）先后刷新均成功，且各自落到正确的既有 workspace 行（TokenSlim id=10、callwarden id=1）——`normalize_root_for_authority` 把 registry 的 `\\?\C:\git_work\TokenSlim` 归一化后精确命中既有 root_path，**未产生幻影 `daemon_ws_*` 行**。
2. **同一符号/源文件映射多个工作区** ✓：schema 层 `file_instances UNIQUE(workspace_id, rel_path)` 按工作区分行，实体按 hash 全局去重。库内实证：
   - 文件内容级：hash `e3b0c442…` 被 **2 个工作区 × 26 条 file_instances** 共享 1 条 `file_contents` 行；
   - 符号内容级：符号 `fmt`、`default` 等的 `symbol_contents` 行被 2 个工作区的符号实例共同引用（`symbols` 按工作区各一行，内容只存一份）。

## 复现命令

```bash
cd /c/git_work/TokenSlim
python "C:/git_work/callwarden/cw.py" workspace list | grep -i tokenslim   # 存在 id 175 / 1288
python "C:/git_work/callwarden/cw.py" workspace set 175                     # 错误切到 callwarden
python "C:/git_work/callwarden/cw.py" status                                # KeyError: 'workspace'
python "C:/git_work/callwarden/cw.py" doctor                                # PRAGMA 未优化
```

> 注：Git Bash 下必须写 Windows 路径 `C:/git_work/callwarden/cw.py`；
> 用 `/c/git_work/...` 会被 Windows Python 解析成 `C:\c\git_work\...` 而报找不到文件。

## 第四轮：is_indexable_path 过滤失效取证（2026-09-05 19:20–20:10）

**实验**：构造 mini fixture（`src/a.py` + `node_modules/pkg/x.js` + `.hidden/h.py`），经生产 daemon（PID 6752，endpoint 14674，manifest sha d1cabb4b…，git 6fa8570c + 今日未提交 FK 修复）执行 `workspace.build_graph` → 结果 `scanned=3, inserted=3`，DB 实录 `['.hidden/h.py','node_modules/pkg/x.js','src/a.py']` 全部入库。**结论：部署二进制的 build_graph 运行时无任何过滤（含隐藏文件规则）。**

**排除项**：
- 非 Python 侧实现：全仓库仅 `fs_handlers.rs` 产出 `scanned/inserted/unchanged` 三元组；compat 白名单（http_server.rs）不含 build_graph。
- 非旧二进制：部署 exe 含今日 17:58 未提交改动独有字符串 `行映射失败`（fs_handlers.rs:232）——确认编译自今日工作树。
- 非增量缓存损坏：touch 源码后全新 release 重编（6m53s、171 warnings）产出 exe（sha 55f34b78…）同样不含 `node_modules` 字面量。
- 过滤代码自 4b1380a（2026-08-20）起在所有提交、HEAD、工作树中均存在（`node_modules` ×3）。

**未决点**：静态字符串探测出现悖论——同一 exe 含 `INSERT OR IGNORE INTO file_contents`、`行映射失败`，却不含 `node_modules`/`__pycache__`/`svelte`。候选解释：LLVM 将短字符串比较优化为立即数（字面量从 .rdata 消失≠功能缺失）；但生产运行时行为证明过滤确实未生效。新 exe 的隔离运行时验证未完成（HTTP 绑定 :0 + compat worker 相对路径 import 失败，为避免污染生产 manifest/命名管道而中止）。

**下一步**：① 以正确部署布局（exe 置于含 `../server/` 的目录）隔离跑新 exe 重放 fixture；② 或在 handle_build_graph 增加过滤计数日志（skipped_by_filter 字段）后经 refresh_shared_runtime.ps1 重新部署验证。

**数据清理**：_tmp_filter_test fixture 目录与 _tmp_verify_root 已删；权威库中实验产生的 workspace id=18 行、3 条 file_instances、3 条 file_contents 已精确删除。

**附带发现**：`SKIP_DIRS` 不含 `other/`——TokenSlim 的 vendored 第三方（other/cc-switch 等，6248 行非 node_modules 噪声）即使过滤修复后仍会入库；建议引入 .gitignore 感知或 per-workspace ignore 配置。50498 条 node_modules 噪声行修复验证后需清理（daemon 持写锁期间不宜直接 DELETE）。

## 第五轮：CLI 符号面修复与验收（2026-09-05 21:44–22:40）

**Blocker A 根因闭环**：CLI `--workspace` 只喂给 RpcDBProxy 本地路径，从不调 `configure_workspace`；route_rpc 兜底 `configure_workspace(PROJECT_ROOT)`（callwarden 仓库根）→ 实证 callwarden 根 = 实例 `822c031c…`（无 snapshot）→ 所有跨项目符号查询报 `snapshot_not_ready`。TokenSlim 实例 = `6108ef48…`（bypass 绑定，snapshot 已发布）。

**代码修复（未提交，待复审）**：
1. `cli/main.py` RpcDBProxy.__init__：workspace_root 非空时同步 `HttpDaemonRpcClient.configure_workspace`；
2. `server/daemon_client.py` route_rpc：兜底 PROJECT_ROOT → `os.getcwd()`（两处，含 task.report 路径）；
3. `server/daemon_client.py` route_rpc：注入 `workspace_root` 参数（compat 面需要）；
4. `server/tools/tools_query.py` `_bind_readonly_db`：`ctx.workspace_id`（registry 数字 id）与权威库数字 id 是两个 ID 空间，缺失/无行时按 workspace_root 规范化（反斜杠→正斜杠、盘符小写）回退查 `workspaces.root_path`；
5. `cli/main.py` call-chain 分支：Rust daemon `query.call_chain_down` 返回扁平边列表，客户端聚合为 legacy levels 结构。

**数据修复**：权威库噪声清理（ws10：56749 文件行 = node_modules 50498 + other/ ~6248 + 隐藏目录；25912 第三方符号；219618 调用边；23801 孤儿 file_contents；29380 孤儿 symbol_contents）→ ws10 剩 923 一阶层文件。清理提交后 daemon 后台管线自动重建符号：312 → 20119 符号、127318 调用边。`workspaces.id=10` 置 `is_active=1`（resolve_workspace_id 的 legacy 兜底需要；多项目语义 caveat 待根治）。`snapshot.publish` generation=2。

**运维发现**：compat worker 是 daemon 持有的常驻 stdin/stdout 管道进程（`C:/Python314/python.exe server\compat_worker.py`，本机 PID 32636，父进程 20232≠daemon 6752）——改 Python worker 侧代码须杀 worker 触发 respawn 才生效。

**验收结果（CLI，--workspace C:/git_work/TokenSlim）**：
- ✅ search / symbol / stats / top-callers（结果已回归一方代码，如 chrome-extension）
- ✅ callers / call-chain / impact
- 已知残留：快照 call_count 口径异常（generation=2 报 1869 vs 库内 127318，影响面待查）；`cw refresh all` 子命令把 "all" 当路径解析（迁移残留 bug）；workspaces 表 is_active=1 的多项目混串风险。

## 第六轮：TokenSlim 符号图补建（2026-09-05 22:20–22:50，方案 A 执行）

**前提修正**：round-5 报的"20119 符号"系误读——其中 19807 属 callwarden(ws1)，TokenSlim(ws10) 仅 312 个（docs/ Python 辅助脚本），核心源码（src/*.rs、chrome-extension/*.ts）符号为 0。

**断链定位**：Rust daemon 无 build_full_graph；workspace.build_directory 为 Rust 版且只写文件级行（last_parsed=0 无后台消费者）；workspace.file.refresh 深解析产物入 CAS（ready_published）但 CAS→权威库投影链缺失。**唯一可写 symbols/calls 的索引器 = Python Builder（db/db_build.py，单体路径残留）**。

**执行**：
1. TokenSlim 新增 `.callwardenignore`（other/、.opencode/、.agent_backups/；默认规则已含 node_modules/target/dist/vendor 等）；
2. 独立进程跑 `CodeGraphDB(workspace_root=TokenSlim).build_full_graph(force=True)`。关键坑两个：① 包结构为 `callwarden.db.db`（非 db_base 导出类）；② `callwarden_core.pyd` 位于仓库根，**cwd 必须在 callwarden 仓库根**（与 compat worker 一致），否则 763 文件全部 "Unsupported skipped"；
3. 结果：763 文件 → **6398 符号、53425 调用边（100% resolved）**，noise 回流 0；snapshot generation=3 重发布（symbol_count=128394 / call_count=53306）。

**验收（CLI + SQL）**：search/callers/call-chain/impact/top-callers 全部命中一方代码；孤儿函数（无入出边）src/ 下可查（如 conpty_probe::probe_conpty 70L、doctor_encoding::detect_codepage 18L 等）；最长函数 top：`create_mcp_server` 2070L（scripts/code_graph/ 内嵌副本）、`main` 1379L、`compact_common_vcs_ack_line` 1012L（src/ 真实热点）。

**遗留**：dangling call edges 105426（指向已删噪声符号的边，后续 GC 清理）；`validate_owned_path(require_file=true)` 的 build_directory bug 已定位未修（本 workaround 不依赖它）；cas_merge 增量路径不产出符号（round-4 遗留）依旧存在。
