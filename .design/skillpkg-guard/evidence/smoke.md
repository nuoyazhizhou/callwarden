# 冒烟集 · 强制并入（feature 级兜底）

## 头部
- 范围：`REQ-01` 全 feature（不绑单条 AC；test-cw SKILL.md §2「任何 scope 都并入」）
- 承接任务：`T-1795312729595-643ffdec`
- `run_revision`：1787ad8fd782b5e40470c5d200f91f3a2bd761e1
- `code_revision`：cc37b901da717f8b6c39ad04bf8dc899a74d6f44
- 执行时间：2026-11-22 10:37:42 +0800

## 环境指纹
OS Windows 11 / AMD64；Python `C:/Python314/python.exe`；`CW_TEST_MODE=1`；`PYTHONPATH=C:/git_work`；未触 Rust。

## 命令原文
```bash
PYTHONPATH=C:/git_work CW_TEST_MODE=1 C:/Python314/python.exe -m pytest \
  tests/test_abi_contract.py tests/test_i18n_key_existence.py \
  tests/test_i18n_placeholder_consistency.py tests/test_cli_reference_sections.py \
  tests/test_external_import_scope.py -q --no-header \
  --junitxml=.design/skillpkg-guard/reports/junit-smoke.xml
```

## 统计
| collected | passed | failed | error | skipped | rc |
|---|---|---|---|---|---|
| 164 | 164 | 0 | 0 | 0 | 0 |

## 必需 nodeid 完成表
文件级 5 项（`smoke.txt` 冻结集全量），逐文件 outcome：

| 文件 | outcome |
|---|---|
| tests/test_abi_contract.py | passed |
| tests/test_i18n_key_existence.py | passed |
| tests/test_i18n_placeholder_consistency.py | passed |
| tests/test_cli_reference_sections.py | passed |
| tests/test_external_import_scope.py | passed |

## 关键输出
```
........................................................................ [ 43%]
........................................................................ [ 87%]
....................                                                     [100%]
rc=0
```

## 判读
**PASS** —— 冻结冒烟集 164 例全绿、rc=0。新增 `skills/_shared/check_skill_packages.py`
未触碰 ABI / i18n / CLI 文档 / 外部符号导入面，整仓级主链路未断（判据 ③⑥）。

> 与本 feature 改动的关系：本需求只新增两个文件，理论上不该影响冒烟面。实跑证明"理论上"
> 成立——这类"理论上不影响"的假设正是冒烟集存在的理由。

## 附件指针
- `.design/skillpkg-guard/reports/junit-smoke.xml`
- sha256：`cef9e106e1bb04c454ac99517e481d879d75fe74a0091871253e1f93d1e765ee`
