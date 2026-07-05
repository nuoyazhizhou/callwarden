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
import json
import os
import shutil
import sqlite3
import tempfile
import time
import traceback
from typing import Any, Dict, List, Optional

from ..analyzers.ignore_spec import IgnoreMatcher
from ..cli.console import cprint
from ..config import norm_path
from ..i18n import t


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

    def gc_archive(
        self,
        force: bool = False,
        dry_run: bool = False,
        operator: str = "cli",
    ) -> Dict[str, Any]:
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
            operator: 触发者（cli / mcp / agent），写入审计记录

        Returns:
            {
                "audit_id": int,      # GC 审计记录 ID（v20）
                "scanned": int,       # 扫描的文件数
                "archived": int,      # 归档的文件数
                "skipped": int,       # 已归档跳过的文件数
                "reasons": dict,      # {归档原因: 文件数}
                "dry_run": bool,
            }
        """
        audit_id = self._start_gc_audit(
            operation="archive",
            dry_run=dry_run,
            policy={"force": force},
            operator=operator,
        )
        try:
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

            candidate_counts = {
                "scanned_files": len(files_to_check),
                "archived_files": archived_count,
                "skipped_files": skipped_count,
            }
            self._complete_gc_audit(
                audit_id=audit_id,
                candidate_counts=candidate_counts,
                deleted_counts=candidate_counts,  # archive 没有删除，候选=实删
                backup_path="",
                backup_size=0,
            )

            return {
                "audit_id": audit_id,
                "scanned": len(files_to_check),
                "archived": archived_count,
                "skipped": skipped_count,
                "reasons": reasons,
                "dry_run": dry_run,
            }
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._fail_gc_audit(audit_id, err_msg)
            raise

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

    def gc_purge(
        self,
        older_than_days: int = 30,
        operator: str = "cli",
    ) -> Dict[str, Any]:
        """彻底清除归档文件（类 Full GC 的旧对象回收）

        把归档超过指定天数的文件实例彻底删除（关联数据在归档时已删除）。
        这是不可逆操作，复活后无法恢复。

        Args:
            older_than_days: 归档超过多少天才清除
            operator: 触发者（cli / mcp / agent），写入审计记录

        Returns:
            {
                "audit_id": int,         # GC 审计记录 ID（v20）
                "purged_files": int,
                "purged_symbols": int,
                "purged_calls": int,
            }
        """
        audit_id = self._start_gc_audit(
            operation="purge",
            dry_run=False,
            policy={"older_than_days": older_than_days},
            operator=operator,
        )
        try:
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
                self._complete_gc_audit(
                    audit_id=audit_id,
                    candidate_counts={"archived_files": 0},
                    deleted_counts={"purged_files": 0, "purged_symbols": 0, "purged_calls": 0},
                    backup_path="",
                    backup_size=0,
                )
                return {
                    "audit_id": audit_id,
                    "purged_files": 0,
                    "purged_symbols": 0,
                    "purged_calls": 0,
                }

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

            deleted_counts = {
                "purged_files": len(fi_ids),
                "purged_symbols": purged_symbols,
                "purged_calls": purged_calls,
            }
            self._complete_gc_audit(
                audit_id=audit_id,
                candidate_counts={"archived_files": len(rows)},
                deleted_counts=deleted_counts,
                backup_path="",
                backup_size=0,
            )

            return {
                "audit_id": audit_id,
                "purged_files": len(fi_ids),
                "purged_symbols": purged_symbols,
                "purged_calls": purged_calls,
            }
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._fail_gc_audit(audit_id, err_msg)
            raise

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
        operator: str = "cli",
    ) -> Dict[str, Any]:
        """按保守保留策略清理冷数据。

        策略：
        - 文件历史：每个文件至少保留最近 keep_versions 个版本，只清理更老且超过 older_than_days 的非当前版本。
        - 外部符号：默认不清理；显式 include_external=True 时只按 last_seen/last_used 时间清理冷包。
        - 删除前默认备份完整 SQLite 数据库到 gzip，便于后续离线导回。

        审计（v20）：开始时写 running 审计记录，成功更新为 completed，
        异常更新为 failed 并记录 error；返回值包含 audit_id 便于追溯。
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

        older_than_days_v = max(1, int(policy["older_than_days"]))
        keep_versions_v = max(1, int(policy["keep_versions"]))
        include_external_v = bool(policy["include_external"])
        external_stale_days_v = max(1, int(policy["external_stale_days"]))
        backup_v = bool(policy["backup_enabled"])
        vacuum_v = bool(policy["vacuum_enabled"])

        audit_id = self._start_gc_audit(
            operation="retention",
            dry_run=dry_run,
            policy={
                "older_than_days": older_than_days_v,
                "keep_versions": keep_versions_v,
                "include_external": include_external_v,
                "external_stale_days": external_stale_days_v,
                "backup_enabled": backup_v,
                "vacuum_enabled": vacuum_v,
                "save_policy": save_policy,
            },
            operator=operator,
        )

        try:
            ws_id = self._get_active_workspace_id()
            version_cutoff = time.time() - older_than_days_v * 86400
            external_cutoff = time.time() - external_stale_days_v * 86400

            version_ids = self._select_retention_file_versions(
                ws_id, version_cutoff, keep_versions_v
            )
            external_packages = (
                self._select_retention_external_packages(external_cutoff)
                if include_external_v
                else []
            )

            # Top N 估算（v20 新增）：dry-run 和 apply 都返回，便于决策
            estimate = self._estimate_retention_top_n(version_ids, external_packages, top_n=10)

            backup_path = ""
            backup_size = 0
            if not dry_run and backup_v and (version_ids or external_packages):
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
                if vacuum_v:
                    self.conn.execute("VACUUM")

            candidate_counts = {
                "file_versions": len(version_ids),
                "external_packages": len(external_packages),
            }
            deleted_counts = {
                "file_versions": deleted_versions,
                "file_symbol_versions": deleted_file_symbols,
                "call_versions": deleted_call_versions,
                "external_symbols": deleted_external_symbols,
                "external_packages": deleted_packages,
                "orphan_symbol_contents": deleted_orphan_symbols,
            }
            self._complete_gc_audit(
                audit_id=audit_id,
                candidate_counts=candidate_counts,
                deleted_counts=deleted_counts,
                backup_path=backup_path,
                backup_size=backup_size,
            )

            return {
                "audit_id": audit_id,
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
                "vacuum": vacuum_v and not dry_run,
                # v20 新增：Top N 收益预估（估算值，非精确）
                "estimate": estimate,
            }
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self._fail_gc_audit(audit_id, err_msg)
            raise

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

    def _estimate_retention_top_n(
        self,
        version_ids: List[int],
        external_packages: List[Dict[str, Any]],
        top_n: int = 10,
    ) -> Dict[str, Any]:
        """估算 retention 的 Top N 收益（v20 新增）

        提供给用户判断"值不值得执行 retention"的决策依据。
        所有数量均为估算（基于候选 ID 集合预统计），不承诺精确磁盘节省。

        Args:
            version_ids: 候选 file_version ID 列表
            external_packages: 候选外部包列表
            top_n: Top N 列表长度（默认 10）

        Returns:
            {
                "approximate_deleted_rows": dict,  # 各表预计删除行数（估算）
                "affected_files_top_n": list,      # 受影响文件 Top N
                "external_packages_top_n": list,   # 受影响外部包 Top N
                "is_estimate": True,               # 标记为估算
            }
        """
        if top_n < 1:
            top_n = 1
        if top_n > 100:
            top_n = 100

        # 1. 估算各表删除行数
        approx: Dict[str, int] = {
            "file_versions": len(version_ids),
            "file_symbol_versions": 0,
            "call_versions": 0,
            "symbol_contents": 0,  # 仅估算，实际依赖 _delete_orphan_symbol_contents
            "external_symbols": 0,
            "external_packages": len(external_packages),
        }
        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            cur = self.conn.execute(
                f"SELECT COUNT(*) as c FROM file_symbol_versions WHERE file_version_id IN ({placeholders})",
                version_ids,
            )
            approx["file_symbol_versions"] = cur.fetchone()["c"]
            cur = self.conn.execute(
                f"SELECT COUNT(*) as c FROM call_versions WHERE file_version_id IN ({placeholders})",
                version_ids,
            )
            approx["call_versions"] = cur.fetchone()["c"]
        if external_packages:
            pkg_names = [p["package_name"] for p in external_packages]
            placeholders = ",".join("?" for _ in pkg_names)
            cur = self.conn.execute(
                f"SELECT COUNT(*) as c FROM external_symbols WHERE package_name IN ({placeholders})",
                pkg_names,
            )
            approx["external_symbols"] = cur.fetchone()["c"]

        # 2. 受影响文件 Top N（按候选版本数排序）
        affected_files: List[Dict[str, Any]] = []
        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            cur = self.conn.execute(
                f"""
                SELECT fi.rel_path,
                       COUNT(fv.id) AS candidate_versions,
                       MIN(fv.parsed_at) AS oldest_parsed,
                       MAX(fv.parsed_at) AS newest_parsed
                FROM file_versions fv
                JOIN file_instances fi ON fi.id = fv.file_instance_id
                WHERE fv.id IN ({placeholders})
                GROUP BY fi.rel_path
                ORDER BY candidate_versions DESC
                LIMIT ?
                """,
                (*version_ids, top_n),
            )
            for row in cur.fetchall():
                affected_files.append({
                    "rel_path": row["rel_path"],
                    "candidate_versions": row["candidate_versions"],
                    "oldest_parsed": row["oldest_parsed"],
                    "newest_parsed": row["newest_parsed"],
                })

        # 3. 受影响外部包 Top N（按符号数排序）
        external_top: List[Dict[str, Any]] = []
        if external_packages:
            pkg_names = [p["package_name"] for p in external_packages]
            placeholders = ",".join("?" for _ in pkg_names)
            cur = self.conn.execute(
                f"""
                SELECT es.package_name,
                       pv.package_version,
                       COUNT(es.id) AS symbol_count,
                       MAX(COALESCE(pv.last_used_at, 0)) AS last_used_at,
                       MAX(COALESCE(pv.last_seen_at, 0)) AS last_seen_at,
                       MAX(COALESCE(pv.installed_at, 0)) AS installed_at
                FROM external_symbols es
                LEFT JOIN package_versions pv
                  ON pv.package_name = es.package_name
                 AND pv.package_version = es.package_version
                WHERE es.package_name IN ({placeholders})
                GROUP BY es.package_name, pv.package_version
                ORDER BY symbol_count DESC
                LIMIT ?
                """,
                (*pkg_names, top_n),
            )
            for row in cur.fetchall():
                last_touch = max(
                    row["last_used_at"] or 0,
                    row["last_seen_at"] or 0,
                    row["installed_at"] or 0,
                )
                external_top.append({
                    "package_name": row["package_name"],
                    "package_version": row["package_version"],
                    "symbol_count": row["symbol_count"],
                    "last_touch": last_touch,
                })

        return {
            "approximate_deleted_rows": approx,
            "affected_files_top_n": affected_files,
            "external_packages_top_n": external_top,
            "is_estimate": True,
        }

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

    # ------------------------------------------------------------------
    # GC 运行审计（v20 新增）
    # ------------------------------------------------------------------
    # 每次 retention / archive / purge 都会写一行 gc_runs 审计记录，
    # 便于事后追溯"为什么少了数据"。流程：
    #   1. 开始时 _start_gc_audit 插入 status=running 记录，返回 audit_id
    #   2. 成功时 _complete_gc_audit 更新为 completed，写入候选/实删/备份明细
    #   3. 异常时 _fail_gc_audit 更新为 failed，写入 error 信息
    #   4. gc_audit_list 查询历史审计记录

    def _start_gc_audit(
        self,
        operation: str,
        dry_run: bool,
        policy: Dict[str, Any],
        operator: str = "cli",
    ) -> int:
        """开始一次 GC 审计记录，返回 audit_id

        Args:
            operation: 操作类型（retention / archive / purge）
            dry_run: 是否预演
            policy: 策略参数字典（会序列化为 JSON 存入 policy_json）
            operator: 触发者（cli / mcp / agent）

        Returns:
            新建的 gc_runs.id
        """
        ws_id = self._get_active_workspace_id() if hasattr(self, "_get_active_workspace_id") else None
        policy_json = json.dumps(policy or {}, ensure_ascii=False, sort_keys=True)
        cur = self.conn.execute(
            """INSERT INTO gc_runs
               (workspace_id, operation, dry_run, policy_json,
                candidate_counts, deleted_counts, backup_path, backup_size,
                started_at, completed_at, status, error, operator)
               VALUES (?, ?, ?, ?, '{}', '{}', '', 0, ?, NULL, 'running', '', ?)""",
            (ws_id, operation, 1 if dry_run else 0, policy_json, time.time(), operator),
        )
        self.conn.commit()
        return cur.lastrowid

    def _complete_gc_audit(
        self,
        audit_id: int,
        candidate_counts: Dict[str, int],
        deleted_counts: Dict[str, int],
        backup_path: str = "",
        backup_size: int = 0,
    ) -> None:
        """标记审计记录为完成，写入候选/实删/备份明细

        Args:
            audit_id: gc_runs.id
            candidate_counts: 候选数量明细（{file_versions: N, external_packages: M}）
            deleted_counts: 实删数量明细
            backup_path: 备份文件路径
            backup_size: 备份文件字节数
        """
        self.conn.execute(
            """UPDATE gc_runs SET
                 candidate_counts = ?, deleted_counts = ?,
                 backup_path = ?, backup_size = ?,
                 completed_at = ?, status = 'completed', error = ''
               WHERE id = ?""",
            (json.dumps(candidate_counts or {}, ensure_ascii=False, sort_keys=True),
             json.dumps(deleted_counts or {}, ensure_ascii=False, sort_keys=True),
             backup_path, backup_size, time.time(), audit_id),
        )
        self.conn.commit()

    def _fail_gc_audit(self, audit_id: int, error: str) -> None:
        """标记审计记录为失败，写入错误信息

        Args:
            audit_id: gc_runs.id
            error: 异常信息（截断到合理长度避免单行过大）
        """
        if not error:
            error = "unknown error"
        # 截断到 2000 字符避免审计表膨胀
        if len(error) > 2000:
            error = error[:2000] + "...(truncated)"
        self.conn.execute(
            """UPDATE gc_runs SET completed_at = ?, status = 'failed', error = ?
               WHERE id = ?""",
            (time.time(), error, audit_id),
        )
        self.conn.commit()

    def gc_audit_list(
        self,
        limit: int = 20,
        operation: Optional[str] = None,
        workspace_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """查询 GC 审计历史记录

        Args:
            limit: 最多返回多少条（默认 20）
            operation: 按操作类型过滤（retention / archive / purge）；None 表示不过滤
            workspace_id: 按工作区过滤；None 表示用当前活动工作区

        Returns:
            审计记录列表，按 started_at 倒序，每条包含：
            - id, workspace_id, operation, dry_run, policy_json
            - candidate_counts, deleted_counts（解析后的 dict）
            - backup_path, backup_size, started_at, completed_at
            - status, error, operator
        """
        if workspace_id is None and hasattr(self, "_get_active_workspace_id"):
            workspace_id = self._get_active_workspace_id()
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500

        sql = "SELECT * FROM gc_runs"
        conditions: List[str] = []
        params: List[Any] = []
        if workspace_id is not None:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if operation:
            conditions.append("operation = ?")
            params.append(operation)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        cur = self.conn.execute(sql, params)
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            # 反序列化 JSON 字段
            for field in ("policy_json", "candidate_counts", "deleted_counts"):
                raw = d.get(field) or "{}"
                try:
                    d[field] = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    d[field] = {}
            rows.append(d)
        return rows

    def gc_audit_get(self, audit_id: int) -> Optional[Dict[str, Any]]:
        """查询单条 GC 审计记录详情

        Args:
            audit_id: gc_runs.id

        Returns:
            审计记录 dict（含反序列化的 JSON 字段），不存在返回 None
        """
        cur = self.conn.execute("SELECT * FROM gc_runs WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        for field in ("policy_json", "candidate_counts", "deleted_counts"):
            raw = d.get(field) or "{}"
            try:
                d[field] = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                d[field] = {}
        return d

    # ------------------------------------------------------------------
    # GC 备份文件列表与检查（v20 新增）
    # ------------------------------------------------------------------
    # gc_archive_list：列出 gc_archives/*.db.gz 文件元信息
    # gc_archive_inspect：解压到临时文件只读打开，返回 schema 版本/表列表/各表行数

    def gc_archive_list(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出当前数据库目录下的 gc_archives/*.db.gz 备份文件

        文件名格式约定：{YYYYMMDD-HHMMSS}-{reason}.db.gz（见 _create_gc_db_backup）。
        从文件名解析 reason；若无匹配则 reason="unknown"。

        Args:
            limit: 最多返回多少条（默认 20，按 mtime 倒序）

        Returns:
            备份文件列表，每条包含：
            - path: 备份文件绝对路径
            - name: 文件名
            - size: 文件字节数
            - mtime: 最后修改时间戳
            - reason: 备份原因（从文件名解析，如 "retention" / "unit"）
        """
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        archive_dir = os.path.join(os.path.dirname(self.db_path), "gc_archives")
        if not os.path.isdir(archive_dir):
            return []

        import re
        reason_pattern = re.compile(r"^\d{8}-\d{6}-(.+)\.db\.gz$")
        items: List[Dict[str, Any]] = []
        for name in os.listdir(archive_dir):
            if not name.endswith(".db.gz"):
                continue
            full_path = os.path.join(archive_dir, name)
            if not os.path.isfile(full_path):
                continue
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            m = reason_pattern.match(name)
            reason = m.group(1) if m else "unknown"
            items.append({
                "path": full_path,
                "name": name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "reason": reason,
            })
        # 按 mtime 倒序
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items[:limit]

    def gc_archive_inspect(self, path: str) -> Dict[str, Any]:
        """检查 GC 备份文件内容（只读，不解压到磁盘永久位置）

        流程：
        1. 解压 .db.gz 到临时文件
        2. 用 sqlite3 只读连接打开
        3. 读取 schema_version、表列表、各表行数
        4. 关闭连接并删除临时文件

        Args:
            path: 备份文件路径（.db.gz）

        Returns:
            {
                "path": str,                 # 备份文件路径
                "name": str,                 # 文件名
                "size": int,                 # 备份文件字节数
                "schema_version": int,        # 备份库的 schema 版本
                "tables": List[Dict],        # 表信息列表 [{name, rows}]
                "workspace_count": int,       # 工作区数量
                "file_version_count": int,    # 文件版本数
                "symbol_count": int,         # 符号数
                "call_count": int,            # 调用关系数
                "gc_runs_count": int,         # GC 审计记录数
                "archived_files_count": int,  # 归档文件数
            }

        Raises:
            FileNotFoundError: 备份文件不存在
            ValueError: 不是有效的 .db.gz 文件
            sqlite3.DatabaseError: 解压后不是有效的 SQLite 数据库
        """
        if not path:
            raise ValueError(t("errors.gc_archive_inspect_path_required",
                               default="archive path is required"))
        # 规范化路径：相对路径基于 gc_archives 目录解析
        if not os.path.isabs(path):
            archive_dir = os.path.join(os.path.dirname(self.db_path), "gc_archives")
            candidate = os.path.join(archive_dir, path)
            if os.path.isfile(candidate):
                path = candidate
            elif not path.endswith(".db.gz") and os.path.isfile(candidate + ".db.gz"):
                path = candidate + ".db.gz"

        if not os.path.isfile(path):
            raise FileNotFoundError(
                t("errors.gc_archive_inspect_not_found",
                  default="archive file not found: {path}", path=path)
            )

        size = os.path.getsize(path)
        name = os.path.basename(path)

        # 解压到临时文件
        fd, temp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with gzip.open(path, "rb") as src, open(temp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

            # 用 URI 只读模式打开，避免修改备份
            uri = f"file:{temp_path}?mode=ro"
            con = sqlite3.connect(uri, uri=True)
            con.row_factory = sqlite3.Row
            try:
                # schema 版本
                try:
                    cur = con.execute("SELECT MAX(version) as v FROM schema_version")
                    row = cur.fetchone()
                    schema_version = row["v"] if row and row["v"] is not None else 0
                except sqlite3.DatabaseError:
                    schema_version = 0

                # 表列表与行数
                cur = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                table_names = [r[0] for r in cur.fetchall()]
                tables: List[Dict[str, Any]] = []
                for tn in table_names:
                    try:
                        cnt = con.execute(f"SELECT COUNT(*) FROM '{tn}'").fetchone()[0]
                    except sqlite3.DatabaseError:
                        cnt = -1
                    tables.append({"name": tn, "rows": cnt})

                # 关键表行数（容错：旧库可能没有某些表）
                def _count(table_name: str) -> int:
                    for t in tables:
                        if t["name"] == table_name:
                            return t["rows"]
                    return 0

                return {
                    "path": path,
                    "name": name,
                    "size": size,
                    "schema_version": schema_version,
                    "tables": tables,
                    "workspace_count": _count("workspaces"),
                    "file_version_count": _count("file_versions"),
                    "symbol_count": _count("symbols"),
                    "call_count": _count("calls"),
                    "gc_runs_count": _count("gc_runs"),
                    "archived_files_count": _count("archived_files"),
                }
            finally:
                con.close()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # GC 备份导入（v20 新增）
    # ------------------------------------------------------------------
    # 从 .db.gz 备份文件导回历史数据到当前库，遵循"当前库优先"原则：
    #   - 只 INSERT OR IGNORE，绝不覆盖现有事实
    #   - 默认 dry_run=True，避免误操作
    #   - 仅导入用户明确指定的目标（file_path 或 package_name）
    #   - 冲突情况（主键已存在、外键不匹配）记录到 skipped 计数
    #
    # 设计目标：
    #   1. 支持 --file src/a.py 形式按文件粒度导回历史版本
    #   2. 支持 --external-package xxx 形式按外部包粒度导回符号定义
    #   3. 应用前 dry-run 预演，让用户看清可导入/跳过数量
    #   4. 写入操作幂等：重复执行不会产生副作用

    def gc_archive_import(
        self,
        path: str,
        file_path: str = "",
        package_name: str = "",
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """从 GC 备份文件导入历史数据到当前库

        设计原则：
        - 只 INSERT OR IGNORE，绝不覆盖现有事实（当前库优先）
        - 默认 dry_run=True，避免误操作
        - 仅导入用户明确指定的目标（file_path 或 package_name）
        - 冲突情况（主键已存在、外键不匹配）记录到 skipped 计数

        Args:
            path: 备份文件路径（.db.gz，支持相对 gc_archives 目录的简写）
            file_path: 要导入的文件相对路径（如 'src/a.py'），
                       导入该文件的 file_versions / file_symbol_versions / symbol_contents
            package_name: 要导入的外部包名，导入对应的 package_versions / external_symbols
            dry_run: True=只统计不实际导入（默认）

        Returns:
            {
                "path": str,            # 备份文件路径
                "dry_run": bool,        # 是否预演
                "target": str,          # 目标类型："file" / "package" / ""
                "target_value": str,    # 目标值（file_path 或 package_name）
                "imported": dict,       # 各类实际导入数量
                "skipped": dict,        # 各类跳过数量（冲突）
                "errors": list,         # 错误信息列表
            }

        Raises:
            ValueError: path 为空 / 未指定目标
            FileNotFoundError: 备份文件不存在

        流程：
        1. 校验 path 存在且为有效 .db.gz
        2. 校验至少指定一个目标（file_path 或 package_name），否则抛 ValueError
        3. 解压备份到临时文件，sqlite3 只读连接
        4. 根据 target 类型查询备份数据：
           - file 模式：按 rel_path 在备份库找 file_instance，
                       取其 file_versions / file_symbol_versions / symbol_contents
           - package 模式：按 package_name 在备份库找 package_versions / external_symbols
        5. dry_run=True：只统计 candidate 数量，不写入
        6. dry_run=False：
           - file 模式：当前库按 rel_path 找 file_instance（不存在则跳过并记 skipped）；
                       INSERT OR IGNORE file_versions / file_symbol_versions / symbol_contents
           - package 模式：INSERT OR IGNORE package_versions / external_symbols
        7. 返回 imported / skipped 统计
        """
        # 启动审计记录
        audit_id = self._start_gc_audit(
            operation="archive_import",
            dry_run=dry_run,
            policy={
                "file_path": file_path,
                "package_name": package_name,
            },
            operator="cli",
        )
        try:
            result = self._do_gc_archive_import(
                path=path,
                file_path=file_path,
                package_name=package_name,
                dry_run=dry_run,
            )
            self._complete_gc_audit(
                audit_id=audit_id,
                candidate_counts=result.get("candidate", {}),
                deleted_counts=result.get("imported", {}),
                backup_path=result.get("path", ""),
                backup_size=0,
            )
            return result
        except Exception as e:
            self._fail_gc_audit(audit_id, str(e))
            raise

    def _do_gc_archive_import(
        self,
        path: str,
        file_path: str,
        package_name: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        """gc_archive_import 的实际执行逻辑（不包审计）

        拆分出来便于 _start_gc_audit/_complete_gc_audit 在外层包裹。
        """
        # 1. 校验 path
        if not path:
            raise ValueError(
                t("errors.gc_archive_import_path_required",
                  default="archive path is required")
            )
        # 相对路径基于 gc_archives 目录解析（与 gc_archive_inspect 一致）
        if not os.path.isabs(path):
            archive_dir = os.path.join(os.path.dirname(self.db_path), "gc_archives")
            candidate = os.path.join(archive_dir, path)
            if os.path.isfile(candidate):
                path = candidate
            elif not path.endswith(".db.gz") and os.path.isfile(candidate + ".db.gz"):
                path = candidate + ".db.gz"
        if not os.path.isfile(path):
            raise FileNotFoundError(
                t("errors.gc_archive_import_not_found",
                  default="archive file not found: {path}", path=path)
            )

        # 2. 校验目标参数
        if not file_path and not package_name:
            raise ValueError(
                t("errors.gc_archive_import_no_target",
                  default="no import target specified (--file or --package is required)")
            )

        target = "file" if file_path else "package"
        target_value = file_path if file_path else package_name

        imported: Dict[str, int] = {
            "file_contents": 0,
            "file_versions": 0,
            "file_symbol_versions": 0,
            "call_versions": 0,
            "symbol_contents": 0,
            "package_versions": 0,
            "external_symbols": 0,
        }
        skipped: Dict[str, int] = {
            "file_versions": 0,
            "file_symbol_versions": 0,
            "call_versions": 0,
            "package_versions": 0,
            "external_symbols": 0,
        }
        errors: List[str] = []

        # 3. 解压备份到临时文件并只读打开
        fd, temp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with gzip.open(path, "rb") as src, open(temp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            backup_uri = f"file:{temp_path}?mode=ro"
            backup_con = sqlite3.connect(backup_uri, uri=True)
            backup_con.row_factory = sqlite3.Row
            try:
                if target == "file":
                    self._import_file_from_backup(
                        backup_con=backup_con,
                        file_path=file_path,
                        dry_run=dry_run,
                        imported=imported,
                        skipped=skipped,
                        errors=errors,
                    )
                else:
                    self._import_package_from_backup(
                        backup_con=backup_con,
                        package_name=package_name,
                        dry_run=dry_run,
                        imported=imported,
                        skipped=skipped,
                        errors=errors,
                    )
            finally:
                backup_con.close()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        if not dry_run:
            self.conn.commit()

        # 组装 candidate 数量（dry_run 时也返回统计）
        candidate = {k: imported[k] + skipped.get(k, 0) for k in imported if k in skipped}
        return {
            "path": path,
            "dry_run": dry_run,
            "target": target,
            "target_value": target_value,
            "imported": imported,
            "skipped": skipped,
            "candidate": candidate,
            "errors": errors,
        }

    def _import_file_from_backup(
        self,
        backup_con: sqlite3.Connection,
        file_path: str,
        dry_run: bool,
        imported: Dict[str, int],
        skipped: Dict[str, int],
        errors: List[str],
    ) -> None:
        """从备份库按 file_path 导回 file_versions 等数据到当前库

        流程：
        1. 备份库查 file_instance by rel_path
        2. 当前库查 file_instance by rel_path（不存在则全部 skipped）
        3. 遍历备份库 file_versions，逐条 INSERT OR IGNORE 到当前库
        4. 对每个 file_version，导回 file_symbol_versions / call_versions / symbol_contents
        """
        ws_id = self._get_active_workspace_id()

        # 1. 备份库找 file_instance
        cur = backup_con.execute(
            "SELECT id FROM file_instances WHERE rel_path = ? LIMIT 1",
            (file_path,),
        )
        row = cur.fetchone()
        if not row:
            errors.append(
                t("errors.gc_archive_import_file_not_in_backup",
                  default="file not found in backup: {path}", path=file_path)
            )
            return
        backup_file_id = row["id"]

        # 2. 当前库找 file_instance（按 rel_path + workspace_id）
        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ? LIMIT 1",
            (ws_id, file_path),
        )
        row = cur.fetchone()
        if not row:
            errors.append(
                t("errors.gc_archive_import_file_not_in_current",
                  default="file not found in current workspace (please --refresh-all first): {path}",
                  path=file_path)
            )
            return
        current_file_id = row["id"]

        # 3. 遍历备份库的 file_versions
        cur = backup_con.execute(
            """SELECT file_instance_id, version_num, content_hash, mtime, total_lines,
                      parsed_at, is_current, is_deleted, commit_hash
               FROM file_versions WHERE file_instance_id = ?""",
            (backup_file_id,),
        )
        backup_versions = [dict(r) for r in cur.fetchall()]
        for bv in backup_versions:
            # 3.1 检查当前库是否已存在相同 (file_instance_id, version_num)
            existing = self.conn.execute(
                "SELECT id FROM file_versions WHERE file_instance_id = ? AND version_num = ?",
                (current_file_id, bv["version_num"]),
            ).fetchone()
            if existing:
                skipped["file_versions"] += 1
                continue

            # 3.2 INSERT OR IGNORE file_contents
            fc_row = backup_con.execute(
                "SELECT content_hash, language, total_lines, first_seen_at FROM file_contents WHERE content_hash = ?",
                (bv["content_hash"],),
            ).fetchone()
            if fc_row:
                if not dry_run:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, ?, ?, ?)",
                        (fc_row["content_hash"], fc_row["language"], fc_row["total_lines"], fc_row["first_seen_at"]),
                    )
                imported["file_contents"] += 1

            # 3.3 INSERT INTO file_versions（不用 OR IGNORE，因为我们已检查过不存在）
            if not dry_run:
                cur_ins = self.conn.execute(
                    """INSERT INTO file_versions
                       (file_instance_id, version_num, content_hash, mtime, total_lines,
                        parsed_at, is_current, is_deleted, commit_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (current_file_id, bv["version_num"], bv["content_hash"], bv["mtime"],
                     bv["total_lines"], bv["parsed_at"], bv["is_current"],
                     bv["is_deleted"], bv["commit_hash"]),
                )
                new_fv_id = cur_ins.lastrowid
            else:
                # dry_run 时不需要写入，candidate 只统计
                imported["file_versions"] += 1
                continue

            imported["file_versions"] += 1

            # 3.4 找备份库对应的 file_version_id（通过 file_instance_id + version_num + parsed_at 唯一定位）
            backup_fv_row = backup_con.execute(
                "SELECT id FROM file_versions WHERE file_instance_id = ? AND version_num = ? AND parsed_at = ?",
                (backup_file_id, bv["version_num"], bv["parsed_at"]),
            ).fetchone()
            if not backup_fv_row:
                continue
            backup_fv_id = backup_fv_row["id"]

            # 3.5 导回 symbol_contents + file_symbol_versions
            fsv_rows = backup_con.execute(
                """SELECT fsv.id, fsv.symbol_hash, fsv.qualified_name, fsv.start_line, fsv.end_line,
                          fsv.module_path, fsv.depth, fsv.is_deleted,
                          sc.name, sc.kind, sc.content, sc.signature, sc.has_comment,
                          sc.comment_content, sc.qualified_name as sc_qualified
                   FROM file_symbol_versions fsv
                   LEFT JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
                   WHERE fsv.file_version_id = ?""",
                (backup_fv_id,),
            ).fetchall()
            for fsv in fsv_rows:
                # symbol_contents
                if fsv["symbol_hash"]:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO symbol_contents
                           (content_hash, name, kind, content, signature, has_comment,
                            comment_content, qualified_name)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (fsv["symbol_hash"], fsv["name"], fsv["kind"], fsv["content"],
                         fsv["signature"], fsv["has_comment"], fsv["comment_content"],
                         fsv["sc_qualified"] or ""),
                    )
                # file_symbol_versions
                existing_fsv = self.conn.execute(
                    """SELECT id FROM file_symbol_versions
                       WHERE file_version_id = ? AND symbol_hash = ? AND start_line = ?""",
                    (new_fv_id, fsv["symbol_hash"], fsv["start_line"]),
                ).fetchone()
                if existing_fsv:
                    skipped["file_symbol_versions"] += 1
                    continue
                self.conn.execute(
                    """INSERT INTO file_symbol_versions
                       (file_version_id, symbol_hash, qualified_name, start_line, end_line,
                        module_path, depth, is_deleted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (new_fv_id, fsv["symbol_hash"], fsv["qualified_name"],
                     fsv["start_line"], fsv["end_line"], fsv["module_path"],
                     fsv["depth"], fsv["is_deleted"]),
                )
                imported["file_symbol_versions"] += 1

            # 3.6 导回 call_versions
            cv_rows = backup_con.execute(
                """SELECT caller_qualified, caller_hash, callee_name, callee_module,
                          callee_qualified, callee_file, call_line, is_cross_file
                   FROM call_versions WHERE file_version_id = ?""",
                (backup_fv_id,),
            ).fetchall()
            for cv in cv_rows:
                existing_cv = self.conn.execute(
                    """SELECT id FROM call_versions
                       WHERE file_version_id = ? AND caller_qualified = ?
                         AND callee_name = ? AND call_line = ?""",
                    (new_fv_id, cv["caller_qualified"], cv["callee_name"], cv["call_line"]),
                ).fetchone()
                if existing_cv:
                    skipped["call_versions"] += 1
                    continue
                self.conn.execute(
                    """INSERT INTO call_versions
                       (file_version_id, caller_qualified, caller_hash, callee_name,
                        callee_module, callee_qualified, callee_file, call_line, is_cross_file)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (new_fv_id, cv["caller_qualified"], cv["caller_hash"],
                     cv["callee_name"], cv["callee_module"], cv["callee_qualified"],
                     cv["callee_file"], cv["call_line"], cv["is_cross_file"]),
                )
                imported["call_versions"] += 1

    def _import_package_from_backup(
        self,
        backup_con: sqlite3.Connection,
        package_name: str,
        dry_run: bool,
        imported: Dict[str, int],
        skipped: Dict[str, int],
        errors: List[str],
    ) -> None:
        """从备份库按 package_name 导回 package_versions / external_symbols 到当前库

        流程：
        1. 备份库查 package_versions by package_name
        2. 对每条 INSERT OR IGNORE package_versions
        3. 备份库查 external_symbols by package_name
        4. 对每条 INSERT OR IGNORE external_symbols
        """
        # 1. package_versions
        pkg_rows = backup_con.execute(
            """SELECT package_name, package_version, installed_at, last_seen_at,
                      last_used_at, import_source
               FROM package_versions WHERE package_name = ?""",
            (package_name,),
        ).fetchall()
        for pkg in pkg_rows:
            existing = self.conn.execute(
                """SELECT package_name FROM package_versions
                   WHERE package_name = ? AND package_version = ?""",
                (pkg["package_name"], pkg["package_version"]),
            ).fetchone()
            if existing:
                skipped["package_versions"] += 1
                continue
            if not dry_run:
                self.conn.execute(
                    """INSERT OR IGNORE INTO package_versions
                       (package_name, package_version, installed_at, last_seen_at,
                        last_used_at, import_source)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (pkg["package_name"], pkg["package_version"], pkg["installed_at"],
                     pkg["last_seen_at"], pkg["last_used_at"], pkg["import_source"]),
                )
            imported["package_versions"] += 1

        # 2. external_symbols
        ext_rows = backup_con.execute(
            """SELECT package_name, package_version, module_path, qualified_name,
                      symbol_name, symbol_kind, signature, docstring, source_file, imported_at
               FROM external_symbols WHERE package_name = ?""",
            (package_name,),
        ).fetchall()
        for ext in ext_rows:
            existing = self.conn.execute(
                "SELECT id FROM external_symbols WHERE qualified_name = ?",
                (ext["qualified_name"],),
            ).fetchone()
            if existing:
                skipped["external_symbols"] += 1
                continue
            if not dry_run:
                self.conn.execute(
                    """INSERT OR IGNORE INTO external_symbols
                       (package_name, package_version, module_path, qualified_name,
                        symbol_name, symbol_kind, signature, docstring, source_file, imported_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ext["package_name"], ext["package_version"], ext["module_path"],
                     ext["qualified_name"], ext["symbol_name"], ext["symbol_kind"],
                     ext["signature"], ext["docstring"], ext["source_file"], ext["imported_at"]),
                )
            imported["external_symbols"] += 1
