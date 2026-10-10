# 构建环境要求（CallWarden 测试前置）

> 本文件是 `TESTING_PLAN.md` 的**构建前置配套文档**。
> 收敛测试套件（T1–T5 / M1–M4）需要预先构建出 `cw-daemon` 二进制；
> 本机（Windows）无 MSVC、无完整 MinGW，最终用 **Rust GNU target + Zig 转发器**打通。
> 其他机器/CI 只需保证「能 `cargo build --bin cw-daemon` 出二进制」即可，具体工具链选型可不同。

## 一、最终架构（本机已验证）

| 组件 | 版本 | 路径 |
|---|---|---|
| rustc / cargo | 1.99.0 | `~/.rustup/toolchains/stable-x86_64-pc-windows-gnu/bin` |
| Rust target | `x86_64-pc-windows-gnu` | — |
| 链接器 | `rust-lld`（Rust 自带） | toolchain `lib/rustlib/.../bin/gcc-ld/ld.lld.exe` |
| C 编译器 | **Zig 0.16.0**（作 clang 前端） | `...\envs\rusttool\Lib\site-packages\ziglang\zig.exe` |
| cc / ar 转发器 | `cc-zig.exe` / `ar-zig.exe` | `~/.cargo/bin/`（源码 `rustenv/cc_zig.rs`、`ar_zig.rs`） |
| as / ar / ranlib 转发器 | `as.exe` / `ar.exe` / `ranlib.exe` | toolchain `.../bin/self-contained/`（供 `dlltool` 生成系统 DLL 导入库） |

**为什么不用 MSVC**：本机无 MSVC v14+ 链接器且非管理员，装不了 VS Build Tools。GNU + Rust 自带 `rust-lld` 可完成链接。

**为什么需要 Zig**：`rust_ext` 依赖 tree-sitter 的 16 个语言 grammar（C 源码），必须经 `cc` crate 调真实 C 编译器；GNU toolchain 自带文件只是链接薄封装，编不了 C。

## 二、关键环境变量与 cargo 配置

```
Path                   += toolchain bin ; ~/.cargo/bin
CARGO_HOME              = ~/.cargo
RUSTUP_HOME             = ~/.rustup
CC_x86_64_pc_windows_gnu = ~/.cargo/bin/cc-zig.exe
AR_x86_64_pc_windows_gnu = ~/.cargo/bin/ar-zig.exe
```

`~/.cargo/config.toml`：

```toml
[target.x86_64-pc-windows-gnu]
linker = "rust-lld"

[env]
CC_x86_64_pc_windows_gnu = "C:\\Users\\wiimu\\.cargo\\bin\\cc-zig.exe"
AR_x86_64_pc_windows_gnu = "C:\\Users\\wiimu\\.cargo\\bin\\ar-zig.exe"
```

## 三、踩过的坑（均已解决，供排错参考）

1. **rustup shim 0 字节**：Windows 上 rustup 用硬链接建 shim，被沙盒拦截 → 改为 PATH 直指 toolchain 真实二进制。
2. **Git Bash `link.exe` 陷阱**：同名 coreutils `link` 被当成 MSVC 链接器 → 用绝对路径指定链接器。
3. **GitHub 不可达**：Zig 改走 PyPI（`pip install ziglang`）。
4. **Rust triple ≠ Zig triple**：cc crate 传 `x86_64-pc-windows-gnu`，Zig 只认 `x86_64-windows-gnu` → 转发器做 vendor 段剥离。
5. **Zig 默认开 UBSan 插桩** → 链接 undefined symbol `__ubsan_*` → 转发器追加 `-fno-sanitize=undefined`。
6. **GNU 工具链缺 `as`/`ar`/`ranlib`**：`dlltool` 给 `ntdll.dll` 等系统 DLL 现生成导入库时 spawn 不到 `as`/`ar` → 在 `self-contained` 放 Zig 转发器。
7. **Role Prompt 构建门禁 fail-closed**：`build.rs` 对 `resources/role_prompts/v1/*` 做 SHA-256 + LF 校验，资产被 CRLF 化会 panic → 归一化为 LF（内容不变）即放行。**建议加 `.gitattributes` 锁 `*.md text eol=lf` 防复发。**

## 四、构建命令（完整，非沙盒执行）

> Zig 相关操作与 `dlltool` 需跳出沙盒（沙盒会拦截子进程 spawn）。

```bash
export PATH="/c/Users/wiimu/.rustup/toolchains/stable-x86_64-pc-windows-gnu/bin:\
/c/Users/wiimu/.cargo/bin:\
/c/Users/wiimu/.workbuddy/binaries/python/envs/rusttool/Lib/site-packages/ziglang:\
/c/Users/wiimu/.rustup/toolchains/stable-x86_64-pc-windows-gnu/lib/rustlib/x86_64-pc-windows-gnu/bin/self-contained:$PATH"
cd /e/git_work/callwarden/rust_ext
cargo build --bin cw-daemon      # 产物 target/debug/cw-daemon.exe（收敛套件优先用 target/release）
cargo build --lib                # 产物 target/debug/callwarden_core.dll（Python 扩展 .pyd）
```

## 五、验证

```bash
cargo --version   # 1.99.0
target/debug/cw-daemon.exe --help   # 输出 serve / schema-check / health-check
```
