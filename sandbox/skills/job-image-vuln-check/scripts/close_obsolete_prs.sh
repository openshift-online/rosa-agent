#!/usr/bin/env bash
# close_obsolete_prs.sh - close any OPEN PR against UPSTREAM, opened from
# FORK_OWNER's fork, whose job-image-vuln-check marker (see render_pr_marker
# in common.sh) names this exact IMAGE but NONE of whose marker CVEs appear
# in CURRENT_CVES - the full, current set of CVE IDs this run's
# quay-vuln-report retrieval (SKILL.md step 2) found for this image, across
# every package.
#
# Such a PR is obsolete: whatever already fixed every one of its CVEs (e.g.
# an unrelated base-image bump that happened to carry that package's fix
# along with it, independent of this job) beat this job to it, so there is
# nothing left for the PR to do.
#
# A PR whose marker CVEs partially overlap CURRENT_CVES (some still present,
# some gone) is deliberately left alone - that bucket isn't obsolete, it's
# still a live fix, and will go through the existing step-0 dedup/supersede
# logic in find_existing_pr.sh when that bucket is reprocessed below. This
# script never does a partial close on a guess.
#
# Usage:
#   UPSTREAM=<owner>/<repo> FORK_OWNER=<account> IMAGE=<image-ref> \
#     CURRENT_CVES="<comma-separated CVE IDs, may be empty>" \
#     ./close_obsolete_prs.sh
#
# CURRENT_CVES may legitimately be empty ("") - that means the latest scan
# found zero fixable CVEs on this image at all, so every open marked PR for
# it is obsolete.
#
# Prints one TSV line per PR closed: <pr-number><TAB><its-now-obsolete-cves>
# Prints nothing if none were obsolete (including the common case of no open
# marked PRs for this image at all) - that is success, not a failure.
#
# Exit codes:
#   0  ran to completion (zero or more PRs closed, each printed above)
#   1  usage error (a required env var is unset - note CURRENT_CVES is
#      allowed to be set-but-empty; only UPSTREAM/FORK_OWNER/IMAGE are
#      strictly required)
#   2  the PR-list call, a close-comment, or a close call failed; a failure
#      Issue was filed against openshift-online/rosa-agent for the first
#      such failure, then the script stops without touching remaining
#      candidates

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

: "${UPSTREAM:?set UPSTREAM (org/repo to search open PRs on)}"
: "${FORK_OWNER:?set FORK_OWNER (the account whose fork PRs come from)}"
: "${IMAGE:?set IMAGE (image ref the current retrieval was run against)}"
: "${CURRENT_CVES=}"  # may be unset or empty; see usage note above

PRS_OUT="$(gh api "repos/$UPSTREAM/pulls?state=open&per_page=100" --paginate 2>&1)" || {
  file_failure_issue "close-obsolete-prs-list" "$PRS_OUT"
  exit 2
}

# Every open, fork-owned, marked PR targeting this exact IMAGE - no PACKAGE
# filter here (unlike find_existing_pr.sh): we need every bucket open
# against this image, not one specific package's bucket.
CANDIDATES="$(echo "$PRS_OUT" | jq -r \
  --arg owner "$FORK_OWNER" --arg image "$IMAGE" '
  .[]
  | select(.head.repo != null
      and (.head.repo.owner.login | ascii_downcase) == ($owner | ascii_downcase))
  | select(.body != null and (.body | test("<!-- job-image-vuln-check"; "s")))
  | . as $pr
  | ($pr.body | capture(
      "<!-- job-image-vuln-check\\s*\\nimage: (?<image>[^\\n]*)\\npackage: (?<package>[^\\n]*)\\ncves: (?<cves>[^\\n]*)\\n-->";
      "s"
    )) as $m
  | select($m.image == $image)
  | [$pr.number, $m.cves] | @tsv
')"

if [ -z "$CANDIDATES" ]; then
  exit 0
fi

# Current-CVE lookup set, one per line, trimmed, blank-safe. Deliberately
# built even when CURRENT_CVES is empty - an empty set makes every candidate
# below correctly fall through as obsolete.
CURRENT_SET="$(printf '%s' "$CURRENT_CVES" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' || true)"

while IFS=$'\t' read -r pr_number pr_cves; do
  [ -z "$pr_number" ] && continue

  still_present="false"
  while IFS= read -r cve; do
    [ -z "$cve" ] && continue
    if printf '%s\n' "$CURRENT_SET" | grep -qx "$cve"; then
      still_present="true"
      break
    fi
  done <<< "$(printf '%s' "$pr_cves" | tr ',' '\n' | sed 's/^ *//; s/ *$//')"

  if [ "$still_present" = "true" ]; then
    continue
  fi

  COMMENT_OUT="$(gh api -X POST "repos/$UPSTREAM/issues/$pr_number/comments" \
    -f body="Closing as obsolete: the latest job-image-vuln-check scan of \`$IMAGE\` no longer lists any of this PR's CVEs ($pr_cves) as fixable - something else (e.g. an unrelated base-image update) already resolved them. If that's wrong, reopen and say why." \
    2>&1)" || {
    file_failure_issue "close-obsolete-prs-comment" "$COMMENT_OUT"
    exit 2
  }
  CLOSE_OUT="$(gh api -X PATCH "repos/$UPSTREAM/pulls/$pr_number" -f state=closed 2>&1)" || {
    file_failure_issue "close-obsolete-prs-close" "$CLOSE_OUT"
    exit 2
  }
  printf '%s\t%s\n' "$pr_number" "$pr_cves"
done <<< "$CANDIDATES"
