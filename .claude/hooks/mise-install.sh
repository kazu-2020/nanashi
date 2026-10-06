#!/bin/bash
# Install the tools in mise.toml. Run only in a cloud session.
# The environment must allow mise.run, go.dev, and dl.google.com. A failure does not stop the session.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
export PATH="$HOME/.local/bin:$PATH"
command -v mise >/dev/null || (curl -fsSL https://mise.run | MISE_QUIET=1 sh) >/dev/null 2>&1
command -v mise >/dev/null || exit 0
mise trust --yes mise.toml >/dev/null 2>&1
mise install >&2 || true

# Use the tools from mise in this script and in the Bash tool.
# Do not use "mise exec". It stops if one tool in mise.toml is not installed.
env=$(mise env -s bash 2>/dev/null)
eval "$env"
[ -n "$CLAUDE_ENV_FILE" ] && echo "$env" >>"$CLAUDE_ENV_FILE"

# The LSP plugins look for the servers in PATH. Link the servers from mise into /usr/local/bin.
for bin in gopls typescript-language-server; do
  path=$(mise which "$bin" 2>/dev/null) && ln -sf "$path" "/usr/local/bin/$bin"
done
# "mise which" gives the rustup proxy. Link the binary of the toolchain in mise.toml.
path=$(rustup which rust-analyzer 2>/dev/null) && ln -sf "$path" /usr/local/bin/rust-analyzer

# typescript-language-server uses the TypeScript in web/node_modules.
(cd web && pnpm install --frozen-lockfile) >&2 || true
exit 0
