#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .git ]; then echo 'No Git repository here. Use the existing yc-prospect-kit folder.'; exit 1; fi
REMOTE="$(git remote get-url origin 2>/dev/null || true)"
case "$REMOTE" in
  *Jha1103/yc-prospect-kit*|*Jha1103:yc-prospect-kit*) ;;
  *) echo "Unexpected Git remote: $REMOTE"; echo 'Refusing to push to an unrelated repository.'; exit 1;;
esac
FILES=(
  README.md EMAIL_DISCOVERY_FIX.md
  ycprospect/web.py ycprospect/db.py ycprospect/discovery.py ycprospect/sources.py
  ycprospect/webassets/index.html ycprospect/webassets/dashboard.css ycprospect/webassets/dashboard.js
  tests/test_web.py tests/test_multisource.py
  'Start YC Prospect Kit.command' 'Publish Dashboard Update.command'
)
git add -- "${FILES[@]}"
if ! git diff --cached --quiet -- "${FILES[@]}"; then
  git commit -m 'Add multi-source startup discovery and founder outreach dashboard' -- "${FILES[@]}"
fi
git push origin HEAD:main
echo 'Dashboard code published.'
open 'https://github.com/Jha1103/yc-prospect-kit'
