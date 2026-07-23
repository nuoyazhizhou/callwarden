# Feature Matrix 整改完整复审（2026-07-22）

## 1. 结论

复审基线为当前 `HEAD=9a5048a`。本轮没有把任务状态、commit message、测试文件中的
断言或 `_feature_matrix.md` 自身的绿色状态当成完成证据，而是从 Linux 默认 systemd
服务、真实 Rust/Python handler、持久化提交顺序、安装目录和 release workflow 反向追踪。

**“前轮全部问题已经修复”仍不成立。** 当前结论是：

- 原 5 个 Rust workspace handler 的 owner ACL 已真实修复；
- PR Checker 原三条 fail-open、D7 原方向/去重错误、Rust memfd owner/seals、核心数字
  基线已经修复；
- watcher save-to-query 在默认 Linux Rust daemon 上仍未闭合，而且存在 generation 先
  committed、CodeGraph merge 后执行的永久丢更新窗口；
- Windows、Linux、macOS 发布链都有确定性阻断，不是“只差干净 runner 验证”；
- Rust daemon 聚焦套件当前为 **326 passed / 25 failed**，不能声称回归全绿；
- `_feature_matrix.md` 的 G8/G11/J8/K4 需要回退，G1/G3/G4 只能确认 handler ACL 修复，
  不能据此证明已安装多用户服务可用。

## 2. 前轮整改逐项判定

| 整改项 | 本轮结论 | 判定 |
|---|---|---|
| Rust workspace ACL 5 个 handler | 均调用 `owned_workspace_by_id(... peer.uid ...)`；顶层 admin-only 门禁存在 | ✅ 原代码缺口已修 |
| watcher save-to-query | 默认 service 未配置 CodeGraph DB；generation 在 merge 前 committed；merge 失败不阻断 | ❌ 未闭合 |
| 三平台安装/发布链 | Windows Gate 4a 固定失败；Linux 包冲突且 daemon 首启失败；macOS tag 包未真正 notarize | ❌ 未闭合 |
| PR Checker 三条 fail-open | git diff、Semgrep `success=false`、finding SQL 异常均进入 `run_errors` | ✅ 原问题已修 |
| D7 方向/去重/FQN 恒真条件 | 三项均已修；歧义短名仍任选首候选 | ✅ 原问题已修，留 P2 |
| Rust memfd owner/seals | 生产 handler 传 `peer.uid`，真实 memfd 强制完整 seals | ✅ 核心问题已修，留 P2 |
| QueryBudget | 仍只用于 refresh frontier，不是所有 daemon 图查询的统一预算 | 📄 仍是能力降级 |
| 206/35/40/v41 基线 | 源码实算和基线脚本一致 | ✅ 核心计数已修 |
| Python/Rust daemon 能力区分 | 文档区分仍正确，Rust metrics/health/migration 缐口仍在 | 📄 不是功能完成 |

## 3. P0 阻断问题

### P0-1 Rust refresh 在 CodeGraph merge 前提交 generation

`daemon_handle_refresh` 在 CAS publish 后立即调用
`file_generation_committed()`，随后返回 `status="committed"`：

- `rust_ext/src/daemon/replicator.rs:318-353`

真正的 CAS -> CodeGraph merge 在外层 handler 返回后才执行，而且失败只记录 warning，不会
回滚 committed generation：

- `rust_ext/src/daemon/workspace.rs:1369-1496`

同一个 `(epoch, seq)` 重试又会被 `seq <= existing_seq` 判 stale：

- `rust_ext/src/daemon/cas.rs:844-863`

因此在 CAS committed 后、CodeGraph merge 前崩溃，或 merge 本身失败时，该保存事件会被
永久视为已处理，客户端同 seq 重试无法补偿。这正是前轮要求修复的崩溃窗口。

### P0-2 默认 Linux daemon 根本未启用 CodeGraph/Snapshot 发布

`DaemonConfig` 默认把 `codegraph_db_path_template` 设为空，空值明确表示不发布 snapshot：

- `rust_ext/src/daemon/config.rs:52-73`

Linux unit 直接运行 `/usr/bin/cw-daemon serve`，没有 `--config`、`EnvironmentFile` 或
`CW_DAEMON_CODEGRAPH_DB_TEMPLATE`；文件内注释甚至仍声明 Rust daemon 是 CAS-only：

- `release/linux/deb/systemd/callwarden-daemon.service:31-47`

handler 在模板为空时把 `db_path` 置空并跳过 merge/publisher：

- `rust_ext/src/daemon/workspace.rs:1360-1367`
- `rust_ext/src/daemon/workspace.rs:1498-1544`

矩阵 G11/J8/K4 所写的“dispatch 从 `res["codegraph_db_path"]` 显式传入绕过”不是 Rust
生产路径的事实。`cw-daemon` 启动时只把 `DaemonConfig` 的模板注入 state。

### P0-3 已安装客户端无法通过声明的 `callwarden-clients` 组连接 socket

systemd service 使用 `User=callwarden`、`Group=callwarden`，`RuntimeDirectory` 没有 setgid：

- `release/linux/deb/systemd/callwarden-daemon.service:18-25`

Rust server 只 `chmod(0660)`，没有把 socket `chown/chgrp` 到 `callwarden-clients`：

- `rust_ext/src/daemon/server.rs:181-188`

把 daemon 用户加入 supplementary group 不会让新建 socket 自动使用该组。因此普通用户即使
加入 `callwarden-clients`，仍不能访问实际为 `callwarden:callwarden` 的 socket。代码 handler
ACL 修复成立，但真实多用户 UDS 安装形态仍不可用。

### P0-4 Windows Gate 4a 仍会确定性失败

PyInstaller spec 在 Windows 明确不构建 `cw-agent`：

- `release/pyinstaller/callwarden.spec:142-151`
- `release/pyinstaller/callwarden.spec:207-225`

workflow 却仍强制验证 `cw`、`cw-client`、`cw-agent` 三个 exe，缺一即 `exit 1`：

- `.github/workflows/enterprise-release.yml:252-258`

同一步还把带 glob 的 wheel 路径整体加引号，pip 不会由 shell 展开实际 wheel 文件：

- `.github/workflows/enterprise-release.yml:237-241`

使用仓库现有 wheel 执行同形态的 `pip install --dry-run --no-deps
"release/dist/callwarden-*.whl[all]"`，已复现 `Invalid wheel filename: callwarden-*`。

即使绕过这两处，MSI 把 `cw-client.exe` 放在 `bin`，却把它依赖的 `_internal` 安装为
`bin/cw-client_internal`；PyInstaller onedir 入口要求 sibling `_internal`：

- `release/windows/callwarden.wxs:97-108`
- `release/windows/callwarden.wxs:137-147`

此外 workflow 安装当前 `wix` dotnet CLI，却使用 WiX v3 `heat`/schema 和 `wix burn` 校验
MSI，工具链版本契约也未闭合：

- `.github/workflows/enterprise-release.yml:222-225,261-295`
- `release/windows/callwarden.wxs:3,37-49`

### P0-5 Linux 五子包不能共装，daemon 首启也会失败

client/local/agent 三个包都把各自 PyInstaller onedir 的内容复制到同一个
`/usr/lib/callwarden/runtime/`，都会拥有 `_internal/*`：

- `release/linux/build_packages.sh:198-221`

三个 control 文件没有 `Replaces/Conflicts`，而 enterprise 元包要求同时安装 client、agent、
daemon，daemon 又依赖 local。dpkg 会遇到跨包同路径覆盖，或者形成无法证明一致的混合 runtime。

即使先解决文件冲突，首次安装只 enable service，不初始化 registry DB：

- `release/linux/deb/daemon.postinst:88-121`

服务启动前却固定执行 `schema-check --strict`；strict 对未初始化 DB 明确返回 1：

- `release/linux/deb/systemd/callwarden-daemon.service:31-33`
- `rust_ext/src/bin/cw_daemon.rs:350-375`

Gate 5c 的 `systemctl start callwarden-daemon` 因而不能在干净 runner 通过。

### P0-6 macOS universal2/sign/notarization 契约未闭合

脚本只把 Rust 扩展用 `lipo` 合成 universal2，随后仍使用当前 runner 的单架构 Python/PyInstaller
构建 launcher 和嵌入式 Python；产物文件名为 universal2，但没有验证入口和 Python runtime
同时含 x86_64/arm64：

- `release/macos/build_pkg.sh:102-129`
- `release/macos/build_pkg.sh:162-200`

codesign 只签两个入口和两个 `callwarden_core.so`，没有递归签 `_internal` 中的其他 Mach-O
依赖：

- `release/macos/build_pkg.sh:295-327`

tag push 没有 `workflow_dispatch.inputs.dry_run`，Gate 4b 用 `|| 'true'` 将
`CW_BUILD_UNSIGNED` 设为 true；Gate 7 又只在 Ubuntu 上声称 Gate 4b 已 notarize：

- `.github/workflows/enterprise-release.yml:318-341`
- `.github/workflows/enterprise-release.yml:601-639`

因此生产 tag 的 pkg 不会按当前链路完成签名、公证和 stapling。

## 4. P1/P2 重要问题

### P1-1 Rust daemon 测试基线是红色

执行：

```text
cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib
326 passed; 25 failed; 60 filtered out
```

失败主要来自扩展 admin-only ACL 后，旧 backup/restore/GC/mount 测试仍使用普通 peer；另有
readonly 方法清单未同步。它们大多不是 handler ACL 的生产回归，但说明整改没有完成完整回归
维护。不能用新增 14 个 ACL 测试或 43 个 replicator 测试通过替代整个 daemon suite。

### P1-2 Rust CAS merge 只写入不完整语法事实

当前 merge 给 `symbol_contents.content` 写空字符串，并把所有 raw call 的 `callee_id` 写成 0：

- `rust_ext/src/daemon/cas_merge.rs:229-300`

它没有 workspace manifest，也没有运行跨文件 call resolve。即使配置和提交顺序修复，最新
snapshot 的符号正文、caller/callee 解析仍可能不完整，不能等价为标准 `db_build.py` 构建结果。

### P2-1 PR Checker 原 fail-open 已修，但 workspace 隔离仍不完整

`passed = errors == 0 and scan_complete` 及三条 `run_errors` 路径成立：

- `cicd/pr_check.py:66-177`

但 `guardrail_findings` schema 没有 `workspace_id`，查询只按 `file_path`；active workspace
读取异常又静默回退到 `ws_id=1`：

- `db/schema.py:453-469`
- `cicd/pr_check.py:258-300`

这更可能造成跨 workspace 误阻断而不是 fail-open，A19/A21 可认为原 P1 修复通过，但备注
应保留该隔离债务。

### P2-2 D7 原问题已修，歧义短名仍不稳定

反向影响方向、跨 target workspace 去重和完整 FQN 后缀判断已经正确：

- `db/db_cross_repo.py:178-248`
- `db/db_cross_repo.py:444-493`

当 import 只含短名且目标仓库有多个同名候选时，仍直接取 SQL 返回的第一个候选并写 0.7
置信度依赖。查询没有 `ORDER BY`，结果可能随存储顺序变化并制造假边：

- `db/db_cross_repo.py:191-215`

### P2-3 memfd 核心修复成立，六重校验不是所有 FD 的强制契约

真实 memfd 会校验 owner 和完整 seals，生产 handler 传入 `peer.uid`。但普通 regular-file FD
在 `F_GET_SEALS` 返回 `EINVAL/ENOTTY` 时直接跳过 seals，expected hash 仍为可选：

- `rust_ext/src/daemon/memfd.rs:164-205`
- `rust_ext/src/daemon/memfd.rs:229-315`
- `rust_ext/src/daemon/workspace.rs:1051-1078`

G10 的原 P1 可以通过，但备注不能写成所有 FD 均强制六重校验。

### P2-4 数字基线通过，不等于全部关键文档一致

`python scripts/check_baseline.py --check` 当前通过：206 MCP、35 功能 Mixin、40 个
`db_*.py`、16 语言、schema v41、产品 0.3.0。脚本只收集 root/docs/tests 下 78 个 Markdown，
又跳过其中 24 个历史/审计文件；其规则只覆盖上述数字模式：

- `scripts/check_baseline.py:142-175`
- `scripts/check_baseline.py:190-265`
- `scripts/check_baseline.py:310-339`

矩阵顶部仍写“88 个 .md”，当前矩阵实际包含 221 个唯一 ID、223 个 ID 形态行（D3 在概览
重复两次）。G8/G11/J8/K4 的绿色又与 systemd unit 的 CAS-only 注释、M4-M6 黄色状态直接
冲突。H14 还写“Windows MSI 仍需 PyInstaller”，N5/N8 已改称 PyInstaller 已加入。

## 5. 矩阵状态建议

必须回退：

- G8：✅ -> 🟡，generation/CAS 有，Rust save-to-query 仍有提交顺序和默认配置缺口；
- G11：✅ -> ❌，默认 Linux service 不启用 CodeGraph/Snapshot，merge 也非崩溃安全；
- J8：✅ -> 🟡，RPC/ACL/memfd 组件大部分成立，端到端数据发布和安装 socket 不成立；
- K4：✅ -> 🟡，dispatch 已接入，但“企业部署 save-to-query 已闭合”为假；
- N5/N6/N7/N8：保持 🟡 或回退 ❌，必须明确当前存在确定性失败，不是只差 runner 证据；
- H14：更新过时备注，不能继续写“缺 PyInstaller 步骤”。

可以保留本轮升级：

- A19/A21：原三条 fail-open 已修，保留 workspace finding 隔离债务；
- D7：原 P1-2 三项已修，保留歧义短名债务；
- G10：原 memfd owner/seals P1 已修，保留普通 FD/hash 可选边界；
- I1/I2/I3/I9/I11-I13/I15-I17/I19：核心 206/35/40/v41 数字已同步；
- G1/G3/G4：可写“Rust handler ACL 已修”，但不要扩展成“安装后的多用户 UDS 已验收”。

仍未完成且矩阵已正确保留黄色/红色的项目包括 A14、G9、G13-G15、G28-G29、H15、
I4/I7/I20、M4-M6 等。本轮没有发现它们被代码真正闭合。

## 6. 验证记录

| 验证 | 结果 |
|---|---|
| PR Checker + D7 + baseline + Python save-to-query 聚焦 pytest | 47 passed, 1 skipped |
| Rust daemon module tests | 326 passed, 25 failed, 60 filtered out |
| `check_baseline.py --check` | 通过；54/78 Markdown 进入数字扫描 |
| Linux/macOS shell syntax | `bash -n` 通过 |
| Windows WXS XML well-formed | 通过；不代表 WiX 语义/工具链通过 |
| Windows workflow 引号 wheel glob | pip dry-run 复现 `Invalid wheel filename: callwarden-*` |
| 本机 PyInstaller bundle | 未执行：当前 Python 环境未安装 PyInstaller |
| 真实 Linux 双 UID / dpkg / systemd / SMB | 本机 Windows 未执行；静态阻断已足以判定当前链路不能通过 |

## 7. 下一轮通过门槛

1. Rust 把 generation committed 移到 CodeGraph merge + manifest + snapshot 成功之后，并提供
   同 seq 崩溃重试测试。
2. 默认安装配置明确启用每 workspace CodeGraph DB，负责建目录、初始化 schema 和恢复。
3. socket 在启动后校验 owner/group/mode，真实双 UID 用户通过 `callwarden-clients` 组连接。
4. 修复 Windows `cw-agent` 检查、wheel glob、WiX 版本和 client `_internal` 布局，在干净
   Windows runner 安装后运行 `cw`/`cw-client`。
5. Linux 每个 PyInstaller role 使用独立 runtime 目录，fresh install 初始化 registry 后再
   strict check，五包共装和 systemd 首启通过。
6. macOS 对入口、Python runtime、全部 Mach-O 做 universal2 和递归 codesign 验证，并在
   macOS runner 实际 notarize/staple。
7. 修复 25 个 Rust daemon 测试，不允许用局部新增用例替代完整 daemon suite。
