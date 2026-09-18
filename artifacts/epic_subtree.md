# Epic T-1787203926824-9f873bfc 子树任务清单

父任务：CW 业务逻辑全量下沉 Rust daemon（三阶段收尾）

总任务节点：201，其中 review 状态：133

## Review 状态任务（待复审候选）

| 任务 ID | 标题 |
|---|---|
| T-1787203937193-0993d120 | S1 T04-followup：cli/main.py 迁移 daemon RPC |
| T-1787321708568-d292ab3c | CLI-02 [control_plane]：cw search daemon-only 读取链路 |
| T-1787321708639-d6d362f4 | CLI-03 [control_plane]：cw task show/list/status-tree 只读 authority 诊断 |
| T-1787321708699-da5d8224 | MCP-001 [Gate/task_projection]：get_role_view → Rust daemon native |
| T-1787321708760-de068a9c | MCP-002 [task_projection]：find_evidence → Rust daemon native |
| T-1787321708856-e3c10624 | MCP-003 [task_projection]：get_freshness_status → Rust daemon native |
| T-1787321709894-21a00d8c | MCP-017 [graph_snapshot]：get_recent_changes → Rust daemon native |
| T-1787321709955-2537b8b4 | MCP-018 [graph_snapshot]：get_impact → Rust daemon native |
| T-1787321710012-289c54c4 | MCP-019 [graph_snapshot]：get_comment_from_version → Rust daemon native |
| T-1787321710069-2c03f5e0 | MCP-020 [graph_snapshot]：get_issue_summary → Rust daemon native |
| T-1787321710140-3047f444 | MCP-021 [graph_snapshot]：find_issues → Rust daemon native |
| T-1787321710300-39d0baf0 | MCP-022 [graph_snapshot]：get_test_coverage → Rust daemon native |
| T-1787321710397-3f9a9028 | MCP-023 [graph_snapshot]：export_module_graph → Rust daemon native |
| T-1787321710484-44c1c940 | MCP-024 [Gate/semantic_projection]：semantic_search → Rust daemon native |
| T-1787321710544-485efa78 | MCP-025 [semantic_projection]：find_similar_functions → Rust daemon native |
| T-1787321710602-4bcbb50c | MCP-026 [semantic_projection]：get_symbol_commit_history → Rust daemon native |
| T-1787321710659-4f3acb88 | MCP-027 [semantic_projection]：parse_codeowners → Rust daemon native |
| T-1787321710720-52daa72c | MCP-028 [semantic_projection]：get_project_dependencies → Rust daemon native |
| T-1787321710784-56a998b8 | MCP-029 [Gate/summary_projection]：get_summary → Rust daemon native |
| T-1787321710849-5a85f634 | MCP-030 [summary_projection]：project_brief → Rust daemon native |
| T-1787321710913-5e5558a4 | MCP-031 [summary_projection]：repo_map → Rust daemon native |
| T-1787321710977-62266414 | MCP-032 [summary_projection]：test_impact_selection → Rust daemon native |
| T-1787321711035-659d600c | MCP-033 [summary_projection]：who_to_ask → Rust daemon native |
| T-1787321711142-6c00c7b8 | MCP-034 [summary_projection]：get_ownership_map → Rust daemon native |
| T-1787321711218-707e9d24 | MCP-035 [summary_projection]：guardrail_scan → Rust daemon native |
| T-1787321711292-74eefc14 | MCP-036 [summary_projection]：guardrail_check_edit → Rust daemon native |
| T-1787321711354-78a01118 | MCP-037 [summary_projection]：guardrail_list_rules → Rust daemon native |
| T-1787321711419-7c795b78 | MCP-038 [summary_projection]：blast_radius → Rust daemon native |
| T-1787321711485-80694f68 | MCP-039 [summary_projection]：ask_codebase → Rust daemon native |
| T-1787321711567-854fb8b4 | MCP-040 [summary_projection]：get_token_savings_report → Rust daemon native |
| T-1787321711671-8b84382c | MCP-041 [summary_projection]：get_vulnerability_blast_radius → Rust daemon native |
| T-1787321711747-900bc900 | MCP-042 [summary_projection]：get_clone_aware_impact → Rust daemon native |
| T-1787321711827-94d36268 | MCP-043 [summary_projection]：review_readiness → Rust daemon native |
| T-1787321711882-9812b654 | MCP-044 [summary_projection]：cross_layer_impact → Rust daemon native |
| T-1787321711941-9b9a78d4 | MCP-045 [summary_projection]：evolution_frequency → Rust daemon native |
| T-1787321712002-9f3f403c | MCP-046 [summary_projection]：hotspot_evolution → Rust daemon native |
| T-1787321712071-a3580d34 | MCP-047 [summary_projection]：defect_learn → Rust daemon native |
| T-1787321712131-a6f798c4 | MCP-048 [Gate/repository_security]：list_branches → Rust daemon native |
| T-1787321712197-aae29150 | MCP-049 [repository_security]：merge_preview → Rust daemon native |
| T-1787321712254-ae4cba14 | MCP-050 [repository_security]：get_edit_history → Rust daemon native |
| T-1787321712317-b20200ec | MCP-051 [repository_security]：find_shared_symbols → Rust daemon native |
| T-1787321712383-b5f00e24 | MCP-052 [repository_security]：cross_repo_impact → Rust daemon native |
| T-1787321712440-b95892fc | MCP-053 [repository_security]：cross_repo_summary → Rust daemon native |
| T-1787321713365-f084e3fc | MCP-067 [task_projection]：list_clones → Rust daemon native |
| T-1787321713424-f4071e14 | MCP-068 [task_projection]：list_clone_groups → Rust daemon native |
| T-1787321713485-f7a90848 | MCP-069 [task_projection]：get_clone_group_detail → Rust daemon native |
| T-1787321713551-fb94f87c | MCP-070 [task_projection]：task_plan_template → Rust daemon native |
| T-1787322794470-a75de064 | CLI-004 [control_plane]：cw daemon（metrics/ping/workspace/publish/query） → Rust daemon HTTP thin client |
| T-1787322794529-aae5f8d4 | CLI-005 [control_plane]：cw-agent start → Rust daemon HTTP thin client |
| T-1787322794614-affbd0b4 | CLI-006 [control_plane]：cw-agent status → Rust daemon HTTP thin client |
| T-1787322794681-b3f8e33c | CLI-007 [graph_snapshot]：cw dependency cycle → Rust daemon HTTP thin client |
| T-1787322794745-b7c1ed10 | CLI-008 [graph_snapshot]：cw dependency explain → Rust daemon HTTP thin client |
| T-1787322794809-bb8f0658 | CLI-009 [graph_snapshot]：cw dependency list → Rust daemon HTTP thin client |
| T-1787322794865-beea9d08 | CLI-010 [graph_snapshot]：cw dependency provider-select → Rust daemon HTTP thin client |
| T-1787322794927-c29e6894 | CLI-011 [assignment_projection]：cw assignment → Rust daemon HTTP thin client |
| T-1787322794986-c6229cec | CLI-012 [task_projection]：cw audit → Rust daemon HTTP thin client |
| T-1787322795054-ca2e2694 | CLI-013 [cli_command_projection]：cw bootstrap → Rust daemon HTTP thin client |
| T-1787322795108-cd691968 | CLI-014 [cli_command_projection]：cw brief → Rust daemon HTTP thin client |
| T-1787322795173-d141864c | CLI-015 [graph_snapshot]：cw build-context → Rust daemon HTTP thin client |
| T-1787322795245-d58f1cf0 | CLI-016 [graph_snapshot]：cw call-chain → Rust daemon HTTP thin client |
| T-1787322795307-d949b968 | CLI-017 [cli_command_projection]：cw callees → Rust daemon HTTP thin client |
| T-1787322795374-dd442bac | CLI-018 [cli_command_projection]：cw callers → Rust daemon HTTP thin client |
| T-1787322795431-e0a47a2c | CLI-019 [cli_command_projection]：cw check-gate → Rust daemon HTTP thin client |
| T-1787322795483-e3c04150 | CLI-020 [cli_command_projection]：cw churn → Rust daemon HTTP thin client |
| T-1787322795588-e9fdfecc | CLI-021 [cli_command_projection]：cw clone → Rust daemon HTTP thin client |
| T-1787322795664-ee8f9090 | CLI-022 [cli_command_projection]：cw comment-coverage → Rust daemon HTTP thin client |
| T-1787322795725-f22932d8 | CLI-023 [cli_command_projection]：cw complexity → Rust daemon HTTP thin client |
| T-1787322795775-f52e96bc | CLI-024 [cli_command_projection]：cw coupled-fns → Rust daemon HTTP thin client |
| T-1787322795829-f85a6d98 | CLI-025 [cli_command_projection]：cw coupling → Rust daemon HTTP thin client |
| T-1787322795894-fc39c6c0 | CLI-026 [cli_command_projection]：cw coverage → Rust daemon HTTP thin client |
| T-1787322795956-fff62664 | CLI-027 [cli_command_projection]：cw dashboard → Rust daemon HTTP thin client |
| T-1787322796027-04260ab0 | CLI-028 [cli_command_projection]：cw defect → Rust daemon HTTP thin client |
| T-1787322796090-07e97aec | CLI-029 [graph_snapshot]：cw dependency → Rust daemon HTTP thin client |
| T-1787322796155-0bc98e68 | CLI-030 [cli_command_projection]：cw evolution → Rust daemon HTTP thin client |
| T-1787322796219-0f9d3760 | CLI-031 [cli_command_projection]：cw file → Rust daemon HTTP thin client |
| T-1787322796275-12f3c370 | CLI-032 [cli_command_projection]：cw fn-metrics → Rust daemon HTTP thin client |
| T-1787322796365-1852e1e8 | CLI-033 [cli_command_projection]：cw fts → Rust daemon HTTP thin client |
| T-1787322796421-1ba86fac | CLI-034 [cli_command_projection]：cw function-issues → Rust daemon HTTP thin client |
| T-1787322796483-1f5d4730 | CLI-035 [cli_command_projection]：cw gc → Rust daemon HTTP thin client |
| T-1787322796578-2508cfc4 | CLI-036 [cli_command_projection]：cw git → Rust daemon HTTP thin client |
| T-1787322796634-285466c0 | CLI-037 [cli_command_projection]：cw grep → Rust daemon HTTP thin client |
| T-1787322796701-2c5e2698 | CLI-038 [cli_command_projection]：cw guardrail → Rust daemon HTTP thin client |
| T-1787322796764-3014c724 | CLI-039 [cli_command_projection]：cw health-report → Rust daemon HTTP thin client |
| T-1787322796833-3431cb68 | CLI-040 [cli_command_projection]：cw hotspot → Rust daemon HTTP thin client |
| T-1787322796916-3926ce34 | CLI-041 [cli_command_projection]：cw impact → Rust daemon HTTP thin client |
| T-1787322796984-3d3e2df0 | CLI-042 [cli_command_projection]：cw issues → Rust daemon HTTP thin client |
| T-1787322797056-4189cf68 | CLI-043 [cli_command_projection]：cw largest-fns → Rust daemon HTTP thin client |
| T-1787322797118-45326058 | CLI-044 [assignment_projection]：cw lease → Rust daemon HTTP thin client |
| T-1787322797185-49338448 | CLI-045 [cli_command_projection]：cw map → Rust daemon HTTP thin client |
| T-1787322797263-4dd9dd94 | CLI-046 [cli_command_projection]：cw metrics → Rust daemon HTTP thin client |
| T-1787322797327-51aada2c | CLI-047 [cli_command_projection]：cw ownership-map → Rust daemon HTTP thin client |
| T-1787322797385-5518ba1c | CLI-048 [cli_command_projection]：cw query → Rust daemon HTTP thin client |
| T-1787322797455-594a8390 | CLI-049 [cli_command_projection]：cw refresh → Rust daemon HTTP thin client |
| T-1787322797511-5c99e860 | CLI-050 [cli_command_projection]：cw review → Rust daemon HTTP thin client |
| T-1787322797571-6031e2d4 | CLI-051 [task_projection]：cw rollback → Rust daemon HTTP thin client |
| T-1787322797641-645edd08 | CLI-052 [cli_command_projection]：cw rule-applicable → Rust daemon HTTP thin client |
| T-1787322797727-697cd6f0 | CLI-053 [cli_command_projection]：cw rule-candidate → Rust daemon HTTP thin client |
| T-1787322797796-6da4f0b4 | CLI-054 [cli_command_projection]：cw rule-cleanup-sync-log → Rust daemon HTTP thin client |
| T-1787322797861-717ae658 | CLI-055 [cli_command_projection]：cw rule-extract → Rust daemon HTTP thin client |
| T-1787322797930-7595285c | CLI-056 [cli_command_projection]：cw rule-insert-block → Rust daemon HTTP thin client |
| T-1787322797980-7895fb80 | CLI-057 [cli_command_projection]：cw rule-list → Rust daemon HTTP thin client |
| T-1787322798105-800faaa0 | CLI-058 [cli_command_projection]：cw rule-seed-bootstrap → Rust daemon HTTP thin client |
| T-1787322798172-840ee260 | CLI-059 [cli_command_projection]：cw rule-sync → Rust daemon HTTP thin client |
| T-1787322798237-87e3fc18 | CLI-060 [cli_command_projection]：cw search → Rust daemon HTTP thin client |
| T-1787322798303-8bd1779c | CLI-061 [cli_command_projection]：cw semgrep → Rust daemon HTTP thin client |
| T-1787322798366-8f9cbfa8 | CLI-062 [cli_command_projection]：cw stats → Rust daemon HTTP thin client |
| T-1787322798433-939358c4 | CLI-063 [cli_command_projection]：cw status → Rust daemon HTTP thin client |
| T-1787322798497-9762fae0 | CLI-064 [cli_command_projection]：cw symbol → Rust daemon HTTP thin client |
| T-1787322798591-9d0507b8 | CLI-065 [cli_command_projection]：cw symbol-history → Rust daemon HTTP thin client |
| T-1787322798663-a14caa24 | CLI-066 [cli_command_projection]：cw test-impact → Rust daemon HTTP thin client |
| T-1787322798722-a4d2c340 | CLI-067 [cli_command_projection]：cw tests → Rust daemon HTTP thin client |
| T-1787322798801-a98790f0 | CLI-068 [cli_command_projection]：cw topo → Rust daemon HTTP thin client |
| T-1787322798858-ace77210 | CLI-069 [cli_command_projection]：cw uncommented → Rust daemon HTTP thin client |
| T-1787322798916-b06209dc | CLI-070 [cli_command_projection]：cw vuln-blast → Rust daemon HTTP thin client |
| T-1787322798971-b3a9f30c | CLI-071 [cli_command_projection]：cw who → Rust daemon HTTP thin client |
| T-1787322799021-b69c2990 | CLI-072 [cli_command_projection]：cw workspace → Rust daemon HTTP thin client |
| T-1787322799077-b9fe69b8 | CLI-073 [cli_command_projection]：cw identity-revoke → Rust daemon HTTP thin client |
| T-1787322799131-bd2db3c8 | CLI-074 [cli_command_projection]：cw local → Rust daemon HTTP thin client |
| T-1787322799188-c09db9b8 | CLI-075 [cli_command_projection]：cw local-apply → Rust daemon HTTP thin client |
| T-1787322799244-c3f2f9c0 | CLI-076 [cli_command_projection]：cw local-capture-auto → Rust daemon HTTP thin client |
| T-1787322799302-c75c1d94 | CLI-077 [cli_command_projection]：cw local-capture-manual → Rust daemon HTTP thin client |
| T-1787322799364-cb120c64 | CLI-078 [cli_command_projection]：cw local-changes → Rust daemon HTTP thin client |
| T-1787322799482-d215a638 | CLI-080 [cli_command_projection]：cw local-commits → Rust daemon HTTP thin client |
| T-1787322800040-f35e9b74 | CLI-089 [cli_command_projection]：cw local-split → Rust daemon HTTP thin client |
| T-1787322800112-f7a9dc0c | CLI-090 [cli_command_projection]：cw local-status → Rust daemon HTTP thin client |
| T-1787322800171-fb277560 | CLI-091 [task_projection]：cw local-task-exists → Rust daemon HTTP thin client |
| T-1787322800239-ff34cf18 | CLI-092 [task_projection]：cw local-task-list → Rust daemon HTTP thin client |
| T-1787322800298-02bb7c40 | CLI-093 [cli_command_projection]：cw local-tree → Rust daemon HTTP thin client |
| T-1787322800362-068ff314 | CLI-094 [assignment_projection]：cw internal lease-write → Rust daemon HTTP thin client |
| T-1787322800435-0aebf778 | CLI-095 [cli_command_projection]：cw run-subcommand-mode → Rust daemon HTTP thin client |
| T-1787322800492-0e4a5838 | CLI-096 [cli_command_projection]：cw main → Rust daemon HTTP thin client |
| T-1787322971676-e9aae4d4 | INT-001 [graph_snapshot]：internal stats_top_files → Rust daemon native |
| T-1787407700109-f5562c60 | P0-K：Role Worker 治理写路径授权与 live authority 对齐 remediation |
