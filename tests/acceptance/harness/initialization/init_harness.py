"""TP-09 公共初始化与实体前置 harness。

复用 TP-07 进程底座（IsolatedDaemon），在其上补一层**修正后的隔离面**与
公开初始化流程的执行/断言。

🔴 修正的隔离面（F-DAEMON-01，见 INIT_CONTRACT.md §3）：
TP-07 的 isolated_env() 覆盖了 USERPROFILE/HOME/CALLWARDEN_HOME/CALLWARDEN_DB/
CALLWARDEN_DAEMON_REGISTRY_DB 并传 `--registry`，但 **daemon 的
`TaskCollabStore::list_unified_workspaces()`（task_collab.rs:1776）独立解析
registry：读 env `CW_DAEMON_REGISTRY_DB`，未设则兜底
`default_registry_db_path()` = 硬编码 `/var/lib/callwarden/registry.db`
（config.rs:28-30，Windows 即 C:\\var\\lib\\callwarden = 生产 registry）**。
实测：不设该变量时 `cw workspace list` 返回 **153 条生产 workspace**；设了
返回 run 根内的 1 条标记。故本 harness 在 daemon 启动前注入
`CW_DAEMON_REGISTRY_DB=<run根>/registry.db`。
"""

from __future__ import annotations

import os
import pytest
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
BOOTSTRAP_DIR = REPO_ROOT / "tests" / "acceptance" / "harness" / "bootstrap"
if str(BOOTSTRAP_DIR) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_DIR))

from daemon_bootstrap import (  # noqa: E402
    DEFAULT_RUN_ROOT,
    IsolatedDaemon,
)

CW_PY = REPO_ROOT / "cw.py"
PYTHON = sys.executable


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class InitializationEnv:
    """TP-09 隔离执行环境：隔离 daemon + 空 workspace 目录 + CLI 隔离 env。"""

    def __init__(self, run_root: Path | None = None, workspace_name: str = "tp09-ws"):
        self.run_root = Path(run_root or (DEFAULT_RUN_ROOT.parent / "TP-09"))
        # 显式指定候选（绝对路径）：设了就以它为准（缺失仍硬错）；未设则用
        # run 根内部署的默认候选。
        explicit_src = os.environ.get("CW_TP09_DAEMON_EXE", "")
        self.daemon_exe = (Path(explicit_src) if explicit_src
                           else self.run_root / "src" / "cw-daemon.exe")
        if not self.daemon_exe.is_file():
            # 未显式指定候选且默认候选缺失 → skip（与 TP-11G D2 / TP-07
            # 同族处置）：Linux CI 的全量 pytest job 不构建 daemon 候选，
            # TP-09 用例仅在有候选时真跑（acceptance-gates 构建候选后
            # 运行）；显式指定仍缺失 → 硬错（fail-loud）。
            if not explicit_src:
                pytest.skip(
                    f"daemon 候选缺失且未显式指定 CW_TP09_DAEMON_EXE"
                    f"（{self.daemon_exe}）：本机/Linux CI 无已验收候选，"
                    f"TP-09 用例仅在有候选时真跑；acceptance-gates 或本地"
                    f"经 CW_TP09_DAEMON_EXE 注入候选后运行")
            raise FileNotFoundError(f"daemon candidate missing: {self.daemon_exe}")
        self.workspace_dir = self.run_root / "ws"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.daemon: IsolatedDaemon | None = None
        self.workspace_name = workspace_name
        # TP-09 钉「自己 run 根内部署的候选」的实际散列，而非 TP-06 冻结候选：
        # TP-06/07 的冻结散列用于发布门禁；TP-09 的义务是隔离面与初始化行为，
        # 候选随修复演 进（2026-10-12 F-DAEMON-01 修复后部署修复构建）。
        # manifest 仍由 daemon 自报散列，故此检查依旧证明 manifest 忠实于二进制。
        pinned = os.environ.get("CW_TP09_DAEMON_SHA256", "")
        self.expected_sha256 = pinned or _sha256_of(self.daemon_exe)

    # ---------- daemon 生命周期 ----------

    def start_daemon(self, timeout: float = 90.0) -> dict:
        if self.daemon is not None:
            raise RuntimeError("daemon already started")
        d = IsolatedDaemon(run_root=self.run_root, daemon_exe=self.daemon_exe,
                           expected_sha256=self.expected_sha256)
        # 🔴 对照实验（2026-10-12 F-DAEMON-01 修复后）：
        # 修复前这里必须注入 CW_DAEMON_REGISTRY_DB，否则 daemon 的
        # list_unified_workspaces() 兜底读生产 registry（153 条生产 workspace）。
        # 修复后 daemon 走启动期 --registry 注入（self.registry_db_path），
        # 故 daemon 侧 env 注入已移除以验证修复真实生效。CLI 侧注入保留
        # （cli_env 中 CALLWARDEN_DAEMON_REGISTRY_DB 供客户端路径解析）。
        self.daemon = d
        manifest = d.start(timeout=timeout)
        d.verify_consistency()
        return manifest

    def stop_daemon(self) -> dict:
        if self.daemon is None:
            return {"stopped": False}
        info = self.daemon.stop()
        self.daemon.dump_log()
        self.daemon = None
        return info

    # ---------- CLI 执行 ----------

    def cli_env(self) -> dict:
        """CLI 子进程环境：与 daemon 同源的隔离面 + autostart 钉候选。"""
        env = dict(os.environ)
        home = self.run_root / "home"
        env["USERPROFILE"] = str(home)
        env["HOME"] = str(home)
        env["CALLWARDEN_HOME"] = str(home)
        env["CALLWARDEN_DB"] = str(home / ".callwarden" / "callwarden.db")
        env["CALLWARDEN_DAEMON_REGISTRY_DB"] = str(
            home / ".callwarden" / "daemon" / "registry.db")
        env["CW_DAEMON_REGISTRY_DB"] = env["CALLWARDEN_DAEMON_REGISTRY_DB"]
        env["CW_DAEMON_BIN"] = str(self.daemon_exe)
        for k in ("CW_DAEMON_BINARY", "CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND",
                  "CW_DAEMON_HTTP_ENDPOINT"):
            env.pop(k, None)
        env["CALLWARDEN_LANG"] = "en_US"
        # 跳过 lazy auto-setup（避免往 run 根 home 写 13 个 MCP 配置）
        env["CALLWARDEN_AUTO_SETUP"] = "0"
        return env

    def run_cli(self, args: list[str], timeout: float = 150.0,
                cwd: Path | None = None) -> dict:
        r = subprocess.run([PYTHON, str(CW_PY)] + args,
                           cwd=str(cwd or self.workspace_dir), env=self.cli_env(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return {"rc": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}

    # ---------- 状态查询（只读，用于断言） ----------

    def registry_workspace_count(self) -> int:
        import sqlite3
        reg = self.run_root / "home" / ".callwarden" / "daemon" / "registry.db"
        if not reg.is_file():
            return -1
        conn = sqlite3.connect(f"file:{reg}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM daemon_workspaces").fetchone()[0]
        except Exception:
            return -1
        finally:
            conn.close()

    def task_db_tables(self) -> list[str]:
        import sqlite3
        db = self.run_root / "home" / ".callwarden" / "callwarden.db"
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        except Exception:
            return []
        finally:
            conn.close()
