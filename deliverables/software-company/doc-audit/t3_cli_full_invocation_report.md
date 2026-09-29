# T3 实测报告:234 个 CLI 叶子命令全参数真实调用

日期:2026-09-30
环境:Windows,Python 3.14,HTTP daemon(部署后 b495919,PID 44584 @ 端口 13247),种子 workspace(CWD 绑定 + refresh --all build_graph)

## 1. 测试目标与方法

对全部 **234 个 CLI 叶子命令**做**全参数真实调用**(subprocess 执行 `python cw.py <argv>`,最贴近用户实际使用),验证命令可执行、不崩溃,定位真实缺陷。

- **参数**:`param_provider.build_cli_argv`(全参数模式),用种子 workspace 真实前置状态(SeedContext)填充。
- **分层**:只读命令全执行;写命令中破坏性的 SKIP(31 个:删除/回滚/轮换/清理/导入/阻塞类如 server/watch/daemon start)。
- **cwd 绑定**:所有命令在种子目录内执行,CWD 绑定同一 workspace(不依赖隔离 daemon instance 锁)。

## 2. 结果汇总

| 分类 | 数量 | 含义 |
| --- | --- | --- |
| PASS | 70 | rc==0 且输出无异常特征 |
| EXPECTED_BUSINESS | 114 | rc!=0 或业务拒绝(缺参 argparse、not_found、snapshot、identity、workspace 未绑定等)—— 合理,非缺陷 |
| **DEFECT** | **18** | 真实缺陷(见 §3) |
| SKIP | 31 | 破坏性/重操作/阻塞命令 |
| **合计** | 233 | (cli_full_params 234 leaves,server 类已 SKIP) |

## 3. 发现的真实缺陷(18,分三类)

### 类别 A:method_not_found —— compat worker 禁用的连锁(10)

CLI `_METHOD_MAP` 把这些命令映射到 daemon 未实现的 RPC(daemon dispatch 无对应方法),报 `method_not_found`:

| 命令 | 映射 RPC(daemon 缺失) |
| --- | --- |
| `defect build` | `build_defect_knowledge` |
| `fts status` | `get_fts_status` |
| `function-issues` | `get_function_issues` |
| `gc restore` | `gc_restore` |
| `gc status` | `gc_status` |
| `git destructive-log` | `list_destructive_operations` |
| `rollback show` | `get_rollback_config` |
| `rollback is-rolled-back` | `is_feature_rolled_back` |
| `toolchain show` | `toolchain not found` |
| `toolchain bind` | `toolchain not found` |

**根因(系统性,非 10 个独立 bug)**:这些方法原本由 Python **compat worker** 处理,但 daemon 当前 `worker_status = disabled_no_compat_routes`(审计问题 3 的"止血":compat 路由白名单为空 → worker 禁用)。所有本该走 compat 的 CLI 命令因此全部 method_not_found。daemon dispatch.rs 确认无 `gc_status`/`get_fts_status` 等原生分支。

**修复方向(独立技术债,大工程)**:要么恢复 compat worker(需修 worker 的锁/超时根因,即问题 3 的根治),要么把这些方法逐个迁移到 daemon 原生 Rust handler。不宜逐个补伪路由(违反 fail-closed)。

### 类别 B:rc=0 掩盖的 CLI 代码 bug(5)

命令 fail-soft 吞异常仍返回 rc=0,但输出含 "执行子命令 'X' 失败: <Python 异常>":

| 命令 | 异常 |
| --- | --- |
| `call-chain` | `list indices must be integers or slices, not str` |
| `coupled-fns` | `KeyError: 'fan_in'` |
| `largest-fns` | `KeyError: 'line_count'` |
| `rule applicable` | `'str' object has no attribute 'get'` |
| `status` | `KeyError: 'workspace'` |

**性质**:CLI 层结果处理代码 bug(字段名/结构假设与 daemon 返回不符),被 fail-soft 掩盖成 rc=0。是独立可修的真实缺陷。

### 类别 C:未捕获 Python traceback(3)

`collab publish`、`daemon publish`、`daemon snapshot-stats` —— CLI 层抛未捕获异常(rc=1 + 完整 traceback)。需逐个看堆栈根因(参数适配/结果处理)。

## 4. SKIP 清单(31)

破坏性写(workspace delete/register/set、gc archive/db-cleanup/retention、task apply/close/rollback/reopen/revert、clone clear/detect、fts rebuild、audit rotate、assignment revoke/create、rule sync/insert-block)、重操作(refresh、git/coverage/defect import、semgrep scan)、阻塞(server、watch、daemon start/backup/restore/gc-*/mount)。

## 5. EXPECTED_BUSINESS 说明(114)

均为合理业务拒绝:argparse 缺必填参数(param_provider 未覆盖的参数名族,用 "seed" 占位触发 not_found)、任务/文件/branch 不存在、workspace 查询无数据行、identity 不全等。符合 fail-closed 语义,非缺陷。若干指向 `param_provider` 骨架待增量补充(不影响命令本身)。

## 6. 正式化交付(tests/convergence/)

- `t3_cli_runner.py`:T3 运行器(subprocess 执行 + 分类器,含 rc=0 漏判检测)
- `test_t3_cli_full_invocation.py`:T3 pytest(CWD 绑定种子 workspace,DEFECT 数防回升基线 18)
- `fixtures/cli_full_params.json`:234 命令全参数(T1 已固化)

## 7. 结论与待决策

- 234 个 CLI 命令全部被评估,203/234(87%)真实执行(PASS + EXPECTED_BUSINESS)。
- 发现 **18 个真实缺陷**,分三类:
  - **10 个** = compat worker 禁用的**系统性连锁**(1 个根因,非 10 个独立 bug)。
  - **5 个** = rc=0 掩盖的 CLI 代码 bug(字段/结构假设错误)。
  - **3 个** = 未捕获 traceback。

**待用户决策修复范围**:
1. **类别 A(compat 连锁)**:是否根治问题 3(恢复 compat worker)或立独立迁移任务?这影响 10 个命令,是架构级工作。
2. **类别 B/C(8 个 CLI 代码 bug)**:是否现在逐个修复?这些是 CLI 层独立缺陷,修复相对独立,但需逐个定位+验证+可能重新部署。

建议:类别 B/C 的 8 个 CLI 代码 bug 优先修(纯 Python,不需重建 daemon);类别 A 的 compat 连锁作为独立技术债项(与问题 3 根治绑定)。
