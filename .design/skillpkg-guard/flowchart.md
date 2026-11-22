# flowchart · skillpkg-guard

> R1 产出。主链路 + **四条异常分支**（空目录 / 缺引用 / 违反单一源 / 坏路径）。

## 主流程

```mermaid
flowchart TD
    A[main argv] --> B{argv 合法?}
    B -- 否 --> Z64[exit 64<br/>usage 到 stderr]
    B -- 是 --> C[resolve skills_root]
    C --> D{root 是目录且存在?}
    D -- 否 --> Z2A[exit 2<br/>no skill packages found]
    D -- 是 --> E[list_subdirs 含 SKILL.md]
    E --> F{包数 == 0?}
    F -- 是 --> Z2B[exit 2<br/>no skill packages found]
    F -- 否 --> G[for each pkg]

    G --> H1[check_frontmatter]
    H1 --> H2[check_references]
    H2 --> H3[check_no_duplicate_gates]
    H3 --> H4[check_smoke_paths]
    H4 --> H5[check_install_injection]
    H5 --> I{还有包?}
    I -- 是 --> G
    I -- 否 --> J{findings 非空?}
    J -- 是 --> Z2C[exit 2<br/>打印 VIOLATION 清单]
    J -- 否 --> Z0[exit 0<br/>打印 OK + 包数]
```

## 异常分支 1 · 空目录 / 路径打错

```mermaid
flowchart LR
    A1[传入 C:/nonexist] --> A2{is_dir?}
    A2 -- 否 --> A3[exit 2<br/>no skill packages found: C:/nonexist]
    A4[skills/ 存在但无子包] --> A5[exit 2<br/>no skill packages found: skills/]
    A3 --> A6[注：故意不用 exit 0<br/>否则路径打错会假绿]
    A5 --> A6
```

## 异常分支 2 · 引用断链（AC-03）

```mermaid
flowchart TD
    B1[SKILL.md 文本] --> B2[正则扫 references/NAME]
    B2 --> B3{文件存在?}
    B3 -- 是 --> B4[通过]
    B3 -- 否 --> B5[VIOLATION pkg: missing reference references/NAME]
    B5 --> B6[exit 2]
```

> 边界：正则只取 `references/<filename>` 形态，文件名限定 `[\w.\-]+`；不追进代码块内的示例路径（见 contracts.md C-02 的豁免规则）。

## 异常分支 3 · 违反单一源（AC-04）

```mermaid
flowchart TD
    C1[pkg/references/cw-gates.md] --> C2{存在?}
    C2 -- 是 --> C3[VIOLATION pkg: references/cw-gates.md must come from _shared]
    C3 --> C4[exit 2]
    C2 -- 否 --> C5[通过]
```

## 异常分支 4 · 冒烟集坏路径（AC-05）

```mermaid
flowchart TD
    D1[smoke.txt] --> D2{存在?}
    D2 -- 否 --> D3[通过<br/>无 smoke.txt 不是违规]
    D2 -- 是 --> D4[按行读 utf-8-sig]
    D4 --> D5[strip; 跳过空行与 # 开头]
    D5 --> D6{path 存在?<br/>相对仓库根}
    D6 -- 是 --> D7[通过]
    D6 -- 否 --> D8[VIOLATION pkg: smoke.txt bad path PATH]
    D8 --> D9[exit 2]
```

> 关键：**整行**做路径，不切 `#`。行内注释会被当成路径的一部分（历史真实故障，见 test-cw `references/smoke.txt` 的注释行修复）。因此 `#` 只允许出现在行首。

## 数据流

```
argv ──► parse_args ──► RepoRoot(derived from __file__ 上溯两级)
                          │
                          ├─► scan_packages(root) -> list[Pkg]
                          ├─► run_checks(pkg) -> list[Finding]
                          └─► format(findings, json?) -> stdout
                                              └─► exit_code(findings, parse_ok)
```

## 状态断言（设计期即固定）

| 断言 | 值 |
|---|---|
| 无中间写入 | 全程只读，无临时文件 |
| 幂等 | 同一输入两次运行，findings 与 exit code 完全一致 |
| 耗时 | 真实三包 < 1s |
| 网络 | 0 次 |
| 第三方依赖 | 0（仅 `argparse`/`json`/`os`/`re`/`sys`/`pathlib`） |
