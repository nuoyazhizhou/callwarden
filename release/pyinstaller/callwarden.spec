# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller spec：把 cw / cw-client / cw-agent 打包成自包含的 --onedir 产物。

P0-B 拆分（2026-07-25）：原 spec 三个入口共享同一 Analysis，导致 cw-client/cw-agent
也携带 parser/grammar。现拆为两个独立 Analysis + 两个 COLLECT：

  - ``dist/callwarden/``        local runtime（含 parser），入口 cw
  - ``dist/callwarden-client/`` client/agent runtime（无 parser），入口
    cw-client + cw-agent（仅 Linux）

client/agent bundle 严禁收集 ``callwarden.parsers.*``、``tree_sitter``、
``tree_sitter_*``、``numpy`` 以及 ``callwarden.db`` 中除 ``db_daemon`` 外的子模块。
通过 excludes + 入口绕过 ``cli.main`` 实现：
``entry_cw_client.py`` / ``entry_cw_agent.py`` 直接调用
``callwarden.cli.daemon_commands`` / ``callwarden.server.agent_watcher``，
不经过 ``cli.main`` 的顶层 ``db``/``parser`` import。同时这两个入口在
``import callwarden`` 之前用 ``sys.modules`` stub 注入 ``callwarden`` 和
``callwarden.db`` 包，跳过 ``callwarden/__init__.py`` 的
``from .db import CodeGraphDB`` 和 ``callwarden/db/__init__.py`` 的同名 import，
避免拉入 ``db.py → db_build.py → parsers → tree_sitter``。

构建命令（Linux/macOS）：
    cd <repo_root>
    pyinstaller release/pyinstaller/callwarden.spec --noconfirm --clean

构建命令（Windows）：
    cd <repo_root>
    pyinstaller release\pyinstaller\callwarden.spec --noconfirm --clean

产物：
    dist/callwarden/           local bundle（所有平台）
      cw                       主入口（含 parser）
      _internal/               Python + Rust 扩展 + parser + grammar
    dist/callwarden-client/    client/agent bundle（仅 Linux）
      cw-client                RPC client（无 parser）
      cw-agent                 watcher agent（无 parser）
      _internal/               Python + Rust 扩展（无 parser/grammar/numpy）
"""

import sys
import os

# === 路径常量 ===
# spec 文件位于 release/pyinstaller/callwarden.spec，项目根目录是上两级
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))
# pyproject.toml 将仓库根目录映射为 callwarden 包，因此源码导入需要仓库父目录。
PACKAGE_PARENT = os.path.dirname(ROOT)
ENTRY_DIR = SPEC_DIR

block_cipher = None

# === Data files（两个 bundle 都需要 i18n）===
# i18n/*.json（i18n.py 通过 __file__ 相对路径定位）
datas = [
    (os.path.join(ROOT, 'i18n', 'en_US.json'), 'callwarden/i18n'),
    (os.path.join(ROOT, 'i18n', 'zh_CN.json'), 'callwarden/i18n'),
]

# === Binaries: Rust 扩展 callwarden_core（两个 bundle 都需要）===
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
if not os.path.exists(rust_ext_path):
    raise FileNotFoundError(
        f'发布构建要求 Rust 扩展存在: {rust_ext_path}\n'
        '请先运行 python release/build.py --rust'
    )
binaries = [(rust_ext_path, '.')]

# === 共享 excludes（local 和 client/agent 都用）===
_common_excludes = [
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

# === local runtime hiddenimports（含 parser）===
_local_hiddenimports = [
    # 1. tree-sitter 核心 API
    # Python parser 仍是解析失败和 CW_DISABLE_RUST_PARSE 场景的正式回退路径，
    # PyInstaller 会从下面的语言 parser hidden imports 自动收集对应 grammar。
    'tree_sitter',

    # 2. callwarden.parsers 懒加载子模块（__init__.py 的 __getattr__ 用 importlib 动态加载）
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

    # 3. cw.py 动态分发的入口模块（importlib.import_module）
    'callwarden.install',
    'callwarden.server.mcp_server',
    'callwarden.cli.main',
    # 注：cw.py 还动态加载 callwarden.tests.{test_name}（cw test 命令），
    # 但 tests 是开发期工具，打包产物中不收集。cw test 在打包后会 ImportError，
    # 这是预期行为——生产环境用户不应运行测试。

    # 4. Rust 扩展模块
    'callwarden_core',

    # 5. MCP stdio server 的目标化模块集合
    # CallWarden 使用 mcp.server.fastmcp，不使用 fastmcp 顶层 CLI、云认证 provider、
    # OpenAPI proxy 或实验 sampling。全量 collect_submodules 会拉入 AWS SDK 等无关依赖。
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

    # 6. 其他运行时依赖
    'pydantic', 'pydantic_core', 'watchdog', 'pathspec', 'numpy',
]

# === client/agent runtime hiddenimports（无 parser，仅 Linux）===
# 直接走 daemon_commands / agent_watcher，绕过 cli.main（避免拉入 db → parsers）
_client_agent_hiddenimports = [
    # daemon RPC 链路（cw-client 入口）
    'callwarden.cli.daemon_commands',
    'callwarden.server.daemon_client',
    'callwarden.server.daemon_server',
    'callwarden.server.daemon_protocol',
    'callwarden.server.snapshot_manager',
    'callwarden.server.query_budget',
    'callwarden.server.metrics',
    'callwarden.server.replicator',

    # agent watcher 链路（cw-agent 入口）
    'callwarden.server.agent_session',
    'callwarden.server.agent_protocol',
    'callwarden.server.agent_watcher',
    'callwarden.server.watcher',

    # daemon registry DB（独立模块，不拉入 db_build）
    # callwarden.db 包通过 excludes 跳过 __init__.py，db_daemon 由 hiddenimports 显式收集
    'callwarden.db.db_daemon',

    # 共享基础
    'callwarden.config',
    'callwarden.i18n',
    'callwarden.cli.console',

    # Rust 扩展（canonicalize_source_py / PySnapshotCache 等，不含 Python parser）
    'callwarden_core',

    # 运行时依赖（无 numpy）
    'pydantic', 'pydantic_core', 'watchdog', 'pathspec',
]

# === client/agent 严禁收集的 parser 模块 ===
_PARSER_EXCLUDES = [
    # callwarden.parsers.* 全部（含 base/module_resolver/call_resolver/call_filter）
    'callwarden.parsers',
    'callwarden.parsers.base',
    'callwarden.parsers.module_resolver',
    'callwarden.parsers.call_resolver',
    'callwarden.parsers.call_filter',
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

    # tree-sitter Python 核心
    'tree_sitter',
    # 16 种 tree-sitter grammar wheels
    'tree_sitter_rust',
    'tree_sitter_typescript',
    'tree_sitter_python',
    'tree_sitter_kotlin',
    'tree_sitter_go',
    'tree_sitter_java',
    'tree_sitter_c',
    'tree_sitter_cpp',
    'tree_sitter_javascript',
    'tree_sitter_c_sharp',
    'tree_sitter_ruby',
    'tree_sitter_php',
    'tree_sitter_swift',
    'tree_sitter_scala',
    'tree_sitter_hcl',
    'tree_sitter_elixir',

    # client/agent 不做本地解析，不需要 numpy
    'numpy',
]

# === client/agent 严禁收集的 local DB 模块（除 db_daemon）===
# callwarden.db 整包 exclude；db_daemon 通过 hiddenimports 显式收集。
# entry_cw_client.py / entry_cw_agent.py 在 import 前用 sys.modules stub
# callwarden.db 包，跳过 db/__init__.py 的 `from .db import CodeGraphDB`，
# 避免 db.py → db_build.py → parsers 拉入链。
_LOCAL_DB_EXCLUDES = [
    'callwarden.db',
    'callwarden.db.db',
    'callwarden.db.db_base',
    'callwarden.db.db_build',
    'callwarden.db.db_query',
    'callwarden.db.db_git',
    'callwarden.db.db_vector',
    'callwarden.db.db_guardrail',
    'callwarden.db.db_impact',
    'callwarden.db.db_evolution',
    'callwarden.db.db_defect_kb',
    'callwarden.db.db_tasks',
    'callwarden.db.db_task_attribution',
    'callwarden.db.db_task_quality',
    'callwarden.db.db_external',
    'callwarden.db.db_check_gate',
    'callwarden.db.db_comment',
    'callwarden.db.db_cas',
    'callwarden.db.db_cas_merge',
    'callwarden.db.db_coverage',
    'callwarden.db.db_clone_detection',
    'callwarden.db.db_clone_groups',
    'callwarden.db.db_cross_repo',
    'callwarden.db.db_dashboard',
    'callwarden.db.db_edit',
    'callwarden.db.db_gc',
    'callwarden.db.db_jobs',
    'callwarden.db.db_lsp',
    'callwarden.db.db_metrics',
    'callwarden.db.db_migrate',
    'callwarden.db.db_ownership',
    'callwarden.db.db_stdlib',
    'callwarden.db.db_summary',
    'callwarden.db.db_toolchain',
    'callwarden.db.db_token_savings',
    'callwarden.db.db_bootstrap',
    'callwarden.db.db_audit_chain',
    'callwarden.db.db_agent_rules',
    'callwarden.db.db_branch',
    'callwarden.db.db_tests',
    'callwarden.db.db_workspace_manifest',

    # cli.main / cli.client / cli.agent 顶层 import db，会拉入 parser
    # client/agent 入口已绕过它们，但 PyInstaller 静态分析仍可能跟踪到，
    # 显式 exclude 让收集阶段 fail closed
    'callwarden.cli.main',
    'callwarden.cli.client',
    'callwarden.cli.agent',

    # cw.py 通过 importlib 动态加载 cli.main / mcp_server / install，
    # 这些模块会拉入 db → parsers。client/agent 不需要 cw 主入口。
    'callwarden.cw',
    'callwarden.install',
    'callwarden.server.mcp_server',
]

_client_agent_excludes = list(_common_excludes) + _PARSER_EXCLUDES + _LOCAL_DB_EXCLUDES

# 注意：semgrep 的 Python/OCaml 运行时不嵌入冻结包。需要安全扫描时，用户单独安装
# semgrep 可执行文件；CallWarden 通过 shutil.which("semgrep") 调用它。

# === Analysis 1: local runtime（含 parser，所有平台）===
# cw 主入口共享同一份 Analysis、PYZ 和 COLLECT，最终目录只有一份运行时。
a_local = Analysis(
    [os.path.join(ENTRY_DIR, 'entry_cw.py')],
    pathex=[PACKAGE_PARENT],
    binaries=binaries,
    datas=datas,
    hiddenimports=_local_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=_common_excludes,
    cipher=block_cipher,
)

# PyInstaller 对 hidden import 缺失默认只发 warning，发布构建必须 fail closed。
_collected_local = {item[0] for item in a_local.pure}
_required_local = {
    'callwarden',
    'callwarden.cw',
    'callwarden.parsers.base',
    'callwarden.server.mcp_server',
}
_missing_local = sorted(_required_local - _collected_local)
if _missing_local:
    raise RuntimeError(
        'local bundle 未收集 CallWarden 核心模块: '
        + ', '.join(_missing_local)
    )

pyz_local = PYZ(a_local.pure, a_local.zipped_data, cipher=block_cipher)

# 按名称提取入口脚本（PyInstaller 6.x 会在 scripts 中混入 rthook，不能按固定下标取）
_local_entry_toc = [s for s in a_local.scripts if s[0] == 'entry_cw']
assert len(_local_entry_toc) == 1, (
    f'local bundle 期望 1 个 entry_cw 脚本，实际: {_local_entry_toc}'
)

# cw 主入口
exe_cw = EXE(
    pyz_local,
    _local_entry_toc,
    exclude_binaries=True,
    name='cw',
    console=True,
    cipher=block_cipher,
)

bundle_local = COLLECT(
    exe_cw,
    a_local.binaries,
    a_local.zipfiles,
    a_local.datas,
    name='callwarden',
)

# === Analysis 2: client/agent runtime（无 parser，仅 Linux）===
# 企业 client/agent 依赖 Linux UDS、SO_PEERCRED 和 SCM_RIGHTS。
# Windows/macOS 不构建 client/agent bundle，只产出 local bundle。
if sys.platform.startswith('linux'):
    a_client = Analysis(
        [
            os.path.join(ENTRY_DIR, 'entry_cw_client.py'),
            os.path.join(ENTRY_DIR, 'entry_cw_agent.py'),
        ],
        pathex=[PACKAGE_PARENT],
        binaries=binaries,
        datas=datas,
        hiddenimports=_client_agent_hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=_client_agent_excludes,
        cipher=block_cipher,
    )

    # fail closed: client/agent bundle 严禁包含 parser 模块
    _collected_client = {item[0] for item in a_client.pure}
    _forbidden_parser = {
        'callwarden.parsers',
        'callwarden.parsers.base',
        'callwarden.parsers.rust',
        'tree_sitter',
        'tree_sitter_rust',
        'numpy',
    }
    _leaked = sorted(_forbidden_parser & _collected_client)
    if _leaked:
        raise RuntimeError(
            'client/agent bundle 严禁包含 parser/numpy 模块， leaked: '
            + ', '.join(_leaked)
        )

    # fail closed: client/agent bundle 必须包含 daemon RPC / agent watcher 核心模块
    _required_client = {
        'callwarden.cli.daemon_commands',
        'callwarden.server.daemon_client',
        'callwarden.server.daemon_server',
        'callwarden.server.agent_watcher',
        'callwarden.db.db_daemon',
        'callwarden_core',
    }
    _missing_client = sorted(_required_client - _collected_client)
    if _missing_client:
        raise RuntimeError(
            'client/agent bundle 缺少 RPC/agent 核心模块: '
            + ', '.join(_missing_client)
        )

    pyz_client = PYZ(a_client.pure, a_client.zipped_data, cipher=block_cipher)

    _client_entry = [s for s in a_client.scripts if s[0] == 'entry_cw_client']
    _agent_entry = [s for s in a_client.scripts if s[0] == 'entry_cw_agent']
    assert len(_client_entry) == 1, (
        f'client/agent bundle 期望 1 个 entry_cw_client 脚本，实际: {_client_entry}'
    )
    assert len(_agent_entry) == 1, (
        f'client/agent bundle 期望 1 个 entry_cw_agent 脚本，实际: {_agent_entry}'
    )

    # cw-client 入口
    exe_cw_client = EXE(
        pyz_client,
        _client_entry,
        exclude_binaries=True,
        name='cw-client',
        console=True,
        cipher=block_cipher,
    )

    # cw-agent 入口
    exe_cw_agent = EXE(
        pyz_client,
        _agent_entry,
        exclude_binaries=True,
        name='cw-agent',
        console=True,
        cipher=block_cipher,
    )

    # client/agent 共享同一份 _internal 运行时（无 parser/grammar/numpy）
    bundle_client = COLLECT(
        exe_cw_client,
        exe_cw_agent,
        a_client.binaries,
        a_client.zipfiles,
        a_client.datas,
        name='callwarden-client',
    )
