#!/usr/bin/env bash
# consumer install-everywhere — install/refresh the consumer skill in every
# detected runtime (Hermes, Claude Code, OpenClaw, Cursor, VS Code/Copilot).
# Usage: bash install-everywhere.sh [--vscode-code "Code"|"Code - Insiders"]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$HERE/skills/consumer"
VSCODE_CODE="Code"
[ "${1:-}" = "--vscode-code" ] && VSCODE_CODE="${2:-Code}"

say() { printf '[install-everywhere] %s\n' "$*"; }
pin() {  # pin resolved repo path into an installed SKILL.md
  printf '\n<!-- resolved repo path: %s -->\n' "$HERE" >> "$1/SKILL.md"
}

say "engine: $HERE"

# --- 1. Hermes (always) ---
mkdir -p "$HOME/.hermes/skills"
rm -rf "$HOME/.hermes/skills/consumer"
cp -R "$SKILL_SRC" "$HOME/.hermes/skills/consumer"
pin "$HOME/.hermes/skills/consumer"
say "Hermes: installed -> ~/.hermes/skills/consumer"

# --- 2. Claude Code (if dir present) ---
if [ -d "$HOME/.claude" ]; then
  mkdir -p "$HOME/.claude/skills"
  rm -rf "$HOME/.claude/skills/consumer"
  cp -R "$SKILL_SRC" "$HOME/.claude/skills/consumer"
  pin "$HOME/.claude/skills/consumer"
  say "Claude Code: installed -> ~/.claude/skills/consumer"
else
  say "Claude Code: not detected, skipped"
fi

# --- 3. OpenClaw (if present) ---
if [ -d "$HOME/.openclaw" ]; then
  mkdir -p "$HOME/.openclaw/skills"
  rm -rf "$HOME/.openclaw/skills/consumer"
  cp -R "$SKILL_SRC/openclaw/." "$HOME/.openclaw/skills/consumer"
  pin "$HOME/.openclaw/skills/consumer"
  say "OpenClaw: installed -> ~/.openclaw/skills/consumer"
else
  say "OpenClaw: not detected, skipped (install OpenClaw, then re-run)"
fi

# --- 4. Cursor (user skills dir present) ---
if [ -d "$HOME/.cursor" ]; then
  mkdir -p "$HOME/.cursor/skills"
  rm -rf "$HOME/.cursor/skills/consumer"
  cp -R "$SKILL_SRC" "$HOME/.cursor/skills/consumer"
  pin "$HOME/.cursor/skills/consumer"
  # project rule if inside a git repo
  if [ -d "$HERE/.git" ] || git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
    mkdir -p "$HERE/.cursor"
    rm -f "$HERE/.cursor/rules/consumer.mdc"
    mkdir -p "$HERE/.cursor/rules"
    cp "$SKILL_SRC/cursor/consumer.mdc" "$HERE/.cursor/rules/consumer.mdc"
    say "Cursor: skill -> ~/.cursor/skills/consumer + project rule .cursor/rules/consumer.mdc"
  else
    say "Cursor: skill -> ~/.cursor/skills/consumer (no git repo here, project rule skipped)"
  fi
else
  say "Cursor: not detected, skipped"
fi

# --- 5. VS Code / Copilot ---
VSCODE_USER="$HOME/Library/Application Support/$VSCODE_CODE/User"
if [ -d "$VSCODE_USER" ]; then
  # workspace mcp.json if this is a project
  if [ -d "$HERE/.git" ] || git -C "$HERE" rev-parse --git-dir >/dev/null 2>&1; then
    mkdir -p "$HERE/.vscode"
    touch "$HERE/.vscode/mcp.json"
    python3 - "$HERE/.vscode/mcp.json" "$HERE" <<'PYEOF'
import json, sys, os
path, here = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
servers = cfg.setdefault("servers", {})
servers["consumer-mcp"] = {
    "type": "stdio",
    "command": "python3",
    "args": [os.path.join(here, "mcp-server.py")],
    "env": {"BRADAN_GRAPH_DB": os.path.expanduser("~/consumer-out/consumer.sqlite3")},
}
json.dump(cfg, open(path, "w"), indent=2)
print("  workspace mcp.json updated (consumer-mcp registered)")
PYEOF
    say "VS Code: workspace .vscode/mcp.json -> consumer-mcp stdio"
  fi
  # user settings: skill path hint
  python3 - "$VSCODE_USER/settings.json" "$HOME/.cursor/skills/consumer" <<'PYEOF'
import json, sys, os
path, skill = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
key = "chat.agent.skills.customPaths"
try:
    paths = cfg.get(key, [])
except Exception:
    paths = []
if skill not in paths:
    paths.append(skill)
cfg[key] = paths
cfg["chat.agent.skills.customPaths"] = paths
json.dump(cfg, open(path, "w"), indent=2)
print("  user settings: chat.agent.skills.customPaths +", skill)
PYEOF
  say "VS Code: user settings skill path registered"
else
  say "VS Code: not detected, skipped"
fi

say "done. verify per RUNTIMES.md (claude /consumer, openclaw skills list, cursor agent)"
