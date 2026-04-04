#Requires -Version 5.1
<#
.SYNOPSIS
    llm-valet installer for Windows.
.DESCRIPTION
    Installs llm-valet into an isolated Python environment under %USERPROFILE%\.llm-valet,
    writes a default config, and registers a Task Scheduler task to auto-start on login.
.EXAMPLE
    irm https://raw.githubusercontent.com/LegionForge/llm-valet/main/install/install.ps1 | iex
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallDir = Join-Path $env:USERPROFILE ".llm-valet"
$VenvDir    = Join-Path $InstallDir ".venv"
$ConfigFile = Join-Path $InstallDir "config.yaml"
$TaskName   = "llm-valet"
$Steps      = 5

function Write-Step { param([int]$n, [string]$msg) Write-Host "`n[$n/$Steps] $msg" -ForegroundColor White }
function Write-Ok   { param([string]$msg) Write-Host "  [OK] $msg" -ForegroundColor Green  }
function Write-Warn { param([string]$msg) Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Fail { param([string]$msg) Write-Host "`nError: $msg`n" -ForegroundColor Red; exit 1 }

Write-Host "`nllm-valet installer" -ForegroundColor White
Write-Host ("━" * 40)

# ── Safety: refuse Administrator ─────────────────────────────────────────────
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    Write-Fail "Do not run as Administrator. Open a normal (non-elevated) PowerShell window."
}

# ── Step 1: Python ────────────────────────────────────────────────────────────
Write-Step 1 "Checking Python version..."

$Python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $parts = $ver.Split('.')
            $major = [int]$parts[0]; $minor = [int]$parts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                $Python = $cmd; break
            }
        }
    } catch { continue }
}

if (-not $Python) {
    Write-Fail "Python 3.11 or newer is required.`n  Download it from https://python.org`n  During install, check 'Add Python to PATH'."
}
Write-Ok "Found $(& $Python --version)"

# ── Step 2: Create install directory and venv ─────────────────────────────────
Write-Step 2 "Setting up install directory at $InstallDir..."

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}
# Restrict directory to current user (mirrors chmod 700)
icacls $InstallDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" 2>&1 | Out-Null

if (Test-Path $VenvDir) {
    Write-Ok "Existing environment found — upgrading in place"
} else {
    & $Python -m venv $VenvDir
    Write-Ok "Created isolated Python environment"
}

$VenvPy    = Join-Path $VenvDir "Scripts\python.exe"
$ValetBin  = Join-Path $VenvDir "Scripts\llm-valet.exe"

# ── Step 3: Install package ───────────────────────────────────────────────────
Write-Step 3 "Installing llm-valet..."

& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet --upgrade llm-valet
if ($LASTEXITCODE -ne 0) { Write-Fail "Package installation failed." }

$installedVer = & $VenvPy -m pip show llm-valet 2>$null | Select-String "^Version:" | ForEach-Object { $_ -replace "Version:\s*","" }
Write-Ok "Installed version $installedVer"

# ── Step 4: Write default config ──────────────────────────────────────────────
Write-Step 4 "Writing configuration..."

if (Test-Path $ConfigFile) {
    Write-Ok "Config already exists — keeping your existing settings"
} else {
    @'
# llm-valet configuration
# Full reference: https://github.com/LegionForge/llm-valet

host: 127.0.0.1
port: 8765
provider: ollama
ollama_url: http://127.0.0.1:11434
model_name:       # leave blank to auto-detect loaded model
api_key:          # required when host is not 127.0.0.1

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
    icacls $ConfigFile /inheritance:r /grant:r "${env:USERNAME}:F" 2>&1 | Out-Null
    Write-Ok "Default config written to $ConfigFile"
}

# ── Step 5: Task Scheduler auto-start ─────────────────────────────────────────
Write-Step 5 "Registering auto-start task..."

if (-not (Test-Path $ValetBin)) {
    Write-Warn "llm-valet.exe not found at expected path — skipping auto-start"
    Write-Warn "Start manually: $ValetBin"
} else {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    if ($existing) {
        # Update the existing task to pick up any path changes
        $action   = New-ScheduledTaskAction -Execute $ValetBin
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable
        Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings | Out-Null
        Write-Ok "Updated existing auto-start task"
    } else {
        $action   = New-ScheduledTaskAction -Execute $ValetBin
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
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
        Write-Ok "Registered auto-start task — llm-valet will start on login"
    }

    # Start now for this session
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Ok "Started for this session"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host ("━" * 40)
Write-Host "  WebUI:    http://localhost:8765"
Write-Host "  API docs: http://localhost:8765/docs"
Write-Host "  Config:   $ConfigFile"
Write-Host ""
Write-Host "  Start manually:   $ValetBin"
Write-Host "  Pause:            curl -X POST http://localhost:8765/pause"
Write-Host "  Resume:           curl -X POST http://localhost:8765/resume"
Write-Host ""
Write-Host "  To uninstall: run install\uninstall.ps1"
Write-Host ""
