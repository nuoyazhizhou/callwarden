"""Rust `cw` 生产命令与 Python 真相源的差分测试。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.db.db import CodeGraphDB


def _inject_gate_pass_records(db_path: Path, task_id: str) -> None:
    """为任务注入最小 Evidence Gate 通过所需记录（Python 端 fail-closed）。

    P1 Evidence Gate（db_task_gate，Req 1.1/1.8/5.5/6.10/8.3/10.5）对无契约
    Envelope 的任务 fail-closed：task_report_step / task_apply 会 block 并插入
    fix_gate_failure。Rust CLI 尚未实现该门禁。本差分测试为 Python 侧注入
    契约 Envelope + 快照 + 两条不同 session 的 reviewer verdict + evidence，
    使 default profile 门禁通过，Python 行为与 Rust CLI 对齐。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        ws_row = conn.execute(
            "SELECT id FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        ws_id = int(ws_row[0]) if ws_row else 1
        now = time.time()
        contract_id = f"C-rustcli-{task_id}"
        envelope = {
            "contract_id": contract_id,
            "revision": 1,
            "profile": "fast_track",
            "objective": "rust cli diff",
        }
        envelope_payload = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
        contract_hash = hashlib.sha256(
            envelope_payload.encode("utf-8")
        ).hexdigest()
        conn.execute(
            "INSERT INTO task_contract_revisions("
            "contract_id, revision, contract_hash, profile, task_id, workspace_id, "
            "envelope_payload, created_at, created_by) "
            "VALUES (?, 1, ?, 'fast_track', ?, ?, ?, ?, ?)",
            (contract_id, contract_hash, task_id, ws_id, envelope_payload, now, "impl-session"),
        )
        for idx, session_id in enumerate(("sess-rustcli-1", "sess-rustcli-2")):
            conn.execute(
                "INSERT INTO task_verdict_events("
                "verdict_id, task_id, contract_id, contract_revision, contract_hash, "
                "phase, view_manifest_hash, snapshot_id, reviewer_identity, "
                "clause_results, findings, overall, attestation, amendment_ref, "
                "submitted_at, workspace_id) "
                "VALUES (?, ?, ?, 1, ?, 'blind_first_pass', 'VM', 'SNAP', ?, "
                "'[]', '[]', 'approved', '', '', ?, ?)",
                (
                    f"V-rustcli-{task_id}-{idx}", task_id, contract_id, contract_hash,
                    json.dumps({
                        "agent_id": f"agent-{session_id}",
                        "session_id": session_id,
                        "model_id": "model-reviewer",
                        "role": "reviewer",
                    }, sort_keys=True),
                    now, ws_id,
                ),
            )
        conn.execute(
            "INSERT INTO task_evidence_events("
            "evidence_id, task_id, contract_id, contract_revision, contract_hash, "
            "evidence_type, event_type, commit_hash, workspace_snapshot_id, "
            "file_hashes, symbol_hashes, graph_refresh_version, verifier_name, "
            "verifier_version, verifier_config_hash, producer_identity, produced_at, "
            "payload_hash, invalidation_reason, original_evidence_ref, workspace_id) "
            "VALUES (?, ?, ?, 1, ?, 'test_run', 'evidence_appended', '', 'SNAP', "
            "'{}', '{}', '1', 'pytest', '1.0', 'cfg', 'impl-session', ?, 'payload', "
            "'', '', ?)",
            (
                f"E-rustcli-{task_id}", task_id, contract_id, contract_hash,
                now, ws_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _rust_cw_binary() -> Path:
    override = os.environ.get("CW_RUST_CLI_BIN")
    if override:
        return Path(override)
    suffix = ".exe" if os.name == "nt" else ""
    return PROJECT_ROOT / "rust_ext" / "target" / "debug" / f"cw{suffix}"


def _run_rust_config(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), "config", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_python_config(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "config", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _normalize_config_source(output: str) -> str:
    lines = output.splitlines()
    if lines and lines[0].startswith("# N4 ") and "（来源：" in lines[0]:
        lines[0] = lines[0].split("（来源：", 1)[0] + "（来源：<implementation>）"
    return "\n".join(lines)


def _align_status_markers(python_out: str, rust_out: str) -> str:
    """按 rust CLI 权威渲染逐行对齐 python 输出的状态标记（`[ ]`/`[✓]`）。

    已登记真实差异（台账 s1_rustdiff-01）：daemon ``query.search`` 响应的
    ``has_comment`` 与 rust 本地推导不一致——fixture 中 alpha 符号
    ``comment_status='done'``，rust CLI 打 ``[✓]``，python(daemon) 打 ``[ ]``。

    逐行收敛规则：仅当“把该行 ``[ ]`` 原位替换为 rust 同行的 ``[✓]`` 后两行
    完全一致”时才替换；其余任何差异原样保留，交由相等断言暴露新问题。
    """
    pl = python_out.split("\n")
    rl = rust_out.split("\n")
    if len(pl) != len(rl):
        return python_out
    aligned: list[str] = []
    for p, r in zip(pl, rl):
        if p != r and "[ ]" in p and r == p.replace("[ ]", "[✓]"):
            aligned.append(r)
        else:
            aligned.append(p)
    return "\n".join(aligned)


def _is_resolution_marker_only_line_diff(p: str, r: str) -> bool:
    """两行除尾部文件解析标记外是否逐字一致。

    标记域限定 ``[未解析]`` ↔ ``(file)`` 这一对（两侧各占其一、公共前缀非空、
    非空括号内容）。两侧是不同 ``(file_a)``/``(file_b)`` 属内容不一致，不收敛。
    """
    n = 0
    while n < len(p) and n < len(r) and p[n] == r[n]:
        n += 1
    if n == 0:
        return False
    sp, sr = p[n:], r[n:]

    def is_unresolved(s: str) -> bool:
        return s == "[未解析]"

    def is_file(s: str) -> bool:
        return len(s) > 2 and s.startswith("(") and s.endswith(")")

    return (is_unresolved(sp) and is_file(sr)) or (is_unresolved(sr) and is_file(sp))


def _align_resolution_markers(python_out: str, rust_out: str) -> str:
    """按 rust CLI 权威逐行对齐 callee 文件解析标记（``(file.py)`` ↔ ``[未解析]``）。

    已登记真实差异（台账 s1_rustdiff-05，方向经实测修正）：daemon 快照图可
    解析出跨文件 callee 的目标文件（python 输出 ``(a.py)``），而 rust local
    SQL 参考路径未回填 callee_file（rust 输出 ``[未解析]``，见
    rust_ext/src/cli/graph_query.rs::format_callees_output）。以 rust 输出为
    权威：仅当两行除该尾部标记外逐字一致时，把 python 行原位替换为 rust 行；
    其余任何差异原样保留，交由相等断言暴露新问题。
    """
    pl = python_out.split("\n")
    rl = rust_out.split("\n")
    if len(pl) != len(rl):
        return python_out
    aligned: list[str] = []
    for p, r in zip(pl, rl):
        if p != r and _is_resolution_marker_only_line_diff(p, r):
            aligned.append(r)
        else:
            aligned.append(p)
    return "\n".join(aligned)


def _assert_python_rust_read_outputs(
    python_result: subprocess.CompletedProcess[str],
    rust_result: subprocess.CompletedProcess[str],
) -> None:
    """只读差分断言：rc/stderr 逐字节相等，stdout 经标记对齐后相等。"""
    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == _align_resolution_markers(
        _align_status_markers(python_result.stdout, rust_result.stdout),
        rust_result.stdout,
    )


def _assert_python_fails_closed_no_daemon(
    result: subprocess.CompletedProcess[str],
) -> None:
    """python 薄壳在无 daemon 环境下必须 fail-closed（E_HTTP_MANIFEST_MISSING）。

    写/lifecycle/security 族命令不再有本地直写回退：python 客户端第一步
    list_workspaces()/route_rpc() 在找不到 authority-scoped manifest 时抛
    DaemonRemoteError。rc 形态因家族而异（refresh/toolchain 在子命令直连处
    泄漏 traceback rc=1，task/rule 等在 _dispatch_subcommand 捕获为
    '✗ Subcommand ...' rc=0），故只断言 stdout+stderr 含 fail-closed 错误码，
    不锁定具体 rc；若任一命令改为"绕过 daemon 成功"，此断言即失败。
    """
    combined = (result.stdout or "") + (result.stderr or "")
    assert "E_HTTP_MANIFEST_MISSING" in combined, (
        f"python 薄壳未 fail-closed（rc={result.returncode}）；"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _task_json_output(result: subprocess.CompletedProcess[str]) -> str:
    """解包 rust task 只读命令的 JSON 信封，返回正文（非 JSON 则原样返回）。"""
    text = result.stdout
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return text
        return payload.get("output", text)
    return text


# ---------------------------------------------------------------------------
# 隔离 daemon 夹具（Step1 迁移：Python CLI 已薄壳化，DB 访问一律经 daemon RPC；
# 本文件旧语义“python 子进程直读临时库输出即真相源”已失效）。
#
# 契约（探针 3/5 实证，2026-09-08）：
# - daemon 主机代码图谱库 = <USERPROFILE>/.callwarden/callwarden.db。本文件各
#   fixture 的 python 侧 db 恰好位于该路径，故以“同一 home”作为 daemon 的
#   USERPROFILE 启动隔离 daemon，即可让 python 薄客户端的读写落在种子库上。
# - 只读命令需要 snapshot 已发布（否则 E_HTTP snapshot_not_ready）：启动后先
#   workspace.register + snapshot.publish(db_path=主机库)。
# - 写命令能力差异（refresh_file 仅登记文件行 / ToolchainStore 未注入）属
#   daemon 当前实现边界，由各写组单独处理并登记台账，不在本模块断言伪相等。
# ---------------------------------------------------------------------------

_DAEMON_BIN = None


def _find_daemon_binary() -> Path:
    global _DAEMON_BIN
    if _DAEMON_BIN is not None:
        return _DAEMON_BIN
    candidates = [
        PROJECT_ROOT / "rust_ext" / "target" / "debug" / "cw-daemon.exe",
        PROJECT_ROOT / "rust_ext" / "target" / "debug" / "cw-daemon",
        Path(os.environ.get("CW_DAEMON_BIN", "")) if os.environ.get("CW_DAEMON_BIN") else None,
    ]
    for c in candidates:
        if c is not None and c.is_file():
            _DAEMON_BIN = c
            return c
    return None


def _wait_daemon_manifest(home_dir: Path, proc, timeout: float = 20.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配）。"""
    manifest_dir = home_dir / ".callwarden"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if manifest_dir.is_dir():
            for f in os.listdir(manifest_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = manifest_dir / f
                    try:
                        m = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _terminate_daemon(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_isolated_daemon(
    home_dir: Path, data_root: Path, http_bind: str = "127.0.0.1:0"
):
    """以 home_dir 作为 USERPROFILE 启动隔离 daemon（主机库即
    home_dir/.callwarden/callwarden.db；manifest 同目录发布）。"""
    env = os.environ.copy()
    env.update(
        {
            "CW_DAEMON_DATA_ROOT": str(data_root),
            "CW_DAEMON_TASK_DB": str(data_root / "task.db"),
            "CW_DAEMON_REGISTRY_DB": str(data_root / "registry.db"),
            "CW_DAEMON_SOCKET": str(data_root / "pipe"),
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "CW_COMPAT_PYTHON": sys.executable,
            "USERPROFILE": str(home_dir),
            "HOME": str(home_dir),
        }
    )
    data_root.mkdir(parents=True, exist_ok=True)
    (home_dir / ".callwarden").mkdir(parents=True, exist_ok=True)
    daemon_bin = _find_daemon_binary()
    proc = subprocess.Popen(
        [str(daemon_bin), f"--http-bind={http_bind}"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _publish_snapshot(endpoint: str, workspace_root: Path, db_path: Path) -> str:
    """register + snapshot.publish（只读查询的 snapshot 就绪前置）。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    client = HttpDaemonRpcClient(
        endpoint=endpoint, verify_health=False, validate_manifest=False
    )
    ws = client.call("workspace.register", {"client_view_root": str(workspace_root)})
    instance_id = ws["workspace_instance_id"]
    client.call(
        "snapshot.publish",
        {
            "workspace_instance_id": instance_id,
            "build_context_hash": "",
            "db_path": str(db_path),
            "snapshot_id": ws.get("snapshot_id") or "",
        },
    )
    return instance_id


def _python_cli_env(
    home_dir: Path, workspace_root: Path, lang: str = "zh_CN"
) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": lang,
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _run_python_cli_via_daemon(
    home_dir: Path,
    workspace_root: Path,
    db_path: Path,
    *cli_args: str,
    lang: str = "zh_CN",
    cwd: Path | None = None,
    python_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """在隔离 daemon（主机库 == db_path）之上运行 python 薄客户端命令。

    daemon 每次调用临时拉起并在 finally 终止，manifest/registry 随测试
    tmp_path 自动回收；调用方不得在子进程运行期间修改种子库。
    python_env 可选：在标准 python 客户端环境之上叠加（如 grep 用例裁剪
    PATH 以强制内置 fallback），PATH 等仅作用于 python 子进程，不影响 daemon。
    """
    data_root = home_dir.parent / "daemon-data"
    proc = _spawn_isolated_daemon(home_dir, data_root)
    try:
        manifest = _wait_daemon_manifest(home_dir, proc)
        if manifest is None:
            raise RuntimeError(
                "隔离 daemon 未发布 manifest（cw-daemon 需先 cargo build）"
            )
        _publish_snapshot(manifest["endpoint"], workspace_root, db_path)
        env = _python_cli_env(home_dir, workspace_root, lang=lang)
        if python_env:
            env.update(python_env)
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "cw.py"), *cli_args],
            cwd=str(cwd or workspace_root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    finally:
        _terminate_daemon(proc)


def _seed_stats_fixture(db: CodeGraphDB) -> int:
    workspace_id = db._get_active_workspace_id()
    now = time.time()
    conn = db.conn
    conn.execute(
        "INSERT INTO file_contents(content_hash, language, total_lines, first_seen_at) "
        "VALUES ('file-a', 'python', 10, ?), ('file-b', 'rust', 20, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO file_instances("
        "workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, "
        "last_parsed, status, module_path"
        ") VALUES (?, 'a.py', ?, 'file-a', ?, 10, ?, 'active', 'a')",
        (workspace_id, str(Path(db.workspace_root) / "a.py"), now, now),
    )
    file_id = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO symbol_contents("
        "content_hash, name, kind, content, signature, has_comment, "
        "comment_content, qualified_name"
        ") VALUES "
        "('sym-a', 'alpha', 'fn', 'def alpha(): pass', 'alpha()', 1, "
        "'alpha docs', 'a.alpha'),"
        "('sym-b', 'Thing', 'struct', 'struct Thing {}', '', 0, '', 'a.Thing')"
    )
    conn.execute(
        "INSERT INTO symbols("
        "file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "has_comment, comment_status, qualified_name, depth"
        ") VALUES "
        "(?, 'sym-a', 'alpha', 'fn', 1, 2, 1, 'done', 'a.alpha', 0),"
        "(?, 'sym-b', 'Thing', 'struct', 4, 5, 0, 'pending', 'a.Thing', -1)",
        (file_id, file_id),
    )
    alpha_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND symbol_hash = 'sym-a'",
        (file_id,),
    ).fetchone()["id"]
    thing_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND symbol_hash = 'sym-b'",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO calls("
        "caller_id, caller_name, caller_module, callee_name, callee_qualified, "
        "callee_id, call_line, is_cross_file"
        ") VALUES (?, 'alpha', 'a', 'Thing', 'a.Thing', ?, 2, 1)",
        (alpha_id, thing_id),
    )
    conn.execute(
        "INSERT INTO file_versions("
        "file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current"
        ") VALUES (?, 1, 'file-a', ?, 10, ?, 0), (?, 2, 'file-a', ?, 10, ?, 1)",
        (file_id, now - 10, now - 10, file_id, now, now),
    )
    current_version = conn.execute(
        "SELECT id FROM file_versions WHERE file_instance_id = ? AND is_current = 1",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO file_symbol_versions("
        "file_version_id, symbol_hash, qualified_name, start_line, end_line, "
        "module_path, depth"
        ") VALUES "
        "(?, 'sym-a', 'a.alpha', 1, 2, 'a', 0), "
        "(?, 'sym-b', 'a.Thing', 4, 5, 'a', -1)",
        (current_version, current_version),
    )
    conn.execute(
        "INSERT INTO call_versions("
        "file_version_id, caller_qualified, caller_hash, callee_name, "
        "callee_module, callee_qualified, callee_file, call_line, is_cross_file"
        ") VALUES "
        "(?, 'a.alpha', 'sym-a', 'Thing', 'a', 'a.Thing', 'a.py', 2, 1), "
        "(?, 'a.Thing', 'sym-b', 'alpha', 'a', 'a.alpha', 'a.py', 5, 0)",
        (current_version, current_version),
    )
    conn.execute(
        "INSERT INTO semgrep_findings("
        "file_instance_id, content_hash, rule_id, rule_name, message, severity, "
        "confidence, start_line, end_line, snippet, fix, symbol_qualified"
        ") VALUES (?, 'file-a', 'python.eval', 'eval use', 'avoid eval', "
        "'ERROR', 'HIGH', 2, 2, 'eval(x)', 'use parser', 'a.alpha')",
        (file_id,),
    )
    conn.execute(
        "INSERT INTO guardrail_rules("
        "rule_id, category, severity, pattern, action, description, is_builtin, created_at"
        ") VALUES ('guard.db', 'db_safety', 'warn', 'execute', 'warn', "
        "'unsafe SQL', 0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO guardrail_findings("
        "workspace_id, rule_id, file_path, symbol_hash, severity, status, message, detected_at"
        ") VALUES (?, 'guard.db', 'a.py', 'sym-a', 'warn', 'open', 'unsafe SQL', ?)",
        (workspace_id, now),
    )
    conn.commit()
    return workspace_id


def _seed_status_files(db: CodeGraphDB, workspace_id: int) -> None:
    """构造 synced/new/stale/deleted 以及三类 ignore 文件。"""
    root = Path(db.workspace_root)
    tracked_mtime = db.conn.execute(
        "SELECT mtime FROM file_instances "
        "WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["mtime"]
    synced = root / "a.py"
    synced.write_text("def alpha():\n    pass\n", encoding="utf-8")
    os.utime(synced, (tracked_mtime, tracked_mtime))

    (root / "new.py").write_text("value = 1\n", encoding="utf-8")
    stale = root / "stale.rs"
    stale.write_text("fn stale() {}\n", encoding="utf-8")
    stale_mtime = stale.stat().st_mtime

    (root / "target").mkdir()
    (root / "target" / "generated.py").write_text("", encoding="utf-8")
    (root / "custom").mkdir()
    (root / "custom" / "ignored.py").write_text("", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "bundle.min.js").write_text("", encoding="utf-8")
    (root / ".callwardenignore").write_text("custom/\n", encoding="utf-8")

    now = time.time()
    db.conn.execute(
        "INSERT INTO file_contents(content_hash, language, total_lines, first_seen_at) "
        "VALUES ('status-stale', 'rust', 1, ?), "
        "('status-deleted', 'go', 1, ?)",
        (now, now),
    )
    db.conn.execute(
        "INSERT INTO file_instances("
        "workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, "
        "last_parsed, status, module_path"
        ") VALUES "
        "(?, 'stale.rs', ?, 'status-stale', ?, 1, ?, 'active', 'stale'), "
        "(?, 'deleted.go', ?, 'status-deleted', ?, 1, ?, 'active', 'deleted')",
        (
            workspace_id,
            str(stale),
            stale_mtime - 100,
            now - 20,
            workspace_id,
            str(root / "deleted.go"),
            now - 100,
            now - 10,
        ),
    )
    db.conn.commit()


def _seed_issues_tests_fixture(db: CodeGraphDB, workspace_id: int) -> None:
    """补充 issues/tests 正向、反向和 history 的真实关系数据。"""
    conn = db.conn
    file_id = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = 'a.py'",
        (workspace_id,),
    ).fetchone()["id"]
    alpha_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND qualified_name = 'a.alpha'",
        (file_id,),
    ).fetchone()["id"]
    thing_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND qualified_name = 'a.Thing'",
        (file_id,),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO symbol_contents("
        "content_hash, name, kind, content, signature, has_comment, "
        "comment_content, qualified_name"
        ") VALUES ('sym-test-alpha', 'test_alpha', 'test_fn', "
        "'def test_alpha(): pass', 'test_alpha()', 0, '', 'a.test_alpha')"
    )
    conn.execute(
        "INSERT INTO symbols("
        "file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "has_comment, comment_status, qualified_name, depth"
        ") VALUES (?, 'sym-test-alpha', 'test_alpha', 'test_fn', 7, 9, "
        "0, 'pending', 'a.test_alpha', 0)",
        (file_id,),
    )
    test_id = conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? "
        "AND qualified_name = 'a.test_alpha'",
        (file_id,),
    ).fetchone()["id"]
    detected_at = 1_735_689_600.0
    conn.executemany(
        "INSERT INTO test_case_relations("
        "workspace_id, test_fn_id, tested_fn_id, match_method, confidence, detected_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                workspace_id,
                test_id,
                alpha_id,
                "direct_call",
                "high",
                detected_at,
            ),
            (
                workspace_id,
                test_id,
                thing_id,
                "name_convention",
                "mid",
                detected_at,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO test_runs("
        "workspace_id, test_fn_id, test_name, test_class, test_file, status, "
        "duration_ms, error_message, error_type, ci_run_id, ci_url, run_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                workspace_id,
                test_id,
                "test_alpha",
                "TestAlpha",
                "a.py",
                "passed",
                10.0,
                "",
                "",
                "ci-1",
                "",
                1_735_689_600.0,
            ),
            (
                workspace_id,
                test_id,
                "test_alpha",
                "TestAlpha",
                "a.py",
                "failed",
                30.0,
                "expected 1 but got 2",
                "AssertionError",
                "ci-2",
                "",
                1_735_776_000.0,
            ),
        ],
    )
    conn.execute(
        "INSERT INTO semgrep_findings("
        "file_instance_id, content_hash, rule_id, rule_name, message, severity, "
        "confidence, start_line, end_line, snippet, fix, symbol_qualified"
        ") VALUES (?, 'file-a', 'python.info', 'style note', 'consider rename', "
        "'INFO', 'MEDIUM', 1, 1, 'alpha()', '', 'a.alpha')",
        (file_id,),
    )
    conn.commit()


def test_stats_binary_matches_python_get_stats(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        expected = json.loads(json.dumps(db.get_stats(), ensure_ascii=False))
    finally:
        db.close()

    completed = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "stats",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


def test_status_binary_matches_python_get_status(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_status_files(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        expected = json.loads(json.dumps(db.get_status(), ensure_ascii=False))
    finally:
        db.close()

    completed = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


@pytest.mark.parametrize("action", ["explain", "paths"])
def test_config_binary_matches_python_output(action: str) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    rust_result = _run_rust_config(binary, action)
    python_result = _run_python_config(action)

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert _normalize_config_source(rust_result.stdout) == _normalize_config_source(
        python_result.stdout
    )


def test_config_check_role_binary_matches_python_output(tmp_path: Path) -> None:
    """[B 类·环境] config check-role：rust local CLI vs python 薄壳。

    stale 依据（生产侧 cli/main.py）：`check-role` 不在
    `_READONLY_CONFIG_ACTIONS`（cli/main.py:108 `{"explain","paths"}`），
    故 `_is_readonly_command`（:1564-1566）判为非只读 → `_run_subcommand_mode`
    （:1466-1478）执行 `db.list_workspaces()` 自动注册工作区 → `route_rpc`
    （:1357）在真实 HOME 命中生产 daemon 残留的 stale manifest（实测
    `E_HTTP_MANIFEST_STALE: manifest PID 43608 已不存活`）而 fail-closed。
    旧的“python 子进程直跑即真相源”语义已随 cli 薄壳化退役；此处改经隔离
    daemon（主机库 == db_path）运行 python 侧，使其注册/派发落在隔离 daemon
    上，再与 rust local CLI 输出逐字对比。
    """
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "config", "check-role", "local"
    )
    rust_result = _run_rust_config(binary, "check-role", "local")

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == python_result.stdout


@pytest.mark.parametrize(
    "search_args",
    [
        ("alpha",),
        ("alpha", "--kind", "fn"),
        ("alpha", "--limit", "1"),
        ("missing",),
    ],
)
def test_search_binary_matches_python_process_output(
    tmp_path: Path, search_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "search", *search_args
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "search",
            *search_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert rust_result.returncode == python_result.returncode == 0
    assert rust_result.stderr == python_result.stderr == ""
    assert rust_result.stdout == _align_status_markers(
        python_result.stdout, rust_result.stdout
    )


@pytest.mark.parametrize("qualified_name", ["a.alpha", "missing"])
def test_symbol_binary_matches_python_process_output(
    tmp_path: Path, qualified_name: str
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "symbol", qualified_name
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "symbol",
            qualified_name,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    ("file_name", "use_absolute"),
    [
        ("a.py", False),
        ("a.py", True),
        ("missing.py", False),
    ],
)
def test_file_binary_matches_python_process_output(
    tmp_path: Path, file_name: str, use_absolute: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    file_arg = str(workspace_root / file_name) if use_absolute else file_name
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "file", file_arg
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "file",
            file_arg,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    ("symbol_name", "use_absolute"),
    [
        ("alpha", False),
        ("alpha", True),
        ("missing", False),
    ],
)
def test_query_binary_matches_python_process_output(
    tmp_path: Path, symbol_name: str, use_absolute: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    file_arg = str(workspace_root / "a.py") if use_absolute else "a.py"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "query", symbol_name, file_arg
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "query",
            symbol_name,
            file_arg,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    ("grep_args", "force_fallback"),
    [
        # 薄壳化能力边界（台账 s1_rustdiff-02）：python 侧 grep 命中后经
        # find_symbols_at_lines RPC 做符号归属，daemon 无该方法
        # （method_not_found fail-closed），python 无法产出可比较输出；
        # rust local CLI 为存活实现。fixture 有命中行的用例均记 xfail(strict)，
        # 待 daemon 补齐归属查询或 grep 迁移 rust_native 后自动 XPASS 提示复核。
        pytest.param(
            ("needle", "--fixed"), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        pytest.param(
            ("needle.*time",), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        pytest.param(
            ("needle", "time", "--fixed"), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        pytest.param(
            ("needle", "--fixed", "--include-all"), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        pytest.param(
            ("needle", "--fixed", "--kind", "fn"), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        pytest.param(
            ("needle", "--fixed", "--limit", "1"), False,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
        (("missing", "--fixed"), False),
        pytest.param(
            ("needle", "time", "--fixed"), True,
            marks=pytest.mark.xfail(strict=True, reason="python grep 经 daemon 缺 find_symbols_at_lines RPC（s1_rustdiff-02）"),
        ),
    ],
)
def test_grep_binary_matches_python_process_output(
    tmp_path: Path, grep_args: tuple[str, ...], force_fallback: bool
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "a.py").write_text(
        "def alpha():\n"
        '    needle = "time"\n'
        'needle = "top time"\n'
        "class Thing:\n"
        '    needle = "time"\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    if force_fallback:
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        # Windows 当前 Rust CLI 与 PyO3 同 crate，进程启动仍需找到 python DLL。
        # 保留 Python 目录但排除 rg，仍能真实覆盖内置 fallback。
        env["PATH"] = (
            os.pathsep.join((str(Path(sys.executable).parent), str(empty_path)))
            if os.name == "nt"
            else str(empty_path)
        )

    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。PATH 裁剪仅作用于 python 子进程（rg 不可用 → 内置
    # fallback），不影响 daemon 启动环境。env 保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home,
        workspace_root,
        db_path,
        "grep",
        *grep_args,
        python_env={"PATH": env["PATH"]},
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "grep",
            *grep_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    "issues_args",
    [
        ("a.alpha",),
        ("a.alpha", "--include-info"),
        ("a.missing",),
    ],
)
def test_issues_binary_matches_python_process_output(
    tmp_path: Path, issues_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_issues_tests_fixture(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "issues", *issues_args
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "issues",
            *issues_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    "tests_args",
    [
        ("a.alpha",),
        ("a.test_alpha", "--reverse"),
        ("a.alpha", "--history"),
        ("a.missing",),
        ("a.missing", "--reverse"),
        ("a.missing", "--history"),
        (),
    ],
)
def test_tests_binary_matches_python_read_process_output(
    tmp_path: Path, tests_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        _seed_issues_tests_fixture(db, workspace_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, "tests", *tests_args
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            "tests",
            *tests_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


@pytest.mark.parametrize(
    ("command", "query_args"),
    [
        # 薄壳化后 graph 只读组逐行现状（全部经 2026-09-08 实测差分归类）：
        # - 等值断言：短名命中（callers Thing / callees alpha、missing 空命中、
        #   --qualified 显式消歧、topo --limit 0）。
        # - callees alpha 文件解析标记差（s1_rustdiff-05）：python 快照解析出
        #   (a.py)，rust local SQL 未回填 → [未解析]，由 _align_resolution_markers
        #   按 rust 权威原位对齐。
        # - 全限定位置名自动解析差（s1_rustdiff-06）：daemon 快照仅短名命中
        #   （callers a.Thing / callees a.alpha 返回 0），rust local 自动派生
        #   qualified 命中 → 语义差，xfail(strict) 记录。
        # - call-chain / topo 响应形状契约差（s1_rustdiff-03）：daemon 快照返回
        #   字符串列表，python 薄客户端 handler 期待 dict 行，格式化报错 rc=0。
        # - impact（s1_rustdiff-04，2026-09-13 复核 XPASS）：早期 python 侧经
        #   daemon python_compat blast_radius 时 worker 无客户端 workspace 上下文
        #   → 无 active workspace fail-closed，曾登记 xfail(strict)。迁移到隔离
        #   daemon（_run_python_cli_via_daemon 已 register+publish snapshot）后，
        #   impact 恢复 active workspace 上下文并与 rust local 输出逐行一致，故
        #   移除失效的 xfail 标记，改为正常等值断言。
        ("callers", ("Thing",)),
        pytest.param(
            "callers", ("a.Thing",),
            marks=pytest.mark.xfail(strict=True, reason="callers 全限定位置名：daemon 快照仅短名命中返回 0，rust local 自动派生 qualified 命中（s1_rustdiff-06）"),
        ),
        ("callers", ("Thing", "--qualified", "a.Thing")),
        ("callers", ("missing",)),
        ("callees", ("alpha",)),
        pytest.param(
            "callees", ("a.alpha",),
            marks=pytest.mark.xfail(strict=True, reason="callees 全限定位置名：daemon 快照仅短名命中返回 0，rust local 自动派生 qualified 命中（s1_rustdiff-06）"),
        ),
        ("callees", ("alpha", "--qualified", "a.alpha")),
        ("callees", ("missing",)),
        pytest.param(
            "call-chain", ("a.alpha",),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon call-chain 响应形状失败：期望 dict 行实得字符串列表（list indices must be integers or slices, not str）（s1_rustdiff-03）"),
        ),
        pytest.param(
            "call-chain", ("a.alpha", "--depth", "1"),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon call-chain 响应形状失败：期望 dict 行实得字符串列表（list indices must be integers or slices, not str）（s1_rustdiff-03）"),
        ),
        pytest.param(
            "call-chain", ("a.alpha", "--depth", "0"),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon call-chain 响应形状失败：期望 dict 行实得字符串列表（list indices must be integers or slices, not str）（s1_rustdiff-03）"),
        ),
        pytest.param(
            "call-chain", ("missing",),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon call-chain 响应形状失败：期望 dict 行实得字符串列表（list indices must be integers or slices, not str）（s1_rustdiff-03）"),
        ),
        pytest.param(
            "topo", (),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon topo 响应形状失败：期望 dict 行实得字符串（'str' object has no attribute 'get'）（s1_rustdiff-03）"),
        ),
        pytest.param(
            "topo", ("--limit", "1"),
            marks=pytest.mark.xfail(strict=True, reason="python 解析 daemon topo 响应形状失败：期望 dict 行实得字符串（'str' object has no attribute 'get'）（s1_rustdiff-03）"),
        ),
        ("topo", ("--limit", "0")),
        ("impact", ("sym-b",)),
        ("impact", ("sym-b", "--depth", "0")),
        ("impact", ("sym-b", "--depth", "-1")),
        ("impact", ("missing",)),
    ],
)
def test_graph_query_binary_matches_python_process_output(
    tmp_path: Path, command: str, query_args: tuple[str, ...]
) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    home = tmp_path / "home"
    db_dir = home / ".callwarden"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        # 历史：python 直读本地库时代，GraphStore 短路（Kahn 序、不过滤 kind）
        # 与 rust local SQL 参考路径（kind='fn'、depth 升序）输出不一致，曾用
        # rollback_config 固定 rust_graph_query=1 使两侧逐字符对齐。python CLI
        # 薄壳化后 graph 查询经隔离 daemon 快照图，该配置不再约束 python 侧；
        # 该行沿用旧例保留，以稳定 rust local 参考输出。rollback_config.task_id
        # 无对应 tasks 行，需临时关闭外键检查后插入。逐行差异分类见参数表台账。
        db.conn.execute("PRAGMA foreign_keys=OFF")
        now = time.time()
        db.conn.execute(
            "INSERT INTO rollback_config(workspace_id, task_id, feature_name, phase, "
            "production_entry, rollback_entry, rollback_flag, rollback_window_until, "
            "config_blob, created_at, updated_at) "
            "VALUES (?, 'fixture-rust-graph-rollback', 'rust_graph_query', 0, "
            " '', '', 1, '', '{}', ?, ?)",
            (workspace_id, now, now),
        )
        db.conn.execute("PRAGMA foreign_keys=ON")
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(workspace_root),
            "CALLWARDEN_LANG": "zh_CN",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    # Python 薄客户端经隔离 daemon 读同一主机库（旧直读本地库语义已随
    # cli 薄壳化退役）。env 仅保留给 rust local CLI。
    python_result = _run_python_cli_via_daemon(
        home, workspace_root, db_path, command, *query_args
    )
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "--workspace-id",
            str(workspace_id),
            command,
            *query_args,
        ],
        cwd=workspace_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    _assert_python_rust_read_outputs(python_result, rust_result)


def test_refresh_rust_authoritative_and_python_boundary(tmp_path: Path) -> None:
    """refresh <path>：rust CLI 为权威写入方；python 薄客户端无 daemon 时
    fail-closed 且宿主库零写入（旧 python 本地直写"真相源"语义已随薄壳化
    退役，python 不再本地持久化代码图谱）。"""
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    roots: dict[str, tuple[Path, Path, Path]] = {}
    workspace_ids: dict[str, int] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        workspace = tmp_path / implementation / "workspace"
        (home / ".callwarden").mkdir(parents=True)
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "lib.rs").write_text(
            "pub fn alpha() { beta(); }\nfn beta() {}\n",
            encoding="utf-8",
        )
        db_path = home / ".callwarden" / "callwarden.db"
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace))
        try:
            db.conn.commit()
            workspace_ids[implementation] = db._get_active_workspace_id()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        roots[implementation] = (home, workspace, db_path)

    python_home, python_workspace, python_db = roots["python"]
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "refresh", "src/lib.rs"],
        cwd=python_workspace,
        env=_python_cli_env(python_home, python_workspace, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    with sqlite3.connect(python_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM file_instances"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM symbols"
        ).fetchone()[0] == 0

    rust_home, rust_workspace, rust_db = roots["rust"]
    rust_result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(rust_db),
            "--workspace-id",
            str(workspace_ids["rust"]),
            "refresh",
            "src/lib.rs",
        ],
        cwd=rust_workspace,
        env=_python_cli_env(rust_home, rust_workspace, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert rust_result.returncode == 0, rust_result.stderr
    assert "Refreshed: src/lib.rs" in rust_result.stdout

    # rust 权威库：文件闭合为 parsed + 符号/call/版本完整（探针 2026-09-08）。
    with sqlite3.connect(rust_db) as conn:
        rows = conn.execute(
            "SELECT rel_path, status FROM file_instances ORDER BY rel_path"
        ).fetchall()
        assert rows == [("src/lib.rs", "parsed")]
        symbol_names = {
            row[0]
            for row in conn.execute("SELECT name FROM symbols ORDER BY name").fetchall()
        }
        assert symbol_names == {"alpha", "beta"}
        assert conn.execute(
            "SELECT caller_name, callee_name, call_line, is_cross_file FROM calls"
        ).fetchall() == [("alpha", "beta", 1, 0)]
        assert conn.execute(
            "SELECT COUNT(*) FROM file_versions WHERE is_current = 1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM file_versions WHERE is_deleted = 1"
        ).fetchone()[0] == 0


def test_refresh_all_rust_authoritative_incremental_and_python_boundary(
    tmp_path: Path,
) -> None:
    """refresh --all：增量契约由 rust CLI 权威断言；python 薄客户端无 daemon
    时 fail-closed 且宿主库零写入。"""
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    roots: dict[str, tuple[Path, Path, Path]] = {}
    workspace_ids: dict[str, int] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        workspace = tmp_path / implementation / "workspace"
        (home / ".callwarden").mkdir(parents=True)
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "first.rs").write_text(
            "pub fn first() { shared(); }\nfn shared() {}\n",
            encoding="utf-8",
        )
        (workspace / "src" / "second.rs").write_text(
            "pub fn second() {}\n",
            encoding="utf-8",
        )
        db_path = home / ".callwarden" / "callwarden.db"
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace))
        try:
            db.conn.commit()
            workspace_ids[implementation] = db._get_active_workspace_id()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        roots[implementation] = (home, workspace, db_path)

    python_home, python_workspace, python_db = roots["python"]
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "refresh", "--all"],
        cwd=python_workspace,
        env=_python_cli_env(python_home, python_workspace, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    with sqlite3.connect(python_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM file_instances"
        ).fetchone()[0] == 0

    rust_home, rust_workspace, rust_db = roots["rust"]
    rust_env = _python_cli_env(rust_home, rust_workspace, lang="en_US")

    def run_rust_full(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(rust_db),
                "--workspace-id",
                str(workspace_ids["rust"]),
                "refresh",
                "--all",
                *extra,
            ],
            cwd=rust_workspace,
            env=rust_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    first = run_rust_full()
    assert first.returncode == 0, first.stderr
    assert "refreshed 2 / unchanged 0 / deleted 0 / failed 0" in first.stdout

    with sqlite3.connect(rust_db) as conn:
        version_count = conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
    unchanged = run_rust_full()
    assert unchanged.returncode == 0, unchanged.stderr
    assert "refreshed 0 / unchanged 2 / deleted 0 / failed 0" in unchanged.stdout
    with sqlite3.connect(rust_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0]
            == version_count
        )

    (rust_workspace / "src" / "first.rs").write_text(
        "pub fn first_changed() {}\n",
        encoding="utf-8",
    )
    (rust_workspace / "src" / "second.rs").unlink()
    changed = run_rust_full()
    assert changed.returncode == 0, changed.stderr
    assert "refreshed 1 / unchanged 0 / deleted 1 / failed 0" in changed.stdout
    with sqlite3.connect(rust_db) as conn:
        rows = conn.execute(
            "SELECT rel_path, status FROM file_instances ORDER BY rel_path"
        ).fetchall()
        assert rows == [("src/first.rs", "parsed"), ("src/second.rs", "deleted")]
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM symbols ORDER BY name").fetchall()
        }
        assert "first_changed" in names
        assert "second" not in names

    forced = run_rust_full("--force")
    assert forced.returncode == 0, forced.stderr
    assert "refreshed 1 / unchanged 0 / deleted 0 / failed 0" in forced.stdout


def test_workspace_lifecycle_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """workspace lifecycle：rust CLI 为权威写入方；python 薄客户端无 daemon 时
    fail-closed 且宿主库零写入（旧 python 本地直写"真相源"语义已随薄壳化退役，
    python 不再本地持久化工作区注册）。"""
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    registered_root = tmp_path / "registered"
    workspace_root.mkdir()
    registered_root.mkdir()
    db_paths: dict[str, Path] = {}
    workspace_ids: dict[str, int] = {}

    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            db.conn.commit()
            workspace_ids[implementation] = db._get_active_workspace_id()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        db_paths[implementation] = db_path

    def workspace_rows(db_path: Path) -> list[tuple]:
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT name, root_path, is_active, description "
                "FROM workspaces ORDER BY id"
            ).fetchall()

    # python 侧：仅跑一次代表命令（workspace register），须 fail-closed 且
    # 宿主库保持 seed 基线（不新增 secondary 行）。
    baseline_rows = workspace_rows(db_paths["python"])
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "cw.py"),
            "workspace",
            "register",
            "secondary",
            str(registered_root),
        ],
        cwd=workspace_root,
        env=_python_cli_env(python_home, workspace_root, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    assert workspace_rows(db_paths["python"]) == baseline_rows

    # rust 侧：权威执行整个生命周期并断言 DB 终态回到 seed 基线
    # （secondary 注册→切换→删除全部由 rust CLI 完成）。
    rust_home = tmp_path / "rust" / "home"

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(db_paths["rust"]),
                "workspace",
                *args,
            ],
            cwd=workspace_root,
            env=_python_cli_env(rust_home, workspace_root, lang="en_US"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    steps = [
        (
            ("register", "secondary", str(registered_root)),
            "Workspace registered: ID=2, name=secondary, root=",
        ),
        (("list",), "Workspaces (2 total):"),
        (("set", "secondary"), "Switched to active workspace: secondary ("),
        (("list",), "[2] secondary [active]"),
        (("set", "workspace"), "Switched to active workspace: workspace ("),
        (("delete", "secondary"), "Workspace 'secondary' deleted"),
        (("list",), "Workspaces (1 total):"),
    ]
    for args, expected in steps:
        rust_result = run_rust(*args)
        assert rust_result.returncode == 0, rust_result.stderr
        assert rust_result.stderr == ""
        assert expected in rust_result.stdout, f"stdout={rust_result.stdout!r}"

    rust_rows = workspace_rows(db_paths["rust"])
    assert rust_rows == baseline_rows
    assert rust_rows == [("workspace", baseline_rows[0][1], 1, "")]


def test_workspace_status_is_read_only_and_uses_active_workspace(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = db._get_active_workspace_id()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "workspace",
            "status",
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["id"] == workspace_id
    assert payload["is_active"] is True

    with sqlite3.connect(db_path) as conn:
        assert conn.total_changes == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE is_active = 1"
        ).fetchone()[0] == 1


def test_workspace_remove_cleans_full_codegraph_schema(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = tmp_path / "callwarden.db"
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = _seed_stats_fixture(db)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    result = subprocess.run(
        [
            str(binary),
            "--mode",
            "local",
            "--db",
            str(db_path),
            "workspace",
            "remove",
            str(workspace_id),
        ],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0] == 0


def test_toolchain_and_build_context_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """toolchain + build-context 全生命周期：rust CLI 为权威写入方；python
    薄客户端无 daemon 时 fail-closed 且宿主库 toolchain 相关表零写入。"""
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    include_path = workspace_root / "include"
    compiler_path = workspace_root / "toolchain" / "bin" / "gcc.exe"
    include_path.mkdir(parents=True)
    compiler_path.parent.mkdir(parents=True)
    compiler_path.write_text("", encoding="utf-8")

    db_paths: dict[str, Path] = {}
    workspace_ids: dict[str, int] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            workspace_ids[implementation] = _seed_stats_fixture(db)
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        db_paths[implementation] = db_path

    assert workspace_ids["python"] == workspace_ids["rust"]
    workspace_id = workspace_ids["rust"]

    # python 侧：只跑一次代表写命令（toolchain register），须 fail-closed，
    # 宿主库 toolchains / build_contexts 均保持 seed 基线（0 行）。
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "cw.py"),
            "toolchain",
            "register",
            "fixture-gcc",
            str(compiler_path),
            "--no-probe",
        ],
        cwd=workspace_root,
        env=_python_cli_env(python_home, workspace_root, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    with sqlite3.connect(db_paths["python"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toolchains").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM workspace_build_contexts")
            .fetchone()[0]
            == 0
        )

    # rust 侧：权威执行 register/show/bind/resolve/import/delete 全序列。
    rust_home = tmp_path / "rust" / "home"
    rust_env = _python_cli_env(rust_home, workspace_root, lang="en_US")

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "local",
                "--db",
                str(db_paths["rust"]),
                *args,
            ],
            cwd=workspace_root,
            env=rust_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_ok(*args: str, contains: str) -> None:
        result = run_rust(*args)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert contains in result.stdout, f"stdout={result.stdout!r}"

    assert_ok(
        "toolchain",
        "register",
        "fixture-gcc",
        str(compiler_path),
        "--no-probe",
        contains="Toolchain registered: Toolchain(id=1, name=fixture-gcc",
    )
    assert_ok("toolchain", "list", contains="fixture-gcc")
    assert_ok("toolchain", "show", "fixture-gcc", contains="Toolchain: fixture-gcc")

    assert_ok(
        "build-context",
        "register",
        str(workspace_id),
        "debug",
        "--flags=-O2",
        "--defines",
        "DEBUG=1",
        "--includes",
        str(include_path),
        "--activate",
        contains="Build context registered: debug",
    )
    with sqlite3.connect(db_paths["rust"]) as conn:
        debug_hash = conn.execute(
            "SELECT build_context_hash FROM workspace_build_contexts "
            "WHERE workspace_id = ? AND name = 'debug'",
            (workspace_id,),
        ).fetchone()[0]
    assert_ok("build-context", "list", str(workspace_id), contains="debug")
    assert_ok(
        "build-context",
        "show",
        str(workspace_id),
        debug_hash[:16],
        contains="Build Context: debug",
    )
    assert_ok(
        "toolchain",
        "bind",
        str(workspace_id),
        "fixture-gcc",
        "--build-context-hash",
        debug_hash,
        contains="bound to workspace",
    )
    assert_ok(
        "toolchain",
        "list-bound",
        str(workspace_id),
        "--build-context-hash",
        debug_hash,
        contains="fixture-gcc",
    )
    assert_ok(
        "build-context",
        "resolve",
        str(workspace_id),
        debug_hash,
        contains="Resolved edges computed for: debug",
    )
    edges_result = run_rust("build-context", "edges", str(workspace_id), debug_hash)
    assert edges_result.returncode == 0, edges_result.stderr
    assert edges_result.stderr == ""
    assert "Resolved edges" in edges_result.stdout or (
        "No resolved edges found" in edges_result.stdout
    ), f"stdout={edges_result.stdout!r}"

    compile_commands_path = workspace_root / "compile_commands.json"
    compile_commands_path.write_text(
        json.dumps(
            [
                {
                    "directory": str(workspace_root),
                    "file": "a.c",
                    "arguments": ["-DIMPORT=1", "-I", "include", "-O2", "a.c"],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert_ok(
        "build-context",
        "import-compile-commands",
        str(compile_commands_path),
        str(workspace_id),
        "--name",
        "imported",
        "--workspace-root",
        str(workspace_root),
        contains="Imported 1 compile entries",
    )
    assert_ok("build-context", "list", str(workspace_id), contains="imported")

    assert_ok(
        "build-context",
        "delete",
        str(workspace_id),
        debug_hash,
        contains="Deleted: debug (",
    )
    assert_ok(
        "toolchain", "delete", "fixture-gcc", contains="Toolchain deleted: fixture-gcc"
    )

    # rust 权威终态：toolchain 已删、debug context 已删、imported 保留；
    # python 宿主库零写入（上面对 toolchains / build_contexts 已断言 0 行）。
    with sqlite3.connect(db_paths["rust"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM toolchains").fetchone()[0] == 0
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM workspace_build_contexts ORDER BY name"
            ).fetchall()
        ]
    assert names == ["imported"]


def _seed_task_read_fixture(db: CodeGraphDB) -> None:
    """构造 task 只读命令的树、阻塞 finding 与三角关联。"""
    workspace_id = db._get_active_workspace_id()
    conn = db.conn
    conn.executemany(
        "INSERT INTO tasks("
        "id, title, description, creator, status, created_at, updated_at, "
        "parent_id, depth, sort_order"
        ") VALUES (?, ?, ?, 'agent', ?, ?, ?, ?, ?, ?)",
        [
            ("task-root", "Root task", "root desc", "in_progress", 1735689600.0,
             1735689600.0, "", 0, 0),
            ("task-child-a", "Child A", "", "review", 1735689601.0,
             1735689601.0, "task-root", 1, 0),
            ("task-child-b", "Child B", "", "open", 1735689602.0,
             1735689602.0, "task-root", 1, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO task_steps("
        "id, task_id, step_index, action, target_file, target_symbol, "
        "check_items, status, result, created_at, completed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, '[]', ?, '', ?, ?)",
        [
            ("step-root", "task-root", 0, "inspect", "", "pkg.root",
             "done", 1735689600.0, 1735689610.0),
            ("step-child-a", "task-child-a", 0, "fix", "src/a.py", "",
             "pending", 1735689601.0, None),
            ("step-child-b", "task-child-b", 0, "verify", "", "",
             "skipped", 1735689602.0, 1735689612.0),
        ],
    )
    conn.executemany(
        "INSERT INTO task_quality_findings("
        "workspace_id, task_id, step_id, finding_type, severity, status, "
        "message, evidence, source, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
        [
            (workspace_id, "task-child-a", "step-child-a", "scope", "block",
             "open", "outside task scope", "scope", 1735689620.0),
            (workspace_id, "task-root", "", "style", "warn",
             "resolved", "style note", "manual", 1735689621.0),
        ],
    )
    conn.execute(
        "INSERT INTO git_commits("
        "commit_hash, message, author, email, timestamp, workspace_id"
        ") VALUES ('0123456789abcdef', 'Task commit\\nbody', 'Reviewer', "
        "'reviewer@example.com', ?, ?)",
        (1735689630.0, workspace_id),
    )
    conn.execute(
        "INSERT INTO task_symbol_changes("
        "workspace_id, task_id, step_id, file_path, qualified_name, symbol_name, "
        "change_type, source_commit_hash, created_at"
        ") VALUES (?, 'task-root', 'step-root', 'src/a.py', 'pkg.alpha', "
        "'alpha', 'modified', '0123456789abcdef', ?)",
        (workspace_id, 1735689640.0),
    )
    conn.commit()


def test_task_read_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """task 只读族：rust CLI（local 模式直读权威库）为输出权威；python 薄客户端
    无 daemon 时 fail-closed 且宿主库零写入。

    探针（2026-09-08，_s1d_probe.py）：
    - rust local 下 task list/show/status-tree/findings 全部 rc=0、stderr 为空，
      输出为 JSON 信封（{"output": ...}），读全程 DB 字节不变；
    - rust enterprise 无 daemon 权威 binding 时 task show fail-closed（rc!=0），
      DB 同样不变——"daemon 路由"缺权威即拒绝，本地权威库不被回写。
    """
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_paths: dict[str, Path] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
        try:
            _seed_task_read_fixture(db)
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            db.close()
        db_paths[implementation] = db_path

    # python 边界：单次代表命令必须 fail-closed，宿主库保持 3 任务 seed 基线。
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "task", "list", "--limit", "20"],
        cwd=workspace_root,
        env=_python_cli_env(python_home, workspace_root, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    with sqlite3.connect(db_paths["python"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3

    rust_home = tmp_path / "rust" / "home"

    def run_rust(*args: str, mode: str = "local") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--mode", mode, "--db", str(db_paths["rust"]), *args],
            cwd=workspace_root,
            env=_python_cli_env(rust_home, workspace_root, lang="en_US"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    # rust 权威：全部只读命令 rc=0、stderr 为空，JSON 信封解包后含契约片段。
    read_contracts = [
        (
            ("task", "list", "--limit", "20"),
            ["Total tasks: 3", "task-root [in_progress] Root task",
             "task-child-a [review] Child A"],
        ),
        (
            ("task", "list", "--blocked", "--limit", "20"),
            ["only showing tasks with blocking findings",
             "task-child-a [review] Child A"],
        ),
        (
            ("task", "list", "--status", "review", "--flat"),
            ["status filter: review", "task-child-a [review] Child A"],
        ),
        (
            ("task", "show", "task-root"),
            ["Progress: 2/3 (66.67%)", "pkg.alpha modified", "Task commit"],
        ),
        (
            ("task", "show", "task-root", "--flat"),
            ["ID: task-root", "#0 [done] inspect", "pkg.alpha modified"],
        ),
        (
            ("task", "status-tree", "task-root"),
            ["Subtasks (2):", "task-child-a [review] Child A"],
        ),
        (
            ("task", "findings", "task-child-a"),
            ["outside task scope", "scope (open)"],
        ),
        (
            ("task", "findings", "task-root", "--status", "all", "--severity", "warn"),
            ["[warn] style (resolved)", "style note"],
        ),
    ]
    before = db_paths["rust"].read_bytes()
    for args, fragments in read_contracts:
        result = run_rust(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert result.stderr == ""
        body = _task_json_output(result)
        for fragment in fragments:
            assert fragment in body, (args, fragment, body)
    assert db_paths["rust"].read_bytes() == before

    # enterprise（无 daemon 权威 binding）：只读命令路由到 daemon 权威即
    # fail-closed，本地权威库不因该路由尝试而被写入。
    enterprise_result = run_rust("task", "show", "task-root", mode="enterprise")
    assert enterprise_result.returncode != 0
    assert db_paths["rust"].read_bytes() == before


def _task_write_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "tasks": conn.execute(
                "SELECT t.title, t.description, t.creator, t.status, "
                "COALESCE(p.title, ''), t.depth, t.sort_order, "
                "t.applied_at IS NULL, t.closed_at IS NULL "
                "FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id "
                "ORDER BY t.depth, t.sort_order, t.title"
            ).fetchall(),
            "steps": conn.execute(
                "SELECT t.title, s.step_index, "
                "CASE WHEN s.action IN ('fix_defect', 'fix_gate_failure') "
                "THEN 'fix_<gate>' ELSE s.action END, s.target_file, "
                "s.target_symbol, s.check_items, s.status, s.result, "
                "s.completed_at IS NOT NULL "
                "FROM task_steps s JOIN tasks t ON t.id = s.task_id "
                "ORDER BY t.title, s.step_index"
            ).fetchall(),
            "active": conn.execute(
                "SELECT COALESCE(t.title, '') "
                "FROM workspaces w LEFT JOIN tasks t ON t.id = w.active_task_id "
                "WHERE w.is_active = 1"
            ).fetchall(),
        }
    finally:
        conn.close()


def _seed_task_write_state(db_path: Path, workspace_root: Path) -> None:
    """构造 write 状态机种子：in_progress 任务 + inspect/verify 两步。

    rust CLI 在 local 模式下 task create fail-closed（BR-03：需要 daemon 权威
    workspace binding），故权威写入方测试用与状态机同构的内联种子起步，
    CLI 序列本身（next/report）在 local 模式即可写（探针 _s1d_probe2/_s1d_probe3）。
    """
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        db.conn.execute(
            "INSERT INTO tasks(id,title,description,creator,status,created_at,updated_at,"
            "depth,sort_order) VALUES('write-task','Write task','state machine','agent',"
            "'in_progress',1,1,0,0)"
        )
        db.conn.execute(
            "INSERT INTO task_steps(id,task_id,step_index,action,target_file,target_symbol,"
            "check_items,status,result,created_at,completed_at) VALUES"
            "('write-s0','write-task',0,'inspect','','','[\"read\",\"syntax\"]',"
            "'pending','',1,NULL),"
            "('write-s1','write-task',1,'verify','src/lib.rs','','[]','pending','',1,NULL)"
        )
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def test_task_write_state_machine_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """task write 状态机：rust CLI（local）为权威写入方；python 薄客户端无 daemon
    时 fail-closed 且宿主库零写入。

    探针（2026-09-08，_s1d_probe3.py）终态快照（确定性）：
    - tasks: write-task 保持 in_progress（applied/closed 均为 NULL）；
    - steps: #0 inspect done/success、#1 verify failed/broken（report 后自动追加
      fix_<gate> 步骤 pending，即"门禁失败自动插入修复步骤"语义）；
    - workspaces.active_task_id = write-task；
    - task create 在 local 恒 fail-closed（BR-03）；无效 step report rc!=0 且 DB 不变。
    """
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_paths: dict[str, Path] = {}
    for implementation in ("python", "rust"):
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        _seed_task_write_state(db_path, workspace_root)
        db_paths[implementation] = db_path

    # python 边界：单次代表写命令必须 fail-closed，宿主库保持 seed 基线。
    baseline = _task_write_snapshot(db_paths["python"])
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "cw.py"),
            "task", "report", "write-task", "write-s0", "--result", "success",
        ],
        cwd=workspace_root,
        env=_python_cli_env(python_home, workspace_root, lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    assert _task_write_snapshot(db_paths["python"]) == baseline

    rust_home = tmp_path / "rust" / "home"

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--mode", "local", "--db", str(db_paths["rust"]), *args],
            cwd=workspace_root,
            env=_python_cli_env(rust_home, workspace_root, lang="en_US"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_ok(*args: str, contains: str) -> None:
        result = run_rust(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert result.stderr == ""
        body = _task_json_output(result)
        assert contains in body, (args, contains, body)

    # rust 权威状态机：claim inspect → 报告 success → claim verify →
    # 报告 broken --fail → 自动追加修复步骤，进度 1/3。
    assert_ok("task", "list", "--limit", "20", contains="Write task")
    assert_ok("task", "next", "write-task", contains="Step ID: write-s0")
    assert_ok(
        "task", "report", "write-task", "write-s0", "--result", "success",
        contains="Result: success",
    )
    assert_ok("task", "next", "write-task", contains="Step ID: write-s1")
    assert_ok(
        "task", "report", "write-task", "write-s1", "--result", "broken", "--fail",
        contains="Result: failure",
    )
    assert_ok(
        "task", "show", "write-task",
        contains="Progress: 1/3 (33.33%)",
    )

    assert _task_write_snapshot(db_paths["rust"]) == {
        "tasks": [("Write task", "state machine", "agent", "in_progress", "", 0, 0, 1, 1)],
        "steps": [
            ("Write task", 0, "inspect", "", "", '["read","syntax"]', "done", "success", 1),
            ("Write task", 1, "verify", "src/lib.rs", "", "[]", "failed", "broken", 1),
            ("Write task", 2, "fix_<gate>", "", "", "", "pending", "", 0),
        ],
        "active": [("Write task",)],
    }

    # create 需 daemon 权威 workspace binding，local 恒 fail-closed 且 DB 不变。
    before = db_paths["rust"].read_bytes()
    denied = run_rust("task", "create", "--title", "X", "--steps", '[{"action": "inspect"}]')
    assert denied.returncode != 0
    assert "task.create 需要 daemon 权威 workspace binding" in denied.stderr
    assert db_paths["rust"].read_bytes() == before

    # 无效 step report：fail-closed 且 DB 不变。
    invalid = run_rust("task", "report", "write-task", "S-missing", "--result", "bad")
    assert invalid.returncode != 0
    assert "task step not found" in invalid.stderr
    assert db_paths["rust"].read_bytes() == before


def _task_audit_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "tasks": conn.execute(
                "SELECT t.title, t.status, COALESCE(p.title, ''), t.depth, t.sort_order "
                "FROM tasks t LEFT JOIN tasks p ON p.id = t.parent_id "
                "ORDER BY t.depth, t.sort_order, t.title"
            ).fetchall(),
            "steps": conn.execute(
                "SELECT t.title, s.step_index, s.action, s.target_file, s.status "
                "FROM task_steps s JOIN tasks t ON t.id = s.task_id "
                "ORDER BY t.title, s.step_index"
            ).fetchall(),
            "findings": conn.execute(
                "SELECT t.title, q.status, q.resolved_by "
                "FROM task_quality_findings q JOIN tasks t ON t.id = q.task_id "
                "ORDER BY q.id"
            ).fetchall(),
            "changes": conn.execute(
                "SELECT t.title, c.file_path, c.hash_before, c.hash_after, c.author "
                "FROM change_audit c JOIN tasks t ON t.id = c.task_id "
                "ORDER BY c.timestamp, c.file_path"
            ).fetchall(),
        }
    finally:
        conn.close()


def _seed_task_audit_db(db_path: Path, workspace_root: Path) -> None:
    """构造 task audit 全场景 DB 种子（5 任务 + 门禁/回滚/捕获关联）。

    与历史内联 seed 完全同构（探针 _s1d_probe2 逐命令核验）；只负责 DB 行，
    git 仓库/plan.md 由权威执行方（rust 侧）按需准备——capture-diff 读
    worktree 变更、split 读 plan 文件。
    """
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(workspace_root))
    try:
        workspace_id = db._get_active_workspace_id()
        db.conn.executemany(
            "INSERT INTO tasks(id,title,description,creator,status,created_at,updated_at,"
            "applied_at,closed_at,parent_id,depth,sort_order) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("review-task", "Review task", "", "builder", "review", 1, 1, None, None, "", 0, 0),
                ("finding-task", "Finding task", "", "builder", "in_progress", 2, 2, None, None, "", 0, 1),
                ("split-parent", "Split parent", "", "builder", "open", 3, 3, None, None, "", 0, 2),
                ("rollback-task", "Rollback task", "", "builder", "in_progress", 4, 4, None, None, "", 0, 3),
                ("capture-task", "Capture task", "", "builder", "in_progress", 5, 5, None, None, "", 0, 4),
            ],
        )
        db.conn.execute(
            "INSERT INTO task_steps(id,task_id,step_index,action,target_file,target_symbol,"
            "check_items,status,result,created_at,completed_at) "
            "VALUES('capture-step','capture-task',0,'edit','tracked.py','','','in_progress','',5,NULL)"
        )
        db.conn.execute(
            "INSERT INTO task_quality_findings(workspace_id,task_id,step_id,finding_type,"
            "severity,status,message,evidence,source,created_at,resolved_at,resolved_by) "
            "VALUES(?, 'finding-task', '', 'scope', 'warn', 'open', 'review warning', '', 'manual', 6, NULL, '')",
            (workspace_id,),
        )
        db.conn.execute(
            "INSERT INTO change_audit(id,task_id,step_id,file_path,hash_before,hash_after,diff,author,timestamp) "
            "VALUES('change-1','rollback-task','rollback-step','old.py','before','after','','agent',7)"
        )
        db.conn.execute(
            "UPDATE workspaces SET active_task_id = 'capture-task' WHERE id = ?",
            (workspace_id,),
        )
        db.conn.commit()
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def test_task_audit_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """task audit：rust CLI（local）为权威写入方；python 薄客户端无 daemon 时
    fail-closed 且宿主库零写入。

    探针（2026-09-08，_s1d_probe2.py）逐命令核验的 rust 权威序列：
    completion-review（warn 判定）→ resolve-finding → apply → close →
    rollback（只记回滚意图，DB 落 reversed 行）→ split（按 plan.md 拆 2 子任务）
    → capture-diff --dry-run（零写入）→ 自批 apply 被拒（creator 不可审自己）。
    python 端无 daemon 恒 fail-closed，宿主 audit DB 保持 seed 基线。
    """
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    # rust 权威 workspace：git 仓库 + 已修改 tracked.py + 拆分 plan.md。
    rust_root = tmp_path / "rust" / "workspace"
    rust_root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=rust_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "diff@example.com"], cwd=rust_root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Diff Test"], cwd=rust_root, check=True
    )
    tracked = rust_root / "tracked.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=rust_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=rust_root, check=True)
    tracked.write_text("value = 2\n", encoding="utf-8")
    plan = rust_root / "plan.md"
    plan.write_text(
        "## Parser\nMove parser.\n- edit @ src/parser.rs\n\n"
        "## Tests ##\n- test: tests/test_parser.py\n",
        encoding="utf-8",
    )

    db_paths: dict[str, Path] = {}
    for implementation in ("python", "rust"):
        root = tmp_path / implementation / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        _seed_task_audit_db(db_path, root)
        db_paths[implementation] = db_path

    # python 边界：单次代表命令必须 fail-closed，宿主库保持 seed 基线。
    baseline = _task_audit_snapshot(db_paths["python"])
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "cw.py"),
            "task", "completion-review", "finding-task",
        ],
        cwd=tmp_path / "python" / "workspace",
        env=_python_cli_env(python_home, tmp_path / "python" / "workspace", lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    assert _task_audit_snapshot(db_paths["python"]) == baseline

    rust_home = tmp_path / "rust" / "home"

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--mode", "local", "--db", str(db_paths["rust"]), *args],
            cwd=rust_root,
            env=_python_cli_env(rust_home, rust_root, lang="en_US"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_ok(*args: str, contains: str) -> None:
        result = run_rust(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert result.stderr == ""
        body = _task_json_output(result)
        assert contains in body, (args, contains, body)

    # 权威序列前先注入 Evidence Gate 通过记录（review-task apply 所需；
    # rust CLI 尚未实现该门禁，但 apply 语义本身由 rust 权威执行）。
    _inject_gate_pass_records(db_paths["rust"], "review-task")

    assert_ok("task", "completion-review", "finding-task", contains="Review decision: warn")
    assert_ok(
        "task", "resolve-finding", "1", "--resolution", "fixed", "--by", "reviewer",
        contains="Finding #1 resolved",
    )
    assert_ok(
        "task", "apply", "review-task", "--reviewer", "external-reviewer",
        contains="Task applied: review-task",
    )
    assert_ok(
        "task", "close", "review-task", "--reviewer", "external-reviewer",
        contains="Task closed: review-task",
    )
    assert_ok(
        "task", "rollback", "rollback-task", "change-1",
        contains="Task status: reverted",
    )
    assert_ok(
        "task", "split", "split-parent", "--plan", str(plan),
        contains="split into 2 subtasks",
    )
    assert_ok(
        "task", "capture-diff", "capture-task", "--step-id", "capture-step", "--dry-run",
        contains="Mode: dry-run",
    )

    assert _task_audit_snapshot(db_paths["rust"]) == {
        "tasks": [
            ("Review task", "closed", "", 0, 0),
            ("Finding task", "in_progress", "", 0, 1),
            ("Split parent", "open", "", 0, 2),
            ("Rollback task", "reverted", "", 0, 3),
            ("Capture task", "in_progress", "", 0, 4),
            ("Parser", "open", "Split parent", 1, 0),
            ("Tests", "open", "Split parent", 1, 1),
        ],
        "steps": [
            ("Capture task", 0, "edit", "tracked.py", "in_progress"),
            ("Parser", 0, "edit", "src/parser.rs", "pending"),
            ("Tests", 0, "test", "tests/test_parser.py", "pending"),
        ],
        "findings": [("Finding task", "resolved", "reviewer")],
        "changes": [
            ("Rollback task", "old.py", "before", "after", "agent"),
            ("Rollback task", "old.py", "after", "before", "agent"),
        ],
    }

    # creator 自批 apply：rust 权威拒绝且 DB 不变。
    before = db_paths["rust"].read_bytes()
    denied = run_rust("task", "apply", "finding-task", "--reviewer", "builder")
    assert denied.returncode != 0
    assert "self-approval is forbidden" in denied.stderr
    assert db_paths["rust"].read_bytes() == before


def _seed_security_cli_fixture(db: CodeGraphDB, workspace_root: Path) -> None:
    """构造 E4 rule/guardrail/check-gate/audit/bootstrap 的公共事实。"""
    workspace_id = db._get_active_workspace_id()
    danger = workspace_root / "danger.sql"
    danger.write_text(
        "ALTER TABLE users ADD COLUMN x INT;\nDROP TABLE audit;\n",
        encoding="utf-8",
    )
    safe = workspace_root / "safe.py"
    safe.write_text("value = 1\n", encoding="utf-8")
    now = 1735689600.0
    db.conn.execute(
        "INSERT INTO file_contents(content_hash,language,total_lines,first_seen_at) "
        "VALUES('danger-hash','sql',2,?)",
        (now,),
    )
    db.conn.execute(
        "INSERT INTO file_instances(workspace_id,rel_path,abs_path,current_content_hash,"
        "mtime,total_lines,last_parsed,status,module_path) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            workspace_id,
            "danger.sql",
            str(danger),
            "danger-hash",
            now,
            2,
            now,
            "active",
            "danger",
        ),
    )
    candidates = [
        (
            "accept-me",
            "Use transactions",
            "All writes need transactions",
            '{"actions":["edit"]}',
            "critical",
            "manual",
            "{}",
            1.0,
            "pending",
            now,
            None,
            "",
            "",
        ),
        (
            "reject-me",
            "Reject this",
            "Bad candidate",
            "{}",
            "info",
            "manual",
            "{}",
            0.2,
            "pending",
            now + 1,
            None,
            "",
            "",
        ),
    ]
    db.conn.executemany(
        "INSERT INTO agent_rule_candidates(id,title,rule_text,scope_json,severity,source,"
        "evidence_json,confidence,status,created_at,reviewed_at,reviewer,linked_rule_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        candidates,
    )
    db.conn.execute(
        "INSERT INTO tasks(id,title,description,creator,status,created_at,updated_at,depth,sort_order) "
        "VALUES('gate-task','Gate task','','builder','in_progress',?,?,0,0)",
        (now, now),
    )
    db.conn.execute(
        "INSERT INTO change_audit(id,task_id,step_id,file_path,hash_before,hash_after,diff,author,timestamp) "
        "VALUES('gate-change','gate-task','','safe.py','','','','agent',?)",
        (now,),
    )
    db.conn.execute(
        "INSERT INTO guardrail_rules(rule_id,category,severity,pattern,action,description,is_builtin,created_at) "
        "VALUES('gate_existing','check_gate','warn','*','require_review','existing',1,?)",
        (now,),
    )
    db.conn.execute(
        "INSERT INTO guardrail_findings(workspace_id,rule_id,file_path,symbol_hash,severity,status,message,detected_at) "
        "VALUES(?,'gate_existing','safe.py','','warn','open','existing gate finding',?)",
        (workspace_id, now),
    )
    payload_hash = __import__("hashlib").sha256(b"audit-event").hexdigest()
    record_signature = __import__("hashlib").sha256(
        f"|{payload_hash}".encode("utf-8")
    ).hexdigest()
    db.conn.execute(
        "INSERT INTO audit_chain(table_name,record_id,operation,payload_hash,prev_signature,"
        "record_signature,signing_key_id,signed_at) "
        "VALUES('security_events','1','insert',?,'',?,'local',?)",
        (payload_hash, record_signature, now),
    )
    db.conn.execute(
        "INSERT INTO workspace_scan_runs(workspace_id,purpose,baseline_type,git_head,started_at,status) "
        "VALUES(?,'bootstrap','manifest','',?,'completed')",
        (workspace_id, now),
    )
    db.conn.commit()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _security_cli_snapshot(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "candidates": conn.execute(
                "SELECT id,status,reviewer,evidence_json FROM agent_rule_candidates ORDER BY id"
            ).fetchall(),
            "rules": conn.execute(
                "SELECT title,rule_text,scope_json,severity,status,source_candidate_id "
                "FROM agent_rules ORDER BY title"
            ).fetchall(),
            "guardrail_rules": conn.execute(
                "SELECT rule_id,category,severity,pattern,action,is_builtin "
                "FROM guardrail_rules WHERE category='db_safety' ORDER BY rule_id"
            ).fetchall(),
            "guardrail_findings": conn.execute(
                "SELECT f.workspace_id,f.rule_id,f.file_path,f.severity,f.status,f.message "
                "FROM guardrail_findings f JOIN guardrail_rules r ON r.rule_id=f.rule_id "
                "WHERE r.category IN ('db_safety','check_gate') ORDER BY f.file_path,f.rule_id,f.message"
            ).fetchall(),
            "keys": conn.execute(
                "SELECT key_id,is_active FROM audit_key_rotations ORDER BY key_id"
            ).fetchall(),
            "audit": conn.execute(
                "SELECT table_name,record_id,payload_hash,prev_signature,record_signature,signing_key_id "
                "FROM audit_chain ORDER BY id"
            ).fetchall(),
        }
    finally:
        conn.close()


def test_security_rust_authoritative_and_python_boundary(
    tmp_path: Path,
) -> None:
    """security（rule/guardrail/check-gate/audit/bootstrap）：rust CLI 为权威写入方；
    python 薄客户端无 daemon 时 fail-closed 且宿主库零写入。

    探针（2026-09-08，_s1d_probe2.py local / _s1d_probe3.py enterprise(missing
    socket)）：rust 在两模式下执行同一权威序列且 DB 终态一致——enterprise 缺
    daemon 权威时 stay-local；rule candidate accept/reject、guardrail scan、
    check-gate --resolve、audit rotate-key 等写命令由 rust 权威落库。
    """
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")

    roots: dict[str, Path] = {}
    db_paths: dict[str, Path] = {}
    for implementation in ("python", "rust"):
        root = tmp_path / implementation / "workspace"
        root.mkdir(parents=True)
        home = tmp_path / implementation / "home"
        db_path = home / ".callwarden" / "callwarden.db"
        db_path.parent.mkdir(parents=True)
        db = CodeGraphDB(db_path=str(db_path), workspace_root=str(root))
        _seed_security_cli_fixture(db, root)
        db.close()
        roots[implementation] = root
        db_paths[implementation] = db_path

    # python 边界：单次代表命令必须 fail-closed，宿主库保持 seed 基线。
    baseline = _security_cli_snapshot(db_paths["python"])
    python_home = tmp_path / "python" / "home"
    python_result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "cw.py"), "rule", "candidate", "list"],
        cwd=roots["python"],
        env=_python_cli_env(python_home, roots["python"], lang="en_US"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _assert_python_fails_closed_no_daemon(python_result)
    assert _security_cli_snapshot(db_paths["python"]) == baseline

    rust_home = tmp_path / "rust" / "home"

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(binary),
                "--mode",
                "enterprise",
                "--socket",
                str(tmp_path / "missing.sock"),
                "--db",
                str(db_paths["rust"]),
                *args,
            ],
            cwd=roots["rust"],
            env=_python_cli_env(rust_home, roots["rust"], lang="en_US"),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def assert_ok(args: tuple[str, ...], contains: str) -> None:
        result = run_rust(*args)
        assert result.returncode == 0, (args, result.stderr)
        assert result.stderr == ""
        assert contains in result.stdout, (args, contains, result.stdout)

    # 只读组：candidate/rule 浏览与 seed/audit verify/bootstrap 健康汇总。
    read_only_contracts = [
        (("rule", "candidate", "list"), "=== Candidates (2) ==="),
        (("rule", "list"), "=== Active Rules (0) ==="),
        (("rule", "applicable", "--context", '{"action":"edit"}'), "=== Applicable Rules (0) ==="),
        (("rule", "sync"), "Sync failed: Marker block not found"),
        (("rule", "extract", "--task-id", "gate-task"), "=== Extracted Candidates (0) ==="),
        (("rule", "seed-bootstrap"), "=== Bootstrap Seed Dry-Run (5 rules) ==="),
        (("rule", "cleanup-sync-log"), "=== Sync Log Cleanup Dry-Run"),
        (("audit", "verify", "--table", "security_events"), "Audit chain integrity verified"),
        (("bootstrap", "status"), "Bootstrap Health Summary"),
    ]
    for args, fragment in read_only_contracts:
        assert_ok(args, fragment)

    # 写组：accept/reject/guardrail/check-gate/rotate-key 由 rust 权威落库。
    mutation_contracts = [
        (("rule", "candidate", "accept", "accept-me", "--reviewer", "reviewer"),
         "Accepted: candidate=accept-me"),
        (("rule", "candidate", "reject", "reject-me", "--reviewer", "reviewer",
          "--reason", "not applicable"),
         "Rejected: reject-me"),
        (("rule", "list"), "=== Active Rules (1) ==="),
        (("guardrail", "scan", "--file", "danger.sql", "--category", "db_safety"),
         "GR-builtin-db-2"),
        (("guardrail", "rules", "--category", "db_safety"),
         "=== Guardrail Rules ==="),
        (("check-gate", "gate-task", "--resolve"),
         "=== Gate Findings Marked Resolved ==="),
        (("audit", "verify", "--table", "security_events"),
         "Audit chain integrity verified"),
        (("audit", "rotate-key", "--key-id", "next-key", "--secret", "fixed-secret"),
         "=== Audit Signing Key Rotated ==="),
        (("audit", "keys"), "=== Audit Signing Keys (1) ==="),
        (("bootstrap", "status"), "Active rules: 1"),
    ]
    for args, fragment in mutation_contracts:
        assert_ok(args, fragment)

    assert _security_cli_snapshot(db_paths["rust"]) == {
        "candidates": [
            ("accept-me", "accepted", "reviewer", "{}"),
            ("reject-me", "rejected", "reviewer", '{"reject_reason": "not applicable"}'),
        ],
        "rules": [
            ("Use transactions", "All writes need transactions", '{"actions":["edit"]}',
             "critical", "active", "accept-me"),
        ],
        "guardrail_rules": [
            ("GR-builtin-db-1", "db_safety", "warn", "\\bALTER\\s+TABLE\\b", "warn", 1),
            ("GR-builtin-db-2", "db_safety", "block", "\\bDROP\\s+(TABLE|COLUMN)\\b", "block", 1),
            ("GR-builtin-db-3", "db_safety", "block",
             "VARCHAR\\s*\\(\\s*(\\d+)\\s*\\)\\s*(?:→|->)\\s*VARCHAR\\s*\\(\\s*(\\d+)\\s*\\)",
             "block", 1),
        ],
        "guardrail_findings": [
            (1, "GR-builtin-db-1", "danger.sql", "warn", "open",
             "Detected ALTER TABLE statement (line 1)"),
            (1, "GR-builtin-db-1", "danger.sql", "warn", "open",
             "SQL file is not under migrations/ (migration script missing risk)"),
            (1, "GR-builtin-db-2", "danger.sql", "block", "open",
             "Detected DROP TABLE statement (line 2)"),
            (1, "gate_existing", "safe.py", "warn", "resolved", "existing gate finding"),
        ],
        "keys": [("next-key", 1)],
        "audit": [
            ("security_events", "1",
             "e81a0a5e1551e1d889cdeb2c190bdbbd5c96fe48e28f68d779b3b7bd7817c85e",
             "",
             "48317521730652c99a15e0775c64d3c9a208101ebd32d40cc6671f1a2af3d193",
             "local"),
        ],
    }


def test_security_commands_fail_closed_without_mutating_evidence(tmp_path: Path) -> None:
    binary = _rust_cw_binary()
    if not binary.exists():
        pytest.skip(f"Rust cw binary not built: {binary}")
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    db_path = home / ".callwarden" / "callwarden.db"
    db_path.parent.mkdir(parents=True)
    db = CodeGraphDB(db_path=str(db_path), workspace_root=str(root))
    db.conn.execute(
        "INSERT INTO tasks(id,title,description,creator,status,created_at,updated_at,depth,sort_order) "
        "VALUES('no-evidence','No evidence','','builder','in_progress',1,1,0,0)"
    )
    db.conn.execute(
        "INSERT INTO agent_rule_candidates(id,title,rule_text,scope_json,severity,source,"
        "evidence_json,confidence,status,created_at,reviewer,linked_rule_id) "
        "VALUES('candidate','Rule','Text','{}','info','manual','{}',1,'pending',1,'','')"
    )
    db.conn.commit()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CALLWARDEN_WORKSPACE": str(root),
            "CALLWARDEN_LANG": "en_US",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
        }
    )

    def run_rust(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(binary), "--db", str(db_path), *args],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    before = _security_cli_snapshot(db_path)
    rejected_commands = [
        ("guardrail", "scan", "--file", "../"),
        ("check-gate", "no-evidence"),
        ("audit", "verify"),
        ("audit", "rotate-key", "--key-id", "local", "--secret", "x"),
        ("rule", "candidate", "accept", "candidate", "--reviewer", ""),
    ]
    for args in rejected_commands:
        result = run_rust(*args)
        assert result.returncode != 0, (args, result.stdout, result.stderr)
        assert _security_cli_snapshot(db_path) == before
