"""TP-07 最小安全进程/传输底座。

从 TP-06 冻结候选启动一个**隔离** daemon（loopback 动态端口 + run 根内全部状态），
核验 source/hash/PID/manifest 一致性，并在 teardown 时保证进程退出、只清理 run 根。

隔离要点（源码实证，见 BOOTSTRAP_CONTRACT.md §3）：
- `http_manifest_dir()` 读 **USERPROFILE/HOME**（不读 CALLWARDEN_HOME），故必须同时
  覆盖 USERPROFILE，否则 manifest 会写入生产 `~/.callwarden` 并与同 owner 的生产
  daemon manifest 同名相撞（authority_id 相同 → 同文件名），制造 E_HTTP_MANIFEST_STALE。
- `default_callwarden_dir()` 读 CALLWARDEN_HOME → USERPROFILE → HOME；任务库与 registry
  另有 CALLWARDEN_DB / CALLWARDEN_DAEMON_REGISTRY_DB / --registry 显式覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RUN_ROOT = Path(os.environ.get(
    "CW_TP07_RUN_ROOT", r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-07"))
# TP-06 冻结候选散列（tests/acceptance/artifacts/build_results.json）
TP06_DAEMON_SHA256 = "6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d"
MANIFEST_GLOB = "http-daemon.*.manifest.json"


class BootstrapError(RuntimeError):
    """底座不变量违反。"""


@dataclass
class IsolatedDaemon:
    """一个隔离 daemon 生命周期。"""

    run_root: Path
    daemon_exe: Path
    expected_sha256: str = TP06_DAEMON_SHA256
    http_bind: str = "127.0.0.1:0"
    registry_db: Path | None = None
    proc: subprocess.Popen | None = field(default=None, init=False)
    log_lines: list[str] = field(default_factory=list, init=False)
    _manifest: dict | None = field(default=None, init=False)
    _reader: threading.Thread | None = field(default=None, init=False)
    _log_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ---------- 布局 ----------

    @property
    def home(self) -> Path:
        return self.run_root / "home"

    @property
    def callwarden_dir(self) -> Path:
        return self.home / ".callwarden"

    @property
    def reports_dir(self) -> Path:
        return self.run_root / "reports"

    def _resolved_registry(self) -> Path:
        return self.registry_db or (self.callwarden_dir / "daemon" / "registry.db")

    # ---------- 隔离环境 ----------

    def isolated_env(self) -> dict:
        """daemon 进程环境：HOME/USERPROFILE/CALLWARDEN_* 全部指向 run 根。"""
        env = dict(os.environ)
        env["USERPROFILE"] = str(self.home)
        env["HOME"] = str(self.home)
        env["CALLWARDEN_HOME"] = str(self.home)
        env["CALLWARDEN_DB"] = str(self.callwarden_dir / "callwarden.db")
        env["CALLWARDEN_DAEMON_REGISTRY_DB"] = str(self._resolved_registry())
        # 迁移期默认 transport=http；清掉继承来的覆盖，保持本卡显式控制
        for k in ("CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND"):
            env.pop(k, None)
        return env

    # ---------- 启动 / 就绪 ----------

    def start(self, timeout: float = 90.0) -> dict:
        """启动 daemon 并等到 manifest 发布且 PID 自洽。返回 manifest。"""
        if self.proc is not None:
            raise BootstrapError("daemon already started")
        for d in (self.callwarden_dir, self.reports_dir, self._resolved_registry().parent):
            d.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(self.daemon_exe),
            "--http-bind", self.http_bind,
            "--registry", str(self._resolved_registry()),
            "serve",
        ]
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.run_root), env=self.isolated_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        # 读线程持续排空 stdout/stderr：Windows 的 select() 不支持管道（只支持
        # socket），必须用线程，否则管道写满会反压阻塞 daemon。
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._manifest = self._wait_manifest(timeout)
        return self._manifest

    def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._log_lock:
                self.log_lines.append(line)

    def _wait_manifest(self, timeout: float) -> dict:
        """轮询 manifest 目录，取 pid 与本进程一致的那份；超时/进程死即失败。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._join_reader()
                raise BootstrapError(
                    f"daemon exited early rc={self.proc.returncode} before ready manifest; "
                    f"log tail: {''.join(self.log_lines[-12:])!r}")
            for f in sorted(self.callwarden_dir.glob(MANIFEST_GLOB)):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("pid") == self.proc.pid:
                    return data
            time.sleep(0.4)
        self._join_reader()
        raise BootstrapError(
            f"manifest not ready within {timeout}s (pid={self.proc.pid if self.proc else None}); "
            f"log tail: {''.join(self.log_lines[-12:])!r}")

    def _join_reader(self, timeout: float = 2.0) -> None:
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=timeout)

    # ---------- 一致性核验 ----------

    def verify_consistency(self) -> dict:
        """source/hash/PID/manifest/executable/endpoint 一致性核验（TP-07 正向义务）。"""
        if self._manifest is None:
            raise BootstrapError("no manifest; start() first")
        m = self._manifest
        checks: dict[str, bool] = {}

        checks["manifest_version"] = m.get("manifest_version") == "callwarden-http-manifest/v1"
        checks["pid_matches_process"] = m.get("pid") == self.proc.pid

        # hash 一致：manifest 声明的 daemon_binary_sha256 == 冻结候选散列
        checks["binary_sha256_matches_frozen_candidate"] = (
            m.get("daemon_binary_sha256") == self.expected_sha256)

        # 可执行文件路径一致：manifest 声明 == 实际启动的 exe（规范化比较）
        declared = str(Path(str(m.get("daemon_executable", ""))))
        checks["executable_path_matches"] = (
            os.path.normcase(declared) == os.path.normcase(str(self.daemon_exe)))

        # endpoint 必须 loopback
        ep = str(m.get("endpoint", ""))
        checks["endpoint_is_loopback"] = ep.startswith("http://127.0.0.1:") or ep.startswith("http://[::1]:")
        checks["endpoint_not_port_zero"] = not ep.endswith(":0")

        # 进程仍在运行（manifest 非陈旧）
        checks["process_alive"] = self.proc.poll() is None

        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            raise BootstrapError(f"consistency checks failed: {failed}; manifest={m}")
        return {"checks": checks, "manifest": m}

    def probe_http(self, timeout: float = 10.0) -> dict:
        """HTTP 探针：证明 listener 真的在服务（任何 HTTP 状态码都算服务中）。"""
        if self._manifest is None:
            raise BootstrapError("no manifest; start() first")
        ep = self._manifest["endpoint"]
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(ep, timeout=3) as r:
                    return {"url": ep, "status": r.status, "reachable": True}
            except urllib.error.HTTPError as exc:
                # 4xx/5xx 也证明 HTTP server 在应答
                return {"url": ep, "status": exc.code, "reachable": True}
            except OSError as exc:
                last = exc
                time.sleep(0.4)
        return {"url": ep, "status": None, "reachable": False, "error": str(last)}

    # ---------- teardown ----------

    def stop(self, timeout: float = 8.0) -> dict:
        """先 terminate（Windows TerminateProcess），超时则强杀；返回退出信息。"""
        if self.proc is None:
            return {"stopped": False, "reason": "not started"}
        how = "terminate"
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            how = "force_kill"
            self.proc.kill()
            self.proc.wait(timeout=timeout)
        rc = self.proc.returncode
        self._join_reader()
        return {"stopped": True, "how": how, "rc": rc}

    def dump_log(self, path: Path | None = None) -> Path:
        target = path or (self.reports_dir / "daemon.log")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(self.log_lines), encoding="utf-8")
        return target

    def cleanup_run_root(self) -> list[str]:
        """只清理 run 根内、且本 daemon 明确写过的子树（fail-closed：越界拒绝）。"""
        removed: list[str] = []
        root = self.run_root.resolve()
        for sub in (self.callwarden_dir, self.reports_dir):
            p = Path(sub).resolve()
            if root not in p.parents and p != root:
                raise BootstrapError(f"refuse to clean outside run root: {p}")
            if p.exists():
                shutil.rmtree(p, ignore_errors=False)
                removed.append(str(p))
        return removed


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def production_manifest_dir() -> Path:
    """真实（非隔离）HOME 下的 .callwarden——用于生产发现守卫。"""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(home) / ".callwarden"


def snapshot_dir(path: Path) -> dict:
    """目录内 manifest 文件名 + mtime 快照（生产发现守卫用）。"""
    if not path.is_dir():
        return {"exists": False, "files": {}}
    files = {}
    for f in path.glob(MANIFEST_GLOB):
        try:
            files[f.name] = f.stat().st_mtime_ns
        except OSError:
            pass
    return {"exists": True, "files": files}
