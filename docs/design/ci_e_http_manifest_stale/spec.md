# Spec: 主 CI `E_HTTP_MANIFEST_STALE` 根因修复 + acceptance-gates 基线分支对齐

状态：待评审（未获得批准前不落地任何代码改动）

## 1. 目标

1. 消除主 CI（`.github/workflows/ci.yml`，Linux ubuntu-latest × Python 3.10–3.14）中随机出现的
   `E_HTTP_MANIFEST_STALE`：`manifest='.../rust_ext/target/debug/cw-daemon'`、
   `pid='.../rust_ext/target/debug/cw-daemon (deleted)'`。
2. 让 `.github/workflows/acceptance-gates.yml` 的 nightly-full / release-artifact 两个 job 的
   Gate 1/2 真正拿到基线比对结果（当前 `baseline_available: false`）。

## 2. 根因（已实证）

### 2.1 失败指纹

- 实证 run：`gh run view 40110299499 --log-failed`（`c:\git_work\callwarden\reports` 外的 CI 日志）。
- traceback 固定为：`tests/_w3_harness.py:743` → `server/daemon_autostart.py:1122`
  `resolve_http_endpoint_and_manifest` → `validate_http_manifest`（`daemon_autostart.py:1057` 附近）。
- 被判 stale 的 PID 实际存活且 daemon 已 ready（stderr 尾部有 `HTTP MVP pre-bound` /
  `ready, waiting for connections`），随后才被 fixture `kill()`。
- 每个 Python 版本命中的模块不同（3.10 task_plan_template、3.11 get_summary、
  3.12 get_symbol_history、3.13 validate_revision_dependencies、3.14 hotspot_evolution），
  各 job 仅 3–4 个 error、其余 8166+ passed → 属于跨模块共享的运行时竞态，不是某个模块的缺陷。

### 2.2 因果链

1. `ci.yml` 的 “Build cw-daemon binary” 只用一种 feature 集构建一次：
   `cargo build --manifest-path rust_ext/Cargo.toml --no-default-features --features sqlite-bundled --bin cw-daemon`
   → 产物 `rust_ext/target/debug/cw-daemon`。
2. 大量 live-daemon 用例（`tests/_w3_harness.find_daemon_binary` 的 debug 首选候选）从**同一路径**
   spawn 隔离 daemon，并在 manifest 里写入 `daemon_executable` 为该路径。
3. Linux-only 用例 `tests/test_w2_3_query_uds_e2e.py::_fresh_daemon_binary`（`real_rust_daemon`
   fixture 每个测试调用一次；`pytestmark` 仅 `sys.platform != "linux"` skip）在测试**运行期**执行：
   `cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
   —— **缺少 `--features sqlite-bundled`**。
4. 与第 1 步 feature 集不同（`rust_ext/Cargo.toml` 的 `default = ["extension-module","sqlite-bundled"]`，
   此处 `--no-default-features` 后未补 bundled）→ cargo 指纹失效 → 重新链接 →
   以新 inode 覆盖 `rust_ext/target/debug/cw-daemon`，旧 inode 被 unlink。
5. 此前已 spawn 的 daemon 仍持有旧 inode；Linux `/proc/<pid>/exe` 返回 `".../cw-daemon (deleted)"`
   （`daemon_autostart.py::_pid_executable` 用 `os.readlink(f"/proc/{pid}/exe")`）。
6. 其他 xdist worker 的 harness 第二次走 `resolve_http_endpoint_and_manifest` →
   `validate_http_manifest` 比较 manifest 的 `daemon_executable` 与 `_pid_executable(pid)` →
   不一致 → 抛 `E_HTTP_MANIFEST_STALE`。
7. 失败模块随版本漂移，因为取决于 xdist 交错：只有「重建发生前已启动、且重建后仍在运行」的 daemon 会中招。

### 2.3 基线分支不一致

- `.github/workflows/acceptance-gates.yml`：
  - PR job（107–114 行）用 `--baseline-ref origin/${{ github.event.pull_request.base.ref }}`（正确）。
  - nightly-full（222–229 行）与 release-artifact（314–321 行）硬编码 `--baseline-ref origin/main`。
- 仓库默认分支是 `master` → `git show origin/main:<manifest>` 失败 →
  `cicd/check_selector_manifest.py` 置 `baseline_available = False`，`removed_vs_baseline` /
  `added_vs_baseline` 恒空。该校验按注释是「可发现性，非硬阻断」，所以 gate 仍 `ok=true`，
  但**基线增删比对实际从未执行**，anti-tamper 的第二层可发现性形同空转。
- 三个 job 的 checkout 都是 `fetch-depth: 0`，`origin/master` 可用，改 ref 即可生效。

## 3. 变更方案（决策已定）

### 3.1 修复 A（根因，必做）：`tests/test_w2_3_query_uds_e2e.py::_fresh_daemon_binary`

- 文件：`tests/test_w2_3_query_uds_e2e.py`
- 位置：`_fresh_daemon_binary()`（35–79 行）里的 `cargo build` 参数。
- 改动：把
  `[cargo, "build", "--no-default-features", "--manifest-path", ...]`
  改为
  `[cargo, "build", "--no-default-features", "--features", "sqlite-bundled", "--manifest-path", ...]`。
- 理由：与 `ci.yml`、`acceptance-gates.yml` 的 daemon 构建入口完全同参 → cargo 指纹命中，
  只打印 `Finished`，不重新链接、不替换 inode，从源头消除 stale。
  同时与 `rust_ext/Cargo.toml` 注释「daemon 的构建入口统一 `--features sqlite-bundled`」一致。
- 不改 `CW_DAEMON_BIN` 覆盖分支与 `CARGO_TARGET_DIR` 透传逻辑。

### 3.2 修复 B（纵深防御，建议同批）：`tests/_w3_harness.py::spawn_isolated_daemon`

- 文件：`tests/_w3_harness.py`
- 改动：spawn 前把选中的 daemon 二进制**复制**（`shutil.copy2`，副本按
  `<data_root>/bin/cw-daemon` 命名）到本实例私有目录，用副本路径 spawn；
  manifest 中记录的 `daemon_executable` 也随之变为私有副本。
- 注意：只能用**复制**，不能用硬链接——硬链接共享 inode，`cargo` 替换共享路径后同样出现
  `(deleted)`，无法防护。
- 理由：即使将来任何测试/工具再次覆盖 `rust_ext/target/debug/cw-daemon`，已运行 daemon 的
  exe 路径仍指向私有副本，`validate_http_manifest` 永不误判；同时让每个隔离 daemon 的
  可执行文件指纹独立可审计。

### 3.3 修复 C（不做）：放宽 `validate_http_manifest`

- 不对 `" (deleted)"` 后缀做特殊放行。语义上它真实表示「正在运行的可执行文件已被替换」，
  放过会掩盖真实的二进制漂移；修复应放在测试侧隔离，而非削弱生产校验。
- 仅在错误消息中保留现有原文（已足够定位）。本 spec 不修改 `server/daemon_autostart.py`。

### 3.4 修复 D（基线对齐，必做）：`acceptance-gates.yml`

- 文件：`.github/workflows/acceptance-gates.yml`
- 第 227 行（nightly-full）与第 319 行（release-artifact）：
  `--baseline-ref origin/main` → `--baseline-ref origin/${{ github.event.repository.default_branch }}`。
- 理由：与 PR job 语义一致；默认分支改名时自动跟随；`fetch-depth: 0` 已保证 ref 可达。
- 不改 `cicd/check_selector_manifest.py` 语义（仍为非硬阻断的可发现性字段）。

## 4. 明确不做

- 不修改 `server/daemon_autostart.py` 的校验逻辑。
- 不调整 `_w3_harness.find_daemon_binary` 的候选优先级。
- 不把 W2.3 二进制构建改成 skip（属 MASKING）。
- 不引入 known-red 豁免。
- 不改 `ci.yml` 的构建步骤（当前唯一构建入口本身正确）。

## 5. 验收标准

1. `ci.yml` 在 master 上连续 2 次 run，Python 3.10–3.14 五个 job 全绿，
   日志中 `E_HTTP_MANIFEST_STALE` 出现次数为 0。
2. 上述 run 中 `test_w2_3_query_uds_e2e.py` 仍真实执行并通过（未被 skip），
   且其构建输出为 `Finished`（无 relink）或仅在私有副本路径产生替换。
3. `acceptance-gates.yml` `profile=full` 的 nightly-full 全绿，
   `reports/manifest_verdict.json` 中 `baseline_ref == "origin/master"`、
   `baseline_available == true`，`removed_vs_baseline` / `added_vs_baseline` 字段为真实列表。
4. release-artifact job 的 Gate 1/2 同样 `baseline_available == true`。
5. 无新增失败、无 skip 数上升（skip 率对比修复前 run）。

## 6. 风险

- 修复 A 依赖「所有前置构建入口参数一致」。若将来有别的测试在同一 target 目录以不同 feature 集
  构建 daemon，仍可能重现——这正是保留修复 B 作为纵深防御的原因。
- 修复 B 每次 spawn 多一次二进制复制（数十 MB 级），耗时远小于单次 daemon 启动，
  且仅影响 live-daemon 用例。
- 基线对齐会让 Gate 1/2 首次输出真实的 removed/added 列表：若清单与 master 基线确有差异，
  会首次暴露为可发现性字段（非阻断）；若需硬阻断，应另立任务，不在本 spec 范围。
