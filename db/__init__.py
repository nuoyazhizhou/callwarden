"""
db/__init__.py
==============

代码图谱数据库层子包 —— **仅测试支持层（test-support only）**。

⚠️ 退休状态（db/ 退休验收③，路线 B）
------------------------------------
业务/DB 逻辑已 100% 下沉 Rust daemon；生产侧（cli / server / cicd）
**零 db 导入**，``import callwarden`` 不再加载任何 ``callwarden.db.*``
模块（由 ``tests/convergence/test_db_retire_prod_purity.py`` 锁定）。

本包保留**仅为测试支持**：遗留类 ``CodeGraphDB``（30+ Mixin 组合的
Python 层图谱数据库）仍被 tests/ 直接使用来构造离线测试夹具。
**生产代码不得导入本包**；新增生产侧 ``from callwarden.db ...``
会被上述守卫测试当场拦下。

兼容层：保持 ``from callwarden.db import CodeGraphDB`` 在 tests 中可用。
``callwarden.db`` 从单文件模块（``db.py``）升级为包（``db/``）后，
此 ``__init__.py`` 重新导出主入口类 ``CodeGraphDB``。
"""

from .db import CodeGraphDB

__all__ = ["CodeGraphDB"]
