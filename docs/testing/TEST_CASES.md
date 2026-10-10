# CallWarden 测试入口与参数规划清单（v7 · 自动生成）

> gen_test_cases.py只读静态源生成；没有运行产品或测试，不代表功能通过。
> 数字与输入hash见STATIC_AUDIT.json；策略/交付门禁见TESTING_PLAN.md。

## 1. 清单与证据边界

CLI分类21，顶层84；快照叶子234，提取233，未提取['server']。
MCP分类17，工具243；source/matrix/category名字集合静态一致。
固化schema集合一致=False；缺当前工具=['detect_call_cycles', 'detect_dependency_cycle']；历史额外项=['detect_cycle', 'detect_cycles']。漂移阻断清单冻结，不能凭总数相等补绿。
CLI snapshot未与当前runtime/argparse比对；MCP未取wire runtime schema。参数表亦为固化快照，默认值/类型/互斥和语义均待复核。
所有行状态为待合同化/未计入语义覆盖，表示未完成逐入口证据映射，不表示存量测试完全不存在。

## 2. 每行的合同义务

C1正常语义；C2逐参数等价类/默认/边界/互斥；C3精确负向及无非法副作用；C4适用写入/幂等/回滚；C5适用故障/恢复；C6跨端共享语义。
须填入真实corpus/profile/实体provenance、独立oracle、runnable selector、timeout/cleanup与证据；适用性不能从工具名推断。常驻入口用启动/ready/交互/停止。

## 3. CLI逐叶子（含未提取项）

| 稳定case族ID | 入口 | 分类 | 快照positionals | 快照options | 状态 |
|---|---|---|---|---|---|
| CLI:assignment create | `assignment create` | assignment_lease | task_id | -h,, --role, --agent-id, --session-id, --model-id, --json | 待合同化 |
| CLI:assignment revoke | `assignment revoke` | assignment_lease | assignment_id | -h,, --json | 待合同化 |
| CLI:assignment show | `assignment show` | assignment_lease | task_id | -h,, --role, --json | 待合同化 |
| CLI:audit keys | `audit keys` | audit_bootstrap |  | -h, | 待合同化 |
| CLI:audit rotate-key | `audit rotate-key` | audit_bootstrap |  | -h,, --key-id, --secret | 待合同化 |
| CLI:audit verify | `audit verify` | audit_bootstrap |  | -h,, --table, --limit | 待合同化 |
| CLI:bootstrap status | `bootstrap status` | audit_bootstrap |  | -h, | 待合同化 |
| CLI:brief | `brief` | query_search |  | -h, | 待合同化 |
| CLI:build-context activate | `build-context activate` | build_context | workspace_id, hash | -h, | 待合同化 |
| CLI:build-context delete | `build-context delete` | build_context | workspace_id, hash | -h, | 待合同化 |
| CLI:build-context edges | `build-context edges` | build_context | workspace_id, hash | -h,, --caller, --limit | 待合同化 |
| CLI:build-context import-compile-commands | `build-context import-compile-commands` | build_context | file, workspace_id | -h,, --name, --activate, --workspace-root | 待合同化 |
| CLI:build-context list | `build-context list` | build_context | workspace_id | -h, | 待合同化 |
| CLI:build-context register | `build-context register` | build_context | workspace_id, name | -h,, --flags, --defines, --includes, --activate | 待合同化 |
| CLI:build-context resolve | `build-context resolve` | build_context | workspace_id, hash | -h, | 待合同化 |
| CLI:build-context show | `build-context show` | build_context | workspace_id, hash | -h, | 待合同化 |
| CLI:call-chain | `call-chain` | call_chain | name | -h,, --depth | 待合同化 |
| CLI:callees | `callees` | call_chain | name | -h,, --qualified | 待合同化 |
| CLI:callers | `callers` | call_chain | name | -h,, --qualified | 待合同化 |
| CLI:check-gate | `check-gate` | audit_bootstrap | task_id | -h,, --resolve, --step-id | 待合同化 |
| CLI:churn | `churn` | code_health |  | -h,, --module, --window | 待合同化 |
| CLI:clone clear | `clone clear` | diagnostics |  | -h, | 待合同化 |
| CLI:clone detect | `clone detect` | diagnostics |  | -h,, --file-filter, --min-lines, --similarity | 待合同化 |
| CLI:clone list | `clone list` | diagnostics |  | -h,, --type, --min-similarity, --limit, --symbol | 待合同化 |
| CLI:clone stats | `clone stats` | diagnostics |  | -h, | 待合同化 |
| CLI:collab gate-trigger | `collab gate-trigger` | collab |  | -h,, --json, --gate-id, --clause, --value | 待合同化 |
| CLI:collab publish | `collab publish` | collab |  | -h,, --json, --workspace, --envelope | 待合同化 |
| CLI:collab reveal | `collab reveal` | collab |  | -h,, --json, --event-id, --task-id, --notes | 待合同化 |
| CLI:collab verdict | `collab verdict` | collab |  | -h,, --json, --task-id, --step-id, --contract-id, --contract-hash, --contract-revision, --role-contract-id, --role-contract-hash, --role-contract-revision, --snapshot-id, --request-id, --phase, --overall, --attestation, --amendment-ref, --clause-results, --findings, --view-manifest-hash, --verdict-id, --agent-id, --session-id, --model-id, --agent-instance-id, --role, --lease-token, --fencing-counter | 待合同化 |
| CLI:comment-coverage | `comment-coverage` | coverage_ownership |  | -h,, --by | 待合同化 |
| CLI:complexity | `complexity` | code_health | limit | -h,, --module | 待合同化 |
| CLI:config check-role | `config check-role` | workspace_database |  | -h, | 待合同化 |
| CLI:config explain | `config explain` | workspace_database |  | -h, | 待合同化 |
| CLI:config paths | `config paths` | workspace_database |  | -h, | 待合同化 |
| CLI:coupled-fns | `coupled-fns` | code_health | limit | -h, | 待合同化 |
| CLI:coupling | `coupling` | code_health |  | -h, | 待合同化 |
| CLI:coverage fn | `coverage fn` | coverage_ownership | name | -h, | 待合同化 |
| CLI:coverage import | `coverage import` | coverage_ownership | file | -h,, --format | 待合同化 |
| CLI:coverage uncovered | `coverage uncovered` | coverage_ownership |  | -h, | 待合同化 |
| CLI:daemon backup | `daemon backup` | daemon_ops |  | -h,, --output | 待合同化 |
| CLI:daemon bridge | `daemon bridge` | daemon_ops |  | -h,, --endpoint, --token-file | 待合同化 |
| CLI:daemon capability | `daemon capability` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon gc-cas | `daemon gc-cas` | daemon_ops | workspace_id | -h,, --grace-days | 待合同化 |
| CLI:daemon gc-snapshots | `daemon gc-snapshots` | daemon_ops |  | -h,, --keep-last | 待合同化 |
| CLI:daemon health | `daemon health` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon list | `daemon list` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon manifest | `daemon manifest` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon metrics | `daemon metrics` | daemon_ops |  | -h,, --format, --name, --from-file | 待合同化 |
| CLI:daemon mode | `daemon mode` | daemon_ops |  | -h,, --set | 待合同化 |
| CLI:daemon mount | `daemon mount` | daemon_ops | register, list, delete | -h, | 待合同化 |
| CLI:daemon ping | `daemon ping` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon publish | `daemon publish` | daemon_ops | workspace_id, db_path | -h,, --build-context | 待合同化 |
| CLI:daemon query | `daemon query` | daemon_ops | workspace_id, value | -h,, --qualified-name, --kind, --limit, --max-depth, --file-path, --fixed, --path, --include-all, --include-info, --reverse, --history | 待合同化 |
| CLI:daemon register | `daemon register` | daemon_ops | root | -h,, --git-remote, --git-head, --toolchain | 待合同化 |
| CLI:daemon restore | `daemon restore` | daemon_ops |  | -h,, --from | 待合同化 |
| CLI:daemon schema-version | `daemon schema-version` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon snapshot-evict | `daemon snapshot-evict` | daemon_ops | workspace_id | -h, | 待合同化 |
| CLI:daemon snapshot-list | `daemon snapshot-list` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon snapshot-stats | `daemon snapshot-stats` | daemon_ops |  | -h, | 待合同化 |
| CLI:daemon status | `daemon status` | daemon_ops | workspace_id | -h, | 待合同化 |
| CLI:daemon toolchain | `daemon toolchain` | daemon_ops | register, list, get, delete, bind, resolve, build, resolved | -h, | 待合同化 |
| CLI:dashboard | `dashboard` | code_health |  | -h,, --full, --with-cycles, --with-evolution, --risks, --top, --json | 待合同化 |
| CLI:defect build | `defect build` | semgrep_defects |  | -h, | 待合同化 |
| CLI:defect learn | `defect learn` | semgrep_defects | commit_hash | -h, | 待合同化 |
| CLI:defect search | `defect search` | semgrep_defects |  | -h,, --category, --severity, --limit | 待合同化 |
| CLI:defect stats | `defect stats` | semgrep_defects |  | -h, | 待合同化 |
| CLI:defect suggest | `defect suggest` | semgrep_defects | symbol_hash | -h,, --finding | 待合同化 |
| CLI:dependency cycle | `dependency cycle` | dependency |  | -h,, --json | 待合同化 |
| CLI:dependency explain | `dependency explain` | dependency |  | -h,, --contract-id, --revision, --json | 待合同化 |
| CLI:dependency inspect | `dependency inspect` | dependency |  | -h,, --task-id, --contract-id, --revision, --json | 待合同化 |
| CLI:dependency list | `dependency list` | dependency |  | -h,, --contract-id, --json | 待合同化 |
| CLI:dependency provider-select | `dependency provider-select` | dependency |  | -h,, --consumer-task-id, --contract-id, --revision, --interface-name, --provider-task-id, --json | 待合同化 |
| CLI:doctor | `doctor` | diagnostics |  | -h,, --add-defender-exclusion | 待合同化 |
| CLI:evolution | `evolution` | code_health | qualified_name | -h,, --window, --defects | 待合同化 |
| CLI:experiment admit | `experiment admit` | experiment | task_id, batch_id | -h,, --strata, --pair-slot, --pair-id, --notes-file, --scope-contract, --json | 待合同化 |
| CLI:experiment batch-create | `experiment batch-create` | experiment |  | -h,, --seed, --min-valid, --min-nontrivial, --assignment-mode, --json | 待合同化 |
| CLI:experiment batch-list | `experiment batch-list` | experiment |  | -h,, --json | 待合同化 |
| CLI:experiment batch-lock | `experiment batch-lock` | experiment | batch_id | -h,, --json | 待合同化 |
| CLI:experiment pause | `experiment pause` | experiment | batch_id | -h,, --trigger, --reason, --json | 待合同化 |
| CLI:experiment record-incident | `experiment record-incident` | experiment | task_id, batch_id | -h,, --type, --reason-code, --detail, --json | 待合同化 |
| CLI:experiment record-invalid | `experiment record-invalid` | experiment | task_id, batch_id | -h,, --reason-code, --detail, --json | 待合同化 |
| CLI:experiment record-metrics | `experiment record-metrics` | experiment | task_id, batch_id | -h,, --tp, --fp, --misses, --duration, --tokens, --tokens-source, --tokens-unavailable-reason, --reopen, --defects, --rollbacks, --obs-window, --group, --nontrivial, --json | 待合同化 |
| CLI:experiment record-reveal | `experiment record-reveal` | experiment | task_id, batch_id | -h,, --sealed, --notes-file, --json | 待合同化 |
| CLI:experiment record-verdict | `experiment record-verdict` | experiment | task_id, batch_id | -h,, --changed, --reason-code, --json | 待合同化 |
| CLI:experiment report | `experiment report` | experiment | batch_id | -h,, --artifacts-dir, --json | 待合同化 |
| CLI:experiment toggle-set | `experiment toggle-set` | experiment |  | -h,, --scope, --value, --scope-key, --json | 待合同化 |
| CLI:experiment toggle-show | `experiment toggle-show` | experiment |  | -h,, --task-id, --workspace-id, --json | 待合同化 |
| CLI:file | `file` | query_search | path | -h, | 待合同化 |
| CLI:fn-metrics | `fn-metrics` | code_health | name | -h, | 待合同化 |
| CLI:fts rebuild | `fts rebuild` | query_search |  | -h, | 待合同化 |
| CLI:fts status | `fts status` | query_search |  | -h, | 待合同化 |
| CLI:function-issues | `function-issues` | semgrep_defects | fn | -h,, --type, --module, --limit | 待合同化 |
| CLI:gc archive | `gc archive` | gc |  | -h,, --force, --dry-run | 待合同化 |
| CLI:gc archive-import | `gc archive-import` | gc | path | -h,, --file, --package, --dry-run, --apply | 待合同化 |
| CLI:gc archive-inspect | `gc archive-inspect` | gc | path | -h, | 待合同化 |
| CLI:gc archive-list | `gc archive-list` | gc |  | -h,, --limit | 待合同化 |
| CLI:gc audit-list | `gc audit-list` | gc |  | -h,, --limit, --operation | 待合同化 |
| CLI:gc audit-show | `gc audit-show` | gc | id | -h, | 待合同化 |
| CLI:gc db-cleanup | `gc db-cleanup` | gc |  | -h,, --dry-run, --apply, --all-but-current | 待合同化 |
| CLI:gc db-migrate-single | `gc db-migrate-single` | gc |  | -h,, --dry-run, --apply, --no-backup | 待合同化 |
| CLI:gc policy | `gc policy` | gc | show, set | -h, | 待合同化 |
| CLI:gc purge | `gc purge` | gc |  | -h,, --older-than | 待合同化 |
| CLI:gc restore | `gc restore` | gc |  | -h,, --path, --force | 待合同化 |
| CLI:gc retention | `gc retention` | gc |  | -h,, --older-than, --keep-versions, --include-external,, --external-stale-days, --backup,, --vacuum,, --dry-run, --apply, --save-policy | 待合同化 |
| CLI:gc status | `gc status` | gc |  | -h, | 待合同化 |
| CLI:git check-push | `git check-push` | git | local_ref, local_sha, remote_ref, remote_sha | -h, | 待合同化 |
| CLI:git check-ref-transaction | `git check-ref-transaction` | git | old_value, new_value, ref_name, flags | -h, | 待合同化 |
| CLI:git check-task | `git check-task` | git |  | -h, | 待合同化 |
| CLI:git destructive-log | `git destructive-log` | git | limit | -h,, --type | 待合同化 |
| CLI:git import | `git import` | git | limit | -h, | 待合同化 |
| CLI:git log | `git log` | git | limit | -h, | 待合同化 |
| CLI:git show | `git show` | git | commit | -h, | 待合同化 |
| CLI:git stats | `git stats` | git |  | -h, | 待合同化 |
| CLI:graph build-from-c | `graph build-from-c` | workspace_database | directory | -h,, --threads, --dump, --max-files, --query | 待合同化 |
| CLI:grep | `grep` | query_search | patterns, same | -h,, --fixed, --limit, --path, --include-all, --kind | 待合同化 |
| CLI:guardrail rules | `guardrail rules` | audit_bootstrap |  | -h,, --category | 待合同化 |
| CLI:guardrail scan | `guardrail scan` | audit_bootstrap |  | -h,, --file, --category | 待合同化 |
| CLI:health-report | `health-report` | code_health |  | -h,, --json | 待合同化 |
| CLI:hotspot | `hotspot` | code_health |  | -h,, --module, --limit | 待合同化 |
| CLI:identity revoke | `identity revoke` | identity |  | -h,, --issuer, --signing-key-id, --revocation-mode, --reason, --agent-id, --session-id, --model-id, --role, --json | 待合同化 |
| CLI:impact | `impact` | call_chain | symbol_hash | -h,, --depth | 待合同化 |
| CLI:install | `install` | setup_install |  | -h,, --all, --lang, --check, --hooks, --force-hooks, --no-post-commit, --no-optional, --verbose, --agent, --detect-agents, --force-agent, --agent-project | 待合同化 |
| CLI:install-agent all | `install-agent all` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent antigravity | `install-agent antigravity` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent claude-code | `install-agent claude-code` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent claude-desktop | `install-agent claude-desktop` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent cline | `install-agent cline` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent cline-cli | `install-agent cline-cli` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent codebuddy-cli | `install-agent codebuddy-cli` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent codex | `install-agent codex` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent comate | `install-agent comate` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent cursor | `install-agent cursor` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent deep-code | `install-agent deep-code` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent devin-cli | `install-agent devin-cli` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent gemini-cli | `install-agent gemini-cli` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent grok-build | `install-agent grok-build` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent jetbrains-junie | `install-agent jetbrains-junie` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent kimi-code | `install-agent kimi-code` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent kiro | `install-agent kiro` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent opencode | `install-agent opencode` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent pearai | `install-agent pearai` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent qoder | `install-agent qoder` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent trae | `install-agent trae` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent windsurf | `install-agent windsurf` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent zcode | `install-agent zcode` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-agent zed | `install-agent zed` | setup_install |  | -h,, --output-dir, --force, --global, --auto-detect, --registry | 待合同化 |
| CLI:install-hook post-commit | `install-hook post-commit` | setup_install |  | -h,, --task-id, --uninstall | 待合同化 |
| CLI:issues | `issues` | semgrep_defects | qualified_name | -h,, --include-info | 待合同化 |
| CLI:largest-fns | `largest-fns` | code_health | limit | -h, | 待合同化 |
| CLI:lease acquire | `lease acquire` | assignment_lease | task_id | -h,, --role, --agent-id, --session-id, --model-id, --agent-instance-id, --ttl, --json | 待合同化 |
| CLI:lease list | `lease list` | assignment_lease |  | -h,, --task-id, --role, --json | 待合同化 |
| CLI:lease release | `lease release` | assignment_lease | task_id | -h,, --role, --token, --agent-id, --session-id, --model-id, --agent-instance-id, --json | 待合同化 |
| CLI:lease renew | `lease renew` | assignment_lease | task_id | -h,, --role, --token, --agent-id, --session-id, --model-id, --agent-instance-id, --ttl, --json | 待合同化 |
| CLI:lease status | `lease status` | assignment_lease | task_id | -h,, --role, --json | 待合同化 |
| CLI:map mermaid | `map mermaid` | query_search |  | -h,, --format | 待合同化 |
| CLI:map text | `map text` | query_search |  | -h,, --format | 待合同化 |
| CLI:metrics | `metrics` | code_health |  | -h, | 待合同化 |
| CLI:ownership-map | `ownership-map` | coverage_ownership |  | -h, | 待合同化 |
| CLI:query | `query` | query_search | name, file | -h, | 待合同化 |
| CLI:refresh | `refresh` | workspace_database | paths | -h,, --all, --force | 待合同化 |
| CLI:review | `review` | semgrep_defects | symbol_hash | -h, | 待合同化 |
| CLI:rollback config | `rollback config` | rollback |  | -h,, --phase, --flag | 待合同化 |
| CLI:rollback is-rolled-back | `rollback is-rolled-back` | rollback | feature_name | -h, | 待合同化 |
| CLI:rollback register | `rollback register` | rollback |  | -h,, --task-id, --feature, --phase, --production-entry, --rollback-entry, --window, --config-json | 待合同化 |
| CLI:rollback set | `rollback set` | rollback | task_id | -h,, --reason | 待合同化 |
| CLI:rollback show | `rollback show` | rollback | task_id | -h, | 待合同化 |
| CLI:rule applicable | `rule applicable` | rule_memory |  | -h,, --context, --limit | 待合同化 |
| CLI:rule candidate | `rule candidate` | rule_memory | create, list, accept, reject | -h, | 待合同化 |
| CLI:rule cleanup-sync-log | `rule cleanup-sync-log` | rule_memory |  | -h,, --older-than, --keep-latest, --apply | 待合同化 |
| CLI:rule extract | `rule extract` | rule_memory |  | -h,, --task-id, --min-occurrences | 待合同化 |
| CLI:rule insert-block | `rule insert-block` | rule_memory |  | -h,, --target, --actor | 待合同化 |
| CLI:rule list | `rule list` | rule_memory |  | -h,, --status, --limit | 待合同化 |
| CLI:rule seed-bootstrap | `rule seed-bootstrap` | rule_memory |  | -h,, --apply | 待合同化 |
| CLI:rule sync | `rule sync` | rule_memory |  | -h,, --target, --apply, --actor | 待合同化 |
| CLI:search | `search` | query_search | query | -h,, --kind, --limit | 待合同化 |
| CLI:semgrep list | `semgrep list` | semgrep_defects | filter | -h,, --severity, --lang, --limit | 待合同化 |
| CLI:semgrep scan | `semgrep scan` | semgrep_defects | paths | -h,, --config, --lang, --timeout, --save, --quick, --incremental, --base, --head | 待合同化 |
| CLI:semgrep stats | `semgrep stats` | semgrep_defects |  | -h, | 待合同化 |
| CLI:server | `server` | setup_install | 未提取 | 未提取 | 待合同化 |
| CLI:setup | `setup` | setup_install |  | -h,, --force, --dry-run | 待合同化 |
| CLI:stats | `stats` | workspace_database |  | -h, | 待合同化 |
| CLI:status | `status` | workspace_database |  | -h, | 待合同化 |
| CLI:symbol | `symbol` | query_search | name | -h, | 待合同化 |
| CLI:symbol-history | `symbol-history` | git | symbol_hash | -h,, --limit | 待合同化 |
| CLI:task apply | `task apply` | task | task_id | -h,, --reviewer, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task assignment-heartbeat | `task assignment-heartbeat` | task | task_id, assignment_id | -h,, --request-id, --fencing-counter, --agent-session-id, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --json | 待合同化 |
| CLI:task assignment-status | `task assignment-status` | task | task_id | -h,, --step-id, --role, --json | 待合同化 |
| CLI:task attest-legacy-workspace-binding | `task attest-legacy-workspace-binding` | task | legacy_task_id, anchor_task_id | -h,, --workspace-id, --workspace-instance-id, --request-id, --evidence-path, --evidence-hash, --lease-token, --fencing-counter, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task bootstrap-executor-evidence | `task bootstrap-executor-evidence` | task | task_id, completely | -h,, --steps, --workspace-id, --workspace-instance-id, --request-id, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task bootstrap-reviewer-pass | `task bootstrap-reviewer-pass` | task | task_id, executor | -h,, --workspace-id, --workspace-instance-id, --request-id, --evidence-path, --evidence-hash, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task capture-diff | `task capture-diff` | task | task_id | -h,, --step-id, --base, --dry-run, --auto, --skip-quality-review, --source-commit-hash | 待合同化 |
| CLI:task cascade-close | `task cascade-close` | task | task_id | -h,, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task claim-recover | `task claim-recover` | task | task_id | -h,, --reason, --request-id, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task close | `task close` | task | task_id | -h,, --reviewer, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task completion-review | `task completion-review` | task | task_id | -h,, --step-id | 待合同化 |
| CLI:task contract-bootstrap | `task contract-bootstrap` | task | task_id, completely | -h,, --envelope-path, --workspace-id, --workspace-instance-id, --request-id, --evidence-path, --evidence-hash, --lease-token, --fencing-counter, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task contract-revise | `task contract-revise` | task | task_id | -h,, --envelope-path, --expected-previous-hash, --workspace-id, --workspace-instance-id, --request-id, --evidence-path, --evidence-hash, --lease-token, --fencing-counter, --agent-id, --agent-instance-id, --session-id, --model-id, --role | 待合同化 |
| CLI:task create | `task create` | task |  | -h,, --title, --desc, --steps, --role-contracts, --workspace-id, --workspace-instance-id, --parent-id, --identity-policy, --task-contract-envelope, --task-id | 待合同化 |
| CLI:task findings | `task findings` | task | task_id | -h,, --status, --severity | 待合同化 |
| CLI:task governance-projection | `task governance-projection` | task | task_id | -h,, --json | 待合同化 |
| CLI:task handoff | `task handoff` | task | task_id | -h,, --from-role, --outcome, --next-role, --next-action, --reason, --independence-requirement, --request-id, --step-id, --report-request-id, --evidence-path, --evidence-hash, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task list | `task list` | task |  | -h,, --blocked, --limit, --status, --flat | 待合同化 |
| CLI:task next | `task next` | task | task_id | -h,, --remediation-step-id, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task next-action | `task next-action` | task | task_id | -h,, --workspace-instance-id, --json | 待合同化 |
| CLI:task prompt | `task prompt` | task | task_id | -h,, --format, --expected-workspace-instance-id | 待合同化 |
| CLI:task reopen | `task reopen` | task | task_id | -h,, --reviewer, --reason, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task report | `task report` | task | task_id, step_id | -h,, --result, --fail, --evidence-path, --evidence-hash, --snapshot-id, --changes-json, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task resolve-finding | `task resolve-finding` | task | finding_id | -h,, --resolution, --by | 待合同化 |
| CLI:task rollback | `task rollback` | task | task_id, step_id | -h, | 待合同化 |
| CLI:task show | `task show` | task | task_id | -h,, --flat | 待合同化 |
| CLI:task split | `task split` | task | task_id | -h,, --plan, --identity-policy | 待合同化 |
| CLI:task status-tree | `task status-tree` | task | task_id | -h, | 待合同化 |
| CLI:task step-resolve | `task step-resolve` | task | task_id, failed_step_id, remediation_step_id, request_id | -h,, --evidence-path, --evidence-hash, --json, --agent-id, --session-id, --model-id, --role, --agent-instance-id, --lease-token, --fencing-counter | 待合同化 |
| CLI:task supersede | `task supersede` | task | old, new | -h,, --reason, --request-id, --evidence-path, --evidence-hash, --lease-token, --fencing-counter, --agent-id, --session-id, --model-id, --role, --agent-instance-id | 待合同化 |
| CLI:task superseded | `task superseded` | task | id | -h, | 待合同化 |
| CLI:test | `test` | setup_install |  |  | 待合同化 |
| CLI:test-impact | `test-impact` | coverage_ownership | qualified_name | -h, | 待合同化 |
| CLI:tests | `tests` | coverage_ownership | qualified_name | -h,, --reverse, --build, --force, --history, --import, --ci-run-id, --ci-url, --limit | 待合同化 |
| CLI:toolchain bind | `toolchain bind` | build_context | workspace_id, toolchain_name | -h,, --build-context-hash | 待合同化 |
| CLI:toolchain delete | `toolchain delete` | build_context | name_or_id | -h, | 待合同化 |
| CLI:toolchain list | `toolchain list` | build_context |  | -h, | 待合同化 |
| CLI:toolchain list-bound | `toolchain list-bound` | build_context | workspace_id | -h,, --build-context-hash | 待合同化 |
| CLI:toolchain register | `toolchain register` | build_context | name, compiler_path | -h,, --sysroot, --description, --no-probe | 待合同化 |
| CLI:toolchain show | `toolchain show` | build_context | name_or_id | -h, | 待合同化 |
| CLI:topo | `topo` | call_chain |  | -h,, --limit | 待合同化 |
| CLI:uncommented | `uncommented` | coverage_ownership | kind | -h,, --module, --limit | 待合同化 |
| CLI:vuln-blast | `vuln-blast` | semgrep_defects |  | -h,, --finding-id, --severity, --depth | 待合同化 |
| CLI:who | `who` | coverage_ownership | file | -h, | 待合同化 |
| CLI:workspace delete | `workspace delete` | workspace_database | id_or_name | -h, | 待合同化 |
| CLI:workspace generate-ignore | `workspace generate-ignore` | workspace_database | dir | -h,, --apply | 待合同化 |
| CLI:workspace list | `workspace list` | workspace_database |  | -h, | 待合同化 |
| CLI:workspace register | `workspace register` | workspace_database | name, root | -h, | 待合同化 |
| CLI:workspace scan | `workspace scan` | workspace_database | dir | -h,, --register, --include-all, --deep | 待合同化 |
| CLI:workspace set | `workspace set` | workspace_database | id_or_name | -h, | 待合同化 |

## 4. MCP逐工具（矩阵声明，不是副作用实测）

| 稳定case族ID | 分类 | backend / op_class | 必填快照参数 | 可选快照参数 | 状态 |
|---|---|---|---|---|---|
| MCP:append_evidence | collab | rust_native / GOVERNANCE_WRITE | task_id, step_id, evidence_id, evidence_type, manifest_path | contract_hash, contract_id, contract_revision, fencing_counter, identity_agent_id, identity_model_id, identity_role, identity_session_id, lease_token, payload, payload_hash, producer_identity, request_id, snapshot_id, test_run_id, verifier_config_hash, verifier_name, verifier_version | 待合同化；快照待复核 |
| MCP:ask_codebase | query_search | rust_native / READ_ONLY | question | include_callees, include_callers, max_tokens, top_k | 待合同化；快照待复核 |
| MCP:assignment_create | assignment_lease | rust_native / PROTECTED_MUTATION | task_id | agent_id, model_id, role, session_id | 待合同化；快照待复核 |
| MCP:assignment_revoke | assignment_lease | rust_native / PROTECTED_MUTATION | assignment_id |  | 待合同化；快照待复核 |
| MCP:assignment_show | assignment_lease | rust_native / READ_ONLY | task_id | role | 待合同化；快照待复核 |
| MCP:audit_verify_chain | audit_bootstrap | rust_native / READ_ONLY |  | limit, table_name | 待合同化；快照待复核 |
| MCP:blast_radius | semgrep_defects | rust_native / READ_ONLY | symbol_hash | depth | 待合同化；快照待复核 |
| MCP:bootstrap_status | audit_bootstrap | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:build_directory | workspace_database | rust_native / PROTECTED_MUTATION | dir_path |  | 待合同化；快照待复核 |
| MCP:build_graph | workspace_database | rust_native / PROTECTED_MUTATION |  | workspace_instance_id | 待合同化；快照待复核 |
| MCP:build_hard_dependency_edges | dependency | task_rpc / PROTECTED_MUTATION | workspace_id, contract_id, contract_revision |  | 待合同化；快照待复核 |
| MCP:cancel_job | task | task_rpc / PROTECTED_MUTATION | job_id |  | 待合同化；快照待复核 |
| MCP:check_action_identity | identity | rust_native / READ_ONLY | identity | require_role | 待合同化；快照待复核 |
| MCP:check_file_health | code_health | rust_native / READ_ONLY | file_path |  | 待合同化；快照待复核 |
| MCP:check_session_separation | identity | rust_native / READ_ONLY | reviewer_identity, implementer_identity |  | 待合同化；快照待复核 |
| MCP:churn_analysis | code_health | rust_native / READ_ONLY |  | module_filter, time_window | 待合同化；快照待复核 |
| MCP:cleanup_agent_rule_sync_log | rule_memory | rust_native / PROTECTED_MUTATION |  | dry_run, keep_latest, older_than_days | 待合同化；快照待复核 |
| MCP:clear_clones | diagnostics | rust_native / PROTECTED_MUTATION |  |  | 待合同化；快照待复核 |
| MCP:compare_snapshots | git | rust_native / READ_ONLY | left_workspace_id, right_workspace_id | scope_type, scope_value | 待合同化；快照待复核 |
| MCP:count_resolved_edges | build_context | rust_native / READ_ONLY | workspace_id, build_context_hash |  | 待合同化；快照待复核 |
| MCP:cross_layer_impact | semgrep_defects | rust_native / READ_ONLY | symbol_hash |  | 待合同化；快照待复核 |
| MCP:cross_repo_impact | diagnostics | rust_native / READ_ONLY | symbol_hash | depth | 待合同化；快照待复核 |
| MCP:cross_repo_summary | diagnostics | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:defect_correlation | code_health | rust_native / READ_ONLY | symbol_hash | window_commits | 待合同化；快照待复核 |
| MCP:defect_learn | semgrep_defects | rust_native / READ_ONLY | fix_commit_hash |  | 待合同化；快照待复核 |
| MCP:defect_search | semgrep_defects | rust_native / READ_ONLY |  | category, severity_filter | 待合同化；快照待复核 |
| MCP:defect_stats | semgrep_defects | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:defect_suggest_fix | semgrep_defects | rust_native / READ_ONLY | symbol_hash | finding_id | 待合同化；快照待复核 |
| MCP:delete_workspace | workspace_database | rust_native / PROTECTED_MUTATION | workspace_id_or_name |  | 待合同化；快照待复核 |
| MCP:detect_call_cycles | call_chain | rust_native / READ_ONLY | 未知（schema缺失） | 未知（schema缺失） | 待合同化；SCHEMA缺失，阻断 |
| MCP:detect_clones | diagnostics | task_rpc / PROTECTED_MUTATION |  | file_filter, min_lines, similarity_threshold | 待合同化；快照待复核 |
| MCP:detect_clones_async | diagnostics | task_rpc / PROTECTED_MUTATION |  | file_filter, min_lines, similarity_threshold | 待合同化；快照待复核 |
| MCP:detect_cross_repo_deps | diagnostics | task_rpc / PROTECTED_MUTATION | source_workspace | target_workspace | 待合同化；快照待复核 |
| MCP:detect_dependency_cycle | dependency | rust_native / READ_ONLY | 未知（schema缺失） | 未知（schema缺失） | 待合同化；SCHEMA缺失，阻断 |
| MCP:diff_branches | workspace_database | rust_native / READ_ONLY | source_branch, target_branch |  | 待合同化；快照待复核 |
| MCP:diff_callees | call_chain | rust_native / READ_ONLY | symbol_a, symbol_b |  | 待合同化；快照待复核 |
| MCP:diff_callers | call_chain | rust_native / READ_ONLY | symbol_a, symbol_b |  | 待合同化；快照待复核 |
| MCP:diff_to_symbol | semgrep_defects | rust_native / READ_ONLY | diff_text |  | 待合同化；快照待复核 |
| MCP:embed_single_symbol | query_search | task_rpc / PROTECTED_MUTATION | symbol_hash |  | 待合同化；快照待复核 |
| MCP:embed_symbols | query_search | task_rpc / PROTECTED_MUTATION |  | force | 待合同化；快照待复核 |
| MCP:embed_symbols_async | query_search | task_rpc / PROTECTED_MUTATION |  | batch_size, force | 待合同化；快照待复核 |
| MCP:evolution_frequency | code_health | rust_native / READ_ONLY | qualified_name | time_window | 待合同化；快照待复核 |
| MCP:export_module_graph | call_chain | rust_native / READ_ONLY |  | format | 待合同化；快照待复核 |
| MCP:extract_rule_candidates_from_quality_findings | rule_memory | rust_native / PROTECTED_MUTATION |  | min_occurrences, task_id | 待合同化；快照待复核 |
| MCP:file_grep | query_search | rust_native / READ_ONLY | pattern | glob, head_limit, output_mode, path | 待合同化；快照待复核 |
| MCP:file_list | query_search | rust_native / READ_ONLY |  | glob, path | 待合同化；快照待复核 |
| MCP:file_read | query_search | rust_native / READ_ONLY | file_path | include_context, limit, offset | 待合同化；快照待复核 |
| MCP:file_symbol_content | query_search | rust_native / READ_ONLY | file_path, symbol_name |  | 待合同化；快照待复核 |
| MCP:find_evidence | collab | rust_native / READ_ONLY |  | contract_id, limit, task_id, verifier | 待合同化；快照待复核 |
| MCP:find_issues | semgrep_defects | rust_native / READ_ONLY |  | issue_type, limit | 待合同化；快照待复核 |
| MCP:find_shared_symbols | diagnostics | rust_native / READ_ONLY |  | workspace_a, workspace_b | 待合同化；快照待复核 |
| MCP:find_similar_functions | query_search | rust_native / READ_ONLY | qualified_name | threshold, top_k | 待合同化；快照待复核 |
| MCP:find_uncovered_functions | coverage_ownership | rust_native / READ_ONLY |  | module_filter, threshold | 待合同化；快照待复核 |
| MCP:gc_archive_import | gc | rust_native / PROTECTED_MUTATION | path | dry_run, file_path, package_name | 待合同化；快照待复核 |
| MCP:gc_archive_inspect | gc | rust_native / READ_ONLY | path |  | 待合同化；快照待复核 |
| MCP:gc_archive_list | gc | rust_native / READ_ONLY |  | limit | 待合同化；快照待复核 |
| MCP:gc_audit_get | gc | rust_native / READ_ONLY | audit_id |  | 待合同化；快照待复核 |
| MCP:gc_audit_list | gc | rust_native / READ_ONLY |  | limit, operation | 待合同化；快照待复核 |
| MCP:gc_policy_get | gc | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:gc_policy_set | gc | rust_native / PROTECTED_MUTATION |  | backup_enabled, external_stale_days, include_external, keep_versions, older_than_days, vacuum_enabled | 待合同化；快照待复核 |
| MCP:gc_retention | gc | rust_native / READ_ONLY |  | backup, dry_run, external_stale_days, include_external, keep_versions, older_than_days, save_policy, vacuum | 待合同化；快照待复核 |
| MCP:generate_summary | query_search | rust_native / PROTECTED_MUTATION | qualified_name, summary | model | 待合同化；快照待复核 |
| MCP:get_action_identity | identity | rust_native / READ_ONLY | action_id | workspace_id | 待合同化；快照待复核 |
| MCP:get_active_build_context | build_context | rust_native / READ_ONLY | workspace_id |  | 待合同化；快照待复核 |
| MCP:get_active_workspace | workspace_database | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_applicable_rules | rule_memory | rust_native / READ_ONLY | context | limit | 待合同化；快照待复核 |
| MCP:get_artifact_freshness | dependency | rust_native / READ_ONLY | workspace_id, task_id | artifact_ref | 待合同化；快照待复核 |
| MCP:get_attestation_validity | identity | rust_native / READ_ONLY | issuer, signing_key_id, issuance_time | workspace_id | 待合同化；快照待复核 |
| MCP:get_build_context | build_context | rust_native / READ_ONLY | workspace_id, build_context_hash |  | 待合同化；快照待复核 |
| MCP:get_call_chain_down | call_chain | rust_native / READ_ONLY | qualified_name | max_depth | 待合同化；快照待复核 |
| MCP:get_call_heatmap | call_chain | rust_native / READ_ONLY |  | group_by, top_n | 待合同化；快照待复核 |
| MCP:get_callees | call_chain | rust_native / READ_ONLY | caller_name | qualified_name | 待合同化；快照待复核 |
| MCP:get_callers | call_chain | rust_native / READ_ONLY | callee_name | qualified_name | 待合同化；快照待复核 |
| MCP:get_clone_aware_impact | diagnostics | rust_native / READ_ONLY | qualified_name | depth | 待合同化；快照待复核 |
| MCP:get_clone_group_detail | diagnostics | rust_native / READ_ONLY | group_id | members_limit | 待合同化；快照待复核 |
| MCP:get_clone_group_stats | diagnostics | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_clone_stats | diagnostics | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_code_health_check | code_health | rust_native / READ_ONLY |  | severity | 待合同化；快照待复核 |
| MCP:get_code_metrics_summary | code_health | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_comment_coverage | coverage_ownership | rust_native / READ_ONLY |  | group_by | 待合同化；快照待复核 |
| MCP:get_comment_from_version | coverage_ownership | rust_native / READ_ONLY | spec |  | 待合同化；快照待复核 |
| MCP:get_commit_changes | git | rust_native / READ_ONLY | commit_hash |  | 待合同化；快照待复核 |
| MCP:get_commit_tasks | task | rust_native / READ_ONLY | commit_hash | include_task_details | 待合同化；快照待复核 |
| MCP:get_complexity_hotspots | code_health | rust_native / READ_ONLY |  | limit, module_filter | 待合同化；快照待复核 |
| MCP:get_coupling_analysis | code_health | rust_native / READ_ONLY |  | limit | 待合同化；快照待复核 |
| MCP:get_coverage_for_symbol | coverage_ownership | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_deepest_functions | call_chain | rust_native / READ_ONLY |  | kind, limit, module_filter | 待合同化；快照待复核 |
| MCP:get_defect_correlation | semgrep_defects | rust_native / READ_ONLY | qualified_name | window_commits | 待合同化；快照待复核 |
| MCP:get_dependency_edges | dependency | rust_native / READ_ONLY | workspace_id | task_id | 待合同化；快照待复核 |
| MCP:get_edit_history | diagnostics | rust_native / READ_ONLY |  | file_path, limit | 待合同化；快照待复核 |
| MCP:get_edit_stats | diagnostics | rust_native / READ_ONLY |  | time_window | 待合同化；快照待复核 |
| MCP:get_file_history | query_search | rust_native / READ_ONLY | file_path |  | 待合同化；快照待复核 |
| MCP:get_file_symbols | query_search | rust_native / READ_ONLY | file_path |  | 待合同化；快照待复核 |
| MCP:get_freshness_status | collab | rust_native / READ_ONLY |  | evidence_id, task_id | 待合同化；快照待复核 |
| MCP:get_function_metrics | code_health | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_gate_decision | collab | rust_native / READ_ONLY |  | gate_id, limit, task_id | 待合同化；快照待复核 |
| MCP:get_git_commits | git | rust_native / READ_ONLY |  | limit, offset | 待合同化；快照待复核 |
| MCP:get_git_stats | git | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_impact | call_chain | rust_native / READ_ONLY | qualified_name | max_depth | 待合同化；快照待复核 |
| MCP:get_interface_providers | dependency | rust_native / READ_ONLY | workspace_id, interface_name | version | 待合同化；快照待复核 |
| MCP:get_issue_summary | semgrep_defects | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_job_stats | task | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_job_status | task | rust_native / READ_ONLY | job_id |  | 待合同化；快照待复核 |
| MCP:get_largest_functions | code_health | rust_native / READ_ONLY |  | limit, module_filter | 待合同化；快照待复核 |
| MCP:get_metrics | diagnostics | rust_native / READ_ONLY |  | format, name, reset, source | 待合同化；快照待复核 |
| MCP:get_module_call_stats | call_chain | rust_native / READ_ONLY |  | limit | 待合同化；快照待复核 |
| MCP:get_most_coupled_functions | code_health | rust_native / READ_ONLY |  | limit | 待合同化；快照待复核 |
| MCP:get_orphan_symbols | call_chain | rust_native / READ_ONLY |  | kind, limit, module_filter | 待合同化；快照待复核 |
| MCP:get_ownership_map | coverage_ownership | rust_native / READ_ONLY |  | module_filter | 待合同化；快照待复核 |
| MCP:get_project_dependencies | gc | rust_native / READ_ONLY |  | languages | 待合同化；快照待复核 |
| MCP:get_recent_changes | query_search | rust_native / READ_ONLY |  | since | 待合同化；快照待复核 |
| MCP:get_resolved_edges | build_context | rust_native / READ_ONLY | workspace_id, build_context_hash | caller_symbol_id, limit | 待合同化；快照待复核 |
| MCP:get_role_view | collab | rust_native / READ_ONLY | task_id | role | 待合同化；快照待复核 |
| MCP:get_semgrep_findings | semgrep_defects | rust_native / READ_ONLY |  | language, limit, rule_id, severity | 待合同化；快照待复核 |
| MCP:get_semgrep_stats | semgrep_defects | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_stats | workspace_database | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_status | workspace_database | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_summary | query_search | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_symbol | query_search | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_symbol_change_tasks | task | rust_native / READ_ONLY |  | limit, qualified_name, symbol_hash | 待合同化；快照待复核 |
| MCP:get_symbol_commit_history | git | rust_native / READ_ONLY | symbol_hash | limit | 待合同化；快照待复核 |
| MCP:get_symbol_content_by_hash | query_search | rust_native / READ_ONLY | content_hash |  | 待合同化；快照待复核 |
| MCP:get_symbol_history | query_search | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_symbol_issues | semgrep_defects | rust_native / READ_ONLY | qualified_name | include_info | 待合同化；快照待复核 |
| MCP:get_symbol_location | query_search | rust_native / READ_ONLY | name | file_path | 待合同化；快照待复核 |
| MCP:get_task_commits | task | task_rpc / READ_ONLY | task_id | include_commit_details | 待合同化；快照待复核 |
| MCP:get_task_symbol_changes | task | task_rpc / READ_ONLY | task_id | file_path, limit, step_id | 待合同化；快照待复核 |
| MCP:get_test_cases | coverage_ownership | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_test_coverage | coverage_ownership | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:get_test_coverage_summary | coverage_ownership | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_test_stability | coverage_ownership | rust_native / READ_ONLY | qualified_name | limit | 待合同化；快照待复核 |
| MCP:get_tested_functions | coverage_ownership | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:get_token_savings_report | query_search | rust_native / READ_ONLY |  | time_window | 待合同化；快照待复核 |
| MCP:get_toolchain | build_context | rust_native / READ_ONLY | name_or_id |  | 待合同化；快照待复核 |
| MCP:get_top_callers | call_chain | rust_native / READ_ONLY |  | kind, limit, module_filter | 待合同化；快照待复核 |
| MCP:get_topological_order | call_chain | rust_native / READ_ONLY |  | limit | 待合同化；快照待复核 |
| MCP:get_uncommented_symbols | coverage_ownership | rust_native / READ_ONLY |  | kind, limit, module_filter | 待合同化；快照待复核 |
| MCP:get_vulnerability_blast_radius | semgrep_defects | rust_native / READ_ONLY |  | depth, finding_id, severity_filter | 待合同化；快照待复核 |
| MCP:get_workspace_toolchains | build_context | rust_native / READ_ONLY | workspace_id | build_context_hash | 待合同化；快照待复核 |
| MCP:guardrail_add_rule | audit_bootstrap | rust_native / PROTECTED_MUTATION | category, pattern | action, description, severity | 待合同化；快照待复核 |
| MCP:guardrail_check_edit | audit_bootstrap | rust_native / READ_ONLY | file_path | proposed_change | 待合同化；快照待复核 |
| MCP:guardrail_list_rules | audit_bootstrap | rust_native / READ_ONLY |  | category_filter | 待合同化；快照待复核 |
| MCP:guardrail_scan | audit_bootstrap | rust_native / READ_ONLY |  | file_filter | 待合同化；快照待复核 |
| MCP:hotspot_evolution | code_health | rust_native / READ_ONLY |  | limit, module_filter | 待合同化；快照待复核 |
| MCP:import_codeowners | coverage_ownership | task_rpc / PROTECTED_MUTATION |  |  | 待合同化；快照待复核 |
| MCP:import_coverage | coverage_ownership | task_rpc / PROTECTED_MUTATION | file_path | format | 待合同化；快照待复核 |
| MCP:import_envelope_dependencies | dependency | task_rpc / PROTECTED_MUTATION | workspace_id, task_id, contract_id, contract_revision, dependencies |  | 待合同化；快照待复核 |
| MCP:import_git_blame | coverage_ownership | task_rpc / PROTECTED_MUTATION |  |  | 待合同化；快照待复核 |
| MCP:import_git_history | git | task_rpc / PROTECTED_MUTATION |  | max_commits | 待合同化；快照待复核 |
| MCP:import_project_dependencies | gc | task_rpc / PROTECTED_MUTATION |  |  | 待合同化；快照待复核 |
| MCP:lease_acquire | assignment_lease | rust_native / PROTECTED_MUTATION | task_id | agent_id, model_id, role, session_id, ttl_seconds | 待合同化；快照待复核 |
| MCP:lease_list_events | assignment_lease | rust_native / READ_ONLY |  | role, task_id | 待合同化；快照待复核 |
| MCP:lease_release | assignment_lease | rust_native / PROTECTED_MUTATION | task_id, role, token | agent_id, model_id, session_id | 待合同化；快照待复核 |
| MCP:lease_renew | assignment_lease | rust_native / PROTECTED_MUTATION | task_id, role, token | agent_id, model_id, session_id, ttl_seconds | 待合同化；快照待复核 |
| MCP:lease_status | assignment_lease | rust_native / READ_ONLY | task_id | role | 待合同化；快照待复核 |
| MCP:link_edit_audit_symbols | task | task_rpc / PROTECTED_MUTATION | audit_id | step_id | 待合同化；快照待复核 |
| MCP:list_attestation_revocations | identity | rust_native / READ_ONLY |  | issuer, signing_key_id, workspace_id | 待合同化；快照待复核 |
| MCP:list_audit_signing_keys | audit_bootstrap | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:list_branches | workspace_database | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:list_build_contexts | build_context | rust_native / READ_ONLY | workspace_id |  | 待合同化；快照待复核 |
| MCP:list_clone_groups | diagnostics | rust_native / READ_ONLY |  | clone_type, limit, min_similarity | 待合同化；快照待复核 |
| MCP:list_clones | diagnostics | rust_native / READ_ONLY |  | clone_type, limit, min_similarity, symbol_id | 待合同化；快照待复核 |
| MCP:list_jobs | task | rust_native / READ_ONLY |  | job_type, limit, status | 待合同化；快照待复核 |
| MCP:list_toolchains | build_context | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:list_workspaces | workspace_database | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:lsp_check_available | diagnostics | rust_native / READ_ONLY |  | language | 待合同化；快照待复核 |
| MCP:lsp_completion | diagnostics | rust_native / READ_ONLY | file_path, line, character |  | 待合同化；快照待复核 |
| MCP:lsp_definition | diagnostics | rust_native / READ_ONLY | file_path, line, character |  | 待合同化；快照待复核 |
| MCP:lsp_diagnostics | diagnostics | rust_native / READ_ONLY | file_path |  | 待合同化；快照待复核 |
| MCP:lsp_hover | diagnostics | rust_native / READ_ONLY | file_path, line, character |  | 待合同化；快照待复核 |
| MCP:lsp_references | diagnostics | rust_native / READ_ONLY | file_path, line, character | include_declaration | 待合同化；快照待复核 |
| MCP:merge_preview | workspace_database | rust_native / READ_ONLY | source_branch, target_branch |  | 待合同化；快照待复核 |
| MCP:parse_codeowners | coverage_ownership | rust_native / READ_ONLY |  | file_path | 待合同化；快照待复核 |
| MCP:project_brief | query_search | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:propose_edit | diagnostics | rust_native / PROTECTED_MUTATION | file_path, new_content | agent_task_id, dry_run, expected_hash, operation, symbol_hash | 待合同化；快照待复核 |
| MCP:propose_range_patch | diagnostics | rust_native / PROTECTED_MUTATION | file_path, start_line, end_line, new_content | agent_task_id, dry_run, expected_hash, symbol_hash | 待合同化；快照待复核 |
| MCP:propose_symbol_id_patch | diagnostics | rust_native / PROTECTED_MUTATION | symbol_id, new_content | agent_task_id, dry_run, expected_hash, expected_symbol_hash, mode | 待合同化；快照待复核 |
| MCP:propose_symbol_patch | diagnostics | rust_native / PROTECTED_MUTATION | file_path, qualified_name, new_content | agent_task_id, dry_run, expected_hash, mode | 待合同化；快照待复核 |
| MCP:prune_external_symbols | gc | task_rpc / PROTECTED_MUTATION |  | keep_project_deps, package_names, vacuum | 待合同化；快照待复核 |
| MCP:publish_interface | dependency | rust_native / PROTECTED_MUTATION | workspace_id, task_id, contract_id, contract_revision, interface_name, version | interface_hash | 待合同化；快照待复核 |
| MCP:record_action_identity | identity | rust_native / GOVERNANCE_WRITE | action_id, action_type, task_id, identity | contract_id, contract_revision, workspace_id | 待合同化；快照待复核 |
| MCP:record_artifact_identity | dependency | rust_native / GOVERNANCE_WRITE | workspace_id, task_id, contract_id, contract_revision, artifact_id, artifact_type, artifact_ref | artifact_hash, workspace_snapshot_id | 待合同化；快照待复核 |
| MCP:record_task_symbol_change | task | task_rpc / PROTECTED_MUTATION | task_id, file_path | change_audit_id, change_type, edit_audit_id, metadata, qualified_name, source, step_id, symbol_hash_after, symbol_hash_before, symbol_name | 待合同化；快照待复核 |
| MCP:record_token_savings | query_search | rust_native / PROTECTED_MUTATION | operation, original_tokens, actual_tokens | agent_task_id, detail | 待合同化；快照待复核 |
| MCP:refresh_file | workspace_database | rust_native / PROTECTED_MUTATION | file_path |  | 待合同化；快照待复核 |
| MCP:register_attestation_revocation | identity | rust_native / GOVERNANCE_WRITE | issuer, signing_key_id | initiating_actor, revocation_mode, revocation_reason, workspace_id | 待合同化；快照待复核 |
| MCP:register_branch | workspace_database | rust_native / PROTECTED_MUTATION | branch_name | repo_root | 待合同化；快照待复核 |
| MCP:register_workspace | workspace_database | rust_native / PROTECTED_MUTATION | name, root_path | description | 待合同化；快照待复核 |
| MCP:remove_file | workspace_database | rust_native / PROTECTED_MUTATION | file_path |  | 待合同化；快照待复核 |
| MCP:repo_map | query_search | rust_native / READ_ONLY |  | format | 待合同化；快照待复核 |
| MCP:resolve_gate_findings | audit_bootstrap | rust_native / PROTECTED_MUTATION | gate_id | resolution, task_id | 待合同化；快照待复核 |
| MCP:restore_all_comments | coverage_ownership | rust_native / PROTECTED_MUTATION |  | file_filter, preview | 待合同化；快照待复核 |
| MCP:restore_comment | coverage_ownership | rust_native / PROTECTED_MUTATION | spec | preview | 待合同化；快照待复核 |
| MCP:revert_edit | diagnostics | rust_native / PROTECTED_MUTATION | audit_id |  | 待合同化；快照待复核 |
| MCP:review_readiness | semgrep_defects | rust_native / READ_ONLY | symbol_hash |  | 待合同化；快照待复核 |
| MCP:rotate_audit_signing_key | audit_bootstrap | rust_native / PROTECTED_MUTATION | key_id | key_secret | 待合同化；快照待复核 |
| MCP:rule_candidate_accept | rule_memory | rust_native / PROTECTED_MUTATION | candidate_id | reviewer | 待合同化；快照待复核 |
| MCP:rule_candidate_create | rule_memory | rust_native / PROTECTED_MUTATION | title, rule_text | confidence, evidence, scope, severity, source | 待合同化；快照待复核 |
| MCP:rule_candidate_list | rule_memory | rust_native / READ_ONLY |  | limit, status | 待合同化；快照待复核 |
| MCP:rule_candidate_reject | rule_memory | rust_native / PROTECTED_MUTATION | candidate_id | reason, reviewer | 待合同化；快照待复核 |
| MCP:rule_insert_agents_md_block | rule_memory | rust_native / PROTECTED_MUTATION |  | actor, target_path | 待合同化；快照待复核 |
| MCP:rule_list | rule_memory | rust_native / READ_ONLY |  | limit, status | 待合同化；快照待复核 |
| MCP:rule_seed_bootstrap | rule_memory | rust_native / PROTECTED_MUTATION |  | dry_run | 待合同化；快照待复核 |
| MCP:rule_sync_agents_md | rule_memory | rust_native / PROTECTED_MUTATION |  | actor, dry_run, target_path | 待合同化；快照待复核 |
| MCP:run_check_gate | audit_bootstrap | rust_native / PROTECTED_MUTATION | task_id, step_id, changed_files |  | 待合同化；快照待复核 |
| MCP:run_semgrep_scan | semgrep_defects | task_rpc / PROTECTED_MUTATION |  | config, languages, timeout | 待合同化；快照待复核 |
| MCP:scan_semgrep_incremental | semgrep_defects | task_rpc / PROTECTED_MUTATION |  | base_branch, config, head, languages, timeout | 待合同化；快照待复核 |
| MCP:search_symbols | query_search | rust_native / READ_ONLY | query | kind, limit | 待合同化；快照待复核 |
| MCP:select_interface_provider | dependency | rust_native / PROTECTED_MUTATION | workspace_id, consumer_task_id, contract_id, contract_revision, interface_name, selected_provider_task_id |  | 待合同化；快照待复核 |
| MCP:semantic_search | query_search | rust_native / READ_ONLY | query | top_k | 待合同化；快照待复核 |
| MCP:semgrep_scan_async | semgrep_defects | task_rpc / PROTECTED_MUTATION |  | config, languages, timeout | 待合同化；快照待复核 |
| MCP:set_active_workspace | workspace_database | rust_native / PROTECTED_MUTATION | workspace_id_or_name |  | 待合同化；快照待复核 |
| MCP:submit_verdict | collab | rust_native / GOVERNANCE_WRITE | task_id, step_id, contract_id, contract_revision, contract_hash, role_contract_id, role_contract_revision, role_contract_hash | amendment_ref, attestation, clause_results, fencing_counter, findings, identity_agent_id, identity_agent_instance_id, identity_model_id, identity_role, identity_session_id, lease_token, overall, phase, request_id, reviewer_identity, snapshot_id, verdict_id, view_manifest_hash | 待合同化；快照待复核 |
| MCP:switch_branch | workspace_database | rust_native / PROTECTED_MUTATION | branch_name |  | 待合同化；快照待复核 |
| MCP:task_apply | task | task_rpc / PROTECTED_MUTATION | task_id | fencing_counter, identity, lease_token, reviewer | 待合同化；快照待复核 |
| MCP:task_assignment_heartbeat | task | task_rpc / PROTECTED_MUTATION | task_id, assignment_id | agent_session_id, fencing_counter, identity, request_id | 待合同化；快照待复核 |
| MCP:task_assignment_status | task | task_rpc / READ_ONLY | task_id | role, step_id | 待合同化；快照待复核 |
| MCP:task_capture_diff | task | task_rpc / PROTECTED_MUTATION | task_id | base, dry_run, skip_quality_review, source_commit_hash, step_id | 待合同化；快照待复核 |
| MCP:task_close | task | task_rpc / PROTECTED_MUTATION | task_id | fencing_counter, identity, lease_token, reviewer | 待合同化；快照待复核 |
| MCP:task_completion_review | task | task_rpc / PROTECTED_MUTATION | task_id | step_id | 待合同化；快照待复核 |
| MCP:task_create | task | task_rpc / PROTECTED_MUTATION | title | creator, description, steps, workspace_id, workspace_instance_id | 待合同化；快照待复核 |
| MCP:task_create_from_plan | task | task_rpc / PROTECTED_MUTATION | title, plan_md | description | 待合同化；快照待复核 |
| MCP:task_create_subtask | task | task_rpc / PROTECTED_MUTATION | parent_task_id, title | creator, description, steps | 待合同化；快照待复核 |
| MCP:task_get_role_prompt | task | rust_native / READ_ONLY | task_id |  | 待合同化；快照待复核 |
| MCP:task_governance_projection | task | task_rpc / READ_ONLY | task_id |  | 待合同化；快照待复核 |
| MCP:task_list | task | task_rpc / READ_ONLY |  | limit, status_filter | 待合同化；快照待复核 |
| MCP:task_next_step | task | task_rpc / PROTECTED_MUTATION | task_id | agent_instance_id, agent_session_id, contract_claim, identity | 待合同化；快照待复核 |
| MCP:task_plan_template | task | rust_native / READ_ONLY |  |  | 待合同化；快照待复核 |
| MCP:task_quality_findings | task | task_rpc / READ_ONLY | task_id | severity, status | 待合同化；快照待复核 |
| MCP:task_remediation_create | task | rust_native / PROTECTED_MUTATION | task_id, source_step_id, request_id, lease_token, fencing_counter | identity_agent_id, identity_model_id, identity_role, identity_session_id, source_findings, source_outcome, source_verdict_id | 待合同化；快照待复核 |
| MCP:task_report_step | task | task_rpc / PROTECTED_MUTATION | task_id, step_id | agent_instance_id, changes, identity, result, snapshot_id, success | 待合同化；快照待复核 |
| MCP:task_resolve_block | task | task_rpc / PROTECTED_MUTATION | task_id, step_id | resolution | 待合同化；快照待复核 |
| MCP:task_resolve_quality_finding | task | task_rpc / PROTECTED_MUTATION | finding_id | resolution, resolved_by | 待合同化；快照待复核 |
| MCP:task_rollback | task | task_rpc / PROTECTED_MUTATION | task_id | change_id, reason | 待合同化；快照待复核 |
| MCP:task_split | task | task_rpc / PROTECTED_MUTATION | task_id, subtasks |  | 待合同化；快照待复核 |
| MCP:task_status | task | task_rpc / READ_ONLY | task_id |  | 待合同化；快照待复核 |
| MCP:task_status_tree | task | task_rpc / READ_ONLY | task_id |  | 待合同化；快照待复核 |
| MCP:task_step_resolve | task | rust_native / PROTECTED_MUTATION | task_id, failed_step_id, remediation_step_id, request_id, evidence_path, evidence_hash, lease_token, fencing_counter | identity_agent_id, identity_model_id, identity_role, identity_session_id | 待合同化；快照待复核 |
| MCP:test_impact_selection | coverage_ownership | rust_native / READ_ONLY | qualified_name |  | 待合同化；快照待复核 |
| MCP:validate_revision_dependencies | dependency | rust_native / READ_ONLY | workspace_id, contract_id, contract_revision |  | 待合同化；快照待复核 |
| MCP:wait_for_job | task | rust_native / READ_ONLY | job_id | poll_interval, timeout | 待合同化；快照待复核 |
| MCP:who_to_ask | coverage_ownership | rust_native / READ_ONLY | file_path |  | 待合同化；快照待复核 |
| MCP:work_next_job | task | task_rpc / PROTECTED_MUTATION | task_id |  | 待合同化；快照待复核 |

## 5. 执行映射与阻断条件

现有T1参数骨架、T2进程内MCP、T3源码CLI、M1静态路由、M2纯度、M3并发、M4入口故障、T4 workspace、T5 LLM各有不同深度；不自动折算为上表C1–C6全部完成。
MCP进程内调用不代替wire；CLI源码不代替安装/冻结产物；假ID业务拒绝不代替正常case；破坏性/重操作在隔离实例验而非永久跳过。
正式合同实现后追加逐case selector/证据映射（独立机器manifest待建），本生成器不得自行把行改为PASS。未知错误、清单漂移、缺selector/报告/前置按策略阻断。
