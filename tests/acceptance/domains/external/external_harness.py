"""TP-11X 外部依赖（Git / Semgrep / LSP / 向量）验收 harness。

复用 TP-11I 隔离底座（GovernanceEnv + registry fixture + run 根自清洁），在
完全隔离的 daemon 上验证「外部依赖面」的**真实可达性与真实响应**：

- X1 可达性矩阵：逐个 method 实测是否可达；不可达（method_not_found）固化
  该事实，可达的断言真实响应结构（不写「调用成功即通过」的占位断言）；
- X2 Git 面：harness workspace 自带真实 git 仓（remote + 首次 commit），
  经 `git_history` 作业导入后断言**具体提交哈希 / 具体计数**；
- X3 Semgrep 面：本机实测 semgrep 1.168.0 已安装（外部工具真实可用），
  断言扫描产物结构；工具缺失场景如实标注；
- X4 LSP 面：6 方法（lsp_hover/definition/references/diagnostics/
  completion/check_available），其中 check_available 是真实 `where` 探测，
  其余 5 个为 fail-soft 空结构（daemon 无 LSP 客户端，见 handler 文档）；
- X5 向量/嵌入面：semantic_search / find_similar_functions 读
  symbol_embeddings；嵌入源为 `embed` 作业（daemon 确定性 hash-v1 向量）。

🔴 关键架构事实（dispatch 层核实，勿猜 method 名）：
- **MCP 工具名 ≠ RPC method**。例如 `run_semgrep_scan` / `embed_symbols` /
  `import_git_history` 是 MCP/CLI 名，其 RPC 通道是 `task.job_submit`
  （job_type=semgrep_scan / embed / git_history）；裸名调用 →
  method_not_found。
- 收敛方法名经 `CONVERGENCE_RPC_METHODS`
  （rust_ext/src/daemon/dispatch.rs:2447）登记后走
  `SnapshotDaemonState::handle_convergence_rpc`
  （rust_ext/src/daemon/snapshot_state.rs:2771）；未登记 → method_not_found。
  本项目已踩过一次：`import_test_results` / `build_test_relations` handler
  已写、match 臂已注册，但**名单未登记** → 恒 method_not_found。
  本域的可达性矩阵即该类缺陷的回归护栏。

零生产触碰：run 根外不写任何文件；fixture 只用 harness 自建的
`tp11x-ws`（与真实任务/受跟踪源文件无交集）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
_ISO_DIR = _HERE.parent / "isolation"
for _d in (str(_HERE), str(_GOV_DIR), str(_ISO_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 复用 TP-11I 底座（GovernanceEnv + run 根自清洁 wipe_run_root）
from isolation_harness import IsolationEnv  # noqa: E402
from governance_harness import RpcResult  # noqa: E402
from server.daemon_client import (  # noqa: E402
    HttpDaemonRpcClient,
    DaemonRemoteError,
    DaemonUnavailableError,
)

# TP-11X 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_EXT = Path(
    os.environ.get("CW_TP11X_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11X"))

# 本域 fixture git 仓的固定种子值（确定性断言用，不联网）
GIT_SEED_EMAIL = "tp11x@example.invalid"
GIT_SEED_AUTHOR = "tp11x"
GIT_SEED_FILE = "README.md"


class ExternalEnv(IsolationEnv):
    """TP-11X 外部依赖执行环境：TP-11 底座 + Git/Semgrep/LSP/向量面助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11x-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_EXT,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)
        self._slow_rpc: Optional[HttpDaemonRpcClient] = None

    # 🔴 外部依赖类 RPC 首调常超默认 30s（semgrep 进程冷启动、LSP 探测、
    # 向量嵌入），统一放宽。慢方法自动放宽只认 task.* 前缀（daemon_client
    # 的 _rpc_timeout_for），这些方法不在其中；且 HttpDaemonRpcClient.call
    # 只认构造时 timeout，故为它们单独持一个慢客户端。
    SLOW_EXTERNAL_PREFIXES = ("run_semgrep", "scan_semgrep", "get_semgrep",
                              "semantic_search", "find_similar", "lsp_")
    SLOW_EXTERNAL_TIMEOUT = 180.0

    def _slow_client(self) -> HttpDaemonRpcClient:
        if self._slow_rpc is None:
            endpoint = str(self._manifest.get("endpoint", ""))
            if not endpoint:
                raise RuntimeError("daemon manifest 缺 endpoint；先 start_daemon()")
            self._slow_rpc = HttpDaemonRpcClient(
                endpoint=endpoint, manifest_path="",
                timeout=self.SLOW_EXTERNAL_TIMEOUT)
        return self._slow_rpc

    def rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
            request_id: str = "", timeout: Optional[float] = None) -> Any:
        if timeout is None and any(method.startswith(p)
                                   for p in self.SLOW_EXTERNAL_PREFIXES):
            try:
                res = self._slow_client().call(method, params or {},
                                               request_id=request_id or None)
                return RpcResult(method, True, result=res)
            except DaemonRemoteError as e:
                return RpcResult(method, False, code=str(getattr(e, "code", "")),
                                 message=str(e))
            except DaemonUnavailableError as e:
                return RpcResult(method, False, code="E_DAEMON_UNAVAILABLE",
                                 message=str(e))
        return super().rpc(method, params, request_id=request_id)

    # ---------- 本地 git 直查（读面真相源，独立于 daemon 响应） ----------

    def git(self, *args: str) -> subprocess.CompletedProcess:
        """在 fixture workspace 内执行 git（不联网，仅本地仓）。"""
        return subprocess.run(["git"] + list(args), cwd=str(self.workspace_dir),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)

    def local_head(self) -> str:
        r = self.git("rev-parse", "HEAD")
        if r.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD 失败：{r.stderr[:200]}")
        return r.stdout.strip()

    def local_commit_count(self) -> int:
        r = self.git("rev-list", "--count", "HEAD")
        if r.returncode != 0:
            raise RuntimeError(f"git rev-list --count 失败：{r.stderr[:200]}")
        return int(r.stdout.strip())

    def local_commit_list(self) -> List[str]:
        """本地 `git log` 的全量提交哈希（新→旧）。"""
        r = self.git("log", "--format=%H")
        if r.returncode != 0:
            raise RuntimeError(f"git log 失败：{r.stderr[:200]}")
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    # ---------- fixture 播种（仅 harness 自建 tp11x-ws，零生产触碰） ----------

    def write_fixture_sources(self) -> List[str]:
        """写入 fixture 源码（须在 build_graph 之前调用，供符号索引消费）。

        返回写入的相对路径列表。文件只落在 harness run 根内的 tp11x-ws，
        与受跟踪源文件零交集。
        """
        files = {
            "ext/alpha.py": (
                "def alpha() -> int:\n"
                "    \"\"\"TP-11X fixture: alpha helper.\"\"\"\n"
                "    return 1\n"
            ),
            "ext/beta.py": (
                "def beta() -> int:\n"
                "    return 2\n"
                "\n"
                "\n"
                "def alpha_wrapper() -> int:\n"
                "    return alpha() + 1\n"
            ),
            "ext/unsafe.py": (
                "def evaluate(expr: str):\n"
                "    return eval(expr)\n"
            ),
        }
        out = []
        for rel, body in files.items():
            target = self.workspace_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            out.append(rel)
        return out

    def write_semgrep_rule(self, name: str = "tp11x_rule.yml") -> str:
        """写本地 semgrep 规则到 run 根，返回绝对路径。

        🔴 用本地规则文件（而非默认 `p/default`）**强制零联网**：默认配置
        会去 semgrep registry 拉取规则集；本地 YAML 由 `semgrep --config
        <path>` 直接消费，全程离线且结果确定性。
        """
        path = self.run_root / name
        # 🔴 message 必须加引号：YAML 裸值里出现第二个 `: ` 会被判为
        # 「mapping values are not allowed here」（semgrep error code 5）。
        path.write_text(
            "rules:\n"
            "  - id: tp11x-no-eval\n"
            "    pattern: eval(...)\n"
            "    message: \"TP-11X fixture rule - eval is forbidden\"\n"
            "    languages: [python]\n"
            "    severity: ERROR\n",
            encoding="utf-8")
        return str(path)

    def seed_git_commit(self, rel_path: str, content: str, message: str) -> str:
        """在 fixture 仓追加一次确定性提交，返回新 HEAD 哈希。

        fixture 只写 harness 自建的 tp11x-ws，与受跟踪源文件零交集。
        """
        target = self.workspace_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        r = self.git("-c", f"user.email={GIT_SEED_EMAIL}",
                     "-c", f"user.name={GIT_SEED_AUTHOR}",
                     "commit", "-q", "-m", message)
        if r.returncode != 0:
            raise RuntimeError(f"git commit 失败：{r.stderr[:200]}")
        return self.local_head()

    # ---------- 作业面（job_executor 初始化 + 提交） ----------

    def job_executor_start(self, db_path: str = "") -> Any:
        """mcp.job_executor.start：建 jobs 表（幂等 DDL）。"""
        db = db_path or str(
            self.run_root / "home" / ".callwarden" / "callwarden.db")
        return self.rpc("mcp.job_executor.start", {"db_path": db})

    def job_submit(self, job_type: str, sync: bool = False, **extra: Any) -> Any:
        """task.job_submit：workspace_instance_id 必填（实测缺 → invalid_params）。"""
        params: Dict[str, Any] = {
            "job_type": job_type,
            "workspace_instance_id": self.setup_workspace()[1],
        }
        if sync:
            params["sync"] = True
        params.update(extra)
        return self.rpc("task.job_submit", params)

    # ---------- Git 读面 ----------

    def git_commits(self, limit: Optional[int] = None,
                    offset: Optional[int] = None,
                    instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self.rpc("query.git_commits", params)

    def git_commit_changes(self, commit_hash: str,
                           instance_id: str = "") -> Any:
        return self.rpc("query.git_commit_changes", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "commit_hash": commit_hash})

    def git_stats(self, instance_id: str = "") -> Any:
        return self.rpc("query.git_stats", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]})

    def file_history(self, file_path: str, instance_id: str = "") -> Any:
        return self.rpc("query.file_history", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "file_path": file_path})

    def commit_tasks(self, commit_hash: str = "",
                     include_task_details: Optional[bool] = None,
                     instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if commit_hash:
            params["commit_hash"] = commit_hash
        if include_task_details is not None:
            params["include_task_details"] = include_task_details
        return self.rpc("query.commit_tasks", params)

    def symbol_commit_history(self, qualified_name: str = "",
                              symbol_hash: str = "",
                              instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if qualified_name:
            params["qualified_name"] = qualified_name
        if symbol_hash:
            params["symbol_hash"] = symbol_hash
        return self.rpc("get_symbol_commit_history", params)

    # ---------- Semgrep 面 ----------

    def semgrep_stats(self, instance_id: str = "") -> Any:
        return self.rpc("query.semgrep_stats", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]})

    def semgrep_findings(self, severity: str = "", limit: Optional[int] = None,
                         instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if severity:
            params["severity"] = severity
        if limit is not None:
            params["limit"] = limit
        return self.rpc("query.semgrep_findings", params)

    def run_semgrep(self, instance_id: str = "", **extra: Any) -> Any:
        """run_semgrep（写面，PROTECTED_MUTATION）：执行 semgrep CLI。"""
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        params.update(extra)
        return self.rpc("run_semgrep", params)

    def run_semgrep_and_save(self, instance_id: str = "",
                             **extra: Any) -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        params.update(extra)
        return self.rpc("run_semgrep_and_save", params)

    def scan_semgrep_incremental(self, instance_id: str = "",
                                 **extra: Any) -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        params.update(extra)
        return self.rpc("scan_semgrep_incremental", params)

    def get_semgrep_summary(self, instance_id: str = "", **extra: Any) -> Any:
        """get_semgrep_summary：⚠️ 只读名字，实际是**即时扫描**（内部复用
        handle_run_semgrep）。默认 config=`p/default` 会去 semgrep registry
        拉规则（联网），故本域一律显式传本地规则路径（离线 + 确定性）。"""
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        params.update(extra)
        return self.rpc("get_semgrep_summary", params)

    # ---------- LSP 面 ----------

    def lsp_check_available(self, language: str = "",
                            instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if language:
            params["language"] = language
        return self.rpc("lsp_check_available", params)

    def lsp_hover(self, file_path: str, line: int, character: int,
                  instance_id: str = "") -> Any:
        return self.rpc("lsp_hover", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "file_path": file_path, "line": line, "character": character})

    def lsp_definition(self, file_path: str, line: int, character: int,
                       instance_id: str = "") -> Any:
        return self.rpc("lsp_definition", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "file_path": file_path, "line": line, "character": character})

    def lsp_references(self, file_path: str, line: int, character: int,
                       instance_id: str = "") -> Any:
        return self.rpc("lsp_references", {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "file_path": file_path, "line": line, "character": character})

    def lsp_diagnostics(self, file_path: str = "",
                        instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1]}
        if file_path:
            params["file_path"] = file_path
        return self.rpc("lsp_diagnostics", params)

    def lsp_completion(self, file_path: str = "", line: int = 1,
                       character: int = 1,
                       instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "line": line, "character": character}
        if file_path:
            params["file_path"] = file_path
        return self.rpc("lsp_completion", params)

    # ---------- 向量 / 嵌入面 ----------

    def semantic_search(self, query: str, top_k: Optional[int] = None,
                        instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "query": query}
        if top_k is not None:
            params["top_k"] = top_k
        return self.rpc("semantic_search", params)

    def find_similar_functions(self, qualified_name: str,
                               threshold: Optional[float] = None,
                               top_k: Optional[int] = None,
                               instance_id: str = "") -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": instance_id or self.setup_workspace()[1],
            "qualified_name": qualified_name}
        if threshold is not None:
            params["threshold"] = threshold
        if top_k is not None:
            params["top_k"] = top_k
        return self.rpc("find_similar_functions", params)

    def semantic_names(self) -> List[str]:
        """已嵌入符号的 qualified_name 列表（find_similar 的合法检索键真相源）。

        🔴 qualified_name 是全路径（本机实测含全小写绝对路径前缀），短名
        查不到；本方法取 semantic_search 的回显作为确定性的合法键来源。
        """
        r = self.semantic_search("a", top_k=100)
        if not r.ok or not isinstance(r.result, list):
            return []
        return [row.get("qualified_name", "") for row in r.result
                if row.get("qualified_name")]
