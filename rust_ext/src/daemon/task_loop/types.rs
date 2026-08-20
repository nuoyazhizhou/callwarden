//! Executor foundation 私有类型（计划 §3.3：Envelope、DomainOutcome、DomainTx、permit）。
//!
//! 这些类型是 foundation 独占的内部骨架，故意 **不可序列化**（不实现
//! `Serialize`/`Deserialize`）：`invocation_class`、两类 transport 与两类 permit 的
//! 字段都不是客户端 JSON/header 参数，防止伪造覆盖。领域 handler 与 transport 之外
//! 的代码不得依赖或构造它们。

use rusqlite::Transaction;
use std::collections::BTreeMap;

/// 公共能力未激活时所有已知 RPC 的 fail-closed 稳定错误码。
pub const ERR_CAPABILITY_DISABLED: &str = "E_TASK_LOOP_CAPABILITY_DISABLED";
/// 已装载 permit 但最终复核发现 authority/fingerprint 失效时的稳定错误码。
pub const ERR_CAPABILITY_REVOKED: &str = "E_TASK_LOOP_CAPABILITY_REVOKED";
/// 缺少结构化 handoff 字段时的稳定错误码。
pub const ERR_HANDOFF_FIELDS_REQUIRED: &str = "E_HANDOFF_FIELDS_REQUIRED";
/// 请求 id 复用但 canonical 参数不同的稳定错误码。
pub const ERR_REQUEST_ID_REUSE_MISMATCH: &str = "E_REQUEST_ID_REUSE_MISMATCH";

/// Envelope 的调用类别，决定受理路径。
///
/// 私有、不可序列化枚举，**不是**客户端可提交的 JSON/header/params 字段。HTTP /
/// Named Pipe / UDS strict parser 只能构造 `ExternalTransport`；只有 daemon 内不经
/// `dispatch.rs` 的私有 validation API 能构造 `InternalValidation`。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvocationClass {
    /// 经外部传输进来的客户端请求。公共路由只对绑定了 fingerprint 的
    /// `PublicPreflightPermit` 开放。
    ExternalTransport,
    /// 只有 daemon 内私有 validation API 能构造。
    InternalValidation,
}

/// 传给领域 handler 的、已严格解析的不可序列化 Envelope 输入。
///
/// 字段固定为 `(workspace_instance_id, canonical_method, request_id, params,
/// invocation_class)`。客户端无法提交或覆盖 `invocation_class` 及任何 marker。
#[derive(Debug)]
pub struct StrictParsedEnvelope {
    pub workspace_instance_id: String,
    pub canonical_method: String,
    pub request_id: String,
    pub params: serde_json::Value,
    pub invocation_class: InvocationClass,
}

/// 冻结的稳定领域错误类别。只能从这里取稳定错误码，禁止依据错误字符串或普通
/// `Result::Err` 推断类别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StableDomainError {
    /// 公共能力未激活 / permit 缺失 / fingerprint 变化。
    CapabilityDisabled,
    /// permit 已被吊销 / authority 失效 / evidence 撤销。
    CapabilityRevoked,
    /// 缺少结构化 handoff 字段。
    HandoffFieldsRequired,
    /// 请求 id 复用且 canonical 参数不一致。
    RequestIdReuseMismatch,
    /// 未被上表归类但必须确定性、可重放的领域拒绝。
    DeterministicReject { code: String },
}

impl StableDomainError {
    pub fn code(&self) -> &'static str {
        match self {
            StableDomainError::CapabilityDisabled => ERR_CAPABILITY_DISABLED,
            StableDomainError::CapabilityRevoked => ERR_CAPABILITY_REVOKED,
            StableDomainError::HandoffFieldsRequired => ERR_HANDOFF_FIELDS_REQUIRED,
            StableDomainError::RequestIdReuseMismatch => ERR_REQUEST_ID_REUSE_MISMATCH,
            StableDomainError::DeterministicReject { code } => {
                // code 来自受控来源，仅用于回显；此处借 static 生命周期。
                // 实际使用方将其转为确定性的尾部内容。
                Box::leak(code.clone().into_boxed_str())
            }
        }
    }
}

/// 基础设施失败类别。只能来自连接、事务、Authoritative_Clock、registry、I/O 或
/// 未分类内部失败；主要由 wrapper 构造，领域 handler 不得伪造。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InfrastructureError {
    Connection,
    Transaction,
    AuthoritativeClock,
    Registry,
    Io,
    Internal { detail: String },
}

/// wrapper 只允许领域 handler 返回的封闭、类型化结果。禁止依据错误字符串或普通
/// `Result::Err` 推断类别。
#[derive(Debug)]
pub enum DomainOutcome {
    /// 领域写入与 ledger result 同时 commit。
    CommitSuccess {
        response: serde_json::Value,
    },
    /// 走 savepoint 撤销回调局部写入后，以可重放 ledger error 提交。
    CommitDeterministicError {
        stable_error: StableDomainError,
    },
    /// 回滚 outer transaction、领域写入和 ledger result。
    RollbackInfrastructureError {
        infrastructure_error: InfrastructureError,
    },
}

/// 传给领域 handler 的受限事务句柄。
///
/// 非 `Clone`，生命周期受 wrapper 限制；不暴露 commit、rollback、savepoint、原始
/// connection ownership、第二写连接创建或外部持久化 I/O。**v1 不支持 commit 后副作用**。
pub struct TaskDomainTx<'a> {
    tx: &'a Transaction<'a>,
}

// TaskDomainTx 借有待提交事务，故意不实现 Clone / Serialize。
impl<'a> TaskDomainTx<'a> {
    /// 仅 foundation wrapper（`apply_domain` 调用方）用于构造。
    ///
    /// 持有不可变 `&Transaction`：rusqlite 的 `execute`/`prepare`/查询均取 `&self`，足以
    /// 满足领域写入；而 `commit`/`rollback` 需所有权、`SAVEPOINT`/第二写连接需 `&mut
    /// Connection`，均无法经此句柄触发，故回调不能覆盖 wrapper 的 savepoint/ledger
    /// 语义（§3.3）。不可变引用在生命周期参数上是协变的，允许 wrapper 以短借用构造并
    /// 在回调返回后立即释放借用、继续使用同一事务。
    pub(crate) fn new(tx: &'a Transaction<'a>) -> Self {
        TaskDomainTx { tx }
    }

    /// 领域回调在受保护事务内执行查询/写入的借用句柄。
    pub(crate) fn tx(&self) -> &Transaction<'a> {
        self.tx
    }
}

/// 进入 handler 前冻结的权威输入。registry/clock/identity/contract recheck 的只读结果。
#[derive(Debug, Clone, Default)]
pub struct FrozenAuthorityInput {
    pub daemon_generation: u64,
    pub authority_id: String,
    pub authority_revision: u64,
    pub fencing_counter: u64,
    pub schema_fingerprint: String,
    pub rules_hash: String,
    pub runtime_binary_hash: String,
    /// 与字段对应的完整绑定快照，供 revalidation 重放比对。
    pub snapshot: BTreeMap<String, String>,
}

/// 公共路由受理所需的精确 event capability。私有、不可序列化；缺失或 fingerprint
/// 变化时公共路由保持 disabled。
#[derive(Debug, Clone)]
pub struct PublicPreflightPermit {
    pub promotion_event_id: String,
    pub workspace_id: String,
    pub request_id: String,
    pub daemon_generation: u64,
    pub authority_id: String,
    pub authority_revision: u64,
    pub fencing_counter: u64,
    pub evidence_id: String,
    pub evidence_hash: String,
    pub schema_fingerprint: String,
    pub rules_hash: String,
    pub runtime_binary_hash: String,
}

/// 仅 daemon 内私有 validation 路径配合 `InvocationClass::InternalValidation` 使用；
/// **绝不**安装到 public route。
#[derive(Debug, Clone)]
pub struct InternalPreflightPermit {
    pub schema_fingerprint: String,
    pub rules_hash: String,
    pub daemon_generation: u64,
}