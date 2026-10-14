"""TP-11P 性能域 harness（tests/acceptance/domains/performance/）。

在完全隔离的 daemon（独立 run 根）上做**性能测量 + 结构不变量**验收：

- 合成语料（确定性、run 根内、禁指受跟踪源文件）：多语言、跨文件调用边，
  规模足以让「全量 vs 增量」的结构性差异可观测（默认 200+ 文件）；
- 冷/温全量构建、单文件增量刷新、廉价 RPC 延迟分布、daemon RSS——
  全部客户端侧 `perf_counter` 计时（daemon 无服务端计时面），RSS 走 WMI
  `Win32_Process.WorkingSetSize`（按 manifest/Popen 自洽 PID）；
- 断言只锁**结构不变量**（测量完备性 / 增量显著低于全量 / RSS 天花板 /
  延迟分布合理 / 重复性 sanity），**不锁任何调优 SLO**。

🔴 SLO 纪律（TESTING_PLAN §8 门禁）：「冻结硬件/corpus/profile 预算通过；
未校准不宣称达到 SLO」。本机硬件/corpus 未冻结，故本域**不宣称达到任何
延迟/吞吐/RSS SLO**；测量值落 `reports/tp11p_baseline.json` 供后续按固定
硬件/corpus 冻结预算后转为正式断言。所有门限均为宽裕的护栏级（smoke）
阈值，用于抓病态回归（如快路径失效、内存失控），不是性能目标。
"""

from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_ISO_DIR = _HERE.parent / "isolation"
_GOV_DIR = _HERE.parent / "governance"
for _d in (str(_HERE), str(_ISO_DIR), str(_GOV_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from isolation_harness import IsolationEnv  # noqa: E402

# TP-11P 独立 run 根
DEFAULT_RUN_ROOT_PERF = Path(
    os.environ.get("CW_TP11P_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11P"))

# 语料默认规模（结构不变量所需下限：full_build 必须显著贵于单文件 refresh）
DEFAULT_PY_FILES = 200
DEFAULT_PY_PACKAGES = 8
EXTRA_RS_FILES = 10
EXTRA_JS_FILES = 10


def _py_module(seed: int, pkg: str, mod: str, n_funcs: int = 6) -> str:
    """确定性生成一个 Python 模块：函数 + 跨模块调用边。"""
    rng = random.Random(seed)
    lines = [f'"""合成语料 {pkg}/{mod}（TP-11P，确定性生成）。"""']
    names = [f"fn_{pkg}_{mod}_{i}" for i in range(n_funcs)]
    for i, name in enumerate(names):
        callee = names[(i + 1) % n_funcs]
        lines.append(f"def {name}(x):")
        lines.append(f"    y = x * {rng.randint(1, 9)}")
        if rng.random() < 0.7:
            lines.append(f"    y = {callee}(y + {rng.randint(1, 5)})")
        lines.append("    return y")
        lines.append("")
    return "\n".join(lines) + "\n"


def _rs_module(seed: int, mod: str) -> str:
    rng = random.Random(seed)
    return (
        f"// 合成语料 rs/{mod}（TP-11P，确定性生成）\n"
        f"pub fn rs_fn_{mod}(x: i64) -> i64 {{\n"
        f"    let y = x * {rng.randint(1, 9)};\n"
        f"    y + {rng.randint(1, 5)}\n"
        f"}}\n"
    )


def _js_module(seed: int, mod: str) -> str:
    rng = random.Random(seed)
    return (
        f"// 合成语料 js/{mod}（TP-11P，确定性生成）\n"
        f"function js_fn_{mod}(x) {{\n"
        f"    return x * {rng.randint(1, 9)} + {rng.randint(1, 5)};\n"
        f"}}\n"
        f"module.exports = {{ js_fn_{mod} }};\n"
    )


class PerfEnv(IsolationEnv):
    """TP-11P 性能环境：隔离底座 + 合成语料 + 计时/RSS 测量。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11p-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_PERF,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)
        self.metrics: Dict[str, Any] = {}

    # ---------- 合成语料 ----------

    def generate_corpus(self, n_py: int = DEFAULT_PY_FILES,
                        n_pkg: int = DEFAULT_PY_PACKAGES) -> List[Path]:
        """在 ws/src 下生成确定性多语言语料，返回全部文件路径。"""
        src = self.workspace_dir / "src"
        src.mkdir(parents=True, exist_ok=True)
        files: List[Path] = []
        per_pkg = max(1, n_py // max(1, n_pkg))
        for i in range(n_py):
            pkg = f"pkg{i % n_pkg:02d}"
            pkg_dir = src / pkg
            pkg_dir.mkdir(parents=True, exist_ok=True)
            mod = f"m{i:04d}"
            p = pkg_dir / f"{mod}.py"
            p.write_text(_py_module(1000 + i, pkg, mod), encoding="utf-8")
            files.append(p)
        rs_dir = src / "rs"
        js_dir = src / "js"
        rs_dir.mkdir(parents=True, exist_ok=True)
        js_dir.mkdir(parents=True, exist_ok=True)
        for i in range(EXTRA_RS_FILES):
            p = rs_dir / f"r{i:03d}.rs"
            p.write_text(_rs_module(2000 + i, f"{i:03d}"), encoding="utf-8")
            files.append(p)
        for i in range(EXTRA_JS_FILES):
            p = js_dir / f"j{i:03d}.js"
            p.write_text(_js_module(3000 + i, f"{i:03d}"), encoding="utf-8")
            files.append(p)
        return files

    # ---------- 计时 ----------

    def timed_rpc(self, method: str, params: Optional[Dict[str, Any]] = None
                  ) -> Tuple[Any, float]:
        """RPC + 客户端侧 wall time（秒）。daemon 无服务端计时面。"""
        t0 = time.perf_counter()
        r = self.rpc(method, params or {})
        return r, time.perf_counter() - t0

    def direct_build_graph(self) -> Tuple[Any, float]:
        """直连 RPC 全量建图（无 CLI 子进程开销，计时干净）。"""
        return self.timed_rpc("workspace.build_graph", {
            "workspace_instance_id": self.workspace_instance_id})

    # ---------- RSS（WMI WorkingSetSize，按 PID） ----------

    @property
    def daemon_pid(self) -> Optional[int]:
        return getattr(self.daemon._inner, "proc", None).pid \
            if self.daemon is not None else None

    def daemon_rss_kb(self) -> Optional[int]:
        """当前 daemon 工作集（KB，tasklist CSV 第 5 列）；查询失败返回 None
        （不伪造）。本机无 wmic（新版 Windows 已移除），故用 tasklist：
        CSV 形如 `"cw-daemon.exe","<pid>","Console","1","25,856 K"`。"""
        pid = self.daemon_pid
        if pid is None:
            return None
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=20).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith('"'):
                    continue  # 无匹配时 tasklist 输出本地化 INFO 行
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 5 and parts[1] == str(pid):
                    digits = "".join(ch for ch in parts[4] if ch.isdigit())
                    return int(digits) if digits else None
        except Exception:
            return None
        return None

    # ---------- 延迟分布 ----------

    def latency_samples(self, method: str, params_fn, n: int = 30
                        ) -> List[float]:
        """对廉价方法连发 n 次，返回每次秒数列表。"""
        samples: List[float] = []
        for _ in range(n):
            _, dt = self.timed_rpc(method, params_fn())
            samples.append(dt)
        return samples

    # ---------- 证据 ----------

    def write_baseline(self, name: str = "tp11p_baseline.json") -> Path:
        p = self.reports_dir / name
        p.write_text(json.dumps(self.metrics, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p


def pctl(samples: List[float], q: float) -> float:
    """分位数（线性插值；样本极少时退化为 max）。"""
    if not samples:
        return 0.0
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def summarize(samples: List[float]) -> Dict[str, float]:
    return {
        "n": len(samples),
        "median": statistics.median(samples) if samples else 0.0,
        "p95": pctl(samples, 0.95),
        "max": max(samples) if samples else 0.0,
        "min": min(samples) if samples else 0.0,
    }
