"""
install.py
==========

Call Warden 一键安装脚本：级联安装核心依赖 + 各语言 tree-sitter grammar + 可选依赖。

使用方式：
    cw install              # 安装核心 + 全部已支持语言
    cw install --all        # 安装核心 + 全部语言 + 全部可选依赖
    cw install --lang csharp ruby  # 仅安装指定语言的 grammar
    cw install --check      # 仅检查依赖状态，不安装
    cw install --no-optional  # 跳过可选依赖（默认行为）
    cw install --verbose    # 显示详细安装日志

设计原则：
1. 级联安装：核心 → 已支持语言 → 扩展语言 → 可选依赖
2. 失败不中断：单个包安装失败只警告，继续安装其他包
3. 状态可见：每个包安装前后打印状态（已有/安装中/成功/失败）
4. 幂等：重复运行不会出错，已安装的包会跳过

退出码：
    0 = 全部成功
    1 = 部分失败（查看输出）
    2 = 网络或 pip 不可用
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .i18n import t


# ---------------------------------------------------------------------
# 依赖定义
# ---------------------------------------------------------------------

@dataclass
class PackageSpec:
    """单个 pip 包的安装规格"""
    pip_name: str           # pip install 用的包名
    import_name: str        # Python import 检查用的模块名
    category: str           # 分类：core / language / optional
    language: str = ""      # 语言名（仅 language 类别）
    description: str = ""   # 人类可读说明


# 核心依赖（必需全功能运行）
CORE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter", "tree_sitter", "core", description="AST 解析引擎"),
    # A15 (2026-07-20): pathspec 提供完整 gitignore 语义支持（字符类/尾随空格/negation）
    # pathspec 不可用时 IgnoreMatcher 降级到自研实现（不完整）
    PackageSpec("pathspec", "pathspec", "core",
                description=".gitignore 完整语法解析（A15）"),
    PackageSpec("fastmcp", "fastmcp", "core", description="MCP Server 框架"),
    PackageSpec("watchdog", "watchdog", "core", description="文件监控守护进程"),
    PackageSpec("numpy", "numpy", "core", description="向量与重复代码检测计算引擎"),
    PackageSpec("semgrep", "semgrep", "core",
                description="多语言静态安全扫描（守护者架构必需）"),
]

# 已支持语言（9 种，与 Semgrep 交集）
SUPPORTED_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-rust", "tree_sitter_rust",
                "language", "rust", "Rust grammar"),
    PackageSpec("tree-sitter-typescript", "tree_sitter_typescript",
                "language", "typescript", "TypeScript/TSX grammar"),
    PackageSpec("tree-sitter-python", "tree_sitter_python",
                "language", "python", "Python grammar"),
    PackageSpec("tree-sitter-kotlin", "tree_sitter_kotlin",
                "language", "kotlin", "Kotlin grammar"),
    PackageSpec("tree-sitter-go", "tree_sitter_go",
                "language", "go", "Go grammar"),
    PackageSpec("tree-sitter-java", "tree_sitter_java",
                "language", "java", "Java grammar"),
    PackageSpec("tree-sitter-c", "tree_sitter_c",
                "language", "c", "C grammar"),
    PackageSpec("tree-sitter-cpp", "tree_sitter_cpp",
                "language", "cpp", "C++ grammar"),
    PackageSpec("tree-sitter-javascript", "tree_sitter_javascript",
                "language", "javascript", "JavaScript/JSX grammar"),
]

# P0 扩展语言（Semgrep 独有，新增支持）
EXTENDED_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-c-sharp", "tree_sitter_c_sharp",
                "language", "csharp", "C# grammar（Semgrep 170+ Pro 规则）"),
    PackageSpec("tree-sitter-ruby", "tree_sitter_ruby", "language",
                "ruby", "Ruby grammar（Semgrep 40+ Pro 规则）"),
]

# P1 扩展语言（Web 与 iOS 生态）
P1_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-php", "tree_sitter_php", "language",
                "php", "PHP grammar（Semgrep 50+ Pro 规则，Web 安全场景）"),
    PackageSpec("tree-sitter-swift", "tree_sitter_swift", "language",
                "swift", "Swift grammar（iOS 生态，Semgrep 60+ Pro 规则）"),
]

# P2 扩展语言（JVM 与 IaC 生态）
P2_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-scala", "tree_sitter_scala",
                "language", "scala", "Scala grammar（JVM 生态，社区规则）"),
    PackageSpec("tree-sitter-hcl", "tree_sitter_hcl", "language",
                "hcl", "Terraform/HCL grammar（IaC 场景）"),
]

# P3 扩展语言（Semgrep Beta）
P3_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-elixir", "tree_sitter_elixir",
                "language", "elixir", "Elixir grammar（Semgrep 仅 Beta）"),
]

# 可选依赖（AI 语义向量 RAG 扩展，按需启用 cw install --all）
OPTIONAL_PACKAGES: List[PackageSpec] = [
    PackageSpec("sentence-transformers", "sentence_transformers",
                "optional", description="向量嵌入（语义搜索，依赖 PyTorch）"),
    PackageSpec("sqlite-vec", "sqlite_vec", "optional", description="向量索引扩展"),
]


@dataclass
class DetectedAgent:
    """检测到的已安装 Agent 信息"""
    agent_key: str
    display: str
    detected_by: str       # "cli" / "config_dir" / "win_config"
    detect_detail: str     # 具体检测到的路径或命令
    family: str = ""       # 产品家族（如 anthropic, cline, google），用于去重和分组摘要


# ---------------------------------------------------------------------
# 安装器实现
# ---------------------------------------------------------------------

@dataclass
class InstallResult:
    """安装结果汇总"""
    total: int = 0
    installed: int = 0          # 本次新安装数
    skipped: int = 0            # 已存在跳过数
    failed: int = 0             # 失败数
    failed_packages: List[str] = field(default_factory=list)


class CallWardenInstaller:
    """Call Warden 级联安装器"""

    def __init__(self, verbose: bool = False):
        """初始化级联安装器

        Args:
            verbose: 是否输出详细安装日志
        """
        self.verbose = verbose
        self.result = InstallResult()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def install_all(self, include_optional: bool = False,
                    languages_only: Optional[Set[str]] = None) -> InstallResult:
        """级联安装：核心 → 已支持语言 → 扩展语言 → 可选依赖

        Args:
            include_optional: 是否安装可选依赖
            languages_only: 若指定，只安装这些语言的 grammar（不装核心包）
        """
        print("=" * 60)
        print(t("cli.messages.install_title"))
        print("=" * 60)
        print()

        # 检查 pip 可用性
        if not self._check_pip():
            print(t("cli.messages.install_pip_unavailable"))
            sys.exit(2)

        if languages_only:
            # 仅安装指定语言的 grammar
            self._install_languages_by_filter(languages_only)
        else:
            # 完整级联安装
            print(t("cli.messages.install_step_1"))
            self._install_group(CORE_PACKAGES)
            print()

            print(t("cli.messages.install_step_2"))
            self._install_group(SUPPORTED_LANGUAGE_PACKAGES)
            print()

            print(t("cli.messages.install_step_3"))
            self._install_group(EXTENDED_LANGUAGE_PACKAGES)
            print()

            print(t("cli.messages.install_step_4"))
            self._install_group(P1_LANGUAGE_PACKAGES)
            print()

            print(t("cli.messages.install_step_5"))
            self._install_group(P2_LANGUAGE_PACKAGES)
            print()

            print(t("cli.messages.install_step_6"))
            self._install_group(P3_LANGUAGE_PACKAGES)
            print()

            if include_optional:
                print(t("cli.messages.install_step_7"))
                self._install_group(OPTIONAL_PACKAGES)
                print()

        # 预下载 Semgrep 规则到 ~/.cache/semgrep/（避免首次扫描卡顿）
        # 仅当 semgrep 已安装时执行，失败不阻塞安装流程
        self._prefetch_semgrep_rules()

        # 打印汇总
        self._print_summary()
        return self.result

    def detect_installed_agents(self) -> List[DetectedAgent]:
        """检测本机已安装的 AI Agent（多层检测：CLI → 配置目录 → Windows 特有路径）

        数据源：cli.agent_registry.AGENT_REGISTRY（统一注册表）
        """
        # 延迟导入，避免循环依赖
        from .cli.agent_registry import AGENT_REGISTRY

        detected: List[DetectedAgent] = []
        seen: Set[str] = set()
        home = os.path.expanduser("~")
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")

        for key, spec in AGENT_REGISTRY.items():
            if key in seen:
                continue

            # 第 1 层：检测 CLI 命令（最高可信度）
            for cmd in spec.cli_commands:
                cmd_path = shutil.which(cmd)
                if cmd_path:
                    detected.append(DetectedAgent(
                        agent_key=key,
                        display=spec.display,
                        detected_by="cli",
                        detect_detail=cmd_path,
                        family=spec.family,
                    ))
                    seen.add(key)
                    break
            if key in seen:
                continue

            # 第 2 层：检测 ~/ 下的配置目录
            for d in spec.config_dirs:
                dir_path = os.path.join(home, d)
                if os.path.isdir(dir_path):
                    detected.append(DetectedAgent(
                        agent_key=key,
                        display=spec.display,
                        detected_by="config_dir",
                        detect_detail=dir_path,
                        family=spec.family,
                    ))
                    seen.add(key)
                    break
            if key in seen:
                continue

            # 第 3 层：Windows 特有路径（%APPDATA% / %LOCALAPPDATA% 下）
            if sys.platform == "win32":
                for d in spec.win_config_dirs:
                    for base in [appdata, localappdata]:
                        if not base:
                            continue
                        dir_path = os.path.join(base, d)
                        if os.path.isdir(dir_path):
                            detected.append(DetectedAgent(
                                agent_key=key,
                                display=spec.display,
                                detected_by="win_config",
                                detect_detail=dir_path,
                                family=spec.family,
                            ))
                            seen.add(key)
                            break
                    if key in seen:
                        break

        return detected

    def print_detected_agents(self, agents: List[DetectedAgent]) -> None:
        """打印检测到的 Agent 列表（含按家族分组摘要）"""
        print(t("cli.messages.install_agent_detect_title",
                default="=== Detected AI Agents ==="))
        if not agents:
            print(t("cli.messages.install_agent_detect_none",
                    default="  No supported AI agents detected."))
            print()
            return

        for a in agents:
            icon = "[CLI]" if a.detected_by == "cli" else (
                "[CFG]" if a.detected_by == "config_dir" else "[WIN]")
            print(f"  {icon} {a.display} ({a.agent_key})")
            print(f"       -> {a.detect_detail}")
        print()

        # 按家族分组摘要
        from collections import defaultdict
        family_groups: Dict[str, List[str]] = defaultdict(list)
        for a in agents:
            family_groups[a.family].append(a.display)
        print("检测到以下 AI 编码工具（按家族分组）：")
        for fam in sorted(family_groups.keys()):
            names = ", ".join(family_groups[fam])
            print(f"  {fam}: {names}")
        print()

    @staticmethod
    def _deduplicate_by_shared_config(detected: List[DetectedAgent]) -> List[DetectedAgent]:
        """对于共享 MCP 配置文件的家族，只保留一个代表 Agent，避免重复写入同一配置文件。

        当前共享配置的家族：
        - cline: cline (VS Code 扩展) 和 cline-cli 都写入 ~/.cline/mcp.json，只需安装一次。
          优先保留 cline-cli（CLI 形态更可靠，配置路径更明确）

        注意：
        - anthropic 家族：claude-code (~/.claude.json) 和 claude-desktop (claude_desktop_config.json)
          使用不同配置文件，两者都需要安装
        - google 家族：gemini-cli (~/.gemini/settings.json) 和 antigravity
          (~/.gemini/config/mcp_config.json) 使用不同配置文件，两者都需要安装
        - 此方法仅在 auto-detect 模式下调用，不影响用户显式指定的 Agent 列表
        """
        # 完全共享配置文件的家族：同家族内多个形态只需安装一次
        shared_config_families = {"cline"}
        # 共享家族内的优先保留 key（CLI 形态更可靠）
        preferred_keys = {"cline": "cline-cli"}

        # 对共享家族内的 Agent 排序：优先 key 排在前面，确保被优先选中
        def _sort_key(agent: DetectedAgent) -> int:
            pref = preferred_keys.get(agent.family)
            if pref and agent.agent_key == pref:
                return -1  # 优先形态排最前面
            if agent.family in shared_config_families:
                return 1   # 同家族非优先排后面
            return 0       # 非共享家族保持原位

        sorted_detected = sorted(detected, key=_sort_key)

        seen_families: Set[str] = set()
        deduped: List[DetectedAgent] = []
        for agent in sorted_detected:
            if agent.family in shared_config_families:
                if agent.family in seen_families:
                    # 同家族已有代表，跳过以避免重复写入同一配置文件
                    continue
                seen_families.add(agent.family)
            deduped.append(agent)
        return deduped

    def install_agent_integrations(
        self,
        agents: Optional[List[DetectedAgent]] = None,
        global_mode: bool = True,
        force: bool = False,
        auto_detect: bool = False,
    ) -> List[str]:
        """为检测到的 Agent 安装 Call Warden MCP 集成

        直接复用 cli/main.py 中的 _write_global_mcp_config 逻辑，
        避免 subprocess 调用自身的复杂性。

        Args:
            agents: 要安装的 Agent 列表，None 时自动检测
            global_mode: True=写入全局配置，False=写入项目级配置
            force: 是否强制覆盖已有配置
            auto_detect: 是否为自动探测模式（True 时对共享配置家族去重）

        Returns:
            创建/更新的文件路径列表
        """
        if agents is None:
            agents = self.detect_installed_agents()
            auto_detect = True  # 自动探测时标记，以便后续去重

        if not agents:
            print(t("cli.messages.install_agent_detect_none",
                    default="  No supported AI agents detected."))
            return []

        # 自动探测模式下，对共享配置文件的家族去重（如 cline 家族只需安装一次）
        if auto_detect:
            original_count = len(agents)
            agents = self._deduplicate_by_shared_config(agents)
            if len(agents) < original_count:
                print(
                    f"  [去重] 共享配置家族去重：{original_count} → {len(agents)} 个 Agent")

        # 延迟导入，避免循环依赖；使用 get_merged_specs 以包含外部 overlay 的 Agent
        from .cli.agent_registry import get_merged_specs
        from .cli.main import _write_global_mcp_config, _write_agent_integration
        from .config import detect_project_root

        created: List[str] = []
        root = detect_project_root(os.getcwd()) or os.getcwd()
        mode = "global" if global_mode else "project"

        print(t("cli.messages.install_agent_install_title",
                default="=== Installing Agent Integrations ==="))
        print(t("cli.messages.install_agent_install_mode",
                default="  Mode: {mode}", mode=mode))
        print(t("cli.messages.install_agent_install_count",
                default="  Agents: {count}", count=len(agents)))
        print()

        # 合并内置 + 外部 overlay，保证外部注册的 Agent 也能被安装
        merged_specs = get_merged_specs()
        for a in agents:
            spec = merged_specs.get(a.agent_key)
            if not spec:
                print(t("cli.messages.install_agent_unknown",
                        default="  [SKIP] Unknown agent: {key}", key=a.agent_key))
                continue

            if global_mode and not spec.get("global_mcp_relpath"):
                print(t("cli.messages.install_agent_no_global",
                        default="  [SKIP] {name}: no global config path (use project mode)",
                        name=a.display))
                continue

            print(t("cli.messages.install_agent_installing",
                    default="  Installing for {name}...", name=a.display))

            if global_mode:
                files = _write_global_mcp_config(spec, root, force)
            else:
                out_root = os.path.join(
                    root, ".callwarden", "agent-integrations")
                files = _write_agent_integration(
                    root, out_root, a.agent_key, spec, force)

            created.extend(files)
            for f in files:
                print(f"    -> {f}")

        print()
        print(t("cli.messages.install_agent_install_done",
                default="  Done. {count} file(s) created/updated.", count=len(created)))
        print()
        return created

    def auto_setup(self, force: bool = False, skip_agents_md: bool = True) -> list:
        """首次运行自动配置：探测已安装 AI 工具 + 全局 MCP 注册。

        设计原则：
        - 幂等：标记文件存在时直接返回，不重复配置
        - 安全：探测为空时不报错，写入失败时打印警告但不崩溃
        - 只处理全局 MCP 注册（global_mode=True），不涉及 AGENTS.md 注入

        Args:
            force: 强制重新配置（忽略标记文件）
            skip_agents_md: 跳过 AGENTS.md 注入（全局安装无项目上下文，默认 True）

        Returns:
            已配置的 Agent key 列表；空列表表示未执行任何配置
        """
        from datetime import datetime, timezone
        from .config import AUTO_SETUP_MARKER

        # 检查标记文件是否已存在（幂等保护）
        if not force and os.path.isfile(AUTO_SETUP_MARKER):
            return []

        # 探测已安装的 AI Agent
        detected = self.detect_installed_agents()
        if not detected:
            # 无探测到的工具，仍然创建标记文件避免每次都扫描
            self._write_auto_setup_marker(AUTO_SETUP_MARKER, [])
            return []

        # 调用全局 MCP 注册
        configured_keys: List[str] = []
        try:
            created_files = self.install_agent_integrations(
                agents=detected,
                global_mode=True,
                force=False,
                auto_detect=True,
            )
            # 只记录实际有文件创建的 agent
            if created_files:
                for a in detected:
                    if any(a.agent_key in f for f in created_files):
                        configured_keys.append(a.agent_key)
        except Exception as e:
            # 写入失败时打印警告但不崩溃
            print(t("cli.messages.auto_setup_write_failed",
                    default="  [WARN] Auto-setup write failed: {error}",
                    error=str(e)))

        # 创建标记文件（无论是否成功都创建，避免每次都重试）
        self._write_auto_setup_marker(AUTO_SETUP_MARKER, configured_keys)

        return configured_keys

    @staticmethod
    def _write_auto_setup_marker(marker_path: str, agents: List[str]) -> None:
        """写入自动配置标记文件（JSON 格式，包含时间戳和已配置的 Agent 列表）

        Args:
            marker_path: 标记文件路径
            agents: 已配置的 Agent key 列表
        """
        from datetime import datetime, timezone

        marker_dir = os.path.dirname(marker_path)
        try:
            os.makedirs(marker_dir, exist_ok=True)
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agents": agents,
            }
            # 原子写入：临时文件 + rename，避免写入中途崩溃导致标记文件损坏
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            tmp_path = marker_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, marker_path)
        except OSError as e:
            # 标记文件写入失败时只打印警告，不中断流程
            print(t("cli.messages.auto_setup_marker_write_failed",
                    default="  [WARN] Could not write auto-setup marker: {error}",
                    error=str(e)))

    def _prefetch_semgrep_rules(self) -> None:
        """后台异步预下载 Semgrep p/default 规则集到本地缓存（非阻塞）

        semgrep --config p/default 首次调用时会从 registry 下载规则到
        ~/.cache/semgrep/，可能导致首次扫描卡顿数十秒。
        本方法在安装完成后启动后台线程下载规则，不阻塞安装流程。

        多用户场景：
        - root 安装（系统级）：下载到 /var/lib/callwarden/semgrep_rules/，
          设置 755 权限供所有用户只读共享
        - 普通用户安装（pip install --user）：下载到 ~/.cache/semgrep/，
          仅当前用户可用
        - 普通用户运行时：优先用系统级共享缓存，缺失才下载到用户级

        策略：
        1. 检查 semgrep CLI 是否可用 + 缓存目录可写权限
        2. Popen 启动子进程 + 后台线程 wait(timeout) 监控
        3. 超时杀进程避免子进程挂起；异常分类处理给出明确提示
        4. 完全非阻塞，失败不影响安装结果
        """
        import shutil
        import threading
        from .config import SYSTEM_SEMGREP_RULES_DIR, is_system_cache_available

        semgrep_path = shutil.which("semgrep")
        if not semgrep_path:
            # Windows: 检查 Python Scripts 目录
            import site
            for site_path in site.getsitepackages():
                scripts_dir = os.path.join(
                    os.path.dirname(site_path), "Scripts")
                semgrep_exe = os.path.join(scripts_dir, "semgrep.exe")
                if os.path.exists(semgrep_exe):
                    semgrep_path = semgrep_exe
                    break

        if not semgrep_path:
            return  # semgrep 未安装，跳过

        # 判断当前用户是否为 root（Linux/Mac）以决定下载目标
        # root 安装到系统级共享路径，普通用户安装到用户级
        is_root = os.name != "nt" and os.geteuid() == 0 if hasattr(os, "geteuid") else False

        if is_root and SYSTEM_SEMGREP_RULES_DIR:
            # root 安装：下载到系统级共享路径 /var/lib/callwarden/semgrep_rules/
            target_cache_dir = SYSTEM_SEMGREP_RULES_DIR
            cache_type = "system"
        else:
            # 普通用户安装：下载到用户级缓存 ~/.cache/semgrep/
            target_cache_dir = os.path.join(
                os.path.expanduser("~"), ".cache", "semgrep")
            cache_type = "user"

        # 系统级缓存已存在且非空，跳过预下载
        if cache_type == "system" and is_system_cache_available():
            return

        # 用户级缓存已存在且非空，跳过预下载
        if cache_type == "user":
            user_cache = os.path.join(
                os.path.expanduser("~"), ".cache", "semgrep")
            if os.path.isdir(user_cache) and os.listdir(user_cache):
                return
            # Windows: 检查 %LOCALAPPDATA%/semgrep/
            win_cache = os.path.join(
                os.environ.get("LOCALAPPDATA", ""), "semgrep")
            if win_cache and os.path.isdir(win_cache) and os.listdir(win_cache):
                return

        # 检查缓存父目录是否存在且可写
        cache_parent = os.path.dirname(
            target_cache_dir) or os.path.expanduser("~")
        try:
            # root 安装时创建系统级目录
            if cache_type == "system" and not os.path.isdir(cache_parent):
                os.makedirs(cache_parent, exist_ok=True)

            if not os.path.isdir(cache_parent):
                print(t("cli.messages.install_semgrep_prefetch_skip",
                        default="[Semgrep] Cache parent dir not exists: {dir}. Skip prefetch.",
                        dir=cache_parent))
                return
            if not os.access(cache_parent, os.W_OK):
                print(t("cli.messages.install_semgrep_prefetch_skip",
                        default="[Semgrep] No write permission for cache dir: {dir}. Skip prefetch.",
                        dir=cache_parent))
                return
        except (OSError, PermissionError) as e:
            print(t("cli.messages.install_semgrep_prefetch_skip",
                    default="[Semgrep] Cache dir check failed: {error}. Skip prefetch.",
                    error=str(e)))
            return

        print(t("cli.messages.install_semgrep_prefetch",
                default="[Semgrep] Starting background rule cache prefetch ({cache_type} cache, p/default, non-blocking, 120s timeout)...",
                cache_type=cache_type))

        # Popen 启动子进程
        try:
            proc = subprocess.Popen(
                [semgrep_path, "--config", "p/default", "--validate"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            print(t("cli.messages.install_semgrep_prefetch_skip",
                    default="[Semgrep] semgrep binary not found. Skip prefetch."))
            return
        except PermissionError as e:
            print(t("cli.messages.install_semgrep_prefetch_skip",
                    default="[Semgrep] Permission denied when launching semgrep: {error}. Skip prefetch.",
                    error=str(e)))
            return
        except OSError as e:
            print(t("cli.messages.install_semgrep_prefetch_skip",
                    default="[Semgrep] OS error when launching semgrep: {error}. Skip prefetch.",
                    error=str(e)))
            return
        except Exception as e:
            print(t("cli.messages.install_semgrep_prefetch_skip",
                    default="[Semgrep] Unexpected error when launching prefetch: {error}. Skip prefetch.",
                    error=str(e)))
            return

        print(t("cli.messages.install_semgrep_prefetch_scheduled",
                default="[Semgrep] Background prefetch scheduled (pid={pid}, cache={cache_type}). Rules will be ready for first scan.",
                pid=proc.pid, cache_type=cache_type))

        # 后台线程监控子进程，超时则 kill
        def _monitor_timeout(p, timeout=120, cache_dir=None, is_system=False):
            try:
                p.wait(timeout=timeout)
                # root 安装完成后，把规则从 root 用户级缓存复制到系统级共享路径
                if is_system and cache_dir:
                    root_user_cache = os.path.join(
                        os.path.expanduser("~"), ".cache", "semgrep"
                    )
                    if os.path.isdir(root_user_cache) and os.listdir(root_user_cache):
                        try:
                            # 创建系统级目录
                            os.makedirs(cache_dir, exist_ok=True)
                            # 复制规则文件到系统级路径
                            import shutil as _shutil
                            for item in os.listdir(root_user_cache):
                                src = os.path.join(root_user_cache, item)
                                dst = os.path.join(cache_dir, item)
                                if os.path.isdir(src):
                                    _shutil.copytree(
                                        src, dst, dirs_exist_ok=True)
                                else:
                                    _shutil.copy2(src, dst)
                            # 设置系统级缓存目录权限为 755（所有用户可读）
                            os.chmod(cache_dir, 0o755)
                            for root_dir, dirs, files in os.walk(cache_dir):
                                for d in dirs:
                                    os.chmod(os.path.join(root_dir, d), 0o755)
                                for f in files:
                                    os.chmod(os.path.join(root_dir, f), 0o644)
                            print(t("cli.messages.install_semgrep_prefetch_system_ok",
                                    default="[Semgrep] System-level shared cache ready at {dir}. All users can use it.",
                                    dir=cache_dir))
                        except (OSError, PermissionError) as e:
                            print(t("cli.messages.install_semgrep_prefetch_skip",
                                    default="[Semgrep] Failed to copy rules to system cache: {error}.",
                                    error=str(e)))
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                    else:
                        import signal
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (OSError, subprocess.SubprocessError):
                    try:
                        p.kill()
                    except (OSError, subprocess.SubprocessError):
                        pass
                print(t("cli.messages.install_semgrep_prefetch_timeout",
                        default="[Semgrep] Background prefetch timed out ({timeout}s). Will retry on first scan.",
                        timeout=timeout))
            except (OSError, subprocess.SubprocessError):
                pass

        monitor_thread = threading.Thread(
            target=_monitor_timeout,
            args=(proc, 120, target_cache_dir if cache_type ==
                  "system" else None, cache_type == "system"),
            daemon=True,
        )
        monitor_thread.start()

    def check_status(self) -> None:
        """仅检查依赖状态，不安装"""
        print("=" * 60)
        print(t("cli.messages.install_check_title"))
        print("=" * 60)
        print()

        print(t("cli.messages.install_check_core"))
        self._check_group(CORE_PACKAGES)
        print()

        print(t("cli.messages.install_check_supported"))
        self._check_group(SUPPORTED_LANGUAGE_PACKAGES)
        print()

        print(t("cli.messages.install_check_p0"))
        self._check_group(EXTENDED_LANGUAGE_PACKAGES)
        print()

        print(t("cli.messages.install_check_p1"))
        self._check_group(P1_LANGUAGE_PACKAGES)
        print()

        print(t("cli.messages.install_check_p2"))
        self._check_group(P2_LANGUAGE_PACKAGES)
        print()

        print(t("cli.messages.install_check_p3"))
        self._check_group(P3_LANGUAGE_PACKAGES)
        print()

        print(t("cli.messages.install_check_optional"))
        self._check_group(OPTIONAL_PACKAGES)
        print()

        print(t("cli.messages.install_check_hint"))

    def install_hooks(
        self,
        force: bool = False,
        with_post_commit: bool = True,
        with_ref_transaction: bool = True,
    ) -> None:
        """安装 Git hooks 到当前仓库（统一入口：pre-commit + pre-push + reference-transaction + post-commit）

        四种 hook 的职责：
        - pre-commit：提交前刷新代码图谱（确保数据库与代码同步）
        - pre-push：推送前运行 check-gate 门禁（需设置 CALLWARDEN_TASK_ID）
        - reference-transaction：审计 ref 变更（reset --hard / branch -f / force push），
            仅记录到 destructive_operations 表，不能拦截 working tree 破坏
            （git 无 pre-checkout/pre-reset hook，reset --hard 工作树写入先于 ref 更新）
        - post-commit：提交后自动捕获变更到 task/audit 闭环（--auto 模式，开箱即用）

        Args:
            force: 若目标 hook 已存在但不是 Call Warden 生成的，True=强制覆盖
            with_post_commit: 是否安装 post-commit hook（默认 True）。
                设为 False 可跳过 post-commit（如用户已有自定义 post-commit 流程）。
            with_ref_transaction: 是否安装 reference-transaction hook（默认 True）。
                设为 False 可跳过（如 git 版本 < 2.28 不支持此 hook）。

        若目标 hook 已存在且不是 Call Warden 生成的，默认拒绝覆盖，
        避免破坏用户自定义流程。
        """
        git_dir = self._find_git_dir(os.getcwd())
        if not git_dir:
            print(t("cli.messages.install_hooks_no_git",
                  default="Not inside a Git repository; hooks were not installed."))
            return

        hooks_dir = os.path.join(git_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        hook_defs = {
            "pre-commit": self._pre_commit_hook(),
            "pre-push": self._pre_push_hook(),
        }
        if with_ref_transaction:
            # L2 审计层：reference-transaction hook 记录 ref 变更到 destructive_operations
            # 仅审计，不拦截（git 无 pre-checkout/pre-reset hook，无法阻止 working tree 破坏）
            hook_defs["reference-transaction"] = self._reference_transaction_hook()
        if with_post_commit:
            # post-commit 使用 --auto 模式（task_id=""），无需环境变量
            hook_defs["post-commit"] = self._post_commit_hook(task_id="")

        installed = 0
        skipped = 0
        for hook_name, content in hook_defs.items():
            hook_path = os.path.join(hooks_dir, hook_name)
            if self._write_hook(hook_path, content, force=force):
                installed += 1
                print(t("cli.messages.install_hooks_installed", hook=hook_path))
            else:
                skipped += 1
                print(t("cli.messages.install_hooks_skipped", hook=hook_path))

        print(t("cli.messages.install_hooks_summary",
              installed=installed, skipped=skipped))

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _find_git_dir(start_dir: str) -> str:
        """从当前目录向上查找 .git 目录。"""
        cur = os.path.abspath(start_dir)
        while True:
            git_path = os.path.join(cur, ".git")
            if os.path.isdir(git_path):
                return git_path
            parent = os.path.dirname(cur)
            if parent == cur:
                return ""
            cur = parent

    @staticmethod
    def _hook_marker() -> str:
        """Call Warden hook 标记，用于幂等更新。"""
        return "# CALLWARDEN-GIT-HOOK"

    def _write_hook(self, hook_path: str, content: str, force: bool = False) -> bool:
        """写入单个 hook，保护用户已有 hook。"""
        marker = self._hook_marker()
        if os.path.exists(hook_path):
            try:
                with open(hook_path, "r", encoding="utf-8", errors="ignore") as f:
                    existing = f.read()
            except OSError:
                existing = ""
            if marker not in existing and not force:
                return False

        with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        mode = os.stat(hook_path).st_mode
        os.chmod(hook_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return True

    def _python_cw_command(self) -> str:
        """生成 hook 中调用 cw.py 的跨平台命令。"""
        cw_py = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "cw.py"))
        cw_py = cw_py.replace(os.sep, "/")
        return f'python "{cw_py}"'

    def _pre_commit_hook(self) -> str:
        """生成 pre-commit hook 内容。

        L3 增强：提交前检查 active_task_id（软门禁，警告不阻止）。

        容错设计（T-1784403320003）：
        - `cw --refresh-all` 在 SQLite WAL 模式下偶尔因 SQLITE_CANTOPEN
          ('unable to open database file') 失败。常见原因：
          (a) MCP Server 或其他 cw 进程持有 -shm 文件锁（间歇性，重试可恢复）；
          (b) TRAE IDE 沙箱拦截 sh.exe 子进程对 ~/.callwarden 目录的写操作
              （持续性，重试无效，需配置沙箱规则）。
        - 原 hook 用 `set -eu` 在此失败时让整个脚本退出非零，git commit 被
          取消，迫使用户 `--no-verify` 绕过。
        - 修复：refresh 失败时重试 3 次（间隔 2 秒），覆盖临时锁场景；
          重试仍失败时打印明确错误信息 + 解决建议（停 MCP Server /
          手动 `cw refresh --all` / 配置 TRAE 沙箱规则），并退出非零保持
          AGENTS.md 规则 1 的硬性要求（提交前必须全量刷新数据库）。
        - check-task 保持软门禁（`|| true`），不阻止 commit。
        """
        cmd = self._python_cw_command()
        marker = self._hook_marker()
        return f"""#!/bin/sh
{marker}
set -eu
export PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}"
# L3: 检查 active_task（软门禁，警告不阻止 commit）
{cmd} git check-task || true
echo "[Call Warden] refreshing code graph before commit..."
# 容错重试：refresh-all 偶尔因 SQLite WAL 锁冲突失败（SQLITE_CANTOPEN），
# 重试 3 次（间隔 2 秒）以覆盖临时锁场景；仍失败时打印建议并退出非零
# （保持 AGENTS.md 规则 1：提交前必须全量刷新数据库）
_refresh_attempt=0
_refresh_max=3
while [ "$_refresh_attempt" -lt "$_refresh_max" ]; do
  if {cmd} --refresh-all; then
    break
  fi
  _refresh_attempt=$((_refresh_attempt + 1))
  if [ "$_refresh_attempt" -lt "$_refresh_max" ]; then
    echo "[Call Warden] refresh 失败，第 $_refresh_attempt/$_refresh_max 次重试（2 秒后）..."
    sleep 2
  fi
done
if [ "$_refresh_attempt" -ge "$_refresh_max" ]; then
  echo "[Call Warden] ERROR: cw --refresh-all 重试 $_refresh_max 次后仍失败。"
  echo "[Call Warden] commit 已被阻止（AGENTS.md 规则 1：提交前必须全量刷新数据库）。"
  echo "[Call Warden] 排查建议："
  echo "  1. 停止 MCP Server：cw server --stop"
  echo "  2. 手动刷新：cw refresh --all"
  echo "  3. 检查 ~/.callwarden/callwarden.db 文件权限和 -shm/-wal 残留"
  echo "  4. 若在 TRAE IDE 中运行（git commit 触发 sh.exe hook）："
  echo "     - TRAE 沙箱可能拦截 sh.exe 子进程对 ~/.callwarden 的写操作"
  echo "     - 在 Settings -> Conversation -> Custom Sandbox Configuration 中"
  echo "       添加允许规则：C:\\\\Users\\\\wanpi\\\\.callwarden\\\\（写权限）"
  echo "     - 或在 PowerShell 终端中手动运行 'cw refresh --all' 后"
  echo "       用 'git commit --no-verify' 跳过 hook"
  echo "  5. 确认 DB 已刷新后重新 commit"
  exit 1
fi
"""

    def _pre_push_hook(self) -> str:
        """生成 pre-push hook 内容。

        L2 增强：检测 force push 并记录到 destructive_operations 表（软门禁，记录不阻止）。
        保留原有 check-gate 逻辑（CALLWARDEN_TASK_ID 环境变量）。
        """
        cmd = self._python_cw_command()
        marker = self._hook_marker()
        return f"""#!/bin/sh
{marker}
set -eu
export PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}"
# L2: 检测 force push（软门禁，记录不阻止）
while read -r local_ref local_sha remote_ref remote_sha; do
  {cmd} git check-push "$local_ref" "$local_sha" "$remote_ref" "$remote_sha" || true
done
# 保留原有 check-gate 逻辑（CALLWARDEN_TASK_ID 环境变量）
if [ -z "${{CALLWARDEN_TASK_ID:-}}" ]; then
  exit 0
fi
echo "[Call Warden] running check-gate for $CALLWARDEN_TASK_ID before push..."
{cmd} check-gate "$CALLWARDEN_TASK_ID"
"""

    def _reference_transaction_hook(self) -> str:
        """生成 reference-transaction hook 内容。

        L2 审计层（2026-07-20 二轮评审补全）：
        - git 无 pre-checkout / pre-reset hook，无法在 working tree 破坏前
          拦截 `git checkout .` / `git reset --hard`。
        - reference-transaction hook 在 ref 更新前触发（prepare）+ 完成后
          触发（committed），但 `reset --hard` 的 working tree 写入先于
          ref 更新，故此 hook 只能作审计层（记录），不能作拦截层。
        - 软门禁：仅记录到 destructive_operations 表，永不 exit 非零。

        输入格式（stdin）：
            <old-value> <new-value> <ref-name> <flags>

        flags 可能包含 "forced" / "no-update" 等，用于识别破坏性 ref 更新
        （如 reset --hard / branch -f / push --force）。
        """
        cmd = self._python_cw_command()
        marker = self._hook_marker()
        return f"""#!/bin/sh
{marker}
# L2: reference-transaction hook — 审计层（软门禁，仅记录不阻止）
# git 无 pre-checkout/pre-reset hook；reset --hard 的 working tree 写入
# 先于 ref 更新，故此 hook 仅用于审计 ref 变更，不能拦截 working tree 破坏
set -eu
export PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}"
while read -r old_value new_value ref_name flags; do
  {cmd} git check-ref-transaction "$old_value" "$new_value" "$ref_name" "$flags" || true
done
"""

    def _post_commit_hook(self, task_id: str = "") -> str:
        """生成 post-commit hook 内容

        在 commit 完成后自动捕获文件变更到 task/audit 闭环。

        Args:
            task_id: 指定的任务 ID。为空时使用 --auto 模式自动检测
                     in_progress 状态的任务（无需手动 export CALLWARDEN_TASK_ID）。

        Returns:
            post-commit hook 脚本内容
        """
        cmd = self._python_cw_command()
        marker = self._hook_marker()
        if task_id:
            # 硬编码 task_id，直接调用
            return f"""#!/bin/sh
{marker}
# post-commit: 自动捕获文件变更到 task/audit 闭环（task_id 硬编码）
export PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}"
echo "[Call Warden] capturing diff for task {task_id}..."
{cmd} task capture-diff "{task_id}" || true
"""
        # 默认使用 --auto 模式，自动检测 in_progress 任务，无需环境变量
        # task_capture_diff_auto() 已有双层 fail-soft 保护（DB + CLI），
        # 不会阻断 git commit 流程
        return f"""#!/bin/sh
{marker}
# post-commit: 自动捕获文件变更到 task/audit 闭环（--auto 模式）
export PYTHONIOENCODING="${{PYTHONIOENCODING:-utf-8}}"
echo "[Call Warden] auto-capturing diff for in-progress task..."
{cmd} task capture-diff --auto || true
"""

    def install_post_commit_hook(
        self, task_id: str = "", uninstall: bool = False
    ) -> bool:
        """安装或卸载 post-commit hook

        Args:
            task_id: 指定的任务 ID。为空时使用 --auto 模式自动检测
                in_progress 状态的任务（基于 active_task 持久化字段，无需
                手动 export CALLWARDEN_TASK_ID 环境变量）。
            uninstall: True=卸载 hook，False=安装 hook

        Returns:
            True=操作成功，False=操作失败（如 git 目录不存在）

        注意：
            `cw install --hooks` 已默认包含 post-commit（--auto 模式），
            无需单独执行此命令。此接口保留用于单独卸载 post-commit 或
            安装硬编码 task_id 的 post-commit（如 CI 流水线场景）。
        """
        git_dir = self._find_git_dir(os.getcwd())
        if not git_dir:
            print(t(
                "cli.messages.install_hooks_no_git",
                default="Not inside a Git repository; hooks were not installed.",
            ))
            return False

        hook_path = os.path.join(git_dir, "hooks", "post-commit")

        if uninstall:
            # 卸载：仅删除 Call Warden 生成的 hook
            if os.path.exists(hook_path):
                try:
                    with open(hook_path, "r", encoding="utf-8", errors="ignore") as f:
                        existing = f.read()
                except OSError:
                    existing = ""
                if self._hook_marker() in existing:
                    os.remove(hook_path)
                    print(t(
                        "cli.messages.install_hook_uninstalled",
                        path=hook_path,
                        default=f"Uninstalled hook: {hook_path}",
                    ))
                else:
                    print(t(
                        "cli.messages.install_hook_skip_non_cw",
                        path=hook_path,
                        default=f"Skipped non-Call-Warden hook: {hook_path}",
                    ))
            else:
                print(t(
                    "cli.messages.install_hook_not_found",
                    path=hook_path,
                    default=f"Hook not found: {hook_path}",
                ))
            return True

        # 安装：写入 post-commit hook
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        content = self._post_commit_hook(task_id=task_id)
        if self._write_hook(hook_path, content, force=False):
            print(t(
                "cli.messages.install_hook_installed",
                path=hook_path,
                default=f"Installed hook: {hook_path}",
            ))
            if task_id:
                print(t(
                    "cli.messages.install_hook_task_id_hardcoded",
                    task_id=task_id,
                    default=f"  task_id hardcoded: {task_id}",
                ))
            else:
                print(t(
                    "cli.messages.install_hook_task_id_auto",
                    default="  task_id auto-detected via active_task (--auto mode)",
                ))
            return True
        else:
            print(t(
                "cli.messages.install_hooks_skipped",
                path=hook_path,
                default=f"Skipped existing non-Call-Warden hook: {hook_path}",
            ))
            return False

    def _check_pip(self) -> bool:
        """检查 pip 是否可用"""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, shell=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _is_package_installed(self, spec: PackageSpec) -> bool:
        """检查包是否已安装（通过 import 测试）"""
        try:
            importlib.import_module(spec.import_name)
            return True
        except ImportError:
            return False

    def _install_group(self, packages: List[PackageSpec]) -> None:
        """安装一组包"""
        for spec in packages:
            self.result.total += 1
            self._install_one(spec)

    def _install_one(self, spec: PackageSpec) -> None:
        """安装单个包"""
        # 检查是否已安装
        if self._is_package_installed(spec):
            self.result.skipped += 1
            print(t("cli.messages.install_status_installed",
                    pip_name=spec.pip_name, desc=spec.description))
            return

        # 执行安装
        print(t("cli.messages.install_status_installing",
                pip_name=spec.pip_name, desc=spec.description))
        cmd = [sys.executable, "-m", "pip", "install", spec.pip_name]
        if not self.verbose:
            cmd.append("--quiet")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, shell=False,
            )
            if result.returncode == 0:
                self.result.installed += 1
                print(t("cli.messages.install_status_success", pip_name=spec.pip_name))
            else:
                self.result.failed += 1
                self.result.failed_packages.append(spec.pip_name)
                err_msg = result.stderr.strip().split(
                    "\n")[-1] if result.stderr else t("cli.messages.install_unknown_error")
                print(t("cli.messages.install_status_failed",
                        pip_name=spec.pip_name, err_msg=err_msg))
        except subprocess.TimeoutExpired:
            self.result.failed += 1
            self.result.failed_packages.append(spec.pip_name)
            print(t("cli.messages.install_status_timeout", pip_name=spec.pip_name))
        except Exception as e:
            self.result.failed += 1
            self.result.failed_packages.append(spec.pip_name)
            print(t("cli.messages.install_status_exception",
                    pip_name=spec.pip_name, err_type=type(e).__name__))

    def _install_languages_by_filter(self, languages: Set[str]) -> None:
        """按语言过滤安装 grammar"""
        all_langs = (SUPPORTED_LANGUAGE_PACKAGES + EXTENDED_LANGUAGE_PACKAGES
                     + P1_LANGUAGE_PACKAGES + P2_LANGUAGE_PACKAGES + P3_LANGUAGE_PACKAGES)
        filtered = [p for p in all_langs if p.language in languages]
        if not filtered:
            print(t("cli.messages.install_no_matching_lang", langs=languages))
            print(t("cli.messages.install_supported_langs",
                    langs=', '.join(p.language for p in all_langs)))
            return
        print(t("cli.messages.install_langs_filter",
                langs=', '.join(languages)))
        self._install_group(filtered)

    def _check_group(self, packages: List[PackageSpec]) -> None:
        """检查一组包的安装状态"""
        for spec in packages:
            installed = self._is_package_installed(spec)
            status = t("cli.messages.install_check_ok") if installed else t(
                "cli.messages.install_check_miss")
            lang_tag = f" ({spec.language})" if spec.language else ""
            print(t("cli.messages.install_check_item",
                    status=status, pip_name=spec.pip_name, desc=spec.description, lang_tag=lang_tag))

    def _print_summary(self) -> None:
        """打印安装汇总"""
        print("=" * 60)
        print(t("cli.messages.install_summary_title"))
        print("=" * 60)
        print(t("cli.messages.install_summary_total", total=self.result.total))
        print(t("cli.messages.install_summary_installed",
              installed=self.result.installed))
        print(t("cli.messages.install_summary_skipped", skipped=self.result.skipped))
        print(t("cli.messages.install_summary_failed", failed=self.result.failed))
        if self.result.failed_packages:
            print(t("cli.messages.install_summary_failed_packages",
                    packages=', '.join(self.result.failed_packages)))
        print()

        if self.result.failed == 0:
            print(t("cli.messages.install_all_success"))
            print()
            print(t("cli.messages.install_next_steps"))
            print(t("cli.messages.install_next_step_1"))
            print(t("cli.messages.install_next_step_2"))
            print(t("cli.messages.install_next_step_3"))
        else:
            print(t("cli.messages.install_partial_failure",
                  failed=self.result.failed))
            print(t("cli.messages.install_manual_install_hint"))
            print(t("cli.messages.install_retry_hint"))
        print()


# ---------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------

def main():
    """CLI 入口：解析参数并执行安装"""
    import argparse

    parser = argparse.ArgumentParser(
        description=t("cli.messages.install_arg_description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("cli.messages.install_arg_epilog"),
    )
    parser.add_argument("--all", action="store_true",
                        help=t("cli.args.install_all"))
    parser.add_argument("--lang", nargs="+", metavar="LANG",
                        help=t("cli.args.install_lang"))
    parser.add_argument("--check", action="store_true",
                        help=t("cli.args.install_check"))
    parser.add_argument("--hooks", action="store_true",
                        help=t("cli.args.install_hooks"))
    parser.add_argument("--force-hooks", action="store_true",
                        help=t("cli.args.install_force_hooks"))
    parser.add_argument("--no-post-commit", action="store_true",
                        help=t("cli.args.install_no_post_commit"))
    parser.add_argument("--no-optional", action="store_true",
                        help=t("cli.args.install_no_optional"))
    parser.add_argument("--verbose", action="store_true",
                        help=t("cli.args.install_verbose"))
    parser.add_argument("--agent", action="store_true",
                        help=t("cli.args.install_agent",
                               default="Auto-detect and install Call Warden MCP integration for installed AI agents"))
    parser.add_argument("--detect-agents", action="store_true",
                        help=t("cli.args.install_detect_agents",
                               default="Only detect installed AI agents, do not install"))
    parser.add_argument("--force-agent", action="store_true",
                        help=t("cli.args.install_force_agent",
                               default="Force overwrite existing agent MCP configs"))
    parser.add_argument("--agent-project", action="store_true",
                        help=t("cli.args.install_agent_project",
                               default="Install agent integration at project level instead of global"))

    args = parser.parse_args()

    installer = CallWardenInstaller(verbose=args.verbose)

    if args.check:
        installer.check_status()
        return

    if args.detect_agents:
        agents = installer.detect_installed_agents()
        installer.print_detected_agents(agents)
        return

    if args.hooks:
        installer.install_hooks(
            force=args.force_hooks,
            with_post_commit=not args.no_post_commit,
        )
        return

    if args.lang:
        # 仅安装指定语言
        installer.install_all(languages_only=set(args.lang))
    else:
        # 完整安装（默认包含全量依赖，除非指定 --no-optional）
        include_optional = not args.no_optional
        installer.install_all(include_optional=include_optional)

    # 自动安装 Agent 集成（--agent flag）
    if args.agent:
        print()
        agents = installer.detect_installed_agents()
        installer.print_detected_agents(agents)
        if agents:
            installer.install_agent_integrations(
                agents=agents,
                global_mode=not args.agent_project,
                force=args.force_agent,
                auto_detect=True,
            )

    # 退出码
    sys.exit(0 if installer.result.failed == 0 else 1)


if __name__ == "__main__":
    main()
