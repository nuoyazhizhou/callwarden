"""P0-A Step 1: 16 语言 golden contract fixtures。

本目录存放每种语言的**人工确认**预期输出（symbols/calls/imports/references），
作为 Rust-only parser 切换后的长期真相源（设计文档 §6.1 语言 golden contract）。

文件结构：
    ``<lang>.json`` — 单语言 golden fixture，包含：
        - ``language``: 语言名
        - ``sample_file``: 样本文件名
        - ``sample_source``: 样本源码（canonical bytes）
        - ``provenance``: 来源、确认时间、commit sha、方法说明
        - ``expected``: 人工确认的预期输出
            - ``symbols``: 符号列表（name/kind/signature/visibility/lexical_parent/line_start/line_end）
            - ``raw_calls``: 调用关系（caller_name/callee_name/ordinal/line）
            - ``imports``: import 列表（source_text/normalized_target）
            - ``references``: 引用列表（仅 HCL 等声明式语言）
        - ``known_gaps``: 当前 Rust/Python parser 相对预期输出的偏差
            - ``parser``: ``rust`` 或 ``python``
            - ``field``: ``symbols``/``calls``/``signature``/``visibility``/``references``/``imports``
            - ``description``: 偏差描述
            - ``phase``: 计划修复阶段（如 ``Phase 1.4``）

设计原则：
- golden fixture 是**人工确认的契约真相**，不代表任一 parser 当前实际输出
- 任何 parser 输出变化必须显式更新 fixture 和原因
- 不允许"整语言零符号"通过；预期输出必须非空（除非语言本身无该构造）
- byte range/ordinal 字段在 Step 4（identity_range_gate）补齐
"""
