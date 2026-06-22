#!/bin/bash
# =============================================================================
#  Claude Traffic Light — Mac Installer
#  Double-click this file in Finder to run.
#  It will open a Terminal window and guide you through setup.
# =============================================================================

# Change to the folder this script lives in (so relative paths work).
cd "$(dirname "$0")" || exit 1

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
err()  { echo -e "  ${RED}✗${RESET}  $1"; }
h1()   { echo -e "\n${BOLD}${CYAN}$1${RESET}"; echo -e "${DIM}$(printf '─%.0s' {1..50})${RESET}"; }

clear
echo ""
echo -e "${BOLD}  🚦  Claude Traffic Light${RESET}"
echo -e "      Mac Setup Installer"
echo ""
echo -e "${DIM}  This will connect your Mac's Claude Code sessions"
echo -e "  to the HUD dashboard running on your Windows PC.${RESET}"
echo ""

# ── Step 1: Windows IP ───────────────────────────────────────────────────────
h1 "Step 1 of 5 — Windows PC address"
echo ""
echo -e "  ${DIM}On your Windows PC, open Command Prompt and type:${RESET}"
echo -e "  ${YELLOW}  ipconfig${RESET}"
echo -e "  ${DIM}Look for 'IPv4 Address' (e.g. 192.168.1.10)${RESET}"
echo ""

while true; do
    read -rp "  Enter the Windows IP address: " WINDOWS_IP
    # Basic IPv4 sanity check
    if [[ "$WINDOWS_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        break
    fi
    err "That doesn't look like a valid IP (e.g. 192.168.1.10). Try again."
done

HUD_URL="http://${WINDOWS_IP}:51790"
ok "Will connect to: ${HUD_URL}"

# ── Step 2: Device name ──────────────────────────────────────────────────────
h1 "Step 2 of 5 — Name this Mac"
echo ""
echo -e "  ${DIM}This name appears on the Windows HUD overlay.${RESET}"
read -rp "  Device name [MacBook]: " DEVICE_NAME
DEVICE_NAME="${DEVICE_NAME:-MacBook}"
DEVICE_ID="mac"
ok "This device will appear as: ${BOLD}${DEVICE_NAME}${RESET}"

# ── Step 3: Check Python 3 ───────────────────────────────────────────────────
h1 "Step 3 of 5 — Checking Python 3"
echo ""

if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    ok "Found: ${PY_VER}"
else
    err "Python 3 is not installed."
    echo ""
    echo -e "  ${DIM}Install it from:${RESET} https://www.python.org/downloads/"
    echo -e "  ${DIM}Or via Homebrew:${RESET} brew install python3"
    echo ""
    read -rp "  Press Enter to close..."
    exit 1
fi

# ── Step 4: Test connection ──────────────────────────────────────────────────
h1 "Step 4 of 5 — Testing connection to Windows"
echo ""
echo -e "  ${DIM}Sending a test ping to ${HUD_URL} …${RESET}"

RESPONSE=$(curl -s -X POST "${HUD_URL}" \
    -H "Content-Type: application/json" \
    -d "{\"device_id\":\"${DEVICE_ID}\",\"device_name\":\"${DEVICE_NAME}\",\"session_id\":\"install_test\",\"state\":\"idle\",\"event\":\"install\"}" \
    --connect-timeout 4 2>&1)

if [ "$RESPONSE" = "ok" ]; then
    ok "Connected! The Windows HUD responded correctly."
    echo -e "  ${DIM}(Check your Windows PC — the overlay may have briefly flashed green)${RESET}"
else
    warn "Could not reach ${HUD_URL}"
    echo ""
    echo -e "  ${DIM}Before continuing, make sure:${RESET}"
    echo -e "  ${DIM}  • The HUD is running on your Windows PC (tray icon visible)${RESET}"
    echo -e "  ${DIM}  • Both Mac and PC are on the same WiFi network${RESET}"
    echo -e "  ${DIM}  • Windows Firewall allows port 51790 (see setup-windows.html)${RESET}"
    echo ""
    read -rp "  Continue anyway? (y/N): " CONT
    [[ "$CONT" =~ ^[Yy]$ ]] || { read -rp "  Press Enter to close..."; exit 1; }
fi

# ── Step 5: Install ──────────────────────────────────────────────────────────
h1 "Step 5 of 5 — Installing"
echo ""

# 5a — Copy the hook file
HOOK_DIR="$HOME/.claude/hud"
HOOK_FILE="$HOOK_DIR/remote_hook.py"
mkdir -p "$HOOK_DIR"
cp "$(dirname "$0")/remote_hook.py" "$HOOK_FILE"
chmod +x "$HOOK_FILE"
ok "Hook copied to ~/.claude/hud/remote_hook.py"

# 5b — Persist env vars in the user's shell rc
SHELL_RC="$HOME/.zshrc"
[ -f "$HOME/.bash_profile" ] && [ ! -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.bash_profile"

# Remove any previous Claude HUD entries
if [ -f "$SHELL_RC" ]; then
    grep -v "CLAUDE_HUD_" "$SHELL_RC" > "${SHELL_RC}.tmp" && mv "${SHELL_RC}.tmp" "$SHELL_RC"
fi

{
    echo ""
    echo "# Claude Traffic Light HUD — added by installer"
    echo "export CLAUDE_HUD_URL=\"${HUD_URL}\""
    echo "export CLAUDE_HUD_DEVICE_ID=\"${DEVICE_ID}\""
    echo "export CLAUDE_HUD_DEVICE_NAME=\"${DEVICE_NAME}\""
} >> "$SHELL_RC"
ok "Saved connection settings to ${SHELL_RC}"

# 5c — Register Claude Code hooks
CMD="CLAUDE_HUD_URL=\"${HUD_URL}\" CLAUDE_HUD_DEVICE_ID=\"${DEVICE_ID}\" CLAUDE_HUD_DEVICE_NAME=\"${DEVICE_NAME}\" python3 \"${HOOK_FILE}\""

python3 - <<PYEOF
import os, json, shutil, time

SETTINGS  = os.path.expanduser("~/.claude/settings.json")
CMD       = r"""${CMD}"""
MARKER    = "claude-hud-remote"
EVENTS    = ["SessionStart","UserPromptSubmit","PreToolUse","PostToolUse",
             "Notification","Stop","SubagentStop","SessionEnd"]
TOOL_EVTS = {"PreToolUse","PostToolUse"}

cfg = {}
if os.path.exists(SETTINGS):
    shutil.copy2(SETTINGS, "{}.bak-{}".format(SETTINGS, time.strftime("%Y%m%d-%H%M%S")))
    try:
        with open(SETTINGS) as f:
            cfg = json.load(f)
    except Exception:
        pass

# Strip any previous remote hooks so re-runs don't duplicate.
hooks = cfg.setdefault("hooks", {})
for ev in list(hooks.keys()):
    hooks[ev] = [e for e in hooks[ev]
                 if not any(MARKER in h.get("command", "")
                            for h in e.get("hooks", []))]
    if not hooks[ev]:
        del hooks[ev]

hooks = cfg.setdefault("hooks", {})
for ev in EVENTS:
    entry = {"hooks": [{"type": "command", "command": CMD}]}
    if ev in TOOL_EVTS:
        entry["matcher"] = "*"
    hooks.setdefault(ev, []).append(entry)

os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
with open(SETTINGS, "w") as f:
    json.dump(cfg, f, indent=2)
print("ok")
PYEOF

if [ $? -eq 0 ]; then
    ok "Claude Code hooks registered in ~/.claude/settings.json"
else
    err "Hook registration failed — check ~/.claude/settings.json manually"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}  ✅  Installation complete!${RESET}"
echo ""
echo -e "  ${DIM}What to do next:${RESET}"
echo -e "  ${BOLD}1.${RESET} Make sure the HUD is running on your Windows PC"
echo -e "  ${BOLD}2.${RESET} Open a new Claude Code session on this Mac"
echo -e "  ${BOLD}3.${RESET} Watch the Windows overlay update within 1 second 🚦"
echo ""
echo -e "  ${DIM}The overlay will show '${DEVICE_NAME}: Working…' when this Mac is busy.${RESET}"
echo -e "  ${DIM}Right-click the overlay on Windows → Primary device to change priority.${RESET}"
echo ""
read -rp "  Press Enter to close..."
