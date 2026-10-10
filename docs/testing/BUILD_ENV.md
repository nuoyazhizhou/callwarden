# 构建环境与测试前置（v7 配套）

> 源码配置核验及待实施命令模板。本轮没有构建、安装或运行测试。

## 1. 源码配置

rust_ext/Cargo.toml默认extension-module，Python扩展使用默认feature；依赖callwarden_core的daemon/test binary需--no-default-features关闭该feature，使PyO3链接libpython。现有Linux E2E和Rust CI均有此用法。关闭feature不等于Python-free，解释器/libpython必须匹配，不能补flag后就宣称构建完成。

Windows按AGENTS规则42用C:\Python314\python.exe并设置PYTHON/PYO3_PYTHON；Linux/macOS使用本平台冻结解释器，不混用Windows.pyd/target。release/build.py --rust构建扩展，不能代替构建daemon。

本机.tokenslim-context误检测为Node/npm。正式实施前核对并修正探测/允许命令；下列仅候选模板，未执行。CI是否部署TokenSlim及native shell须在profile明确。

## 2. Windows未来实施模板

```powershell
$env:PYTHON = 'C:\Python314\python.exe'
$env:PYO3_PYTHON = 'C:\Python314\python.exe'
tokenslim run C:\Python314\python.exe release/build.py --rust
tokenslim run cargo build --release --manifest-path rust_ext/Cargo.toml --no-default-features --bin cw-daemon
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features
# selector及报告路径在正式合同冻结；占位不能直接执行。
tokenslim run C:\Python314\python.exe -m pytest <frozen-selectors> --junitxml=<isolated-report>
```

## 3. Linux/macOS未来实施模板

```sh
# 隔离venv中选定冻结解释器，PYO3_PYTHON设其绝对路径。
tokenslim run python3 release/build.py --rust
tokenslim run cargo build --release --manifest-path rust_ext/Cargo.toml --no-default-features --bin cw-daemon
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml --no-default-features
tokenslim run python3 -m pytest <frozen-selectors> --junitxml=<isolated-report>
```

cargo test默认命令不保证全部适用suite可并行完成。src/integration/bin、平台cfg、singleton敏感和ignored测试形成显式清单，必要时隔离/串行；不把--lib daemon::当全量，也不临时--skip吞交付义务。

## 4. 前置manifest与验收

profile固定OS/架构、Python/ABI、Rust toolchain/target、Cargo.lock、C编译器/链接器、依赖/SDK、扩展/binary路径及hash、source revision、外部工具/规则/模型、corpus摘要。grammar需要真实C工具链，未来实测可用性，不凭PATH推定。

干净安装核验entrypoint实际import的扩展来源/hash；源码根、site-packages和运行daemon分别核验。PID/executable与构建hash一致，health/ping不是来源证明。空目录正式初始化，不SQL补表；缺必需前置FAIL/ERROR，不skip。

隔离HOME/USERPROFILE/manifest/endpoint；不使用生产daemon/真实库。安装/升级/恢复在可回收venv/VM/容器/目录及准备好的离线依赖资产中执行。Role Prompt LF/hash门禁保留，不绕过build.rs；check_ci_gates/check_skip_rate尚未实现，不以模板宣称落地。

## 5. 历史环境

v6的wiimu、/e/git_work、Rust1.99/Zig0.16、GNU转发器和“无MSVC”为其他会话历史描述，本轮未核验，不能代表当前wanpi环境或跨平台要求。原件/hash见.backups/20261010-153813-before-v7；复用前另核验，不直接修改全局cargo配置或索要沙箱豁免。
