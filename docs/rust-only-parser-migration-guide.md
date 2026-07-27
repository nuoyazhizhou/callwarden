# Call Warden Rust-only Parser 迁移与回滚指南

> 版本：0.3.3+（Rust-only parser 生产切换）
> 日期：2026-07-25
> 关联设计：[rust-only-parser-cutover-plan.md](design/rust-only-parser-cutover-plan.md) §9 回滚策略
> 适用对象：从 0.3.2 及之前版本升级到 0.3.3+ 的用户

## 1. 迁移前检查

### 1.1 确认当前版本

```bash
cw --version
# 期望输出：Call Warden 0.3.2 或更早版本
```

### 1.2 备份工作空间数据

虽然 Rust-only parser 切换**不改变 workspace schema**（设计 §9.3），但建议升级前备份：

```bash
# 备份用户级单库
cp ~/.callwarden/callwarden.db ~/.callwarden/callwarden.db.bak.$(date +%Y%m%d)

# 或用 cw 自带备份（如果可用）
cw gc db-migrate-single --apply  # 旧版多库迁移到单库（如适用）
```

### 1.3 检查工作空间状态

```bash
# 列出所有工作空间
cw workspace list

# 检查每个工作空间的 snapshot 状态
cw workspace status <workspace_id>
```

## 2. 升级流程

### 2.1 下载新版本

从 GitHub Release 下载 0.3.3+ 对应平台的压缩包：

```bash
# Linux x86_64
wget https://github.com/nuoyazhizhou/callwarden/releases/download/v0.3.14/callwarden-linux-amd64.tar.gz

# macOS arm64
wget https://github.com/callwarden/callwarden/releases/download/v0.3.3/callwarden-macos-arm64.tar.gz

# Windows amd64
curl -L -o callwarden-windows-amd64.zip https://github.com/callwarden/callwarden/releases/download/v0.3.3/callwarden-windows-amd64.zip
```

### 2.2 替换安装目录

```bash
# Linux/macOS
mv callwarden callwarden.old  # 备份旧版本
tar xzf callwarden-linux-amd64.tar.gz
./callwarden/cw --version  # 验证新版本

# Windows
Rename-Item callwarden callwarden.old
Expand-Archive callwarden-windows-amd64.zip
.\callwarden\cw.exe --version
```

### 2.3 验证升级

```bash
# 1. 验证 frozen cw 可启动
cw --version
cw --help
cw server --check-imports

# 2. 验证 16 语言 Rust parser 可用
cw doctor  # 检查 Rust grammar/ABI 自检

# 3. 验证现有工作空间可读取
cw workspace list
cw --refresh-all <existing-workspace>  # 增量刷新

# 4. 验证 schema 版本
cw schema version  # 应与升级前一致（设计 §9.3 不改变 schema）
```

### 2.4 重新激活 watcher（如适用）

```bash
# 启动 watcher
cw watcher start <workspace>

# 验证 save-to-query
echo "def new_fn(): pass" >> <workspace>/main.py
cw query new_fn <workspace>/main.py
```

## 3. 回滚流程

### 3.1 触发回滚的条件

以下情况应考虑回滚：

- Rust parser 在生产工作空间产生系统性漏符号
- `rust-strict` 模式下频繁 `failed` 状态导致工作空间不可用
- 单语言 gate 在 canary 阶段失败
- daemon 崩溃或 watcher 异常

### 3.2 回滚步骤（首选：回滚到上一正式安装包）

设计 §9.2 回滚优先级 1：回滚到上一正式安装包。

```bash
# 1. 停止当前版本 watcher / daemon
cw watcher stop <workspace>
cw server --stop  # 如有 daemon 运行

# 2. 恢复上一版本安装目录
rm -rf callwarden
mv callwarden.old callwarden  # 或重新解压 0.3.2 压缩包

# 3. 验证旧版本可启动
./callwarden/cw --version  # 应为 0.3.2

# 4. 验证旧版本可读取现有 snapshot
./callwarden/cw workspace list
./callwarden/cw query <symbol> <file>  # 应能查到旧 snapshot 数据

# 5. 重新启动 watcher
./callwarden/cw watcher start <workspace>
```

旧版本 cw 能读取上一版本创建的 snapshot（设计 §9.3 数据兼容）。

### 3.3 回滚步骤（备选：保留 snapshot + 关闭自动 refresh）

设计 §9.2 回滚优先级 2：关闭受影响 workspace 的自动 refresh，保留上一 snapshot。

```bash
# 1. 停止 watcher（保留 snapshot 不动）
cw watcher stop <workspace>

# 2. snapshot 仍可查询（只读模式）
cw query <symbol> <file>

# 3. 等待修复版本发布，或回滚到上一版本（见 3.2）
```

### 3.4 回滚步骤（极端：独立 parser-compat 包）

设计 §9.2 回滚优先级 4：极端情况下发布独立 `parser-compat` 包。

仅在以下情况使用：
- 上一版本安装包不可用
- Rust parser 缺陷影响所有工作空间
- 无法立即发布修复版本

`parser-compat` 包含 Python grammar，作为临时过渡，用户手动安装：

```bash
# 下载 parser-compat 包（如发布）
wget https://github.com/callwarden/callwarden/releases/download/v0.3.3/callwarden-parser-compat-linux-x86_64.tar.gz

# 解压到 callwarden 安装目录的 _internal/
tar xzf callwarden-parser-compat-linux-x86_64.tar.gz -C callwarden/_internal/

# 设置环境变量启用 Python fallback
export CW_PARSE_MODE=python-reference  # 临时过渡，仅用于回滚
```

**警告**：`parser-compat` 包仅作为最后手段，不应长期使用。一旦 Rust parser
修复版本发布，应立即移除 `parser-compat` 并升级到修复版本。

### 3.5 禁止的回滚行为

设计 §9.2 明确禁止：

> 禁止在同一生产进程中临时下载 Python grammar 并静默恢复，因为这会破坏离线部署、
> SBOM、签名和可复现性。

具体禁止：
- ❌ 在 frozen cw 运行时自动从网络下载 Python grammar
- ❌ 通过环境变量静默启用 Python fallback 而不告知用户
- ❌ 在 schema 不兼容时静默覆盖旧 snapshot
- ❌ dirty overlay 在重建或回滚期间进入 Global CAS

## 4. 数据兼容性

### 4.1 schema 兼容

设计 §9.3：
- 本计划原则上不改变 workspace schema
- parser ABI 或 CAS key 变化时必须提升 parser ABI
- 新 ABI 产物不能覆盖旧 ABI CAS entry

当前 ABI 版本（`release/version.toml`）：

```toml
[abi]
python = "cp311"
parser = 2
snapshot = 2
schema_registry = 3
schema_cas = 2
schema_workspace = 4
```

### 4.2 CAS 兼容

- 相同 canonical bytes 的文件跨版本共享 CAS entry（Global CAS）
- parser ABI 变化时，新 ABI 产物写入新 CAS entry，不覆盖旧 ABI entry
- 回滚版本应能读取旧 snapshot；不能读取时从源文件重建

### 4.3 dirty overlay 处理

设计 §9.3：
- dirty overlay 在重建或回滚期间不得进入 Global CAS
- 重建时应使用 clean checkout 的 canonical bytes
- 回滚时应保留上一 snapshot 的 CAS entry 不被覆盖

## 5. 故障排查

### 5.1 Rust parser 失败

```bash
# 查看 parser diagnostics
cw doctor
cw parse diagnostics <file>

# 查看 generation 状态
cw generation status <workspace>
```

如果 Rust parser 持续 `failed`：
1. 检查文件编码（UTF-8/BOM/CRLF）
2. 检查文件大小（超大文件可能 OOM）
3. 检查 Rust 扩展是否正确加载（`cw doctor`）
4. 回滚到上一版本（见 §3.2）

### 5.2 frozen cw 启动失败

```bash
# 验证 Rust 扩展存在
ls callwarden/_internal/callwarden_core.*  # .pyd / .so

# 验证无系统 Python 依赖
PYTHONHOME= PYTHONPATH= callwarden/cw --version

# 查看 server check-imports
callwarden/cw server --check-imports
```

### 5.3 watcher 不工作

```bash
# 检查 watcher 状态
cw watcher status <workspace>

# 重启 watcher
cw watcher stop <workspace>
cw watcher start <workspace>

# 验证 save-to-query
echo "def test_fn(): pass" >> <workspace>/test.py
sleep 2  # 等待 debounce
cw query test_fn <workspace>/test.py
```

## 6. 联系与反馈

- GitHub Issues：https://github.com/callwarden/callwarden/issues
- 文档：https://docs.callwarden.dev
- 邮件：support@callwarden.dev

如遇 Rust-only parser 相关的阻塞性问题，请在 Issue 标题前加 `[rust-only-parser]`。
