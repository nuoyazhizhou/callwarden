"""Phase 8.5: Daemon 数据备份与恢复。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（backup/restore）

提供：
1. BackupManager：备份管理器
   - 备份 registry DB、CAS DB、audit log DB
   - 备份 daemon 配置文件
   - 备份 workspace manifests
   - 支持全量备份和增量备份
2. RestoreManager：恢复管理器
   - 从备份恢复 DB 文件
   - 验证备份完整性
   - 恢复后触发 health check

备份格式：
    <backup_root>/
    ├── backup_meta.json       # 备份元数据
    ├── registry.db            # registry DB 副本
    ├── cas.db                 # CAS DB 副本（如存在）
    ├── audit.db               # audit log DB 副本（如存在）
    ├── daemon.json            # daemon 配置副本
    └── snapshots/             # snapshot 目录副本（如存在）

backup_meta.json 格式：
{
    "backup_id": "B-<13ts>-<8hex>",
    "timestamp": 1783698970.0,
    "backup_type": "full",
    "daemon_version": "1.0.0",
    "files": [
        {"name": "registry.db", "size": 1024, "sha256": "..."},
        ...
    ],
    "checksum": "..."
}
"""

from __future__ import annotations

import os
import time
import json
import shutil
import hashlib
import secrets
from pathlib import PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Phase 4-3 P3 wire-production: Rust 短路（backup 纯计算）
# ============================================================
# backup_restore.py 的 _compute_file_sha256 / _compute_meta_checksum 默认走 Rust
# PyO3 API（callwarden_core.backup_compute_file_sha256 / backup_compute_meta_checksum），
# rollback_config 中 feature=rust_daemon_backup_compute 置为 1 时回退 Python。
# Rust 失败时 fail-soft 降级到 Python 纯计算路径。
#
# 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.4

_RUST_BACKUP_AVAILABLE = False
_RUST_BACKUP_MANAGER_AVAILABLE = False
_callwarden_core = None
try:
    import callwarden_core as _callwarden_core  # type: ignore
    if (
        hasattr(_callwarden_core, "backup_compute_file_sha256")
        and hasattr(_callwarden_core, "backup_compute_meta_checksum")
    ):
        _RUST_BACKUP_AVAILABLE = True
    _RUST_BACKUP_MANAGER_AVAILABLE = all(
        hasattr(_callwarden_core, name)
        for name in (
            "backup_full",
            "backup_db_only",
            "restore_backup",
            "verify_backup",
            "list_backups",
            "delete_backup",
            "cleanup_backups",
        )
    )
except ImportError:
    _callwarden_core = None

_BACKUP_ROLLBACK_CACHE: Dict[str, Any] = {"ts": 0.0, "value": False}
_BACKUP_ROLLBACK_CACHE_TTL = 60.0


def _is_rust_backup_rolled_back() -> bool:
    """检查 rust_daemon_backup_compute feature 是否已回滚（60s 缓存）。

    SRV-003 收敛（server backup restore Python authority → Rust daemon）：
    rollback_config 的 SQLite 权威已下沉到 daemon，本函数为纯薄客户端，
    经 `mcp.backup_restore.is_rust_backup_rolled_back` 查询。只读 authority 读：
    daemon 不可用时 fail-soft 视为未回滚（返回 False，与 metrics/health/audit
    的 rollback 探测一致），绝不回退本地 SQLite。
    60s 缓存保留，避免热路径频繁 RPC。
    """
    now = time.time()
    if now - _BACKUP_ROLLBACK_CACHE["ts"] < _BACKUP_ROLLBACK_CACHE_TTL:
        return _BACKUP_ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    try:
        result = _call_daemon_rpc("mcp.backup_restore.is_rust_backup_rolled_back", {})
        value = bool(isinstance(result, dict) and result.get("rolled_back"))
    except Exception:
        # fail-soft：只读探测，daemon 不可用时视为未回滚（不回退本地 SQLite）
        value = False
    _BACKUP_ROLLBACK_CACHE["ts"] = now
    _BACKUP_ROLLBACK_CACHE["value"] = value
    return value


def _rust_backup_available() -> bool:
    """Rust backup 短路是否可用（模块加载 + 未回滚）。"""
    return _RUST_BACKUP_AVAILABLE and not _is_rust_backup_rolled_back()


def _rust_backup_manager_available() -> bool:
    """完整 backup/restore Rust 核心是否可用。"""
    return _RUST_BACKUP_MANAGER_AVAILABLE and not _is_rust_backup_rolled_back()


def _decode_rust_json(value: str) -> Dict[str, Any]:
    """解码 Rust backup API 返回的 JSON，错误不静默吞掉。"""
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise RuntimeError("Rust backup API returned a non-object result")
    return decoded


def _validate_backup_id(backup_id: str) -> str:
    r"""校验备份 ID（C8/P1：对齐 Rust `validate_backup_id`）。

    Rust 规则（rust_ext/src/backup_restore.rs `validate_backup_id`）：
    `Path::new(backup_id).components()` 必须恰好一个 `Normal` 组件。
    等价拒绝条件（Python 侧显式实现）：
    - 空值、`.`、`..`
    - 含分隔符（`/` 或 `\`）→ 多级路径或 CurDir/ParentDir 特殊组件
    - 盘符前缀（`C:`、`C:foo`）或根路径（`/`、`C:\`）→ 绝对路径

    校验通过返回原 ID；否则抛 ValueError。Python fallback 在拼接
    `os.path.join(self._backup_root, backup_id)` 前必须调用，防止路径穿越。
    """
    if not isinstance(backup_id, str) or not backup_id:
        raise ValueError("backup_id 不能为空")
    if backup_id in (".", ".."):
        raise ValueError(f"非法 backup_id: {backup_id}")
    if "/" in backup_id or "\\" in backup_id:
        raise ValueError(f"非法 backup_id: {backup_id}")
    p = PureWindowsPath(backup_id)
    if p.drive or p.root:
        raise ValueError(f"非法 backup_id: {backup_id}")
    return backup_id


# ============================================================
# BackupManager
# ============================================================


class BackupManager:
    """Daemon 数据备份管理器。

    用法：
        mgr = BackupManager(config, backup_root="/var/backups/callwarden")
        result = mgr.backup_full()
        # result = {"backup_id": "B-...", "path": "...", "files": [...]}
    """

    def __init__(self, config, backup_root: str = ""):
        """初始化备份管理器。

        Args:
            config: DaemonConfig 实例
            backup_root: 备份根目录（为空时使用 data_root/backups）
        """
        self._config = config
        self._backup_root = backup_root or os.path.join(config.data_root, "backups")

    def backup_full(self, backup_id: Optional[str] = None) -> Dict[str, Any]:
        """执行全量备份。

        备份内容：
        - registry DB
        - CAS DB（如存在）
        - audit log DB（如存在）
        - daemon 配置文件
        - snapshots 目录（如存在）

        Args:
            backup_id: 自定义备份 ID（为空时自动生成）

        Returns:
            备份结果摘要
        """
        backup_id = backup_id or self._generate_backup_id()
        if _rust_backup_manager_available():
            return _decode_rust_json(
                _callwarden_core.backup_full(
                    self._backup_root,
                    backup_id,
                    self._config.registry_db_path,
                    self._config.cas_db_path,
                    self._config.audit_log_path,
                    self._config.data_root,
                )
            )
        final_dir, temp_dir = self._prepare_backup_dir_atomic(backup_id)

        files_info: List[Dict[str, Any]] = []
        try:
            # 备份 registry DB
            registry_info = self._backup_file(
                self._config.registry_db_path, temp_dir, "registry.db"
            )
            if registry_info:
                files_info.append(registry_info)

            # 备份 CAS DB
            cas_info = self._backup_file(
                self._config.cas_db_path, temp_dir, "cas.db"
            )
            if cas_info:
                files_info.append(cas_info)

            # 备份 audit log DB
            audit_info = self._backup_file(
                self._config.audit_log_path, temp_dir, "audit.db"
            )
            if audit_info:
                files_info.append(audit_info)

            # C8/B3：补齐 daemon 配置文件（回退布局与 Rust 默认布局一致）
            daemon_info = self._backup_file(
                os.path.join(self._config.data_root, "daemon.json"),
                temp_dir,
                "daemon.json",
            )
            if daemon_info:
                files_info.append(daemon_info)

            # 备份 snapshots 目录
            snapshot_dir = os.path.join(self._config.data_root, "snapshots")
            if os.path.isdir(snapshot_dir):
                dest_snapshot_dir = os.path.join(temp_dir, "snapshots")
                shutil.copytree(snapshot_dir, dest_snapshot_dir)
                files_info.append({
                    "name": "snapshots/",
                    "type": "directory",
                    "file_count": sum(len(files) for _, _, files in os.walk(dest_snapshot_dir)),
                })

            # 生成元数据
            meta = {
                "backup_id": backup_id,
                "timestamp": time.time(),
                "backup_type": "full",
                "daemon_version": "1.0.0",
                "files": files_info,
                "total_size": sum(f.get("size", 0) for f in files_info if "size" in f),
            }

            # 计算整体校验和
            meta["checksum"] = self._compute_meta_checksum(meta)

            # 写入元数据文件（临时目录内）
            meta_path = os.path.join(temp_dir, "backup_meta.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            # C8/B4：原子发布——rename 临时目录到最终目录
            os.rename(temp_dir, final_dir)
        except Exception:
            # 对齐 Rust create_backup：失败时清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        return meta

    def backup_db_only(self, backup_id: Optional[str] = None) -> Dict[str, Any]:
        """只备份数据库文件（不含 snapshots）。

        Args:
            backup_id: 自定义备份 ID

        Returns:
            备份结果摘要
        """
        backup_id = backup_id or self._generate_backup_id()
        if _rust_backup_manager_available():
            return _decode_rust_json(
                _callwarden_core.backup_db_only(
                    self._backup_root,
                    backup_id,
                    self._config.registry_db_path,
                    self._config.cas_db_path,
                    self._config.audit_log_path,
                    self._config.data_root,
                )
            )
        final_dir, temp_dir = self._prepare_backup_dir_atomic(backup_id)

        files_info: List[Dict[str, Any]] = []
        try:
            for src_path, dest_name in [
                (self._config.registry_db_path, "registry.db"),
                (self._config.cas_db_path, "cas.db"),
                (self._config.audit_log_path, "audit.db"),
            ]:
                info = self._backup_file(src_path, temp_dir, dest_name)
                if info:
                    files_info.append(info)

            meta = {
                "backup_id": backup_id,
                "timestamp": time.time(),
                "backup_type": "db_only",
                "daemon_version": "1.0.0",
                "files": files_info,
                "total_size": sum(f.get("size", 0) for f in files_info if "size" in f),
            }
            meta["checksum"] = self._compute_meta_checksum(meta)

            meta_path = os.path.join(temp_dir, "backup_meta.json")
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            # C8/B4：原子发布——rename 临时目录到最终目录
            os.rename(temp_dir, final_dir)
        except Exception:
            # 对齐 Rust create_backup：失败时清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        return meta

    def list_backups(self) -> List[Dict[str, Any]]:
        """列出所有备份。"""
        if _rust_backup_manager_available():
            result = json.loads(_callwarden_core.list_backups(self._backup_root))
            if not isinstance(result, list):
                raise RuntimeError("Rust backup list API returned a non-list result")
            return result
        if not os.path.isdir(self._backup_root):
            return []

        backups = []
        for entry in os.listdir(self._backup_root):
            meta_path = os.path.join(self._backup_root, entry, "backup_meta.json")
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    backups.append(meta)
                except (json.JSONDecodeError, OSError):
                    continue

        # 按时间倒序
        backups.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return backups

    def get_backup_info(self, backup_id: str) -> Optional[Dict[str, Any]]:
        """获取单个备份的元数据。

        C8/B1：Rust 可用时经 Rust `list_backups` 过滤实现（明确等价）——
        Rust list_backups 与 Python 直读读的是同一 `backup_meta.json`，
        无效 meta 在两侧均视为不存在（返回 None）；且 Rust 侧只扫描真实
        备份目录，天然规避 backup_id 路径穿越。Rust 不可用时降级直读。
        """
        if _rust_backup_manager_available():
            for meta in self.list_backups():
                if meta.get("backup_id") == backup_id:
                    return meta
            return None
        # C8/P1：fallback 直读前校验 backup_id，防止路径穿越
        _validate_backup_id(backup_id)
        meta_path = os.path.join(self._backup_root, backup_id, "backup_meta.json")
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def delete_backup(self, backup_id: str) -> bool:
        """删除备份。

        Args:
            backup_id: 备份 ID

        Returns:
            True 如果删除成功
        """
        if _rust_backup_manager_available():
            return bool(_callwarden_core.delete_backup(self._backup_root, backup_id))
        # C8/P1：fallback 删除前校验 backup_id，防止路径穿越
        _validate_backup_id(backup_id)
        backup_dir = os.path.join(self._backup_root, backup_id)
        if not os.path.isdir(backup_dir):
            return False
        shutil.rmtree(backup_dir)
        return True

    def cleanup_old_backups(self, keep_count: int = 5) -> int:
        """清理旧备份，保留最近 N 个。

        Args:
            keep_count: 保留的备份数量

        Returns:
            删除的备份数量
        """
        if _rust_backup_manager_available():
            return int(_callwarden_core.cleanup_backups(self._backup_root, keep_count))
        backups = self.list_backups()
        if len(backups) <= keep_count:
            return 0

        to_delete = backups[keep_count:]
        deleted = 0
        for meta in to_delete:
            backup_id = meta.get("backup_id", "")
            if backup_id and self.delete_backup(backup_id):
                deleted += 1
        return deleted

    # ----- 内部方法 -----

    def _prepare_backup_dir_atomic(self, backup_id: str) -> Tuple[str, str]:
        """原子发布准备（C8/B4，对齐 Rust `create_backup`）。

        返回 (final_dir, temp_dir)。最终目录已存在（重复 ID）或临时目录
        已存在（上次失败残留）时抛 FileExistsError（fail-closed），
        与 Rust 的「备份已存在 / 备份临时目录已存在」拒绝语义一致。
        """
        # C8/P1：fallback 路径先校验 backup_id，防止路径穿越逃出 backup_root
        _validate_backup_id(backup_id)
        final_dir = os.path.join(self._backup_root, backup_id)
        if os.path.exists(final_dir):
            raise FileExistsError(f"备份已存在: {final_dir}")
        os.makedirs(self._backup_root, exist_ok=True)
        temp_dir = os.path.join(self._backup_root, f".{backup_id}.partial")
        if os.path.exists(temp_dir):
            raise FileExistsError(f"备份临时目录已存在: {temp_dir}")
        os.makedirs(temp_dir)
        return final_dir, temp_dir

    def _backup_file(self, src_path: str, dest_dir: str, dest_name: str) -> Optional[Dict[str, Any]]:
        """备份单个文件（SRV-003：daemon RPC 薄客户端）。

        文件备份权威已下沉到 daemon（`backup_restore_handlers.rs::handle_backup_file`，
        `.db` 走 wal_checkpoint + VACUUM INTO，失败降级 daemon 侧 fs::copy）。
        本方法不再打开本地 SQLite、不保留本地 business fallback；
        daemon 不可用时抛 DaemonUnavailableError（fail-closed）。

        Args:
            src_path: 源文件路径
            dest_dir: 目标目录
            dest_name: 目标文件名

        Returns:
            文件信息字典，如果源文件不存在返回 None
        """
        result = _call_daemon_rpc("mcp.backup_restore.backup_file", {
            "src_path": src_path,
            "dest_dir": dest_dir,
            "dest_name": dest_name,
        })
        if result is None:
            return None
        if not isinstance(result, dict):
            raise RuntimeError("daemon backup_file 返回非 object 结果")
        return result

    def _compute_file_sha256(self, file_path: str) -> str:
        """计算文件的 SHA-256。

        Phase 4-3 P3 wire-production：
            默认走 Rust `callwarden_core.backup_compute_file_sha256`，rollback_config
            中 feature=rust_daemon_backup_compute 置为 1 时回退 Python。
            Rust 失败时 fail-soft 降级到 Python 纯计算路径。
        """
        if _rust_backup_available():
            try:
                return _callwarden_core.backup_compute_file_sha256(file_path)
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _compute_meta_checksum(self, meta: Dict[str, Any]) -> str:
        """计算元数据的校验和。

        Phase 4-3 P3 wire-production：
            默认走 Rust `callwarden_core.backup_compute_meta_checksum`，rollback_config
            中 feature=rust_daemon_backup_compute 置为 1 时回退 Python。
            Rust 失败时 fail-soft 降级到 Python 纯计算路径。
        """
        if _rust_backup_available():
            try:
                # Rust 端负责排除 checksum 字段并重新稳定序列化
                meta_json = json.dumps(meta, ensure_ascii=False)
                return _callwarden_core.backup_compute_meta_checksum(meta_json)
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # 排除 checksum 自身
        meta_copy = {k: v for k, v in meta.items() if k != "checksum"}
        content = json.dumps(meta_copy, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _generate_backup_id() -> str:
        """生成备份 ID：B-<13ts>-<8hex>。

        后缀 8 位 hex（32 bit）而非 4 位 hex：降低快速循环内碰撞概率（生日悖论）。
        """
        ts = int(time.time() * 1000)
        hex_part = secrets.token_hex(4)
        return f"B-{ts}-{hex_part}"


# ============================================================
# RestoreManager
# ============================================================


class RestoreManager:
    """Daemon 数据恢复管理器。

    用法：
        mgr = RestoreManager(config, backup_root="/var/backups/callwarden")
        result = mgr.restore("B-1783698970123-abcd")
        if result["status"] == "success":
            # 恢复成功，重启 daemon
            pass
    """

    def __init__(self, config, backup_root: str = ""):
        self._config = config
        self._backup_root = backup_root or os.path.join(config.data_root, "backups")

    def restore(self, backup_id: str) -> Dict[str, Any]:
        """从备份恢复数据。

        Args:
            backup_id: 备份 ID

        Returns:
            恢复结果摘要
        """
        if _rust_backup_manager_available():
            return _decode_rust_json(
                _callwarden_core.restore_backup(
                    self._backup_root,
                    backup_id,
                    self._config.registry_db_path,
                    self._config.cas_db_path,
                    self._config.audit_log_path,
                    self._config.data_root,
                )
            )
        # C8/P1：fallback 恢复前校验 backup_id，防止路径穿越
        _validate_backup_id(backup_id)
        backup_dir = os.path.join(self._backup_root, backup_id)
        if not os.path.isdir(backup_dir):
            return {
                "status": "failure",
                "error": f"backup not found: {backup_id}",
            }

        # 读取元数据
        meta_path = os.path.join(backup_dir, "backup_meta.json")
        if not os.path.isfile(meta_path):
            return {
                "status": "failure",
                "error": "backup_meta.json not found",
            }

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except json.JSONDecodeError as e:
            return {
                "status": "failure",
                "error": f"invalid backup_meta.json: {e}",
            }

        # 验证校验和
        expected_checksum = meta.get("checksum", "")
        actual_checksum = self._compute_meta_checksum(meta)
        if expected_checksum != actual_checksum:
            return {
                "status": "failure",
                "error": "checksum mismatch",
                "expected": expected_checksum,
                "actual": actual_checksum,
            }

        # 恢复文件
        restored_files: List[Dict[str, Any]] = []

        for file_info in meta.get("files", []):
            file_name = file_info.get("name", "")
            if not file_name:
                continue

            # 跳过目录（snapshots/ 单独处理）
            if file_info.get("type") == "directory":
                src_dir = os.path.join(backup_dir, file_name.rstrip("/"))
                dest_dir = os.path.join(self._config.data_root, file_name.rstrip("/"))
                if os.path.isdir(src_dir):
                    if os.path.exists(dest_dir):
                        shutil.rmtree(dest_dir)
                    shutil.copytree(src_dir, dest_dir)
                    restored_files.append({
                        "name": file_name,
                        "status": "restored",
                    })
                continue

            # 文件恢复
            src_path = os.path.join(backup_dir, file_name)
            if not os.path.isfile(src_path):
                restored_files.append({
                    "name": file_name,
                    "status": "skipped",
                    "reason": "source file not found",
                })
                continue

            # 验证文件 SHA-256
            expected_sha = file_info.get("sha256", "")
            if expected_sha:
                actual_sha = self._compute_file_sha256(src_path)
                if actual_sha != expected_sha:
                    restored_files.append({
                        "name": file_name,
                        "status": "failed",
                        "reason": f"sha256 mismatch",
                    })
                    continue

            # 确定目标路径
            dest_path = self._get_dest_path(file_name)
            if not dest_path:
                restored_files.append({
                    "name": file_name,
                    "status": "skipped",
                    "reason": "unknown file type",
                })
                continue

            # 恢复文件
            dir_path = os.path.dirname(dest_path)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)

            shutil.copy2(src_path, dest_path)
            restored_files.append({
                "name": file_name,
                "status": "restored",
                "dest_path": dest_path,
            })

        return {
            "status": "success",
            "backup_id": backup_id,
            "timestamp": time.time(),
            "restored_files": restored_files,
            "backup_meta": meta,
        }

    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """验证备份完整性（不执行恢复）。

        Args:
            backup_id: 备份 ID

        Returns:
            验证结果
        """
        if _rust_backup_manager_available():
            return _decode_rust_json(
                _callwarden_core.verify_backup(self._backup_root, backup_id)
            )
        # C8/P1：fallback 校验前先校验 backup_id，防止路径穿越
        _validate_backup_id(backup_id)
        backup_dir = os.path.join(self._backup_root, backup_id)
        if not os.path.isdir(backup_dir):
            return {
                "status": "invalid",
                "error": f"backup not found: {backup_id}",
            }

        meta_path = os.path.join(backup_dir, "backup_meta.json")
        if not os.path.isfile(meta_path):
            return {
                "status": "invalid",
                "error": "backup_meta.json not found",
            }

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except json.JSONDecodeError as e:
            return {
                "status": "invalid",
                "error": f"invalid meta: {e}",
            }

        # 验证校验和
        expected_checksum = meta.get("checksum", "")
        actual_checksum = self._compute_meta_checksum(meta)
        if expected_checksum != actual_checksum:
            return {
                "status": "corrupted",
                "error": "checksum mismatch",
                "backup_id": backup_id,
            }

        # 验证每个文件
        file_results = []
        all_valid = True
        for file_info in meta.get("files", []):
            file_name = file_info.get("name", "")
            if file_info.get("type") == "directory":
                src_dir = os.path.join(backup_dir, file_name.rstrip("/"))
                exists = os.path.isdir(src_dir)
                file_results.append({
                    "name": file_name,
                    "valid": exists,
                })
                if not exists:
                    all_valid = False
                continue

            src_path = os.path.join(backup_dir, file_name)
            if not os.path.isfile(src_path):
                file_results.append({
                    "name": file_name,
                    "valid": False,
                    "reason": "file not found",
                })
                all_valid = False
                continue

            # 验证 SHA-256
            expected_sha = file_info.get("sha256", "")
            if expected_sha:
                actual_sha = self._compute_file_sha256(src_path)
                valid = actual_sha == expected_sha
                file_results.append({
                    "name": file_name,
                    "valid": valid,
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                })
                if not valid:
                    all_valid = False
            else:
                file_results.append({
                    "name": file_name,
                    "valid": True,
                })

        return {
            "status": "valid" if all_valid else "corrupted",
            "backup_id": backup_id,
            "files": file_results,
        }

    # ----- 内部方法 -----

    def _get_dest_path(self, file_name: str) -> Optional[str]:
        """根据文件名确定恢复目标路径。"""
        if file_name == "registry.db":
            return self._config.registry_db_path
        elif file_name == "cas.db":
            return self._config.cas_db_path
        elif file_name == "audit.db":
            return self._config.audit_log_path
        elif file_name == "daemon.json":
            return os.path.join(self._config.data_root, "daemon.json")
        return None

    def _compute_file_sha256(self, file_path: str) -> str:
        """计算文件的 SHA-256。

        C8/B2：与 BackupManager 覆盖对齐——默认走 Rust
        `callwarden_core.backup_compute_file_sha256`，rollback_config 中
        feature=rust_daemon_backup_compute 置为 1 时回退 Python；
        Rust 失败时 fail-soft 降级到 Python 纯计算路径。
        """
        if _rust_backup_available():
            try:
                return _callwarden_core.backup_compute_file_sha256(file_path)
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _compute_meta_checksum(self, meta: Dict[str, Any]) -> str:
        """计算元数据的校验和。

        C8/B2：与 BackupManager 覆盖对齐——默认走 Rust
        `callwarden_core.backup_compute_meta_checksum`（Rust 端负责排除
        checksum 字段并重新稳定序列化），Rust 失败时 fail-soft 降级。
        """
        if _rust_backup_available():
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)
                return _callwarden_core.backup_compute_meta_checksum(meta_json)
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        meta_copy = {k: v for k, v in meta.items() if k != "checksum"}
        content = json.dumps(meta_copy, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================
# daemon RPC 薄客户端接入（SRV-003）
# ============================================================


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一 fail-closed 客户端发起 RPC（不回退本地 SQLite）。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)
