"""Phase 8 metrics endpoint 闭合测试。

验证：
1. `cw daemon metrics` CLI 子命令（--format prometheus/json + --name 过滤）
2. `get_metrics` MCP 工具（format=json/prometheus + name）

现状（CLI-004 整改后）：metrics 默认经 daemon RPC 获取（Rust daemon 为唯一
authority，fail-closed），不再有 --local/--reset 进程内降级；--from-file 仅作
显式离线快照检视。
"""

import json
import subprocess
import sys
import os
from unittest.mock import patch

import pytest

# 添加项目根到 sys.path 以便直接 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from callwarden.server.metrics import (
    get_metrics_collector,
    reset_metrics_collector,
)
from callwarden.cli.daemon_commands import run_daemon_command, _parser


# stale 依据（A 类：测试侧期望陈旧）：metrics CLI 默认路径已改为 daemon RPC
# （cli/daemon_commands.py:479-509，Rust daemon 为唯一 authority，fail-closed），
# 不再走进程内 MetricsCollector 的 hidden local 降级；CLI 输出的是 daemon 进程
# 指标（daemon.* / callwarden_daemon_*），不含 Python 进程的 memory_rss_bytes。
# 原用例直接调用 CLI 依赖 live daemon（不可达时 rc=2，可达时指标集不同），
# 因此注入伪 RPC 客户端，使 CLI 格式化路径离线且确定性地被验证。
_FAKE_DAEMON_SNAPSHOT = {
    "timestamp": 123.0,
    "uptime": 45.0,
    "counters": {"daemon.rpc_total": {"help": "", "values": {"": 7}}},
    "gauges": {
        "daemon.uptime_seconds": {"help": "Daemon uptime in seconds", "values": {"": 45.0}},
        "daemon.active_jobs": {"help": "", "values": {"": 0}},
    },
    "histograms": {},
}

_FAKE_DAEMON_PROMETHEUS = (
    "# HELP callwarden_daemon_uptime_seconds Daemon uptime in seconds\n"
    "# TYPE callwarden_daemon_uptime_seconds gauge\n"
    "callwarden_daemon_uptime_seconds 45.0\n"
)


class _FakeDaemonRpcClient:
    """伪 daemon RPC 客户端：返回 Rust daemon metrics.snapshot/prometheus 形状。"""

    def __init__(self, socket_path=None, **kwargs):
        pass

    def call(self, method, params=None, **kwargs):
        if method == "metrics.prometheus":
            return _FAKE_DAEMON_PROMETHEUS
        return _FAKE_DAEMON_SNAPSHOT


# ----------------------------------------------------------------------
# CLI 子命令：cw daemon metrics
# ----------------------------------------------------------------------

def test_metrics_cli_parser_json_default():
    """--format 缺省为 json（--reset/--local 已在 CLI-004 整改中移除）。"""
    args = _parser(include_serve=False).parse_args(["metrics"])
    assert args.format == "json"
    assert args.name is None


def test_metrics_cli_parser_prometheus_format():
    """--format prometheus 正确解析。"""
    args = _parser(include_serve=False).parse_args(
        ["metrics", "--format", "prometheus"]
    )
    assert args.format == "prometheus"


def test_metrics_cli_parser_name_filter():
    """--name 过滤参数正确解析。"""
    args = _parser(include_serve=False).parse_args(
        ["metrics", "--name", "memory_rss_bytes"]
    )
    assert args.name == "memory_rss_bytes"


def test_metrics_cli_reset_flag_removed():
    """--reset 已从 CLI 移除（CLI-004：metrics 仅经 daemon RPC，无本地重置）。"""
    with pytest.raises(SystemExit):
        _parser(include_serve=False).parse_args(["metrics", "--reset"])


def test_metrics_cli_local_flag_removed():
    """--local 已从 CLI 移除（CLI-004：取消进程内 SQLite 降级）。"""
    with pytest.raises(SystemExit):
        _parser(include_serve=False).parse_args(["metrics", "--local", "--reset"])


def test_metrics_cli_from_file_missing_returns_error(capsys):
    """--from-file 指向缺失文件时 fail-closed 返回 exit code 2。"""
    rc = run_daemon_command(
        ["metrics", "--from-file", "no-such-snapshot.json"], include_serve=False
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "快照文件不存在或损坏" in err


def test_metrics_cli_json_output_has_daemon_gauges(capsys):
    """CLI json 输出来自 daemon RPC 快照（daemon.* gauge）。"""
    with patch(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", _FakeDaemonRpcClient
    ):
        rc = run_daemon_command(["metrics", "--format", "json"], include_serve=False)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "timestamp" in data
    assert "uptime" in data
    assert "counters" in data
    assert "gauges" in data
    assert "histograms" in data
    # daemon RPC 快照的 gauge 应被原样输出
    assert "daemon.uptime_seconds" in data["gauges"]


def test_metrics_cli_prometheus_output_starts_with_help(capsys):
    """CLI prometheus 输出直接透传 daemon metrics.prometheus 文本。"""
    with patch(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", _FakeDaemonRpcClient
    ):
        rc = run_daemon_command(
            ["metrics", "--format", "prometheus"], include_serve=False
        )
    out = capsys.readouterr().out
    assert rc == 0
    # Prometheus 文本格式必有 # HELP 和 # TYPE 行
    assert "# HELP" in out
    assert "# TYPE" in out
    # 应包含 daemon 指标名
    assert "callwarden_daemon_uptime_seconds" in out


def test_metrics_cli_name_filter_returns_subset(capsys):
    """--name 过滤后只返回指定指标（daemon RPC 快照）。"""
    with patch(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", _FakeDaemonRpcClient
    ):
        rc = run_daemon_command(
            ["metrics", "--name", "daemon.uptime_seconds"], include_serve=False
        )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["found"] is True
    assert data["name_filter"] == "daemon.uptime_seconds"
    assert "daemon.uptime_seconds" in data["gauges"]
    # 其他类别应为空
    assert data["counters"] == {}
    assert data["histograms"] == {}


def test_metrics_cli_name_filter_nonexistent_returns_found_false(capsys):
    """--name 不存在时 found=False。"""
    with patch(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", _FakeDaemonRpcClient
    ):
        rc = run_daemon_command(
            ["metrics", "--name", "nonexistent_metric"], include_serve=False
        )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["found"] is False


# ----------------------------------------------------------------------
# MCP 工具：get_metrics
# ----------------------------------------------------------------------

def _mcp_sources_combined():
    """mcp_server.py 与 server/tools/*.py 合并源码。

    拆分后 @mcp.tool() 工具分布在 server/tools/ 功能域模块中，静态断言需合并扫描。
    """
    paths = [os.path.join(PROJECT_ROOT, "server", "mcp_server.py")]
    tools_dir = os.path.join(PROJECT_ROOT, "server", "tools")
    if os.path.isdir(tools_dir):
        paths += [
            os.path.join(tools_dir, f)
            for f in sorted(os.listdir(tools_dir))
            if f.endswith(".py") and f != "__init__.py"
        ]
    chunks = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            chunks.append(f.read())
    return "\n".join(chunks)


def test_get_metrics_mcp_tool_registered():
    """MCP get_metrics 工具已注册到 mcp_server。

    验证 @mcp.tool() 装饰器已生效，不依赖 fastmcp 内部 API。
    """
    import re
    content = _mcp_sources_combined()
    # 找到 get_metrics 函数定义
    match = re.search(
        r'@mcp\.tool\(\)\s*\n\s*def get_metrics\(', content
    )
    assert match is not None, "get_metrics MCP 工具未在 mcp_server.py 中注册"

    # 验证 mcp_server 能正常 import 并创建
    from callwarden.server.mcp_server import create_mcp_server
    mcp = create_mcp_server()
    assert mcp is not None


def test_get_metrics_mcp_tool_count_increased():
    """MCP 工具总数 243（拆分后注册在 server/tools 功能域模块）。"""
    import re
    content = _mcp_sources_combined()
    matches = re.findall(r'(?m)^    @mcp\.tool\(\)$', content)
    # P4 assignment/lease 新增 8 工具（227→235），P3/P4 后 237；
    # 后续批次（SRV/backup/gc/snapshot/mount/toolchain 等）增至 243。
    assert len(matches) == 243, f"MCP 工具数应为 243，实际 {len(matches)}"


def test_get_metrics_mcp_function_callable():
    """直接验证 get_metrics 函数体内的逻辑可执行。

    不通过 fastmcp 协议层，而是验证关键代码路径：
    1. from callwarden.server.metrics import get_metrics_collector 可用
    2. collector.to_prometheus() / to_json() 可调用
    3. reset 路径可执行
    """
    reset_metrics_collector()
    collector = get_metrics_collector()

    # 验证 json 路径
    data = collector.to_json()
    assert "timestamp" in data
    assert "counters" in data
    assert "gauges" in data
    assert "histograms" in data
    assert "memory_rss_bytes" in data["gauges"]

    # 验证 prometheus 路径
    text = collector.to_prometheus()
    assert "# HELP" in text
    assert "# TYPE" in text
    assert "memory_rss_bytes" in text

    # 验证 name 过滤逻辑（与 MCP 工具内一致的实现）
    name = "memory_rss_bytes"
    found = False
    for category in ("counters", "gauges", "histograms"):
        if name in data[category]:
            found = True
    assert found is True

    # 验证 reset 路径
    collector.increment("requests_total", 3)
    assert collector.get_metric("requests_total").get() == 3.0
    collector.reset()
    assert collector.get_metric("requests_total").get() == 0.0


def test_get_metrics_mcp_name_filter_nonexistent():
    """name 过滤不存在的指标时 found=False。"""
    reset_metrics_collector()
    collector = get_metrics_collector()
    data = collector.to_json()
    name = "nonexistent_metric_xyz"
    found = False
    for category in ("counters", "gauges", "histograms"):
        if name in data[category]:
            found = True
    assert found is False
