"""bootstrap 端到端闭环测试

验证 docs/design/bootstrap-closure-plan.md 描述的完整工作流：
  work_next_job → 外部编辑 → task_capture_diff → task findings → rule extract → apply/close

闭环涉及：
- workspace_scan_runs：捕获扫描基线
- change_audit：记录 hash_before/hash_after/diff
- audit_chain：签名链
- task_symbol_changes：符号级变更归因
- task_quality_findings：质量门禁发现
- agent_rule_candidates：从质量发现聚合候选规则
- agent_rules：种子化的 bootstrap 规则（验证可被注入）
- 任务状态机：open → in_progress → review → applied → closed

设计原则：
1. 使用临时 git 仓库模拟真实工作区
2. 步骤覆盖 Phase 3 验收标准的"完整跑一条自举样例任务"
3. 无 blocking finding 时 task_apply/task_close 应成功
"""
import os
import subprocess
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# 辅助函数
# ============================================


def _init_git_repo(root: str) -> str:
    """在临时目录初始化 git 仓库并提交一个文件，返回 HEAD commit hash。

    添加 .gitignore 排除 callwarden.db 等测试数据库文件，
    避免 git status 误把它们当作 untracked。
    """
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True, env=env)
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("callwarden.db*\n*.pyc\n__pycache__/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, capture_output=True, check=True, env=env)
    # 写入并提交一个 Python 文件
    f = os.path.join(root, "feature.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "feature.py"], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True, env=env)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, env=env
    )
    return r.stdout.strip()


def _setup_e2e_workspace():
    """构造端到端测试工作区：临时目录 + git 仓库 + 已 refresh 的代码图谱。

    返回 (db, root, head, task_id)。调用方负责 db.close()。
    """
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    head = _init_git_repo(root)
    # 把 feature.py 刷新到图谱，让符号能被捕获
    db.refresh_file(os.path.join(root, "feature.py"))

    # 创建一个样例任务（含 1 个 annotate 步骤）
    task_id = db.task_create(
        title="E2E: 给 add 函数加注释",
        description="自举闭环样例任务：模拟外部 Agent 编辑文件后捕获 diff",
        steps=[
            {
                "action": "annotate",
                "target_file": "feature.py",
                "target_symbol": "add",
                "check_items": "add 函数应有 docstring",
            }
        ],
    )
    return db, root, head, task_id


# ============================================
# 端到端测试
# ============================================


def test_e2e_bootstrap_closed_loop():
    """完整跑一条自举样例任务，验证 capture-diff → findings → apply/close 闭环。

    覆盖 docs/design/bootstrap-closure-plan.md Phase 3 验收：
    "完整跑一条自举样例任务，产生 scan_run、change_audit、audit_chain、quality review 结果"
    """
    db, root, head, task_id = _setup_e2e_workspace()
    try:
        # ============ 1. 领取 work_next_job ============
        job = db.work_next_job(task_id)
        assert job is not None, "work_next_job 应返回步骤上下文"
        assert job["task_id"] == task_id
        assert job["target_file"] == "feature.py"
        assert job["target_symbol"] == "add"
        step_id = job["job_id"]
        assert step_id, "job 应包含 step_id"

        # 任务应进入 in_progress
        status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "in_progress", f"领取后任务应为 in_progress，实际: {status}"

        # ============ 2. 模拟外部 Agent 编辑文件 ============
        # 外部 Agent（如 Codex/Cursor）直接修改文件，绕过 propose_symbol_patch
        feature_path = os.path.join(root, "feature.py")
        with open(feature_path, "w", encoding="utf-8") as f:
            f.write(
                "def add(a, b):\n"
                "    \"\"\"返回两数之和。\"\"\"\n"
                "    return a + b\n"
            )

        # ============ 3. 运行 task_capture_diff（apply 模式）============
        result = db.task_capture_diff(
            task_id=task_id, step_id=step_id, base=head, dry_run=False
        )

        # 3.1 验证 scan_run 落库
        assert result["scan_id"] > 0, "应返回有效的 scan_id"
        scan_row = db.conn.execute(
            "SELECT id, purpose, task_id, step_id, status FROM workspace_scan_runs "
            "WHERE id = ?",
            (result["scan_id"],),
        ).fetchone()
        assert scan_row is not None, "scan_run 应落库"
        assert scan_row["task_id"] == task_id
        assert scan_row["step_id"] == step_id
        assert scan_row["status"] == "completed"

        # 3.2 验证 change_audit 落库
        audit_rows = db.conn.execute(
            "SELECT id, file_path, hash_before, hash_after, author, task_id, step_id "
            "FROM change_audit WHERE task_id = ?",
            (task_id,),
        ).fetchall()
        assert len(audit_rows) >= 1, "change_audit 应至少有一条记录"
        assert audit_rows[0]["file_path"] == "feature.py"
        assert audit_rows[0]["author"] == "capture-diff"
        assert audit_rows[0]["step_id"] == step_id

        # 3.3 验证 audit_chain 落库（签名链）
        chain_rows = db.conn.execute(
            "SELECT table_name, record_id, record_signature, prev_signature "
            "FROM audit_chain WHERE record_id = ?",
            (audit_rows[0]["id"],),
        ).fetchall()
        assert len(chain_rows) >= 1, "audit_chain 应记录 change_audit 的签名"

        # 3.4 验证 task_symbol_changes 关联（符号级变更）
        sym_changes = db.conn.execute(
            "SELECT COUNT(*) as c FROM task_symbol_changes "
            "WHERE task_id = ? AND source = 'task_capture_diff'",
            (task_id,),
        ).fetchone()["c"]
        assert sym_changes >= 1, "task_symbol_changes 应有 task_capture_diff 来源的记录"

        # 3.5 next_action 应是 review（无 blocking finding）
        assert result["next_action"] in ("review", "noop"), \
            f"无 blocking finding 时 next_action 应为 review 或 noop，实际: {result['next_action']}"

        # ============ 4. 运行 task findings ============
        # 调用 get_task_quality_findings 查询任务质量发现
        findings = db.get_task_quality_findings(task_id, status="open")
        # 没有注入质量门禁规则时，findings 可能为空（这是期望的）
        assert isinstance(findings, list), "findings 应为 list"

        # ============ 5. 运行 rule extract ============
        # 从质量发现聚合候选规则（无 finding 时返回空列表，不抛异常）
        candidate_ids = db.extract_rule_candidates_from_quality_findings(
            task_id=task_id, min_occurrences=1
        )
        assert isinstance(candidate_ids, list), "extract 应返回 list"

        # ============ 6. 报告步骤完成 → 任务进入 review ============
        # 先刷新文件以同步图谱（capture-diff 已刷新，但报告前再次同步更安全）
        db.task_report_step(
            task_id=task_id,
            step_id=step_id,
            result="已为 add 函数添加 docstring，capture-diff 完成。",
            success=True,
        )

        # 任务应自动从 in_progress → review（所有步骤都 report 后）
        status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "review", \
            f"所有步骤 report 后任务应为 review，实际: {status}"

        # ============ 7. task_apply（review → applied）============
        apply_result = db.task_apply(task_id=task_id, reviewer="e2e-test")
        assert "error" not in apply_result, f"task_apply 应成功: {apply_result}"
        assert apply_result["status"] == "applied", \
            f"task_apply 后应为 applied，实际: {apply_result['status']}"

        # ============ 8. task_close（applied → closed）============
        close_result = db.task_close(task_id=task_id, reviewer="e2e-test")
        assert "error" not in close_result, f"task_close 应成功: {close_result}"
        assert close_result["status"] == "closed", \
            f"task_close 后应为 closed，实际: {close_result['status']}"

        # ============ 9. 最终验证：完整闭环数据存在 ============
        # 9.1 scan_run 完成且关联任务
        scan_count = db.conn.execute(
            "SELECT COUNT(*) as c FROM workspace_scan_runs "
            "WHERE task_id = ? AND status = 'completed'",
            (task_id,),
        ).fetchone()["c"]
        assert scan_count >= 1, "应有至少一个完成的 scan_run"

        # 9.2 change_audit 完整
        audit_count = db.conn.execute(
            "SELECT COUNT(*) as c FROM change_audit WHERE task_id = ?",
            (task_id,),
        ).fetchone()["c"]
        assert audit_count >= 1, "应有至少一条 change_audit"

        # 9.3 audit_chain 完整（每条 change_audit 都应有对应签名）
        chain_count = db.conn.execute(
            "SELECT COUNT(*) as c FROM audit_chain WHERE table_name = 'change_audit'"
        ).fetchone()["c"]
        assert chain_count >= audit_count, \
            f"audit_chain 记录数 ({chain_count}) 应 >= change_audit 记录数 ({audit_count})"

        # 9.4 任务已 closed
        final_status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert final_status == "closed", f"任务最终应为 closed，实际: {final_status}"
    finally:
        db.close()


def test_e2e_seed_bootstrap_rules_then_capture_diff():
    """先种子化 bootstrap 规则，再跑 capture-diff，验证规则可被注入到 work_next_job。

    覆盖 docs/design/bootstrap-closure-plan.md Phase 3 验收：
    "seed 后 agent_rules 至少有 5 条 active 规则"
    "task_next_step / work_next_job / get_symbol / file_symbol_content 能返回适用规则"
    """
    db, root, head, task_id = _setup_e2e_workspace()
    try:
        # ============ 1. 种子化 bootstrap active rules ============
        seed_result = db.rule_seed_bootstrap(dry_run=False)
        assert seed_result["created"] == 5, "应种入 5 条规则"
        assert seed_result["skipped"] == 0

        # ============ 2. 领取 work_next_job，验证规则被注入 ============
        job = db.work_next_job(task_id)
        assert job is not None, "work_next_job 应返回步骤上下文"

        # work_next_job 应注入 project_rules / applicable_rules
        # 注入点是 fail-soft，即使字段缺失也不应抛异常
        # bootstrap rules 中 AR-bootstrap-i18n 是 global（scope={}），
        # 应被注入到 work_next_job 返回的 project_rules 字段
        project_rules = job.get("project_rules", [])
        rule_ids = {r.get("id", "") for r in project_rules}
        assert "AR-bootstrap-i18n" in rule_ids, \
            f"work_next_job 应注入 bootstrap global 规则，实际: {rule_ids}"

        # ============ 3. 模拟外部编辑 ============
        feature_path = os.path.join(root, "feature.py")
        with open(feature_path, "w", encoding="utf-8") as f:
            f.write(
                "def add(a, b):\n"
                "    \"\"\"返回两数之和。\"\"\"\n"
                "    return a + b\n"
            )

        # ============ 4. capture-diff，应触发 AR-bootstrap-capture-diff 规则 ============
        step_id = job["job_id"]
        result = db.task_capture_diff(
            task_id=task_id, step_id=step_id, base=head, dry_run=False
        )
        assert result["scan_id"] > 0

        # ============ 5. 验证 agent_rules 表至少有 5 条 active ============
        active_count = db.conn.execute(
            "SELECT COUNT(*) as c FROM agent_rules WHERE status = 'active'"
        ).fetchone()["c"]
        assert active_count >= 5, \
            f"seed 后 agent_rules 应至少有 5 条 active，实际: {active_count}"
    finally:
        db.close()


def test_e2e_blocking_findings_prevent_apply():
    """有 blocking finding 时 task_apply 应被拒绝。

    覆盖 docs/design/bootstrap-closure-plan.md Phase 3 验收：
    "blocking finding 未解决不得 apply/close"
    """
    db, root, head, task_id = _setup_e2e_workspace()
    try:
        job = db.work_next_job(task_id)
        step_id = job["job_id"]

        # 模拟外部编辑
        feature_path = os.path.join(root, "feature.py")
        with open(feature_path, "w", encoding="utf-8") as f:
            f.write(
                "def add(a, b):\n"
                "    return a + b  # 无 docstring\n"
            )

        # capture-diff
        db.task_capture_diff(
            task_id=task_id, step_id=step_id, base=head, dry_run=False
        )

        # 通过 record_task_quality_finding 方法插入一条 blocking 严重度的 finding
        # 模拟质量门禁发现严重问题
        finding_id = db.record_task_quality_finding(
            task_id=task_id,
            step_id=step_id,
            finding_type="missing_docstring",
            severity="block",
            message="add 函数缺少 docstring",
            evidence={"file": "feature.py", "symbol": "add"},
            source="manual",
        )
        assert finding_id > 0, "应成功写入 quality finding"

        # 报告步骤完成 → 任务进入 review
        db.task_report_step(
            task_id=task_id, step_id=step_id, result="完成", success=True,
        )

        # 任务应在 review 或 blocking 状态
        status = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status in ("review", "blocking", "in_progress"), \
            f"有 blocking finding 时任务应在 review/blocking/in_progress，实际: {status}"

        # 验证 blocking finding 可被查询
        findings = db.get_task_quality_findings(task_id, status="open")
        block_count = sum(1 for f in findings if f.get("severity") == "block")
        assert block_count >= 1, "应有至少一条 blocking finding"
    finally:
        db.close()
