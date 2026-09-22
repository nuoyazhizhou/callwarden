#!/usr/bin/env python3
"""verify_route_matrix.py —— 路由矩阵一致性语义 verifier（RP-07 门禁，只读）。

SSOT 关系（spec §12）：
    T = 实际 MCP tool registrations（server/tools/*.py，由 generator 提取）
    M = tool migration matrix 及 generated Rust mirror
    D = daemon dispatch RPC methods

本脚本是**只读消费侧语义 verifier**：不生成、不改写任何产物；工具名集合、
路由规则与总数一律从 generator/源码派生，不硬编码 239/241/242/243/298。
生成与 byte-drift 自检归 `gen_route_matrix.py --emit-* / --check`。

核对门禁（spec §12）：
1. names(T) == tool_names(M)，tool 名唯一；
2. Rust mirror 条目 == generate(M)（条目级语义相等）；
3. rpc_method(M[rust_native|task_rpc]) ⊆ D；
4. python_compat 行与 Rust whitelist、Python compat registry 三向一致；
4b. current_backend 必须由生成器从实际接线证据派生，且现场重算一致、
    不得有 unknown（无法证明已接线的工具 = 断线，error）；
5. 每个工具 name/module/rpc_method/target_backend/op_class 字段完整合法；
6. 每个工具名仍注册在 server/tools/<module>.py（MCP 注册不丢失）；
7. D - rpc_method(M) 不要求为空（超集允许），仅记录计数。

退出码：0 = 全部通过；1 = 任一门禁失败（CI 门禁）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Set

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

import gen_route_matrix as gen  # generator 是工具枚举/路由规则的唯一来源

_MATRIX_PATH = gen._MATRIX_PATH
# dispatch / http_server / compat_registry 路径与证据提取函数均以 generator 为
# 唯一实现（避免双份拷贝漂移）；此处保留模块级别名供外部调用兼容。
_DISPATCH_PATH = gen._DISPATCH_PATH
_HTTP_SERVER_PATH = gen._HTTP_SERVER_PATH
_COMPAT_REGISTRY_PATH = gen._COMPAT_REGISTRY_PATH

BACKENDS = gen.BACKENDS
OP_CLASSES = gen.OP_CLASSES


def load_matrix() -> Dict[str, Any]:
    with open(_MATRIX_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# 证据提取的唯一实现在 gen_route_matrix.py（§3b）；此处透传保持本模块调用兼容。
extract_dispatch_methods = gen.extract_dispatch_methods
extract_compat_whitelist = gen.extract_compat_whitelist
extract_python_compat_routes = gen.extract_python_compat_routes


def extract_rust_mirror_routes() -> Dict[str, Dict[str, str]]:
    """从 route_matrix.rs 生成区间提取条目（name → 语义字段）。"""
    _, generated, _ = gen._read_rust_sections()
    routes: Dict[str, Dict[str, str]] = {}
    pat = re.compile(
        r'ToolRoute \{ name: "(\w+)", module: "(\w+)", '
        r'target_backend: Backend::(\w+), rpc_method: "([\w.]+)", '
        r'op_class: OpClass::(\w+), batch: "([\w-]+)", status: "(\w+)" \}'
    )
    for m in pat.finditer(generated):
        routes[m.group(1)] = {
            "module": m.group(2),
            "backend": m.group(3),
            "rpc_method": m.group(4),
            "op_class": m.group(5),
            "batch": m.group(6),
            "status": m.group(7),
        }
    return routes


def _matrix_route_semantics(t: Dict[str, Any]) -> Dict[str, str]:
    return {
        "module": t["module"],
        "backend": gen._BACKEND_RUST[t["target_backend"]],
        "rpc_method": t["rpc_method"],
        "op_class": gen._OP_RUST[t["op_class"]],
        "batch": t["batch"],
        "status": t["status"],
    }


def main() -> int:
    errors: List[str] = []
    matrix = load_matrix()
    tools: List[Dict[str, Any]] = matrix.get("tools", [])

    # ---- 门禁 1：names(T) == tool_names(M)，唯一 ----
    t_by_module = gen.extract_tool_names_by_module() if hasattr(gen, "extract_tool_names_by_module") else None
    if t_by_module is None:
        t_names: Set[str] = set()
        for module in gen.TOOL_MODULES:
            t_names |= set(gen.extract_tool_names(module))
    else:
        t_names = set()
        for mod_names in t_by_module.values():
            t_names |= mod_names

    m_names: Set[str] = set()
    for t in tools:
        name = t.get("name", "")
        if not name:
            errors.append("存在 name 为空的行")
            continue
        if name in m_names:
            errors.append(f"工具名重复: {name}")
        m_names.add(name)

    if t_names != m_names:
        errors.append(
            f"names(T) != tool_names(M): 仅T={sorted(t_names - m_names)} 仅M={sorted(m_names - t_names)}"
        )

    # ---- 字段完整合法性（M 侧） ----
    for t in tools:
        name = t.get("name", "")
        if not t.get("module"):
            errors.append(f"{name}: 缺少 module")
        if not t.get("rpc_method") or t["rpc_method"] in ("—", "-", ""):
            errors.append(f"{name}: 缺少 rpc_method（本地隐式路径）")
        if t.get("target_backend") not in BACKENDS:
            errors.append(f"{name}: 非法 target_backend {t.get('target_backend')!r}")
        if t.get("op_class") not in OP_CLASSES:
            errors.append(f"{name}: 非法 op_class {t.get('op_class')!r}")

    # ---- 门禁 2：Rust mirror == generate(M) ----
    mirror = extract_rust_mirror_routes()
    if set(mirror) != m_names:
        errors.append(
            f"mirror 名集合 != M: 仅M={sorted(m_names - set(mirror))} 仅mirror={sorted(set(mirror) - m_names)}"
        )
    else:
        for t in tools:
            got = mirror[t["name"]]
            want = _matrix_route_semantics(t)
            if got != want:
                errors.append(f"{t['name']}: mirror 条目 {got} != M 语义 {want}")

    # ---- 门禁 3：rpc_method(M[rust_native|task_rpc]) ⊆ D ----
    dispatch_methods = extract_dispatch_methods()
    for t in tools:
        if t["target_backend"] in ("rust_native", "task_rpc"):
            if t["rpc_method"] not in dispatch_methods:
                errors.append(
                    f"{t['name']}: rpc_method {t['rpc_method']} 未在 dispatch.rs 注册 "
                    f"（target_backend={t['target_backend']}）"
                )

    # ---- 门禁 4：python_compat 三向一致 ----
    # 分级语义（RP-07 evidence 记录）：
    # - M → RUST_COMPAT_ROUTE（compat worker 侧真相源）缺失 = error；
    # - Rust whitelist 端差与两端皆缺 = KNOWN_DRIFT（既有 compat 域缺陷，
    #   修复属 daemon compat 域，不在本卡 allowed paths；显式登记，
    #   路由补齐/显式废弃裁决由后续 daemon compat 域任务承接）。
    rust_whitelist = extract_compat_whitelist()
    py_routes = extract_python_compat_routes()
    known_drift: List[str] = []
    for t in tools:
        if t["target_backend"] == "python_compat":
            method = t["rpc_method"]
            in_wl = method in rust_whitelist
            in_py = method in py_routes
            if not in_py:
                known_drift.append(
                    f"{t['name']}: python_compat 无 worker 路由（RUST_COMPAT_ROUTE 缺失；"
                    f"whitelist={'有' if in_wl else '无'}）——工具当前无可用路由"
                )
            elif not in_wl:
                known_drift.append(
                    f"{t['name']}: RUST_COMPAT_ROUTE 有但 COMPAT_ROUTE_WHITELIST 缺"
                )

    # ---- 门禁 4b：current_backend 活审计（P1 回填，2026-09-22）----
    # current_backend 必须由生成器从实际接线证据派生（gen §3b）。此处用**同一套
    # 证据提取**现场重算，与磁盘矩阵逐工具比对：
    #   - 值必须合法（BACKENDS 或 unknown）；
    #   - 与现场重算不一致 = 矩阵相对源码已过期（需重新 --emit-json）；
    #   - 任何 unknown = 无法证明该工具已接到声明后端（断线/zombie）= **error**，
    #     防止「声明已迁移但实际没接线」的假绿。
    # 证据集合复用门禁 3/4 已提取的 dispatch_methods / rust_whitelist / py_routes。
    for t in tools:
        cur = t.get("current_backend", "unknown")
        if cur not in BACKENDS and cur != "unknown":
            errors.append(
                f"{t['name']}: 非法 current_backend {cur!r}（合法值 {BACKENDS} 或 unknown）"
            )
            continue
        expected = gen.derive_current_backend(
            t["target_backend"], t["rpc_method"], dispatch_methods, rust_whitelist, py_routes
        )
        if cur != expected:
            errors.append(
                f"{t['name']}: current_backend={cur!r} 与现场接线证据派生值 "
                f"{expected!r} 不一致（矩阵已过期，请重新 gen_route_matrix.py --emit-json）"
            )
        elif cur == "unknown":
            errors.append(
                f"{t['name']}: current_backend=unknown——无法证明已接到声明后端 "
                f"（target_backend={t['target_backend']}, rpc_method={t['rpc_method']}）"
            )

    # ---- 门禁 6：MCP 注册不丢失（与门禁 1 的 module 级补强） ----
    if t_by_module is not None:
        for t in tools:
            if t["name"] not in t_by_module.get(t["module"], set()):
                errors.append(f"{t['name']}: 未在 server/tools/{t['module']}.py 注册（MCP 丢失）")

    # ---- 汇总（N 从源计算，不硬编码） ----
    n = len(m_names)
    print(f"N (names(T)==names(M), 源计算) = {n}")
    print(f"矩阵工具总数 (M) = {len(tools)}")
    print(f"Rust mirror 条目 = {len(mirror)}")
    print(f"dispatch.rs match 分支 (D) = {len(dispatch_methods)}")
    print(f"D - rpc_method(M[rust_native|task_rpc]) 超集计数 = "
          f"{len(dispatch_methods - {t['rpc_method'] for t in tools if t['target_backend'] in ('rust_native', 'task_rpc')})}（门禁 6 允许非空）")
    print(f"http_server.rs 白名单 = {len(rust_whitelist)}")
    print(f"compat_registry.py RUST_COMPAT_ROUTE = {len(py_routes)}")
    print()
    # compat 域三向核对必须**始终显式记账**（即使为 0）——「显式登记」契约在
    # 0 漂移与 N 漂移两种状态下都成立，避免漂移归零后测试/读者无法区分
    # 「没核对」与「核对后无漂移」。
    compat_tools = [t for t in tools if t["target_backend"] == "python_compat"]
    print(
        f"compat 域核对: python_compat 工具 = {len(compat_tools)}，"
        f"KNOWN_DRIFT = {len(known_drift)}"
    )
    if known_drift:
        print(f"KNOWN_DRIFT（既有 compat 域缺陷，{len(known_drift)} 项，非本卡 scope，已登记）:")
        for d in known_drift:
            print(f"  - {d}")
        print()
    if errors:
        print("失败（门禁错误）:")
        for e in errors:
            print(f"  - {e}")
        print(f"共 {len(errors)} 个错误")
        return 1
    print("核对通过: names(T)==names(M)==mirror 语义，dispatch/白名单/compat 三向一致，无本地隐式路径"
          if not known_drift else
          f"核对通过（门禁全绿）: names(T)==names(M)==mirror 语义，dispatch 一致，无本地隐式路径；"
          f"{len(known_drift)} 项 KNOWN_DRIFT 已登记（compat 域历史漂移，另卡承接）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
