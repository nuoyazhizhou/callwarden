# Call Warden Rust-only Parser 发布说明

> 版本：0.3.3+（Rust-only parser 生产切换）
> 日期：2026-07-25
> 关联设计：[rust-only-parser-cutover-plan.md](design/rust-only-parser-cutover-plan.md)
> 任务：T-1784986236712-736b2331（Rust-only parser 生产切换与 Python grammar 退场）

## 1. 概述

Call Warden 0.3.3+ 完成从 Python/Rust 双 parser 到 Rust-only parser 的生产切换。
正式发布物（PyInstaller 冻结包）物理移除 Python `tree-sitter` 核心、16 种 Python
grammar wheel 和 `callwarden.parsers.*` 语言实现模块，生产解析统一由 Rust
`callwarden_core` 扩展完成。

### 1.1 主要变化

| 项目 | 0.3.2 及之前 | 0.3.3+ |
|------|-------------|--------|
| 生产 parser | Rust 为主，Python fallback | **Rust-only**（fail closed） |
| Python `tree-sitter` 核心 | 进入冻结包 | **移除** |
| 16 种 Python grammar wheel | 进入冻结包 | **移除** |
| `callwarden.parsers.*` | 进入冻结包 | **移除**（保留源码作为开发 reference） |
| 解析失败处理 | 静默回退 Python | **显式 fail closed**，记录 diagnostics |
| client/agent bundle | 携带 parser | **零 parser**（设计 Phase 1） |
| 包体（unpacked） | ~31 MiB parser 开销 | **减少 ≥25 MiB**（设计 §10 门禁） |
| 包体（archived） | — | **减少 ≥8 MiB**（设计 §10 门禁） |

### 1.2 兼容性

- **数据库 schema**：无变化（设计 §9.3 原则上不改变 workspace schema）
- **parser ABI**：保持 `parser = 2`（version.toml `[abi]` 段）
- **Python CLI/MCP 表现层**：保留，不变更
- **MCP 工具数量**：不变（229+）
- **CLI 命令数量**：不变（145+）
- **支持语言数量**：不变（16 种）

## 2. 安装说明

### 2.1 正式发布包（推荐）

从 GitHub Release 下载对应平台的压缩包：

| 平台 | 架构 | 压缩包 | 角色 |
|------|------|--------|------|
| Windows | amd64 | `callwarden-windows-amd64.zip` | local |
| macOS | arm64 | `callwarden-macos-arm64.tar.gz` | local |
| Linux | x86_64 | `callwarden-linux-x86_64.tar.gz` | local + client/agent/daemon |
| Linux | aarch64 | `callwarden-linux-aarch64.tar.gz` | local + client/agent/daemon |

**Linux 独有 client/agent 包**：`callwarden-client-linux-<arch>.tar.gz`（无 parser/numpy，
仅 RPC + watcher，UDS/TCP 通信）。

### 2.2 解压即用

```bash
# Linux/macOS
tar xzf callwarden-linux-x86_64.tar.gz
./callwarden/cw --version
./callwarden/cw --help

# Windows
Expand-Archive callwarden-windows-amd64.zip
.\callwarden\cw.exe --version
```

正式发布包是**自包含的**，不依赖系统 Python。

### 2.3 源码安装（开发期）

```bash
# 基础安装（不含 Python parser reference）
pip install .

# 开发期 reference（含 Python parser 用于 alignment 对照）
pip install ".[parser-reference]"
```

`parser-reference` extra 仅用于开发期 Rust/Python alignment 对照，不进入正式发布包。

## 3. Fallback 移除说明

### 3.1 Python parser 不再进入正式包

正式发布包中物理移除以下内容：

- `tree_sitter`（Python tree-sitter 核心）
- `tree_sitter_rust` / `tree_sitter_typescript` / ... / `tree_sitter_elixir`（16 种 grammar wheel）
- `callwarden.parsers.*`（Python 各语言 parser 实现模块）
- `_binding*.pyd` / `_binding*.so`（tree-sitter Python 核心 binding 原生库）

bundle inspector（`release/inspect_pyinstaller_bundle.py`）默认 fail closed：
所有 role 都禁止 PARSER_DISTRIBUTIONS（零容忍）。

### 3.2 解析失败行为

Rust parser 失败时**不再静默回退 Python**，而是按设计 §5.3 错误语义处理：

| 状态 | 行为 |
|------|------|
| `ok` | 发布完整 ParseFact |
| `partial` | 发布可用事实并持久化 diagnostics，不冒充完整成功 |
| `unsupported` | 不发布空图谱，记录语言/构造并进入可观测失败 |
| `failed` | 不替换上一代可查询 snapshot，记录失败并允许重试 |
| `stale` | generation CAS 拒绝，不覆盖新状态 |

### 3.3 frozen runtime 强制 rust-strict

正式冻结包固定 `CW_PARSE_MODE=rust-strict`。frozen build 收到 `python-reference`
或 `CW_DISABLE_RUST_PARSE` 时返回明确错误（设计 §7）。

## 4. 兼容包与回滚策略

### 4.1 发布前回滚（gate 失败）

任一语言 gate 失败时，该版本不得删除 local/daemon 的 Python grammar（设计 §9.1）。
client/agent 轻包可独立发布，不受 local parser gate 阻塞。

### 4.2 发布后回滚优先级

设计 §9.2 回滚优先级：

1. **回滚到上一正式安装包**（首选）
   - 下载上一版本 GitHub Release
   - 替换当前安装目录
   - 上一版本 cw 能读取旧 snapshot（设计 §9.3）

2. **关闭受影响 workspace 的自动 refresh，保留上一 snapshot**
   - `cw watcher stop <workspace>`
   - 上一 snapshot 仍可查询

3. **发布修复后的 Rust parser patch**
   - 修复 Rust parser 缺陷
   - 重新构建并发布

4. **极端情况：发布独立 `parser-compat` 包**
   - 单独发布含 Python grammar 的兼容包
   - 用户手动安装作为临时过渡

### 4.3 禁止行为

**禁止**在同一生产进程中临时下载 Python grammar 并静默恢复（设计 §9.2）。
原因：
- 破坏离线部署能力
- 破坏 SBOM 完整性
- 破坏签名和可复现性
- 隐藏缺陷，延误修复

### 4.4 数据兼容

- parser ABI 变化时必须提升 parser ABI 版本号
- 新 ABI 产物不能覆盖旧 ABI CAS entry
- 回滚版本应能读取旧 snapshot，不能读取时从源文件重建
- dirty overlay 在重建或回滚期间不得进入 Global CAS

## 5. 包体报告

### 5.1 门禁（设计 §10）

| 指标 | 门禁 |
|------|------:|
| client/agent parser distribution 数 | 0 |
| local/daemon Python grammar distribution 数 | 0 |
| 安装目录减少 | ≥25 MiB |
| 压缩包减少 | ≥8 MiB |
| 单文件 parse P95 | <50 ms |
| watcher save-to-query P95 | <3 s |
| Rust parser panic | 0 |
| clean fixture fatal parse miss | 0 |
| generation 丢失 | 0 |
| stale generation 覆盖 | 0 |
| 10×5 clean workspace 重复 parse 率 | <5% |

### 5.2 实际报告（待 CI 运行）

每个平台的实际包体报告由以下脚本生成：
- `release/inspect_pyinstaller_bundle.py`：bundle 结构 + distribution 占比 + fail closed
- `.github/workflows/e2e/run_platform_e2e.py`：8 项 E2E 验证 + 性能报告
- `release/verify_upgrade_rollback_supply_chain.py`：升级/回滚/供应链验证 + SBOM

报告 JSON 包含：
- `bundle_unpacked_mib`：解压目录体积
- `artifact_mib`：压缩包体积
- `startup_time_median_ms`：启动时间中位数
- `rss_peak_kb`：峰值 RSS
- `parse_latency_p95_ms`：单文件 parse P95 延迟

### 5.3 预期节省

基于设计文档 §2.5 的本机度量：

| 组件 | 0.3.2 大小 | 0.3.3+ 状态 |
|------|----------:|----------:|
| `tree-sitter` 核心 | 0.25 MiB | 移除 |
| 16 种 grammar wheel | 30.97 MiB | 移除 |
| 合计 | 31.22 MiB | **0 MiB** |

实际节省由各平台原生库可压缩性决定，第一版门禁设为：
- 安装目录至少减少 25 MiB
- 压缩包至少减少 8 MiB
- 每个平台报告真实差值，不用一个平台的结果外推其他平台

## 6. 跨平台验收矩阵

设计 §8 Phase 6 验收矩阵：

| 平台 | 架构 | 角色 | workflow |
|------|------|------|----------|
| Windows | amd64 | local/client | `.github/workflows/e2e-verify-windows-amd64.yml` |
| macOS | arm64 | local/client | `.github/workflows/e2e-verify-macos-arm64.yml` |
| Linux | x86_64 | local/client/agent/daemon | `.github/workflows/e2e-verify-linux-x86_64.yml` |
| Linux | aarch64 | local/client/agent/daemon | `.github/workflows/e2e-verify-linux-aarch64.yml` |

附加 Linux 兼容矩阵：
- glibc 下限（manylinux2014 = glibc 2.17）
- musl 静态构建（alpine 容器）
- Ubuntu 14.04–24.04 容器挂载路径
- SMB/CIFS 与 VS Code Remote 工作区

## 7. 灰度发布路径（设计 §8 Phase 7）

1. 内部开发机先运行 `shadow` 模式，收集真实差异
2. 选择 1–2 个仓库运行 `rust-strict` canary
3. 扩展到 10 用户 × 5 workspace 共享场景
4. 观察一个完整开发周期：
   - checkout/repo sync
   - dirty save
   - daemon restart
   - schema upgrade
   - mixed encoding
5. 发布 Rust-only parser 正式版
6. 下一小版本删除冻结构建中的兼容开关
7. Python parser 源码至少保留两个版本周期

## 8. SBOM 和许可证

每个正式发布包都包含 SBOM manifest，记录：
- `product` / `version`
- `platform`（OS / arch / libc）
- `parser_abi`（parser / snapshot / schema_registry / schema_cas / schema_workspace）
- `grammar_versions`（tree-sitter-* crate 版本）
- `rust_extension_sha256`（callwarden_core.pyd/.so SHA256）
- `python_version`
- `license_files`（bundle 中的 LICENSE 文件清单）
- `provenance`（源 commit / build runner / build repo）

SBOM 由 `release/verify_upgrade_rollback_supply_chain.py` 生成。

## 9. 已知限制

- **PyInstaller 在 musl 环境下可能存在兼容性问题**：alpine 容器中 PyInstaller
  构建可能失败，此时只验证 Rust 扩展的 musl 静态链接
- **client/agent bundle 仅 Linux 构建**：UDS/SO_PEERCRED/SCM_RIGHTS 是 Linux 特有
- **macOS 暂不支持 x86_64**：当前 CI 只有 macos-latest（arm64），如需 x86_64 支持
  需要在 macos-13 runner 上单独构建

## 10. 参考文档

- [Rust-only parser 切换设计](design/rust-only-parser-cutover-plan.md)
- [跨平台打包与发布计划](design/cross-platform-packaging-release-plan.md)
- [Parse 输入 ABI](design/parse-input-abi.md)
- [企业 Phase 1-3 详情](design/enterprise-phase1-phase3-detail.md)
- [独立复审报告](design/rust-only-parser-cutover-reaudit.md)
- [Rust-only parser 包体报告](design/rust-only-parser-size-report.md)
