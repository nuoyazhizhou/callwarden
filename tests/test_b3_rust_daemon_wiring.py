"""批次3-B Rust daemon 接线缺口修复验证测试（G11/G29）。

验证 _feature_matrix.md 中 Rust 端接线修复（当前仍生效的条目）：

- G11 Replicator 注入到 Rust refresh 主路径：DaemonConfig + WorkspaceDaemonState +
  SnapshotDaemonState + cw_daemon.rs state_factory + workspace.rs Replicator 创建点
- G29 QueryBudget：新增 rust_ext/src/daemon/budget.rs 模块 + frontier.rs
  AffectedFrontier.partial + compute_frontier_with_budget + bfs_*_with_budget

【已删除的守卫（登记，勿复活）】
- G10/G20 memfd 四重（后六重）校验：ef28ab9（UDS→HTTPS+mTLS 单轨化，ADR-002）
  整体删除 rust_ext/src/daemon/memfd.rs、test_phase5_g10_memfd_e2e.py 及
  workspace.rs 的 read_from_fd_with_validation 调用点——SCM_RIGHTS/memfd FD
  传递面在单轨架构下无存在路径，属有意移除而非迁移（protocol.rs 的
  recv_message_with_fds 一并删除，见 _feature_matrix.md G10/G20 行 ⊘）。
  对应测试类 TestG1020MemfdValidation 同步删除；其中与 memfd 无关的
  workspace.rs「read_to_end 必须有界」守卫保留为
  TestWorkspaceBoundedReads（gz 解压路径仍在）。
- G21/G22 send_msg/_recv_msg_with_fd/call_with_fd 命名别名：ef28ab9 随 FD 面
  删除 protocol.rs 三个别名函数，对应测试类 TestG2122NamingAliases 同步删除
  （见 _feature_matrix.md G21/G22 行 ⊘）。

测试策略：源码字符串匹配（不依赖 cargo build，Windows 也能跑）。
对于需要在 Linux 实际跑 daemon 的场景，留给 E2E 测试覆盖。
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUST_EXT = os.path.join(ROOT, "rust_ext", "src")
DAEMON_DIR = os.path.join(RUST_EXT, "daemon")
BIN_DIR = os.path.join(RUST_EXT, "bin")


def _read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_daemon_module(name):
    return _read_file(os.path.join(DAEMON_DIR, name))


# ============================================================
# G10/G20: memfd 四重校验 —— 已随 ef28ab9 单轨化移除（见模块 docstring 登记）；
# 仅保留与 memfd 无关的 workspace.rs 有界读取守卫。
# ============================================================


class TestWorkspaceBoundedReads:
    """workspace.rs 的 read_to_end 必须出现在有界读取函数体内（.take() 限容）。"""

    def test_workspace_read_to_end_is_bounded(self):
        """断言收窄说明（2026-10-17，CD-5 D3）：原断言是「任何非注释行出现
        read_to_end 即 fail」，这是纯词法扫描，无法识别有界语义变体——
        decompress_gz_bounded（workspace.rs）用 GzDecoder::new(gz)
        .take(cap as u64) 先把读取流量限容再 read_to_end，事后还检查
        out.len() > max_out 拒绝，是防解压炸弹的有界读，不应判红。
        收窄后：定位 read_to_end 所在函数体（向上找最近 fn 签名行，取其
        行首缩进，同缩进的 } 为函数体闭合），函数体内须含 ``.take(``。
        take() 被删除时本断言会再次变红（同一缺陷可再红，非 MASKING）。
        """
        source = _read_daemon_module("workspace.rs")
        lines = source.split("\n")
        fn_re = re.compile(r"\bfn\s+\w+\s*\(")
        violations = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            if "read_to_end" not in line:
                continue
            # 定位 read_to_end 所属函数：向上找最近的 fn 签名行
            fn_idx = None
            for j in range(i, -1, -1):
                if fn_re.search(lines[j]):
                    fn_idx = j
                    break
            if fn_idx is None:
                violations.append(f"L{i+1} 无法定位所属函数（fn 签名缺失）")
                continue
            indent = lines[fn_idx][:len(lines[fn_idx]) - len(lines[fn_idx].lstrip())]
            # 同缩进闭合 } 为函数体末行
            end_idx = None
            for k in range(i + 1, len(lines)):
                if lines[k] == indent + "}":
                    end_idx = k
                    break
            if end_idx is None:
                violations.append(
                    f"L{i+1} 无法定位函数体闭合（fn 行 L{fn_idx+1}）")
                continue
            body = "\n".join(lines[fn_idx:end_idx + 1])
            if ".take(" not in body:
                violations.append(
                    f"L{i+1} 的函数（fn 行 L{fn_idx+1}）使用 read_to_end "
                    f"但函数体内没有 .take( 限容：非有界读取")
        if violations:
            pytest.fail("workspace bounded read: " + "; ".join(violations))


# ============================================================
# G11: Replicator 注入到 Rust refresh 主路径
# ============================================================


class TestG11ReplicatorWiring:
    """G11: Replicator 在 Rust refresh 主路径触发 publish_snapshot。"""

    def test_g11_config_has_codegraph_db_path_template(self):
        """DaemonConfig 应有 codegraph_db_path_template 字段。"""
        source = _read_daemon_module("config.rs")
        assert "codegraph_db_path_template" in source, (
            "G11: DaemonConfig 缺少 codegraph_db_path_template 字段"
        )

    def test_g11_config_has_resolve_method(self):
        """DaemonConfig 应有 resolve_codegraph_db_path 方法。"""
        source = _read_daemon_module("config.rs")
        assert "fn resolve_codegraph_db_path" in source, (
            "G11: 缺少 resolve_codegraph_db_path 方法"
        )

    def test_g11_config_has_env_override(self):
        """apply_env_overrides 应支持 CW_DAEMON_CODEGRAPH_DB_TEMPLATE。"""
        source = _read_daemon_module("config.rs")
        assert "CW_DAEMON_CODEGRAPH_DB_TEMPLATE" in source, (
            "G11: 未注册 CW_DAEMON_CODEGRAPH_DB_TEMPLATE env override"
        )

    def test_g11_workspace_state_has_publisher_field(self):
        """WorkspaceDaemonState 应有 snapshot_publisher 字段。"""
        source = _read_daemon_module("workspace.rs")
        assert "snapshot_publisher:" in source, (
            "G11: WorkspaceDaemonState 缺少 snapshot_publisher 字段"
        )
        assert "codegraph_db_path_template:" in source, (
            "G11: WorkspaceDaemonState 缺少 codegraph_db_path_template 字段"
        )

    def test_g11_workspace_state_has_builders(self):
        """WorkspaceDaemonState 应有 with_snapshot_publisher + with_codegraph_db_path_template。"""
        source = _read_daemon_module("workspace.rs")
        assert "fn with_snapshot_publisher" in source, (
            "G11: WorkspaceDaemonState 缺少 with_snapshot_publisher builder"
        )
        assert "fn with_codegraph_db_path_template" in source, (
            "G11: WorkspaceDaemonState 缺少 with_codegraph_db_path_template builder"
        )

    def test_g11_snapshot_state_has_passthrough_builders(self):
        """SnapshotDaemonState 应透传 publisher builder 到 base。"""
        source = _read_daemon_module("snapshot_state.rs")
        assert "fn with_snapshot_publisher" in source, (
            "G11: SnapshotDaemonState 缺少 with_snapshot_publisher 透传"
        )
        assert "fn with_codegraph_db_path_template" in source, (
            "G11: SnapshotDaemonState 缺少 with_codegraph_db_path_template 透传"
        )

    def test_g11_cw_daemon_state_factory_injects_publisher(self):
        """cw_daemon.rs state_factory 应创建共享 SnapshotCache + Publisher 注入。"""
        source = _read_file(os.path.join(BIN_DIR, "cw_daemon.rs"))
        assert "shared_snapshot_cache" in source, (
            "G11: cw_daemon.rs 未提升 Arc<SnapshotCache> 到闭包外共享"
        )
        assert "shared_publisher" in source, (
            "G11: cw_daemon.rs 未创建共享 Arc<SnapshotCachePublisher>"
        )
        assert "SnapshotCachePublisher::new" in source, (
            "G11: cw_daemon.rs 未调用 SnapshotCachePublisher::new"
        )
        assert ".with_snapshot_publisher" in source, (
            "G11: cw_daemon.rs state_factory 未注入 publisher"
        )
        assert ".with_codegraph_db_path_template" in source, (
            "G11: cw_daemon.rs state_factory 未注入 db_path 模板"
        )

    def test_g11_workspace_refresh_uses_publisher(self):
        """workspace.rs Replicator 创建点应按 publisher + db_path 注入。"""
        source = _read_daemon_module("workspace.rs")
        # 不应再硬编码 db_path 为 ""
        assert "codegraph_db_path_template.is_empty()" in source or \
               "self.codegraph_db_path_template" in source, (
                   "G11: workspace.rs 未读取 codegraph_db_path_template 字段"
               )
        # 应有 with_snapshot_publisher 注入逻辑
        assert "with_snapshot_publisher" in source, (
            "G11: workspace.rs Replicator 未注入 publisher"
        )

    def test_g11_workspace_refresh_returns_snapshot_published_status(self):
        """workspace.rs 应返回 snapshot_published 状态字段。"""
        source = _read_daemon_module("workspace.rs")
        assert '"snapshot_published"' in source, (
            "G11: workspace.rs 未返回 snapshot_published 字段"
        )

    def test_g11_workspace_refresh_has_warning_branches(self):
        """workspace.rs 应区分降级路径的不同 warning 文案。"""
        source = _read_daemon_module("workspace.rs")
        assert "codegraph_db_path_template 未配置" in source, (
            "G11: 缺少 db_path 未配置 warning"
        )
        assert "SnapshotCachePublisher 未注入" in source, (
            "G11: 缺少 publisher 未注入 warning"
        )


# ============================================================
# G21/G22: 命名统一包装 —— 已随 ef28ab9 单轨化移除（见模块 docstring 登记）。
# protocol.rs 现仅保留 send_message/recv_message（无 FD 变体）。
# ============================================================


# ============================================================
# G29: QueryBudget
# ============================================================


class TestG29QueryBudget:
    """G29: 新增 budget.rs 模块 + frontier.rs compute_frontier_with_budget。"""

    def test_g29_budget_module_exists(self):
        """rust_ext/src/daemon/budget.rs 应存在。"""
        path = os.path.join(DAEMON_DIR, "budget.rs")
        assert os.path.exists(path), "G29: budget.rs 模块文件不存在"

    def test_g29_budget_module_registered(self):
        """daemon/mod.rs 应注册 budget 模块。"""
        source = _read_daemon_module("mod.rs")
        assert "pub mod budget;" in source, "G29: mod.rs 未注册 budget 模块"

    def test_g29_budget_has_query_budget_struct(self):
        """budget.rs 应有 QueryBudget 结构体。"""
        source = _read_daemon_module("budget.rs")
        assert "pub struct QueryBudget" in source, (
            "G29: 缺少 QueryBudget 结构体"
        )
        assert "max_depth" in source, "G29: QueryBudget 缺少 max_depth 字段"
        assert "max_nodes" in source, "G29: QueryBudget 缺少 max_nodes 字段"
        assert "timeout_ms" in source, "G29: QueryBudget 缺少 timeout_ms 字段"

    def test_g29_budget_has_tracker(self):
        """budget.rs 应有 BudgetTracker 结构体。"""
        source = _read_daemon_module("budget.rs")
        assert "pub struct BudgetTracker" in source, (
            "G29: 缺少 BudgetTracker 结构体"
        )
        assert "fn visit_node" in source, "G29: BudgetTracker 缺少 visit_node 方法"
        assert "fn is_exceeded" in source, "G29: BudgetTracker 缺少 is_exceeded 方法"
        assert "fn is_partial" in source, "G29: BudgetTracker 缺少 is_partial 方法"

    def test_g29_budget_has_default_10k_nodes_5s_timeout(self):
        """QueryBudget::default() 应有 max_nodes=10000 + timeout_ms=5000。"""
        source = _read_daemon_module("budget.rs")
        assert "10_000" in source, "G29: 默认 max_nodes 不是 10000"
        assert "5_000" in source, "G29: 默认 timeout_ms 不是 5000"

    def test_g29_frontier_has_partial_field(self):
        """AffectedFrontier 应有 partial 字段。"""
        source = _read_file(os.path.join(RUST_EXT, "frontier.rs"))
        assert "pub partial: bool" in source, (
            "G29: AffectedFrontier 缺少 partial 字段"
        )

    def test_g29_frontier_has_compute_with_budget(self):
        """FrontierComputer 应有 compute_frontier_with_budget 方法。"""
        source = _read_file(os.path.join(RUST_EXT, "frontier.rs"))
        assert "fn compute_frontier_with_budget" in source, (
            "G29: FrontierComputer 缺少 compute_frontier_with_budget 方法"
        )

    def test_g29_frontier_has_bfs_with_budget(self):
        """FrontierComputer 应有 bfs_upstream_with_budget + bfs_downstream_with_budget。"""
        source = _read_file(os.path.join(RUST_EXT, "frontier.rs"))
        assert "fn bfs_upstream_with_budget" in source, (
            "G29: 缺少 bfs_upstream_with_budget"
        )
        assert "fn bfs_downstream_with_budget" in source, (
            "G29: 缺少 bfs_downstream_with_budget"
        )

    def test_g29_frontier_summary_shows_partial(self):
        """AffectedFrontier.summary() 应包含 [PARTIAL] 标记。"""
        source = _read_file(os.path.join(RUST_EXT, "frontier.rs"))
        assert "[PARTIAL]" in source or "PARTIAL" in source, (
            "G29: summary() 未包含 partial 标记"
        )

    def test_g29_python_exposed_compute_with_budget(self):
        """lib.rs 应注册 compute_frontier_with_budget 给 Python。"""
        source = _read_file(os.path.join(RUST_EXT, "lib.rs"))
        assert "compute_frontier_with_budget" in source, (
            "G29: lib.rs 未注册 compute_frontier_with_budget"
        )

    def test_g29_python_exposed_partial_getter(self):
        """PyAffectedFrontier 应有 partial getter。"""
        source = _read_file(os.path.join(RUST_EXT, "frontier.rs"))
        assert "fn partial(&self) -> bool" in source, (
            "G29: PyAffectedFrontier 缺少 partial getter"
        )


# ============================================================
# 集成验证：_feature_matrix.md 状态同步
# ============================================================
# 原 TestFeatureMatrixStatusRust 断言 _feature_matrix.md 中 G10/G20/G21/G22
# 四行状态为 ✅；ef28ab9（HTTPS+mTLS 单轨化）移除 memfd.rs 与 protocol.rs FD
# 别名后，四行已改记为 ⊘ 已移除（见 _feature_matrix.md），不再有可断言的
# ✅ 状态，该测试类随之删除；G11/G29 经复审回退本就不断言 ✅。
