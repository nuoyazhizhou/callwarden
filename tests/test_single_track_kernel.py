"""ADR-002 单轨内核静态断言。

唯一 daemon RPC 传输 = HTTPS + mTLS。本文件直接扫描源码，防止 pipe / UDS /
明文客户端在后续改动中复活。断言失败只能通过删除对应回退路径修复，
不得用 skip / xfail 掩盖。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_SRC = REPO_ROOT / "rust_ext" / "src"
SERVER_DIR = REPO_ROOT / "server"
CLI_DIR = REPO_ROOT / "cli"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_absent(path: Path, needles):
    text = _read(path)
    hits = [n for n in needles if n in text]
    assert not hits, f"{path.relative_to(REPO_ROOT)} 仍含已退役符号: {hits}"


def _assert_absent_in_tree(root: Path, needles):
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".rs", ".py"}:
            continue
        text = _read(path)
        for needle in needles:
            if needle in text:
                hits.append((str(path.relative_to(REPO_ROOT)), needle))
    assert not hits, f"{root.name}/ 仍含已退役符号: {hits}"


def test_python_daemon_service_is_removed():
    """U3：Python 侧 daemon_server.py 运行时无引用，整文件退役。"""
    assert not (SERVER_DIR / "daemon_server.py").exists()


def test_no_python_uds_or_pipe_client():
    """U2/U3：Python 客户端只允许 HTTPS，不得重建 UDS / named-pipe 客户端。"""
    for name in ("daemon_client.py", "daemon_autostart.py", "daemon_protocol.py"):
        _assert_absent(
            SERVER_DIR / name,
            [
                "class UnixDaemonRpcClient",
                "AF_UNIX",
                "CreateFileW",
                "_WindowsPipeSocket",
            ],
        )


def test_no_rust_legacy_rpc_client():
    """U2：Rust legacy RPC 客户端（UDS / named-pipe）已退役。"""
    _assert_absent(
        RUST_SRC / "daemon" / "client.rs",
        ["pub mod unix", "pub mod windows", "daemon_client_call_py"],
    )
    _assert_absent(
        RUST_SRC / "daemon" / "daemon_autostart_handlers.rs",
        ["UnixStream"],
    )


def test_no_rust_fd_passing_kernel():
    """U1：SCM_RIGHTS / memfd 内核已删除（HTTPS 无法传 FD）。"""
    assert not (RUST_SRC / "daemon" / "memfd.rs").exists()
    _assert_absent(
        RUST_SRC / "daemon" / "protocol.rs",
        [
            "send_message_with_fds",
            "recv_message_with_fds",
            "DEFAULT_MAX_FDS",
            "SCM_RIGHTS",
        ],
    )


def test_no_uds_listener_or_pipe_creator_in_rust():
    """单轨：Rust 侧不得存在 UDS listener 或 named-pipe 创建者。"""
    _assert_absent_in_tree(RUST_SRC, ["UnixListener", "CreateNamedPipeW"])


def test_no_cw_bridge_product_reference():
    """S4：cw-bridge 产物已下线，业务源码不得再引用。"""
    for root in (SERVER_DIR, CLI_DIR):
        hits = []
        for path in sorted(root.rglob("*.py")):
            text = _read(path)
            if "cw-bridge" in text or "cw_bridge" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
        assert not hits, f"{root.name}/ 仍引用 cw-bridge: {hits}"


def test_daemon_transport_defaults_to_https():
    """单轨默认值：客户端与 Rust daemon 均默认 HTTPS。"""
    assert '"https://127.0.0.1:6374"' in _read(SERVER_DIR / "daemon_autostart.py")
    assert '"https".to_string()' in _read(RUST_SRC / "daemon" / "dispatch.rs")


def test_s4_01_no_pipe_or_uds_rpc_branch_in_production():
    """S4-01：生产树无 pipe / UDS RPC 活分支。

    Python 生产树（server/cli）严格子串断言（当前零命中，含注释也不允许）；
    Rust 树按行剥离 `//` 注释后断言——保留的 `ImpersonateNamedPipeClient`
    等均为退役说明注释，不构成活分支。新增 `use ...NamedPipe` 或
    `UnixDaemonRpcClient` 调用即失败。
    """
    needles = ("NamedPipe", "UnixDaemonRpcClient")
    _assert_absent_in_tree(SERVER_DIR, needles)
    _assert_absent_in_tree(CLI_DIR, needles)
    hits = []
    for path in sorted(RUST_SRC.rglob("*.rs")):
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            code = line.split("//", 1)[0]
            for needle in needles:
                if needle in code:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{needle}")
    assert not hits, f"rust_ext/src/ 存在 pipe/UDS RPC 活分支: {hits}"


def test_s4_03_mcp_server_has_no_local_daemon_mode_branch():
    """S4-03：mcp_server.py 无 daemon_mode 活引用（local 分支已删除）。

    AST 解析：daemon_mode 不得作为名字、函数参数、属性或关键字参数出现；
    docstring 中的退役说明（Constant 字符串）不误判。
    """
    import ast

    tree = ast.parse(_read(SERVER_DIR / "mcp_server.py"))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == "daemon_mode"
        ) or (
            isinstance(node, ast.arg)
            and node.arg == "daemon_mode"
        ) or (
            isinstance(node, ast.Attribute)
            and node.attr == "daemon_mode"
        ) or (
            isinstance(node, ast.keyword)
            and (node.arg or "") == "daemon_mode"
        ):
            hits.append(node.lineno)
    assert not hits, f"server/mcp_server.py 仍存在 daemon_mode 活引用: 行 {hits}"
