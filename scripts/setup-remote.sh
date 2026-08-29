#!/usr/bin/env bash
# Wire this working copy to your office GitHub repo using values from .env.
# Safe to re-run. Nothing here is committed; it only touches local .git/config.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env found. Run:  cp .env.example .env  and fill it in."; exit 1; }
set -a; . ./.env; set +a
: "${GITHUB_REPO:?set GITHUB_REPO in .env}"
remote="${GITHUB_REMOTE:-origin}"

# For HTTPS + token pushing, embed the token in the local remote URL.
url="$GITHUB_REPO"
if [ -n "${GIT_PUSH_TOKEN:-}" ] && printf '%s' "$url" | grep -q '^https://'; then
  url="https://${GIT_PUSH_TOKEN}@$(printf '%s' "$GITHUB_REPO" | sed 's#^https://##')"
fi

if git remote | grep -qx "$remote"; then
  git remote set-url "$remote" "$url"
else
  git remote add "$remote" "$url"
fi

[ -n "${GIT_USER_NAME:-}" ]  && git config user.name  "$GIT_USER_NAME"
[ -n "${GIT_USER_EMAIL:-}" ] && git config user.email "$GIT_USER_EMAIL"

echo "Remote '$remote' -> $(git remote get-url "$remote" | sed 's#//[^@]*@#//***@#')"
echo
echo "Next:"
echo "  git add -A && git commit -m 'Initial IndexNow automation' && git push -u $remote main"
