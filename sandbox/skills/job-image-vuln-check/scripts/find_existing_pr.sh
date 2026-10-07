#!/usr/bin/env bash
# find_existing_pr.sh - look for an OPEN PR against UPSTREAM, opened from
# FORK_OWNER's fork, that already targets this exact IMAGE + PACKAGE bucket
# (SKILL.md step 5: one fix per PR, keyed by package+fixed-version+layer,
# never batched across a whole image). Depends on open_pr.sh having embedded
# a job-image-vuln-check marker (image/package/cves) in the PR body - see
# render_pr_marker in common.sh. Never modifies anything - read-only lookup.
#
# Usage:
#   UPSTREAM=<owner>/<repo> FORK_OWNER=<account> IMAGE=<image-ref> \
#     PACKAGE=<pkg> ./find_existing_pr.sh
#
# On a match, prints one TSV line to stdout:
#   <pr-number><TAB><branch><TAB><comma-separated CVEs from that PR's marker>
# and exits 0. Prints nothing on no-match or error.
#
# Exit codes:
#   0  match found, printed on stdout
#   1  usage error (a required env var is unset)
#   2  the PR-list API call failed; a failure Issue was filed against
#      openshift-online/rosa-agent
#   3  no matching open PR found - the normal "nothing to dedup against yet"
#      case, not a failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

: "${UPSTREAM:?set UPSTREAM (org/repo to search open PRs on)}"
: "${FORK_OWNER:?set FORK_OWNER (the account whose fork PRs come from)}"
: "${IMAGE:?set IMAGE (image ref this bucket was found on)}"
: "${PACKAGE:?set PACKAGE (the package/dependency this bucket bumps)}"

PRS_OUT="$(gh api "repos/$UPSTREAM/pulls?state=open&per_page=100" --paginate 2>&1)" || {
  file_failure_issue "find-existing-pr" "$PRS_OUT"
  exit 2
}

# The whole match - filtering by fork owner, presence of a marker, and an
# exact image+package match against that marker - is done inside jq so we
# never have to smuggle a multi-line PR body through a bash variable/loop.
# "s" (single-line/DOTALL) lets ".\n" in the regex span the marker's
# newlines; the captured "cves" group itself is single-line so @tsv is safe.
MATCH="$(echo "$PRS_OUT" | jq -r \
  --arg owner "$FORK_OWNER" --arg image "$IMAGE" --arg package "$PACKAGE" '
  [ .[]
    | select(.head.repo != null
        and (.head.repo.owner.login | ascii_downcase) == ($owner | ascii_downcase))
    | select(.body != null and (.body | test("<!-- job-image-vuln-check"; "s")))
    | . as $pr
    | ($pr.body | capture(
        "<!-- job-image-vuln-check\\s*\\nimage: (?<image>[^\\n]*)\\npackage: (?<package>[^\\n]*)\\ncves: (?<cves>[^\\n]*)\\n-->";
        "s"
      )) as $m
    | select($m.image == $image and $m.package == $package)
    | [$pr.number, $pr.head.ref, $m.cves] | @tsv
  ] | first // empty
')"

if [ -z "$MATCH" ]; then
  exit 3
fi
echo "$MATCH"
