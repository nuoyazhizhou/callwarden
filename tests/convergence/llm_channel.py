#!/usr/bin/env python3
"""llm_channel.py —— DeepSeek(OpenAI 兼容)LLM 通道,用于 T5 可理解性端到端测试。

从仓库根 .env 读取 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL(DeepSeek 兼容
OpenAI Chat Completions),用 httpx 直连,不引入 openai SDK。

用途:让 LLM 只读 MCP 工具的 name+description(+params schema),在不给实现的
前提下判断它能否正确理解并选对工具/生成合法参数,评估工具文档可理解性。

无 API key(.env 缺失或未配)时构造抛 RuntimeError,T5 测试据此 skip。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

import httpx

# tests/convergence/ → 仓库根(两级 dirname)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_PATH = os.path.join(_REPO_ROOT, ".env")


def _load_env() -> Dict[str, str]:
    """极简 .env 解析(KEY=VALUE,忽略注释/空行)。"""
    env: Dict[str, str] = {}
    if os.path.isfile(_ENV_PATH):
        for line in open(_ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def llm_available() -> bool:
    """是否配置了可用的 LLM key(供 pytest skip 判定)。"""
    env = _load_env()
    key = os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL") or env.get("OPENAI_BASE_URL", "")
    return bool(key and base)


class LLMChannel:
    def __init__(self):
        env = _load_env()
        self.api_key = os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY", "")
        self.base_url = (os.environ.get("OPENAI_BASE_URL")
                         or env.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.model = (os.environ.get("OPENAI_MODEL") or env.get("OPENAI_MODEL")
                      or env.get("LLM_MODEL", "deepseek-chat"))
        self.timeout = float(env.get("LLM_HTTP_TIMEOUT", "60"))
        if not self.api_key or not self.base_url:
            raise RuntimeError("缺少 OPENAI_API_KEY / OPENAI_BASE_URL(.env)")

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.0,
             max_tokens: int = 1024, json_mode: bool = False) -> Dict[str, Any]:
        url = self.base_url + "/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
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
