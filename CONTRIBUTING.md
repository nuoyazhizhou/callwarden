# 贡献指南

欢迎为 Call Warden 贡献代码！本指南帮助你快速参与项目。

## 项目结构

```
callwarden/
├── analyzers/        # 分析层：调用链 / 覆盖率 / 缺陷 / ignore 规则
├── cicd/             # CI/CD：SARIF / 增量 / PR 检查
├── cli/              # CLI 命令行入口
├── db/               # 数据库层：23 个 Mixin + schema
├── parsers/          # 16 种语言 tree-sitter 解析器
├── rust_ext/         # PyO3 Rust 扩展（性能加速）
├── server/           # MCP Server + 文件监控
├── tests/            # 测试套件
└── docs/             # 文档
```

## 开发环境

```bash
# 1. 克隆仓库
git clone https://github.com/nuoyazhizhou/callwarden.git
cd callwarden

# 2. 安装开发依赖
cw install --all

# 3. 验证安装
cw --version
```

## 代码规范

### 通用

- **注释语言**：中文（与项目现有风格保持一致）
- **注释方式**：手动添加，按函数调用链遍历，不使用脚本批量生成
- **路径分隔符**：内部使用正斜杠 `/` 标准化，消除跨平台差异
- **行尾**：文件内容哈希计算前需统一为 LF
- **最小侵入**：修改时精确影响分析，避免无关变更

### Python

- Python 3.10+
- 类型注解：所有公共方法必须标注参数与返回类型
- docstring：公共方法用 Google 风格中文 docstring
- 导入：标准库 → 第三方 → 本项目，每组内字母序

### 数据库变更

- **必须版本化迁移**：在 `db/db_base.py` 的 `_get_migrations()` 注册新版本
- **必须事务化**：迁移函数失败自动回滚
- **必须更新 SCHEMA_VERSION**：`db/schema.py` 顶部常量
- 大表字段变更需评估索引性能影响（参考 Lessons Learned）

### 新增语言解析器

1. 在 `parsers/` 下创建 `<lang>_parser.py`，继承 `BaseParser`
2. 在 `config.py` 的 `LANGUAGE_CONFIG` 注册语言配置
3. 在 `install.py` 添加 grammar 包安装组
4. 在 `requirements.txt` 添加 tree-sitter-<lang> 依赖
5. 在 `parsers/__init__.py` 和 `db_build.py` 工厂集成
6. 编写测试用例

### 新增 Mixin 模块

1. 在 `db/` 下创建 `db_<name>.py`，定义 `<Name>Mixin` 类
2. 在 `db/db.py` 的 `CodeGraphDB` 继承链中加入 Mixin
3. 如需新表：在 `schema.py` 添加 DDL + 在 `db_base.py` 注册迁移
4. MCP 工具：在 `server/mcp_server.py` 用 `@mcp.tool()` 注册
5. CLI 命令：在 `cli/main.py` 添加子命令分发
6. 编写测试

## 测试

```bash
# 全套测试
cd /path/to/callwarden
cw test test_p0_bugfixes
cw test test_p1_features
cw test test_p2_features
cw test test_p3_features
cw test test_csharp_ruby
cw test test_p1_p3_languages
cw test test_gc
cw test test_stress       # 10w 符号压力
cw test test_fuzz          # 安全 fuzz
```

提交前请确保所有测试通过，无回归。

## 提交流程

1. Fork 仓库并创建特性分支：`git checkout -b feature/your-feature`
2. 编写代码 + 测试，确保本地测试通过
3. 提交时使用清晰的 commit message：
   - `feat:` 新功能
   - `fix:` Bug 修复
   - `refactor:` 重构
   - `docs:` 文档
   - `test:` 测试
   - `perf:` 性能
4. 提交 PR，描述变更目的、影响范围、测试方式

## PR 检查清单

- [ ] 代码遵循项目规范（中文注释 / 类型注解 / 最小侵入）
- [ ] 数据库变更有版本化迁移
- [ ] 新功能有测试覆盖
- [ ] 所有测试通过，无回归
- [ ] 文档已更新（如涉及用户可见变更）
- [ ] commit message 清晰

## 文档维护

- **活文档**放 `docs/`：用户文档、架构、CLI/MCP 参考
- **设计文档**放 `docs/design/`：实现状态、竞品分析、架构规格
- **历史归档**放 `docs/history/`：已过时但有参考价值的文档，标注"历史"
- 不要删除历史文档，归档即可

## 安全相关

如发现安全漏洞，请**不要**公开 Issue，直接邮件联系维护者。
