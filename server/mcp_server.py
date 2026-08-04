"""
mcp_server.py
=============

代码知识图谱 MCP 服务器。

提供 MCP 工具接口，支持多容器共享调用（通过共享数据库文件）。
部署方式：在宿主机安装一次，所有容器通过 $HOME 共享路径调用。
"""

import os
import sys
import time
from typing import Any, Dict, Optional

# 确保可以导入 callwarden 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcp.server.fastmcp import FastMCP
    HAS_FASTMCP = True
    _FASTMCP_IMPORT_ERROR = None
except ImportError as exc:
    HAS_FASTMCP = False
    _FASTMCP_IMPORT_ERROR = exc

from ..db import CodeGraphDB
from ..config import PROJECT_ROOT, get_project_db_path
from ..i18n import t



from ._mcp_common import get_db  # noqa: F401 (供外部模块复用)



def create_mcp_server():
    """创建 MCP 服务器实例（工具注册收敛到 server/tools 功能域模块）"""
    if not HAS_FASTMCP:
        message = t("cli.messages.mcp_server_fastmcp_not_installed")
        if _FASTMCP_IMPORT_ERROR is not None:
            message = f"{message}: {_FASTMCP_IMPORT_ERROR}"
        print(message, file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP("callwarden", dependencies=["callwarden"])

    from .tools import register_all
    register_all(mcp)

    return mcp



def _auto_sync_agents_md() -> Dict[str, Any]:
    """启动时自动同步 AGENTS.md（fail-soft，不阻断启动）

    把当前 active 的 Agent Rule Memory 同步到 AGENTS.md 标记区，
    让无 MCP 的 Agent 也能从 AGENTS.md 看到已生效规则。

    安全策略：
    - 同步失败不阻断 MCP Server 启动（fail-soft）
    - 使用 dry_run=False 实际写入文件，并记录 agent_rule_sync_log
    - 标记区不存在时静默跳过（不插入标记块，避免改写用户文件）
    - 所有输出走 stderr，不污染 stdio 协议

    Returns:
        dict: 同步结果摘要（含 success / rule_count / error 等字段）
    """
    try:
        db = get_db()
        result = db.rule_sync_agents_md(
            target_path="AGENTS.md",
            dry_run=False,
            actor="mcp_server_startup",
        )
        return result
    except Exception as exc:
        # fail-soft：任何异常都不阻断启动，仅记录错误
        return {
            "success": False,
            "dry_run": False,
            "target_path": "AGENTS.md",
            "rule_count": 0,
            "rule_ids": [],
            "before_hash": "",
            "after_hash": "",
            "error": str(exc),
        }


def _ensure_semgrep_rules_cache() -> None:
    """启动时检查 semgrep 规则缓存，缺失则后台异步预下载（非阻塞）

    semgrep --config p/default 首次调用会从 registry 下载规则到本地缓存
    （~/.cache/semgrep/ 或 %LOCALAPPDATA%\\semgrep\\），可能耗时数十秒。

    多用户场景查找顺序：
    1. 系统级共享缓存 /var/lib/callwarden/semgrep_rules/（root 预下载，只读）
       → 有则复制到用户级 ~/.cache/semgrep/（一次性复制，后续直接用）
    2. 用户级缓存 ~/.cache/semgrep/ 已存在 → 直接用
    3. 都没有 → 后台启动 semgrep --validate 下载到用户级

    安全策略：
    - 完全非阻塞：Popen 启动子进程 + 后台线程 wait(timeout) 监控
    - 超时杀进程：后台线程 120s 后 kill 子进程，避免网络卡住时子进程挂起
    - 异常分类处理：权限/目录/网络/未知异常分别给出明确提示
    - 所有输出走 stderr，不污染 stdio 协议
    - 仅检查 p/default 规则集（run_check_gate / run_semprep 默认配置）
    """
    import shutil
    import subprocess
    import threading

    # 导入共享缓存配置
    try:
        from config import SYSTEM_SEMGREP_RULES_DIR, is_system_cache_available
    except ImportError:
        SYSTEM_SEMGREP_RULES_DIR = ""
        is_system_cache_available = lambda: False

    semgrep_path = shutil.which("semgrep")
    if not semgrep_path:
        return  # semgrep 未安装，静默跳过

    # 检查缓存目录是否存在（semgrep 缓存路径因平台而异）
    # Linux/Mac: ~/.cache/semgrep/
    # Windows: %LOCALAPPDATA%\\semgrep\\
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "semgrep")
    if not os.path.isdir(cache_dir):
        win_cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "semgrep")
        if os.path.isdir(win_cache):
            cache_dir = win_cache  # Windows 缓存已存在，无需预下载

    # 用户级缓存已存在且非空，直接用（semgrep CLI 会自动管理缓存更新）
    if os.path.isdir(cache_dir) and os.listdir(cache_dir):
        return

    # 1. 检查系统级共享缓存是否可用（root 预下载）
    # 有则复制到用户级缓存（一次性操作，后续直接用用户级）
    if is_system_cache_available():
        print(
            t(
                "cli.messages.semgrep_rules_copy_from_system",
                default="[Semgrep] Copying rules from system cache to user cache...",
            ),
            file=sys.stderr,
        )
        try:
            os.makedirs(cache_dir, exist_ok=True)
            import shutil as _shutil
            for item in os.listdir(SYSTEM_SEMGREP_RULES_DIR):
                src = os.path.join(SYSTEM_SEMGREP_RULES_DIR, item)
                dst = os.path.join(cache_dir, item)
                if os.path.isdir(src):
                    _shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    _shutil.copy2(src, dst)
            print(
                t(
                    "cli.messages.semgrep_rules_copy_ok",
                    default="[Semgrep] Rules copied from system cache. Ready for scanning.",
                ),
                file=sys.stderr,
            )
            return  # 复制完成，无需下载
        except (OSError, PermissionError) as e:
            print(
                t(
                    "cli.messages.semgrep_rules_prefetch_skip",
                    default="[Semgrep] Failed to copy from system cache: {error}. Will download on first scan.",
                    error=str(e),
                ),
                file=sys.stderr,
            )
            # 复制失败，继续走下载流程

    # 检查缓存目录的父目录是否可写（semgrep 会创建缓存目录）
    cache_parent = os.path.dirname(cache_dir) or os.path.expanduser("~")
    try:
        if not os.path.isdir(cache_parent):
            print(
                t(
                    "cli.messages.semgrep_rules_prefetch_skip",
                    default="[Semgrep] Cache parent dir not exists: {dir}. Will download on first scan.",
                    dir=cache_parent,
                ),
                file=sys.stderr,
            )
            return
        # 检查写权限
        if not os.access(cache_parent, os.W_OK):
            print(
                t(
                    "cli.messages.semgrep_rules_prefetch_skip",
                    default="[Semgrep] No write permission for cache dir: {dir}. Will download on first scan.",
                    dir=cache_parent,
                ),
                file=sys.stderr,
            )
            return
    except (OSError, PermissionError) as e:
        print(
            t(
                "cli.messages.semgrep_rules_prefetch_skip",
                default="[Semgrep] Cache dir check failed: {error}. Will download on first scan.",
                error=str(e),
            ),
            file=sys.stderr,
        )
        return

    print(
        t(
            "cli.messages.semgrep_rules_prefetch",
            default="[Semgrep] Rule cache missing, starting background prefetch (non-blocking, 120s timeout)...",
        ),
        file=sys.stderr,
    )

    # 后台异步执行：Popen 启动子进程 + 后台线程监控超时
    # 主线程立即返回不等待，后台线程在 120s 后 kill 卡住的子进程
    try:
        proc = subprocess.Popen(
            [semgrep_path, "--config", "p/default", "--validate"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # Windows 下创建新进程组，便于 kill 整个进程树
            # Linux/Mac 下用 start_new_session=True 形成新会话
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError:
        print(
            t(
                "cli.messages.semgrep_rules_prefetch_skip",
                default="[Semgrep] semgrep binary not found. Will download on first scan.",
            ),
            file=sys.stderr,
        )
        return
    except PermissionError as e:
        print(
            t(
                "cli.messages.semgrep_rules_prefetch_skip",
                default="[Semgrep] Permission denied when launching semgrep: {error}. Will download on first scan.",
                error=str(e),
            ),
            file=sys.stderr,
        )
        return
    except OSError as e:
        print(
            t(
                "cli.messages.semgrep_rules_prefetch_skip",
                default="[Semgrep] OS error when launching semgrep: {error}. Will download on first scan.",
                error=str(e),
            ),
            file=sys.stderr,
        )
        return
    except Exception as e:
        # 兜底捕获所有未知异常，绝不阻塞 MCP Server
        print(
            t(
                "cli.messages.semgrep_rules_prefetch_skip",
                default="[Semgrep] Unexpected error when launching prefetch: {error}. Will download on first scan.",
                error=str(e),
            ),
            file=sys.stderr,
        )
        return

    # 后台线程监控子进程，超时则 kill（避免网络卡住时子进程无限挂起）
    def _monitor_timeout(p: subprocess.Popen, timeout: int = 120) -> None:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时杀进程：网络不通或 semgrep 卡住
            try:
                if os.name == "nt":
                    # Windows: taskkill 整个进程树
                    subprocess.run(
                        ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                        capture_output=True, timeout=10,
                    )
                else:
                    # Linux/Mac: kill 整个进程组
                    import signal
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                try:
                    p.kill()
                except (OSError, subprocess.SubprocessError):
                    pass
            print(
                t(
                    "cli.messages.semgrep_rules_prefetch_timeout",
                    default="[Semgrep] Background prefetch timed out ({timeout}s). Will retry on first scan.",
                    timeout=timeout,
                ),
                file=sys.stderr,
            )
        except (OSError, subprocess.SubprocessError):
            # 子进程已退出或异常，静默处理
            pass

    monitor_thread = threading.Thread(
        target=_monitor_timeout,
        args=(proc, 120),
        daemon=True,  # 守护线程，主进程退出时自动结束
    )
    monitor_thread.start()


def _print_auto_sync_summary(result: Dict[str, Any]) -> None:
    """打印 AGENTS.md 自动同步摘要到 stderr

    MCP Server 使用 stdio 传输协议，所有日志必须走 stderr，
    否则会污染协议输出导致 client 解析失败。

    Args:
        result: _auto_sync_agents_md() 返回的结果字典
    """
    if result.get("success"):
        count = result.get("rule_count", 0)
        print(
            t(
                "cli.messages.agents_md_auto_sync_success",
                count=count,
                default=f"[Auto Sync] AGENTS.md 已同步，共 {count} 条规则",
            ),
            file=sys.stderr,
        )
    else:
        error = result.get("error", "")
        # 标记区不存在时给出更友好的提示
        if "marker" in error.lower() or "not found" in error.lower():
            print(
                t(
                    "cli.messages.agents_md_auto_sync_no_marker",
                    default="[Auto Sync] AGENTS.md 标记区不存在，跳过同步。请先运行 `cw rule insert-block` 插入标记块。",
                ),
                file=sys.stderr,
            )
        else:
            print(
                t(
                    "cli.messages.agents_md_auto_sync_skipped",
                    error=error,
                    default=f"[Auto Sync] AGENTS.md 同步跳过：{error}",
                ),
                file=sys.stderr,
            )




def main():
    """MCP 服务器入口

    启动流程：
    1. create_mcp_server() 创建服务器实例并注册所有 MCP 工具
    2. --check-imports 模式完成注册后立即退出，不触发数据库写入和网络下载
    3. _auto_sync_agents_md() 自动同步 AGENTS.md（fail-soft，不阻断启动）
    4. _ensure_semgrep_rules_cache() 检查 semgrep 规则缓存（fail-soft）
    5. server.run() 启动 stdio 传输
    """
    server = create_mcp_server()
    if "--check-imports" in sys.argv[1:]:
        print("Call Warden MCP imports OK")
        return
    # 启动时自动同步 AGENTS.md（C2 新增）
    sync_result = _auto_sync_agents_md()
    _print_auto_sync_summary(sync_result)
    # 启动时检查 semgrep 规则缓存，缺失则预下载（避免首次扫描卡顿）
    _ensure_semgrep_rules_cache()
    server.run()


if __name__ == "__main__":
    main()

