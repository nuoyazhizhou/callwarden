"""
db_gc.py
========

代码图谱 GC（Garbage Collection）Mixin。

类 Java GC 的分代回收机制：
- 新生代（Young Gen）= file_instances 中 status='active'/'pending' 的活跃文件
- 老年代（Old Gen）= archived_files 表，被 .gitignore/.callwardenignore 命中的文件迁入
- GC 触发点：build 完成后自动调用 gc_archive（类 Young GC）
- Full GC：手动调用 gc_archive(force=True) 完整扫描所有已入库文件
- 复活（Promotion Demotion）：取消 ignore 后调用 gc_restore 把文件迁回主表

归档策略（最小侵入）：
- 不删除 file_instances 行（保留 ID 稳定性），只把 status 改为 'archived'
- 不删除 symbols/calls（保留快照供复活时恢复）
- 在 archived_files 表记录归档元数据（路径、hash、符号数、归档原因）
- 查询接口默认过滤 status != 'archived' 的文件（避免归档文件污染结果）

依赖：
- file_instances 表（status 字段）
- archived_files 表（v14 新增）
- IgnoreMatcher（analyzers/ignore_spec.py）
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional

from ..analyzers.ignore_spec import IgnoreMatcher
from ..cli.console import cprint
from ..config import norm_path


# 默认硬编码 ignore 规则（与 db_build.py 保持一致，供 GC 复用）
# 注意：这些规则也作为 GC 的"基线"，已归档的文件即使取消 .gitignore 中的规则，
# 默认规则命中的文件仍然保持归档（除非 force=True 且明确要复活）
DEFAULT_IGNORE_RULES: List[str] = [
    # VCS / 包管理 / Python 虚拟环境
    ".git/", "node_modules/", ".next/",
    "__pycache__/", ".venv/", "venv/", "env/", ".tox/", "*.egg-info/",
    # 构建输出目录
    "target/", "dist/", "build/", "out/", "output/", "outputs/",
    "obj/", "bin/", "rootfs/", "staging/", "sysroot/", "ccache/",
    # 预构建 / 二进制 / 工具链
    "prebuilt/", "prebuilts/", "blob/", "toolchain/", "toolchains/",
    "ndk/", "jdk/",
    # autogen 代码目录
    "autogen/", "auto_gen/", "generated/", "gen/", "generated_src/",
    "proto_gen/", "protobuf_gen/", "grpc_gen/", "moc/",
    # autogen 文件名模式
    "*.pb.cc", "*.pb.h", "*.pb.go",
    "*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py",
    "*.grpc.cc", "*.grpc.h",
    "moc_*.cpp", "ui_*.h", "qrc_*.cpp",
    "*.pyc", "*.pyo",
    # repo 工具元数据
    ".repo/",
]

DEFAULT_GC_POLICY: Dict[str, Any] = {
    "older_than_days": 365,
    "keep_versions": 100,
    "include_external": False,
    "external_stale_days": 365,
    "backup_enabled": True,
    "vacuum_enabled": False,
}


class GCMixin:
    """代码图谱 GC Mixin

    通过 self.conn 访问数据库连接，提供归档/复活/状态查询能力。
    集成到 CodeGraphDB 主类。
    """

    def _build_ignore_matcher(self) -> IgnoreMatcher:
        """构建忽略规则匹配器

        合并规则来源（按优先级，后者覆盖前者）：
        1. 默认硬编码规则（DEFAULT_IGNORE_RULES）
        2. workspace 根目录的 .gitignore
        3. workspace 根目录的 .callwardenignore
        4. 子目录的 .gitignore（按路径深度应用）

        Returns:
            配置好的 IgnoreMatcher 实例
        """
        matcher = IgnoreMatcher(self.workspace_root)
        # 默认规则作为基线
        matcher.add_default_rules(DEFAULT_IGNORE_RULES)
        # 加载 workspace 的 .gitignore / .callwardenignore
        matcher.load_workspace_ignores()
        return matcher

    def gc_archive(self, force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        """扫描已入库文件，把命中 ignore 规则的迁到 archived_files

        类 Java Young GC：扫描新生代（active 文件），晋升到老年代（archived）。

        归档策略（释放空间，复活时重新解析）：
        - 保留 file_instances 行（status='archived'，保留 ID 稳定性）
        - 删除 symbols / calls / file_versions / file_symbol_versions（释放空间）
        - 在 archived_files 表记录归档元数据（路径、hash、符号数、归档原因）
        - 查询接口 JOIN file_instances 时自然过滤（status='archived' 不参与业务查询）
        - 复活时仅改 status='active'，下次 build 自动重新解析

        Args:
            force: True=扫描所有 active 文件（Full GC）；False=只扫描最近 build 新增的
            dry_run: True=只统计不实际归档（预演）

        Returns:
            {
                "scanned": int,       # 扫描的文件数
                "archived": int,      # 归档的文件数
                "skipped": int,       # 已归档跳过的文件数
                "reasons": dict,      # {归档原因: 文件数}
                "dry_run": bool,
            }
        """
        matcher = self._build_ignore_matcher()
        ws_id = self._get_active_workspace_id()

        # 查询所有 active 文件（force=True）或仅 pending 状态（增量 GC）
        # 增量模式：只扫描 status='pending' 的文件（最近 build 新增或更新的）
        # 完整模式：扫描所有 status != 'archived' 且 != 'deleted' 的文件
        if force:
            cur = self.conn.execute(
                """SELECT id, workspace_id, rel_path, abs_path, current_content_hash, status
                   FROM file_instances
                   WHERE workspace_id = ? AND status NOT IN ('archived', 'deleted')""",
                (ws_id,),
            )
        else:
            cur = self.conn.execute(
                """SELECT id, workspace_id, rel_path, abs_path, current_content_hash, status
                   FROM file_instances
                   WHERE workspace_id = ? AND status = 'pending'""",
                (ws_id,),
            )

        files_to_check = [dict(row) for row in cur.fetchall()]
        archived_count = 0
        skipped_count = 0
        reasons: Dict[str, int] = {}

        for fi in files_to_check:
            rel_path = fi["rel_path"]

            # 用 IgnoreMatcher 判断
            if not matcher.is_ignored(rel_path, is_dir=False):
                # 未被忽略，若状态是 pending 则改为 active
                if fi["status"] == "pending":
                    self.conn.execute(
                        "UPDATE file_instances SET status = 'active' WHERE id = ?",
                        (fi["id"],),
                    )
                continue

            # 已归档过的跳过
            cur = self.conn.execute(
                "SELECT id FROM archived_files WHERE file_instance_id = ?",
                (fi["id"],),
            )
            if cur.fetchone():
                skipped_count += 1
                continue

            # 统计符号数和调用关系数（用于归档记录，删除前统计）
            sym_cur = self.conn.execute(
                "SELECT COUNT(*) as c FROM symbols WHERE file_instance_id = ?",
                (fi["id"],),
            )
            symbol_count = sym_cur.fetchone()["c"]
            call_cur = self.conn.execute(
                """SELECT COUNT(*) as c FROM calls
                   WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)""",
                (fi["id"],),
            )
            call_count = call_cur.fetchone()["c"]

            # 推断归档原因（找第一条命中的规则）
            reason = self._find_ignore_reason(matcher, rel_path)
            reasons[reason] = reasons.get(reason, 0) + 1

            if not dry_run:
                # 插入归档记录
                self.conn.execute(
                    """INSERT INTO archived_files
                       (file_instance_id, workspace_id, rel_path, abs_path,
                        content_hash, symbol_count, call_count, archive_reason, archived_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fi["id"], ws_id, rel_path, fi["abs_path"],
                     fi["current_content_hash"], symbol_count, call_count,
                     reason, time.time()),
                )
                # 标记文件实例为 archived（不删除，保留 ID 稳定性）
                self.conn.execute(
                    "UPDATE file_instances SET status = 'archived' WHERE id = ?",
                    (fi["id"],),
                )
                # 删除关联数据（释放空间，复活时重新解析重建）
                self._delete_file_associated_data(fi["id"])

            archived_count += 1

        if not dry_run:
            self.conn.commit()

        return {
            "scanned": len(files_to_check),
            "archived": archived_count,
            "skipped": skipped_count,
            "reasons": reasons,
            "dry_run": dry_run,
        }

    def _delete_file_associated_data(self, file_instance_id: int) -> None:
        """删除文件关联的符号/调用/版本数据（归档时调用，释放空间）

        Args:
            file_instance_id: 文件实例 ID
        """
        # 删除调用关系（caller 是该文件的符号的 calls）
        self.conn.execute(
            """DELETE FROM calls
               WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)""",
            (file_instance_id,),
        )
        # 删除符号
        self.conn.execute(
            "DELETE FROM symbols WHERE file_instance_id = ?",
            (file_instance_id,),
        )
        # 删除符号版本关联
        self.conn.execute(
            """DELETE FROM file_symbol_versions
               WHERE file_version_id IN (
                   SELECT id FROM file_versions WHERE file_instance_id = ?
               )""",
            (file_instance_id,),
        )
        # 删除文件版本
        self.conn.execute(
            "DELETE FROM file_versions WHERE file_instance_id = ?",
            (file_instance_id,),
        )

    def _find_ignore_reason(self, matcher: IgnoreMatcher, rel_path: str) -> str:
        """找出命中的第一条 ignore 规则（用于归档原因记录）

        Args:
            matcher: 已配置好的 IgnoreMatcher
            rel_path: 文件相对路径

        Returns:
            归档原因字符串（如 "default:.git/" 或 ".gitignore:build/"）
        """
        # 标准化路径
        rel_path = rel_path.replace("\\", "/").lstrip("./")

        # 按规则来源顺序查找（全局规则 → 子目录规则）
        for rule in matcher.global_rules:
            if rule.regex.search(rel_path):
                if rule.negation:
                    continue
                return f"{rule.source}:{rule.pattern}" + ("/" if rule.dir_only else "")

        for ancestor_dir, rules in matcher.dir_rules.items():
            for rule in rules:
                if rule.regex.search(rel_path):
                    if rule.negation:
                        continue
                    return f"{rule.source}:{rule.pattern}" + ("/" if rule.dir_only else "")

        return "unknown"

    def gc_restore(self, rel_paths: List[str] = None, force: bool = False) -> Dict[str, Any]:
        """复活已归档文件（类 GC demotion，老年代降回新生代）

        当 .gitignore / .callwardenignore 中的规则被移除后，调用此方法把归档文件迁回主表。
        复活后 status='pending'，下次 build 会自动重新解析重建 symbols/calls。

        Args:
            rel_paths: 要复活的文件相对路径列表；为空则扫描所有归档文件
            force: True=即使仍命中 ignore 规则也强制复活（慎用）

        Returns:
            {
                "scanned": int,
                "restored": int,
                "still_ignored": int,   # 仍被忽略未复活的文件数
            }
        """
        matcher = self._build_ignore_matcher()
        ws_id = self._get_active_workspace_id()

        if rel_paths:
            # 指定路径复活
            placeholders = ",".join("?" * len(rel_paths))
            cur = self.conn.execute(
                f"""SELECT af.* FROM archived_files af
                    JOIN file_instances fi ON af.file_instance_id = fi.id
                    WHERE af.workspace_id = ? AND af.rel_path IN ({placeholders})""",
                [ws_id] + rel_paths,
            )
        else:
            # 扫描所有归档文件
            cur = self.conn.execute(
                """SELECT * FROM archived_files WHERE workspace_id = ?""",
                (ws_id,),
            )

        archived_rows = [dict(row) for row in cur.fetchall()]
        restored = 0
        still_ignored = 0

        for af in archived_rows:
            rel_path = af["rel_path"]

            # 检查是否仍被忽略
            if not force and matcher.is_ignored(rel_path, is_dir=False):
                still_ignored += 1
                continue

            # 复活：删除归档记录 + 把 file_instances 状态改为 pending（让下次 build 重新解析）
            self.conn.execute(
                "DELETE FROM archived_files WHERE id = ?",
                (af["id"],),
            )
            self.conn.execute(
                "UPDATE file_instances SET status = 'pending' WHERE id = ?",
                (af["file_instance_id"],),
            )
            restored += 1

        self.conn.commit()

        return {
            "scanned": len(archived_rows),
            "restored": restored,
            "still_ignored": still_ignored,
        }

    def gc_status(self) -> Dict[str, Any]:
        """查询 GC 状态（类 JVM GC 统计）

        Returns:
            {
                "active_files": int,        # 活跃文件数
                "archived_files": int,      # 归档文件数
                "archived_symbols": int,    # 归档文件涉及的符号数
                "archived_calls": int,      # 归档文件涉及的调用关系数
                "deleted_files": int,       # 已删除文件数
                "archive_ratio": float,     # 归档率（archived / total）
                "recent_archives": list,    # 最近 10 条归档记录
            }
        """
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            """SELECT
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
                SUM(CASE WHEN status = 'archived' THEN 1 ELSE 0 END) as archived,
                SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                COUNT(*) as total
               FROM file_instances WHERE workspace_id = ?""",
            (ws_id,),
        )
        row = cur.fetchone()
        active = row["active"] or 0
        archived = row["archived"] or 0
        deleted = row["deleted"] or 0
        total = row["total"] or 0

        # 归档文件涉及的符号和调用关系数
        cur = self.conn.execute(
            """SELECT
                COALESCE(SUM(symbol_count), 0) as symbols,
                COALESCE(SUM(call_count), 0) as calls
               FROM archived_files WHERE workspace_id = ?""",
            (ws_id,),
        )
        sym_call = cur.fetchone()
        archived_symbols = sym_call["symbols"] or 0
        archived_calls = sym_call["calls"] or 0

        # 最近的归档记录
        cur = self.conn.execute(
            """SELECT rel_path, archive_reason, archived_at, symbol_count
               FROM archived_files WHERE workspace_id = ?
               ORDER BY archived_at DESC LIMIT 10""",
            (ws_id,),
        )
        recent = [dict(r) for r in cur.fetchall()]

        return {
            "active_files": active,
            "archived_files": archived,
            "archived_symbols": archived_symbols,
            "archived_calls": archived_calls,
            "deleted_files": deleted,
            "archive_ratio": (archived / total) if total > 0 else 0.0,
            "recent_archives": recent,
        }

    def gc_purge(self, older_than_days: int = 30) -> Dict[str, int]:
        """彻底清除归档文件（类 Full GC 的旧对象回收）

        把归档超过指定天数的文件实例彻底删除（关联数据在归档时已删除）。
        这是不可逆操作，复活后无法恢复。

        Args:
            older_than_days: 归档超过多少天才清除

        Returns:
            {"purged_files": int, "purged_symbols": int, "purged_calls": int}
        """
        ws_id = self._get_active_workspace_id()
        cutoff = time.time() - older_than_days * 86400

        # 查询要清除的归档文件
        cur = self.conn.execute(
            """SELECT file_instance_id, symbol_count, call_count FROM archived_files
               WHERE workspace_id = ? AND archived_at < ?""",
            (ws_id, cutoff),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {"purged_files": 0, "purged_symbols": 0, "purged_calls": 0}

        fi_ids = [r["file_instance_id"] for r in rows]
        purged_symbols = sum(r["symbol_count"] for r in rows)
        purged_calls = sum(r["call_count"] for r in rows)
        placeholders = ",".join("?" * len(fi_ids))

        # 归档时已删除关联数据，这里只需删 file_instances 和 archived_files 记录
        self.conn.execute(
            f"DELETE FROM archived_files WHERE file_instance_id IN ({placeholders})",
            fi_ids,
        )
        self.conn.execute(
            f"DELETE FROM file_instances WHERE id IN ({placeholders})",
            fi_ids,
        )

        self.conn.commit()

        return {
            "purged_files": len(fi_ids),
            "purged_symbols": purged_symbols,
            "purged_calls": purged_calls,
        }

    def gc_retention(
        self,
        older_than_days: Optional[int] = None,
        keep_versions: Optional[int] = None,
        include_external: Optional[bool] = None,
        external_stale_days: Optional[int] = None,
        dry_run: bool = True,
        backup: Optional[bool] = None,
        vacuum: Optional[bool] = None,
        save_policy: bool = False,
    ) -> Dict[str, Any]:
        """按保守保留策略清理冷数据。

        策略：
        - 文件历史：每个文件至少保留最近 keep_versions 个版本，只清理更老且超过 older_than_days 的非当前版本。
        - 外部符号：默认不清理；显式 include_external=True 时只按 last_seen/last_used 时间清理冷包。
        - 删除前默认备份完整 SQLite 数据库到 gzip，便于后续离线导回。
        """
        policy = self._resolve_gc_retention_policy(
            older_than_days=older_than_days,
            keep_versions=keep_versions,
            include_external=include_external,
            external_stale_days=external_stale_days,
            backup=backup,
            vacuum=vacuum,
        )
        if save_policy:
            self.set_gc_policy(
                older_than_days=older_than_days,
                keep_versions=keep_versions,
                include_external=include_external,
                external_stale_days=external_stale_days,
                backup_enabled=backup,
                vacuum_enabled=vacuum,
            )

        older_than_days = int(policy["older_than_days"])
        keep_versions = int(policy["keep_versions"])
        include_external = bool(policy["include_external"])
        external_stale_days = int(policy["external_stale_days"])
        backup = bool(policy["backup_enabled"])
        vacuum = bool(policy["vacuum_enabled"])

        if older_than_days < 1:
            older_than_days = 1
        if keep_versions < 1:
            keep_versions = 1
        if external_stale_days < 1:
            external_stale_days = 1

        ws_id = self._get_active_workspace_id()
        version_cutoff = time.time() - older_than_days * 86400
        external_cutoff = time.time() - external_stale_days * 86400

        version_ids = self._select_retention_file_versions(
            ws_id, version_cutoff, keep_versions
        )
        external_packages = (
            self._select_retention_external_packages(external_cutoff)
            if include_external
            else []
        )

        backup_path = ""
        backup_size = 0
        if not dry_run and backup and (version_ids or external_packages):
            backup_info = self._create_gc_db_backup("retention")
            backup_path = backup_info["path"]
            backup_size = backup_info["size"]

        deleted_versions = 0
        deleted_file_symbols = 0
        deleted_call_versions = 0
        deleted_external_symbols = 0
        deleted_packages = 0
        deleted_orphan_symbols = 0

        if not dry_run:
            if version_ids:
                placeholders = ",".join("?" for _ in version_ids)
                cur = self.conn.execute(
                    f"DELETE FROM call_versions WHERE file_version_id IN ({placeholders})",
                    version_ids,
                )
                deleted_call_versions = cur.rowcount if cur.rowcount is not None else 0
                cur = self.conn.execute(
                    f"DELETE FROM file_symbol_versions WHERE file_version_id IN ({placeholders})",
                    version_ids,
                )
                deleted_file_symbols = cur.rowcount if cur.rowcount is not None else 0
                cur = self.conn.execute(
                    f"DELETE FROM file_versions WHERE id IN ({placeholders})",
                    version_ids,
                )
                deleted_versions = cur.rowcount if cur.rowcount is not None else 0

            for pkg in external_packages:
                cur = self.conn.execute(
                    "DELETE FROM external_symbols WHERE package_name = ? AND package_version = ?",
                    (pkg["package_name"], pkg["package_version"]),
                )
                deleted_external_symbols += cur.rowcount if cur.rowcount is not None else 0
                cur = self.conn.execute(
                    "DELETE FROM package_versions WHERE package_name = ? AND package_version = ?",
                    (pkg["package_name"], pkg["package_version"]),
                )
                deleted_packages += cur.rowcount if cur.rowcount is not None else 0

            deleted_orphan_symbols = self._delete_orphan_symbol_contents()
            self.conn.commit()
            if vacuum:
                self.conn.execute("VACUUM")

        return {
            "dry_run": dry_run,
            "policy": policy,
            "saved_policy": save_policy,
            "backup_path": backup_path,
            "backup_size": backup_size,
            "candidate_file_versions": len(version_ids),
            "candidate_external_packages": len(external_packages),
            "deleted_file_versions": deleted_versions,
            "deleted_file_symbol_versions": deleted_file_symbols,
            "deleted_call_versions": deleted_call_versions,
            "deleted_external_symbols": deleted_external_symbols,
            "deleted_external_packages": deleted_packages,
            "deleted_orphan_symbol_contents": deleted_orphan_symbols,
            "vacuum": vacuum and not dry_run,
        }

    def get_gc_policy(self) -> Dict[str, Any]:
        """读取当前 workspace 的 GC retention 策略，不存在则创建默认策略。"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "SELECT * FROM gc_policies WHERE workspace_id = ?",
            (ws_id,),
        )
        row = cur.fetchone()
        if not row:
            policy = dict(DEFAULT_GC_POLICY)
            self.set_gc_policy(**policy)
            policy["workspace_id"] = ws_id
            return policy
        return self._row_to_gc_policy(dict(row))

    def set_gc_policy(
        self,
        older_than_days: Optional[int] = None,
        keep_versions: Optional[int] = None,
        include_external: Optional[bool] = None,
        external_stale_days: Optional[int] = None,
        backup_enabled: Optional[bool] = None,
        vacuum_enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """更新当前 workspace 的 GC retention 策略。"""
        current = self.get_gc_policy_without_create()
        policy = dict(DEFAULT_GC_POLICY)
        if current:
            policy.update(current)

        updates = {
            "older_than_days": older_than_days,
            "keep_versions": keep_versions,
            "include_external": include_external,
            "external_stale_days": external_stale_days,
            "backup_enabled": backup_enabled,
            "vacuum_enabled": vacuum_enabled,
        }
        for key, value in updates.items():
            if value is None:
                continue
            if key in ("include_external", "backup_enabled", "vacuum_enabled"):
                policy[key] = bool(value)
            else:
                policy[key] = max(1, int(value))

        ws_id = self._get_active_workspace_id()
        now = time.time()
        self.conn.execute(
            """INSERT INTO gc_policies
               (workspace_id, older_than_days, keep_versions, include_external,
                external_stale_days, backup_enabled, vacuum_enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace_id) DO UPDATE SET
                 older_than_days = excluded.older_than_days,
                 keep_versions = excluded.keep_versions,
                 include_external = excluded.include_external,
                 external_stale_days = excluded.external_stale_days,
                 backup_enabled = excluded.backup_enabled,
                 vacuum_enabled = excluded.vacuum_enabled,
                 updated_at = excluded.updated_at""",
            (
                ws_id,
                int(policy["older_than_days"]),
                int(policy["keep_versions"]),
                1 if policy["include_external"] else 0,
                int(policy["external_stale_days"]),
                1 if policy["backup_enabled"] else 0,
                1 if policy["vacuum_enabled"] else 0,
                now,
            ),
        )
        self.conn.commit()
        policy["workspace_id"] = ws_id
        policy["updated_at"] = now
        return policy

    def get_gc_policy_without_create(self) -> Dict[str, Any]:
        """读取当前 workspace 的 GC 策略；不存在时返回空字典。"""
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "SELECT * FROM gc_policies WHERE workspace_id = ?",
            (ws_id,),
        )
        row = cur.fetchone()
        return self._row_to_gc_policy(dict(row)) if row else {}

    def _resolve_gc_retention_policy(
        self,
        older_than_days: Optional[int],
        keep_versions: Optional[int],
        include_external: Optional[bool],
        external_stale_days: Optional[int],
        backup: Optional[bool],
        vacuum: Optional[bool],
    ) -> Dict[str, Any]:
        """合并 DB policy 与本次运行参数。"""
        policy = self.get_gc_policy()
        overrides = {
            "older_than_days": older_than_days,
            "keep_versions": keep_versions,
            "include_external": include_external,
            "external_stale_days": external_stale_days,
            "backup_enabled": backup,
            "vacuum_enabled": vacuum,
        }
        for key, value in overrides.items():
            if value is None:
                continue
            if key in ("include_external", "backup_enabled", "vacuum_enabled"):
                policy[key] = bool(value)
            else:
                policy[key] = max(1, int(value))
        return policy

    def _row_to_gc_policy(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """把数据库行转为 Python 策略字典。"""
        return {
            "workspace_id": row.get("workspace_id"),
            "older_than_days": int(row.get("older_than_days") or DEFAULT_GC_POLICY["older_than_days"]),
            "keep_versions": int(row.get("keep_versions") or DEFAULT_GC_POLICY["keep_versions"]),
            "include_external": bool(row.get("include_external")),
            "external_stale_days": int(row.get("external_stale_days") or DEFAULT_GC_POLICY["external_stale_days"]),
            "backup_enabled": bool(row.get("backup_enabled")),
            "vacuum_enabled": bool(row.get("vacuum_enabled")),
            "updated_at": row.get("updated_at", 0),
        }

    def _select_retention_file_versions(
        self,
        workspace_id: int,
        cutoff: float,
        keep_versions: int,
    ) -> List[int]:
        """选择可归档删除的旧文件版本。"""
        cur = self.conn.execute(
            """
            WITH ranked AS (
                SELECT
                    fv.id,
                    fv.file_instance_id,
                    fv.parsed_at,
                    fv.is_current,
                    ROW_NUMBER() OVER (
                        PARTITION BY fv.file_instance_id
                        ORDER BY fv.version_num DESC
                    ) AS version_rank
                FROM file_versions fv
                JOIN file_instances fi ON fi.id = fv.file_instance_id
                WHERE fi.workspace_id = ?
            )
            SELECT r.id
            FROM ranked r
            WHERE r.is_current = 0
              AND r.parsed_at < ?
              AND r.version_rank > ?
              AND NOT EXISTS (
                  SELECT 1 FROM file_symbol_versions fsv
                  JOIN symbol_contents sc ON sc.content_hash = fsv.symbol_hash
                  WHERE fsv.file_version_id = r.id AND sc.has_comment = 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM file_symbol_versions fsv
                  JOIN task_symbol_changes tsc
                    ON tsc.symbol_hash_before = fsv.symbol_hash
                    OR tsc.symbol_hash_after = fsv.symbol_hash
                  WHERE fsv.file_version_id = r.id
              )
            """,
            (workspace_id, cutoff, keep_versions),
        )
        return [row["id"] for row in cur.fetchall()]

    def _select_retention_external_packages(self, cutoff: float) -> List[Dict[str, Any]]:
        """选择超过冷数据阈值的外部包版本。"""
        cur = self.conn.execute(
            """
            SELECT package_name, package_version,
                   MAX(COALESCE(last_used_at, 0), COALESCE(last_seen_at, 0), COALESCE(installed_at, 0)) AS last_touch
            FROM package_versions
            WHERE lower(package_name) != 'stdlib'
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
        return [row for row in rows if (row.get("last_touch") or 0) < cutoff]

    def _delete_orphan_symbol_contents(self) -> int:
        """删除已无任何引用的符号内容。"""
        cur = self.conn.execute(
            """
            DELETE FROM symbol_contents
            WHERE content_hash NOT IN (SELECT symbol_hash FROM symbols)
              AND content_hash NOT IN (SELECT symbol_hash FROM file_symbol_versions)
              AND content_hash NOT IN (SELECT symbol_hash FROM comments)
              AND content_hash NOT IN (SELECT symbol_hash FROM evolution_metrics)
              AND content_hash NOT IN (SELECT symbol_hash FROM defect_fixes)
              AND content_hash NOT IN (SELECT before_hash FROM defect_fixes)
              AND content_hash NOT IN (SELECT after_hash FROM defect_fixes)
              AND content_hash NOT IN (SELECT symbol_hash_before FROM task_symbol_changes)
              AND content_hash NOT IN (SELECT symbol_hash_after FROM task_symbol_changes)
            """
        )
        return cur.rowcount if cur.rowcount is not None else 0

    def _create_gc_db_backup(self, reason: str) -> Dict[str, Any]:
        """创建压缩 SQLite 备份。"""
        archive_dir = os.path.join(os.path.dirname(self.db_path), "gc_archives")
        os.makedirs(archive_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        final_path = os.path.join(archive_dir, f"{stamp}-{reason}.db.gz")

        fd, temp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dst = sqlite3.connect(temp_path)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
            with open(temp_path, "rb") as src, gzip.open(final_path, "wb") as gz:
                shutil.copyfileobj(src, gz)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        return {"path": final_path, "size": os.path.getsize(final_path)}
