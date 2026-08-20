"""0C：authority 写路径 gate-first 锁序与无 bypass 结构回归测试。

计划 §3.3 全局锁序 `CapabilityMutationGate → authority store → task DB`：
任何 authority 写入口都必须在打开 DB 写 transaction 之前完成 gate 前置校验
（gate-first）。本文件从**结构层面**锁定该顺序，并核对 Rust
`CapabilityMutationGate` 的锁序契约与 0C 测试清单：

1. 每个已知 authority 写入口（db ×3 / API ×1 / CLI ×1）的源码中，gate 标记
   必须出现在该方法首个 DB 写语句**之前**（防止有人后置/删除 gate 检查）。
2. Rust `capability_control.rs` 冻结锁序文档与 fail-closed 错误码常量稳定。
3. Rust 0C 测试四契约（fail-closed / 锁序串行化 / 无死锁 / poisoned）保持存在。

不重复 test_task_loop_capability_authority.py 的功能断言，本文件专注结构契约。
"""

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# 已知 authority 写入口 → (相对路径, 方法名, gate 标记, 首个 DB 写语句标记)
AUTHORITY_WRITE_ENTRIES = [
    ("db/db_task_identity.py", "register_attestation_revocation",
     "_require_authority_gate()", "self.conn.execute"),
    ("db/db_task_evidence.py", "invalidate_evidence",
     "_require_authority_gate()", "self.conn.execute"),
    ("db/db_task_evidence.py", "revoke_verifier",
     "_require_authority_gate()", "self.conn.execute"),
    ("cli/main.py", "_identity_revoke",
     'get_daemon_mode() != "local"', "register_attestation_revocation"),
    ("server/tools/tools_p3_identity.py", "register_attestation_revocation",
     '_get_daemon_mode() != "local"', "db.register_attestation_revocation("),
]


def _gate_before_first_write(rel_path, def_name, gate_marker, db_marker):
    """返回 gate 标记是否出现在方法体首个 DB 写语句之前。"""
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    idx_def = src.index(f"def {def_name}(")
    body = src[idx_def:]
    idx_gate = body.index(gate_marker)
    idx_db = body.index(db_marker)
    return idx_gate < idx_db, (idx_gate, idx_db)


@pytest.mark.parametrize(
    "entry",
    AUTHORITY_WRITE_ENTRIES,
    ids=lambda e: f"{e[0]}::{e[1]}",
)
def test_authority_write_entry_is_gate_first(entry):
    """锁序 gate → authority store → task DB：gate 检查必须先于任何 DB 写语句。"""
    rel_path, def_name, gate_marker, db_marker = entry
    ok, (idx_gate, idx_db) = _gate_before_first_write(
        rel_path, def_name, gate_marker, db_marker
    )
    assert ok, (
        f"{rel_path}::{def_name} 违反 gate-first：gate 标记 {gate_marker!r} "
        f"(src@{idx_gate}) 出现在 DB 写语句 {db_marker!r} (src@{idx_db}) 之后，"
        "存在绕过 CapabilityMutationGate 的直写风险"
    )


# ============================================================
# Rust gate 锁序契约与 0C 测试清单
# ============================================================

def test_rust_gate_documents_frozen_lock_order():
    """Rust CapabilityMutationGate 必须声明冻结锁序（防止后续实现改写）。

    文档含 `→` 与换行，按两个连续片段分别断言。
    """
    src = (_REPO_ROOT / "rust_ext/src/daemon/task_loop/capability_control.rs").read_text(
        encoding="utf-8"
    )
    assert (
        "CapabilityMutationGate → Capability Authority store transaction" in src
    ), "capability_control.rs 缺少冻结锁序文档（gate → authority store）"
    assert "task-DB transaction" in src, "capability_control.rs 缺少冻结锁序文档（→ task DB）"
    assert "pub fn acquire" in src, "gate 必须暴露 acquire（唯一 mutation 入口）"
    assert "pub fn revalidate_public_permit" in src, "gate 必须暴露提交前最终复核"


def test_rust_gate_fail_closed_error_constant_stable():
    """gate fail-closed 错误码常量必须与 db/API/CLI 三层一致（单一稳定码）。"""
    src = (_REPO_ROOT / "rust_ext/src/daemon/task_loop/types.rs").read_text(
        encoding="utf-8"
    )
    assert (
        'ERR_CAPABILITY_DISABLED: &str = "E_TASK_LOOP_CAPABILITY_DISABLED"' in src
    ), "types.rs 缺少 E_TASK_LOOP_CAPABILITY_DISABLED 稳定常量"


def test_rust_gate_0c_tests_cover_four_contracts():
    """0C Rust 测试清单必须保持四契约（fail-closed / 串行化 / 活性 / poisoned）。"""
    src = (_REPO_ROOT / "rust_ext/src/daemon/task_loop/capability_control_test.rs").read_text(
        encoding="utf-8"
    )
    required = (
        "revalidate_public_permit_is_fail_closed",
        "gate_serializes_concurrent_store_mutations",
        "gate_release_allows_next_acquire_without_deadlock",
        "poisoned_gate_returns_internal_error_not_deadlock",
    )
    for name in required:
        assert f"fn {name}" in src, f"capability_control_test.rs 缺少测试 {name}"
