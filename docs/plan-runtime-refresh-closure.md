# Runtime Refresh 阶段收口脚本与 TOOLS 文档

## S1 脚本范围与安全停止规则

target_file: scripts/refresh_shared_runtime.ps1
验收标准：脚本仅匹配仓库路径下 cw-daemon.exe 和 cw.py server 进程，拒绝按名称杀任意 Python/daemon；TaskId 必须匹配 ^T-[0-9]+-[0-9a-z-]+$；独立 target 编译，构建失败不停止现有服务；失败时 fail-closed 并尝试恢复上一版本；禁止删除 callwarden.db/WAL/SHM。
测试命令：rg -n "Owned-Processes|Stop-Owned|TaskId.*notmatch" scripts/refresh_shared_runtime.ps1
result：脚本第 22-24 行强制 TaskId 正则校验；第 49-75 行 Owned-Processes 仅匹配仓库路径和 runtime 路径下的 cw-daemon/cw.py server/cw-bridge；第 77-90 行 Stop-Owned 拒绝停止当前脚本进程并使用 Wait-Process+Force 降级。

## S2 独立 Rust target 构建、产物校验与版本化安装

target_file: scripts/refresh_shared_runtime.ps1
验收标准：在 rust_ext/target/stage-refresh 独立编译 release；5 个产物（cw-daemon.exe/cw.exe/cw-client.exe/cw-agent.exe/cw-bridge.exe）SHA-256 记录到 evidence；安装到 ~/.callwarden/runtime/current，上一版本保留到 versions/ 用于回滚。
测试命令：rg -n "stage-refresh|cargo build|Get-FileHash|versions|current" scripts/refresh_shared_runtime.ps1
result：脚本第 38-41 行设置 TargetDir 默认 rust_ext/target/stage-refresh；evidence JSON 记录 5 个二进制 SHA-256；runtime/current 软链接切换，versions/ 保留 KeepVersions（默认 3）个历史版本。

## S3 daemon/bridge 启动、健康检查与失败回滚

target_file: scripts/refresh_shared_runtime.ps1
验收标准：安装后启动 cw-daemon.exe，等待 cw daemon ping 返回 exit_code=0；-StartBridge 探测 WSL 默认网关并启动 cw-bridge，固定写入 bridge.manifest.json/bridge.token/bridge.wsl.env；失败时恢复上一版本 daemon 二进制。
测试命令：rg -n "Ping|daemon ping|Resolve-BridgeEndpoint|bridge.manifest|rollback" scripts/refresh_shared_runtime.ps1
result：脚本第 104-116 行 Ping 函数设置 CW_DAEMON_ENDPOINT 和 CW_DAEMON_BIN 后调用 cw.py daemon ping；第 124-138 行 Resolve-BridgeEndpoint 通过 wsl.exe ip route 探测网关，回退 127.0.0.1:8456；evidence 记录 daemon pid=20096/bridge pid=65352，rollback=false。

## S4 MCP 重启边界、Windows/WSL bridge 配置和 TOOLS 文档

target_file: scripts/refresh_shared_runtime.ps1, TOOLS.md, server/daemon_autostart.py, server/mcp_server.py
验收标准：脚本不启动 stdio MCP Server（-RestartMcp 只停止仓库内 cw.py server，由 IDE 重连）；daemon_autostart.py 添加 _is_tcp_endpoint 检查防止 WSL/Linux 误将 bridge 端点当本地 daemon 启动；mcp_server.py 添加异步 daemon startup probe 避免阻塞 stdio 握手；TOOLS.md 第 9 节文档化脚本边界和 WSL venv 约定。
测试命令：rg -n "_is_tcp_endpoint|ensure_daemon_for_startup|_launch_daemon_startup_probe" server/daemon_autostart.py server/mcp_server.py; rg -n "阶段收口" TOOLS.md
result：daemon_autostart.py 第 120-124 行添加 TCP endpoint 检查；mcp_server.py 第 93-119 行添加 _start_daemon_for_mcp_startup 和 _launch_daemon_startup_probe 后台线程；TOOLS.md 添加 44 行第 9 节文档。

## S5 真实运行证据、smoke tests、hash 清单和审计归档

target_file: scripts/refresh_shared_runtime.ps1
验收标准：evidence JSON 记录 status=passed、git_head、runtime_version、5 个二进制 SHA-256、processes_before/after、smoke 测试结果、bridge 配置；smoke tests 包含 cw --version 和 cw daemon ping，exit_code 均为 0；evidence 写入 ~/.callwarden/runtime/evidence/ 目录。
测试命令：rg -n "Atomic-Json|EvidenceRoot|smoke|RunSmokeTests" scripts/refresh_shared_runtime.ps1
result：evidence 路径 C:\Users\wanpi\.callwarden\runtime\evidence\20260810-182125-06e9d4241898-f7e48086.json；status=passed；git_head=06e9d42418985221835682dc4aa055b4675b47f7；5 个二进制 SHA-256 已独立核验与当前安装文件一致；daemon ping exit_code=0；bridge tcp://172.30.16.1:8456；smoke tests 2 项全部通过。
