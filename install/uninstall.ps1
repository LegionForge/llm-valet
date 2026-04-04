#Requires -Version 5.1
<#
.SYNOPSIS
    llm-valet uninstaller for Windows.
.DESCRIPTION
    Stops the auto-start task, removes the llm-valet program files, and optionally
    removes your settings and logs. Makes no other changes to your system.
.PARAMETER Purge
    Remove everything including settings and logs without prompting.
.EXAMPLE
    .\uninstall.ps1
.EXAMPLE
    .\uninstall.ps1 -Purge
#>
param(
    [switch]$Purge
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:USERPROFILE ".llm-valet"
$VenvDir    = Join-Path $InstallDir ".venv"
$TaskName   = "llm-valet"

function Write-Step { param([string]$msg) Write-Host "`n$msg" -ForegroundColor White }
function Write-Ok   { param([string]$msg) Write-Host "  [OK] $msg" -ForegroundColor Green  }
function Write-Warn { param([string]$msg) Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Fail { param([string]$msg) Write-Host "`nError: $msg`n" -ForegroundColor Red; exit 1 }

Write-Host "`nllm-valet uninstaller" -ForegroundColor White
Write-Host ("━" * 40)

if (-not (Test-Path $InstallDir)) {
    Write-Host "llm-valet does not appear to be installed (not found: $InstallDir)"
    exit 0
}

# ── Step 1: Stop and remove the scheduled task ───────────────────────────────
Write-Step "Removing auto-start task..."

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -ne "Ready" -and $task.State -ne "Disabled") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Ok "Stopped running task"
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Ok "Removed scheduled task '$TaskName'"
} else {
    Write-Ok "No scheduled task found (already removed or never registered)"
}

# ── Step 2: Remove files ──────────────────────────────────────────────────────
Write-Step "Removing llm-valet files..."

if (-not $Purge) {
    Write-Host ""
    Write-Host "  Your settings and logs are stored in: $InstallDir" -ForegroundColor Yellow
    Write-Host ""
    $response = Read-Host "  Remove settings and logs too? [y/N]"
    if ($response -match "^[Yy]") { $Purge = $true }
    Write-Host ""
}

if ($Purge) {
    Remove-Item -Recurse -Force $InstallDir
    Write-Ok "Removed $InstallDir (including settings and logs)"
} else {
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir
        Write-Ok "Removed program files"
    }
    Write-Warn "Your settings were kept: $InstallDir\config.yaml"
    Write-Warn "To remove them later: Remove-Item -Recurse `"$InstallDir`""
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "llm-valet has been uninstalled." -ForegroundColor Green
Write-Host "No other changes were made to your system."
Write-Host ""
