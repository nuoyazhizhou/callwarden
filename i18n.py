"""
i18n.py
=======

多语言国际化支持模块。
"""

import json
import os
from typing import Dict, Optional

# 默认语言
DEFAULT_LANG = "zh_CN"

# 缓存已加载的语言资源
_lang_cache: Dict[str, Dict] = {}


def _get_i18n_dir() -> str:
    """获取 i18n 资源目录"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")


def set_language(lang: str):
    """设置当前语言

    Args:
        lang: 语言代码，如 "zh_CN", "en_US"
    """
    global _current_lang
    _current_lang = lang
    # 预加载
    _load_lang(lang)


def get_language() -> str:
    """获取当前语言"""
    return _current_lang


def _load_lang(lang: str) -> Dict:
    """加载指定语言的资源文件"""
    if lang in _lang_cache:
        return _lang_cache[lang]

    i18n_dir = _get_i18n_dir()
    lang_file = os.path.join(i18n_dir, f"{lang}.json")

    if not os.path.exists(lang_file):
        # 回退到默认语言
        if lang != DEFAULT_LANG:
            return _load_lang(DEFAULT_LANG)
        return {}

    try:
        with open(lang_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        _lang_cache[lang] = data
        return data
    except Exception:
        return {}


# 当前语言（初始化为默认）
_current_lang = DEFAULT_LANG


def t(key: str, default: Optional[str] = None, **kwargs) -> str:
    """翻译文本（支持占位符替换）

    Args:
        key: 点分隔的键路径，如 "cli.messages.done"
        default: 键不存在时的默认值，若为 None 则返回 key 本身
        **kwargs: 占位符参数，如 {name: "test"} 替换 {name}

    Returns:
        翻译后的文本，如果键不存在则返回 default 或 key 本身
    """
    data = _load_lang(_current_lang)

    parts = key.split(".")
    value = data
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        return default if default is not None else key

    if isinstance(value, str):
        try:
            return value.format(**kwargs)
        except (KeyError, ValueError):
            return value

    return str(value)


def get_arg_help(arg_name: str) -> str:
    """获取 CLI 参数的帮助文本"""
    return t(f"cli.args.{arg_name.replace('-', '_')}", default="")


def get_msg(msg_key: str, default: Optional[str] = None, **kwargs) -> str:
    """获取消息文本"""
    return t(f"cli.messages.{msg_key}", default=default, **kwargs)


def get_error(err_key: str, default: Optional[str] = None, **kwargs) -> str:
    """获取错误文本"""
    return t(f"errors.{err_key}", default=default, **kwargs)
