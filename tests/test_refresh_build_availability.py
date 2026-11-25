"""T-1795491554078-3616ae10：大仓 refresh 期间 daemon 探活可用性（注入式）。

任务验收项：大仓 refresh（>2000 文件）期间 daemon /health 与 /v1/rpc 连续
探测不超时。历史实证（2026-11-24 hermes-agent 2071 文件）：修复前
handle_refresh_file 在 dispatch 线程持 daemon state 锁内做全仓
resolve_raw_calls（O(全仓 raw 名 × 全仓边)），单文件刷新即可阻塞全部
RPC 探活约 15 分钟（/health 与 /v1/rpc TLS 握手超时）。

注入式种子（幂等，不依赖外部大仓）：
- 250 个种子文件 × 400 符号 = 10 万 distinct 名字，每名 1 条 raw 边
  （callee_id=0、唯一候选符号）。修复前 refresh_file 的全仓 resolve 逐名
  候选查询 + UPDATE（每名一次 SQL prepare+execute），持 state 锁数十秒
  → /v1/rpc 探测超时（回归信号）；修复后单文件刷新只重算本文件受影响
  名字集合（~51 名），毫秒级返回。
- 12 个真实磁盘目标文件各 50 个函数调用 seeded_target_fn（唯一定义），
  两轮刷新断言 ok 且跨文件 raw 边被解析（calls_resolved>=50）。

探测：后台线程每 250ms 同时探 /health（verify_health）与 /v1/rpc
（task.list，dispatch 需 state 锁），覆盖整个 refresh 序列，断言零失败。
"""

import hashlib
import os
import sqlite3
import threading
import time

from callwarden.server.daemon_client import HttpDaemonRpcClient

# 强制使用本 worktree 构建的 daemon 二进制（含本任务修复）。find_daemon_binary
# 的相对路径候选按 CWD 解析，从 pkgroot（junction）跑 pytest 时全部失效；且本机
# 用户级环境变量 CW_DAEMON_BIN 常指向陈旧 runtime 构建——setdefault 会被其抢占，
# 必须显式覆盖，否则测试静默跑在旧二进制上、修复形同虚设。模块导入期设置（先于
# fixture 实例化）。realpath 解析 junction：manifest 的 PID 校验按字符串比较可执
# 行路径，junction 形式与进程解析出的真实路径不一致会被误判 E_HTTP_MANIFEST_STALE。
_WORKTREE_BIN = os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, "rust_ext",
    "target", "release", "cw-daemon.exe"))
os.environ["CW_DAEMON_BIN"] = _WORKTREE_BIN

# 种子规模：10 万 distinct 名字（修复前全仓 resolve 的线性成本来源）。
_SEED_FILES = 250
_SYMS_PER_FILE = 400

# 目标文件数 × 每文件函数数；两轮刷新（首轮建边、次轮覆盖「旧符号已存在」路径）。
_TARGET_FILES = 12
_FNS_PER_TARGET = 50

# 探测节奏与超时（对齐 watchdog 口径：探活超时必须显著小于阻塞窗口才有区分度）。
_PROBE_INTERVAL_SECS = 0.25
_PROBE_TIMEOUT_SECS = 5.0
_REFRESH_PER_FILE_BOUND_SECS = 5.0


def _seed_heavy_repo(cg_db, root, ws_id):
    """向 codegraph 主库注入大仓种子（幂等：按 seedheavy/ 前缀判重）。

    - _SEED_FILES 个种子文件 × _SYMS_PER_FILE 个符号，名字互不相同；
    - 每个名字恰好 1 条 raw 边（callee_id=0）且恰好 1 个候选符号 →
      修复前全仓 resolve 走「唯一候选」路径逐名 UPDATE（最重分支）；
    - 另设 1 个定义文件提供 seeded_target_fn（目标文件 raw 边的解析锚点）。
    """
    conn = sqlite3.connect(cg_db, timeout=60)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM file_instances WHERE rel_path LIKE 'seedheavy/%'"
        ).fetchone()
        if row[0] >= _SEED_FILES + 1:
            return  # 幂等：已注入（同模块内重复调用直接跳过）
        now = time.time()
        next_file = conn.execute(
            "SELECT COALESCE(MAX(id),0)+1 FROM file_instances").fetchone()[0]
        next_sym = conn.execute(
            "SELECT COALESCE(MAX(id),0)+1 FROM symbols").fetchone()[0]

        file_rows = []
        sym_rows = []
        call_rows = []
        fid = next_file
        sid = next_sym
        for j in range(_SEED_FILES):
            rel = f"seedheavy/mod_{j:03d}.py"
            file_rows.append((fid, ws_id, rel, os.path.join(root, rel), now, "tracked"))
            for k in range(_SYMS_PER_FILE):
                name = f"heavy_fn_{sid:06d}"
                sym_rows.append((sid, fid, hashlib.sha256(name.encode()).hexdigest(),
                                 name, "fn", "public", k * 4 + 1, k * 4 + 3, name))
                # 唯一候选 + 唯一 raw 边：全仓 resolve 对每个名字都要走一次
                # 候选查询 + UPDATE（prepare+execute）。
                call_rows.append((sid, name, "seedheavy", name, "", 0, k * 4 + 2, 0))
                sid += 1
            fid += 1
        # 定义文件：seeded_target_fn（无 raw 边指向它，仅供目标文件解析）。
        rel = "seedheavy/defs.py"
        file_rows.append((fid, ws_id, rel, os.path.join(root, rel), now, "tracked"))
        sym_rows.append((sid, fid, hashlib.sha256(b"seeded_target_fn").hexdigest(),
                         "seeded_target_fn", "fn", "public", 1, 3, "seeded_target_fn"))

        conn.executemany(
            "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, mtime, status)"
            " VALUES (?, ?, ?, ?, ?, ?)", file_rows)
        conn.executemany(
            "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,"
            " start_line, end_line, qualified_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sym_rows)
        conn.executemany(
            "INSERT INTO calls (caller_id, caller_name, caller_module, callee_name,"
            " callee_module, callee_id, call_line, is_cross_file)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", call_rows)
        conn.commit()
    finally:
        conn.close()


def _daemon_ws_id(cg_db, root):
    """读 daemon 在 codegraph 库自建的 workspace 行 id（按规范化 root 匹配）。

    种子行（build_empty_codegraph）的 root_path 是 raw 混合分隔符路径；
    ensure_workspace_row_by_root 按规范化形式（canonicalize → \\ → / →
    盘符小写）精确匹配不中会另建一行。种子必须挂到 daemon 的行，否则解析
    候选查询按 workspace 过滤后 0 候选 → calls_resolved 恒 0。
    """
    norm = os.path.realpath(root).replace("\\", "/")
    if norm.startswith("//?/"):
        norm = norm[4:]
    if len(norm) >= 2 and norm[1] == ":":
        norm = norm[0].lower() + norm[1:]
    conn = sqlite3.connect(cg_db, timeout=60)
    try:
        rows = conn.execute(
            "SELECT id, root_path FROM workspaces ORDER BY id").fetchall()
    finally:
        conn.close()
    # 精确匹配 daemon 的规范化形式（种子 raw 行分隔符/盘符大小写不同，不会误配）。
    for wid, rpath in rows:
        if rpath == norm:
            return wid
    # 兜底：probe refresh 刚发生，daemon 自建行必为最新行。
    return max(wid for wid, _ in rows)


def _target_source():
    lines = []
    for i in range(_FNS_PER_TARGET):
        lines.append(f"def target_fn_{i:03d}():")
        lines.append("    seeded_target_fn()")
        lines.append("")
    return "\n".join(lines)


def _probe_loop(stop_evt, endpoint, stats):
    """并发探测线程：/health 与 /v1/rpc 各一套短连接客户端。

    task.list 是 READ_ONLY RPC，dispatch 需拿 daemon state 锁——refresh
    持锁期间该探测必然排队，超时即视为可用性破坏。
    """
    rpc_client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                                     validate_manifest=False,
                                     timeout=_PROBE_TIMEOUT_SECS)
    while not stop_evt.is_set():
        try:
            rpc_client.verify_health(endpoint=endpoint)
            stats["health_ok"] += 1
        except Exception as exc:  # noqa: BLE001 探测失败即记录（含超时/握手失败）
            stats["health_fail"].append(f"{time.time():.3f} {exc}")
        try:
            rpc_client.call("task.list", {"status": "", "limit": 10, "workspace_id": 1})
            stats["rpc_ok"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["rpc_fail"].append(f"{time.time():.3f} {exc}")
        stop_evt.wait(_PROBE_INTERVAL_SECS)


def test_refresh_large_repo_probes_stay_alive(w3_live):
    """大仓 refresh 序列期间 /health 与 /v1/rpc 连续探测零超时。"""
    client = w3_live["client"]
    inst = w3_live["inst"]
    endpoint = w3_live["endpoint"]
    root = w3_live["data_root"] + "/repo"
    cg_db = os.path.join(w3_live["data_root"], "userhome", ".callwarden",
                         "callwarden.db")

    # 目标文件落真实磁盘（pytest 进程创建 → daemon 侧 ownership 检查可通过）。
    pkg = os.path.join(root, "pkg")
    os.makedirs(pkg, exist_ok=True)
    rel_files = []
    for i in range(_TARGET_FILES):
        rel = f"pkg/target_{i:02d}.py"
        with open(os.path.join(root, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(_target_source())
        rel_files.append(rel)

    # probe refresh：先让 daemon 的 ensure_workspace_row_by_root 自建 ws 行
    # （种子行 root_path 为 raw 路径，规范化精确匹配不中），再以该行 id 种子。
    probe = client.call("workspace.file.refresh_file", {
        "workspace_instance_id": inst,
        "file_path": rel_files[0],
    })
    assert probe.get("ok") is True, f"probe refresh 失败: {probe!r}"
    _seed_heavy_repo(cg_db, root, _daemon_ws_id(cg_db, root))

    stats = {"health_ok": 0, "rpc_ok": 0, "health_fail": [], "rpc_fail": []}
    stop_evt = threading.Event()
    prober = threading.Thread(target=_probe_loop, args=(stop_evt, endpoint, stats),
                              daemon=True)
    prober.start()
    try:
        # 两轮刷新：覆盖「文件首次入库」与「旧符号已存在（删旧插新降级再重挂）」。
        for _pass in (1, 2):
            for rel in rel_files:
                t0 = time.time()
                r = client.call("workspace.file.refresh_file", {
                    "workspace_instance_id": inst,
                    "file_path": rel,
                })
                elapsed = time.time() - t0
                assert r.get("ok") is True, f"refresh {rel} 未成功: {r!r}"
                assert r.get("calls_resolved", 0) >= _FNS_PER_TARGET, (
                    f"refresh {rel} 跨文件 raw 边未解析: {r!r}")
                assert elapsed < _REFRESH_PER_FILE_BOUND_SECS, (
                    f"refresh {rel} 耗时 {elapsed:.2f}s 超界（增量重算失效？）")
    finally:
        stop_evt.set()
        prober.join(timeout=15)

    # 探测零失败（/health 与 /v1/rpc 各自独立计数）。
    assert not stats["health_fail"], (
        f"/health 探测失败 {len(stats['health_fail'])} 次: "
        f"{stats['health_fail'][:3]}")
    assert not stats["rpc_fail"], (
        f"/v1/rpc 探测失败 {len(stats['rpc_fail'])} 次: {stats['rpc_fail'][:3]}")
    # 探测确有发生（防止探测线程静默死亡造成假绿）。
    assert stats["health_ok"] >= 4, f"/health 探测次数过少: {stats}"
    assert stats["rpc_ok"] >= 4, f"/v1/rpc 探测次数过少: {stats}"
