"""版本同步验证器。

读取 release/version.toml 并验证所有版本引用一致。
构建时调用，不一致则失败。

用法：
    python release/version_sync.py          # 验证
    python release/version_sync.py --fix    # 修复不一致
"""

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "release" / "version.toml"


def _parse_toml_simple(text: str) -> dict:
    """Python < 3.11 且未安装 tomli 时的零依赖 TOML 解析降级实现。"""
    import re
    result: dict = {}
    cur_sec = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m_sec = re.match(r"^\[([a-zA-Z0-9_\-]+)\]$", line)
        if m_sec:
            cur_sec = m_sec.group(1)
            result.setdefault(cur_sec, {})
            continue
        m_kv = re.match(r"^([a-zA-Z0-9_\-]+)\s*=\s*(.+)$", line)
        if m_kv and cur_sec:
            k, v_str = m_kv.group(1), m_kv.group(2).strip()
            if v_str.startswith('"') and v_str.endswith('"'):
                v: Any = v_str[1:-1]
            elif v_str.startswith("[") and v_str.endswith("]"):
                v = re.findall(r'"([^"]*)"', v_str)
            elif v_str.isdigit():
                v = int(v_str)
            else:
                v = v_str
            result[cur_sec][k] = v
    return result


def load_version_toml() -> dict:
    """加载 release/version.toml。"""
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        import tomllib
        return tomllib.loads(content)
    except ImportError:
        try:
            import tomli
            return tomli.loads(content)
        except ImportError:
            return _parse_toml_simple(content)


def check_python_version(expected: str, fix: bool = False) -> bool:
    """检查 pyproject.toml 和 __init__.py 版本。"""
    pyproject = ROOT / "pyproject.toml"
    init_py = ROOT / "__init__.py"

    # pyproject.toml
    with open(pyproject, "r", encoding="utf-8") as f:
        content = f.read()
    if f'version = "{expected}"' not in content:
        if fix:
            import re
            content = re.sub(
                r'version = "[^"]*"',
                f'version = "{expected}"',
                content,
                count=1,
            )
            with open(pyproject, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  [FIXED] pyproject.toml -> {expected}")
        else:
            print(f"  [FAIL] pyproject.toml version mismatch (expected {expected})")
            return False
    else:
        print(f"  [OK] pyproject.toml = {expected}")

    # __init__.py
    with open(init_py, "r", encoding="utf-8") as f:
        content = f.read()
    if f'__version__ = "{expected}"' not in content:
        if fix:
            import re
            content = re.sub(
                r'__version__ = "[^"]*"',
                f'__version__ = "{expected}"',
                content,
            )
            with open(init_py, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  [FIXED] __init__.py -> {expected}")
        else:
            print(f"  [FAIL] __init__.py version mismatch (expected {expected})")
            return False
    else:
        print(f"  [OK] __init__.py = {expected}")

    return True


def check_rust_version(expected: str, fix: bool = False) -> bool:
    """检查 rust_ext/Cargo.toml 版本。"""
    cargo_toml = ROOT / "rust_ext" / "Cargo.toml"
    with open(cargo_toml, "r", encoding="utf-8") as f:
        content = f.read()
    if f'version = "{expected}"' not in content:
        if fix:
            import re
            content = re.sub(
                r'^version = "[^"]*"',
                f'version = "{expected}"',
                content,
                count=1,
                flags=re.MULTILINE,
            )
            with open(cargo_toml, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  [FIXED] Cargo.toml -> {expected}")
        else:
            print(f"  [FAIL] Cargo.toml version mismatch (expected {expected})")
            return False
    else:
        print(f"  [OK] Cargo.toml = {expected}")
    return True


def main():
    """主入口。"""
    fix = "--fix" in sys.argv
    data = load_version_toml()
    version = data["product"]["version"]
    abi = data["abi"]

    print(f"Call Warden {version}")
    print(f"ABI: parser={abi['parser']} snapshot={abi['snapshot']} "
          f"schema_registry={abi['schema_registry']} "
          f"schema_cas={abi['schema_cas']} schema_workspace={abi['schema_workspace']}")
    print()

    ok = True
    print("Version consistency:")
    ok &= check_python_version(version, fix)
    ok &= check_rust_version(version, fix)

    if ok:
        print("\n[PASS] All versions consistent")
    else:
        print("\n[FAIL] Version mismatch detected" + (" (use --fix to repair)" if not fix else ""))
        sys.exit(1)


if __name__ == "__main__":
    main()
