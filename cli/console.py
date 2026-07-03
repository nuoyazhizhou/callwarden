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


def should_use_color() -> bool:
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
    if not should_use_color():
        return text
    code = _COLORS.get(color, "")
    if not code:
        return text
    return f"{code}{text}{_COLORS['reset']}"


def cprint(text: str = "", color: Optional[str] = None, bold: bool = False, **kwargs):
    if bold:
        text = colorize(text, "bold")
    if color:
        text = colorize(text, color)
    print(text, **kwargs)


def success(msg: str) -> str:
    return colorize(f"✓ {msg}", "green")


def error(msg: str) -> str:
    return colorize(f"✗ {msg}", "red")


def warning(msg: str) -> str:
    return colorize(f"⚠ {msg}", "yellow")


def info(msg: str) -> str:
    return colorize(f"ℹ {msg}", "cyan")


def dim(msg: str) -> str:
    return colorize(msg, "dim")


def bold(msg: str) -> str:
    return colorize(msg, "bold")


_progress_active = [False]
_last_line_len = [0]


def print_progress(current: int, total: int, message: str = ""):
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
    global _progress_active, _last_line_len
    if should_use_color() and _progress_active[0]:
        sys.stdout.write("\r" + " " * _last_line_len[0] + "\r")
        sys.stdout.flush()
        _progress_active[0] = False
        _last_line_len[0] = 0


def format_duration(seconds: float) -> str:
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
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


def print_build_summary(parsed: int, unchanged: int, skipped: int, failed: int,
                        symbols: int, calls: int, resolved_calls: int, duration: float):
    print()
    cprint(f"  ═══════ 构建总结 ═══════", "cyan", bold=True)
    print()
    cprint(f"  文件:", "bold")
    if parsed:
        cprint(f"    新增/更新: {parsed}", "green")
    if unchanged:
        cprint(f"    未变化跳过: {unchanged}", "dim")
    if skipped:
        cprint(f"    不支持跳过: {skipped}", "dim")
    if failed:
        cprint(f"    解析失败: {failed}", "red")
    print()
    cprint(f"  图谱:", "bold")
    cprint(f"    符号总数: {symbols:,}", "bright_cyan")
    if calls:
        rate = f"({resolved_calls / calls * 100:.1f}% 已解析)" if calls else ""
        cprint(f"    调用关系: {calls:,} {rate}", "bright_cyan")
    print()
    cprint(f"  耗时: {format_duration(duration)}", "yellow")
    if failed:
        cprint(f"  ⚠ {failed} 个文件解析失败，已跳过", "yellow")
    else:
        cprint(f"  ✓ 构建完成", "green")
    print()


class Spinner:
    def __init__(self, message: str = ""):
        self.message = message
        self._frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._idx = 0
        self._start = 0
        self._active = False

    def start(self):
        self._active = True
        self._start = time.time()
        if not should_use_color():
            print(f"  {self.message}...")
            self._active = False
            return
        self._render()

    def _render(self):
        if not self._active:
            return
        frame = self._frames[self._idx % len(self._frames)]
        elapsed = time.time() - self._start
        line = f"  {frame} {self.message} ({elapsed:.1f}s)"
        sys.stdout.write(f"\r{line}   ")
        sys.stdout.flush()
        self._idx += 1

    def tick(self):
        if self._active:
            self._render()

    def stop(self, final_msg: Optional[str] = None):
        if not self._active:
            return
        self._active = False
        elapsed = time.time() - self._start
        if final_msg:
            sys.stdout.write(f"\r  {final_msg} ({elapsed:.1f}s)   \n")
        else:
            sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
