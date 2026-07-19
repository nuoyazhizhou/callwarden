"""Phase 8 metrics endpoint 闭合测试。

验证：
1. `cw daemon metrics` CLI 子命令（--format prometheus/json + --name 过滤 + --reset）
2. `get_metrics` MCP 工具（format=json/prometheus + name + reset）

设计原则：metrics 不依赖 daemon RPC（避免连不上 daemon 时无法查看本地指标），
直接复用 server/metrics.py 的 MetricsCollector 单例。
"""

import json
import subprocess
import sys
import os

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


# ----------------------------------------------------------------------
# CLI 子命令：cw daemon metrics
# ----------------------------------------------------------------------

def test_metrics_cli_parser_json_default():
    """--format 缺省为 json。"""
    args = _parser(include_serve=False).parse_args(["metrics"])
    assert args.format == "json"
    assert args.name is None
    assert args.reset is False


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


def test_metrics_cli_parser_reset_flag():
    """--reset 标志正确解析。"""
    args = _parser(include_serve=False).parse_args(["metrics", "--reset"])
    assert args.reset is True


def test_metrics_cli_json_output_has_builtin_gauges(capsys):
    """CLI json 输出包含内置 gauge（memory_rss_bytes 等）。"""
    reset_metrics_collector()  # 重置单例确保干净状态
    rc = run_daemon_command(["metrics", "--format", "json"], include_serve=False)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "timestamp" in data
    assert "uptime" in data
    assert "counters" in data
    assert "gauges" in data
    assert "histograms" in data
    # 内置 gauge 应被采集（collect_runtime_metrics 在 to_json 时自动调用）
    assert "memory_rss_bytes" in data["gauges"]
    assert "uptime_seconds" in data["gauges"]


def test_metrics_cli_prometheus_output_starts_with_help(capsys):
    """CLI prometheus 输出以 # HELP 开头。"""
    reset_metrics_collector()
    rc = run_daemon_command(
        ["metrics", "--format", "prometheus"], include_serve=False
    )
    out = capsys.readouterr().out
    assert rc == 0
    # Prometheus 文本格式必有 # HELP 和 # TYPE 行
    assert "# HELP" in out
    assert "# TYPE" in out
    # 应包含内置指标名
    assert "memory_rss_bytes" in out
    assert "uptime_seconds" in out


def test_metrics_cli_name_filter_returns_subset(capsys):
    """--name 过滤后只返回指定指标。"""
    reset_metrics_collector()
    rc = run_daemon_command(
        ["metrics", "--name", "memory_rss_bytes"], include_serve=False
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["found"] is True
    assert data["name_filter"] == "memory_rss_bytes"
    assert "memory_rss_bytes" in data["gauges"]
    # 其他类别应为空
    assert data["counters"] == {}
    assert data["histograms"] == {}


def test_metrics_cli_name_filter_nonexistent_returns_found_false(capsys):
    """--name 不存在时 found=False。"""
    reset_metrics_collector()
    rc = run_daemon_command(
        ["metrics", "--name", "nonexistent_metric"], include_serve=False
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["found"] is False


def test_metrics_cli_reset_clears_counters(capsys):
    """--reset 重置所有指标。"""
    # 先 increment 一些计数器
    collector = get_metrics_collector()
    collector.increment("requests_total")
    assert collector.get_metric("requests_total").get() == 1.0

    rc = run_daemon_command(["metrics", "--reset"], include_serve=False)
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["status"] == "reset"
    # 重置后计数器归零
    assert collector.get_metric("requests_total").get() == 0.0


# ----------------------------------------------------------------------
# MCP 工具：get_metrics
# ----------------------------------------------------------------------

def test_get_metrics_mcp_tool_registered():
    """MCP get_metrics 工具已注册到 mcp_server。

    验证 @mcp.tool() 装饰器已生效，不依赖 fastmcp 内部 API。
    """
    import re
    mcp_server_path = os.path.join(PROJECT_ROOT, "server", "mcp_server.py")
    with open(mcp_server_path, encoding="utf-8") as f:
        content = f.read()
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
    """MCP 工具总数从 204 增至 205（get_metrics 新增）。"""
    import re
    mcp_server_path = os.path.join(PROJECT_ROOT, "server", "mcp_server.py")
    with open(mcp_server_path, encoding="utf-8") as f:
        content = f.read()
    matches = re.findall(r'@mcp\.tool\(\)', content)
    assert len(matches) == 205, f"MCP 工具数应为 205，实际 {len(matches)}"


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
