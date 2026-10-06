#!/bin/bash
# Install the tools in mise.toml. Run only in a cloud session.
# The network policy can block some downloads. A failure does not stop the session.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
# Use the official install script. mise.run is blocked, so get the same script from mise.jdx.dev.
# If the script fails, get mise from the npm registry.
export PATH="$HOME/.local/bin:$PATH"
command -v mise >/dev/null || (curl -fsSL https://mise.jdx.dev/install.sh | MISE_QUIET=1 sh) >/dev/null 2>&1
command -v mise >/dev/null || npm install -g @jdxcode/mise >/dev/null 2>&1
command -v mise >/dev/null || exit 0
mise trust --yes mise.toml >/dev/null 2>&1

# The network policy blocks go.dev. If mise cannot install Go, get the same Go from proxy.golang.org.
if ! mise install go >/dev/null 2>&1; then
  v=$(sed -n 's/^go = "\(.*\)"$/\1/p' mise.toml)
  dir="$HOME/.cache/go-toolchain/$v"
  if [ ! -x "$dir/bin/go" ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/go.zip" "https://proxy.golang.org/golang.org/toolchain/@v/v0.0.1-go$v.linux-amd64.zip" &&
      unzip -q "$tmp/go.zip" -d "$tmp" &&
      mkdir -p "$(dirname "$dir")" &&
      mv "$tmp/golang.org/toolchain@v0.0.1-go$v.linux-amd64" "$dir"
    rm -rf "$tmp"
  fi
  [ -x "$dir/bin/go" ] && mise link --force "go@$v" "$dir" >/dev/null 2>&1
fi

mise install >&2 || true

# Give the Bash tool the tools from mise.
[ -n "$CLAUDE_ENV_FILE" ] && mise env -s bash >>"$CLAUDE_ENV_FILE"

# The LSP plugins look for the servers in PATH. Link the servers from mise into /usr/local/bin.
for bin in gopls typescript-language-server; do
  path=$(mise which "$bin" 2>/dev/null) && ln -sf "$path" "/usr/local/bin/$bin"
done
# "mise which" gives the rustup proxy. Link the binary of the toolchain in mise.toml.
path=$(mise exec -- rustup which rust-analyzer 2>/dev/null) && ln -sf "$path" /usr/local/bin/rust-analyzer
exit 0
