# HTTP 安全面演进设计：从 dev_loopback_unauthenticated 到身份层（Security Profile v2/v3）

> 来源：`docs/reports/client_convergence_codereview_20260909.md` P2-CR9（登记备忘，不阻塞收敛目标）。
> 状态：**design-only**（本文档只定义方案与迁移路径，不改变现行行为；实现须按 §7 分期立项）。
> 日期：2026-09-09。作者：修复 agent（CR9 承接）。

## 1. 现状（Profile v1 基线，代码事实）

现行 HTTP transport 安全面 = `dev_loopback_unauthenticated`（`http_server.rs` `SECURITY_PROFILE` 常量）：

| 面 | 现状 |
| --- | --- |
| 绑定 | 仅 loopback（`resolve_loopback` 强校验，非 loopback bind 直接 `E_HTTP_MVP_LOOPBACK_ONLY` fail-closed，manifest 不发布） |
| 认证 | 无。所有请求合成 `synthetic_local_owner_peer()`（= daemon 自身 owner 的 SID/uid + daemon PID） |
| 身份隔离 | 无 OS 级 peer cred（与 named-pipe/UDS 通道不同，TCP 无法取对端 SID） |
| authority 作用域 | manifest 文件名 + `authority_id` 字段按本地用户隔离（Windows 纯 SID / Unix `uid-{uid}`），客户端 `validate_http_manifest` 校验 authority 一致性 |
| 授权 | capability registry（`http-mvp-cap-registry-v1`，路由/operation_class 白名单）之上无身份层——**registry 回答"哪些方法可用"，不回答"谁在调用"** |
| 传输 | 明文 HTTP（loopback 上无嗅探面，但本机其它用户进程可 loopback 嗅探视内核版本而定） |

## 2. 威胁模型（v1 的实际边界）

**v1 已覆盖**：
- 远程访问：loopback-only 强校验，fail-closed。
- 跨用户误用：manifest authority 校验使客户端只发现"自己用户"的 daemon。

**v1 未覆盖（CR9 登记的缺口）**：
1. **同机其它低权进程**：任何本机进程均可 connect 127.0.0.1 并以 daemon owner 身份执行全部 RPC（含 `task.apply/close`、workspace 注册、codegraph 写入）。威胁=同机恶意/被攻陷进程横向提权到 daemon owner 权限。
2. **多 agent 共享 daemon**：多 agent（WorkBuddy/Claude/Codex 等并存）经同一 HTTP endpoint 调用时，彼此不可区分——全部映射为同一个合成 peer，治理事件的 `role`/`agent_id` 之外**无传输层身份**，lease/identity 门禁只能依赖应用层自报身份。
3. **跨机场景（未启用）**：若未来 237 工具 HTTP 化开放到局域网/远程，v1 直接不可用（loopback 校验会拒绝，但那时需要的是完整身份层而非仅放开绑定）。

判定（与报告一致）：当前拓扑（单用户开发机、单 owner）下 1/2 为可接受残留风险；**条件触发面**是多 agent 并存常态化与跨机需求。

## 3. 目标 Profile 枚举（演进路径）

```
dev_loopback_unauthenticated        ← v1（现行）
loopback_token_authenticated        ← v2（本设计主体：同机身份层）
cross_host_mutual_tls               ← v3（跨机，仅登记方向，不展开）
```

原则：
- **fail-closed**：新 profile 解析失败/凭据缺失一律拒绝，绝不静默降级到 v1。
- **profile 可观测**：manifest、`/health`、`/capabilities` 均暴露 `security_profile` 字段，客户端可按 profile 决定是否附带凭据。
- **能力先于暴露**：任何放宽绑定的 profile 必须先具备身份层，顺序不可倒置。

## 4. Profile v2 设计：loopback_token_authenticated

### 4.1 凭据生命周期

- **生成**：daemon 首次以 v2 启动时生成 256-bit 随机 token（`getrandom`/OS CSPRNG），落地 `~/.callwarden/http-daemon.<authority_id>.token`，权限 **0600 / Windows 仅 owner ACL**（与 manifest 同目录、同 authority 命名规则，复用 `http_manifest_filename` 的转义逻辑）。
- **轮换**：每次 daemon 重启重新生成（旧 token 立即失效）；写 token 与写 manifest 在同一个 prebind 原子窗口内完成（CR10 已建立的 prebind 时序直接复用）。
- **分发**：不进 manifest（manifest 可能被低权读取面触达）；客户端按约定路径读 token 文件（owner-only），读不到 → 按该 profile fail-closed 报 `E_HTTP_TOKEN_UNAVAILABLE`。

### 4.2 传输与校验

- 客户端在每个 JSON-RPC 请求附 `Authorization: Bearer <token>`（/capabilities、/health 豁免——探测面保持匿名可读，但不泄露方法面）。
- daemon 在 axum middleware 层校验（`/v1/rpc` 及全部 mutation 路由）：
  - **constant-time 比较**（`subtle` 或手写 XOR 累积），防时序侧信道；
  - 失败 → HTTP 401 + `{"code": "E_HTTP_UNAUTHENTICATED"}`，**不进入 dispatch**，capability registry 不触达；
  - middleware 位于 router 构建处（`build_router`），一层覆盖全部路由，方法级白名单不重复实现。
- 传输仍为明文 loopback：token 对同 owner 进程本就可读，明文不扩大威胁面（威胁模型 §2.1 的对手是"非 owner 进程"，其读不到 token 文件）。

### 4.3 身份层与 PeerCredential 映射

v2 的关键升级：**从"合成单 peer"到"凭据→身份映射"**：

- token 文件旁带 `token_meta`（同 0600）：`{token_hash, issued_at, profile, agent_default_role}`。
- **多 agent 子 token（可选扩展，同一机制）**：daemon 提供 `auth.token.issue`（owner peer 专用 RPC，经 named-pipe/UDS 通道调用）签发子 token，每枚绑定 `{agent_id, allowed_roles, expires_at}`，落 `tokens/` 目录。HTTP middleware 校验主 token/子 token 后，将 `PeerCredential` 的映射交给 dispatch：
  - 主 token → 现行 `synthetic_local_owner_peer()`（行为不变）；
  - 子 token → 合成 peer 携带 `agent_id` 派生的稳定 uid 域（映射函数冻结进 schema 注释），使治理事件、lease、identity 记录可区分调用方 agent。
- capability registry 之上的关系：registry 继续回答"方法→route/operation_class"；身份层回答"调用方→peer"；二者在 dispatch 汇合后由既有 `is_protected_mutation`/owner-key 逻辑做每方法授权。**不把授权塞进 registry 行**（避免 registry 从路由面演化为 ACL 面，保持 H1 冻结语义）。

### 4.4 配置面

- `CW_DAEMON_HTTP_PROFILE=loopback_token_authenticated`（env，缺省仍 v1）；显式指定 v2 时 token 生成/校验全链激活。
- manifest/health 新增 `security_profile` 实际值（v1 下现值不变）；客户端 `HttpDaemonRpcClient` 按 profile 自动附带 Bearer（profile 读取自 manifest，客户端不新增配置项）。

## 5. Profile v3（方向登记，不展开）

跨机 = 绑定放宽 + 传输加密 + 双向身份，三者必须同时成立：mTLS（daemon 侧证书由 owner 签发 agent 证书）、绑定改为显式指定非 loopback 地址（移除 loopback 强校验的 profile 分支）、每方法授权引入网络分区信任域。当前无触发需求，仅禁止"只放开绑定不加密"的实现路径。

## 6. 验收标准（实现时逐条对应）

1. v2 模式下：无 token 请求 → 401 `E_HTTP_UNAUTHENTICATED`，daemon 日志含拒绝原因；capability registry 不可达。
2. 错误 token（含 1-bit 翻转）→ 401；正确 token → 与 v1 行为逐位一致（回归对比法）。
3. token 文件权限：Windows `icacls` 仅 owner+SYSTEM；POSIX stat 0600。非 owner 用户读 token → 拒绝（OS 层验证）。
4. daemon 重启 → 旧 token 失效（401）、新 manifest+token 同窗口原子生效（复用 CR10 prebind 测试骨架）。
5. 子 token：过期 → 401；`allowed_roles` 外的治理 mutation（如 verdict.submit）→ dispatch 层拒绝（E_ROLE_*，非 401——身份成立、授权不成立）。
6. 回归测试：middleware 单测（constant-time 比较锚点、豁免路由清单）+ prebind 集成测试扩展。

## 7. 分期与承接

| 阶段 | 内容 | 规模 |
| --- | --- | --- |
| S1 | 主 token + middleware + 配置面 + manifest profile 字段（§4.1/4.2/4.4，无子 token） | 小（单 PR：http_server.rs + daemon_client.py 各一处） |
| S2 | 子 token 签发 RPC + PeerCredential 映射 + 治理事件区分 agent | 中（触及 dispatch/token 存储，需 schema 审视） |
| S3 | 默认 profile 翻转为 v2 + v1 保留为显式逃生门 | 小（翻转 + 文档 + 兼容窗口一个版本） |

触发条件（满足其一即启动 S1）：多 agent 并存调用 daemon 成为常态；出现同机不可信进程共存的部署形态；237 工具 HTTP 化立项。

## 8. 非目标

- 不解决 Python compat worker 通道的安全面（其经 stdin/stdout 由 daemon 拉起，信任域同 daemon）。
- 不引入外部 IdP/OAuth——单机工具链，OS 用户边界 + 文件权限即信任根。
- 不改变 named-pipe/UDS 通道（已有 OS peer cred，安全面强于 HTTP v1/v2）。
