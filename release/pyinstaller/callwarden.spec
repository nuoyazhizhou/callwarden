# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 cw / cw-client / cw-agent 打包成自包含的 --onedir 产物。

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
    dist/cw/              --onedir 目录（cw 主入口）
    dist/cw-client/       --onedir 目录（cw-client）
    dist/cw-agent/        --onedir 目录（cw-agent）

注意：三个入口共用同一个 Analysis（依赖收集一次），分别 EXE + COLLECT。
PyInstaller 会自动去重共享的 .so/.pyd，但 --onedir 模式下每个目录独立。
deb/rpm 打包时建议把三个目录合并到 /usr/lib/callwarden/runtime/ 并创建软链接。
"""

import sys
import os
from PyInstaller.utils.hooks import collect_submodules

# === 路径常量 ===
# spec 文件位于 release/pyinstaller/callwarden.spec，项目根目录是上两级
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))
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

rust_ext_path = os.path.join(ROOT, rust_ext_name)
binaries = []
if os.path.exists(rust_ext_path):
    binaries = [(rust_ext_path, '.')]
else:
    print(f'WARNING: Rust 扩展 {rust_ext_name} 不存在于 {rust_ext_path}')
    print('  请先运行 cargo build --release（rust_ext/）')
    print('  callwarden 会在运行时降级到纯 Python，但性能会显著下降')

# === Hidden imports ===
hiddenimports = []

# 1. tree-sitter 核心 API（不含 grammar 二进制）
# 注意：16 种语言的 tree-sitter grammar 已由 callwarden_core（Rust 扩展）静态链接。
# 打包版本通过 Rust 路径加载 grammar，排除 Python grammar 包可节省约 300MB。
# 开发环境（pip install）仍使用 Python grammar 包作为 Rust 扩展未安装时的回退。
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

# 5. fastmcp / mcp SDK（动态子模块收集）
# pip 包名 fastmcp，实际导入路径 mcp.server.fastmcp
try:
    hiddenimports += collect_submodules('mcp')
except Exception:
    hiddenimports += [
        'mcp', 'mcp.server', 'mcp.server.fastmcp',
        'mcp.server.stdio', 'mcp.server.sse',
        'mcp.types', 'mcp.shared', 'mcp.shared.exceptions',
    ]

try:
    hiddenimports += collect_submodules('fastmcp')
except Exception:
    pass

# 6. 其他运行时依赖
hiddenimports += ['pydantic', 'pydantic_core', 'watchdog', 'pathspec']

# === Excludes（减小体积）===
excludes = [
    # --- Python tree-sitter grammar 包（Rust 扩展已静态链接，打包时冗余）---
    'tree_sitter_rust', 'tree_sitter_python', 'tree_sitter_typescript',
    'tree_sitter_kotlin', 'tree_sitter_go', 'tree_sitter_java',
    'tree_sitter_c', 'tree_sitter_cpp', 'tree_sitter_c_sharp',
    'tree_sitter_ruby', 'tree_sitter_php', 'tree_sitter_swift',
    'tree_sitter_scala', 'tree_sitter_hcl', 'tree_sitter_elixir',

    # --- 未使用的间接依赖 ---
    'tree_sitter_languages',   # CW 直接 import 各语言 grammar，不通过此聚合包

    # --- 可选依赖：向量搜索（PyTorch 全家桶 ~2GB）---
    'torch', 'torchvision', 'torchaudio',
    'sentence_transformers', 'sentence-transformers',
    'transformers', 'tokenizers', 'safetensors',
    'huggingface_hub', 'huggingface-hub',

    # --- 可选依赖：sqlite-vec ---
    'sqlite_vec', 'sqlite-vec',

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
    'mailbox', 'mimetypes',
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

# 注意：semgrep 未排除，因为它是 CW 的核心功能（安全扫描）。
# 如果 PyInstaller 打包 semgrep 失败（OCaml 引擎兼容性问题），
# 可在此添加 'semgrep' 到 excludes，用户需要单独 pip install semgrep。

# === Analysis ===
# 三个入口共用同一个 Analysis，PyInstaller 会自动收集依赖
# P0-3.6 修复（2026-07-22）：cw-agent 是 Linux-only 角色（systemd user unit），
# Windows 不需要，跳过构建以节省时间和磁盘空间
_entry_scripts = [
    os.path.join(ENTRY_DIR, 'entry_cw.py'),
    os.path.join(ENTRY_DIR, 'entry_cw_client.py'),
]
if sys.platform != 'win32':
    _entry_scripts.append(os.path.join(ENTRY_DIR, 'entry_cw_agent.py'))

a = Analysis(
    _entry_scripts,
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# === 按名称提取入口脚本（PyInstaller 6.x 会在 scripts 中混入 rthook，不能按固定下标取）===
_entry_toc = [s for s in a.scripts if s[0].startswith('entry_')]
assert len(_entry_toc) >= 2, f'期望至少 2 个入口脚本，实际: {_entry_toc}'
_scripts_cw = [s for s in _entry_toc if s[0] == 'entry_cw']
_scripts_client = [s for s in _entry_toc if s[0] == 'entry_cw_client']
_scripts_agent = [s for s in _entry_toc if s[0] == 'entry_cw_agent']

# === EXE + COLLECT ===
# 多入口模式：每个 EXE 只包含自己的入口脚本（rthook 由 PyInstaller 自动注入 PKG）

# cw 主入口
exe_cw = EXE(
    pyz,
    _scripts_cw,
    exclude_binaries=True,
    name='cw',
    console=True,
    cipher=block_cipher,
)
coll_cw = COLLECT(
    exe_cw,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='cw',
)

# cw-client
exe_cw_client = EXE(
    pyz,
    _scripts_client,
    exclude_binaries=True,
    name='cw-client',
    console=True,
    cipher=block_cipher,
)
coll_cw_client = COLLECT(
    exe_cw_client,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='cw-client',
)

# cw-agent（仅 Linux/macOS 构建）
if sys.platform != 'win32' and _scripts_agent:
    exe_cw_agent = EXE(
        pyz,
        _scripts_agent,
        exclude_binaries=True,
        name='cw-agent',
        console=True,
        cipher=block_cipher,
    )
    coll_cw_agent = COLLECT(
        exe_cw_agent,
        a.binaries,
        a.zipfiles,
        a.datas,
        name='cw-agent',
    )
