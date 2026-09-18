"""经 daemon authority 创建 NF1/NF2 独立缺陷卡（C-21 修复期新发现登记的承接卡）。

背景：C-21 `T-1789397153231-f07a8d84`（§W20 F4 第二路由块收口）修复/实测期新发现两个
独立生产缺陷（登记见 `pyt_regression_step4_handoff_backlog.md` §W20 F4 条目「新发现登记」）：
  - NF1（P1）：`gate.resolve_findings` 的 UPDATE SQL 引用 tasks 表不存在的列 workspace_id
    （与 §W20 F1 同族，C-19 修复 admin_handlers.rs 两处时未覆盖此处）→ 恒 prepare 失败。
  - NF2（P1）：`summary.generate` 的 INSERT ... ON CONFLICT(symbol_hash) DO UPDATE
    在权威 schema 无匹配 UNIQUE(symbol_hash) 约束（symbol_summaries 仅两个非唯一索引）→
    恒运行时错误。曾被路由缺陷双重掩盖（C-21 修复前 handler 不可达），自上线从未端到端可用。

两者均以 xfail(strict) 正例体锚冻结于 C-21 卡矩阵
（tests/test_c21_edit_rule_route_workspace_authority.py），「转绿须由独立缺陷卡承接」——
即本脚本创建的两张卡。

NF2 修复方向裁决要点（step0 复核后正式落定）：
  symbol_summaries 的设计语义是版本化多行（db/db_base.py `_migrate_v5_to_v6` docstring
  「同一符号可保留多版本历史摘要」；Python 侧 db/db_summary.py `generate_summary` 用
  UPDATE is_current=0 → version=MAX+1 → INSERT 事务）。修法 = 把 Rust handler 重写为
  与 Python 侧同源的版本化语义，**不是**加 UNIQUE 约束（全列 UNIQUE 会破坏版本化设计与
  Python 侧实现）。同文件 job_runner.rs 的 ON CONFLICT(symbol_hash)（symbol_embeddings）
  合法（symbol_hash 是 PRIMARY KEY），不在范围。

NF1 修复方向（C-19 先例直接套用）：
  SQL 子查询 `task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4)` 改
  `task_id IN (SELECT task_id FROM task_workspace_bindings WHERE workspace_id = ?4)`
  （与 admin_handlers.rs handle_gc_audit_get 同款；task_workspace_bindings 为权威
  workspace 映射，task_collab_shared.rs::task_bound_workspace_id 同源语义）。

建卡纪律（F5 教训落实）：本脚本全部 step target_file 为文件级 token（多文件 `;` 连接；
「文档尚未存在」的 step0 类场景用预声明确定性文件名 nf1/nf2_remediation_inventory.md）；
executor_allowed 为合同 allowed_paths，可目录前缀，不参与 changes 全等比对。

两张卡均为产品代码卡（落点 rust_ext/src/daemon/edit_handlers.rs）→ 必须走部署门禁。

用法（先确保 daemon 就绪；端点以 `cw daemon health` 回执为准，可用环境变量覆盖）：
    CW_DAEMON_ENDPOINT=http://127.0.0.1:<port> \
    python deliverables/software-company/create_nf_defect_cards.py
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

# 承接来源：PYT 回归父卡（C 桶承接卡与 NF 缺陷卡同挂此父卡）
PARENT_ID = "T-1788871227327-45c94bd8"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
ENDPOINT = os.environ.get("CW_DAEMON_ENDPOINT", "http://127.0.0.1:8535")

TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"

# C-21 卡矩阵（NF1/NF2 xfail 锚所在文件）与共同落点
EDIT_HANDLERS = "rust_ext/src/daemon/edit_handlers.rs"
C21_TEST_FILE = "tests/test_c21_edit_rule_route_workspace_authority.py"


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
            "allowed_paths": "governance records, verdicts, ledger and projection read-only",
            "forbidden_paths": "source edits; direct database writes",
            "commands": "adjudication",
            "acceptance_checks": (
                "verdict grounded in reviewer evidence; apply/close only after acceptance"
            ),
            "required_evidence": "ACCEPT or REJECT verdict record",
            "handoff_to": "", "independence": "not_applicable",
        },
    ]


def build_cards() -> list[dict]:
    """NF1/NF2 两张卡（产品代码卡，部署门禁必走）。"""
    executor_allowed = [
        EDIT_HANDLERS,
        C21_TEST_FILE,
        "deliverables/software-company/",
        "cw_task_commit_ledger.json",
        "docs/evidence/",
    ]
    return [
        {
            "title": (
                "NF1 承接：gate.resolve_findings SQL 引用不存在列 tasks.workspace_id 修复"
                "（§W20 F1 同族，C-21 期新发现）"
            ),
            "findings": "NF1",
            "origin": (
                "C-21 `T-1789397153231-f07a8d84`（§W20 F4 第二路由块收口）修复期新发现"
                "（backlog §W20 F4 条目「新发现登记」）：edit_handlers.rs "
                "handle_resolve_gate_findings 的 UPDATE SQL "
                "`WHERE decision_id = ?3 AND task_id IN (SELECT id FROM tasks WHERE "
                "workspace_id = ?4)` 引用 tasks 表不存在的列 workspace_id（C-19 修复期已 "
                "PRAGMA 实证 tasks 无此列）→ 恒 prepare 失败。与 §W20 F1 同族（列不存在型），"
                "C-19 修复 admin_handlers.rs gc_audit_get/list 两处时未覆盖此处。"
                "C-21 测试文件以 xfail(strict) 正例体锚 "
                "test_gate_resolve_findings_nf1_known_defect 冻结形态，「转绿须由独立缺陷卡"
                "承接」—— 即本卡。修法 = C-19 先例直接套用：子查询改 task_workspace_bindings"
                "（task_collab_shared.rs::task_bound_workspace_id 权威语义同源）"
            ),
            "backlog_section": "§W20",
            "executor_allowed": executor_allowed,
            "acceptance": (
                "resolve_findings 的 SQL 不再引用 tasks.workspace_id，改经 "
                "task_workspace_bindings 子查询作用域（与 C-19 handle_gc_audit_get 修复"
                "同款、task_bound_workspace_id 权威语义同源）；handler 其余逻辑与 "
                "changed==0 → gate_not_found 分支语义零改动；C-21 NF1 xfail(strict) 锚"
                "迁移为正确行为正例（删除标记保留断言），NF2 锚本卡禁碰（属 NF2 卡）；"
                "路由层（dispatch.rs / snapshot_state.rs）零触碰（C-21 已修路由臂）；"
                "cargo build 零 error；cargo test 零新增失败（同集对照）；"
                "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                "-TaskId 本卡 task_id、health.git_commit==HEAD、三方 sha256 一致、"
                "PID 记录、rollback=false；部署后生产只读 probe 回执"
                "（修复前恒 prepare 失败 → 部署后返回结构化响应）；"
                "git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：本 HEAD handle_resolve_gate_findings SQL 原文与行号"
                "（行号须实测，严禁照抄本卡/backlog 的行号）、PRAGMA tasks 列清单实证、"
                "task_workspace_bindings 权威映射依据（task_collab_shared.rs 注释引用）、"
                "C-19 handle_gc_audit_get 修复先例对照、C-21 NF1 锚断言原文复核、"
                "rust_ext/src 全部 tasks.workspace_id 引用同型扫描结论（确认 NF1 唯一残留）；"
                "修复 diff 说明（前后对照）；隔离 daemon 前后实测：before（prepare 失败）"
                "/ after（结构化响应 + 边界负例）；22 测试隔离矩阵全绿回执"
                "（NF1 锚转绿、NF2 锚按其卡进度保持 xfail）；cargo build/test 输出；"
                "部署回执（refresh_shared_runtime 三方哈希 + PID + rollback=false）；"
                "生产只读 probe 前后回执；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/nf1_remediation_inventory.md",
                    "target_symbol": "F1 同族缺陷复核与同型扫描（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 复核 edit_handlers.rs handle_resolve_gate_findings 的 "
                        "SQL 原文与行号（行号须实测，严禁照抄本卡/backlog 的行号），确认 "
                        "task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4) 的坏列引用",
                        "PRAGMA 实证：tasks 表无 workspace_id 列；task_workspace_bindings 有 "
                        "(task_id, workspace_id) 权威映射；引用 C-19 卡修复先例"
                        "（admin_handlers.rs handle_gc_audit_get 同款子查询改法）作依据",
                        "复核 tests/test_c21_edit_rule_route_workspace_authority.py NF1 锚"
                        "（test_gate_resolve_findings_nf1_known_defect）的断言原文与行号，"
                        "确认迁移范围（仅 NF1 锚；NF2 锚本卡禁碰——属 NF2 卡）",
                        "同型扫描：grep rust_ext/src 全部 tasks.workspace_id 引用"
                        "（C-19 已修 admin_handlers.rs 两处，确认 NF1 为唯一残留）；"
                        "确认 gate.resolve_findings 的路由臂（dispatch.rs / "
                        "snapshot_state.rs）已在 C-21 修复，本卡零触碰路由层",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": EDIT_HANDLERS,
                    "target_symbol": "resolve_findings binding 表作用域修复",
                    "check_items": [
                        "SQL 子查询改 task_id IN (SELECT task_id FROM "
                        "task_workspace_bindings WHERE workspace_id = ?4)"
                        "（对齐 C-19 handle_gc_audit_get 先例）；不改动 handler 其余逻辑"
                        "与 changed==0 → gate_not_found 分支语义",
                        "不改路由层（dispatch.rs / snapshot_state.rs）、不改 db/**、"
                        "不得直写 SQLite",
                        "cargo build 零 error",
                    ],
                },
                {
                    "action": "test",
                    "target_file": C21_TEST_FILE,
                    "target_symbol": "NF1 xfail 锚迁移为正例",
                    "check_items": [
                        "删除 test_gate_resolve_findings_nf1_known_defect 的 xfail(strict) "
                        "标记（保留正例体断言 err is None），docstring 与文件头注记同步更新",
                        "22 测试隔离矩阵全绿：NF1 锚转绿；NF2 锚本卡禁碰（按 NF2 卡进度"
                        "保持 xfail 或已由 NF2 卡先行转绿）",
                        "pytest 全绿（.venv_test 解释器）；cargo test 零新增失败（同集对照）",
                    ],
                },
                {
                    "action": "verify",
                    "target_file": EDIT_HANDLERS,
                    "target_symbol": "部署门禁 + 生产只读 probe + 披露",
                    "check_items": [
                        "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                        "-TaskId 本卡 task_id；核对 health.git_commit==HEAD、运行中二进制 "
                        "sha256 与构建产物一致、PID 记录、rollback=false",
                        "生产只读核查：gate.resolve_findings 修复前恒 prepare 失败 → "
                        "部署后返回结构化响应（gate_not_found 或命中）；"
                        "无自然写触发时显式披露「生产无合成写 probe，功能证明=隔离矩阵」",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 C-19/C-21 的"
                        " task_id）；台账与治理文档分开提交",
                    ],
                },
            ],
        },
        {
            "title": (
                "NF2 承接：summary.generate upsert ON CONFLICT(symbol_hash) 无匹配 UNIQUE "
                "约束修复（版本化语义重写，C-21 期新发现）"
            ),
            "findings": "NF2",
            "origin": (
                "C-21 `T-1789397153231-f07a8d84` step2 A/B 实测新发现"
                "（backlog §W20 F4 条目「新发现登记」）：edit_handlers.rs "
                "handle_summary_generate 的 INSERT INTO symbol_summaries ... "
                "ON CONFLICT(symbol_hash) DO UPDATE 在权威 schema 无匹配 "
                "UNIQUE(symbol_hash) 约束（symbol_summaries 仅 idx_summaries_hash / "
                "idx_summaries_current 两个非唯一索引，PRAGMA index_list 实证）→ "
                "恒运行时错误。曾被路由缺陷双重掩盖（C-21 修复前第二路由块 handler "
                "不可达），自上线从未端到端可用。C-21 测试文件以 xfail(strict) 正例体锚 "
                "test_summary_generate_nf2_known_defect 冻结形态，「转绿须由独立缺陷卡"
                "承接」—— 即本卡。修复方向裁决：symbol_summaries 设计语义是版本化多行"
                "（db/db_base.py _migrate_v5_to_v6 docstring「同一符号可保留多版本历史"
                "摘要」；Python 侧 db/db_summary.py generate_summary 用 UPDATE "
                "is_current=0 → version=MAX+1 → INSERT 事务）→ 修法 = Rust handler "
                "重写为与 Python 侧同源的版本化语义，**不是**加 UNIQUE 约束（全列 "
                "UNIQUE 会破坏版本化设计与 Python 侧实现）。同文件 job_runner.rs 的 "
                "ON CONFLICT(symbol_hash)（symbol_embeddings）合法（symbol_hash 是 "
                "PRIMARY KEY），不在范围"
            ),
            "backlog_section": "§W20",
            "executor_allowed": executor_allowed,
            "acceptance": (
                "handle_summary_generate 不再使用 ON CONFLICT(symbol_hash) upsert，"
                "改版本化事务（UPDATE is_current=0 → version=MAX+1 → INSERT），与 "
                "db/db_summary.py generate_summary 同源语义；不新增 UNIQUE 约束、"
                "不改 schema（db/**）、不改路由层；同 hash 重复调用产生新版本行且旧行 "
                "is_current 翻转（版本化行为断言）；C-21 NF2 xfail(strict) 锚迁移为"
                "正确行为正例（删除标记保留断言），NF1 锚本卡禁碰（属 NF1 卡）；"
                "同型扫描确认 rust_ext/src 的 ON CONFLICT(symbol_hash) 仅此一处坏点"
                "（job_runner.rs symbol_embeddings 为 PRIMARY KEY 合法形态，留论证）；"
                "cargo build 零 error；cargo test 零新增失败（同集对照）；"
                "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                "-TaskId 本卡 task_id、health.git_commit==HEAD、三方 sha256 一致、"
                "PID 记录、rollback=false；部署后生产只读 probe 回执；"
                "git diff --check clean；commit 前缀用本卡 task_id"
            ),
            "evidence": (
                "step0 复核文档：本 HEAD handle_summary_generate SQL 原文与行号"
                "（行号须实测）、PRAGMA 实证（symbol_summaries 两非唯一索引 vs "
                "symbol_embeddings PRIMARY KEY 合法形态锚）、设计语义复核"
                "（db/db_base.py docstring + db/db_summary.py 事务先例）、修复方向裁决记录"
                "（重写为版本化语义 vs 加 UNIQUE 约束的取舍，含可选部分唯一索引 "
                "UNIQUE(symbol_hash) WHERE is_current=1 兜底评估）、"
                "C-21 NF2 锚断言原文复核、rust_ext/src 全部 ON CONFLICT(symbol_hash) "
                "同型扫描结论；修复 diff 说明（前后对照）；隔离 daemon 前后实测："
                "before（运行时错误）/ after（版本化写入 + 同 hash 二次调用新版本行 + "
                "is_current 翻转断言）；22 测试隔离矩阵全绿回执；cargo build/test 输出；"
                "部署回执（refresh_shared_runtime 三方哈希 + PID + rollback=false）；"
                "生产只读 probe 前后回执；git diff --check"
            ),
            "steps": [
                {
                    "action": "adjudicate",
                    "target_file": "deliverables/software-company/nf2_remediation_inventory.md",
                    "target_symbol": "upsert 缺陷复核与修复方向裁决（强制前置，不改代码）",
                    "check_items": [
                        "在本 HEAD 复核 edit_handlers.rs handle_summary_generate 的 SQL "
                        "原文与行号（行号须实测，严禁照抄本卡/backlog 的行号），确认 "
                        "INSERT ... ON CONFLICT(symbol_hash) DO UPDATE 的无匹配约束引用",
                        "PRAGMA 实证：symbol_summaries 仅 idx_summaries_hash / "
                        "idx_summaries_current 两个非唯一索引；对照 symbol_embeddings."
                        "symbol_hash 是 PRIMARY KEY（sqlite_autoindex pk 实证）—— "
                        "同写法在彼表合法的形态锚",
                        "设计语义复核与修复方向裁决：db/db_base.py _migrate_v5_to_v6 "
                        "docstring（版本化多行）+ db/db_summary.py generate_summary"
                        "（UPDATE is_current=0 → version 自增 → INSERT 事务）→ 裁决修法 ="
                        "重写 handler 为版本化语义，而非加 UNIQUE 约束（破坏 Python 侧"
                        "多版本实现）；评估可选兜底（部分唯一索引 UNIQUE(symbol_hash) "
                        "WHERE is_current=1）的价值与 ON CONFLICT 兼容性并在 inventory "
                        "记录裁决",
                        "复核 tests/test_c21_edit_rule_route_workspace_authority.py NF2 锚"
                        "（test_summary_generate_nf2_known_defect）的断言原文与行号，"
                        "确认迁移范围（仅 NF2 锚；NF1 锚本卡禁碰——属 NF1 卡）",
                        "同型扫描：grep rust_ext/src 全部 ON CONFLICT(symbol_hash)"
                        "（确认仅 edit_handlers.rs 一处坏点；job_runner.rs "
                        "symbol_embeddings 为 PRIMARY KEY 合法，留论证排除）",
                        "本 step 不改任何代码，产出落 deliverables 证据文档",
                    ],
                },
                {
                    "action": "implement",
                    "target_file": EDIT_HANDLERS,
                    "target_symbol": "summary.generate 版本化语义重写",
                    "check_items": [
                        "ON CONFLICT upsert 改版本化事务：UPDATE symbol_summaries SET "
                        "is_current = 0 WHERE symbol_hash = ? → SELECT MAX(version)+1 → "
                        "INSERT（对齐 db/db_summary.py generate_summary 同源语义）；"
                        "同 hash 重复调用产生新版本行、旧行 is_current 翻转",
                        "不新增 UNIQUE 约束、不改 schema（db/**）、不改路由层、"
                        "不得直写 SQLite；handler 其余查询逻辑（symbols JOIN "
                        "file_instances 作用域）零改动",
                        "cargo build 零 error",
                    ],
                },
                {
                    "action": "test",
                    "target_file": C21_TEST_FILE,
                    "target_symbol": "NF2 xfail 锚迁移为正例",
                    "check_items": [
                        "删除 test_summary_generate_nf2_known_defect 的 xfail(strict) "
                        "标记（保留正例体断言 err is None），docstring 与文件头注记同步更新",
                        "视夹具能力补版本化行为断言：同 hash 二次调用后 MAX(version) 自增、"
                        "恰一行 is_current=1",
                        "22 测试隔离矩阵全绿：NF2 锚转绿；NF1 锚本卡禁碰（按 NF1 卡进度"
                        "保持 xfail 或已由 NF1 卡先行转绿）",
                        "pytest 全绿（.venv_test 解释器）；cargo test 零新增失败（同集对照）",
                    ],
                },
                {
                    "action": "verify",
                    "target_file": EDIT_HANDLERS,
                    "target_symbol": "部署门禁 + 生产只读 probe + 披露",
                    "check_items": [
                        "产品代码改动 → 部署门禁必须走：scripts/refresh_shared_runtime.ps1 "
                        "-TaskId 本卡 task_id；核对 health.git_commit==HEAD、运行中二进制 "
                        "sha256 与构建产物一致、PID 记录、rollback=false",
                        "生产只读核查：summary.generate 修复前恒运行时错误 → 部署后返回"
                        "结构化响应（generated 计数）；无自然写触发时显式披露"
                        "「生产无合成写 probe，功能证明=隔离矩阵」",
                        "git diff --check clean",
                        "commit prefix uses THIS card task_id（严禁复用 C-21/NF1 的"
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
            f"承接来源：{card.get('origin', 'C-21 修复期新发现登记')}。"
            f"登记 finding：{card['findings']}。\n\n"
            f"权威 finding 明细见 `deliverables/software-company/"
            f"pyt_regression_step4_handoff_backlog.md` {card.get('backlog_section', '§W20')}"
            f" F4 条目「新发现登记」；NF1/NF2 xfail(strict) 正例体锚冻结于 "
            f"`{C21_TEST_FILE}`。\n\n"
            f"合同边界：仅修本卡 allowed_paths 内缺陷文件；不得改动 `db/**`、"
            f"`scripts/refresh_shared_runtime.ps1`；不直接写 SQLite；提交前缀必须使用"
            f"本卡自身 task_id（严禁复用父卡 `[T-1788871227327-45c94bd8]` 或"
            f"C-21 `[T-1789397153231-f07a8d84]` id）。\n\n"
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
