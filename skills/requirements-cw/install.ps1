# install.ps1 — requirements-cw 技能分发安装脚本
# 用法: powershell -ExecutionPolicy Bypass -File install.ps1
# 幂等：可重复执行；已安装会覆盖为包内最新版本。
# 目标宿主：OpenAI Codex（桌面版/CLI 共享 ~/.codex）、Claude Code（~/.claude）、Trae CN（~/.trae-cn，存在才装）。
# 注意：本脚本**不写 .gitignore**——设计工件与证据要随 git 跟踪（基线证据链）。

$ErrorActionPreference = 'Stop'
$SkillName = 'requirements-cw'
$Source = Join-Path $PSScriptRoot 'SKILL.md'
if (-not (Test-Path $Source)) { throw "找不到 $Source，请在技能包目录内运行。" }

$Targets = @(
    @{ Home = Join-Path $env:USERPROFILE '.codex';  Skills = 'skills'; Agents = $true },
    @{ Home = Join-Path $env:USERPROFILE '.claude'; Skills = 'skills'; Agents = $false },
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
    # 共享门禁注入：源 skills/_shared/cw-gates.md（与 code-review-cw 共用的单一事实源）
    $sharedGates = Join-Path (Split-Path $PSScriptRoot -Parent) '_shared\cw-gates.md'
    if (-not (Test-Path $sharedGates)) {
        $localGates = Join-Path $PSScriptRoot 'references\cw-gates.md'
        if (Test-Path $localGates) {
            $sharedGates = $localGates
            Write-Warning "未找到 skills/_shared/cw-gates.md，改用包内副本（可能过期）"
        } else {
            throw "找不到共享门禁 skills/_shared/cw-gates.md；分发时须连同 skills/_shared/ 一起拷贝。"
        }
    }
    $destRef = Join-Path $destDir 'references'
    New-Item -ItemType Directory -Force -Path $destRef | Out-Null
    Copy-Item -Force -Path $sharedGates -Destination (Join-Path $destRef 'cw-gates.md')
    $installed += $destDir

    # AGENTS.md 常驻注入（仅 Codex）：按 description 自动触发 + 全局指令兜底。
    if ($t.Agents) {
        $agentsMd = Join-Path $t.Home 'AGENTS.md'
        $begin = '<!-- requirements-cw:begin -->'
        $end = '<!-- requirements-cw:end -->'
        $section = @"
$begin
## 需求分析与拆解工作流（requirements-cw 技能）

- 模糊需求、新功能、新项目、技术选型、需求拆解类请求，使用 ``requirements-cw`` 技能（位于 ``~/.codex/skills/$SkillName/``，先读其 SKILL.md 再行动）：R0 澄清 ≤3 轮 → （新项目）技术栈选型门禁 → 可视化 → 符号锚定 → contract 卡片 + 设计门禁 + 基线冻结 → 注入 callwarden 任务单 → 开发 → 收口与 feature 级验收。
- 每个评审门禁必须产出四件套（方案对比 ≥2 / 被否决理由 / 异常场景矩阵 8 类 / 未决项三态），hotfix 豁免须满足单文件 + 无新增符号 + 已有测试覆盖三条。
- 追溯主线：REQ 编号 → 卡片 → task_id → AC → 证据（``.design/<feature>/evidence/``）。证据缺失即验收不通过。
- 已有代码的审查交给 ``code-review-cw``，不要用本技能。
- callwarden MCP 不可用时，只做 R0-R3 出方案并如实声明"未建单、未验收"，不得假称已建单或已闭环。
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

Write-Host "requirements-cw 安装完成：" -ForegroundColor Green
$installed | ForEach-Object { Write-Host "  - $_" }
Write-Host "重启对应宿主会话后生效；技能按 description 自动触发，无需每次手动选择。"
Write-Host "提示：技能运行期在 <repo>/.design/<feature>/ 落盘设计工件与证据，需保留 git 跟踪。"
