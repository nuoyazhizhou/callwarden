"""经 daemon authority 创建 C 桶承接卡（PYT 回归卡 step#4 识别的跨 scope 生产缺陷）。

背景：PYT 回归卡（tests-only 边界）step#4 在定界遗留失败文件时，现场复核出 9 条真实生产
缺陷（登记见 `pyt_regression_step4_handoff_backlog.md` §W13）。其修复落点全部在 PYT 卡合同
forbidden_paths（`cli/**`、`db/**`、`rust_ext/src/**`、`server/**`），按 W5/W12 裁决口径
「剥离为独立 remediation 卡承接，PYT 卡保持 tests-only」执行。

本脚本按 §W14「建议切分」创建 C 桶承接卡：
  - C-03（P0 阻断）：`rust_ext` unix target 编译 5 错 → 单独一卡（W15 明细）
  - C-04..C-07（P1/P2）：`cli/main.py` i18n 遮蔽 + RPC 契约 → 合并一卡
  - C-08..C-09（P1/P2）：`server/` 授权清单与错误 code → 合并一卡

2026-09-14 追加（卡②执行期新发现 C-13/C-14/C-15，经裁决按「落点文件 + 契约耦合」切为 2 卡）：
  - C-13（P1）：`semgrep_handlers.rs` 未编译未接线 → 单独一卡（`mod.rs`/dispatch 面）
  - C-14..C-15（P1）：daemon `admin.assignment_create/revoke` 契约漂移 → 合并一卡
    （同 `admin_handlers.rs`，且 step0 需先做 `assignment_revoke` 契约单源裁决）

2026-09-14 再次追加（卡 A 执行/复核期实测出界新发现 C-16/C-17，见 backlog §W18 与
`c16_c17_remediation_handoff_20260914.md`）：
  - C-16（P1）：`assignment_show` 的 workspace 缺省路径不走权威 resolver → positive 分支
    对生产（CLI/MCP）调用方不可达 → 单独一卡（落 `task_collab_lease.rs`）
  - C-17（P1）：admin 路由块其余 handler 的 workspace 命名空间错配 → 单独一卡，
    **强制 step0 先做逐 handler 盘点再定修法**

2026-09-14 第三次追加（卡 C 执行/复核期实测出界新发现 C-18，见 backlog §W19）：
  - C-18（P1）：MCP-015 `assignment_show` 4 例陈旧断言（pre-authority「静默 none」语义）
    + W3 harness 基建隐患（find_daemon_binary MSYS 路径静默跳过 / mtime 假阳性）→ tests-only 卡

2026-09-14 第四次追加（C-18 闭环后承接 backlog §W20 卡 D 相邻缺陷 F1-F5，按「落点文件 + 缺陷同型」逐卡切分）：
  - C-19（P1）：`admin.gc_audit_get/list` SQL 引用 tasks 表不存在的列 workspace_id
    → 恒 prepare 失败（§W20 F1，C-17 测试文件 known-defect 锚冻结形态，本卡承接转绿）
    → 产品代码卡（admin_handlers.rs），必须走部署门禁

2026-09-14 第五次追加（C-19 闭环后承接 §W20 F2-F5；本批 step target_file 全部文件级，
  预声明确定性文档名 —— 直接落实 F5 教训，不再产生新的目录级白名单卡）：
  - C-20（P1）：`record_action_identity` 缺 `action_identities.action_id`（NOT NULL UNIQUE，
    schema 语义 `ACT-<uuid>`）+ `register_attestation_revocation` 缺
    `attestation_revocation_records.revocation_id`（`REV-<uuid>`）→ 两 INSERT 恒
    NOT NULL constraint failed（§W20 F2+F3 合并一卡，同文件 admin_handlers.rs，
    对齐卡 A `gen_assignment_id()` 先例）→ 产品代码卡，必须走部署门禁
  - C-21（P1）：`edit.*`/`gate.*`/`rule.*`/`guardrail.add_rule`/`summary.generate` 第二路由块
    同类代理 id 缺陷（snapshot_state.rs 匹配臂，2026-09-14 实测 19 方法）→ 同 C-17 根因，
    路由层单点 `open_codegraph_db_write` 收口（紧邻 semgrep 块为正确形态锚）
    → 产品代码卡，必须走部署门禁
  - C-22（P2）：C 桶建卡模板 step target_file 目录级白名单与 changes[] 全等比对不兼容
    （E_CHANGE_PATH_NOT_ALLOWED，卡 D step2 实测；C-13 先例 §4.1 同源）→ 模板改文件级 +
    校验测试固化 → tests/deliverables-only 卡，不触发部署门禁

2026-09-15 第六次修订（C-22 自身执行，§W20 F5 闭环）：本脚本 build_cards() 历史遗留的
  15 处目录级 step target_file（tests/ ×4、rust_ext ×4、deliverables/software-company/ ×6、runtime/current ×1）
  全量改文件级 —— 已闭环卡引用实际产物名（T-<ts>-<id>-evidence.md / -inventory.md），
  多文件用 `;` 连接（与 daemon task_collab_lifecycle.rs L285 拆分规则对齐）。

F5 教训注记（语义区分，勿再混淆）：
  - step `target_file`：**文件级全等**白名单 —— daemon 侧 `changes[].file_path` 与之做
    逐字符串全等比对（task_collab_lifecycle.rs：`;`/`,` 拆分 + 反斜杠→正斜杠归一后 `==` 判定，
    无目录前缀/通配语义）；目录级条目永远无法命中，恒 E_CHANGE_PATH_NOT_ALLOWED。
  - `executor_allowed`（合同 allowed_paths）：**可目录前缀** —— 仅是 executor 的工作区
    边界声明，不参与 changes 全等比对。
  - 两者混用是本缺陷根因；新增卡一律文件级 target_file（「文档尚未存在」的 step0 类
    场景用预声明确定性名，如 cNN_remediation_inventory.md），tests/test_c_bucket_template_
    target_file_whitelist.py 固化该不变量。

用法（先确保 daemon 就绪；端点以 `cw daemon health` 回执为准，可用环境变量覆盖）：
    CW_DAEMON_ENDPOINT=http://127.0.0.1:<port> \
    python deliverables/software-company/create_c_bucket_remediation_tasks.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.server.daemon_client import HttpDaemonRpcClient

# 承接来源：PYT 回归卡（其 step#4 = T-1789139378194-02f1f66c, fix_defect）
PARENT_ID = "T-1788871227327-45c94bd8"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
ENDPOINT = os.environ.get("CW_DAEMON_ENDPOINT", "http://127.0.0.1:1615")

BACKLOG = PROJECT_ROOT / "deliverables" / "software-company" / "pyt_regression_step4_handoff_backlog.md"
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    """计算 role 启动模板 sha256（大写），与 manifest 的 content_sha256 口径一致。"""
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts(executor_allowed: list[str], acceptance: str, evidence: str) -> list[dict]:
    """A′ 三角色合同：executor 的 allowed_paths 显式覆盖缺陷文件 + tests + deliverables。"""
    return [
        {
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps(executor_allowed),
            "forbidden_paths": json.dumps([
                "db/", "scripts/refresh_shared_runtime.ps1",
                "direct SQLite writes", "task.apply", "task.close",
                "task.supersede", "status forgery",
            ]),
            "commands": "cargo build; cargo test; pytest; git diff --check",
            "acceptance_checks": acceptance,
            "required_evidence": evidence,
            "handoff_to": "reviewer", "independence": "required",
        },
        {
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only source under review, tests, evidence and daemon projection",
            "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
            "commands": "read-only review",
            "acceptance_checks": (
                "root cause addressed (not masked); forbidden paths untouched; "
                "regression evidence reproducible on a clean checkout"
            ),
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator", "independence": "required",
        },
        {
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "lifecycle finalization only",
            "forbidden_paths": "production edits; direct database writes",
            "commands": "task.apply; task.close; task.next_action after reviewer PASS",
            "acceptance_checks": "independent reviewer PASS; apply then close then COMPLETE",
            "required_evidence": "finalization manifest",
            "handoff_to": "complete", "independence": "required",
        },
    ]


def build_cards() -> list[dict]:
    """3 张承接卡的 title / steps / contracts（description 统一附 backlog 摘要）。"""
    return [
        {
            "title": "C-03 承接：rust_ext unix/Linux target 编译阻断修复（解除 WSL 共存契约阻断）",
            "findings": "C-03",
            "executor_allowed": [
                "rust_ext/src/daemon/transport.rs",
                "rust_ext/src/daemon/http_server.rs",
                "rust_ext/src/daemon/daemon_autostart_handlers.rs",
                "rust_ext/src/daemon/server.rs",
                "rust_ext/Cargo.toml",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "WSL 内 cargo build --no-default-features --bin cw-daemon 零 error；"
                "tests/test_wsl_local_daemon_e2e.py 由 2 errors 转 pass"
            ),
            "evidence": (
                "WSL cargo build 全文；pytest tests/test_wsl_local_daemon_e2e.py 输出；"
                "Windows cargo build 回归未退化；git diff"
            ),
            "steps": [
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/transport.rs",
                    "target_symbol": "create_listener",
                    "check_items": [
                        "E0063: unix ServerConfig 初始化补 http: None（对齐 server.rs:65-104）",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/http_server.rs",
                    "target_symbol": "unix socket permission setup",
                    "check_items": [
                        "E0603: std::os::unix::fs::Permissions is private → 改用 std::fs::Permissions",
                        "E0599: 补 use std::os::unix::fs::PermissionsExt;",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/daemon_autostart_handlers.rs",
                    "target_symbol": "handle_try_connect_unix",
                    "check_items": [
                        "E0599: SocketAddr::from_path → from_pathname(..).ok()",
                        "E0599: UnixStream::connect_timeout 不存在 → UnixStream::connect（或 socket2）",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_wsl_local_daemon_e2e.py",
                    "target_symbol": "wsl_authority fixture",
                    "check_items": [
                        "WSL 内 cargo build 成功，fixture 不再 pytest.fail",
                        "2 用例转 pass（本机 WSL 前置已就绪，不 skip）",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/transport.rs;rust_ext/src/daemon/http_server.rs;rust_ext/src/daemon/daemon_autostart_handlers.rs",
                    "target_symbol": "unix target compilability closure",
                    "check_items": [
                        "cargo build --no-default-features 零 error（WSL）",
                        "cargo build（Windows 默认 feature）未退化",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id, never the PYT card id",
                    ],
                },
            ],
        },
        {
            "title": "C-04..C-07 承接：cli/main.py i18n 遮蔽与 RPC 契约缺陷修复",
            "findings": "C-04, C-05, C-06, C-07",
            "executor_allowed": [
                "cli/main.py",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "cw churn / cw test-impact / cw assignment create|revoke 在真实数据下不再崩；"
                "cw semgrep stats <PATH> 尊重显式路径参数"
            ),
            "evidence": (
                "4 条命令的实跑回执（前/后）；相关测试输出；git diff --check"
            ),
            "steps": [
                {
                    "action": "implement",
                    "target_file": "cli/main.py",
                    "target_symbol": "churn trend loop",
                    "check_items": [
                        "C-04: for t in trend[:20] 遮蔽模块级 i18n t → 重命名循环变量",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "cli/main.py",
                    "target_symbol": "test-impact loop",
                    "check_items": [
                        "C-05: for i, t in enumerate(tests, 1) 遮蔽 i18n t → 重命名循环变量",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "cli/main.py",
                    "target_symbol": "assignment create/revoke call sites",
                    "check_items": [
                        "C-06: 2 元组解包 dict 回包 → 按 RPC 回包契约解包",
                        "C-06: 方法名对齐 daemon route_matrix admin.assignment_create / admin.assignment_revoke",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "cli/main.py",
                    "target_symbol": "_METHOD_MAP / get_semgrep_summary",
                    "check_items": [
                        "C-07: 补 _METHOD_MAP 条目，避免位置参数降级为 arg0 被静默丢弃",
                        "C-07: 确保 target_paths 透传到 daemon handle_run_semgrep",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_cli_005_http_rpc.py;tests/test_cli_006_http_rpc.py;tests/test_cli_011_http_rpc.py;tests/test_cli_020_http_rpc.py;tests/test_cli_057_http_rpc.py;tests/test_cli_061_http_rpc.py;tests/test_cli_066_http_rpc.py",
                    "target_symbol": "cli churn / test-impact / assignment / semgrep regression",
                    "check_items": [
                        "覆盖 trend 非空分支与 ≥1 测试命中分支",
                        "daemon 模式下 assignment create/revoke 回包解析正确",
                    ],
                },
            ],
        },
        {
            "title": "C-08..C-09 承接：server/ 授权清单与错误 code 契约修复",
            "findings": "C-08, C-09",
            "executor_allowed": [
                "server/daemon_server.py",
                "server/daemon_client.py",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "Python legacy daemon 的 ADMIN_ONLY_METHODS 与 Rust dispatch.rs 清单一致（含 "
                "mcp.backup_restore.backup_file，fail-closed）；SharedTaskWriterRequiredError.code "
                "为其自身 code 而非父类默认值"
            ),
            "evidence": (
                "pytest tests/test_phase8_admin_rpc_authz.py 输出；code 属性断言；git diff --check"
            ),
            "steps": [
                {
                    "action": "implement",
                    "target_file": "server/daemon_server.py",
                    "target_symbol": "ADMIN_ONLY_METHODS",
                    "check_items": [
                        "C-08: 补 mcp.backup_restore.backup_file，与 rust_ext/src/daemon/dispatch.rs:2254 对齐",
                        "保持未授权 peer fail-closed",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "server/daemon_client.py",
                    "target_symbol": "SharedTaskWriterRequiredError.__init__",
                    "check_items": [
                        "C-09: super().__init__(f\"{self.code}: {message}\", code=self.code) 显式传 code",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_phase8_admin_rpc_authz.py",
                    "target_symbol": "admin-only authz",
                    "check_items": [
                        "未授权 peer 调 backup_file 被拒",
                        "SharedTaskWriterRequiredError.code 断言",
                    ],
                },
            ],
        },
        {
            "title": "C-13 承接：semgrep_handlers 编译接线与 semgrep RPC route 落地（解锁 C-07 端到端）",
            "findings": "C-13",
            "executor_allowed": [
                "rust_ext/src/daemon/mod.rs",
                "rust_ext/src/daemon/snapshot_state.rs",
                "rust_ext/src/daemon/dispatch.rs",
                "rust_ext/src/daemon/http_server.rs",
                "rust_ext/src/daemon/route_matrix.rs",
                "rust_ext/src/daemon/semgrep_handlers.rs",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "daemon 对 run_semgrep / run_semgrep_and_save / scan_semgrep_incremental / "
                "get_semgrep_summary 四个方法名可达（不再 method_not_found）；"
                "cw semgrep scan cli 与 --quick 在 daemon 模式下端到端可用；"
                "cargo build（Windows 默认 feature）零 error，cargo test 未退化"
            ),
            "evidence": (
                "cargo build 全文；cw semgrep scan cli --json 与 --quick --json 实跑回执（前/后）；"
                "相关 pytest 输出；git diff --check"
            ),
            "steps": [
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/mod.rs",
                    "target_symbol": "module declarations",
                    "check_items": [
                        "C-13: 补 pub mod semgrep_handlers;（该文件 CLI-061 已存在但从未被编译）",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "dispatch match arms",
                    "check_items": [
                        "C-13: 为 4 个裸方法名 run_semgrep / run_semgrep_and_save / "
                        "scan_semgrep_incremental / get_semgrep_summary 增 arm；方法名须与 "
                        "cli/main.py:1207-1212 的 _METHOD_MAP rpc_method 逐字一致",
                        "复用同款本地 use（对照 snapshot_state.rs:2731 的 "
                        "`use super::admin_handlers as admin;`）引入 semgrep_handlers",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/route_matrix.rs",
                    "target_symbol": "ToolRoute 登记 / http_server native 面",
                    "check_items": [
                        "C-13: route/probe 面同步登记；若 HTTP 面存在 method allowlist 或 native "
                        "probe 白名单则一并补齐，若判定无需改动须在证据中说明依据",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c13_semgrep_dispatch_wiring.py",
                    "target_symbol": "semgrep cli e2e",
                    "check_items": [
                        "daemon 模式下 cw semgrep scan cli 不再 method_not_found: 未知方法: run_semgrep",
                        "cw semgrep scan cli --quick 不再 未知方法: get_semgrep_summary",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": (
                        "rust_ext/src/daemon/mod.rs;"
                        "rust_ext/src/daemon/snapshot_state.rs;"
                        "rust_ext/src/daemon/route_matrix.rs"
                    ),
                    "target_symbol": "compilability + regression closure",
                    "check_items": [
                        "cargo build 零 error；cargo test 未退化",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id, never the PYT card id",
                    ],
                },
            ],
        },
        {
            "title": "C-14..C-15 承接：daemon assignment create/revoke 契约一致性修复（assignment_id 单源）",
            "findings": "C-14, C-15",
            "executor_allowed": [
                "rust_ext/src/daemon/admin_handlers.rs",
                "docs/mcp_tools.md",
                "docs/cli_reference.md",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "cw assignment create 不再 NOT NULL constraint failed，并返回真实 ASG-<uuid>；"
                "cw assignment revoke 与已文档化契约一致（show → create → revoke 往返可用）；"
                "docs/mcp_tools.md / docs/cli_reference.md 与实现单源一致；"
                "cargo build / cargo test 未退化"
            ),
            "evidence": (
                "cw assignment create|show|revoke 实跑回执（前/后）；契约单源裁决记录；"
                "cargo build 全文；相关 pytest 输出；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/T-1789290073049-6442e268-evidence.md",
                    "target_symbol": "assignment_revoke 契约单源裁决",
                    "check_items": [
                        "C-15: 裁决 assignment_revoke 以 assignment_id 为准（对齐 "
                        "docs/mcp_tools.md:1972）还是保留 task_id；若需要 assignment_id → task_id "
                        "反查则一并定案",
                        "裁决结论须写入 deliverables/software-company/ 并作为后续 step 依据"
                        "（本 step 不直接改产品代码）",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "handle_assignment_create",
                    "check_items": [
                        "C-14: INSERT 补 assignment_id 列，写入 ASG-<uuid>"
                        "（TEXT NOT NULL UNIQUE，见 db/schema.py:1626-1639）",
                        "返回值 assignment_id 为 ASG-<uuid> 字符串，而非 last_insert_rowid() 整数",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "handle_assignment_revoke（及 assignment_show 反查如裁决需要）",
                    "check_items": [
                        "C-15: 按 step0 裁决实现入参契约；返回体对齐文档 "
                        "{ok, assignment_id, revoked_at}",
                        "append 语义：置 status=revoked + revoked_at，不删除记录",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c14_c15_assignment_contract.py",
                    "target_symbol": "assignment round-trip + docs 同步",
                    "check_items": [
                        "show → create → revoke 往返可用；NOT NULL 回归用例",
                        "docs/mcp_tools.md:1968-1972 与 docs/cli_reference.md:3246-3257 与实现逐字一致",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "contract + regression closure",
                    "check_items": [
                        "cargo build 零 error；cargo test 未退化",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id",
                    ],
                },
            ],
        },
        {
            "title": "C-16 承接：daemon assignment_show workspace 权威解析修复（打通 CLI 端 show→create→revoke 往返）",
            "findings": "C-16",
            "origin": (
                "卡 A `T-1789340885245-071cb9b4`（C-14/C-15）执行与独立复核期实测出界发现"
                "（越出该卡 executor_allowed）"
            ),
            "backlog_section": "§W18",
            "executor_allowed": [
                "rust_ext/src/daemon/task_collab_lease.rs",
                "server/daemon_client.py",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "cw assignment show <task_id> --role <role> --json 在 assignment 存在时返回 active 行"
                "（非 {\"status\":\"none\"}），且 assignment_id 与 create 回执逐字一致；"
                "show → create → revoke → show 四步在 CLI 通道端到端可用（撤销后回 none）；"
                "无 binding 的 legacy task 调 show 必须 fail-closed（E_TASK_WORKSPACE_UNBOUND）"
                "而非静默 none；显式传不一致 workspace_id → E_WORKSPACE_AUTHORITY_MISMATCH；"
                "task_id 为空 → invalid_params；cargo build 零 error；cargo test 零新增失败；"
                "git diff --check clean"
            ),
            "evidence": (
                "本 HEAD 上 handle_assignment_show workspace 缺省路径的逐行复核；"
                "server/daemon_client.py _is_task_scoped_authority_request / workspace 注入门禁的"
                "逐行复核与「是否需改」裁决（含实测依据或明确不改的理由）；"
                "新增负向矩阵 6 条用例文件与 pytest 输出；cargo build / cargo test 与前后的"
                "cw assignment show|create|revoke 实跑回执；部署门禁回执或「未部署」显式披露；"
                "git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/T-1789365537146-bef4c2e4-evidence.md",
                    "target_symbol": "C-16 根因复核与调用侧注入门禁裁决",
                    "check_items": [
                        "在本 HEAD 逐行复核 task_collab_lease.rs 的 handle_assignment_show"
                        " workspace 解析路径，确认是否仍回落常量缺省；行号会漂移，"
                        "严禁照抄交接文档 §3.2 或卡 A 证据 §4.1 的行号与表述",
                        "逐行复核 server/daemon_client.py 的 _is_task_scoped_authority_request()"
                        " 与 workspace 注入门禁：交接文档 §3.2 认定本 HEAD 实际规则是"
                        "「非空 task_id/superseded_id 即跳过注入」，而卡 A 证据 §4.1 写的是"
                        "「只对 task./lease. 前缀注入」（陈旧）——须以本 HEAD 为准并写入证据",
                        "裁决 server/daemon_client.py 是否需要改：语义上「task-scoped 由 daemon"
                        " 从不可变 binding 解析」本就正确，缺的是 daemon 侧实现；若判不改，"
                        "须给出部署后真实 CLI 往返的实测依据，严禁在未实测前改注入门禁"
                        "（blast radius 覆盖全部 task/lease 方法）",
                        "裁决结论须写入 deliverables/software-company/ 并作为后续 step 依据"
                        "（本 step 不直接改产品代码）",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/task_collab_lease.rs",
                    "target_symbol": "handle_assignment_show workspace 权威解析",
                    "check_items": [
                        "缺省 workspace 改走权威 resolver："
                        "crate::daemon::task_collab::task_bound_workspace_id(conn, &task_id, requested)",
                        "requested = params.get(\"workspace_id\").and_then(Value::as_i64)；参数缺省时应由"
                        "不可变 task_workspace_bindings 解析，而非回落常量缺省值",
                        "task_id 为空时必须在取 conn 之前 fail-closed 为 invalid_params"
                        "（不得带着空 task_id 去查 binding，不得 panic）",
                        "无 binding 的 legacy task → E_TASK_WORKSPACE_UNBOUND fail-closed，"
                        "严禁静默回落 {\"status\":\"none\"} 掩盖",
                        "显式 workspace_id 与 binding 不一致 → E_WORKSPACE_AUTHORITY_MISMATCH",
                        "保持 handler 只读语义；不得改 db/**、不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c16_assignment_show_workspace_authority.py",
                    "target_symbol": "assignment_show workspace authority 负向矩阵",
                    "check_items": [
                        "新增本卡专属用例文件（卡 A 的 tests/test_c14_c15_assignment_contract.py"
                        " 为源码契约范式，不覆盖本卡）",
                        "6 条负向矩阵：①参数无 workspace_id 且 task 有 binding → 命中 active 行；"
                        "②参数无 workspace_id 且 task 无 binding → E_TASK_WORKSPACE_UNBOUND；"
                        "③参数 workspace_id 为错误值 → E_WORKSPACE_AUTHORITY_MISMATCH；"
                        "④task_id 为空 → invalid_params（不 panic、不查 binding）；"
                        "⑤已 revoke 的 assignment → {\"status\":\"none\"}；"
                        "⑥只读性：调用前后 task_assignments 内容不变",
                        "严禁只做源码字符串断言充数；若新增隔离 daemon live 用例，"
                        "须避开陈旧二进制陷阱（用部署门禁产出的 stage-refresh binary）",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/task_collab_lease.rs",
                    "target_symbol": "workspace authority + CLI end-to-end closure",
                    "check_items": [
                        "cargo build 零 error；cargo test 零新增失败（须与 HEAD 基线同集对照）",
                        "若需 live 验证：scripts/refresh_shared_runtime.ps1 -TaskId <本卡 task_id>，"
                        "核对三方哈希一致并记录 rollback=false；若未部署，必须在证据中显式写明"
                        "「本轮未部署 runtime，live daemon 仍为旧 binary，结论仅在库测试中成立」",
                        "真实 CLI 通道往返：cw assignment show → create → revoke → show",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 PYT 卡 [T-1788871227327-45c94bd8]"
                        " 或卡 A [T-1789340885245-071cb9b4]）",
                    ],
                },
            ],
        },
        {
            "title": "C-17 承接：admin 路由 workspace 命名空间盘点与同类 handler 收口",
            "findings": "C-17",
            "origin": (
                "卡 A `T-1789340885245-071cb9b4`（C-14/C-15）执行与独立复核期实测出界发现"
                "（越出该卡 executor_allowed；卡 A 仅收口 assignment_create/revoke 两处）"
            ),
            "backlog_section": "§W18",
            "executor_allowed": [
                "rust_ext/src/daemon/snapshot_state.rs",
                "rust_ext/src/daemon/admin_handlers.rs",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "admin 路由块逐 handler 盘点表完整且逐项有源码依据（禁止推断）；"
                "被判定有风险的 handler 有实测前后回执；判无风险的须给源码/实测依据（不得空白）；"
                "盘点结论若为「多数 handler 无风险」则范围已收窄并写入本卡合同；"
                "cargo build 零 error；cargo test 零新增失败；git diff --check clean；"
                "commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "deliverables/software-company/ 下的逐 handler 盘点表（method / handler / writes_to /"
                " fk_target / workspace_id 用法 / 风险等级 / 建议修法）；方法名总数复核结论"
                "（交接文档 §4.2 逐行数=21、扣已收口 2 = 19，backlog §W18 与卡 A 证据记 18，"
                "差异须澄清并回报 backlog）；被判定有风险 handler 的实测前后回执；"
                "cargo build / cargo test 输出；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/T-1789365537230-c3f02eb4-inventory.md",
                    "target_symbol": "admin 路由 workspace 命名空间盘点（强制前置）",
                    "check_items": [
                        "产出逐 handler 一行的盘点表，字段：method / handler / writes_to"
                        "（实际被写的 DB 文件 + 表名，须读源码确认，严禁推断）/ fk_target"
                        "（是否有 FOREIGN KEY ... REFERENCES workspaces(id)）/ workspace_id 用法"
                        "（直接写入 / 仅 WHERE 过滤 / 未使用）/ 风险等级（恒失败 / 静默误报 / 无风险，"
                        "附依据）/ 建议修法（走 task_bound_workspace_id / 改路由签名 / 无需改）",
                        "在本 HEAD 逐行复核 snapshot_state.rs admin 路由块的方法名总数：交接文档"
                        " §4.2 逐行复核为 21（扣卡 A 已收口 assignment_create/revoke 2 个 = 19 待盘点），"
                        "而 backlog §W18 与卡 A 证据 §4.2 记作 18 —— 必须在证据中澄清该差异"
                        "并回报 backlog，不得在未复核时沿用 18，也不得照抄本卡或交接文档的行号",
                        "严禁跳过盘点直接照抄卡 A 的 assignment 修法：gc_* / clear_clones /"
                        " snapshot_compare / branch_* 写的是 codegraph 面表，其 workspace 语义与"
                        " task DB workspaces(id) 未必同源；record_action_identity /"
                        " register_attestation_revocation / record_artifact_identity / publish_interface /"
                        " select_interface_provider 可能同时落 task DB 与 codegraph DB",
                        "若盘点结论为「多数 handler 无风险」，须把范围收窄并把结论写进本卡合同，"
                        "不得硬把 19 个都改（本 step 不改产品代码）",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "admin route workspace authority",
                    "check_items": [
                        "仅收口 step0 盘点判定为「恒失败」或「静默误报」的 handler；"
                        "判无风险的必须留下源码/实测依据，不得空白",
                        "需要 task DB 语义的 handler 统一改走权威 resolver"
                        " crate::daemon::task_collab::task_bound_workspace_id，与卡 A 的"
                        " assignment_create/revoke 收口同源（经 task_collab.rs 的 pub(crate) use 再导出）",
                        "风险判定若涉及路由层签名（owned_workspace 传入的是 daemon registry 代理 id，"
                        "与 task DB workspaces.id 不同命名空间），须先证明再改签名，"
                        "禁止在未证明的情况下改动 shared 路由行为",
                        "不得改 db/**、不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c17_admin_route_workspace_authority.py",
                    "target_symbol": "admin handler workspace authority regression",
                    "check_items": [
                        "被判有风险的 handler 须有实测前后回执（禁止只做源码字符串断言）",
                        "回归覆盖盘点表判定为有风险的 handler 对应方法名",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "admin workspace namespace closure",
                    "check_items": [
                        "cargo build 零 error；cargo test 零新增失败（须与 HEAD 基线同集对照）",
                        "若需 live 验证：scripts/refresh_shared_runtime.ps1 -TaskId <本卡 task_id>，"
                        "核对三方哈希一致并记录 rollback=false；若未部署须在证据中显式披露",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 PYT 卡或卡 A 的 task_id）",
                    ],
                },
            ],
        },
        {
            "title": "C-18 承接：MCP-015 assignment_show 陈旧断言修复与 W3 harness 基建收敛",
            "findings": "C-18",
            "origin": (
                "卡 C `T-1789365537146-bef4c2e4`（C-16）step3 release_verify 的「同集基线对照」"
                "实测发现（backlog §W19）：C-16 让 handle_assignment_show 走权威 resolver 后，"
                "tests/test_mcp_assignment_show_http_rpc.py 4 例以合成 task_id 断言"
                "「无 active assignment → {\"status\":\"none\"}」，编码的是权威模型落地之前的"
                "「静默 none」语义，与 fail-closed 不可同时成立 → 陈旧断言（A 桶第三类根因）；"
                "另发现 tests/_w3_harness.py::find_daemon_binary() 两处基建隐患："
                "CW_DAEMON_BIN 的 MSYS 风格路径在 Windows Python 下 isfile 判 False 被静默跳过，"
                "且候选取 mtime 最新可回落陈旧 debug 构建（已复现一次假阳性）"
            ),
            "backlog_section": "§W19",
            "executor_allowed": [
                "tests/_w3_harness.py",
                "tests/test_mcp_assignment_show_http_rpc.py",
                "tests/",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "tests/test_mcp_assignment_show_http_rpc.py 原 4 例失败恢复 passed："
                "3 例经种 binding 的 well-known 已绑定 task 保留「no active assignment → none」"
                "原覆盖语义；test_assignment_show_unknown_workspace 改断"
                " E_WORKSPACE_AUTHORITY_MISMATCH；新增「无 binding → E_TASK_WORKSPACE_UNBOUND」"
                "正例；_w3_harness.py 追加 binding seed 不改既有行；find_daemon_binary()"
                " 对 MSYS 路径候选不再静默跳过，且选中候选必须打印路径与 sha256；"
                "W3 家族其余 test_mcp_*_http_rpc.py 同集对照零新增失败；"
                "本卡为 tests-only，不改任何产品代码（rust_ext/**、server/**、db/** 均禁）；"
                "git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：4 例失败签名与 A/B 反证引用（基线 20 passed vs 卡 C 后"
                " 4 failed/16 passed）、逐例陈旧断言判定表（含源码行号，行号须在本 HEAD 复核，"
                "严禁照抄 backlog §W19）、W3 家族同类扫描结论；"
                "harness seed 与 find_daemon_binary 收敛的 diff 说明；"
                "4 例修复前后 pytest 全量输出（含新正例）；W3 家族同集对照输出"
                "（修复前基线 vs 修复后，二进制 sha256 一并记录）；"
                "「tests-only 未部署」的显式披露；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/T-1789377001689-0aee0a6c-inventory.md",
                    "target_symbol": "C-18 陈旧断言逐例复核与 W3 家族扫描（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 逐例复核 4 例失败（no_match/with_role/unknown_workspace/"
                        "new_client_instance_stable）的断言原文与失败签名，确认与 backlog §W19"
                        " 记载一致；若本 HEAD 已漂移（如卡 D 186582e 起路由层变更）须如实记录",
                        "逐例判定：断言是否编码 pre-authority「静默 none」语义；"
                        "判定须给 tests/_w3_harness.py setup_w3_client 只 seed workspaces"
                        " 不 seed task_workspace_bindings 的源码依据",
                        "扫描 W3 家族其余 test_mcp_*_http_rpc.py 是否存在同族合成 task_id 断言，"
                        "产出逐文件结论表（有/无同类断言，附证据行）；"
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "tests/_w3_harness.py",
                    "target_symbol": "w3 harness binding seed + find_daemon_binary 收敛",
                    "check_items": [
                        "在 _w3_harness.py **追加**（严禁改既有行）一个 well-known 已绑定 task 的"
                        " task_workspace_bindings seed（task_id 常量 + binding 行），"
                        "保证既有 20 例语义不受影响（既有用例只 seed workspaces 的行为不变）",
                        "find_daemon_binary() 收敛：候选存在性判定兼容 MSYS 风格路径"
                        "（os.path.exists/isfile 前 转 Windows 路径或双判定）；"
                        "选中候选必须打印最终路径与 sha256（禁止静默回落）",
                        "不得改 rust_ext/**、server/**、db/**；不得直写 SQLite",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "tests/test_mcp_assignment_show_http_rpc.py",
                    "target_symbol": "assignment_show 4 例断言迁移 + UNBOUND 正例",
                    "check_items": [
                        "no_match / with_role / new_client_instance_stable 3 例的合成 task_id"
                        " 改指向 well-known 已绑定 task，保留「有 binding 但无 active assignment"
                        " → {\"status\":\"none\"}」原覆盖语义",
                        "test_assignment_show_unknown_workspace 改断"
                        " E_WORKSPACE_AUTHORITY_MISMATCH（显式传不一致 workspace_id）",
                        "新增「无 binding task → E_TASK_WORKSPACE_UNBOUND」正例（fail-closed 覆盖）",
                        "不改产品代码；断言迁移后 4 例 + 新正例全部 passed",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_mcp_assignment_show_http_rpc.py",
                    "target_symbol": "assignment_show 陈旧断言回归（A/B 同集对照）",
                    "check_items": [
                        "修复后全文件 pytest 全绿（4 例恢复 + 新正例），记录二进制 sha256",
                        "与修复前基线同集对照（期望基线 4 failed/16 passed → 修复后全 passed），"
                        "证明用例判别力；对照输出留证据",
                        "W3 家族其余 test_mcp_*_http_rpc.py 同集跑一遍，零新增失败",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "deliverables/software-company/T-1789377001689-0aee0a6c-inventory.md",
                    "target_symbol": "tests-only closure（无部署）",
                    "check_items": [
                        "本卡 tests-only：无产品代码改动 → 不触发部署门禁，"
                        "但必须在证据中显式披露「未部署及原因」",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用卡 C/C-16 或卡 D/C-17 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
        {
            "title": "C-19 承接：admin.gc_audit_get/list 引用不存在列 tasks.workspace_id 修复（§W20 F1）",
            "findings": "C-19",
            "origin": (
                "卡 D `T-1789365537230-c3f02eb4`（C-17）修复期实测出界新发现"
                "（backlog §W20 F1）：C-17 收口 admin 路由块命名空间后，"
                "admin.gc_audit_get（admin_handlers.rs）与 admin.gc_audit_list 的 SQL "
                "仍引用 tasks 表不存在的列 workspace_id（tasks 实际列 id/title/description/"
                "creator/status/created_at/updated_at/applied_at/closed_at/parent_id/depth/"
                "sort_order，PRAGMA 实证）→ 恒 prepare 失败（no such column）。"
                "该缺陷与命名空间错配根因不同（列不存在 vs 值域错），C-17 测试文件以 "
                "known-defect 锚 test_gc_audit_get_f1_known_defect_still_fails 冻结形态，"
                "「转绿须由独立缺陷卡承接」—— 即本卡。权威 workspace 映射为 "
                "task_workspace_bindings（task_collab_shared.rs::task_bound_workspace_id "
                "同源语义），change_audit 经 ca.task_id 与 binding 表 join 即得正确作用域"
            ),
            "backlog_section": "§W20",
            "executor_allowed": [
                "rust_ext/src/daemon/admin_handlers.rs",
                "tests/test_c17_admin_route_workspace_authority.py",
                "deliverables/software-company/",
            ],
            "acceptance": (
                "gc_audit_get/list 的 SQL 不再引用 tasks.workspace_id，改经 "
                "task_workspace_bindings join 作用域（与 task_bound_workspace_id 权威语义同源）；"
                "C-17 F1 known-defect 锚迁移为正确行为正例：绑定 task 的 change_audit 行 "
                "gc_audit_get 可命中、gc_audit_list 只返回本 workspace 绑定 task 的行、"
                "unbound task 与跨 workspace task 的行不可见（隔离负例）；"
                "其余 admin handler 与路由层（snapshot_state.rs）零触碰；"
                "cargo build 零 error；cargo test 零新增失败（同集对照）；"
                "产品代码改动 → 部署门禁必须走：refresh_shared_runtime.ps1 -TaskId 本卡 task_id、"
                "health.git_commit==HEAD、三方 sha256 一致、PID 记录、rollback=false；"
                "部署后生产只读 probe 回执（修复前恒 prepare 失败 → 部署后返回行）；"
                "git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：本 HEAD 两 handler SQL 原文与行号、PRAGMA tasks 列清单实证、"
                "task_workspace_bindings 权威依据（task_collab_shared.rs 注释引用）、"
                "C-17 F1 锚断言原文复核、Python db_gc.py gc_audit_* 走 gc_runs 表与本缺陷无关"
                " 的排除依据、全部 gc_*/audit 类 handler 同型坏列扫描结论；"
                "修复 diff 说明（两处 SQL 的前后对照）；"
                "隔离 daemon 前后实测：before（no such column）/ after（命中预置行 + 隔离负例）；"
                "cargo build/test 输出；部署回执（refresh_shared_runtime 三方哈希 + PID）；"
                "生产只读 probe 前后回执；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/T-1789392878852-bb9bef18-inventory.md",
                    "target_symbol": "F1 缺陷复核与同型扫描（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 复核 admin_handlers.rs gc_audit_get/gc_audit_list 的 SQL 原文"
                        "与行号（行号须实测，严禁照抄本卡/backlog 的行号），确认引用 "
                        "tasks.workspace_id 的两处（get 的子查询 / list 的 JOIN+WHERE）",
                        "PRAGMA 实证：tasks 表无 workspace_id 列；task_workspace_bindings 有 "
                        "(task_id, workspace_id) 权威映射；引用 task_collab_shared.rs "
                        "task_bound_workspace_id 的权威语义注释作依据",
                        "复核 tests/test_c17_admin_route_workspace_authority.py F1 锚"
                        "（test_gc_audit_get_f1_known_defect_still_fails）的断言原文与行号，"
                        "确认迁移范围（仅 F1 锚，F2/F3 锚本卡禁碰——属 C-20）",
                        "同型扫描：grep 全部 rust_ext/src 的 tasks.workspace_id 引用"
                        "（确认仅此两处）；grep db/ 与 server/ 的 tasks.workspace_id"
                        "（Python db_gc.py gc_audit_* 走 gc_runs 表，属另一套审计概念，"
                        "留排除依据防混淆）；扫描 change_audit 的写入路径（cli/task.rs INSERT）"
                        "确认只读面修复不影响写入",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "gc_audit_get/gc_audit_list binding 表作用域修复",
                    "check_items": [
                        "gc_audit_get：task_id IN 子查询改 "
                        "(SELECT task_id FROM task_workspace_bindings WHERE workspace_id = ?2)",
                        "gc_audit_list：JOIN tasks t 改 JOIN task_workspace_bindings b "
                        "ON b.task_id = ca.task_id，WHERE b.workspace_id = ?1",
                        "两 handler 的签名/返回结构/参数校验不变；不改路由层 snapshot_state.rs、"
                        "不改其它 admin handler；不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c17_admin_route_workspace_authority.py",
                    "target_symbol": "F1 锚迁移为正例 + workspace 隔离负例",
                    "check_items": [
                        "夹具预置 change_audit 域夹具：绑定 task（TASK_C17）1 行 + "
                        "unbound task 1 行 + 跨 workspace task（workspace_id=其它 id）1 行",
                        "test_gc_audit_get_f1_known_defect_still_fails 迁移为正确行为正例："
                        "gc_audit_get(audit_id=绑定行) 命中且返回结构完整"
                        "（id/task_id/file_path/hash_before/hash_after/diff/author/timestamp）",
                        "新增 gc_audit_list 隔离矩阵：只返回绑定 task 行；unbound 与跨 workspace "
                        "行不可见；limit 语义保持",
                        "F2/F3 known-defect 锚保持原样且仍失败（本卡不修的回归锚不许动）",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "cargo 构建/测试 + 隔离 daemon 全矩阵",
                    "check_items": [
                        "cargo build 零 error（隔离 CARGO_TARGET_DIR）；cargo test 与基线同集对照"
                        "零新增失败",
                        "隔离 daemon 矩阵：tests/test_c17_admin_route_workspace_authority.py "
                        "全文件 pytest 全绿（F1 迁移正例 + 隔离负例 + ACL 门禁不受影响）；"
                        "记录二进制 sha256",
                        "修复前基线同集对照：CW_DAEMON_BIN 指向旧二进制时 F1 锚如实失败"
                        "（no such column），证明判别力；对照输出留证据",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "产品代码部署门禁 + 生产只读 probe",
                    "check_items": [
                        "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                        "-TaskId 本卡 task_id；核对 health.git_commit==HEAD、运行中二进制 sha256 "
                        "与构建产物一致、PID 记录、rollback=false",
                        "部署后生产只读 probe：admin.gc_audit_get/list 返回真实行"
                        "（生产 change_audit 3620 行中 1722 行属 workspace 1 绑定 task）；"
                        "修复前同 probe 恒 prepare 失败（no such column）留对照",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用卡 D/C-17 或 C-18 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
        {
            "title": "C-20 承接：admin.record_action_identity/register_attestation_revocation 缺 NOT NULL UNIQUE 列修复（§W20 F2+F3）",
            "findings": "C-20",
            "origin": (
                "卡 D `T-1789365537230-c3f02eb4`（C-17）盘点实测出界新发现"
                "（backlog §W20 F2+F3，合并一卡：同文件 admin_handlers.rs、同缺陷形态）："
                "`admin.record_action_identity` 的 INSERT 缺 `action_identities.action_id`"
                "（schema.py:1549 `action_id TEXT NOT NULL UNIQUE`，语义注释 `ACT-<uuid>`）→ "
                "恒 NOT NULL constraint failed；`admin.register_attestation_revocation` 的 "
                "INSERT 缺 `attestation_revocation_records.revocation_id`（schema.py:1592 "
                "`revocation_id TEXT NOT NULL UNIQUE`，语义注释 `REV-<uuid>`）→ 同型恒失败。"
                "C-17 测试文件以 F2/F3 known-defect 锚（test_record_action_identity_f2_"
                "known_defect_still_fails / test_register_attestation_revocation_f3_"
                "known_defect_still_fails）冻结形态，「转绿须由独立缺陷卡承接」—— 即本卡。"
                "修法对齐卡 A（T-1789340885245-071cb9b4）`gen_assignment_id()` 先例"
                "（admin_handlers.rs，`ASG-<hex16>` 经 entropy crate，Rust 侧无 uuid crate）"
                "生成 `ACT-`/`REV-` 前缀 id 入列"
            ),
            "backlog_section": "§W20",
            "executor_allowed": [
                "rust_ext/src/daemon/admin_handlers.rs",
                "tests/test_c17_admin_route_workspace_authority.py",
                "deliverables/software-company/",
                "cw_task_commit_ledger.json",
                "docs/evidence/",
            ],
            "acceptance": (
                "record_action_identity 与 register_attestation_revocation 的 INSERT 补齐 "
                "action_id / revocation_id 列，id 由 Rust 侧生成（`ACT-<hex16>`/`REV-<hex16>`，"
                "对齐 gen_assignment_id 先例：entropy crate、每次调用新生成）；"
                "表 schema（db/**）零触碰；C-17 F2/F3 known-defect 锚迁移为正确行为正例"
                "（隔离 daemon 调用成功 + 落库行带前缀 id + 字段读回完整 + 重复调用生成不同 id）；"
                "F1（C-19 已迁移的）正例与隔离负例保持全绿不受影响；其余 admin handler 零触碰；"
                "cargo build 零 error；cargo test 零新增失败（同集对照）；"
                "产品代码改动 → 部署门禁必须走：refresh_shared_runtime.ps1 -TaskId 本卡 task_id、"
                "health.git_commit==HEAD、三方 sha256 一致、PID 记录、rollback=false；"
                "生产核查为只读（写路径缺陷不做生产合成写 probe，须显式披露并以部署同 sha "
                "二进制的隔离矩阵为功能证明）；git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：本 HEAD 两 INSERT 原文与实测行号、PRAGMA/schema.py 两表"
                " NOT NULL UNIQUE 列实证（含 ACT-<uuid>/REV-<uuid> 语义注释引用）、"
                "gen_assignment_id() 先例原文、全部 rust_ext INSERT 缺 NOT NULL UNIQUE 列"
                " 同型扫描结论、F2/F3 锚断言原文复核；修复 diff 说明；"
                "隔离 daemon 前后实测：before（NOT NULL constraint failed）/ after"
                "（成功 + ACT-/REV- 前缀行读回）；cargo build/test 输出；"
                "部署回执（refresh_shared_runtime 三方哈希 + PID）；生产只读核查回执"
                "（两表行数与最新行 id 形态）与「未做生产写 probe」披露；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/c20_remediation_inventory.md",
                    "target_symbol": "F2+F3 缺陷复核与同型扫描（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 复核 admin_handlers.rs record_action_identity / "
                        "register_attestation_revocation 两 INSERT 的原文与实测行号"
                        "（行号须实测，严禁照抄本卡/backlog 的行号），确认缺列位置",
                        "schema 实证：db/schema.py action_identities.action_id TEXT NOT NULL "
                        "UNIQUE（注释 ACT-<uuid>）、attestation_revocation_records.revocation_id "
                        "TEXT NOT NULL UNIQUE（注释 REV-<uuid>）；db/** 本卡零改动",
                        "复核卡 A 先例 gen_assignment_id()（admin_handlers.rs，ASG-<hex16> "
                        "entropy crate 生成）原文，作为 id 生成范式锚",
                        "复核 tests/test_c17_admin_route_workspace_authority.py F2/F3 锚"
                        "（test_record_action_identity_f2_known_defect_still_fails / "
                        "test_register_attestation_revocation_f3_known_defect_still_fails）"
                        "断言原文与行号，确认迁移范围（仅 F2/F3，F1 已由 C-19 迁移的正例禁碰）",
                        "同型扫描：grep rust_ext/src 全部 INSERT，对照 schema 找其它缺 "
                        "NOT NULL UNIQUE 列的写入（超出 F2/F3 的只登记不修）",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "ACT-/REV- id 生成 + 两 INSERT 补列",
                    "check_items": [
                        "对齐 gen_assignment_id() 范式新增/复用 id 生成：ACT-<hex16> 与 "
                        "REV-<hex16>（entropy crate，每次调用新生成，无 uuid crate 依赖）",
                        "record_action_identity INSERT 补 action_id 列；"
                        "register_attestation_revocation INSERT 补 revocation_id 列；"
                        "两 handler 签名/参数校验/返回结构不变；不改其它 admin handler、"
                        "不改路由层 snapshot_state.rs、不改 db/**、不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c17_admin_route_workspace_authority.py",
                    "target_symbol": "F2/F3 锚迁移为正例（隔离 daemon）",
                    "check_items": [
                        "F2 锚迁移：隔离 daemon 调 admin.record_action_identity 成功；"
                        "只读核查 action_identities 落库行 action_id 以 ACT- 前缀且全字段读回"
                        "（action_type/task_id/agent_id/session_id/model_id/role/recorded_at）；"
                        "重复调用生成不同 action_id（UNIQUE 不撞）",
                        "F3 锚迁移：隔离 daemon 调 admin.register_attestation_revocation 成功；"
                        "落库行 revocation_id 以 REV- 前缀且全字段读回（issuer/signing_key_id/"
                        "revocation_mode/revocation_reason/initiating_actor/revoked_at）",
                        "F1 已迁移正例与隔离负例（C-19 交付）保持全绿；F1/F2/F3 锚注释段更新",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "cargo 构建/测试 + A/B 判别力对照",
                    "check_items": [
                        "cargo build 零 error（隔离 CARGO_TARGET_DIR）；cargo test 与基线同集对照"
                        "零新增失败",
                        "tests/test_c17_admin_route_workspace_authority.py 全文件 pytest 全绿"
                        "（F1 正例/负例 + F2/F3 迁移正例 + ACL 门禁不受影响）；记录二进制 sha256",
                        "修复前基线同集对照：CW_DAEMON_BIN 指向修复前二进制时 F2/F3 锚如实失败"
                        "（NOT NULL constraint failed），证明判别力；对照输出留证据",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/admin_handlers.rs",
                    "target_symbol": "产品代码部署门禁 + 生产只读核查（披露无合成写 probe）",
                    "check_items": [
                        "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                        "-TaskId 本卡 task_id；核对 health.git_commit==HEAD、运行中二进制 sha256 "
                        "与构建产物一致、PID 记录、rollback=false",
                        "生产只读核查（禁合成写）：SELECT 行数与最新行 id 形态对照；显式披露"
                        "「写路径缺陷不做生产合成写 probe，功能证明=部署同 sha 二进制的隔离矩阵」；"
                        "本卡自身 A′ 治理写在部署后新产生的 identity 行可作为自然观测点（如有）",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用卡 D/C-17、C-18 或 C-19 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
        {
            "title": "C-21 承接：edit.*/gate.*/rule.* 第二路由块代理 workspace id 缺陷收口（§W20 F4）",
            "findings": "C-21",
            "origin": (
                "卡 D `T-1789365537230-c3f02eb4`（C-17）盘点范围仅圈「admin 路由块」，"
                "修复期实测出界新发现（backlog §W20 F4）：snapshot_state.rs 方法匹配臂中的"
                "第二路由块（edit.propose/edit.propose_range_patch/edit.propose_symbol_id_patch/"
                "edit.propose_symbol_patch/edit.revert/edit.restore_all_comments/"
                "edit.restore_comment/edit.record_token_savings/gate.resolve_findings/"
                "gate.run_check/rule.seed_bootstrap/rule.extract_candidates/"
                "rule.candidate_accept/rule.candidate_create/rule.candidate_reject/"
                "rule.insert_agents_md_block/rule.sync_agents_md/guardrail.add_rule/"
                "summary.generate，2026-09-14 实测 19 方法，行号 3262 邻域——执行期须实测为准）"
                "与 admin 块同构：owned_workspace() 的 registry 代理 ROWID 作 handler "
                "workspace 作用域 + open_write 物理库，与物理库 workspaces.id 不同命名空间"
                "（同 C-17 根因）。修法同款：路由层单点改 open_codegraph_db_write（真 id 解析 + "
                "ACL 保留），handler 层零改动；紧邻 semgrep 写面块（C-13 接线）已是正确形态锚"
            ),
            "backlog_section": "§W20",
            "executor_allowed": [
                "rust_ext/src/daemon/snapshot_state.rs",
                "tests/test_c21_edit_rule_route_workspace_authority.py",
                "deliverables/software-company/",
                "cw_task_commit_ledger.json",
                "docs/evidence/",
            ],
            "acceptance": (
                "第二路由块的路由层单点收口：owned_workspace 代理 ROWID + open_write 改为 "
                "open_codegraph_db_write(peer, ws) 取（真 workspace_id, conn），与紧邻 semgrep "
                "块形态一致；方法名列表/match 臂与全部 handler 零改动；admin 路由块与其它路由块"
                "零触碰；db/** 零触碰；新隔离矩阵 tests/test_c21_edit_rule_route_workspace_"
                "authority.py：registry dummy 行占 ROWID=1 复现「代理≠真 id」，写面正例"
                "（至少 edit.propose/edit.revert/rule.candidate_create/guardrail.add_rule）落"
                "物理库真 id 作用域且可读回 + 跨 workspace 隔离负例；cargo build 零 error；"
                "cargo test 零新增失败（同集对照）；产品代码改动 → 部署门禁必须走："
                "refresh_shared_runtime.ps1 -TaskId 本卡 task_id、health.git_commit==HEAD、"
                "三方 sha256 一致、PID 记录、rollback=false；生产核查为只读（无自然写触发时"
                "显式披露，功能证明=隔离矩阵）；git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 盘点文档：本 HEAD 第二路由块逐方法清单（方法名+实测行号+读写性质+"
                "缺陷形态判定）、紧邻 semgrep 块正确形态对照、第三路由块同型扫描结论、"
                "handler 对 conn 用法复核（零 handler 改动可行性）；修复 diff 说明；"
                "隔离 daemon 矩阵输出（写面正例读回 + 隔离负例 + 代理≠真 id 复现说明）；"
                "cargo build/test 输出；A/B 判别力对照（旧二进制同集如实失败）；"
                "部署回执（三方哈希 + PID）；生产只读核查回执与披露；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/c21_remediation_inventory.md",
                    "target_symbol": "第二路由块逐方法盘点（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 枚举第二路由块全部方法：方法名逐字清单 + 实测行号 + "
                        "读写性质（严禁照抄本卡/backlog 的行号与方法数，以 HEAD 实测为准）",
                        "对照 C-17 admin 块收口形态与紧邻 semgrep 块 open_codegraph_db_write "
                        "正确形态，逐方法判定缺陷（代理 id 来源 + open_write 连接）",
                        "同型扫描：grep snapshot_state.rs 其余 owned_workspace( + open_write "
                        "组合，确认是否存在第三路由块（只登记，不属本卡 scope）",
                        "复核 edit/rule handler 对 conn 的用法（确认路由层单点收口下 "
                        "handler 层零改动可行）",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "第二路由块单点 open_codegraph_db_write 收口",
                    "check_items": [
                        "第二路由块改 open_codegraph_db_write(peer, ws) 取（真 workspace_id, "
                        "conn)，删除 owned_workspace 代理 id 提取；方法名列表/match 臂不动",
                        "handler 层零改动；admin 路由块/semgrep 块/其它路由块零触碰；"
                        "不改 db/**；不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c21_edit_rule_route_workspace_authority.py",
                    "target_symbol": "新隔离矩阵：写面正例 + 跨 workspace 负例",
                    "check_items": [
                        "夹具复用 C-17 范式：registry 先插 dummy 行占 ROWID=1 复现「代理≠真 id」；"
                        "USERPROFILE 隔离；HTTP 合成 peer uid=4294967295（Windows）",
                        "写面正例（至少 edit.propose / edit.revert / rule.candidate_create / "
                        "guardrail.add_rule 各 1）：调用成功且落物理库真 id 作用域，只读读回校验",
                        "隔离负例：跨 workspace 调用不可见/不命中；ACL 门禁行为不受影响",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "cargo 构建/测试 + A/B 判别力对照",
                    "check_items": [
                        "cargo build 零 error（隔离 CARGO_TARGET_DIR）；cargo test（含 "
                        "task_collab lib 套件）与基线同集对照零新增失败",
                        "tests/test_c21_edit_rule_route_workspace_authority.py pytest 全绿；"
                        "C-17 测试文件（admin 块）全绿不受影响；记录二进制 sha256",
                        "修复前基线同集对照：CW_DAEMON_BIN 指向修复前二进制时新矩阵如实失败，"
                        "证明判别力；对照输出留证据",
                    ],
                },
                {
                    "action": "release_verify",
                    "target_file": "rust_ext/src/daemon/snapshot_state.rs",
                    "target_symbol": "产品代码部署门禁 + 生产只读核查",
                    "check_items": [
                        "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                        "-TaskId 本卡 task_id；核对 health.git_commit==HEAD、运行中二进制 sha256 "
                        "与构建产物一致、PID 记录、rollback=false",
                        "生产只读核查：读型臂（step0 盘点出的只读方法）前后对照 + 行数/id 形态；"
                        "无自然写触发时显式披露「生产无合成写 probe，功能证明=隔离矩阵」",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 C-17/C-18/C-19/C-20 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
        {
            "title": "C-22 承接：C 桶建卡模板 step target_file 目录级白名单与 changes[] 全等比对不兼容修复（§W20 F5）",
            "findings": "C-22",
            "origin": (
                "卡 D `T-1789365537230-c3f02eb4`（C-17）step2/step3 实测出界新发现"
                "（backlog §W20 F5）：daemon 侧 `changes[].file_path` 与 `task_steps.target_file` "
                "做全等比对（`;`/`,` 拆分、`\\\\`→`/` 归一），目录级 step target_file"
                "（`tests/`、`rust_ext`、`deliverables/software-company/` 等）永远无法全等命中 → "
                "报 `E_CHANGE_PATH_NOT_ALLOWED`，目录白名单下的 step 只能 `changes=[]` + "
                "evidence/commit 留痕（C-13 卡先例 §4.1 同源；卡 D 实测）。后果：changes 入账"
                "缺失、可审计性降级。修法（本卡 scope=模板侧）：建卡脚本 build_cards() 全部 "
                "step target_file 改文件级（「文档尚未存在」的 step0 类场景用预声明确定性文件名，"
                "如 cNN_remediation_inventory.md；多文件用 `;` 连接），并以校验测试固化防回归；"
                "C-20/C-21 卡已按此范式建卡（第五次追加起生效）。daemon 侧目录前缀匹配为可选项，"
                "不属本卡 scope（若未来需要另立产品代码卡）"
            ),
            "backlog_section": "§W20",
            "executor_allowed": [
                "deliverables/software-company/create_c_bucket_remediation_tasks.py",
                "tests/test_c_bucket_template_target_file_whitelist.py",
                "deliverables/software-company/",
                "cw_task_commit_ledger.json",
                "docs/evidence/",
            ],
            "acceptance": (
                "建卡脚本 build_cards() 全部卡的全部 step target_file 为文件级 token"
                "（无目录级条目；多文件用 `;` 连接；step0 类预声明确定性文件名）；"
                "docstring 记录 F5 教训与 target_file（文件级全等）vs executor_allowed"
                "（合同 allowed_paths，可目录前缀）的语义区分；新增校验测试 "
                "tests/test_c_bucket_template_target_file_whitelist.py 固化上述不变量；"
                "幂等范式不破坏（既有卡标题判重 → exists，零重复创建）；"
                "tests/deliverables-only：无产品代码改动 → 不触发部署门禁，须显式披露；"
                "不改 daemon/db/**；git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：daemon 全等比对语义的代码依据（校验点 grep + 引用）、"
                "现存模板全部 step target_file 值盘点（标记目录级条目）、预声明命名约定说明；"
                "修复 diff 说明；校验测试输出；幂等回归回执（跑脚本全量 exists 零 created，"
                "CW_DAEMON_ENDPOINT 须显式指向当前 daemon）；「未部署」披露；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/c22_remediation_inventory.md",
                    "target_symbol": "全等比对语义复核 + 模板盘点（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 复核 daemon changes[].file_path 与 task_steps.target_file "
                        "全等比对的校验点（grep 实测位置 + 归一规则 `;`/`,` 拆分、`\\\\`→`/`），"
                        "引用 C-17/C-13 卡 E_CHANGE_PATH_NOT_ALLOWED 实测作证",
                        "盘点 build_cards() 现存全部卡的 step target_file 值，标记目录级条目"
                        "（含 step0 类「文档尚未存在」场景）",
                        "确认预声明命名约定：task_id 建卡前未知 → step0 文档用确定性前缀名"
                        "（如 cNN_remediation_inventory.md），实现文件级白名单先于文件存在",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": "deliverables/software-company/create_c_bucket_remediation_tasks.py",
                    "target_symbol": "模板 step target_file 全量改文件级",
                    "check_items": [
                        "全部卡的全部 step target_file 改文件级 token（`;` 连接多文件；"
                        "step0 类用预声明确定性文件名）；存量卡已在 daemon 侧创建，"
                        "改 dict 不影响已建任务（仅影响未来重建/参考），须在注释中说明",
                        "docstring 补 F5 教训注记：step target_file（文件级全等）vs "
                        "executor_allowed（合同 allowed_paths，可目录前缀）语义区分",
                        "不改 daemon、不改 db/**、不改任何测试夹具；不得直写 SQLite",
                    ],
                },
                {
                    "action": "test",
                    "target_file": "tests/test_c_bucket_template_target_file_whitelist.py",
                    "target_symbol": "新增校验测试固化文件级不变量",
                    "check_items": [
                        "import build_cards()：断言每张卡每步 target_file 无目录级 token"
                        "（不以 `/` 结尾、非现存目录路径）；多文件 token 可按 `;`/`,` 拆为"
                        "具体文件路径",
                        "断言 docstring 含 F5 教训注记（防回退）；断言 C-20/C-21/C-22 三张新卡"
                        "亦满足（预声明文件名在列）",
                        "pytest 全绿（.venv_test 解释器）",
                    ],
                },
                {
                    "action": "verify",
                    "target_file": "deliverables/software-company/create_c_bucket_remediation_tasks.py",
                    "target_symbol": "幂等回归 + 未部署披露",
                    "check_items": [
                        "跑建卡脚本全量（CW_DAEMON_ENDPOINT 显式指向当前 daemon，以 "
                        "cw daemon health 回执为准）：全部既有标题 → exists、created=0，"
                        "证明重构未破坏幂等范式；回执留证据",
                        "本卡 tests+deliverables-only：无产品代码改动 → 不触发部署门禁，"
                        "须在证据中显式披露「未部署及原因」",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 C-17..C-21 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
    ]


def main() -> None:
    # 显式 endpoint + 关闭 manifest 校验/健康交叉核对：避免命中真实 HOME 的 stale
    # manifest（E_HTTP_MANIFEST_STALE）。显式 loopback endpoint 本身即合法发现路径。
    client = HttpDaemonRpcClient(
        endpoint=ENDPOINT, verify_health=False, validate_manifest=False,
    )

    listing = client.call("task.list", {
        "parent_id": PARENT_ID, "status": "", "limit": 200,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
    })
    existing = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    titles = {
        item.get("title") for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, dict)
    }

    results = []
    for card in build_cards():
        if card["title"] in titles:
            results.append({"result": "exists", "title": card["title"]})
            continue
        description = (
            f"承接来源：{card.get('origin', f'PYT 回归卡 `{PARENT_ID}` step#4（tests-only 边界）')}。"
            f"登记 finding：{card['findings']}。\n\n"
            f"权威 finding 明细见 `deliverables/software-company/"
            f"pyt_regression_step4_handoff_backlog.md` {card.get('backlog_section', '§W13/W14/W15')}"
            f"；C-16/C-17 另见 `deliverables/software-company/"
            f"c16_c17_remediation_handoff_20260914.md`（自包含工单）。\n\n"
            f"合同边界：仅修本卡 allowed_paths 内缺陷文件；不得改动 `db/**`、"
            f"`scripts/refresh_shared_runtime.ps1`；不直接写 SQLite；提交前缀必须使用"
            f"本卡自身 task_id（严禁复用 PYT 卡 `[T-1788871227327-45c94bd8]` 或"
            f"卡 A `[T-1789340885245-071cb9b4]`）。\n\n"
            f"验收：{card['acceptance']}\n证据：{card['evidence']}\n"
        )
        response = client.call("task.create", {
            "title": card["title"],
            "description": description,
            "parent_id": PARENT_ID,
            "workspace_id": WORKSPACE_ID,
            "workspace_instance_id": WORKSPACE_INSTANCE_ID,
            "identity_policy": "legacy_identity_v1",
            "steps": card["steps"],
            "role_contracts": contracts(
                card["executor_allowed"], card["acceptance"], card["evidence"]
            ),
        })
        results.append({
            "result": "created",
            "title": card["title"],
            "findings": card["findings"],
            "task_id": response.get("task_id") if isinstance(response, dict) else None,
            "governance_projection": (
                response.get("governance_projection") if isinstance(response, dict) else None
            ),
        })
    print(json.dumps({"parent_id": PARENT_ID, "cards": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
