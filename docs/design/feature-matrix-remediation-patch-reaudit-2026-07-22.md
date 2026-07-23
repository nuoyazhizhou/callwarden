# Feature Matrix 六项整改补丁独立复审（2026-07-22）

## 1. 结论

本轮不采信实现 Agent 的完成声明，重新沿生产入口、失败路径、测试和发布脚本核对当前
未提交工作区。结论是：**六项整改中 1 项完成、2 项部分完成、3 项未完成**，当前仍不能
宣称 enterprise watcher save-to-query 或三平台发布链闭合。

| 原整改项 | 复审结论 | 判定 |
|---|---|---|
| P0-1 generation 提交顺序 | generation 仍在 snapshot publish 前提交，且忽略 CAS 条件更新失败 | ❌ 未完成 |
| P0-2 默认 Linux daemon 发布 | 默认路径已启用，但 fresh CodeGraph DB 没有初始化主 schema | ❌ 未完成 |
| P0-3 多用户 socket 权限 | 安装脚本补了组关系，server 也尝试 chown；失败仍 fail-open | 🟡 部分完成 |
| P0-4 三平台发布链 | 若干确定性问题已修；Windows WiX 和 macOS 架构契约仍阻断 | 🟡 部分完成 |
| P1-1 Rust daemon 测试红色 | 完整 daemon suite 351/351 通过 | ✅ 完成 |
| P1-2 Rust CAS merge 事实完整性 | 正文写入有进展；跨文件 resolve、manifest 和既有空正文仍不正确 | ❌ 未完成 |

## 2. P0 阻断问题

### P0-1 generation 仍早于完整发布提交

`daemon_handle_refresh` 已不再直接写 committed，但外层 handler 仍在调用
`Replicator::replicate()` 和 snapshot publish **之前**写
`latest_committed_generation`：

- `rust_ext/src/daemon/workspace.rs:1510-1536`
- `rust_ext/src/daemon/workspace.rs:1538-1579`

这不满足上一轮“CodeGraph merge + manifest + snapshot 成功之后再 committed”的门槛。
snapshot 加载、发布或 staging apply 失败时，该 generation 已被提交，同 seq 重试会被
`file_generation_seen_inner` 按 committed generation 拒绝。

此外还有两个并发/失败路径：

1. `merge_summary` 缺失时 `unwrap_or(true)`，CAS 结果缺失、未进入 merge 或 DB path 为空仍会
   committed；`cas_miss`、`no_symbols` 也被当作成功。
2. `file_generation_committed()` 返回 `Ok(false)` 表示更新条件失效、已有更新覆盖当前 seen，
   handler 只处理 `Err`，仍继续 replicate。旧 handler 因而可能发布 stale snapshot。

对应条件更新语义见 `rust_ext/src/daemon/cas.rs:883-931`。当前测试验证了函数级 CAS，未覆盖
“merge 成功但 snapshot publish 失败”和两个 refresh handler 交错覆盖的生产场景。

### P0-2 fresh CodeGraph DB 首次 refresh 必然 merge 失败

默认 `codegraph_db_path_template` 已改为非空，目录也会创建；但生产路径随后只是
`rusqlite::Connection::open(&db_path)` 创建空 SQLite 文件：

- `rust_ext/src/daemon/config.rs:66-83`
- `rust_ext/src/daemon/workspace.rs:1369-1438`

`merge_cas_to_codegraph()` 在创建 manifest 表之前，先执行对 `workspaces`、`file_contents`、
`file_instances`、`symbols`、`calls`、`symbol_contents` 的 SQL；生产路径没有初始化这些表：

- `rust_ext/src/daemon/cas_merge.rs:73-156`
- `rust_ext/src/daemon/cas_merge.rs:652-702`

因此干净安装的首个 save 会得到 `no such table: workspaces`，不会产生可查询 snapshot。
`cw-daemon schema-check` 只初始化 registry DB，不初始化每 workspace 的 CodeGraph DB。

351 个 Rust 测试没有发现该问题，因为每个 merge 正向测试都先调用测试 helper 手工创建完整
CodeGraph schema：`rust_ext/src/daemon/cas_merge.rs:743-826`。

### P0-3 socket 修复仍是 fail-open

`daemon.postinst` 创建 `callwarden-clients` 并把 `callwarden` 用户加入附加组，这部分方向正确。
server bind 后也新增了 group lookup 与 chown：

- `release/linux/deb/daemon.postinst:24-45`
- `rust_ext/src/daemon/server.rs:184-229`

但组不存在或 chown 失败时只打印 warning，daemon 继续 ready；没有回读并校验 socket 的
owner/group/mode。结果仍可能是“服务健康但所有真实客户端无权连接”。企业默认配置指定了
`callwarden-clients`，此时配置错误应 fail closed。

新增代码还在整个 Unix 分支读取 `libc::__errno_location()`；Apple libc 暴露的是 `__error()`。
`server.rs` 以 `#![cfg(unix)]` 编译，因而该写法会阻断 macOS Rust 构建。

### P0-4 Windows 发布仍有 WiX 工具链硬冲突

以下修复成立：Windows 不再要求 `cw-agent.exe`、wheel glob 先解析实际文件、`cw-client.exe`
和 `_internal` 改为 sibling 布局。

但 workflow 安装的是 WiX v4 dotnet CLI，并调用 `wix build`；WXS 仍明确使用 WiX v3
namespace/`Product` schema，构建注释也仍是 `candle/light`。workflow 还调用 `wix heat` 和
`wix burn ... --inspect`，没有安装或固定与这些命令匹配的扩展/版本：

- `.github/workflows/enterprise-release.yml:222-305`
- `release/windows/callwarden.wxs:1-49`

XML well-formed 不能证明 WiX 语义可编译。Gate 4a 仍不能判完成。

### P0-5 macOS 从假 universal2 改成了未闭合的 arm64 降级

脚本把输出名从 `universal2` 改成 `arm64`，承认 PyInstaller runtime 是单架构；这是诚实的
降级，不是原 N6 universal2 要求的完成。workflow、版本清单和设计仍承诺 x86_64 + arm64：

- `release/macos/build_pkg.sh:82-93`
- `.github/workflows/enterprise-release.yml:201,312-344`
- `release/version.toml:34-35`
- `docs/design/cross-platform-packaging-release-plan.md:65-167`

脚本也没有对 PyInstaller 入口和嵌入式 Python 执行 `file`/`lipo` 架构校验，却无条件把产物
命名为 arm64。tag 的 `CW_BUILD_UNSIGNED` 条件已修，notarytool/stapler 路径存在；但在 Apple
编译错误和架构契约修复前，不能据此宣称 pkg 发布链通过。

## 3. P1 数据正确性

### P1-1 Rust daemon suite 已修复

本轮实跑：

```text
cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib
351 passed; 0 failed; 60 filtered out
```

这项可以关闭。`current_daemon_uid()` 与测试 peer 的 Windows 契约已同步，原 25 个 ACL 相关
失败不再存在。测试全绿不覆盖本文列出的 fresh schema、snapshot 失败和跨文件解析语义。

### P1-2 CAS merge 仍不等价于标准构建

`cas_symbol_contents.content` 已接入，这一子项成立；但仍有以下正确性缺口：

1. `LEFT JOIN` 缺失正文时用空字符串继续成功；`INSERT OR IGNORE` 不会修复旧数据库里同 hash
   的空正文。
2. 跨文件 callee 只解析“当前正在 merge 的文件”的出边。A 先于 B merge 时，A -> B 保持
   `callee_id=0`；B 后到不会回扫 A。B 更新时既有入边被清零，也不会绑定到 B 的新 symbol。
3. 短名/qualified name 查询使用无 `ORDER BY` 的 `LIMIT 1`，同名符号时会任取候选；没有
   import/use、模块、语言或歧义拒绝语义。
4. manifest 查询读取 `(file_size, total_lines)` 却只取第 2 列，并把 `total_lines` 写入
   `file_size`。`raw_hash/source_encoding/bom_kind/newline_style/mtime_ns` 全部硬编码，所有文件
   又固定 `is_dirty=1`，与 clean snapshot/CAS 共享语义不一致。

证据：

- `rust_ext/src/daemon/cas_merge.rs:235-325`
- `rust_ext/src/daemon/cas_merge.rs:334-422`
- `rust_ext/src/daemon/cas_merge.rs:498-519`
- `rust_ext/src/daemon/cas_merge.rs:558-581`

新增测试只覆盖预先建好 schema 的同文件 `main -> helper`，没有覆盖 A/B 两种 merge 顺序、
同名歧义、callee 更新、缺失正文或 clean manifest。

## 4. 文档与矩阵不一致

实现 Agent 没有修改 `_feature_matrix.md`。当前 G8/G11/J8/K4 仍标成“已闭合”，并保留
“企业部署 save-to-query 数据链已闭合”等已被上述生产反例否定的描述：

- `_feature_matrix.md:224-227`
- `_feature_matrix.md:375`
- `_feature_matrix.md:387`

H14 仍写 Windows 缺 PyInstaller，已经过时；N6 标题仍是 universal2，而实现已主动降级为
arm64。审计文件是历史证据，不应回写；矩阵的当前状态必须重新回退和更新备注。

建议状态：

- G8：✅ -> 🟡；generation/CAS 基础存在，完整发布提交点仍错误。
- G11：✅ -> ❌；默认 fresh CodeGraph DB 无 schema，snapshot 链不可用。
- J8/K4：✅ -> 🟡；RPC/ACL 组件存在，save-to-query 生产链未闭合。
- N5/N6/N8：保持 🟡，明确 WiX/Apple 架构阻断，不得写成只差 runner 证据。
- H14/N7：同步本轮真实局部修复，Linux独立 role runtime 和 registry fresh init 可记为完成子项。

## 5. 验证记录

| 验证 | 结果 |
|---|---|
| Rust daemon 完整 suite | 351 passed, 0 failed, 60 filtered |
| Python PR/D7/baseline/save-to-query 聚焦 suite | 47 passed, 1 skipped |
| `scripts/check_baseline.py --check` | 通过，54/78 Markdown 纳入数字扫描 |
| Linux/macOS shell syntax | `bash -n` 通过 |
| Windows WXS | XML well-formed；未证明 WiX v4 可编译 v3 WXS |
| 真实 MSI/pkg/deb/systemd/双 UID | 本机 Windows 未执行；源码仍有确定性阻断 |

## 6. 下一轮通过门槛

1. 把 committed 放到 snapshot publish 成功之后；`Ok(false)` 必须中止 stale handler，并补
   snapshot 失败、双 handler 交错、同 seq 重试测试。
2. 用唯一 schema/migration 入口初始化每 workspace CodeGraph DB，补空文件 DB 的真实 refresh
   到 query E2E。
3. socket group 配置非空时 chown/校验失败必须阻止 ready，并在 Linux 双 UID 测试验证 mode/GID。
4. CAS merge 改为 workspace 级 resolve pass，结果不得依赖文件到达顺序；manifest 使用真实
   canonical/raw 元数据。
5. Windows 固定单一 WiX 版本与 schema/harvest/验证命令，在干净 runner 构建并安装 MSI。
6. 明确选择 arm64-only 或真正 universal2；同步 workflow/version/docs，并在 macOS runner 校验
   所有 Mach-O 架构、签名、公证和 stapling。

