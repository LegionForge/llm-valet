#!/usr/bin/env bash
# llm-valet uninstaller — macOS and Linux
# Usage: bash uninstall.sh
#        bash uninstall.sh --purge    (also removes settings and logs)
set -euo pipefail

INSTALL_DIR="$HOME/.llm-valet"
PURGE=false

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
step()  { echo -e "\n${BOLD}$*${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}!${NC} $*"; }
die()   { echo -e "\n${RED}Error:${NC} $*\n" >&2; exit 1; }

# ── Parse flags ───────────────────────────────────────────────────────────────
for arg in "$@"; do
  [[ "$arg" == "--purge" ]] && PURGE=true
done

echo -e "\n${BOLD}llm-valet uninstaller${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "Do not run as root. Run as your normal user account."
fi

if [[ ! -d "$INSTALL_DIR" ]]; then
  echo "llm-valet does not appear to be installed (directory not found: $INSTALL_DIR)"
  exit 0
fi

# ── Step 1: Stop and remove the auto-start service ───────────────────────────
step "Removing auto-start service..."

if [[ "$(uname)" == "Darwin" ]]; then
  PLIST_LABEL="com.legionforge.llm-valet"
  PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
  UID_VAL="$(id -u)"

  if launchctl list "$PLIST_LABEL" &>/dev/null 2>&1; then
    launchctl bootout "gui/$UID_VAL" "$PLIST" 2>/dev/null || \
      launchctl unload "$PLIST" 2>/dev/null || true
    ok "LaunchAgent stopped"
  else
    ok "LaunchAgent was not running"
  fi

  if [[ -f "$PLIST" ]]; then
    rm -f "$PLIST"
    ok "Removed $PLIST"
  fi

elif [[ "$(uname)" == "Linux" ]]; then
  SERVICE_FILE="$HOME/.config/systemd/user/llm-valet.service"

  if command -v systemctl &>/dev/null; then
    systemctl --user stop    llm-valet 2>/dev/null || true
    systemctl --user disable llm-valet 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    ok "systemd user service stopped and disabled"
  fi

  if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    ok "Removed $SERVICE_FILE"
  fi
fi

# ── Step 2: Remove the installation ──────────────────────────────────────────
step "Removing llm-valet files..."

if ! $PURGE; then
  # Ask interactively if not already told to purge
  echo ""
  echo "  Your settings and logs are stored in: $INSTALL_DIR"
  echo ""
  read -r -p "  Remove settings and logs too? [y/N] " response
  [[ "${response,,}" == "y" || "${response,,}" == "yes" ]] && PURGE=true
  echo ""
fi

if $PURGE; then
  rm -rf "$INSTALL_DIR"
  ok "Removed $INSTALL_DIR (including settings and logs)"
else
  # Remove only the venv; leave config.yaml and log files
  rm -rf "$INSTALL_DIR/.venv"
  ok "Removed installed program files"
  warn "Your settings were kept: $INSTALL_DIR/config.yaml"
  warn "Run with --purge to remove everything, or delete manually: rm -rf $INSTALL_DIR"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}llm-valet has been uninstalled.${NC}"
echo "No other changes were made to your system."
echo ""
