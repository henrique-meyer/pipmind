#!/usr/bin/env bash
set -euo pipefail

# A completed main CI run includes publication/deployment when the site changed.
# Compare cumulatively so a replaced pending run or failed release cannot lose a site change.
BASE_SHA=$(gh api --method GET "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs" -f branch=main -f event=push -f status=success -f per_page=1 --jq '.workflow_runs[0].head_sha // empty')
if test -z "$BASE_SHA"; then
  echo 'changed=true' >> "$GITHUB_OUTPUT"
  exit 0
fi
[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
git merge-base --is-ancestor "$BASE_SHA" "$GITHUB_SHA"
if git diff --quiet "$BASE_SHA" "$GITHUB_SHA" -- app web assets Dockerfile .dockerignore .gitattributes pyproject.toml uv.lock scripts/smoke.py .github/scripts/landing-changes.sh .github/workflows/ci.yml .github/workflows/publish.yml .github/workflows/deploy.yml; then
  echo 'changed=false' >> "$GITHUB_OUTPUT"
else
  RESULT=$?
  test "$RESULT" -eq 1 || exit "$RESULT"
  echo 'changed=true' >> "$GITHUB_OUTPUT"
fi
