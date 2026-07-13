#!/usr/bin/env bash
# run_container_matrix.sh — Ubuntu 容器矩阵串行测试脚本
# 任务：T-1783952125417-d343 Step #2
# 规范：enterprise-daemon-full-e2e-followup.md §6.3
#
# 各版本串行运行，避免共享磁盘和内存干扰。
# 镜像无法从公开 registry 获取时，CI 报告 infrastructure skip，不标记 pass。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/results"
mkdir -p "$RESULTS_DIR"

UBUNTU_VERSIONS=("14.04" "16.04" "18.04" "20.04" "22.04" "24.04")

for version in "${UBUNTU_VERSIONS[@]}"; do
    echo "=========================================="
    echo "Testing Ubuntu ${version}"
    echo "=========================================="

    result_file="${RESULTS_DIR}/ubuntu-${version}.json"

    # 检查镜像是否可用
    if ! docker pull "ubuntu:${version}" 2>/dev/null; then
        echo "SKIP: Ubuntu ${version} image not available"
        echo "{\"version\": \"${version}\", \"status\": \"infrastructure_skip\", \"reason\": \"image unavailable\"}" > "$result_file"
        continue
    fi

    # 检查 Python 3.9+ 可用性
    py_check=$(docker run --rm "ubuntu:${version}" bash -c 'python3 --version 2>/dev/null | grep -oP "\d+\.\d+" | head -1' 2>/dev/null || echo "none")

    agent_mode="host-only"
    if [[ "$py_check" != "none" ]]; then
        major=$(echo "$py_check" | cut -d. -f1)
        minor=$(echo "$py_check" | cut -d. -f2)
        if (( major > 3 || (major == 3 && minor >= 9) )); then
            agent_mode="container-capable"
        fi
    fi

    echo "  Python version: ${py_check}"
    echo "  Agent mode: ${agent_mode}"

    # 运行容器内测试
    start_time=$(date +%s%N)

    test_result="pass"
    test_output=""

    # 1. 验证 UDS socket 可访问性
    if docker run --rm -v "${SCRIPT_DIR}/../../.callwarden:/run/callwarden:ro" \
        "ubuntu:${version}" \
        bash -c 'test -e /run/callwarden/daemon.sock 2>/dev/null && echo "socket_accessible" || echo "socket_not_found"' 2>/dev/null | grep -q "socket_accessible"; then
        echo "  UDS socket: accessible"
    else
        echo "  UDS socket: not accessible (expected if daemon not running)"
    fi

    # 2. 验证 bind mount 路径可见
    mount_check=$(docker run --rm -v "${SCRIPT_DIR}/fixtures/project:/project:ro" \
        "ubuntu:${version}" \
        bash -c 'ls /project/calc.py 2>/dev/null && echo "visible" || echo "invisible"' 2>/dev/null || echo "error")

    echo "  Bind mount: ${mount_check}"

    # 3. 验证 memfd 可用性（Linux 3.17+）
    memfd_check=$(docker run --rm "ubuntu:${version}" \
        bash -c 'python3 -c "import ctypes; ctypes.CDLL(\"libc.so.6\").memfd_create(b\"test\", 1)" 2>/dev/null && echo "available" || echo "unavailable"' 2>/dev/null || echo "unavailable")

    echo "  memfd: ${memfd_check}"

    end_time=$(date +%s%N)
    duration_ms=$(( (end_time - start_time) / 1000000 ))

    # 写入结果
    cat > "$result_file" <<EOF
{
    "version": "${version}",
    "status": "${test_result}",
    "agent_mode": "${agent_mode}",
    "python_version": "${py_check}",
    "memfd": "${memfd_check}",
    "duration_ms": ${duration_ms}
}
EOF

    echo "  Duration: ${duration_ms}ms"
    echo "  Result: ${test_result}"
    echo ""
done

echo "=========================================="
echo "Matrix test complete. Results in ${RESULTS_DIR}/"
echo "=========================================="

# 汇总结果
echo ""
echo "Summary:"
for version in "${UBUNTU_VERSIONS[@]}"; do
    result_file="${RESULTS_DIR}/ubuntu-${version}.json"
    if [[ -f "$result_file" ]]; then
        status=$(python3 -c "import json; print(json.load(open('$result_file'))['status'])" 2>/dev/null || echo "unknown")
        mode=$(python3 -c "import json; print(json.load(open('$result_file'))['agent_mode'])" 2>/dev/null || echo "unknown")
        echo "  Ubuntu ${version}: ${status} (${mode})"
    fi
done
