#!/usr/bin/env python3
"""llm_channel.py —— DeepSeek(OpenAI 兼容) LLM 通道,用于可理解性端到端测试(3=b)。

从仓库根 .env 读取 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL(DeepSeek 兼容 OpenAI Chat Completions),
用已装的 httpx 直连,不引入 openai SDK。

用途:让 LLM 只读某个 MCP 工具的 description+inputSchema(或 CLI --help),
在不给实现的前提下,判断它能否正确理解并生成合法调用参数。
"""
from __future__ import annotations
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_ENV_PATH = os.path.join(_REPO_ROOT, ".env")


def _load_env() -> Dict[str, str]:
    """极简 .env 解析(KEY=VALUE,忽略注释/空行,不覆盖已存在的 os.environ)。"""
    env: Dict[str, str] = {}
    if os.path.isfile(_ENV_PATH):
        for line in open(_ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class LLMChannel:
    def __init__(self):
        env = _load_env()
        self.api_key = os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY", "")
        self.base_url = (os.environ.get("OPENAI_BASE_URL") or env.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.model = os.environ.get("OPENAI_MODEL") or env.get("OPENAI_MODEL") or env.get("LLM_MODEL", "deepseek-chat")
        self.timeout = float(env.get("LLM_HTTP_TIMEOUT", "60"))
        if not self.api_key or not self.base_url:
            raise RuntimeError("缺少 OPENAI_API_KEY / OPENAI_BASE_URL(.env)")

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.0,
             max_tokens: int = 1024, json_mode: bool = False) -> Dict[str, Any]:
        """发一次 chat completion,返回 {content, latency_s, usage, raw}。"""
        url = self.base_url + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.time()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
        latency = time.time() - t0
        resp.raise_for_status()
        data = resp.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            pass
        return {
            "content": content,
            "latency_s": round(latency, 2),
            "usage": data.get("usage", {}),
            "model": data.get("model", self.model),
        }


def _selftest():
    ch = LLMChannel()
    print(f"base_url={ch.base_url} model={ch.model} key=***{ch.api_key[-4:] if len(ch.api_key) > 4 else ''}")
    r = ch.chat([{"role": "user", "content": "只回复两个字:通了"}], max_tokens=16)
    print(f"latency={r['latency_s']}s usage={r['usage']}")
    print(f"content={r['content']!r}")


if __name__ == "__main__":
    _selftest()
