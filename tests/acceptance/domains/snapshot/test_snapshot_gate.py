"""TP-11Q 快照门 vs HEAD 回归验收：snapshot 发布/门控/沉默过期/恢复。

在完全隔离的 daemon 上验证快照面（snapshot gate）语义。🔴 本套件是
**顺序场景套件**（用例间共享 daemon 快照状态，按定义顺序执行）：

- Q1 daemon 侧门控：直连 RPC build_graph 不自动发布 → query.* 全
  snapshot_not_ready（且不影响其他已发布 workspace 的查询）；
- Q2 CLI refresh 隐式发布 + 元信息：generation / symbol_count /
  source_db_path 可观测；
- Q3 HEAD 推进 + 仅 daemon 侧重建（不重发 publish）→ **沉默过期 + 读面
  分裂**：内存读面（query.search / query.stats / snapshot.stats）服务旧
  generation（无错误、无 staleness 信号），query-DB 读面（query.symbol
  全限定名）直读实时库能查到新符号；
- Q4 显式 snapshot.publish 恢复：generation 递增、内存读面反映新数据、
  回显 registry 权威 snapshot_id；
- Q5 daemon 重启门控复位：快照缓存纯内存 → 重启后一切查询回到
  snapshot_not_ready；再 publish 后 generation 从 1 重新计；
- Q6 snapshot_id 权威绑定负向：与 registry 当前权威值不一致 →
  invalid_params。

🔴 关键探针结论（已固化、禁再猜）：
- 直连 RPC workspace.build_graph **不发布快照**；CLI refresh --all 经
  rule.sync_agents_md（route_rpc rule.* → needs_snapshot →
  _ensure_remote_snapshot）隐式发布 —— 用户日常 refresh 不撞门控靠 CLI 层
  补偿，daemon 侧无自动发布；
- query.symbol 的 qualified_name 是**模块限定精确匹配**
  （src/seed.seed_fn，不是短名 seed_fn，symbol_query.rs:44）；
- Q3 的「沉默过期」是**已观测的产品现状（非缺陷）**：daemon 不检测
  staleness，靠 refresh/publish 由客户端保持新鲜。本套件锁定该契约，使
  任何未来引入「服务端 staleness 门」的改动都必须同步更新本套件。
"""

from __future__ import annotations

import pytest

from snapshot_harness import SnapshotEnv

SEED_FN = "seed_fn"
SEED_QNAME = "src/seed.seed_fn"
MARKER_FN = "q3_marker"
MARKER_FILE = "src/q3marker.py"
MARKER_QNAME = "src/q3marker.q3_marker"


@pytest.fixture(scope="module")
def env():
    """模块级共享环境：隔离 daemon + 主 workspace 建图（CLI refresh 隐式发布）。

    顺序场景套件：Q1 先观测「全新第二 workspace 的门控」，之后 Q3/Q4/Q5 在
    主 workspace 上推进 HEAD 与快照状态。fixture 末尾按 TP-11 惯例 stop。
    """
    e = SnapshotEnv()
    # run 根自清洁（防上一轮残留 daemon/registry 污染本轮断言）
    # 🔴 必须显式传 e.run_root：无参默认会打到 TP-11I 的 run 根
    SnapshotEnv.wipe_run_root(e.run_root)
    e = SnapshotEnv()
    e.start_daemon()
    try:
        e.write_ws_file("src/seed.py", f"def {SEED_FN}():\n    return 1\n")
        # CLI refresh --all：建图 + 隐式发布（rule.sync_agents_md 链路）
        e.cli_refresh_all()
        yield e
    finally:
        e.stop_daemon()


# ---------------------------------------------------------------- Q1 daemon 侧门控

def test_q1_daemon_build_graph_does_not_publish_and_gates_queries(env):
    """Q1：直连 RPC build_graph 后，该 workspace 的 query.* 全被门控拦截。

    在全新的第二 workspace 上验证（不影响主 workspace 已发布快照）：
    build_graph 只落库、不发布快照——这是 daemon 侧的真实契约，也是
    「silently never publishes」的门控基础。同时证明门控是 per-instance
    的：第二 workspace 被拦截时，主 workspace 查询照常工作。
    """
    # 全新第二 workspace（独立目录，不经 CLI/refresh）
    wd = env.run_root / "ws-q1-gate"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "gate_src.py").write_text("def gate_fn():\n    return 0\n",
                                    encoding="utf-8")
    reg = env.rpc("workspace.register", {"client_view_root": str(wd)})
    assert reg.ok, f"register(gate) 失败：{reg!r}"
    gate_inst = str(reg.result.get("workspace_instance_id"))
    assert gate_inst and gate_inst != env._ws(), reg.result
    act = env.rpc("workspace.activate", {"workspace_instance_id": gate_inst})
    assert act.ok, f"activate(gate) 失败：{act!r}"
    bg = env.rpc("workspace.build_graph", {"workspace_instance_id": gate_inst})
    assert bg.ok, f"build_graph(gate) 失败：{bg!r}"
    assert (bg.result or {}).get("symbols", 0) >= 1, bg.result

    # 门控：未发布快照 → query.* 全部 snapshot_not_ready（错误码精确匹配）
    for r in (env.rpc("query.search", {"workspace_instance_id": gate_inst,
                                       "query": "gate_fn"}),
              env.rpc("query.symbol", {"workspace_instance_id": gate_inst,
                                       "qualified_name": "gate_src.gate_fn"}),
              env.rpc("query.stats", {"workspace_instance_id": gate_inst}),
              env.rpc("snapshot.stats", {"workspace_instance_id": gate_inst})):
        assert not r.ok and r.code == "snapshot_not_ready", (
            f"未 publish 的 workspace 查询应被 snapshot_not_ready 拦截：{r!r}")

    # per-instance 隔离：门控不串扰主 workspace（已发布）的查询
    main_search = env.query_search(SEED_FN)
    assert main_search.ok, f"主 workspace 查询被第二 workspace 状态影响：{main_search!r}"

    # snapshot.list_workspaces 只列**已发布缓存就绪**的 workspace
    listed = env.snapshot_list_workspaces()
    assert listed.ok, listed
    instances = {e.get("workspace_instance_id")
                 for e in (listed.result or [])}
    assert env._ws() in instances, "主 workspace 应在已发布列表中"
    assert gate_inst not in instances, "未发布的 gate workspace 不应出现在列表中"


# ---------------------------------------------------------------- Q2 隐式发布 + 元信息

def test_q2_cli_refresh_publishes_snapshot_with_observable_meta(env):
    """Q2：CLI refresh --all 隐式发布快照，元信息（generation/库路径）可观测。

    fixture 已执行 refresh，此处断言发布结果：snapshot.stats 可读、
    generation >= 1、symbol_count > 0、source_db_path 指向权威单库；
    query.search 内存读面与 query.symbol 实时库读面都能查到种子符号。
    """
    st = env.snapshot_stats()
    assert st.ok, f"refresh 后 snapshot.stats 应可用：{st!r}"
    meta = st.result or {}
    gen0 = meta.get("generation")
    sym0 = meta.get("symbol_count")
    assert isinstance(gen0, int) and gen0 >= 1, meta
    assert isinstance(sym0, int) and sym0 > 0, meta
    # Windows verbatim 前缀归一比较（daemon canonicalize 结果）
    assert str(meta.get("source_db_path", "")).replace("\\\\?\\", "") \
        == env.db_path(), meta

    # 内存读面：query.search
    search = env.query_search(SEED_FN)
    assert search.ok, search
    hits = search.result or []
    assert any(h.get("qualified_name") == SEED_QNAME for h in hits), hits

    # 实时库读面：query.symbol（模块限定全名，精确匹配契约）
    sym = env.query_symbol(SEED_QNAME)
    assert sym.ok, sym
    detail = sym.result
    assert isinstance(detail, dict), f"{SEED_QNAME} 应有符号详情：{sym!r}"
    assert detail.get("qualified_name") == SEED_QNAME, detail
    assert detail.get("file_path") == "src/seed.py", detail

    # query.stats 附带 generation + source_db_path（客户端可据此检测新鲜度）
    qs = env.query_stats()
    assert qs.ok, qs
    qm = qs.result or {}
    assert qm.get("generation") == gen0, qm
    assert str(qm.get("source_db_path", "")).replace("\\\\?\\", "") \
        == env.db_path(), qm


# ---------------------------------------------------------------- Q3 HEAD 推进沉默过期

def test_q3_head_move_without_publish_serves_stale_and_splits_read_paths(env):
    """Q3（核心回归）：HEAD 推进后只重建不重发 publish → 沉默过期 + 读面分裂。

    场景与断言（全部来自四分探针实测，非推测）：
    1. 新 commit（HEAD 移动）→ 显式 re-register：同 instance、新 snapshot_id；
    2. 仅直连 RPC build_graph（绝不碰 CLI refresh，否则隐式 publish）：
       新符号已落库；
    3. 沉默过期：snapshot.stats generation/symbol_count 原地不动、
       query.search 内存读面查不到新符号（ok=True、空结果——**没有任何
       错误或 staleness 信号**）；
    4. 读面分裂：同一时刻 query.symbol（实时库读面）**能**查到新符号详情。
    """
    st0 = env.snapshot_stats()
    assert st0.ok, st0
    gen_before = st0.result["generation"]
    sym_before = st0.result["symbol_count"]
    s1 = env.registry_snapshot_id()

    # HEAD 推进：写 marker 文件 + 提交
    env.write_ws_file(MARKER_FILE, f"def {MARKER_FN}():\n    return 42\n")
    new_head = env.git_commit_all("tp11q: q3 marker")
    # 显式 re-register（同 instance、新 snapshot_id）
    reg = env.register_with_head(new_head, instance_id=env._ws())
    assert reg.ok, f"re-register(new head) 失败：{reg!r}"
    s2 = env.registry_snapshot_id()
    assert s1 and s2 and s1 != s2, (
        f"HEAD 移动后 registry snapshot_id 应重算：S1={s1} S2={s2}")
    assert str(reg.result.get("workspace_instance_id")) == env._ws(), reg.result

    # 仅 daemon 侧重建（🔴 禁止 CLI refresh——会隐式 publish）
    bg = env.rpc("workspace.build_graph", {"workspace_instance_id": env._ws()})
    assert bg.ok, f"build_graph 失败：{bg!r}"
    assert (bg.result or {}).get("symbols", 0) >= 1, bg.result

    # 沉默过期：快照元信息原地不动
    st1 = env.snapshot_stats()
    assert st1.ok, st1
    assert st1.result["generation"] == gen_before, (
        f"未重发 publish 时 generation 不应移动：{st1.result}")
    assert st1.result["symbol_count"] == sym_before, (
        f"未重发 publish 时 symbol_count 不应变化：{st1.result}")

    # 沉默过期：内存读面查不到新符号（ok=True 空结果，无任何告警）
    stale_search = env.query_search(MARKER_FN)
    assert stale_search.ok, stale_search
    assert stale_search.result == [], (
        f"内存读面应沉默服务旧数据（空结果）：{stale_search.result}")

    # query.stats 同样反映旧快照（generation 未移动）
    qs = env.query_stats()
    assert qs.ok and qs.result.get("generation") == gen_before, qs

    # 读面分裂：query.symbol 实时库读面能查到新符号
    fresh_sym = env.query_symbol(MARKER_QNAME)
    assert fresh_sym.ok, fresh_sym
    assert isinstance(fresh_sym.result, dict) and \
        fresh_sym.result.get("qualified_name") == MARKER_QNAME, (
            f"query-DB 读面应实时反映新数据：{fresh_sym!r}")

    # 保存 Q4 要用的基线（模块内跨用例传递经 env 缓存）
    env._q3_baseline = {"gen": gen_before, "sym": sym_before, "snap": s2}


# ---------------------------------------------------------------- Q4 显式 publish 恢复

def test_q4_explicit_publish_restores_freshness(env):
    """Q4：显式 snapshot.publish 恢复新鲜——generation 递增、读面刷新。

    断言（实测契约）：publish 返回 generation = 旧值 + 1、symbol_count 增长、
    snapshot_id 回显 registry 权威值（HEAD 移动后重算的 S2）；随后
    query.search 内存读面查到 Q3 的新符号；snapshot.stats 同步刷新。
    """
    base = getattr(env, "_q3_baseline", None)
    assert base is not None, "Q3 必须先运行（顺序场景套件）"

    pub = env.publish()
    assert pub.ok, f"publish 失败：{pub!r}"
    out = pub.result or {}
    assert out["generation"] == base["gen"] + 1, out
    assert out["symbol_count"] > base["sym"], out
    assert out.get("snapshot_id") == base["snap"], (
        f"publish 应回显 registry 权威 snapshot_id：{out}")

    # 内存读面恢复新鲜
    search = env.query_search(MARKER_FN)
    assert search.ok, search
    assert any(h.get("qualified_name") == MARKER_QNAME
               for h in (search.result or [])), search.result

    st = env.snapshot_stats()
    assert st.ok and st.result["generation"] == base["gen"] + 1, st.result


# ---------------------------------------------------------------- Q5 重启门控复位

def test_q5_daemon_restart_resets_gate_and_generation(env):
    """Q5：daemon 重启 → 快照缓存（纯内存）丢失，门控复位，generation 重计。

    断言（实测契约）：重启后 query.* 回到 snapshot_not_ready、
    snapshot.list_workspaces 为空；再次 publish 成功且 generation **从 1
    重新计数**（per-process 计数器，不持久化跨重启）；数据为最新（Q3 的
    marker 可见）。
    """
    # 重启前主符号仍可查（正控）
    pre = env.query_search(SEED_FN)
    assert pre.ok and pre.result, pre

    env.stop_daemon()
    env.start_daemon()
    # 重启后实例 id 不变（registry 持久化）
    assert env._ws(), "registry 持久化：重启后 instance id 应保持"

    # 门控复位
    for r in (env.query_search(SEED_FN), env.snapshot_stats()):
        assert not r.ok and r.code == "snapshot_not_ready", (
            f"重启后未发布快照应被门控拦截：{r!r}")
    listed = env.snapshot_list_workspaces()
    assert listed.ok and (listed.result or []) == [], listed

    # 再发布：generation 从 1 重新计数（per-process）
    pub = env.publish()
    assert pub.ok, f"重启后 publish 失败：{pub!r}"
    assert pub.result["generation"] == 1, (
        f"重启后 generation 应从 1 重新计：{pub.result}")

    # 查询恢复且数据为最新（Q3 marker 在线）
    search = env.query_search(MARKER_FN)
    assert search.ok and any(h.get("qualified_name") == MARKER_QNAME
                             for h in (search.result or [])), search.result
    st = env.snapshot_stats()
    assert st.ok and st.result["generation"] == 1, st.result


# ---------------------------------------------------------------- Q6 snapshot_id 权威绑定

def test_q6_publish_with_mismatched_snapshot_id_rejected(env):
    """Q6：publish 显式传与 registry 权威值不一致的 snapshot_id → invalid_params。

    snapshot 权威归 workspace.register：HEAD 移动会重算 registry
    snapshot_id（Q3 已证），publish 不得为同一注册 workspace 发布不一致
    的快照身份（handle_snapshot_publish 显式比对，snapshot_state.rs:704）。
    """
    current = env.registry_snapshot_id()
    assert current, "主 workspace 应有权威 snapshot_id（git provenance）"
    bad = env.publish(snapshot_id="deadbeefdeadbeef")
    assert not bad.ok and bad.code == "invalid_params", (
        f"不一致的 snapshot_id 应被 invalid_params 拒绝：{bad!r}")

    # 对照：不传 snapshot_id 时继承 registry 权威值，发布成功
    ok_pub = env.publish()
    assert ok_pub.ok, ok_pub
    assert ok_pub.result.get("snapshot_id") == current, ok_pub.result
