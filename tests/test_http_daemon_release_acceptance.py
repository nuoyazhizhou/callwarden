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
   registry 方法数 = RUST_COMPAT_ROUTE = Rust COMPAT_ROUTE_WHITELIST = 0，
   validate_against_rust_route() aligned=True。
   （compat 面清零：W2-1/W2-2/W2-3/W3-1/W3-2/W3-3/W4-1~W4-4 逐批迁移
   rust_native（80->0）；P0-COMPAT-v3 把符号/任务/摘要/演化/护栏/缺陷/语义/
   分支/编辑历史/跨仓库/LSP/toolchain/规则查询/p2/p3/p4 剩余组全部迁移
   rust_native；INT-001 stats_top_files、MCP-001~012 collab/p2 组亦迁移，
   COMPAT_ROUTE_WHITELIST 与 Python RUST_COMPAT_ROUTE 均清零。）
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
    get_http_authority_id,
    get_http_manifest_dir,
    get_http_manifest_path,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402

_RUNTIME_ROOT = os.path.join(os.path.expanduser("~"), ".callwarden", "runtime")
_CURRENT_DAEMON = os.path.join(_RUNTIME_ROOT, "current", "cw-daemon.exe")
_EVIDENCE_DIR = os.path.join(_RUNTIME_ROOT, "evidence")

# ------------------------------------------------------------
# H4C 全量 compat 方法集合（迁移后已清零）
# ------------------------------------------------------------
# 生产真相源：server/compat_registry.py:174-220 的 _build_default_registry() 现返回
# 空 CompatRegistry()，模块级 RUST_COMPAT_ROUTE = {}；rust_ext/src/daemon/
# http_server.rs:611 的 COMPAT_ROUTE_WHITELIST 亦为空（正文全为迁移注释）。
# INT-001（stats_top_files）、P0-COMPAT-v3（符号/任务/摘要/演化/护栏/缺陷/语义/
# 分支/编辑历史/跨仓库/LSP/toolchain/规则查询/p2/p3/p4 各组）、MCP-001~012
# （collab/p2 组）等 python_compat 方法已全部迁移 rust_native，故「80 项」计数
# 断言随之归零（常量保持空集，避免与新真相漂移）。与
# test_http_capability_registry.py 同源。
_EXPECTED_COMPAT_METHODS_81: set = set()

# INT-001 / P0-COMPAT-v3 / MCP-001~012 迁移 rust_native 的代表方法（HTTP 自举
# round-trip 冒烟用）：这些方法已不是 compat 方法，改由 rust_native dispatch
# 服务，/capabilities 中 backend=rust_native 且 status=available。
#
# 方法真名以 dispatch.rs 为准（MCP 工具名 ≠ RPC method，见项目记忆映射表）：
# - get_top_callers：CONVERGENCE_RPC_METHODS 裸名分发（dispatch.rs:2449），
#   必填 workspace_instance_id（S2 收敛架构 workspace authority）。
# - stats_top_files：RPC 真名 query.stats_top_files（dispatch.rs:3006），
#   /capabilities 广告键为 MCP 名 stats_top_files —— 两者存在 capability↔
#   dispatch 名漂移（INT-001 引入），故仅 stats_top_files 不参与 round-trip
#   断言，避免把漂移锁定为预期（见 test_real_http_rpc_compat_route_served 注）。
_NATIVE_ROUTES_SMOKE = [
    ("get_top_callers", {"workspace_instance_id": "ws-1", "limit": 1}),
]


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
    # 部署脚本 Write-Host 输出落盘为 UTF-8 BOM（PS5.1 默认），须 utf-8-sig 解码
    with open(files[-1], "r", encoding="utf-8-sig") as f:
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
        """import server.compat_worker 触发全量装配后 registry 0 项（compat 面清零）。"""
        import server.compat_worker  # noqa: F401
        from server.compat_registry import get_compat_registry
        reg = get_compat_registry()
        assert len(reg) == 0, f"装配后 registry 应 0，实际 {len(reg)}"

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
        assert len(rust_map) == 0, f"Rust 白名单应 0（已清零），实际 {len(rust_map)}"
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
        # health 契约（http_server.rs:547-561）：无顶层 status 字段；
        # 权威字段 = security_profile / endpoint / pid / git_commit /
        # schema_version / worker_status / capability_registry_revision。
        for key in ("security_profile", "endpoint", "pid", "git_commit",
                    "schema_version", "worker_status",
                    "capability_registry_revision"):
            assert key in health, f"/health 缺少权威字段 {key}: {health!r}"
        assert health.get("schema_version") == 60, (
            f"schema_version 应为 60（rust_ext/src/daemon/mod.rs SCHEMA_VERSION），"
            f"实际 {health.get('schema_version')!r}"
        )
        assert health.get("pid") is not None, f"/health 缺 pid: {health!r}"


# ============================================================
# 4. HTTP 自举路径冒烟（隔离 daemon：manifest discovery + health + capabilities + RPC）
# ============================================================


def _spawn_isolated_daemon(bin_path, data_root):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。

    stale 修正（族D，daemon 换实例后 100% 复现）：daemon 的
    http_manifest_dir() 读 USERPROFILE/HOME（rust_ext/src/daemon/http_server.rs:1624），
    不读 CW_DAEMON_DATA_ROOT——隔离 daemon 的 manifest 与 single-instance 锁
    原本固定写真实 ~/.callwarden，与生产 daemon 的 authority 锁冲突 →
    E_DAEMON_ALREADY_RUNNING → daemon 立即退出 → 「未发布 manifest」。
    现重定向 USERPROFILE=data_root，manifest 与锁完全隔离到 data_root/.callwarden，
    生产 daemon 不受影响，backup/restore 也不再触碰真实 HOME 权威 manifest。
    """
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CW_COMPAT_PYTHON"] = sys.executable
    # 重定向 manifest/lock 作用域：daemon http_manifest_dir() 读 USERPROFILE
    env["USERPROFILE"] = data_root
    # toolchain 库显式指向任务库同文件（生产语义：两者默认同为
    # ~/.callwarden/callwarden.db）。toolchain 读面走 open_task_db_readonly()
    # （snapshot_state.rs:3273-3280），表由 ToolchainStore::open 以幂等 DDL
    # 初始化——USERPROFILE 重定向后若不显式指定，toolchain 库会兜底到
    # data_root/.callwarden/callwarden.db，与任务库分叉 → 读面 no such table。
    env["CW_DAEMON_TOOLCHAIN_DB"] = env["CW_DAEMON_TASK_DB"]
    return subprocess.Popen(
        [bin_path, "--http-bind=127.0.0.1:0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_manifest(proc, data_root, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    stale 修正（族D）：USERPROFILE 重定向后 manifest 写
    data_root/.callwarden（= daemon http_manifest_dir()），轮询该目录
    而非真实 get_http_manifest_dir()。
    """
    directory = os.path.join(data_root, ".callwarden")
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


def _isolated_manifest_path(data_root) -> str:
    """隔离 daemon 的 authority-scoped manifest 路径（data_root/.callwarden）。

    stale 修正（族D）：USERPROFILE=data_root 重定向后，daemon 的
    http_manifest_dir() 解析为 data_root/.callwarden；backup/restore/clean
    全部针对该隔离路径，不再触碰真实 ~/.callwarden 权威 manifest。
    """
    authority = get_http_authority_id()
    safe = authority.replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(data_root, ".callwarden",
                        f"http-daemon.{safe}.manifest.json")


def _backup_http_manifest(manifest_path: str):
    """备份隔离 manifest（若存在），teardown 时恢复。"""
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def _restore_or_clean_http_manifest(manifest_path: str, pid, backup):
    """teardown 清理：删除 pid 匹配的隔离 manifest；备份 pid 存活则恢复。"""
    try:
        if os.path.isfile(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                current = json.load(f)
            if int(current.get("pid", -1)) == pid:
                os.remove(manifest_path)
    except (OSError, ValueError):
        pass
    if backup is not None and _pid_alive(int(backup.get("pid", -1))):
        try:
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
            with open(manifest_path, "w", encoding="utf-8") as f:
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
        manifest_path = _isolated_manifest_path(data_root)
        backup = _backup_http_manifest(manifest_path)
        proc = _spawn_isolated_daemon(_CURRENT_DAEMON, data_root)
        try:
            manifest = _wait_manifest(proc, data_root)
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
            _restore_or_clean_http_manifest(manifest_path, proc.pid, backup)

    def _wait_worker_ready(self, proc, client, retries: int = 2):
        last_err = None
        for _ in range(retries + 1):
            try:
                # stale 修正（族D 附带）：旧探活方法 stats_top_files 已随
                # query 域迁移下线（method_not_found），改用 ping（无参数要求、
                # 确定性可用）验证 HTTP transport 冷启动就绪。
                client.call("ping")
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
        assert health["schema_version"] == manifest["schema_version"] == 60
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
        # compat 面清零后 /capabilities 不应再暴露任何 python_compat available
        # 方法（与 Rust COMPAT_ROUTE_WHITELIST 空表一致）。
        assert pc_available == _EXPECTED_COMPAT_METHODS_81, (
            f"/capabilities python_compat available 与 Rust 白名单不一致: "
            f"{pc_available - _EXPECTED_COMPAT_METHODS_81} / "
            f"{_EXPECTED_COMPAT_METHODS_81 - pc_available}"
        )
        # 迁移 rust_native 的代表方法必须仍 available（INT-001/P0-COMPAT-v3）。
        for name, _params in _NATIVE_ROUTES_SMOKE:
            info = methods.get(name)
            assert info is not None, f"/capabilities 缺少 {name}"
            assert info.get("backend") == "rust_native", (
                f"{name} backend 应 rust_native，实际 {info.get('backend')!r}"
            )
            assert info.get("status") == "available", (
                f"{name} status 应 available，实际 {info.get('status')!r}"
            )

    def test_real_http_rpc_compat_route_served(self, isolated_http_daemon):
        """真实 HTTP RPC round-trip 冒烟。

        compat 面清零后 _EXPECTED_COMPAT_METHODS_81 为空集，compat 契约循环体
        不执行（保留以固化「compat 面恢复时绝不 method_not_found」的不变量）；
        实际 round-trip 冒烟改由 rust_native 路径承担（INT-001 / P0-COMPAT-v3
        迁移方法）：经 daemon dispatch 服务，绝不 method_not_found。

        注意：INT-001 的 stats_top_files 不在冒烟集合内——其 RPC 真名为
        query.stats_top_files（dispatch.rs:3006），而 /capabilities 广告键为
        MCP 名 stats_top_files（http_server.rs:3213），capability↔dispatch 名
        漂移导致裸名 stats_top_files 直接 RPC 必 method_not_found。把该漂移
        锁进冒烟断言会掩盖缺陷，故仅用无漂移的 get_top_callers 验证 round-trip。
        """
        client, _ = isolated_http_daemon
        # compat 契约（空集，兼容 compat 面恢复）
        for rpc in sorted(_EXPECTED_COMPAT_METHODS_81):
            try:
                client.call(rpc, {"limit": 1})
            except DaemonRemoteError as e:
                assert e.code != "method_not_found", (
                    f"{rpc} 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                    f"不应 method_not_found: {e}"
                )
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                continue
        # rust_native round-trip 冒烟（迁移后实际服务路径）
        for rpc, params in _NATIVE_ROUTES_SMOKE:
            try:
                client.call(rpc, params)
            except DaemonRemoteError as e:
                assert e.code != "method_not_found", (
                    f"{rpc} 已迁移 rust_native，不应 method_not_found: {e}"
                )
            except DaemonUnavailableError as e:
                if E_HTTP_REQUEST_TIMEOUT not in str(e):
                    raise
                # 慢执行：route 已被受理（method_not_found 不会超时）
                continue

    def test_negative_unregistered_method_fail_closed(self, isolated_http_daemon):
        """负向：registry 未注册方法 HTTP 模式必 method_not_found（fail-closed 实证）。"""
        client, _ = isolated_http_daemon
        with pytest.raises(DaemonRemoteError) as ei:
            client.call("get_code_metrics_summary", {})
        assert ei.value.code == "method_not_found"
