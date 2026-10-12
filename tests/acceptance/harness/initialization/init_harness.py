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

import json
import os
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
    TP06_DAEMON_SHA256,
)

CW_PY = REPO_ROOT / "cw.py"
PYTHON = sys.executable


class InitializationEnv:
    """TP-09 隔离执行环境：隔离 daemon + 空 workspace 目录 + CLI 隔离 env。"""

    def __init__(self, run_root: Path | None = None, workspace_name: str = "tp09-ws"):
        self.run_root = Path(run_root or (DEFAULT_RUN_ROOT.parent / "TP-09"))
        self.daemon_exe = self.run_root / "src" / "cw-daemon.exe"
        if not self.daemon_exe.is_file():
            raise FileNotFoundError(f"daemon candidate missing: {self.daemon_exe}")
        self.workspace_dir = self.run_root / "ws"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.daemon: IsolatedDaemon | None = None
        self.workspace_name = workspace_name

    # ---------- daemon 生命周期 ----------

    def start_daemon(self, timeout: float = 90.0) -> dict:
        if self.daemon is not None:
            raise RuntimeError("daemon already started")
        d = IsolatedDaemon(run_root=self.run_root, daemon_exe=self.daemon_exe)
        # F-DAEMON-01：daemon 进程内注入 Rust 侧真正读取的 registry 覆盖
        os.environ["CW_DAEMON_REGISTRY_DB"] = str(d._resolved_registry())
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
