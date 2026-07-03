"""
db_lsp.py
========

LSP 集成 Mixin。

通过 Language Server Protocol 获取语义信息，补充 tree-sitter 的静态分析。
支持 Python（pylsp）/ TypeScript（typescript-language-server）/ Go（gopls）。

设计原则：
- LSP 是即时查询，不持久化（结果用于增强现有图谱，不单独存储）
- LSP 服务器按需启动，进程池管理（避免每次查询都启动新进程）
- 优雅降级：LSP 不可用时返回空结果，不影响主流程

SEC-002 安全加固：
- 所有 subprocess 调用强制 shell=False（默认即如此，显式声明）
- file_path 参数经 _validate_file_path 校验，拒绝 shell 元字符和目录遍历
- LSP 进程有最大存活时间（LSP_PROCESS_MAX_LIFETIME），超时自动重启
- Linux 平台通过 preexec_fn 限制子进程资源（CPU/内存）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional


# SEC-002: 危险的 shell 元字符正则（出现即拒绝）
_SHELL_META_PATTERN = re.compile(r'[;|&$`()\n\r]')

# SEC-002: LSP 进程最大存活时间（秒），超时自动重启避免僵尸进程
LSP_PROCESS_MAX_LIFETIME = 600  # 10 分钟


class LspMixin:
    """LSP 集成 Mixin

    提供 hover / definition / references / diagnostics / completion 等语义查询能力。
    LSP 服务器通过 subprocess 启动，用 JSON-RPC over stdio 通信。

    依赖：
    - 可执行文件：pylsp / typescript-language-server / gopls（按需启动）
    - 不需要数据库表（LSP 结果是即时查询）
    """

    # LSP 服务器配置（按语言）
    _LSP_SERVERS = {
        "python": {
            "command": "pylsp",
            "args": [],
            "init_options": {},
        },
        "typescript": {
            "command": "typescript-language-server",
            "args": ["--stdio"],
            "init_options": {},
        },
        "go": {
            "command": "gopls",
            "args": ["serve"],
            "init_options": {},
        },
        "rust": {
            "command": "rust-analyzer",
            "args": [],
            "init_options": {},
        },
    }

    # LSP 服务器进程缓存（language -> {"proc": Popen, "started_at": float}）
    _lsp_processes: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # SEC-002 安全加固方法
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_file_path(file_path: str) -> bool:
        """SEC-002: 校验文件路径安全性

        拒绝以下危险输入：
        1. 包含 shell 元字符（; | & $ ` () 换行）的路径
        2. 包含 .. 的路径（防止目录遍历）
        3. 空路径或非字符串

        Args:
            file_path: 待校验的文件路径

        Returns:
            True 表示路径安全，False 表示危险
        """
        if not file_path or not isinstance(file_path, str):
            return False
        # 拒绝 shell 元字符
        if _SHELL_META_PATTERN.search(file_path):
            return False
        # 拒绝目录遍历（.. 可能导致读取项目外的文件）
        # 标准化路径后检查是否包含 ..
        normalized = os.path.normpath(file_path)
        if ".." in normalized.split(os.sep):
            return False
        return True

    @staticmethod
    def _set_subprocess_resource_limits():
        """SEC-002: 设置子进程资源限制（仅 Linux/Mac，Windows 不支持）

        作为 preexec_fn 传入 subprocess.Popen，在子进程 fork 后、exec 前执行。
        限制 CPU 时间 60 秒、虚拟内存 2GB，防止恶意 LSP 卡死主进程。
        """
        try:
            import resource
            # CPU 时间限制 60 秒（软限制）
            resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
            # 虚拟内存限制 2GB（软限制）
            resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 * 1024 * 1024, 2 * 1024 * 1024 * 1024))
        except (ImportError, OSError, ValueError):
            # Windows 无 resource 模块或设置失败，忽略
            pass

    def lsp_hover(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """获取符号的 hover 信息（类型签名、文档注释等）

        Args:
            file_path: 文件绝对路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "file_path": str,
                "line": int,
                "character": int,
                "contents": str,       -- hover 文本内容
                "available": bool,     -- LSP 是否可用
            }
        """
        response = self._lsp_request(
            file_path, "textDocument/hover",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line, "character": character},
            },
        )
        if response is None:
            return {
                "file_path": file_path,
                "line": line,
                "character": character,
                "contents": "",
                "available": False,
            }

        # 解析 hover 响应
        contents = ""
        if isinstance(response, dict):
            result = response.get("result")
            if result:
                if "contents" in result:
                    c = result["contents"]
                    if isinstance(c, str):
                        contents = c
                    elif isinstance(c, dict):
                        contents = c.get("value", "")
                    elif isinstance(c, list):
                        contents = "\n".join(
                            item.get("value", "") if isinstance(item, dict) else str(item)
                            for item in c
                        )
        return {
            "file_path": file_path,
            "line": line,
            "character": character,
            "contents": contents,
            "available": True,
        }

    def lsp_definition(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """跳转到定义

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "definitions": [
                    {"uri": str, "line": int, "character": int, "file_path": str}
                ],
                "available": bool,
            }
        """
        response = self._lsp_request(
            file_path, "textDocument/definition",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line, "character": character},
            },
        )
        if response is None:
            return {"definitions": [], "available": False}

        result = response.get("result") if isinstance(response, dict) else None
        definitions: List[Dict[str, Any]] = []

        if result:
            # result 可能是单个 Location 或 Location[]
            locations = result if isinstance(result, list) else [result]
            for loc in locations:
                if isinstance(loc, dict) and "uri" in loc:
                    uri = loc["uri"]
                    range_info = loc.get("range", {})
                    start = range_info.get("start", {})
                    definitions.append({
                        "uri": uri,
                        "file_path": self._uri_to_path(uri),
                        "line": start.get("line", 0),
                        "character": start.get("character", 0),
                    })

        return {"definitions": definitions, "available": True}

    def lsp_references(
        self,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
    ) -> Dict[str, Any]:
        """查找引用

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）
            include_declaration: 是否包含定义本身

        Returns:
            {
                "references": [
                    {"uri": str, "line": int, "character": int, "file_path": str}
                ],
                "total": int,
                "available": bool,
            }
        """
        response = self._lsp_request(
            file_path, "textDocument/references",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": include_declaration},
            },
        )
        if response is None:
            return {"references": [], "total": 0, "available": False}

        result = response.get("result") if isinstance(response, dict) else None
        references: List[Dict[str, Any]] = []

        if result and isinstance(result, list):
            for loc in result:
                if isinstance(loc, dict) and "uri" in loc:
                    uri = loc["uri"]
                    start = loc.get("range", {}).get("start", {})
                    references.append({
                        "uri": uri,
                        "file_path": self._uri_to_path(uri),
                        "line": start.get("line", 0),
                        "character": start.get("character", 0),
                    })

        return {
            "references": references,
            "total": len(references),
            "available": True,
        }

    def lsp_diagnostics(self, file_path: str) -> Dict[str, Any]:
        """获取文件诊断信息（错误、警告）

        注意：LSP 的诊断是推送式的（textDocument/publishDiagnostics），
        此方法发送 didOpen 后等待短暂时间收集诊断。

        Args:
            file_path: 文件路径

        Returns:
            {
                "file_path": str,
                "diagnostics": [
                    {
                        "line": int,
                        "character": int,
                        "message": str,
                        "severity": int,  -- 1=Error, 2=Warning, 3=Info, 4=Hint
                        "source": str,
                    }
                ],
                "total": int,
                "available": bool,
            }
        """
        # SEC-002: 路径安全校验，拒绝恶意输入
        if not self._validate_file_path(file_path):
            return {
                "file_path": file_path,
                "diagnostics": [],
                "total": 0,
                "available": False,
            }

        # 简化实现：用 didOpen 触发诊断，然后读取缓存
        # 完整实现需要处理 notification 消息流
        lang = self._detect_language_from_file(file_path)
        if not lang:
            return {
                "file_path": file_path,
                "diagnostics": [],
                "total": 0,
                "available": False,
            }

        # 读取文件内容
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            return {
                "file_path": file_path,
                "diagnostics": [],
                "total": 0,
                "available": False,
            }

        proc = self._get_lsp_process(file_path, lang)
        if not proc:
            return {
                "file_path": file_path,
                "diagnostics": [],
                "total": 0,
                "available": False,
            }

        # 发送 didOpen
        uri = self._path_to_uri(file_path)
        self._send_notification(proc, "textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": lang,
                "version": 1,
                "text": content,
            }
        })

        # 等待诊断推送（LSP 会异步推送 publishDiagnostics）
        time.sleep(0.5)
        diagnostics = self._read_diagnostics(proc, uri)

        return {
            "file_path": file_path,
            "diagnostics": diagnostics,
            "total": len(diagnostics),
            "available": True,
        }

    def lsp_completion(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """获取补全建议

        Args:
            file_path: 文件路径
            line: 行号（0-based）
            character: 列号（0-based）

        Returns:
            {
                "completions": [
                    {"label": str, "kind": int, "detail": str}
                ],
                "total": int,
                "available": bool,
            }
        """
        response = self._lsp_request(
            file_path, "textDocument/completion",
            {
                "textDocument": {"uri": self._path_to_uri(file_path)},
                "position": {"line": line, "character": character},
            },
        )
        if response is None:
            return {"completions": [], "total": 0, "available": False}

        result = response.get("result") if isinstance(response, dict) else None
        completions: List[Dict[str, Any]] = []

        if result:
            items = result if isinstance(result, list) else result.get("items", [])
            for item in items:
                completions.append({
                    "label": item.get("label", ""),
                    "kind": item.get("kind", 0),
                    "detail": item.get("detail", ""),
                })

        return {
            "completions": completions,
            "total": len(completions),
            "available": True,
        }

    def lsp_check_available(self, language: str = "") -> Dict[str, Any]:
        """检查 LSP 服务器是否可用

        Args:
            language: 语言（python/typescript/go/rust），为空则检查所有

        Returns:
            {
                "available_servers": {"python": True, "typescript": False, ...},
                "total_available": int,
            }
        """
        languages = [language] if language else list(self._LSP_SERVERS.keys())
        available: Dict[str, bool] = {}

        for lang in languages:
            config = self._LSP_SERVERS.get(lang)
            if not config:
                available[lang] = False
                continue
            # 检查可执行文件是否存在
            try:
                result = subprocess.run(
                    ["where" if os.name == "nt" else "which", config["command"]],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                available[lang] = result.returncode == 0
            except Exception:
                available[lang] = False

        return {
            "available_servers": available,
            "total_available": sum(1 for v in available.values() if v),
        }

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _path_to_uri(file_path: str) -> str:
        """将文件路径转换为 file:// URI"""
        abs_path = os.path.abspath(file_path).replace("\\", "/")
        if not abs_path.startswith("/"):
            abs_path = "/" + abs_path
        return f"file://{abs_path}"

    @staticmethod
    def _uri_to_path(uri: str) -> str:
        """将 file:// URI 转换为文件路径"""
        if uri.startswith("file://"):
            path = uri[7:]
            if os.name == "nt" and path.startswith("/"):
                path = path[1:]
            return path.replace("/", os.sep)
        return uri

    def _detect_language_from_file(self, file_path: str) -> str:
        """从文件路径推断语言"""
        ext = os.path.splitext(file_path)[1].lower()
        ext_map = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "typescript",
            ".jsx": "typescript",
            ".go": "go",
            ".rs": "rust",
        }
        return ext_map.get(ext, "")

    def _get_lsp_process(self, file_path: str, language: str = "") -> Optional[Any]:
        """获取或启动 LSP 服务器进程

        进程缓存：同一语言的 LSP 服务器只启动一次，后续复用。
        SEC-002: 进程超过 LSP_PROCESS_MAX_LIFETIME 自动重启，避免僵尸进程。
        """
        if not language:
            language = self._detect_language_from_file(file_path)
        if not language:
            return None

        config = self._LSP_SERVERS.get(language)
        if not config:
            return None

        # 检查缓存
        if language in self._lsp_processes:
            cached = self._lsp_processes[language]
            proc = cached["proc"]
            started_at = cached["started_at"]
            # 进程仍在运行
            if proc.poll() is None:
                # SEC-002: 检查进程是否超时（超过最大存活时间则重启）
                if time.time() - started_at < LSP_PROCESS_MAX_LIFETIME:
                    return proc
                # 超时，终止旧进程
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                # 继续启动新进程
            # 进程已退出，移除缓存
            del self._lsp_processes[language]

        # 启动 LSP 服务器
        try:
            # SEC-002: 显式 shell=False + Linux 资源限制
            preexec_fn = None
            if os.name != "nt":
                # Linux/Mac 平台限制子进程资源
                preexec_fn = self._set_subprocess_resource_limits

            proc = subprocess.Popen(
                [config["command"]] + config["args"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                shell=False,  # SEC-002: 显式声明，防止 shell 注入
                preexec_fn=preexec_fn,  # SEC-002: 资源限制（仅 Linux）
            )
            # 发送 initialize 请求
            self._send_request(proc, "initialize", {
                "processId": os.getpid(),
                "rootUri": self._path_to_uri(os.path.dirname(file_path)),
                "capabilities": {},
                "initializationOptions": config.get("init_options", {}),
            })

            # 发送 initialized 通知
            self._send_notification(proc, "initialized", {})

            # SEC-002: 记录启动时间，用于超时重启判断
            self._lsp_processes[language] = {
                "proc": proc,
                "started_at": time.time(),
            }
            return proc
        except FileNotFoundError:
            # LSP 服务器未安装
            return None
        except Exception:
            return None

    def _lsp_request(
        self,
        file_path: str,
        method: str,
        params: Dict[str, Any],
        timeout: float = 5.0,
    ) -> Optional[Dict[str, Any]]:
        """发送 LSP 请求并等待响应

        Args:
            file_path: 文件路径（用于确定语言和启动 LSP）
            method: LSP 方法名（如 textDocument/hover）
            params: 请求参数
            timeout: 超时时间（秒）

        Returns:
            响应字典，LSP 不可用时返回 None
        """
        # SEC-002: 路径安全校验，拒绝恶意输入
        if not self._validate_file_path(file_path):
            return None

        lang = self._detect_language_from_file(file_path)
        proc = self._get_lsp_process(file_path, lang)
        if not proc:
            return None

        # 确保 didOpen 已发送（LSP 要求文件先打开才能查询）
        uri = params.get("textDocument", {}).get("uri", "")
        if uri:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                self._send_notification(proc, "textDocument/didOpen", {
                    "textDocument": {
                        "uri": uri,
                        "languageId": lang,
                        "version": 1,
                        "text": content,
                    }
                })
            except Exception:
                pass

        return self._send_request(proc, method, params, timeout)

    def _send_request(
        self,
        proc: Any,
        method: str,
        params: Dict[str, Any],
        timeout: float = 5.0,
    ) -> Optional[Dict[str, Any]]:
        """发送 JSON-RPC 请求并等待响应"""
        if not hasattr(self, "_lsp_request_id"):
            self._lsp_request_id = 0
        self._lsp_request_id += 1

        message = {
            "jsonrpc": "2.0",
            "id": self._lsp_request_id,
            "method": method,
            "params": params,
        }
        message_str = json.dumps(message)
        content = f"Content-Length: {len(message_str)}\r\n\r\n{message_str}"

        try:
            proc.stdin.write(content)
            proc.stdin.flush()

            # 读取响应（带超时）
            start_time = time.time()
            while time.time() - start_time < timeout:
                # 读取 Content-Length header
                header = ""
                while True:
                    char = proc.stdout.read(1)
                    if not char:
                        break
                    header += char
                    if header.endswith("\r\n\r\n"):
                        break

                # 解析 Content-Length
                length = 0
                for line in header.split("\r\n"):
                    if line.startswith("Content-Length:"):
                        length = int(line.split(":")[1].strip())
                        break

                if length == 0:
                    continue

                # 读取响应体
                body = proc.stdout.read(length)
                if not body:
                    continue

                response = json.loads(body)
                # 检查是否是我们请求的响应（id 匹配）
                if response.get("id") == self._lsp_request_id:
                    return response
                # 如果是 notification，跳过继续等待

        except Exception:
            return None

        return None

    def _send_notification(
        self,
        proc: Any,
        method: str,
        params: Dict[str, Any],
    ) -> None:
        """发送 JSON-RPC 通知（不等待响应）"""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        message_str = json.dumps(message)
        content = f"Content-Length: {len(message_str)}\r\n\r\n{message_str}"

        try:
            proc.stdin.write(content)
            proc.stdin.flush()
        except Exception:
            pass

    def _read_diagnostics(
        self,
        proc: Any,
        uri: str,
    ) -> List[Dict[str, Any]]:
        """读取诊断推送消息"""
        diagnostics: List[Dict[str, Any]] = []
        try:
            # 非阻塞读取所有可用数据
            import select
            import sys

            if os.name == "nt":
                # Windows 不支持 select on pipe，用超时读取
                proc.stdout.flush()
                # 简化：直接尝试读取
                while True:
                    header = ""
                    while True:
                        char = proc.stdout.read(1)
                        if not char:
                            return diagnostics
                        header += char
                        if header.endswith("\r\n\r\n"):
                            break

                    length = 0
                    for line in header.split("\r\n"):
                        if line.startswith("Content-Length:"):
                            length = int(line.split(":")[1].strip())
                            break

                    if length == 0:
                        continue

                    body = proc.stdout.read(length)
                    if not body:
                        continue

                    msg = json.loads(body)
                    if msg.get("method") == "textDocument/publishDiagnostics":
                        params = msg.get("params", {})
                        if params.get("uri") == uri:
                            for d in params.get("diagnostics", []):
                                start = d.get("range", {}).get("start", {})
                                diagnostics.append({
                                    "line": start.get("line", 0),
                                    "character": start.get("character", 0),
                                    "message": d.get("message", ""),
                                    "severity": d.get("severity", 0),
                                    "source": d.get("source", ""),
                                })
                            break  # 只读取一次诊断
            else:
                # Unix: 用 select 检查可读
                while select.select([proc.stdout], [], [], 0.1)[0]:
                    header = ""
                    while True:
                        char = proc.stdout.read(1)
                        if not char:
                            break
                        header += char
                        if header.endswith("\r\n\r\n"):
                            break

                    length = 0
                    for line in header.split("\r\n"):
                        if line.startswith("Content-Length:"):
                            length = int(line.split(":")[1].strip())
                            break

                    if length == 0:
                        continue

                    body = proc.stdout.read(length)
                    msg = json.loads(body)
                    if msg.get("method") == "textDocument/publishDiagnostics":
                        params = msg.get("params", {})
                        if params.get("uri") == uri:
                            for d in params.get("diagnostics", []):
                                start = d.get("range", {}).get("start", {})
                                diagnostics.append({
                                    "line": start.get("line", 0),
                                    "character": start.get("character", 0),
                                    "message": d.get("message", ""),
                                    "severity": d.get("severity", 0),
                                    "source": d.get("source", ""),
                                })
                            return diagnostics
        except Exception:
            pass

        return diagnostics

    def lsp_shutdown(self) -> None:
        """关闭所有 LSP 服务器进程"""
        for lang, proc in list(self._lsp_processes.items()):
            try:
                self._send_request(proc, "shutdown", {})
                self._send_notification(proc, "exit", {})
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._lsp_processes.clear()
