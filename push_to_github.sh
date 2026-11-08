#!/usr/bin/env bash
set -euo pipefail

REPO_NAME="${1:-yc-prospect-kit}"
if ! command -v git >/dev/null 2>&1; then
  echo "git not installed: install Xcode Command Line Tools (xcode-select --install)" >&2
  exit 1
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI not installed. On Mac: brew install gh; then: gh auth login" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Not authenticated to GitHub. Run: gh auth login" >&2
  exit 1
fi
if [ ! -d .git ]; then
  git init -b main
fi
if [ -n "$(git remote)" ]; then
  echo "This local folder already has a Git remote. Refusing to overwrite or choose a different remote." >&2
  exit 1
fi
if [ -z "$(git config user.email || true)" ] || [ -z "$(git config user.name || true)" ]; then
  echo "Configure Git identity first: git config --global user.name 'Your Name'; git config --global user.email 'you@example.com'" >&2
  exit 1
fi
# Only code/docs are staged; secrets, tokens, and data are ignored.
git add .
if ! git rev-parse --verify HEAD >/dev/null 2>&1 || ! git diff --cached --quiet; then
  git commit -m "Initial YC Prospect Kit implementation"
fi
gh repo create "$REPO_NAME" --private --source=. --remote=origin --push
printf '\nPublished to private GitHub repository: %s\n' "$(gh repo view --json url -q .url)"
