# T-1795872106380-72d3c4ec 独立复评证据（blind_first_pass）

- 任务：T-1795872106380-72d3c4ec — webobs F-3 后续：server/web_static 加入 pyproject package-data 并更正 read-model §7
- 被审步骤：S-1795872106385-73263a4c（step_index=0，action=write，target_file `pyproject.toml,docs/design/web-observer-read-model.md`）
- 被审交付 commit：`716e0c4a90b132f267fe41a8750d82be0bfe005e`
- 相位：`blind_first_pass`；结论：**pass**（verdict `V-018acaa8450b10d8d3bae626`，task_verdict_events event_id=944）
- 评审身份：agent_id=`reviewer-webobs-f3-01` / agent_instance_id=`inst-webobs-f3-review-20261129-01` /
  session_id=`sess-webobs-f3-review-20261129-01` / model_id=`custom-local:deepseek-v4-flash` / role=`reviewer`
- Review lease：`L-73e447288561e129`（fencing_counter=1，ttl=3600，acquired_at=1795884577）
- 独立性：与 executor（`inst-webobs-f3b-20261129-01` / `sess-webobs-f3b-20261129-01`）实例与会话均不重叠；
  daemon `validate_lease_for_mutation` 通过
- 合同绑定：TC-T-1795872106380-72d3c4ec r1 `sha256:5d2f53f94d10aa27a4eab8eb9ebdc53dc0cbdcdfbd900d7de0feabfdf02db0bb`；
  Role Contract `rcr-T-1795872106380-72d3c4ec-reviewer-r1` r1 `sha256:3e8debc9adca99d95e5e7cb25b53ff50e41fdf18a89a3f943dd353bcfef34f0b`
- snapshot_id（verdict 绑定）：`20a422e1300666c8`（来自 task_events 18576 的 executor handoff）
- view_manifest_hash：`7fbf767ac640d4f2627a09410f909b62df3bc8941ec778115b79f14c51e2652c`（本次 `cw task prompt` bundle 全文字节 sha256）
- 日期：2026-11-29

> 本文件为评审方独立复算记录；所有数字均为评审侧现场重跑所得，不采信 executor 自报值。

## 1. package-data 路径前缀推论核验（结论：正确）

事实核对：

```
$ sed -n 128,156p pyproject.toml
[tool.setuptools]
package-dir = { "callwarden" = "." }
packages = [ "callwarden", ..., "callwarden.server", "callwarden.server.tools" ]

[tool.setuptools.package-data]
callwarden = [
    "i18n/*.json",
    # Web 观测面（F-3）：静态看板单文件随 wheel 发布，路径相对包根
    # （package-dir 已把 callwarden 映射到仓库根，故写成 server/web_static/...）
    "server/web_static/*.html",
]
```

- `package-dir = { "callwarden" = "." }` 把包 `callwarden` 的物理根映射到仓库根，故 `server/web_static` 即
  `callwarden.server.web_static` 的物理目录；`[tool.setuptools.package-data]` 的 pattern 相对**包根**解释。
- 因此 `"server/web_static/*.html"` 与既有 `"i18n/*.json"`（`i18n/en_US.json` → `callwarden/i18n/en_US.json`）
  同属一条映射规律；写成 `callwarden/server/web_static/*.html` 反而会指向不存在的 `./callwarden/server/...`。
- 该推论不止于推理：实测 wheel 内落地路径为 `callwarden/server/web_static/index.html`（见 §2）。

## 2. 正向 wheel 复算（结论：通过）

评审侧命令（先清空陈旧 `build/`，`--no-cache-dir` 关缓存）：

```bash
cd /c/git_work/callwarden
rm -rf build /tmp/cw_rev_pos && mkdir -p /tmp/cw_rev_pos
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir -w /tmp/cw_rev_pos
```

输出（尾部）：

```
Building wheel for callwarden (pyproject.toml): finished with status 'done'
  Created wheel for callwarden: filename=callwarden-0.3.23-cp313-cp313-win_amd64.whl
  size=6766439 sha256=f43a74d0f58bb7cd832947bc5e321006d013e922978fa0f1bc0a032e3f43c0af
```

条目级检查（`zipfile`）：

```
total_entries: 129
has_target: True
entry_sha256: 9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17
entry_size: 18708
src_sha256:   9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17
src_size:     18708
equal: True
i18n_entries: ['callwarden/i18n/__init__.py', 'callwarden/i18n/en_US.json', 'callwarden/i18n/zh_CN.json']
```

与 executor 自报一致项：条目数 129、wheel 字节数 6766439、条目 sha256 与源文件 sha256 完全相同。
不一致项：wheel 整文件 sha256 不同（评审侧 `f43a74d0…`，executor 侧 `6d0b56…`）。同尺寸不同哈希源于
zip 条目时间戳/构建临时路径元数据，属正常非确定性；判定依赖的是条目级内容哈希，结论不受影响。

## 3. 负向对照复算（结论：对照有效）

为避免改动工作树（禁区：不得修改 pyproject.toml），评审侧用 `git archive HEAD` 生成干净副本做负向对照，
仍强制从零构建（副本内无 `build/`，不存在陈旧 `build/lib.win-amd64-cpython-313` 污染）：

```bash
cd /c/git_work/callwarden
rm -rf /tmp/cw_neg_src /tmp/cw_rev_neg && mkdir -p /tmp/cw_neg_src /tmp/cw_rev_neg
git archive HEAD | tar -x -C /tmp/cw_neg_src
# 仅移除条目行（评论行保留，见 §5 观察项）
cd /tmp/cw_neg_src && python - <<'EOF'   # 删除 '    "server/web_static/*.html",'
EOF
cp /c/git_work/callwarden/callwarden_core.pyd /tmp/cw_neg_src/   # 归档不含 gitignore 的预编译扩展
rm -rf /tmp/cw_neg_src/build
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir -w /tmp/cw_rev_neg
```

首次尝试因归档缺 `callwarden_core.pyd` 失败（`error: 预编译二进制不存在: …\cw_neg_src\callwarden_core.pyd`），
补齐二进制后成功。结果：

```
neg_total_entries: 128
neg_has_target: False
neg_web_static_entries: []
neg_i18n_entries: ['callwarden/i18n/__init__.py', 'callwarden/i18n/en_US.json', 'callwarden/i18n/zh_CN.json']
```

结论：去掉本条目后目标文件消失（129→128），同期 `i18n/*.json` 仍在包内，证明「入包」由本次 package-data
声明驱动，而非陈旧 build 残留；executor 披露的陈旧 build 坑真实存在且已被双方各自的干净构建规避。

## 4. read-model 表述核验（结论：与事实一致）

- `docs/design/web-observer-read-model.md:126`（§7 第 6 行，修复后）：
  「§2 单文件 `index.html` + 原生 JS；静态资源由 `[tool.setuptools.package-data]`（`callwarden` 包键）的
  `server/web_static/*.html` 条目声明，随 wheel 发布（路径相对包根，包根 = 仓库根）」
  → 与 §1/§2 实测逐项吻合（键名、pattern、相对包根、随 wheel 发布均为事实）。
- 同文档 `:29`「`server/web_static/index.html`（package-data）」：修复前为**未兑现的声称**，修复后已成为事实，
  executor 判「已成事实、无需改动」成立，不改也有利于最小改动面。
- 全库 `grep -rn "package-data" docs/` 仅剩上述两处（另有 `docs/reports/web-observer-mvp-executor-evidence-20261124.md:47`
  的历史陈述，属既往执行记录，不在本卡 target_file 白名单内，不构成本卡缺陷）。

## 5. 改动面与禁区核验（结论：无越界）

```
$ git show --name-only --format="%H" 716e0c4
716e0c4a90b132f267fe41a8750d82be0bfe005e
docs/design/web-observer-read-model.md
evidence/T-1795872106380-72d3c4ec/evidence.md
pyproject.toml

$ git show --numstat --format="" 716e0c4
1	1	docs/design/web-observer-read-model.md
129	0	evidence/T-1795872106380-72d3c4ec/evidence.md
3	0	pyproject.toml
```

- 恰好 3 文件：2 个白名单源码/文档文件 + 证据文档；`git show 716e0c4 | grep -c snapshot_state` = 0，
  确认未把并行线在途的 `rust_ext/src/daemon/snapshot_state.rs` 混入提交。
- 工作树当前 `git diff --name-only HEAD` 仅 `rust_ext/src/daemon/snapshot_state.rs`（并行线在途，与本卡无关）；
  `pyproject.toml` 与 `docs/design/web-observer-read-model.md` 工作树哈希 == HEAD blob 哈希（未被事后篡改）：

```
c7b0fef9679329b37b0eeb95f830c19dace6185281565211d02adc6b9962e59d  pyproject.toml
9c725376c428b1042e991c3c0771e6881c5ab9de6dccf06cf54da01eddaaf37e  docs/design/web-observer-read-model.md
```

## 6. 证据完整性核验（结论：充分）

```
$ sha256sum evidence/T-1795872106380-72d3c4ec/evidence.md
b05527863181f88bf2b708107599310662ff3d8355ac1151e9df94583cb29369  evidence.md   # 与任务卡给定值一致
```

- evidence.md §4 记录的两文件哈希与本次实测完全相同（见 §5 代码块），说明证据文档描述的就是被提交内容。
- evidence.md §2/§3 给出了可复制的命令（含 `rm -rf build`、`--no-cache-dir`）与可判定的输出键值，
  并主动披露了第一轮负向对照被陈旧 build 污染的事实与纠正过程——属自证式披露，可信度高。

## 7. 逐项结论

| # | 评审重点 | 结论 | 依据 |
|---|---|---|---|
| 1 | package-data 路径前缀正确性 | pass | §1：`package-dir` 映射推导 + wheel 内实测落地 `callwarden/server/web_static/index.html` |
| 2 | wheel 验证可信（正/负向） | pass | §2 正向 129 条含目标且条目 sha256 == 源；§3 干净副本负向 128 条缺失、i18n 仍在 |
| 3 | read-model §7 表述与事实一致；:29 是否需同步 | pass | §4：:126 逐项对齐；:29 修复后已成事实，无需改动 |
| 4 | scope 是否越界 | pass | §5：提交恰 3 文件、无 `snapshot_state.rs`；工作树两文件 == HEAD |
| 5 | 证据完整性 | pass | §6：evidence.md sha256 与给定值一致，内部哈希与实测一致，命令可复现 |

## 8. 观察项（不影响 pass，供 adjudicator 归档参考）

1. `evidence.md` §1 标题写「仅 2 个文件」，指的是源码/文档改动（2 个），提交实际含第 3 个证据文档；
   表述略可精炼，但正文 §1/§5 已明确列出本证据文档，无实质误导。
2. wheel 整文件 sha256 不可字节级复现（zip 时间戳/临时路径元数据），跨会话比对请以条目级哈希为准；
   evidence.md 记录的是单次构建的整包哈希，本身无错。
3. 本次负向对照在 `git archive` 副本中删除了 `"server/web_static/*.html"` 行但保留其上方两行注释，
   故负向 wheel 内无该条目而注释不影响构建——与 executor 的「移除条目」语义等价。
