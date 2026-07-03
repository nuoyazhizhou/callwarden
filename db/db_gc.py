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

import os
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
