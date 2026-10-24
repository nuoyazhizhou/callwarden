# Call Warden 全栈测试与全项目代码审查报告

- **审查对象**：callwarden @ `cf53afe`（origin/master，2026-10-24）
- **执行档**：review（用户显式授权联网，安全维度 SCA 实跑；压测无目标环境未实测）
- **范围**：全 21 类测试覆盖维度 + 全项目代码审查（抽样，范围声明见 §7）
- **编排**：8 位域专家并行审查（单元/集成/系统验收/保障/性能可靠/安全/兼容体验/流程度量）；主理人对 3 项关键论断做独立复核（Gate 1/2 复现实锤、KR-B 触发链源码核对、e2e 运行历史核验）
- **纪律**：全程只读，未修改/提交任何仓库文件；MASKING 判据（改绿后同一缺陷能否再红）贯穿测试质量裁定

---

## 0. 门禁结论：**不通过**

问题不在测试资产不足——21 类中 18 类「已发现」且多处达到高水准（243 工具真实 MCP wire 全量、Python↔Rust 差分契约锁死、A′ 四角色治理环、双 UID 隔离、frozen 产物真安装 E2E、kill-9 恢复链）。真正系统性风险是**门禁失效模式**：多处「设计完备、运行实效为零」——nightly 验收门从未执行（KR-C）、反篡改前置门恒红（Gate 1/2 汇总行缺陷）、主 CI 因 known-red 零隔离恒红 4+5、E2E 退出码只 warning 不阻断、Rust 集成测试只编译不执行。红灯疲劳使门禁丧失信号意义，「存在但从未生效」的门禁比没有门禁更危险（虚假安全感）。

**复评条件**（全部满足后可申请复评）：
1. KR-C 修复：nightly 安装步可过，Gate 1/2 产出真实 verdict.json 证据；
2. Gate 1/2 汇总行缺陷修复，且两个门禁脚本单测入库；
3. known-red 隔离机制落地（`docs/testing/known_red/` 登记表 + quarantine job，带到期日）；
4. KR-A 按裁决 6 条件落地并转绿（MASKING 自检通过）；
5. GAP-SEC-01（写路径遍历）与 GAP-SEC-02（bandit 范围外 75 条）修复；
6. Rust 集成测试（`rust_ext/tests/`）进入 CI 执行。

---

## 1. 21 类三态总表

| # | 测试类型 | 负责域 | 覆盖三态 | 门禁三态 | 关键证据 |
|---|---|---|---|---|---|
| 1 | 单元测试 | 苏雅 | 已发现（充分） | **不通过** | Rust 2095 #[test] + pytest 7307 函数/583 文件；治理写/ABI/属主负向锚点充足；但 KR-A 门禁红 + 1 个真空伪测试 |
| 2 | 测试覆盖率 | 苏雅 | 已发现（配置可信） | 待核实 | ci.yml coverage gate（fail_under=40 强制）；生产域 `pragma: no cover` 0 处；数字本机未实测（无 pytest-cov） |
| 3 | 变异测试 | 苏雅 | **未发现** | **不通过** | 无 mutmut/cargo-mutants 任何痕迹；附完整引入设计（首批范围+CI 集成+MASKING 机械化关系） |
| 4 | 集成测试 | 柯砚 | 已发现（真实充分） | 通过（附 2 条件） | w3 harness 起真 cw-daemon+真 SQLite；94 个 live 文件；Rust daemon_e2e/p1_f_recovery；本地 70+ 用例绿 |
| 5 | 契约测试 | 柯砚 | 已发现（面广） | 通过（附未锁面清单） | D4 Python/Rust 差分（37 用例）；role prompt sha256 golden；route matrix 239/243 工具三向一致 |
| 6 | 接口 API 测试 | 柯砚 | 已发现 | 通过 | 243 工具 wire 全量（DEFECT==0 硬断言）；77 个 http_rpc；状态码/超时/幂等/鉴权逐项覆盖 |
| 7 | 系统/E2E | 白樾 | 已发现（链路真实） | **不通过** | frozen→UDS/pipe→DB→MCP stdio→dpkg/MSI 真链路；但结果不阻断（三 workflow 仅 ::warning::）+ watcher/schema 两处名实不符 + arm64 盲区 |
| 8 | 验收 UAT | 白樾 | 已发现 | 待核实 | acceptance-gates 三门 fail-closed 机械可判（实跑证成）；缺 AC↔selector 追溯矩阵、签收角色、CI 实跑状态 |
| 9 | BDD 规格 | 白樾 | **未发现** | **不通过** | 9 个 .feature 全在 testcode/（第三方靶场语料）；附「不建议引入独立层」反对论证 + 轻量路线设计 |
| 10 | 冒烟测试 | 闻朔 | 已发现（机制完整） | 待核实 | frozen 冒烟 step + 无系统 Python 断言 + Bootstrap Gate + 243 工具/15 语言冻结守卫；但产物 E2E advisory 不阻断 |
| 11 | 回归测试 | 闻朔 | 已发现（套件+治理设计优秀） | **不通过** | 9433 用例/618 文件；TP 冻结 selectors + 双门禁 + revision 前置判定；但 Gate 1/2 恒红、known-red 零隔离、回归集本机不可跑 |
| 12 | 探索性测试 | 闻朔 | **未发现**（外部记录待确认） | 待核实 | 全域扫描无 charter 资产；附 charter 模板 + EXP-01/02/03 首批建议 |
| 13 | 性能测试 | 骆骋 | **未发现** | **不通过**（未实测） | 无压测资产；budgets.json NOT_CALIBRATED；附关键路径/基线/阈值/门禁化完整设计 |
| 14 | 可靠性测试 | 骆骋 | 已发现（部分） | **不通过** | 本机 91 例全绿（Degraded_Mode 22 + kill-9 15 + chaos 34 + 恢复 18+）；但 CI 执行链断裂（Rust 集成不执行 + E2E 近 3 个月无成功） |
| 15 | 安全测试 | 关屿-3 | 已发现（门禁四件套接入） | 待核实 | cargo audit 0 vuln / bandit CI 口径 0 / fuzz 63 过 / 授权与 HTTP 负向实测过；但 pip-audit 超时、semgrep 未实跑、HTTP 安全套件本机 20 ERROR，且 2 个严重缺口（写路径遍历 PoC + bandit 范围外 75 条） |
| 16 | 兼容性测试 | 阮清 | 已发现（核心格真实执行） | 通过（附 3 严重盲区） | Linux×py3.10-3.14 全量、Windows Rust 147+83 例、三平台 frozen E2E、wheel tag 断言；盲区：windows-arm64 零 E2E、Windows Python 全量零执行、MCP wire 无 Windows |
| 17 | 可用性测试 | 阮清 | 已发现（机器代理证据强） | 待核实 | 337 例本机绿（错误契约/帮助 84 命令/i18n 1951 键零差/输出一致性）；缺真人/启发式证据 |
| 18 | 无障碍测试 | 阮清 | 已发现（CLI 类比大面积实现） | **不通过** | console.py 三层降级 + ensure_utf8 + 跨语言 parity；但无 WCAG 目标、无检测、无「管道零 ESC」断言 |
| 19 | QA 流程 | 何序-2 | 已发现（机械化程度高） | 待核实 | A′ 环 Rust 治理单测 172 过；结构化 handoff；TP 双门禁负向实测正确；但 nightly 零证据（KR-C）+ 门禁脚本零单测 |
| 20 | 质量指标 | 何序-2 | 已发现（多工具实跑过） | 待核实 | route matrix 0 漂移、client purity 0 违例、coverage manifest 双真相源；但覆盖率不可解释 + 技术债账本 4 项漏登 |
| 21 | 全项目代码审查 | 何序-2 | 已发现（抽样范围内通过） | 通过（附 10 发现） | 参数化 SQL/事务兜底/finally 释放/豁免全带理由；C-1 阻断（同 Gate 1/2 缺陷）+ C-2 fail-open + migrate 2 项 |

---

## 2. 系统性诊断：三类门禁失效模式

| 模式 | 表现 | 实例 |
|---|---|---|
| **M1 恒红型**（红灯变噪音） | 门禁永远红 ⇒ 人人忽略 | Gate 1/2 汇总行污染（三次独立实证）；known-red 零隔离致主 CI 恒红 4 failed + 5 errors |
| **M2 零执行型**（门禁从未生效） | 有 job 从不跑/跑了不判 | KR-C（nightly 安装即败，Gate 从未执行）；Rust 集成测试 `--no-run` 只编译；E2E 退出码 ::warning::；双 UID 隔离无 workflow 调用点 |
| **M3 名实不符型**（宣称强于实际） | 测试名说是 A 实测是 B | save-to-query 测裸 SQLite INSERT+SELECT；watcher「简化版直接用 cw query」；schema N-1 实为删 DB 重初始化；`test_uid_isolation` body=pass 真空伪测试 |

**共同根因**：门禁与豁免机制自身没有测试（`cicd/` 两个门禁脚本零单测——本轮 C-1 缺陷即因此存活），且豁免/白名单机制无日落与到期机制（RETIRED_LEGACY_FILES「只减不加」是唯一正确范式）。

---

## 3. KR-A / KR-B / KR-C 归因与关闭路线

### KR-A（test_srv_019×4）——归因完成，关闭路线已裁决（何序-2 裁决 1：采纳+6 条件）

- **归因**（git 级证据链，何序-2 逐条核验属实）：83 findings = **79 条**（`server/cas_schema.py` 34 + `daemon_registry.py` 14 + `toolchain_store.py` 31）——db-retire 三提交（64cda51/e93a571/739d17d）把存储层从根 `db/` 搬进 `server/` 进入审计扫描面；生产权威已由 Rust 1:1 接管（`workspace.rs:343`/`cas.rs:130`/`toolchain.rs:188`，`route_matrix.rs:160-163/306-332` status=migrated/stable），Python 影子 daemon 入口 2026-09-09 下线（`cli/daemon_commands.py:24-27`）+ **4 条**（`server/migrate.py`）——活命令 `cw gc db-migrate-single`（`cli/main.py:8056-8058`→`server/cli_admin.py:140`→`server/migrate.py:75`），Rust 无数据迁移等价物。
- **关闭路线**：① 三存储文件经**第二类 db-retire 过渡声明**清零（冻结基线逐条比对守卫，粒度 file:line:handler:kind，基线只减不加；日落条款绑 db-retire 里程碑，逾期守卫红）；② `migrate.py` 以 `live_legacy_authority` 单独关闭（下沉 Rust 或命令退役，**不得白名单/声明豁免**）；③ 证据指针指向 route_matrix.rs 行号——**不得引用 srv019 矩阵文档**（2026-08-29 快照不覆盖这三文件）；④ 断言不放宽（finding_count==0 / passed is True / retired_files_count==14 保持严格，第二类条目报告中单列）；⑤ 机制落地跑绿前 KR-A 保持 known-red。
- **复测（MASKING 自检）**：落地后向 cas_schema.py 注入一行 `sqlite3.connect(...)`，audit 必须再 exit 1、test_srv_019 必须再红——能红才闭环。

### KR-B（test_mcp_get_token_savings×5）——归因完成，双形态区分，主修方案明确

- **CI 形态**（E_HTTP_MANIFEST_STALE）：`tests/test_w2_3_query_uds_e2e.py:51-60` 的 `_fresh_daemon_binary()` 构建缺 `--features sqlite-bundled`（`ci.yml:54` 带）⇒ feature 集不同 → cargo 换 fingerprint 重建并**原子替换** `rust_ext/target/debug/cw-daemon`（新 inode）⇒ 旧 inode 上运行的隔离 daemon `/proc/<pid>/exe` 带 `(deleted)` ⇒ `server/daemon_autostart.py:1053-1061` 的 manifest executable 校验（`config.py:1143-1161` 不剥后缀）fail-closed。主理人已源码级独立确认该触发链。
- **本机形态**（E_HTTP_DAEMON_UNAVAILABLE，端口 9739）：隔离 daemon 被常驻生产 daemon 的 SID 级命名管道顶死（CreateNamedPipe error 5，harness 自记载 `tests/_w3_harness.py:778-779`）。
- **关闭路线**：主修 **B2**——`tests/_w3_harness.py:654-677` Popen 前 `os.link` 硬链接 + copy2 回落，session 级按 sha256 复用（先例：`governance_harness.py:268`），免疫整类「外部重建替换共享二进制」竞态；同步收口 8 个自有 `_spawn_isolated_daemon` harness。辅修 A（`daemon_autostart.py:1055` 剥离 `" (deleted)"`）需契约 Owner 裁决。
- **闭环判据**：CI 连续 3 轮 × Python 3.10–3.14 的 token_savings 0 ERROR 且无新红面。

### KR-C（acceptance-gates nightly ≥5 连挂）——根因钉死，修复明确

- `pyproject.toml:28` `semgrep>=1.50` 在**核心 dependencies**；nightly 跑 `windows-latest` × py3.14 ⇒ semgrep 无 cp314 Windows wheel 且其 setup.py 明拒 Windows（日志：`Semgrep does not support Windows yet`）⇒ 安装步即败 ⇒ **Gate 1/2、Gate 2/2 从未执行，整个 nightly 验收门自建成起零证据产出**。
- **修复**：semgrep 移出核心依赖改 extra + 运行期懒加载（全仓无 Python 代码 import semgrep，迁移安全）；或 nightly 拆「基础安装/完整安装」两步。
- **闭环**：连续 2 次 nightly 绿 + Gate 1/2 verdict.json 证据存档。

---

## 4. 五要素问题清单（TOP 12）

### T-1 KR-C：nightly 验收门零证据（P0）
① acceptance-gates nightly 自 2026-10-19 起 ≥5 连挂，均止于依赖安装。② 测试 bug 点：`pyproject.toml:28`（semgrep 核心依赖）vs `acceptance-gates.yml:131`（windows×py3.14 runner）。③ 修改：semgrep 改 extra + 懒加载，或拆两步安装；修后 workflow_dispatch dry_run 实跑。④ 目的：恢复全 profile 验收证据产出。⑤ 闭环：连续 2 次 nightly 绿 + Gate verdict 存档。

### T-2 Gate 1/2 汇总行污染（P0，阻断）
① `check_selector_manifest._load` 把 pytest 汇总行当未登记 selector，manifest 与源码完全一致也 exit 2——反篡改门从未通过（本机 + 闻朔 + 何序-2 三次独立实证：`unregistered_new: ["114 tests collected in 0.45s"]`）。② `cicd/check_selector_manifest.py:63-67`；调用方 `acceptance-gates.yml:80/156/221`。③ 修改：`_load` 加 nodeid 形态过滤（含 `::`，剔除 `^\d+ tests? collected`）；两个门禁脚本补单测（真实 collected/JUnit fixture 正负向）。④ 目的：恢复反篡改门实效；根因是门禁脚本零单测。⑤ 闭环：修复后本机 collected 跑 exit 0、删一条 manifest 跑 exit 2；单测全绿。

### T-3 known-red 零隔离（P0）
① KR-A/KR-B 共 9 红仅文档登记，CI 无隔离 ⇒ 每次 push 主 test job 恒红。② `ci.yml:168-180`（无 deselect/quarantine）。③ 修改：建 `docs/testing/known_red/` 登记表（编号/根因/owner/关闭条件/**到期日**）+ quarantine job（deselect 出主门、单独报数、到期升级红）。④ 目的：主门禁的红只表示本次改动引入的回归。⑤ 闭环：主 job 绿 + quarantine job 按预期红。

### T-4 KR-A：83 条 residue（P0）
见 §3 关闭路线。① audit passed=false、4 用例红。② `tests/test_srv_019.py:72`（断言正确）；根因在生产侧搬迁。③ 方案 C（第二类过渡声明+冻结基线守卫+日落；migrate 单独关闭）。④ 目的：不虚假声称、不削弱门禁地恢复「零可执行 Python authority」不变量。⑤ 闭环：见 §3 MASKING 自检。

### T-5 写路径无 workspace 根约束（P0，安全严重）
① `db.propose_edit` 的 `_resolve_abs_path` 绝对路径原样采用、相对路径不拒 `..`，PoC 证实 `../escape_rel.txt` 与 workspace 外绝对路径均 `status=applied` 落盘（生产不可达——daemon 侧审计 only；CW_TEST_MODE/直接嵌入可达）。② `db/db_edit.py:113-126`；对照组 `db/db_lsp.py:82-106` 有校验；fuzz 缺口 `tests/test_fuzz.py:152-203`。③ 修改：解析后 `commonpath`/`is_relative_to` 校验 + 显式拒 `..` + realpath 后再比一次；复用 `_validate_file_path`。④ 目的：防越权写。⑤ 闭环：fuzz 增 traversal-write 用例断言 failed 且 workspace 外无文件。

### T-6 bandit 门禁范围外 75 条（P0，安全严重）
① CI bandit 只扫 cli/server/cw.py/config.py；db/（生产 SQL 层）与 scripts/ 完全在门禁外——实跑 75 条 medium+：B608×64、B314×7、B602 HIGH×1（`scripts/split_commit_069.py:15` shell=True）、B324 HIGH×1（FP）。② `ci.yml:372`（范围）；`db/db_agent_rules.py:1044` 等。③ 修改：db/ 纳入范围 + `.bandit` baseline（75 条登记只减不减）；B608 逐条 triage；B314 换 defusedxml；split_commit 改 shell=False。④ 目的：生产 SQL/XML 面注入风险可见。⑤ 闭环：扩面后 db/ 0 未登记 finding。

### T-7 Rust 集成测试只编译不执行（P0）
① `rust_ext/tests/`（daemon_e2e.rs 真实二进制 UDS 全 RPC、p1_f_recovery_e2e.rs kill-9 链 15 例、role_prompt_assets_golden.rs sha256 门）CI 仅 `--no-run --tests` 编译门；三个执行 job 均 `--lib`+模块过滤——「测试存在但从不执行」（柯砚 GAP-2 ≡ 骆骋 R-1 双域收敛）。② `ci.yml:454-518`。③ 修改：rust-unit-test job 增 `cargo test --tests`（CARGO_BIN_EXE 自动注入）。④ 目的：kill-9 恢复/SIGTERM/角色门有 CI 防线。⑤ 闭环：连续 3 轮绿。

### T-8 E2E 结果 advisory 不阻断（P0）
① 三平台 e2e-verify 的 E2E 退出码非零仅 `::warning::`，产物照传照发。② `e2e-verify-linux-x86_64.yml:181-183`、`e2e-verify-windows-amd64.yml:142-144`、`e2e-verify-linux-aarch64.yml:229-233`（白樾 G1 ≡ 闻朔 GAP-3 双域收敛）。③ 修改：tag 触发时非零改 exit 1；dispatch 形态保留 warning 但 job summary 置顶。④ 目的：部署后冒烟的存在意义是快速判定。⑤ 闭环：注入失败 E2E（临时改 EXPECTED_LANGUAGES）→ tag run 必须红。

### T-9 windows-arm64 产物零 E2E（P0）
① `pyinstaller-build.yml:24-27,173-177` 构建并发布 arm64 frozen 包，三个 e2e-verify 无对应（白樾 G5 ≡ 阮清 G1 双域收敛；阮清独立核验 `grep arm` 仅构建矩阵命中）。③ 修改：新增 `e2e-verify-windows-arm64.yml`（windows-11-arm runner；若不可用则 arm64 显式「构建不发布」并移出矩阵，禁止中间态）。⑤ 闭环：workflow 存在且 exit 0，或矩阵收口 + guard 断言。

### T-10 Windows Python 全量零执行（P1）
① ci.yml test job 实为 ubuntu-only（`ci.yml:12`）——**主理人早期向用户陈述的「ubuntu/windows × py3.10–3.14 全量矩阵」有误，阮清纠正并经源码核实**；Windows Python 覆盖仅 acceptance domains（py3.14）+ 7 个靶向文件；70 个测试文件引用 win32 分支无 CI 回归网。③ 修改：ci.yml 增 windows × py3.14 全量 job（沿用 CD-1 联接+PYTHONPATH 现成模式 `ci.yml:145-154`）。⑤ 闭环：连续 3 次 push 绿 + skip 集差异有书面解释。

### T-11 真空伪测试 test_uid_isolation（P1）
① ownership UID 隔离用例 body 为 `pass`，Linux root CI 空跑通过——安全关键路径假绿。② `tests/test_phase5_git_clean_dirty.py:397`。③ 修改：实现真双 UID 隔离（复用 e2e_workspace_authority_* harness）或 strict xfail + 跟踪任务，不得留 pass。⑤ 闭环：越权查询注入必须红。

### T-12 门禁脚本零单测（P1）
① `cicd/check_ci_gates.py`/`check_selector_manifest.py`/`gen_coverage_manifest.py` 无直接单测——T-2 缺陷因此存活，「门禁的缺陷只能靠 CI 红暴露」。③ 修改：三脚本建单测文件（真实 pytest 输出/JUnit/collected fixture 化，正负向各一组）。⑤ 闭环：单测入库且绿；后续改门禁先跑单测。

---

## 5. 完整缺口清单（按优先级；详情见各域报告）

### P0（11 项）
| # | 缺口 | 域来源 |
|---|---|---|
| 1 | KR-C nightly 零证据（semgrep 核心依赖） | 何序-2 漏登#1 |
| 2 | Gate 1/2 汇总行污染 + 门禁脚本零单测 | 白樾G2/闻朔GAP-1/何序C-1（三域收敛） |
| 3 | known-red 零隔离，主 CI 恒红 | 闻朔 GAP-2 |
| 4 | KR-A 83 条 residue | 苏雅 P0-1/何序裁决1 |
| 5 | 写路径遍历（PoC 证实） | 关屿-3 GAP-SEC-01 |
| 6 | bandit 范围外 75 条（2 HIGH） | 关屿-3 GAP-SEC-02 |
| 7 | Rust 集成测试 CI 只编译不执行 | 柯砚 GAP-2 ≡ 骆骋 R-1 |
| 8 | E2E advisory 不阻断 | 白樾 G1 ≡ 闻朔 GAP-3 |
| 9 | windows-arm64 零 E2E | 白樾 G5 ≡ 阮清 G1 |
| 10 | KR-B manifest stale 竞态 | 柯砚 GAP-1 |
| 11 | 真空伪测试 test_uid_isolation | 苏雅 P0-2 |

### P1（18 项）
Windows Python 全量零执行（阮清G2）；MCP wire 无 Windows（阮清G3）；回归集本机不可跑+报错误导（闻朔GAP-4/柯砚GAP-3）；8 个自有 harness 同类竞态（柯砚GAP-4）；覆盖率口径三层（source 含退休模块/maxfail 耦合/未实测——苏雅D2+何序裁决2）；selector 冻结无受控流程（闻朔GAP-6）；权限正路径弱断言（苏雅P1-3）；AC↔selector 无追溯矩阵（白樾G6）；schema N-1 无真实载体（白樾G4）；watcher 名实不符（白樾G3）；kill-9 E2E 三月无成功证据（骆骋R-2）；双 UID 无 CI 调用（骆骋R-5）；混沌 mock 化（骆骋R-4）；save-to-query 名实不符（骆骋R-6）；Python SCA 无锁文件（关屿-3 GAP-SEC-03）；gitleaks 不扫工作树+宽 allowlist（GAP-SEC-04）；semgrep 规则集不含注入类（GAP-SEC-05）；HTTP 状态码透传薄（柯砚GAP-5）。

### P2/P3（15 项）
变异测试缺失（苏雅P2-8，设计已备）；Rust 覆盖零门禁（苏雅P2-7）；性能全线缺失+ budgets 未校准（骆骋P-1~P-5）；探索性无 charter（闻朔GAP-8，模板已备）；无定期恢复演练（骆骋R-3）；parser ABI 负向门未自动化（柯砚GAP-6）；分页边界覆盖不均（柯砚GAP-7）；aarch64/win-arm64 无 PyPI wheel（阮清G4）；可用性无真人证据（阮清G6）；无障碍无 WCAG 口径（阮清G5）；skip-rate 参数误用（闻朔GAP-7）；变更影响面选择未落地（闻朔GAP-5）；migrate.py 备份秒级+损坏库裸 traceback（何序C-4/C-5）；check_ci_gates --expect-revision fail-open（何序C-2）；tokio/instant advisory 未登记（关屿-3 GAP-SEC-06）；账本 4 项漏登（何序5.2）；E2E macos 死代码 ok=True（阮清G7）；时区行为零覆盖（阮清G8）；HTTP RPC 安全套件待核实（关屿-3 GAP-SEC-07）。

---

## 6. 修复批次建议

| 批次 | 主题 | 内容 | 周期 |
|---|---|---|---|
| 1 | 门禁复苏 | T-1（KR-C）+ T-2（Gate 1/2+单测）+ T-3（known-red 隔离） | 1–2 天 |
| 2 | 既有红闭环 | T-4（KR-A 机制）+ T-10 兄弟项 KR-B（B2 harness）+ T-11（真空测试） | 3–5 天 |
| 3 | 安全加固 | T-5 + T-6 + gitleaks + 锁文件 | 2–3 天 |
| 4 | 执行面补齐 | T-7 + T-10 + T-9 + T-8 | 约 1 周 |
| 5 | 度量与设计（持续） | 覆盖率分层（何序裁决2）+ 变异引入 + 探索性 charter + 性能基线校准 | 持续 |

---

## 7. 未实测项登记（不得视为通过）

| 项 | 原因 |
|---|---|
| 覆盖率行/分支数字 | 本机 cp314 无 pytest-cov（daemon 符号比代理被 owner ACL 拒绝） |
| pip-audit（Python SCA） | 三次网络超时（EXIT=124），与前任同一失败模态 |
| semgrep CI 实跑 | 本机不可装（正是 KR-C 根因），仅配置审阅 |
| HTTP RPC 安全套件 | 本机隔离 daemon 拒连 20/20 ERROR（环境根因，需 CI 裁定） |
| 性能压测 | 无目标环境（联网授权不含压测目标），仅设计 |
| 可用性真人证据 | 无报告/启发式记录 |
| 双 UID 隔离实跑 | 需 Linux root runner，本机 skip |
| 探索性外部记录 | 待团队确认是否存在仓库外 charter |

**抽样范围声明（何序-2 代码审查）**：全量审了 server/{cas_schema,daemon_registry,migrate,cli_admin 头部}、cicd/ 三门禁脚本、scripts/{gen_coverage_manifest,check_client_purity,verify_route_matrix}、tests/{test_srv_019,test_db_retire_prod_purity}；抽样审了 server/toolchain_store L150-270、cli/main.py 约 200 行、workspace.rs L1040-1155。**未覆盖**：cli/main.py 其余约 16200 行、toolchain_store 其余约 1090 行、rust_ext 其余 79 个 daemon 模块、tests/ 全部——以上未覆盖部分结论不成立。

---

## 8. 各域结论表

| 域 | 专家 | 结论 | 一句话关键证据 |
|---|---|---|---|
| 单元·组件 | 苏雅-2 | 不通过 | KR-A 真缺口 + 真空伪测试；变异缺失（设计已备） |
| 集成·接口 | 柯砚-2 | 通过（附条件） | KR-B 竞态触发链钉死；Rust 集成 CI 不执行 |
| 系统·验收 | 白樾 | 不通过 | E2E 链路真实但结果 advisory + 两处名实不符 |
| 测试保障 | 闻朔 | 不通过 | Gate 1/2 恒红 + known-red 零隔离（设计完备实效零） |
| 性能·可靠 | 骆骋 | 不通过 | 91 例本机绿但 CI 执行链断裂；性能全线未实测 |
| 安全 | 关屿-3 | 待核实 | 2 严重缺口 + 3 项未能实测；cargo audit/bandit/fuzz 实测过 |
| 兼容·体验 | 阮清 | 待核实 | 兼容通过附 3 盲区；可用性/无障碍缺证据与口径 |
| 流程·度量·代码 | 何序-2 | 待核实 | 两项裁决落地；账本 4 项漏登；抽样内通过 |

---

*报告完。本报告基于 8 位域专家独立审查 + 主理人三项独立复核；所有「未发现」均为扫描未命中而非不存在断言；所有失败均调查至根因。*

---

## 附：批次 1 修复落地记录（2026-10-24，门禁复苏三件套）

| 提交 | 内容 | 独立复验（主理人重跑，非采信工人自证） |
|---|---|---|
| `0aca239` | semgrep 移出核心依赖（KR-C 修复；保留 all/semgrep extra，ci.yml:493 安全 job 显式安装不动） | tomllib 断言通过；Windows×cp314 dry-run rc=0 零 semgrep 匹配；tests/ 无依赖断言受影响；附带核实 release-artifact job 走 `--no-deps` 从未被影响 |
| `3bf9a04` | Gate 1/2 `_load` 加 nodeid（含 `::`）过滤 + `tests/test_cicd_gates.py` 14 用例 | 真实 collect+校验 **exit 0**（114/114；修复前 exit 2 汇总行污染）；删真实 nodeid ⇒ **exit 2** 且 `unregistered_new` 精确定位（门未改瞎）；14 单测绿 |
| `d349cb3` | known-red 隔离机制：`docs/testing/known_red/`（KR-A/B/C 登记，A/B expiry 2026-11-23、C 2026-10-31）+ `cicd/known_red.py` + 12 单测 + ci.yml 接线（主 job deselect + quarantine job） | `known_red.py` exit 0；deselect 恰 9 nodeid（18 词）；实跑两 KR 文件 **rc=0**（登记用例 0 执行，反证：不加 deselect 则 srv_019 exit 1 四 FAILED）；yaml 10 job 解析通过 |
| 本提交 | deselect 步显式 `set -euo pipefail` | 实证：`bash -e` 下 rc=64 步失败、无 `-e` 则 SURVIVED 静默放行——原实现依赖 runner 隐式 flag，与 C-2 fail-open 同型，按纪律显式化 |

**复评条件进展**：条件 1（KR-C）、2（Gate 1/2 + 门禁单测）、3（known-red 隔离）代码侧已落地并经独立复验；**CI 行为级证据待下一轮运行实证**（acceptance-gates nightly 安装步可过 + Gate 1/2 出真实 verdict + 主 test job 转绿 + quarantine job 如实报数）。条件 4（KR-A 机制）、5（安全 2 缺口）、6（Rust 集成入 CI）属批次 2–4，未动。
