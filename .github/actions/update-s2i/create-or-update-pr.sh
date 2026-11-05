#!/usr/bin/env bash
# Commit S2I updates and create their pull request when needed.

set -euo pipefail

branch="automation/update-s2i-requirements"

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git checkout -b "$branch"
git add -- "$REQUIREMENTS_FILE"
[ ! -f "$SOURCES_FILE" ] || git add -- "$SOURCES_FILE"
git commit -m "Update S2I requirements ($(date +%Y-%m-%d))"
git push --force --set-upstream origin "$branch"

existing=$(gh pr list --head "$branch" --json number --jq '.[0].number // empty')
if [ -z "$existing" ]; then
  body="Automated S2I update."
  body="${body}"$'\n\n'"Changes include:"
  body="${body}"$'\n'"- Regenerated \`$REQUIREMENTS_FILE\`"
  if [ -f "$SOURCES_FILE" ]; then
    body="${body}"$'\n'"- Updated the upper-constraints commit in \`$SOURCES_FILE\`"
  fi
  gh pr create --title "Update S2I requirements" --body "$body"
fi
