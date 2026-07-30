# Phase 5-4 契约：安装器、升级、回滚与六平台 smoke

**Task ID**: `T-1785148066857-a7b3df55`（Phase 5-4，父任务 T-1785148066857-a972dd1c）
**状态**: contract
**日期**: 2026-07-30
**验证环境**: Windows 10 (开发主机) + WSL2 (Linux E2E) + GitHub Actions (六平台 CI)

## 1. 范围

Phase 5-4 是 Phase 5 的收尾阶段，验证安装器、升级/回滚管道和六平台 smoke 测试的端到端可用性。本阶段以**验证现有资产**为主，不新增核心功能代码。

**涉及**：
- **安装器验证**：`install.py`（pip 级联安装）+ `release/build.py`（统一构建管道）+ DEB/MSI/pkg 包脚本
- **升级/回滚验证**：`release/verify_upgrade_rollback_supply_chain.py`（N-1 升级 + 回滚 + SBOM + 离线安装）
- **六平台 smoke**：CI workflow matrix 验证冻结产物的 `cw --version` / `cw --help` / `cw server --check-imports`
- **本地 smoke**：开发环境中运行 `cw` CLI 的基础验证

**六平台清单**：
1. `windows-amd64` — Windows 10+ x86_64
2. `windows-arm64` — Windows 11 ARM64
3. `linux-amd64` — Ubuntu 22.04 x86_64
4. `linux-arm64` — Ubuntu 24.04 ARM64
5. `macos-arm64` — macOS 14+ Apple Silicon
6. `linux-musl` — Alpine 3.19+ (musl 静态链接)

**不涉及**（已在 Phase 5-1/5-2/5-3 完成）：
- 新增 Rust CLI 代码（clap 命令树 + config loader + stats + output 层）
- 新增 daemon RPC client/agent
- 新增路由/兼容输出层

## 2. 现有资产盘点

### 2.1 安装器资产

| 资产 | 路径 | 说明 |
|---|---|---|
| Python 安装器 | `install.py` | pip 级联安装：核心 → 语言 grammar → 可选依赖；幂等 + 失败不中断 |
| 统一构建管道 | `release/build.py` | setuptools + maturin + PyInstaller 编排 |
| 版本同步 | `release/version_sync.py` + `release/version.toml` | 唯一版本源，多文件同步 |
| 配置加载器 | `release/config_loader.py` | 分层配置（默认 → 文件 → 环境变量 → CLI） |
| 产物检查 | `release/inspect_pyinstaller_bundle.py` | PyInstaller bundle inspector + fail-closed |
| 构建脚本 | `release/_check_artifacts.py` | 构建产物完整性检查 |

### 2.2 平台打包资产

| 平台 | 路径 | 说明 |
|---|---|---|
| Linux DEB | `release/linux/deb/` | 5 角色包（daemon/agent/client/enterprise/local）+ systemd unit + sysusers/tmpfiles |
| Linux 构建脚本 | `release/linux/build_packages.sh` | DEB 构建脚本 |
| macOS pkg | `release/macos/build_pkg.sh` | macOS pkg 构建脚本 |
| Windows MSI | `release/windows/callwarden.wxs` | WiX MSI 定义 |
| PyInstaller spec | `release/pyinstaller/callwarden.spec` | 冻结包规格（local + client 角色） |
| PyInstaller 入口 | `release/pyinstaller/entry_*.py` | cw / cw_agent / cw_client 三入口 |
| 运行时依赖 | `release/pyinstaller/requirements-runtime*.txt` | 白名单运行时依赖（local/client 分离） |

### 2.3 升级/回滚资产

| 资产 | 路径 | 说明 |
|---|---|---|
| 升级回滚验证 | `release/verify_upgrade_rollback_supply_chain.py` | N-1 升级 + 回滚 + SBOM + 离线安装验证 |
| 离线安装脚本 | `release/linux/deb/offline/install-offline.sh` | DEB 离线安装 |
| 回滚策略文档 | `docs/design/rust-only-parser-cutover-plan.md §9` | 回滚优先级（包 → snapshot → patch → compat 包） |
| 部署手册 | `docs/design/daemon-deploy-runbook.md` | 9 节部署/升级/回滚手册 |

### 2.4 CI smoke 测试资产

| 资产 | 路径 | 说明 |
|---|---|---|
| PyInstaller 构建 | `.github/workflows/pyinstaller-build.yml` | 5 平台 matrix + smoke test 步骤 |
| E2E amd64 | `.github/workflows/e2e-verify-linux-x86_64.yml` | Linux x86_64 E2E |
| E2E arm64 | `.github/workflows/e2e-verify-linux-aarch64.yml` | Linux ARM64 E2E + musl 静态构建（第 6 平台） |
| 企业发布 | `.github/workflows/enterprise-release.yml` | 企业级发布管道 |
| E2E 运行脚本 | `.github/workflows/e2e/run_platform_e2e.py` | 共享 E2E 测试脚本 |

### 2.5 smoke 测试步骤（pyinstaller-build.yml L77-93）

```bash
# 所有平台共通
"./dist/callwarden/cw" --version
"./dist/callwarden/cw" --help >/dev/null
"./dist/callwarden/cw" server --check-imports

# Linux 专属（client/agent 独立 bundle）
"./dist/callwarden-client/cw-client" --help >/dev/null
"./dist/callwarden-client/cw-agent" --help >/dev/null
```

## 3. 验证矩阵

### 3.1 D1: 安装器验证

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D1.1 | `cw install --check` | 检查依赖状态，不安装 | 退出码 0（已安装）或 1（部分缺失） |
| D1.2 | `cw install` | 级联安装核心 + 语言 + 可选依赖 | 幂等，已安装的跳过 |
| D1.3 | `cw install --lang rust python` | 仅安装指定语言 grammar | 只安装 rust + python grammar |
| D1.4 | `python release/build.py --check` | 版本一致性检查 | version.toml 与 pyproject.toml/Cargo.toml 一致 |
| D1.5 | `python release/build.py --rust` | 构建 Rust 扩展 | 生成 .pyd/.so |
| D1.6 | `python release/build.py --pyinstaller --role local` | 构建 local 角色冻结包 | dist/callwarden/cw 可执行 |

### 3.2 D2: 升级/回滚验证

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D2.1 | `release/verify_upgrade_rollback_supply_chain.py --bundle dist/callwarden` | N-1 升级 + 回滚 + SBOM 全通过 | 退出码 0 |
| D2.2 | schema N-1 → N 升级 | schema_version 自动迁移 | 无 `no such table` 错误 |
| D2.3 | 回滚到 N-1 | rollback_config flag=1 回退 Python 路径 | 功能不中断 |
| D2.4 | SBOM 检查 | cyclonedx.json 完整 | 所有依赖有 license |
| D2.5 | 离线安装 | install-offline.sh 无网络可用 | DEB 安装成功 |

### 3.3 D3: 六平台 smoke

| 平台 | CI workflow | smoke 步骤 | 验证点 |
|---|---|---|---|
| D3.1 | windows-amd64 | cw --version / --help / check-imports | 三者退出码 0 |
| D3.2 | windows-arm64 | 同上 | 同上 |
| D3.3 | linux-amd64 | cw --version / --help / check-imports + cw-client/cw-agent --help | 五者退出码 0 |
| D3.4 | linux-arm64 | 同 linux-amd64 | 同上 |
| D3.5 | macos-arm64 | cw --version / --help / check-imports | 三者退出码 0 |
| D3.6 | linux-musl | alpine 容器内 musl 静态构建 + smoke | 构建成功 + cw --version 退出码 0 |

### 3.4 D4: 本地 smoke（开发环境）

| 场景 | 命令 | 期望行为 |
|---|---|---|
| D4.1 | `cw --version` | 输出版本号，退出码 0 |
| D4.2 | `cw --help` | 输出帮助，退出码 0 |
| D4.3 | `cw stats` | 输出 JSON 统计，退出码 0 |
| D4.4 | `cw server --check-imports` | MCP 工具注册无 ImportError，退出码 0 |
| D4.5 | `cw install --check` | 依赖状态检查，退出码 0 或 1 |

## 4. 预期差异

### 4.1 平台特定差异

| 维度 | Windows | Linux | macOS | Alpine(musl) |
|---|---|---|---|---|
| 后缀 | .exe / .dll | 无 / .so | 无 / .dylib | 无 / .so（静态） |
| daemon | 不可用（stub） | UDS + SO_PEERCRED | UDS（LOCAL_PEERCRED） | UDS + SO_PEERCRED |
| client/agent | 独立 bundle | 独立 bundle | 不构建 | 不构建 |
| PyInstaller | 有 | 有 | 有 | 可能有兼容性问题 |
| systemd | 无 | 有 | 无 | 有（OpenRC 可选） |

### 4.2 不可在本地验证的场景

1. **CI 六平台 smoke**：需 GitHub Actions runner，本地仅能验证 Windows + WSL2
2. **macOS pkg 构建**：需 macOS 环境
3. **DEB 包安装**：需 Linux 环境（WSL2 可验证）
4. **musl 静态构建**：需 alpine 容器或 musl 工具链

## 5. 实现计划

### P0: 契约与资产盘点（当前）

1. **编写本契约文档** ✅
2. **盘点现有资产**：安装器/构建/升级回滚/CI smoke 已齐全
3. **识别缺口**：无契约文档（本文件填补）、无 §37 migration-manifest 记录

### P1: 本地 smoke 验证

1. **运行 `cw --version` / `cw --help` / `cw stats`**：验证 CLI 基础功能
2. **运行 `cw server --check-imports`**：验证 MCP 工具注册
3. **运行 `cw install --check`**：验证依赖检查
4. **运行 `python release/build.py --check`**：验证版本一致性

### P2: 升级/回滚验证

1. **检查 `verify_upgrade_rollback_supply_chain.py` 脚本完整性**
2. **验证 rollback_config 表**：确认所有 Rust 短路 feature 已登记
3. **验证 schema 迁移路径**：SCHEMA_VERSION 一致性

### P3: CI smoke 验证（文档级）

1. **确认 `pyinstaller-build.yml` matrix 覆盖六平台**
2. **确认 smoke test 步骤完整**（cw --version / --help / check-imports）
3. **确认 `e2e-verify-linux-aarch64.yml` 含 musl 静态构建**
4. **记录 CI 验证状态**（需 CI 真实运行确认）

### P4: 文档与收尾

1. **migration-manifest.md §37 Phase 5-4 Review 清单**
2. **更新状态表第 315 行**为 ✅
3. **close Phase 5-4 任务 + Phase 5 父任务**

## 6. 验收标准

1. **D1 安装器**：`cw install --check` + `release/build.py --check` 通过
2. **D2 升级/回滚**：rollback_config 完整 + schema 一致性通过
3. **D3 六平台 smoke**：CI workflow matrix 覆盖六平台 + smoke 步骤完整（CI 实际运行需 GitHub Actions）
4. **D4 本地 smoke**：`cw --version` / `--help` / `stats` / `check-imports` 全通过
5. **migration-manifest.md §37 Review 清单完整**
6. **Phase 5-4 任务 7 步状态机完成 + closed**
7. **Phase 5 父任务 closed**（所有 4 个子任务完成）

## 7. 风险与注意事项

### 7.1 AGENTS.md 强制规则

- **规则 29**：PyInstaller 发布验收必须实例化 MCP Server（`cw server --check-imports`）
- **规则 30**：PyInstaller 排除包前必须审计生产顶层导入
- **规则 23**：TRAE 沙箱拦截 sh.exe 子进程对 `~/.callwarden/` 的写操作
- **规则 22**：代码变更必须同步更新文档（本阶段涉及 migration-manifest.md）

### 7.2 平台特定风险

1. **musl 静态构建兼容性**：PyInstaller 在 alpine 上可能有兼容性问题（已知风险，workflow 中有处理）
2. **Windows ARM64**：PyInstaller 6.21+ 支持，但需 runner 可用
3. **macOS 签名**：pkg 需 notarization（企业部署可选）
4. **离线安装**：DEB 离线安装需预下载所有依赖包

### 7.3 本地验证局限

- Windows 开发环境无法验证 Linux/macOS 专属场景
- CI 六平台 smoke 需 GitHub Actions 实际运行
- WSL2 可部分验证 Linux 场景（但非真实 CI 环境）

## 8. 与 Phase 5-1/5-2/5-3 的关系

| Phase | 交付物 | Phase 5-4 关系 |
|---|---|---|
| 5-1 | Rust CLI 命令树 + config + stats | Phase 5-4 验证 `cw` binary 可执行 + smoke 通过 |
| 5-2 | Rust client/agent + daemon RPC | Phase 5-4 验证 `cw-client`/`cw-agent` binary smoke |
| 5-3 | 路由与兼容输出 | Phase 5-4 验证输出在终端/journal 中的兼容性 |
| **5-4** | **安装器/升级/回滚/smoke 验证** | **不新增功能，验证 Phase 5-1/5-2/5-3 产物的端到端可用性** |

## 9. 下一步

Phase 5-4 完成后，Phase 5 全部子任务收尾。下一步推进：
- **Phase 6**: 文档与示例（用户指南/开发者文档/示例）
- **Phase 7**: 清理 rollback_config（rollback_window_until 过期后删除 rollback_entry）
