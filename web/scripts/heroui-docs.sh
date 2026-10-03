#!/usr/bin/env bash
# Download the HeroUI React v3 docs to web/.heroui-docs/ if they are missing.
# The SessionStart hook runs this script. Git ignores the docs, so a new clone,
# a new worktree, and a cloud session do not have them.
# The index of the docs is in web/CLAUDE.md. This script does not change it.
# To update the index after a HeroUI upgrade, run in web/:
#   pnpm dlx heroui-cli@3.0.5 agents-md --react --output CLAUDE.md
set -u
# Pin the version. Thus, a new release runs only after a review of this file.
version=3.0.5
cd "$(dirname "$0")/.." || exit 0
# The marker is written last. If it is missing or old, download the docs again.
marker=.heroui-docs/.version
[ "$(cat "$marker" 2>/dev/null)" = "$version" ] && exit 0
# Download to a staging directory. Replace the docs only after a success.
# The CLI also writes an index. Keep it in the docs directory, not in CLAUDE.md.
staging=$(mktemp -d) || exit 0
trap 'rm -rf "$staging"' EXIT
if (cd "$staging" && HEROUI_ANALYTICS_DISABLED=1 pnpm dlx "heroui-cli@$version" \
  agents-md --react --output .heroui-docs/INDEX.md >/dev/null 2>&1) &&
  [ -n "$(ls -A "$staging/.heroui-docs/react" 2>/dev/null)" ]; then
  rm -rf .heroui-docs && mv "$staging/.heroui-docs" .heroui-docs &&
    echo "$version" >"$marker"
else
  echo "heroui-docs: the download failed. Run web/scripts/heroui-docs.sh." >&2
fi
# Do not stop the session if the download fails.
exit 0
