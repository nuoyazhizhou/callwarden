//! Stage_Toggle 存储与前置阶段校验（Req 13.11–13.21）。
//!
//! ## 职责
//! - 在 daemon 拥有的配置存储中持久化 P0–P4 各阶段的 Stage_Toggle
//! - 支持 global / workspace / task 三级作用域
//! - 记录每次变更的 actor Peer_Identity 与 Authoritative_Clock 时间
//! - 有效值解析：task > workspace > global，缺值继承更宽作用域，全局默认 disabled
//! - 前置阶段校验：P2/P3/P4 要求 P1 启用；P4 额外要求 P3 启用
//! - 违反前置条件时以 Structured_Reason 拒绝变更
//! - 同一配置存储承载 Independence_Policy（required/solo），D0 只提供存取
//!
//! ## P0 独立性（Req 13.17）
//! P0 不依赖 P1–P4，无前置阶段要求。
//!
//! ## 职责边界
//! D0 只提供 Stage_Toggle 和 Independence_Policy 的存储与解析。
//! 语义判定（如某 profile 是否要求 Independent_Review）在 P1 的 4.5 实现。

use rusqlite::{params, Connection};
use std::sync::Arc;

use super::clock::AuthoritativeClock;
use super::dispatch::DaemonRpcError;
use super::task_loop::capability_control::CapabilityMutationGate;

/// 产品化阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Stage {
    P0,
    P1,
    P2,
    P3,
    P4,
}

impl Stage {
    /// 所有阶段（P0–P4）。
    pub const ALL: [Stage; 5] = [Stage::P0, Stage::P1, Stage::P2, Stage::P3, Stage::P4];

    pub fn as_str(&self) -> &'static str {
        match self {
            Stage::P0 => "P0",
            Stage::P1 => "P1",
            Stage::P2 => "P2",
            Stage::P3 => "P3",
            Stage::P4 => "P4",
        }
    }

    /// 前置阶段要求（Req 13.13）。
    ///
    /// - P0: 无前置（Req 13.17）
    /// - P1: 无前置
    /// - P2: 要求 P1
    /// - P3: 要求 P1
    /// - P4: 要求 P1 + P3
    pub fn prerequisites(&self) -> &'static [Stage] {
        match self {
            Stage::P0 => &[],
            Stage::P1 => &[],
            Stage::P2 => &[Stage::P1],
            Stage::P3 => &[Stage::P1],
            Stage::P4 => &[Stage::P1, Stage::P3],
        }
    }
}

impl std::fmt::Display for Stage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

/// 作用域（Req 13.11）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToggleScope {
    /// 全局
    Global,
    /// Workspace 级
    Workspace(String),
    /// Task 级
    Task(String),
}

impl ToggleScope {
    /// 作用域标识（存储用）。
    pub fn scope_key(&self) -> String {
        match self {
            ToggleScope::Global => "global".to_string(),
            ToggleScope::Workspace(id) => format!("workspace:{}", id),
            ToggleScope::Task(id) => format!("task:{}", id),
        }
    }

    /// 作用域优先级（数值越大优先级越高）。
    fn precedence(&self) -> u8 {
        match self {
            ToggleScope::Global => 0,
            ToggleScope::Workspace(_) => 1,
            ToggleScope::Task(_) => 2,
        }
    }
}

/// Independence_Policy 取值（Req 5.12–5.17）。
///
/// D0 只提供存取，语义判定在 P1 的 4.5 实现。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IndependencePolicy {
    /// 要求 Independent_Review（默认）
    Required,
    /// 豁免 Independent_Review
    Solo,
}

impl IndependencePolicy {
    pub fn as_str(&self) -> &'static str {
        match self {
            IndependencePolicy::Required => "required",
            IndependencePolicy::Solo => "solo",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "required" => Some(Self::Required),
            "solo" => Some(Self::Solo),
            _ => None,
        }
    }
}

/// Stage_Toggle 配置存储（daemon 拥有，Req 13.11）。
///
/// 使用 rusqlite 持久化，支持 in-memory（测试）和文件（生产）。
/// 每次变更记录 actor Peer_Identity 与 Authoritative_Clock 时间。
///
/// ## 锁序（计划 §3.3 / 0A 冻结，Requirement 15 AC 23）
/// 每个写入口（`set_toggle`、`set_independence_policy`）在打开任何 DB 写事务
/// **之前**取得 `CapabilityMutationGate`，并持续持有到全部写事务 commit 完成，
/// 遵守全局锁序 `CapabilityMutationGate → authority store transaction →
/// task-DB transaction`。本 store 只触碰 authority store，不触碰 task-DB。
pub struct StageToggleStore {
    conn: Connection,
    clock: Arc<AuthoritativeClock>,
    gate: Arc<CapabilityMutationGate>,
}

impl StageToggleStore {
    /// 创建 in-memory 存储（测试用）。
    pub fn open_in_memory(clock: Arc<AuthoritativeClock>) -> Result<Self, DaemonRpcError> {
        let conn = Connection::open_in_memory()
            .map_err(|e| DaemonRpcError::internal_error(format!("打开内存数据库失败: {}", e)))?;
        let store = Self {
            conn,
            clock,
            gate: Arc::new(CapabilityMutationGate::default()),
        };
        store.init_schema()?;
        Ok(store)
    }

    /// 创建文件存储（生产用）。
    pub fn open(path: &str, clock: Arc<AuthoritativeClock>) -> Result<Self, DaemonRpcError> {
        let conn = Connection::open(path)
            .map_err(|e| DaemonRpcError::internal_error(format!("打开配置数据库失败: {}", e)))?;
        let store = Self {
            conn,
            clock,
            gate: Arc::new(CapabilityMutationGate::default()),
        };
        store.init_schema()?;
        Ok(store)
    }

    /// 注入 daemon 全局共享的 `CapabilityMutationGate`（0B 接入点）。
    ///
    /// 默认实例为 store 私有门禁；daemon 组装时可用同一 gate 串行化
    /// authority-store 与 task-DB 的写路径，避免不同实例破坏全局锁序。
    pub fn attach_gate(&mut self, gate: Arc<CapabilityMutationGate>) {
        self.gate = gate;
    }

    /// 从 scope 派生 workspace 归属键（gate 语义归属用）。
    ///
    /// - Global → `"global"`
    /// - Workspace(id) → `"workspace:{id}"`
    /// - Task(id) → `"task:{id}"`（任务级变更归其任务 scope）
    fn scope_workspace_key(scope: &ToggleScope) -> String {
        match scope {
            ToggleScope::Global => "global".to_string(),
            ToggleScope::Workspace(id) => format!("workspace:{}", id),
            ToggleScope::Task(id) => format!("task:{}", id),
        }
    }

    /// 初始化 schema。
    fn init_schema(&self) -> Result<(), DaemonRpcError> {
        self.conn
            .execute_batch(
                "
            CREATE TABLE IF NOT EXISTS stage_toggles (
                stage       TEXT NOT NULL,
                scope_key   TEXT NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 0,
                actor       TEXT NOT NULL DEFAULT '',
                changed_at  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (stage, scope_key)
            );

            CREATE TABLE IF NOT EXISTS independence_policies (
                scope_key   TEXT NOT NULL PRIMARY KEY,
                policy      TEXT NOT NULL DEFAULT 'required',
                actor       TEXT NOT NULL DEFAULT '',
                changed_at  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS toggle_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stage       TEXT NOT NULL,
                scope_key   TEXT NOT NULL,
                old_value   INTEGER,
                new_value   INTEGER NOT NULL,
                actor       TEXT NOT NULL,
                changed_at  INTEGER NOT NULL
            );
            ",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("初始化 schema 失败: {}", e)))?;
        Ok(())
    }

    /// 设置 Stage_Toggle（带前置阶段校验，Req 13.13, 13.14）。
    ///
    /// 启用 P2/P3/P4 时校验前置阶段是否已启用（同一有效作用域）。
    /// 违反前置条件时拒绝变更并返回 Structured_Reason。
    pub fn set_toggle(
        &mut self,
        stage: Stage,
        scope: &ToggleScope,
        enabled: bool,
        actor: &str,
    ) -> Result<(), DaemonRpcError> {
        // 锁序：在打开任何 authority-store 写事务之前取得 CapabilityMutationGate，
        // 并持续持有到全部写事务 commit 完成（计划 §3.3 / Requirement 15 AC 23）。
        let workspace_key = Self::scope_workspace_key(scope);
        let _gate = self.gate.acquire(&workspace_key)?;

        // 前置阶段校验（Req 13.13, 13.14）
        if enabled {
            for prereq in stage.prerequisites() {
                let prereq_effective = self.resolve_effective(*prereq, scope)?;
                if !prereq_effective {
                    return Err(DaemonRpcError::new(
                        "stage_prerequisite_missing",
                        format!(
                            "无法启用 {}：前置阶段 {} 在当前有效作用域未启用",
                            stage, prereq
                        ),
                    ));
                }
            }
        }

        // 禁用阶段时，检查是否有更高阶段依赖它（Req 13.14）
        if !enabled {
            for higher in Stage::ALL {
                if higher.prerequisites().contains(&stage) {
                    let higher_effective = self.resolve_effective(higher, scope)?;
                    if higher_effective {
                        return Err(DaemonRpcError::new(
                            "stage_dependency_conflict",
                            format!(
                                "无法禁用 {}：更高阶段 {} 在当前有效作用域已启用且依赖 {}",
                                stage, higher, stage
                            ),
                        ));
                    }
                }
            }
        }

        let scope_key = scope.scope_key();
        let now = self.clock.now_millis();

        // 读取旧值（审计用）
        let old_value: Option<bool> = self
            .conn
            .query_row(
                "SELECT enabled FROM stage_toggles WHERE stage = ?1 AND scope_key = ?2",
                params![stage.as_str(), scope_key],
                |row| row.get::<_, i32>(0).map(|v| v != 0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询旧值失败: {}", e)))?;

        // 写入新值
        self.conn
            .execute(
                "INSERT INTO stage_toggles (stage, scope_key, enabled, actor, changed_at)
                 VALUES (?1, ?2, ?3, ?4, ?5)
                 ON CONFLICT(stage, scope_key) DO UPDATE SET
                   enabled = ?3, actor = ?4, changed_at = ?5",
                params![stage.as_str(), scope_key, enabled as i32, actor, now as i64],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("写入 toggle 失败: {}", e)))?;

        // 追加审计日志
        self.conn
            .execute(
                "INSERT INTO toggle_audit_log (stage, scope_key, old_value, new_value, actor, changed_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    stage.as_str(),
                    scope_key,
                    old_value.map(|v| v as i32),
                    enabled as i32,
                    actor,
                    now as i64
                ],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("写入审计日志失败: {}", e)))?;

        Ok(())
    }

    /// 解析有效 Stage_Toggle 值（Req 13.12）。
    ///
    /// 优先级：task > workspace > global，缺值继承更宽作用域，全局默认 disabled。
    pub fn resolve_effective(
        &self,
        stage: Stage,
        scope: &ToggleScope,
    ) -> Result<bool, DaemonRpcError> {
        // 按优先级从高到低查找
        let scopes_to_check: Vec<String> = match scope {
            ToggleScope::Global => vec!["global".to_string()],
            ToggleScope::Workspace(ws_id) => {
                vec![format!("workspace:{}", ws_id), "global".to_string()]
            }
            ToggleScope::Task(task_id) => {
                // task 作用域需要知道所属 workspace 才能继承
                // 简化：task > global（workspace 层由调用方提供）
                vec![format!("task:{}", task_id), "global".to_string()]
            }
        };

        for sk in &scopes_to_check {
            let result: Option<bool> = self
                .conn
                .query_row(
                    "SELECT enabled FROM stage_toggles WHERE stage = ?1 AND scope_key = ?2",
                    params![stage.as_str(), sk],
                    |row| row.get::<_, i32>(0).map(|v| v != 0),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 toggle 失败: {}", e)))?;

            if let Some(enabled) = result {
                return Ok(enabled);
            }
        }

        // 全局默认 disabled
        Ok(false)
    }

    /// 解析有效 Stage_Toggle 值（含 workspace 继承链）。
    ///
    /// 完整继承链：task > workspace > global。
    pub fn resolve_effective_full(
        &self,
        stage: Stage,
        workspace_id: Option<&str>,
        task_id: Option<&str>,
    ) -> Result<bool, DaemonRpcError> {
        let mut scopes_to_check: Vec<String> = Vec::new();

        if let Some(tid) = task_id {
            scopes_to_check.push(format!("task:{}", tid));
        }
        if let Some(wid) = workspace_id {
            scopes_to_check.push(format!("workspace:{}", wid));
        }
        scopes_to_check.push("global".to_string());

        for sk in &scopes_to_check {
            let result: Option<bool> = self
                .conn
                .query_row(
                    "SELECT enabled FROM stage_toggles WHERE stage = ?1 AND scope_key = ?2",
                    params![stage.as_str(), sk],
                    |row| row.get::<_, i32>(0).map(|v| v != 0),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 toggle 失败: {}", e)))?;

            if let Some(enabled) = result {
                return Ok(enabled);
            }
        }

        Ok(false)
    }

    /// 获取所有阶段的有效 toggle 集合（用于 gate decision 记录，Req 13.15）。
    pub fn resolve_all_effective(
        &self,
        workspace_id: Option<&str>,
        task_id: Option<&str>,
    ) -> Result<Vec<(Stage, bool)>, DaemonRpcError> {
        let mut result = Vec::new();
        for stage in Stage::ALL {
            let enabled = self.resolve_effective_full(stage, workspace_id, task_id)?;
            result.push((stage, enabled));
        }
        Ok(result)
    }

    /// 设置 Independence_Policy（Req 5.12–5.17，D0 只存取）。
    pub fn set_independence_policy(
        &mut self,
        scope: &ToggleScope,
        policy: IndependencePolicy,
        actor: &str,
    ) -> Result<(), DaemonRpcError> {
        // 锁序：在打开任何 authority-store 写事务之前取得 CapabilityMutationGate。
        let workspace_key = Self::scope_workspace_key(scope);
        let _gate = self.gate.acquire(&workspace_key)?;

        let scope_key = scope.scope_key();
        let now = self.clock.now_millis();

        self.conn
            .execute(
                "INSERT INTO independence_policies (scope_key, policy, actor, changed_at)
                 VALUES (?1, ?2, ?3, ?4)
                 ON CONFLICT(scope_key) DO UPDATE SET
                   policy = ?2, actor = ?3, changed_at = ?4",
                params![scope_key, policy.as_str(), actor, now as i64],
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("写入 policy 失败: {}", e)))?;

        Ok(())
    }

    /// 解析有效 Independence_Policy（继承链同 Stage_Toggle）。
    ///
    /// 默认值：required。
    pub fn resolve_independence_policy(
        &self,
        workspace_id: Option<&str>,
        task_id: Option<&str>,
    ) -> Result<IndependencePolicy, DaemonRpcError> {
        let mut scopes_to_check: Vec<String> = Vec::new();

        if let Some(tid) = task_id {
            scopes_to_check.push(format!("task:{}", tid));
        }
        if let Some(wid) = workspace_id {
            scopes_to_check.push(format!("workspace:{}", wid));
        }
        scopes_to_check.push("global".to_string());

        for sk in &scopes_to_check {
            let result: Option<String> = self
                .conn
                .query_row(
                    "SELECT policy FROM independence_policies WHERE scope_key = ?1",
                    params![sk],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|e| DaemonRpcError::internal_error(format!("查询 policy 失败: {}", e)))?;

            if let Some(policy_str) = result {
                return Ok(IndependencePolicy::from_str(&policy_str)
                    .unwrap_or(IndependencePolicy::Required));
            }
        }

        // 默认 required
        Ok(IndependencePolicy::Required)
    }

    /// 获取审计日志条目数。
    pub fn audit_log_count(&self) -> Result<u64, DaemonRpcError> {
        let count: u64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM toggle_audit_log", [], |row| {
                row.get(0)
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("查询审计日志失败: {}", e)))?;
        Ok(count)
    }
}

/// rusqlite OptionalExtension trait 简化。
trait OptionalExt<T> {
    fn optional(self) -> Result<Option<T>, rusqlite::Error>;
}

impl<T> OptionalExt<T> for Result<T, rusqlite::Error> {
    fn optional(self) -> Result<Option<T>, rusqlite::Error> {
        match self {
            Ok(v) => Ok(Some(v)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn make_store() -> StageToggleStore {
        let clock = Arc::new(AuthoritativeClock::new());
        StageToggleStore::open_in_memory(clock).unwrap()
    }

    #[test]
    fn test_default_all_disabled() {
        let store = make_store();
        for stage in Stage::ALL {
            let enabled = store.resolve_effective_full(stage, None, None).unwrap();
            assert!(!enabled, "{} 默认应为 disabled", stage);
        }
    }

    #[test]
    fn test_set_and_resolve_global() {
        let mut store = make_store();
        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();

        assert!(store.resolve_effective_full(Stage::P1, None, None).unwrap());
        assert!(!store.resolve_effective_full(Stage::P2, None, None).unwrap());
    }

    #[test]
    fn test_scope_precedence_task_over_workspace_over_global() {
        let mut store = make_store();

        // global: P1 enabled
        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();

        // workspace: P1 disabled
        store
            .set_toggle(
                Stage::P1,
                &ToggleScope::Workspace("ws-1".to_string()),
                false,
                "uid:1000",
            )
            .unwrap();

        // task: P1 enabled
        store
            .set_toggle(
                Stage::P1,
                &ToggleScope::Task("task-1".to_string()),
                true,
                "uid:1000",
            )
            .unwrap();

        // 全局有效：enabled
        assert!(store.resolve_effective_full(Stage::P1, None, None).unwrap());

        // workspace 有效：disabled（workspace 覆盖 global）
        assert!(!store
            .resolve_effective_full(Stage::P1, Some("ws-1"), None)
            .unwrap());

        // task 有效：enabled（task 覆盖 workspace）
        assert!(store
            .resolve_effective_full(Stage::P1, Some("ws-1"), Some("task-1"))
            .unwrap());
    }

    #[test]
    fn test_prerequisite_p2_requires_p1() {
        let mut store = make_store();

        // 未启用 P1 就启用 P2 → 拒绝
        let result = store.set_toggle(Stage::P2, &ToggleScope::Global, true, "uid:1000");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "stage_prerequisite_missing");
        assert!(err.message.contains("P1"));
    }

    #[test]
    fn test_prerequisite_p4_requires_p1_and_p3() {
        let mut store = make_store();

        // 启用 P1
        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();

        // 未启用 P3 就启用 P4 → 拒绝
        let result = store.set_toggle(Stage::P4, &ToggleScope::Global, true, "uid:1000");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "stage_prerequisite_missing");
        assert!(err.message.contains("P3"));

        // 启用 P3 后再启用 P4 → 成功
        store
            .set_toggle(Stage::P3, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        store
            .set_toggle(Stage::P4, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        assert!(store.resolve_effective_full(Stage::P4, None, None).unwrap());
    }

    #[test]
    fn test_p0_no_prerequisite() {
        let mut store = make_store();

        // P0 无需前置，直接启用
        store
            .set_toggle(Stage::P0, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        assert!(store.resolve_effective_full(Stage::P0, None, None).unwrap());
    }

    #[test]
    fn test_disable_p1_blocked_when_p2_enabled() {
        let mut store = make_store();

        // 启用 P1 → P2
        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        store
            .set_toggle(Stage::P2, &ToggleScope::Global, true, "uid:1000")
            .unwrap();

        // 禁用 P1 → 拒绝（P2 依赖 P1）
        let result = store.set_toggle(Stage::P1, &ToggleScope::Global, false, "uid:1000");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "stage_dependency_conflict");
        assert!(err.message.contains("P2"));
    }

    #[test]
    fn test_independence_policy_default_required() {
        let store = make_store();
        let policy = store.resolve_independence_policy(None, None).unwrap();
        assert_eq!(policy, IndependencePolicy::Required);
    }

    #[test]
    fn test_independence_policy_set_and_resolve() {
        let mut store = make_store();

        // 全局设为 solo
        store
            .set_independence_policy(&ToggleScope::Global, IndependencePolicy::Solo, "uid:1000")
            .unwrap();
        assert_eq!(
            store.resolve_independence_policy(None, None).unwrap(),
            IndependencePolicy::Solo
        );

        // workspace 设为 required（覆盖 global）
        store
            .set_independence_policy(
                &ToggleScope::Workspace("ws-1".to_string()),
                IndependencePolicy::Required,
                "uid:1000",
            )
            .unwrap();
        assert_eq!(
            store
                .resolve_independence_policy(Some("ws-1"), None)
                .unwrap(),
            IndependencePolicy::Required
        );
    }

    #[test]
    fn test_audit_log_recorded() {
        let mut store = make_store();
        assert_eq!(store.audit_log_count().unwrap(), 0);

        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        assert_eq!(store.audit_log_count().unwrap(), 1);

        store
            .set_toggle(Stage::P1, &ToggleScope::Global, false, "uid:2000")
            .unwrap();
        assert_eq!(store.audit_log_count().unwrap(), 2);
    }

    #[test]
    fn test_resolve_all_effective() {
        let mut store = make_store();
        store
            .set_toggle(Stage::P0, &ToggleScope::Global, true, "uid:1000")
            .unwrap();
        store
            .set_toggle(Stage::P1, &ToggleScope::Global, true, "uid:1000")
            .unwrap();

        let all = store.resolve_all_effective(None, None).unwrap();
        assert_eq!(all.len(), 5);
        assert!(all[0].1); // P0 enabled
        assert!(all[1].1); // P1 enabled
        assert!(!all[2].1); // P2 disabled
        assert!(!all[3].1); // P3 disabled
        assert!(!all[4].1); // P4 disabled
    }

    #[test]
    fn test_stage_prerequisites() {
        assert_eq!(Stage::P0.prerequisites(), &[]);
        assert_eq!(Stage::P1.prerequisites(), &[]);
        assert_eq!(Stage::P2.prerequisites(), &[Stage::P1]);
        assert_eq!(Stage::P3.prerequisites(), &[Stage::P1]);
        assert_eq!(Stage::P4.prerequisites(), &[Stage::P1, Stage::P3]);
    }

    #[test]
    fn test_inheritance_from_workspace_to_task() {
        let mut store = make_store();

        // workspace 启用 P1
        store
            .set_toggle(
                Stage::P1,
                &ToggleScope::Workspace("ws-1".to_string()),
                true,
                "uid:1000",
            )
            .unwrap();

        // task 未设置，继承 workspace
        assert!(store
            .resolve_effective_full(Stage::P1, Some("ws-1"), Some("task-99"))
            .unwrap());

        // 不同 workspace 未设置，继承 global（disabled）
        assert!(!store
            .resolve_effective_full(Stage::P1, Some("ws-2"), None)
            .unwrap());
    }
}
