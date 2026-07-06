"""
db.py
=====

代码知识图谱数据库核心类。

提供数据库基础操作、源码解析、版本管理、查询接口等核心功能。
通过 Mixin 模式扩展调用链分析、缺陷检测、覆盖率统计等高级功能。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from ..config import (
    norm_path, read_file_normalized,
    detect_language_from_path, get_supported_extensions, compute_content_hash,
    detect_project_root, get_default_workspace_name, get_project_db_path,
)
from ..i18n import t

# 计算项目根目录（callwarden 包自身的根目录）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGES_DIR = os.path.dirname(SCRIPT_DIR)  # callwarden/ 根目录
PROJECT_ROOT = PACKAGES_DIR

# 为 config 模块补充 PROJECT_ROOT（兼容其他模块的导入）
import sys
from .. import config as _config_module
if not hasattr(_config_module, 'PROJECT_ROOT'):
    _config_module.PROJECT_ROOT = PROJECT_ROOT

from .schema import SCHEMA_SQL, SCHEMA_VERSION
from ..parsers import RustParser, ModuleResolver, CallResolver, create_parser
from ..analyzers import CallChainMixin, IssueAnalyzerMixin, CoverageMixin
from ..cli.console import cprint, print_progress, clear_progress, Spinner, format_duration, print_build_summary


def _migrate_v1_to_v2(conn: sqlite3.Connection):
    """v1 -> v2: 新增 Semgrep 相关表和索引"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semgrep_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            rule_id TEXT NOT NULL,
            rule_name TEXT DEFAULT '',
            message TEXT DEFAULT '',
            severity TEXT DEFAULT 'INFO',
            confidence TEXT DEFAULT 'UNKNOWN',
            language TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            col INTEGER DEFAULT 0,
            end_col INTEGER DEFAULT 0,
            snippet TEXT DEFAULT '',
            fix TEXT DEFAULT '',
            symbol_id INTEGER DEFAULT 0,
            symbol_qualified TEXT DEFAULT '',
            scanned_at REAL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS semgrep_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT DEFAULT 'full',
            config TEXT DEFAULT '',
            started_at REAL NOT NULL,
            completed_at REAL DEFAULT 0,
            total_findings INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending'
        )
    """)

    # 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_file ON semgrep_findings(file_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_symbol ON semgrep_findings(symbol_qualified)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_severity ON semgrep_findings(severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_rule ON semgrep_findings(rule_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_lang ON semgrep_findings(language)")



def _migrate_v2_to_v3(conn: sqlite3.Connection):
    """v2 -> v3: hash 为主、path 为副、多工作区、删除标记

    迁移逻辑：
    - 创建 workspaces 表，插入默认工作区
    - 创建 file_contents 表，从现有 file_versions 中去重 content_hash 填充
    - 创建 file_instances 表，将旧 files 表数据迁移过来
    - 改造 symbols 表：新增 file_instance_id 替代 file_id，新增 symbol_hash
    - 改造 comments 表：symbol_hash 替代 symbol_id
    - 改造 calls 表：caller_id 关联新 symbols.id
    - 改造 file_versions 表：file_instance_id 替代 file_id，新增 is_deleted
    - 改造 file_symbol_versions 表：新增 is_deleted
    - 改造 semgrep_findings 表：file_instance_id 替代 file_id，新增 content_hash
    - 改造 semgrep_scans 表：新增 workspace_id
    - 更新所有索引

    注意：SQLite 不支持 DROP COLUMN，采用重命名旧表+建新表+迁移数据的方式
    """
    now = time.time()

    # ---- 1. 创建 workspaces 表 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            root_path TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            is_active INTEGER DEFAULT 0,
            description TEXT DEFAULT ''
        )
    """)

    # 插入默认工作区（用 PROJECT_ROOT 作为根目录）
    default_name = os.path.basename(os.path.normpath(PROJECT_ROOT))
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (name, root_path, created_at, is_active, description) VALUES (?, ?, ?, 1, '默认工作区（迁移自 v2）')",
        (default_name, norm_path(PROJECT_ROOT), now),
    )

    # ---- 2. 创建 file_contents 表 ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_contents (
            content_hash TEXT PRIMARY KEY,
            language TEXT DEFAULT '',
            total_lines INTEGER DEFAULT 0,
            first_seen_at REAL NOT NULL
        )
    """)

    # 从 file_versions 中去重 content_hash 填充
    conn.execute("""
        INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
        SELECT DISTINCT fv.content_hash, '', fv.total_lines, fv.parsed_at
        FROM file_versions fv
        WHERE fv.content_hash IS NOT NULL AND fv.content_hash != ''
    """)

    # ---- 3. 创建 file_instances 表（替代 files） ----
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT NOT NULL,
            current_content_hash TEXT DEFAULT '',
            mtime REAL NOT NULL,
            total_lines INTEGER DEFAULT 0,
            last_parsed REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            module_path TEXT DEFAULT '',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
            UNIQUE(workspace_id, rel_path)
        )
    """)

    # 迁移 files 表数据到 file_instances
    conn.execute("""
        INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
        SELECT
            1 as workspace_id,
            f.path as rel_path,
            f.abs_path,
            COALESCE((
                SELECT fv.content_hash FROM file_versions fv
                WHERE fv.file_id = f.id AND fv.is_current = 1
                LIMIT 1
            ), '') as current_content_hash,
            f.mtime,
            COALESCE((
                SELECT fv.total_lines FROM file_versions fv
                WHERE fv.file_id = f.id AND fv.is_current = 1
                LIMIT 1
            ), 0) as total_lines,
            COALESCE((
                SELECT fv.parsed_at FROM file_versions fv
                WHERE fv.file_id = f.id AND fv.is_current = 1
                LIMIT 1
            ), 0) as last_parsed,
            f.status,
            f.module_path
        FROM files f
    """)

    # ---- 4. 改造 symbols 表 ----
    # 重命名旧表
    conn.execute("ALTER TABLE symbols RENAME TO symbols_old_v2")

    # 创建新表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            symbol_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            visibility TEXT DEFAULT 'private',
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            start_col INTEGER DEFAULT 0,
            end_col INTEGER DEFAULT 0,
            signature TEXT DEFAULT '',
            has_comment INTEGER DEFAULT 0,
            comment_status TEXT DEFAULT 'pending',
            module_path TEXT DEFAULT '',
            qualified_name TEXT DEFAULT '',
            depth INTEGER DEFAULT -1,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)

    # 迁移数据：file_id -> file_instance_id，symbol_hash 从 file_symbol_versions 获取
    conn.execute("""
        INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, start_col, end_col, signature, has_comment, comment_status, module_path, qualified_name, depth)
        SELECT
            fi.id as file_instance_id,
            COALESCE(sc.content_hash, '') as symbol_hash,
            s.name,
            s.kind,
            s.visibility,
            s.start_line,
            s.end_line,
            s.start_col,
            s.end_col,
            s.signature,
            s.has_comment,
            s.comment_status,
            s.module_path,
            s.qualified_name,
            s.depth
        FROM symbols_old_v2 s
        JOIN file_instances fi ON fi.workspace_id = 1 AND fi.rel_path = (
            SELECT f.path FROM files f WHERE f.id = s.file_id
        )
        LEFT JOIN symbol_contents sc ON sc.qualified_name = s.qualified_name
        LEFT JOIN file_symbol_versions fsv ON fsv.qualified_name = s.qualified_name
        LEFT JOIN file_versions fv ON fv.id = fsv.file_version_id AND fv.file_id = s.file_id
        GROUP BY s.id
    """)

    # 删除旧表
    conn.execute("DROP TABLE symbols_old_v2")

    # ---- 5. 改造 comments 表 ----
    # 检查旧 comments 表是否存在（v2 可能没有）
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comments'")
    if cur.fetchone():
        conn.execute("ALTER TABLE comments RENAME TO comments_old_v2")

    # 创建新表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_hash TEXT NOT NULL,
            comment_type TEXT DEFAULT 'doc',
            content TEXT DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)

    # 迁移旧数据（如果有的话）
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='comments_old_v2'")
    if cur.fetchone():
        conn.execute("""
            INSERT INTO comments (symbol_hash, comment_type, content, created_at)
            SELECT
                COALESCE(sc.content_hash, '') as symbol_hash,
                c.comment_type,
                c.content,
                c.created_at
            FROM comments_old_v2 c
            LEFT JOIN symbol_contents sc ON sc.qualified_name = (
                SELECT s.qualified_name FROM symbols s WHERE s.id = c.symbol_id
            )
            WHERE c.symbol_id IS NOT NULL
        """)
        conn.execute("DROP TABLE comments_old_v2")

    # ---- 6. 改造 calls 表 ----
    conn.execute("ALTER TABLE calls RENAME TO calls_old_v2")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id INTEGER NOT NULL,
            caller_name TEXT NOT NULL,
            caller_module TEXT NOT NULL,
            callee_name TEXT NOT NULL,
            callee_module TEXT DEFAULT '',
            callee_qualified TEXT DEFAULT '',
            callee_file TEXT DEFAULT '',
            callee_id INTEGER DEFAULT 0,
            call_line INTEGER DEFAULT 0,
            is_cross_file INTEGER DEFAULT 0,
            FOREIGN KEY (caller_id) REFERENCES symbols(id)
        )
    """)

    # 迁移 calls：通过 qualified_name 重新关联新 symbols.id
    conn.execute("""
        INSERT INTO calls (caller_id, caller_name, caller_module, callee_name, callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file)
        SELECT
            COALESCE(s_caller.id, 0) as caller_id,
            c.caller_name,
            c.caller_module,
            c.callee_name,
            c.callee_module,
            c.callee_qualified,
            c.callee_file,
            COALESCE(s_callee.id, 0) as callee_id,
            c.call_line,
            c.is_cross_file
        FROM calls_old_v2 c
        LEFT JOIN symbols s_caller ON s_caller.qualified_name = (
            SELECT s2.qualified_name FROM symbols_old_v2 s2 WHERE s2.id = c.caller_id
        )
        LEFT JOIN symbols s_callee ON s_callee.qualified_name = c.callee_qualified
        WHERE s_caller.id IS NOT NULL
    """)

    conn.execute("DROP TABLE calls_old_v2")

    # ---- 7. 改造 file_versions 表 ----
    conn.execute("ALTER TABLE file_versions RENAME TO file_versions_old_v2")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            version_num INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            total_lines INTEGER DEFAULT 0,
            parsed_at REAL NOT NULL,
            is_current INTEGER DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
        )
    """)

    # 迁移数据：file_id -> file_instance_id
    conn.execute("""
        INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted)
        SELECT
            fi.id as file_instance_id,
            fv.version_num,
            fv.content_hash,
            fv.mtime,
            fv.total_lines,
            fv.parsed_at,
            fv.is_current,
            0 as is_deleted
        FROM file_versions_old_v2 fv
        JOIN file_instances fi ON fi.workspace_id = 1 AND fi.rel_path = (
            SELECT f.path FROM files f WHERE f.id = fv.file_id
        )
    """)

    conn.execute("DROP TABLE file_versions_old_v2")

    # ---- 8. 改造 file_symbol_versions 表 ----
    conn.execute("ALTER TABLE file_symbol_versions RENAME TO file_symbol_versions_old_v2")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_symbol_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_version_id INTEGER NOT NULL,
            symbol_hash TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            module_path TEXT DEFAULT '',
            depth INTEGER DEFAULT -1,
            is_deleted INTEGER DEFAULT 0,
            FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)

    # 迁移数据：通过 file_id 关联新 file_version_id
    conn.execute("""
        INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
        SELECT
            fv_new.id as file_version_id,
            fsv.symbol_hash,
            fsv.qualified_name,
            fsv.start_line,
            fsv.end_line,
            fsv.module_path,
            fsv.depth,
            0 as is_deleted
        FROM file_symbol_versions_old_v2 fsv
        JOIN file_versions_old_v2 fv_old ON fv_old.id = fsv.file_version_id
        JOIN file_versions fv_new ON fv_new.file_instance_id = (
            SELECT fi.id FROM file_instances fi
            WHERE fi.workspace_id = 1 AND fi.rel_path = (
                SELECT f.path FROM files f WHERE f.id = fv_old.file_id
            )
        ) AND fv_new.version_num = fv_old.version_num
    """)

    conn.execute("DROP TABLE file_symbol_versions_old_v2")

    # ---- 9. 改造 semgrep_findings 表 ----
    conn.execute("ALTER TABLE semgrep_findings RENAME TO semgrep_findings_old_v2")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS semgrep_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            content_hash TEXT DEFAULT '',
            rule_id TEXT NOT NULL,
            rule_name TEXT DEFAULT '',
            message TEXT DEFAULT '',
            severity TEXT DEFAULT 'INFO',
            confidence TEXT DEFAULT 'UNKNOWN',
            language TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            snippet TEXT DEFAULT '',
            fix TEXT DEFAULT '',
            symbol_id INTEGER DEFAULT 0,
            symbol_qualified TEXT DEFAULT '',
            scanned_at REAL DEFAULT 0,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            UNIQUE(content_hash, rule_id, start_line)
        )
    """)

    # 迁移数据
    conn.execute("""
        INSERT INTO semgrep_findings (file_instance_id, content_hash, rule_id, rule_name, message, severity, confidence, language, start_line, end_line, snippet, fix, symbol_id, symbol_qualified, scanned_at)
        SELECT
            fi.id as file_instance_id,
            COALESCE(fi.current_content_hash, '') as content_hash,
            sf.rule_id,
            sf.rule_name,
            sf.message,
            sf.severity,
            sf.confidence,
            sf.language,
            sf.start_line,
            sf.end_line,
            sf.snippet,
            sf.fix,
            sf.symbol_id,
            sf.symbol_qualified,
            sf.scanned_at
        FROM semgrep_findings_old_v2 sf
        JOIN file_instances fi ON fi.workspace_id = 1 AND fi.rel_path = (
            SELECT f.path FROM files f WHERE f.id = sf.file_id
        )
    """)

    conn.execute("DROP TABLE semgrep_findings_old_v2")

    # ---- 10. 改造 semgrep_scans 表 ----
    conn.execute("ALTER TABLE semgrep_scans RENAME TO semgrep_scans_old_v2")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS semgrep_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT DEFAULT 'full',
            config TEXT DEFAULT '',
            workspace_id INTEGER DEFAULT 0,
            started_at REAL NOT NULL,
            completed_at REAL DEFAULT 0,
            total_findings INTEGER DEFAULT 0,
            files_scanned INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running'
        )
    """)

    # 迁移数据
    conn.execute("""
        INSERT INTO semgrep_scans (scan_type, config, workspace_id, started_at, completed_at, total_findings, files_scanned, status)
        SELECT scan_type, config, 1 as workspace_id, started_at, completed_at, total_findings, 0 as files_scanned, status
        FROM semgrep_scans_old_v2
    """)

    conn.execute("DROP TABLE semgrep_scans_old_v2")

    # ---- 11. 删除旧 files 表 ----
    conn.execute("DROP TABLE IF EXISTS files")

    # ---- 12. 创建所有索引 ----
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workspaces_active ON workspaces(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_contents_lang ON file_contents(language)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_instances_workspace ON file_instances(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_instances_hash ON file_instances(current_content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_instances_relpath ON file_instances(rel_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_hash ON symbols(symbol_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols(module_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_callee_qualified ON calls(callee_qualified)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(symbol_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_hash ON file_versions(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_file ON file_symbol_versions(file_version_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_hash ON file_symbol_versions(symbol_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_qualified ON file_symbol_versions(qualified_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_versions_file ON call_versions(file_version_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_call_versions_caller ON call_versions(caller_qualified)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_instance ON semgrep_findings(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_hash ON semgrep_findings(content_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_rule ON semgrep_findings(rule_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_severity ON semgrep_findings(severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_symbol ON semgrep_findings(symbol_qualified)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_semgrep_language ON semgrep_findings(language)")



def _migrate_v3_to_v4(conn: sqlite3.Connection):
    """v3 -> v4: Git 集成：关联 commit，查看变更影响

    - 创建 git_commits 表：存储 commit 信息（hash、message、author、time）
    - 创建 git_file_changes 表：关联 commit 和文件变更
    - 创建 git_symbol_changes 表：关联 commit 和符号变更
    - 为 file_versions 添加 commit_hash 字段
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS git_commits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_hash TEXT UNIQUE NOT NULL,
            message TEXT DEFAULT '',
            author TEXT DEFAULT '',
            email TEXT DEFAULT '',
            timestamp REAL NOT NULL,
            workspace_id INTEGER NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS git_file_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_hash TEXT NOT NULL,
            file_instance_id INTEGER NOT NULL,
            change_type TEXT NOT NULL,
            old_content_hash TEXT DEFAULT '',
            new_content_hash TEXT DEFAULT '',
            FOREIGN KEY (commit_hash) REFERENCES git_commits(commit_hash),
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS git_symbol_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_hash TEXT NOT NULL,
            symbol_hash TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_content TEXT DEFAULT '',
            new_content TEXT DEFAULT '',
            FOREIGN KEY (commit_hash) REFERENCES git_commits(commit_hash),
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)

    conn.execute("ALTER TABLE file_versions ADD COLUMN commit_hash TEXT DEFAULT ''")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_commits_hash ON git_commits(commit_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_commits_workspace ON git_commits(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_commits_timestamp ON git_commits(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_file_changes_commit ON git_file_changes(commit_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_file_changes_file ON git_file_changes(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_symbol_changes_commit ON git_symbol_changes(commit_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_git_symbol_changes_symbol ON git_symbol_changes(symbol_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_commit ON file_versions(commit_hash)")



def _migrate_v4_to_v5(conn: sqlite3.Connection):
    """v4 -> v5: 添加向量嵌入表（sqlite-vec）

    - 创建 symbol_embeddings 表：按 symbol_hash 关联符号内容，存储语义向量
    - 默认模型 jina-v2-base-code，维度 768
    - 新增 model_version 索引便于按模型筛选
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_embeddings (
            symbol_hash TEXT PRIMARY KEY,
            embedding BLOB NOT NULL,
            model_version TEXT NOT NULL DEFAULT 'jina-v2-base-code',
            dim INTEGER NOT NULL DEFAULT 768,
            embedded_at REAL NOT NULL,
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON symbol_embeddings(model_version)")


def _migrate_v5_to_v6(conn: sqlite3.Connection):
    """v5 -> v6: 添加符号摘要表

    - 创建 symbol_summaries 表：按 symbol_hash 关联符号内容，存储 AI 生成的摘要
    - 支持版本化（version 字段），同一符号可保留多版本历史摘要
    - is_current 标记当前使用的摘要版本
    - 新增 hash 和 current 索引便于按符号查询和过滤当前摘要
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_hash TEXT NOT NULL,
            summary TEXT NOT NULL,
            model TEXT DEFAULT 'manual',
            version INTEGER NOT NULL DEFAULT 1,
            is_current INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_hash ON symbol_summaries(symbol_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_current ON symbol_summaries(is_current)")


def _migrate_v6_to_v7(conn: sqlite3.Connection):
    """v6 -> v7: 添加任务管理表（任务驱动 MCP 的核心）

    - 创建 tasks 表：管理 agent 创建的结构化任务
    - 创建 task_steps 表：每个任务包含多个有序步骤
    - 创建 change_audit 表：记录每个步骤对文件的修改（hash + diff）
    - 新增 task_steps 的 task_id / status 索引和 change_audit 的 task_id 索引
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            creator TEXT NOT NULL DEFAULT 'agent',
            status TEXT NOT NULL DEFAULT 'open',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            closed_at REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_steps (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_file TEXT DEFAULT '',
            target_symbol TEXT DEFAULT '',
            check_items TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_audit (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT,
            file_path TEXT NOT NULL,
            hash_before TEXT DEFAULT '',
            hash_after TEXT DEFAULT '',
            diff TEXT DEFAULT '',
            author TEXT NOT NULL DEFAULT 'agent',
            timestamp REAL NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_task ON change_audit(task_id)")


def _migrate_v7_to_v8(conn: sqlite3.Connection):
    """v7 -> v8: 添加文件所有权表

    - 创建 file_ownership 表：记录每个文件实例的负责人
    - source 字段标识来源（codeowners / git_blame）
    - 同时保留最近一次 commit 的作者信息，便于综合判断负责人
    - 新增 file_instance_id 索引便于按文件查询所有权
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            owner TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'codeowners',
            confidence REAL DEFAULT 1.0,
            last_commit_hash TEXT DEFAULT '',
            last_commit_author TEXT DEFAULT '',
            last_commit_time REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ownership_file ON file_ownership(file_instance_id)")


def _migrate_v8_to_v9(conn: sqlite3.Connection):
    """v8 -> v9: 添加覆盖率数据表

    - 创建 coverage_data 表：存储从 LCOV/Cobertura 报告导入的行级覆盖率数据
    - file_instance_id 关联到 file_instances，symbol_id 关联到 symbols（可为空）
    - report_source 标识来源（lcov / cobertura）
    - 新增 file_instance_id / symbol_id / report_source 索引便于按文件、符号、来源查询
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coverage_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            symbol_id INTEGER,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            hit_count INTEGER DEFAULT 0,
            report_source TEXT DEFAULT 'lcov',
            imported_at REAL NOT NULL,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (symbol_id) REFERENCES symbols(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coverage_file ON coverage_data(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coverage_symbol ON coverage_data(symbol_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coverage_source ON coverage_data(report_source)")


def _migrate_v9_to_v10(conn: sqlite3.Connection):
    """v9 -> v10: 守护者架构表（生产安全护栏 + 变更影响 + 演化智能 + 缺陷知识库）

    - 创建 guardrail_rules 表：可阻断的规则定义
    - 创建 guardrail_findings 表：规则扫描结果
    - 创建 change_impacts 表：跨层变更影响记录
    - 创建 evolution_metrics 表：演化指标缓存
    - 创建 defect_patterns 表：缺陷模式库
    - 创建 defect_fixes 表：缺陷修复案例
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guardrail_rules (
            rule_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warn',
            pattern TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT 'warn',
            description TEXT DEFAULT '',
            is_builtin INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guardrail_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            symbol_hash TEXT DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'warn',
            status TEXT NOT NULL DEFAULT 'open',
            message TEXT DEFAULT '',
            detected_at REAL NOT NULL,
            resolved_at REAL,
            FOREIGN KEY (rule_id) REFERENCES guardrail_rules(rule_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guardrail_findings_rule ON guardrail_findings(rule_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guardrail_findings_file ON guardrail_findings(file_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guardrail_findings_severity ON guardrail_findings(severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_guardrail_findings_status ON guardrail_findings(status)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_impacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_symbol TEXT NOT NULL,
            impact_type TEXT NOT NULL,
            target_symbol TEXT NOT NULL,
            target_layer TEXT NOT NULL DEFAULT 'code',
            confidence REAL DEFAULT 1.0,
            detected_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_change_impacts_source ON change_impacts(source_symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_change_impacts_layer ON change_impacts(target_layer)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_metrics (
            symbol_hash TEXT PRIMARY KEY,
            change_count INTEGER DEFAULT 0,
            defect_count INTEGER DEFAULT 0,
            hotspot_score REAL DEFAULT 0.0,
            first_seen REAL,
            last_changed_at REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_hotspot ON evolution_metrics(hotspot_score)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS defect_patterns (
            pattern_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            detection_rule TEXT DEFAULT '',
            fix_template TEXT DEFAULT '',
            severity TEXT DEFAULT 'warn',
            learned_from TEXT DEFAULT '',
            case_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_defect_patterns_category ON defect_patterns(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_defect_patterns_severity ON defect_patterns(severity)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS defect_fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_id TEXT,
            symbol_hash TEXT DEFAULT '',
            before_hash TEXT DEFAULT '',
            after_hash TEXT DEFAULT '',
            fix_diff TEXT DEFAULT '',
            effectiveness REAL DEFAULT 0.0,
            created_at REAL NOT NULL,
            FOREIGN KEY (pattern_id) REFERENCES defect_patterns(pattern_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_defect_fixes_pattern ON defect_fixes(pattern_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_defect_fixes_symbol ON defect_fixes(symbol_hash)")


def _migrate_v10_to_v11(conn: sqlite3.Connection):
    """v10 -> v11: Token 节省账本表

    - 创建 token_savings_ledger 表：记录每次 Agent 操作（RAG / 调用链 / 摘要 / 注释恢复）节省的 token 数
    - 用于宣传利器（"已为你节省 N tokens"）和优化依据（哪些操作节省最多）
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_savings_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL,
            workspace_id INTEGER,
            agent_task_id TEXT DEFAULT '',
            original_tokens INTEGER DEFAULT 0,
            actual_tokens INTEGER DEFAULT 0,
            tokens_saved INTEGER DEFAULT 0,
            savings_pct REAL DEFAULT 0.0,
            detail TEXT DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_savings_op ON token_savings_ledger(operation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_savings_workspace ON token_savings_ledger(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_savings_created ON token_savings_ledger(created_at)")


def _migrate_v11_to_v12(conn: sqlite3.Connection):
    """v11 -> v12: 安全文件编辑审计表（Agent OS 核心：propose_edit 安全编辑流水线）

    - 创建 file_edit_audit 表：记录 Agent 通过 propose_edit 提交的每一次编辑
    - 字段包含 file_hash_before/after（SHA-256 校验）、diff_summary、status 状态机
    - status 流转：pending -> applied / reverted / failed
    - 关联 workspace_id 和 agent_task_id，便于按工作区/任务追溯
    - 新增 file_path / status / agent_task_id 三个索引，支持按文件、状态、任务查询
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_edit_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            file_path TEXT NOT NULL,
            operation TEXT NOT NULL,
            file_hash_before TEXT DEFAULT '',
            file_hash_after TEXT DEFAULT '',
            symbol_hash TEXT DEFAULT '',
            agent_task_id TEXT DEFAULT '',
            diff_summary TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            applied_at REAL DEFAULT 0,
            reverted_at REAL DEFAULT 0,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edit_audit_file ON file_edit_audit(file_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edit_audit_status ON file_edit_audit(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edit_audit_task ON file_edit_audit(agent_task_id)")


def _migrate_v12_to_v13(conn: sqlite3.Connection):
    """v12 -> v13: 跨仓库分析表

    - 创建 cross_repo_deps 表：记录仓库间的依赖关系（import / call / shared_symbol）
    - 用于跨仓库影响分析、共享代码识别、依赖变更传播追踪
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cross_repo_deps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_workspace_id INTEGER NOT NULL,
            target_workspace_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL,
            source_symbol_hash TEXT DEFAULT '',
            target_symbol_hash TEXT DEFAULT '',
            evidence TEXT DEFAULT '',
            confidence REAL DEFAULT 0.0,
            detected_at REAL NOT NULL,
            FOREIGN KEY (source_workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY (target_workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_repo_source ON cross_repo_deps(source_workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_repo_target ON cross_repo_deps(target_workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_repo_type ON cross_repo_deps(dependency_type)")


def _migrate_v13_to_v14(conn: sqlite3.Connection):
    """v13 -> v14: 归档表（类 Java GC 老年代）

    - 创建 archived_files 表：被 .gitignore / .callwardenignore 命中的文件迁出主表
    - 保留 file_instance_id 关联和符号/调用数统计，便于取消 ignore 后复活
    - file_instances.status 新增 'archived' 状态（无需 ALTER，status 是 TEXT）
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS archived_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT NOT NULL,
            content_hash TEXT DEFAULT '',
            symbol_count INTEGER DEFAULT 0,
            call_count INTEGER DEFAULT 0,
            archive_reason TEXT DEFAULT '',
            archived_at REAL NOT NULL,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_files_instance ON archived_files(file_instance_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_files_workspace ON archived_files(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_files_path ON archived_files(rel_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archived_files_hash ON archived_files(content_hash)")


def _migrate_v14_to_v15(conn: sqlite3.Connection):
    """v14 -> v15: 父子任务支持

    - tasks 表增加 parent_id / depth / sort_order 三列
    - 已有任务默认 parent_id='', depth=0, sort_order=0
    - 增加 parent_id 和 status 索引，加速任务树遍历
    """
    # SQLite 不支持 ADD COLUMN IF NOT EXISTS，用 PRAGMA table_info 检测
    cur = conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in cur.fetchall()}

    if "parent_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT DEFAULT ''")
    if "depth" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
    if "sort_order" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")

    # 已有任务设置默认值
    conn.execute("UPDATE tasks SET parent_id = '' WHERE parent_id IS NULL OR parent_id = ''")
    conn.execute("UPDATE tasks SET depth = 0 WHERE depth IS NULL")
    conn.execute("UPDATE tasks SET sort_order = 0 WHERE sort_order IS NULL")

    # 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")


def _migrate_v15_to_v16(conn: sqlite3.Connection):
    """v15 -> v16: 外部符号表（标准库 + 第三方包）

    - external_symbols: 存储项目外部的符号信息
    - package_versions: 记录已导入的包及其版本
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL,
            package_version TEXT DEFAULT '',
            module_path TEXT NOT NULL,
            qualified_name TEXT NOT NULL UNIQUE,
            symbol_name TEXT NOT NULL,
            symbol_kind TEXT DEFAULT 'fn',
            signature TEXT DEFAULT '',
            docstring TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            imported_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_symbols_name ON external_symbols(symbol_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_symbols_qualified ON external_symbols(qualified_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_symbols_package ON external_symbols(package_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_symbols_module ON external_symbols(module_path)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS package_versions (
            package_name TEXT NOT NULL,
            package_version TEXT NOT NULL,
            installed_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
            PRIMARY KEY (package_name, package_version)
        )
    """)


def _migrate_v16_to_v17(conn: sqlite3.Connection):
    """v16 -> v17: 任务-符号变更归因表"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_symbol_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            task_id TEXT NOT NULL,
            step_id TEXT DEFAULT '',
            edit_audit_id INTEGER DEFAULT 0,
            change_audit_id TEXT DEFAULT '',
            file_path TEXT NOT NULL,
            qualified_name TEXT DEFAULT '',
            symbol_name TEXT DEFAULT '',
            symbol_hash_before TEXT DEFAULT '',
            symbol_hash_after TEXT DEFAULT '',
            change_type TEXT NOT NULL DEFAULT 'modified',
            source TEXT NOT NULL DEFAULT 'manual',
            metadata TEXT DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_task ON task_symbol_changes(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_step ON task_symbol_changes(step_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_edit ON task_symbol_changes(edit_audit_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_file ON task_symbol_changes(file_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_before ON task_symbol_changes(symbol_hash_before)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_after ON task_symbol_changes(symbol_hash_after)")


def _migrate_v17_to_v18(conn: sqlite3.Connection):
    """v17 -> v18: 外部包冷数据追踪字段"""
    cur = conn.execute("PRAGMA table_info(package_versions)")
    cols = {row[1] for row in cur.fetchall()}

    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE package_versions ADD COLUMN last_seen_at REAL NOT NULL DEFAULT 0")
        conn.execute("UPDATE package_versions SET last_seen_at = installed_at WHERE last_seen_at IS NULL OR last_seen_at = 0")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE package_versions ADD COLUMN last_used_at REAL DEFAULT 0")
    if "import_source" not in cols:
        conn.execute("ALTER TABLE package_versions ADD COLUMN import_source TEXT DEFAULT 'external'")


def _migrate_v18_to_v19(conn: sqlite3.Connection):
    """v18 -> v19: GC retention 策略表"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gc_policies (
            workspace_id INTEGER PRIMARY KEY,
            older_than_days INTEGER NOT NULL DEFAULT 365,
            keep_versions INTEGER NOT NULL DEFAULT 100,
            include_external INTEGER NOT NULL DEFAULT 0,
            external_stale_days INTEGER NOT NULL DEFAULT 365,
            backup_enabled INTEGER NOT NULL DEFAULT 1,
            vacuum_enabled INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)


def _migrate_v19_to_v20(conn: sqlite3.Connection):
    """v19 -> v20: GC 运行审计表

    记录每次 retention/archive/purge 的策略参数、候选数量、实删数量、
    备份路径、起止时间和状态，便于事后追溯"为什么少了数据"。
    使用 CREATE TABLE IF NOT EXISTS 保证旧库重复执行幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gc_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            operation TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            policy_json TEXT DEFAULT '',
            candidate_counts TEXT DEFAULT '{}',
            deleted_counts TEXT DEFAULT '{}',
            backup_path TEXT DEFAULT '',
            backup_size INTEGER DEFAULT 0,
            started_at REAL NOT NULL,
            completed_at REAL,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT DEFAULT '',
            operator TEXT DEFAULT 'cli',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gc_runs_workspace ON gc_runs(workspace_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gc_runs_operation ON gc_runs(operation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gc_runs_status ON gc_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gc_runs_started ON gc_runs(started_at)")


def _migrate_v20_to_v21(conn: sqlite3.Connection):
    """v20 -> v21: 任务质量门禁发现表

    承载任务完成门禁发现，区别于通用 guardrail_findings：
    把 Semgrep、复杂度、调用链一致性、scope violation、i18n 硬编码等
    质量问题挂到 task/step 上，使 open error/block finding 阻止任务进入 done。
    使用 CREATE TABLE IF NOT EXISTS 保证旧库重复执行幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_quality_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            task_id TEXT NOT NULL,
            step_id TEXT DEFAULT '',
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warn',
            status TEXT NOT NULL DEFAULT 'open',
            message TEXT NOT NULL,
            evidence TEXT DEFAULT '',
            source TEXT DEFAULT '',
            created_at REAL NOT NULL,
            resolved_at REAL,
            resolved_by TEXT DEFAULT '',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_quality_task ON task_quality_findings(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_quality_step ON task_quality_findings(step_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_quality_status ON task_quality_findings(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_quality_severity ON task_quality_findings(severity)")


def _migrate_v21_to_v22(conn: sqlite3.Connection):
    """v21 -> v22: 审计签名链表

    为关键审计表（task_quality_findings / change_audit / file_edit_audit 等）
    生成可验证的 hash/HMAC 链，防止误改或有意篡改。
    每条记录包含 payload_hash + prev_signature + record_signature，形成链式结构。
    第一阶段使用 SHA-256 链（signing_key_id='local'）；第二阶段可切换到 HMAC。
    使用 CREATE TABLE IF NOT EXISTS 保证旧库重复执行幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_chain (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            operation TEXT NOT NULL DEFAULT 'insert',
            payload_hash TEXT NOT NULL,
            prev_signature TEXT DEFAULT '',
            record_signature TEXT NOT NULL,
            signing_key_id TEXT DEFAULT 'local',
            signed_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_chain_table_record ON audit_chain(table_name, record_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_chain_signature ON audit_chain(record_signature)")


def _migrate_v22_to_v23(conn: sqlite3.Connection):
    """v22 -> v23: Agent Rule Memory 表

    新增三张表，承载"项目规则记忆"全链路：
    - agent_rule_candidates: 候选规则（默认 pending，需 accept 后才生效）
    - agent_rules: 已接受规则（status=active 才参与上下文注入和 AGENTS.md 同步）
    - agent_rule_sync_log: 同步日志（记录每次 AGENTS.md 同步的摘要，便于审计追溯）

    设计原则：
    1. 候选规则不直接生效，避免误注入到 Agent 上下文。
    2. accepted 规则通过 get_applicable_rules 按 scope 匹配后注入到
       task_next_step / work_next_job / get_symbol / file_symbol_content。
    3. AGENTS.md 同步默认 dry-run，apply 时只改 marker block，不触碰人工维护内容。

    使用 CREATE TABLE IF NOT EXISTS 保证旧库重复执行幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_rule_candidates (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            rule_text TEXT NOT NULL,
            scope_json TEXT DEFAULT '{}',
            severity TEXT DEFAULT 'info',
            source TEXT DEFAULT 'manual',
            evidence_json TEXT DEFAULT '{}',
            confidence REAL DEFAULT 0.0,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            reviewed_at REAL,
            reviewer TEXT DEFAULT '',
            linked_rule_id TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_status ON agent_rule_candidates(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_source ON agent_rule_candidates(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_severity ON agent_rule_candidates(severity)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_rules (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            rule_text TEXT NOT NULL,
            scope_json TEXT DEFAULT '{}',
            severity TEXT DEFAULT 'info',
            status TEXT DEFAULT 'active',
            source_candidate_id TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            synced_to_agents_md INTEGER DEFAULT 0,
            sync_hash TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rules_status ON agent_rules(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rules_severity ON agent_rules(severity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rules_synced ON agent_rules(synced_to_agents_md)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_rule_sync_log (
            id TEXT PRIMARY KEY,
            target_path TEXT NOT NULL,
            rule_ids_json TEXT DEFAULT '[]',
            before_hash TEXT DEFAULT '',
            after_hash TEXT DEFAULT '',
            dry_run INTEGER DEFAULT 1,
            created_at REAL NOT NULL,
            actor TEXT DEFAULT 'agent'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rule_sync_log_target ON agent_rule_sync_log(target_path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_rule_sync_log_created ON agent_rule_sync_log(created_at)")


def _migrate_v23_to_v24(conn: sqlite3.Connection):
    """v23 -> v24: tasks 表新增 applied_at 字段

    任务状态机设计为 open → in_progress → review → applied → closed，
    但之前只实现了 open → in_progress → review，缺少 review → applied → closed 转换。

    新增 applied_at 字段记录 review → applied 的审核通过时间，与 closed_at 配合
    完成完整状态机。设计原则：写代码的 Agent 不能自己 applied/closed，必须由其他
    会话的 LLM 审核 applied 和 closed，避免奖励函数激励直接 close。

    使用 PRAGMA table_info 检测字段是否存在，保证幂等。
    """
    cur = conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in cur.fetchall()}
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN applied_at REAL")


def _migrate_v24_to_v25(conn: sqlite3.Connection):
    """v24 -> v25: 新增 workspace_scan_runs 表（自举扫描基线）

    自举闭环需要记录每次 capture/review 的基线（commit / status_hash / mtime / manifest），
    用于判断两次扫描之间真实变更了哪些文件，并关联 task/step。

    设计决策：
    - 不在 workspaces 表加字段，而是新建独立 scan run 表（一个 workspace 可有多次扫描）
    - Git 项目优先用 git_head + git_status_hash；非 Git 项目回退到 root_mtime + manifest_hash
    - 第一阶段不实现 workspace_scan_files（逐文件 manifest 表），优先复用 file_instances.mtime

    使用 CREATE TABLE IF NOT EXISTS 保证幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'bootstrap',
            task_id TEXT DEFAULT '',
            step_id TEXT DEFAULT '',
            baseline_type TEXT NOT NULL DEFAULT 'git',
            git_head TEXT DEFAULT '',
            git_merge_base TEXT DEFAULT '',
            git_status_hash TEXT DEFAULT '',
            root_mtime REAL DEFAULT 0,
            file_count INTEGER DEFAULT 0,
            manifest_hash TEXT DEFAULT '',
            changed_files_json TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{}',
            started_at REAL NOT NULL,
            completed_at REAL,
            status TEXT DEFAULT 'running',
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_workspace
        ON workspace_scan_runs(workspace_id, purpose, started_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_task
        ON workspace_scan_runs(task_id, step_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_git_head
        ON workspace_scan_runs(git_head)
    """)


class CodeGraphBase:
    """代码知识图谱数据库核心基类

    提供数据库连接管理、schema 迁移、工作区管理等核心功能。
    """

    def __init__(self, db_path: str = "", workspace_root: Optional[str] = None):
        """初始化代码图谱数据库实例

        工作流程：
        1. 检测并设置工作区根目录（优先使用传入路径，否则从调用路径向上探测）
        2. 计算数据库文件路径（默认 ~/.callwarden/<hash>/callwarden.db）
        3. 建立 SQLite 连接并应用性能优化 PRAGMA
        4. 初始化解析器、模块解析器、调用关系解析器
        5. 初始化 schema（自动版本化迁移，保留数据）
        6. 初始化工作区记录

        Args:
            db_path: 数据库文件路径，为空时按工作区自动计算
            workspace_root: 工作区根目录，为空时自动探测
        """
        # 自动检测项目根目录
        if workspace_root:
            self.workspace_root = norm_path(os.path.abspath(workspace_root))
        else:
            detected = detect_project_root(PROJECT_ROOT)
            self.workspace_root = detected if detected else PROJECT_ROOT

        # 架构修改：一个项目一个 SQLite 数据库
        # db_path 为空时，根据项目根路径自动计算 $HOME/.callwarden/16位hash/callwarden.db
        if not db_path:
            db_path = get_project_db_path(self.workspace_root)

        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 连接重试 + busy_timeout 前置
        # 关键修复：必须在执行任何 SQL（包括 SELECT 1）之前先设置 busy_timeout，
        # 否则当 MCP Server 持有 EXCLUSIVE 锁时，SELECT 1 会立即失败（默认 0ms）。
        # busy_timeout=5000：5 秒内锁释放立即返回；超时则快速失败让上层友好提示重试，
        # 避免 CLI 卡死 30 秒（原 30000ms 过长，用户无法接受）。
        self.conn = None
        _last_err = None
        for _attempt in range(3):
            try:
                self.conn = sqlite3.connect(db_path)
                self.conn.row_factory = sqlite3.Row
                # 前置 busy_timeout：内核级锁等待，5 秒内锁释放立即返回
                self.conn.execute("PRAGMA busy_timeout=5000")
                # 前置 WAL：确保 -wal/-shm 文件存在，避免 rollback journal 全库锁
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("SELECT 1").fetchone()
                break
            except sqlite3.OperationalError as _e:
                _last_err = _e
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                self.conn = None
                # 仅在内核级 30 秒等待仍失败时才重试（通常是长事务或崩溃残留）
                import time as _time
                _time.sleep(0.5 * (_attempt + 1))
        if self.conn is None:
            raise _last_err if _last_err else sqlite3.OperationalError("connect failed")
        # 性能优化 PRAGMA（WAL 和 busy_timeout 已在连接重试阶段前置）
        # synchronous=NORMAL：WAL 模式下仅在 checkpoint 时 fsync，比 FULL 快 5-10 倍
        # cache_size=-64000：64MB 内存页缓存，减少磁盘 I/O
        # temp_store=MEMORY：临时表和排序在内存中完成
        # mmap_size=268435456：256MB 内存映射，大数据库随机读更快
        # locking_mode=NORMAL：保持并发读写能力（不要用 EXCLUSIVE，会阻塞其他连接）
        # foreign_keys=OFF：入库期间关闭外键检查，避免每次 INSERT 触发引用完整性校验
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA mmap_size=268435456")
        self.conn.execute("PRAGMA locking_mode=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.parser = RustParser()

        self.module_resolver = ModuleResolver(self.workspace_root)
        self.call_resolver = CallResolver(self.module_resolver, self.parser)

        # 活动工作区
        self.active_workspace: Optional[Dict[str, Any]] = None

        self._init_schema()
        self._init_workspace()


    def _init_schema(self):
        """初始化数据库 Schema，支持版本化自动迁移（保留数据）"""
        # 确保 schema_version 表存在
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL,
                description TEXT DEFAULT ''
            )
        """)
        self.conn.commit()

        # 获取当前版本
        current_version = self._get_current_version()

        if current_version == 0:
            # 全新数据库：直接执行完整 SCHEMA_SQL 并记录版本
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, time.time(), "初始 schema"),
            )
            self.conn.commit()
        elif current_version < SCHEMA_VERSION:
            # 需要迁移：按版本顺序执行增量迁移
            self._migrate_schema(current_version, SCHEMA_VERSION)
        # current_version == SCHEMA_VERSION: 无需操作


    def _get_current_version(self) -> int:
        """获取当前数据库 schema 版本"""
        try:
            cur = self.conn.execute("SELECT MAX(version) as v FROM schema_version")
            row = cur.fetchone()
            return row["v"] if row and row["v"] is not None else 0
        except Exception:
            return 0


    def _migrate_schema(self, from_version: int, to_version: int):
        """按版本顺序执行增量迁移

        Args:
            from_version: 当前版本
            to_version: 目标版本
        """
        import traceback

        migrations = self._get_migrations()

        for v in range(from_version + 1, to_version + 1):
            if v not in migrations:
                continue

            migration = migrations[v]
            print(t("cli.messages.db_base_migration_start", from_version=from_version, to=v, desc=migration['description']))

            try:
                self.conn.execute("BEGIN")
                migration["func"](self.conn)
                self.conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                    (v, time.time(), migration["description"]),
                )
                self.conn.commit()
            except Exception as e:
                self.conn.rollback()
                print(t("cli.messages.db_base_migration_failed", version=v, error=e))
                traceback.print_exc()
                raise


    def _get_migrations(self) -> Dict[int, Dict]:
        """获取所有版本迁移函数

        每个迁移函数接收一个 sqlite3.Connection 参数，在事务内执行。
        返回: {版本号: {"description": 描述, "func": 迁移函数}}
        """
        return {
            2: {
                "description": t("cli.messages.migration_v2", default="Add Semgrep issue and scan tables"),
                "func": _migrate_v1_to_v2,
            },
            3: {
                "description": t("cli.messages.migration_v3", default="Hash-first/path-secondary multi-workspace model with deletion markers"),
                "func": _migrate_v2_to_v3,
            },
            4: {
                "description": t("cli.messages.migration_v4", default="Git integration: associate commits and inspect change impact"),
                "func": _migrate_v3_to_v4,
            },
            5: {
                "description": t("cli.messages.migration_v5", default="Add vector embedding tables (sqlite-vec)"),
                "func": _migrate_v4_to_v5,
            },
            6: {
                "description": t("cli.messages.migration_v6", default="Add symbol summary tables (versioned AI-generated function/module summaries)"),
                "func": _migrate_v5_to_v6,
            },
            7: {
                "description": t("cli.messages.migration_v7", default="Add task management tables (tasks / task_steps / change_audit)"),
                "func": _migrate_v6_to_v7,
            },
            8: {
                "description": t("cli.messages.migration_v8", default="Add file ownership table (CODEOWNERS + git blame)"),
                "func": _migrate_v7_to_v8,
            },
            9: {
                "description": t("cli.messages.migration_v9", default="Add coverage data table (LCOV/Cobertura line coverage)"),
                "func": _migrate_v8_to_v9,
            },
            10: {
                "description": t("cli.messages.migration_v10", default="Add guardian architecture tables (guardrails, impacts, evolution metrics, defect patterns/fixes)"),
                "func": _migrate_v9_to_v10,
            },
            11: {
                "description": t("cli.messages.migration_v11", default="Add token savings ledger table"),
                "func": _migrate_v10_to_v11,
            },
            12: {
                "description": t("cli.messages.migration_v12", default="Add safe file edit audit table (propose_edit safe edit pipeline)"),
                "func": _migrate_v11_to_v12,
            },
            13: {
                "description": t("cli.messages.migration_v13", default="Add cross-repository analysis table (cross-repo dependencies)"),
                "func": _migrate_v12_to_v13,
            },
            14: {
                "description": t("cli.messages.migration_v14", default="Add archive table (files matched by ignore rules moved out of primary tables)"),
                "func": _migrate_v13_to_v14,
            },
            15: {
                "description": t("cli.messages.migration_v15", default="Add parent/child task support (parent_id/depth/sort_order task trees)"),
                "func": _migrate_v14_to_v15,
            },
            16: {
                "description": t("cli.messages.migration_v16", default="Add external symbol tables (external_symbols + package_versions for stdlib and third-party symbols)"),
                "func": _migrate_v15_to_v16,
            },
            17: {
                "description": t("cli.messages.migration_v17", default="Add task-symbol change attribution table"),
                "func": _migrate_v16_to_v17,
            },
            18: {
                "description": t("cli.messages.migration_v18", default="Add external package cold-data tracking fields"),
                "func": _migrate_v17_to_v18,
            },
            19: {
                "description": t("cli.messages.migration_v19", default="Add GC retention policy table"),
                "func": _migrate_v18_to_v19,
            },
            20: {
                "description": t("cli.messages.migration_v20", default="Add GC run audit table (records policy/candidates/deletions/backup/status per GC operation)"),
                "func": _migrate_v19_to_v20,
            },
            21: {
                "description": t("cli.messages.migration_v21", default="Add task quality findings table (task completion gate findings, distinct from guardrail_findings)"),
                "func": _migrate_v20_to_v21,
            },
            22: {
                "description": t("cli.messages.migration_v22", default="Add audit chain table (hash/HMAC chain for verifying integrity of audit records)"),
                "func": _migrate_v21_to_v22,
            },
            23: {
                "description": t("cli.messages.migration_v23", default="Add Agent Rule Memory tables (agent_rule_candidates / agent_rules / agent_rule_sync_log)"),
                "func": _migrate_v22_to_v23,
            },
            24: {
                "description": t("cli.messages.migration_v24", default="Add tasks.applied_at column for review-to-applied state transition"),
                "func": _migrate_v23_to_v24,
            },
            25: {
                "description": t("cli.messages.migration_v25", default="Add workspace_scan_runs table for bootstrap scan baseline"),
                "func": _migrate_v24_to_v25,
            },
        }


    def _init_workspace(self):
        """初始化工作区：确保工作区存在，设置为活动工作区"""
        # 检查是否已有活动工作区
        cur = self.conn.execute(
            "SELECT * FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1"
        )
        row = cur.fetchone()

        if row:
            self.active_workspace = dict(row)
            self.workspace_root = row["root_path"]
            return

        # 没有活动工作区，根据 workspace_root 创建或查找
        workspace_name = get_default_workspace_name(self.workspace_root)

        cur = self.conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?",
            (self.workspace_root,),
        )
        row = cur.fetchone()

        if row:
            # 已存在，设为活动
            self.conn.execute(
                "UPDATE workspaces SET is_active = 1 WHERE id = ?",
                (row["id"],),
            )
        else:
            # 不存在，创建新工作区
            cur = self.conn.execute(
                "INSERT INTO workspaces (name, root_path, created_at, is_active, description) VALUES (?, ?, ?, 1, '')",
                (workspace_name, self.workspace_root, time.time()),
            )
            new_id = cur.lastrowid
            cur = self.conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (new_id,)
            )
            row = cur.fetchone()

        self.conn.commit()
        self.active_workspace = dict(row) if row else None


    def close(self):
        """关闭数据库连接，释放底层资源"""
        self.conn.close()

    # --------------------------------------------------------------------
    # 工作区管理方法
    # --------------------------------------------------------------------


    def register_workspace(self, name: str, root_path: str, description: str = "") -> int:
        """注册新工作区

        Args:
            name: 工作区名称（唯一）
            root_path: 工作区根目录绝对路径
            description: 描述

        Returns:
            新工作区 ID
        """
        root_path = norm_path(os.path.abspath(root_path))

        # 检查是否已存在
        cur = self.conn.execute(
            "SELECT id FROM workspaces WHERE name = ? OR root_path = ?",
            (name, root_path),
        )
        row = cur.fetchone()
        if row:
            return row["id"]

        cur = self.conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at, is_active, description) VALUES (?, ?, ?, 0, ?)",
            (name, root_path, time.time(), description),
        )
        self.conn.commit()
        return cur.lastrowid


    def list_workspaces(self) -> List[Dict]:
        """列出所有工作区"""
        cur = self.conn.execute(
            "SELECT * FROM workspaces ORDER BY is_active DESC, id ASC"
        )
        return [dict(row) for row in cur]


    def set_active_workspace(self, workspace_id_or_name) -> bool:
        """设置活动工作区

        Args:
            workspace_id_or_name: 工作区 ID（int）或名称（str）

        Returns:
            是否成功
        """
        if isinstance(workspace_id_or_name, int):
            cur = self.conn.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id_or_name,),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM workspaces WHERE name = ?",
                (workspace_id_or_name,),
            )

        row = cur.fetchone()
        if not row:
            return False

        # 优化：目标 workspace 已是 active 时跳过写操作，避免在锁竞争时被阻塞
        # （只读命令如 task list 也会经过这里，跳过 UPDATE 可避免被 MCP Server 写锁卡住）
        if row["is_active"] == 1:
            self.active_workspace = dict(row)
            self.workspace_root = row["root_path"]
            self.module_resolver = ModuleResolver(self.workspace_root)
            self.call_resolver = CallResolver(self.module_resolver, self.parser)
            return True

        # 取消其他工作区的活动状态
        self.conn.execute("UPDATE workspaces SET is_active = 0")
        self.conn.execute(
            "UPDATE workspaces SET is_active = 1 WHERE id = ?",
            (row["id"],),
        )
        self.conn.commit()

        self.active_workspace = dict(row)
        self.workspace_root = row["root_path"]
        self.module_resolver = ModuleResolver(self.workspace_root)
        self.call_resolver = CallResolver(self.module_resolver, self.parser)

        return True


    def get_active_workspace(self) -> Optional[Dict]:
        """获取当前活动工作区"""
        return self.active_workspace


    def delete_workspace(self, workspace_id_or_name) -> bool:
        """删除工作区（级联删除所有实例和版本）

        Args:
            workspace_id_or_name: 工作区 ID（int）或名称（str）

        Returns:
            是否成功
        """
        if isinstance(workspace_id_or_name, int):
            cur = self.conn.execute(
                "SELECT id FROM workspaces WHERE id = ?",
                (workspace_id_or_name,),
            )
        else:
            cur = self.conn.execute(
                "SELECT id FROM workspaces WHERE name = ?",
                (workspace_id_or_name,),
            )

        row = cur.fetchone()
        if not row:
            return False

        ws_id = row["id"]

        try:
            self.conn.execute("BEGIN")

            # 删除调用关系版本（通过 file_version_id -> file_instance_id）
            self.conn.execute("""
                DELETE FROM call_versions WHERE file_version_id IN (
                    SELECT fv.id FROM file_versions fv
                    JOIN file_instances fi ON fv.file_instance_id = fi.id
                    WHERE fi.workspace_id = ?
                )
            """, (ws_id,))

            # 删除文件-符号关联版本
            self.conn.execute("""
                DELETE FROM file_symbol_versions WHERE file_version_id IN (
                    SELECT fv.id FROM file_versions fv
                    JOIN file_instances fi ON fv.file_instance_id = fi.id
                    WHERE fi.workspace_id = ?
                )
            """, (ws_id,))

            # 删除文件版本
            self.conn.execute("""
                DELETE FROM file_versions WHERE file_instance_id IN (
                    SELECT id FROM file_instances WHERE workspace_id = ?
                )
            """, (ws_id,))

            # 删除 semgrep 结果
            self.conn.execute("""
                DELETE FROM semgrep_findings WHERE file_instance_id IN (
                    SELECT id FROM file_instances WHERE workspace_id = ?
                )
            """, (ws_id,))

            # 删除 semgrep 扫描记录
            self.conn.execute("DELETE FROM semgrep_scans WHERE workspace_id = ?", (ws_id,))

            # 删除符号（当前快照）
            self.conn.execute("""
                DELETE FROM symbols WHERE file_instance_id IN (
                    SELECT id FROM file_instances WHERE workspace_id = ?
                )
            """, (ws_id,))

            # 删除文件实例
            self.conn.execute("DELETE FROM file_instances WHERE workspace_id = ?", (ws_id,))

            # 删除工作区
            self.conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))

            self.conn.commit()

            # 如果删除的是活动工作区，清除活动状态
            if self.active_workspace and self.active_workspace.get("id") == ws_id:
                self.active_workspace = None

            return True
        except Exception as e:
            self.conn.rollback()
            print(t("cli.messages.db_base_delete_workspace_failed", error=e))
            return False

    # --------------------------------------------------------------------
    # 完整构建流程
    # --------------------------------------------------------------------


    def _get_active_workspace_id(self) -> int:
        """获取当前活动工作区 ID"""
        if self.active_workspace:
            return self.active_workspace.get("id", 1)
        return 1
