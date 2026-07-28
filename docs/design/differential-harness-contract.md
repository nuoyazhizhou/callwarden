# Python/Rust Differential Harness 与基线契约（Phase 0 子任务 3 Contract）

> 本文件是 [rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 0 第三个子任务的契约交付物。
> 它定义 Python/Rust 双实现差分对照的 harness 接口、基线数据结构、性能基线指标和回归阈值，
> 作为后续每个功能子任务 differential-test 步骤的执行框架。
>
> 真相源：
> - `tests/parser_contract/generate_baseline.py`（基线生成脚本）
> - `tests/parser_contract/baseline.json`（基线数据）
> - `tests/parser_contract/test_baseline.py`（基线校验测试）
> - `tests/test_rust_python_alignment.py`（已存在的对齐测试）
> - `tests/parser_contract/test_identity_range.py`（身份与范围对齐测试）
> - `abi-error-code-contract.md`（ABI 契约）
>
> 维护规则：每次 ABI 变更或新增差分测试时同步本文件。

## 1. Differential Harness 设计目标

### 1.1 核心目标

- **统一对照入口**：所有 Python/Rust 双实现差分测试通过同一 harness 执行
- **结构化结果比较**：不只是 pass/fail，而是输出结构化差异（字段级）
- **可重放基线**：基线 JSON 可重新生成，记录 commit_sha 用于追溯
- **回归检测**：性能基线 + 功能基线双重回归门禁
- **缺口显式化**：已知缺口在 baseline.json 中显式记录，不静默放行

### 1.2 设计原则

- **不修改代码**：harness 只读取 parser 输出并比较
- **失败如实记录**：任一 parser 报错必须记录到 `gaps`，不掩饰
- **无外部 fixture 依赖**：所有样本代码内联或来自 `golden/` fixtures
- **跨平台兼容**：Windows/Linux/macOS 均可运行
- **CI 友好**：harness 可在 CI 中自动运行，生成 JUnit XML + JSON 报告

## 2. Harness 接口契约

### 2.1 DifferentialHarness 类接口

```python
class DifferentialHarness:
    """Python/Rust 双实现差分对照 harness。

    用法：
        harness = DifferentialHarness()
        result = harness.compare_parse(path, lang, module_path="test")
        if result.has_diff:
            print(result.format_diff())
    """

    def __init__(self, rust_strict: bool = True) -> None:
        """初始化 harness。

        Args:
            rust_strict: True=Rust 解析失败时抛异常，False=记录到 gaps
        """

    def compare_parse(
        self,
        file_path: str,
        lang: str,
        module_path: str = "test",
    ) -> "DiffResult":
        """对照解析同一文件，返回结构化差异结果。"""

    def compare_batch(
        self,
        files: list[tuple[str, str, str]],
    ) -> list["DiffResult"]:
        """批量对照解析。"""

    def generate_baseline(
        self,
        output_path: Path,
        commit_sha: str | None = None,
    ) -> dict:
        """生成基线 JSON，记录当前 commit 的双实现能力快照。"""

    def verify_baseline(
        self,
        baseline_path: Path,
        regression_threshold: float = 1.5,
    ) -> "BaselineVerification":
        """验证当前代码与基线的一致性，检测回归。"""
```

### 2.2 DiffResult 数据结构

```python
@dataclass
class DiffResult:
    """单文件差分对照结果。"""

    file_path: str
    language: str
    module_path: str

    # Python 解析结果
    py_result: dict | None  # None 表示解析失败
    py_error: str | None

    # Rust 解析结果
    rs_result: dict | None
    rs_error: str | None

    # 结构化差异
    symbol_diff: SymbolDiff
    call_diff: CallDiff
    import_diff: ImportDiff
    reference_diff: ReferenceDiff

    # 诊断
    py_diagnostics: dict | None
    rs_diagnostics: dict | None

    @property
    def has_diff(self) -> bool:
        """是否存在任何差异（排除已知差异后）。"""

    @property
    def has_error(self) -> bool:
        """任一 parser 是否报错。"""

    def format_diff(self) -> str:
        """格式化差异为可读字符串。"""

    def to_dict(self) -> dict:
        """序列化为 dict（用于 JSON 输出）。"""
```

### 2.3 差异类型定义

```python
@dataclass
class SymbolDiff:
    """符号差异。"""

    only_in_py: list[dict]  # Python 有，Rust 无
    only_in_rs: list[dict]  # Rust 有，Python 无
    field_mismatches: list[FieldMismatch]  # 同一符号字段不一致
    known_diffs: list[KnownDiff]  # 已知差异（不视为失败）

@dataclass
class CallDiff:
    """调用差异。"""

    only_in_py: list[dict]
    only_in_rs: list[dict]
    field_mismatches: list[FieldMismatch]
    known_diffs: list[KnownDiff]

@dataclass
class FieldMismatch:
    """字段级差异。"""

    symbol_name: str
    field_name: str
    py_value: Any
    rs_value: Any

@dataclass
class KnownDiff:
    """已知差异（在 baseline.json 中声明）。"""

    description: str
    phase: str  # 哪个 Phase 会修复
    reason: str
```

### 2.4 BaselineVerification 数据结构

```python
@dataclass
class BaselineVerification:
    """基线验证结果。"""

    baseline_commit: str
    current_commit: str
    is_consistent: bool  # 功能一致性
    has_performance_regression: bool  # 性能回归
    regressions: list[Regression]
    new_gaps: list[str]  # 新发现的缺口
    fixed_gaps: list[str]  # 已修复的缺口

@dataclass
class Regression:
    """回归项。"""

    metric: str
    baseline_value: float
    current_value: float
    ratio: float  # current / baseline
    threshold: float
    is_regression: bool
```

## 3. 基线数据结构契约

### 3.1 baseline.json 顶层结构

```json
{
    "generated_at": "ISO8601",
    "commit_sha": "<git HEAD>",
    "platform": {
        "python": "...",
        "platform": "...",
        "machine": "..."
    },
    "language_capability": {
        "<lang>": {
            "rust_supported": true,
            "python_parser_available": true,
            "sample_path": "<fixture filename>",
            "symbols_count_py": 0,
            "symbols_count_rs": 0,
            "kinds_py": [],
            "kinds_rs": [],
            "signature_present_py": true,
            "signature_present_rs": true,
            "visibility_present_py": true,
            "visibility_present_rs": true,
            "calls_count_py": 0,
            "calls_count_rs": 0,
            "imports_count_py": 0,
            "imports_count_rs": 0,
            "references_present_py": true,
            "references_present_rs": true,
            "rust_module_path": true,
            "python_module_path": true,
            "known_symbol_diffs_count": 0,
            "known_call_diffs_count": 0,
            "known_symbol_diffs_reason": "",
            "known_call_diffs_reason": "",
            "gaps": []
        }
    },
    "phase0_completion_gates": {
        "tests_expose_typescript_gap": true,
        "tests_expose_php_gap": true,
        "tests_expose_scala_gap": true,
        "tests_expose_hcl_gap": true
    },
    "bundle_distribution_breakdown": {},
    "bundle_size_baseline": {
        "unpacked_bytes": 0,
        "unpacked_mb": 0.0,
        "methodology": ""
    },
    "performance_baseline": {
        "parse_p50_ms": 0.0,
        "parse_p95_ms": 0.0,
        "graphstore_load_p50_ms": 0.0,
        "get_callers_p50_ms": 0.0
    }
}
```

### 3.2 language_capability 字段契约

| 字段 | 类型 | 说明 |
|---|---|---|
| `rust_supported` | bool | Rust parser 是否支持该语言 |
| `python_parser_available` | bool | Python parser 是否可用 |
| `sample_path` | str | 样本文件名 |
| `symbols_count_py` | int | Python parser 提取的符号数 |
| `symbols_count_rs` | int | Rust parser 提取的符号数 |
| `kinds_py` | list[str] | Python 提取的符号种类 |
| `kinds_rs` | list[str] | Rust 提取的符号种类 |
| `signature_present_py` | bool | Python 是否输出 signature 字段 |
| `signature_present_rs` | bool | Rust 是否输出 signature 字段 |
| `visibility_present_py` | bool | Python 是否输出 visibility 字段 |
| `visibility_present_rs` | bool | Rust 是否输出 visibility 字段 |
| `calls_count_py` | int | Python 提取的调用数 |
| `calls_count_rs` | int | Rust 提取的调用数 |
| `imports_count_py` | int | Python 提取的 import 数 |
| `imports_count_rs` | int | Rust 提取的 import 数 |
| `references_present_py` | bool | Python 是否输出 references |
| `references_present_rs` | bool | Rust 是否输出 references |
| `rust_module_path` | bool | Rust 是否输出 module_path |
| `python_module_path` | bool | Python 是否输出 module_path |
| `known_symbol_diffs_count` | int | 已知符号差异数 |
| `known_call_diffs_count` | int | 已知调用差异数 |
| `known_symbol_diffs_reason` | str | 已知符号差异原因 |
| `known_call_diffs_reason` | str | 已知调用差异原因 |
| `gaps` | list[str] | 当前明确暴露的缺口描述 |

### 3.3 phase0_completion_gates 契约

| Gate | 说明 | 通过条件 |
|---|---|---|
| `tests_expose_typescript_gap` | TypeScript 缺口 | baseline 检测到 Rust 漏提取符号 |
| `tests_expose_php_gap` | PHP 缺口 | baseline 检测到 property 缺失 |
| `tests_expose_scala_gap` | Scala 缺口 | baseline 检测到对象方法调用缺失 |
| `tests_expose_hcl_gap` | HCL 缺口 | baseline 检测到引用未在 Rust 路径提取 |

**注意**：这些 gate 为 `True` 表示 baseline **正确暴露了缺口**，而非缺口已修复。当 Rust 修复缺口后，gate 应改为 `False` 并更新 baseline 生成脚本。

## 4. 性能基线契约

### 4.1 性能指标定义

| 指标 | 测量方法 | 单位 | 目标 |
|---|---|---|---|
| `parse_p50_ms` | 单文件 parse 50 次取中位数 | ms | < 100 |
| `parse_p95_ms` | 单文件 parse 100 次取 P95 | ms | < 200 |
| `graphstore_load_p50_ms` | GraphStore 加载 1M 符号 | ms | < 5000 |
| `graphstore_load_p95_ms` | GraphStore 加载 1M 符号 | ms | < 10000 |
| `get_callers_p50_ms` | GraphStore get_callers 1000 次 | ms | < 1 |
| `get_callers_p95_ms` | GraphStore get_callers 1000 次 | ms | < 5 |
| `watcher_update_p95_ms` | 单文件 watcher 更新 | ms | < 3000 |
| `build_full_graph_p50_ms` | 1M 符号全量构建 | ms | 待测 |
| `build_full_graph_p95_ms` | 1M 符号全量构建 | ms | 待测 |

### 4.2 性能基线测量原则

- **测量工具**：`time.perf_counter()`（Python）+ `std::time::Instant`（Rust）
- **样本量**：P50 至少 50 次，P95 至少 100 次
- **预热**：前 5 次结果丢弃，避免冷启动影响
- **隔离**：每次测量独立进程，避免缓存污染
- **硬件记录**：baseline.json 必须记录 CPU 型号、内存、磁盘类型
- **跨平台**：Windows/Linux/macOS 分别建立基线

### 4.3 回归阈值

| 指标类型 | 回归阈值 | 处理策略 |
|---|---|---|
| 功能基线（符号/调用数） | 0（不允许回归） | 立即修复 |
| 性能 P50 | 1.5x（基线 1.5 倍） | 警告，CI 标黄 |
| 性能 P95 | 2.0x（基线 2 倍） | 失败，CI 标红 |
| 内存 RSS | 1.5x | 警告 |
| 二进制体积 | 1.2x | 警告 |

### 4.4 性能基线文件位置

- `tests/parser_contract/baseline.json`：功能基线（16 语言能力）
- `tests/perf/perf_baseline.json`：性能基线（P50/P95 指标）
- `tests/perf/hardware_info.json`：硬件信息记录

## 5. 差分测试分类

### 5.1 已有差分测试（Phase 0 前已存在）

| 测试文件 | 覆盖范围 | 说明 |
|---|---|---|
| `tests/test_rust_python_alignment.py` | 16 语言符号/调用对齐 | 已修复假绿，严格比较 |
| `tests/parser_contract/test_identity_range.py` | 身份与范围对齐 | local_id/parent/range/hash |
| `tests/parser_contract/test_golden_fixtures.py` | golden fixture 结构校验 | 16 语言 fixture 完整性 |
| `tests/parser_contract/test_encoding_error.py` | 编码与错误处理 | BOM/CRLF/UTF16/GBK/syntax error |
| `tests/parser_contract/test_baseline.py` | baseline.json 校验 | 4 个 Phase 0 gate |
| `tests/test_p13_perf_baseline.py` | 阶段耗时基线 | _stage_timings 暴露 |

### 5.2 新增差分测试（Phase 0 子任务 3 新增）

| 测试文件 | 覆盖范围 | 说明 |
|---|---|---|
| `tests/test_differential_harness.py` | harness 接口契约 | 验证 DifferentialHarness 类接口 |
| `tests/test_performance_baseline.py` | 性能基线 | P50/P95 指标验证 |

### 5.3 差分测试分类

| 类型 | 触发条件 | 失败处理 |
|---|---|---|
| **严格对齐** | 字段必须完全一致 | 立即修复 |
| **已知差异** | 在 baseline.json 声明 | 更新 baseline，记录 phase |
| **性能回归** | P50 > 1.5x | 警告，分析原因 |
| **性能失败** | P95 > 2.0x | 阻塞合并 |
| **功能回归** | 符号/调用数减少 | 阻塞合并 |
| **缺口暴露** | baseline.json gate=True | 记录，不阻塞 |

## 6. 已知差异管理

### 6.1 已知差异声明位置

- **符号差异**：`baseline.json` 的 `language_capability.<lang>.known_symbol_diffs_*`
- **调用差异**：`baseline.json` 的 `language_capability.<lang>.known_call_diffs_*`
- **fixture 级差异**：`tests/parser_contract/golden/<lang>.json` 的 `known_gaps` 字段

### 6.2 已知差异字段契约

```json
{
    "parser": "rust",
    "field": "signature",
    "description": "Rust 未实现 signature 提取",
    "phase": "Phase 2.7",
    "reason": "extract_signature 函数待实现",
    "fix_commit": null
}
```

### 6.3 已知差异生命周期

1. **发现缺口**：差分测试发现新差异 → 声明到 baseline.json
2. **修复缺口**：Rust 实现补齐 → 更新 baseline.json，标记 `fix_commit`
3. **验证修复**：差分测试通过 → 从 `known_gaps` 移除
4. **回归检测**：后续测试若再次出现该差异 → 失败（不再视为已知）

## 7. CI 集成契约

### 7.1 CI 流程

```yaml
# 伪代码
- name: Generate Baseline
  run: python tests/parser_contract/generate_baseline.py

- name: Verify Baseline
  run: python tests/parser_contract/verify_baseline.py --regression-threshold 1.5

- name: Run Differential Tests
  run: pytest tests/test_rust_python_alignment.py tests/parser_contract/ -v

- name: Run Performance Tests
  run: pytest tests/test_performance_baseline.py -v --benchmark-only

- name: Upload Baseline
  uses: actions/upload-artifact@v3
  with:
    name: baseline-${{ github.sha }}
    path: tests/parser_contract/baseline.json
```

### 7.2 CI 门禁

| 门禁 | 失败条件 | 处理 |
|---|---|---|
| 功能对齐 | 严格对齐测试失败 | 阻塞合并 |
| baseline 一致性 | baseline.json 与代码不一致 | 阻塞合并 |
| 性能回归 | P50 > 1.5x 或 P95 > 2.0x | 阻塞合并 |
| 缺口暴露 | phase0_completion_gates 应为 True 但为 False | 阻塞合并（baseline 退化） |
| 缺口修复 | 新增 fix_commit 但未更新 baseline | 警告 |

## 8. 不变量

1. **baseline.json 必须可重生成**：任何 commit 都能运行 `generate_baseline.py` 生成有效 JSON
2. **commit_sha 必须一致**：baseline.json 的 commit_sha 必须与当前 HEAD 一致，否则需重新生成
3. **已知差异必须声明**：所有非严格对齐的差异必须在 baseline.json 或 golden fixture 中显式声明
4. **性能基线必须跨平台**：Windows/Linux/macOS 分别建立，不混用
5. **差分测试必须可独立运行**：不依赖外部服务（MCP/daemon）
6. **harness 必须无副作用**：不修改文件、不写数据库、不启动进程
7. **缺口修复必须更新 baseline**：修复缺口后必须重新生成 baseline.json
8. **回归阈值必须显式**：所有阈值在 baseline.json 或测试代码中显式声明

## 9. 生产接入点

### 9.1 已接入的差分测试基础设施

| 入口 | 模块 | 说明 |
|---|---|---|
| `tests/parser_contract/generate_baseline.py` | 基线生成脚本 | 16 语言能力快照 |
| `tests/parser_contract/baseline.json` | 基线数据 | 功能基线 |
| `tests/parser_contract/test_baseline.py` | 基线校验 | 4 个 Phase 0 gate |
| `tests/test_rust_python_alignment.py` | 对齐测试 | 16 语言严格对齐 |
| `tests/parser_contract/test_identity_range.py` | 身份对齐 | local_id/parent/range |
| `tests/parser_contract/golden/` | golden fixtures | 16 语言契约真相 |

### 9.2 待接入的差分测试基础设施（本子任务新增）

| 入口 | 模块 | 说明 |
|---|---|---|
| `tests/differential_harness.py` | harness 实现 | DifferentialHarness 类 |
| `tests/test_differential_harness.py` | harness 测试 | 验证 harness 接口 |
| `tests/perf/perf_baseline.json` | 性能基线数据 | P50/P95 指标 |
| `tests/test_performance_baseline.py` | 性能基线测试 | 验证性能指标 |

## 10. Review 清单

### 10.1 待 Review 关键点

1. **harness 接口完整性**：第 2 节 DifferentialHarness 类接口是否满足后续功能子任务需求？
2. **基线数据结构**：第 3 节 baseline.json 结构是否完整？是否需要补充字段？
3. **性能指标**：第 4 节 9 个性能指标是否覆盖关键路径？阈值是否合理？
4. **差分测试分类**：第 5 节分类是否清晰？严格对齐 vs 已知差异的边界是否明确？
5. **已知差异管理**：第 6 节生命周期是否完整？修复后回归检测是否可靠？
6. **CI 集成**：第 7 节门禁是否充分？是否遗漏关键检查？
7. **不变量**：第 8 节 8 个不变量是否充分？
8. **生产接入点**：第 9 节是否遗漏关键基础设施？

### 10.2 风险与注意事项

- **baseline.json 维护**：每次修复缺口后需手动重新生成，存在遗忘风险
- **性能基线跨平台**：不同硬件配置导致基线差异，需在 CI 中固定硬件
- **golden fixture 与 baseline 重复**：两者都记录已知差异，存在信息冗余风险
- **harness 复杂度**：DifferentialHarness 类可能过度设计，需评估是否真的需要统一入口
- **性能测试稳定性**：CI 环境性能波动大，P95 阈值可能过于严格
- **commit_sha 一致性**：本地修改未提交时 baseline.json 的 commit_sha 与 HEAD 不一致

## 11. Phase 0 子任务 3 Review 清单（2026-07-27）

### 11.1 交付物

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/design/differential-harness-contract.md` | 文档（真相源） | 10 章节：Harness 设计目标 / 接口契约 / 基线数据结构 / 性能基线 / 差分测试分类 / 已知差异管理 / CI 集成 / 不变量 / 生产接入点 / Review |
| `rust_ext/src/differential_baseline.rs` | Rust 模块 | 7 性能目标常量 + 4 回归阈值 + 4 Phase 0 gate + LanguageCapability 22 字段 + PerformanceBaseline 9 字段 + Regression 检测 + BaselineVerification + KnownDiff + BaselineSnapshot + 18 单元测试 |
| `tests/test_differential_harness.py` | Python 测试 | 31 个差分测试：契约完整性 / Rust 模块一致性 / baseline.json 结构 / 已有差分测试 / 不变量 / 跨语言常量一致性 |
| `server/differential_harness_service.py` | Python 服务 | DifferentialHarnessService 查询服务（只读/无状态/无锁），镜像 Rust 模块常量，10 个查询方法 |

### 11.2 测试结果

| 测试套件 | 结果 |
|---|---|
| Rust `differential_baseline` 单元测试 | ✅ 18 passed |
| Python `test_differential_harness.py` | ✅ 31 passed |
| Python `test_abi_contract.py`（回归） | ✅ 26 passed |
| Python `test_migration_manifest.py`（回归） | ✅ 15 passed, 1 skipped |
| `cargo check` 编译 | ✅ 通过（仅 warnings） |
| `DifferentialHarnessService` 跨语言一致性 | ✅ parse_p50=100ms/p50_threshold=1.5/typescript_gate ok |
| baseline.json 加载与 Phase 0 gate 验证 | ✅ all_gates_exposed=True |

### 11.3 待 Review 关键点

1. **harness 接口完整性**：第 2 节 DifferentialHarness 类接口是否满足后续功能子任务需求？是否过度设计？
2. **基线数据结构**：第 3 节 baseline.json 结构是否完整？22 个 language_capability 字段是否必要？
3. **性能指标**：第 4 节 9 个性能指标是否覆盖关键路径？P50/P95 目标值是否合理？回归阈值（1.5x/2.0x）是否合适？
4. **差分测试分类**：第 5 节分类是否清晰？严格对齐 vs 已知差异的边界是否明确？6 类分类是否冗余？
5. **已知差异管理**：第 6 节生命周期 4 步是否完整？修复后回归检测机制是否可靠？
6. **CI 集成**：第 7 节 5 个门禁是否充分？是否遗漏关键检查？CI 流程是否可落地？
7. **不变量**：第 8 节 8 个不变量是否充分？是否需要补充？
8. **跨语言一致性**：Rust `differential_baseline.rs` 和 Python `differential_harness_service.py` 镜像是否完整？后续是否应通过 PyO3 直接共享？
9. **与 abi_contract 模块的边界**：differential_baseline 和 abi_contract 是否存在职责重叠？是否需要合并？

### 11.4 风险与注意事项

- **baseline.json 维护**：每次修复缺口后需手动重新生成，存在遗忘风险。可考虑后续在 pre-commit hook 中自动检查
- **性能基线跨平台**：不同硬件配置导致基线差异，需在 CI 中固定硬件或使用相对比值
- **golden fixture 与 baseline 重复**：两者都记录已知差异，存在信息冗余风险。后续可考虑统一
- **harness 复杂度**：DifferentialHarness 类当前只定义了接口，实际实现需要后续子任务补充。需评估是否真的需要统一入口
- **性能测试稳定性**：CI 环境性能波动大，P95 阈值可能过于严格。可考虑使用相对比值而非绝对值
- **commit_sha 一致性**：本地修改未提交时 baseline.json 的 commit_sha 与 HEAD 不一致。CI 中应强制重新生成
- **跨语言常量镜像**：Rust `differential_baseline.rs` 和 Python `differential_harness_service.py` 是镜像关系，变更时需双向同步
- **callwarden_core.pyd DLL 依赖**：refresh 时 parser 不可用，符号未解析。需在后续任务中修复
- **与 abi_contract 的边界**：differential_baseline 聚焦性能和基线，abi_contract 聚焦 ABI 和错误码。当前职责清晰，但后续可能需要合并常量定义
