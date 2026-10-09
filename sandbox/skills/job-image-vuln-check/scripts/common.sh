#!/usr/bin/env bash
# common.sh - shared helper sourced by this skill's other scripts. Not meant
# to be executed directly.

# file_failure_issue STAGE DETAIL
#
# Files a GitHub Issue against openshift-online/rosa-agent (this job's own
# repo, never the repo being contributed to) describing a failed stage.
# Mirrors github-labels/open-labeled-pr.sh's fail() pattern. Does not exit -
# callers decide their own exit code after calling this.
file_failure_issue() {
  local stage="$1" detail="$2"
  gh api -X POST "repos/openshift-online/rosa-agent/issues" \
    -f title="job-image-vuln-check failure: $stage" \
    -f body="Automated image-vulnerability-check job failed.

- Stage: $stage

Captured error output:

\`\`\`
$detail
\`\`\`

🤖 Filed automatically by job-image-vuln-check." \
    >/dev/null 2>&1 \
    || echo "ALSO FAILED to file the failure Issue against openshift-online/rosa-agent" >&2
  echo "FAILED at stage '$stage':" >&2
  echo "$detail" >&2
}

# render_pr_marker IMAGE PACKAGE CVES
#
# Emits an HTML-comment block (invisible in the rendered PR, greppable in
# the raw body) embedding this PR's image ref, the package/dependency it
# bumps, and the CVE IDs it fixes. open_pr.sh appends this when IMAGE,
# PACKAGE and CVES are all given; find_existing_pr.sh looks for it on a
# later run to answer "is there already an open PR for this exact
# image+package bucket, and if so which CVEs does it cover" (see SKILL.md
# step 6, "one fix per PR, keyed by package+fixed-version+layer"). CVES is
# a single comma-separated string, e.g. "CVE-2024-1,CVE-2024-2".
render_pr_marker() {
  local image="$1" package="$2" cves="$3"
  printf '\n\n<!-- job-image-vuln-check\nimage: %s\npackage: %s\ncves: %s\n-->\n' \
    "$image" "$package" "$cves"
}
