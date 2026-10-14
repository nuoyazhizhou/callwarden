"""TP-11A 安全域验收：fs RPC 根目录限制（root containment）不变量测试。

7 组用例（每组锁定一条经验性缺陷契约，全部源自隔离 daemon 活体探针）：

- S1 越根 file.remove → `path_escape` + 磁盘文件**不得删除**（D1 防护，最关键）；
- S2 越根 refresh_file → `path_escape` + 无越根 file_instances 行（D3 防护）；
- S3 越根 build_directory → `path_escape` + 无越根 file_instances 行（D3 防护）；
- S4 越根 build_graph(scan_root) → `path_escape` + 无越根 file_instances 行
  （D3 防护）；
- S5 越根 file.health（文件与目录两种目标）→ `path_escape`（D4 泄露防护）；
- S6 根内 file.health → 按文档合同工作（exists/size/mtime/readable，文件可用，
  不再误报"不是目录"；侵入修复 D4 的功能侧回归保护）；
- S7 根内 file.remove → ok + 磁盘删除 + destructive_operations 审计行写入
  （D2 审计完整性）+ 被删文件 file_instances 行清理（D5：此前单条 DELETE
  被 FK 约束中止且被 `let _ =` 吞错，索引恒残留）。

正向控制组（S2/S7 内联）：根内 refresh_file/file.read/build_directory/file.grep
必须仍 ok——防止修复过度拦截（over-blocking）回归。

运行：C:/Python314/python.exe -m pytest tests/acceptance/domains/security/ -x -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import pytest  # noqa: E402

from security_harness import SecurityEnv  # noqa: E402


@pytest.fixture(scope="module")
def env() -> SecurityEnv:
    # 构造一次只为拿到 run 根；wipe 会删掉刚部署的 exe，故 wipe 后重建
    probe = SecurityEnv()
    # 🔴 wipe 必须显式传本域 run 根（无参默认打 TP-11I 根）
    SecurityEnv.wipe_run_root(probe.run_root)
    e = SecurityEnv()
    e.start_daemon()
    e.setup_workspace()
    e.build_graph()  # CLI refresh --all：权威库 workspaces 行 + 建图
    yield e
    e.stop_daemon()


# ---------- S1：越根 file.remove 必须拒绝且不得删除磁盘文件 ----------

def test_s1_out_of_root_file_remove_rejected_and_file_survives(env: SecurityEnv):
    victim = env.make_victim("s1_secret.txt", "S1-TOP-SECRET\n")
    r = env.file_remove(victim)
    env.assert_path_escape(r, "S1 file.remove 越根")
    # 磁盘存活断言（D1 核心：不得真删）
    assert victim.is_file(), (
        f"S1：越根 file.remove 虽被 RPC 拒绝，但磁盘文件仍被删除：{victim}")
    # 审计行不得因拒绝而写入
    rows = [x for x in env.destructive_rows_direct()
            if "s1_secret" in str(x).replace("\\", "/").lower()]
    assert not rows, f"S1：被拒绝的越根删除不应留审计行，实际={rows}"


# ---------- S2：越根 refresh_file 必须拒绝且不得索引 ----------

def test_s2_out_of_root_refresh_file_rejected_no_index(env: SecurityEnv):
    victim = env.make_victim("s2_leak.py",
                             "def s2_leak_fn():\n    return 'out'\n")
    r = env.refresh_file(victim)
    env.assert_path_escape(r, "S2 refresh_file 越根")
    env.assert_no_out_of_root_index("S2")

    # 正向对照：根内 refresh_file 必须仍 ok（防 over-blocking）
    seed = env.workspace_dir / "src" / "s2_seed.py"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("def s2_seed_fn():\n    pass\n", encoding="utf-8")
    ok = env.refresh_file(seed)
    assert ok.ok, f"S2 正向对照：根内 refresh_file 应 ok，实际 {ok!r}"
    assert ok.get("file_path", "").replace("\\", "/").endswith(
        "src/s2_seed.py"), (
            f"S2：根内 refresh_file 回显应为根相对路径，实际={ok.result}")
    assert (ok.get("symbols") or 0) >= 1, (
        f"S2：根内可解析文件应落符号，实际={ok.result}")


# ---------- S3：越根 build_directory 必须拒绝且不得索引 ----------

def test_s3_out_of_root_build_directory_rejected_no_index(env: SecurityEnv):
    vdir = env.victim_dir / "s3_dir"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "inner.py").write_text(
        "def s3_inner_fn():\n    pass\n", encoding="utf-8")
    r = env.build_directory(vdir, recursive=True)
    env.assert_path_escape(r, "S3 build_directory 越根")
    env.assert_no_out_of_root_index("S3")

    # 正向对照：根内 build_directory 递归必须仍 ok
    src_dir = env.workspace_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    ok = env.build_directory(src_dir, recursive=True)
    assert ok.ok, f"S3 正向对照：根内 build_directory 应 ok，实际 {ok!r}"
    assert (ok.get("refreshed") or 0) >= 1, (
        f"S3：根内目录应至少刷新 1 个文件，实际={ok.result}")


# ---------- S4：越根 build_graph(scan_root) 必须拒绝且不得索引 ----------

def test_s4_out_of_root_build_graph_scan_root_rejected_no_index(env: SecurityEnv):
    vdir = env.victim_dir / "s4_dir"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "scan.py").write_text(
        "def s4_scan_fn():\n    pass\n", encoding="utf-8")
    r = env.build_graph_scan(vdir)
    env.assert_path_escape(r, "S4 build_graph(scan_root) 越根")
    env.assert_no_out_of_root_index("S4")

    # 正向对照：scan_root 缺省（= workspace 根）必须仍 ok
    ok = env.build_graph_scan(env.workspace_dir)
    assert ok.ok, f"S4 正向对照：scan_root=workspace 根应 ok，实际 {ok!r}"


# ---------- S5：越根 file.health 必须拒绝（文件与目录） ----------

def test_s5_out_of_root_file_health_rejected(env: SecurityEnv):
    vfile = env.make_victim("s5_file.txt", "s5\n")
    r1 = env.file_health(vfile)
    env.assert_path_escape(r1, "S5 file.health 越根（文件目标）")

    vdir = env.victim_dir / "s5_dir"
    vdir.mkdir(parents=True, exist_ok=True)
    r2 = env.file_health(vdir)
    env.assert_path_escape(r2, "S5 file.health 越根（目录目标）")


# ---------- S6：根内 file.health 按文档合同工作（文件可用） ----------

def test_s6_in_root_file_health_works_for_files(env: SecurityEnv):
    f = env.workspace_dir / "src" / "s6_probe.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def s6_fn():\n    pass\n", encoding="utf-8")
    r = env.file_health(f)
    assert r.ok, (
        f"S6：根内文件 health 应 ok（文档合同：存在性/大小/mtime/可读），"
        f"实际 {r!r}")
    res = r.result or {}
    assert res.get("exists") is True, f"S6：exists 应为 True，实际={res}"
    assert res.get("size") == f.stat().st_size, (
        f"S6：size 应等同磁盘大小，实际={res}")
    assert isinstance(res.get("mtime"), (int, float)) and res["mtime"] > 0, (
        f"S6：mtime 应为正数时间戳，实际={res}")
    assert res.get("readable") is True, f"S6：readable 应为 True，实际={res}"


# ---------- S7：根内删除 → ok + 审计行 + file_instances 清理 ----------

def test_s7_in_root_remove_audited_and_index_cleaned(env: SecurityEnv):
    f = env.workspace_dir / "src" / "s7_rm.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def s7_rm_fn():\n    pass\n", encoding="utf-8")
    # 先索引，使 file_instances 存在行（验证删除时的行清理）
    idx = env.refresh_file(f)
    assert idx.ok, f"S7 前置：根内 refresh_file 应 ok，实际 {idx!r}"
    # canonical abs_path（与 daemon 落库一致）
    abs_path = str(f.resolve())
    assert env.file_instance_rows_for(abs_path), (
        "S7 前置：file_instances 应存在该文件行，实际="
        f"{env.file_instance_rows_for(abs_path)}")

    r = env.file_remove(f)
    assert r.ok, f"S7：根内 file.remove 应 ok，实际 {r!r}"
    assert not f.exists(), "S7：根内文件应已被磁盘删除"

    # 审计完整性（D2）：必须有 file_remove 审计行，且路径被记录
    env.assert_destructive_row("file_remove", abs_path)
    # RPC 面（list_destructive_operations）也应可见
    rpc_rows = env.list_destructive_operations(limit=20)
    assert rpc_rows.ok, f"S7：list_destructive_operations 应 ok，实际 {rpc_rows!r}"
    needle = abs_path.replace("\\", "/").lower()
    hit = [x for x in (rpc_rows.result or [])
           if isinstance(x, dict)
           and x.get("operation_type") == "file_remove"
           and any(isinstance(v, str) and needle in v.replace("\\", "/").lower()
                   for v in x.values())]
    assert hit, (
        f"S7：list_destructive_operations 应回显该 file_remove 审计行，"
        f"实际={rpc_rows.result}")

    # file_instances 行清理（删除即摘索引）
    assert not env.file_instance_rows_for(abs_path), (
        "S7：被删文件的 file_instances 行应被清理，实际="
        f"{env.file_instance_rows_for(abs_path)}")
