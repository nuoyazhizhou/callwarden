# TP-06 可追溯测试候选构建合同（冻结版 r1）

阶段 B0 前置 · owner: acceptance-builder · 日期 2026-10-12 · 上游：TP-01、TP-02（已 closed）、BUILD_ENV.md §2/§4

本文冻结 TP-06 的 committed profile、run 根、源快照、MSVC 环境、全部构建命令（cwd/env/expected rc/报告路径）与产出物口径。结果类散列写入同目录 `build_results.json`（证据 manifest），不在本文手抄。依据 TASK_BREAKDOWN §5：TP-06 须列全部确切命令、cwd/env、expected rc 与报告路径后才可派工。

## 1. 冻结的 committed profile（业务决策落盘）

用户 2026-10-12 决策「仅 Windows + Py3.14」。本卡只冻结本机可直接执行的那一个 profile；其余 3 个 CI 候选保持 `frozen=false / UNDECIDED`，未经业务决策不冒然扩 scope。

| 字段 | 冻结值 | 来源 |
|---|---|---|
| profile_id | `P-COMMITTED-windows-amd64-py314` | 本卡冻结 |
| os / arch / libc | windows / x86_64 / msvc-runtime | 实测 |
| python_version / abi | 3.14.3 / `cpython-314`（MSC v.1944, AMD64） | 实测 `C:/Python314/python.exe` |
| interpreter_path | `C:\Python314\python.exe` | BUILD_ENV §2 + 实测 |
| rust_toolchain | 1.93.1（01f6ddf75 2026-02-11），cargo 1.93.1 | 实测（锁定具体版本，解决 DP-06） |
| rust_target | 宿主默认（x86_64-pc-windows-msvc） | 无 rust-toolchain 文件，宿主默认 |
| c_compiler_linker | MSVC link.exe 14.44.35207（HostX64\x64） | 实测（见 §4 遮蔽修复） |
| dependencies_sdk | Windows SDK 10.0.26100.0（ucrt/um/shared + x64 lib）；MSVC 14.44.35207 | 实测 |
| source_revision | `c053763a779f7f872daec1cb0521433b65a7ba1a`（HEAD） | `git rev-parse HEAD` |
| external_tools | rg 缺失（本卡不依赖；TP-01 静态生成器的前置，另卡处理）；tokenslim 包装器本机不使用（见下） | 实测 |
| transport | http / named_pipe（本机 Windows profile 不含 unix_socket） | TESTING_PLAN §5 |

tokenslim 决策（DP-07/DP-08）：本机 `.tokenslim-context` 不存在，BUILD_ENV §1 所述「误检测为 Node/npm」在本机已不成立；本卡构建命令**不套 `tokenslim run` 前缀**，直接执行 cargo / python。CI 是否部署 tokenslim 仍由后续 CI profile 决策，本卡不涉及。

未决、但不阻塞本卡：DP-04（原生 Rust CLI 是否发布——TP-06 只构建 daemon + 扩展，不含原生 CLI 发布口径）、DP-05（LLM 是否在发布范围）。

**边界声明**：本 profile 冻结在 TP-06 自己的 allowed 路径内。`docs/testing/planning/assets/profiles.json` 是 TP-02 的 allowed 文件，本卡不修改；其 4 个 candidate 仍为 UNDECIDED、DP-01/DP-03 仍为 DECISION_REQUIRED，与本卡不冲突——本卡是「本机可执行 profile」的冻结，不等于发布承诺矩阵的最终裁定。若需把该决策同步进 profiles.json，由 Planner 另走 TP-02 文档修订。

## 2. run 根与批准父目录

```
run_root = C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-06/
批准父目录 = C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/
owner 标记 = acceptance-builder（TP-06）
run_id     = run-20261012-1
```

子目录用途（全部在批准父目录内，不与生产/源仓/其他 run 相交）：

| 路径 | 用途 |
|---|---|
| `src/` | 批准源快照副本（构建 cwd 来源） |
| `src_snapshot.tar` | 快照 tar 留档（溯源） |
| `env/build_env.py` | MSVC 环境构建器（vcvars + SDK 补齐） |
| `env/env.json` | 冻结的构建环境（36660 bytes） |
| `run_build.py` | daemon 构建驱动 |
| `reports/build-daemon.log` | daemon 构建完整日志 |
| `reports/build-extension.log` | 扩展构建完整日志 |
| `reports/import-test.log` | 扩展 import 测试输出 |
| `artifacts/` | run 内候选暂存（复制到仓库 `tests/acceptance/artifacts/` 前的中转） |

禁止：缓存/venv/target 写回共享源仓（`C:/git_work/callwarden/rust_ext/target`）；以扩展构建冒充 daemon；联网下载依赖（用 `--offline`，全部来自本机 cargo 注册表缓存）。

## 3. 源快照溯源

- 命令：`git archive --format=tar HEAD > src_snapshot.tar`（cwd=`C:/git_work/callwarden`）
- 解压：Python `tarfile.extractall('src')`
- HEAD = `c053763a779f7f872daec1cb0521433b65a7ba1a`；成员 2839；tar 47216640 bytes
- 只含 tracked 文件，不含 `.backups/`、`.bak_v5/`（untracked 备份，TP-06 共同 excluded）
- 快照内 `rust_ext/target` 初始为空（gitignored），故本卡为**冷构建**；热缓存（共享仓 36 GB target）不拷入 run 根，避免 36 GB 复制并保持候选来源干净

## 4. MSVC 环境构建（含遮蔽与 reg.exe 拦截修复）

两个环境陷阱及解法（均为本卡实测结论）：

1. **GNU link.exe 遮蔽**：git-bash `PATH` 中 `/usr/bin/link.exe`（coreutils）排在 MSVC link 之前，cargo 报 `link: extra operand 'kernel32.lib'`。解法：用 vcvars64.bat 注入的 `PATH`（MSVC `HostX64\x64` 排首位），`where link.exe` 首命中即 MSVC 14.44.35207。**不剥 `/usr/bin`**（会连带失效 coreutils），靠 vcvars PATH 前缀取胜。
2. **reg.exe 被沙箱拦截 → SDK 丢失**：vcvars64.bat 内部调 reg.exe 探测 Windows SDK，沙箱拦截后 `WindowsSDKVersion` 为空、INCLUDE/LIB 缺 ucrt/um，cargo 报 `LNK1181: cannot open input file 'kernel32.lib'`。解法：`env/build_env.py` 手动追加 SDK 10.0.26100.0 的 `Include/{shared,ucrt,um}` 与 `Lib/{ucrt,um}/x64`，并设 `WindowsSdkDir/WindowsSDKVersion/WindowsSDKLibVersion/UCRTVersion`。
3. **编码**：vcvars 输出含中文系统页字节（0xb2），Python text 模式 UTF-8 解码致 reader thread 崩溃。解法：`capture_output=True` 字节模式 + `decode('mbcs', errors='replace')`。

冻结命令：

```
C:/Python314/python.exe env/build_env.py dump        # 生成 env/env.json（rc=0 期望）
```

env.json 关键项（实测）：INCLUDE 6 条、LIB 4 条、`WindowsSDKVersion=10.0.26100.0\`、`UCRTVersion=10.0.26100.0`、`VCToolsVersion=14.44.35207`、`VSCMD_ARG_TGT_ARCH=x64`。daemon 构建另需 `PYO3_CONFIG_FILE=%TEMP%/pyo3_config.txt`（`version=3.14 / lib_name=python314 / shared=true / lib_dir=C:\Python314\libs`），使 `--no-default-features` 关闭 extension-module 后 PyO3 链接 libpython。

## 5. 冻结命令链（派工口径）

每行列：序号 / 命令 / cwd / env / expected rc / 报告。全部在 run 根内执行。

### 5.1 daemon 候选

```
# B1 环境构建
C:/Python314/python.exe env/build_env.py dump
  cwd: <run_root>
  env: 继承
  rc : 0
  报告: env/env.json（存在 + 关键项校验）

# B2 daemon 构建（--no-default-features 关 extension-module，PyO3 链 libpython）
cargo build --release --manifest-path <run_root>/src/rust_ext/Cargo.toml --no-default-features --bin cw-daemon --offline
  cwd: <run_root>/src/rust_ext
  env: env.json + PYO3_CONFIG_FILE=%TEMP%/pyo3_config.txt
  rc : 0
  报告: reports/build-daemon.log（stdout+stderr 合并）
  驱动: C:/Python314/python.exe run_build.py（封装上式并计时）
  产物: <run_root>/src/rust_ext/target/release/cw-daemon.exe
```

不得以 `cargo build --lib`（扩展）或 `build.py --rust` 的产物冒充 daemon：feature 集不同（extension-module 开 vs 关），PyO3 链接模式不同。

### 5.2 Python 扩展候选

```
# B3 扩展构建（默认 feature = extension-module；cargo build --lib --release）
C:/Python314/python.exe release/build.py --rust
  cwd: <run_root>/src
  env: env.json + PYO3_CONFIG_FILE=%TEMP%/pyo3_config.txt
  rc : 0
  报告: reports/build-extension.log
  内部: version_sync.py 版本校验 → cargo build --lib --release（cwd=src/rust_ext）
        → 复制 src/rust_ext/target/release/callwarden_core.dll → src/callwarden_core.pyd
  产物: <run_root>/src/callwarden_core.pyd
```

注：`build.py` 内 `run()` 不捕获输出，直接继承父进程 stdout/stderr，故驱动脚本须自行重定向到报告。

### 5.3 import 与 ABI 溯源

```
# B4 扩展 import + ABI 暴露
C:/Python314/python.exe -c "import callwarden_core; print(callwarden_core.__file__); print(callwarden_core.__doc__ or '<no doc>')"
  cwd: <run_root>/src
  env: 继承（不注入 MSVC；import 不需链接器）
  rc : 0
  报告: reports/import-test.log
  期望: __file__ 落在 <run_root>/src/callwarden_core.pyd（证明 import 来源即本候选，非 site-packages）
```

### 5.4 散列记录

```
# B5 候选散列（sha256）
C:/Python314/python.exe - <<EOF
对 src/rust_ext/target/release/cw-daemon.exe、src/callwarden_core.pyd、
以及 src/rust_ext/target/release/callwarden_core.dll（若存在）算 sha256 + size，
连同 source_revision、rustc/python/link 版本写入 artifacts/build_results.json
EOF
  cwd: <run_root>
  rc : 0
  报告: artifacts/build_results.json
```

## 6. 产出物与仓库落地

run 内候选 → 仓库 `tests/acceptance/artifacts/`（本卡唯一 allowed 写路径）：

| 仓库文件 | 内容 |
|---|---|
| `tests/acceptance/artifacts/BUILD_CONTRACT.md` | 本文（冻结合同） |
| `tests/acceptance/artifacts/build_results.json` | 候选散列、版本、路径、rc、耗时（证据 manifest） |

二进制本体**不**入库（体积 + 不可审计），只入散列与溯源字段；消费方（TP-07+）从 run 根复制或按散列自行重建。`.backups/`、`.bak_v5/` 永不提交。

## 7. 负向义务（违反即 BLOCKED）

- 以扩展构建（`--lib` / `build.py --rust`）冒充 daemon 候选 → 拒绝
- target / 缓存 / venv 写回共享源仓 → 拒绝
- 构建联网下载依赖（未带 `--offline` 或 `--offline` 失败后未报告就改联网） → 拒绝
- `where link.exe` 首命中非 MSVC 却继续构建 → 拒绝
- env.json 缺 SDK 路径（`WindowsSDKVersion` 为空）却继续构建 → 拒绝
- import 命中的 `callwarden_core` 不来自本候选路径 → 拒绝
- 散列缺失 / rc 非 0 / 报告缺失 → 拒绝
- 修改 `docs/testing/planning/assets/profiles.json`（TP-02 allowed）或其他卡文件 → 拒绝

## 8. 上游 finding 登记（溯源，非本卡修复项）

**F-TP02-01（test-contract finding，非 daemon/client P-FIX）**：`docs/testing/planning/assets/validate_assets.py` L279-297 以「corpus revision == 当前 HEAD」为判据，产生自引用悖论——任何含 corpus 修改的提交都推进 HEAD，使刚写入的 revision 立刻过期；唯一 PASS 态是未提交工作树。ASSET_CONTRACT L190-191 的既定工作方式是「冻结后若 HEAD 推进，须重发 snapshot.publish 并重跑验收」，故该行为是**合同设计语义**而非实现 bug；但它使「已提交且 PASS」状态不可达，属合同级可用性缺陷。
- source revision：`c053763`
- 受阻任务：TP-02 self-check 的「已提交 PASS」态（TP-02 任务已 closed，产出可用，判定非阻塞）
- 能力缺口：validate_assets.py 缺少「已冻结基线的版本化锚定」（如显式 frozen_at_revision 字段 + 散列绑定，替代 HEAD 独断）
- 处置：本卡不修（属 TP-02 allowed 文件）；登记在此供 Planner 决策是否另走 TP-02 文档修订。按 TASK_BREAKDOWN §6，此项不是 daemon/client 生产修复，**不立 P-FIX**。

**F-GEN-01**：本机 `rg` 缺失。BUILD_ENV §1 明确「静态文档生成器文件枚举以 rg 为明确前置」。本卡（构建）不依赖 rg；TP-01 生成器执行前须冻结 rg 版本与可执行路径。

## 9. 回滚

- 只清理 run 根（有本 run 归属证据且仍在批准父目录内）：`src/`、`src_snapshot.tar`、`reports/`、`artifacts/`、`env/env.json`
- 不 reset 共享工作树、不删旧证据、不改生产数据
- 若构建失败：保留 reports/ 全部日志作为受阻证据，`build_results.json` 记 rc 与失败阶段，不伪造成功

## 10. 状态

| 项 | 状态 |
|---|---|
| 合同冻结 | r1（本文） |
| profile 决策 | 已冻结（§1） |
| 环境构建（B1） | DONE rc=0（env.json 36660 bytes，6 INCLUDE / 4 LIB，SDK 10.0.26100.0） |
| daemon 构建（B2） | **DONE rc=0，729.2s**；cw-daemon.exe sha256 `6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d`，46621184 bytes，`cw_daemon 0.3.23` |
| 扩展构建（B3） | **DONE rc=0，263.7s**；callwarden_core.pyd sha256 `ea65a1c1392570ee815b0d2c504dee774820e634e19162b81e9e4b88f7127a2f`，36686336 bytes |
| import 溯源（B4） | **DONE rc=0**，`loaded_from_candidate=true`（import 命中 run 根候选，非 site-packages），PY 3.14.3 |
| 散列记录（B5） | DONE，`build_results.json`（dll 与 pyd 散列一致，证 copy2 语义） |
| 仓库落地 | DONE：本文 + build_results.json → `tests/acceptance/artifacts/`；证据副本 → `deliverables/testing/TP-06/run-20261012-1/` |
| 隔离验证 | DONE：共享仓 `rust_ext/target` 未被污染（其 cw-daemon.exe mtime 2026-10-10 09:42，size 46619648 ≠ run 根 46621184），target 全留在 run 根内 |

**结论**：TP-06 两个可追溯候选（daemon + Python 扩展）均构建成功，来源、feature 集、Python ABI、工具链与散列全链可复算，负向义务（不冒充、不回写、不联网）逐项满足。下游 TP-07 可从 run 根只读复制候选。
