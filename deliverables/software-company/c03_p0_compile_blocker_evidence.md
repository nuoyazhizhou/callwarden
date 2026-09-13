# C-03 承接卡 —— `rust_ext` unix/Linux 编译阻断修复 证据

> 本文件是承接卡 `T-1789290072972-5fad5b5c`（P0，C-03）的**任务绑定证据载体**。
> WSL 编译零错误、`tests/test_wsl_local_daemon_e2e.py` 由 2 errors 转 pass、
> Windows 回归未退化、`git diff --check` 干净，均以本文件为准。

| 项 | 值 |
|---|---|
| task | `T-1789290072972-5fad5b5c` |
| parent | `T-1788871227327-45c94bd8`（PYT 回归卡） |
| finding | C-03（`pyt_regression_step4_handoff_backlog.md` §W15） |
| 角色 | executor（`lease_role=implementer`，handoff → reviewer） |
| 合同 | `TC-T-1789290072972-5fad5b5c` r1，`hash=sha256:a5409d75ddb6d8d62b7d9806de519c45a4b8b6f554fe0acbc3e7e0b74b8b0bc2` |
| 角色合同 | `rcl-T-1789290072972-5fad5b5c-executor` / `rcr-...-executor-r1`，`hash=sha256:a46bf7c603a958d3664b678b3c0665ea6563b79d7898e5f29520d7c951964d6c` |
| allowed_paths | `deliverables/software-company/`、`rust_ext/Cargo.toml`、`rust_ext/src/daemon/{transport,http_server,daemon_autostart_handlers,server}.rs`、`tests/` |
| forbidden_paths | `db/`、`direct SQLite writes`、`scripts/refresh_shared_runtime.ps1`、`status forgery`、`task.apply`、`task.close`、`task.supersede` |
| 证据生成日 | 2026-09-13 |

---

## 0. Acceptance 逐条结论

| # | acceptance 原文 | 结论 | 证据 |
|---|---|---|---|
| ① | WSL 内 `cargo build --no-default-features --bin cw-daemon` 零 error | **达标**（`ERR_COUNT=0`，`Finished dev profile`） | §1 |
| ② | `tests/test_wsl_local_daemon_e2e.py` 由 2 errors 转 pass | **达标**（`2 passed in 20.13s`） | §2 |
| ③ | Windows `cargo build` 回归未退化 | **达标**（`WIN_EXIT=0`） | §3 |
| ④ | `git diff --check` 干净 | **达标**（`CHECK_EXIT=0`） | §4 |

> **对验收判据②的诚实披露（须在此如实说明，不得省略）**：
> 判据②的转绿**包含测试夹具修正**（`tests/test_wsl_local_daemon_e2e.py` 3 类 5 处，
> 见 §2.2）。依据为新增权威门禁 commit `4b1380a`（`task.create` 强制显式
> `workspace_id > 0` 且 task-DB `workspaces` 必须存在）与共存契约 §7.2
> （WSL local-daemon 必须 `CW_DAEMON_TRANSPORT=uds`）。
> **未弱化任何断言、未使用 xfail/skip 掩盖、未改动任何被断言的行为口径**。
> 生产侧修复（§1）是本判据从 errors 转 pass 的**必要条件**：修复前 fixture 在
> 「2. WSL 内构建 cw-daemon」步即 `pytest.fail`，两个用例均 error。

---

## 1. Acceptance ① —— WSL 内零 error 编译

### 1.1 命令（W15 复现命令，逐字一致）

```bash
export PATH="/root/.cargo/bin:$PATH"; export HOME=/root;
export CARGO_TARGET_DIR=/root/callwarden-wsl-e2e-target-sub7;
cd /mnt/c/git_work/callwarden;
cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon
```

### 1.2 实测输出（尾部）

```text
warning: `callwarden-core` (lib) generated 176 warnings (run `cargo fix --lib -p callwarden-core` to apply 80 suggestions)
warning: `callwarden-core` (bin "cw-daemon") generated 1 warning (run `cargo fix --bin "cw-daemon" -p callwarden-core` to apply 1 suggestion)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 10.64s
WSL_EXIT=0
```

```text
$ cargo build ... 2>&1 | grep -c "^error"
ERR_COUNT=0
```

- 修复前基线（W15）：`error: could not compile callwarden-core (lib) due to 5 previous errors; 85 warnings emitted`
- 修复后：**0 error**；warning 数（176 + 1）为既有历史告警，非本卡引入。

### 1.3 5 个编译错误的逐条修复（与 W15 表格一一对应）

| # | code | 落点 | 错误 | 修复 |
|---|---|---|---|---|
| 1 | E0063 | `rust_ext/src/daemon/transport.rs` | unix 分支 `ServerConfig` 初始化缺字段 `http` | 补 `http: None`（工厂函数不启用 HTTP overlay） |
| 2 | E0603 | `rust_ext/src/daemon/http_server.rs` | `std::os::unix::fs::Permissions` 为私有类型别名 | 改用 `std::fs::Permissions` |
| 3 | E0599 | `rust_ext/src/daemon/http_server.rs` | `Permissions::from_mode` 缺 trait 导入 | 加 `use std::os::unix::fs::PermissionsExt;` |
| 4 | E0599 | `rust_ext/src/daemon/daemon_autostart_handlers.rs` | `SocketAddr::from_path` 不存在 | 改为按路径直接 `UnixStream::connect(endpoint).ok()` |
| 5 | E0599 | `rust_ext/src/daemon/daemon_autostart_handlers.rs` | `UnixStream::connect_timeout` 不存在（std 无此 API） | 同上；`timeout` 仍按 RPC 契约接收但不参与本路径执行 |

> 说明：第 4/5 项原建议为 `from_pathname(...).ok()` / 引入 `socket2`。本卡采用**更小侵入**的
> `UnixStream::connect(endpoint)`：UDS connect 是本地操作（`ENOENT`/`ECONNREFUSED` 立即返回），
> 不存在网络级等待，`connect_timeout` 语义本就不适用；`let _ = timeout;` 显式保留参数契约，
> 未改变任何对外行为与返回结构。

---

## 2. Acceptance ② —— `tests/test_wsl_local_daemon_e2e.py` 转 pass

### 2.1 命令与结果

```text
$ & C:\Python314\python.exe -m pytest tests/test_wsl_local_daemon_e2e.py -v
collected 2 items
tests\test_wsl_local_daemon_e2e.py ..                                    [100%]
============================= 2 passed in 20.13s ==============================
```

- 修复前基线（W15）：fixture 在构建步 `pytest.fail`，**2 errors**。
- 修复后：**2 passed**（`test_wsl_daemon_authority_isolated_and_writable`、`test_wsl_daemon_survives_restart`）。

### 2.2 夹具修正明细（3 类 5 处，断言口径不变）

| 类 | 位置 | 修正 | 依据 |
|---|---|---|---|
| A · 权威前置 | fixture 新增步骤 6：`workspace.register` + 向隔离 task-DB `workspaces` 写入权威数字 id | 让 `task.create` 的 resolver（`workspace_reconciliation.rs:128-143`）与 binding（`task_collab.rs:186-202`）通过 | 权威门禁 commit `4b1380a`（`required_workspace_id_param` 强制显式 `workspace_id > 0`，否则 `E_WORKSPACE_AUTHORITY_MISMATCH`） |
| B · 显式 workspace 入参 | `test_wsl_daemon_authority_isolated_and_writable`、`test_wsl_daemon_survives_restart` 两处 `task.create` 补 `workspace_id` / `workspace_instance_id` | 同上（禁止 active workspace / cwd 隐式补齐） | 同上 |
| C · 传输口径 | fixture 启动脚本、restart 脚本两处显式 `export CW_DAEMON_TRANSPORT=uds;` | 使 `hello.transport == "uds"` 断言可满足 | 共存契约 §7.2；`rust_ext/src/bin/cw_daemon.rs:231-260` H6 迁移期默认会自设 `transport=http` |

- 上述修改**全部落在 `tests/`（本卡 allowed_paths）**，均为**夹具前置**，未改动任何断言表达式、未放宽阈值、未新增 skip/xfail。
- `deliverables/software-company/` 下临时取证脚本 `_tmp_c03_probe.sh` 已删除，不进入提交。

---

## 3. Acceptance ③ —— Windows 回归未退化

```text
$ cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon
warning: `callwarden-core` (lib) generated 176 warnings (run `cargo fix --lib -p callwarden-core` to apply 79 suggestions)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.74s
WIN_EXIT=0
```

- 5 个错误全部位于 `#[cfg(unix)]` / `#[cfg(not(windows))]` 分支，Windows target 不编译这些分支
  → 属**平台盲区 latent 缺陷**，修复对 Windows 生产面零影响；实测 Windows 构建**未退化**。

---

## 4. Acceptance ④ —— `git diff --check`

```text
$ git diff --check
CHECK_EXIT=0
```

```text
$ git diff --stat
 rust_ext/src/daemon/daemon_autostart_handlers.rs | 11 +++--
 rust_ext/src/daemon/http_server.rs               |  5 ++-
 rust_ext/src/daemon/transport.rs                 |  3 ++
 tests/test_wsl_local_daemon_e2e.py               | 52 ++++++++++++++++++++++--
 4 files changed, 63 insertions(+), 8 deletions(-)
```

- 无空白错误、无冲突标记；`rust_ext/src/daemon/server.rs` 与 `rust_ext/Cargo.toml` 未改动
  （它们在 allowed_paths 内但本卡无需变更）。

---

## 5. 完整 diff（生产修复 + 夹具修正）

```diff
diff --git a/rust_ext/src/daemon/daemon_autostart_handlers.rs b/rust_ext/src/daemon/daemon_autostart_handlers.rs
@@ -108,10 +108,13 @@ pub fn handle_try_connect_unix(params: &Value) -> Result<Value, DaemonRpcError>
     #[cfg(unix)]
     {
-        use std::os::unix::net::{SocketAddr as UnixSockAddr, UnixStream};
-        let result = UnixSockAddr::from_path(std::path::Path::new(endpoint))
-            .ok()
-            .and_then(|addr| UnixStream::connect_timeout(&addr, timeout).ok());
+        use std::os::unix::net::UnixStream;
+        // 修正：std 无 `SocketAddr::from_path`（等价 API 为 `from_pathname`，且返回
+        // io::Result），亦无 `UnixStream::connect_timeout`（E0599，仅 TcpStream 有）。
+        // UDS connect 是本地操作（ENOENT/ECONNREFUSED 立即返回），不存在网络级等待，
+        // 因此直接按路径连接；`timeout` 仍按 RPC 契约接收，但不参与本路径的执行。
+        let _ = timeout;
+        let result = UnixStream::connect(endpoint).ok();
         let connectable = result.is_some();
         drop(result);
         Ok(json!({

diff --git a/rust_ext/src/daemon/http_server.rs b/rust_ext/src/daemon/http_server.rs
@@ -1450,7 +1450,10 @@ fn publish_manifest_atomic(path: &PathBuf, value: &Value) -> Result<(), HttpServ
     #[cfg(not(windows))]
     {
         // Unix：owner-only 权限 0600
-        let _ = std::fs::set_permissions(&tmp, std::os::unix::fs::Permissions::from_mode(0o600));
+        // 修正：`std::os::unix::fs::Permissions` 已是私有类型别名（E0603），须改用
+        // `std::fs::Permissions`；`from_mode` 由 `PermissionsExt` trait 提供（E0599）。
+        use std::os::unix::fs::PermissionsExt;
+        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
         // Unix rename 覆盖已存在目标（原子）
         std::fs::rename(&tmp, path).map_err(|e| HttpServerError::Manifest(e.to_string()))?;
     }

diff --git a/rust_ext/src/daemon/transport.rs b/rust_ext/src/daemon/transport.rs
@@ -160,6 +160,9 @@ pub fn create_listener(
         socket_mode: 0o660,
         accept_timeout: config.accept_timeout,
         socket_group: None,
+        // H1: unix 分支同样必须显式初始化 `http` 字段（ServerConfig 定义见
+        // server.rs:65-85，含 `pub http`）；工厂函数不启用 HTTP overlay，固定 None。
+        http: None,
     };
     let listener = UnixTransportListener::bind(&server_config)?;
     Ok(Box::new(listener))
```

夹具修正（`tests/test_wsl_local_daemon_e2e.py`，3 类 5 处）见 §2.2 及仓库 `git diff`。

---

## 6. 合规声明

- 本卡改动**全部落在 allowed_paths**；未触碰 `db/`、未做 direct SQLite writes、
  未改 `scripts/refresh_shared_runtime.ps1`、未调用 `task.apply`/`task.close`/`task.supersede`、无状态伪造。
- 未修改 `rust_ext/src/daemon/server.rs` 与 `rust_ext/Cargo.toml`（allowed 但无需变更）。
- 未弱化断言、未用 xfail/skip 掩盖、未删除任何测试用例。
- 证据可复现：§1/§2/§3/§4 命令逐字可重跑。
