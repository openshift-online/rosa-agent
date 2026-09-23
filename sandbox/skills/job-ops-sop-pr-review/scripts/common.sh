#!/usr/bin/env bash
# common.sh - shared helper sourced by this skill's own workflow. Not meant
# to be executed directly.

# file_failure_issue STAGE DETAIL
#
# Files a GitHub Issue against openshift-online/rosa-agent (this job's own
# repo, never openshift/ops-sop) describing a genuine technical failure.
# Mirrors job-image-vuln-check/scripts/common.sh's file_failure_issue().
# Does not exit - callers decide their own exit code after calling this.
#
# Only call this for a real technical problem (an API error, a missing
# skill file, a policy-denied response, etc). A run that simply finds no
# eligible PRs, or finds PRs needing no pings, is NOT a failure - do not
# call this for that.
file_failure_issue() {
  local stage="$1" detail="$2"
  gh api -X POST "repos/openshift-online/rosa-agent/issues" \
    -f title="job-ops-sop-pr-review failure: $stage" \
    -f body="The automated ops-sop PR review job hit a technical problem.

- Stage: $stage

Captured error output:

\`\`\`
$detail
\`\`\`

Suggested fix: investigate the stage and error above and adjust the
job-ops-sop-pr-review skill or its ops_sop_pr_review package accordingly.

🤖 Filed automatically by job-ops-sop-pr-review." \
    >/dev/null 2>&1 \
    || echo "ALSO FAILED to file the failure Issue against openshift-online/rosa-agent" >&2
  echo "FAILED at stage '$stage':" >&2
  echo "$detail" >&2
}
