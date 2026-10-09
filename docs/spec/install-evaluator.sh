#!/bin/bash
# Install the Rust evaluator of Quint for Linux in ~/.quint (or QUINT_HOME).
# Quint downloads it on the first run, but first it asks the GitHub API for the release.
# The GitHub proxy of a cloud session blocks that request, and CI runners share the API rate limit.
# This script downloads the release file directly, from the repository that Quint uses (quint-co/quint).
# If you change the Quint version in mise.toml, set EVALUATOR to QUINT_EVALUATOR_VERSION of that version.
# Then set the SHA-256 of each release file again.
set -euo pipefail
EVALUATOR=v0.7.0
[ "$(uname -s)" = Linux ] || exit 0
dir="${QUINT_HOME:-$HOME/.quint}/rust-evaluator-$EVALUATOR"
[ -x "$dir/quint_evaluator" ] && exit 0
case "$(uname -m)" in
  aarch64 | arm64) arch=aarch64 sha=26c052831243cdba9cd81f8da0c1df053f3c92cfd3fd8f050c87c45642414f93 ;;
  *) arch=x86_64 sha=869adb8762a518483eb39f67fde622253a58527494e15afe77d610a0b6dab7d2 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/evaluator.tar.gz" \
  "https://github.com/quint-co/quint/releases/download/evaluator%2F$EVALUATOR/quint_evaluator-$arch-unknown-linux-gnu.tar.gz"
echo "$sha  $tmp/evaluator.tar.gz" | sha256sum -c --quiet
# Extract in $tmp first. A stopped extraction must not leave a partial evaluator in $dir.
mkdir "$tmp/out"
tar -xz -C "$tmp/out" -f "$tmp/evaluator.tar.gz"
mkdir -p "$(dirname "$dir")"
rm -rf "$dir"
mv "$tmp/out" "$dir"
