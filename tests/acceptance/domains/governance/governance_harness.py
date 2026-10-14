"""TP-11G 治理/租约全生命周期验收 harness（tests/acceptance/domains/governance/）。

在完全隔离的 daemon（独立 run 根：loopback 随机端口 + run 根内全部状态）上
验证治理/租约全生命周期的**真实行为**：

- G1：A′ 环全链路（create → claim → lease acquire → report → handoff →
  reviewer lease → verdict → apply → close），每步断言状态跃迁；
- G2：租约生命周期（acquire → status → renew → release → list_events 审计；
  raw token 只返回一次、renew 幂等 counter 不变、跨角色/错 token 拒绝）；
- G3：负向治理拒绝（review 态 claim / 无 verdict 的 apply/close /
  身份不一致 / 孤儿 claim 恢复 / supersede→cascade_close 作废出口）。

设计要点：
- 复用 TP-09 InitializationEnv 的隔离面（CW_DAEMON_REGISTRY_DB 注入 +
  USERPROFILE/HOME/CALLWARDEN_* 重定向 + CW_DAEMON_BIN 钉候选），零生产触碰；
- daemon 候选从 TP-10/src（已验收的 247b9a16 修复版）部署到本 run 根，
  落盘后逐字节保真校验；换基准经 CW_TP11G_DAEMON_SRC 显式指定；
- **治理写走 daemon RPC 直调**（HttpDaemonRpcClient 显式 endpoint，禁本地
  SQLite）：task.create / task.claim / task.report / task.handoff /
  task.apply / task.close / task.claim.recover / lease.* / verdict.submit
  均返回结构化 JSON，业务拒绝以 error.code 断言（fail-closed 语义）；
- A′ 三角色身份常量见 ROLE_IDENTITIES；identity 三坑（verdict/handoff 须带
  非空 agent_instance_id 且两处一致；apply/close 三元组须与 lease holder 一致）
  由本 harness 集中提供；
- CLI 子进程 env 带 CALLWARDEN_SKIP_AUTO_SETUP=1（TP-09 的
  CALLWARDEN_AUTO_SETUP=0 是无效变量名，首次 CLI 调用会触发 13 个 MCP
  配置写入 run 根 home——本 harness 修正）。

绝对不触碰生产：run 根外不写任何文件；registry/task DB/manifest/锁全部落在
run 根内。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
INIT_DIR = REPO_ROOT / "tests" / "acceptance" / "harness" / "initialization"
for _d in (str(INIT_DIR), str(REPO_ROOT)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from init_harness import InitializationEnv  # noqa: E402
from server.daemon_client import (  # noqa: E402
    HttpDaemonRpcClient,
    DaemonRemoteError,
    DaemonUnavailableError,
)

PYTHON = sys.executable
CW_PY = REPO_ROOT / "cw.py"

# 验收基准：TP-11G 复用 TP-10/src 已验收候选（247b9a16049ec4d7…，含 91ae8dc
# 路由三件套修复）。换基准经 CW_TP11G_DAEMON_SRC 显式指定。
# I5 修复后基线（ab3347d1…，含 task.create 三路径 registry owner ACL）；
# TP-11J 作业落库修复后基线 97f0eed7（含异步作业可观测性）；
# TP-11W 修复后基线 85da91ef（file.list/file.grep 的 path 子目录剪枝可用）。
# 默认指向 run 根之外的 candidates（run 根会被自清洁整体删除，候选必须放外面）；
# 旧基线经 CW_TP11G_DAEMON_SRC 显式指定。
DEFAULT_DAEMON_SRC = Path(
    os.environ.get(
        "CW_TP11G_DAEMON_SRC",
        r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/candidates/l5-fix/cw-daemon.exe"))
KNOWN_DAEMON_SHA256_PREFIXES: Tuple[str, ...] = (
    "f1fa3736a16dbe7d",
    "247b9a16049ec4d7",
    "ab3347d186b35b8b",
    "97f0eed734137f76",
    "85da91efa1c75fb6",
    "4cdc6c16543f74a6",
    "8b3b32f80e6fc395",
    "ace30cd3588f1fb4",
    "39d70a27173fe578",  # L5/L5b/X3c 修复（resolve_workspace canonical + 版本化镜像 + save_findings）
    "714532bb4fd38d88",  # TP-11A fs 根目录限制（无 D5 索引清理，被 59a83204 取代）
    "59a83204ed4c4f25",  # TP-11A fs 根目录限制 + 审计列修正 + remove 索引 FK 级联清理
)
DEFAULT_RUN_ROOT = Path(
    os.environ.get("CW_TP11G_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11G"))

# A′ 三角色身份常量（identity 三坑集中管理：verdict/handoff 须带非空
# agent_instance_id；apply/close 三元组须与 lease holder 一致）。
# lease_role 映射（36f1c81 后 daemon 自动投影，显式 --role 优先）：
# executor→implementer、reviewer→reviewer、adjudicator→reviewer。
# 🔴 contract_role 是 role_contracts 表的角色名（executor/reviewer/
# adjudicator），与 identity 的 role 对 executor 差一个词，查询合同必须用它。
CONTRACT_ROLE_NAMES: Dict[str, str] = {
    "executor": "executor",
    "reviewer": "reviewer",
    "adjudicator": "adjudicator",
}
ROLE_IDENTITIES: Dict[str, Dict[str, str]] = {
    "executor": {
        "agent_id": "tp11g-exec",
        "session_id": "tp11g-sess-exec",
        "model_id": "tp11g-model-exec",
        "agent_instance_id": "tp11g-inst-exec",
        "role": "implementer",
        "lease_role": "implementer",
    },
    "reviewer": {
        "agent_id": "tp11g-rev",
        "session_id": "tp11g-sess-rev",
        "model_id": "tp11g-model-rev",
        "agent_instance_id": "tp11g-inst-rev",
        "role": "reviewer",
        "lease_role": "reviewer",
    },
    "adjudicator": {
        "agent_id": "tp11g-adj",
        "session_id": "tp11g-sess-adj",
        "model_id": "tp11g-model-adj",
        "agent_instance_id": "tp11g-inst-adj",
        "role": "adjudicator",
        "lease_role": "reviewer",
    },
}


# A′ 标准三角色 legacy contract 模板（cli/main.py:15368 _build_role_contracts
# 原文照搬：daemon 不建默认模板，由客户端在同一 RPC 提交三条合同；缺合同任务
# 无法 claim/bootstrap）。prompt_hash 为对应 prompt 模板的稳定占位。
APRIME_ROLE_CONTRACTS: List[Dict[str, Any]] = [
    {
        "role": "executor",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.executor.startup.v1",
        "prompt_hash": "59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6",
        "allowed_paths": "task-card scoped paths only",
        "forbidden_paths": "task.apply; task.close; task.supersede; out-of-scope production/schema changes",
        "commands": "task.next_action; task.claim; task.report; task.handoff",
        "acceptance_checks": "one tool/CLI link; tests; evidence manifest/hash; executor_ready_for_review",
        "required_evidence": "implementation plan; test output; negative test; daemon round-trip evidence",
        "handoff_to": "reviewer",
        "independence": "required",
    },
    {
        "role": "reviewer",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.reviewer.startup.v1",
        "prompt_hash": "6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E",
        "allowed_paths": "read-only review evidence and structured review handoff",
        "forbidden_paths": "production edits; task.apply; task.close; task.supersede",
        "commands": "task.next_action; task.contract.get; task.handoff",
        "acceptance_checks": "independent verification of scope, diff, tests, evidence, gate and matrix condition",
        "required_evidence": "review record; findings or reviewer_pass evidence manifest/hash",
        "handoff_to": "adjudicator",
        "independence": "required",
    },
    {
        "role": "adjudicator",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
        "prompt_hash": "42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E",
        "allowed_paths": "final review and protected task finalization within daemon authority",
        "forbidden_paths": "production edits; local SQLite fallback; status forgery",
        "commands": "task.next_action; task.apply; task.close; task.handoff; task.supersede when separately authorized",
        "acceptance_checks": "ACCEPT requires valid reviewer lease/fencing then apply, close and next_action=COMPLETE",
        "required_evidence": "final review; lease/fencing provenance; apply/close/COMPLETE verification",
        "handoff_to": "complete",
        "independence": "required",
    },
]


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class RpcResult:
    """单次 RPC 调用的结构化结果（含业务拒绝码，供断言 fail-closed 语义）。"""

    __slots__ = ("ok", "result", "code", "message", "method")

    def __init__(self, method: str, ok: bool, result: Any = None,
                 code: str = "", message: str = ""):
        self.method = method
        self.ok = ok
        self.result = result
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        if self.ok:
            return f"RpcResult({self.method} ok)"
        return f"RpcResult({self.method} REJECTED {self.code}: {self.message})"

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.result, dict):
            return self.result.get(key, default)
        return default


class GovernanceEnv(InitializationEnv):
    """TP-11G 隔离执行环境：TP-09 隔离面 + 治理/租约 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11g-ws"):
        self.run_root = Path(run_root or DEFAULT_RUN_ROOT)
        self.daemon_exe = self.run_root / "src" / "cw-daemon.exe"
        self.deploy_daemon(Path(daemon_src or DEFAULT_DAEMON_SRC))
        self.workspace_dir = self.run_root / "ws"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.daemon = None
        self.workspace_name = workspace_name
        self.expected_sha256 = os.environ.get(
            "CW_TP11G_DAEMON_SHA256", "") or _sha256_of(self.daemon_exe)
        self.reports_dir = self.run_root / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._rpc: Optional[HttpDaemonRpcClient] = None
        self._manifest: Dict[str, Any] = {}
        self.workspace_id: Optional[int] = None
        self.workspace_instance_id: Optional[str] = None

    # ---------- daemon 部署 ----------

    def deploy_daemon(self, src: Path) -> None:
        """部署候选到 run 根；散列须命中已验收前缀，落盘后逐字节保真校验。"""
        if not src.is_file():
            raise FileNotFoundError(f"daemon candidate missing: {src}")
        sha = _sha256_of(src)
        if not any(sha.startswith(p) for p in KNOWN_DAEMON_SHA256_PREFIXES):
            raise RuntimeError(
                f"daemon candidate {src} sha256={sha} 不匹配已验收前缀 "
                f"{KNOWN_DAEMON_SHA256_PREFIXES}；换基准须先独立验收")
        self.daemon_exe.parent.mkdir(parents=True, exist_ok=True)
        if self.daemon_exe.is_file() and \
                _sha256_of(self.daemon_exe) == sha:
            return  # 已就位且一致
        shutil.copy2(src, self.daemon_exe)
        if _sha256_of(self.daemon_exe) != sha:
            raise RuntimeError("daemon 部署保真校验失败（落盘散列与候选不一致）")

    # ---------- daemon 生命周期 ----------

    def start_daemon(self, timeout: float = 90.0) -> dict:
        if self.daemon is not None:
            raise RuntimeError("daemon already started")
        d = IsolatedDaemonProxy(self)  # 延迟构造避免循环依赖
        self.daemon = d
        manifest = d.start(timeout=timeout)
        d.verify_consistency()
        self._manifest = manifest
        return manifest

    def _client(self) -> HttpDaemonRpcClient:
        """RPC 客户端：显式 endpoint（本 run 根 daemon），不做 manifest 发现。

        manifest 一致性与 PID 自洽已由 IsolatedDaemon.verify_consistency()
        证明；此处显式钉 endpoint 避免发现逻辑读到生产 manifest。
        """
        if self._rpc is None:
            endpoint = str(self._manifest.get("endpoint", ""))
            if not endpoint:
                raise RuntimeError("daemon manifest 缺 endpoint；先 start_daemon()")
            self._rpc = HttpDaemonRpcClient(
                endpoint=endpoint,
                manifest_path="",           # 显式 endpoint，跳过 manifest 校验
                authority_id=str(self._manifest.get("authority_id", "")),
                verify_health=False,
                validate_manifest=False,
                timeout=60.0,
            )
        return self._rpc

    def rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
            request_id: str = "") -> RpcResult:
        """daemon RPC 直调；业务拒绝（DaemonRemoteError）转为 RpcResult(ok=False)。

        超时走客户端默认（慢方法前缀自动放宽）。需要更长超时的外部依赖类
        方法（semgrep / LSP / 向量）由子类自持慢客户端（见 ExternalEnv）。
        """
        try:
            res = self._client().call(method, params or {},
                                      request_id=request_id or None)
            return RpcResult(method, True, result=res)
        except DaemonRemoteError as e:
            return RpcResult(method, False, code=str(getattr(e, "code", "")),
                             message=str(e))
        except DaemonUnavailableError as e:
            return RpcResult(method, False, code="E_DAEMON_UNAVAILABLE",
                             message=str(e))

    def stop_daemon(self) -> dict:
        if self.daemon is None:
            return {"stopped": False}
        info = self.daemon.stop()
        self.daemon.dump_log()
        self.daemon = None
        self._rpc = None
        return info

    # ---------- CLI 子进程（仅用于 workspace 播种与 CLI 面观察） ----------

    def cli_env(self) -> dict:
        env = super().cli_env()
        # 🔴 TP-09 的 CALLWARDEN_AUTO_SETUP=0 是无效变量名；正确 opt-out 是
        # CALLWARDEN_SKIP_AUTO_SETUP=1（_check_auto_setup 只认它），否则首次
        # CLI 调用会往 run 根 home 写 13 个 MCP 配置文件。
        env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
        return env

    def run_cli(self, args: List[str], timeout: float = 150.0,
                cwd: Optional[Path] = None) -> Dict[str, Any]:
        import subprocess
        r = subprocess.run(
            [PYTHON, str(CW_PY)] + args,
            cwd=str(cwd or self.workspace_dir), env=self.cli_env(),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
        return {"rc": r.returncode, "stdout": r.stdout or "",
                "stderr": r.stderr or ""}

    # ---------- workspace 前置（lease 面要求 active workspace） ----------

    def setup_workspace(self) -> Tuple[int, str]:
        """register + activate 隔离 workspace，返回 (task_db_workspace_id, instance_id)。

        registry 的 numeric id 与权威库（task-DB）自增 id 是**两个 ID 空间**
        （fs_handlers.rs:95 ensure_workspace_row_by_root 按 root_path 唯一键
        分配）；task.create 需要的是权威库 id。权威配对经 workspace.status 的
        task_db_workspace_id / task_db_instance_id 取得（与
        daemon_client.resolve_workspace_pair_from_daemon 同源，禁止客户端
        推断/合成 numeric id）。幂等：重复调用直接返回已解析配对。
        """
        if self.workspace_id is not None and self.workspace_instance_id:
            return self.workspace_id, self.workspace_instance_id
        # git provenance 必须在首次 register 前就绪：instance id 与 snapshot_id
        # 都由 (owner_uid, norm_root, git_remote_url, git_head_commit_sha)
        # 计算（workspace.rs:437），事后补 git 会产生同 root 的第二个 instance。
        git_remote, git_head = self._ensure_git_workspace()
        reg = self.rpc("workspace.register", {
            "client_view_root": str(self.workspace_dir),
            "git_remote_url": git_remote,
            "git_head_commit_sha": git_head,
        })
        if not reg.ok or not isinstance(reg.result, dict):
            raise RuntimeError(f"workspace.register 失败：{reg!r}")
        inst = str(reg.result.get("workspace_instance_id"))
        # workspace.activate 只认 workspace_instance_id（Rust workspace.rs:1829）
        act = self.rpc("workspace.activate", {"workspace_instance_id": inst})
        if not act.ok:
            raise RuntimeError(f"workspace.activate 失败：{act!r}")
        self.workspace_instance_id = inst
        ws_id = self._resolve_task_db_workspace_id(inst)
        self.workspace_id = ws_id
        return ws_id, inst

    def _ensure_git_workspace(self) -> Tuple[str, str]:
        """确保 workspace 目录是带 remote + commit 的 git 仓（snapshot 前置）。

        snapshot_id 只在 register 同时提供 git_remote_url + git_head_commit_sha
        时计算（workspace.rs:437）；空 provenance → snapshot_id=None →
        snapshot.publish 无 id → task.report 的 E_TASK_REPORT_SNAPSHOT_REQUIRED。
        remote 用本地 file:// URL（不联网、零外部依赖）。

        🔴 自愈：只查 `.git` 目录存在不够——run 根 wipe 残留的**坏 .git**
        （rmtree 被占用句柄部分删除）会让 is_dir()=True 但 rev-parse 失败。
        故以 rev-parse 结果为权威，不可用则删 .git 重建。
        """
        import subprocess
        wd = str(self.workspace_dir)
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git"] + list(args), cwd=wd,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)

        def _repo_ok() -> bool:
            return (self.workspace_dir / ".git").is_dir() and \
                git("rev-parse", "HEAD").returncode == 0

        if not _repo_ok():
            git_dir = self.workspace_dir / ".git"
            if git_dir.exists():
                import shutil
                shutil.rmtree(git_dir, ignore_errors=True)
            git("init", "-q")
            (self.workspace_dir / "README.md").write_text(
                "# tp11g governance isolation workspace\n", encoding="utf-8")
            git("add", "-A")
            git("-c", "user.email=tp11g@example.invalid",
                "-c", "user.name=tp11g", "commit", "-q", "-m", "tp11g seed")
        # remote 用本地 file:// URL（deterministic，不依赖任何外部服务）
        remote = f"file://{self.workspace_dir.as_posix()}"
        cur = git("remote", "get-url", "origin")
        if cur.returncode != 0 or cur.stdout.strip() != remote:
            git("remote", "remove", "origin")
            git("remote", "add", "origin", remote)
        head = git("rev-parse", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(f"git rev-parse HEAD 失败：{head.stderr[:200]}")
        return remote, head.stdout.strip()

    def _resolve_task_db_workspace_id(self, instance_id: str) -> Optional[int]:
        """取权威库 numeric id（task.create 的 workspace_id）。

        优先经 workspace.status 的 task_db_workspace_id（capture 链已建立时）；
        全新隔离环境首条 capture 尚未建立（由首次 task.create 自己写），此时
        回退为**只读**查权威库 workspaces 表、按 root_path 匹配——该 id 由
        ensure_workspace_row_by_root 按 root_path 唯一键分配（fs_handlers.rs:95），
        与 registry numeric id 是两个 ID 空间，禁止互相套用。
        """
        st = self.rpc("workspace.status", {"workspace_instance_id": instance_id})
        if st.ok and isinstance(st.result, dict):
            v = st.result.get("task_db_workspace_id")
            if isinstance(v, int) and v > 0:
                return v
        return self._read_task_db_workspace_id_by_root()

    def _read_task_db_workspace_id_by_root(self) -> Optional[int]:
        """只读查权威库 workspaces 表按 root_path 取 id（建图后可用）。"""
        import sqlite3
        db = self.run_root / "home" / ".callwarden" / "callwarden.db"
        if not db.is_file():
            return None
        target = self._norm_root(str(self.workspace_dir))
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for wid, root in conn.execute("SELECT id, root_path FROM workspaces"):
                if self._norm_root(str(root)) == target:
                    return int(wid)
        except Exception:
            return None
        finally:
            conn.close()
        return None

    @staticmethod
    def _norm_root(p: str) -> str:
        """root_path 规范化（与 ensure_workspace_row_by_root 的 normalize 对齐：
        小写 + 反斜杠转正斜杠 + 去尾部斜杠 + 去 \\\\?\\ 前缀）。"""
        s = str(p).replace("\\\\?\\", "").replace("\\", "/").rstrip("/")
        return s.lower()

    def workspace_pair_params(self) -> Dict[str, Any]:
        """task.create 的 BR-02 workspace 配对参数（daemon 权威解析值）。

        权威库 workspaces 行须先由 codegraph 构建路径创建（调用方先
        build_graph/refresh）；未就绪时 fail-closed，不用 registry numeric id
        补齐（registry/task-DB 是两个 ID 空间）。
        """
        ws_id, inst = self.setup_workspace()
        if ws_id is None or not inst:
            raise RuntimeError(
                "权威库 workspace 行未就绪（task_db_workspace_id 缺失）；"
                "请先对 workspace 根执行 build_graph/refresh 再创建任务")
        return {"workspace_id": ws_id, "workspace_instance_id": inst}

    # ---------- 建图前置（权威库 workspaces 行来源） ----------

    def build_graph(self, timeout: float = 600.0) -> Dict[str, Any]:
        """CLI refresh --all 建图：触发权威库 workspaces 行 + codegraph 读面。

        与 TP-10C 已验证配方一致（公开 CLI ONLY，不写 run 根外）。
        """
        r = self.run_cli(["refresh", "--all"], timeout=timeout)
        if r["rc"] != 0:
            raise RuntimeError(
                f"refresh --all 失败：{r['stdout'][:400]}{r['stderr'][:400]}")
        # 建图后重解析权威配对（task_db_workspace_id 此时才可用）
        self.workspace_id = None
        self.workspace_instance_id = None
        return self.setup_workspace()

    # ---------- 身份 / 合同 ----------

    @staticmethod
    def identity(role_key: str) -> Dict[str, str]:
        """三角色结构化身份（核心四字段 + agent_instance_id）。"""
        r = ROLE_IDENTITIES[role_key]
        return {
            "agent_id": r["agent_id"],
            "session_id": r["session_id"],
            "model_id": r["model_id"],
            "role": r["role"],
            "agent_instance_id": r["agent_instance_id"],
        }

    @staticmethod
    def identity_as(holder_key: str, acting_role: str) -> Dict[str, str]:
        """以某角色身份三元组、但 acting_role 标另一角色的身份。

        close 的 holder 校验只比对 agent_id/session_id/model_id
        （validate_lease_for_mutation 步骤 6，不含 role），故 adjudicator
        可持 reviewer 的 lease、以 reviewer 三元组 + role=adjudicator 执行
        close（P0-E 语义：reviewer 持 lease、adjudicator 执行）。
        """
        ident = GovernanceEnv.identity(holder_key)
        ident["role"] = acting_role
        return ident

    @staticmethod
    def lease_role(role_key: str) -> str:
        return ROLE_IDENTITIES[role_key]["lease_role"]

    def contract_claim(self, task_id: str, role_key: str) -> Optional[Dict[str, str]]:
        """task.contract_get → contract_claim{skill_id,skill_version,prompt_hash}。

        🔴 查询用的是**合同角色名**（executor/reviewer/adjudicator，即
        role_contracts.role），不是 identity 角色（implementer/reviewer/
        adjudicator）——两者对 executor 差一个词，用错会拿到空合同。

        无冻结合同时返回 None（表示无需携带）；与 CLI _fetch_contract_claim
        同源（权威读面 daemon RPC）。
        """
        role = CONTRACT_ROLE_NAMES[role_key]
        res = self.rpc("task.contract_get", {"task_id": task_id, "role": role})
        if not res.ok or not isinstance(res.result, dict):
            return None
        for c in (res.result.get("contracts") or []):
            if c.get("role") == role and c.get("skill_id"):
                return {
                    "skill_id": c["skill_id"],
                    "skill_version": c.get("skill_version", ""),
                    "prompt_hash": c.get("prompt_hash", ""),
                }
        return None

    def contract_detail(self, task_id: str, role_key: str) -> Optional[Dict[str, Any]]:
        """取该角色的完整合同行（verdict.submit 的 contract/role_contract 源）。"""
        role = CONTRACT_ROLE_NAMES[role_key]
        res = self.rpc("task.contract_get", {"task_id": task_id, "role": role})
        if not res.ok or not isinstance(res.result, dict):
            return None
        for c in (res.result.get("contracts") or []):
            if c.get("role") == role:
                return c
        return None

    # ---------- 身份注册（claim 前置：未注册 agent 禁领） ----------

    def agent_register(self, role_key: str) -> RpcResult:
        """注册三角色 agent 身份（claim 前置；未注册 → E_IDENTITY_UNREGISTERED）。

        🔴 agent_id 必须走**顶层扁平参数**：handle_agent_register 只从
        params.agent_id 读 agent_id（task_collab.rs:2717），identity 对象里的
        agent_id 不参与；缺席会回退注册成 agent-{owner_key}，导致 claim 时
        「未注册身份」。
        """
        r = ROLE_IDENTITIES[role_key]
        return self.rpc("agent.register", {
            "agent_id": r["agent_id"],
            "agent_name": r["agent_id"],
            "capabilities": [],
            "identity": {
                "agent_id": r["agent_id"],
                "session_id": r["session_id"],
                "model_id": r["model_id"],
                "role": r["role"],
                "agent_instance_id": r["agent_instance_id"],
            },
        })

    def register_all_roles(self) -> Dict[str, RpcResult]:
        return {k: self.agent_register(k) for k in ROLE_IDENTITIES}

    # ---------- 证据清单（bootstrap / verdict 需要 path+hash） ----------

    def write_evidence(self, name: str, payload: Dict[str, Any]) -> Tuple[str, str]:
        """写证据清单到 run 根，返回 (abspath, sha256)。"""
        import hashlib
        path = self.reports_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(blob)
        return str(path), hashlib.sha256(blob).hexdigest()

    # ---------- A′ 冷启动引导（P0-F Bootstrap Evidence / Review Bridge） ----------

    def bootstrap_executor_evidence(self, task_id: str, step_id: str,
                                    role_key: str = "executor",
                                    request_id: str = "") -> RpcResult:
        """executor 逐项追加 completed-step bootstrap evidence 并推 review。

        仅当任务已 immutable workspace binding 且 Task/Role/step 投影全空。
        """
        ws_id, inst = self.setup_workspace()
        epath, ehash = self.write_evidence(
            f"tp11g_bootstrap_evidence_{task_id}.json",
            {"task_id": task_id, "step_id": step_id, "kind": "bootstrap_executor_evidence"})
        return self.rpc("task.bootstrap_executor_evidence", {
            "task_id": task_id,
            "steps": [{"step_id": step_id,
                       "evidence_path": epath, "evidence_hash": ehash}],
            "workspace_id": ws_id,
            "workspace_instance_id": inst,
            "request_id": request_id or f"tp11g-bee-{task_id}",
            "identity": self.identity(role_key),
        })

    def bootstrap_reviewer_pass(self, task_id: str,
                                role_key: str = "reviewer",
                                request_id: str = "") -> RpcResult:
        """reviewer bootstrap pass + daemon 同事务签发专用 reviewer lease。

        reviewer 身份须与 executor 三重（agent/instance/session）不同。
        """
        ws_id, inst = self.setup_workspace()
        epath, ehash = self.write_evidence(
            f"tp11g_bootstrap_reviewer_pass_{task_id}.json",
            {"task_id": task_id, "kind": "bootstrap_reviewer_pass"})
        return self.rpc("task.bootstrap_reviewer_pass", {
            "task_id": task_id,
            "workspace_id": ws_id,
            "workspace_instance_id": inst,
            "request_id": request_id or f"tp11g-brp-{task_id}",
            "evidence_path": epath,
            "evidence_hash": ehash,
            "identity": self.identity(role_key),
        })

    # ---------- Snapshot（verdict 前置：snapshot_id 必填） ----------

    def snapshot_publish(self, request_id: str = "",
                         instance_id: str = "") -> RpcResult:
        """发布 review snapshot，返回含 snapshot_id 的响应。

        db_path 须绝对路径（不支持 ~ 展开）；workspace_instance_id 必带。
        instance_id 非空时对指定 workspace 发布（默认主 workspace）。
        """
        ws_id, inst = self.setup_workspace()
        inst = instance_id or inst
        db = self.run_root / "home" / ".callwarden" / "callwarden.db"
        return self.rpc("snapshot.publish", {
            "workspace_instance_id": inst,
            "workspace_root": str(self.workspace_dir),
            "db_path": str(db),
            "build_context_hash": "",
            "snapshot_id": "",
            "request_id": request_id or f"tp11g-snap-{inst}",
        })

    # ---------- Verdict（reviewer 盲审） ----------

    def verdict_submit(self, task_id: str, step_id: str, contract: Dict[str, Any],
                       role_contract: Dict[str, Any], snapshot_id: str,
                       role_key: str = "reviewer", overall: str = "pass",
                       phase: str = "blind_first_pass",
                       lease_token: str = "", fencing_counter: int = -1,
                       request_id: str = "",
                       view_manifest_hash: str = "") -> RpcResult:
        """提交 Reviewer Verdict（verdict.submit，GOVERNANCE_WRITE）。

        contract/role_contract 权威来源优先用 `task.next_action` 响应：
        - task_contract: {id, revision, hash}
        - role_contract: {id(=lineage_id) 或 lineage_id, revision, hash}
        （create 响应的 governance_projection.role_contracts 用 lineage_id，
        next_action 的 role_contract 用 id——两者指同一 lineage。）
        view_manifest_hash 来自 role_view.get(role=reviewer)（必填）。
        identity 五字段齐备且 role 限定 reviewer/independent_reviewer。
        """
        if not view_manifest_hash:
            rv = self.rpc("role_view.get",
                          {"task_id": task_id,
                           "role": CONTRACT_ROLE_NAMES[role_key]})
            view_manifest_hash = rv.get("view_manifest_hash") if rv.ok else ""
        role_lineage = role_contract.get("lineage_id") or role_contract.get("id") or ""
        params: Dict[str, Any] = {
            "task_id": task_id,
            "step_id": step_id,
            "contract_id": contract["id"],
            "contract_hash": contract["hash"],
            "contract_revision": int(contract["revision"]),
            "role_contract_id": role_lineage,
            "role_contract_hash": role_contract["hash"],
            "role_contract_revision": int(role_contract["revision"]),
            "snapshot_id": snapshot_id,
            "request_id": request_id or f"tp11g-verdict-{task_id}",
            "phase": phase,
            "overall": overall,
            "attestation": "tp11g independent review attestation",
            "amendment_ref": "",
            "clause_results": [],
            "findings": [],
            "view_manifest_hash": view_manifest_hash,
            "identity": self.identity(role_key),
        }
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        return self.rpc("verdict.submit", params)

    # ---------- 治理投影（只读，Reviewer 权威投影，绝不返回 lease token） ----------

    def governance_projection(self, task_id: str) -> RpcResult:
        return self.rpc("task.governance_projection.get", {"task_id": task_id})

    def next_action(self, task_id: str) -> RpcResult:
        ws_id, inst = self.setup_workspace()
        return self.rpc("task.next_action", {
            "task_id": task_id, "workspace_instance_id": inst})

    # ---------- 任务生命周期 ----------

    def task_create(self, title: str, desc: str = "",
                    steps: Optional[List[Dict[str, Any]]] = None,
                    role_contracts: Optional[List[Dict[str, Any]]] = None,
                    parent_id: str = "") -> RpcResult:
        """创建任务。

        默认携带 A′ 标准三角色 legacy 合同（APRIME_ROLE_CONTRACTS）——daemon
        不建默认模板，缺合同任务 governance_blocked 且无法 claim/bootstrap。
        """
        params: Dict[str, Any] = {
            "title": title,
            "description": desc,
            "steps": steps if steps is not None else [],
            "creator": "agent",
            "role_contracts": (APRIME_ROLE_CONTRACTS if role_contracts is None
                               else role_contracts),
            "identity_policy": "legacy_identity_v1",
        }
        if parent_id:
            params["parent_id"] = parent_id
        params.update(self.workspace_pair_params())
        return self.rpc("task.create", params)

    def task_status(self, task_id: str) -> RpcResult:
        return self.rpc("task.status", {"task_id": task_id})

    def task_list(self) -> RpcResult:
        """任务列表（BR-02：须显式 workspace_id，禁 active/cwd 补齐）。"""
        ws_id, _ = self.setup_workspace()
        if ws_id is None:
            return RpcResult("task.list", False, code="E_TASK_WORKSPACE_UNBOUND",
                             message="权威库 workspace 行未就绪")
        return self.rpc("task.list", {"workspace_id": ws_id})

    def task_claim(self, task_id: str, role_key: str = "executor",
                   remediation_step_id: str = "") -> RpcResult:
        """claim 首个待执行步骤（P3 身份 + contract_claim + agent_session_id）。

        daemon 门禁：identity.session_id 必须等于 agent_session_id
        （task_collab_lease.rs:222），否则 E_IDENTITY_SESSION_MISMATCH。
        """
        ident = self.identity(role_key)
        params: Dict[str, Any] = {"task_id": task_id, "identity": ident,
                                  "agent_session_id": ident["session_id"]}
        if remediation_step_id:
            params["remediation_step_id"] = remediation_step_id
        cc = self.contract_claim(task_id, role_key)
        if cc:
            params["contract_claim"] = cc
        return self.rpc("task.claim", params)

    def task_report(self, task_id: str, step_id: str, role_key: str,
                    summary: str = "tp11g step done",
                    changes: Optional[List[Dict[str, Any]]] = None,
                    lease_token: str = "", fencing_counter: int = -1,
                    snapshot_id: str = "",
                    evidence_path: str = "",
                    evidence_hash: str = "") -> RpcResult:
        """回报步骤结果（纯调查步骤省略 changes 最稳）。

        snapshot_id 必填（E_TASK_REPORT_SNAPSHOT_REQUIRED，须先
        snapshot.publish）。evidence_path/hash 建议显式提供：后续
        executor_ready_for_review handoff 的 provenance 校验要求
        step/evidence 三元组与已持久化 report 事件一致。
        """
        params: Dict[str, Any] = {
            "task_id": task_id, "step_id": step_id, "summary": summary,
            "success": True, "identity": self.identity(role_key),
        }
        if changes is not None:
            params["changes"] = changes
        if snapshot_id:
            params["snapshot_id"] = snapshot_id
        if evidence_path:
            params["evidence_path"] = evidence_path
        if evidence_hash:
            params["evidence_hash"] = evidence_hash
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        cc = self.contract_claim(task_id, role_key)
        if cc:
            params["contract_claim"] = cc
        return self.rpc("task.report", params)

    def task_handoff(self, task_id: str, from_role_key: str, to_role_key: str,
                     outcome: str, next_action: str,
                     reason: str = "tp11g handoff",
                     step_id: str = "", request_id: str = "",
                     report_request_id: str = "",
                     evidence_path: str = "", evidence_hash: str = "",
                     lease_token: str = "",
                     fencing_counter: int = -1) -> RpcResult:
        """结构化交接（from_role 结构化；identity 与 verdict/handoff 一致）。

        evidence_path/evidence_hash 必填（E_HANDOFF_STRUCTURED_REQUIRED）：
        留空时自动写一份证据清单到 run 根并计算 sha256。
        """
        if not evidence_path or not evidence_hash:
            evidence_path, evidence_hash = self.write_evidence(
                f"tp11g_handoff_{task_id}_{from_role_key}.json",
                {"task_id": task_id, "from_role": from_role_key,
                 "to_role": to_role_key, "outcome": outcome,
                 "step_id": step_id, "kind": "handoff"})
        params: Dict[str, Any] = {
            "task_id": task_id,
            "from_role": CONTRACT_ROLE_NAMES[from_role_key],
            "outcome": outcome,
            "next_role": CONTRACT_ROLE_NAMES[to_role_key],
            "next_action": next_action,
            "reason": reason,
            "independence_requirement": "required",
            "request_id": request_id or f"tp11g-ho-{task_id}-{from_role_key}",
            "step_id": step_id,
            "report_request_id": report_request_id or f"tp11g-rr-{task_id}",
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
            "identity": self.identity(from_role_key),
        }
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        return self.rpc("task.handoff", params)

    def task_apply(self, task_id: str, role_key: str = "reviewer",
                   lease_token: str = "",
                   fencing_counter: int = -1) -> RpcResult:
        """review → applied（identity 三元组须与 lease holder 一致）。"""
        params: Dict[str, Any] = {"task_id": task_id,
                                  "identity": self.identity(role_key)}
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        return self.rpc("task.apply", params)

    def task_close(self, task_id: str, role_key: str = "adjudicator",
                   lease_token: str = "",
                   fencing_counter: int = -1,
                   lease_holder_key: str = "") -> RpcResult:
        """applied → closed。

        lease_holder_key 非空时以该角色身份三元组 + role=adjudicator 执行
        （P0-E：reviewer 持 lease、adjudicator 执行 close）；默认用 role_key
        自身三元组。
        """
        if lease_holder_key:
            ident = self.identity_as(lease_holder_key,
                                     ROLE_IDENTITIES[role_key]["role"])
        else:
            ident = self.identity(role_key)
        params: Dict[str, Any] = {"task_id": task_id,
                                  "identity": ident,
                                  "reviewer": ROLE_IDENTITIES["reviewer"]["role"]}
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        return self.rpc("task.close", params)

    def task_supersede(self, old_task_id: str, new_task_id: str,
                       reason: str = "tp11g supersede",
                       role_key: str = "adjudicator",
                       request_id: str = "",
                       lease_token: str = "",
                       fencing_counter: int = -1,
                       evidence_path: str = "",
                       evidence_hash: str = "") -> RpcResult:
        """作废出口：old 被 new 替代（append-only，不改被替代任务行）。

        🔴 daemon 参数名是 superseded_id / superseding_id（不是 old/new_task_id）；
        request_id / workspace_instance_id / reviewer lease 凭证 /
        evidence manifest（path+hash）均为必填（幂等 key 成员 + 治理写门禁，
        fail-closed）。evidence 留空时自动写一份到 run 根。
        """
        if not evidence_path or not evidence_hash:
            evidence_path, evidence_hash = self.write_evidence(
                f"tp11g_supersede_{old_task_id}.json",
                {"superseded_id": old_task_id, "superseding_id": new_task_id,
                 "reason": reason, "kind": "supersede_evidence"})
        params: Dict[str, Any] = {
            "superseded_id": old_task_id,
            "superseding_id": new_task_id,
            "reason": reason,
            "request_id": request_id or f"tp11g-sup-{old_task_id}-{new_task_id}",
            "workspace_instance_id": self.setup_workspace()[1],
            "evidence_path": evidence_path,
            "evidence_hash": evidence_hash,
        }
        if role_key:
            params["identity"] = self.identity(role_key)
        if lease_token:
            params["lease_token"] = lease_token
        if fencing_counter >= 0:
            params["fencing_counter"] = fencing_counter
        return self.rpc("task.supersede", params)

    def task_superseded_by(self, task_id: str) -> RpcResult:
        return self.rpc("task.superseded_by", {"task_id": task_id})

    def task_cascade_close(self, task_id: str) -> RpcResult:
        return self.rpc("task.cascade_close", {"task_id": task_id})

    def task_claim_recover(self, task_id: str, role_key: str = "adjudicator",
                           reason: str = "tp11g orphaned claim",
                           request_id: str = "") -> RpcResult:
        return self.rpc("task.claim.recover", {
            "task_id": task_id, "reason": reason,
            "request_id": request_id or f"tp11g-cr-{task_id}",
            "identity": self.identity(role_key)})

    # ---------- 租约生命周期 ----------

    @staticmethod
    def _lease_identity(role_key: str) -> Dict[str, str]:
        """lease 面身份：agent_id/session_id/model_id/role 四核心字段齐备
        （daemon E_IDENTITY_INCOMPLETE 要求；agent_instance_id 可选但保持与
        注册一致）。"""
        r = ROLE_IDENTITIES[role_key]
        return {
            "agent_id": r["agent_id"],
            "session_id": r["session_id"],
            "model_id": r["model_id"],
            "role": r["lease_role"],
            "agent_instance_id": r["agent_instance_id"],
        }

    def lease_acquire(self, task_id: str, role_key: str,
                      ttl: float = 3600.0) -> RpcResult:
        return self.rpc("lease.acquire", {
            "task_id": task_id, "role": self.lease_role(role_key),
            "identity": self._lease_identity(role_key),
            "ttl_seconds": ttl})

    def lease_renew(self, task_id: str, role_key: str, token: str,
                    ttl: float = 3600.0) -> RpcResult:
        # 🔴 参数名是 token（handle_lease_extend 只认 params.token），不是 lease_token
        return self.rpc("lease.renew", {
            "task_id": task_id, "role": self.lease_role(role_key),
            "token": token,
            "identity": self._lease_identity(role_key),
            "ttl_seconds": ttl})

    def lease_release(self, task_id: str, role_key: str,
                      token: str) -> RpcResult:
        # 🔴 参数名是 token（handle_lease_release 只认 params.token）
        return self.rpc("lease.release", {
            "task_id": task_id, "role": self.lease_role(role_key),
            "token": token,
            "identity": self._lease_identity(role_key)})

    def lease_status(self, task_id: str, role: str = "") -> RpcResult:
        params: Dict[str, Any] = {"task_id": task_id}
        if role:
            params["role"] = role
        return self.rpc("lease.status", params)

    def lease_list_events(self, task_id: str = "",
                          role: str = "") -> RpcResult:
        params: Dict[str, Any] = {}
        if task_id:
            params["task_id"] = task_id
        if role:
            params["role"] = role
        return self.rpc("lease.list_events", params)

    # ---------- token / counter 提取 ----------

    @staticmethod
    def extract_token(res: RpcResult) -> Optional[str]:
        """从 lease acquire 响应提取 raw token（不同键名兜底）。"""
        d = res.result
        if not isinstance(d, dict):
            return None
        for k in ("lease_token", "token", "raw_token", "credential"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for k2 in ("token", "raw_token", "value"):
                    if isinstance(v.get(k2), str) and v[k2]:
                        return v[k2]
        return None

    @staticmethod
    def extract_counter(res: RpcResult) -> Optional[int]:
        d = res.result
        if not isinstance(d, dict):
            return None
        for k in ("fencing_counter", "counter"):
            v = d.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, dict) and isinstance(v.get("fencing_counter"), int):
                return v["fencing_counter"]
        return None


class IsolatedDaemonProxy:
    """IsolatedDaemon 薄包装：避免 harness import 期循环依赖。

    env 已持有 run_root/daemon_exe/expected_sha256；本代理把这些传给
    daemon_bootstrap.IsolatedDaemon 并转发 start/stop/verify_consistency/
    dump_log。
    """

    def __init__(self, env: GovernanceEnv):
        from daemon_bootstrap import IsolatedDaemon
        self._inner = IsolatedDaemon(run_root=env.run_root,
                                     daemon_exe=env.daemon_exe,
                                     expected_sha256=env.expected_sha256)

    def start(self, timeout: float = 90.0) -> dict:
        return self._inner.start(timeout=timeout)

    def stop(self) -> dict:
        return self._inner.stop()

    def verify_consistency(self) -> dict:
        return self._inner.verify_consistency()

    def dump_log(self, path: Optional[Path] = None) -> None:
        if path is None:
            path = self._inner.run_root / "reports" / "daemon.log"
        self._inner.dump_log(path)
