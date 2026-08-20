#!/usr/bin/env pwsh

[CmdletBinding()]
param(
    # The task engine invokes this script automatically only for the
    # CallWarden self-bootstrap workspace (or an explicit deployment contract).
    # Ordinary projects should not call the deployment path implicitly.
    [Parameter(Mandatory = $true)]
    [string]$TaskId,
    [ValidateSet("debug", "release")]
    [string]$Configuration = "release",
    [switch]$RestartMcp,
    [switch]$StartBridge,
    [switch]$RestartBridge,
    [string]$BridgeEndpoint = "auto",
    [switch]$RunSmokeTests,
    [switch]$NoPersistDaemonPath,
    [string]$TargetDir = "",
    [int]$KeepVersions = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($TaskId -notmatch '^T-[0-9]+-[0-9a-z-]+$') {
    throw "TaskId 必须是实际任务 ID，例如 T-1786346158666-e9316534；禁止使用占位符"
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RustManifest = Join-Path $RepoRoot "rust_ext\Cargo.toml"
$PythonExe = "C:\Python314\python.exe"
$RuntimeRoot = Join-Path $env:USERPROFILE ".callwarden\runtime"
$VersionRoot = Join-Path $RuntimeRoot "versions"
$CurrentRoot = Join-Path $RuntimeRoot "current"
$EvidenceRoot = Join-Path $RuntimeRoot "evidence"
$BridgeRoot = Join-Path $env:USERPROFILE ".callwarden"
$BridgeTokenPath = Join-Path $BridgeRoot "bridge.token"
$BridgeManifestPath = Join-Path $BridgeRoot "bridge.manifest.json"
$BridgeWslEnvPath = Join-Path $BridgeRoot "bridge.wsl.env"
$BridgePidPath = Join-Path $RuntimeRoot "bridge.pid"
$BridgeLogPath = Join-Path $EvidenceRoot "bridge.log"
if ([string]::IsNullOrWhiteSpace($TargetDir)) {
    $TargetDir = Join-Path $RepoRoot "rust_ext\target\stage-refresh"
}
$TargetDir = [IO.Path]::GetFullPath($TargetDir)

function Info([string]$Message) { Write-Host "[runtime-refresh] $Message" -ForegroundColor Cyan }
function Digest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Require-WindowsPython314 {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "找不到 Windows 权威 Python 3.14: $PythonExe"
    }
    $identity = @(& $PythonExe -c "import sys; print(sys.executable); print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1)
    if ($LASTEXITCODE -ne 0 -or $identity.Count -lt 2 -or $identity[1].Trim() -ne "3.14") {
        throw "runtime refresh 必须使用 Python 3.14: $($identity -join '; ')"
    }
    return [pscustomobject]@{ executable = $identity[0].Trim(); version = $identity[1].Trim() }
}

function Inspect-PythonRuntimeDependency([string]$BinaryPath, [string]$Label) {
    $dumpbin = Get-Command dumpbin.exe -ErrorAction SilentlyContinue
    if ($null -eq $dumpbin) {
        throw "找不到 dumpbin.exe，无法核验 cw-daemon.exe 的 Python DLL 依赖；拒绝部署"
    }
    $output = @(& $dumpbin.Source /dependents $BinaryPath 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "dumpbin /dependents 失败，exit=$LASTEXITCODE"
    }
    $joined = $output -join "`n"
    $pythonDlls = @([regex]::Matches($joined, '(?im)^\s*(python\d+\.dll)\s*$') |
        ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() } | Select-Object -Unique)
    $unexpected = @($pythonDlls | Where-Object { $_ -ne "python314.dll" })
    if ($unexpected.Count -gt 0) {
        throw "$Label 链接了非 Python 3.14 runtime：$($unexpected -join ', ')；拒绝部署"
    }
    # cw-daemon 可以是纯 Rust/Python-free 二进制；此时没有 python*.dll 是合法的。
    # 只有直接导入 Python DLL 时才要求其精确为 python314.dll。
    $mode = if ($pythonDlls.Count -eq 0) { "python_free" } else { "python314_direct" }
    return [pscustomobject]@{ mode = $mode; dependencies = $pythonDlls; output = $joined }
}

function Resolve-CoreRuntimeTargets {
    # `cw.py` 在仓库根运行时可加载顶层 Pyd；已安装的 `cw.exe` 则从同一
    # Python 3.14 user site-packages 导入 package extension。两者都必须验证。
    $targets = @()
    $repoPyd = Join-Path $RepoRoot "callwarden_core.pyd"
    if (Test-Path -LiteralPath $repoPyd -PathType Leaf) {
        $targets += [pscustomobject]@{ kind = "repository_source"; path = [IO.Path]::GetFullPath($repoPyd) }
    }

    $tempDir = [IO.Path]::GetTempPath()
    Push-Location $tempDir
    try {
        $probe = @(& $PythonExe -c "import importlib; print(importlib.import_module('callwarden_core.callwarden_core').__file__)" 2>&1)
        $probeCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($probeCode -ne 0 -or $probe.Count -ne 1 -or [string]::IsNullOrWhiteSpace($probe[0])) {
        throw "无法解析已安装 cw.exe 的 callwarden_core extension 路径：$($probe -join '; ')"
    }
    $installedPyd = [IO.Path]::GetFullPath($probe[0].Trim())
    if (-not (Test-Path -LiteralPath $installedPyd -PathType Leaf)) {
        throw "已安装 callwarden_core extension 不存在: $installedPyd"
    }
    $targets += [pscustomobject]@{ kind = "python314_site_package"; path = $installedPyd }

    $scriptsProbe = @(& $PythonExe -c "import sysconfig; print(sysconfig.get_path('scripts', scheme=sysconfig.get_preferred_scheme('user')))" 2>&1)
    $scriptsCode = $LASTEXITCODE
    if ($scriptsCode -ne 0 -or $scriptsProbe.Count -ne 1) {
        throw "无法解析 Python 3.14 user scripts 路径：$($scriptsProbe -join '; ')"
    }
    $cwExe = Join-Path $scriptsProbe[0].Trim() "cw.exe"
    if (-not (Test-Path -LiteralPath $cwExe -PathType Leaf)) {
        throw "找不到与 Python 3.14 对应的权威 cw.exe: $cwExe"
    }

    $deduped = @($targets | Group-Object { $_.path.ToLowerInvariant() } | ForEach-Object { $_.Group[0] })
    return [pscustomobject]@{ targets = $deduped; cw_exe = $cwExe }
}

function Deploy-CoreExtensions([string]$SourcePath, [object[]]$Targets, [string]$BackupRoot) {
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "缺少本次构建的 callwarden_core library: $SourcePath"
    }
    $expectedHash = Digest $SourcePath
    if ([string]::IsNullOrWhiteSpace($expectedHash)) { throw "无法计算 callwarden_core build hash" }
    New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

    $deployed = @()
    try {
        foreach ($target in $Targets) {
            $destination = [string]$target.path
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
                throw "拒绝向不存在的 extension target 写入: $destination"
            }
            $backup = Join-Path $BackupRoot ("{0}-{1}" -f $target.kind, [IO.Path]::GetFileName($destination))
            $stage = "$destination.$([guid]::NewGuid().ToString('N')).new"
            try {
                Copy-Item -LiteralPath $SourcePath -Destination $stage -ErrorAction Stop
                [IO.File]::Replace($stage, $destination, $backup, $true)
            } finally {
                if (Test-Path -LiteralPath $stage -PathType Leaf) { Remove-Item -LiteralPath $stage -Force }
            }
            $installedHash = Digest $destination
            $record = [pscustomobject]@{ kind = $target.kind; path = $destination; backup = $backup; sha256 = $installedHash; python_dependency_mode = $null; python_dependencies = @() }
            $deployed += $record
            if ($installedHash -ne $expectedHash) {
                throw "callwarden_core 部署 hash 不一致: $destination"
            }
            $dependency = Inspect-PythonRuntimeDependency $destination ([string]$target.kind)
            $record.python_dependency_mode = $dependency.mode
            $record.python_dependencies = $dependency.dependencies
        }
    } catch {
        Restore-CoreExtensions $deployed
        throw
    }
    return @($deployed)
}

function Restore-CoreExtensions([object[]]$Deployed) {
    foreach ($item in @($Deployed)) {
        if ($null -eq $item -or -not (Test-Path -LiteralPath $item.backup -PathType Leaf)) { continue }
        $stage = "$($item.path).$([guid]::NewGuid().ToString('N')).rollback"
        try {
            Copy-Item -LiteralPath $item.backup -Destination $stage -ErrorAction Stop
            [IO.File]::Replace($stage, $item.path, $item.backup, $true)
        } finally {
            if (Test-Path -LiteralPath $stage -PathType Leaf) { Remove-Item -LiteralPath $stage -Force }
        }
    }
}

function Verify-AuthorityCli([string]$CwExePath, [string]$VerificationTaskId) {
    $output = @(& $CwExePath lease status $VerificationTaskId --role implementer 2>&1)
    $code = $LASTEXITCODE
    $text = $output -join "`n"
    if ($code -ne 0 -or $text -match 'MIGRATION_FAILED: schema checksum mismatch') {
        throw "权威 cw.exe lease status 未通过 migration authority 验证：$text"
    }
    return [pscustomobject]@{ executable = $CwExePath; exit_code = $code; output = $text }
}

function Owned-Processes {
    $repoPattern = [regex]::Escape($RepoRoot.TrimEnd("\"))
    $runtimePattern = [regex]::Escape($RuntimeRoot.TrimEnd("\"))
    $items = @()
    foreach ($p in @(Get-CimInstance Win32_Process)) {
        $cmd = [string]$p.CommandLine
        $exe = [string]$p.ExecutablePath
        $normalizedCmd = $cmd.Replace('/', '\')
        $normalizedExe = $exe.Replace('/', '\')
        $daemon = ($p.Name -match "^cw-daemon(?:\.exe)?$") -and
            (($normalizedExe -match $repoPattern) -or ($normalizedCmd -match $repoPattern) -or
             ($normalizedExe -match $runtimePattern) -or ($normalizedCmd -match $runtimePattern))
        $mcp = ($normalizedCmd -match "cw\.py[\s\\]+server") -and
            ($normalizedCmd -match $repoPattern)
        $bridge = ($p.Name -match "^cw-bridge(?:\.exe)?$") -and
            (($normalizedExe -match $repoPattern) -or ($normalizedCmd -match $repoPattern) -or
             ($normalizedExe -match $runtimePattern) -or ($normalizedCmd -match $runtimePattern))
        if ($daemon -or $mcp -or $bridge) {
            $items += [pscustomobject]@{
                pid = [int]$p.ProcessId; name = [string]$p.Name
                executable = $exe; command_line = $cmd
                kind = if ($daemon) { "daemon" } elseif ($bridge) { "bridge" } else { "mcp" }
            }
        }
    }
    return @($items)
}

function Stop-Owned([pscustomobject]$Item) {
    if ($Item.pid -eq $PID) { throw "拒绝停止当前脚本进程" }
    $process = Get-Process -Id $Item.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    Info "停止 Call Warden $($Item.kind): PID $($Item.pid)"
    Stop-Process -Id $Item.pid -ErrorAction Stop
    try { Wait-Process -Id $Item.pid -Timeout 10 -ErrorAction Stop }
    catch {
        if (Get-Process -Id $Item.pid -ErrorAction SilentlyContinue) {
            Info "进程未及时退出，精确强制停止 PID $($Item.pid)"
            Stop-Process -Id $Item.pid -Force -ErrorAction Stop
        }
    }
}

function Get-AllDaemons {
    # P2：全量枚举本机 cw-daemon 进程（不限 repo/runtime 路径匹配），
    # 覆盖所有启动入口（refresh 脚本 / autostart / IDE supervisor）拉起的实例，
    # 作为探针去重的完整视图。
    $items = @()
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='cw-daemon.exe'")) {
        if ([int]$p.ProcessId -eq $PID) { continue }
        $items += [pscustomobject]@{
            pid = [int]$p.ProcessId
            name = "cw-daemon.exe"
            executable = [string]$p.ExecutablePath
            command_line = [string]$p.CommandLine
            created_at = $p.CreationDate
            kind = "daemon"
        }
    }
    return @($items)
}

function Get-DaemonCreationDate([int]$Pid) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$Pid" -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    return ([DateTime]$p.CreationDate)
}

function Ensure-DaemonSingleInstance {
    # P2：统一 daemon 启动入口 + 探针去重（与 autostart/supervisor 同一语义：
    # 先 ping 探活，已有可用实例则复用不新起；否则停止全部冗余后启动新实例）。
    # 返回 [pscustomobject]@{ action = "reuse"|"start"; pid = <int> }。
    param(
        [string]$EndpointValue,
        [string]$DaemonPath,
        [DateTime]$SwappedAt
    )
    $probe = Ping $EndpointValue $DaemonPath
    $reusePid = $null
    if ($probe.ok) {
        try {
            $probeJson = $probe.output | ConvertFrom-Json
            $candidatePid = [int]$probeJson.pid
            $candidateProc = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
            $created = Get-DaemonCreationDate $candidatePid
            if ($null -ne $candidateProc -and $null -ne $created -and $created -ge $SwappedAt) {
                $exeMatches = [string]((Get-CimInstance Win32_Process -Filter "ProcessId=$candidatePid" -ErrorAction SilentlyContinue).ExecutablePath)
                if ($exeMatches -eq $DaemonPath) { $reusePid = $candidatePid }
            }
        } catch { $reusePid = $null }
    }
    if ($null -ne $reusePid) {
        Info "探针发现可用 daemon PID $reusePid（runtime/current 替换后启动），复用不新起；清理其余冗余实例"
        @(Get-AllDaemons | Where-Object { $_.pid -ne $reusePid }) | ForEach-Object { Stop-Owned $_ }
        return [pscustomobject]@{ action = "reuse"; pid = $reusePid }
    }
    Info "无可用 daemon 实例（或为旧版本），停止全部 cw-daemon 后启动新实例"
    @(Get-AllDaemons) | ForEach-Object { Stop-Owned $_ }
    $proc = Start-Process -FilePath $DaemonPath -ArgumentList @("--socket", $EndpointValue) -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    return [pscustomobject]@{ action = "start"; pid = $proc.Id }
}

function Endpoint {
    $old = $env:PYTHONIOENCODING; $env:PYTHONIOENCODING = "utf-8"
    try {
        $value = (& $PythonExe -c "import config; print(config.get_default_daemon_endpoint())" 2>$null).Trim()
        if ([string]::IsNullOrWhiteSpace($value)) { throw "无法解析 daemon endpoint" }
        return $value
    } finally {
        if ($null -eq $old) { Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue }
        else { $env:PYTHONIOENCODING = $old }
    }
}

function Ping([string]$EndpointValue, [string]$DaemonPath) {
    $oldEndpoint = $env:CW_DAEMON_ENDPOINT; $oldBinary = $env:CW_DAEMON_BIN
    $env:CW_DAEMON_ENDPOINT = $EndpointValue; $env:CW_DAEMON_BIN = $DaemonPath
    try {
        $output = @(& $PythonExe (Join-Path $RepoRoot "cw.py") daemon ping 2>&1)
        return [pscustomobject]@{ ok = ($LASTEXITCODE -eq 0); exit_code = $LASTEXITCODE; output = ($output -join "`n") }
    } finally {
        if ($null -eq $oldEndpoint) { Remove-Item Env:CW_DAEMON_ENDPOINT -ErrorAction SilentlyContinue }
        else { $env:CW_DAEMON_ENDPOINT = $oldEndpoint }
        if ($null -eq $oldBinary) { Remove-Item Env:CW_DAEMON_BIN -ErrorAction SilentlyContinue }
        else { $env:CW_DAEMON_BIN = $oldBinary }
    }
}

function Atomic-Json([string]$Path, [object]$Value) {
    $tmp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmp -Encoding utf8
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Resolve-BridgeEndpoint {
    if ($BridgeEndpoint -ne "auto") { return $BridgeEndpoint }
    try {
        # 不嵌套 bash/awk，避免 PowerShell 转义破坏 WSL 命令；直接解析 ip route 输出。
        $routeLines = @(& wsl.exe -d Ubuntu -- ip route 2>$null)
        $defaultRoute = $routeLines | Where-Object { $_ -match '^default\s+via\s+' } | Select-Object -First 1
        $gateway = if ($defaultRoute -match '^default\s+via\s+([^\s]+)') { $Matches[1] } else { "" }
        if ($gateway -match '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$') {
            return "$gateway`:8456"
        }
    } catch {
        Info "无法探测 WSL 默认网关，bridge 回退 Windows loopback"
    }
    return "127.0.0.1:8456"
}

function Ensure-BridgeToken {
    New-Item -ItemType Directory -Force -Path $BridgeRoot | Out-Null
    $existing = if (Test-Path -LiteralPath $BridgeTokenPath -PathType Leaf) {
        (Get-Content -LiteralPath $BridgeTokenPath -Raw -ErrorAction Stop).Trim()
    } else { "" }
    if ([string]::IsNullOrWhiteSpace($existing)) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [Convert]::ToBase64String($bytes) | Set-Content -LiteralPath $BridgeTokenPath -NoNewline -Encoding ascii
    }
    try {
        & icacls.exe $BridgeTokenPath /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(R)" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls exit=$LASTEXITCODE" }
    } catch {
        throw "无法为 bridge token 设置当前用户 ACL：$($_.Exception.Message)"
    }
    return (Get-Content -LiteralPath $BridgeTokenPath -Raw -ErrorAction Stop).Trim()
}

function Start-Bridge([string]$BridgePath) {
    $resolvedEndpoint = Resolve-BridgeEndpoint
    if ($resolvedEndpoint -match "^tcp://") { $bridgeBind = $resolvedEndpoint.Substring(6) } else { $bridgeBind = $resolvedEndpoint }
    if ($bridgeBind -notmatch "^[^:]+:[0-9]+$") { throw "BridgeEndpoint 必须是 host:port 或 tcp://host:port" }
    $token = Ensure-BridgeToken
    if ([string]::IsNullOrWhiteSpace($token)) { throw "bridge token 为空" }
    $owned = @(Owned-Processes | Where-Object kind -eq "bridge")
    if ($RestartBridge) { $owned | ForEach-Object { Stop-Owned $_ } }
    $existingPid = $null
    if (Test-Path -LiteralPath $BridgePidPath -PathType Leaf) {
        $rawPid = (Get-Content -LiteralPath $BridgePidPath -Raw).Trim()
        if ($rawPid -match '^[0-9]+$') { $existingPid = [int]$rawPid }
    }
    $running = $null
    if ($existingPid) {
        $candidate = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($candidate -and ($candidate.Path -eq $BridgePath)) { $running = $candidate }
    }
    if (-not $running) {
        $owned | Where-Object { $_.pid -ne $existingPid } | ForEach-Object { Stop-Owned $_ }
        $env:CW_BRIDGE_ENDPOINT = $bridgeBind
        $env:CW_BRIDGE_LISTEN_ADDR = $bridgeBind
        $env:CW_BRIDGE_TOKEN_FILE = $BridgeTokenPath
        $env:CW_BRIDGE_MANIFEST = $BridgeManifestPath
        $bridgeOut = Join-Path $EvidenceRoot "bridge.stdout.log"
        $bridgeErr = Join-Path $EvidenceRoot "bridge.stderr.log"
        $running = Start-Process -FilePath $BridgePath -ArgumentList @() -WorkingDirectory $RepoRoot -WindowStyle Hidden `
            -RedirectStandardOutput $bridgeOut -RedirectStandardError $bridgeErr -PassThru
        Set-Content -LiteralPath $BridgePidPath -Value ([string]$running.Id) -Encoding ascii
        Info "启动 Windows bridge: PID $($running.Id), endpoint tcp://$bridgeBind"
    } else {
        Info "复用 Windows bridge: PID $($running.Id), endpoint tcp://$bridgeBind"
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 250
        if ($running.HasExited) { throw "cw-bridge 已退出，详见 $BridgeLogPath" }
        if (Test-Path -LiteralPath $BridgeManifestPath -PathType Leaf) {
            $manifest = Get-Content -LiteralPath $BridgeManifestPath -Raw | ConvertFrom-Json
            if ([string]$manifest.endpoint -match ":$([regex]::Escape(($bridgeBind -split ':')[-1]))$") { break }
        }
    }
    if (-not (Test-Path -LiteralPath $BridgeManifestPath -PathType Leaf)) { throw "bridge manifest 未生成: $BridgeManifestPath" }
    $drive = $BridgeRoot.Substring(0, 1).ToLowerInvariant()
    $wslBridgeRoot = "/mnt/$drive" + ($BridgeRoot.Substring(2) -replace '\\', '/')
    @(
        "# 由 refresh_shared_runtime.ps1 生成；供 WSL MCP/CLI source",
        "export CW_AUTHORITY=windows-host",
        "export CW_DAEMON_TRANSPORT=windows-bridge",
        "export CW_BRIDGE_MANIFEST=$wslBridgeRoot/bridge.manifest.json",
        "export CW_BRIDGE_TOKEN_FILE=$wslBridgeRoot/bridge.token"
    ) | Set-Content -LiteralPath $BridgeWslEnvPath -Encoding utf8
    $oldEndpoint = $env:CW_BRIDGE_ENDPOINT; $oldTokenFile = $env:CW_BRIDGE_TOKEN_FILE
    try {
        $env:CW_BRIDGE_ENDPOINT = "tcp://$bridgeBind"
        $env:CW_BRIDGE_TOKEN_FILE = $BridgeTokenPath
        $health = @(& $PythonExe (Join-Path $RepoRoot "cw.py") daemon bridge --endpoint "tcp://$bridgeBind" --token-file $BridgeTokenPath 2>&1)
        $healthCode = $LASTEXITCODE
        $health | Set-Content -LiteralPath $BridgeLogPath -Encoding utf8
        if ($healthCode -ne 0) { throw "bridge health 失败，exit=$healthCode；详见 $BridgeLogPath" }
    } finally {
        if ($null -eq $oldEndpoint) { Remove-Item Env:CW_BRIDGE_ENDPOINT -ErrorAction SilentlyContinue } else { $env:CW_BRIDGE_ENDPOINT = $oldEndpoint }
        if ($null -eq $oldTokenFile) { Remove-Item Env:CW_BRIDGE_TOKEN_FILE -ErrorAction SilentlyContinue } else { $env:CW_BRIDGE_TOKEN_FILE = $oldTokenFile }
    }
    return [pscustomobject]@{ pid = $running.Id; endpoint = "tcp://$bridgeBind"; token_file = $BridgeTokenPath; manifest = $BridgeManifestPath; wsl_env = $BridgeWslEnvPath; health_log = $BridgeLogPath }
}

$head = (& git -C $RepoRoot rev-parse HEAD).Trim()
$version = "{0}-{1}-{2}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $head.Substring(0, 12), ([guid]::NewGuid().ToString("N").Substring(0, 8))
$buildDir = Join-Path $TargetDir $(if ($Configuration -eq "release") { "release" } else { $Configuration })
$buildLog = Join-Path $EvidenceRoot "$version-build.log"
$evidencePath = Join-Path $EvidenceRoot "$version.json"
$oldCurrent = $null
$oldDaemonPath = $null
$coreDeployed = @()
$result = [ordered]@{
    task_id = $TaskId; status = "failed"; started_at = [DateTime]::UtcNow.ToString("o")
    finished_at = $null; git_head = $head; configuration = $Configuration
    target_dir = $TargetDir; runtime_version = $version; repository = $RepoRoot
    endpoint = $null; binaries = @(); processes_before = @(); processes_after = @()
    python = $null; pyo3_python = $null; daemon_runtime = $null; core_runtime = @(); authority_cli = $null
    smoke = @(); rollback = $false; error = $null
}
New-Item -ItemType Directory -Force -Path $VersionRoot, $EvidenceRoot | Out-Null

try {
    if (-not (Test-Path -LiteralPath $RustManifest -PathType Leaf)) { throw "找不到 Rust manifest: $RustManifest" }
    $pythonRuntime = Require-WindowsPython314
    $env:PYTHON = $PythonExe
    $env:PYO3_PYTHON = $PythonExe
    $result.python = $pythonRuntime
    $result.pyo3_python = $env:PYO3_PYTHON
    $before = Owned-Processes; $result.processes_before = $before
    $oldDaemonItems = @($before | Where-Object kind -eq "daemon" | Select-Object -First 1)
    if ($oldDaemonItems.Count -gt 0) {
        $oldDaemonPath = [string]$oldDaemonItems[0].executable
    }
    Info "先构建新 runtime，不停止现有 MCP/daemon"
    $cargoArgs = @("build", "--manifest-path", $RustManifest, "--target-dir", $TargetDir,
        "--lib", "--bin", "cw-daemon", "--bin", "cw", "--bin", "cw-client", "--bin", "cw-agent", "--bin", "cw-bridge")
    if ($Configuration -eq "release") { $cargoArgs += "--release" }
    elseif ($Configuration -ne "debug") { $cargoArgs += @("--profile", $Configuration) }
    $buildOutput = @(& cargo @cargoArgs 2>&1); $buildCode = $LASTEXITCODE
    $buildOutput | Set-Content -LiteralPath $buildLog -Encoding utf8
    if ($buildCode -ne 0) { throw "cargo build 失败，exit=$buildCode；详见 $buildLog" }

    $names = @("cw-daemon.exe", "cw.exe", "cw-client.exe", "cw-agent.exe", "cw-bridge.exe")
    $built = @()
    foreach ($name in $names) {
        $source = Join-Path $buildDir $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "构建成功但缺少产物: $source" }
        $built += [pscustomobject]@{ name = $name; source = $source; sha256 = Digest $source }
    }

    $coreSource = Join-Path $buildDir "callwarden_core.dll"
    if (-not (Test-Path -LiteralPath $coreSource -PathType Leaf)) {
        throw "构建成功但缺少 PyO3 extension: $coreSource"
    }
    $coreRuntime = Resolve-CoreRuntimeTargets
    $coreBackupRoot = Join-Path $VersionRoot "$version.core-backup"
    $coreDeployed = Deploy-CoreExtensions $coreSource $coreRuntime.targets $coreBackupRoot
    $result.core_runtime = $coreDeployed
    $result.authority_cli = Verify-AuthorityCli $coreRuntime.cw_exe $TaskId

    $endpointValue = Endpoint; $result.endpoint = $endpointValue
    if ($RestartMcp) { $before | Where-Object kind -eq "mcp" | ForEach-Object { Stop-Owned $_ } }
    # P2：daemon 停止不再在此处无条件执行；统一在 runtime/current 替换后
    # 由 Ensure-DaemonSingleInstance 做"探针去重 + 启动"（存在新实例则复用）。

    $stage = Join-Path $VersionRoot "$version.staging"
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    foreach ($item in $built) { Copy-Item -LiteralPath $item.source -Destination (Join-Path $stage $item.name) }
    if (Test-Path -LiteralPath $CurrentRoot) {
        $oldCurrent = Join-Path $RuntimeRoot "previous-$([DateTime]::Now.ToString('yyyyMMdd-HHmmss'))"
        Move-Item -LiteralPath $CurrentRoot -Destination $oldCurrent
    }
    Move-Item -LiteralPath $stage -Destination $CurrentRoot
    $currentSwappedAt = Get-Date
    $daemonPath = Join-Path $CurrentRoot "cw-daemon.exe"
    $expectedDaemonHash = [string](($built | Where-Object name -eq "cw-daemon.exe" | Select-Object -First 1).sha256)
    $installedDaemonHash = Digest $daemonPath
    if ([string]::IsNullOrWhiteSpace($expectedDaemonHash) -or $installedDaemonHash -ne $expectedDaemonHash) {
        throw "runtime/current/cw-daemon.exe hash 与本次构建产物不一致；拒绝启动"
    }
    $pythonDependency = Inspect-PythonRuntimeDependency $daemonPath "cw-daemon.exe"
    if (-not $NoPersistDaemonPath) {
        [Environment]::SetEnvironmentVariable("CW_DAEMON_BIN", $daemonPath, "User")
        $env:CW_DAEMON_BIN = $daemonPath
    }
    $env:CW_DAEMON_ENDPOINT = $endpointValue
    $env:CW_DAEMON_TASK_DB = Join-Path $env:USERPROFILE ".callwarden\callwarden.db"
    $daemonStart = Ensure-DaemonSingleInstance $endpointValue $daemonPath $currentSwappedAt
    $result.daemon_start_action = $daemonStart.action
    $result.daemon_start_pid = $daemonStart.pid

    $ready = $false; $lastPing = $null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500; $lastPing = Ping $endpointValue $daemonPath
        if ($lastPing.ok) { $ready = $true; break }
    }
    if (-not $ready) { throw "daemon 未通过 ping：$($lastPing.output)" }
    $runningDaemon = Get-Process -Id $daemonStart.pid -ErrorAction SilentlyContinue
    if ($null -eq $runningDaemon -or [string]::IsNullOrWhiteSpace($runningDaemon.Path)) {
        throw "daemon 已退出或无法读取 executable path；拒绝宣称 runtime 已切换"
    }
    $runningPath = [IO.Path]::GetFullPath($runningDaemon.Path)
    $expectedPath = [IO.Path]::GetFullPath($daemonPath)
    if (-not $runningPath.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "运行 daemon 路径不等于 runtime/current：$runningPath"
    }
    $runningHash = Digest $runningPath
    if ($runningHash -ne $expectedDaemonHash) {
        throw "运行 daemon hash 与本次构建产物不一致；拒绝部署"
    }
    $result.daemon_runtime = [pscustomobject]@{
        pid = $runningDaemon.Id; executable = $runningPath; sha256 = $runningHash
        expected_sha256 = $expectedDaemonHash; python_dependency_mode = $pythonDependency.mode
        python_dependencies = $pythonDependency.dependencies
        ping_exit_code = $lastPing.exit_code; ping_output = $lastPing.output
    }

    if ($StartBridge) {
        $bridgePath = Join-Path $CurrentRoot "cw-bridge.exe"
        $result.bridge = Start-Bridge $bridgePath
    }

    $result.binaries = @($built | ForEach-Object {
        $installed = Join-Path $CurrentRoot $_.name
        [pscustomobject]@{ name = $_.name; path = $installed; sha256 = Digest $installed }
    })
    if ($RunSmokeTests) {
        $commands = @(
            @($PythonExe, (Join-Path $RepoRoot "cw.py"), "--version"),
            @($PythonExe, (Join-Path $RepoRoot "cw.py"), "daemon", "ping")
        )
        foreach ($command in $commands) {
            $text = @(& $command[0] $command[1..($command.Count - 1)] 2>&1); $code = $LASTEXITCODE
            $result.smoke += [pscustomobject]@{ command = ($command -join " "); exit_code = $code; output = ($text -join "`n") }
            if ($code -ne 0) { throw "smoke test 失败: $($command -join ' ')" }
        }
    }
    $result.processes_after = @(Owned-Processes); $result.status = "passed"
    Info "runtime 切换成功；daemon/bridge 已就绪，MCP 由 IDE supervisor 重连"
}
catch {
    $result.error = $_.Exception.Message
    if ($coreDeployed.Count -gt 0) {
        Restore-CoreExtensions $coreDeployed
        $result.core_runtime_rollback = $true
    }
    if ($oldCurrent -and (Test-Path -LiteralPath $oldCurrent)) {
        $result.rollback = $true
        @(Get-AllDaemons) | ForEach-Object { Stop-Owned $_ }
        if (Test-Path -LiteralPath $CurrentRoot) { Remove-Item -LiteralPath $CurrentRoot -Recurse -Force }
        Move-Item -LiteralPath $oldCurrent -Destination $CurrentRoot
        $oldDaemon = Join-Path $CurrentRoot "cw-daemon.exe"
        [Environment]::SetEnvironmentVariable("CW_DAEMON_BIN", $oldDaemon, "User")
        Start-Process -FilePath $oldDaemon -ArgumentList @("--socket", (Endpoint)) -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null
    } elseif ($oldDaemonPath -and (Test-Path -LiteralPath $oldDaemonPath -PathType Leaf)) {
        $result.rollback = $true
        $existingDaemon = @(Get-AllDaemons)
        if ($existingDaemon.Count -eq 0) {
            Info "安装失败，重新启动停止前的 daemon: $oldDaemonPath"
            [Environment]::SetEnvironmentVariable("CW_DAEMON_BIN", $oldDaemonPath, "User")
            Start-Process -FilePath $oldDaemonPath -ArgumentList @("--socket", (Endpoint)) -WorkingDirectory $RepoRoot -WindowStyle Hidden | Out-Null
        } else {
            Info "安装失败，保留已有 daemon，避免回滚重复启动；PID=$($existingDaemon.pid -join ',')"
        }
    }
    Write-Error "[runtime-refresh] $($_.Exception.Message)"
    throw
}
finally {
    $result.finished_at = [DateTime]::UtcNow.ToString("o")
    Atomic-Json $evidencePath $result
    Info "证据已写入: $evidencePath"
}

@(Get-ChildItem -LiteralPath $VersionRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike "*.staging" } | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $KeepVersions) | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
Write-Host "Runtime refresh completed: $version" -ForegroundColor Green
