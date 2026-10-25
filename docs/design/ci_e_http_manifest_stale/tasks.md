# Tasks: 主 CI `E_HTTP_MANIFEST_STALE` 修复 + acceptance-gates 基线对齐

依据：[spec.md](./spec.md)。获批后方可执行；每项完成即标记。

## T1 修复 W2.3 构建参数（根因）

- 文件：`tests/test_w2_3_query_uds_e2e.py`
- 函数：`_fresh_daemon_binary()`（35–79 行）
- 改动：`cargo build` 参数补 `"--features", "sqlite-bundled"`，与 `ci.yml` /
  `acceptance-gates.yml` 的 daemon 构建入口同参，避免运行期 relink 覆盖共享 inode。
- 保留：`CW_DAEMON_BIN` 覆盖分支、`CARGO_TARGET_DIR` 透传、`--no-default-features`。
- 验证（本地）：Windows 上 `python -m pytest tests/test_w2_3_query_uds_e2e.py --collect-only`
  应全部 skip（Linux-only），确认无语法/参数错误；真实行为由 CI 验证。
- 完成判据：CI run 中该文件真实执行且 `cargo build` 输出 `Finished`（无 relink）。

## T2 隔离 daemon 二进制副本（纵深防御）

- 文件：`tests/_w3_harness.py`
- 函数：`_private_daemon_binary(bin_path)`（新增）+ `spawn_isolated_daemon` 调用它
- 改动：对位于 `rust_ext/target` 下的构建产物，按 `(size, mtime_ns)` 在
  `rust_ext/target/.w3-bin-cache/` 缓存一份副本（保留原扩展名），用副本 spawn；
  manifest 的 `daemon_executable` 自然指向副本。其他路径候选（如 runtime/current）不复制。
- 约束：必须复制，不得硬链接（硬链接共享 inode，无法防护 `(deleted)`）。
- 实证修正：初版按用例复制到 `data_root/bin` → Linux 主 CI /tmp 配额耗尽
  （Errno 122，ci run 40154241136 五个测试 job 全红）；改为构建指纹级去重，
  一次构建只占一份磁盘。
- 验证：`tests/test_mcp_test_impact_selection_http_rpc.py` 本地全绿，
  `rust_ext/target/.w3-bin-cache/` 仅一份副本；CI 中 manifest 路径变为缓存副本。

## T3 基线分支对齐（nightly / release）

- 文件：`.github/workflows/acceptance-gates.yml`
- 位置：第 227 行、第 319 行
- 改动：`--baseline-ref origin/main` → `--baseline-ref origin/${{ github.event.repository.default_branch }}`。
- 验证：`python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/acceptance-gates.yml'))"`
  通过；CI 中两个 job 的 `manifest_verdict.json` 显示 `baseline_ref=origin/master`、
  `baseline_available=true`。

## T4 回归验证（CI 级）

- 前置：T1–T3 已提交并 push 到 master。
- 步骤：
  1. `gh run watch` 等待 `ci.yml` 一次完整 run，确认 3.10–3.14 五个 test job 全绿、
     `E_HTTP_MANIFEST_STALE` 计数为 0、`test_w2_3_query_uds_e2e.py` 未 skip。
  2. `gh workflow run acceptance-gates.yml --ref master -f profile=full`，
     下载 `reports/verdict_full.json` 与 `reports/manifest_verdict.json`，
     核对 `ok=true`、`baseline_available=true`。
  3. 重复第 1 步一次（连续 2 次绿），排除偶发。
- 完成判据：spec §5 的 5 条全部满足，并保留 run id + verdict JSON 作为证据。

## T5 证据归档与汇报

- 记录：两次 ci.yml run id、一次 acceptance-gates run id、各 job 耗时、
  `manifest_verdict.json` 关键字段（`baseline_ref` / `baseline_available` /
  `removed_vs_baseline` / `added_vs_baseline`）。
- 汇报：说明 `E_HTTP_MANIFEST_STALE` 已消除，以及基线比对首次真实生效的结果
  （若出现 removed/added 差异，按「可发现性、非阻断」如实列出，另立硬阻断任务评估）。

## 待批准

用户确认本 spec 与任务拆分后才执行 T1–T5；未批准前不做任何代码/工作流改动。
