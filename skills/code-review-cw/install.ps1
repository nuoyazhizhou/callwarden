# install.ps1 — code-review-cw 技能分发安装脚本
# 用法: powershell -ExecutionPolicy Bypass -File install.ps1
# 幂等：可重复执行；已安装会覆盖为包内最新版本。
# 目标宿主：OpenAI Codex（桌面版/CLI 共享 ~/.codex）、Claude Code（~/.claude）、Trae CN（~/.trae-cn，存在才装）。

$ErrorActionPreference = 'Stop'
$SkillName = 'code-review-cw'
$Source = Join-Path $PSScriptRoot 'SKILL.md'
if (-not (Test-Path $Source)) { throw "找不到 $Source，请在技能包目录内运行。" }

$Targets = @(
    @{ Home = Join-Path $env:USERPROFILE '.codex';  Skills = 'skills';  Agents = $true },
    @{ Home = Join-Path $env:USERPROFILE '.claude'; Skills = 'skills';  Agents = $false },
    @{ Home = Join-Path $env:USERPROFILE '.trae-cn'; Skills = 'skills'; Agents = $false }
)

$installed = @()
foreach ($t in $Targets) {
    # Trae 等宿主目录不存在则跳过；codex / claude 为主分发目标，目录不存在则创建。
    if ($t.Home -like '*\.trae-cn' -and -not (Test-Path $t.Home)) { continue }
    $skillsDir = Join-Path $t.Home $t.Skills
    $destDir = Join-Path $skillsDir $SkillName
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Copy-Item -Force -Path $Source -Destination (Join-Path $destDir 'SKILL.md')
    # 附带 references / scripts / assets 资产（存在才拷）
    foreach ($asset in 'references', 'scripts', 'assets') {
        $srcAsset = Join-Path $PSScriptRoot $asset
        if (Test-Path $srcAsset) {
            Copy-Item -Force -Recurse -Path $srcAsset -Destination (Join-Path $destDir $asset)
        }
    }
    # 共享门禁注入：源 skills/_shared/cw-gates.md（与 requirements-cw 共用的单一事实源）
    # 单一源，无降级路径：check_skill_packages.py 的 AC-04 禁止包内 references/cw-gates.md，
    # 故"包内副本"回退在此仓库形态下不可达（PL-01）。留着它只会让人以为可以靠副本兜底。
    $sharedGates = Join-Path (Split-Path $PSScriptRoot -Parent) '_shared\cw-gates.md'
    if (-not (Test-Path $sharedGates)) {
        throw "找不到共享门禁 skills/_shared/cw-gates.md（唯一事实源）；分发时须连同 skills/_shared/ 一起拷贝，包内副本不再被接受。"
    }
    $destRef = Join-Path $destDir 'references'
    New-Item -ItemType Directory -Force -Path $destRef | Out-Null
    Copy-Item -Force -Path $sharedGates -Destination (Join-Path $destRef 'cw-gates.md')
    $installed += $destDir

    # AGENTS.md 常驻注入（仅 Codex）：让技能无需每次手动选择，按 description 自动触发 + 全局指令兜底。
    if ($t.Agents) {
        $agentsMd = Join-Path $t.Home 'AGENTS.md'
        $begin = '<!-- code-review-cw:begin -->'
        $end = '<!-- code-review-cw:end -->'
        $section = @"
$begin
## Code Review 工作流（code-review-cw 技能）

- 任何代码审查请求（"review 这个 PR / diff / 模块 / 提交"），使用 `code-review-cw` 技能（位于 `~/.codex/skills/$SkillName/`，先读其 SKILL.md 再行动）：先图谱驱动选靶，再只读审查出带优先级的报告；每个需动作的发现建为 callwarden 任务单，全部建单完成后才统一修复，四阶段严格串行。
- callwarden MCP 工具不可用时，按技能内「工具不可用兜底」降级为纯审查模式，并如实声明任务单未推进。
$end
"@
        if (Test-Path $agentsMd) {
            $content = Get-Content $agentsMd -Raw
            if ($content -match [regex]::Escape($begin)) {
                # 幂等替换已有段
                $pattern = "(?s)" + [regex]::Escape($begin) + ".*?" + [regex]::Escape($end)
                $content = $content -replace $pattern, $section
                Set-Content -Path $agentsMd -Value $content -NoNewline
            } else {
                Add-Content -Path $agentsMd -Value "`n$section"
            }
        } else {
            Set-Content -Path $agentsMd -Value $section
        }
    }
}

Write-Host "code-review-cw 安装完成：" -ForegroundColor Green
$installed | ForEach-Object { Write-Host "  - $_" }
Write-Host "重启对应宿主会话后生效；技能按 description 自动触发，无需每次手动选择。"
