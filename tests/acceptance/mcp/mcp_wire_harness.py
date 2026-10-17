"""TP-10M MCP wire 线束（验收套件基础设施）。

本模块是 TP-10M「MCP 全工具 wire 语义/参数合同」验收套件的唯一基建：

1. 隔离部署：在 run 根（C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10M）
   内拷贝一份 daemon 二进制（散列须命中 KNOWN_DAEMON_SHA256_PREFIXES 之一，
   全量 sha256 记入证据；候选来源优先级见 _DAEMON_CANDIDATES），全部 HOME/USERPROFILE/CALLWARDEN_*
   环境重定向到 run 根，CW_DAEMON_BIN 钉到本 run 根内的拷贝——MCP server
   自启动的 daemon 因此完全隔离，绝不发现/连生产 daemon（沿用 TP-08M 已证路径）。
2. 真实 wire：以子进程启动 `cw.py server`（FastMCP stdio 传输），经 mcp 官方
   客户端 stdio_client + ClientSession 说 wire 协议。会话托管在后台事件循环
   线程里（本机无 pytest-asyncio，且 ClientSession 必须在创建它的同一循环内
   使用），测试线程经 run_coroutine_threadsafe 投递调用。
3. 通过 wire 播种：register_workspace → build_graph → 查询/搜索工具学到真实
   符号名，装配 param_provider.SeedContext；再对隔离 codegraph 库补 5 张读面
   表（镜像 run_t2_t3_evidence._ensure_codegraph_seed_tables），否则
   build-context/clone 读面会因缺表产生假 DEFECT。

零生产触碰：所有写落在 run 根内（fail-closed 路径校验）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pytest
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ------------------------------------------------------------------ 路径
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]  # tests/acceptance/mcp -> repo root
CONV = REPO_ROOT / "tests" / "convergence"
SEED_SAMPLE = CONV / "seed_sample"
FROZEN_INVENTORY = (REPO_ROOT / "docs" / "testing" / "manifests" / "frozen"
                    / "inventory_frozen.json")
MCP_SCHEMA_FIXTURE = CONV / "fixtures" / "mcp_full_schema.json"
INVENTORY_FIXTURE = CONV / "fixtures" / "audit_mcp_inventory.json"

RUN_ROOT = Path(os.environ.get(
    "CW_TP10M_RUN_ROOT", r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10M"))
# daemon 候选来源（按优先级）：散列须命中 KNOWN_DAEMON_SHA256_PREFIXES。
# 注：TP-10/src 的拷贝可能被并发流程替换（2026-10-13 13:22 观察到被换成
# 247b9a16…，即 CONVERGENCE_RPC_METHODS 修复版），此时回落到仓库 release
# 构建——它与原始验收散列 f1fa3736… 一致。两者均验收通过，MCP 工具行为相同。
_DAEMON_CANDIDATES: tuple[Path, ...] = tuple(
    Path(p) for p in (
        os.environ.get("CW_TP10M_SRC_DAEMON", ""),
        r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10/src/cw-daemon.exe",
        str(REPO_ROOT / "rust_ext" / "target" / "release" / "cw-daemon.exe"),
        str(REPO_ROOT / "rust_ext" / "target" / "debug" / "cw-daemon.exe"),
    ) if p
)
# 已验收过的 daemon 二进制 sha256 前缀（全量记录在证据 meta 里）：
#   f1fa3736… —— 原始验收构建（TP-10M/src 与仓库 release target 同散列）
#   247b9a16… —— CONVERGENCE_RPC_METHODS 修复版（仅 CLI 路径，不影响 MCP 工具）
KNOWN_DAEMON_SHA256_PREFIXES = (
    "f1fa3736a16dbe7d",
    "247b9a16049ec4d7",
)


def _sha_is_known(sha: str) -> bool:
    return any(sha.startswith(p) for p in KNOWN_DAEMON_SHA256_PREFIXES)
PYTHON = os.environ.get("CW_TP10M_PYTHON", r"C:/Python314/python.exe")
CALLWARDEN_LANG = "en_US"

for _p in (str(REPO_ROOT), str(CONV)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import param_provider as pp  # noqa: E402

EXPECTED_TOOL_COUNT = 243
# 单次 call_tool 超时（秒）。重操作在 SKIP_TOOLS 里已排除；播种的 build_graph
# 单独走更长的 _SEED_BUILD_TIMEOUT。
CALL_TIMEOUT = 120.0
SEED_BUILD_TIMEOUT = 300.0


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ 隔离环境
class HarnessError(RuntimeError):
    """线束不变量违反。"""


def isolated_env(run_root: Path, daemon_exe: Path) -> dict:
    """MCP server 子进程环境：全部归属重定向到 run 根，daemon 钉到本 run 根拷贝。

    沿用 TP-08M 已证配方：不覆盖 CW_DAEMON_BIN 则 server 会按 ambient 环境去
    启动**生产 daemon**（_find_daemon_binary 的 env 优先级）。
    """
    env = dict(os.environ)
    home = run_root / "home"
    (home / ".callwarden").mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["CALLWARDEN_HOME"] = str(home)
    env["CALLWARDEN_DB"] = str(home / ".callwarden" / "callwarden.db")
    env["CALLWARDEN_DAEMON_REGISTRY_DB"] = str(
        home / ".callwarden" / "daemon" / "registry.db")
    # 不让 server 发现/连生产 daemon
    for k in ("CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND", "CW_DAEMON_BINARY",
              "CW_DAEMON_HTTP_ENDPOINT", "CW_DAEMON_SOCKET", "CW_DAEMON_TASK_DB"):
        env.pop(k, None)
    env["CW_DAEMON_BIN"] = str(daemon_exe)
    env["CALLWARDEN_LANG"] = CALLWARDEN_LANG
    env["PYTHONUTF8"] = "1"
    return env


# ------------------------------------------------------------------ codegraph 补种
_CODEGRAPH_DDL = [
    """CREATE TABLE IF NOT EXISTS workspace_build_contexts (
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    name TEXT DEFAULT '',
    compile_flags TEXT DEFAULT '[]',
    defines TEXT DEFAULT '{}',
    include_paths TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, build_context_hash)
)""",
    """CREATE TABLE IF NOT EXISTS resolved_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    caller_symbol_id INTEGER NOT NULL,
    callee_symbol_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL,
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    resolution_method TEXT DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id, call_line)
)""",
    "CREATE INDEX IF NOT EXISTS idx_build_contexts_ws ON workspace_build_contexts(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_build_contexts_active ON workspace_build_contexts(workspace_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_resolved_edges_ws_ctx ON resolved_edges(workspace_id, build_context_hash)",
    "CREATE INDEX IF NOT EXISTS idx_resolved_edges_caller ON resolved_edges(caller_symbol_id)",
    "CREATE INDEX IF NOT EXISTS idx_resolved_edges_callee ON resolved_edges(callee_symbol_id)",
    """CREATE TABLE IF NOT EXISTS clone_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    group_hash TEXT NOT NULL,
    clone_type INTEGER NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    similarity REAL DEFAULT 0.0,
    representative_symbol_id INTEGER NOT NULL,
    member_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, group_hash)
)""",
    """CREATE TABLE IF NOT EXISTS clone_group_members (
    group_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, symbol_id)
)""",
    """CREATE TABLE IF NOT EXISTS clone_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    symbol_a_id INTEGER NOT NULL,
    symbol_b_id INTEGER NOT NULL,
    clone_type INTEGER NOT NULL,
    similarity REAL NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    lines_a INTEGER DEFAULT 0,
    lines_b INTEGER DEFAULT 0,
    detected_at REAL NOT NULL
)""",
]


def ensure_codegraph_seed_tables(codegraph_db: Path) -> None:
    """隔离 codegraph 库补 5 张读面表（镜像 run_t2_t3_evidence）。

    daemon 的 build_graph 只写符号/边主表；build-context / clone 读面查的
    5 表在全新库中不存在，不补则相关读工具假 DEFECT（no such table）。
    """
    import sqlite3

    codegraph_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(codegraph_db), timeout=15)
    try:
        for stmt in _CODEGRAPH_DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ 结果模型
@dataclass
class WireCall:
    """一次真实 wire call_tool 的完整结果。"""

    name: str
    arguments: dict
    # wire 层是否拿到了 CallToolResult（True）还是传输/协议层失败（False）
    delivered: bool
    isError: bool | None = None
    # 解析后的业务载荷：content 里 JSON 文本 / structuredContent；非 JSON 则 None
    payload: Any = None
    # 业务 error 文本（{"error": ...} / success=false / isError 时提取）
    error_text: str = ""
    # 内容块是否全部是可解析 JSON（结构化）还是纯文本
    structured: bool = False
    content_kinds: list[str] = field(default_factory=list)
    exc_type: str = ""
    exc_message: str = ""

    @property
    def ok(self) -> bool:
        """wire 层成功且工具未报错。"""
        return self.delivered and not self.isError and not self.error_text


def _extract_error_text(obj: Any) -> str:
    """从已解析 JSON 载荷里找业务 error 文本。"""
    if isinstance(obj, dict):
        for key in ("error", "message", "detail"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                inner = _extract_error_text(v)
                if inner:
                    return inner
        if obj.get("success") is False:
            return str(obj.get("error") or obj.get("message") or "success=false")
    return ""


# ------------------------------------------------------------------ 线束
class McpWireHarness:
    """一个隔离的 MCP wire 会话生命周期。

    用法（测试侧同步）：
        h = McpWireHarness()
        h.start()                 # 拉起 server 子进程 + wire 会话
        ctx = h.seed()            # 通过 wire 播种 workspace
        r = h.call("get_callers", {"callee_name": "add"})
        h.stop()
    """

    def __init__(self, run_root: Path = RUN_ROOT) -> None:
        self.run_root = Path(run_root)
        self.daemon_exe = self.run_root / "src" / "cw-daemon.exe"
        self.home = self.run_root / "home"
        self.reports_dir = self.run_root / "reports"
        self.seed_dir = self.run_root / "ws" / "seed"
        self.codegraph_db = self.home / ".callwarden" / "callwarden.db"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._init_result: Any = None
        self.tools: list[Any] = []
        self.tool_names: list[str] = []
        self.init_info: dict = {}
        self.list_pages: int = 0
        self.duplicates: list[str] = []
        self._conn_error: BaseException | None = None
        self._proc_pid: int | None = None
        self._daemon_pids: list[int] = []
        self.seed_ctx: pp.SeedContext | None = None
        self.seed_info: dict = {}

    # ---------------- 部署 ----------------

    def _deploy_binary(self) -> str:
        """把已知散列的 daemon 拷进 run 根并核验。

        候选来源按优先级匹配已知 sha256 前缀；run 根内已有拷贝且散列吻合则直接复用。
        拷入后硬校验拷贝散列与来源一致（拷贝保真），未知散列一律拒绝。
        """
        self.daemon_exe.parent.mkdir(parents=True, exist_ok=True)
        # run 根内已有合法拷贝则复用
        if self.daemon_exe.is_file():
            sha = sha256_of(self.daemon_exe)
            if _sha_is_known(sha):
                return sha
        # 否则从候选里找散列吻合的来源拷入
        for cand in _DAEMON_CANDIDATES:
            if not cand.is_file():
                continue
            try:
                sha = sha256_of(cand)
            except OSError:
                continue
            if _sha_is_known(sha):
                shutil.copy2(cand, self.daemon_exe)
                # 拷贝保真硬校验：落盘拷贝必须与来源逐字节一致
                got = sha256_of(self.daemon_exe)
                if got != sha:
                    raise HarnessError(
                        f"daemon copy fidelity failure: {self.daemon_exe} "
                        f"({got}) != {cand} ({sha})")
                return sha
        # CW_TP10M_SRC_DAEMON 未显式设置且无已知散列候选 → skip（与
        # TP-07/TP-09 D2 同族两态处置）：Linux CI 全量 pytest job 不构建
        # daemon 候选，本地新编译 daemon 散列亦未入验收白名单——TP-10M
        # MCP wire 用例仅在有已验收候选时真跑。显式设置 CW_TP10M_SRC_DAEMON
        # 后仍失败 → 保持 HarnessError 硬错（fail-loud，acceptance-gates
        # 注入候选验收，缺失/未知散列仍是真缺陷）。
        if "CW_TP10M_SRC_DAEMON" not in os.environ:
            pytest.skip(
                f"无已知散列 daemon 候选且未显式指定 CW_TP10M_SRC_DAEMON"
                f"（candidates={[str(c) for c in _DAEMON_CANDIDATES]}）："
                f"TP-10M MCP wire 用例仅在有已验收候选时真跑；"
                f"acceptance-gates 或本地经 CW_TP10M_SRC_DAEMON 注入候选后运行")
        raise HarnessError(
            f"no daemon candidate matches a known sha256 prefix "
            f"{KNOWN_DAEMON_SHA256_PREFIXES}; "
            f"candidates={[str(c) for c in _DAEMON_CANDIDATES]}")

    def _reset_state(self) -> None:
        """清掉 run 根内上一轮的运行状态，保证可复现（fail-closed：只在 run 根内）。

        注意：reports 目录**不擦**——各验收子套件（C1/C2/C3/C4）的证据要累积
        在同一 run 根下供 C-suite 取阅；每份证据自带 generated_at / git_head。
        """
        root = self.run_root.resolve()
        for sub in (self.home, self.run_root / "ws"):
            p = Path(sub).resolve()
            if p == root or root not in p.parents:
                raise HarnessError(f"refuse to clean outside run root: {p}")
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        for d in (self.home / ".callwarden" / "daemon", self.reports_dir,
                  self.seed_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------- daemon 生命周期 ----------------

    def _manifest_paths(self) -> list[Path]:
        cw = self.home / ".callwarden"
        if not cw.is_dir():
            return []
        return sorted(cw.glob("http-daemon.*.manifest.json"))

    def _read_manifest(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _pid_is_ours(self, manifest: dict) -> bool:
        """manifest 声明的 daemon_executable 落在 run 根内才允许回收。"""
        exe = str(manifest.get("daemon_executable") or "")
        try:
            resolved = Path(exe).resolve()
        except OSError:
            return False
        return str(resolved).lower().startswith(str(self.run_root.resolve()).lower())

    def _kill_pid(self, pid: int) -> None:
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    def _reap_leftover_daemons(self) -> None:
        """回收上一轮残留的 detached daemon（仅限 run 根内的二进制）。"""
        for path in self._manifest_paths():
            manifest = self._read_manifest(path)
            if manifest and self._pid_is_ours(manifest):
                self._kill_pid(int(manifest.get("pid") or 0))

    def _wait_daemon_ready(self, timeout: float = 120.0) -> None:
        """轮询 manifest + /health，直到隔离 daemon 开始应答。"""
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            for path in self._manifest_paths():
                manifest = self._read_manifest(path)
                if not manifest:
                    continue
                endpoint = manifest.get("endpoint")
                pid = manifest.get("pid")
                if isinstance(pid, int) and pid not in self._daemon_pids:
                    if self._pid_is_ours(manifest):
                        self._daemon_pids.append(pid)
                if not endpoint:
                    continue
                try:
                    with urllib.request.urlopen(
                            f"{endpoint}/health", timeout=3) as r:
                        r.read()
                        return
                except urllib.error.HTTPError:
                    # 任何 HTTP 应答都说明 listener 已服务
                    return
                except OSError as exc:
                    last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5)
        raise HarnessError(
            f"isolated daemon not ready within {timeout}s (last={last}); "
            f"manifests={[str(p) for p in self._manifest_paths()]}")

    # ---------------- 生命周期 ----------------

    def start(self, timeout: float = 180.0) -> dict:
        """拉起后台事件循环 + server 子进程 + wire 会话；返回 init 信息。"""
        self._reap_leftover_daemons()
        self._reset_state()
        self.daemon_sha256 = self._deploy_binary()
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="tp10m-wire-loop")
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            self.stop()
            raise HarnessError(f"wire session not ready within {timeout}s")
        if self._conn_error is not None:
            self.stop()
            raise HarnessError(
                f"wire session failed: {type(self._conn_error).__name__}: "
                f"{self._conn_error}")
        # server 启动期探针的 wait_window（15s）常短于 daemon 冷启动（~20s，
        # 含 SQLite/缓存初始化）；显式等到 manifest + /health 就绪再放行，
        # 否则首个 RPC 会以 E_HTTP_REQUEST_TIMEOUT 假失败。
        self._wait_daemon_ready()
        # 隔离 codegraph 库补读面 5 表（镜像 run_t2_t3_evidence）：daemon 的
        # build_graph 只建主表，build-context / clone 读面查的 5 表在全新库中
        # 不存在，不补则相关读工具假 DEFECT（no such table）。所有模块共享。
        ensure_codegraph_seed_tables(self.codegraph_db)
        # 钉住 server 端 workspace 绑定，绕开冷启动首次查询的 git 挂死
        # （见 prime_workspace_binding 的根因分析）。
        self.prime_workspace_binding()
        return self.init_info

    def prime_workspace_binding(self) -> WireCall:
        """首次 query.* 调用前先经非 query 路径注册一次 workspace。

        根因（源码实证 server/daemon_client.py:2186）：route_rpc 对 query.* /
        workspace.status 等 needs_snapshot 面在 ``_ensure_remote_snapshot`` 里
        以**非空 db_path** 调用 ``_workspace_snapshot_metadata(root)``，后者
        用 ``subprocess.run(["git", ...], capture_output=True)`` **无超时**地
        取 git provenance；该 git 子进程在 MCP server（stdio 管道子进程）里
        偶发挂死（句柄继承），导致冷启动后首个查询工具永不返回。
        而 register_workspace / build_graph 等非 query 路径走 db_path=None
        分支，**不跑 git**，只把 ``_remote_workspace_id`` 钉住；之后所有
        query.* 调用都跳过 register、只走 snapshot.publish（无 git）。
        本卡规定的播种顺序（register → build → query）天然避开该路径；
        这里在 start() 末尾显式 prime 一次，保证所有模块都不会撞上。
        """
        self.seed_dir.mkdir(parents=True, exist_ok=True)
        for fname in os.listdir(SEED_SAMPLE):
            src = SEED_SAMPLE / fname
            if src.is_file():
                shutil.copy2(src, self.seed_dir / fname)
        r = self.call("register_workspace", {
            "name": f"tp10m-prime-{uuid.uuid4().hex[:6]}",
            "root_path": str(self.seed_dir),
            "description": "TP-10M workspace binding prime",
        })
        if not r.delivered or r.isError or r.error_text:
            raise HarnessError(
                f"prime register_workspace failed: {r.error_text[:200]}")
        return r

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except BaseException as exc:  # noqa: BLE001
            self._conn_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _connect(self) -> None:
        """建立 stdio 子进程 + ClientSession，翻页拉取全部工具。"""
        self._stack = AsyncExitStack()
        env = isolated_env(self.run_root, self.daemon_exe)
        params = StdioServerParameters(
            command=PYTHON,
            args=[str(REPO_ROOT / "cw.py"), "server"],
            env=env,
            cwd=str(REPO_ROOT),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(
            ClientSession(read, write))
        self._init_result = await self._session.initialize()
        self.init_info = {
            "protocolVersion": getattr(self._init_result, "protocolVersion", None),
            "serverName": (self._init_result.serverInfo.name
                           if self._init_result.serverInfo else None),
            "serverVersion": (self._init_result.serverInfo.version
                              if self._init_result.serverInfo else None),
            "instructions_present": bool(
                getattr(self._init_result, "instructions", "") or ""),
            "capabilities": bool(getattr(self._init_result, "capabilities", None)),
        }
        await self._list_all_tools()

    async def _list_all_tools(self) -> None:
        assert self._session is not None
        self.tools = []
        self.tool_names = []
        seen: set[str] = set()
        cursor = None
        self.list_pages = 0
        while True:
            self.list_pages += 1
            result = await self._session.list_tools(cursor=cursor)
            for t in result.tools:
                if t.name in seen:
                    self.duplicates.append(t.name)
                seen.add(t.name)
                self.tools.append(t)
                self.tool_names.append(t.name)
            cursor = result.nextCursor
            if not cursor:
                break
            if self.list_pages > 50:
                raise HarnessError("tools/list pagination exceeded 50 pages")

    def stop(self, timeout: float = 30.0) -> dict:
        """关会话、停循环、回收 server 子进程与 detached daemon。"""
        info: dict = {}
        loop, stack = self._loop, self._stack
        self._loop, self._stack = None, None
        if loop is not None and loop.is_running():
            async def _shutdown():
                if stack is not None:
                    await stack.aclose()
            try:
                fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
                fut.result(timeout=timeout)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._session = None
        # 回收本 run 拉起的 detached daemon（都是 run 根内二进制，见 _pid_is_ours）
        for pid in self._daemon_pids:
            self._kill_pid(pid)
        self._daemon_pids = []
        return info

    # ---------------- wire 调用 ----------------

    def _tool_by_name(self, name: str) -> Any:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def input_schema_of(self, name: str) -> dict:
        """wire 上 tools/list 返回的活 inputSchema（参数合同真相源）。"""
        tool = self._tool_by_name(name)
        return (tool.inputSchema or {}) if tool is not None else {}

    def call(self, name: str, arguments: dict | None = None,
             timeout: float = CALL_TIMEOUT) -> WireCall:
        """经真实 wire 调用一次工具（投递到会话所在事件循环）。"""
        if self._loop is None or self._session is None:
            raise HarnessError("harness not started or already stopped")
        args = dict(arguments or {})
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, args), self._loop)
            result = fut.result(timeout=timeout)
        except BaseException as exc:  # noqa: BLE001
            return WireCall(name=name, arguments=args, delivered=False,
                            exc_type=type(exc).__name__,
                            exc_message=str(exc)[:600])
        return self._materialize(name, args, result)

    def _materialize(self, name: str, args: dict, result: Any) -> WireCall:
        kinds: list[str] = []
        texts: list[str] = []
        for block in getattr(result, "content", []) or []:
            kind = type(block).__name__
            kinds.append(kind)
            text = getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)
        structured_content = getattr(result, "structuredContent", None)
        is_error = bool(getattr(result, "isError", False))

        # 载荷优先取 structuredContent；否则尝试解析文本内容块
        payload: Any = structured_content
        parsed_from_text = False
        if payload is None and texts:
            try:
                payload = json.loads(texts[0])
                parsed_from_text = True
            except (ValueError, TypeError):
                payload = None
        # 结构化判定：MCP structuredContent 在，或文本块可解析为 JSON 对象/数组
        structured = bool(structured_content is not None or
                          (parsed_from_text and isinstance(payload, (dict, list))))
        error_text = ""
        if is_error:
            error_text = " ".join(texts)[:400] or "isError=true"
        if not error_text:
            error_text = _extract_error_text(payload)
        return WireCall(
            name=name, arguments=args, delivered=True, isError=is_error,
            payload=payload, error_text=error_text, structured=structured,
            content_kinds=kinds,
        )

    # ---------------- wire 播种 ----------------

    def seed(self) -> pp.SeedContext:
        """通过 wire（公开 API，不写 SQL）播种 workspace 并装配 SeedContext。"""
        if self.seed_ctx is not None:
            return self.seed_ctx
        if self._loop is None or self._session is None:
            raise HarnessError("harness not started")

        self.seed_dir.mkdir(parents=True, exist_ok=True)
        for fname in os.listdir(SEED_SAMPLE):
            src = SEED_SAMPLE / fname
            if src.is_file():
                shutil.copy2(src, self.seed_dir / fname)

        async def _do_seed() -> dict:
            assert self._session is not None
            reg = await self._session.call_tool("register_workspace", {
                "name": f"tp10m-seed-{uuid.uuid4().hex[:8]}",
                "root_path": str(self.seed_dir),
                "description": "TP-10M wire-seeded workspace",
            })
            reg_payload = _payload_of(reg)
            ws_id, ws_inst = _find_ids(reg_payload)
            if ws_inst is None:
                raise HarnessError(
                    f"register_workspace did not return workspace ids: "
                    f"{_truncate(json.dumps(reg_payload, ensure_ascii=False))}")
            # build_graph 是重操作，单独给长超时
            build = await self._session.call_tool(
                "build_graph", {"workspace_instance_id": ws_inst})
            build_payload = _payload_of(build)
            # 无 workspace 参数的查询面（query.*/file.*/symbol.*）在 HTTP 模式下
            # 由 route_rpc 绑定到 server 启动时的 PROJECT_ROOT（主仓）workspace，
            # 不是种子 workspace（_ensure_remote_snapshot 用 client._project_root）。
            # 只有把主仓 workspace 也构建一次，这些查询工具才能经 wire 返回真实
            # 符号名；这是「通过 wire 学真实符号名」的唯一可行路径（约 200s）。
            repo_build = await self._session.call_tool("build_graph", {})
            repo_build_payload = _payload_of(repo_build)
            # 通过 wire 学真实符号名 / 文件路径
            qnames: list[str] = []
            files: list[str] = []
            for query in ("route_rpc", "compute", "add", "register_workspace"):
                sr = await self._session.call_tool(
                    "search_symbols", {"query": query, "limit": 20})
                _collect_symbols(_payload_of(sr), qnames, files)
            # 种子 workspace 自身的符号（小而稳定）
            seed_build_symbols = build_payload.get("symbols") if isinstance(
                build_payload, dict) else None
            if not qnames:
                qnames = ["compute", "add", "multiply"]
            if not files:
                files = ["calc.py", "service.ts"]
            # 去重保序
            qnames = list(dict.fromkeys(qnames))
            files = list(dict.fromkeys(files))
            return {
                "workspace_id": ws_id, "workspace_instance_id": ws_inst,
                "qnames": qnames, "files": files,
                "register_payload": reg_payload,
                "build_payload": build_payload,
                "repo_build_payload": repo_build_payload,
                "seed_build_symbols": seed_build_symbols,
            }

        fut = asyncio.run_coroutine_threadsafe(_do_seed(), self._loop)
        info = fut.result(timeout=SEED_BUILD_TIMEOUT * 3)
        # 补种幂等（start() 已建过；build_graph 之后重建一次确保 daemon
        # 自己的 schema 与读面 5 表共存于同一库）
        ensure_codegraph_seed_tables(self.codegraph_db)
        ws_id = info["workspace_id"]
        self.seed_ctx = pp.SeedContext(
            workspace_id=(int(ws_id) if ws_id is not None else None),
            workspace_instance_id=info["workspace_instance_id"],
            root=str(self.seed_dir),
            known_qualified_names=info["qnames"],
            known_callee_names=[c for c in ("add", "multiply", "apply", "scale")
                                if c in info["qnames"]] or [info["qnames"][0]],
            known_file_paths=info["files"],
        )
        self.seed_info = {
            "workspace_id": self.seed_ctx.workspace_id,
            "workspace_instance_id": self.seed_ctx.workspace_instance_id,
            "known_qualified_names": self.seed_ctx.known_qualified_names,
            "known_file_paths": self.seed_ctx.known_file_paths,
            "register_payload": info["register_payload"],
            "build_payload": info["build_payload"],
            "repo_build_payload": info["repo_build_payload"],
            "codegraph_db": str(self.codegraph_db),
        }
        return self.seed_ctx

    # ---------------- 静态清单 ----------------

    def frozen_mcp_names(self) -> set[str]:
        with open(FROZEN_INVENTORY, encoding="utf-8") as f:
            data = json.load(f)
        return {e["tool_name"] for e in data.get("entries", [])
                if e.get("surface") == "mcp"}

    def schema_by_name(self) -> dict[str, dict]:
        """param_provider 使用的冻结 schema（{name, required, params}）。"""
        with open(MCP_SCHEMA_FIXTURE, encoding="utf-8") as f:
            data = json.load(f)
        return {t["name"]: t for t in data.get("tools", [])}

    def op_class_by_name(self) -> dict[str, str]:
        with open(INVENTORY_FIXTURE, encoding="utf-8") as f:
            data = json.load(f)
        return {t["name"]: t.get("op_class", "READ_ONLY")
                for t in data.get("tools", [])}

    def build_params(self, name: str, required_only: bool = False) -> dict:
        """为工具生成参数（param_provider + 本线束 SeedContext）。"""
        schema = self.schema_by_name().get(name)
        ctx = self.seed_ctx or pp.SeedContext()
        if not schema:
            return {}
        return pp.build_mcp_params(schema, ctx, required_only=required_only)

    # ---------------- 证据 ----------------

    def write_report(self, filename: str, payload: dict) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n",
            encoding="utf-8")
        return path

    def meta(self) -> dict:
        return {
            "card": "TP-10M",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "git_head": git_head(),
            "run_root": str(self.run_root),
            "daemon_binary": str(self.daemon_exe),
            "daemon_sha256": getattr(self, "daemon_sha256", None),
            "transport": "stdio (real subprocess cw.py server)",
            "interpreter": sys.executable,
            "initialize": self.init_info,
            "tool_count": len(self.tools),
            "pagination_pages": self.list_pages,
            "seed": self.seed_info,
        }


# ------------------------------------------------------------------ 辅助
def _payload_of(result: Any) -> Any:
    """从 CallToolResult 提取业务载荷（structuredContent 优先，否则解析文本）。"""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return text
    return None


def _find_ids(payload: Any) -> tuple[Any, Any]:
    """递归找 workspace_id / workspace_instance_id。"""
    found: dict[str, Any] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("workspace_id", "workspace_instance_id") and v is not None:
                    found.setdefault(k, v)
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return found.get("workspace_id"), found.get("workspace_instance_id")


def _collect_symbols(payload: Any, qnames: list[str], files: list[str]) -> None:
    """从查询结果里收集符号限定名与文件相对路径。"""
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("results", "symbols", "rows", "items", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                rows = v
                break
        if not rows:
            # 单符号结果（get_symbol 等）
            rows = [payload]
    repo_prefix = str(REPO_ROOT).replace("\\", "/").lower() + "/"
    for r in rows:
        if not isinstance(r, dict):
            continue
        qn = r.get("qualified_name") or r.get("name") or r.get("symbol_name")
        if isinstance(qn, str) and qn:
            qnames.append(qn)
        fp = (r.get("file_rel_path") or r.get("file_path")
              or r.get("rel_path") or r.get("relative_path"))
        if isinstance(fp, str) and fp:
            norm = fp.replace("\\", "/")
            low = norm.lower()
            if low.startswith(repo_prefix):
                fp = norm[len(repo_prefix):]
            elif low.endswith(".py") and "/" not in norm:
                pass  # 种子文件名（calc.py）保持原样
            files.append(fp)


def _truncate(s: str, n: int = 400) -> str:
    return s if len(s) <= n else s[:n] + "…"
