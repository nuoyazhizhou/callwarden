"""C-13 回归：semgrep_handlers 编译接线与 semgrep RPC route 落地。

背景（C-13 根因）：`rust_ext/src/daemon/semgrep_handlers.rs`（CLI-061 产物
`T-1787322798303-8bd1779c`）虽然存在于仓库，但从未在 `daemon/mod.rs` 声明
`pub mod`，因此**从未参与编译**；四个裸方法名
`run_semgrep` / `run_semgrep_and_save` / `scan_semgrep_incremental` /
`get_semgrep_summary` 在 daemon 侧恒 `method_not_found`，导致
`cw semgrep scan` 端到端死亡（`未知方法: run_semgrep`）。

本测试在**源码契约层**做确定性回归（与 `tests/convergence/test_m1_route_matrix.py`
同款「文本提取 + 集合断言」范式，不依赖 live daemon / 不依赖 semgrep 二进制）：

  1. `daemon/mod.rs` 声明 `pub mod semgrep_handlers;`（否则文件不参与编译）；
  2. `daemon/dispatch.rs` 的 `CONVERGENCE_RPC_METHODS` 含 4 个 rpc_method；
  3. `daemon/dispatch.rs` 的 `PROTECTED_MUTATION_METHODS` 含 3 个写方法；
  4. `daemon/snapshot_state.rs` 的 `handle_convergence_rpc` 把 4 个方法名
     分派到 `semgrep::handle_*`（写方法走主库连接、读方法走只读连接）。

方法名必须与 `cli/main.py:1207-1212` 的 `_METHOD_MAP` rpc_method 逐字一致
（`test_method_names_match_cli_contract` 固化该跨端契约）。

live 端到端回执（`cw semgrep scan cli` / `--quick` 不再 method_not_found）
由本卡 step3 证据 manifest 记录，见 `deliverables/software-company/`。
"""
from __future__ import annotations

import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_DIR = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon")
_MOD_RS = os.path.join(_DAEMON_DIR, "mod.rs")
_DISPATCH_RS = os.path.join(_DAEMON_DIR, "dispatch.rs")
_SNAPSHOT_STATE_RS = os.path.join(_DAEMON_DIR, "snapshot_state.rs")
_CLI_MAIN = os.path.join(_REPO_ROOT, "cli", "main.py")

# CLI-061 `_METHOD_MAP` 的四条 semgrep rpc_method（C-13 契约单源）。
_SEMGREP_RPC_METHODS = (
    "run_semgrep",
    "get_semgrep_summary",
    "run_semgrep_and_save",
    "scan_semgrep_incremental",
)
_SEMGREP_WRITE_METHODS = (
    "run_semgrep",
    "run_semgrep_and_save",
    "scan_semgrep_incremental",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _rust_const_set(source: str, const_name: str) -> set[str]:
    """提取 `const NAME: &[&str] = &[ ... ];` 内的全部字符串字面量。"""
    m = re.search(
        rf"{re.escape(const_name)}\s*:\s*&\[&str\]\s*=\s*&\[(.*?)\];",
        source,
        re.S,
    )
    assert m, f"{const_name} 未在源码中找到"
    return set(re.findall(r'"([a-zA-Z0-9_.]+)"', m.group(1)))


class TestSemgrepModuleDeclared:
    def test_semgrep_handlers_module_is_declared(self):
        """C-13 根因：文件存在但 mod.rs 未声明 → 从未参与编译。"""
        src = _read(_MOD_RS)
        assert re.search(r"^\s*pub mod semgrep_handlers;\s*$", src, re.M), (
            "daemon/mod.rs 未声明 `pub mod semgrep_handlers;`——"
            "semgrep_handlers.rs 将不参与编译，四个方法恒 method_not_found（C-13）"
        )

    def test_semgrep_handlers_file_exists(self):
        path = os.path.join(_DAEMON_DIR, "semgrep_handlers.rs")
        assert os.path.isfile(path), "semgrep_handlers.rs 不存在（CLI-061 产物缺失）"
        handlers = _read(path)
        for fn in (
            "handle_run_semgrep",
            "handle_run_semgrep_and_save",
            "handle_scan_semgrep_incremental",
            "handle_get_semgrep_summary",
        ):
            assert re.search(rf"pub fn {fn}\(", handlers), f"semgrep_handlers.rs 缺少 {fn}"


class TestDispatchMethodRegistration:
    def test_convergence_rpc_methods_contains_semgrep(self):
        methods = _rust_const_set(_read(_DISPATCH_RS), "CONVERGENCE_RPC_METHODS")
        missing = [m for m in _SEMGREP_RPC_METHODS if m not in methods]
        assert not missing, (
            f"CONVERGENCE_RPC_METHODS 缺少 semgrep rpc_method {missing}——"
            "dispatch 兜底链 is_convergence_rpc 不命中，方法恒 method_not_found（C-13）"
        )

    def test_protected_mutation_methods_contains_semgrep_writes(self):
        methods = _rust_const_set(_read(_DISPATCH_RS), "PROTECTED_MUTATION_METHODS")
        missing = [m for m in _SEMGREP_WRITE_METHODS if m not in methods]
        assert not missing, (
            f"PROTECTED_MUTATION_METHODS 缺少 semgrep 写方法 {missing}——"
            "写面不会经唯一串行化点（与 cw semgrep 的 PROTECTED_MUTATION 分类不一致）"
        )

    def test_read_method_not_classified_as_protected_mutation(self):
        """get_semgrep_summary 为只读汇总，不得登记为受保护写。"""
        methods = _rust_const_set(_read(_DISPATCH_RS), "PROTECTED_MUTATION_METHODS")
        assert "get_semgrep_summary" not in methods, (
            "get_semgrep_summary 是只读汇总（READ_ONLY），不应登记进 PROTECTED_MUTATION_METHODS"
        )

    def test_method_names_match_cli_contract(self):
        """跨端契约：dispatch 登记的方法名 = cli/main.py `_METHOD_MAP` rpc_method。"""
        cli_src = _read(_CLI_MAIN)
        for method in _SEMGREP_RPC_METHODS:
            assert re.search(rf'"{re.escape(method)}"\s*:', cli_src), (
                f"cli/main.py _METHOD_MAP 缺少 rpc_method {method}（跨端契约断裂）"
            )


class TestSnapshotStateSemgrepDispatch:
    def test_handle_convergence_rpc_matches_semgrep_methods(self):
        src = _read(_SNAPSHOT_STATE_RS)
        for method in _SEMGREP_RPC_METHODS:
            assert re.search(rf'"{re.escape(method)}"', src), (
                f"snapshot_state.rs handle_convergence_rpc 未分发方法 {method}"
            )

    def test_semgrep_handlers_aliased_and_invoked(self):
        src = _read(_SNAPSHOT_STATE_RS)
        assert re.search(r"use super::semgrep_handlers as semgrep;", src), (
            "handle_convergence_rpc 未以本地 use 引入 semgrep_handlers（C-13 接线）"
        )
        for fn in (
            "semgrep::handle_run_semgrep(",
            "semgrep::handle_run_semgrep_and_save(",
            "semgrep::handle_scan_semgrep_incremental(",
            "semgrep::handle_get_semgrep_summary(",
        ):
            assert fn in src, f"snapshot_state.rs 未调用 {fn}"

    def test_write_methods_use_codegraph_write_connection(self):
        """三个写方法须经 open_codegraph_db_write（主库写），读方法走 open_query_connection。"""
        src = _read(_SNAPSHOT_STATE_RS)
        m = re.search(
            r'"run_semgrep"\s*\|\s*"run_semgrep_and_save"\s*\|\s*"scan_semgrep_incremental"\s*=>\s*\{(.*?)\n\s*\}',
            src,
            re.S,
        )
        assert m, "未找到 semgrep 写方法 dispatch arm"
        assert "open_codegraph_db_write(" in m.group(1), (
            "semgrep 写方法未使用 open_codegraph_db_write（主库写连接）"
        )
