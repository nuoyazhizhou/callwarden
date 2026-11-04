# 远端控制面设计：HTTPS + mTLS 单轨传输（ADR-002）

> 状态：**已裁决（2026-11-04）**。本文档冻结架构决策与不变量；实现按 §10 的 S1–S4 分期，
> 每期一张独立任务卡（父卡 `[REMOTE] HTTPS+mTLS 远端控制面`）。
> 取代：`docs/design/http-security-profile-v2-design.md`（其 v2 `loopback_token_authenticated`
> 与 v3 `cross_host_mutual_tls` 方向均被本 ADR 收敛为单轨 mTLS）。
> 验收矩阵：`docs/design/cli-mcp-matrix-backlog.md` §38。

---

## 1. 决策（ADR）

**唯一传输 = HTTPS + 双向 TLS（mTLS）。**

1. daemon 只监听 TLS 端口；不再存在明文 HTTP 监听面。
2. 客户端必须出示由 daemon 信任的 CA 签发的客户端证书；服务端必须出示覆盖目标
   host/IP 的服务端证书。
3. 删除 Named Pipe（Windows）、Unix Domain Socket、`cw-bridge` 二进制及其全部测试夹具、
   打包项与运行时分支（S1 删除 bridge，S4 清尾）。
4. 身份由证书 SAN 派生的 `principal_id` 承担，不再用 OS peer uid/SID 做授权主语。
5. 文件面仍由**客户端本机执行**；daemon 只做治理、索引、审计与快照接收。daemon 永不
   打开远端 principal 提交的路径。
6. 默认绑定 loopback；只有显式 `--allow-remote` + 有效 mTLS 才允许非 loopback。
7. 保留 Python MCP（先接 mTLS），Rust `cw`/`cw-client` 后续接 HTTPS；不因传输改造重写语言。

被否决的方案：

| 方案 | 否决理由 |
| --- | --- |
| 保留 pipe/UDS，仅为远程加 mTLS | 双轨矩阵翻倍；WSL 仍需 bridge；跨用户/跨 OS 行为不统一 |
| HTTPS + Bearer token（无客户端证书） | token 可被同机进程读取/复制，无法把调用方绑定到 agent 身份；无法满足“谁在调用” |
| daemon 直接读远端路径（网络盘/UNC/挂载） | 破坏 F-5 的 `host_real_root` 锚定模型；ACL、大小写、符号链接语义跨 OS 不一致 |
| 把 Python MCP 重写为 Rust | 传输与语言无关；重写成本远大于收益，且会阻塞产品能力 |
| 外部 PKI 强制依赖（企业 CA/ACME） | 开发机/离线环境不可用；内置 CA 已足够，外部 CA 作为可选导入 |

---

## 2. 现状事实（实施基线）

| 面 | 现行事实 | 证据 |
| --- | --- | --- |
| HTTP profile | 固定 `dev_loopback_unauthenticated`，明文、合成 local-owner peer | `rust_ext/src/daemon/http_server.rs` L4-5、L61 |
| 绑定 | `resolve_loopback` 强校验，非 loopback `E_HTTP_MVP_LOOPBACK_ONLY` | 同文件 L331 |
| HTTP 栈 | axum 0.8 + hyper 1 + tokio 1，HTTP/1.1 only；无 rustls/reqwest | `rust_ext/Cargo.toml` L150-157 |
| CLI 传输 | `cw.exe` Enterprise 走 Named Pipe / UDS | `rust_ext/src/cli/runtime.rs` L298-353 |
| 桥 | `cw-bridge` 仅为 WSL 场景存在 | `rust_ext/Cargo.toml` L36-41、`rust_ext/src/bin/cw_bridge.rs` |
| peer 身份 | `peer_uid` / `current_uid` 共 160 处 Rust 引用（8 个模块）+ 48 处 Python 引用 | `rust_ext/src/daemon/*.rs`、`server/`、`cli/` |
| snapshot | `snapshot.publish` 接收 `db_path` 或 FD；FD 仅 Unix | `rust_ext/src/daemon/snapshot_state.rs` L699、L754-780 |
| 文件面 | 路径按 workspace `host_real_root` canonicalize 后校验根内 | `rust_ext/src/daemon/fs_handlers.rs` L165-248 |
| Python MCP | `HttpDaemonRpcClient` 用 `urllib.request` 明文调用；存在 `daemon_mode=local` 分支 | `server/mcp_server.py` L50-107、`server/daemon_client.py` L2402/L2491 |
| 工具直连 DB | 9 个 tool 模块、22 处 `sqlite3`/`get_db`/`_get_db_path_for_daemon` 残留 | `server/tools/*.py` |

---

## 3. 威胁模型与安全不变量

### 3.1 必须阻止

| 威胁 | 防线 |
| --- | --- |
| 未持证书的本机/远端进程调用 RPC | TLS 层 `client_auth_required`，握手阶段拒绝 |
| 攻击者用自有 CA 伪造客户端/服务端 | 客户端只信任 pinned daemon CA；daemon 只信任自签 CA |
| 证书主体伪造（改 CN 冒充他人） | 授权只读 SAN URI `spiffe://callwarden.local/principal/<id>`，不读 CN |
| 过期/吊销证书继续使用 | 握手校验证书链 + 有效期 + 吊销集合；连接最大存活时间兜底 |
| 跨 workspace / 跨 agent 越权 | `principal → workspace` 授权表；每个 RPC 校验 principal 归属 |
| 明文嗅探 / MITM | 全链路 TLS；不存在明文端口；客户端强制 `check_hostname` + CA pin |
| bootstrap token 被窃取后反复使用 | 单次性 token、10 分钟 TTL、绑定 principal/workspace、原子消费、审计 |
| 重放旧写请求 | `write.grant` 绑定 `base_hash + task_id/step_id + 过期时间`；nonce/request_id 去重 |
| 远端路径穿越 | 远端 principal 的路径参数一律拒绝；路径解析只在客户端发生 |

### 3.2 明确不阻止（必须写进用户文档，禁止过度承诺）

1. **本机 shell 旁路**：agent 若被授予本机 shell，可直接改 SQLite/文件。MCP/daemon 的
   权限模型**无法阻止**同机同用户（或更高权限）进程绕过 RPC。唯一防线是 OS 文件 ACL、
   磁盘加密与最小权限运行；这属于部署责任，不是协议能力。
2. **daemon owner 被攻陷**：owner 可读 CA 私钥与全部数据。
3. **拒绝服务**：TLS 握手与请求洪泛只做限流，不承诺可用性。
4. **本机 root/管理员**：可读取任何 0600 文件（Unix root / Windows Administrators）。

### 3.3 不变量（实现必须机械保持）

- **I1 单轨**：任一构建产物中不得存在明文 HTTP listener 或 pipe/UDS listener 分支。
- **I2 fail-closed**：缺证书、证书不合法、principal 未授权、grant 不匹配，一律拒绝，
  绝不降级到匿名或旧 profile。
- **I3 身份唯一来源**：授权判断只允许读 `principal_id`；`peer_uid`/SID 只能作为诊断元数据。
- **I4 daemon 不碰远端路径**：远端请求中出现绝对路径/`db_path`/`host_real_root` 即拒绝。
- **I5 写绑定基线**：任何写提交必须携带 grant 与 `base_hash`，不匹配即拒绝且不更新索引。
- **I6 可审计**：签发、吊销、授权变更、grant 发放/提交/拒绝全部落审计事件。

---

## 4. PKI

### 4.1 组成

```
~/.callwarden/pki/                     （目录 0700 / Windows owner-only ACL）
├── ca/
│   ├── ca.crt                         （PEM，公开）
│   ├── ca.key                         （PEM，0600；仅 daemon owner 可读）
│   └── ca.meta.json                   （subject、serial、not_after、fingerprint）
├── server/
│   ├── server.crt
│   └── server.key
├── identities/<principal_id>/
│   ├── client.crt
│   ├── client.key
│   └── meta.json                      （serial、issued_at、not_after、workspace_ids）
├── bootstrap/<token_id>.json          （单次 token，0600，短 TTL）
└── revocations.json                   （serial 集合缓存，权威在 DB）
```

- 算法：ECDSA P-256（默认）；可选 RSA-3072。TLS 仅 AEAD 套件，TLS 1.2+（优先 1.3）。
- CA：自签，`O=CallWarden Local CA`，有效期 10 年；可导入外部 CA（配置
  `pki.external_ca_cert/key`，此时不再自签）。
- 服务端叶证书：`O=CallWarden Daemon`，EKU `serverAuth`，有效期 1 年；
  SAN 必须覆盖 `DNS:localhost`、`IP:127.0.0.1`、`IP:::1`、`IP:<LAN>`、`DNS:<hostname>`、
  `DNS:<fqdn>`；启动时若 SAN 不覆盖实际绑定地址 → `E_TLS_SAN_MISMATCH` fail-closed。
- 客户端叶证书：`O=CallWarden Client`，EKU `clientAuth`，有效期 90 天；
  SAN 必含 `URI:spiffe://callwarden.local/principal/<principal_id>`。
- `principal_id`：不透明稳定标识，`pr_` + 32 位小写 hex；由 daemon 签发时分配，
  与 OS 用户、机器、进程均无关。

### 4.2 引导（解决“先有证书才能连”的鸡生蛋）

```
daemon 首次启动（无 CA）
  └─ 生成 CA + server 证书 → 发布 manifest（含 CA 证书与指纹）
       └─ owner 本地执行 `cw pki token issue --principal pr_xxx --workspace <id>`
            └─ 客户端 `cw pki enroll --token <token> --csr client.csr`
                 └─ POST /v1/enroll（仅服务端认证 TLS，无需客户端证书）
                      └─ daemon 校 token（未用/未过期/绑定匹配）→ 签发 client.crt
                           └─ token 原子消费 + 审计事件
                                └─ 之后所有 /v1/rpc 必须 mTLS
```

- token：256-bit CSPRNG，`token_id + secret` 两段；文件 0600；TTL 600s；单次。
- `/v1/enroll` 是**唯一**允许“无客户端证书”的写面；它不接受任何业务方法。
- 失败响应统一 `E_ENROLL_TOKEN_INVALID`，不回显“过期/已用/主体不匹配”细节，避免 oracle。
- token 发放仅允许 daemon owner principal（本地 CLI 或已 mTLS 的 owner 会话）。

### 4.3 轮换与吊销

| 操作 | 行为 |
| --- | --- |
| `cw pki renew` | 已持有效客户端证书者经 `/v1/enroll/renew` 换发新证书；旧证书在 overlap 窗口后吊销 |
| `cw pki rotate-server` | 用同一 CA 签发新服务端证书；写入 `server/`，重启生效，manifest 同步更新 |
| `cw pki rotate-ca` | 生成新 CA，双 CA 信任窗口（旧+新并存 ≤ 24h），全部客户端重签后吊销旧 CA |
| `cw pki revoke --serial <s>` | 写 `pki_revocations`（权威 DB + 审计），daemon 内存吊销集热更新 |
| 吊销生效 | 握手期拒绝；已建立的 keep-alive 连接由 `max_conn_age`（默认 300s）强制断开重连 |

---

## 5. 身份层

### 5.1 映射链

```
TLS peer cert
  → 校验：链到 pinned CA / EKU clientAuth / 有效期 / serial 未吊销 / SAN URI 合法
  → principal_id = SAN URI 末段
  → pki_principals（active? kind: human|agent|service）
  → workspace_principal_grants（principal_id, workspace_id, role: owner|agent|readonly）
  → PeerCredential { principal_id, kind, auth_method="mtls", cert_serial,
                     cert_fingerprint_sha256, allowed_workspace_ids, agent_instance_id? }
```

- `agent_instance_id` 不在证书里硬编码；由客户端在 RPC 顶层自报，daemon 只把它当作
  **该 principal 的会话标签**，不能提升权限（授权仍只看 principal）。
- 一个 agent 一张证书是推荐形态；同一证书多 agent 共享时，治理事件只能区分到 principal，
  无法区分实例——该限制必须写进部署文档。

### 5.2 peer_uid 收口

- Rust：`PeerCredential.peer_uid` 保留为**诊断字段**，授权比较改为
  `principal_id` + `allowed_workspace_ids`；8 个模块、160 处引用逐一审计。
- Python：48 处 `peer_uid`/`current_uid` 引用改为 principal 断言；`daemon_mode=local`
  分支在 S1 删除（local 模式不再有任何合法传输）。
- workspace owner 迁移：首次 TLS 启动把既有 `owner_uid` 行映射到 daemon owner principal，
  写审计事件 `pki_owner_migration`；其他 principal 默认无权限，须显式 grant。

### 5.3 授权粒度

| 资源 | 授权键 |
| --- | --- |
| workspace 读 | `workspace_principal_grants.role ∈ {owner, agent, readonly}` |
| workspace 写 / graph 写 | `role ∈ {owner, agent}` |
| 治理写（task.apply/close、ACL 变更） | `role = owner` 或显式 `governance` 授权 |
| 跨 workspace | 不存在“全局”授权；每个 workspace 单独 grant |

---

## 6. 绑定与暴露

| 配置 | 结果 |
| --- | --- |
| 默认（无参数） | `https://127.0.0.1:0`，TLS + mTLS，manifest 发布 |
| `--bind 127.0.0.1:PORT` | 同上，固定端口 |
| `--bind <非 loopback> --allow-remote` | 允许；必须有有效服务端证书且 SAN 覆盖该地址 |
| `--bind <非 loopback>` 无 `--allow-remote` | `E_HTTP_BIND_REMOTE_NOT_ALLOWED`，不建 listener |
| 任何“关闭 TLS”参数 | 不存在该参数；旧 `CW_DAEMON_HTTP_PROFILE=dev_loopback_unauthenticated` 视为非法配置 |

manifest 升级为 `callwarden-http-manifest/v2`：

```json
{
  "schema": "callwarden-http-manifest/v2",
  "security_profile": "https_mtls_v1",
  "endpoint": "https://127.0.0.1:7633",
  "bind_scope": "loopback",
  "auth": "mtls_client_cert_required",
  "tls": {
    "min_version": "1.2",
    "ca_cert_pem": "-----BEGIN CERTIFICATE-----...",
    "ca_fingerprint_sha256": "<hex>",
    "server_cert_sha256": "<hex>",
    "server_not_after": "2027-11-04T00:00:00Z",
    "san": ["DNS:localhost", "IP:127.0.0.1", "IP:::1"]
  },
  "daemon_pid": 25008,
  "git_commit": "...",
  "capability_registry_revision": "http-mvp-cap-registry-v1"
}
```

- manifest 仍按 authority 分文件、0600；CA 证书与指纹是公开信息，可放 manifest。
- `/health`：仅服务端认证（无需客户端证书），返回 profile/commit/pid，供探活与
  autostart 判断；不得泄露 principal 列表、grant、路径。
- `/capabilities`：要求客户端证书（S1 起），避免匿名枚举方法面。
- `/v1/enroll`：仅服务端认证 + 有效 token。
- `/v1/rpc`、`/v1/jobs*`：强制客户端证书。

---

## 7. 传输与客户端

| 端 | 实现 |
| --- | --- |
| daemon | `axum-server`/`tokio-rustls`（新增依赖）；`rustls` 作为唯一 TLS 后端；HTTP/1.1 + ALPN `http/1.1` |
| Python MCP/CLI | `urllib.request` + `ssl.SSLContext(PROTOCOL_TLS_CLIENT)`：`load_verify_locations(ca)`、`load_cert_chain(cert,key)`、`check_hostname=True`、`minimum_version=TLSv1_2`；证书路径来自 manifest + `CW_PKI_DIR` |
| Rust `cw`/`cw-client` | 新增 `reqwest`（`rustls-tls`，关闭系统根证书）或 `hyper-rustls`；CA pin 与客户端证书同源 |
| 证书定位 | 优先 env（`CW_CLIENT_CERT`/`CW_CLIENT_KEY`/`CW_CA_CERT`），其次 `~/.callwarden/pki/identities/<principal>/`，再次 manifest 指定目录；缺失 → `E_MTLS_CLIENT_CERT_MISSING` |
| 主机名 | 远端部署必须用证书 SAN 中的 DNS 名连接；用 IP 连接时 SAN 必须含该 IP |

错误码（新增，稳定字符串）：

```
E_TLS_REQUIRED
E_TLS_HANDSHAKE_FAILED
E_TLS_SAN_MISMATCH
E_MTLS_CLIENT_CERT_MISSING
E_MTLS_CLIENT_CERT_REJECTED
E_MTLS_CERT_EXPIRED
E_MTLS_CERT_REVOKED
E_MTLS_UNKNOWN_CA
E_PRINCIPAL_UNKNOWN
E_PRINCIPAL_NOT_AUTHORIZED
E_PRINCIPAL_WORKSPACE_FORBIDDEN
E_ENROLL_TOKEN_INVALID
E_ENROLL_TOKEN_EXPIRED
E_ENROLL_TOKEN_USED
E_ENROLL_CSR_INVALID
E_HTTP_BIND_REMOTE_NOT_ALLOWED
E_REMOTE_PATH_FORBIDDEN
E_REMOTE_DB_PATH_FORBIDDEN
```

---

## 8. 远端文件面（S2/S3）

### 8.1 只读（S2）

- `file.read` / `file.grep` / `file.list` / `build_graph` / `build_directory` 在**客户端本机**
  执行；客户端把结果（内容或结构化结果）经 RPC 上传。
- 代码图谱：客户端在本地构建 codegraph DB，然后 `snapshot.upload` 上传（分块或内存
  gzip+base64），daemon 校验 `sha256`、大小、manifest 后导入自己的快照存储。
- `snapshot.publish` 的 `db_path` 参数在远端 profile 下返回
  `E_REMOTE_DB_PATH_FORBIDDEN`；FD 分支仅在同机 Unix 且显式 local 身份时保留（S4 删除）。
- daemon 只提供索引查询（symbols/callers/callees/impact 等），不执行任何文件读取。

### 8.2 写治理（S3）

```
1. 客户端（已 mTLS）请求 `write.grant`：
   { workspace_id, task_id, step_id, path(workspace-relative), base_hash, intent }
2. daemon 校验 principal 授权 + task/step 归属 + base_hash 与当前索引一致
   → 返回 { grant_id, expires_at(≤15min), base_hash, allowed_paths }
3. 客户端本机写入文件（原子替换），计算 new_hash
4. 客户端 `write.commit`：{ grant_id, new_hash, delta(或 full content), request_id }
5. daemon 校验 grant 未过期/未消费/principal 匹配/base_hash 仍一致
   → 写审计、更新索引、消费 grant；不一致 → 拒绝且不更新索引
```

fail-closed 条件：

| 条件 | 错误码 |
| --- | --- |
| grant 过期/已消费 | `E_WRITE_GRANT_EXPIRED` / `E_WRITE_GRANT_CONSUMED` |
| principal 与 grant 不匹配 | `E_WRITE_GRANT_PRINCIPAL_MISMATCH` |
| `base_hash` 与 daemon 索引不一致 | `E_WRITE_BASE_HASH_MISMATCH` |
| 路径越出 workspace 或不在 grant allowed_paths | `E_WRITE_PATH_FORBIDDEN` |
| 并发第二个提交使用同一 base_hash | `E_WRITE_STALE_BASE` |
| task/step 已关闭或不属于该 principal | `E_WRITE_TASK_FORBIDDEN` |

- 同一文件并发写：`write.grant` 对 `(workspace_id, path)` 加逻辑租约；后到者等待或拒绝
  （默认拒绝，`E_WRITE_LOCKED`）。
- 写入完成后必须回传新内容的 hash 与增量；daemon 不复算文件内容，只校验 hash 链。

---

## 9. 删除与打包

| 对象 | 阶段 | 动作 |
| --- | --- | --- |
| `rust_ext/src/bin/cw_bridge.rs` + `[[bin]] cw-bridge` | S1 | 删除源码/构建目标；WSL 场景改直连 HTTPS |
| Named Pipe / UDS 运行时分支 | S4 | 删除 `cli/runtime.rs` 分支、`daemon/transport_windows.rs` peer SID 逻辑、UDS framing |
| `tests/test_windows_bridge_e2e.py`、`test_wsl_authority_routing.py`、`test_windows_wsl_authority_e2e.py` | S4 | 改造成 HTTPS 跨 OS 矩阵或移除 |
| Python `UnixDaemonRpcClient` / `daemon_mode=local` | S4 | 删除本地分支；只保留 HTTP(S) client |
| `scripts/refresh_shared_runtime.ps1` 中的 bridge 产物 | S1/S4 | 同步清理 |
| 发布产物 | S4 | 收敛为 `cw`、`cw-daemon`、`cw-client`（+ Python wheel） |

---

## 10. 分期

### S1 · mTLS 控制面（父卡子卡 1）

范围：daemon TLS、内置 CA、`cw pki` CLI、principal 层、Python 客户端证书、manifest v2、
删除 `cw-bridge`。

关键落点：`rust_ext/src/daemon/http_server.rs`、新增 `rust_ext/src/daemon/pki*.rs` /
`auth_principal.rs`、`rust_ext/src/cli/*`（pki 子命令）、`rust_ext/Cargo.toml`、
`server/daemon_client.py`、`server/mcp_server.py`、`cli/main.py`、`db/schema.py`（PKI 表）、
`tests/test_*mtls*.py`。

验收：见 backlog §38.1。强制负例：无证书、错 CA、错 SAN、过期、吊销、非 loopback 无
`--allow-remote`、明文端口不存在、token 重放、跨 workspace principal。

### S2 · 远端只读（子卡 2）

范围：`snapshot.upload`、客户端本机 `file.read/file.grep/file.list/build_graph`、
daemon 拒绝远端 `db_path`。

验收：Windows↔WSL、Windows↔Linux 两个方向各完成一次只读调用 + 快照上传 + 索引查询；
`db_path`/绝对路径负例返回稳定错误码。

### S3 · 远端写治理（子卡 3）

范围：`write.grant` / `write.commit` / 审计、索引更新、并发锁。

验收：见 backlog §38.3；冲突、越权、过期、重复提交全部 fail-closed；审计链可回放。

### S4 · 清理与矩阵（子卡 4）

范围：删除 pipe/UDS/bridge 残留、Python local 分支、测试夹具；把跨 OS 矩阵接入
`scripts/check_e2e_matrix_closure.py`。

验收：全仓 `grep` 无生产 pipe/UDS/bridge 代码路径；矩阵脚本退出码 0；发布产物只含
HTTPS 客户端。

依赖：S2/S3/S4 均依赖 S1；S3 依赖 S2 的客户端执行框架；S4 依赖 S1–S3 全部合入。
**执行方式：严格串行（S1→S2→S3→S4），禁止并行 checkout；共享文件
`server/daemon_client.py`、`server/tools/`、`cli/main.py` 由当前阶段独占，**
前一阶段 adjudicate 闭环后才派发下一张卡（`task_dependencies` 为空，顺序由生命周期与派工保证）。

---

## 11. 验收矩阵

完整可执行矩阵见 `docs/design/cli-mcp-matrix-backlog.md` §38。总门禁：

```powershell
python scripts/check_e2e_matrix_closure.py
```

各阶段另加：

| 阶段 | 命令 |
| --- | --- |
| S1 | `pytest tests/test_mtls_control_plane.py -q`；`cargo test --manifest-path rust_ext/Cargo.toml tls` |
| S2 | `pytest tests/test_remote_read_plane.py -q`（Windows↔WSL/Linux 由环境变量选择方向） |
| S3 | `pytest tests/test_remote_write_governance.py -q` |
| S4 | `pytest tests/test_transport_single_track.py -q` + 矩阵脚本 |

---

## 12. 风险与残余风险

| 风险 | 处置 |
| --- | --- |
| 证书分发成本高（每 agent 一张） | `cw pki enroll` 一条命令；token 由 owner 批量签发；文档给出脚本模板 |
| Windows 证书/私钥 ACL 易被同用户读取 | 0600 等价 ACL；文档声明同用户进程不可隔离；推荐每 agent 独立 OS 用户/容器 |
| 时钟偏移导致证书误判 | 允许 5 分钟 skew；文档要求 NTP |
| 远端部署 DNS/证书 SAN 不匹配 | 启动时 SAN 预检 fail-closed，错误信息列出缺失 SAN |
| 旧客户端/旧 CLI 连不上 | manifest v2 + 明确最低版本；S1 不提供回退开关（单轨决策） |
| Python `urllib` 对客户端证书支持有限 | 统一用 `ssl.SSLContext` + opener；测试覆盖双向认证；必要时引入 `httpx`（S1 决策点） |
| agent 用 shell 直改 DB/文件 | 文档明示无法阻止；部署层用 ACL/磁盘加密/独立用户；S3 审计只覆盖 RPC 路径 |

---

## 13. 回滚

- S1：保留旧二进制与旧 manifest；回滚 = 停新 daemon、启旧 daemon、旧客户端继续用
  pipe/UDS。**但** S1 合入后 `cw-bridge` 已删除，WSL 用户必须同步回滚。
- S2/S3：新增方法为独立路由；回滚只需客户端不发新方法，daemon 保留兼容读路径。
- S4：删除类变更不可逆，回滚 = revert commit；无数据迁移（PKI 表保留）。
- 数据库：PKI 表为新增表，无破坏性迁移；吊销记录只追加。

---

## 14. 未决问题（不阻塞 S1）

1. ~~是否引入 `httpx` 替代 `urllib`~~ **已决（2026-11-04）：S1 保持 `urllib.request` +
   `ssl.SSLContext`，零新增运行时依赖**；仅当双向认证/证书链测试被 stdlib 阻塞时，
   再单独评估引入 `httpx` 并同步 `requirements.txt` 与打包声明。
2. 外部 CA 导入的配置面与 UI。
3. 证书热加载（不重启轮换服务端证书）。
4. 多租户/多组织 CA 分层（当前单 CA 足够）。
5. `write.commit` 的 delta 格式（unified diff vs 全量内容 + hash）。
