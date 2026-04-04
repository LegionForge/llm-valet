#!/usr/bin/env bash
# llm-valet install script — macOS / Linux
# Usage: curl -fsSL https://raw.githubusercontent.com/LegionForge/llm-valet/main/install/install.sh | bash
set -euo pipefail

REPO="LegionForge/llm-valet"
CONFIG_DIR="$HOME/.llm-valet"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
MIN_PYTHON="3.11"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[llm-valet]${NC} $*"; }
warn()    { echo -e "${YELLOW}[llm-valet]${NC} $*"; }
error()   { echo -e "${RED}[llm-valet]${NC} $*" >&2; exit 1; }

# ── Safety: refuse root ───────────────────────────────────────────────────────
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  error "Do not run this installer as root. Run as your normal user."
fi

# ── Python version check ──────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c "import sys; print('%d.%d' % sys.version_info[:2])")
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null \
       || "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  error "Python $MIN_PYTHON+ is required. Install it and re-run."
fi
info "Using Python: $($PYTHON --version)"

# ── Install package ───────────────────────────────────────────────────────────
info "Installing llm-valet..."
"$PYTHON" -m pip install --upgrade "llm-valet" 2>&1 | tail -5

# ── Create config directory ───────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -f "$CONFIG_FILE" ]]; then
  info "Writing default config to $CONFIG_FILE"
  cat > "$CONFIG_FILE" <<'EOF'
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
EOF
  chmod 600 "$CONFIG_FILE"
else
  warn "Config already exists — skipping: $CONFIG_FILE"
fi

# ── macOS: install LaunchAgent ────────────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
  LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
  PLIST="$LAUNCH_AGENTS/com.legionforge.llm-valet.plist"
  VALET_BIN="$(command -v llm-valet 2>/dev/null || echo "")"

  if [[ -z "$VALET_BIN" ]]; then
    warn "llm-valet binary not found in PATH — skipping LaunchAgent install"
  else
    mkdir -p "$LAUNCH_AGENTS"
    info "Installing LaunchAgent: $PLIST"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.legionforge.llm-valet</string>
  <key>ProgramArguments</key>
  <array>
    <string>${VALET_BIN}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${CONFIG_DIR}/valet.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>${CONFIG_DIR}/valet.stderr.log</string>
</dict>
</plist>
EOF
    chmod 644 "$PLIST"

    # Load the agent for the current session
    UID_VAL="$(id -u)"
    launchctl bootstrap "gui/$UID_VAL" "$PLIST" 2>/dev/null || true
    info "LaunchAgent loaded — llm-valet will auto-start on login"
  fi
fi

# ── Linux: install systemd user service ──────────────────────────────────────
if [[ "$(uname)" == "Linux" ]]; then
  SYSTEMD_DIR="$HOME/.config/systemd/user"
  SERVICE="$SYSTEMD_DIR/llm-valet.service"
  VALET_BIN="$(command -v llm-valet 2>/dev/null || echo "")"

  if [[ -z "$VALET_BIN" ]]; then
    warn "llm-valet binary not found in PATH — skipping systemd service install"
  elif command -v systemctl &>/dev/null; then
    mkdir -p "$SYSTEMD_DIR"
    info "Installing systemd user service: $SERVICE"
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
    systemctl --user start  llm-valet
    info "systemd user service enabled and started"
  else
    warn "systemctl not found — skipping service install (run llm-valet manually)"
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Installation complete."
echo ""
echo "  WebUI:   http://localhost:8765"
echo "  API docs: http://localhost:8765/docs"
echo "  Config:   $CONFIG_FILE"
echo ""
echo "  Manual start:  llm-valet"
echo "  Manual control: curl -X POST http://localhost:8765/pause"
echo ""
