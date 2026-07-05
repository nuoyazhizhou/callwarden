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

        # 3. 选择签名算法
        hmac_key = _get_hmac_key()
        if hmac_key is not None:
            signing_key_id = "hmac"
            security_level = "hmac"
        else:
            signing_key_id = "local"
            security_level = "hash_only"

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

        # 当前 HMAC key（用于重新计算签名）
        hmac_key = _get_hmac_key()
        current_security_level = "hmac" if hmac_key is not None else "hash_only"

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
            # 使用与原签名相同的密钥策略
            if signing_key_id == "hmac":
                # 原签名使用 HMAC，需要用当前 HMAC key 重新计算
                # 如果当前无 HMAC key，则签名必然不匹配
                if hmac_key is None:
                    reasons.append("signature_mismatch")
                    recomputed = ""
                else:
                    recomputed = _compute_signature(
                        prev_signature, payload_hash, hmac_key
                    )
                    if recomputed != record_signature:
                        reasons.append("signature_mismatch")
            else:
                # 原签名使用 SHA-256，用 SHA-256 重新计算
                recomputed = _compute_signature(
                    prev_signature, payload_hash, None
                )
                if recomputed != record_signature:
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
