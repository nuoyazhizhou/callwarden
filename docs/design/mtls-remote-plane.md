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
  `principal_id` + `allowed_workspace_ids`；实测 186 处 / 10 文件（见 §15.1），逐处处置。
- Python：服务器侧 48 处 / 4 文件，另有 `cli/main.py` 4 处；`daemon_mode=local`
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
| daemon | `tokio-rustls` 自定义 accept 循环（`hyper-util` 承载 axum Router）；`rustls` 作为唯一 TLS 后端；HTTP/1.1 + ALPN `http/1.1`；逐连接 `max_conn_age` |
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

> **协议冻结**：upload 的分块/内联格式、hash 契约、大小上限、幂等键、拒绝错误码、
> 客户端执行工具清单与返回形态，全部冻结于 §16（S2 实现与验收的唯一契约源）。

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
| S4 | `pytest tests/test_single_track_kernel.py -q` + 矩阵脚本（S4-01/S4-03 由 `test_s4_01`/`test_s4_03` 承载） |

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

---

## 15. S1 现状盘点与冻结（inventory 证据）

盘点基线：commit `f1aec7b`，工作树 clean（`git status --porcelain` 空输出）。

检索命令（仓库根执行）：

```powershell
rg -c "peer_uid|current_uid" rust_ext/src   # 186 处 / 10 文件
rg -c "peer_uid|current_uid" server         # 48 处 / 4 文件
rg -c "peer_uid|current_uid" cli            # 4 处 / 1 文件
```

处置代号：

- **A** 授权主语迁移：比较/过滤键从 `peer_uid`/`owner_uid` 改为 `principal_id` +
  `workspace_principal_grants`
- **D** 诊断保留：日志、错误消息、ping 元数据、数据库诊断列；不参与任何授权判断（I3）
- **X** 删除：随 pipe/UDS/memfd/bridge 在 S4 清理，S1 不再新增调用点
- **P** Python 兼容层：保留函数签名，内部不再以 uid 做授权；S4 随 local daemon 删除
- **T** 测试夹具：随实现同步改写，不保留 uid 授权断言

### 15.1 peer_uid / current_uid 逐文件处置

Rust（`rust_ext/src`，186 处）：

| 文件 | 处数 | 主要用途 | 处置 |
| --- | --- | --- | --- |
| `daemon/workspace.rs` | 63 | `owned_workspace` / `owned_workspace_any_status` / `owned_workspace_by_id` / `check_path_owner` / `validate_owned_path[_any]` 的 owner 比较；测试夹具 | A：4 个授权函数改 principal grant；`check_path_owner` 降级为本地 defense-in-depth（D）；其余 T/D |
| `daemon/memfd.rs` | 40 | Unix FD `st_uid == peer_uid` 属主校验与测试 | D：校验保留为诊断防线；X：FD 传输路径 S4 删除 |
| `cli/external.rs` | 19 | ping 取 `peer_uid` → 按 `owner_uid` 过滤候选；`peer_uid==0` / 未知时的 admin 旁路 | A：改为 principal 可见 workspace 集合；删除 admin 旁路（I3）；其余 D |
| `daemon/task_collab.rs` | 14 | `verify_task_binding_owner` / `verify_registry_owner_for_instance` 的 owner 比较 | A：改 principal + task assignment 归属 |
| `daemon/server.rs` | 10 | `ping` 响应 `peer_uid` 测试 | T/D：测试断言改 principal |
| `daemon/snapshot_state.rs` | 10 | `owned_workspace(peer_uid)` 调用、`validate_snapshot_fd` | A：workspace 校验改 principal；X：FD 校验随 FD 路径 S4 删除 |
| `daemon/dispatch.rs` | 9 | `ping` 返回 `peer_uid`；admin-only 规则 `peer.uid == 0` | D：ping 元数据；A：admin 规则改 owner principal（禁止以 uid==0 授权） |
| `daemon_query.rs` | 9 | PyO3 导出 `validate_owned_path` / `check_workspace_owner` / `current_uid` | P/D：兼容签名保留，内部改 principal；S4 删除 |
| `daemon/peercred.rs` | 6 | `current_uid()` 与测试 | D：仅诊断；新增 principal/serial/fingerprint 字段 |
| `daemon/replicator.rs` | 6 | 会话表 `peer_uid` 列写入 | D：诊断列保留；X：S4 随 replicator 删除 |

Python（`server/` 48 处 + `cli/main.py` 4 处）：

| 文件 | 处数 | 主要用途 | 处置 |
| --- | --- | --- | --- |
| `server/daemon_server.py` | 35 | `_owned_workspace*`、`_validate_owned_path`、`_validate_snapshot_fd`、ping uid、dispatch 透传 | P/A：内部改 principal（HTTP 模式不经过此模块）；X：S4 随 pipe/UDS 删除 |
| `server/ipc_transport.py` | 6 | memfd 属主校验与参数 | D → X（S4） |
| `server/replicator.py` | 6 | 会话表 `peer_uid` 列 | D → X（S4） |
| `server/agent_protocol.py` | 1 | 文档字符串示例 | D |
| `cli/main.py` | 4 | `cw daemon ping` 展示 `peer_uid` | D：仅展示，不参与授权 |

收口结论：**授权路径上的 uid 比较共 7 处函数/分支** —— `owned_workspace`、
`owned_workspace_any_status`、`owned_workspace_by_id`、`check_path_owner`（含
`validate_owned_path[_any]`）、`verify_task_binding_owner`、
`verify_registry_owner_for_instance`，以及 `daemon/dispatch.rs` 的 admin 旁路。
S1 完成后这些路径只读 `PeerCredential.principal_id` / `allowed_workspace_ids`；
`peer_uid` 仅出现在日志、错误消息、ping 与诊断列（I3）。
Review 检查项：授权分支中不得出现 `peer_uid`/`owner_uid` 比较（`rg "peer_uid|owner_uid"`
逐处确认命中的是诊断/测试/删除代码）。

### 15.2 Cargo 依赖冻结

新增到 `rust_ext/Cargo.toml` `[dependencies]`（版本为本机 registry 实测）：

| crate | 版本 | feature | 用途 |
| --- | --- | --- | --- |
| `rustls` | 0.23.44 | `default-features=false`, `ring`, `std`, `tls12`, `logging` | TLS 唯一后端 |
| `tokio-rustls` | 0.26.5 | `default-features=false`, `ring`, `tls12`, `logging` | async TLS acceptor |
| `rustls-pemfile` | 2.2 | 默认 | PEM 证书/私钥读取 |
| `rcgen` | 0.14.10 | 默认（`crypto`,`pem`,`ring`） | CA / 服务端 / 客户端证书签发 |
| `x509-parser` | 0.18.1 | 默认 | 解析客户端证书 SAN URI / serial / 有效期 |
| `hyper-util` | 0.1.20 | `server-auto`, `tokio`, `service` | 自定义 accept 循环内的 `serve_connection` |

机械约束：

1. **不引入系统根信任**：不添加 `rustls-platform-verifier`、`webpki-roots`、
   `openssl`；daemon 只信任 `pki/ca/ca.crt`，客户端只信任 manifest pin 的 CA。
2. **不引入 `aws-lc-rs`**（避免 Windows 上 cmake/nasm 构建链）：三个 crypto crate
   全部走 `ring`；进程启动安装一次 `rustls::crypto::ring::default_provider()`，
   重复安装返回 `AlreadyInstalled` 视为正常。
3. **不引入 `axum-server`**：`max_conn_age` 与吊销集热更新需要逐连接控制，改用
   `tokio-rustls` 自定义 accept 循环；HTTP 层仍是现有 axum 0.8 Router。
4. **S1 不引入 `reqwest`/`hyper-rustls`**：Rust 客户端的第一步（`cw pki enroll`）
   允许复用 Python/`urllib` 等价实现或薄封装；若后续步骤必须引入，必须
   `rustls-tls` + `default-features=false` 并关闭系统根证书，且在本表追加记录。
5. 工具链：本机 `rustc 1.93.1`；rcgen 0.14.10 最低 1.88，满足。

### 15.3 错误码冻结（S1 实施面）

稳定字符串，大小写敏感。S1 实现前五组；路径类两组仅为 S2/S3 预留，
S1 不得提前返回：

| 组 | 错误码 | S1 |
| --- | --- | --- |
| TLS | `E_TLS_REQUIRED`、`E_TLS_HANDSHAKE_FAILED`、`E_TLS_SAN_MISMATCH` | 实现 |
| mTLS 证书 | `E_MTLS_CLIENT_CERT_MISSING`、`E_MTLS_CLIENT_CERT_REJECTED`、`E_MTLS_CERT_EXPIRED`、`E_MTLS_CERT_REVOKED`、`E_MTLS_UNKNOWN_CA` | 实现 |
| principal | `E_PRINCIPAL_UNKNOWN`、`E_PRINCIPAL_NOT_AUTHORIZED`、`E_PRINCIPAL_WORKSPACE_FORBIDDEN` | 实现 |
| 引导 | `E_ENROLL_TOKEN_INVALID`、`E_ENROLL_TOKEN_EXPIRED`、`E_ENROLL_TOKEN_USED`、`E_ENROLL_CSR_INVALID` | 实现 |
| 绑定 | `E_HTTP_BIND_REMOTE_NOT_ALLOWED` | 实现 |
| 远端路径 | `E_REMOTE_PATH_FORBIDDEN`、`E_REMOTE_DB_PATH_FORBIDDEN` | 预留（S2/S3） |

- 既有 `E_HTTP_MVP_LOOPBACK_ONLY` 仅保留为旧 profile 的显式非法配置诊断，
  不再是任何合法路径。
- 握手失败对外只回 `E_TLS_HANDSHAKE_FAILED`；细分原因写 daemon 审计日志，
  不回显给调用方（避免 oracle）。
- `/v1/enroll` 的 token 失败统一回 `E_ENROLL_TOKEN_INVALID`（ADR §4.2）。

### 15.4 manifest v2 字段冻结

`callwarden-http-manifest/v2`，按 authority 分文件、0600 / owner-only ACL、
临时文件 + 原子 rename：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema` | string | 固定 `"callwarden-http-manifest/v2"`；读到 v1 视为过期客户端，不降级 |
| `security_profile` | string | 固定 `"https_mtls_v1"` |
| `endpoint` | string | `https://<host>:<port>`，绑定成功后写入实际端口 |
| `bind_scope` | string | `loopback` 或 `remote`（后者必须 `--allow-remote`） |
| `auth` | string | 固定 `"mtls_client_cert_required"` |
| `tls.min_version` | string | 固定 `"1.2"` |
| `tls.ca_cert_pem` | string | daemon CA PEM |
| `tls.ca_fingerprint_sha256` | string | 64 位小写 hex |
| `tls.server_cert_sha256` | string | 64 位小写 hex |
| `tls.server_not_after` | string | RFC3339 UTC |
| `tls.san` | string[] | 覆盖实际绑定地址；缺失 → `E_TLS_SAN_MISMATCH` |
| `daemon_pid` | number | 当前进程 PID |
| `git_commit` | string | 运行时 `git rev-parse HEAD` |
| `capability_registry_revision` | string | 现为 `http-mvp-cap-registry-v1` |

- manifest 不含私钥、principal 列表、grant、workspace 路径。
- `/health` 仅暴露 profile/commit/pid/capability revision；`/capabilities` 与
  `/v1/rpc` 需客户端证书。
- 旧 `CW_DAEMON_HTTP_PROFILE=dev_loopback_unauthenticated` 与任何“关闭 TLS”
  开关视为非法配置，启动即失败。

### 15.5 盘点结论

- 单轨 mTLS 的依赖面收敛为 6 个 crate，全部 `ring`、无系统根、无 `aws-lc-rs`。
- uid 授权点共 7 处，S1 必须全部切到 principal；其余 179 处 Rust / 48 处 Python
  为诊断、测试或待删除代码。
- 未发现与 ADR §3.1 冲突的额外库存；本盘点不新增威胁面。

---

## 16. S2 协议冻结（snapshot.upload 与远端只读面）

> 冻结时间：2026-11-09；归属卡 `[REMOTE-S2]`（`T-1793774130739-6e096a14`）。
> 本节是 S2 实现与验收的**唯一契约源**；§8.1 的 ADR 级概述以本节为准。
> 矩阵口径见 `docs/design/cli-mcp-matrix-backlog.md` §38.2（S2-01…S2-09）。

**实施基线事实**（冻结前摸查证据，作为契约的可实施性依据）：

- `snapshot.publish`（`rust_ext/src/daemon/snapshot_state.rs:698`）必填 `db_path`，
  daemon 直接 attach 客户端给定 SQLite 路径；`build_and_publish_blocking`
  （`rust_ext/src/snapshot.rs:267`）从该路径的 `file_instances`/`symbols`/`calls`
  三表装载发布（DDL 见 `rust_ext/src/storage.rs`），**不复制 DB**。
- Python 侧 codegraph builder 已存在：`db/db_build.py` 的 `BuildMixin`
  （`build` / `build_full_graph` / `build_directory` / `refresh_file` …），
  客户端本机构建工作库**无需重写 Rust 解析逻辑**。
- 既有上限：daemon `MAX_BODY_BYTES = 64 MiB`（`http_server.rs:68`），
  Python 通用 body 预检 `HTTP_MAX_BODY_BYTES = 8 MiB`（`config.py:1776`）。
- 既有幂等面：HTTP 层 `http_dedup`（PK = `(workspace_instance_id, method,
  request_id)`，24h 保留，`http_server.rs:254-336`）；`snapshot.publish` 属
  PROTECTED_MUTATION，已受其覆盖。
- 既有错误码：`E_REQUEST_TOO_LARGE`（413/预检）、`E_PAYLOAD_TOO_LARGE`
  （`http_server.rs:1301`）、`E_HTTP_DAEMON_UNAVAILABLE`（客户端连接失败，
  `daemon_client.py:76`）。

### 16.1 profile 判定与路径拒绝（I4 落地）

判定键：daemon 启动时的 **`bind_scope`**（manifest v2 字段，§15.4），运行时只读、
不可变更；客户端从同一 manifest 读取，保证两端单一真源。

- `bind_scope=loopback`：**同机语义**，`snapshot.publish` 的 `db_path` 兼容路径保留
  （既有行为，回滚兼容）。
- `bind_scope=remote`（`--bind <非 loopback> --allow-remote`）：以下 fail-closed：

| 触发 | 错误码 | 约束 |
| --- | --- | --- |
| `snapshot.publish` params 含 `db_path` | `E_REMOTE_DB_PATH_FORBIDDEN` | daemon 不 open/attach 任何客户端路径；落审计 |
| 任何 RPC params 含绝对路径（Posix `/` 前缀、Windows 盘符/UNC/`\` 前缀）或 `host_real_root` | `E_REMOTE_PATH_FORBIDDEN` | 路径解析只发生在客户端；daemon 无法验证跨 OS 路径，上送即拒 |
| FD / multipart 载荷分支（`snapshot.publish`） | method_not_found | 单轨 HTTPS 无 FD（既有 `snapshot_state.rs:752` 语义） |

- 矩阵口径（S2-08）：Windows↔WSL 与 Windows↔Linux 均以**对端 daemon 绑定
  LAN/hostname + `--allow-remote`**（`bind_scope=remote`）为准；loopback 仅承载
  同机语义（client 与 daemon 可见同一文件系统）。
- **S2-05 落点**：远端 profile 下，只读工具的绝对路径参数由**客户端工具层先行
  fail-closed**（返回同一 `E_REMOTE_PATH_FORBIDDEN` 结构化错误，不发起 RPC）；
  直连 RPC 的绝对路径参数由 daemon 兜底拒绝。客户端本机路径一律以
  workspace 相对位置表达。

### 16.2 `snapshot.upload` 协议（新增 RPC，remote profile 唯一快照入口）

**内容载体**：codegraph 工作库 SQLite 字节流，gzip 压缩。schema 契约 =
`GraphStore::load_from_sqlite_blocking`（`rust_ext/src/graph.rs:1980`）消费的三表：
`file_instances`（`id, rel_path, status, …`）、`symbols`（`id, file_instance_id,
kind, name, qualified_name, module_path, start_line, end_line, depth`）、
`calls`（`caller_id, callee_id, callee_name, call_line, is_cross_file`）。
上游 builder（Python `db/db_build.py` BuildMixin 等价路径或本机 `cw` CLI）必须产出
该 schema；多余表忽略；缺表/缺列 → `E_SNAPSHOT_UPLOAD_SCHEMA_INVALID`。

**hash 契约**：`sha256` 与 `raw_size` 均针对**原始 SQLite 字节流**（gzip 解压后）；
gzip 流与 base64 编码不参与 hash。任何 hash/size 不符 →
`E_SNAPSHOT_UPLOAD_HASH_MISMATCH`，不导入、不残留（S2-06 篡改/截断防御）。

**内联格式**（单请求，`snapshot.upload`）：

```
params = {
  "workspace_instance_id": str,     // 必填；归属校验同 snapshot.publish 既有路径
  "mode": "inline",                 // 固定
  "content_encoding": "gzip",       // 固定 "gzip"（"identity" 为预留，本卡不实现）
  "payload_b64": str,               // base64(gzip(sqlite bytes))
  "sha256": str,                    // 64 位小写 hex，sqlite bytes 摘要
  "raw_size": int,                  // sqlite bytes 字节数
  "build_context_hash": str?,       // 可选，语义同 snapshot.publish
  "snapshot_id": str?,              // 可选，与 workspace.register 权威值校验
  "request_id": str                 // 幂等键
}
→ 200 { "generation", "symbol_count", "call_count", "workspace_instance_id",
         "snapshot_id" }            // 与 snapshot.publish 返回同形（S2-03 回显口径）
```

**分块格式**（`snapshot.upload.chunk` + `snapshot.upload.commit`）：

```
snapshot.upload.chunk = {
  "workspace_instance_id": str,
  "upload_id": str,                 // 客户端生成（uuid4 hex），全部分块与 commit 复用
  "chunk_index": int,               // 0-based
  "total_chunks": int,              // ≥2（=1 应走内联）
  "chunk_b64": str,                 // base64(gzip 流的第 i 段字节)；gzip 流按字节切片
  "chunk_sha256": str,              // 该段字节摘要（逐段完整性）
  "request_id": str                 // 幂等键，重传同一分块复用
}
→ 200 { "upload_id", "chunk_index", "chunks_received": int }

snapshot.upload.commit = {
  "workspace_instance_id": str,
  "upload_id": str,
  "total_chunks": int,              // 必须与各 chunk 一致
  "sha256": str,                    // 完整 sqlite bytes 摘要
  "raw_size": int,
  "build_context_hash": str?,
  "snapshot_id": str?,
  "request_id": str                 // 提交级幂等键；Replay 返回首次结果
}
→ 200 同 snapshot.publish 返回形
```

**组装语义**：daemon 按 `chunk_index` 拼接成完整 gzip 流 → gunzip → 校验
`raw_size` → `sha256` → 写 daemon 本机临时文件（owner-only ACL）→
`build_and_publish_blocking(temp_path, …)`（复用既有发布路径）→
发布后删除临时文件。任一环节失败：不发布、不落库、不残留临时文件。

**大小上限**（冻结）：

| 项 | 上限 | 超限 |
| --- | --- | --- |
| 单请求 HTTP body | 64 MiB（既有 `MAX_BODY_BYTES`） | HTTP 413 → 客户端 `E_REQUEST_TOO_LARGE` |
| 内联 `payload_b64` 解码后 | 32 MiB | `E_PAYLOAD_TOO_LARGE` |
| 单分块 `chunk_b64` 解码后 | 16 MiB | `E_PAYLOAD_TOO_LARGE` |
| 组装后 raw sqlite bytes | 512 MiB | `E_PAYLOAD_TOO_LARGE` |
| 分块数 `total_chunks` | 4096 | `E_PAYLOAD_TOO_LARGE` |

- 上传面的客户端 body 预检按上表 64 MiB 口径，**不复用**通用
  `HTTP_MAX_BODY_BYTES = 8 MiB`（upload 单独口径）。
- S2-07 口径：接近 64 MiB body 的上传要么成功，要么 `E_PAYLOAD_TOO_LARGE`；
  **禁止截断入库**。

**幂等键**（复用 `http_dedup`，PK = `(workspace_instance_id, method, request_id)`，
24h 保留）：

- `snapshot.upload`（内联）与 `snapshot.upload.commit`：同 `request_id` + 相同
  params → Replay 返回首次结果；同 `request_id` + 不同 params →
  `E_REQUEST_ID_REUSE_MISMATCH`（既有语义，`config.py:1822`）。
- `snapshot.upload.chunk`：同 `request_id` 重传 → Replay，不重复写盘；乱序到达按
  `chunk_index` 归位。
- `commit` 时分块不齐 → `E_SNAPSHOT_UPLOAD_CHUNK_MISSING`。
- `upload_id` 生命周期：自首块到达起 2h 有效；超时 daemon 清理未提交的分块存储；
  commit 命中已过期 `upload_id` → `E_SNAPSHOT_UPLOAD_EXPIRED`。

**方法注册**：三个方法名纳入 daemon dispatch、capability registry 与
PROTECTED_MUTATION 幂等面（与 `snapshot.publish` 同级）。

**新增错误码**（稳定字符串；`E_REMOTE_*` 自 §15.3 “预留”转为 S2 实现；
注册于 `rust_ext/src/daemon/error_codes.rs` 与 `server/config.py`）：

```
E_REMOTE_DB_PATH_FORBIDDEN        // §16.1
E_REMOTE_PATH_FORBIDDEN           // §16.1
E_SNAPSHOT_UPLOAD_SCHEMA_INVALID  // 三表 schema 不符
E_SNAPSHOT_UPLOAD_HASH_MISMATCH   // sha256/raw_size 校验失败（S2-06）
E_SNAPSHOT_UPLOAD_CHUNK_MISSING   // commit 时分块不齐
E_SNAPSHOT_UPLOAD_EXPIRED         // upload_id 过期
```

`E_PAYLOAD_TOO_LARGE` / `E_REQUEST_TOO_LARGE` / `E_HTTP_DAEMON_UNAVAILABLE`
复用既有码，不新增。

### 16.3 与既有方法的关系

- `snapshot.publish`：loopback 行为不变；remote profile 下被 §16.1 拒绝，快照入口
  唯一为 `snapshot.upload`（S2-04）。
- `build_graph` / `build_directory`（remote）：客户端用既有 Python builder
  （`db/db_build.py` BuildMixin 等价路径）在本机构建工作库 → 经 `snapshot.upload`
  上传 → 返回同形字段。落库与符号解析在客户端完成；daemon 只接收产物。
  约束：不重写 tree-sitter 解析、不新增 Python 运行时依赖。
- `mcp.common.get_db_path_for_daemon`（remote）：返回 daemon 本机权威任务库路径，
  仅供客户端 publish 注入层使用，**不得**用于客户端路径上送。

### 16.4 客户端本机执行工具清单与返回形态（remote profile）

**执行位置与路径解析**：MCP 宿主进程（`server/tools/`）。路径解析键 = 客户端
workspace 根（`CALLWARDEN_WORKSPACE` → MCP `_project_root`，同 F-5 修复的注入层
口径）；相对路径按该根解析；绝对路径先行 fail-closed（§16.1）。`bind_scope=loopback`
时维持既有 daemon 执行路径不变（回滚兼容，S2 落地以 remote profile 为条件）。

**返回形态**：与 daemon 侧 handler **同构**（MCP 契约不变，S2-02「结果与客户端本地
执行一致」的口径），object 形返回追加 `exec_origin: "client"` 元字段：

| 工具 | 客户端执行体 | 返回字段（daemon 既有 + 追加） |
| --- | --- | --- |
| `file.read` | 本机按行读；`offset`(默认 0)/`limit`(默认 200) 分页 | `file_path, offset, limit, total_lines, content, exec_origin` |
| `file.grep` | 本机正则/glob 过滤；`head_limit`(默认 50) | `matches:[{file_path,line,text}], count, exec_origin` |
| `file.list` | 本机 walk + glob | 裸数组 `[{rel_path,size,is_dir}]`（保持数组契约，不追加字段） |
| `build_graph` | 本机 builder 全量扫描 → `snapshot.upload` | `ok, scanned, inserted, unchanged, skipped_cached, deleted, archived, skipped_unparsed, symbols, calls, calls_resolved, calls_ambiguous, exec_origin` |
| `build_directory` | 本机目录扫描（`recursive` 默认 false）→ 目录子集工作库 → `snapshot.upload` | `ok, scanned, refreshed, symbols, calls, calls_resolved, calls_ambiguous, exec_origin` |

- 已知契约漂移（本卡不处理，登记）：Python tool 多发 `include_context`
  （file.read）/`output_mode`（file.grep），Rust 侧未消费
  （`tools_workspace.py:209/231` ↔ `fs_handlers.rs`）；remote 客户端执行体同样
  不消费，保持与 daemon 侧行为一致。
- **S2-09 落点**：daemon 不可达 / mTLS 握手失败 → 结构化错误
  `E_HTTP_DAEMON_UNAVAILABLE`（含 endpoint 与原因），**禁止**回退本地 SQLite
  直读（I1/I2 单轨 + fail-closed）。

### 16.5 S2 验收映射

| 矩阵行 | 契约落点 |
| --- | --- |
| S2-01 / S2-02 | §16.4 工具表 + `exec_origin` |
| S2-03 | §16.2 全协议 + 返回形 |
| S2-04 | §16.1 `db_path` 拒绝 |
| S2-05 | §16.1 绝对路径拒绝（客户端先行 + daemon 兜底） |
| S2-06 | §16.2 hash 契约 + 组装语义 fail-closed |
| S2-07 | §16.2 大小上限表 |
| S2-08 | §16.1 矩阵口径（Windows↔WSL、Windows↔Linux，对端 `--allow-remote`） |
| S2-09 | §16.4 `E_HTTP_DAEMON_UNAVAILABLE` 无本地降级 |
