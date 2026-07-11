"""
Phase 7.0: JobExecutor 单例

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

MCP server 内共享一个 JobExecutor 实例。
首次调用时启动 executor，注册默认 handlers。
"""

from __future__ import annotations

import threading
from typing import Optional

from .job_executor import JobExecutor
from .job_handlers import register_default_handlers


_executor_lock = threading.Lock()
_executor_instance: Optional[JobExecutor] = None


def get_job_executor(db_path: str, workspace_root: str = "") -> JobExecutor:
    """获取 JobExecutor 单例

    首次调用时启动 executor，注册默认 handlers。

    参数：
        db_path: SQLite 数据库路径
        workspace_root: workspace 根路径（当前未使用，预留）

    返回：JobExecutor 实例（已启动）
    """
    global _executor_instance
    if _executor_instance is not None:
        return _executor_instance

    with _executor_lock:
        if _executor_instance is not None:
            return _executor_instance

        executor = JobExecutor(
            db_path=db_path,
            max_concurrent_jobs=2,
            max_duration_seconds=1800,  # 30 分钟
        )
        register_default_handlers(executor)
        executor.start()
        _executor_instance = executor
        return _executor_instance


def shutdown_job_executor() -> None:
    """关闭 JobExecutor 单例（用于进程退出）"""
    global _executor_instance
    if _executor_instance is not None:
        _executor_instance.stop(wait=False)
        _executor_instance = None
