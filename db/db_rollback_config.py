"""
db_rollback_config.py
=====================

迁移回滚配置 Mixin（Rollback Config Mixin）。

全量 Rust 迁移自举计划使用：每个功能子任务在 wire-production step
必须登记一条 rollback_config 记录，声明生产入口、回滚入口和回滚窗口。

事实层由 rollback_config 表（schema v42）表达；
本模块提供注册/查询/设置回滚标志四个核心方法。

设计原则：
- 只读方法（get/list/is_feature_rolled_back）可走 MCP（WAL 安全）
- 写方法（register/set_flag）走 CLI（避免与 MCP 长连接撞锁）
- rollback_flag=1 时生产入口走 rollback_entry（切回 Python）
- rollback_window_until 过期后 Phase 7 删除 rollback_entry
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


class RollbackConfigMixin:
    """迁移回滚配置 Mixin

    提供迁移功能子任务的回滚配置注册、查询和紧急回滚开关能力。
    与 TaskMixin（任务状态机）协作：wire-production step 完成后调用
    register_rollback_config 登记回滚路径。
    """

    def register_rollback_config(
        self,
        task_id: str,
        feature_name: str,
        phase: int,
        production_entry: str,
        rollback_entry: str,
        rollback_window_until: str = "",
        config_blob: Any = None,
    ) -> Dict[str, Any]:
        """注册一条 rollback_config 记录

        在功能子任务的 wire-production step 完成后调用，声明该功能的
        生产入口、回滚入口和回滚窗口。若同一 task_id 已有记录，更新之。

        Args:
            task_id: 关联的迁移子任务 ID（必填）
            feature_name: 功能名称（必填，如 "rust_sqlite_connection"）
            phase: 所属阶段（1-7）
            production_entry: 生产入口路径（必填，如 "db/db_base.py:CodeGraphDB._connect"）
            rollback_entry: 回滚入口路径（必填，切回 Python 的代码位置）
            rollback_window_until: 回滚窗口有效期（ISO8601），过期后回滚入口将被删除
            config_blob: 额外配置（dict，如 {"flag": "CW_USE_RUST_SQLITE"}）

        Returns:
            {success, id, action} 或 {success: False, error: ...}
        """
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        if not feature_name:
            return {"success": False, "error": "feature_name is required"}
        if not production_entry:
            return {"success": False, "error": "production_entry is required"}
        if not rollback_entry:
            return {"success": False, "error": "rollback_entry is required"}

        # 序列化 config_blob
        if config_blob is None:
            config_blob_str = ""
        elif isinstance(config_blob, str):
            config_blob_str = config_blob
        else:
            try:
                config_blob_str = json.dumps(config_blob, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                config_blob_str = str(config_blob)

        ws_id = self._get_active_workspace_id() if hasattr(self, "_get_active_workspace_id") else None
        now = time.time()

        try:
            # 检查是否已有记录（按 task_id 查询）
            cur = self.conn.execute(
                "SELECT id, rollback_flag FROM rollback_config WHERE task_id = ?",
                (task_id,),
            )
            existing = cur.fetchone()

            if existing:
                # 更新现有记录（保留 rollback_flag 不变，需通过 set_rollback_flag 修改）
                self.conn.execute(
                    """
                    UPDATE rollback_config
                    SET feature_name = ?, phase = ?, production_entry = ?,
                        rollback_entry = ?, rollback_window_until = ?,
                        config_blob = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (feature_name, phase, production_entry, rollback_entry,
                     rollback_window_until, config_blob_str, now, task_id),
                )
                self.conn.commit()
                return {
                    "success": True,
                    "id": existing["id"],
                    "action": "updated",
                    "task_id": task_id,
                    "rollback_flag": existing["rollback_flag"],
                }
            else:
                # 插入新记录
                cur = self.conn.execute(
                    """
                    INSERT INTO rollback_config
                        (workspace_id, task_id, feature_name, phase,
                         production_entry, rollback_entry, rollback_flag,
                         rollback_window_until, config_blob,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (ws_id, task_id, feature_name, phase,
                     production_entry, rollback_entry,
                     rollback_window_until, config_blob_str,
                     now, now),
                )
                self.conn.commit()
                return {
                    "success": True,
                    "id": cur.lastrowid,
                    "action": "inserted",
                    "task_id": task_id,
                    "rollback_flag": 0,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_rollback_config(self, task_id: str) -> Optional[Dict[str, Any]]:
        """查询单个任务的回滚配置

        Args:
            task_id: 任务 ID（必填）

        Returns:
            rollback_config dict 或 None（未找到时）
        """
        if not task_id:
            return None

        try:
            cur = self.conn.execute(
                "SELECT * FROM rollback_config WHERE task_id = ?",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            # 反序列化 config_blob
            if result.get("config_blob"):
                try:
                    result["config_blob"] = json.loads(result["config_blob"])
                except (json.JSONDecodeError, TypeError):
                    pass  # 保留原始字符串
            return result
        except Exception:
            return None

    def list_rollback_configs(
        self,
        phase: int = 0,
        rollback_flag: int = -1,
    ) -> List[Dict[str, Any]]:
        """批量查询回滚配置

        Args:
            phase: 阶段过滤（0=所有阶段，>0=指定阶段）
            rollback_flag: 回滚标志过滤（-1=所有，0=正常，1=已回滚）

        Returns:
            rollback_config dict 列表，按 phase, feature_name 升序
        """
        sql = "SELECT * FROM rollback_config WHERE 1=1"
        params: List[Any] = []

        if phase > 0:
            sql += " AND phase = ?"
            params.append(phase)

        if rollback_flag >= 0:
            sql += " AND rollback_flag = ?"
            params.append(rollback_flag)

        sql += " ORDER BY phase ASC, feature_name ASC"

        try:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
            results = []
            for row in rows:
                item = dict(row)
                if item.get("config_blob"):
                    try:
                        item["config_blob"] = json.loads(item["config_blob"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(item)
            return results
        except Exception:
            return []

    def set_rollback_flag(
        self,
        task_id: str,
        flag: int,
        reason: str = "",
    ) -> Dict[str, Any]:
        """设置回滚标志（写操作，走 CLI）

        flag=1 时生产入口应走 rollback_entry（切回 Python）。
        flag=0 时恢复正常 Rust 路径。

        Args:
            task_id: 任务 ID（必填）
            flag: 回滚标志（0=正常 Rust，1=已回滚到 Python）
            reason: 回滚原因（记录到 audit_chain）

        Returns:
            {success, task_id, feature_name, rollback_flag, previous_flag}
        """
        if not task_id:
            return {"success": False, "error": "task_id is required"}
        if flag not in (0, 1):
            return {"success": False, "error": "flag must be 0 or 1"}

        now = time.time()

        try:
            cur = self.conn.execute(
                "SELECT id, feature_name, rollback_flag FROM rollback_config WHERE task_id = ?",
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "success": False,
                    "error": t(
                        "cli.messages.rollback_config_not_found",
                        default="rollback_config not found for task_id={task_id}",
                        task_id=task_id,
                    ),
                }

            previous_flag = row["rollback_flag"]
            feature_name = row["feature_name"]

            if previous_flag == flag:
                return {
                    "success": True,
                    "task_id": task_id,
                    "feature_name": feature_name,
                    "rollback_flag": flag,
                    "previous_flag": previous_flag,
                    "note": "flag unchanged",
                }

            self.conn.execute(
                "UPDATE rollback_config SET rollback_flag = ?, updated_at = ? WHERE task_id = ?",
                (flag, now, task_id),
            )
            self.conn.commit()

            # 写入审计链（fail-soft）
            if hasattr(self, "sign_audit_record"):
                try:
                    self.sign_audit_record(
                        "rollback_config",
                        str(row["id"]),
                        {
                            "task_id": task_id,
                            "feature_name": feature_name,
                            "operation": "set_rollback_flag",
                            "previous_flag": previous_flag,
                            "new_flag": flag,
                            "reason": reason or "unspecified",
                            "timestamp": now,
                        },
                        operation="update",
                    )
                except Exception:
                    pass  # fail-soft

            return {
                "success": True,
                "task_id": task_id,
                "feature_name": feature_name,
                "rollback_flag": flag,
                "previous_flag": previous_flag,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def is_feature_rolled_back(self, feature_name: str) -> bool:
        """生产入口快速查询：功能是否已回滚（只读，可走 MCP）

        生产入口在每次调用前检查此方法，若返回 True 则走 rollback_entry。

        Args:
            feature_name: 功能名称（必填）

        Returns:
            True 表示该功能已回滚到 Python（rollback_flag=1）
        """
        if not feature_name:
            return False

        try:
            cur = self.conn.execute(
                "SELECT rollback_flag FROM rollback_config WHERE feature_name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (feature_name,),
            )
            row = cur.fetchone()
            if not row:
                return False
            return row["rollback_flag"] == 1
        except Exception:
            return False
