# mindmap · skillpkg-guard

> R1 产出。领域概念与归属，用于确认"检查器该管什么、不该管什么"。

```
技能包结构自检 (skillpkg-guard)
├── 输入域
│   ├── skills/                     ← 扫描根（assumption：不含 _shared/、不含 .agents/skills/）
│   │   ├── requirements-cw/        ← 真实包（正例）
│   │   ├── code-review-cw/         ← 真实包（正例）
│   │   ├── test-cw/                ← 真实包 + smoke.txt（正例）
│   │   └── README.md               ← 非目录，跳过
│   └── _shared/                    ← 单一门禁源所在，不被自身扫描
│       ├── cw-gates.md             ← 唯一事实源
│       └── check_skill_packages.py ← 本需求新增（被检查者与检查者同目录，注意不自我扫描）
├── 被检查对象（5 类）
│   ├── SKILL.md frontmatter        ← name 与目录名一致 / description 非空 / 必须有 frontmatter
│   ├── references/ 引用完整性      ← SKILL.md 提到的 references/<file> 必须存在
│   ├── references/cw-gates.md      ← 禁止存在（单一源）
│   ├── references/smoke.txt        ← 非注释行必须是真实路径（相对仓库根）
│   └── install.ps1                 ← 必须含 _shared/cw-gates.md 注入片段
├── 输出域
│   ├── stdout 人读                 ← 每行 "VIOLATION <pkg>: <reason>"
│   ├── --json 机读                 ← CI 解析用
│   └── exit code                   ← 0 合规 / 2 违规 / 64 用法错
├── 明确不管（Out）
│   ├── DESIGN.md 版本号一致性
│   ├── install.ps1 的 PowerShell 语法
│   ├── AGENTS.md 注入段唯一性
│   └── docs/ 与 .agents/skills/ 的同类问题
└── 相邻但不同源
    ├── cicd/check_ci_gates.py      ← 测试结果门禁（吃 junit），本需求不并入
    └── cicd/known_red.py           ← 退出码三态约定的来源（0/2/64）
```
