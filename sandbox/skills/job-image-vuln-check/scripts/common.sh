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
