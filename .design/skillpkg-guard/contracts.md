# contract-cards · skillpkg-guard

> R3 产出。6 张卡（全部 `new`），卡全文同时进任务单 description。
> REQ-01（must，revision 1）。文件统一：`skills/_shared/check_skill_packages.py`。

## 0. 数据形态（三处共用，先定死）

```python
@dataclass(frozen=True)
class Finding:
    package: str   # 包目录名；顶层问题用 "-"
    code: str      # 机器可判的违规码，见 §7
    detail: str    # 人读细节（含路径/文件名）

@dataclass(frozen=True)
class Package:
    path: Path          # skills/<name>
    skill_text: str     # SKILL.md 全文（utf-8-sig 已解码）
    frontmatter: dict[str, str] | None   # None = 无 frontmatter
```

约束：`Finding` / `Package` 均 `frozen=True`（只读语义，防检查器自己改状态）；不使用继承。

---

## C-01 · `parse_frontmatter`

- **文件/模块**：`skills/_shared/check_skill_packages.py::parse_frontmatter`
- **REQ**：REQ-01（must，revision：1）
- **签名**：`def parse_frontmatter(text: str) -> dict[str, str] | None:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `text` | `str` | 任意；可空串；可含 BOM（上游 `utf-8-sig` 已剥）；可 CRLF | 被调方：首行不匹配即返回 `None`，不抛 |

- **出参契约**：
  - 成功：`dict[str, str]`，键为 frontmatter 顶层的 `key: value`，值为去首尾空白的字符串；折行续写（不以 `key:` 开头的行）追加到**上一个键**，用单个空格连接
  - 失败：**不抛异常**。以下三种均返回 `None`：
    | 条件 | 触发 |
    |---|---|
    | 首个非空行不是 `---` | 无 frontmatter |
    | 找不到闭合 `---` | frontmatter 未闭合 |
    | 闭合前无任何 `key: value` 行 | 空 frontmatter |
  - **异常场景矩阵勾选**：空值 ✓（返回 None）；数据边界 ✓（BOM/CRLF/超长行/折行）；其余 6 类不适用（纯内存、无 IO）
- **异常路径**：本函数零 IO、零异常。`None` 与 `{}` 语义不同——`None` = 没有 frontmatter（违规），`{}` 不产出（空块按 None 处理）
- **副作用**：无。纯函数
- **依赖符号**：new（本卡片）
- **测试要点（GWT）**：
  - Given `"---\nname: a\ndescription: d\n---\n"` When 解析 Then `== {"name": "a", "description": "d"}`
  - Given `"# 无 frontmatter\n"` When 解析 Then `is None`
  - Given `"---\nname: a\n"`（未闭合）When 解析 Then `is None`
  - Given `"---\ndescription: 第一行\n  第二行\n---\n"` When 解析 Then `description == "第一行 第二行"`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发（纯函数不适用）
- **验收映射**：AC-02
- **注释要求**：doc comment 写 ①做什么 ②为什么不用 yaml（brief NFR 禁第三方依赖）③`None` vs `{}` 的错误语义
- **assumptions**：frontmatter 只解析**顶层**键；不支持嵌套（YAML 列表/字典）——现有三包均无嵌套，出现则另开 REQ

---

## C-02 · `load_packages`

- **文件/模块**：`skills/_shared/check_skill_packages.py::load_packages`
- **REQ**：REQ-01（must，revision：1）
- **签名**：`def load_packages(skills_root: Path) -> list[Package]:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `skills_root` | `Path` | 可不存在、可非目录 | 调用方 `main` 先判存在性并给专门错误；本函数不重复校验存在性，但**不抛 `FileNotFoundError`**——不存在时返回 `[]` |

- **出参契约**：
  - 成功：按目录名排序的 `list[Package]`；只收**直接子目录且含 `SKILL.md`**；`README.md` 等文件跳过；`_shared/` 因无 `SKILL.md` 自然跳过
  - 失败：返回 `[]`（空目录、路径不存在、路径是文件）
  - **异常场景矩阵勾选**：空值 ✓（返回 `[]`）；无权限 ✓（`OSError` 上抛，见异常路径）；重复 不适用；冲突 不适用；依赖失败 不适用；重试 不适用（幂等）；取消恢复 不适用；数据边界 ✓（BOM/CRLF 由 `utf-8-sig` 吸收）
- **异常路径**：
  | 失败点 | 去向 |
  |---|---|
  | `iterdir()` 权限拒绝 | **上抛 `OSError`**，由 `main` 捕获 → stderr 打路径+原因 → `EXIT_VIOLATION`（不抛栈） |
  | `SKILL.md` 读取失败 | 同上（上抛 `OSError`） |
  | 符号链接环 | 不处理：`iterdir` 不递归，无环风险 |
- **副作用**：读磁盘（`SKILL.md` 全文）。无状态、无锁、无网络
- **依赖符号**：new C-01 `parse_frontmatter`
- **测试要点（GWT）**：
  - Given `tmp_path` 下有 `a/SKILL.md`、`b/`（无 SKILL.md）、`README.md` When 加载 Then 只得到 `[a]`
  - Given 空目录 When 加载 Then `== []`（不抛）
  - Given `SKILL.md` 含 BOM When 加载 Then `skill_text` 首字符是 `-` 而非 `\ufeff`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-01（正例）、AC-02、AC-05
- **注释要求**：doc comment 写 ①为什么「含 SKILL.md」是包的唯一判据 ②为什么不存在时返回空列表而不是抛
- **assumptions**：不递归子目录（`.agents/skills/`、`docs/` 不在扫描范围，见 brief §5.4）

---

## C-03 · `referenced_reference_files`

- **文件/模块**：`skills/_shared/check_skill_packages.py::referenced_reference_files`
- **REQ**：REQ-01（must，revision：1）
- **签名**：`def referenced_reference_files(text: str) -> set[str]:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `text` | `str` | 任意 Markdown 文本 | 被调方：无匹配返回空集 |

- **出参契约**：
  - 成功：`set[str]`，元素是**文件名**（不含 `references/` 前缀），如 `{"cw-gates.md", "smoke.txt"}`
  - 失败：空集
  - **异常场景矩阵勾选**：空值 ✓（空集）；数据边界 ✓（超长行不截断、多个引用去重）；其余不适用
- **异常路径**：零 IO、零异常。正则 `references/([\w.\-]+)`，`findall` 不去重但 `set()` 去重
- **副作用**：无。纯函数
- **依赖符号**：new
- **测试要点（GWT）**：
  - Given ``"见 `references/cw-gates.md` 与 `references/smoke.txt`"`` When 提取 Then `== {"cw-gates.md", "smoke.txt"}`
  - Given `"../_shared/cw-gates.md"`（相对路径，非包内 references）When 提取 Then 空集（**不得**把 `_shared` 那条算成包内引用）
  - Given `"references/"`（无文件名）When 提取 Then 空集
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-03
- **注释要求**：doc comment 写 ①只认 `references/<filename>` 形态 ②为什么不追进代码块内的示例路径（避免把示例当断言）
- **assumptions**：不做代码块感知（`SKILL.md` 的示例里没有 `references/xxx` 形态的假路径；若出现，输出违规并由人判断——宁可吵不可漏）

---

## C-04 · `check_package`

- **文件/模块**：`skills/_shared/check_skill_packages.py::check_package`
- **REQ**：REQ-01（must，revision：**2** — CL-02 新增 `READ_FAILED`，读盘失败不抛）
- **签名**：`def check_package(pkg: Package, repo_root: Path) -> list[Finding]:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `pkg` | `Package` | 由 C-02 构造，已含 `skill_text` | 调用方保证 |
  | `repo_root` | `Path` | 仓库根；`smoke.txt` 路径以此为基准 | 调用方保证 |

- **出参契约**：
  - 成功：`list[Finding]`，**空列表 = 合规**；顺序固定为 ①frontmatter ②references ③duplicate-gates ④smoke ⑤install（保证输出稳定可断言）
  - 失败：本函数不抛（`findings` 累积；单条检查失败不短路，让一次跑暴露全部问题）
  - **异常场景矩阵勾选**：空值 ✓（`skill_text=""` → `FRONTMATTER_MISSING`）；无权限 由 C-02 处理，此处不重复；冲突 ✓（包内 `references/cw-gates.md`）；数据边界 ✓（空 `smoke.txt` 合法；`smoke.txt` 含 CRLF/空行/纯注释行均合法）
- **异常路径**：
  | 失败点 | 去向 |
  |---|---|
  | 无 frontmatter | Finding `FRONTMATTER_MISSING`（继续做后续检查，不早退） |
  | `name` ≠ 目录名 | Finding `FRONTMATTER_NAME_MISMATCH`，detail 含两者 |
  | `description` 缺失或空白 | Finding `FRONTMATTER_DESCRIPTION_EMPTY` |
  | 引用缺失 | 逐个 Finding `REFERENCE_MISSING`（一个缺失一条，不合并） |
  | `references/cw-gates.md` 存在 | Finding `DUPLICATE_GATES` |
  | `smoke.txt` 行路径不存在 | 逐个 Finding `SMOKE_BAD_PATH` |
  | `install.ps1` 不存在 | Finding `INSTALL_MISSING` |
  | `install.ps1` 存在但无注入片段 | Finding `INSTALL_MISSING_INJECTION` |
  | `smoke.txt` 不存在 | **不报**（可选文件，AC-05 只约束"存在时必须真实"） |
- **副作用**：读磁盘（`references/*`、`smoke.txt`、`install.ps1` 的存在性/内容）。`Path.is_file()` 权限异常被 `try/except OSError` 吸收为 `REFERENCE_MISSING` 之外的独立 Finding？**不**——直接视为不存在
- **依赖符号**：new C-01/C-03；reuse `EXIT_*` 常量语义
- **测试要点（GWT）**：
  - Given 真实三包（`skills/requirements-cw|code-review-cw|test-cw`）When 检查 Then 全部返回 `[]` ← **AC-01 的核心断言，跑在 CI 会立刻抓误报**
  - Given 包内 `references/cw-gates.md` 存在 When 检查 Then 含 `DUPLICATE_GATES`
  - Given `smoke.txt` 写 `tests/不存在.py` When 检查 Then 含 `SMOKE_BAD_PATH`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-01 / AC-02 / AC-03 / AC-04 / AC-05 / AC-06
- **注释要求**：doc comment 写 ①五类检查各自对应 brief 哪条约束 ②为什么单条失败不短路 ③为什么 `smoke.txt` 缺失不算违规
- **assumptions**：`install.ps1` **缺失** 算违规（包没有安装器 = 装不上）；brief 只写了"缺注入片段"，本卡把"缺文件"单列一个 code

---

## C-05 · `format_report`

- **文件/模块**：`skills/_shared/check_skill_packages.py::format_report`
- **REQ**：REQ-01（must，revision：1）
- **签名**：`def format_report(findings: list[Finding], package_count: int, as_json: bool) -> str:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `findings` | `list[Finding]` | 可空 | 被调方：空列表输出 OK 行 |
  | `package_count` | `int` | ≥0 | 调用方保证 |
  | `as_json` | `bool` | — | — |

- **出参契约**：
  - 成功：`str`（**不带尾随换行**，由 `print` 补）
    - 人读 + 空 findings：`"OK: 3 skill packages compliant"`
    - 人读 + 有 findings：每行 `"VIOLATION <package>: [<code>] <detail>"` 后接 `"<n> violation(s) in <m> package(s)"`
    - `--json`：`{"ok": bool, "packages": int, "findings": [{"package","code","detail"}]}`，`ensure_ascii=False`（中文 detail 不转义）
  - 失败：不抛
  - **异常场景矩阵勾选**：空值 ✓（OK 行）；其余不适用（纯格式化）
- **异常路径**：零 IO、零异常
- **副作用**：无。纯函数
- **依赖符号**：new
- **测试要点（GWT）**：
  - Given `([], 3, False)` When 格式化 Then 输出含 `"OK"` 且含 `"3"`
  - Given `([Finding("a","REFERENCE_MISSING","references/x.md")], 1, False)` When 格式化 Then 输出含 `"VIOLATION a"` 与 `"references/x.md"`
  - Given 任意 findings + `as_json=True` When 格式化 Then `json.loads()` 可解析且 `ok is False`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-01、AC-07
- **注释要求**：doc comment 写 ①`ok` 字段与退出码必须同源（同一 `len(findings)==0` 判据）②`ensure_ascii=False` 的原因
- **assumptions**：JSON schema 只这三键，不加版本号（无消费方前不预留）

---

## C-06 · `main`

- **文件/模块**：`skills/_shared/check_skill_packages.py::main`
- **REQ**：REQ-01（must，revision：**2** — CL-02 补 `check_package` 循环的 `except OSError` 兜底）
- **签名**：`def main(argv: list[str] | None = None) -> int:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `argv` | `list[str] \| None` | `None` → 取 `sys.argv[1:]`；测试直接传列表 | 被调方 |
  | `--root` | 路径串 | 缺省 = `<repo_root>/skills`，`repo_root` 由 `Path(__file__).resolve().parents[2]` 推出 | argparse |
  | `--json` | flag | 开关 | argparse |

- **出参契约**：
  - 成功（合规）：stdout `format_report(...)` + `return EXIT_OK (0)`
  - 失败（违规）：stdout `format_report(...)` + `return EXIT_VIOLATION (2)`
  - 失败（用法错）：`argparse` 自身打 usage 到 **stderr** + `return EXIT_USAGE (64)`——通过 `parser.error` 的 `SystemExit.code` 拦截转换，**不让 `SystemExit` 漏出 `main`**
  - 失败（`--root` 不存在或非目录 / 包数为 0）：stderr `"no skill packages found: <path>"` + `return EXIT_VIOLATION (2)` ← **防路径打错导致假绿**
  - 失败（`OSError`）：stderr `"cannot read: <path>: <reason>"` + `return EXIT_VIOLATION (2)`，**不抛栈**
  - **异常场景矩阵勾选**：空值 ✓（包数 0 → exit 2）；无权限 ✓（OSError → exit 2）；重复 ✓（跨包 `name` 冲突 → `DUPLICATE_PACKAGE_NAME`）；冲突 ✓（包内 cw-gates.md）；依赖失败 ✓（不 import 任何第三方）；重试 ✓（幂等，无副作用）；取消恢复 不适用（无中间写入）；数据边界 ✓（BOM/CRLF）
- **异常路径**：
  | 失败点 | 去向 |
  |---|---|
  | 未知参数 | `SystemExit(2)` → 转 `EXIT_USAGE(64)` |
  | `--root` 不存在/非目录 | exit 2 + `no skill packages found` |
  | 扫到 0 个包 | exit 2 + `no skill packages found` |
  | 读盘 `OSError` | exit 2 + 原因，不抛栈 |
  | 跨包 `name` 重复 | Finding `DUPLICATE_PACKAGE_NAME`（package 字段列全部冲突包名）→ exit 2 |
- **副作用**：写 stdout/stderr；读磁盘。无文件写入、无网络、无 daemon
- **依赖符号**：new C-02/C-04/C-05；reuse `cicd/known_red.py:206-271` 的 `main(argv)->int` + `sys.exit(main())` 形态
- **测试要点（GWT）**：
  - Given `--root <真实 skills/>` When `main(["--root", ...])` Then `== 0`
  - Given `--root <tmp 空目录>` When 调用 Then `== 2` 且 stderr 含 `no skill packages found`
  - Given `["--nope"]` When 调用 Then `== 64`（且不抛 `SystemExit`）
  - Given 两个包 `name` 相同 When 调用 Then `== 2` 且 stdout 含 `DUPLICATE_PACKAGE_NAME`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-01 / AC-06 / AC-07
- **注释要求**：doc comment 写 ①三态退出码语义 ②为什么 `SystemExit` 必须转成 `64` ③为什么"0 个包"是违规而不是合规
- **assumptions**：`repo_root` 由 `__file__` 上溯两级（`skills/_shared/x.py` → `skills/` → 仓库根）；不依赖 CWD，也不依赖 `PYTHONPATH`

---

## C-07 · `_read_text`（CL-02 新增）

- **文件/模块**：`skills/_shared/check_skill_packages.py::_read_text`
- **REQ**：REQ-01（must，revision：1）
- **签名**：`def _read_text(path: Path) -> str:`
- **入参契约**：

  | 参数 | 类型 | 约束 | 校验归属 |
  |---|---|---|---|
  | `path` | `Path` | 存在的文件 | 调用方先 `is_file()`；本函数不判存在性 |

- **出参契约**：
  - 成功：`str`，BOM 已剥
  - 失败：**抛 `OSError`**。两种来源合并为同一类型：
    | 来源 | 消息 |
    |---|---|
    | 真实 IO 失败（权限/被占用/消失） | 原 `OSError` 透传 |
    | 非 UTF-8 编码（`UnicodeDecodeError`） | `<path>: 非 UTF-8 编码（<reason>，byte <n>）；请另存为 UTF-8` |
  - **异常场景矩阵勾选**：数据边界 ✓（BOM / 非 UTF-8）；无权限 ✓（透传）；其余不适用
- **异常路径**：`UnicodeDecodeError` **不得原样上抛**——它继承 `ValueError` 而非 `OSError`，上抛会让 `except OSError` 接不住，检查器以 traceback 崩溃、退出码退化为 1（CL-02 实证）
- **副作用**：读磁盘。无写入
- **依赖符号**：new
- **测试要点（GWT）**：
  - Given GBK 编码的 `smoke.txt` When `check_package` Then `READ_FAILED` + `exit 2`，无 traceback
  - Given GBK 编码的 `SKILL.md` When `main` Then `exit 2`
  - 清单：[x] 边界值 [x] 异常分支 [ ] 并发
- **验收映射**：AC-08
- **注释要求**：doc comment 写 ①为什么归一为 OSError ②中文 Windows 记事本场景
- **assumptions**：只处理 UTF-8 与非 UTF-8 两类；不做编码嗅探自动转码（静默转码会把乱码当正常路径）

---

## 7. 违规码总表（唯一的机器可判集合）

| code | 触发 | 对应 AC |
|---|---|---|
| `FRONTMATTER_MISSING` | 无 frontmatter / 未闭合 / 空块 | AC-02 |
| `FRONTMATTER_NAME_MISMATCH` | `name` ≠ 目录名 | AC-02 |
| `FRONTMATTER_DESCRIPTION_EMPTY` | `description` 缺失或空白 | AC-02 |
| `DUPLICATE_PACKAGE_NAME` | 跨包 `name` 相同 | AC-02（并入） |
| `REFERENCE_MISSING` | 引用的 `references/<f>` 不存在且不在注入白名单 | AC-03 |
| `DUPLICATE_GATES` | 包内存在 `references/cw-gates.md` | AC-04 |
| `SMOKE_BAD_PATH` | `smoke.txt` 行路径不存在 | AC-05 |
| `INSTALL_MISSING` | `install.ps1` 不存在 | AC-06 |
| `INSTALL_MISSING_INJECTION` | `install.ps1` 无 `_shared/cw-gates.md` 注入片段 | AC-06 |
| `READ_FAILED` | 包内文件读取失败（权限 / 被占用 / **非 UTF-8 编码**） | AC-08（CL-02） |

> 码表即契约：新增检查必须同时加码、加 AC（或并入既有 AC）、加测试。禁止出现"能报但不测"的码——那在 test-cw 视角就是 GAP。

## 8. 被检查对象现状核对（设计期实测定基）

| 包 | references 实际文件 | SKILL.md 引用（去注入项） | `install.ps1` 注入 | 预期结果 |
|---|---|---|---|---|
| requirements-cw | change-ticket/contract-card/gate-card | 三者均存在 | ✅ 第 34 行 | `[]` |
| code-review-cw | （空目录） | 仅 `cw-gates.md`（注入项，豁免） | ✅ 第 33 行 | `[]` |
| test-cw | evidence-spec.md / smoke.txt | 两者存在 + `cw-gates.md`（豁免） | ✅ 第 34 行 | `[]` |

⇒ AC-01 的正例是可成立的（这是 F-A1 修正后才成立的，修正前必然 3 红）。
