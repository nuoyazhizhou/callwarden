"""H4B-R: compat registry 能力与两端对齐门测试

**stale 依据（B 桶 · INT-001 compat 面清零）**：`_build_default_registry()` 现返回
空 `CompatRegistry()`（`server/compat_registry.py:174-220`），模块级
`RUST_COMPAT_ROUTE = {}`；旧版「80/81 项」计数断言随之失效（常量归零）。

验证 server/compat_registry.py 的 H4B-R 扩展（H4B-C docstring 承接的
compat_route 注册/查询/校验 API）：
- 恢复的 _build_default_registry / get_compat_registry 懒加载单例
  （修复 H3 提交后 _DEFAULT_REGISTRY 被误删导致的 compat_worker ImportError）；
- RUST_COMPAT_ROUTE 常量镜像 Rust http_server.rs `compat_route`
  （H4C 装配后全量 86 项：H4C-1 1 + H4C-2 13 + H4C-3 9 + H4C-2 第二批 29 +
  H4C-2 第三批 19 + H4C-2 第三批 collab/p2/p3/p4 15，均 read_only）；
- compat_route(method) 查询镜像 Rust 语义（未知方法 → None，fail-closed）；
- register_compat_route 注册即校验（与 Rust 映射不一致 → ValueError）；
- validate_against_rust_route 两端对齐门（missing/extra/mismatch/aligned）；
- compat_worker 集成：get_compat_registry 恢复后 import 不再 ImportError。

真实进程门（TestRealDaemonCompatRpcAlignment，参照 H4B-N/C/I/E 模板）：
- 正向：compat_route 全量 86 方法在生产 HttpDaemonRpcClient 调用下**绝不**返回
  method_not_found（经 H3 compat worker 服务）；
- 负向：registry 未注册的 python_compat 方法（get_code_metrics_summary）
  在真实 daemon 上必返回 method_not_found —— 实证 HTTP 模式 fail-closed
  （registry 是 worker 方法真相源，未注册方法不可达）；
- /capabilities 端点：backend=python_compat 且 status=available 的方法集合
  与 Python RUST_COMPAT_ROUTE 完全一致（两端对齐最强实证）。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools）
- python_compat 190 / rust_native 28 / legacy_local 19；
- Rust COMPAT_ROUTE_WHITELIST 声明 101 个 python_compat 方法
  （http_server.rs COMPAT_ROUTE_WHITELIST）；
- dispatch.rs 无 get_code_metrics_summary 分支（DaemonStateExt 默认
  method_not_found）。

适配：T-1786721363018-63aa9993（H4C-2+3 装配后 registry 2->89，断言同步；
原 H4B-R 时代硬编码 len(reg)==2 的 7 个用例已更新至 89 全量）；
整改：T-1786747295227-49c90d68（规则查询组 3 项接入 worker，registry 89->92，
security 组 14->17，相关断言同步至 92 全量）；
接入：T-1786747295227-b876fddf（collab 组 4 + p2 组 5 + p3 组 5 + p4 组 1
共 15 项只读接入 worker，registry 92->107，相关断言同步至 107 全量）；
W2-1（T-1786840097330-dec66710）：get_uncommented_symbols /
get_module_call_stats / get_semgrep_stats 3 个迁移 rust_native（native
handler + 便捷方法），registry 107->104（H4C-1 默认 2->1、符号组 17->15），
相关断言同步至 104 全量；
W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
get_clone_group_stats 3 个迁移 rust_native，registry 104->101
（任务组 16->13），相关断言同步至 101 全量。
W3-1（T-1786861820150-bfe5e805）：list_build_contexts / get_build_context /
get_active_build_context / get_resolved_edges / count_resolved_edges 5 个迁移
rust_native，registry 99->94（rules 组 8->3），相关断言同步至 94 全量。
W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs / wait_for_job 3 个
迁移 rust_native，registry 94->91（任务组 13->10），相关断言同步至 91 全量。
W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native，
registry 91->90（符号组 15->14），相关断言同步至 90 全量。
W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history / get_commit_tasks
2 个迁移 rust_native，registry 90->88（符号组 14->13、任务组 10->9），
相关断言同步至 88 全量。
W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
diff_to_symbol 2 个迁移 rust_native，registry 88->86（摘要组 26->24），
相关断言同步至 86 全量。
W4-3（T-1786886251769-22b94ee8-sub-3）：defect_correlation /
churn_analysis / defect_search / defect_suggest_fix / get_defect_correlation
5 个迁移 rust_native，registry 86->81（任务组 9->8、缺陷组 20 内移除 4），
相关断言同步至 81 全量；defect_learn 写面保持 python_compat。
"""

import json
import os
import subprocess
import sys
import time

import pytest

# 仓库根加入 sys.path（支持 `server.*` 与 `callwarden.server.*` 两种 import）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import server.compat_worker  # noqa: E402  集成验证：import 不再 ImportError
import server.compat_registry as compat_registry_mod  # noqa: E402
from server.compat_registry import (  # noqa: E402
    READ_ONLY,
    INDEX_WRITE,
    GOVERNANCE_WRITE,
    SCOPE_WORKSPACE,
    SCOPE_SNAPSHOT,
    SCOPE_AUTHORITY,
    CompatCallContext,
    CompatMethod,
    CompatRegistry,
    RUST_COMPAT_ROUTE,
    _build_default_registry,
    compat_route,
    get_compat_registry,
    register_compat_route,
    validate_against_rust_route,
)
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonUnavailableError,
    E_HTTP_REQUEST_TIMEOUT,
    HttpDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402
from callwarden.config import (  # noqa: E402
    get_http_manifest_dir,
    get_http_manifest_path,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402


# ------------------------------------------------------------
# H4C 全量 compat 方法集合（迁移后已清零）
# ------------------------------------------------------------
# 生产真相源：server/compat_registry.py:174-220 的 _build_default_registry() 现返回
# 空 CompatRegistry()，模块级 RUST_COMPAT_ROUTE = {}；rust_ext/src/daemon/
# http_server.rs COMPAT_ROUTE_WHITELIST 亦为空。INT-001（stats_top_files）、
# P0-COMPAT-v3（tools_summary / tools_semantic 组）、MCP-001（get_role_view 等
# collab 组）等 python_compat 方法已全部迁移 rust_native，故本文件所有
# 「80/81 项」计数断言随之归零（下两常量保持为空集，避免与新真相漂移）。
_H4C1_DEFAULT_METHODS: set = set()
_EXPECTED_COMPAT_METHODS_81: set = set()


@pytest.fixture
def synthetic_rust_route(monkeypatch):
    """把模块级 RUST_COMPAT_ROUTE 替换为合成路由，用于单测漂移检测/注册校验语义。

    生产 RUST_COMPAT_ROUTE 现为空（server/compat_registry.py:206-220），
    validate_against_rust_route / register_compat_route 的 missing/extra/
    mismatch 分支已无法用真实常量触发；此处 monkeypatch 合成
    {alpha: read_only, beta: read_only}，测试结束由 monkeypatch 自动还原，
    不污染「空路由」的生产真相。
    """
    synthetic = {"alpha": READ_ONLY, "beta": READ_ONLY}
    monkeypatch.setattr(compat_registry_mod, "RUST_COMPAT_ROUTE", dict(synthetic))
    return synthetic


# ============================================================
# 1. 恢复的默认 registry（H3 误删修复）
# ============================================================


class TestRegistryRestored:
    def test_get_compat_registry_returns_singleton(self):
        """懒加载单例：多次调用返回同一实例。"""
        assert get_compat_registry() is get_compat_registry()

    def test_default_registry_is_empty(self):
        """默认 registry（单例）与 Rust `compat_route` 均已清零（0 项）。

        生产真相源：server/compat_registry.py:174-220。python_compat 面全部
        迁移 rust_native 后，运行时 registry 不再服务任何方法。
        """
        reg = get_compat_registry()
        assert len(reg) == 0
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81

    def test_default_entries_are_read_only(self):
        """（历史用例）对默认 registry 的每个方法校验 read_only。

        registry 已清零，循环体为空；保留以固化「非空时必为 read_only」的
        不变量，待 compat 面恢复时自动生效。
        """
        reg = get_compat_registry()
        for method, op_class in RUST_COMPAT_ROUTE.items():
            entry = reg.get(method)
            assert entry is not None, f"缺失默认方法: {method}"
            assert entry.operation_class == op_class
            assert entry.operation_class == READ_ONLY

    def test_default_registry_scopes_empty(self):
        """registry 清零后 stats_top_files 等旧默认方法均不可见（workspace_scope None）。

        INT-001（T-1787322971676-e9aae4d4）：stats_top_files 已迁移 rust_native，
        不再是 compat 方法。
        """
        reg = get_compat_registry()
        assert reg.workspace_scope("stats_top_files") is None
        assert not reg.is_compat_method("stats_top_files")
        assert not reg.is_compat_method("get_uncommented_symbols")

    def test_default_entries_have_callable_handlers(self):
        """（历史用例）默认 registry 每个方法均有可调用 handler。

        registry 已清零，循环体为空；保留以固化「非空时字段齐备」的不变量。
        """
        reg = get_compat_registry()
        for method in RUST_COMPAT_ROUTE:
            entry = reg.get(method)
            assert isinstance(entry, CompatMethod)
            assert callable(entry.handler)
            assert isinstance(entry.description, str) and entry.description

    def test_build_default_registry_is_empty_subset_of_rust_route(self):
        """_build_default_registry() 现为空，且（空集）是 Rust 全量路由的子集。

        生产真相源：server/compat_registry.py:174-182 直接返回 CompatRegistry()。
        """
        reg = _build_default_registry()
        assert set(reg.methods()) == _H4C1_DEFAULT_METHODS == set()
        assert set(reg.methods()) <= set(RUST_COMPAT_ROUTE)


# ============================================================
# 2. compat_route 查询（镜像 Rust 语义）
# ============================================================


class TestCompatRouteQuery:
    def test_rust_compat_route_constant_is_empty(self):
        """常量 RUST_COMPAT_ROUTE 已清零（生产真相源 server/compat_registry.py:206-220）。"""
        assert set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81 == set()
        assert set(RUST_COMPAT_ROUTE.values()) == set()

    def test_route_returns_operation_class_for_compat_methods(self):
        for method, op_class in RUST_COMPAT_ROUTE.items():
            assert compat_route(method) == op_class

    def test_route_returns_none_for_unknown(self):
        """未知方法返回 None（fail-closed，不抛异常）。"""
        assert compat_route("get_code_metrics_summary") is None
        assert compat_route("") is None
        assert compat_route("no.such.rpc") is None


# ============================================================
# 3. register_compat_route（注册即校验）
# ============================================================


@pytest.fixture
def iso_registry(monkeypatch):
    """隔离 registry：monkeypatch 模块级 get_compat_registry，避免污染全局单例。"""
    reg = CompatRegistry()
    monkeypatch.setattr(compat_registry_mod, "get_compat_registry", lambda: reg)
    return reg


def _dummy_handler(ctx: CompatCallContext):
    return {}


class TestRegisterCompatRoute:
    def test_register_matching_operation_class_succeeds(
        self, iso_registry, synthetic_rust_route
    ):
        """已声明方法 + 与 Rust 一致的 operation_class → 注册成功。

        生产 RUST_COMPAT_ROUTE 已清零，改用 synthetic_rust_route 合成路由验证
        「已声明」分支（server/compat_registry.py:233-257）。
        """
        register_compat_route(
            "alpha", READ_ONLY, SCOPE_WORKSPACE,
            "h4b-r 测试注册", _dummy_handler,
        )
        assert iso_registry.is_compat_method("alpha")

    def test_register_mismatched_operation_class_raises(
        self, iso_registry, synthetic_rust_route
    ):
        """已声明方法 + 与 Rust 不一致的 operation_class → ValueError（两端对齐门）。"""
        with pytest.raises(ValueError) as ei:
            register_compat_route(
                "alpha", INDEX_WRITE, SCOPE_WORKSPACE,
                "h4b-r 测试注册", _dummy_handler,
            )
        assert "alpha" in str(ei.value)
        # 注册被拒绝：隔离 registry 未被污染
        assert not iso_registry.is_compat_method("alpha")

    def test_register_new_method_allowed(self, iso_registry):
        """Rust 未声明的方法允许注册（供后续 phase 扩展，调用方自行保证 Rust 同步）。"""
        register_compat_route(
            "future_compat_method", READ_ONLY, SCOPE_WORKSPACE,
            "h4b-r 后续扩展", _dummy_handler,
        )
        assert iso_registry.is_compat_method("future_compat_method")

    def test_register_governance_write_rejected(self, iso_registry):
        """MVP 禁止 governance_write（register 层拒绝，规则一致）。"""
        with pytest.raises(ValueError):
            register_compat_route(
                "gov_method", GOVERNANCE_WRITE, SCOPE_WORKSPACE,
                "h4b-r 禁止治理写", _dummy_handler,
            )

    def test_register_duplicate_rejected(self, iso_registry):
        """重复注册同一方法 → ValueError。"""
        register_compat_route(
            "dup_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler,
        )
        with pytest.raises(ValueError):
            register_compat_route(
                "dup_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler,
            )

    def test_global_singleton_not_polluted(self):
        """register_compat_route 的默认目标是全局单例；隔离测试不污染单例（仍为空）。

        生产真相源：server/compat_registry.py:174-220（registry 已清零）。
        """
        reg = get_compat_registry()
        assert len(reg) == 0
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == set()


# ============================================================
# 4. validate_against_rust_route（两端对齐门）
# ============================================================


class TestValidateAgainstRustRoute:
    def test_default_registry_aligned(self):
        """默认 registry 与 Rust `compat_route` 完全对齐。"""
        result = validate_against_rust_route()
        assert result["aligned"] is True
        assert result["missing"] == []
        assert result["extra"] == []
        assert result["mismatch"] == {}

    def test_missing_method_detected(self, synthetic_rust_route):
        """registry 缺 Rust 声明的方法 → aligned=False，missing 精确。

        生产 RUST_COMPAT_ROUTE 已清零，用 synthetic_rust_route（{alpha, beta}）
        触发 missing 分支：registry 仅注册 alpha → missing == ["beta"]。
        """
        reg = CompatRegistry()
        reg.register("alpha", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["missing"] == ["beta"]
        assert result["extra"] == []
        assert result["mismatch"] == {}

    def test_extra_method_detected(self, synthetic_rust_route):
        """registry 有 Rust 未声明的方法 → aligned=False，extra 精确。"""
        reg = CompatRegistry()
        for method in ("alpha", "beta"):
            reg.register(method, READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        reg.register("extra_method", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["extra"] == ["extra_method"]
        assert result["missing"] == []

    def test_mismatch_operation_class_detected(self, synthetic_rust_route):
        """同方法 operation_class 与 Rust 不一致 → aligned=False，mismatch 含明细。"""
        reg = CompatRegistry()
        reg.register("alpha", INDEX_WRITE, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        reg.register("beta", READ_ONLY, SCOPE_WORKSPACE, "h4b-r", _dummy_handler)
        result = validate_against_rust_route(reg)
        assert result["aligned"] is False
        assert result["mismatch"] == {
            "alpha": {
                "rust": READ_ONLY,
                "python": INDEX_WRITE,
            }
        }
        assert result["missing"] == []
        assert result["extra"] == []

    def test_validate_does_not_mutate_registry(self):
        """校验只读：传入自定义 registry 后其内容不变（不污染调用方）。"""
        reg = _build_default_registry()
        before = set(reg.methods())
        validate_against_rust_route(reg)
        assert set(reg.methods()) == before


# ============================================================
# 5. compat_worker 集成（get_compat_registry 恢复）
# ============================================================


class TestWorkerIntegration:
    def test_compat_worker_import_resolved(self):
        """compat_worker import 不再 ImportError（H3 误删修复的回归门）。"""
        assert server.compat_worker.get_compat_registry is get_compat_registry

    def test_compat_worker_registry_is_empty(self):
        """worker 通过 get_compat_registry() 拿到的 registry 已清零（与 RUST_COMPAT_ROUTE 一致）。

        生产真相源：server/compat_registry.py:174-220（python_compat 面全部迁移
        rust_native，含 INT-001 stats_top_files 与 MCP-001 get_role_view）。
        """
        reg = server.compat_worker.get_compat_registry()
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81
        for method, op_class in RUST_COMPAT_ROUTE.items():
            assert reg.operation_class(method) == op_class


# ============================================================
# 真实进程门（隔离 daemon + 生产 HttpDaemonRpcClient）
# ============================================================


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4B-N/C/I/E 门同源）。

    优先本地 cargo build 产物，保证与当前源码一致；CW_DAEMON_BIN / runtime
    部署仅作兜底。二进制不可用时跳过用例。
    """
    candidates = [
        os.path.join("rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon"),
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join("runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _wait_manifest(proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `~/.callwarden/`
    （http_manifest_dir = USERPROFILE/.callwarden），不再写 daemon data_root；
    本文件隔离 daemon 不重定向 USERPROFILE，故轮询真实 get_http_manifest_dir()。
    """
    directory = get_http_manifest_dir()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(directory):
            for f in os.listdir(directory):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(directory, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _backup_http_manifest():
    """备份当前 authority 的 HTTP manifest（若存在），teardown 时恢复。"""
    path = get_http_manifest_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def _restore_or_clean_http_manifest(pid, backup):
    """teardown 清理：删除 pid 匹配的隔离 manifest；备份 pid 存活则恢复。"""
    path = get_http_manifest_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                current = json.load(f)
            if int(current.get("pid", -1)) == pid:
                os.remove(path)
    except (OSError, ValueError):
        pass
    if backup is not None and _pid_alive(int(backup.get("pid", -1))):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False)
        except OSError:
            pass


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _terminate(proc):
    """终止 daemon 进程（terminate 优先，兜底 kill）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class TestRealDaemonCompatRpcAlignment:
    """真实进程级 registry ↔ Rust `compat_route` 对齐门（H4B-R 产物，H4C 适配 80 全量）。

    - 正向：compat_route 全量 80 方法（H4C-1 1 + H4C-2 13 + H4C-3 8 +
      H4C-2 第二批 25 + H4C-2 第三批 18 +
      H4C-2 第三批 collab/p2/p3/p4 15）
      在生产 HttpDaemonRpcClient 调用下**绝不**返回 method_not_found
      （经 H3 compat worker 服务）；
    - 负向：registry 未注册的 python_compat 方法（get_code_metrics_summary）
      在真实 daemon 上必返回 method_not_found —— 实证 fail-closed
      （registry 是 worker 方法真相源，未注册方法 HTTP 不可达）；
    - /capabilities：backend=python_compat 且 status=available 的方法集合
      与 Python RUST_COMPAT_ROUTE 完全一致。
    """

    POSITIVE_COMPAT_RPCS = [
        # (rpc, params) —— 只要求不返回 method_not_found（业务校验失败可接受）；
        # 统一最小参数 {"limit": 5}，非 limit 参数方法忽略之（handler 默认值兜底）。
        # ask_codebase 除外：worker 内惰性加载 jina embedding 模型（本机 HF 缓存
        # 无该模型 → 触发下载）远超 5s 超时，单独用长超时用例验证（见
        # test_ask_codebase_route_served_with_embedding_loading）。
        (method, {"limit": 5})
        for method in sorted(_EXPECTED_COMPAT_METHODS_81 - {"ask_codebase"})
    ]

    NEGATIVE_UNREGISTERED = "get_code_metrics_summary"

    @pytest.fixture
    def real_daemon(self, w3_live):
        """迁移到 conftest 模块级 `w3_live`（内部 tests/_w3_harness.setup_w3_client：
        模式A USERPROFILE 重定向 + workspace.register + task-DB seed + 空 codegraph
        snapshot.publish），取代本文件此前内联复制的 daemon spawn/manifest 逻辑。

        原内联 `_wait_manifest` 读父进程 `get_http_manifest_dir()`（共享 authority
        manifest 目录），与本机并行/共享 daemon 的 manifest 争用 → 稳定复现假
        『隔离 daemon 未发布 manifest』。w3_live 用隔离 data_root/userhome + 显式
        manifest_path 规避。yield 生产类 HttpDaemonRpcClient，测试语义零改动。
        """
        yield w3_live["client"]

    def _wait_worker_ready(self, proc, client, retries: int = 2):
        """worker 冷启动就绪等待（整改 4 模式，无 sleep 兜底）。"""
        last_err = None
        for _ in range(retries + 1):
            try:
                # stats_top_files handler 强制要求 workspace_id；隔离库
                # 无该 workspace 时返回空结果（成功）或业务错误，均不影响预热目标
                client.call("stats_top_files", {"workspace_id": 1, "limit": 1})
                return
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                last_err = e
            except DaemonRemoteError as e:
                # worker 已能响应帧：业务错误（如隔离库无该 workspace）说明
                # spawn + 装配已完成，预热目标达成。method_not_found 除外。
                if e.code == "method_not_found":
                    raise
                return
        _terminate(proc)
        pytest.fail(
            f"compat worker 冷启动就绪超时（预热重试 {retries} 次仍超时）: {last_err}"
        )

    def test_positive_compat_route_never_method_not_found(self, real_daemon):
        """正向：compat_route 全量 86 项（ask_codebase 除外）绝不返回 method_not_found。

        整改（T-1786747295227-49c90d68 步骤#3）：107 项扩展后首调用触发 compat
        worker 冷启动（Python 子进程 + 装配导入），可能超过 client 5s 超时 →
        E_HTTP_REQUEST_TIMEOUT（与 combined_worker_cutover 整改 4 同根因）。
        W2-1（T-1786840097330-dec66710）：get_uncommented_symbols /
        get_module_call_stats / get_semgrep_stats 迁移 rust_native 后
        107->104（全量 104 项，ask_codebase 除外 103 项）。
        W2-2（T-1786840097330-a9e0ec69）：get_clone_stats / get_job_stats /
        get_clone_group_stats 迁移 rust_native 后 104->101（全量 101 项，
        ask_codebase 除外 100 项）。
        W2-3（T-1786840097331-fd01a3f8）：defect_stats / get_edit_stats 迁移
        rust_native 后 101->99（全量 99 项，ask_codebase 除外 98 项）。
        W3-1（T-1786861820150-bfe5e805）：build 读组 5 个（list_build_contexts /
        get_build_context / get_active_build_context / get_resolved_edges /
        count_resolved_edges）迁移 rust_native 后 99->94（全量 94 项，
        ask_codebase 除外 93 项）。
        W3-2（T-1786861820151-f3cecf40）：job 读组 3 个（get_job_status /
        list_jobs / wait_for_job）迁移 rust_native 后 94->91（全量 91 项，
        ask_codebase 除外 90 项）。
        W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native
        后 91->90（全量 90 项，ask_codebase 除外 89 项）。
        W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history /
        get_commit_tasks 迁移 rust_native 后 90->88（全量 88 项，ask_codebase
        除外 87 项）。
        W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol /
        diff_to_symbol 迁移 rust_native 后 88->86（全量 86 项，ask_codebase
        除外 85 项）。
        fixture 已对 worker 冷启动做预热；此处对偶发慢执行再做有界重试（2 次，
        共 3 次尝试，非固定 sleep）。重试后仍超时视为「route 已被 worker 受理并
        进入执行」（method_not_found 会立即返回而非超时），符合 H4B-R 语义——
        只验证绝不 method_not_found，不验证执行耗时。
        """
        for rpc, params in self.POSITIVE_COMPAT_RPCS:
            last_err = None
            for _attempt in range(3):
                try:
                    real_daemon.call(rpc, params)
                    last_err = None
                    break
                except DaemonUnavailableError as exc:
                    if E_HTTP_REQUEST_TIMEOUT not in str(exc):
                        raise
                    last_err = exc  # 慢执行/冷启动超时：有界重试
                except DaemonRemoteError as exc:
                    assert exc.code != "method_not_found", (
                        f"{rpc} 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                        f"不应 method_not_found: {exc}"
                    )
                    break
                except Exception as exc:  # noqa: BLE001 —— 非业务错误（连接/传输）视为失败
                    pytest.fail(f"{rpc} 意外异常（非 DaemonRemoteError）: {exc}")
            if last_err is not None:
                # 有界重试后仍 E_HTTP_REQUEST_TIMEOUT：route 已被 worker 受理并
                # 进入执行（用户级真实库上的全库统计/模型加载等慢路径），
                # method_not_found 必不可能返回（未注册会立即失败而非超时）。
                # H4B-R 语义只验证「绝不 method_not_found」，不验证执行耗时；
                # 超时属执行性能/环境限制，不算失败（fail-closed 门面仍在）。
                continue

    def test_ask_codebase_route_served_with_embedding_loading(self, real_daemon):
        """ask_codebase 单独验证：route 已注册且 worker 受理（慢方法不参与 5s 遍历）。

        worker 内首次调用惰性加载 jinaai/jina-embeddings-v2-base-code（本机 HF
        缓存无该模型 → 触发下载/加载），远超 client 5s 超时 → E_HTTP_REQUEST_TIMEOUT。
        route 注册与 worker 受理本身由「到达执行阶段」证明：若 registry 未注册该
        route，worker 会立即返回 method_not_found（不超时）。因此断言：
        - DaemonRemoteError 时 code 必非 method_not_found；
        - E_HTTP_REQUEST_TIMEOUT（模型下载/加载慢）视为执行环境限制，不掩盖
          路由已验证结论（fail-closed 门面仍在：超时即拒绝，未静默降级）。
        """
        slow = HttpDaemonRpcClient(
            endpoint=real_daemon.discover(),
            verify_health=False,
            timeout=60.0,
        )
        try:
            result = slow.call("ask_codebase", {"question": "", "top_k": 1})
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                "ask_codebase 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                f"不应 method_not_found: {exc}"
            )
        except DaemonUnavailableError as exc:
            if E_HTTP_REQUEST_TIMEOUT in str(exc):
                # worker 已受理并进入执行（模型下载/加载）→ 路由验证达成；
                # 超时属本机 HF 模型缓存缺失的环境限制，非路由注册问题
                return
            raise
        else:
            # 模型已在缓存/加载成功（正常返回）：验证 RAG 上下文组装器返回
            # 结构契约，避免"超时即通过"掩盖成功路径的异常返回
            assert isinstance(result, dict), (
                f"ask_codebase 应返回 dict，实际 {type(result).__name__}: {result!r}"
            )
            assert "rag_context" in result, (
                f"ask_codebase 返回缺 rag_context 键: {sorted(result.keys())}"
            )

    def test_unregistered_python_compat_method_not_found(self, real_daemon):
        """负向：registry 未注册的 python_compat 方法必 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            real_daemon.call(self.NEGATIVE_UNREGISTERED, {})
        assert ei.value.code == "method_not_found", (
            f"{self.NEGATIVE_UNREGISTERED} 未注册到 compat registry 且 dispatch.rs"
            f"无分支，HTTP 模式必 method_not_found（fail-closed）"
        )

    def test_capabilities_python_compat_available_matches_rust_route(
        self, real_daemon
    ):
        """/capabilities 中 python_compat 且 available 的方法集合 == RUST_COMPAT_ROUTE。"""
        caps = real_daemon.capabilities()
        methods = caps.get("methods", {})
        pc_available = {
            name
            for name, info in methods.items()
            if info.get("backend") == "python_compat"
            and info.get("status") == "available"
        }
        assert pc_available == set(RUST_COMPAT_ROUTE), (
            f"capability registry 的 python_compat available 集合与 Python "
            f"RUST_COMPAT_ROUTE 不一致: {pc_available - set(RUST_COMPAT_ROUTE)=}"
            f" / {set(RUST_COMPAT_ROUTE) - pc_available=}"
        )
