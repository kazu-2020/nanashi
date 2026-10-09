#!/bin/bash
# Install the Rust evaluator of Quint for Linux in ~/.quint (or QUINT_HOME).
# Quint downloads it on the first run, but first it asks the GitHub API for the release.
# The GitHub proxy of a cloud session blocks that request, and CI runners share the API rate limit.
# The download of the release file has neither problem.
# The npm package of Quint downloads from the same repository (quint-co/quint).
# If you change the Quint version in mise.toml, set EVALUATOR to QUINT_EVALUATOR_VERSION of that version.
# Then set the SHA-256 of each release file again. The script refuses a file with a different SHA-256.
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
mkdir -p "$dir"
tar -xz -C "$dir" -f "$tmp/evaluator.tar.gz"
