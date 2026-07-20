"""批次3-B Rust daemon 接线缺口修复验证测试（G10/G11/G20/G21/G22/G29）。

验证 _feature_matrix.md 中 6 个 🟡 部分完成条目的 Rust 端修复：

- G10/G20 memfd 四重校验：新增 rust_ext/src/daemon/memfd.rs 模块 +
  workspace.rs handle_workspace_file_refresh 接入 read_from_fd_with_validation
- G11 Replicator 注入到 Rust refresh 主路径：DaemonConfig + WorkspaceDaemonState +
  SnapshotDaemonState + cw_daemon.rs state_factory + workspace.rs Replicator 创建点
- G21/G22 send_msg / _recv_msg_with_fd / call_with_fd 命名统一包装：
  protocol.rs 新增三个别名函数
- G29 QueryBudget：新增 rust_ext/src/daemon/budget.rs 模块 + frontier.rs
  AffectedFrontier.partial + compute_frontier_with_budget + bfs_*_with_budget

测试策略：源码字符串匹配（不依赖 cargo build，Windows 也能跑）。
对于需要在 Linux 实际跑 daemon 的场景，留给 E2E 测试覆盖。
"""

import os

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
# G10/G20: memfd 四重校验
# ============================================================


class TestG1020MemfdValidation:
    """G10/G20: 新增 memfd.rs 模块 + workspace.rs FD 读取路径接入。"""

    def test_g1020_memfd_module_exists(self):
        """rust_ext/src/daemon/memfd.rs 应存在。"""
        path = os.path.join(DAEMON_DIR, "memfd.rs")
        assert os.path.exists(path), "G10/G20: memfd.rs 模块文件不存在"

    def test_g1020_memfd_module_registered(self):
        """daemon/mod.rs 应注册 memfd 模块。"""
        source = _read_daemon_module("mod.rs")
        assert "pub mod memfd;" in source, "G10/G20: mod.rs 未注册 memfd 模块"

    def test_g1020_memfd_module_has_cfg_unix(self):
        """memfd.rs 应有 #![cfg(unix)] 限定（FD 路径仅 Unix）。"""
        source = _read_daemon_module("memfd.rs")
        assert "#![cfg(unix)]" in source, "G10/G20: memfd.rs 缺少 #![cfg(unix)]"

    def test_g1020_memfd_has_four_validations(self):
        """memfd.rs 应实现四重校验函数 read_from_fd_with_validation。"""
        source = _read_daemon_module("memfd.rs")
        assert "pub fn read_from_fd_with_validation" in source, (
            "G10/G20: memfd.rs 缺少 read_from_fd_with_validation 函数"
        )
        # 四重校验关键标志
        assert "S_IFREG" in source, "G10/G20: 缺少 S_IFREG 类型校验"
        assert "st_size" in source, "G10/G20: 缺少 st_size 大小预检"
        assert "DEFAULT_MAX_FD_READ_BYTES" in source, "G10/G20: 缺少容量上限常量"
        assert "Sha256" in source, "G10/G20: 缺少 SHA-256 摘要校验"

    def test_g1020_memfd_has_default_max(self):
        """memfd.rs 应定义 DEFAULT_MAX_FD_READ_BYTES = 64MB。"""
        source = _read_daemon_module("memfd.rs")
        assert "64 * 1024 * 1024" in source, (
            "G10/G20: DEFAULT_MAX_FD_READ_BYTES 不是 64MB"
        )

    def test_g1020_memfd_has_error_variants(self):
        """memfd.rs 应定义 FdReadError 枚举（含 5 个变体）。"""
        source = _read_daemon_module("memfd.rs")
        assert "pub enum FdReadError" in source, "G10/G20: 缺少 FdReadError 枚举"
        assert "FstatFailed" in source, "G10/G20: 缺少 FstatFailed 变体"
        assert "NotRegularFile" in source, "G10/G20: 缺少 NotRegularFile 变体"
        assert "SizeExceedsLimit" in source, "G10/G20: 缺少 SizeExceedsLimit 变体"
        assert "ReadFailed" in source, "G10/G20: 缺少 ReadFailed 变体"
        assert "DigestMismatch" in source, "G10/G20: 缺少 DigestMismatch 变体"

    def test_g1020_workspace_uses_memfd_validation(self):
        """workspace.rs handle_workspace_file_refresh 应调用 memfd::read_from_fd_with_validation。"""
        source = _read_daemon_module("workspace.rs")
        assert "use crate::daemon::memfd;" in source, (
            "G10/G20: workspace.rs 未 import memfd 模块"
        )
        assert "memfd::read_from_fd_with_validation" in source, (
            "G10/G20: workspace.rs 未调用 read_from_fd_with_validation"
        )
        assert "memfd::DEFAULT_MAX_FD_READ_BYTES" in source, (
            "G10/G20: workspace.rs 未使用 DEFAULT_MAX_FD_READ_BYTES"
        )

    def test_g1020_workspace_no_read_to_end(self):
        """workspace.rs FD 路径不应再使用 read_to_end 无界读。"""
        source = _read_daemon_module("workspace.rs")
        # read_to_end 应该只在注释中出现（历史说明），不在生产代码
        # 找 read_to_end 在实际代码（非注释）中的出现
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("//"):
                continue
            if "read_to_end" in stripped:
                pytest.fail(
                    f"G10/G20: workspace.rs L{i+1} 仍在使用 read_to_end: {line.strip()}"
                )

    def test_g1020_workspace_supports_expected_sha256(self):
        """workspace.rs FD 路径应支持客户端传入 expected_sha256 参数。"""
        source = _read_daemon_module("workspace.rs")
        assert "expected_sha256" in source, (
            "G10/G20: workspace.rs 未支持 expected_sha256 参数"
        )


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
# G21/G22: 命名统一包装
# ============================================================


class TestG2122NamingAliases:
    """G21/G22: protocol.rs 新增 send_msg / _recv_msg_with_fd / call_with_fd。"""

    def test_g2122_protocol_has_send_msg_alias(self):
        """protocol.rs 应有 send_msg 函数。"""
        source = _read_daemon_module("protocol.rs")
        assert "pub fn send_msg" in source, "G21: 缺少 send_msg 函数"

    def test_g2122_protocol_has_recv_msg_with_fd_alias(self):
        """protocol.rs 应有 _recv_msg_with_fd 函数。"""
        source = _read_daemon_module("protocol.rs")
        assert "pub fn _recv_msg_with_fd" in source, (
            "G22: 缺少 _recv_msg_with_fd 函数"
        )

    def test_g2122_protocol_has_call_with_fd(self):
        """protocol.rs 应有 call_with_fd 请求-响应组合函数。"""
        source = _read_daemon_module("protocol.rs")
        assert "pub fn call_with_fd" in source, (
            "G21/G22: 缺少 call_with_fd 函数"
        )

    def test_g2122_send_msg_calls_send_message(self):
        """send_msg 应委托给 send_message。"""
        source = _read_daemon_module("protocol.rs")
        # 找 send_msg 函数体，验证它调用 send_message
        assert "send_message(writer, message, max_bytes)" in source, (
            "G21: send_msg 未委托给 send_message"
        )

    def test_g2122_recv_msg_with_fd_calls_recv_message_with_fds(self):
        """_recv_msg_with_fd 应委托给 recv_message_with_fds。"""
        source = _read_daemon_module("protocol.rs")
        assert "recv_message_with_fds(sock, max_bytes, max_fds)" in source, (
            "G22: _recv_msg_with_fd 未委托给 recv_message_with_fds"
        )

    def test_g2122_call_with_fd_combines_both(self):
        """call_with_fd 应组合 send_msg + _recv_msg_with_fd。"""
        source = _read_daemon_module("protocol.rs")
        # 验证 call_with_fd 函数体调用 send_msg + _recv_msg_with_fd
        assert "send_msg(sock, request, max_bytes)" in source, (
            "G21/G22: call_with_fd 未调用 send_msg"
        )
        assert "_recv_msg_with_fd(sock, max_bytes, max_fds)" in source, (
            "G21/G22: call_with_fd 未调用 _recv_msg_with_fd"
        )


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


class TestFeatureMatrixStatusRust:
    """_feature_matrix.md 中 6 个 Rust 接线条目状态应为 ✅。"""

    @pytest.mark.parametrize(
        "gid",
        ["G10", "G11", "G20", "G21", "G22", "G29"],
    )
    def test_feature_matrix_rust_status_updated(self, gid):
        """_feature_matrix.md 中对应条目状态应从 🟡 改为 ✅。"""
        matrix_path = os.path.join(ROOT, "_feature_matrix.md")
        source = _read_file(matrix_path)
        # 找形如 | G10 | ... | ✅ 已修复（2026-07-20 批次3） | ...
        pattern_prefix = f"| {gid} |"
        lines = source.split("\n")
        for line in lines:
            if line.startswith(pattern_prefix):
                assert "✅ 已修复（2026-07-20 批次3）" in line, (
                    f"{gid}: _feature_matrix.md 状态未更新为 ✅，line: {line.strip()}"
                )
                return
        pytest.fail(f"{gid}: _feature_matrix.md 中找不到该条目")
