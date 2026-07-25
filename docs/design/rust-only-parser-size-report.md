# Rust-only Parser 切换前后发布物体积报告

> 状态：基线已锁定，待 CI 同 runner 实测填充  
> 版本：v1  
> 日期：2026-07-25  
> 关联设计：[rust-only-parser-cutover-plan.md](rust-only-parser-cutover-plan.md) §8 Phase 5 步骤 7 + §10 性能与包体门禁  
> 关联任务：`T-1784986236714-d99b0b76` Step #5 `publish_before_after_size_report`  
> 生成工具：`python release/build.py --pyinstaller` → `release/inspect_pyinstaller_bundle.py`

## 1. 报告目的

P1-G（Rust-only parser 生产切换）将正式发布物中的 Python `tree-sitter` 核心、
16 种 `tree-sitter_<lang>` grammar wheel 和 `callwarden.parsers.*` 语言实现模块
物理移除。本报告记录切换前后的发布物体积差异，验证满足设计 §10 的包体门禁：

| 指标 | 门禁 |
|---|---:|
| 安装目录减少（unpacked） | ≥25 MiB |
| 压缩包减少（archived） | ≥8 MiB |
| client/agent bundle parser distribution 文件数 | 0 |
| local bundle Python grammar distribution 文件数 | 0 |
| Rust `callwarden_core` 误删 | 不允许 |

## 2. 测量方法论

### 2.1 同 runner / 同 commit / 同压缩参数（强制）

为避免平台、构建缓存和压缩工具差异引入的噪声，所有 before/after 测量必须满足：

1. **同 runner image**：before 和 after 必须在同一个 CI runner 镜像上执行
   （例如 `ubuntu-22.04` / `windows-2022` / `macos-14`）。禁止用一台 Linux 实测
   与一台 Windows 实测做差。
2. **同 commit**：before 和 after 必须从同一颗 commit 切出两个分支——
   - `before`：在该 commit 上保留旧的 `release/pyinstaller/requirements-runtime.txt`
     和 `callwarden.spec`（含 Python tree-sitter hidden imports）；
   - `after`：在该 commit 上应用 P1-G 全部修改（删除 grammar 依赖、删除 hidden
     imports、加入 `_PARSER_GRAMMAR_EXCLUDES`）。
3. **同构建缓存状态**：每次测量前执行 `pyinstaller --clean`，禁止复用
   `build/callwarden/` 中的缓存 PYZ / Analysis 中间产物。
4. **同压缩参数**：
   - Linux/macOS：`tar czf <artifact> callwarden/`（gzip 默认级）
   - Windows：`Compress-Archive -Path 'callwarden' -DestinationPath <artifact>`
5. **同 Python 解释器**：before 和 after 必须使用同一颗 Python（同版本、同 ABI），
   避免 `.pyc` 文件大小差异。

### 2.2 测量工具

| 工具 | 输出 | 用途 |
|---|---|---|
| `release/inspect_pyinstaller_bundle.py --bundle <dir> --pyz-toc <toc> --report <json>` | bundle-report-{role}.json | unpacked 体积、distribution 占比、fail closed 检查 |
| `python release/build.py --pyinstaller` | dist/callwarden/ + bundle-report-local.json + artifact-manifest.json | 一站式构建 + 检查 + manifest |
| `du -sb dist/callwarden` / `Get-ChildItem -Recurse \| Measure-Object -Property Length -Sum` | 字节数 | 交叉验证 unpacked 体积 |
| `stat -c %s <artifact>` / `(Get-Item <artifact>).Length` | 字节数 | 压缩包体积 |

### 2.3 distribution 分类

bundle inspector 把 bundle 中的文件按 distribution 聚合（详见
`release/inspect_pyinstaller_bundle.py` 的 `_classify_distribution`）：

| distribution 名 | 含义 | P1-G 后预期 |
|---|---|---:|
| `tree_sitter` | Python tree-sitter 核心（`tree_sitter/`、`_binding*.pyd/.so`） | 0 文件 |
| `tree_sitter_<lang>` × 16 | 16 种 grammar wheel | 0 文件 |
| `callwarden_parsers` | `callwarden/parsers/*_parser.py` 等 Python 实现 | 0 文件 |
| `callwarden_core` | Rust 扩展 `callwarden_core.pyd` / `.so` | ≥1 文件（必须存在） |
| `python_runtime` | `_internal/` 中其他标准库与第三方依赖 | 减少（间接依赖清理） |
| `other` | 无法归类的文件 | 不强制变化方向 |

## 3. 基线（before）数据

### 3.1 设计文档已记录的理论基线

来源：[rust-only-parser-cutover-plan.md](rust-only-parser-cutover-plan.md) §2.5
（本机当前 Python 环境测得 tree-sitter 核心和 grammar 未压缩大小）：

| 组件 | 大小 |
|---|---:|
| `tree-sitter` 核心 | 0.25 MiB |
| 16 种 grammar wheel 文件 | 30.97 MiB |
| **合计** | **31.22 MiB** |

注意：此为 Python 包安装目录中的 `.pyd/.so` 单文件大小，不等同于 PyInstaller
bundle 中的体积。PyInstaller 会去重、剥离调试符号、压缩 PYZ，因此 bundle 中
tree-sitter distribution 实际字节数需以 bundle inspector 报告为准。

### 3.2 CI 实测基线（待填充）

> 占位字段：CI 在 P1-G 合并前的最后一次正式构建中，由
> `python release/build.py --pyinstaller` 生成 `bundle-report-local.json`，
> 把其中 `unpacked_bytes` / `distributions` 字段填入下表。

| 平台 | runner | commit | unpacked_bytes | unpacked_mb | tree_sitter bytes | tree_sitter_<lang> bytes | callwarden_parsers bytes | 压缩包 bytes | 压缩包 MiB |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| Linux x86_64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Linux aarch64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Windows amd64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| macOS arm64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |

## 4. 切换后（after）数据

### 4.1 预期 distribution 变化

P1-G 后所有 role 的 bundle 都通过 `_PARSER_GRAMMAR_EXCLUDES` 排除 Python
parser/grammar，并由 bundle inspector 的文件级 fail closed 检查兜底：

- `tree_sitter` distribution：**0 文件 / 0 字节**
- `tree_sitter_<lang>` × 16：**每个 0 文件 / 0 字节**
- `callwarden_parsers` distribution：**0 文件 / 0 字节**
- `callwarden_core` distribution：**≥1 文件**（Rust 扩展必须存在，由
  `_check_callwarden_core_present` 强制）

### 4.2 CI 实测数据（待填充）

> 占位字段：P1-G 合并后由 `python release/build.py --pyinstaller` 在同 runner、
> 同 commit、同压缩参数下生成。每个平台的 `bundle-report-local.json` 和
> `artifact-manifest.json` 作为 CI artifact 持久化。

| 平台 | runner | commit | unpacked_bytes | unpacked_mb | callwarden_core bytes | python_runtime bytes | 压缩包 bytes | 压缩包 MiB |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Linux x86_64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Linux aarch64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Windows amd64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| macOS arm64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |

## 5. 前后差异（gate 验证）

### 5.1 门禁计算公式

```
unpacked_delta = before_unpacked_bytes - after_unpacked_bytes
archived_delta = before_archived_bytes - after_archived_bytes
```

每个平台独立计算，禁止用一个平台的结果外推其他平台（设计 §2.5）。

### 5.2 门禁验证表

| 平台 | unpacked_delta MiB | unpacked 门禁 ≥25 MiB | archived_delta MiB | archived 门禁 ≥8 MiB | Rust callwarden_core 保留 | 结论 |
|---|---:|:---:|---:|:---:|:---:|:---:|
| Linux x86_64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Linux aarch64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| Windows amd64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |
| macOS arm64 | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ | _待填充_ |

### 5.3 理论预期（基于 §3.1 基线）

- 16 种 grammar wheel 未压缩合计 30.97 MiB，tree-sitter 核心 0.25 MiB，
  合计 31.22 MiB。
- PyInstaller bundle 中的 tree-sitter distribution 实际字节数通常小于等于
  Python 安装目录字节数（PYZ 压缩 + 去重），因此 unpacked_delta 预期落在
  25–31 MiB 区间，刚好满足 ≥25 MiB 门禁。
- 压缩包收益取决于各平台原生库的可压缩性。Linux glibc 的 `.so` 通常可压缩至
  原大小的 30–40%，Windows `.pyd` 因包含调试符号可压缩性更高。预期
  archived_delta 满足 ≥8 MiB 门禁。
- 若某平台 unpacked_delta < 25 MiB，必须按设计 §10 要求提供逐文件证据解释
  平台差异（例如该平台 grammar wheel 本身体积较小），不得通过调整压缩参数
  凑数。

## 6. client/agent bundle 额外门禁

client/agent bundle（仅 Linux）除满足上述 parser distribution 零容忍外，还
额外排除 `numpy` 和 `callwarden.db` 中除 `db_daemon` 外的子模块。预期：

| distribution | client/agent 预期文件数 | 说明 |
|---|---:|---|
| `tree_sitter` | 0 | fail closed |
| `tree_sitter_<lang>` × 16 | 0 | fail closed |
| `callwarden_parsers` | 0 | fail closed |
| `callwarden_core` | ≥1 | RPC 链路需要 Rust 扩展 |
| `numpy` | 0 | client/agent 不做本地解析，无向量搜索路径 |

client/agent bundle 的体积变化不在 ≥25 MiB / ≥8 MiB 门禁范围内（client/agent
本来就是 P0-B 拆出的轻包），但 bundle inspector 仍会输出其 distribution 报告
供审计。

## 7. CI 集成

### 7.1 工作流位置

体积报告由 `.github/workflows/pyinstaller-build.yml` 在 Package artifacts 步骤
后生成。每次正式构建（push to main / tag）都会产出：

- `callwarden-<platform>.<ext>`：压缩包 artifact
- `callwarden-<platform>-bundle-report.json`：bundle inspector 报告

CI 完成后，由 release manager 把 before/after 报告中的 `unpacked_bytes` 和
`artifact_bytes` 填入本报告 §3.2 和 §4.2 表格，并计算 §5.2 门禁验证表。

### 7.2 一站式构建命令

```bash
# 在 CI runner 上一次性完成：Rust 扩展检查 + PyInstaller 构建 + bundle inspector + manifest
python release/build.py --pyinstaller --role all

# 产物：
#   dist/callwarden/                    local bundle（所有平台）
#   dist/callwarden-client/             client/agent bundle（仅 Linux）
#   release/bundle-report-local.json    local bundle inspector 报告
#   release/bundle-report-client.json   client bundle inspector 报告（仅 Linux）
#   release/artifact-manifest.json      含 parser ABI + bundle 体积摘要的 manifest
```

### 7.3 manifest 字段

`release/artifact-manifest.json` 现在记录以下 P1-G 字段（由
`release/build.py` 的 `generate_manifest(bundles, bundle_reports)` 写入）：

```json
{
  "product": "Call Warden",
  "version": "0.3.2",
  "abi": {
    "python": "cp311",
    "parser": 2,
    "snapshot": 2,
    "schema_registry": 3,
    "schema_cas": 2,
    "schema_workspace": 4
  },
  "runtime": {
    "python_min": "3.10",
    "rust_edition": "2021",
    "tree_sitter": "0.26",
    "pyo3": "0.29"
  },
  "build_host": {
    "os": "Linux",
    "machine": "x86_64",
    "python": "3.11.x"
  },
  "bundles": [
    {
      "path": "dist/callwarden",
      "role": "local",
      "unpacked_bytes": 0,
      "unpacked_mb": 0.0,
      "file_count": 0,
      "module_count": 0,
      "distributions": {
        "callwarden_core": {"file_count": 1, "byte_count": 0}
      }
    }
  ]
}
```

`abi.parser` 字段是 P1-G 后发布审计的关键证据，回滚时必须校验 parser ABI
一致（设计 §9.3）。

## 8. 失败处理

### 8.1 unpacked_delta < 25 MiB

1. 检查 `bundle-report-local.json` 的 `distributions` 字段，确认
   `tree_sitter` / `tree_sitter_<lang>` / `callwarden_parsers` 是否真的为 0。
2. 若上述 distribution 非零，说明 spec excludes 或 hidden imports 未生效，
   阻塞发布并修复 `release/pyinstaller/callwarden.spec`。
3. 若上述 distribution 全为 0 但 unpacked_delta 仍 < 25 MiB，说明该平台
   grammar wheel 本身体积较小（例如某些 Linux 发行版的 `.so` 剥离了调试符号），
   需在 §5.2 表格附加逐文件证据，由 Reviewer 裁决是否放行。

### 8.2 archived_delta < 8 MiB

1. 检查压缩参数是否与 before 一致（gzip 级别、zip 方法）。
2. 检查 PyInstaller `--clean` 是否生效（避免缓存复用导致 after 体积虚高）。
3. 若压缩参数一致但 archived_delta 仍 < 8 MiB，说明该平台 grammar wheel
   可压缩性低（例如已经过 strip 的 `.so`），需在 §5.2 表格附加证据。

### 8.3 Rust callwarden_core 误删

bundle inspector 的 `_check_callwarden_core_present` 会直接报错，阻塞发布。
修复 `release/pyinstaller/callwarden.spec` 的 `binaries = [(rust_ext_path, '.')]`
配置，确认 `CW_RUST_EXT_PATH` 环境变量或根目录 `callwarden_core.pyd/.so` 存在。

## 9. 报告更新责任

| 时机 | 责任人 | 动作 |
|---|---|---|
| P1-G 合并前 | Release Packaging Agent | 在 CI 跑 before 构建，填充 §3.2 |
| P1-G 合并后首次正式构建 | Release Packaging Agent | 在同 runner 跑 after 构建，填充 §4.2 + §5.2 |
| 每次正式 release | Release Manager | 重新跑 before/after（如有 grammar 版本变化），更新报告 |
| parser ABI 升级 | Release Manager | 同步更新 §7.3 manifest 字段示例 |

本报告在 P1-G 任务进入 review 前必须包含 §1–§7 完整结构；§3.2 / §4.2 / §5.2
的实测数据可在 CI 产出后补充，但门禁计算公式和理论预期必须可复现。
