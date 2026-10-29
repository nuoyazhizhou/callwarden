# Checklist：workspace-snapshot-remediation

## 缺陷 A（CLI 禁止本地派生 workspace 实例）

- [x] `cli/main.py` 的 `task next-action` / `task supersede` 不再调用
      `derive_workspace_instance_id`（仅剩 graph/watch 的非目标使用点）
- [x] 未显式传 `--workspace-instance-id` 时，instance 来自
      `resolve_workspace_pair_from_daemon` 权威解析
- [x] 显式 `--workspace-instance-id` 仍优先，原样转发由 daemon 按 binding 校验
- [x] 「项目根可定位但无唯一权威配对」前置拒绝：`E_WORKSPACE_AUTHORITY_UNAVAILABLE`，
      不发 RPC，`--json` 输出 `{ok:false, code, message}`
- [x] 「daemon 不可达 / 项目根不可定位」保持既有路由错误语义
      （local 模式与 enterprise 不可用测试仍断言 `E_DAEMON_UNAVAILABLE`）

## 缺陷 B（snapshot_id 恒非空）

- [x] `compute_snapshot_id` 在 remote+head 齐全时逐字节等同旧公式
      `sha256("{remote}|{head}|{fp}")[:16]`
- [x] 无 remote 时输出 `sha256("instance:{instance}|{fp}")[:16]`，恒 16 hex 且不随 head 漂移
- [x] `register_workspace_preferred` 的 adopt / insert 分支均写入非空 snapshot
- [x] 空 provenance re-register 不抹除既有 remote/head/toolchain，也不降级 snapshot 公式
- [x] 同一物理 checkout 的 legacy 兄弟行传播 snapshot（provenance 仅在非空时覆盖）
      —— Rust 侧按 `normalize_identity_root` 比对，覆盖 `\\?\` verbatim 根
- [x] `update_workspace_vcs_head`：remote 非空才重算；remote 为空时 snapshot 保持不变
- [x] `cargo check` 通过；`cargo test --lib workspace::tests` 全绿（149 passed / 0 failed）

## 回归与验证

- [x] `tests/test_task_next_action_e2e.py` 的 CLI 用例显式传 `--workspace-instance-id`
- [x] 新增 fail-closed 用例：项目根可定位 + 无 registry 配对 + 不传参 → `code` 以 `E_WORKSPACE_` 开头
- [x] `python -m pytest tests/test_task_next_action_e2e.py -q` 通过（隔离 daemon 被占用时按既有规则 skip）
- [x] daemon 重新构建部署到 `runtime/current`，smoke tests 通过
      —— 证据 `runtime/evidence/20261029-095505-bd5cd60fc1bc-e7eeaed7.json`
      （status=passed；daemon PID 32424，hash `489bde5a…` 与构建一致；
      `cw --version` / `daemon ping` 均 exit 0）
- [x] TokenSlim 真机：`workspace register` / `collab publish` 返回非空 `snapshot_id`
      —— register adopt workspace_id=175 并把 `83fdbcf3453ab265` 传播到
      verbatim 兄弟行 1288（`\\?\C:\git_work\TokenSlim`）；publish 返回
      `snapshot_id=83fdbcf3453ab265`
- [x] TokenSlim 真机：临时探针卡 `task next-action` 无 `E_WORKSPACE_AUTHORITY_MISMATCH`；
      `task report` 带 snapshot 成功；探针卡验证后关闭
      —— 探针卡 `T-1793239588268-8fe6f514`：next-action `decision=READY`（未传
      `--workspace-instance-id`，CLI 经 daemon 解析 `d079f60cac8d6186`）；
      `task report … --snapshot-id 83fdbcf3453ab265` 成功；已 cascade-close
- [x] callwarden 自身 `task next-action` 无回归
      —— `T-1793238550960-0b8411a4` 返回 `decision=READY`，无 authority 错误
- [x] 代码 + spec/tasks/checklist 已提交（`9a9846a`）
