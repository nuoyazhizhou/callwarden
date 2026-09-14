# T-1789340885170-02a8fe9c —— C-13 承接卡独立 Reviewer 复核回执（blind_first_pass）

> 本文件是 **reviewer 视角**的 task-bound 独立复核证据，与 implementer 的
> `T-1789340885170-02a8fe9c-evidence.md` 相互独立。all 数字均为 reviewer 在本机**独立实跑**
> 的回执，**不采信 executor 报告**，代码/回归均独立重现后给出 PASS 结论依据。

## 0. 复核身份与 lease

| 项 | 值 |
| --- | --- |
| task_id | `T-1789340885170-02a8fe9c` |
| role | reviewer（blind_first_pass） |
| agent_id | `reviewer-c13-wb-01` |
| agent_instance_id | `inst-review-c13-20260914` |
| session_id | `sess-review-c13-20260914` |
| model_id | `workbuddy` |
| reviewer lease | `L-526f03de5b10db00`（counter=1） |
| 独立于 executor？ | 是（executor 为 `executor-pytreg-01` / `sess-exec-pytreg-01-6c9613cd`） |

## 1. 复核结论

**overall = pass**，阶段 `blind_first_pass`。C-13 根因（`semgrep_handlers.rs` 未在
`daemon/mod.rs` 声明 `pub mod` → 从未参与编译 → 4 个方法恒 `method_not_found`）
已由本卡修复，未掩蔽；未触碰 forbidden_paths；回归证据可在干净 checkout 重现。

## 2. 独立复算回执（不依赖 executor 报告）

### 2.1 代码复核（只读源码）

- `rust_ext/src/daemon/mod.rs` 现含 `pub mod semgrep_handlers;`（+ 根因注释），
  该模块正式参与编译；
- `rust_ext/src/daemon/snapshot_state.rs` 的 `handle_convergence_rpc` 新增 4 个方法
  名的 `semgrep::handle_*` 分派 arm：3 个写面（`run_semgrep` / `run_semgrep_and_save` /
  `scan_semgrep_incremental`）走 `open_codegraph_db_write` 主库写连接，1 个只读面
  （`get_semgrep_summary`）走 `open_query_connection`；
- `rust_ext/src/daemon/dispatch.rs`：`CONVERGENCE_RPC_METHODS` 含 4 个方法名、
  `PROTECTED_MUTATION_METHODS` 含 3 个写面，方法名与 `cli/main.py` `_METHOD_MAP`
  逐字一致；
- 上述三处逻辑与 C-13 修复目标一致，未发现掩蔽或绕过。

### 2.2 独立编译（reviewer 自行从源码构建）

```
cargo build --manifest-path rust_ext/Cargo.toml
Finished dev profile [unoptimized + debuginfo] target(s) in 17.76s
CARGO_BUILD_EXIT=0    # 零 error
```

日志：`deliverables/software-company/_c13_reviewer_cargo_build.log`

### 2.3 独立 route_matrix 门禁

```
python scripts/verify_route_matrix.py
核对通过（门禁全绿）
ROUTE_MATRIX_EXIT=0
```

### 2.4 独立源码契约回归

```
python -m pytest tests/test_c13_semgrep_dispatch_wiring.py -q
9 passed
PYTEST_C13_EXIT=0
```

### 2.5 证据文件哈希复核

implementer evidence manifest SHA-256 = `390ca6b4ae773e8b9d3709dfc002dc8c719343f1dc3e5d93952aa3128c049b28`，
与 daemon 已持久化的 report evidence_hash 一致。

## 3. verdict / snapshot provenance

- reviewer verdict：`V-76f8df41df79cc908c9d7875`（overall=pass，blind_first_pass）
- review_input_snapshot_id：`a87ae6c43b7bb6cd`
- view_manifest_hash：`7a87c1f1864b16986c5c43e964dad15d51c3043077224ee0421e7d1d130c1784`
- task_contract：`TC-T-1789340885170-02a8fe9c` rev=1
  hash=`sha256:152d3c902bb289facbdafeb4ade9c77476e8c876e70011057905964135189487`
- role_contract：`rcl-T-1789340885170-02a8fe9c-reviewer` rev=1
  hash=`sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`

## 4. 结论与后续

Reviewer 判 **PASS**。依据 role-protocol §7，提交 `reviewer_pass` handoff 将任务移交
adjudicator 进行受保护收尾（apply → applied_pending_close → close → completed）。