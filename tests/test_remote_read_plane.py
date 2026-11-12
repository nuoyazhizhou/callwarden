r"""S2（ADR-002）远端只读面客户端本机执行单测（S2-01/02/05/08/09）。

契约：`docs/design/mtls-remote-plane.md` §16（L568-772，已冻结）；
验收行：`docs/design/cli-mcp-matrix-backlog.md` §38.2。

覆盖矩阵（对齐 §16.4 客户端执行工具清单与返回形态）：
- S2-01 file.read 客户端本机执行：内容=客户端本地文件（Rust str::lines()
  语义：\r\n 计一换行、行尾 \r 剥离、末尾换行不产生空行），offset/limit
  下限钳制（offset <0→0、limit<1→1），exec_origin="client"，零 RPC——
  daemon 从未打开过该路径（桩 client.call 任何调用即断言失败）。
- S2-02 file.grep/file.list 与客户端本地执行一致：grep 大小写不敏感子串
  （非正则，正则元字符按字面匹配）+ head_limit 双层截断 + glob 段语义
  （*→[^/]* 不跨目录段，**/*.py 才能命中深层）+ 扫描剪枝
  （SKIP_DIRS/hidden/.md 等非可索引扩展名）；file.list 裸数组（不加
  exec_origin）、按 rel_path 排序、含 is_dir/size；零 RPC。
- S2-05 绝对路径拒绝：Windows 盘符（C:\...、D:/...）与 POSIX（/etc/...）
  形态的参数在客户端 fail-closed 抛 E_REMOTE_PATH_FORBIDDEN，
  不发起任何 RPC（桩 calls/uploads 均为空）。
- S2-08 跨 OS 只读矩阵：env CW_S2_REMOTE_ENDPOINT 提供
  对侧 OS（WSL/Linux）daemon 时运行真实 mTLS 往返（file.read 本机执行
  + file.list + build_graph→snapshot.upload 发布 + query.stats 索引可查）；
  缺省 skip（本机为 Windows 单端，另一方向由 CI 矩阵承载）。
- S2-09 无本地降级：remote profile + daemon 不可达 → 结构化错误
  （DaemonUnavailableError.code=E_HTTP_DAEMON_UNAVAILABLE），不回退
  本地 SQLite（sqlite3.connect 全局钉死作回退探针）；route_rpc
  needs_snapshot remote 守卫（不取 daemon db_path）；bind_scope
  manifest 解析 fail-closed 回滚 loopback。

stub 边界：`tools_workspace._remote_client` 为模块级函数，直接 monkeypatch
打桩（与 test_workspace_rpc_http.py 同款 harness 风格）；`_remote_client`
之外所有 daemon 侧判定（bind_scope/上传协议/服务端校验）由
rust_ext/src/daemon/snapshot_state.rs 的 `s2_upload_tests`（cargo test）与
本文件 §S2-04 半（tests/test_remote_snapshot_upload.py）覆盖。
"""

import os

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 跨 OS 只读矩阵（S2-08）：env 提供对侧 OS daemon endpoint 与 mTLS 材料时启用。
# CW_S2_REMOTE_ENDPOINT如 https://10.x.x.x:4014；mTLS 证书经
# CW_S2_REMOTE_CERT / CW_S2_REMOTE_KEY / CW_S2_REMOTE_CA 指定（对侧 daemon
# 签发的 client cert，与本机 loopback CA 分离）。
_REMOTE_ENDPOINT = os.environ.get("CW_S2_REMOTE_ENDPOINT", "")
requires_remote_daemon = pytest.mark.skipif(
    not _REMOTE_ENDPOINT,
    reason="跨 OS 只读矩阵需 CW_S2_REMOTE_ENDPOINT（对侧 OS daemon；"
           "本机为 Windows 单端，另一方向由 CI 矩阵承载）",
)


class _StubRemoteClient:
    """remote profile 桩 client：任何 daemon RPC 调用一律断言失败。

    S2-01/02 的核心断言即"本机执行零 RPC"——read/grep/list 不应触发
    client.call（桩将其转为 AssertionError），也不应触发上传或
    workspace 注册。ensure_remote_workspace/upload_snapshot 保留可编程
    失败（S2-09 无 daemon 降级/S2-06 篡改传播）。
    """

    def __init__(self, root, ws_id="inst-s2-read-1"):
        self._project_root = root
        self._remote_workspace_id = ws_id
        self.calls = []
        self.uploads = []
        self.ensure_error = None
        self.upload_error = None

    def is_remote_profile(self):
        return True

    def bind_scope(self):
        return "remote"

    def configure_workspace(self, root):
        self._project_root = root

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        raise AssertionError(
            f"远端只读面不应发起 {method} RPC（S2-01/02：客户端本机执行零 RPC）"
        )

    def ensure_remote_workspace(self):
        self.calls.append(("ensure_remote_workspace", {}))
        if self.ensure_error is not None:
            raise self.ensure_error
        return self._remote_workspace_id

    def _ensure_remote_snapshot(self, db_path=None):
        """route_rpc needs_snapshot 注入入口（remote 语义：只注册不发布）。"""
        self.calls.append(("_ensure_remote_snapshot", {"db_path": db_path}))
        if self.ensure_error is not None:
            raise self.ensure_error
        return self._remote_workspace_id

    def upload_snapshot(self, workspace_instance_id, raw_bytes, **kwargs):
        self.uploads.append((workspace_instance_id, raw_bytes, kwargs))
        if self.upload_error is not None:
            raise self.upload_error
        return {"ok": True, "snapshot_id": "snap-s2-read-1"}


class _CaptureMCP:
    """捕获 register(mcp) 注册的闭包工具，便于直接调用（绕过 FastMCP）。"""

    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn

        return decorator


# fixture 工作树布局（rel 相对根）：
#   alpha.py            CRLF + "marker alpha"（file.read 语义 + grep 大小写）
#   crlf.txt            行尾语义 fixture（\r\n / \n / 空行 / 末尾 \r）
#   gamma.py            3 处 "zzz"（head_limit 双层截断）
#   pkg/beta.py         "a.b" 字面与 "axb"（grep 非正则断言）
#   pkg/nested/deep.py  深层 .py（glob 段语义：*.py 不命中、**/*.py 命中）
#   notes.md            非可索引扩展名（剪枝）
#   node_modules/x.py   SKIP_DIRS 剪枝
#   .hidden/secret.py   hidden 段剪枝
ALPHA = "import os\r\n# marker alpha\r\ndef alpha():\r\n    return 1\r\n"
CRLF_TXT = "one\r\ntwo\nthree\r\n\nfive\r\n"
GAMMA = "zzz_one\ndef gamma():\n    zzz_two\n    zzz_three\n"
BETA = "def beta():\n    rate = a.b * 2\naxb_zz = axb + 1\n"
DEEP = "zzz_deep\ndef deep():\n    return 0\n"


@pytest.fixture
def remote_tools(tmp_path, monkeypatch):
    """构造 remote profile 工作树 + 桩 client，返回 (stub, tools, root)。"""
    from callwarden.server.tools import tools_workspace

    # write_bytes：精确字节落盘（write_text 在 Windows 会把 "\n" 再翻成
    # "\r\n"，对显式 "\r\n" fixture 产生 "\r\r\n" 双倍行，破坏行号/行数断言）
    (tmp_path / "alpha.py").write_bytes(ALPHA.encode("utf-8"))
    (tmp_path / "crlf.txt").write_bytes(CRLF_TXT.encode("utf-8"))
    (tmp_path / "gamma.py").write_bytes(GAMMA.encode("utf-8"))
    (tmp_path / "notes.md").write_bytes(b"zzz_md_marker\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_bytes(b"zzz_nm_marker\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_bytes(b"zzz_hidden_marker\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "beta.py").write_bytes(BETA.encode("utf-8"))
    (tmp_path / "pkg" / "nested").mkdir()
    (tmp_path / "pkg" / "nested" / "deep.py").write_bytes(DEEP.encode("utf-8"))

    stub = _StubRemoteClient(str(tmp_path))
    monkeypatch.setattr(tools_workspace, "_remote_client", lambda: stub)
    mcp = _CaptureMCP()
    tools_workspace.register(mcp)
    yield stub, mcp._tools, tmp_path


# ----------------------------------------------------------------------
# S2-01：file.read 客户端本机执行
# ----------------------------------------------------------------------

class TestS201FileReadClientLocal:
    """S2-01：客户端本机 file.read，内容来自本机文件，daemon 零接触。"""

    def test_s2_01_read_returns_local_content_with_exec_origin(self, remote_tools):
        _stub, tools, _root = remote_tools

        result = tools["file_read"]("alpha.py")

        assert set(result) == {
            "file_path", "offset", "limit", "total_lines", "content",
            "exec_origin",
        }, f"§16.4 file.read remote 返回形态，实际 {sorted(result)}"
        assert result["file_path"] == "alpha.py"          # 入参原样回传
        assert result["exec_origin"] == "client"
        assert result["offset"] == 0 and result["limit"] == 200
        # Rust str::lines() 语义：\r\n 计一换行、行尾 \r 剥离、末尾换行不产生空行
        assert result["total_lines"] == 4
        assert result["content"] == "import os\n# marker alpha\ndef alpha():\n    return 1"

    def test_s2_01_line_ending_and_offset_limit_semantics(self, remote_tools):
        _stub, tools, _root = remote_tools

        full = tools["file_read"]("crlf.txt")
        assert full["total_lines"] == 5                    # 末尾 \r\n 不产生空行
        assert full["content"] == "one\ntwo\nthree\n\nfive"

        window = tools["file_read"]("crlf.txt", offset=1, limit=2)
        assert window["total_lines"] == 5
        assert window["content"] == "two\nthree"

        beyond = tools["file_read"]("crlf.txt", offset=10, limit=5)
        assert beyond["content"] == ""                     # offset 越界不报错

        assert tools["file_read"]("crlf.txt", offset=-3)["offset"] == 0   # 负 offset 钳 0
        assert tools["file_read"]("crlf.txt", limit=0)["limit"] == 1     # limit<1 钳 1
        assert tools["file_read"]("crlf.txt", limit=-5)["limit"] == 1

    def test_s2_01_daemon_never_opened_the_path(self, remote_tools):
        """核心断言：整轮 read 期间桩 client 无任何 RPC/上传/注册调用。"""
        stub, tools, _root = remote_tools

        for rel in ("alpha.py", "crlf.txt", "pkg/beta.py", "pkg/nested/deep.py"):
            tools["file_read"](rel)

        assert stub.calls == [], f"file.read 本机执行不应发起 RPC，实际 {stub.calls}"
        assert stub.uploads == []

    def test_s2_01_missing_file_and_path_escape_structured_errors(self, remote_tools, tmp_path):
        """S2-09 结构化错误口径：missing→path_not_found、越根→path_escape，
        不回退本地 SQLite、不静默成功。"""
        _stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_read"]("nope.py")
        assert exc.value.code == "path_not_found"

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_read"]("pkg")                      # 目录不是文件
        assert exc.value.code == "path_not_found"

        sibling = tmp_path.parent / "s2_outside_probe.py"
        sibling.write_text("zzz\n", encoding="utf-8")
        try:
            with pytest.raises(DaemonRemoteError) as exc:
                tools["file_read"]("../s2_outside_probe.py")
            assert exc.value.code == "path_escape"
        finally:
            sibling.unlink()

        # include_context 为 §16.4 契约漂移项：remote 分支同样不生成
        # symbols/symbol_contexts（与 daemon handler 行为一致），返回仍为
        # 基础六键形态。
        result = tools["file_read"]("crlf.txt", include_context=True)
        assert "symbols" not in result and "symbol_contexts" not in result


# ----------------------------------------------------------------------
# S2-02：file.grep / file.list 与客户端本地执行一致
# ----------------------------------------------------------------------

class TestS202GrepListConsistency:
    """S2-02：grep/list 本机执行，语义与 daemon handler（fs_handlers.rs）同构。"""

    def test_s2_02_grep_case_insensitive_literal(self, remote_tools):
        _stub, tools, _root = remote_tools

        # 大小写不敏感：pattern "MARKER" 命中小写 "marker alpha"
        result = tools["file_grep"]("MARKER")
        assert result["exec_origin"] == "client"
        assert result["count"] == 1
        assert result["matches"][0]["file_path"] == "alpha.py"
        assert result["matches"][0]["line"] == 2
        assert result["matches"][0]["text"] == "# marker alpha"

    def test_s2_02_grep_is_substring_not_regex(self, remote_tools):
        """正则元字符按字面子串处理：'a.b' 命中字面 'a.b'，不命中 'axb'。"""
        _stub, tools, _root = remote_tools

        result = tools["file_grep"]("a.b")
        assert result["count"] == 1, "只应命中 pkg/beta.py 的字面 'a.b'"
        assert result["matches"][0]["file_path"] == "pkg/beta.py"
        assert result["matches"][0]["line"] == 2
        assert result["matches"][0]["text"].endswith("rate = a.b * 2")

        # '(' 同理按字面：匹配 "def (" 字面片段
        paren = tools["file_grep"]("def g")
        assert paren["count"] == 1
        assert paren["matches"][0]["file_path"] == "gamma.py"

    def test_s2_02_grep_head_limit_double_truncation(self, remote_tools):
        """head_limit 双层截断：外层文件循环顶 + 内层行循环 break。

        扫描序（rel 排序）alpha.py, gamma.py, pkg/beta.py, pkg/nested/deep.py；
        'zzz' 命中序：gamma L1/L3/L4（GAMMA 第 1/3/4 行）、deep L1。
        """
        _stub, tools, _root = remote_tools

        full = tools["file_grep"]("zzz")
        assert full["count"] == 4, (
            "剪枝必须生效：node_modules/.hidden/.md 的 zzz 命中不计数，"
            f"实际 {full['count']}"
        )

        limited = tools["file_grep"]("zzz", head_limit=2)
        assert limited["count"] == 2
        assert [m["line"] for m in limited["matches"]] == [1, 3]
        assert limited["count"] == len(limited["matches"])

        one = tools["file_grep"]("zzz", head_limit=1)
        assert one["count"] == 1
        assert one["matches"][0]["file_path"] == "gamma.py"

    def test_s2_02_grep_glob_segment_semantics(self, remote_tools):
        """glob 段语义与 Rust glob_match 同构：* 不跨目录段。"""
        _stub, tools, _root = remote_tools

        root_only = tools["file_grep"]("zzz", glob="*.py")
        assert {m["file_path"] for m in root_only["matches"]} == {"gamma.py"}

        deep = tools["file_grep"]("zzz", glob="**/*.py")
        # glob 段语义（同 Rust glob_match，fs_handlers.rs L2497 测试）：
        # "**/" 要求至少一层目录段——根级 gamma.py 不命中
        assert {m["file_path"] for m in deep["matches"]} == {"pkg/nested/deep.py"}

        scoped = tools["file_grep"]("zzz", path="pkg", glob="**/*.py")
        # path 子树扫描的 rel 仍按工作区根表达（与 daemon handler 一致）
        assert {m["file_path"] for m in scoped["matches"]} == {"pkg/nested/deep.py"}

        empty = tools["file_grep"]("zzz", glob="*.md")
        assert empty["count"] == 0, ".md 非可索引扩展名，扫描阶段已剪枝"

    def test_s2_02_grep_scan_pruning(self, remote_tools):
        """SKIP_DIRS / hidden / 非可索引扩展名在扫描阶段剪枝。"""
        _stub, tools, _root = remote_tools

        assert tools["file_grep"]("zzz_nm_marker")["count"] == 0        # node_modules
        assert tools["file_grep"]("zzz_hidden_marker")["count"] == 0    # hidden
        assert tools["file_grep"]("zzz_md_marker")["count"] == 0        # .md

    def test_s2_02_grep_empty_pattern_invalid_params(self, remote_tools):
        _stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_grep"]("")
        assert exc.value.code == "invalid_params"

    def test_s2_02_list_bare_array_sorted_with_dir_entries(self, remote_tools):
        """file.list 返回裸数组（无 exec_origin）、按 rel_path 排序、含 is_dir。"""
        _stub, tools, root = remote_tools

        rows = tools["file_list"]()
        # conftest autouse `_isolate_db_path` 在 tmp 建隔离目录
        # "callwarden_dir"，非本 fixture 工作树成员，排除后再断言
        rows = [r for r in rows if r["rel_path"] != "callwarden_dir"]

        assert isinstance(rows, list), "§16.4：file.list 远端分支返回裸数组"
        assert rows == sorted(rows, key=lambda r: r["rel_path"])
        names = {r["rel_path"] for r in rows}
        assert names == {
            ".hidden", "alpha.py", "crlf.txt", "gamma.py",
            "node_modules", "notes.md", "pkg",
        }
        for row in rows:
            assert set(row) == {"rel_path", "size", "is_dir"}, (
                f"file.list 行键契约，实际 {sorted(row)}"
            )
            assert row["is_dir"] == os.path.isdir(os.path.join(str(root), row["rel_path"]))
            assert row["size"] == os.path.getsize(os.path.join(str(root), row["rel_path"]))

    def test_s2_02_list_glob_and_subdir(self, remote_tools):
        _stub, tools, _root = remote_tools

        py_only = tools["file_list"](glob="*.py")
        assert [r["rel_path"] for r in py_only] == ["alpha.py", "gamma.py"]

        pkg = tools["file_list"](path="pkg")
        assert [r["rel_path"] for r in pkg] == ["pkg/beta.py", "pkg/nested"]

        pkg_files = tools["file_list"](path="pkg", glob="*.py")
        # glob 按工作区根 rel 表达（同 daemon fs_handlers.rs L1709：
        # base 子目录不重设 glob 基准）——"pkg/beta.py" 不匹配 "*.py"
        assert [r["rel_path"] for r in pkg_files] == []

    def test_s2_02_zero_rpc_for_grep_and_list(self, remote_tools):
        stub, tools, _root = remote_tools

        tools["file_grep"]("zzz")
        tools["file_list"]()
        tools["file_list"](path="pkg", glob="*.py")

        assert stub.calls == [], f"grep/list 本机执行不应发起 RPC，实际 {stub.calls}"
        assert stub.uploads == []


# ----------------------------------------------------------------------
# S2-05：绝对路径拒绝（客户端 fail-first，不发起 RPC）
# ----------------------------------------------------------------------

class TestS205AbsolutePathRejection:
    """S2-05：绝对路径参数（盘符 / POSIX 根）客户端先行拒绝。

    §16.1 路径拒绝表：remote profile 下任意 RPC params 中的绝对路径一律
    E_REMOTE_PATH_FORBIDDEN，客户端先于 daemon 兜底拒绝。
    """

    @pytest.mark.parametrize(
        "abs_path",
        [
            r"C:\Windows\win.ini",
            "D:/some/abs/path.py",
            "/etc/passwd",
            "\\\\server\\share\\file.py",
        ],
    )
    def test_s2_05_file_read_rejects_absolute_path(self, remote_tools, abs_path):
        stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_read"](abs_path)
        assert exc.value.code == "E_REMOTE_PATH_FORBIDDEN"
        assert "file_read" in exc.value.message
        # 未发起任何 RPC / 上传 / 注册（失败先于网络）
        assert stub.calls == []
        assert stub.uploads == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"path": r"C:\Users"},
            {"path": "/home"},
            {"glob": "/abs/glob/*.py"},
        ],
    )
    def test_s2_05_grep_and_list_reject_absolute_path(self, remote_tools, kwargs):
        stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_grep"]("zzz", **kwargs)
        assert exc.value.code == "E_REMOTE_PATH_FORBIDDEN"
        assert "file_grep" in exc.value.message

        with pytest.raises(DaemonRemoteError) as exc:
            tools["file_list"](**kwargs)
        assert exc.value.code == "E_REMOTE_PATH_FORBIDDEN"
        assert "file_list" in exc.value.message

        assert stub.calls == []
        assert stub.uploads == []

    def test_s2_05_build_directory_rejects_absolute_path(self, remote_tools):
        stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["build_directory"](r"C:\abs\dir")
        assert exc.value.code == "E_REMOTE_PATH_FORBIDDEN"
        assert stub.calls == [] and stub.uploads == []

        with pytest.raises(DaemonRemoteError) as exc:
            tools["build_directory"]("/abs/dir")
        assert exc.value.code == "E_REMOTE_PATH_FORBIDDEN"
        assert stub.calls == [] and stub.uploads == []

    def test_s2_05_build_directory_empty_path_invalid_params(self, remote_tools):
        _stub, tools, _root = remote_tools

        with pytest.raises(DaemonRemoteError) as exc:
            tools["build_directory"]("")
        assert exc.value.code == "invalid_params"

    def test_s2_05_relative_paths_still_pass(self, remote_tools):
        """相对路径不受影响（白盒：回归守护，保证拒绝规则不过度）。"""
        _stub, tools, _root = remote_tools

        result = tools["file_read"]("pkg/beta.py")
        assert result["exec_origin"] == "client"


# ----------------------------------------------------------------------
# S2-09：无 daemon 时结构化错误，不回退本地 SQLite
# ----------------------------------------------------------------------

class TestS209NoDaemonNoLocalFallback:
    """S2-09：remote profile + daemon 不可达 → 结构化错误，禁止本地降级。"""

    def test_s2_09_build_graph_daemon_down_structured_error(self, remote_tools):
        """daemon 不可达时 build_graph 的 upload 阶段失败且不回退。"""
        stub, tools, root = remote_tools
        stub.ensure_error = DaemonUnavailableError(
            "daemon 不可达（模拟 mTLS 握手失败）", code="E_HTTP_DAEMON_UNAVAILABLE"
        )

        with pytest.raises(DaemonUnavailableError) as exc:
            tools["build_graph"]()
        assert exc.value.code == "E_HTTP_DAEMON_UNAVAILABLE"
        # 失败前无任何上传尝试；无 snapshot.publish 回退
        assert stub.uploads == []
        assert all(m != "snapshot.publish" for m, _ in stub.calls)

    def test_s2_09_route_rpc_remote_guard_skips_db_path_resolution(
        self, remote_tools, monkeypatch
    ):
        """route_rpc needs_snapshot remote 守卫：remote profile 不解析
        daemon db_path（mcp.common.get_db_path_for_daemon 零调用）。"""
        from callwarden.server import daemon_client

        stub, _tools, root = remote_tools
        stub.calls = []

        instance_calls = []

        class _RouteStub(_StubRemoteClient):
            def call(self, method, params=None, **kwargs):
                instance_calls.append((method, params or {}))
                return {}

            def call_with_autostart(self, method, params=None, **kwargs):
                instance_calls.append((method, params or {}))
                return {"ok": True, "method": method}

        route_stub = _RouteStub(str(root))

        def fake_get_instance():
            instance_calls.append(("__get_instance__", {}))
            return route_stub

        monkeypatch.setattr(
            daemon_client.HttpDaemonRpcClient, "get_instance",
            staticmethod(fake_get_instance),
        )
        # 兼容 _get_rpc_client_for_route 的解析路径（两者同源 get_instance）
        monkeypatch.setattr(
            daemon_client, "_get_rpc_client_for_route", fake_get_instance,
        )

        result = daemon_client.route_rpc(
            "query.stats", {"limit": 5}, "READ_ONLY"
        )

        assert result == {"ok": True, "method": "query.stats"}
        methods = [m for m, _ in instance_calls]
        assert "mcp.common.get_db_path_for_daemon" not in methods, (
            "remote 守卫：不得解析 daemon db_path"
        )
        assert "snapshot.publish" not in methods
        # needs_snapshot 的 remote 守卫：经 _ensure_remote_snapshot 注册，
        # snapshot_db_path 保持 None（不发本机 db 路径）
        assert ("_ensure_remote_snapshot", {"db_path": None}) in route_stub.calls, (
            "remote profile 下 needs_snapshot 方法仍需 workspace 注册（无 db_path）"
        )
        # 最终 RPC 注入了 workspace_instance_id
        final = [p for m, p in instance_calls if m == "query.stats"]
        assert final and final[0].get("workspace_instance_id") == "inst-s2-read-1"

    def test_s2_09_route_rpc_daemon_down_does_not_fallback_to_sqlite(
        self, remote_tools, monkeypatch
    ):
        """route_rpc 在 remote profile + daemon 不可达时抛结构化错误，
        全局钉死 sqlite3.connect 证明无本地 SQLite 回退（薄客户端契约）。"""
        import sqlite3

        from callwarden.server import daemon_client

        stub, _tools, root = remote_tools

        class _DownStub(_StubRemoteClient):
            def __init__(self, root):
                super().__init__(root)
                self.ensure_error = DaemonUnavailableError(
                    "daemon 不可达", code="E_HTTP_DAEMON_UNAVAILABLE"
                )

            def call_with_autostart(self, method, params=None, **kwargs):
                raise AssertionError("daemon 不可达时不应到达 RPC 分发")

        down = _DownStub(str(root))

        def fake_get_instance():
            return down

        monkeypatch.setattr(
            daemon_client.HttpDaemonRpcClient, "get_instance",
            staticmethod(fake_get_instance),
        )
        monkeypatch.setattr(
            daemon_client, "_get_rpc_client_for_route", fake_get_instance,
        )

        def _no_sqlite(*args, **kwargs):
            raise AssertionError("S2-09：禁止回退本地 SQLite（route_rpc）")

        monkeypatch.setattr(sqlite3, "connect", _no_sqlite)

        with pytest.raises(DaemonUnavailableError) as exc:
            daemon_client.route_rpc("query.stats", {"limit": 5}, "READ_ONLY")
        assert exc.value.code == "E_HTTP_DAEMON_UNAVAILABLE"
        # 失败先于 db_path 解析
        assert all(m != "mcp.common.get_db_path_for_daemon" for m, _ in down.calls)

    def test_s2_09_bind_scope_manifest_parsing_fail_closed(self, monkeypatch, tmp_path):
        """bind_scope 读 manifest v2 顶层字段：无网络往返、缺失/异常回滚
        loopback（remote 判定只能来自 manifest，不猜不默认）。"""
        from callwarden.server import daemon_client

        def _new_client():
            return daemon_client.HttpDaemonRpcClient.__new__(
                daemon_client.HttpDaemonRpcClient
            )

        explicit = _new_client()
        explicit._manifest = {"bind_scope": "remote"}
        assert explicit.is_remote_profile() is True
        assert explicit.bind_scope() == "remote"

        loopback = _new_client()
        loopback._manifest = {"bind_scope": "loopback"}
        assert loopback.is_remote_profile() is False

        missing = _new_client()
        missing._manifest = {"version": 2}
        assert missing.is_remote_profile() is False, "缺失 bind_scope 回滚 loopback"

        # _manifest 未缓存时走 resolve_http_endpoint_and_manifest(validate=False)
        resolving = _new_client()
        monkeypatch.setattr(
            daemon_client, "resolve_http_endpoint_and_manifest",
            lambda **kw: (None, {"bind_scope": "remote"}),
        )
        assert resolving.is_remote_profile() is True
        assert resolving._manifest == {"bind_scope": "remote"}, "解析结果应缓存"
        # 缓存 manifest 但不得据此认为 endpoint 已发现
        assert getattr(resolving, "_resolved_endpoint", "unset") == "unset"

        broken = _new_client()
        monkeypatch.setattr(
            daemon_client, "resolve_http_endpoint_and_manifest",
            lambda **kw: (_ for _ in ()).throw(OSError("manifest 缺失")),
        )
        assert broken.is_remote_profile() is False, "解析失败 fail-closed 回滚"


# ----------------------------------------------------------------------
# S2-08：跨 OS 只读矩阵（env 门控，对侧 OS daemon）
# ----------------------------------------------------------------------

@requires_remote_daemon
class TestS208CrossOsReadOnlyMatrix:
    """S2-08：Windows 客户端 → 对侧 OS（WSL/Linux）daemon 的只读链路。

    启用方式（在 Windows 端）：
      set CW_S2_REMOTE_ENDPOINT=https://<对侧 IP>:4014
      set CW_S2_REMOTE_CERT=C:\\path\to\\client.crt
      set CW_S2_REMOTE_KEY=C:\\path\to\\client.key
      set CW_S2_REMOTE_CA=C:\\path\to\remote-daemon-ca.crt
      set CW_S2_REMOTE_ROOT=C:\\path\to\\client\\workspace
    对侧 daemon 需 bind_scope=remote（manifest v2）。反方向（Linux 客户端
    → Windows daemon）由 CI 矩阵在对侧 OS 上以同款 env 运行本文件承载。
    """

    def _make_remote_client(self, monkeypatch, root):
        import ssl

        from callwarden.server import daemon_client
        from callwarden.server.daemon_client import HttpDaemonRpcClient

        # 必须走真实构造函数：用 __new__ 绕过 __init__ 会缺 _resolved_endpoint /
        # _project_root / _remote_workspace_id 等状态，实机路径（workspace.register）
        # 必然 AttributeError。构造后直接注入 discovery 结果，跳过 loopback 发现。
        client = HttpDaemonRpcClient()
        client.configure_workspace(root)
        client._manifest = {
            "bind_scope": "remote",
            "endpoint": _REMOTE_ENDPOINT,
            "protocol_version": 2,
        }
        client._explicit_endpoint = _REMOTE_ENDPOINT
        client._resolved_endpoint = _REMOTE_ENDPOINT
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(
            os.environ["CW_S2_REMOTE_CERT"], os.environ["CW_S2_REMOTE_KEY"]
        )
        ctx.load_verify_locations(os.environ["CW_S2_REMOTE_CA"])
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        client._ssl_ctx = ctx
        monkeypatch.setattr(
            daemon_client, "_get_rpc_client_for_route", lambda: client,
        )
        # route_rpc（query.stats 等）走 HttpDaemonRpcClient.get_instance() 单例，
        # 不走 _get_rpc_client_for_route；不钉住单例会另建无 manifest 的 client
        # 并在 discover 处 E_HTTP_MANIFEST_MISSING。
        monkeypatch.setattr(
            HttpDaemonRpcClient, "get_instance", lambda: client,
        )
        return client

    def test_s2_08_read_list_upload_roundtrip(self, tmp_path, monkeypatch):
        from callwarden.server import daemon_client
        from callwarden.server.tools import tools_workspace

        root = os.environ.get("CW_S2_REMOTE_ROOT", os.getcwd())
        probe = os.path.join(root, "s2_08_probe.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("def probe():\n    return 's2-08'\n")
        try:
            client = self._make_remote_client(monkeypatch, root)
            monkeypatch.setattr(
                tools_workspace, "_remote_client", lambda: client
            )
            mcp = _CaptureMCP()
            tools_workspace.register(mcp)
            tools = mcp._tools

            # 只读链路 1：file.read 客户端本机执行（对侧 daemon 零文件打开）
            result = tools["file_read"]("s2_08_probe.py")
            assert result["exec_origin"] == "client"
            assert "def probe():" in result["content"]

            # 只读链路 2：file.list 裸数组
            rows = tools["file_list"]()
            assert isinstance(rows, list)
            assert any(r["rel_path"] == "s2_08_probe.py" for r in rows)

            # 只读链路 3：build_graph → snapshot.upload 发布到对侧 daemon
            # （S2-03 链路：客户端本机构建 SQLite 字节经 mTLS 上传）
            built = tools["build_graph"]()
            assert built.get("exec_origin") == "client"
            assert built.get("ok") is True
            assert built["scanned"] >= 1

            # 发布后索引可查：query.stats 经 route_rpc remote 守卫注入
            # workspace_instance_id 后由对侧 daemon 解析
            stats = daemon_client.route_rpc(
                "query.stats", {"limit": 5}, "READ_ONLY"
            )
            assert isinstance(stats, dict)
        finally:
            try:
                os.unlink(probe)
            except OSError:
                pass
