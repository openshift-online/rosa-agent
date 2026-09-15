#!/usr/bin/env bash
# open-labeled-pr.sh — deterministic fork -> PR -> create-label -> attach-label
# workflow. Every step below is a fixed gh api (REST-only) call with explicit
# success/failure handling — there is no judgment call for the caller to make
# once the required inputs are set.
#
# This generalizes the ad hoc "if --label fails because the label doesn't
# exist yet, create it" fallback used by job-sop-improve into a reusable
# script, and codifies the standard fork -> PR -> label -> failure-issue
# pattern used across ROSA-Agent jobs.
#
# Requires: the feature branch already committed and pushed to your fork
# (FORK_OWNER/UPSTREAM's repo name) before calling this script. This script
# does not create branches, commit, or push — only forks, opens the PR,
# ensures the label exists, and attaches it.
#
# Usage:
#   FORK_OWNER=<your-account> \
#   UPSTREAM=<org>/<repo> \
#   BRANCH=<branch-already-pushed-to-your-fork> \
#   TITLE="<pr title>" \
#   BODY="<pr body>" \
#   [LABEL=ROSA-Agent] \
#   [LABEL_COLOR=793B90] \
#   [LABEL_DESC="ROSA-Agent-involvement label"] \
#   ./open-labeled-pr.sh
#
# Prints the new PR number on stdout on success.
#
# Exit codes:
#   0  PR opened and labeled
#   1  usage error (a required env var is unset)
#   2  a required gh api call failed; a failure Issue was filed against
#      openshift-online/rosa-agent (the exception: if filing that Issue
#      itself also fails, both failures are reported on stderr)

set -euo pipefail

: "${FORK_OWNER:?set FORK_OWNER (the account whose fork to use)}"
: "${UPSTREAM:?set UPSTREAM (org/repo to open the PR against)}"
: "${BRANCH:?set BRANCH (branch already pushed to your fork)}"
: "${TITLE:?set TITLE (PR title)}"
: "${BODY:?set BODY (PR body)}"
: "${LABEL:=ROSA-Agent}"
: "${LABEL_COLOR:=793B90}"
: "${LABEL_DESC:=ROSA-Agent-involvement label}"

fail() {
  local stage="$1" detail="$2"
  gh api -X POST "repos/openshift-online/rosa-agent/issues" \
    -f title="open-labeled-pr failure: $stage" \
    -f body="Automated PR-with-label workflow failed.

- Stage: $stage
- Upstream: $UPSTREAM
- Branch: $FORK_OWNER:$BRANCH
- Detail: $detail

🤖 Filed automatically by open-labeled-pr.sh." \
    >/dev/null \
    || echo "ALSO FAILED to file the failure Issue against openshift-online/rosa-agent" >&2
  echo "FAILED at stage '$stage': $detail" >&2
  exit 2
}

# 1. Fork. Forking an already-forked repo is a no-op in the GitHub API, so
#    this is always safe to call whether or not FORK_OWNER already has one.
gh api -X POST "repos/$UPSTREAM/forks" >/dev/null \
  || fail "fork" "POST repos/$UPSTREAM/forks failed"

# 2. Discover upstream's actual default branch — never hard-code main vs
#    master — then open the PR from the fork's branch against it.
DEFAULT_BRANCH="$(gh api "repos/$UPSTREAM" --jq '.default_branch')" \
  || fail "default-branch" "GET repos/$UPSTREAM failed"

PR_NUMBER="$(gh api -X POST "repos/$UPSTREAM/pulls" \
  -f title="$TITLE" -f body="$BODY" \
  -f head="$FORK_OWNER:$BRANCH" -f base="$DEFAULT_BRANCH" \
  --jq '.number')" \
  || fail "pr-create" "POST repos/$UPSTREAM/pulls failed"

# 3. Create the label on upstream if it doesn't exist yet. A 422 here means
#    it already exists — that is success, not failure, and an existing
#    label's color/description is left untouched (never overwritten).
gh api -X POST "repos/$UPSTREAM/labels" \
  -f name="$LABEL" -f color="$LABEL_COLOR" -f description="$LABEL_DESC" \
  >/dev/null 2>&1 || true

# 4. Attach the label to the PR (the issues endpoint works for PRs too).
gh api -X POST "repos/$UPSTREAM/issues/$PR_NUMBER/labels" \
  -f "labels[]=$LABEL" >/dev/null \
  || fail "label-attach" "POST repos/$UPSTREAM/issues/$PR_NUMBER/labels failed for PR #$PR_NUMBER"

echo "$PR_NUMBER"
