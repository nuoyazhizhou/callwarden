"""perf: Python daemon vs Rust cw_daemon binary 延迟基线对比。

对比项：
- ping: 基础 RPC 协议往返延迟（不含业务逻辑）
- health: 查询 workspace 数量（含 1 次 SQL COUNT）
- schema.version: 查询 schema_meta 表（含 1 次 SQL SELECT）
- workspace.list: 列出当前 UID 的 workspace（含 1 次 SQL SELECT）
- query.stats: 查询快照 stats（无 snapshot 时返回错误，对比错误路径延迟）

每个方法调用 N 次，统计：
- 中位数（P50）
- P95
- P99
- 平均值
- 最小/最大

输出：表格对比 + JSON 详细数据。

用法（在 WSL/Linux 中）：
    python3 perf_daemon_baseline.py --target rust --socket /tmp/rust.sock --iterations 200
    python3 perf_daemon_baseline.py --target python --socket /tmp/py.sock --iterations 200
    python3 perf_daemon_baseline.py --compare --rust-socket /tmp/rust.sock --python-socket /tmp/py.sock

依赖：仅标准库（不导入 callwarden 包，避免依赖问题）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from statistics import mean, median
from typing import Dict, List, Tuple


HEADER = struct.Struct("!I")
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


# ============================================
# UDS JSON-RPC 客户端（最小实现，不依赖 callwarden 包）
# ============================================


def send_message(sock: socket.socket, message: Dict) -> None:
    """发送长度分帧 JSON 请求。"""
    payload = json.dumps(message).encode("utf-8")
    if len(payload) > DEFAULT_MAX_MESSAGE_BYTES:
        raise ValueError(f"消息过大: {len(payload)} > {DEFAULT_MAX_MESSAGE_BYTES}")
    sock.sendall(HEADER.pack(len(payload)))
    sock.sendall(payload)


def recv_message(sock: socket.socket) -> Dict:
    """接收长度分帧 JSON 响应。"""
    header = _recv_exact(sock, HEADER.size)
    size = HEADER.unpack(header)[0]
    if size > DEFAULT_MAX_MESSAGE_BYTES:
        raise ValueError(f"响应过大: {size} > {DEFAULT_MAX_MESSAGE_BYTES}")
    payload = _recv_exact(sock, size)
    return json.loads(payload.decode("utf-8"))


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """精确读取 size 字节。"""
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("连接断开")
        buf.extend(chunk)
    return bytes(buf)


def rpc_call(sock_path: str, method: str, params: Dict, timeout: float = 30.0) -> Tuple[Dict, float]:
    """发起一次 RPC 调用，返回 (response, latency_seconds)。

    每次调用新建一个 UDS 连接（与 daemon 的"每连接一请求"模型一致）。
    """
    start = time.perf_counter()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        send_message(sock, request)
        response = recv_message(sock)
        latency = time.perf_counter() - start
        return response, latency
    finally:
        sock.close()


# ============================================
# 基准测试套件
# ============================================


def benchmark_method(
    sock_path: str,
    method: str,
    params: Dict,
    iterations: int,
    warmup: int = 10,
) -> Dict:
    """对单个方法执行 N 次调用，返回统计结果。"""
    # 预热：让 daemon 的 worker 线程池进入热状态
    for _ in range(warmup):
        try:
            rpc_call(sock_path, method, params)
        except Exception as e:
            print(f"  [WARMUP] {method} 调用失败: {e}", file=sys.stderr)
            return {
                "method": method,
                "error": str(e),
                "iterations": 0,
            }

    # 正式测量
    latencies: List[float] = []
    errors = 0
    for i in range(iterations):
        try:
            _, latency = rpc_call(sock_path, method, params)
            latencies.append(latency * 1_000_000)  # 转换为微秒
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  [ITER {i}] {method} 失败: {e}", file=sys.stderr)

    if not latencies:
        return {
            "method": method,
            "error": "all calls failed",
            "errors": errors,
            "iterations": iterations,
        }

    latencies_sorted = sorted(latencies)
    n = len(latencies_sorted)

    def percentile(p: float) -> float:
        idx = max(0, min(n - 1, int(n * p / 100)))
        return latencies_sorted[idx]

    return {
        "method": method,
        "iterations": n,
        "errors": errors,
        "min_us": latencies_sorted[0],
        "max_us": latencies_sorted[-1],
        "mean_us": mean(latencies),
        "p50_us": median(latencies),
        "p95_us": percentile(95),
        "p99_us": percentile(99),
    }


def benchmark_suite(sock_path: str, iterations: int) -> List[Dict]:
    """对一组方法执行基准测试。"""
    methods = [
        ("ping", {}),
        ("health", {}),
        ("schema.version", {}),
        ("workspace.list", {}),
        # query.stats 在无 snapshot 时会返回错误，对比错误路径延迟
        ("query.stats", {"workspace_instance_id": "1"}),
    ]
    results = []
    for method, params in methods:
        print(f"  benchmarking {method}...", end=" ", flush=True)
        result = benchmark_method(sock_path, method, params, iterations)
        if "error" in result and "iterations" in result and result["iterations"] == 0:
            print(f"FAILED: {result['error']}")
        else:
            print(
                f"p50={result['p50_us']:.1f}us p95={result['p95_us']:.1f}us "
                f"p99={result['p99_us']:.1f}us (n={result['iterations']}, err={result['errors']})"
            )
        results.append(result)
    return results


# ============================================
# 启动 / 停止 daemon（Python 版，需 callwarden 包）
# ============================================


def start_python_daemon(sock_path: str, registry_db: str) -> Tuple:
    """启动 Python daemon，返回 (server_thread, stop_event)。

    绕过 callwarden/__init__.py 的重量级 parser 依赖加载：
    用 sys.modules 注入 mock 模块替代 tree_sitter_* grammar 包，
    因为 perf 测试只关心 RPC 层延迟，不需要实际 parse 功能。
    """
    import threading
    import types

    # 注入 mock 模块，替代 tree_sitter_* grammar 包
    # daemon_server.py 间接导入 callwarden.parsers → 各 parser 文件 → tree_sitter_*
    # 这些 grammar 包在 WSL 环境未安装，但 daemon RPC 层不需要 parse 功能
    _MOCK_GRAMMARS = [
        "tree_sitter_rust", "tree_sitter_typescript", "tree_sitter_python",
        "tree_sitter_kotlin", "tree_sitter_go", "tree_sitter_java",
        "tree_sitter_c", "tree_sitter_cpp", "tree_sitter_javascript",
        "tree_sitter_c_sharp", "tree_sitter_ruby", "tree_sitter_php",
        "tree_sitter_swift", "tree_sitter_scala", "tree_sitter_hcl",
        "tree_sitter_elixir", "tree_sitter_languages",
    ]
    for name in _MOCK_GRAMMARS:
        if name not in sys.modules:
            mock_mod = types.ModuleType(name)
            # 部分 parser 文件访问 LANGUAGE 常量
            mock_mod.LANGUAGE = "mock_language"
            sys.modules[name] = mock_mod

    # 注入 mock 的 callwarden_core（Rust 扩展，未安装时降级）
    if "callwarden_core" not in sys.modules:
        mock_core = types.ModuleType("callwarden_core")
        # 提供 GraphStore 等类的 stub（daemon 不依赖 Rust 扩展）
        class _Stub:
            def __getattr__(self, name):
                raise AttributeError(f"callwarden_core.{name} not available (mock)")
        mock_core.GraphStore = _Stub()
        sys.modules["callwarden_core"] = mock_core

    from callwarden.server.daemon_server import (
        EnterpriseDaemonServer,
        EnterpriseDaemonService,
    )

    os.makedirs(os.path.dirname(sock_path), exist_ok=True)
    os.makedirs(os.path.dirname(registry_db), exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    service = EnterpriseDaemonService(registry_db=registry_db)
    server = EnterpriseDaemonServer(sock_path, service, max_workers=8)

    stop_event = threading.Event()

    def _run():
        try:
            server.serve_forever()
        except Exception as e:
            if not stop_event.is_set():
                print(f"[PY-DAEMON] error: {e}", file=sys.stderr)

    # 启动 server 线程
    server_thread = threading.Thread(target=_run, daemon=True, name="py-daemon-server")
    server_thread.start()

    # 等待 socket 就绪
    deadline = time.time() + 10
    while not os.path.exists(sock_path):
        if time.time() > deadline:
            raise TimeoutError("Python daemon socket 未在 10 秒内就绪")
        time.sleep(0.05)

    return server, stop_event


def stop_python_daemon(server, stop_event) -> None:
    """停止 Python daemon。"""
    stop_event.set()
    server.shutdown()


# ============================================
# 主流程
# ============================================


def cmd_benchmark(args) -> int:
    """对单个 daemon 执行基准测试。"""
    print(f"=== Benchmark target={args.target} socket={args.socket} iterations={args.iterations} ===")
    results = benchmark_suite(args.socket, args.iterations)

    # 输出 JSON 到文件
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {"target": args.target, "socket": args.socket, "results": results},
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"\nJSON 结果已保存到 {args.output}")

    return 0


def cmd_start_python(args) -> int:
    """启动 Python daemon 并保持运行（用于外部对比测试）。"""
    print(f"启动 Python daemon: socket={args.socket} registry={args.registry}")
    server, stop_event = start_python_daemon(args.socket, args.registry)
    print(f"Python daemon 已启动: {args.socket}")
    print("按 Ctrl+C 停止...")

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止 Python daemon...")
        stop_python_daemon(server, stop_event)
    return 0


def cmd_compare(args) -> int:
    """对比 Rust 和 Python daemon（需两者均已启动）。"""
    print("=== Performance Comparison: Rust cw_daemon vs Python daemon ===")
    print(f"iterations per method: {args.iterations}\n")

    print("--- Rust cw_daemon binary ---")
    rust_results = benchmark_suite(args.rust_socket, args.iterations)

    print("\n--- Python daemon ---")
    py_results = benchmark_suite(args.python_socket, args.iterations)

    # 输出对比表
    print("\n" + "=" * 80)
    print("对比表（单位：微秒 us）")
    print("=" * 80)
    print(
        f"{'method':<22} {'Rust p50':>10} {'Py p50':>10} {'ratio':>8} "
        f"{'Rust p95':>10} {'Py p95':>10} {'ratio':>8} "
        f"{'Rust p99':>10} {'Py p99':>10} {'ratio':>8}"
    )
    print("-" * 80)

    for r, p in zip(rust_results, py_results):
        if "error" in r or "error" in p:
            print(f"{r['method']:<22}  ERROR")
            continue
        rust_p50 = r.get("p50_us", 0)
        py_p50 = p.get("p50_us", 0)
        rust_p95 = r.get("p95_us", 0)
        py_p95 = p.get("p95_us", 0)
        rust_p99 = r.get("p99_us", 0)
        py_p99 = p.get("p99_us", 0)

        ratio50 = py_p50 / rust_p50 if rust_p50 > 0 else 0
        ratio95 = py_p95 / rust_p95 if rust_p95 > 0 else 0
        ratio99 = py_p99 / rust_p99 if rust_p99 > 0 else 0

        print(
            f"{r['method']:<22} "
            f"{rust_p50:>10.1f} {py_p50:>10.1f} {ratio50:>7.2f}x "
            f"{rust_p95:>10.1f} {py_p95:>10.1f} {ratio95:>7.2f}x "
            f"{rust_p99:>10.1f} {py_p99:>10.1f} {ratio99:>7.2f}x"
        )

    # 保存 JSON
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "rust": rust_results,
                    "python": py_results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        print(f"\nJSON 结果已保存到 {args.output}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    bench = sub.add_parser("benchmark", help="对单个 daemon 执行基准测试")
    bench.add_argument("--target", required=True, choices=["rust", "python"])
    bench.add_argument("--socket", required=True, help="UDS socket 路径")
    bench.add_argument("--iterations", type=int, default=200)
    bench.add_argument("--output", help="输出 JSON 文件路径")
    bench.set_defaults(func=cmd_benchmark)

    start_py = sub.add_parser("start-python", help="启动 Python daemon（用于对比测试）")
    start_py.add_argument("--socket", required=True)
    start_py.add_argument("--registry", required=True)
    start_py.set_defaults(func=cmd_start_python)

    cmp = sub.add_parser("compare", help="对比 Rust 和 Python daemon")
    cmp.add_argument("--rust-socket", required=True)
    cmp.add_argument("--python-socket", required=True)
    cmp.add_argument("--iterations", type=int, default=200)
    cmp.add_argument("--output", help="输出 JSON 文件路径")
    cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
