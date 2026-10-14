"""TP-11W watcher/dirty overlay/增量环境 harness：复用 TP-11 底座 + 文件面助手。

在完全隔离的 daemon 上验证增量（dirty overlay）语义：

- W1 增量刷新：改文件内容 → workspace.file.refresh_file → 读面反映新内容
  （dirty overlay 生效，不需要全量重建）；
- W2 文件移除：workspace.file.remove → 文件从索引摘除；
- W3 目录构建：workspace.build_directory 增量构建子目录；
- W4 文件读面：file.read / file.list / file.grep / file.symbol_content /
  file.health 可达且结构完整。

🔴 全部 fs 方法走收敛 RPC 分支，需 workspace_instance_id，且写面需要
snapshot 就绪（open_codegraph_db_write 的 snapshot 门 + owned_workspace ACL）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
_ISO_DIR = _HERE.parent / "isolation"
for _d in (str(_HERE), str(_GOV_DIR), str(_ISO_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# 复用 TP-11I 底座（GovernanceEnv + run 根自清洁 wipe_run_root）
from isolation_harness import IsolationEnv  # noqa: E402

# TP-11W 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_INCR = Path(
    os.environ.get("CW_TP11W_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11W"))


class IncrementalEnv(IsolationEnv):
    """TP-11W 增量执行环境：TP-11 底座 + 文件/构建面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11w-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_INCR,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    def _ws(self, instance_id: str = "") -> str:
        return instance_id or self.setup_workspace()[1]

    # ---------- fixture：工作区内文件增删 ----------

    def write_ws_file(self, rel_path: str, content: str) -> Path:
        """在隔离 workspace 内写文件（返回绝对路径）。"""
        p = self.workspace_dir / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read_ws_file(self, rel_path: str) -> str:
        return (self.workspace_dir / rel_path).read_text(encoding="utf-8")

    def delete_ws_file(self, rel_path: str) -> bool:
        p = self.workspace_dir / rel_path
        if p.is_file():
            p.unlink()
            return True
        return False

    # ---------- 路径归一 ----------
    #
    # 🔴 实测契约（TP-11W 探针，非猜测）：workspace.file.read / refresh_file /
    # remove / file.health 一律按「绝对路径」解析，传相对路径会 path_not_found
    # （报的是原样字符串，不做相对 workspace 拼接）。refresh_file 成功后回显的
    # file_path 反而是相对路径 —— 入参与出参语义不对称，这里统一在 harness 层
    # 把相对入参拼成绝对，避免测试里到处手写 abs。

    def abs_path(self, rel_path: str) -> str:
        """把工作区相对路径拼成绝对路径（已是绝对则原样返回）。

        rel_path 为空串时返回 workspace 根 —— build_directory 之类目录级入口
        传空串会直接 path_not_found。
        """
        p = Path(rel_path)
        if p.is_absolute():
            return str(p)
        if rel_path == "":
            return str(self.workspace_dir.resolve())
        return str((self.workspace_dir / p).resolve())

    # ---------- 文件/构建面 ----------

    def fs_call(self, method: str, **extra: Any) -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": self._ws()}
        params.update(extra)
        return self.rpc(method, params)

    def refresh_file(self, rel_path: str, **extra: Any) -> Any:
        return self.fs_call("workspace.file.refresh_file",
                            file_path=self.abs_path(rel_path), **extra)

    def remove_file(self, rel_path: str, **extra: Any) -> Any:
        return self.fs_call("workspace.file.remove",
                            file_path=self.abs_path(rel_path), **extra)

    def file_read(self, rel_path: str, **extra: Any) -> Any:
        return self.fs_call("workspace.file.read",
                            file_path=self.abs_path(rel_path), **extra)

    # 🔴 file.list 的参数名是 path / glob（不是 directory）—— 传错名字会被静默
    # 忽略、回全量列表，极易误判成「子目录过滤失效」缺陷。path 走
    # validate_owned_path + 根内校验，越界返回 path_escape。
    def file_list(self, rel_dir: str = "", glob: str = "") -> Any:
        params: Dict[str, Any] = {}
        if rel_dir:
            params["path"] = self.abs_path(rel_dir)
        if glob:
            params["glob"] = glob
        return self.fs_call("workspace.file.list", **params)

    def file_grep(self, pattern: str, **extra: Any) -> Any:
        return self.fs_call("workspace.file.grep", pattern=pattern, **extra)

    def file_health(self, rel_path: str = "") -> Any:
        """file.health 文件健康检查（exists/size/mtime/readable）。

        TP-11A D4 修复后：文件按合同可用（此前 require_file 误用导致传文件
        恒报"不是目录"）；目录仍被忍受；越根一律 path_escape。
        """
        return self.fs_call("workspace.file.health",
                            file_path=self.abs_path(rel_path) if rel_path else "")

    # 🔴 symbol_content 是 fs 面唯一吃「相对路径」的方法：SQL 里按
    # fi.rel_path 精确匹配（fs_handlers.rs:930），传绝对路径查不到。
    def file_symbol_content(self, rel_path: str, symbol_name: str) -> Any:
        return self.fs_call("workspace.file.symbol_content",
                            file_path=rel_path, symbol_name=symbol_name)

    # 🔴 build_directory 的参数名是 dir_path（不是 directory），且同样要绝对路径：
    # 传相对路径会相对 daemon CWD 解析 → 落在 workspace 外 → path_escape
    # （TP-11A 修复前是静默 scanned=0，极易误判为「构建成功但没文件」）。
    def build_directory(self, rel_dir: str = "", recursive: bool = True,
                        **extra: Any) -> Any:
        return self.fs_call("workspace.build_directory",
                            dir_path=self.abs_path(rel_dir) if rel_dir else "",
                            recursive=recursive, **extra)

    def build_graph(self, **extra: Any) -> Any:
        return self.fs_call("workspace.build_graph", **extra)
