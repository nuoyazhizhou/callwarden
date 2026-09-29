# T5 实测报告:真实 LLM(DeepSeek)可理解性端到端

日期:2026-09-30
环境:Windows,Python 3.14,LLM = DeepSeek `deepseek-v4-flash`(OpenAI 兼容 API,reasoning 模型,latency ~2-15s/次)

## 1. 测试目标与方法

验证 MCP 工具的 **name + description(+params schema)** 能否让真实 LLM 正确理解并选对工具、填对参数——即"工具文档对 LLM 的可理解性"。

**方法**:给 LLM 一批工具卡片(name + description 首段 + required 参数)+ 一个自然语言意图,要求它以 JSON 返回 `{tool, arguments}`。评估:
- **选对率**:选的 tool ∈ 期望集合
- **参数覆盖率**:生成的 arguments 是否含该工具全部 required 参数

三档难度递增。

## 2. 结果汇总

| 档次 | 场景 | 结果 |
| --- | --- | --- |
| 简单(窄候选,4 个/题) | 15 个任务,选对 + 参数覆盖 | **15/15 = 100%** |
| 困难 A(全量 243 纯工具名,无描述) | 10 个任务 | **9/10 = 90%** |
| 困难 B(易混淆同域工具,带描述,精确区分) | 7 个任务 | **7/7 = 100%** |

## 3. 可理解性评估

### 优秀面
- **窄候选 100%**:给相关域工具卡片时,LLM 100% 选对工具且参数覆盖 required,包括填 `callee_name`(get_callers)、`query`(search_symbols)、`file_path`(file_read)等,还会主动补 `workspace_id`/`workspace_instance_id`(task_create)。
- **易混淆精确区分 100%**:靠 description 细微差别,LLM 全部选对:
  - `get_stats`(纯计数) vs `get_status`(workspace 状态)
  - `cross_layer_impact`(跨架构层) vs `get_impact`/`blast_radius`
  - `file_symbol_content`(源码) vs `get_symbol`(详情) vs `get_symbol_location`(位置)
  - `get_largest_functions`(行数) vs `get_complexity_hotspots`(复杂度)
  - `task_split` vs `task_create_subtask`
  - `get_call_chain_down`(递归多层) vs `get_callers`(直接)

  说明工具 description 对语义边界的刻画足够清晰。
- **全量 243 纯名字 90%**:即使只给工具名(无描述),LLM 仍能从 243 个里正确定位 9/10,说明**工具命名整体自解释性好**。

### 唯一发现:命名歧义 `detect_cycle` / `detect_cycles`

困难 A 唯一失败:意图"检测调用环",LLM 在 243 个纯名字候选下返回**空 tool**(无法决断)。根因:

| 工具 | description |
| --- | --- |
| `detect_cycle` | 检测**硬依赖图**中的环(Req 9.7) |
| `detect_cycles` | 检测**循环调用**(调用图) |

两者**名字仅差一个 `s`**,但语义不同(依赖图 vs 调用图)。纯名字候选下 LLM 无法区分而弃选。带 description 时可区分(困难 B 中调用链类全对)。

**性质**:可理解性/命名问题,**非功能缺陷**(两工具都存在且可用)。

**建议**(非本轮修复,记录备查):
- 改名消歧,如 `detect_dependency_cycle` / `detect_call_cycle`;或
- description 首句加显式前缀,如 "[依赖图] ..." / "[调用图] ...",让纯名字/首句即可区分。

## 4. 正式化交付(tests/convergence/)

- `llm_channel.py`:DeepSeek 通道(从 .env 读 key,httpx 直连,无 openai SDK 依赖);`llm_available()` 供 skip 判定
- `test_t5_llm_understandability.py`:T5 pytest(10 任务,选对率 ≥ 0.8 基线防回退;无 API key 时 skip)。实测 1 passed(21.30s)

## 5. 结论

- MCP 工具文档对 LLM 的**可理解性整体优秀**:窄候选 100%、易混淆精确区分 100%、全量纯名字 90%。
- LLM 能正确选工具并填对必填参数,工具 description 对语义边界刻画清晰。
- **唯一可理解性问题**:`detect_cycle`/`detect_cycles` 命名歧义(依赖图 vs 调用图,仅差 1 字母),建议改名或描述加前缀区分。非功能缺陷。
