#!/usr/bin/env bash
# 在 Linux root 环境执行真实 SO_PEERCRED + SCM_RIGHTS 双 UID 验收。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: dual UID acceptance requires root" >&2
    exit 2
fi

if ! python3 -c "import callwarden_core, pytest" >/dev/null 2>&1; then
    echo "error: install callwarden_core and pytest for this Python first" >&2
    exit 3
fi
cd "$ROOT_DIR"
python3 -m pytest tests/test_enterprise_daemon_uds.py -q
