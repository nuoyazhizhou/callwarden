$ErrorActionPreference = 'Stop'

# Register 3 callwarden repo skills into QwenWork skills dir via directory junctions.
# ASCII-only file: Windows PowerShell 5.1 decodes BOM-less UTF-8 .ps1 as GBK.
$srcRoot = 'C:\git_work\callwarden\.agents\skills'
$dstRoot = 'C:\Users\wanpi\.qwenworkcn\skills'
$names   = @('cw-task-loop', 'cw-planner-architect', 'cw-executor-senior-engineer')

foreach ($name in $names) {
    $src = Join-Path $srcRoot $name
    $dst = Join-Path $dstRoot $name

    if (-not (Test-Path -LiteralPath (Join-Path $src 'SKILL.md'))) {
        Write-Output ('SKIP        ' + $name + ' : source has no SKILL.md')
        continue
    }

    if (Test-Path -LiteralPath $dst) {
        $item = Get-Item -LiteralPath $dst -Force
        $isLink = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        if ($isLink) {
            Write-Output ('EXISTS-LINK ' + $name + ' : ' + ($item.Target -join ','))
        }
        else {
            Write-Output ('EXISTS-REAL ' + $name + ' : real dir at target, NOT touched')
        }
        continue
    }

    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
    Write-Output ('CREATED     ' + $name)
}

Write-Output '--- verify: read through junction ---'
foreach ($name in $names) {
    $skillFile = Join-Path (Join-Path $dstRoot $name) 'SKILL.md'
    if (Test-Path -LiteralPath $skillFile) {
        $len = (Get-Item -LiteralPath $skillFile).Length
        Write-Output ('READ OK     ' + $name + ' bytes=' + $len)
    }
    else {
        Write-Output ('READ FAIL   ' + $name)
    }
}

Write-Output '--- reparse targets ---'
foreach ($name in $names) {
    $item = Get-Item -LiteralPath (Join-Path $dstRoot $name) -Force
    Write-Output ($name + ' Attributes=' + $item.Attributes + ' Target=' + ($item.Target -join ','))
}
