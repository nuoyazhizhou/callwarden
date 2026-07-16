# 测试矩阵 4：resolved_edges + L5 include_path/sysroot 解析报告

> 执行时间：2026-07-16 15:17:43
> C/C++ 项目数：4

## resolved_edges 计算结果

| 项目 | ws_id | bc_hash | resolve 耗时 | edges 总数 | source | skipped |
|------|-------|---------|-------------|-----------|--------|---------|
| curl | 1 | b29e6d413683889e | 0.9s | 500 |  | 0 |
| fmt | 1 | b29e6d413683889e | 0.4s | 500 |  | 0 |
| redis | 1 | b29e6d413683889e | 0.7s | 500 |  | 0 |
| spdlog | 1 | b29e6d413683889e | 0.3s | 500 |  | 0 |

## resolution_method 分布（L1-L5）

| 项目 | exact_match | simple_name_unique | same_file | include_path | sysroot | from_calls | unresolved |
|------|-------------|---------------------|-----------|--------------|---------|------------|-----------|
| curl | 0 | 0 | 0 | 0 | 0 | 500 | 0 |
| fmt | 0 | 0 | 0 | 0 | 0 | 500 | 0 |
| redis | 0 | 0 | 0 | 0 | 0 | 500 | 0 |
| spdlog | 0 | 0 | 0 | 0 | 0 | 500 | 0 |

## L5 include_path/sysroot 解析验证

L5 新增的 `include_path` 和 `sysroot` resolution_method 用于解决 C/C++ 头文件多候选歧义。
触发条件：简名有多个候选（`len(candidates) > 1`）且 build context 有 include_paths 或 toolchain sysroot。

### 验证点

1. **include_path 命中**：candidate 的 rel_path 前缀匹配 build_context.include_paths
2. **sysroot 命中**：candidate 的 rel_path 前缀匹配 toolchain.sysroot 或 include_dirs 的 basename
3. **include_path 优先于 sysroot**
4. **多匹配则交由 unresolved**

## 汇总统计

- 总 C/C++ 项目数：4
- 总 resolved_edges：2000
- 已解析（非 unresolved）：2000
- L4a include_path 命中：0
- L4b sysroot 命中：0
- L5 解析率（已解析/总数）：100.0%