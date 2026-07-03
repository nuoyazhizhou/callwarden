"""
db/__init__.py
==============

代码图谱数据库层子包。

兼容层：保持 ``from code_graph.db import CodeGraphDB`` 在迁移后仍可用。
``code_graph.db`` 从单文件模块（``db.py``）升级为包（``db/``）后，
此 ``__init__.py`` 重新导出主入口类 ``CodeGraphDB``，
使所有既有引用方（cli / server / cicd / tests）无需修改即可继续工作。
"""

from .db import CodeGraphDB

__all__ = ["CodeGraphDB"]
