# 函数 contract 卡片

> R3 的核心交付物。**卡片全文写进任务单 description**，executor 不用回翻设计文档。
> 一个 `new`/`extend` 函数一张卡；`reuse` 不建卡，建接线任务。

## 一、模板

```markdown
### <函数名>
- **文件/模块**：<相对路径>::<qualified_name>
- **REQ**：<REQ-xx>（<must/should/could>，revision：<n>）
- **签名**：<语言相关签名，Rust 例：fn process(req: Request) -> Result<Response, ProcError>>
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
- **出参契约**：
  - 成功：<形态>
  - 失败：错误枚举 <E>，每个变体 ↔ 失败点：
    | 错误变体 | 触发条件 | 上层如何处理 |
  - **异常场景矩阵勾选**：<8 类中本函数涉及哪些；不涉及标"不适用">
- **异常路径**：<每个可能失败的点 → 处理方式；禁止裸 unwrap / 吞异常>
- **副作用**：<IO / 状态 / 锁 / 网络>（<关联 NFR 项，无则省略>）
- **依赖符号**：reuse <qname> / extend <qname> / new <本卡片>
- **测试要点（GWT 双格式）**：
  - Given/When/Then ≥2 条，**其中 ≥1 条为失败分支**（Then 为错误形态）
  - 清单：[ ] 边界值 [ ] 异常分支 [ ] 并发场景
- **验收映射**：<ac-map.md 的 AC id>
- **注释要求**：<doc comment：what + why + 错误语义>
- **assumptions**：<R0 遗留假设，含 parking-lot，无则"无">
```

## 二、字段判据（写不出 = 设计没做完）

| 字段 | 合格判据 |
|---|---|
| 文件/模块 | 精确到文件路径 + `qualified_name`，不是"在 xxx 模块里加个函数" |
| 签名 | 能直接照抄进代码；类型、错误类型、所有权/生命周期都定下来 |
| 入参 | 每个参数有约束（非空/范围/格式/长度）+ **校验归属**（调用方 or 被调方，不重复不漏） |
| 出参 | 成功形态 + **每个错误变体对得上一个失败点**，无悬空变体、无未归类的失败 |
| 异常路径 | 每个失败点有明确去向（向上抛 / 转错误码 / 降级 / 中止）；**禁止**裸 `unwrap` / 静默吞异常 / 只写快乐路径 |
| 副作用 | IO、状态变更、锁顺序、网络调用逐一列出；有锁必须写清获取顺序 |
| 测试要点 | ≥1 条失败分支；Then 允许是错误形态 |
| 验收映射 | 只引用 AC id，不重复写验收内容（唯一单源） |

## 三、审查 checklist（逐卡自审，MUST / MUST NOT）

**MUST**

- 每项直接映射代码动作（精确路径 / 签名 / 表结构）
- 每个失败点有去向，且上层能区分（错误变体不合并成 `Other`）
- 边界值、异常分支可测
- doc comment 写 what + why + 错误语义

**MUST NOT**

- 模糊描述（"适当处理"、"做必要校验"）
- 过度设计（KISS/YAGNI：当前 REQ 用不到的参数/抽象层/扩展点）
- 裸 `unwrap` / `expect` 掩盖可恢复错误
- 静默 `let _ = ...` 吞掉 `Result`
- 一个函数承担两个 REQ 的职责（拆卡）

## 四、GWT 写法

```markdown
- Given 用户已登录但无该 workspace 权限
  When  调用 publish(workspace_id)
  Then  返回 Err(ProcError::Forbidden)，且不写入任何 capture 记录
```

**失败分支是硬要求**，不是可选加分。Then 可以是：

- 返回具体错误变体
- 状态保持不变（无副作用）
- 降级到 fallback 且标注 degraded

## 五、粒度

粒度判断引用 `cw-planner-architect` 的复杂度预检（>3 独立模块 / >5 实现步骤 → 拆分），**引用不复制阈值**。

| 函数类型 | 粒度 | 理由 |
|---|---|---|
| 核心函数（被依赖 / 含业务规则 / 有外部 IO） | 一单一 function | 可独立验收、独立审查 |
| 叶子 / 工具函数（同文件） | 按文件分组一单 | 避免任务单爆炸 |
| 纯 CRUD / 包装 | 并入调用方任务 | 无独立设计价值 |
| `reuse` 接线 | 一单或按文件分组 | 复用不落地 = 假复用 |

## 六、示例（节选）

```markdown
### publish_capture
- **文件/模块**：rust_ext/src/daemon/workspace.rs::daemon::workspace::publish_capture
- **REQ**：REQ-03（must，revision：1）
- **签名**：fn publish_capture(conn: &Connection, input: &PublishInput) -> Result<Snapshot, CaptureError>
- **入参契约**：
  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | input.root_path | String | 绝对路径、非空 | 调用方（R4 建单前已 resolve） |
  | input.workspace_instance_id | String | 非空、与权威 capture 一致 | 被调方（fail-closed） |
- **出参契约**：
  - 成功：Snapshot { id, revision, rules_hash }
  - 失败：CaptureError，变体 ↔ 失败点：
    | 错误变体 | 触发条件 | 上层如何处理 |
    |---|---|---|
    | InstanceRequired | instance 为空 | 返回 400，提示重新 register |
    | AuthorityMismatch | 与权威 instance 不一致 | 中止，不写任何记录 |
- **异常路径**：instance 空 → InstanceRequired；hash 不符 → AuthorityMismatch 且不落库；DB 错误 → 向上抛，事务回滚
- **副作用**：写 workspace_authority_captures（append-only）；无锁
- **依赖符号**：reuse sha256_hex / new 本卡片
- **测试要点**：
  - Given 空 instance，When publish，Then Err(InstanceRequired) 且表内行数不变
  - Given 权威 instance=A，When 传 B，Then Err(AuthorityMismatch)
- **验收映射**：AC-05
```

## 七、卡片缺陷（R5 才发现）

记录 finding + 走变更单（`references/change-ticket.md`）：卡片 `revision+1` → 关联任务留言 → 重跑该卡 check_items。**不得擅自改设计后继续写代码**。
