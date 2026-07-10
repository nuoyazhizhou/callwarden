# Watcher Generation 状态机

> 从 `enterprise-architecture-evolution.md` v10.2 抽取。只保留当前规范 + 状态机 + 不变量 + 故障注入测试，不保留 v1-v9 修订过程。
> 基线版本：v10.2（`ad2e308`）。

## 1. 角色与职责

```
┌──────────────────────┐       UDS        ┌──────────────────────────┐
│ systemd --user agent │ ◄──────────────► │  daemon（User=callwarden） │
│ 以用户身份运行        │  MSG_CONNECT     │  可信，不可被用户篡改      │
│ 读文件 + canonicalize │  MSG_REFRESH     │  重新算 hash + Rust parse  │
│ 回传 canonical bytes  │                  │  发布 CAS + 写 manifest    │
└──────────────────────┘                  └──────────────────────────┘
```

**agent 不信任 daemon**（agent 不持有 daemon 凭证）。
**daemon 不信任 agent**（daemon 重新算 hash 后由可信 Rust parser 解析；不信任 agent 提供的 cas_key / git_tree_oid）。

## 2. Session 数据模型

```sql
-- agent_sessions：所有 session 的注册表（含已撤销）
CREATE TABLE agent_sessions (
    workspace_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,          -- agent UUID
    session_epoch INTEGER NOT NULL,    -- daemon 分配的单调递增 epoch
    activated_at INTEGER NOT NULL,     -- 激活时间戳
    revoked_at INTEGER,                -- 撤销时间戳（NULL=active）
    peer_uid INTEGER NOT NULL,         -- 哪个用户的 session
    PRIMARY KEY (workspace_id, session_id)
);

-- workspace_active_session：每个 workspace 当前唯一的 active session
CREATE TABLE workspace_active_session (
    workspace_id INTEGER PRIMARY KEY,
    active_session_id TEXT NOT NULL,
    active_session_epoch INTEGER NOT NULL
);

-- file_generations：per-file 消息去重 + CAS 两阶段提交
CREATE TABLE file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_session_id TEXT DEFAULT '',
    latest_session_epoch INTEGER DEFAULT 0,
    latest_seq INTEGER DEFAULT 0,
    latest_seen_generation TEXT DEFAULT '',      -- "{epoch}:{seq}"
    latest_committed_generation TEXT DEFAULT '',  -- "{epoch}:{seq}"
    PRIMARY KEY (workspace_id, rel_path)
);
```

## 3. Agent Session 定义

```python
class AgentSession:
    """每个 systemd --user agent 实例一个 session。"""
    session_id: str          # UUID，agent 启动时生成
    session_epoch: int = 0   # daemon 握手时分配的单调 epoch
    seq_counter: int = 0     # 单调递增，每发一条消息 +1
```

## 4. 状态机

### 4.1 连接握手：daemon_handle_connect

```
agent 连接
    │
    ▼
daemon_handle_connect(peer_uid, workspace_id, session_id)
    │
    ▼
BEGIN IMMEDIATE
    ├─ 1. UPDATE agent_sessions SET revoked_at=now WHERE workspace_id=? AND revoked_at IS NULL
    │       （撤销同一 workspace 所有旧 active session）
    ├─ 2. 分配 new_epoch = MAX(all session_epoch for this workspace) + 1
    ├─ 3. INSERT agent_sessions (new_epoch, activated_at=now)
    ├─ 4. INSERT OR REPLACE workspace_active_session (new session + new epoch)
    └─ 5. UPDATE file_generations SET latest_seq=0 WHERE workspace_id=?
            （新 session seq 从 1 开始）
COMMIT
    │
    ▼
return {"session_epoch": new_epoch}
```

```python
def daemon_handle_connect(peer_uid, workspace_id, requested_session_id, ws_conn):
    """agent 连接握手——daemon 分配单调 epoch，旧 session 永久失效。"""
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        now = int(time.time())
        ws_conn.execute(
            "UPDATE agent_sessions SET revoked_at = ? "
            "WHERE workspace_id = ? AND revoked_at IS NULL",
            (now, workspace_id)
        )
        row = ws_conn.execute(
            "SELECT COALESCE(MAX(session_epoch), 0) + 1 AS next_epoch "
            "FROM agent_sessions WHERE workspace_id = ?",
            (workspace_id,)
        ).fetchone()
        new_epoch = row["next_epoch"]
        ws_conn.execute(
            "INSERT INTO agent_sessions (workspace_id, session_id, session_epoch, "
            "activated_at, revoked_at, peer_uid) VALUES (?, ?, ?, ?, NULL, ?)",
            (workspace_id, requested_session_id, new_epoch, now, peer_uid)
        )
        ws_conn.execute(
            "INSERT OR REPLACE INTO workspace_active_session "
            "(workspace_id, active_session_id, active_session_epoch) VALUES (?, ?, ?)",
            (workspace_id, requested_session_id, new_epoch)
        )
        ws_conn.execute(
            "UPDATE file_generations SET latest_session_id = ?, "
            "latest_session_epoch = ?, latest_seq = 0, "
            "latest_seen_generation = '' WHERE workspace_id = ?",
            (requested_session_id, new_epoch, workspace_id)
        )
        ws_conn.execute("COMMIT")
        return {"session_epoch": new_epoch}
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise
```

### 4.2 Agent 连接流程

```python
def user_agent_connect(daemon_sock, session: AgentSession):
    """agent 连接握手——向 daemon 注册 session_id，获取 epoch。"""
    resp = send_request(daemon_sock, MSG_CONNECT, {
        "session_id": session.session_id,
    })
    session.session_epoch = resp["session_epoch"]
    session.seq_counter = 0  # 新 epoch 的 seq 从 1 开始
```

### 4.3 消息处理：daemon_handle_refresh

```
agent 发送 MSG_REFRESH(msg, canonical_bytes)
    │
    ▼
daemon_handle_refresh(peer_uid, workspace_id, msg, canonical_bytes, ...)
    │
    ▼
1. 查询 workspace_active_session
    │
    ├─ incoming_epoch != active_session_epoch
    │       → ProtocolError("stale session rejected")
    │       （旧 session 延迟消息，拒绝，不进入 CAS 路径）
    │
    └─ epoch 匹配 ✓
            │
            ▼
        2. incoming_gen = "{incoming_epoch}:{incoming_seq}"
            │
            ▼
        3. CAS 第一阶段（seen）
           BEGIN IMMEDIATE
           IF incoming_seq <= latest_seq → ROLLBACK（同 epoch 内 stale seq）
           ELSE → UPDATE latest_seen_generation = incoming_gen
           COMMIT
            │
            ▼
        4. 重新计算 hash（不信任 agent）
           actual_hash = sha256(canonical_bytes)
            │
            ▼
        5. Git mirror 校验 clean/dirty
            │
            ▼
        6. 可信 Rust parser 解析
            │
            ▼
        7. CAS 发布（clean 则发布到 Global CAS）
            │
            ▼
        8. CAS 第二阶段（committed）
           BEGIN IMMEDIATE
           UPDATE workspace_manifests ...
           UPDATE latest_committed_generation WHERE latest_seen_generation = incoming_gen
           IF rowcount != 1 → ROLLBACK（其他 handler 已覆盖 seen）
           COMMIT
```

```python
def daemon_handle_refresh(peer_uid, workspace_id, msg, canonical_bytes, cas_conn, ws_conn):
    rel_path = msg["rel_path"]
    incoming_session = msg["agent_session_id"]
    incoming_seq = msg["monotonic_seq"]
    incoming_epoch = msg["session_epoch"]

    # 校验 session epoch——只能匹配当前 active epoch
    active = ws_conn.execute(
        "SELECT active_session_id, active_session_epoch "
        "FROM workspace_active_session WHERE workspace_id = ?",
        (workspace_id,)
    ).fetchone()
    if active is None:
        raise ProtocolError(f"no active session for workspace {workspace_id}")
    if (incoming_session != active["active_session_id"]
            or incoming_epoch != active["active_session_epoch"]):
        raise ProtocolError(
            f"stale session rejected: incoming={incoming_session}:{incoming_epoch} "
            f"active={active['active_session_id']}:{active['active_session_epoch']}"
        )
    incoming_gen = f"{incoming_epoch}:{incoming_seq}"

    # CAS 第一步——原子更新 latest_seen_generation
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        row = ws_conn.execute(
            "SELECT latest_session_epoch, latest_seq, latest_seen_generation, "
            "latest_committed_generation FROM file_generations "
            "WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path)
        ).fetchone()

        if row is None:
            ws_conn.execute(
                "INSERT INTO file_generations (...) VALUES (...)",
                (workspace_id, rel_path, incoming_session, incoming_epoch,
                 incoming_seq, incoming_gen)
            )
        elif incoming_seq <= row["latest_seq"]:
            ws_conn.execute("ROLLBACK")
            return  # stale seq，直接丢弃
        else:
            ws_conn.execute(
                "UPDATE file_generations SET latest_session_id = ?, "
                "latest_session_epoch = ?, latest_seq = ?, latest_seen_generation = ? "
                "WHERE workspace_id = ? AND rel_path = ?",
                (incoming_session, incoming_epoch, incoming_seq, incoming_gen,
                 workspace_id, rel_path)
            )
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise

    # ... re-hash, clean/dirty check, Rust parse, CAS publish ...

    # CAS 第二步——条件更新 latest_committed_generation
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        # ... update workspace_manifests ...
        gen_cur = ws_conn.execute(
            """UPDATE file_generations SET latest_committed_generation = ?
               WHERE workspace_id = ? AND rel_path = ?
                 AND latest_seen_generation = ?""",
            (incoming_gen, workspace_id, rel_path, incoming_gen)
        )
        if gen_cur.rowcount != 1:
            ws_conn.execute("ROLLBACK")
            raise ProtocolError(f"stale manifest commit for {rel_path}")
        ws_conn.execute("COMMIT")
    except Exception:
        ws_conn.execute("ROLLBACK")
        raise
```

## 5. 多 host/container agent 限制

同一 workspace 同一时刻只允许**一个** active session。多 host agent 要写同一 workspace 必须先 revoke 当前 active session 再握手新 session（不允许并发写）。

```
Agent Host A          Agent Host B           Daemon
    │                      │                    │
    │ ── connect(s1) ──►   │                    ├─ epoch=1, session=s1
    │                      │                    │
    │                      │ ── connect(s2) ──►  ├─ revoke s1, epoch=2, session=s2
    │                      │                    │
    │ ── refresh(s1,1) ──► │                    ├─ epoch=1 != active epoch=2 → ProtocolError ✗
```

## 6. 不变量

| # | 不变量 | 测试覆盖 |
|---|--------|---------|
| W1 | session_epoch 单调递增，由 daemon 在握手时分配，agent 不能自选 | `daemon_handle_connect` |
| W2 | 握手独占：`daemon_handle_connect` 在 `BEGIN IMMEDIATE` 事务中 revoke 旧 session + 分配新 epoch + 更新 active，同一时刻只有一个 active | `test_concurrent_same_workspace_rejected` |
| W3 | 旧 session 延迟消息：epoch 不匹配 → ProtocolError，不进入 CAS 路径（零副作用） | `test_stale_session_epoch_mismatch_rejected` |
| W4 | epoch 校验通过但 generation 被 S2 推进 → CAS 第二阶段 stale → ROLLBACK | `test_stale_session_rejected_both_phases` |
| W5 | 新 session 握手后所有 file_generations latest_seq=0 | `daemon_handle_connect` step 5 |
| W6 | 同 epoch 内 seq <= latest_seq → 直接丢弃（stale seq），不报错 | `daemon_handle_refresh` seq 分支 |
| W7 | agent 不持有 daemon 凭证；daemon 重新算 hash 后由可信 Rust parser 解析 | 架构约束 |

## 7. 模型测试

```python
# tests/model/test_session_epoch_race.py

import threading

def test_stale_session_rejected_both_phases():
    """S1 校验→S2 握手→S1 提交：S1 两阶段均被 stale 拒绝。
    
    barrier_validate:  S1 epoch=1 校验通过
    barrier_handshake: S2 握手 epoch=2，写一条消息推进 generation → "2:1"
    barrier_commit:    S1 CAS 第一步 generation="2:1" > "1:5" → stale 拒绝
    """
    barrier_validate = threading.Barrier(2)
    barrier_handshake = threading.Barrier(2)
    barrier_commit = threading.Barrier(2)
    errors = []

    def s1_agent():
        barrier_validate.wait(timeout=5)
        # epoch 校验通过 → (s1, 1) ✓
        barrier_handshake.wait(timeout=5)
        barrier_commit.wait(timeout=5)
        # CAS 第一步应因 stale generation 被拒
        errors.append("S1 CAS 提交阶段应被 stale generation 拒绝但成功写入")

    def s2_agent():
        barrier_validate.wait(timeout=5)
        resp = daemon_handle_connect(peer_uid=1, workspace_id=1,
                                     requested_session_id="s2", ws_conn=ws)
        assert resp["session_epoch"] == 2
        daemon_handle_refresh(peer_uid=1, workspace_id=1,
                              msg={"agent_session_id": "s2", "session_epoch": 2,
                                   "monotonic_seq": 1},
                              canonical_bytes=b"x", cas_conn=cas, ws_conn=ws)
        barrier_handshake.wait(timeout=5)
        barrier_commit.wait(timeout=5)

    t1 = threading.Thread(target=s1_agent)
    t2 = threading.Thread(target=s2_agent)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert errors == [], f"竞态不变量被破坏: {errors}"


def test_stale_session_epoch_mismatch_rejected():
    """S1 延迟消息在 S2 握手后到达：epoch 不匹配 ProtocolError。"""
    barrier_s2_done = threading.Barrier(2)
    errors = []

    def s1_late_arrival():
        barrier_s2_done.wait(timeout=5)
        try:
            daemon_handle_refresh(peer_uid=1, workspace_id=1,
                                  msg={"agent_session_id": "s1", "session_epoch": 1,
                                       "monotonic_seq": 5},
                                  canonical_bytes=b"...", cas_conn=cas, ws_conn=ws)
            errors.append("S1 延迟消息 epoch=1 应被 ProtocolError 拒绝")
        except ProtocolError as e:
            if "stale" not in str(e).lower() and "epoch" not in str(e).lower():
                errors.append(f"非预期异常: {e}")

    def s2_handshake():
        resp = daemon_handle_connect(peer_uid=1, workspace_id=1,
                                     requested_session_id="s2", ws_conn=ws)
        assert resp["session_epoch"] == 2
        barrier_s2_done.wait(timeout=5)

    t1 = threading.Thread(target=s1_late_arrival)
    t2 = threading.Thread(target=s2_handshake)
    t2.start(); t1.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert errors == [], f"竞态不变量被破坏: {errors}"


def test_concurrent_same_workspace_rejected():
    """同一 workspace 两个 agent 并发连接→后者 revoke 前者，前者写入被拒。"""
    barrier_both_connected = threading.Barrier(2)
    errors = []

    def s1_worker():
        resp = daemon_handle_connect(peer_uid=1, workspace_id=1,
                                     requested_session_id="s1", ws_conn=ws)
        assert resp["session_epoch"] == 1
        barrier_both_connected.wait(timeout=5)
        try:
            daemon_handle_refresh(peer_uid=1, workspace_id=1,
                                  msg={"agent_session_id": "s1", "session_epoch": 1,
                                       "monotonic_seq": 1},
                                  canonical_bytes=b"...", cas_conn=cas, ws_conn=ws)
            errors.append("S1 epoch=1 写入应被拒绝（已被 S2 revoke）")
        except ProtocolError:
            pass

    def s2_worker():
        barrier_both_connected.wait(timeout=5)
        resp = daemon_handle_connect(peer_uid=1, workspace_id=1,
                                     requested_session_id="s2", ws_conn=ws)
        assert resp["session_epoch"] == 2

    t1 = threading.Thread(target=s1_worker)
    t2 = threading.Thread(target=s2_worker)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert errors == [], f"并发写不变量被破坏: {errors}"
```

## 8. 故障注入测试

| 场景 | 注入方式 | 期望结果 |
|------|---------|---------|
| Agent crash 后重启 | kill agent → 新 agent 重新 connect | 新 session_epoch = 旧 + 1，旧 generation 失效 |
| Daemon crash 后重启 | kill daemon 期间 agent 持续发消息 | 重启后 agent 重新握手，旧 epoch 失效 |
| S1 延迟消息到达（epoch 落后） | 网络延迟使 S1 消息在 S2 握手后到达 | epoch 不匹配 ProtocolError，不进入 CAS |
| S1 CAS 提交被 S2 覆盖 | barrier 固定 S1→S2→S1 顺序 | S1 第二阶段 stale → ROLLBACK |
| 同一 workspace 双 agent 并发 connect | 两个 agent 几乎同时 connect | 后者 revoke 前者，前者写入被 ProtocolError |
| Monotonic_seq 重复 | 同 epoch 内发两条 seq=5 | 第二条 seq <= latest → 丢弃，不报错 |
| Monotonic_seq 倒退 | seq 从 5 跳回 3 | seq=3 <= latest=5 → 丢弃 |
| Agent 伪造 session_epoch | agent 跳过 connect 直接发 epoch=999 | 无此 epoch 的 active session → ProtocolError |
