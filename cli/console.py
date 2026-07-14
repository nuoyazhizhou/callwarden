"""
console.py
==========

CLI 输出工具：彩色文本、进度条、格式化统计。
支持 Windows 终端（自动启用 ANSI 支持）。
"""

import os
import sys
import time
from typing import Optional

from ..i18n import t


_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_cyan": "\033[96m",
}

_use_color = None


def _enable_vt_mode():
    """启用 Windows 终端的 VT100 虚拟终端处理模式

    Windows 默认不支持 ANSI 颜色转义序列，需要通过 kernel32 API
    显式启用 ENABLE_VIRTUAL_TERMINAL_PROCESSING 标志。

    Returns:
        True 表示启用成功或非 Windows 平台，False 表示启用失败
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        return True
    except Exception:
        return False


def ensure_utf8_output():
    """强制 stdout/stderr 使用 UTF-8 编码，避免 Windows GBK 控制台无法输出 Unicode 字符

    修复 Bug L11: CLI 输出含 ✓ ↳ 等 Unicode 字符，Windows GBK 控制台
    编码不了直接崩溃（UnicodeEncodeError）。在入口处统一设置 UTF-8 + replace 错误处理，
    确保任何字符都能输出（无法编码时用 ? 替代而非崩溃）。

    所有 CLI 入口（cw.py / __main__.py / cli/main.py:main()）都应调用此函数。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, TypeError):
                pass


def should_use_color() -> bool:
    """检测当前环境是否应该使用彩色输出

    判定规则（按优先级）：
    1. NO_COLOR 环境变量存在 → 禁用颜色
    2. stdout 非 TTY → 禁用颜色（如管道/重定向）
    3. FORCE_COLOR 环境变量存在 → 强制启用并激活 VT 模式
    4. 否则按 VT 模式启用结果决定

    结果会缓存到全局 _use_color，避免重复检测。

    Returns:
        True 表示启用彩色输出，False 表示禁用
    """
    global _use_color
    if _use_color is not None:
        return _use_color
    if os.environ.get("NO_COLOR"):
        _use_color = False
        return False
    if not sys.stdout.isatty():
        _use_color = False
        return False
    if os.environ.get("FORCE_COLOR"):
        _enable_vt_mode()
        _use_color = True
        return True
    _use_color = _enable_vt_mode()
    return _use_color


def colorize(text: str, color: str) -> str:
    """为文本添加 ANSI 颜色转义序列

    Args:
        text: 原始文本
        color: 颜色名称，对应 _COLORS 中的键（如 red/green/bold/dim）

    Returns:
        带颜色转义序列的文本，非 TTY 环境下直接返回原文本
    """
    if not should_use_color():
        return text
    code = _COLORS.get(color, "")
    if not code:
        return text
    return f"{code}{text}{_COLORS['reset']}"


def cprint(text: str = "", color: Optional[str] = None, bold: bool = False, **kwargs):
    """带颜色的 print 封装

    Args:
        text: 要输出的文本
        color: 前景色名称（red/green/blue 等）
        bold: 是否加粗
        **kwargs: 透传给 print 的额外参数（end/flush/file 等）
    """
    if bold:
        text = colorize(text, "bold")
    if color:
        text = colorize(text, color)
    print(text, **kwargs)


def success(msg: str) -> str:
    """生成成功消息（绿色 ✓ 前缀）

    Args:
        msg: 消息内容

    Returns:
        带绿色 ✓ 前缀的格式化字符串
    """
    return colorize(f"✓ {msg}", "green")


def error(msg: str) -> str:
    """生成错误消息（红色 ✗ 前缀）

    Args:
        msg: 消息内容

    Returns:
        带红色 ✗ 前缀的格式化字符串
    """
    return colorize(f"✗ {msg}", "red")


def warning(msg: str) -> str:
    """生成警告消息（黄色 ⚠ 前缀）

    Args:
        msg: 消息内容

    Returns:
        带黄色 ⚠ 前缀的格式化字符串
    """
    return colorize(f"⚠ {msg}", "yellow")


def info(msg: str) -> str:
    """生成信息消息（青色 ℹ 前缀）

    Args:
        msg: 消息内容

    Returns:
        带青色 ℹ 前缀的格式化字符串
    """
    return colorize(f"ℹ {msg}", "cyan")


def dim(msg: str) -> str:
    """生成暗色文本（低亮度）

    Args:
        msg: 消息内容

    Returns:
        低亮度的格式化字符串
    """
    return colorize(msg, "dim")


def bold(msg: str) -> str:
    """生成加粗文本

    Args:
        msg: 消息内容

    Returns:
        加粗的格式化字符串
    """
    return colorize(msg, "bold")


_progress_active = [False]
_last_line_len = [0]


def print_progress(current: int, total: int, message: str = ""):
    """显示进度条（TTY 下实时刷新，非 TTY 下按间隔打印）

    Args:
        current: 当前进度值
        total: 总进度值（<=0 时直接返回）
        message: 进度条右侧附加的消息文本
    """
    global _progress_active, _last_line_len
    if total <= 0:
        return
    is_tty = should_use_color()
    progress = min(current / total, 1.0)
    filled = int(28 * progress)
    bar = "█" * filled + "░" * (28 - filled)
    pct = int(progress * 100)
    line = f"  [{bar}] {pct:3d}% {current}/{total}  {message}"

    if is_tty:
        out = sys.stdout
        line_len = len(line)
        if _progress_active[0]:
            out.write("\r")
        else:
            _progress_active[0] = True
        padded = line + " " * max(0, _last_line_len[0] - line_len)
        out.write(padded)
        out.flush()
        _last_line_len[0] = line_len
        if current >= total:
            out.write("\n")
            out.flush()
            _progress_active[0] = False
            _last_line_len[0] = 0
    else:
        if current == 1 or current >= total or current % 50 == 0:
            print(line)


def clear_progress():
    """清除当前行的进度条显示（仅 TTY 环境有效）"""
    global _progress_active, _last_line_len
    if should_use_color() and _progress_active[0]:
        sys.stdout.write("\r" + " " * _last_line_len[0] + "\r")
        sys.stdout.flush()
        _progress_active[0] = False
        _last_line_len[0] = 0


def format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的时长字符串

    自动选择合适的单位：毫秒(ms) / 秒(s) / 分钟(m) / 小时(h)

    Args:
        seconds: 秒数（浮点数）

    Returns:
        格式化的时长字符串，如 "120ms", "3.5s", "2m30s", "1h15m"
    """
    if seconds < 0.001:
        return f"{seconds*1000:.1f}ms"
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds % 60
    if m < 60:
        return f"{m}m{s:.0f}s"
    h = int(m // 60)
    m = m % 60
    return f"{h}h{m}m"


def format_size(n: int) -> str:
    """将字节数格式化为人类可读的大小字符串

    Args:
        n: 字节数

    Returns:
        格式化的大小字符串，如 "128 B", "1.5 KB", "2.0 MB"
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


def print_build_summary(parsed: int, unchanged: int, skipped: int, failed: int,
                        symbols: int, calls: int, resolved_calls: int, duration: float):
    """打印构建完成后的总结报告

    Args:
        parsed: 成功解析的文件数（新增/更新）
        unchanged: 未变化而跳过的文件数
        skipped: 不支持的语言而跳过的文件数
        failed: 解析失败的文件数
        symbols: 符号总数
        calls: 调用关系总数
        resolved_calls: 已解析（成功匹配）的调用关系数
        duration: 构建总耗时（秒）
    """
    print()
    cprint(t("cli.messages.console_build_summary_title"), "cyan", bold=True)
    print()
    cprint(t("cli.messages.console_files_label"), "bold")
    if parsed:
        cprint(t("cli.messages.console_files_parsed", parsed=parsed), "green")
    if unchanged:
        cprint(t("cli.messages.console_files_unchanged", unchanged=unchanged), "dim")
    if skipped:
        cprint(t("cli.messages.console_files_skipped", skipped=skipped), "dim")
    if failed:
        cprint(t("cli.messages.console_files_failed", failed=failed), "red")
    print()
    cprint(t("cli.messages.console_graph_label"), "bold")
    cprint(t("cli.messages.console_symbols_total", symbols=symbols), "bright_cyan")
    if calls:
        pct = resolved_calls / calls * 100
        rate = t("cli.messages.console_resolved_rate", rate=f"{pct:.1f}")
        cprint(t("cli.messages.console_calls_total", calls=calls, rate=rate), "bright_cyan")
    print()
    cprint(t("cli.messages.console_duration", duration=format_duration(duration)), "yellow")
    if failed:
        cprint(t("cli.messages.console_build_failed_warning", failed=failed), "yellow")
    else:
        cprint(t("cli.messages.console_build_done"), "green")
    print()


class Spinner:
    """终端旋转加载动画（Braille 字符），用于耗时操作的视觉反馈

    TTY 环境下实时刷新动画，非 TTY 环境下仅打印一次起始消息。

    Attributes:
        message: 加载提示消息
    """

    def __init__(self, message: str = ""):
        """初始化旋转动画

        Args:
            message: 加载提示消息（如 "Building graph..."）
        """
        self.message = message
        self._frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._idx = 0
        self._start = 0
        self._active = False

    def start(self):
        """启动旋转动画"""
        self._active = True
        self._start = time.time()
        if not should_use_color():
            print(f"  {self.message}...")
            self._active = False
            return
        self._render()

    def _render(self):
        """渲染当前帧到终端（覆盖原行）"""
        if not self._active:
            return
        frame = self._frames[self._idx % len(self._frames)]
        elapsed = time.time() - self._start
        line = f"  {frame} {self.message} ({elapsed:.1f}s)"
        sys.stdout.write(f"\r{line}   ")
        sys.stdout.flush()
        self._idx += 1

    def tick(self):
        """推进一帧（由调用方在循环中调用）"""
        if self._active:
            self._render()

    def stop(self, final_msg: Optional[str] = None):
        """停止动画并清理行

        Args:
            final_msg: 完成后显示的最终消息（None 则清空行）
        """
        if not self._active:
            return
        self._active = False
        elapsed = time.time() - self._start
        if final_msg:
            sys.stdout.write(f"\r  {final_msg} ({elapsed:.1f}s)   \n")
        else:
            sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
