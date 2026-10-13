"""TP-11E 编辑/hash/并发冲突/回滚环境 harness：复用 TP-11 底座 + 编辑面助手。

在完全隔离的 daemon 上验证编辑审计面：

- E1 提案生命周期：edit.propose / propose_range_patch / propose_symbol_patch /
  propose_symbol_id_patch → file_edit_audit 写入，status='proposed'；
- E2 回滚语义：edit.revert → 'reverted'；**重复 revert 报 edit_not_found**
  （不是静默成功，WHERE status != 'reverted'）；不存在的 edit_id 同错；
- E3 隔离与 fail-closed：revert 带他 workspace 的 edit_id → edit_not_found
  （WHERE workspace_id = ?）；缺必填参数 → invalid_params；
- E4 符号补丁：符号不存在 → symbol_not_found（不静默建空编辑）；
- E5 守卫面：guardrail_check_edit、edit.stats。

🔴 全部编辑方法均要求 workspace_instance_id 并走 open_codegraph_db_write
（snapshot 门 + owned_workspace ACL），故读/写前须先建图 + 发布 snapshot。
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

# TP-11E 独立 run 根（不复用其他域默认值，避免套件互踩）
DEFAULT_RUN_ROOT_EDIT = Path(
    os.environ.get("CW_TP11E_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11E"))


class EditingEnv(IsolationEnv):
    """TP-11E 编辑执行环境：TP-11 底座 + 编辑面 RPC 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11e-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_EDIT,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- 提案面 ----------

    def _ws(self, instance_id: str = "") -> str:
        return instance_id or self.setup_workspace()[1]

    def propose_edit(self, file_path: str, new_content: str,
                     operation: str = "", instance_id: str = "") -> Any:
        """edit.propose：file_path + new_content 必填。"""
        params: Dict[str, Any] = {
            "workspace_instance_id": self._ws(instance_id),
            "file_path": file_path,
            "new_content": new_content,
        }
        if operation:
            params["operation"] = operation
        return self.rpc("edit.propose", params)

    def propose_range_patch(self, file_path: str, new_content: str,
                            start_line: int = 1, end_line: int = 1,
                            instance_id: str = "") -> Any:
        return self.rpc("edit.propose_range_patch", {
            "workspace_instance_id": self._ws(instance_id),
            "file_path": file_path,
            "new_content": new_content,
            "start_line": start_line,
            "end_line": end_line,
        })

    def propose_symbol_patch(self, qualified_name: str, new_content: str,
                             instance_id: str = "") -> Any:
        return self.rpc("edit.propose_symbol_patch", {
            "workspace_instance_id": self._ws(instance_id),
            "qualified_name": qualified_name,
            "new_content": new_content,
        })

    def propose_symbol_id_patch(self, symbol_id: int, new_content: str,
                                instance_id: str = "") -> Any:
        return self.rpc("edit.propose_symbol_id_patch", {
            "workspace_instance_id": self._ws(instance_id),
            "symbol_id": symbol_id,
            "new_content": new_content,
        })

    # ---------- 回滚面 ----------

    def revert_edit(self, edit_id: int, instance_id: str = "") -> Any:
        return self.rpc("edit.revert", {
            "workspace_instance_id": self._ws(instance_id),
            "edit_id": edit_id,
        })

    # ---------- 守卫 / 统计 ----------

    def guardrail_check_edit(self, file_path: str, new_content: str,
                             instance_id: str = "", **extra: Any) -> Any:
        params: Dict[str, Any] = {
            "workspace_instance_id": self._ws(instance_id),
            "file_path": file_path,
            "new_content": new_content,
        }
        params.update(extra)
        return self.rpc("guardrail_check_edit", params)

    def edit_stats(self, instance_id: str = "") -> Any:
        return self.rpc("edit.stats",
                        {"workspace_instance_id": self._ws(instance_id)})
