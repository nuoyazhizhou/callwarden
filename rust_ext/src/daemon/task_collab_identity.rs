//! Action identity 与 session separation 查询域。
//! 保留 identity 解析、校验和 evidence freshness helper 的原有语义。

use super::*;

impl TaskCollabStore {
    pub fn handle_get_action_identity(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id: i64 = params
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let action_id = params
            .get("action_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let conn = self.conn.lock().unwrap();
        let mut stmt = conn
            .prepare(
                "SELECT id, workspace_id, action_id, action_type, task_id, \
                        contract_id, contract_revision, agent_id, session_id, \
                        model_id, role, recorded_at \
                 FROM action_identities \
                 WHERE workspace_id = ? AND action_id = ?",
            )
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("查询 action_identities 失败: {}", e))
            })?;
        let found = stmt
            .query_row(params![workspace_id, action_id], |r| {
                let mut m = Map::new();
                m.insert("id".to_string(), Value::Number(r.get::<_, i64>(0)?.into()));
                m.insert(
                    "workspace_id".to_string(),
                    Value::Number(r.get::<_, i64>(1)?.into()),
                );
                m.insert(
                    "action_id".to_string(),
                    Value::String(r.get::<_, String>(2)?),
                );
                m.insert(
                    "action_type".to_string(),
                    Value::String(r.get::<_, String>(3)?),
                );
                m.insert("task_id".to_string(), Value::String(r.get::<_, String>(4)?));
                m.insert(
                    "contract_id".to_string(),
                    Value::String(r.get::<_, String>(5)?),
                );
                m.insert(
                    "contract_revision".to_string(),
                    Value::Number(r.get::<_, i64>(6)?.into()),
                );
                m.insert(
                    "agent_id".to_string(),
                    Value::String(r.get::<_, String>(7)?),
                );
                m.insert(
                    "session_id".to_string(),
                    Value::String(r.get::<_, String>(8)?),
                );
                m.insert(
                    "model_id".to_string(),
                    Value::String(r.get::<_, String>(9)?),
                );
                m.insert("role".to_string(), Value::String(r.get::<_, String>(10)?));
                m.insert(
                    "recorded_at".to_string(),
                    Value::Number(
                        serde_json::Number::from_f64(r.get::<_, f64>(11)?)
                            .unwrap_or(serde_json::Number::from(0)),
                    ),
                );
                Ok(Value::Object(m))
            })
            .optional()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("映射 action_identities 失败: {}", e))
            })?;

        match found {
            Some(v) => Ok(v),
            None => Ok(Value::Null),
        }
    }

    // MCP-011（T-1787321709518-0b31a484）：check_action_identity 迁移 rust_native。
    // 语义与 Python tools_p3_identity._h_check_action_identity 一致：解析 identity JSON
    // 字符串 → 校验结构化身份（agent_id/session_id/model_id/role 四字段齐全 +
    // require_role 匹配）→ 返回 {"valid": bool, "reason": {...}}。
    pub fn handle_check_action_identity(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let identity_str = params
            .get("identity")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let require_role = params
            .get("require_role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // 1. 解析 identity JSON 字符串（_p3_resolve_identity_arg）
        if identity_str.trim().is_empty() {
            let mut reason = Map::new();
            reason.insert(
                "code".to_string(),
                Value::String("E_IDENTITY_INCOMPLETE".to_string()),
            );
            reason.insert(
                "message_key".to_string(),
                Value::String("error.identity_incomplete".to_string()),
            );
            reason.insert(
                "detail".to_string(),
                Value::String(
                    "identity 必须是 JSON 对象 {agent_id, session_id, model_id, role}".to_string(),
                ),
            );
            let mut result = Map::new();
            result.insert("valid".to_string(), Value::Bool(false));
            result.insert("reason".to_string(), Value::Object(reason));
            return Ok(Value::Object(result));
        }
        let parsed: Result<Value, _> = serde_json::from_str(&identity_str);
        let parsed = match parsed {
            Ok(v) => v,
            Err(_) => {
                let mut reason = Map::new();
                reason.insert(
                    "code".to_string(),
                    Value::String("E_IDENTITY_INCOMPLETE".to_string()),
                );
                reason.insert(
                    "message_key".to_string(),
                    Value::String("error.identity_incomplete".to_string()),
                );
                reason.insert(
                    "detail".to_string(),
                    Value::String(
                        "identity 必须是 JSON 对象 {agent_id, session_id, model_id, role}"
                            .to_string(),
                    ),
                );
                let mut result = Map::new();
                result.insert("valid".to_string(), Value::Bool(false));
                result.insert("reason".to_string(), Value::Object(reason));
                return Ok(Value::Object(result));
            }
        };
        let obj = match parsed.as_object() {
            Some(o) => o,
            None => {
                let mut reason = Map::new();
                reason.insert(
                    "code".to_string(),
                    Value::String("E_IDENTITY_INCOMPLETE".to_string()),
                );
                reason.insert(
                    "message_key".to_string(),
                    Value::String("error.identity_incomplete".to_string()),
                );
                reason.insert(
                    "detail".to_string(),
                    Value::String("identity 必须是 JSON 对象".to_string()),
                );
                let mut result = Map::new();
                result.insert("valid".to_string(), Value::Bool(false));
                result.insert("reason".to_string(), Value::Object(reason));
                return Ok(Value::Object(result));
            }
        };

        let agent_id = obj
            .get("agent_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let session_id = obj
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let model_id = obj
            .get("model_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let role = obj
            .get("role")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // 2. validate_action_identity（纯逻辑，无 DB 查询）
        let valid;
        let mut reason = Map::new();
        if agent_id.is_empty() || session_id.is_empty() || model_id.is_empty() || role.is_empty() {
            valid = false;
            reason.insert(
                "code".to_string(),
                Value::String("E_IDENTITY_INCOMPLETE".to_string()),
            );
            reason.insert(
                "message_key".to_string(),
                Value::String("error.identity_incomplete".to_string()),
            );
            reason.insert(
                "detail".to_string(),
                Value::String(
                    "缺失必要的 Identity 字段 (agent_id, session_id, model_id, role)".to_string(),
                ),
            );
        } else if !require_role.is_empty() && role != require_role {
            valid = false;
            reason.insert(
                "code".to_string(),
                Value::String("E_IDENTITY_ROLE_MISMATCH".to_string()),
            );
            reason.insert(
                "message_key".to_string(),
                Value::String("error.identity_role_mismatch".to_string()),
            );
            reason.insert(
                "detail".to_string(),
                Value::String(format!("角色不匹配: 期望 {}, 实际 {}", require_role, role)),
            );
            reason.insert(
                "expected_role".to_string(),
                Value::String(require_role.clone()),
            );
            reason.insert("actual_role".to_string(), Value::String(role.clone()));
        } else {
            valid = true;
            reason.insert("code".to_string(), Value::String("OK".to_string()));
        }

        let mut result = Map::new();
        result.insert("valid".to_string(), Value::Bool(valid));
        result.insert("reason".to_string(), Value::Object(reason));
        Ok(Value::Object(result))
    }

    // MCP-012（T-1787321709584-0f2573f4）：check_session_separation 迁移 rust_native。
    // 语义与 Python tools_p3_identity._h_check_session_separation 一致：解析 reviewer/
    // implementer_identity JSON 字符串 → 非 dict 返回 E_IDENTITY_INCOMPLETE → 校验
    // Reviewer Session 与 Implementer Session 不同（相等 → E_IDENTITY_SESSION_NOT_SEPARATED）
    // → 返回 {"valid": bool, "reason": {...}}。
    pub fn handle_check_session_separation(
        &self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let reviewer_str = params
            .get("reviewer_identity")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let implementer_str = params
            .get("implementer_identity")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        // 解析并校验两方 identity 均为 JSON 对象
        let reviewer = Self::parse_identity_object(&reviewer_str);
        let implementer = Self::parse_identity_object(&implementer_str);
        let (reviewer, implementer) = match (reviewer, implementer) {
            (Ok(r), Ok(i)) => (r, i),
            _ => {
                let mut reason = Map::new();
                reason.insert(
                    "code".to_string(),
                    Value::String("E_IDENTITY_INCOMPLETE".to_string()),
                );
                reason.insert(
                    "message_key".to_string(),
                    Value::String("error.identity_incomplete".to_string()),
                );
                reason.insert(
                    "detail".to_string(),
                    Value::String("reviewer/implementer_identity 必须是 JSON 对象".to_string()),
                );
                let mut result = Map::new();
                result.insert("valid".to_string(), Value::Bool(false));
                result.insert("reason".to_string(), Value::Object(reason));
                return Ok(Value::Object(result));
            }
        };

        // validate_session_separation：session_id 均非空且相等 → 未分离
        let reviewer_session = reviewer
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let implementer_session = implementer
            .get("session_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();

        let valid;
        let mut reason = Map::new();
        if !reviewer_session.is_empty()
            && !implementer_session.is_empty()
            && reviewer_session == implementer_session
        {
            valid = false;
            reason.insert(
                "code".to_string(),
                Value::String("E_IDENTITY_SESSION_NOT_SEPARATED".to_string()),
            );
            reason.insert(
                "message_key".to_string(),
                Value::String("error.identity_session_not_separated".to_string()),
            );
            reason.insert(
                "detail".to_string(),
                Value::String(format!(
                    "Reviewer Session ({}) 等于 Implementer Session",
                    reviewer_session
                )),
            );
            reason.insert(
                "reviewer_session".to_string(),
                Value::String(reviewer_session.clone()),
            );
            reason.insert(
                "implementer_session".to_string(),
                Value::String(implementer_session.clone()),
            );
        } else {
            valid = true;
            reason.insert("code".to_string(), Value::String("OK".to_string()));
        }

        let mut result = Map::new();
        result.insert("valid".to_string(), Value::Bool(valid));
        result.insert("reason".to_string(), Value::Object(reason));
        Ok(Value::Object(result))
    }

    // 解析 identity JSON 字符串为对象；空串/非法 JSON/非对象均返回 Err。
    fn parse_identity_object(s: &str) -> Result<Map<String, Value>, ()> {
        if s.trim().is_empty() {
            return Err(());
        }
        match serde_json::from_str::<Value>(s) {
            Ok(Value::Object(m)) => Ok(m),
            _ => Err(()),
        }
    }

    // 从 start 出发 BFS 回到自身的最短 cycle path；找不到时回退 DFS 任意环。
    pub(crate) fn detect_cycle_find_shortest(
        graph: &BTreeMap<String, Vec<String>>,
        start: &str,
    ) -> Vec<String> {
        use std::collections::VecDeque;
        let mut queue: VecDeque<(String, Vec<String>)> = VecDeque::new();
        queue.push_back((start.to_string(), vec![start.to_string()]));
        let mut visited: HashSet<String> = HashSet::new();
        visited.insert(start.to_string());
        while let Some((node, path)) = queue.pop_front() {
            if let Some(neighbors) = graph.get(&node) {
                for nb in neighbors {
                    if nb == start && path.len() >= 1 {
                        let mut p = path.clone();
                        p.push(start.to_string());
                        return p;
                    }
                    if !visited.contains(nb) {
                        visited.insert(nb.clone());
                        let mut np = path.clone();
                        np.push(nb.clone());
                        queue.push_back((nb.clone(), np));
                    }
                }
            }
        }
        Self::detect_cycle_find_any(graph, start)
    }

    // DFS 回退：从 start 出发找任意回到 start 的 cycle path。
    fn detect_cycle_find_any(graph: &BTreeMap<String, Vec<String>>, start: &str) -> Vec<String> {
        let mut path: Vec<String> = Vec::new();
        let mut visited: HashSet<String> = HashSet::new();
        fn dfs(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            start: &str,
            path: &mut Vec<String>,
            visited: &mut HashSet<String>,
        ) -> Vec<String> {
            path.push(node.to_string());
            visited.insert(node.to_string());
            if let Some(neighbors) = graph.get(node) {
                for nb in neighbors {
                    if nb == start && path.len() >= 1 {
                        let mut p = path.clone();
                        p.push(start.to_string());
                        return p;
                    }
                    if !visited.contains(nb) {
                        let r = dfs(nb, graph, start, path, visited);
                        if !r.is_empty() {
                            return r;
                        }
                    }
                }
            }
            path.pop();
            visited.remove(node);
            Vec::new()
        }
        dfs(start, graph, start, &mut path, &mut visited)
    }

    /// 复刻 Python db_task_evidence.derive_freshness 的核心派生逻辑（snapshot/hash
    /// 比较维度在调用方未传入时跳过，保持与 Python 「freshness.status」 RPC 一致）。
    pub(crate) fn derive_evidence_freshness(
        conn: &rusqlite::Connection,
        evidence_id: &str,
        current_contract_revision: i64,
    ) -> String {
        const FRESHNESS_FRESH: &str = "fresh";
        const FRESHNESS_STALE: &str = "stale";
        const FRESHNESS_INVALID: &str = "invalid";
        const FRESHNESS_SUPERSEDED: &str = "superseded";

        // 查找原始 Evidence（event_type = evidence_appended）
        let row = conn
            .query_row(
                "SELECT verifier_name, verifier_version, verifier_config_hash, \
                 contract_revision, workspace_snapshot_id, file_hashes, symbol_hashes, \
                 graph_refresh_version FROM task_evidence_events \
                 WHERE evidence_id = ? AND event_type = ?",
                params![evidence_id, "evidence_appended"],
                |r| {
                    Ok((
                        r.get::<_, Option<String>>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<String>>(2)?,
                        r.get::<_, Option<i64>>(3)?,
                        r.get::<_, Option<String>>(4)?,
                        r.get::<_, Option<String>>(5)?,
                        r.get::<_, Option<String>>(6)?,
                        r.get::<_, Option<String>>(7)?,
                    ))
                },
            )
            .optional();

        let row = match row {
            Ok(Some(r)) => r,
            Ok(None) => return FRESHNESS_INVALID.to_string(),
            Err(_) => return "unknown".to_string(),
        };

        let (v_name, v_version, v_config, bound_revision, _snap, _fh, _sh, _gv) = row;
        let mut candidates: Vec<(i32, &str)> = Vec::new();

        // 1. 个体失效：存在 original_evidence_ref = evidence_id 的 invalidated 事件
        let invalidated: bool = conn
            .query_row(
                "SELECT 1 FROM task_evidence_events \
                 WHERE original_evidence_ref = ? AND event_type = ?",
                params![evidence_id, "evidence_invalidated"],
                |r| r.get::<_, i64>(0),
            )
            .ok()
            .is_some();
        if invalidated {
            candidates.push((3, FRESHNESS_INVALID));
        }

        // 2. Verifier 注册/信任/撤销
        if let (Some(name), Some(ver), Some(cfg)) = (&v_name, &v_version, &v_config) {
            let trust: Option<String> = conn
                .query_row(
                    "SELECT trust_status FROM verifier_registry \
                     WHERE name = ? AND version = ? AND config_hash = ?",
                    params![name, ver, cfg],
                    |r| r.get::<_, String>(0),
                )
                .ok();
            if trust.is_none() {
                candidates.push((3, FRESHNESS_INVALID));
            } else if trust.as_deref() != Some("trusted") {
                candidates.push((3, FRESHNESS_INVALID));
            }
            let revoked: bool = conn
                .query_row(
                    "SELECT 1 FROM verifier_revocation_records \
                     WHERE verifier_name = ? AND verifier_version = ? AND verifier_config_hash = ?",
                    params![name, ver, cfg],
                    |r| r.get::<_, i64>(0),
                )
                .ok()
                .is_some();
            if revoked {
                candidates.push((3, FRESHNESS_INVALID));
            }
        }

        // 3. superseded：当前契约 revision 前进
        if let Some(br) = bound_revision {
            if current_contract_revision > br {
                candidates.push((2, FRESHNESS_SUPERSEDED));
            }
        }

        // 4. stale：snapshot/file/symbol/graph 维度——仅当调用方传入时比较；
        //    本 RPC 调用方未传入，默认跳过（与 Python freshness.status 一致）。

        if candidates.is_empty() {
            return FRESHNESS_FRESH.to_string();
        }
        // 按优先级（invalid=3 > superseded=2 > stale=1 > fresh=0）取最高
        candidates.sort_by(|a, b| b.0.cmp(&a.0));
        candidates[0].1.to_string()
    }

}
