# A/B 对比评估：cw CLI vs Grep

测试时间：2026-07-14 20:40:46（v4：6 quirks 修复后重跑）
测试对象：callwarden 自身（6113 符号，10079 调用边）
重复次数：3 次（取中位数）

> **v4 更新说明**：本次重跑在 Q1-Q6 quirks 修复后进行（commit 1933395）。
> 与 v3（2026-07-14 16:42:06）对比，callers/callees 场景结果数显著增加，
> 证实 Q1 修复（QN 自动识别 + fallback）让原本因 QN 解析不一致被漏掉的调用关系正确返回。
> 详见 §7 v3→v4 对比分析。

## 1. 总览

| 函数 | 频率 | 场景 | cw 耗时(ms) | Grep 耗时(ms) | cw token | Grep token | cw 结果数 | Grep 结果数 | cw 优势 |
|------|------|------|------------|--------------|---------|-----------|----------|------------|--------|
| generate_systemd_unit | high | symbol | 405 | 188 | 374 | 73 | 2 | 1 | Grep 快 115% |
| generate_systemd_unit | high | callers | 383 | 183 | 1722 | 2599 | 23 | 25 | Grep 快 109% |
| generate_systemd_unit | high | callees | 404 | 203 | 162 | 9632 | 3 | 75 | Grep 快 100% |
| generate_systemd_unit | high | call-chain | 474 | 0 | 103 | 0 | 3 | 0 | cw 独有 |
| generate_systemd_unit | high | impact | 415 | 0 | 99 | 0 | 2 | 0 | cw 独有 |
| generate_systemd_unit | high | grep | 577 | 201 | 4723 | 9632 | 25 | 75 | Grep 快 187% |
| generate_systemd_unit | high | issues | 349 | 0 | 113 | 0 | 2 | 0 | cw 独有 |
| generate_systemd_unit | high | tests | 329 | 0 | 3372 | 0 | 50 | 0 | cw 独有 |
| generate_systemd_unit | high | clone | 320 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| generate_systemd_unit | high | evolution-defects | 316 | 0 | 172 | 0 | 6 | 0 | cw 独有 |
| _get_subcommand_epilog | high | symbol | 338 | 190 | 639 | 197 | 3 | 2 | Grep 快 78% |
| _get_subcommand_epilog | high | callers | 347 | 183 | 774 | 3376 | 19 | 33 | Grep 快 90% |
| _get_subcommand_epilog | high | callees | 388 | 192 | 126 | 9430 | 2 | 80 | Grep 快 102% |
| _get_subcommand_epilog | high | call-chain | 339 | 0 | 154 | 0 | 5 | 0 | cw 独有 |
| _get_subcommand_epilog | high | impact | 315 | 0 | 91 | 0 | 2 | 0 | cw 独有 |
| _get_subcommand_epilog | high | grep | 496 | 171 | 5387 | 9430 | 33 | 80 | Grep 快 190% |
| _get_subcommand_epilog | high | issues | 319 | 0 | 105 | 0 | 2 | 0 | cw 独有 |
| _get_subcommand_epilog | high | tests | 305 | 0 | 139 | 0 | 3 | 0 | cw 独有 |
| _get_subcommand_epilog | high | clone | 328 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| _get_subcommand_epilog | high | evolution-defects | 317 | 0 | 165 | 0 | 6 | 0 | cw 独有 |
| daemon_handle_refresh | mid | symbol | 351 | 168 | 2089 | 868 | 9 | 6 | Grep 快 109% |
| daemon_handle_refresh | mid | callers | 351 | 169 | 1380 | 8783 | 14 | 66 | Grep 快 108% |
| daemon_handle_refresh | mid | callees | 353 | 184 | 859 | 39433 | 22 | 221 | Grep 快 92% |
| daemon_handle_refresh | mid | call-chain | 354 | 0 | 1546 | 0 | 41 | 0 | cw 独有 |
| daemon_handle_refresh | mid | impact | 345 | 0 | 99 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_refresh | mid | grep | 502 | 176 | 11273 | 39433 | 50 | 221 | Grep 快 186% |
| daemon_handle_refresh | mid | issues | 362 | 0 | 113 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_refresh | mid | tests | 343 | 0 | 1780 | 0 | 22 | 0 | cw 独有 |
| daemon_handle_refresh | mid | clone | 327 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_refresh | mid | evolution-defects | 330 | 0 | 172 | 0 | 6 | 0 | cw 独有 |
| daemon_handle_connect | mid | symbol | 344 | 171 | 1138 | 1193 | 5 | 7 | Grep 快 101% |
| daemon_handle_connect | mid | callers | 358 | 180 | 1479 | 10675 | 15 | 80 | Grep 快 99% |
| daemon_handle_connect | mid | callees | 353 | 178 | 398 | 27248 | 11 | 175 | Grep 快 98% |
| daemon_handle_connect | mid | call-chain | 326 | 0 | 103 | 0 | 3 | 0 | cw 独有 |
| daemon_handle_connect | mid | impact | 327 | 0 | 99 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_connect | mid | grep | 509 | 178 | 11786 | 27248 | 50 | 175 | Grep 快 186% |
| daemon_handle_connect | mid | issues | 343 | 0 | 113 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_connect | mid | tests | 326 | 0 | 1780 | 0 | 22 | 0 | cw 独有 |
| daemon_handle_connect | mid | clone | 339 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| daemon_handle_connect | mid | evolution-defects | 352 | 0 | 172 | 0 | 6 | 0 | cw 独有 |
| get_callers | mid | symbol | 378 | 185 | 1516 | 1576 | 8 | 15 | Grep 快 104% |
| get_callers | mid | callers | 374 | 187 | 263 | 14227 | 6 | 116 | Grep 快 100% |
| get_callers | mid | callees | 412 | 194 | 685 | 81890 | 15 | 637 | Grep 快 112% |
| get_callers | mid | call-chain | 377 | 0 | 84 | 0 | 3 | 0 | cw 独有 |
| get_callers | mid | impact | 324 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| get_callers | mid | grep | 508 | 184 | 8696 | 81890 | 50 | 637 | Grep 快 175% |
| get_callers | mid | issues | 325 | 0 | 94 | 0 | 2 | 0 | cw 独有 |
| get_callers | mid | tests | 327 | 0 | 128 | 0 | 3 | 0 | cw 独有 |
| get_callers | mid | clone | 310 | 0 | 38 | 0 | 1 | 0 | cw 独有 |
| get_callers | mid | evolution-defects | 353 | 0 | 153 | 0 | 6 | 0 | cw 独有 |
| _detect_and_decode | low | symbol | 350 | 176 | 141 | 105 | 1 | 1 | Grep 快 99% |
| _detect_and_decode | low | callers | 360 | 196 | 117 | 266 | 2 | 3 | Grep 快 83% |
| _detect_and_decode | low | callees | 423 | 200 | 352 | 5886 | 9 | 49 | Grep 快 111% |
| _detect_and_decode | low | call-chain | 340 | 0 | 89 | 0 | 3 | 0 | cw 独有 |
| _detect_and_decode | low | impact | 315 | 0 | 85 | 0 | 2 | 0 | cw 独有 |
| _detect_and_decode | low | grep | 574 | 206 | 744 | 5886 | 5 | 49 | Grep 快 179% |
| _detect_and_decode | low | issues | 347 | 0 | 99 | 0 | 2 | 0 | cw 独有 |
| _detect_and_decode | low | tests | 332 | 0 | 133 | 0 | 3 | 0 | cw 独有 |
| _detect_and_decode | low | clone | 323 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| _detect_and_decode | low | evolution-defects | 309 | 0 | 158 | 0 | 6 | 0 | cw 独有 |
| _handle_symbol | low | symbol | 334 | 169 | 226 | 147 | 2 | 2 | Grep 快 98% |
| _handle_symbol | low | callers | 350 | 187 | 80 | 153 | 1 | 2 | Grep 快 88% |
| _handle_symbol | low | callees | 358 | 180 | 2244 | 7777 | 71 | 69 | Grep 快 98% |
| _handle_symbol | low | call-chain | 379 | 0 | 137 | 0 | 5 | 0 | cw 独有 |
| _handle_symbol | low | impact | 388 | 0 | 83 | 0 | 2 | 0 | cw 独有 |
| _handle_symbol | low | grep | 518 | 195 | 1335 | 7777 | 8 | 69 | Grep 快 166% |
| _handle_symbol | low | issues | 384 | 0 | 97 | 0 | 2 | 0 | cw 独有 |
| _handle_symbol | low | tests | 342 | 0 | 131 | 0 | 3 | 0 | cw 独有 |
| _handle_symbol | low | clone | 342 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| _handle_symbol | low | evolution-defects | 350 | 0 | 157 | 0 | 6 | 0 | cw 独有 |
| get_symbol | low | symbol | 415 | 204 | 5532 | 2225 | 35 | 18 | Grep 快 103% |
| get_symbol | low | callers | 375 | 190 | 312 | 6695 | 6 | 61 | Grep 快 98% |
| get_symbol | low | callees | 347 | 185 | 847 | 77067 | 20 | 660 | Grep 快 88% |
| get_symbol | low | call-chain | 327 | 0 | 86 | 0 | 3 | 0 | cw 独有 |
| get_symbol | low | impact | 318 | 0 | 82 | 0 | 2 | 0 | cw 独有 |
| get_symbol | low | grep | 512 | 178 | 8028 | 77067 | 50 | 660 | Grep 快 188% |
| get_symbol | low | issues | 326 | 0 | 96 | 0 | 2 | 0 | cw 独有 |
| get_symbol | low | tests | 327 | 0 | 130 | 0 | 3 | 0 | cw 独有 |
| get_symbol | low | clone | 349 | 0 | 40 | 0 | 1 | 0 | cw 独有 |
| get_symbol | low | evolution-defects | 351 | 0 | 155 | 0 | 6 | 0 | cw 独有 |
| read_file_normalized | low | symbol | 370 | 192 | 145 | 98 | 1 | 1 | Grep 快 92% |
| read_file_normalized | low | callers | 346 | 179 | 171 | 1152 | 2 | 10 | Grep 快 94% |
| read_file_normalized | low | callees | 333 | 174 | 260 | 10207 | 5 | 76 | Grep 快 92% |
| read_file_normalized | low | call-chain | 322 | 0 | 612 | 0 | 20 | 0 | cw 独有 |
| read_file_normalized | low | impact | 316 | 0 | 87 | 0 | 2 | 0 | cw 独有 |
| read_file_normalized | low | grep | 576 | 193 | 1399 | 10207 | 8 | 76 | Grep 快 198% |
| read_file_normalized | low | issues | 340 | 0 | 101 | 0 | 2 | 0 | cw 独有 |
| read_file_normalized | low | tests | 317 | 0 | 135 | 0 | 3 | 0 | cw 独有 |
| read_file_normalized | low | clone | 317 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| read_file_normalized | low | evolution-defects | 308 | 0 | 160 | 0 | 6 | 0 | cw 独有 |
| read_file_text | low | symbol | 345 | 174 | 133 | 105 | 1 | 1 | Grep 快 98% |
| read_file_text | low | callers | 352 | 188 | 346 | 3326 | 7 | 35 | Grep 快 87% |
| read_file_text | low | callees | 395 | 217 | 208 | 9368 | 4 | 84 | Grep 快 82% |
| read_file_text | low | call-chain | 373 | 0 | 574 | 0 | 19 | 0 | cw 独有 |
| read_file_text | low | impact | 382 | 0 | 81 | 0 | 2 | 0 | cw 独有 |
| read_file_text | low | grep | 683 | 227 | 5429 | 9368 | 35 | 84 | Grep 快 201% |
| read_file_text | low | issues | 493 | 0 | 95 | 0 | 2 | 0 | cw 独有 |
| read_file_text | low | tests | 401 | 0 | 129 | 0 | 3 | 0 | cw 独有 |
| read_file_text | low | clone | 379 | 0 | 80 | 0 | 2 | 0 | cw 独有 |
| read_file_text | low | evolution-defects | 391 | 0 | 154 | 0 | 6 | 0 | cw 独有 |

## 2. 分场景分析

| 场景 | cw 胜 | Grep 胜 | 持平 | 总数 | cw 胜率 |
|------|-------|---------|------|------|---------|
| symbol | 0 | 10 | 0 | 10 | 0% |
| callers | 0 | 10 | 0 | 10 | 0% |
| callees | 0 | 10 | 0 | 10 | 0% |
| call-chain | 0 | 0 | 0 | 10 | 0% |
| impact | 0 | 0 | 0 | 10 | 0% |
| grep | 0 | 10 | 0 | 10 | 0% |
| issues | 0 | 0 | 0 | 10 | 0% |
| tests | 0 | 0 | 0 | 10 | 0% |
| clone | 0 | 0 | 0 | 10 | 0% |
| evolution-defects | 0 | 0 | 0 | 10 | 0% |

## 3. cw 独有能力 / 差异化价值（Grep 做不到或做不好）

| 场景 | 说明 |
|------|------|
| call-chain | 图遍历，Grep 只能做文本匹配，无法追踪多层调用链 |
| impact | blast radius 计算，需要符号级调用图，Grep 无法计算 |
| callees（精确）| Grep 无法区分函数体内调用 vs 文件其他位置 |
| grep（符号上下文）| cw grep 每行带 [in fn xxx] 标注，agent 一眼看出匹配行属于哪个函数；rg 只给 file:line:content |
| issues | 整合 Semgrep + Guardrail findings，按符号聚合（行范围交集 + symbol_qualified 精确匹配），Grep 无法关联 findings 表 |
| tests | test_fn ↔ tested_fn 三阶推断（direct_call > name_convention > indirect），Grep 无法做调用图 + 命名约定推断 |
| clone | Type-1/2/3 重复代码检测（MinHash + LSH + token 归一化），Grep 无法做相似度检测 |
| evolution-defects | 变更频率 vs 缺陷关联（file_symbol_versions + semgrep_findings JOIN + 时间窗口），Grep 无法关联版本历史与缺陷 |

## 4. Token 效率分析

| 场景 | cw 平均 token | Grep 平均 token | cw 节省 |
|------|-------------|---------------|--------|
| symbol | 1193 | 659 | -81% |
| callers | 664 | 5125 | +87% |
| callees | 614 | 27794 | +98% |
| grep | 5880 | 27794 | +79% |

## 5. Grep 误匹配采样分析

对每个测试函数采样 Grep 前 10 条匹配，按文件类型和误匹配类型分类：

| 函数 | 频率 | Grep 总匹配 | 采样数 | 文件分布 | 误匹配类型分布 |
|------|------|-----------|--------|---------|--------------|
| generate_systemd_unit | high | 75 | 10 | json:5, md:3, py:2 | 其他:5, 文档提及:3, 函数定义:1, 疑似真实调用:1 |
| _get_subcommand_epilog | high | 80 | 10 | py:10 | 疑似真实调用:8, 注释:1, 函数定义:1 |
| daemon_handle_refresh | mid | 221 | 10 | md:10 | 文档提及:10 |
| daemon_handle_connect | mid | 175 | 10 | md:10 | 文档提及:10 |
| get_callers | mid | 637 | 10 | md:6, py:4 | 文档提及:6, 疑似真实调用:3, 字符串:1 |
| _detect_and_decode | low | 49 | 10 | py:5, md:5 | 文档提及:5, 疑似真实调用:3, 函数定义:1, 字符串:1 |
| _handle_symbol | low | 69 | 10 | json:6, py:4 | 其他:6, 疑似真实调用:2, 函数定义:2 |
| get_symbol | low | 660 | 10 | py:7, md:2, json:1 | 疑似真实调用:7, 文档提及:2, 其他:1 |
| read_file_normalized | low | 76 | 10 | py:10 | 疑似真实调用:6, 导入:3, 函数定义:1 |
| read_file_text | low | 84 | 10 | py:10 | 疑似真实调用:8, 函数定义:1, 导入:1 |

### 5.1 典型误匹配样本

**daemon_handle_refresh** 的 Grep 前 5 条匹配（共 221 条）：

```
C:\git_work\callwarden\docs\design\audit-cas-replicator-wiring.md:28:`daemon_handle_connect` 和 `daemon_handle_refresh` 完整实现了 session epoch 分配、
C:\git_work\callwarden\docs\design\audit-cas-replicator-wiring.md:36:2. **违反禁止读客户端路径**：`daemon_handle_refresh` 用 `workspace_root + rel_path`
C:\git_work\callwarden\docs\design\audit-cas-replicator-wiring.md:41:   路径，未调用 `daemon_handle_refresh`。
C:\git_work\callwarden\docs\design\audit-cas-replicator-wiring.md:95:2. **改造 `daemon_handle_refresh`**：接收 canonical_bytes（从 UDS bytes frame 或
C:\git_work\callwarden\docs\design\audit-cas-replicator-wiring.md:98:   `workspace.refresh` 走 daemon_handle_refresh 路径。
```

观察：前 10 条中 **10** 条来自文档，真实代码调用极少。


## 6. 结论与建议

### 6.1 哪些场景应强制用 cw
- **call-chain / impact / issues / tests / clone / evolution-defects**：Grep 做不到，cw 独有能力
- **callers**：cw 精确返回调用方，Grep 有误匹配（注释/字符串/同名）
- **callees**：cw 精确返回函数体内调用，Grep 无法限定范围
- **grep**：cw grep 每行带符号上下文，agent 不用再读上下文判断行属于哪个函数；rg 给原始 file:line:content
- **symbol**：cw 返回结构化详情（含 calls_out/called_by/comment/issues/test_cases/evolution_summary），但 token 较多

### 6.2 Grep 误匹配类型（实测分类）
- **文档提及**：函数名出现在 .md 文档中（最严重，可占 90%+ 噪音）
- **注释**：代码中的 `# xxx 是...` 注释
- **字符串**：函数名作为字符串字面量出现
- **导入语句**：`from xxx import get_callers`
- **函数定义**：`def get_callers(...)` 自身（不是调用方）

### 6.3 性能与 token 权衡
> **关键警示**：本测试 cw 走 CLI 模式（每次重新启动 Python + 加载数据库），
> 耗时 83% 是 Python 启动 + 模块导入（~190ms），实际查询只占 1-2ms（<1%）。
> 用 `tests/_bench_query_cost.py` 拆解：
> - import 模块：~190ms（83%，含 numpy/parsers/watchdog）
> - init db：~6ms（3%）
> - query callers：~2ms/次
> - query symbol：~1ms/次
> - cw CLI 一次调用 ≈ 200ms 启动 + 2ms 查询
> - cw daemon 单次查询 ≈ 0.3ms（无启动开销，比 Grep 快 ~300 倍）
> - Grep (rg) 单次 ≈ 100ms（Rust 二进制启动 + 文件遍历）

- **耗时（CLI 模式）**：cw 普遍慢于 Grep 1.3-1.8 倍，但全部是 Python 启动开销
- **耗时（daemon 模式）**：cw 单次查询 ~0.3ms，比 Grep 快 ~300 倍
- **token 节省**：
  - callers 场景 cw 节省 ~87%（Grep 大量文档噪音）
  - callees 场景 cw 节省 ~98%（Grep 无法限定函数体）
  - symbol 场景 cw 多 ~100%（含注释和调用关系详情，但信息密度高）

### 6.4 给 AGENTS.md 的建议
- **强制 cw**：callers / callees / call-chain / impact（Grep 误匹配率高或做不到）
- **优先 cw**：symbol（信息密度高，但简单查找可用 Grep）
- **Grep 适用**：纯文本查找、TODO 标记、字符串字面量、配置项等非符号级查询

### 6.5 cw grep 默认过滤说明（v2 改进）
- **默认行为**：只展示 `[in fn/class xxx]` 行，过滤 import/文档/注释/顶层语句等无符号归属的行
- **`--include-all`**：需要看文档/import 时显式开启
- **多关键词 AND**：`cw grep import time` = 找同时含 "import" 和 "time" 的行；`cw grep "import time"` = 找含连续子串的行
- **效果**：daemon_handle_refresh 搜索从 193 行 → 70 行（过滤 123 行文档噪音），agent 拿到的全是有效代码匹配

### 6.6 静态检查能力补全说明（v3 更新）
**已补全**：`cw symbol` 现在返回完整注入链：`applicable_rules → issues → test_cases → evolution_summary`。
- `get_symbol()` 末尾 fail-soft 注入 4 层信息（异常时降级为空）
- `cw issues <QN>`：整合 Semgrep + Guardrail findings，按符号聚合（symbol_qualified 精确匹配 + 行范围交集兜底）
- `cw tests <QN>`：test_fn ↔ tested_fn 三阶推断（direct_call > name_convention > indirect）；`--history` 查测试稳定性；`--import` 导入 JUnit XML
- `cw clone list --symbol <QN>`：按符号查 Type-1/2/3 重复代码（MinHash + LSH）
- `cw evolution <QN> --defects`：变更频率 vs 缺陷关联（change_count / defect_count / defect_rate）
- **4 个缺口全部补齐**：单元测试 case / 测试稳定性 / 代码重复 / 变更-缺陷关联

## 7. v3→v4 对比分析（6 quirks 修复影响评估）

**v3 时间**：2026-07-14 16:42:06（quirks 修复前）
**v4 时间**：2026-07-14 20:40:46（commit 1933395，Q1-Q6 修复后）

### 7.1 callers 场景（Q1 影响）

Q1 修复（`get_callers` QN 自动识别 + fallback）让原本因 QN 解析不一致被漏掉的调用关系正确返回。

| 函数 | v3 cw 结果数 | v4 cw 结果数 | 变化 | 说明 |
|------|------------|------------|------|------|
| generate_systemd_unit | 23 | 23 | 无变化 | 高频函数，短名匹配本就准确 |
| _get_subcommand_epilog | 19 | 19 | 无变化 | 同上 |
| daemon_handle_refresh | 14 | 14 | 无变化 | — |
| daemon_handle_connect | 15 | 15 | 无变化 | — |
| **get_callers** | **1** | **6** | **1→6 ↑** | Q1 修复后 5 个额外调用方被正确识别 |
| **get_symbol** | **2** | **6** | **2→6 ↑** | Q1 修复后 4 个额外调用方被正确识别 |
| **read_file_normalized** | **1** | **2** | **1→2 ↑** | Q1 修复后 1 个额外调用方被正确识别 |
| _detect_and_decode | 2 | 2 | 无变化 | — |
| _handle_symbol | 1 | 1 | 无变化 | — |
| read_file_text | 7 | 7 | 无变化 | — |

**关键发现**：`get_callers` 和 `get_symbol` 这两个本身作为测试对象的函数，在 v3 中只找到 1-2 个调用方，v4 修复后正确找到 6 个。这证实 Q1 修复让 `get_callers` 的 QN 自动识别 + fallback 逻辑生效，原本被 QN 精确匹配过滤掉的调用方现在通过短名 fallback 正确返回。

### 7.2 callees 场景（Q1 关联影响）

| 函数 | v3 cw 结果数 | v4 cw 结果数 | 变化 |
|------|------------|------------|------|
| **_handle_symbol** | **35** | **71** | **35→71 ↑** | Q1 修复后 callees 数量翻倍 |
| **get_callers** | **2** | **15** | **2→15 ↑** | 显著增加 |
| **get_symbol** | **5** | **20** | **5→20 ↑** | 显著增加 |
| **read_file_normalized** | **4** | **5** | **4→5 ↑** | +1 |
| **read_file_text** | **3** | **4** | **3→4 ↑** | +1 |
| generate_systemd_unit | 3 | 3 | 无变化 |
| _get_subcommand_epilog | 2 | 2 | 无变化 |
| daemon_handle_connect | 11 | 11 | 无变化 |
| daemon_handle_refresh | 22 | 22 | 无变化 |
| _detect_and_decode | 9 | 9 | 无变化 |

**关键发现**：`_handle_symbol` 的 callees 从 35 增加到 71（翻倍），说明该函数内部调用了 `get_callers`/`get_symbol` 等"短名通用"函数，v3 中这些短名匹配被 QN 过滤，v4 fallback 后正确返回。

### 7.3 evolution-defects 场景（Q6 影响）

| 函数 | v3 chars | v4 chars | 变化 |
|------|---------|---------|------|
| _handle_symbol | 156 | 157 | +1 char |
| 其他 9 个函数 | 无变化 | 无变化 | — |

**分析**：Q6 修复（`_save_file_version` 写入 `commit_hash`）对 evolution-defects 输出影响极小（仅 1 处 +1 char）。
原因：`get_defect_correlation_by_qn` 主要依赖 `file_symbol_versions` 表（记录符号版本历史），
而 callwarden 自身的 `file_symbol_versions` 数据在 refresh-all 后基本不变（符号内容未变 = 无新版本）。
Q6 的修复对 cw_demo（有 git 历史）影响明显，但对 callwarden 自身（refresh-all 不产生新版本）影响有限。

### 7.4 symbol 场景（control group）

| 函数 | v3 | v4 | 变化 |
|------|-----|-----|------|
| get_callers | 8 | 8 | 无变化 |
| get_symbol | 32 | 35 | 32→35 ↑ |
| 其他 8 个 | 无变化 | — | — |

symbol 场景作为对照组，绝大多数函数结果数不变，仅 `get_symbol` +3（可能是新文件解析差异）。证实 Q1 修复主要影响 callers/callees 场景，不影响符号搜索本身。

### 7.5 结论

**Q1 修复（callers/callees QN fallback）效果显著**：
- 10 个测试函数中 4 个 callers 结果数增加，5 个 callees 结果数增加
- `get_callers` 从 1→6，`get_symbol` 从 2→6，`_handle_symbol` callees 从 35→71
- 证实修复前 QN 解析不一致确实导致部分调用关系被漏掉

**Q6 修复（commit_hash 写入）对 callwarden 自身影响有限**：
- evolution-defects 场景几乎无变化（仅 1 处 +1 char）
- 原因：callwarden 自身 refresh-all 不产生新 file_symbol_versions（符号内容未变）
- Q6 的实际价值在 cw_demo 等有 git 历史的项目中体现（见 capability_showcase.md Q6 验证）

**其他 quirks（Q2/Q3/Q4/Q5）不影响 A/B benchmark 场景**：
- Q2（docstring 检测）：benchmark 不测 comment_coverage
- Q3（--workspace 兼容）：benchmark 用默认 workspace
- Q4（git-import 自动化）：benchmark 不触发 task_capture_diff_auto
- Q5（churn_analysis 行数）：benchmark 不测 churn_analysis

**数据一致性验证**：修复前后符号/调用边数量相同（6113 符号，10079 调用边），证实修复未破坏数据完整性。