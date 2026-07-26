"""挂载 R8-R11 子任务到 Rust-only parser 父任务。

复审报告（2026-07-26）拒绝通过，仍有 4 个 P0 + 3 个 P1 阻塞项。
本脚本将 R8-R11 四个子任务挂到 T-1784986236712-736b2331 下。

R5-R7 已完成：P0-1/P0-2/P0-3 + 测试更新已就绪；
R8-R11 需补：契约门禁真实对齐 / 恢复链 / inspector / 发布证据。
"""
import sys
import os

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB

PARENT_ID = "T-1784986236712-736b2331"

tasks_to_mount = [
    {
        "title": "R8: 修复契约门禁假绿（P0-4 真实 golden/signature/visibility 对齐）",
        "desc": (
            "复审 P0-4：tests/parser_contract/test_golden_fixtures.py 只检查 JSON 结构，"
            "tests/test_rust_python_alignment.py:708-709 只比较双方都有非空 signature 的项，"
            ":722-747 又要求 Rust signature 全空才通过。"
            "rust_ext/src/multi_lang.rs:914 仍把所有通用语言 signature 写为空字符串。"
            "本任务：1) 移除 test_signature_rust_all_empty 假绿测试；"
            "2) 改造 test_signature_alignment 用 golden fixture 期望的 signature 字符串"
            "作为契约真相，Rust parser 输出必须逐字段对齐（或显式记录 known_gap）；"
            "3) 同步推进 visibility 真实对齐。"
        ),
        "status": "open",
    },
    {
        "title": "R9: 闭合恢复链（P1-1 ParserDoctor + retry log replay）",
        "desc": (
            "复审 P1-1：workspace.rs:1296-1316 把允许重试的失败追加到 parse_retry.log，"
            "但 replay_pending() 只在自身单测中调用。daemon 启动 recover_all_workspaces() "
            "和 RPC workspace.recover 都只处理 staging.log，从未读取 parse_retry.log。"
            "ParserDoctor 仍无生产调用，ParserMetrics 的 latency 固定记录为 0.0。"
            "本任务：1) daemon 启动时调用 parse_retry_log::replay_pending()；"
            "2) RPC workspace.recover 增加 retry log 重放路径；"
            "3) ParserDoctor 接入生产观测点（health/metrics），latency 用真实测量。"
        ),
        "status": "open",
    },
    {
        "title": "R10: 修复 inspector 真实加载 + 拦截重复扩展（P1-2）",
        "desc": (
            "复审 P1-2：release/inspect_pyinstaller_bundle.py:414-421 仍以模块名 "
            "callwarden_core_verify 加载 PyO3 扩展，真实执行 --verify-rust-parse 得到 "
            "'dynamic module does not define module export function "
            "(PyInit_callwarden_core_verify)'。"
            "冻结包同时包含两份 36.17MB Rust 扩展："
            "_internal/callwarden_core.cp314-win_amd64.pyd 和 "
            "_internal/callwarden_core/callwarden_core.cp314-win_amd64.pyd。"
            "普通 inspector 仍返回 exit 0。"
            "本任务：1) 修正 inspector 真实加载名为 callwarden_core；"
            "2) 普通模式必须检测重复 Rust 扩展并 exit 1；"
            "3) 修正 PyInstaller spec 避免重复收集 callwarden_core.pyd。"
        ),
        "status": "open",
    },
    {
        "title": "R11: 提供发布证据（P1-3 四平台 before/after 体积表）",
        "desc": (
            "复审 P1-3：origin/master 仍为 9409ede，最新 tag 仍为 v0.3.2；"
            "docs/design/rust-only-parser-size-report.md:94-143 的 Windows、macOS、"
            "Linux x86_64、Linux aarch64 before/after/delta 仍全部待填充。"
            "本任务：1) 在本机完成 Windows frozen bundle 体积采集；"
            "2) 通过 cargo + rustup target + cross 或 CI workflow 获取其他平台体积；"
            "3) 填充 size-report.md 四平台 before/after/delta 表格。"
        ),
        "status": "open",
    },
]


def main():
    db = CodeGraphDB()
    cur = db.conn.execute("SELECT id, title, status FROM tasks WHERE id = ?", (PARENT_ID,))
    parent = cur.fetchone()
    if not parent:
        print(f"错误：父任务 {PARENT_ID} 不存在")
        sys.exit(1)
    print(f"父任务：{PARENT_ID} [{parent['status']}] {parent['title']}")
    print()

    created = 0
    for t in tasks_to_mount:
        cur = db.conn.execute(
            "SELECT id, status FROM tasks WHERE title = ? AND parent_id = ?",
            (t["title"], PARENT_ID),
        )
        existing = cur.fetchone()
        if existing:
            print(f"  Exists: {existing['id']} [{existing['status']}] {t['title']}")
            continue
        task_id = db.task_create(
            title=t["title"],
            description=t["desc"],
            parent_id=PARENT_ID,
            steps=[],
        )
        print(f"  Created: {task_id} {t['title']}")
        created += 1

    print(f"\n共创建 {created} 个新子任务")
    db.conn.close()


if __name__ == "__main__":
    main()
