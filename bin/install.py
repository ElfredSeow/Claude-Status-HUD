"""
Installer for the Claude Code Status HUD.

  python install.py            # merge hooks into ~/.claude/settings.json (backup first)
  python install.py --uninstall  # remove the HUD hooks again

Only touches the "hooks" section; every other setting is preserved. A timestamped
backup of settings.json is written next to it before any change.
"""

import os
import sys
import json
import time
import shutil

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(PROJECT, "hooks", "hud_hook.py")

PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

MARKER = "claude-status-hud"  # lets us identify/remove our own hooks
COMMAND = f'"{PYTHON}" "{HOOK}"'

# Events that should register the HUD hook. PreToolUse/PostToolUse match all tools.
EVENTS = {
    "SessionStart":     None,
    "UserPromptSubmit": None,
    "PreToolUse":       "*",
    "PostToolUse":      "*",
    "Notification":     None,
    "Stop":             None,
    "SubagentStop":     None,
    "SessionEnd":       None,
}


def load():
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def backup():
    if os.path.exists(SETTINGS):
        dst = f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(SETTINGS, dst)
        return dst
    return None


def is_ours(entry):
    for h in entry.get("hooks", []):
        if MARKER in h.get("command", ""):
            return True
    return False


def write(cfg):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, SETTINGS)


def strip_ours(cfg):
    hooks = cfg.get("hooks", {})
    for ev in list(hooks.keys()):
        hooks[ev] = [e for e in hooks[ev] if not is_ours(e)]
        if not hooks[ev]:
            del hooks[ev]
    if "hooks" in cfg and not cfg["hooks"]:
        del cfg["hooks"]
    return cfg


def install():
    cfg = load()
    b = backup()
    strip_ours(cfg)  # avoid duplicates on re-run
    hooks = cfg.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": COMMAND}]}
        if matcher is not None:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    write(cfg)
    print("Installed HUD hooks into", SETTINGS)
    if b:
        print("Backup saved to", b)
    print("Hooked events:", ", ".join(EVENTS))


def uninstall():
    cfg = load()
    b = backup()
    strip_ours(cfg)
    write(cfg)
    print("Removed HUD hooks.")
    if b:
        print("Backup saved to", b)


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
