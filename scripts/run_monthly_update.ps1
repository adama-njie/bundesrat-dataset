<#
.SYNOPSIS
    Monthly Bundesrat corpus update, for Task Scheduler.

.DESCRIPTION
    Thin wrapper around src/br_update.py. Resolves the project root from its own
    location, logs to outputs/logs/, records the tool environment, and exits
    with Python's exit code so the scheduler records a failure as a failure.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_monthly_update.ps1
    powershell -ExecutionPolicy Bypass -File scripts\run_monthly_update.ps1 -DryRun
#>
param(
    [switch]$DryRun,
    [string]$Python = "python"
)

# Never hard-code the project path: a scheduled task may run from anywhere.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "outputs\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogFile = Join-Path $LogDir "monthly_update_$Stamp.log"

# Keep the last 12 runs; a year of monthly logs is plenty to diagnose from.
Get-ChildItem $LogDir -Filter "monthly_update_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 12 |
    Remove-Item -Force -ErrorAction SilentlyContinue

# ⚠️ A scheduled task has a DIFFERENT environment from your shell: different
# PATH, no user profile, possibly a different Python. When a run behaves
# unlike your manual one, this block is the first thing you will want.
$header = @"
=== Bundesrat monthly update ===
started      : $(Get-Date -Format o)
project root : $ProjectRoot
whoami       : $(whoami)
python       : $((Get-Command $Python -ErrorAction SilentlyContinue).Source)
python ver   : $(& $Python -V 2>&1)
BUNDESRAT_PDF_DIR          : $env:BUNDESRAT_PDF_DIR
BUNDESRAT_PREPROCESSED_DIR : $env:BUNDESRAT_PREPROCESSED_DIR
================================
"@
$header | Out-File -FilePath $LogFile -Encoding utf8

$script = Join-Path $ProjectRoot "src\br_update.py"
$pyArgs = @("`"$script`"")
if ($DryRun) { $pyArgs += "--dry-run" }

# ⚠️ TWO separate encoding traps, and avoiding one exposes the other.
# 1. PowerShell 5.1 writes *>> redirection as UTF-16. Mixed with Python's UTF-8
#    output that produces "S t a g e  1" in the log. Append through cmd instead.
# 2. Python then falls back to the console codepage (cp1252) when its stdout is
#    a redirected handle, so em dashes and arrows arrive as "?". Force UTF-8.
$env:PYTHONIOENCODING = "utf-8"
$cmdLine = "`"$Python`" $($pyArgs -join ' ') >> `"$LogFile`" 2>&1"
Push-Location $ProjectRoot
try {
    cmd /c $cmdLine
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

"finished     : $(Get-Date -Format o) (exit $code)" |
    Out-File -FilePath $LogFile -Append -Encoding utf8

Write-Host "Log: $LogFile"
exit $code
