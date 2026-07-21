"""挂载 7 个子任务到父任务 T-1784677235802-3a116c18

复审整改-v2 的 7 个子任务，按 P0 优先级排序。
"""
import sys
import os

# 需要把 callwarden 包的父目录加到 path，使 `from callwarden.db.db import CodeGraphDB` 可用
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB

db = CodeGraphDB()
parent_id = "T-1784677235802-3a116c18"

subtasks = [
    {
        "title": "P0-1 Rust daemon ACL 修复（5 个 handler 接入 peer_uid 校验）",
        "description": """复审报告 §3 P0-1：Rust system daemon 的 5 个 workspace-id handler 仍忽略 _peer。

证据：rust_ext/src/daemon/snapshot_state.rs:960-1149
- handle_toolchain_resolve (L960-963): 参数命名 _peer 并忽略
- handle_build_context_list (L1023-1026)
- handle_resolved_edges_store (L1074-1077)
- handle_resolved_edges_get (L1123-1126)
- handle_resolved_edges_count (L1146-1149)

修复方案：
1. 5 个 handler 接入 peer_uid 校验（参考 Python 端 _owned_workspace_by_id）
2. 需要在 Rust 端实现 workspace owner 查询（查 workspaces 表 owner_uid 列）
3. 非 owner 的跨 UID 调用返回 workspace_forbidden 错误
4. admin（root/daemon uid）跳过校验

影响矩阵：G1/G3/G4 🟡→✅、J8 部分""",
        "steps": [
            "在 Rust 端实现 owned_workspace(peer_uid, workspace_id) 查询",
            "5 个 handler 接入 peer_uid 校验",
            "编写跨 UID 拒绝路径测试",
            "cargo check + cargo test",
        ],
    },
    {
        "title": "P0-2 Rust daemon CAS → CodeGraph merge",
        "description": """复审报告 §3 P0-2：Rust daemon publish_snapshot 只调用 build_and_publish_blocking 重载 SQLite，未做 CAS → CodeGraph merge。

证据：rust_ext/src/daemon/replicator.rs:740-768
Linux systemd 启动 Rust cw_daemon，但 Rust daemon 路径未接入 merge_cas_to_codegraph。

修复方案（二选一）：
方案 A：在 Rust 端实现 CAS merge（工作量大，需移植 db_cas_merge.py 逻辑）
方案 B（推荐）：明确文档降级——Rust daemon 当前不支持 save-to-query，
              企业部署需使用 Python daemon 路径（systemd unit 启动 python -m callwarden.server）

影响矩阵：G8/G11 ❌→🟡（若方案 B）、J8/K4 部分""",
        "steps": [
            "评估方案 A vs B 的工作量",
            "实施方案（若 A：Rust 端实现 merge；若 B：文档明确降级 + systemd unit 调整）",
            "测试验证",
        ],
    },
    {
        "title": "P0-2 Python replicator 提交顺序调整（generation 后置）",
        "description": """复审报告 §3 P0-3：Python 路径非原子提交。

证据：server/replicator.py:254-269
先提交 latest_committed_generation，后做 CodeGraph merge 和 manifest upsert（L277+）。
后半段失败后同一 seq 重试会判 stale 丢弃，事件永久无法恢复。

修复方案：
1. 调整提交顺序：先做 merge + manifest，后提交 generation
2. 或：改为两阶段提交（先 staging，全部成功后原子提交）
3. merge_cas_to_codegraph 跨库不原子问题需要评估（CodeGraph DB + manifest DB）

影响矩阵：G8/G11 部分""",
        "steps": [
            "分析当前提交顺序的依赖关系",
            "调整顺序：merge + manifest 先行，generation 后置",
            "补充 symbol_contents 写入 + 入边清理",
            "测试：模拟 merge 失败后重试能否恢复",
        ],
    },
    {
        "title": "P0-3 Linux/macOS 打包脚本修复（含 Python 解释器+site-packages）",
        "description": """复审报告 §3 P0-4：包内只复制临时 venv 的 launcher，没有打包 Python 解释器、site-packages 和依赖。

证据：
- release/linux/build_packages.sh:169-195 只复制 $VENV_BIN/cw 等脚本
- release/macos/build_pkg.sh:164-190 同样只复制 launcher
- shebang 指向构建机临时 venv，安装后无法启动
- Linux maintainer scripts 调用不存在的 Rust 命令（drain/snapshot create/migrate）

修复方案：
方案 A（推荐）：改用 PyInstaller --onefile 打包（含 Python 解释器+依赖）
方案 B：打包整个 venv（site-packages + python 二进制）+ 修正 shebang
方案 C：明确文档降级——当前仅支持 wheel 安装（pip install），不提供系统包

附加修复：
- Linux maintainer scripts 删除不存在的 daemon 子命令调用
- macOS workflow Gate 2 删除无效 universal2-apple-darwin target

影响矩阵：N6/N7 ❌→🟡/✅、N8 部分""",
        "steps": [
            "评估方案 A/B/C 工作量",
            "实施选定方案",
            "修正 Linux maintainer scripts",
            "修正 macOS workflow target",
            "验证打包产物可执行",
        ],
    },
    {
        "title": "P1-1 PR Checker 三条 fail-open 闭合",
        "description": """复审报告 §4 P1-1：PR Checker 仍有三条 fail-open 路径。

证据：
1. cicd/incremental.py:50-58 — git diff 失败返回空列表，PRChecker 解释为"无改动"并通过
2. cicd/pr_check.py:99-109 — scan_semgrep_incremental() 返回 {success: false} 不检查
3. cicd/pr_check.py:232-263 — _query_open_findings() 对 guardrail/Semgrep SQL 异常静默 pass

附加问题：Semgrep 查询只按 rel_path JOIN，没有 active workspace_id 条件，
可能把另一个 workspace 同路径的 finding 混入本次 PR。

修复方案：
1. get_changed_files() 失败时抛异常或返回 sentinel，PRChecker 检测后 fail-closed
2. run_pr_check() 检查 scan_semgrep_incremental 返回值的 success 字段
3. _query_open_findings() SQL 异常时记录到 run_errors，不静默 pass
4. Semgrep 查询加 workspace_id 条件

影响矩阵：A19/A21 🟡→✅""",
        "steps": [
            "修复 get_changed_files() 失败语义",
            "修复 run_pr_check() 检查 success 字段",
            "修复 _query_open_findings() 异常处理",
            "加 workspace_id 条件到 Semgrep 查询",
            "测试三条 fail-open 路径闭合",
        ],
    },
    {
        "title": "P1-2 D7 cross_repo_impact 方向 + 跨 workspace 去重修复",
        "description": """复审报告 §4 P1-2：D7 影响传播方向错误 + 跨 workspace 去重错误。

证据：
1. db/db_cross_repo.py:438-453 — cross_repo_impact() 把 source_symbol_hash = changed_symbol
   的 target workspace 列为受影响仓库。但 cross_repo_deps 语义是 "source import target"，
   改变调用方不会反向影响被调用库，方向错误。
2. 同轮去重 key 只有 (source_hash, target_hash)（L217-221），没有 target workspace。
   同一 CAS symbol 出现在多个目标仓库时，后续仓库会被误去重。

修复方案：
1. cross_repo_impact() 改为查 source_workspace_id = changed_workspace 的记录
   （即"我的仓库依赖了别人，别人变了影响我"）
2. 去重 key 加入 target_workspace_id：(source_hash, target_hash, target_workspace_id)
3. 评估短名候选循环的 endswith 恒真问题

影响矩阵：D7 🟡→✅、I2/I7/I19 部分""",
        "steps": [
            "修正 cross_repo_impact 查询方向",
            "去重 key 加 target_workspace_id",
            "评估短名候选 endswith 问题",
            "测试影响传播方向正确性",
        ],
    },
    {
        "title": "P1-5 基线脚本扫描盲区修复",
        "description": """复审报告 §4 P1-5：基线脚本产生假阴性。

证据：
1. docs/architecture.md:49,323 仍写"39 个文件"——扫描器只匹配"N 个 db_*.py"，
   抓不到反引号包裹的 `db_*.py`（39 个文件）`
2. _feature_matrix.md:362 I38 仍写"33 个 Mixin 类（39 个 db_*.py 文件）"——
   因含 → 被整行跳过
3. 扫描器输出使用传入路径总数 76，不是实际扫描文件数

修复方案：
1. 扩展 db_files 扫描模式：增加反引号包裹 `` `db_*.py`（N 个文件）`` 模式
2. 修正 SKIP_MARKERS：不要因 → 整行跳过，改为只在行首跳过
3. 扫描器输出改为实际扫描文件数
4. 修复 architecture.md L49/L323 的 39→40
5. 修复 _feature_matrix.md I38 的 33/39→35/40

影响矩阵：I1/I17 🟡→✅""",
        "steps": [
            "扩展 db_files 扫描模式（反引号包裹）",
            "修正 SKIP_MARKERS 逻辑",
            "修复 architecture.md L49/L323",
            "修复 _feature_matrix.md I38",
            "验证 check_baseline.py --check 真实可信",
        ],
    },
]

for st in subtasks:
    # steps 必须是 dict 列表（db_tasks.task_create 期望 {"action": ...} 形式）
    step_dicts = [{"action": s} if isinstance(s, str) else s for s in st["steps"]]
    result = db.task_create(
        title=st["title"],
        description=st["description"],
        parent_id=parent_id,
        steps=step_dicts,
    )
    print(f"  子任务: {st['title'][:40]}... -> {result}")

db.close()
print("完成")
