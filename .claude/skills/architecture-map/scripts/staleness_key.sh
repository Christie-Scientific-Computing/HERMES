#!/usr/bin/env bash
# Computes a staleness key for a repository, used to detect whether an
# existing ARCHITECTURE.md map is still fresh.
#
# For a git repo: hashes (HEAD commit + working-tree status), so both new
# commits AND uncommitted/staged/untracked changes invalidate the key.
# `git status --porcelain` is index-based and does not read file contents,
# so this stays fast even on large repos.
#
# For a non-git directory: falls back to a structural fingerprint of
# (path, size, mtime) across the tree. Also stat-based, never reads file
# contents.
#
# Usage: staleness_key.sh [repo-root]
# Output (two lines):
#   key=<git|fingerprint>:<hash>   -- opaque freshness check
#   head=<commit-hash-or-empty>    -- git HEAD at generation time, if
#                                      applicable; lets a caller diff
#                                      what changed for a targeted refresh
#                                      (the composite key itself is not
#                                      reversible for that purpose)

set -euo pipefail

REPO_ROOT="${1:-.}"
cd "$REPO_ROOT"

IGNORE_DIRS='(^|/)(\.git|node_modules|__pycache__|\.venv|venv|dist|build|target|\.next|\.cache|coverage)(/|$)'

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HEAD_HASH=$(git rev-parse HEAD 2>/dev/null || echo "")
  STATUS=$(git status --porcelain 2>/dev/null || echo "")
  KEY=$(printf '%s\n%s' "$HEAD_HASH" "$STATUS" | sha256sum | cut -d' ' -f1)
  echo "key=git:${KEY}"
  echo "head=${HEAD_HASH}"
else
  # Non-git fallback: stat-based fingerprint, never reads file contents,
  # so cost stays proportional to file count, not file size.
  FINGERPRINT=$(find . -type f \
      | grep -Ev "$IGNORE_DIRS" \
      | sort \
      | xargs -I{} stat -c '%n:%s:%Y' {} 2>/dev/null \
      | sha256sum | cut -d' ' -f1)
  echo "key=fingerprint:${FINGERPRINT}"
  echo "head="
fi
