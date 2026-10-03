#!/usr/bin/env bash
# Download the HeroUI React v3 docs to web/.heroui-docs/ if they are missing.
# The SessionStart hook runs this script. Git ignores the docs, so a new clone,
# a new worktree, and a cloud session do not have them.
# The index of the docs is in web/CLAUDE.md. This script does not change it.
# To update the index after a HeroUI upgrade, run in web/:
#   npx -y heroui-cli@3.0.5 agents-md --react --output CLAUDE.md
set -u
cd "$(dirname "$0")/.." || exit 0
[ -d .heroui-docs/react ] && exit 0
# Pin the version. Thus, a new release runs only after a review of this file.
# The CLI also writes an index. Put it in the ignored directory, not in CLAUDE.md.
if ! HEROUI_ANALYTICS_DISABLED=1 npx -y heroui-cli@3.0.5 agents-md --react \
  --output .heroui-docs/INDEX.md >/dev/null 2>&1; then
  echo "heroui-docs: the download failed. Run web/scripts/heroui-docs.sh." >&2
fi
# Do not stop the session if the download fails.
exit 0
