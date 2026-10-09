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
#   TITLE="<pr title>" BODY="<pr body>" \
#   [IMAGE=<image-ref> PACKAGE=<pkg> CVES="<CVE-1,CVE-2>"] \
#   [SUPERSEDES=<old-pr-number>] \
#   ./open_pr.sh
#
# IMAGE, PACKAGE and CVES, when all three are given, get a
# job-image-vuln-check marker appended to BODY (see render_pr_marker in
# common.sh) so a later run of that job can find this PR again and compare
# CVE sets - see find_existing_pr.sh. Omit all three for callers outside
# job-image-vuln-check; partial sets are ignored (no marker is added unless
# all three are present).
#
# SUPERSEDES, when given, is an older open PR number that find_existing_pr.sh
# has already established covers a strict subset of this PR's CVEs for the
# same image+package bucket (SKILL.md step 6, step 0's "supersede" case).
# Once the new PR opens successfully, this script comments on SUPERSEDES
# referencing the new PR and closes it. This script trusts the caller's
# judgement about the subset relationship - it does not re-verify it.
#
# Prints the new PR number on stdout on success.
#
# Exit codes:
#   0  PR opened (and, if SUPERSEDES was given, the old PR closed)
#   1  usage error (a required env var is unset)
#   2  the PR-create call failed; a failure Issue was filed against
#      openshift-online/rosa-agent with the full captured error. Also used
#      if PR-create succeeded but the SUPERSEDES comment/close step failed -
#      the new PR number is still printed on stdout in that case.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

: "${FORK_OWNER:?set FORK_OWNER (the account whose fork to open the PR from)}"
: "${UPSTREAM:?set UPSTREAM (org/repo to open the PR against)}"
: "${BRANCH:?set BRANCH (branch already pushed to your fork)}"
: "${TITLE:?set TITLE (PR title)}"
: "${BODY:?set BODY (PR body)}"

FULL_BODY="$BODY"
if [ -n "${IMAGE:-}" ] && [ -n "${PACKAGE:-}" ] && [ -n "${CVES:-}" ]; then
  FULL_BODY="$BODY$(render_pr_marker "$IMAGE" "$PACKAGE" "$CVES")"
fi

DEFAULT_BRANCH_OUT="$(gh api "repos/$UPSTREAM" --jq '.default_branch' 2>&1)" || {
  file_failure_issue "default-branch" "$DEFAULT_BRANCH_OUT"
  exit 2
}

PR_OUT="$(gh api -X POST "repos/$UPSTREAM/pulls" \
  -f title="$TITLE" -f body="$FULL_BODY" \
  -f head="$FORK_OWNER:$BRANCH" -f base="$DEFAULT_BRANCH_OUT" \
  --jq '.number' 2>&1)" || {
  file_failure_issue "pr-create" "$PR_OUT"
  exit 2
}

echo "$PR_OUT"

if [ -n "${SUPERSEDES:-}" ]; then
  COMMENT_OUT="$(gh api -X POST "repos/$UPSTREAM/issues/$SUPERSEDES/comments" \
    -f body="Superseded by #$PR_OUT - the CVE set for this package has grown since this PR was opened; closing in favor of the new PR, which covers the full current set." \
    2>&1)" || {
    file_failure_issue "supersede-comment" "$COMMENT_OUT"
    exit 2
  }
  CLOSE_OUT="$(gh api -X PATCH "repos/$UPSTREAM/pulls/$SUPERSEDES" -f state=closed 2>&1)" || {
    file_failure_issue "supersede-close" "$CLOSE_OUT"
    exit 2
  }
fi
