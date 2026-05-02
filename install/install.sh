#!/usr/bin/env bash
# llm-valet installer — macOS and Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/LegionForge/llm-valet/main/install/install.sh | bash
set -euo pipefail

INSTALL_DIR="$HOME/.llm-valet"
VENV_DIR="$INSTALL_DIR/.venv"
CONFIG_FILE="$INSTALL_DIR/config.yaml"
PACKAGE="legionforge-llm-valet"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
STEPS=5
FRESH_INSTALL=false
API_KEY=""

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
step()  { echo -e "\n${BOLD}[$1/$STEPS]${NC} $2"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!${NC} $*"; }
die()   { echo -e "\n${RED}Error:${NC} $*\n" >&2; exit 1; }

echo -e "\n${BOLD}llm-valet installer${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Safety ───────────────────────────────────────────────────────────────────
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Do not run as root. Run as your normal user account."
fi

# ── Step 1: Python ────────────────────────────────────────────────────────────
step 1 "Checking Python version..."
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR) else 1)" 2>/dev/null; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]] && [[ "$(uname)" == "Darwin" ]]; then
  # Homebrew installs Python into a prefix that is not on the PATH in
  # non-interactive shells (curl-pipe installs, launchd, SSH without a
  # login shell).  Source the Homebrew environment and retry.
  BREW_SHELLENV=""
  for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$brew_bin" ]]; then
      BREW_SHELLENV="$("$brew_bin" shellenv)"
      break
    fi
  done
  if [[ -n "$BREW_SHELLENV" ]]; then
    eval "$BREW_SHELLENV"
    for cmd in python3 python; do
      if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR) else 1)" 2>/dev/null; then
          PYTHON="$cmd"
          break
        fi
      fi
    done
  fi
fi

if [[ -z "$PYTHON" ]]; then
  die "Python $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR or newer is required.\n  macOS: brew install python\n  Ubuntu/Debian: sudo apt install python3\n  Download: https://python.org"
fi
ok "Found $($PYTHON --version)"

# ── Step 2: Create install directory ─────────────────────────────────────────
step 2 "Setting up install directory at $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR"

# Create the isolated virtual environment (or reuse it on upgrade)
if [[ -d "$VENV_DIR" ]]; then
  ok "Existing environment found — upgrading in place"
else
  "$PYTHON" -m venv "$VENV_DIR"
  ok "Created isolated Python environment"
fi

VENV_PY="$VENV_DIR/bin/python"
VALET_BIN="$VENV_DIR/bin/llm-valet"

# ── Step 3: Install package ───────────────────────────────────────────────────
step 3 "Installing $PACKAGE..."
"$VENV_PY" -m pip install --quiet --upgrade pip

# If this script is running from inside a cloned repo (pyproject.toml exists
# one level up), install from local source. Otherwise install from PyPI.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$REPO_ROOT/pyproject.toml" ]]; then
  ok "Local repo detected — installing from source"
  "$VENV_PY" -m pip install --quiet -e "$REPO_ROOT"
else
  "$VENV_PY" -m pip install --quiet --upgrade legionforge-llm-valet
fi
ok "Installed $("$VENV_PY" -m pip show llm-valet 2>/dev/null | awk '/^Version:/{print $2}')"

# ── Step 4: Write default config ──────────────────────────────────────────────
step 4 "Writing configuration..."
if [[ -f "$CONFIG_FILE" ]]; then
  ok "Config already exists — keeping your existing settings"
else
  FRESH_INSTALL=true
  if command -v openssl &>/dev/null; then
    API_KEY=$(openssl rand -hex 32)
  else
    API_KEY=$("$PYTHON" -c "import secrets; print(secrets.token_hex(32))")
  fi
  cat > "$CONFIG_FILE" <<EOF
# llm-valet configuration
# Full reference: https://github.com/LegionForge/llm-valet

host: 127.0.0.1
port: 8765
provider: ollama
ollama_url: http://127.0.0.1:11434
model_name:       # leave blank to auto-detect loaded model
api_key: $API_KEY

thresholds:
  ram_pause_pct: 85.0
  ram_resume_pct: 60.0
  cpu_pause_pct: 90.0
  cpu_sustained_seconds: 30
  gpu_vram_pause_pct: 85.0
  pause_timeout_seconds: 120
  check_interval_seconds: 10
EOF
  chmod 600 "$CONFIG_FILE"
  ok "Default config written to $CONFIG_FILE"
fi

# ── Step 5: Register auto-start service ──────────────────────────────────────
step 5 "Registering auto-start service..."

if [[ "$(uname)" == "Darwin" ]]; then
  # ── macOS: LaunchAgent ─────────────────────────────────────────────────────
  LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
  PLIST_LABEL="com.legionforge.llm-valet"
  PLIST="$LAUNCH_AGENTS/$PLIST_LABEL.plist"
  mkdir -p "$LAUNCH_AGENTS"

  # Unload any previous version before writing new plist
  UID_VAL="$(id -u)"
  launchctl bootout "gui/$UID_VAL" "$PLIST" 2>/dev/null || true

  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VALET_BIN}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${INSTALL_DIR}/valet.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${INSTALL_DIR}/valet.stderr.log</string>
</dict>
</plist>
EOF
  chmod 644 "$PLIST"
  launchctl bootstrap "gui/$UID_VAL" "$PLIST" 2>/dev/null && ok "LaunchAgent registered — starts automatically at login" || warn "LaunchAgent registered (will start on next login)"

elif [[ "$(uname)" == "Linux" ]]; then
  # ── Linux: systemd user service ────────────────────────────────────────────
  if ! command -v systemctl &>/dev/null; then
    warn "systemctl not found — auto-start not configured"
    warn "Start manually: $VALET_BIN"
  else
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    SERVICE="$SYSTEMD_DIR/llm-valet.service"
    mkdir -p "$SYSTEMD_DIR"

    cat > "$SERVICE" <<EOF
[Unit]
Description=llm-valet — LLM lifecycle manager
After=network.target

[Service]
ExecStart=${VALET_BIN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable llm-valet
    systemctl --user start  llm-valet 2>/dev/null || true
    ok "systemd user service enabled — starts automatically at login"
  fi

else
  warn "Unsupported platform '$(uname)' — auto-start not configured"
  warn "Start manually: $VALET_BIN"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Installation complete!${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  WebUI:    http://localhost:8765"
echo "  API docs: http://localhost:8765/docs"
echo "  Config:   $CONFIG_FILE"
echo ""
if [[ "$FRESH_INSTALL" == "true" ]]; then
  echo -e "  ${YELLOW}${BOLD}API key (save this):${NC} $API_KEY"
  echo "  Required for LAN access (when host: 0.0.0.0 in config)."
  echo ""
fi
echo "  Start manually:   $VALET_BIN"
echo "  Pause:            curl -X POST http://localhost:8765/pause"
echo "  Resume:           curl -X POST http://localhost:8765/resume"
echo ""
echo "  To uninstall: bash <(curl -fsSL https://raw.githubusercontent.com/LegionForge/llm-valet/main/install/uninstall.sh)"
echo ""
