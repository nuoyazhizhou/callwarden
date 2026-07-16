# 批量浅克隆 16 语言 x 2 知名开源项目到 testcode/repos/
# 支持中断恢复：已存在的目录自动跳过
# 支持磁盘空间检查：低于阈值（5GB）自动停止

$ErrorActionPreference = "Continue"
$RepoRoot = "c:\git_work\callwarden"
$CloneDir = Join-Path $RepoRoot "testcode\repos"
$ManifestPath = Join-Path $RepoRoot "tests\fixtures\realworld_repos.json"
$LogFile = Join-Path $RepoRoot "tests\fixtures\clone_log.txt"

if (-not (Test-Path $CloneDir)) {
    New-Item -ItemType Directory -Path $CloneDir -Force | Out-Null
}

$MinFreeGB = 5

function Get-FreeGB {
    $free = (Get-PSDrive C).Free
    return [math]::Round($free / 1GB, 2)
}

function Write-Log {
    param([string]$Msg)
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

Set-Content -Path $LogFile -Value "=== Clone started at $(Get-Date) ===" -Encoding UTF8

$Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json

Write-Log "Clone dir: $CloneDir"
Write-Log "Total repos to clone: $($Manifest.repos.Count)"
Write-Log "Initial free space: $(Get-FreeGB) GB"
Write-Log "---"

$success = 0
$skipped = 0
$failed = 0
$stopped = $false

foreach ($repo in $Manifest.repos) {
    $freeGB = Get-FreeGB
    if ($freeGB -lt $MinFreeGB) {
        Write-Log "STOP: Free space $freeGB GB < threshold $MinFreeGB GB"
        $stopped = $true
        break
    }

    $repoName = $repo.name
    $lang = $repo.lang
    $url = $repo.url
    $target = Join-Path $CloneDir $repoName

    if (Test-Path $target) {
        Write-Log "SKIP  [$lang] $repoName (already exists)"
        $skipped++
        continue
    }

    Write-Log "CLONE [$lang] $repoName from $url"
    $start = Get-Date

    & git clone --depth 1 --single-branch $url $target 2>&1 | Out-Null
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        $sizeBytes = (Get-ChildItem $target -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        $sizeMB = [math]::Round($sizeBytes / 1MB, 1)
        $elapsedSec = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
        $freeGBNow = Get-FreeGB
        Write-Log "  OK  size=${sizeMB}MB elapsed=${elapsedSec}s free=${freeGBNow}GB"
        $success++
    } else {
        Write-Log "  FAIL git exit=$exitCode"
        $failed++
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Log "---"
Write-Log "Summary: success=$success skipped=$skipped failed=$failed stopped=$stopped"
Write-Log "Final free space: $(Get-FreeGB) GB"
Write-Log "=== Clone finished at $(Get-Date) ==="

if ($stopped) {
    exit 2
}
if ($failed -gt 0) {
    exit 1
}
exit 0
