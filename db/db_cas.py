"""db_cas.py —— db/ 退休过渡期重导出壳

db/ 退休 phase-3：CAS 存储层已整体搬迁至 ``callwarden.server.cas_schema``。
本模块仅保留重导出，供仍在过渡期引用 ``callwarden.db.db_cas`` 的代码（主要是测试）使用；
新代码应直接 ``from callwarden.server.cas_schema import ...``。

搬迁内容对照：
- CAS_SCHEMA_DDL / CAS_INDEX_SQL / init_cas_schema / FILE_GENERATIONS_DDL → cas_schema
- compute_cas_key_v1 / cas_lookup / cas_pin / cas_publish / cas_publish_with_retry
  → cas_schema（从 replicator 重导出，Rust 优先版）
- _flock_* / _HAS_FCNTL / _HAS_MSVCRT / cas_gc
  / file_generation_seen / file_generation_committed → cas_schema
"""
from __future__ import annotations

from callwarden.server.cas_schema import (  # noqa: F401
    FILE_GENERATIONS_DDL,
    CAS_SCHEMA_DDL,
    CAS_INDEX_SQL,
    init_cas_schema,
    compute_cas_key_v1,
    cas_lookup,
    cas_pin,
    cas_publish,
    cas_publish_with_retry,
    cas_gc,
    file_generation_seen,
    file_generation_committed,
    _flock_exclusive,
    _flock_shared,
    _flock_unlock,
    _HAS_FCNTL,
)

# _HAS_MSVCRT 仅在 fcntl 不可用时由 cas_schema 定义，这里安全兜底
import callwarden.server.cas_schema as _cas_schema  # noqa: E402

_HAS_MSVCRT = getattr(_cas_schema, "_HAS_MSVCRT", False)  # noqa: F401
