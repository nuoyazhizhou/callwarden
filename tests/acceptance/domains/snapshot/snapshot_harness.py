"""TP-11Q 快照门 vs HEAD 回归 harness：复用 TP-11 底座 + 快照面助手。

在完全隔离的 daemon 上验证 snapshot 发布/门控/过期语义（快照门 vs HEAD）：

- Q1 门控前置：daemon 侧「直连 RPC build_graph 不自动发布」——未 publish 时
  所有 query.* 返回 snapshot_not_ready（per-instance 门控，不影响其他已发布
  workspace）；
- Q2 CLI refresh --all 隐式发布：refresh 链路末尾的 rule.sync_agents_md 经
  route_rpc（rule.* 前缀命中 needs_snapshot → _ensure_remote_snapshot）触发
  snapshot.publish —— 这是「用户日常 refresh 后不会撞门控」的真实原因；
- Q3 HEAD 推进 + 仅 daemon 侧重建（不 publish）后的**沉默过期 + 读面分裂**：
  内存读面（query.search / query.stats / snapshot.stats）沉默服务旧
  generation（无错误、无 staleness 信号），而 query-DB 读面（query.symbol
  全限定名）直读实时主库、能查到新符号；
- Q4 显式 snapshot.publish 恢复：generation 递增、内存读面反映新数据，
  且 publish 响应回显 registry 权威 snapshot_id（HEAD 移动后已重算）；
- Q5 daemon 重启门控复位：快照缓存纯内存，重启后 query.* 回到
  snapshot_not_ready、snapshot.list_workspaces 为空，再次 publish 后
  generation 从 1 重新计数（per-process 计数器，非持久化）；
- Q6 snapshot_id 权威绑定负向：publish 显式传与 registry 当前权威值不一致
  的 snapshot_id → invalid_params。

🔴 实测契约（源码实证 + 探针，禁猜）：
- daemon 未配 codegraph_db_path_template → codegraph 与任务库合一
  （one-sqlite-per-host），snapshot.publish 的 db_path 须传**绝对路径**
  （不支持 ~/ 展开，validate_owned_path owner ACL）；
- 🔴 publish 副作用归因（二分探针实证）：**直连 RPC workspace.build_graph
  不发布快照**；而 CLI `refresh --all` 末尾的 `rule_sync_agents_md`（route_rpc
  'rule.sync_agents_md'）因 rule.* 前缀命中 needs_snapshot →
  _ensure_remote_snapshot(db_path) → 隐式 snapshot.publish。故「用户日常
  refresh 后不撞门控」由 CLI 层补偿，daemon 侧本身无自动发布；
- HEAD 变化 → 同 instance_id、新 snapshot_id（workspace.rs:4076 测试契约）；
  publish 时 snapshot_id 与 registry 权威值不一致 → invalid_params；
- Windows 下 GraphSnapshot::query_db_path() == source_db_path（/proc/self/fd
  分支仅 Linux），query-DB 读面直读**实时主库**只读连接（query.symbol 全
  限定名精确匹配 fsv.qualified_name，如 src/seed.seed_fn）；
- query.search/callers/callees/stats 读发布时冻结的内存 GraphStore（沉默
  过期面），query.symbol 及 open_query_connection 系方法读 query_db_path
  实时库（新鲜面）——两条读面在「重建未重发 publish」时分裂。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
_ISO_DIR = _HERE.parent / "isolation"
for _d in (str(_HERE), str(_GOV_DIR), str(_ISO_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from isolation_harness import IsolationEnv  # noqa: E402

# TP-11Q 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_SNAP = Path(
    os.environ.get("CW_TP11Q_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11Q"))


class SnapshotEnv(IsolationEnv):
    """TP-11Q 快照门执行环境：TP-11 底座 + 快照/查询面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11q-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_SNAP,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    def _ws(self) -> str:
        return self.setup_workspace()[1]

    # ---------- fixture：工作区内文件写入 ----------

    def write_ws_file(self, rel_path: str, content: str) -> Path:
        """在隔离 workspace 内写文件（返回绝对路径）。"""
        p = self.workspace_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    # ---------- 权威库路径（one-sqlite-per-host） ----------

    def db_path(self) -> str:
        """snapshot.publish 的 db_path：run 根权威库绝对路径。

        daemon 未配 codegraph_db_path_template，codegraph 与任务库合一
        （config.rs:413 主机级单库）；publish 要求绝对路径 + owner ACL。
        """
        return str(self.run_root / "home" / ".callwarden" / "callwarden.db")

    # ---------- 快照面 ----------

    def publish(self, db_path: Optional[str] = None, **extra: Any):
        params: Dict[str, Any] = {
            "workspace_instance_id": self._ws(),
            "db_path": db_path or self.db_path(),
        }
        params.update(extra)
        return self.rpc("snapshot.publish", params)

    def snapshot_stats(self, instance_id: str = ""):
        return self.rpc("snapshot.stats", {
            "workspace_instance_id": instance_id or self._ws()})

    def snapshot_list_workspaces(self):
        return self.rpc("snapshot.list_workspaces", {})

    # ---------- 查询面 ----------

    def query_search(self, query: str, **extra: Any):
        params: Dict[str, Any] = {
            "workspace_instance_id": self._ws(), "query": query}
        params.update(extra)
        return self.rpc("query.search", params)

    def query_symbol(self, qualified_name: str):
        return self.rpc("query.symbol", {
            "workspace_instance_id": self._ws(),
            "qualified_name": qualified_name})

    def query_stats(self):
        return self.rpc("query.stats", {
            "workspace_instance_id": self._ws()})

    # ---------- git HEAD 推进（Q3 场景） ----------

    def git_head(self) -> str:
        r = self._git("rev-parse", "HEAD")
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f"rev-parse HEAD 失败：{r.stderr[:200]}")
        return r.stdout.strip()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + list(args), cwd=str(self.workspace_dir),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)

    def git_commit_all(self, msg: str) -> str:
        """把工作区改动提交为新 commit（返回新 HEAD sha）。"""
        self._git("add", "-A")
        r = self._git("-c", "user.email=tp11q@example.invalid",
                      "-c", "user.name=tp11q", "commit", "-q", "-m", msg)
        if r.returncode != 0:
            raise RuntimeError(f"git commit 失败：{r.stdout[:200]}{r.stderr[:200]}")
        return self.git_head()

    def registry_snapshot_id(self, instance_id: str = "") -> Optional[str]:
        """只读查 registry 行当前权威 snapshot_id（随 HEAD 重算）。"""
        row = self.registry_row(instance_id or self._ws())
        if not row:
            return None
        v = row.get("snapshot_id")
        return str(v) if v else None

    def git_remote(self) -> str:
        """本地 file:// remote（与 _ensure_git_workspace 同源，不联网）。"""
        return f"file://{self.workspace_dir.as_posix()}"

    def register_with_head(self, head_sha: str, instance_id: str = ""):
        """以指定 HEAD 显式 re-register（不改 active、不经 CLI refresh）。

        用于 Q3「HEAD 推进」场景：register 以新 head_sha 重新注册同一 root，
        契约为同 instance_id、重算 snapshot_id（workspace.rs:4076 测试）。
        **严禁**在此前后调 CLI refresh —— refresh 链路会隐式 publish，破坏
        沉默过期观测。
        """
        params: Dict[str, Any] = {
            "client_view_root": str(self.workspace_dir),
            "git_remote_url": self.git_remote(),
            "git_head_commit_sha": head_sha,
        }
        r = self.rpc("workspace.register", params)
        if r.ok and instance_id:
            got = str((r.result or {}).get("workspace_instance_id", ""))
            if got and got != instance_id:
                raise RuntimeError(
                    f"re-register 产生了不同 instance（{instance_id} → {got}）；"
                    "同一 root 的 HEAD 变化应保持同一 instance")
        return r

    # ---------- 建图（复用 CLI refresh --all；注意其隐式 publish 副作用） ----------

    def cli_refresh_all(self) -> None:
        """CLI refresh --all：建图 + 隐式发布快照（rule.sync_agents_md 链路）。

        与基类 build_graph 同义（重命名为语义自明）：CLI refresh 末尾的
        rule_sync_agents_md 经 route_rpc 的 rule.* needs_snapshot 分支触发
        _ensure_remote_snapshot → snapshot.publish。**该调用本身就发布快照**，
        观测「沉默过期」时不得使用本方法（改用直连 RPC build_graph）。
        """
        self.build_graph()
