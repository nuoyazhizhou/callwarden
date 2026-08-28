"""Stage_Toggle 迁移脚本（Req 13.19）。

当 Daemon-owned 配置存储对某 workspace 可用时，将 Experiment_Batch_Config 中
记录的 P0 Stage_Toggle 值迁移到 daemon 配置存储，保留原始作用域，
并记录迁移 actor 与 Authoritative_Clock 时间。

用法：
    python server/stage_toggle_migration.py [--db PATH] [--dry-run]

迁移语义（Req 13.19）：
- 保留每条 P0 Stage_Toggle 的原始作用域（global/workspace/task）
- 不重置为默认 disabled
- 记录 migration actor 和 Authoritative_Clock 迁移时间
- 迁移后 Experiment_Batch_Config 中的 P0 值仅作审计历史（Req 13.20）
"""

import json
import os
import sys
import time
from pathlib import Path


MIGRATE_METHOD = "mcp.stage_toggle_migration.migrate_p0_toggles"


def _call_daemon_rpc(method: str, params: dict) -> object:
    """通过统一 HTTP client 调用 daemon，不打开本地 authority 数据库。"""
    from callwarden.server._mcp_common import _call_daemon_rpc as rpc

    return rpc(method, params)


def find_experiment_batch_configs(workspace_root: str) -> Path | None:
    """查找 workspace 的 Experiment_Batch_Config 文件。"""
    candidates = [
        Path(workspace_root) / ".callwarden" / "experiment_batch_config.json",
        Path(workspace_root) / "experiment_batch_config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def load_p0_toggles_from_config(config_path: Path) -> list[dict]:
    """从 Experiment_Batch_Config 读取 P0 Stage_Toggle 记录。

    返回 [{"scope_key": "global"|"workspace:<id>"|"task:<id>", "enabled": bool, "actor": str, "changed_at": int}]
    """
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    toggles = []
    # 支持全局 P0 toggle
    if "p0_enabled" in data:
        toggles.append({
            "scope_key": "global",
            "enabled": bool(data["p0_enabled"]),
            "actor": data.get("p0_actor", "migration"),
            "changed_at": data.get("p0_changed_at", int(time.time() * 1000)),
        })

    # 支持 workspace 级 P0 toggle
    for ws_id, ws_cfg in data.get("workspaces", {}).items():
        if "p0_enabled" in ws_cfg:
            toggles.append({
                "scope_key": f"workspace:{ws_id}",
                "enabled": bool(ws_cfg["p0_enabled"]),
                "actor": ws_cfg.get("p0_actor", "migration"),
                "changed_at": ws_cfg.get("p0_changed_at", int(time.time() * 1000)),
            })

    # 支持 task 级 P0 toggle
    for task_id, task_cfg in data.get("tasks", {}).items():
        if "p0_enabled" in task_cfg:
            toggles.append({
                "scope_key": f"task:{task_id}",
                "enabled": bool(task_cfg["p0_enabled"]),
                "actor": task_cfg.get("p0_actor", "migration"),
                "changed_at": task_cfg.get("p0_changed_at", int(time.time() * 1000)),
            })

    return toggles


def migrate_p0_toggles(
    db_path: str,
    toggles: list[dict],
    migration_actor: str = "stage_toggle_migration",
    dry_run: bool = False,
) -> int:
    """将 P0 toggle 迁移到 daemon 配置存储。

    返回迁移的记录数。
    """
    if not toggles:
        return 0

    result = _call_daemon_rpc(MIGRATE_METHOD, {
        "db_path": db_path,
        "toggles": toggles,
        "migration_actor": migration_actor,
        "dry_run": dry_run,
    })
    if not isinstance(result, dict) or "migrated_count" not in result:
        raise RuntimeError(f"Stage_Toggle migration RPC returned invalid result: {result!r}")
    count = int(result["migrated_count"])
    if dry_run:
        for toggle in toggles:
            print(f"  [dry-run] P0 scope={toggle['scope_key']} enabled={toggle['enabled']}")
    return count


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Stage_Toggle P0 迁移（Req 13.19）")
    parser.add_argument("--db", default=None, help="daemon 配置存储路径")
    parser.add_argument("--workspace", default=".", help="workspace 根目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    args = parser.parse_args()

    # 确定 daemon 配置存储路径
    db_path = args.db
    if db_path is None:
        home = Path.home()
        db_path = str(home / ".callwarden" / "daemon_config.db")

    # 查找 Experiment_Batch_Config
    config_path = find_experiment_batch_config(args.workspace)
    if config_path is None:
        print("未找到 Experiment_Batch_Config，无需迁移。")
        return

    print(f"找到配置: {config_path}")
    toggles = load_p0_toggles_from_config(config_path)
    if not toggles:
        print("配置中无 P0 Stage_Toggle 记录，无需迁移。")
        return

    print(f"发现 {len(toggles)} 条 P0 Stage_Toggle 记录")
    count = migrate_p0_toggles(db_path, toggles, dry_run=args.dry_run)
    print(f"迁移完成: {count} 条记录写入 {db_path}")


if __name__ == "__main__":
    main()
