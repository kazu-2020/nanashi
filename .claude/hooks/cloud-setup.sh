#!/bin/bash
# The setup script of the cloud environment runs this file before Claude Code starts.
# A cloud session does not install the plugins in .claude/settings.json, so this file installs them.
# Plugins load when Claude Code starts, so a SessionStart hook is too late for them.
cd "$(dirname "$0")/../.." || exit 0

# The cloud session does not have the claude-plugins-official marketplace. Add it from GitHub.
claude plugin marketplace add anthropics/claude-plugins-official >&2 || true
python3 -I -c '
import json
s = json.load(open(".claude/settings.json"))
for m in s.get("extraKnownMarketplaces", {}).values():
    print("marketplace", m["source"]["repo"])
for p, on in s.get("enabledPlugins", {}).items():
    if on:
        print("plugin", p)
' | while read -r kind name; do
  if [ "$kind" = marketplace ]; then
    claude plugin marketplace add "$name" </dev/null >&2 || true
  else
    claude plugin install "$name" </dev/null >&2 || true
  fi
done

# Install the tools now, so that the environment cache keeps them.
CLAUDE_CODE_REMOTE=true CLAUDE_PROJECT_DIR="$PWD" .claude/hooks/mise-install.sh
exit 0
