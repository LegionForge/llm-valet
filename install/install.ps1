#Requires -Version 5.1
<#
.SYNOPSIS
    llm-valet install script for Windows.
.DESCRIPTION
    Installs llm-valet via pip, writes a default config, and optionally
    registers a Windows Task Scheduler task for auto-start on login.
.EXAMPLE
    irm https://raw.githubusercontent.com/LegionForge/llm-valet/main/install/install.ps1 | iex
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ConfigDir  = Join-Path $env:USERPROFILE ".llm-valet"
$ConfigFile = Join-Path $ConfigDir "config.yaml"
$TaskName   = "llm-valet"

function Write-Info  { param($msg) Write-Host "[llm-valet] $msg" -ForegroundColor Green  }
function Write-Warn  { param($msg) Write-Host "[llm-valet] $msg" -ForegroundColor Yellow }
function Write-Err   { param($msg) Write-Host "[llm-valet] $msg" -ForegroundColor Red; exit 1 }

# ── Safety: refuse Administrator ─────────────────────────────────────────────
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Err "Do not run this installer as Administrator. Run as your normal user."
}

# ── Python version check ──────────────────────────────────────────────────────
$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        $major, $minor = $ver.Split('.') | ForEach-Object { [int]$_ }
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
            $python = $cmd
            break
        }
    } catch { continue }
}

if (-not $python) {
    Write-Err "Python 3.11+ is required. Download from https://python.org and re-run."
}
Write-Info "Using Python: $(& $python --version)"

# ── Install package ───────────────────────────────────────────────────────────
Write-Info "Installing llm-valet..."
& $python -m pip install --upgrade llm-valet
if ($LASTEXITCODE -ne 0) { Write-Err "pip install failed." }

# ── Create config directory ───────────────────────────────────────────────────
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir | Out-Null
}

# Restrict config dir to current user only (icacls — no PowerShell native equivalent)
icacls $ConfigDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null

if (-not (Test-Path $ConfigFile)) {
    Write-Info "Writing default config to $ConfigFile"
    @'
# llm-valet configuration
# Full reference: https://github.com/LegionForge/llm-valet

host: 127.0.0.1
port: 8765
provider: ollama
ollama_url: http://127.0.0.1:11434
model_name:       # leave blank to auto-detect loaded model
api_key:          # required when host is 0.0.0.0

thresholds:
  ram_pause_pct: 85.0
  ram_resume_pct: 60.0
  cpu_pause_pct: 90.0
  cpu_sustained_seconds: 30
  gpu_vram_pause_pct: 85.0
  pause_timeout_seconds: 120
  check_interval_seconds: 10
'@ | Set-Content -Path $ConfigFile -Encoding UTF8

    # Restrict config file to current user (mirrors chmod 600)
    icacls $ConfigFile /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
} else {
    Write-Warn "Config already exists — skipping: $ConfigFile"
}

# ── Find llm-valet executable ─────────────────────────────────────────────────
$valetBin = (Get-Command "llm-valet" -ErrorAction SilentlyContinue)?.Source
if (-not $valetBin) {
    # Try Scripts directory in user Python path
    $scripts = & $python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    $candidate = Join-Path $scripts "llm-valet.exe"
    if (Test-Path $candidate) { $valetBin = $candidate }
}

# ── Task Scheduler: auto-start on login ──────────────────────────────────────
if ($valetBin) {
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if ($existingTask) {
        Write-Warn "Scheduled task '$TaskName' already exists — skipping"
    } else {
        Write-Info "Registering Task Scheduler task: $TaskName"

        $action  = New-ScheduledTaskAction -Execute $valetBin
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable

        Register-ScheduledTask `
            -TaskName  $TaskName `
            -Action    $action `
            -Trigger   $trigger `
            -Settings  $settings `
            -RunLevel  Limited `
            -Force | Out-Null

        # Start it immediately for this session
        Start-ScheduledTask -TaskName $TaskName
        Write-Info "Task registered and started — llm-valet will auto-start on login"
    }
} else {
    Write-Warn "llm-valet executable not found in PATH — skipping Task Scheduler registration"
    Write-Warn "Run manually: python -m llm_valet.api"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Info "Installation complete."
Write-Host ""
Write-Host "  WebUI:    http://localhost:8765"
Write-Host "  API docs: http://localhost:8765/docs"
Write-Host "  Config:   $ConfigFile"
Write-Host ""
Write-Host "  Manual start:   llm-valet"
Write-Host "  Manual control: curl -X POST http://localhost:8765/pause"
Write-Host ""
