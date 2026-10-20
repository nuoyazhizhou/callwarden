"""CI-DEBT 深水区 A/B 探针插件（临时，实验结束后删除）。

用途：判别 fts stale read（tests/test_p7_batch_calls.py:287 "FTS 搜索 multiply
应返回结果，实际 []"）是否由 build 路径的 Rust 批量写入短路引入。

机制：CW_WAL_AB_ROLLBACK_RUST_BATCH=1 时，对每个新建的 CodeGraphDB 实例注入
rollback_config 行，把 build 路径全部 6 个 Rust 写短路置为回滚
（rust_batch_register_files / rust_load_file_result / rust_batch_resolve_and_save_calls /
rust_batch_save_file_versions / rust_compute_symbol_diff / rust_batch_save_symbols），
强制符号/文件版本/调用关系的写入走 Python 降级路径。

判读：
- 关闭后转绿 → fts 失败确由 Rust 写入路径引入（与 malformed/IOERR 同族），
  后续在 Rust 侧修（checkpoint 门/单实例化）；
- 仍红 → Rust 写入路径不是根因，需转向 schema 初始化或 Python 双连接方向。

默认（env 未设置）完全惰性：不 patch、不注入，对任何其他 pytest 运行零影响。
仅由 CI probe job 的 config C 通过 -p 显式加载。
"""
import os
import time

# build 路径全部 Rust 写短路特性名（db/db_build.py 中 is_feature_rolled_back 门）
_RUST_WRITE_FEATURES = (
    "rust_batch_register_files",
    "rust_load_file_result",
    "rust_batch_resolve_and_save_calls",
    "rust_batch_save_file_versions",
    "rust_compute_symbol_diff",
    "rust_batch_save_symbols",
)

_ENABLED = os.environ.get("CW_WAL_AB_ROLLBACK_RUST_BATCH") == "1"

# rollback_config 插入模板（schema：rust_ext/src/storage.rs:332 与
# db/db_base.py:2156 同形；is_feature_rolled_back 按 updated_at DESC 取最新行）
_INSERT_SQL = (
    "INSERT OR REPLACE INTO rollback_config "
    "(task_id, feature_name, phase, production_entry, rollback_entry, "
    " rollback_flag, created_at, updated_at) "
    "VALUES (?, ?, 0, '', '', 1, ?, ?)"
)


def _patch_code_graph_db() -> None:
    """env 开启时 patch CodeGraphDB.__init__：建库即注入回滚行。"""
    if not _ENABLED:
        return
    from callwarden.db.db import CodeGraphDB

    if getattr(CodeGraphDB, "_wal_ab_patched", False):
        return
    _orig_init = CodeGraphDB.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        try:
            now = time.time()
            for feature in _RUST_WRITE_FEATURES:
                self.conn.execute(
                    _INSERT_SQL, ("wal-ab-probe", feature, now, now)
                )
            self.conn.commit()
        except Exception:
            # 注入失败（表不存在等）不静默改变测试语义——仅本探针用途，
            # 失败时让测试以原路径运行，结果由 CI 日志人工判读。
            pass

    CodeGraphDB.__init__ = _patched_init
    CodeGraphDB._wal_ab_patched = True


_patch_code_graph_db()
