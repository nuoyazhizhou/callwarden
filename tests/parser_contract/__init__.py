"""P0-A 解析契约测试与基线工具包。

本目录属于 Rust-only parser 生产切换计划（docs/design/rust-only-parser-cutover-plan.md）
的 Phase 0 工作，用于：

1. ``baseline.json`` — 当前 commit 下 16 语言能力清单 + bundle distribution 字节占比基线
2. ``generate_baseline.py`` — 重新生成 baseline.json 的脚本
3. ``golden/`` — 每种语言的人工确认预期符号/调用/import fixture
4. ``gate_report.py`` — 机读门禁汇总（成功/失败计数 + 阻塞语言清单）
5. ``test_*.py`` — 各项契约对齐测试

不修改 Rust parser 实现。所有失败由对应语言 Agent 修复。
"""
