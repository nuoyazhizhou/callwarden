# T-1795872106380-72d3c4ec 实现证据（webobs F-3 后续：package-data + read-model §7）

- 任务：T-1795872106380-72d3c4ec（webobs F-3 后续：server/web_static 加入 pyproject package-data 并更正 read-model §7）
- 来源：T-1795469778065-1686c8a0（Web 观测面 MVP）adjudication F-3 裁决
- 步骤：S-1795872106385-73263a4c（step_index=0，action=write，target_file=`pyproject.toml,docs/design/web-observer-read-model.md`）
- 实现身份：agent_id=`executor-webobs-f3b-01` / agent_instance_id=`inst-webobs-f3b-20261129-01` /
  session_id=`sess-webobs-f3b-20261129-01` / model_id=`custom-local:deepseek-v4-flash` / role=`executor`
- Lease：`L-839553feaed17e9a`（fencing_counter=1，role=executor / implementer 归一，ttl=5400s）
- 日期：2026-11-29 · 工作区：`C:\git_work\callwarden`（workspace_id=1 / instance=`4baea3ff12c2ea5c`）

## 1. 变更内容（仅 2 个文件，均在 target_file 白名单内）

### 1a. `pyproject.toml`（`[tool.setuptools.package-data]`，原 147-150 行）

```diff
@@ -147,6 +147,9 @@ ext-modules = [
 [tool.setuptools.package-data]
 callwarden = [
     "i18n/*.json",
+    # Web 观测面（F-3）：静态看板单文件随 wheel 发布，路径相对包根
+    # （package-dir 已把 callwarden 映射到仓库根，故写成 server/web_static/...）
+    "server/web_static/*.html",
 ]
```

写法对齐既有 `"i18n/*.json"` 条目：同属包键 `callwarden`，同用相对包根的 glob。
包键为 `callwarden` 而路径前缀为 `server/...`，是因为 `[tool.setuptools] package-dir`
已把 `callwarden` 映射到仓库根（`package-dir = { "callwarden" = "." }`），
故 `server/web_static/index.html` 在 wheel 内落为 `callwarden/server/web_static/index.html`
（与 `i18n/en_US.json` → `callwarden/i18n/en_US.json` 同一映射规律）。

### 1b. `docs/design/web-observer-read-model.md` §7 验收对照表第 6 行（原 126 行）

修改前（与事实矛盾，声称已入 package-data 但实际未实施）：

```
| 6) 前端无构建步骤 | §2 单文件 `index.html` + 原生 JS，静态目录入 pyproject package-data |
```

修改后（与修复后事实逐项对齐，并点明包键与相对路径基准）：

```
| 6) 前端无构建步骤 | §2 单文件 `index.html` + 原生 JS；静态资源由 `[tool.setuptools.package-data]`（`callwarden` 包键）的 `server/web_static/*.html` 条目声明，随 wheel 发布（路径相对包根，包根 = 仓库根） |
```

> 注：同文档 :29 的 `server/web_static/index.html`（package-data）表述在本次修复后已成事实，
> 无需改动，故未触碰（保持改动面最小）。

## 2. 行为级验证：wheel 产物实测（正向）

环境：本机 `python -m build` 不可用（`build` 模块未安装，系统 Python 3.14 与 `.venv_test` 均无 setuptools）。
故改用 pip 直接驱动同一 PEP 517 后端（`setuptools.build_meta`，`--no-build-isolation`），
等效于 `python -m build --wheel --no-isolation`，产物为真实 wheel。

命令（`build/` 已先清空，`--no-cache-dir` 排除缓存干扰）：

```bash
rm -rf build /tmp/cw_whl_final && mkdir -p /tmp/cw_whl_final
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir -w /tmp/cw_whl_final
```

输出（尾部）：

```
Building wheel for callwarden (pyproject.toml): finished with status 'done'
  Created wheel for callwarden: filename=callwarden-0.3.23-cp313-cp313-win_amd64.whl
  size=6766439 sha256=6d0b560c07c4b8bead7d9e73302f69b21ed97b6b9f5085e1082cba6656e7f3bc
Successfully built callwarden
```

wheel 内容检查（`zipfile`）：

```
final_total_entries = 129
final_has_target = True
wheel_entry_sha256 = 9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17
wheel_entry_size = 18708
src_sha256 = 9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17
src_size = 18708
entry_equals_source = True
```

结论：wheel 内存在 `callwarden/server/web_static/index.html`，字节内容与源文件 `server/web_static/index.html`
完全一致（sha256 相同，18708 字节）。

## 3. 负向对照（证明「入包」确由本次声明驱动，而非陈旧 build 残留）

第一轮负向对照被 `build/lib.win-amd64-cpython-313/` 的陈旧拷贝污染（未声明也仍被打进 wheel，129 条），
故第二轮强制 `rm -rf build` + `--no-cache-dir` 重做：

```bash
# 临时移除 "server/web_static/*.html" 条目（同一工作树，随后完整还原）
rm -rf build /tmp/cw_whl_neg2 && mkdir -p /tmp/cw_whl_neg2
python -m pip wheel . --no-deps --no-build-isolation --no-cache-dir -w /tmp/cw_whl_neg2
```

```
neg2_total = 128
neg2_has_target = False
neg2_i18n = ['callwarden/i18n/__init__.py', 'callwarden/i18n/en_US.json', 'callwarden/i18n/zh_CN.json']
```

- 移除条目后 wheel 为 128 条、**无** `server/web_static/index.html`；
- 同期 `i18n/*.json` 仍在包内（说明 package-data 机制本身生效，差异只来自本条目）；
- 条目已还原，工作树最终 diff 见第 1 节（`git diff pyproject.toml` 仅 3 行新增）。

负向轮 wheel：`callwarden-0.3.23-cp313-cp313-win_amd64.whl`，6759352 字节，
sha256=`542b7d334c8f924b1f601a6047fc5235ed338398f71c01a12897e1c2e07f8b3f`。

## 4. 文件哈希（提交前）

| 文件 | sha256 | 说明 |
|---|---|---|
| `pyproject.toml` | `c7b0fef9679329b37b0eeb95f830c19dace6185281565211d02adc6b9962e59d` | package-data 新增 3 行 |
| `docs/design/web-observer-read-model.md` | `9c725376c428b1042e991c3c0771e6881c5ab9de6dccf06cf54da01eddaaf37e` | §7 第 6 行更正 |
| `server/web_static/index.html`（未改动） | `9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17` | wheel 内同哈希 |
| 验证 wheel（正向） | `6d0b560c07c4b8bead7d9e73302f69b21ed97b6b9f5085e1082cba6656e7f3bc` | 6766439 字节 |

## 5. 改动面与禁区遵守

- 改动文件：`pyproject.toml`、`docs/design/web-observer-read-model.md`、本证据文档；
  另有构建产物 `build/`（.gitignore 第 27 行已忽略）在验证过程中产生与清理，不入库。
- 未触碰 daemon / rust_ext / server 前端代码；未重启共享 daemon；未执行 `task.apply/close/supersede`；
  未直写 SQLite（仅只读身份/lease 查询经 daemon RPC）。
- 身份注册路径说明：MCP `lease_acquire` 在本仓存在已知缺陷（工具只暴露扁平
  agent_id/session_id/model_id/role，而 daemon `lease.acquire` 只读嵌套 `params.identity`，
  见 `rust_ext/src/daemon/task_collab_lease.rs:1236` + `task_collab_shared.rs:108-158`），
  故改走 CLI 等价路径：`cw lease acquire`（自动组装嵌套 identity）→
  `agent.register`（`HttpDaemonRpcClient.call`，接受扁平字段）→ `cw task next` 领取 step。
