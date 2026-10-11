# 首批测试规划任务注册回执

本轮由用户批准正式入库，仅注册 TP-01～TP-04；其余候选卡未入库。创建经 TokenSlim 包装现行 cw CLI，角色合同和任务由 daemon 创建。未领取步骤、未分配或伪造执行者身份、未提交 verdict、未执行 apply/close。

| 方案编号 | 真实 task_id | 步骤 | 当前状态 |
| --- | --- | --- | --- |
| TP-01 | T-1791627645904-fbe3a4c4 | 5 | open / queued |
| TP-02 | T-1791627784101-291afe54 | 5 | open / queued |
| TP-03 | T-1791627785150-67976028 | 4 | open / queued |
| TP-04 | T-1791627786186-a562f188 | 4 | open / queued |

工作区由当前 daemon 解析并核验：workspace_id=1，workspace_instance_id=4baea3ff12c2ea5c。每项有独立 workspace binding、Task Contract，以及 executor/reviewer/adjudicator 三份 Role Contract。`verify_registration.py` 读取 daemon 角色合同并逐字段比较：12 份合同一致；任务状态回读及实时 next-action 确认 18 步未领取，assignment holder 为空。READY/CLAIM 表示允许领取，不表示已经执行。

注册输入见 cards.json，原始创建返回及 rc 见各 TP 的 create 日志，任务状态、合同与 next-action 分别归档；verification.json 是核验摘要，source_baseline.json 保留审批输入的文件散列。验收脚本尚待任务实施，本轮没有运行产品测试或构建。

当前 daemon 未声明 planner_governance_v1 或 role_prompt_compiler_v1。使用其实际声明的 task_rpc 创建入口和已支持的三角色合同；生产 prompt 字段没有本地伪造。按共享协议使用精确 daemon instance 的只读 next-action 回落。HANDOFF.json 仅为 design-only 通知，未向 daemon 发送 Planner outcome；无法验证的 Planner identity/provenance 明确 unavailable。

下一棒为 Executor：用本表精确 task_id 重新读取合同、绑定和 next-action，核验当前 capability，登记真实 identity 后正式领取。四项只允许各自静态规划白名单与本任务证据目录；下游任务须完成前置并冻结合同后另行注册。
