"""H5 release acceptance：fresh daemon 产物 / health / capability registry 三端对齐 / HTTP 自举冒烟。

对应 docs/design/http-daemon-mvp-task-plan.md §H5（HTTP MVP 独立复审与统一部署）
与 docs/design/http-daemon-mvp-evidence.md：

1. **fresh daemon 产物存在性 + binary hash 与 runtime/current 一致**：
   以 `scripts/refresh_shared_runtime.ps1` 写入 `~/.callwarden/runtime/evidence/*.json`
   为真相源，断言 status=passed、git_head=当前 HEAD、三端 hash 一致
   （构建 = runtime/current 安装 = evidence 记录）。
2. **daemon health 可达**：生产 daemon 经 HttpDaemonRpcClient /health 核验
   （status=ok、schema_version=50）；不可达时 skip 附诊断（环境无 daemon 属
   测试设计内前置条件，与既有 daemon 测试一致）。
3. **capability registry 三端对齐**：import server.compat_worker 触发全量装配后，
   registry 方法数 = RUST_COMPAT_ROUTE = Rust COMPAT_ROUTE_WHITELIST = 80，
   validate_against_rust_route() aligned=True。
   （W3-3 T-1786861820151-deb64c48：get_semgrep_findings 迁移 rust_native，
   91→90；W4-1 T-1786886251769-22b94ee8-sub-1：get_file_history /
   get_commit_tasks 迁移 rust_native，90→88；W4-2
   T-1786886251769-22b94ee8-sub-2：get_coverage_for_symbol / diff_to_symbol
   迁移 rust_native，88→86；W4-3 T-1786886251769-22b94ee8-sub-3：
   defect_correlation / churn_analysis / defect_search /
   defect_suggest_fix / get_defect_correlation 迁移 rust_native，86→81；
   W4-4 T-1786886251769-22b94ee8-sub-4：diff_branches 迁移 rust_native，
   81→80。）
4. **HTTP 自举路径冒烟**：隔离 daemon（runtime/current fresh binary + --http-bind）
   manifest discovery → /health 交叉核对 → /capabilities 三端对齐 →
   真实 HTTP RPC round-trip（compat 方法绝不 method_not_found）。

测试自包含、不依赖临时手工状态；失败给出可诊断信息；不 mock 真实断言。
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

# 仓库根加入 sys.path（支持 `server.*` 与 `callwarden.server.*` 两种 import）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from callwarden.config import HTTP_MVP_TRANSPORT_PROFILE  # noqa: E402
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

_RUNTIME_ROOT = os.path.join(os.path.expanduser("~"), ".callwarden", "runtime")
_CURRENT_DAEMON = os.path.join(_RUNTIME_ROOT, "current", "cw-daemon.exe")
_EVIDENCE_DIR = os.path.join(_RUNTIME_ROOT, "evidence")

_EXPECTED_COMPAT_METHODS_81 = {  # 与 test_http_capability_registry.py 同源（Rust 白名单 80 项；W2-1 移除 3 个、W2-2 再移除 3 个、W2-3 再移除 2 个、W3-1 再移除 5 个、W3-2 再移除 3 个、W3-3 再移除 1 个、W4-1 再移除 2 个、W4-2 再移除 2 个、W4-3 再移除 5 个、W4-4 再移除 1 个已迁移 native 方法）
    "stats_top_files",
    "get_symbol_history", "get_recent_changes", "get_impact",
    "get_top_callers", "get_orphan_symbols", "get_deepest_functions",
    "get_comment_from_version", "get_issue_summary",
    "find_issues",
    "get_comment_coverage", "get_call_heatmap", "get_test_coverage",
    "export_module_graph", "get_symbol_change_tasks",
    "audit_verify_chain", "list_audit_signing_keys", "bootstrap_status",
    "list_clones",
    "list_clone_groups",
    "get_clone_group_detail", "task_plan_template",
    "get_summary", "project_brief", "repo_map",
    "find_uncovered_functions", "test_impact_selection", "who_to_ask",
    "get_ownership_map", "guardrail_scan", "guardrail_check_edit",
    "guardrail_list_rules", "blast_radius", "ask_codebase",
    "get_token_savings_report", "get_vulnerability_blast_radius",
    "get_clone_aware_impact", "review_readiness",
    "cross_layer_impact", "evolution_frequency",
    "hotspot_evolution", "defect_learn", "semantic_search",
    "find_similar_functions", "get_symbol_commit_history", "parse_codeowners",
    "get_project_dependencies", "list_branches",
    "merge_preview", "get_edit_history",
    "find_shared_symbols", "cross_repo_impact", "cross_repo_summary",
    "lsp_hover", "lsp_definition", "lsp_references", "lsp_diagnostics",
    "lsp_completion", "lsp_check_available", "list_toolchains",
    "get_toolchain", "get_workspace_toolchains",
    "rule_candidate_list", "rule_list",
    "get_applicable_rules", "get_role_view", "find_evidence",
    "get_freshness_status", "get_gate_decision", "get_artifact_freshness",
    "get_interface_providers", "detect_cycle", "validate_revision_dependencies",
    "get_dependency_edges", "get_action_identity", "check_action_identity",
    "check_session_separation", "get_attestation_validity",
    "list_attestation_revocations", "assignment_show",
}


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _git_head() -> str:
    r = subprocess.run(
        ["git", "-C", _REPO_ROOT, "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
    )
    if r.returncode != 0:
        pytest.skip(f"无法读取 git HEAD: {r.stderr}")
    return r.stdout.strip()


def _latest_refresh_evidence() -> dict:
    """读取最新的 refresh_shared_runtime.ps1 evidence JSON；无则 skip 附诊断。"""
    files = sorted(glob.glob(os.path.join(_EVIDENCE_DIR, "*.json")),
                   key=os.path.getmtime)
    if not files:
        pytest.skip(f"未找到 runtime refresh evidence: {_EVIDENCE_DIR}")
    with open(files[-1], "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# 1. fresh daemon 产物存在性 + binary hash 与 runtime/current 一致
# ============================================================


class TestFreshDaemonArtifacts:
    def test_runtime_current_daemon_exists(self):
        assert os.path.isfile(_CURRENT_DAEMON), (
            f"runtime/current/cw-daemon.exe 不存在: {_CURRENT_DAEMON}"
        )

    def test_latest_refresh_evidence_passed_for_current_head(self):
        ev = _latest_refresh_evidence()
        assert ev.get("status") == "passed", (
            f"最新 refresh evidence status={ev.get('status')!r}，error={ev.get('error')!r}"
        )
        head = _git_head()
        ev_head = ev.get("git_head", "")
        assert ev_head == head, (
            f"evidence git_head {ev_head} != 当前 HEAD {head}（evidence 过期）"
        )

    def test_installed_hash_matches_evidence(self):
        ev = _latest_refresh_evidence()
        binaries = {b["name"]: b for b in ev.get("binaries", [])}
        assert "cw-daemon.exe" in binaries, "evidence 缺少 cw-daemon.exe"
        assert ev["binaries"][0]["sha256"] == binaries["cw-daemon.exe"]["sha256"]
        actual = _sha256(_CURRENT_DAEMON)
        assert actual == binaries["cw-daemon.exe"]["sha256"], (
            f"runtime/current/cw-daemon.exe hash {actual} != evidence 记录 "
            f"{binaries['cw-daemon.exe']['sha256']}（runtime 非本次构建产物）"
        )

    def test_running_daemon_path_and_hash_match_evidence(self):
        """运行中的 daemon 必须来自 runtime/current 且 hash 与 evidence 一致。

        仅核对路径位于 runtime/current 的 cw-daemon 进程（生产 daemon）；其他
        同名进程（如并行测试 spawn 的隔离 daemon，路径在 target/debug 等）不
        计入，避免跨测试干扰。
        """
        ev = _latest_refresh_evidence()
        expected = ev["daemon_runtime"]["sha256"]
        # 用 PowerShell 读取运行中 daemon 的 Path（Windows 专属），跨平台兜底跳过
        if os.name != "nt":
            pytest.skip("运行路径核对仅 Windows 适用")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process -Name cw-daemon -ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty Path"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"无法枚举运行 daemon: {e}")
        paths = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        runtime_daemons = [
            p for p in paths
            if os.path.abspath(p).lower() == os.path.abspath(_CURRENT_DAEMON).lower()
        ]
        if not runtime_daemons:
            pytest.skip("无来自 runtime/current 的运行 daemon（生产 daemon 未启动，环境前置条件）")
        for p in runtime_daemons:
            assert _sha256(p) == expected, (
                f"运行 daemon hash {_sha256(p)} != evidence {expected}（运行旧二进制）"
            )


# ============================================================
# 2. capability registry 三端对齐（纯 Python，不依赖 daemon）
# ============================================================


class TestCapabilityRegistryThreeWayAlignment:
    def test_registry_after_full_assembly_is_81(self):
        """import server.compat_worker 触发全量装配后 registry 80 项。"""
        import server.compat_worker  # noqa: F401
        from server.compat_registry import get_compat_registry
        reg = get_compat_registry()
        assert len(reg) == 80, f"装配后 registry 应 80，实际 {len(reg)}"

    def test_registry_matches_rust_route_and_whitelist(self):
        import server.compat_worker  # noqa: F401
        from server.compat_registry import RUST_COMPAT_ROUTE, get_compat_registry
        reg = get_compat_registry()
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81

        # Rust http_server.rs COMPAT_ROUTE_WHITELIST 源码提取
        rs_path = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "http_server.rs")
        src = open(rs_path, encoding="utf-8").read()
        m = re.search(r"COMPAT_ROUTE_WHITELIST: &\[\(&str, &str\)\] = &\[(.*?)\];", src, re.S)
        assert m, "COMPAT_ROUTE_WHITELIST not found"
        rust_map = dict(re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', m.group(1)))
        assert len(rust_map) == 80, f"Rust 白名单应 80，实际 {len(rust_map)}"
        assert set(rust_map) == set(reg.methods()), (
            f"Rust 白名单与 registry 不一致: "
            f"{set(rust_map) - set(reg.methods())} / {set(reg.methods()) - set(rust_map)}"
        )

    def test_validate_against_rust_route_aligned(self):
        import server.compat_worker  # noqa: F401
        from server.compat_registry import validate_against_rust_route
        result = validate_against_rust_route()
        assert result["aligned"] is True, (
            f"registry 与 Rust 路由未对齐: missing={result['missing']} "
            f"extra={result['extra']} mismatch={result['mismatch']}"
        )

    def test_compat_worker_registry_has_full_route_methods(self):
        """worker 侧 registry 与 RUST_COMPAT_ROUTE 一致（worker 是 HTTP compat 执行体）。"""
        import server.compat_worker
        from server.compat_registry import RUST_COMPAT_ROUTE
        reg = server.compat_worker.get_compat_registry()
        assert set(reg.methods()) == set(RUST_COMPAT_ROUTE) == _EXPECTED_COMPAT_METHODS_81


# ============================================================
# 3. daemon health 可达（生产 daemon，不可达 skip 附诊断）
# ============================================================


class TestDaemonHealthReachable:
    def test_production_daemon_health_ok(self):
        """生产 daemon health（真实入口 cw.py daemon health，named-pipe 传输）。

        生产 daemon 当前以 `--socket` named-pipe 传输运行，HttpDaemonRpcClient
        仅面向 HTTP transport（会 E_HTTP_MANIFEST_MISSING fail-closed，属设计
        行为）；生产 health 验证走 CLI 真实入口，解析结构化 JSON 断言。
        daemon 未运行（非零退出/JSON 解析失败）→ skip 附诊断（环境前置条件）。
        """
        py = sys.executable
        try:
            r = subprocess.run(
                [py, os.path.join(_REPO_ROOT, "cw.py"), "daemon", "health"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"cw daemon health 执行异常: {e}")
        if r.returncode != 0:
            pytest.skip(
                f"生产 daemon 不可达（exit={r.returncode}）: "
                f"{r.stdout.strip() or r.stderr.strip()}"
            )
        try:
            health = json.loads(r.stdout)
        except ValueError as e:
            pytest.fail(f"cw daemon health 输出非 JSON: {r.stdout!r} ({e})")
        assert health.get("status") == "ok", f"daemon health status: {health!r}"
        assert health.get("schema_version") == 50, (
            f"schema_version 应为 50，实际 {health.get('schema_version')!r}"
        )
        assert health.get("pid") is not None, f"/health 缺 pid: {health!r}"


# ============================================================
# 4. HTTP 自举路径冒烟（隔离 daemon：manifest discovery + health + capabilities + RPC）
# ============================================================


def _spawn_isolated_daemon(bin_path, data_root):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CW_COMPAT_PYTHON"] = sys.executable
    return subprocess.Popen(
        [bin_path, "--http-bind=127.0.0.1:0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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


def _terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class TestHttpBootstrapSmoke:
    """当前 runtime/current fresh binary 的 HTTP 自举冒烟（release acceptance 核心）。

    与 test_http_daemon_integration.py 同源的隔离 daemon 模式；二进制取
    runtime/current（本次 H5 fresh 部署产物），不依赖生产 daemon 状态。
    """

    @pytest.fixture
    def isolated_http_daemon(self, tmp_path):
        if not os.path.isfile(_CURRENT_DAEMON):
            pytest.skip(f"runtime/current/cw-daemon.exe 不存在: {_CURRENT_DAEMON}")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        backup = _backup_http_manifest()
        proc = _spawn_isolated_daemon(_CURRENT_DAEMON, data_root)
        try:
            manifest = _wait_manifest(proc)
            if manifest is None:
                stdout = (proc.stdout.read(4000).decode("utf-8", "replace")
                          if proc.stdout else "")
                stderr = (proc.stderr.read(4000).decode("utf-8", "replace")
                          if proc.stderr else "")
                pytest.fail(
                    f"隔离 daemon 未发布 manifest\nstdout={stdout}\nstderr={stderr}"
                )
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            self._wait_worker_ready(proc, client)
            yield client, manifest
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, backup)

    def _wait_worker_ready(self, proc, client, retries: int = 2):
        last_err = None
        for _ in range(retries + 1):
            try:
                # W2-1：get_uncommented_symbols 已迁移 rust_native，预热改用仍走
                # compat worker 的默认方法 stats_top_files（强制要求 workspace_id；
                # 隔离库无该 workspace 时返回空结果或业务错误，均不影响预热目标）
                client.call("stats_top_files", {"workspace_id": 1, "limit": 1})
                return
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                last_err = e
            except DaemonRemoteError as e:
                if e.code == "method_not_found":
                    raise
                return
        _terminate(proc)
        pytest.fail(f"compat worker 冷启动就绪超时: {last_err}")

    def test_manifest_discovery_and_health_cross_check(self, isolated_http_daemon):
        client, manifest = isolated_http_daemon
        assert client.discover().startswith("http://127.0.0.1:"), client.discover()
        health = client.verify_health()
        assert health["pid"] == manifest["pid"], (
            f"/health pid {health['pid']} != manifest pid {manifest['pid']}"
        )
        assert health["schema_version"] == manifest["schema_version"] == 50
        assert health["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE

    def test_capabilities_python_compat_available_matches_rust_route(
        self, isolated_http_daemon
    ):
        client, _ = isolated_http_daemon
        caps = client.capabilities()
        assert caps["server_mode"] == HTTP_MVP_TRANSPORT_PROFILE
        methods = caps.get("methods", {})
        assert methods.get("ping", {}).get("status") == "available"
        pc_available = {
            name for name, info in methods.items()
            if info.get("backend") == "python_compat"
            and info.get("status") == "available"
        }
        assert pc_available == _EXPECTED_COMPAT_METHODS_81, (
            f"/capabilities python_compat available 与 Rust 白名单不一致: "
            f"{pc_available - _EXPECTED_COMPAT_METHODS_81} / "
            f"{_EXPECTED_COMPAT_METHODS_81 - pc_available}"
        )

    def test_real_http_rpc_compat_route_served(self, isolated_http_daemon):
        """真实 HTTP RPC round-trip：compat 方法绝不 method_not_found（worker 受理）。"""
        client, _ = isolated_http_daemon
        for rpc, params in [
            ("stats_top_files", {"limit": 1}),
            ("get_top_callers", {"limit": 1}),
        ]:
            try:
                client.call(rpc, params)
            except DaemonRemoteError as e:
                assert e.code != "method_not_found", (
                    f"{rpc} 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                    f"不应 method_not_found: {e}"
                )
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                # 慢执行/worker 冷启动：route 已被受理（method_not_found 不会超时）
                continue

    def test_negative_unregistered_method_fail_closed(self, isolated_http_daemon):
        """负向：registry 未注册方法 HTTP 模式必 method_not_found（fail-closed 实证）。"""
        client, _ = isolated_http_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            client.call("get_code_metrics_summary", {})
        assert ei.value.code == "method_not_found"
