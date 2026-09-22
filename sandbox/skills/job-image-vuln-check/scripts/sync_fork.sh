#!/usr/bin/env bash
# sync_fork.sh - fast-forward a fork's default branch to its upstream's
# actual default branch (discovered via the API, never assumed), then push
# the fork. Never forces a merge/rebase past a divergence.
#
# Usage:
#   FORK=<owner>/<repo> UPSTREAM=<owner>/<repo> [DIR=<path>] ./sync_fork.sh
#
# DIR (default: the repo's own name, e.g. "rosa-agent") is where the clone
# lives; it's created via `gh repo clone` if it doesn't exist yet. On
# success, prints the synced default branch name on stdout and leaves the
# repo checked out there.
#
# Exit codes:
#   0  synced and pushed
#   1  usage error (a required env var is unset)
#   2  the fork has diverged from upstream (a --ff-only merge would fail) -
#      a failure Issue is filed against openshift-online/rosa-agent; a human
#      must resolve the divergence, this script never forces past it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

: "${FORK:?set FORK (owner/repo of your fork)}"
: "${UPSTREAM:?set UPSTREAM (owner/repo to sync from)}"
: "${DIR:=${FORK##*/}}"

DEFAULT_BRANCH="$(gh api "repos/$UPSTREAM" --jq '.default_branch')"

if [ ! -d "$DIR/.git" ]; then
  gh repo clone "$FORK" "$DIR" -- --origin origin
fi
cd "$DIR"

git remote add upstream "https://github.com/$UPSTREAM.git" 2>/dev/null || true
git fetch upstream "$DEFAULT_BRANCH" >&2
git fetch origin "$DEFAULT_BRANCH" >&2
# Redirect these to stderr: git's own status chatter ("Switched to branch
# ...", "Your branch is up to date...") otherwise lands on stdout too,
# breaking the "only the branch name goes to stdout" contract below.
git checkout "$DEFAULT_BRANCH" >&2 2>/dev/null || git checkout -b "$DEFAULT_BRANCH" "origin/$DEFAULT_BRANCH" >&2

if ! git merge --ff-only "upstream/$DEFAULT_BRANCH" >&2; then
  file_failure_issue "fork-sync" "Fork $FORK's '$DEFAULT_BRANCH' has diverged from upstream $UPSTREAM's '$DEFAULT_BRANCH' (a --ff-only merge failed). Not forcing a merge/rebase - a human must resolve this."
  exit 2
fi

git push origin "$DEFAULT_BRANCH" >&2
echo "$DEFAULT_BRANCH"
