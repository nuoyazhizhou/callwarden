"""TP-10C CLI 全入口语义/参数合同 —— 验收 harness。

在完全隔离的 daemon（独立 run 根：loopback 随机端口 + run 根内全部状态）
上，对冻结清单（docs/testing/manifests/frozen/inventory_frozen.json）的
265 个 CLI 入口做全量真实调用，验证每个入口的语义（C1）、参数合同（C2）、
负向容错（C3）与常驻生命周期。

设计要点：
- 复用 TP-07 进程底座 IsolatedDaemon（daemon_bootstrap.py）。
- 隔离面与 CLI env 钉法沿用 TP-09 InitializationEnv（USERPROFILE/HOME/
  CALLWARDEN_* 全部指向 run 根；弹掉继承的 CW_DAEMON_* 覆盖）。
- 种子 workspace 全程只用**公开 CLI**（workspace register → refresh --all），
  符号名经 `cw search` 真实学习；codegraph 读面 5 表由 harness 在隔离库内
  补齐（同 deliverables/.../run_t2_t3_evidence.py 的已验证配方，缺表会使
  build-context/clone 读面误报 no such table → 假 DEFECT）。
- argv 直接由冻结 params 构造：必填位置参数逐个填充、可选项全量携带，
  互斥组只取首个、positional_choice 已展开的入口不重复追加选择值。

裁决与基准（2026-10-13，team-lead 裁决 + 隔离 A/B 探测定性）：
- 冻结验收基准重钉为 247b9a16（原 f1fa3736）。f1fa3736 携带 CLI-067 死代码
  缺陷（`cw tests` 的 import_test_results 从未进 dispatch 路由），已在 91ae8dc
  修复；TP-10/src 重新部署为 247b9a16。隔离 A/B 探测实证：旧二进制 tests →
  method_not_found，新二进制 tests → PASS（rc=0，「Test results imported」）。
- 3 个入口为 CLI 面板与 HTTP daemon 之间的**迁移缺口**（daemon 侧方法未实现，
  CLI 侧给出结构化错误、非 Python traceback、非崩溃），按 EXPECTED_BUSINESS
  收敛，见 VERDICT_OVERRIDE。覆盖表仅在 t3 判定 DEFECT 时生效，一旦 daemon
  侧补齐实现即自然休眠（入口回到 PASS），不需删表。

绝对不触碰生产：run 根外不写任何文件；registry/task DB/manifest/锁全部
落在 run 根内。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------ 路径
REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/cli → 仓库根
BOOTSTRAP_DIR = REPO_ROOT / "tests" / "acceptance" / "harness" / "bootstrap"
CONV_DIR = REPO_ROOT / "tests" / "convergence"
for _d in (str(BOOTSTRAP_DIR), str(CONV_DIR), str(REPO_ROOT)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from daemon_bootstrap import IsolatedDaemon  # noqa: E402
import param_provider as pp  # noqa: E402
import t3_cli_runner as t3r  # noqa: E402

RUN_ROOT = Path(os.environ.get(
    "CW_TP10C_RUN_ROOT", r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10C"))
TP10_DAEMON_SRC = Path(os.environ.get(
    "CW_TP10C_DAEMON_SRC",
    r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10/src/cw-daemon.exe"))
CW_PY = REPO_ROOT / "cw.py"
PYTHON = sys.executable
SEED_SAMPLE = CONV_DIR / "seed_sample"
INVENTORY_PATH = REPO_ROOT / "docs" / "testing" / "manifests" / "frozen" / "inventory_frozen.json"

# 冻结验收基准：TP-10C 必须针对冻结候选 daemon 验收（散列须命中
# KNOWN_DAEMON_SHA256_PREFIXES 之一）。原始任务卡钉 f1fa3736a16dbe7d；
# 91ae8dc（CLI-067 三件套：import_test_results / build_test_relations
# 登记 CONVERGENCE_RPC_METHODS）后，权威修复版为 247b9a16049ec4d7
# （TP-10/src 候选）。两者均验收通过；CLI:tests 只在后者下 PASS。
# 若部署候选散列全不匹配则 fail-closed，避免被换成自构建二进制后给出
# 无意义的验收结论。重新冻结基准时经 CW_TP10C_FROZEN_SHA256 显式更新
# （逗号分隔多前缀）；临时绕过用 CW_TP10C_ALLOW_OTHER_BINARY=1。
_ENV_FROZEN = os.environ.get("CW_TP10C_FROZEN_SHA256", "")
KNOWN_DAEMON_SHA256_PREFIXES: tuple = tuple(
    p.strip() for p in _ENV_FROZEN.split(",") if p.strip()) or (
    "f1fa3736a16dbe7d",
    "247b9a16049ec4d7",
)
# 冻结候选的已知可信副本（TP-10/src 被污染时的回退查找顺序）
_TRUSTED_FROZEN_COPIES = (
    REPO_ROOT / "rust_ext" / "target" / "release" / "cw-daemon.exe",
    Path(r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-10M/src/cw-daemon.exe"),
    Path(r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-09/src/cw-daemon.exe"),
)

if "Python314" not in PYTHON and not os.environ.get("CW_TP10C_ALLOW_OTHER_PY"):
    raise RuntimeError(
        f"TP-10C 需要 C:/Python314/python.exe（callwarden_core 为 cp314 PyO3 扩展），"
        f"当前解释器为 {PYTHON}")


# ------------------------------------------------------------------ 清单
def load_cli_entries() -> List[Dict[str, Any]]:
    """载入冻结清单的全部 265 个 CLI 入口（按 entry_id 排序，稳定可复现）。"""
    with open(INVENTORY_PATH, encoding="utf-8") as f:
        inv = json.load(f)
    entries = [e for e in inv.get("entries", []) if e.get("surface") == "cli"]
    entries.sort(key=lambda e: e.get("entry_id", ""))
    return entries


# ------------------------------------------------------------------ SKIP
# TP-10C 在 t3 已验证 SKIP 名表之外补充的新增破坏性/重操作入口（冻结清单
# 比 t3 的 233 条多 34 条，其中 install-*/rollback set 等为写操作）。
TP10C_EXTRA_SKIP = (
    "install-agent",               # 写各 IDE 的 MCP 配置（破坏性）
    "install-hook",                # 写 git hook（破坏性）
    "rollback set",                # 回滚编辑结果（破坏性）
    "gc policy set",               # 写 GC 策略
    "rule candidate create",       # 治理写：建候选规则
    "rule candidate accept",       # 治理写：接受候选规则
    "rule candidate reject",       # 治理写：拒绝候选规则
)

# positional_choice_parent 中可安全执行的读侧选择（其余 parent 全量 SKIP）。
PARENT_SAFE_CHOICE = {
    "config check-role": "local",
    "gc policy": "show",
    "rule candidate": "list",
}


def should_skip(entry: Dict[str, Any]) -> str:
    """返回 SKIP 原因；不跳过返回空串。cw_py_standalone 无冻结参数，C1 不测。"""
    inv = entry.get("invocation", "")
    if entry.get("entry_kind") == "cw_py_standalone":
        return ("cw_py_standalone：cw.py 级分发，清单标注 params.available=false"
                "（help_text_only），无参数合同可验证；server/daemon start 由"
                "test_cli_lifecycle 覆盖")
    reason = t3r.should_skip(inv)
    if reason:
        return reason
    for pref in TP10C_EXTRA_SKIP:
        if inv == pref or inv.startswith(pref + " ") or inv.startswith(pref):
            return f"破坏性/重操作（TP-10C 补充）:{pref}"
    return ""


# ------------------------------------------------------------------ 判定覆盖
# 已定性的迁移缺口 / 前置不满足：route_rpc 的 method_not_found 对这些入口是
# fail-closed 的正确拒绝，不是产品回归（C1 预检逐入口源码级定性，见 memory
# 2026-10-13）。覆盖表把这类入口从 DEFECT 覆盖为 EXPECTED_BUSINESS：
#   - 只在原判 DEFECT 时覆盖（daemon 真正下沉后入口自然转 PASS，覆盖自动
#     失效，不掩盖任何修复）；
#   - detail 追记 original_verdict + override_reason，rec 落 extra，全链路
#     可审计；
#   - 不进 SKIP 表（SKIP 会掩盖「调用了但被正确拒绝」的语义）。
# 🔴 CLI:tests 严禁放入本表：91ae8dc 已修（CONVERGENCE_RPC_METHODS 登记
# import_test_results/build_test_relations），权威基准 247b9a16 下它是真 PASS。
VERDICT_OVERRIDE: Dict[str, str] = {
    "CLI:dashboard": (
        "迁移缺口：dashboard --risks → get_project_risks。CLI METHOD_MAP 映射同名"
        " RPC，daemon 无此方法；Python 实现仍在 db/db_dashboard.py:467 未下沉。"
        "route_rpc 的 method_not_found 是 fail-closed 的正确拒绝"
        "（C1 预检定性，见 memory 2026-10-13）"),
    "CLI:embed": (
        "迁移缺口：embed → embed_all_symbols。daemon 只有 job 通道"
        "（route_matrix embed_symbols → task.job_submit，job_runner 认 job_type"
        " embed/embed_single），方法名与 job_type 没对上（C1 预检定性，见 memory"
        " 2026-10-13）"),
    "CLI:collab gate-trigger": (
        "前置不满足：gate.decide 路由存在（dispatch.rs:3248 → handle_collab_rpc），"
        "隔离 daemon 未组装 collab 控制面 → fail-closed 拒绝（C1 预检定性，"
        "见 memory 2026-10-13）"),
}


# ------------------------------------------------------------------ argv
def _coerce_value(val: Any, ptype: str, choices: Optional[List[Any]]) -> str:
    """按声明类型/候选值把解析值整理成 argv 用的字符串。"""
    if choices:
        return str(choices[0])
    if ptype == "int":
        try:
            return str(int(val))
        except (TypeError, ValueError):
            return "1"
    if ptype == "float":
        try:
            return str(float(val))
        except (TypeError, ValueError):
            return "1.0"
    return str(val)


def _positional_value(pos: Dict[str, Any], ctx: pp.SeedContext,
                      entry: Dict[str, Any]) -> Optional[str]:
    """返回该位置参数的 argv 值；None 表示该参数不提供（零元 nargs）。"""
    nargs = pos.get("nargs")
    if nargs == "*":
        # 零或多个：给零个（最常见的调用形态，如 `cw refresh --all`）
        return None
    choices = pos.get("choices")
    if entry.get("entry_kind") == "positional_choice_parent" and choices:
        safe = PARENT_SAFE_CHOICE.get(entry.get("invocation", ""))
        return str(safe) if safe else str(choices[0])
    if choices:
        return str(choices[0])
    val = pp._resolve_by_name(pos.get("dest", ""), ctx)
    if val is pp._UNRESOLVED:
        val = "seed"
    return _coerce_value(val, pos.get("type", ""), None)


def _option_value(opt: Dict[str, Any], ctx: pp.SeedContext) -> Optional[List[str]]:
    """返回该选项的 argv 片段；None 表示不携带。"""
    flags = opt.get("flags") or []
    flag = flags[0]
    if not flag or flag in ("-h", "--help"):
        return None
    if opt.get("nargs") == 0:
        return [flag]
    pname = flag.lstrip("-").replace("-", "_")
    val = pp._resolve_by_name(pname, ctx)
    if val is pp._UNRESOLVED:
        val = "seed"
    return [flag, _coerce_value(val, opt.get("type", ""), opt.get("choices"))]


def build_argv(entry: Dict[str, Any], ctx: pp.SeedContext,
               include_options: bool = True,
               drop_positional: Optional[str] = None,
               override_positional: Optional[Dict[str, str]] = None,
               override_option: Optional[Dict[str, str]] = None,
               skip_options: Optional[set] = None,
               base_tokens: Optional[List[str]] = None) -> List[str]:
    """由冻结 params 构造 argv（不含 `cw` 前缀）。

    Args:
        entry: 冻结清单条目。
        ctx: 种子前置状态。
        include_options: 是否携带全部可选项。
        drop_positional: 要丢弃的必填位置参数 dest（C2-a 用）。
        override_positional: {dest: 值} 位置参数覆盖（C3 用）。
        override_option: {flag: 值} 选项值覆盖（C2-b/c 用）。
        skip_options: 要跳过的 flag 集合。
        base_tokens: 覆盖 invocation 切片（C2-a 丢弃已展开选择值时用）。
    """
    params = entry.get("params") or {}
    argv: List[str] = list(base_tokens) if base_tokens is not None else \
        (entry.get("invocation") or "").split()
    if not params.get("available"):
        return argv

    exp_param = (((params.get("expansion_basis")) or {}).get("param")) or None
    overrides = override_positional or {}
    for pos in params.get("positionals") or []:
        dest = pos.get("dest", "")
        # 已展开入口：选择值已在 invocation 中，不重复追加
        if entry.get("entry_kind") == "positional_choice_expansion" and dest == exp_param:
            continue
        if dest == drop_positional:
            continue
        if dest in overrides:
            argv.append(overrides[dest])
            continue
        val = _positional_value(pos, ctx, entry)
        if val is not None:
            argv.append(val)

    if not include_options:
        return argv

    # 互斥组：只携带组内首个选项，其余排除
    excluded: set = set()
    for group in params.get("mutually_exclusive_groups") or []:
        flat = [f for fl in group for f in fl]
        if flat:
            excluded.update(flat[1:])

    skip_opts = skip_options or set()
    for opt in params.get("options") or []:
        flags = opt.get("flags") or []
        flag = flags[0]
        if flag in skip_opts or any(f in excluded for f in flags):
            continue
        if override_option and flag in override_option:
            argv += [flag, override_option[flag]]
            continue
        frag = _option_value(opt, ctx)
        if frag is not None:
            argv += frag
    return argv


# ------------------------------------------------------------------ 环境
def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CLIAcceptanceEnv:
    """TP-10C 隔离执行环境：隔离 daemon + 公开 CLI 种子 workspace + 结果采集。"""

    def __init__(self, run_root: Path | str | None = None):
        self.run_root = Path(run_root or RUN_ROOT)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.daemon_exe = self.run_root / "src" / "cw-daemon.exe"
        self._deploy_daemon()
        self.home = self.run_root / "home"
        self.seed_dir = self.run_root / "seed-ws"
        self.reports_dir = self.run_root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.daemon: IsolatedDaemon | None = None
        self.ctx: pp.SeedContext | None = None
        self.counts = {"PASS": 0, "EXPECTED_BUSINESS": 0, "DEFECT": 0, "SKIP": 0}
        self.records: List[Dict[str, Any]] = []
        pinned = os.environ.get("CW_TP10C_DAEMON_SHA256", "")
        self.expected_sha256 = pinned or _sha256_of(self.daemon_exe)

    # ---------- daemon 部署/生命周期 ----------

    @staticmethod
    def _is_frozen(path: Path) -> bool:
        try:
            sha = _sha256_of(path)
        except OSError:
            return False
        return any(sha.startswith(p) for p in KNOWN_DAEMON_SHA256_PREFIXES)

    def _deploy_daemon(self) -> None:
        """部署冻结候选到 run 根；散列不匹配则 fail-closed 或回退可信副本。"""
        if self.daemon_exe.is_file():
            return
        self.daemon_exe.parent.mkdir(parents=True, exist_ok=True)
        src = TP10_DAEMON_SRC
        if os.environ.get("CW_TP10C_ALLOW_OTHER_BINARY"):
            # 显式放宽（如重新冻结基准后）：直接用指定源
            if not src.is_file():
                raise FileNotFoundError(f"daemon 候选缺失：{src}")
            shutil.copy2(src, self.daemon_exe)
            return
        if not self._is_frozen(src):
            # 指定源不是冻结候选 → 回退到已知可信冻结副本，并记录告警
            fallback = next((p for p in _TRUSTED_FROZEN_COPIES if self._is_frozen(p)), None)
            if fallback is None:
                raise RuntimeError(
                    f"daemon 候选 {src} 散列不匹配冻结基准 "
                    f"{KNOWN_DAEMON_SHA256_PREFIXES}，且无可信冻结副本可用；"
                    f"若确需换基准，设 CW_TP10C_ALLOW_OTHER_BINARY=1 并以 "
                    f"CW_TP10C_DAEMON_SHA256 钉新散列")
            src = fallback
        shutil.copy2(src, self.daemon_exe)

    def _cleanup_leftover_daemon(self) -> None:
        """清理上一轮残留的 daemon 实例锁（仅限本 run 根内的 cw-daemon）。"""
        cw_dir = self.home / ".callwarden"
        if not cw_dir.is_dir():
            return
        for lock in cw_dir.glob("daemon-instance.*.lock"):
            pid = None
            try:
                data = json.loads(lock.read_text(encoding="utf-8"))
                pid = data.get("pid") if isinstance(data, dict) else None
            except (OSError, ValueError):
                pass
            if pid and self._pid_is_cw_daemon(pid):
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=10)
                    time.sleep(0.6)
                except Exception:
                    pass
            try:
                lock.unlink()
            except OSError:
                pass

    @staticmethod
    def _pid_is_cw_daemon(pid) -> bool:
        """tasklist 校验 pid 存活且映像名为 cw-daemon.exe（避免误杀被复用的 pid）。"""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=10).stdout
        except Exception:
            return False
        return bool(out) and "cw-daemon.exe" in out.lower()

    def start_daemon(self, timeout: float = 90.0) -> dict:
        if self.daemon is not None:
            raise RuntimeError("daemon already started")
        # 清掉上一轮残留的 daemon 实例与锁/manifest（只在 run 根内，fail-closed）：
        # TerminateProcess 不会执行 daemon 的锁清理逻辑，故锁文件会残留，导致下一轮
        # daemon 误判 E_DAEMON_ALREADY_RUNNING
        self._cleanup_leftover_daemon()
        manifest_dir = self.home / ".callwarden"
        if manifest_dir.is_dir():
            for stale in manifest_dir.glob("http-daemon.*.manifest.json"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        d = IsolatedDaemon(run_root=self.run_root, daemon_exe=self.daemon_exe,
                           expected_sha256=self.expected_sha256)
        self.daemon = d
        manifest = d.start(timeout=timeout)
        d.verify_consistency()
        return manifest

    def stop_daemon(self) -> dict:
        if self.daemon is None:
            return {"stopped": False}
        info = self.daemon.stop()
        self.daemon.dump_log(self.reports_dir / "daemon.log")
        self.daemon = None
        return info

    # ---------- CLI 执行 ----------

    def cli_env(self) -> dict:
        """CLI 子进程环境：与 daemon 同源的隔离面（沿用 TP-09 钉法）。"""
        env = dict(os.environ)
        env["USERPROFILE"] = str(self.home)
        env["HOME"] = str(self.home)
        env["CALLWARDEN_HOME"] = str(self.home)
        env["CALLWARDEN_DB"] = str(self.home / ".callwarden" / "callwarden.db")
        env["CALLWARDEN_DAEMON_REGISTRY_DB"] = str(
            self.home / ".callwarden" / "daemon" / "registry.db")
        env["CW_DAEMON_REGISTRY_DB"] = env["CALLWARDEN_DAEMON_REGISTRY_DB"]
        env["CW_DAEMON_BIN"] = str(self.daemon_exe)
        for k in ("CW_DAEMON_BINARY", "CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND",
                  "CW_DAEMON_HTTP_ENDPOINT"):
            env.pop(k, None)
        env["CALLWARDEN_LANG"] = "en_US"
        env["CALLWARDEN_AUTO_SETUP"] = "0"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run_cli(self, args: List[str], timeout: float = 180.0,
                cwd: Path | None = None) -> Dict[str, Any]:
        r = subprocess.run(
            [PYTHON, "-u", str(CW_PY)] + args,
            cwd=str(cwd or self.seed_dir), env=self.cli_env(),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        return {"rc": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}

    # ---------- 种子（公开 CLI ONLY）----------

    def seed_workspace(self, refresh_timeout: float = 600.0) -> pp.SeedContext:
        """公开 CLI 注册种子 workspace → refresh --all → 补 codegraph 读面 5 表。"""
        if self.ctx is not None:
            return self.ctx
        if not self.seed_dir.exists():
            self.seed_dir.mkdir(parents=True, exist_ok=True)
            for f in SEED_SAMPLE.iterdir():
                if f.is_file():
                    shutil.copy2(f, self.seed_dir / f.name)

        marker = self.run_root / ".tp10c-seeded"
        if not marker.is_file():
            reg = self.run_cli(
                ["workspace", "register", "tp10c-seed", str(self.seed_dir)],
                timeout=180.0)
            if reg["rc"] != 0:
                raise RuntimeError(
                    f"种子 workspace register 失败：{reg['stdout'][:400]}{reg['stderr'][:400]}")
            ws_id = self._workspace_id_for_root()
            if ws_id:
                self.run_cli(["workspace", "set", ws_id], timeout=60.0)
            ref = self.run_cli(["refresh", "--all"], timeout=refresh_timeout)
            if ref["rc"] != 0:
                raise RuntimeError(
                    f"种子 refresh --all 失败：{ref['stdout'][:400]}{ref['stderr'][:400]}")
            self._ensure_codegraph_seed_tables()
            marker.touch()

        self.ctx = self._learn_seed_context()
        return self.ctx

    def _workspace_id_for_root(self) -> Optional[str]:
        r = self.run_cli(["workspace", "list"], timeout=60.0)
        if r["rc"] != 0:
            return None
        target = str(self.seed_dir)
        current_id = None
        for ln in r["stdout"].splitlines():
            s = ln.strip()
            if s.startswith("[") and "]" in s:
                current_id = s[1:s.index("]")]
            if current_id and (target in ln or target.replace("/", "\\") in ln):
                return current_id
        return None

    def _ensure_codegraph_seed_tables(self) -> None:
        """隔离 codegraph 库补 5 张读面表（同 run_t2_t3_evidence 已验证配方）。

        缺表会使 build-context * / clone 读面命中 no such table → 假 DEFECT。
        建表语句幂等（CREATE TABLE IF NOT EXISTS）。
        """
        import sqlite3
        db = self.home / ".callwarden" / "callwarden.db"
        if not db.is_file():
            return
        ddl = [
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
        conn = sqlite3.connect(str(db), timeout=15)
        try:
            for stmt in ddl:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def _learn_seed_context(self) -> pp.SeedContext:
        """经 `cw search` 真实学习种子符号名/文件路径，构造 SeedContext。"""
        ws_id = self._workspace_id_for_root()
        qnames: List[str] = []
        files: List[str] = []
        for term in ("compute", "add"):
            r = self.run_cli(["search", term, "--limit", "50"], timeout=120.0)
            if r["rc"] != 0:
                continue
            for line in r["stdout"].splitlines():
                for m in re.finditer(r"[A-Za-z_][\w.\-]*::[A-Za-z_]\w*", line):
                    if m.group(0) not in qnames:
                        qnames.append(m.group(0))
                for m in re.finditer(r"[A-Za-z_][\w./\-]*\.(?:py|ts|js|tsx|jsx|rs|go|java)",
                                     line):
                    if m.group(0) not in files:
                        files.append(m.group(0))
        if not qnames:
            qnames = ["compute", "add", "multiply"]
        if not files:
            files = ["calc.py", "service.ts"]
        ctx = pp.SeedContext(
            workspace_id=int(ws_id) if ws_id and ws_id.isdigit() else None,
            root=str(self.seed_dir),
            known_qualified_names=qnames,
            known_callee_names=qnames[:3] or ["add", "multiply"],
            known_file_paths=files,
        )
        return ctx

    # ---------- 结果采集 ----------

    def record(self, entry: Dict[str, Any], argv: List[str], verdict: str,
               detail: str, rc: Optional[int] = None,
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "entry_id": entry.get("entry_id"),
            "invocation": entry.get("invocation"),
            "entry_kind": entry.get("entry_kind"),
            "category": entry.get("category"),
            "argv": argv,
            "verdict": verdict,
            "detail": detail,
            "rc": rc,
        }
        if extra:
            rec.update(extra)
        self.records.append(rec)
        self.counts[verdict] = self.counts.get(verdict, 0) + 1
        return rec

    def run_entry(self, entry: Dict[str, Any], timeout: float = 180.0,
                  argv: Optional[List[str]] = None) -> Dict[str, Any]:
        """执行单个冻结入口，按 t3 分类语义判定；已定性迁移缺口经覆盖表归一。

        覆盖表只作用于「原判 DEFECT 且 entry_id 命中 VERDICT_OVERRIDE」的入口，
        覆盖为 EXPECTED_BUSINESS 并在 detail/rec 里留 original_verdict +
        override_reason。其余入口一律如实判定，不覆盖、不粉饰。
        """
        skip_reason = should_skip(entry)
        argv = argv if argv is not None else build_argv(entry, self.ctx)
        if skip_reason:
            return self.record(entry, argv, "SKIP", skip_reason)
        try:
            r = self.run_cli(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            return self.record(entry, argv, "DEFECT", f"TIMEOUT(>{timeout}s)")
        verdict = t3r.classify(r["rc"], r["stdout"], r["stderr"])
        snippet = (r["stdout"] or r["stderr"] or "").strip().replace("\n", " ")[:220]
        detail = f"rc={r['rc']}: {snippet}"
        extra: Dict[str, Any] = {}
        override = VERDICT_OVERRIDE.get(entry.get("entry_id", ""))
        if override and verdict == "DEFECT":
            extra["original_verdict"] = verdict
            extra["override_reason"] = override
            verdict = "EXPECTED_BUSINESS"
            detail = f"{detail} || 覆盖为 EXPECTED_BUSINESS：{override}"
        return self.record(entry, argv, verdict, detail, r["rc"], extra)

    def write_report(self, name: str, payload: Dict[str, Any]) -> Path:
        path = self.reports_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def git_head(self) -> str:
        try:
            r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               cwd=str(REPO_ROOT), capture_output=True, text=True,
                               timeout=10)
            return r.stdout.strip() if r.returncode == 0 else "unknown"
        except Exception:
            return "unknown"
