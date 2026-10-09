#!/bin/bash
# Install the Rust evaluator of Quint for Linux in ~/.quint (or QUINT_HOME).
# Quint downloads it on the first run, but first it asks the GitHub API for the release.
# The GitHub proxy of a cloud session blocks that request, and CI runners share the API rate limit.
# The download of the release file has neither problem.
# If you change the Quint version in mise.toml, set EVALUATOR to QUINT_EVALUATOR_VERSION of that version.
set -euo pipefail
EVALUATOR=v0.7.0
[ "$(uname -s)" = Linux ] || exit 0
dir="${QUINT_HOME:-$HOME/.quint}/rust-evaluator-$EVALUATOR"
[ -x "$dir/quint_evaluator" ] && exit 0
case "$(uname -m)" in
  aarch64 | arm64) arch=aarch64 ;;
  *) arch=x86_64 ;;
esac
mkdir -p "$dir"
curl -fsSL "https://github.com/quint-co/quint/releases/download/evaluator%2F$EVALUATOR/quint_evaluator-$arch-unknown-linux-gnu.tar.gz" |
  tar -xz -C "$dir"
