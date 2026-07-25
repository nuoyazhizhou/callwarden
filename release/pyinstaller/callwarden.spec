# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller spec：把 cw / cw-client / cw-agent 打包成自包含的 --onedir 产物。

P0-3 整改（2026-07-22）：原 build_packages.sh 只复制 venv 的 console_scripts，
shebang 指向构建机临时 venv，安装后无法启动。改用 PyInstaller --onedir 打包，
产物含 Python 解释器 + 全部依赖 + Rust 扩展，安装后不依赖系统 Python。

构建命令（Linux/macOS）：
    cd <repo_root>
    pyinstaller release/pyinstaller/callwarden.spec --noconfirm --clean

构建命令（Windows）：
    cd <repo_root>
    pyinstaller release\pyinstaller\callwarden.spec --noconfirm --clean

产物：
    dist/callwarden/      --onedir 目录
      cw                  主入口
      cw-client           仅 Linux
      cw-agent            仅 Linux
      _internal/          所有入口共享的 Python 与原生依赖

所有入口共用同一个 Analysis、PYZ 和 COLLECT，最终目录只有一份运行时。
"""

import sys
import os

# === 路径常量 ===
# spec 文件位于 release/pyinstaller/callwarden.spec，项目根目录是上两级
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))
# pyproject.toml 将仓库根目录映射为 callwarden 包，因此源码导入需要仓库父目录。
PACKAGE_PARENT = os.path.dirname(ROOT)
ENTRY_DIR = os.path.join(SPEC_DIR)

block_cipher = None

# === Data files ===
# i18n/*.json（i18n.py 通过 __file__ 相对路径定位）
datas = [
    (os.path.join(ROOT, 'i18n', 'en_US.json'), 'callwarden/i18n'),
    (os.path.join(ROOT, 'i18n', 'zh_CN.json'), 'callwarden/i18n'),
]

# === Binaries (Rust 扩展 callwarden_core) ===
# 构建后的二进制在项目根目录（release/build.py 复制到这里）
if sys.platform == 'win32':
    rust_ext_name = 'callwarden_core.pyd'
else:
    # Linux: .so, macOS: .so（Python 扩展统一 .so 后缀）
    rust_ext_name = 'callwarden_core.so'

# 开发机上的根目录扩展可能正被 MCP/watcher 加载。允许发布验证从 Cargo
# staging 目录读取新构建，CI 和正式发布仍默认使用项目根目录。
rust_ext_path = os.environ.get(
    'CW_RUST_EXT_PATH',
    os.path.join(ROOT, rust_ext_name),
)
rust_ext_path = os.path.abspath(rust_ext_path)
binaries = []
if os.path.exists(rust_ext_path):
    binaries = [(rust_ext_path, '.')]
else:
    raise FileNotFoundError(
        f'发布构建要求 Rust 扩展存在: {rust_ext_path}\n'
        '请先运行 python release/build.py --rust'
    )

# === Hidden imports ===
hiddenimports = []

# 1. tree-sitter 核心 API
# Python parser 仍是解析失败和 CW_DISABLE_RUST_PARSE 场景的正式回退路径，
# PyInstaller 会从下面的语言 parser hidden imports 自动收集对应 grammar。
hiddenimports += [
    'tree_sitter',
]

# 2. callwarden.parsers 懒加载子模块（__init__.py 的 __getattr__ 用 importlib 动态加载）
hiddenimports += [
    'callwarden.parsers.base',
    'callwarden.parsers.module_resolver',
    'callwarden.parsers.call_resolver',
    'callwarden.parsers.rust',
    'callwarden.parsers.typescript',
    'callwarden.parsers.python_parser',
    'callwarden.parsers.kotlin_parser',
    'callwarden.parsers.go_parser',
    'callwarden.parsers.java_parser',
    'callwarden.parsers.c_parser',
    'callwarden.parsers.csharp_parser',
    'callwarden.parsers.ruby_parser',
    'callwarden.parsers.php_parser',
    'callwarden.parsers.swift_parser',
    'callwarden.parsers.scala_parser',
    'callwarden.parsers.hcl_parser',
    'callwarden.parsers.elixir_parser',
]

# 3. cw.py 动态分发的入口模块（importlib.import_module）
hiddenimports += [
    'callwarden.install',
    'callwarden.server.mcp_server',
    'callwarden.cli.main',
]
# 注：cw.py 还动态加载 callwarden.tests.{test_name}（cw test 命令），
# 但 tests 是开发期工具，打包产物中不收集。cw test 在打包后会 ImportError，
# 这是预期行为——生产环境用户不应运行测试。

# 4. Rust 扩展模块
hiddenimports += ['callwarden_core']

# 5. MCP stdio server 的目标化模块集合
# CallWarden 使用 mcp.server.fastmcp，不使用 fastmcp 顶层 CLI、云认证 provider、
# OpenAPI proxy 或实验 sampling。全量 collect_submodules 会拉入 AWS SDK 等无关依赖。
hiddenimports += [
    'mcp.server.fastmcp',
    'mcp.server.fastmcp.exceptions',
    'mcp.server.fastmcp.server',
    'mcp.server.fastmcp.prompts.base',
    'mcp.server.fastmcp.prompts.manager',
    'mcp.server.fastmcp.resources.base',
    'mcp.server.fastmcp.resources.resource_manager',
    'mcp.server.fastmcp.resources.templates',
    'mcp.server.fastmcp.resources.types',
    'mcp.server.fastmcp.tools.base',
    'mcp.server.fastmcp.tools.tool_manager',
    'mcp.server.fastmcp.utilities.context_injection',
    'mcp.server.fastmcp.utilities.func_metadata',
    'mcp.server.fastmcp.utilities.logging',
    'mcp.server.fastmcp.utilities.types',
    'mcp.server.stdio',
    'mcp.shared.exceptions',
    'mcp.types',
]

# 6. 其他运行时依赖
hiddenimports += ['pydantic', 'pydantic_core', 'watchdog', 'pathspec']

# === Excludes（减小体积）===
excludes = [
    # --- 未使用的间接依赖 ---
    'tree_sitter_languages',   # CW 直接 import 各语言 grammar，不通过此聚合包
    'fastmcp',                 # 未使用的新版 CLI/provider/experimental 聚合包
    'boto3', 'botocore', 's3transfer',
    'opentelemetry', 'opentelemetry_api', 'opentelemetry_sdk',
    'dns', 'email_validator',  # CallWarden 不使用 Pydantic 的 EmailStr/network extra

    # --- 可选依赖：向量搜索（PyTorch 全家桶 ~2GB）---
    'torch', 'torchvision', 'torchaudio',
    'sentence_transformers', 'sentence-transformers',
    'transformers', 'tokenizers', 'safetensors',
    'huggingface_hub', 'huggingface-hub',

    # --- 可选依赖：sqlite-vec ---
    'sqlite_vec', 'sqlite-vec',

    # --- 外置能力：冻结包通过 PATH 调用 semgrep 可执行文件 ---
    'semgrep',

    # --- 开发/测试工具（生产环境不需要）---
    'pytest', 'pytest_asyncio', 'pytest_xdist', 'pytest_timeout',
    '_pytest', 'pluggy', 'iniconfig',
    'setuptools', 'setuptools_scm',
    'pip', 'wheel', 'distutils',
    'build', 'twine', 'keyring',

    # --- GUI 库（CW 是 CLI/MCP 工具）---
    'tkinter', '_tkinter', 'tk', 'turtle', 'idlelib',

    # --- 未使用的标准库模块 ---
    'unittest', 'unittest2', 'doctest',
    'lib2to3', 'ensurepip', 'venv',
    'pydoc', 'pdb', 'bdb',
    'profile', 'cProfile', 'pstats',
    'xmlrpc', 'xmlrpc.server', 'xmlrpc.client',
    'mailbox',
    'ftplib', 'poplib', 'imaplib', 'nntplib',
    'curses', 'readline',
    'test', 'test.support',

    # --- 其他不需要的包 ---
    'IPython', 'jupyter', 'notebook',
    'matplotlib', 'scipy', 'pandas',
    'PIL', 'Pillow',
    'Crypto', 'cryptography',
    'lxml',
]

# 注意：semgrep 的 Python/OCaml 运行时不嵌入冻结包。需要安全扫描时，用户单独安装
# semgrep 可执行文件；CallWarden 通过 shutil.which("semgrep") 调用它。

# === Analysis ===
# 三个入口共用同一个 Analysis，PyInstaller 会自动收集依赖
# P0-3.6 修复（2026-07-22）：cw-agent 是 Linux-only 角色（systemd user unit），
# Windows 不需要，跳过构建以节省时间和磁盘空间
_entry_scripts = [os.path.join(ENTRY_DIR, 'entry_cw.py')]
if sys.platform.startswith('linux'):
    _entry_scripts.extend([
        os.path.join(ENTRY_DIR, 'entry_cw_client.py'),
        os.path.join(ENTRY_DIR, 'entry_cw_agent.py'),
    ])

a = Analysis(
    _entry_scripts,
    pathex=[PACKAGE_PARENT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
)

# PyInstaller 对 hidden import 缺失默认只发 warning，发布构建必须 fail closed。
_collected_modules = {item[0] for item in a.pure}
_required_modules = {
    'callwarden',
    'callwarden.cw',
    'callwarden.parsers.base',
    'callwarden.server.mcp_server',
}
if sys.platform.startswith('linux'):
    _required_modules.update({
        'callwarden.cli.client',
        'callwarden.cli.agent',
    })
_missing_modules = sorted(_required_modules - _collected_modules)
if _missing_modules:
    raise RuntimeError(
        'PyInstaller 未收集 CallWarden 核心模块: '
        + ', '.join(_missing_modules)
    )

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# === 按名称提取入口脚本（PyInstaller 6.x 会在 scripts 中混入 rthook，不能按固定下标取）===
_entry_toc = [s for s in a.scripts if s[0].startswith('entry_')]
_expected_entry_count = 3 if sys.platform.startswith('linux') else 1
assert len(_entry_toc) == _expected_entry_count, (
    f'期望 {_expected_entry_count} 个入口脚本，实际: {_entry_toc}'
)
_scripts_cw = [s for s in _entry_toc if s[0] == 'entry_cw']
_scripts_client = [s for s in _entry_toc if s[0] == 'entry_cw_client']
_scripts_agent = [s for s in _entry_toc if s[0] == 'entry_cw_agent']

# === EXE + 单一 COLLECT ===
# 每个 EXE 只包含自己的入口脚本，所有 EXE 共享同一份运行时依赖。

# cw 主入口
exe_cw = EXE(
    pyz,
    _scripts_cw,
    exclude_binaries=True,
    name='cw',
    console=True,
    cipher=block_cipher,
)
_executables = [exe_cw]

# 企业 client/agent 依赖 Linux UDS、SO_PEERCRED 和 SCM_RIGHTS。
if sys.platform.startswith('linux'):
    exe_cw_client = EXE(
        pyz,
        _scripts_client,
        exclude_binaries=True,
        name='cw-client',
        console=True,
        cipher=block_cipher,
    )
    _executables.append(exe_cw_client)

    exe_cw_agent = EXE(
        pyz,
        _scripts_agent,
        exclude_binaries=True,
        name='cw-agent',
        console=True,
        cipher=block_cipher,
    )
    _executables.append(exe_cw_agent)

bundle = COLLECT(
    *_executables,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='callwarden',
)
