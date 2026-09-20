# -*- coding: utf-8 -*-
"""C-13 根因复现脚本（权威证据）。

db/db_stdlib.py import_stdlib_symbols_for_lang 在符号已存在（skipped）后
仍执行 INSERT external_symbols → FOREIGN KEY constraint failed。
空库 build_full_graph() 必现。

用法：python deliverables/software-company/pyt_step6_c13_repro.py
退出码 0 = 已修复（无异常）；1 = 缺陷仍存在。
"""
import os
import sys
import tempfile

sys.path.insert(0, "C:/git_work")

from callwarden.db.db import CodeGraphDB  # noqa: E402


def main():
    tmp = tempfile.mkdtemp(prefix="cw_c13_")
    db_path = os.path.join(tmp, "t.db")
    db = CodeGraphDB(db_path=db_path, workspace_root=tmp)
    db.register_workspace(name="c13-repro", root_path=tmp)
    try:
        db.build_full_graph()
    except Exception as exc:
        print("FAIL: %s: %s" % (type(exc).__name__, str(exc)[:200]))
        return 1
    print("OK: 空库 build_full_graph 零异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
