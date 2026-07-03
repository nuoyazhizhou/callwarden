"""
db_comment.py
=============

代码知识图谱注释恢复 Mixin 类。

提供历史注释恢复、版本对比、批量恢复等功能。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..config import atomic_write_file


class CommentMixin:
    """注释恢复功能 Mixin

    通过 self.conn 访问数据库连接，提供注释恢复相关功能。
    """

    def get_comment_from_version(self, spec: str) -> Optional[Dict]:
        """从历史版本获取注释（支持 fn@vN 或 fn@hash 格式）"""
        if "@" not in spec:
            return None

        parts = spec.split("@")
        qualified_name = parts[0]
        version_ref = parts[1]

        history = self.get_symbol_history(qualified_name)
        if not history:
            return None

        target = None
        if version_ref.startswith("v"):
            version_num = int(version_ref[1:])
            for h in history:
                if h["version_num"] == version_num:
                    target = h
                    break
        else:
            for h in history:
                if h["symbol_hash"].startswith(version_ref):
                    target = h
                    break

        if not target:
            return None

        content = self.get_symbol_content_by_hash(target["symbol_hash"])
        if not content:
            return None

        return {
            "qualified_name": qualified_name,
            "version_num": target["version_num"],
            "symbol_hash": target["symbol_hash"],
            "file_path": target["file_path"],
            "start_line": target["start_line"],
            "end_line": target["end_line"],
            "comment_content": content.get("comment_content", ""),
            "has_comment": content.get("has_comment", 0),
            "full_content": content.get("content", ""),
        }


    def restore_comment(self, spec: str, preview: bool = False) -> Dict:
        """恢复注释到当前文件"""
        info = self.get_comment_from_version(spec)
        if not info:
            return {"success": False, "error": f"未找到: {spec}"}

        if not info["has_comment"] or not info["comment_content"]:
            return {"success": False, "error": f"版本 v{info['version_num']} 没有注释"}

        abs_path = os.path.join(self.workspace_root, info["file_path"])
        if not os.path.exists(abs_path):
            return {"success": False, "error": f"文件不存在: {info['file_path']}"}

        with open(abs_path, "r", encoding="utf-8") as f:
            current_content = f.read()
        current_lines = current_content.split("\n")

        fn_name = info["qualified_name"].split("::")[-1]
        fn_line_idx = -1
        for i, line in enumerate(current_lines):
            stripped = line.strip()
            if stripped.startswith("fn ") or stripped.startswith("pub fn ") or stripped.startswith("pub(crate) fn ") or stripped.startswith("pub(super) fn ") or stripped.startswith("pub(self) fn ") or stripped.startswith("unsafe fn ") or stripped.startswith("pub unsafe fn "):
                parts = stripped.split()
                for j, p in enumerate(parts):
                    if p == "fn" and j + 1 < len(parts):
                        name_candidate = parts[j + 1].split("(")[0].split("<")[0]
                        if name_candidate == fn_name:
                            fn_line_idx = i
                            break
                if fn_line_idx >= 0:
                    break

        if fn_line_idx == -1:
            return {"success": False, "error": f"文件中未找到函数定义: {fn_name}"}

        has_current_comment = False
        for i in range(max(0, fn_line_idx - 5), fn_line_idx):
            if current_lines[i].strip().startswith("///"):
                has_current_comment = True
                break

        insert_idx = fn_line_idx
        while insert_idx > 0:
            prev_line = current_lines[insert_idx - 1].strip()
            if prev_line.startswith("///") or prev_line.startswith("//!") or prev_line.startswith("#[") or prev_line == "":
                insert_idx -= 1
            else:
                break

        comment_lines = info["comment_content"].split("\n")
        skip_idx = insert_idx
        while skip_idx < len(current_lines) and (current_lines[skip_idx].strip() == "" or current_lines[skip_idx].strip().startswith("///") or current_lines[skip_idx].strip().startswith("//!")):
            skip_idx += 1
        new_lines = current_lines[:insert_idx] + [""] + comment_lines + [""] + current_lines[skip_idx:]
        new_content = "\n".join(new_lines)

        if preview:
            return {
                "success": True,
                "preview": True,
                "file_path": info["file_path"],
                "qualified_name": info["qualified_name"],
                "old_comment": "有" if has_current_comment else "无",
                "new_comment": info["comment_content"],
                "insert_at_line": insert_idx + 1,
                "fn_at_line": fn_line_idx + 1,
                "new_content_preview": new_content[:500] + "..." if len(new_content) > 500 else new_content,
            }

        # SEC-001：原子写入，避免半写入状态
        atomic_write_file(abs_path, new_content)

        self.refresh_file(abs_path)

        return {
            "success": True,
            "file_path": info["file_path"],
            "qualified_name": info["qualified_name"],
            "restored_from": f"v{info['version_num']}",
            "comment_lines": len(comment_lines),
        }


    def restore_all_comments(self, preview: bool = False, file_filter: Optional[str] = None) -> Dict:
        """批量恢复所有有注释历史的函数注释

        Args:
            preview: 是否只预览不写入
            file_filter: 只恢复指定文件的注释（相对路径）

        Returns:
            恢复统计结果
        """
        ws_id = self._get_active_workspace_id()
        query = """
            SELECT 
                fsv.qualified_name,
                sc.comment_content,
                fi.rel_path as file_path,
                fv.parsed_at
            FROM file_symbol_versions fsv
            JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            JOIN file_versions fv ON fsv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND sc.has_comment = 1 AND sc.kind = 'fn' AND fsv.is_deleted = 0
        """

        params = [ws_id]
        if file_filter:
            query += " AND fi.rel_path = ?"
            params.append(file_filter)

        query += " ORDER BY fi.rel_path, fsv.qualified_name, fv.parsed_at DESC"

        cur = self.conn.execute(query, params)
        rows = cur.fetchall()

        latest_comments = {}
        for qname, comment, fpath, parsed_at in rows:
            key = (fpath, qname)
            if key not in latest_comments or parsed_at > latest_comments[key][1]:
                latest_comments[key] = (comment, parsed_at)

        results = {
            "total_found": len(latest_comments),
            "restored": 0,
            "skipped": 0,
            "failed": 0,
            "files": {},
            "errors": [],
        }

        by_file = {}
        for (fpath, qname), (comment, _) in latest_comments.items():
            if fpath not in by_file:
                by_file[fpath] = []
            by_file[fpath].append({
                "qualified_name": qname,
                "comment_content": comment,
            })

        for fpath, symbols in by_file.items():
            abs_path = os.path.join(self.workspace_root, fpath)
            if not os.path.exists(abs_path):
                results["skipped"] += len(symbols)
                results["errors"].append(f"文件不存在: {fpath}")
                continue

            with open(abs_path, "r", encoding="utf-8") as f:
                current_content = f.read()
            current_lines = current_content.split("\n")

            file_restored = 0
            file_failed = 0
            file_skipped = 0

            fn_positions = []
            for sym in symbols:
                fn_name = sym["qualified_name"].split("::")[-1]
                fn_line_idx = -1
                for i, line in enumerate(current_lines):
                    stripped = line.strip()
                    if stripped.startswith("fn ") or stripped.startswith("pub fn ") or stripped.startswith("pub(crate) fn ") or stripped.startswith("pub(super) fn ") or stripped.startswith("pub(self) fn ") or stripped.startswith("unsafe fn ") or stripped.startswith("pub unsafe fn "):
                        parts = stripped.split()
                        name_part = None
                        for j, p in enumerate(parts):
                            if p == "fn" and j + 1 < len(parts):
                                name_candidate = parts[j + 1].split("(")[0].split("<")[0]
                                name_part = name_candidate
                                break
                        if name_part == fn_name:
                            fn_line_idx = i
                            break
                if fn_line_idx >= 0:
                    fn_positions.append((fn_line_idx, sym))

            fn_positions.sort(key=lambda x: x[0], reverse=True)

            for fn_line_idx, sym in fn_positions:
                has_current_comment = False
                for i in range(max(0, fn_line_idx - 5), fn_line_idx):
                    if current_lines[i].strip().startswith("///"):
                        has_current_comment = True
                        break

                if has_current_comment:
                    file_skipped += 1
                    continue

                insert_idx = fn_line_idx
                while insert_idx > 0:
                    prev_line = current_lines[insert_idx - 1].strip()
                    if prev_line.startswith("///") or prev_line.startswith("//!") or prev_line.startswith("#[") or prev_line == "":
                        insert_idx -= 1
                    else:
                        break

                comment_lines = sym["comment_content"].split("\n")
                skip_idx = insert_idx
                while skip_idx < len(current_lines) and (current_lines[skip_idx].strip() == "" or current_lines[skip_idx].strip().startswith("///") or current_lines[skip_idx].strip().startswith("//!")):
                    skip_idx += 1

                new_lines = current_lines[:insert_idx] + [""] + comment_lines + [""] + current_lines[skip_idx:]
                current_lines = new_lines
                file_restored += 1

            if not preview and file_restored > 0:
                new_content = "\n".join(current_lines)
                # SEC-001：原子写入，避免半写入状态
                atomic_write_file(abs_path, new_content)
                self.refresh_file(abs_path)

            results["restored"] += file_restored
            results["failed"] += file_failed
            results["skipped"] += file_skipped
            results["files"][fpath] = {
                "restored": file_restored,
                "skipped": file_skipped,
                "failed": file_failed,
                "total": len(symbols),
            }

        return results


