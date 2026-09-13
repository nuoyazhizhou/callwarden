"""pytest 全局配置：测试数据库隔离（防止污染 ~/.callwarden/callwarden.db）"""
import pytest

from callwarden import config as _cw_config
from callwarden.db import db_base as _db_base


@pytest.fixture(scope="module")
def w3_live(tmp_path_factory):
    """W3 家族共享隔离 daemon 权威栈（模块级，退出 kill proc）。

    统一「需 live daemon」测试的建栈方式（模式A USERPROFILE 重定向 + workspace.register
    + task-DB seed + 空 codegraph snapshot.publish），不依赖后台常驻 daemon（W8 不稳）。
    返回 dict：client / inst(workspace_instance_id) / endpoint / proc / data_root。
    """
    from _w3_harness import setup_w3_client

    data_root = str(tmp_path_factory.mktemp("w3_live_"))
    root_path = data_root + "/repo"
    client, inst, endpoint, proc = setup_w3_client(data_root, root_path, ws_id=1)
    if proc is None:
        pytest.skip("未找到隔离 cw-daemon 二进制，跳过 live 用例")
    try:
        yield {"client": client, "inst": inst, "endpoint": endpoint, "proc": proc,
               "data_root": data_root}
    finally:
        proc.kill()


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """自动隔离测试数据库路径，避免污染 ~/.callwarden/callwarden.db"""
    def _fake_get_project_db_path(project_root: str = "") -> str:
        return str(tmp_path / "test_isolated.db")

    monkeypatch.setattr(_cw_config, "get_project_db_path", _fake_get_project_db_path)
    monkeypatch.setattr(_db_base, "get_project_db_path", _fake_get_project_db_path)
    # 隔离 CALLWARDEN_DIR：manifest / registry / dedup 换到每测试独立 tmp 目录，
    # 避免连续/并发测试共享真实 HOME manifest 被写空 → JSONDecodeError →
    # E_HTTP_MANIFEST_STALE（W4，实测复现）。
    _isolated_dir = tmp_path / "callwarden_dir"
    _isolated_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_cw_config, "CALLWARDEN_DIR", str(_isolated_dir))
    yield


class RouteStub:
    """`route_task_read` / `route_task_write` 的可编程替身。

    daemon authority 化（RP-09）后 CLI 的任务读写不再直连本地 DB，而是经
    ``route_task_read`` / ``route_task_write`` 走 daemon RPC。单测用本替身记录
    ``(kind, method, params)`` 调用契约并按 method 回放预设 payload，即可在无
    daemon 环境下断言「CLI 走对了 RPC 且渲染了 daemon 回包」这一现代语义。

    默认**不**执行 fallback（纯 RPC 模式）；需要验证 legacy 本地回落时显式设置
    ``use_fallback = True``。
    """

    def __init__(self) -> None:
        self.calls: list = []
        self.use_fallback = False
        self._replies: dict = {}

    # ---- 配置 ----
    def reply(self, method: str, payload):
        """为某个 RPC method 预设回包。"""
        self._replies[method] = payload
        return self

    # ---- 查询 ----
    @property
    def methods(self) -> list:
        return [c[1] for c in self.calls]

    def last_params(self, method: str):
        for _kind, m, params in reversed(self.calls):
            if m == method:
                return params
        return None

    def count(self, method: str) -> int:
        return self.methods.count(method)

    # ---- 替身实现 ----
    def _dispatch(self, kind: str, method: str, params, fallback):
        self.calls.append((kind, method, dict(params or {})))
        if method in self._replies:
            return self._replies[method]
        if self.use_fallback and callable(fallback):
            return fallback()
        return {}

    def read(self, method, params=None, fallback=None):
        return self._dispatch("read", method, params, fallback)

    def write(self, method, params=None, fallback=None):
        return self._dispatch("write", method, params, fallback)


@pytest.fixture
def route_stub(monkeypatch):
    """安装 route_task_read/route_task_write 替身（daemon authority 语义）。"""
    from callwarden.cli import main as _cli_main

    stub = RouteStub()
    monkeypatch.setattr(_cli_main, "route_task_read", stub.read, raising=False)
    monkeypatch.setattr(_cli_main, "route_task_write", stub.write, raising=False)
    return stub

