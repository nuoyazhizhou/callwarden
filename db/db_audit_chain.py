"""
db_audit_chain.py
================

审计签名链（Audit Chain）Mixin。

为关键审计表（task_quality_findings / change_audit / file_edit_audit 等）
生成可验证的 hash/HMAC 链，防止误改或有意篡改。

每条 audit_chain 记录包含：
- payload_hash：记录内容的 SHA-256 摘要
- prev_signature：上一条同表记录的 record_signature（首条为空串）
- record_signature：本条记录的签名

签名算法：
- 有 HMAC key 时（CALLWARDEN_AUDIT_HMAC_KEY 或 ~/.callwarden/audit.key）：
  HMAC-SHA256(key, prev_signature + "|" + payload_hash)
  signing_key_id='hmac'，security_level='hmac'
- 无 HMAC key 时：
  SHA-256(prev_signature + "|" + payload_hash)
  signing_key_id='local'，security_level='hash_only'

链结构：每个 table_name 维护一条独立链，按 id 升序前后相连。
verify_audit_chain 可校验链连续性与签名匹配，发现直接改库导致的篡改。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional


# HMAC 密钥文件路径（按用户主目录展开）
_AUDIT_KEY_FILE = os.path.expanduser("~/.callwarden/audit.key")


def _get_hmac_key() -> Optional[bytes]:
    """获取 HMAC 密钥

    优先级：
    1. 环境变量 CALLWARDEN_AUDIT_HMAC_KEY
    2. 文件 ~/.callwarden/audit.key（读取后 strip）
    3. None（回落到 SHA-256 链）

    Returns:
        密钥字节串或 None
    """
    # 1. 环境变量优先
    env_key = os.environ.get("CALLWARDEN_AUDIT_HMAC_KEY")
    if env_key:
        return env_key.encode("utf-8")

    # 2. 密钥文件
    if os.path.isfile(_AUDIT_KEY_FILE):
        try:
            with open(_AUDIT_KEY_FILE, "rb") as f:
                content = f.read().strip()
                if content:
                    return content
        except OSError:
            # 读取失败时静默回落到 SHA-256 链
            pass

    # 3. 无密钥
    return None


def _compute_signature(
    prev_signature: str,
    payload_hash: str,
    hmac_key: Optional[bytes],
) -> str:
    """计算 record_signature

    Args:
        prev_signature: 上一条记录的签名（首条为空串）
        payload_hash: 本条记录内容的 SHA-256 摘要
        hmac_key: HMAC 密钥，None 时使用 SHA-256

    Returns:
        十六进制签名字符串
    """
    message = f"{prev_signature}|{payload_hash}".encode("utf-8")
    if hmac_key is not None:
        return hmac.new(hmac_key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


class AuditChainMixin:
    """审计签名链 Mixin

    提供 canonical_json / sign_audit_record / verify_audit_chain 三个核心方法。
    通过 Mixin 模式混入 CodeGraphDB，与 TaskQualityMixin 等协作：
    关键审计记录写入时调用 sign_audit_record 留下签名痕迹，
    运维或审查时调用 verify_audit_chain 检测直接改库导致的篡改。

    支持签名密钥轮换（C7）：
    - rotate_signing_key(new_key_id, new_key_secret) 轮换到新密钥
    - 轮换后新记录用新 key 签名（signing_key_id = new_key_id）
    - 旧记录保持原签名不变（signing_key_id 不变）
    - 验证时按 signing_key_id 从 audit_key_rotations 查找对应密钥
    """

    def canonical_json(self, payload: Any) -> str:
        """稳定序列化 payload 为 JSON 字符串

        用于保证相同语义的 payload 始终产生相同的字符串，
        从而 payload_hash 可复现。

        - sort_keys=True：递归排序所有 dict 的 key
        - ensure_ascii=False：保留 Unicode 字符，避免 \\uXXXX 转义
        - separators=(',', ':')：紧凑格式，无多余空格

        Args:
            payload: 任意可 JSON 序列化的对象（dict/list/str/int/float/bool/None）

        Returns:
            稳定的 JSON 字符串
        """
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # ============================================
    # 密钥轮换（C7）
    # ============================================

    def rotate_signing_key(
        self,
        new_key_id: str,
        new_key_secret: str,
    ) -> Dict[str, Any]:
        """轮换审计签名密钥

        流程：
        1. 将当前 active 密钥置为 inactive（is_active=0）
        2. 插入新密钥记录（is_active=1）
        3. 返回轮换信息

        轮换后：
        - 新的 sign_audit_record 调用使用新密钥签名
        - 旧记录保持原签名不变（signing_key_id 不变）
        - verify_audit_chain 按 signing_key_id 查找对应密钥验证

        Args:
            new_key_id: 新密钥标识（唯一，如 "key-2026-07"）
            new_key_secret: 新密钥内容（用于 HMAC 计算）

        Returns:
            {
                "success": True,
                "key_id": new_key_id,
                "rotated_at": float,
                "previous_key_id": str,  # 前一个 active 密钥的 key_id（无则为空串）
            }

        Raises:
            ValueError: new_key_id 或 new_key_secret 为空
        """
        if not new_key_id or not new_key_id.strip():
            raise ValueError("new_key_id is required")
        if not new_key_secret:
            raise ValueError("new_key_secret is required")

        now = time.time()
        new_key_id = new_key_id.strip()

        # 查询当前 active 密钥
        cur = self.conn.execute(
            "SELECT key_id FROM audit_key_rotations WHERE is_active = 1 LIMIT 1"
        )
        row = cur.fetchone()
        previous_key_id = row["key_id"] if row else ""

        # 将当前 active 密钥置为 inactive
        if previous_key_id:
            self.conn.execute(
                "UPDATE audit_key_rotations SET is_active = 0 WHERE is_active = 1"
            )

        # 插入新密钥（若 key_id 已存在则更新，幂等）
        self.conn.execute(
            """
            INSERT INTO audit_key_rotations (key_id, key_secret, rotated_at, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key_id) DO UPDATE SET
                key_secret = excluded.key_secret,
                rotated_at = excluded.rotated_at,
                is_active = 1
            """,
            (new_key_id, new_key_secret, now),
        )
        self.conn.commit()

        return {
            "success": True,
            "key_id": new_key_id,
            "rotated_at": now,
            "previous_key_id": previous_key_id,
        }

    def list_signing_keys(self) -> List[Dict[str, Any]]:
        """列出所有签名密钥轮换记录

        Returns:
            密钥列表，按 rotated_at 倒序，每项含 key_id/rotated_at/is_active
            （不返回 key_secret，避免泄露）
        """
        cur = self.conn.execute(
            "SELECT key_id, rotated_at, is_active FROM audit_key_rotations "
            "ORDER BY rotated_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def _get_active_signing_key(self) -> tuple:
        """获取当前活跃签名密钥

        优先级：
        1. audit_key_rotations 表中 is_active=1 的记录
        2. 环境变量 CALLWARDEN_AUDIT_HMAC_KEY / 文件 ~/.callwarden/audit.key
        3. None（回落到 SHA-256 链）

        Returns:
            (key_id, key_bytes, security_level) 三元组
            - key_id: 密钥标识（如 "key-2026-07" / "hmac" / "local"）
            - key_bytes: 密钥字节串（None 时表示无密钥，用 SHA-256）
            - security_level: "hmac" 或 "hash_only"
        """
        # 1. 查询 audit_key_rotations 中的 active 密钥
        cur = self.conn.execute(
            "SELECT key_id, key_secret FROM audit_key_rotations WHERE is_active = 1 LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            return (row["key_id"], row["key_secret"].encode("utf-8"), "hmac")

        # 2. 回落到环境变量/文件
        hmac_key = _get_hmac_key()
        if hmac_key is not None:
            return ("hmac", hmac_key, "hmac")

        # 3. 无密钥
        return ("local", None, "hash_only")

    def _lookup_signing_key(self, key_id: str) -> Optional[bytes]:
        """按 key_id 查找签名密钥（用于验证时选择对应密钥）

        查找顺序：
        1. audit_key_rotations 表中 key_id 对应的 key_secret
        2. 若 key_id == "hmac"，回落到当前环境变量/文件密钥（向后兼容）
        3. 若 key_id == "local"，返回 None（SHA-256 链）
        4. 其他未知 key_id，返回 None（无法验证，标记为 mismatch）

        Args:
            key_id: 签名时使用的密钥标识

        Returns:
            密钥字节串，或 None（SHA-256 或未知密钥）
        """
        if key_id == "local":
            return None  # SHA-256 链，无需密钥

        # 查询 audit_key_rotations
        cur = self.conn.execute(
            "SELECT key_secret FROM audit_key_rotations WHERE key_id = ? LIMIT 1",
            (key_id,),
        )
        row = cur.fetchone()
        if row:
            return row["key_secret"].encode("utf-8")

        # 向后兼容：legacy "hmac" key_id，用当前环境变量/文件密钥
        if key_id == "hmac":
            return _get_hmac_key()

        # 未知 key_id（密钥已被删除或来自其他实例）
        return None

    def sign_audit_record(
        self,
        table_name: str,
        record_id: str,
        payload: Any,
        operation: str = "insert",
    ) -> Dict[str, Any]:
        """为关键审计记录写入 audit_chain

        流程：
        1. 计算 payload_hash = SHA-256(canonical_json(payload))
        2. 查询同 table_name 的最后一条记录的 record_signature 作为 prev_signature
           （首条记录 prev_signature 为空串）
        3. 根据是否有 HMAC key 选择签名算法
        4. 写入 audit_chain 表
        5. 返回包含签名信息的 dict

        Args:
            table_name: 被签名记录所属的表名（如 "task_quality_findings"）
            record_id: 被签名记录的主键（转为字符串存储）
            payload: 被签名记录的内容（dict 或可序列化对象）
            operation: 操作类型（insert/update/delete），默认 insert

        Returns:
            dict 包含：
            - id: audit_chain 记录 ID
            - table_name: 表名
            - record_id: 记录主键
            - operation: 操作类型
            - payload_hash: 内容摘要
            - prev_signature: 上一条签名
            - record_signature: 本条签名
            - signing_key_id: 签名密钥标识（'local' 或 'hmac'）
            - security_level: 安全级别（'hash_only' 或 'hmac'）
        """
        # 1. 计算 payload_hash
        payload_json = self.canonical_json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        # 2. 查询同 table_name 的最后一条记录的 record_signature
        cur = self.conn.execute(
            "SELECT record_signature FROM audit_chain "
            "WHERE table_name = ? ORDER BY id DESC LIMIT 1",
            (table_name,),
        )
        row = cur.fetchone()
        prev_signature = row["record_signature"] if row else ""

        # 3. 获取当前活跃签名密钥（支持轮换，C7）
        signing_key_id, hmac_key, security_level = self._get_active_signing_key()

        # 4. 计算 record_signature
        record_signature = _compute_signature(
            prev_signature, payload_hash, hmac_key
        )

        # 5. 写入 audit_chain
        cur = self.conn.execute(
            """
            INSERT INTO audit_chain
                (table_name, record_id, operation, payload_hash,
                 prev_signature, record_signature, signing_key_id, signed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                table_name,
                str(record_id),
                operation,
                payload_hash,
                prev_signature,
                record_signature,
                signing_key_id,
                time.time(),
            ),
        )
        self.conn.commit()

        return {
            "id": cur.lastrowid,
            "table_name": table_name,
            "record_id": str(record_id),
            "operation": operation,
            "payload_hash": payload_hash,
            "prev_signature": prev_signature,
            "record_signature": record_signature,
            "signing_key_id": signing_key_id,
            "security_level": security_level,
        }

    def verify_audit_chain(
        self,
        table_name: str = "",
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """验证审计链是否连续、签名是否匹配

        检查项：
        1. 每条记录的 record_signature 是否匹配重新计算的签名
        2. 每条记录的 prev_signature 是否匹配上一条的 record_signature
        3. 首条记录的 prev_signature 是否为空串

        Args:
            table_name: 指定表名时只验证该表的链；为空时验证全部
            limit: 最多验证的记录数，默认 1000

        Returns:
            dict 包含：
            - table_name: 验证的表名（空串表示全部）
            - total_count: 验证的记录总数
            - verified_count: 通过验证的记录数
            - broken_count: 不通过的记录数
            - broken_records: 不通过的记录列表，每项含 id/reason
            - security_level: 当前签名安全级别
        """
        # 构造查询
        if table_name:
            sql = (
                "SELECT id, table_name, record_id, operation, payload_hash, "
                "prev_signature, record_signature, signing_key_id, signed_at "
                "FROM audit_chain WHERE table_name = ? ORDER BY id ASC LIMIT ?"
            )
            params: tuple = (table_name, limit)
        else:
            sql = (
                "SELECT id, table_name, record_id, operation, payload_hash, "
                "prev_signature, record_signature, signing_key_id, signed_at "
                "FROM audit_chain ORDER BY id ASC LIMIT ?"
            )
            params = (limit,)

        cur = self.conn.execute(sql, params)
        records = cur.fetchall()

        # 当前活跃密钥（用于报告 security_level）
        _, _, current_security_level = self._get_active_signing_key()

        broken_records: List[Dict[str, Any]] = []
        verified_count = 0

        # 链连续性校验：跟踪上一条记录的 record_signature
        # 按 table_name 分组（table_name 为空时全部当作一条链处理，但因为
        # verify 时按 id 升序遍历，不同 table_name 的记录会交错，需要分组）
        # 为简化：按 table_name 分组维护 prev_signature
        prev_signature_map: Dict[str, str] = {}

        for row in records:
            row_table = row["table_name"]
            row_id = row["id"]
            payload_hash = row["payload_hash"]
            prev_signature = row["prev_signature"]
            record_signature = row["record_signature"]
            signing_key_id = row["signing_key_id"]

            reasons: List[str] = []

            # 1. 验证 prev_signature 连续性
            expected_prev = prev_signature_map.get(row_table, "")
            if prev_signature != expected_prev:
                reasons.append("chain_broken")

            # 2. 重新计算 record_signature 并验证
            # 按 signing_key_id 查找对应密钥（支持轮换，C7）
            record_key = self._lookup_signing_key(signing_key_id)
            if signing_key_id == "local":
                # SHA-256 链，record_key 为 None
                recomputed = _compute_signature(
                    prev_signature, payload_hash, None
                )
                if recomputed != record_signature:
                    reasons.append("signature_mismatch")
            elif record_key is not None:
                # 找到对应 HMAC 密钥，重新计算
                recomputed = _compute_signature(
                    prev_signature, payload_hash, record_key
                )
                if recomputed != record_signature:
                    reasons.append("signature_mismatch")
            else:
                # signing_key_id 非 "local" 但找不到对应密钥
                reasons.append("signature_mismatch")

            # 3. 首条记录 prev_signature 应为空串
            if expected_prev == "" and prev_signature != "":
                reasons.append("first_prev_not_empty")

            if reasons:
                broken_records.append({
                    "id": row_id,
                    "table_name": row_table,
                    "record_id": row["record_id"],
                    "reasons": reasons,
                })
            else:
                verified_count += 1

            # 更新 prev_signature_map
            prev_signature_map[row_table] = record_signature

        return {
            "table_name": table_name,
            "total_count": len(records),
            "verified_count": verified_count,
            "broken_count": len(broken_records),
            "broken_records": broken_records,
            "security_level": current_security_level,
        }
