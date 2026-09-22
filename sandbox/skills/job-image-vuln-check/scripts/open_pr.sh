#!/usr/bin/env bash
# open_pr.sh - open a plain PR (no label) from an already-pushed branch on
# your fork, against upstream's actual default branch. Files a failure
# Issue against openshift-online/rosa-agent on error. This is the smaller,
# no-labeling counterpart to github-labels/open-labeled-pr.sh - use that one
# instead if the PR needs a label.
#
# Precondition: your fork exists and BRANCH is already pushed to it, before
# calling this script - it does not fork, branch, commit, or push.
#
# Usage:
#   FORK_OWNER=<account> UPSTREAM=<owner>/<repo> BRANCH=<pushed-branch> \
#   TITLE="<pr title>" BODY="<pr body>" ./open_pr.sh
#
# Prints the new PR number on stdout on success.
#
# Exit codes:
#   0  PR opened
#   1  usage error (a required env var is unset)
#   2  the PR-create call failed; a failure Issue was filed against
#      openshift-online/rosa-agent with the full captured error

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

: "${FORK_OWNER:?set FORK_OWNER (the account whose fork to open the PR from)}"
: "${UPSTREAM:?set UPSTREAM (org/repo to open the PR against)}"
: "${BRANCH:?set BRANCH (branch already pushed to your fork)}"
: "${TITLE:?set TITLE (PR title)}"
: "${BODY:?set BODY (PR body)}"

DEFAULT_BRANCH_OUT="$(gh api "repos/$UPSTREAM" --jq '.default_branch' 2>&1)" || {
  file_failure_issue "default-branch" "$DEFAULT_BRANCH_OUT"
  exit 2
}

PR_OUT="$(gh api -X POST "repos/$UPSTREAM/pulls" \
  -f title="$TITLE" -f body="$BODY" \
  -f head="$FORK_OWNER:$BRANCH" -f base="$DEFAULT_BRANCH_OUT" \
  --jq '.number' 2>&1)" || {
  file_failure_issue "pr-create" "$PR_OUT"
  exit 2
}

echo "$PR_OUT"
