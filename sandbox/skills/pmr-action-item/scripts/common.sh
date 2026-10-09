#!/usr/bin/env bash
# common.sh - shared helper sourced by this skill's other scripts. Not meant
# to be executed directly.

# file_failure_issue STAGE DETAIL
#
# Files a GitHub Issue against openshift-online/rosa-agent (this job's own
# repo, never the repo the ticket's work happens in) describing a genuine
# technical failure in the job itself - e.g. the fork-sync script erroring.
#
# This is NOT for ticket-specific blockers (ambiguous requirements, a
# security finding you can't safely resolve, a 403 from the target repo's
# own needs, failing tests you can't fix). Those get a comment on the Jira
# ticket instead (see SKILL.md step 7) - a human watching the ticket, not an
# Issue against this repo's own tracker, is the right audience for them.
#
# Mirrors job-image-vuln-check/scripts/common.sh's file_failure_issue(). Does
# not exit - callers decide their own exit code after calling this.
file_failure_issue() {
  local stage="$1" detail="$2"
  gh api -X POST "repos/openshift-online/rosa-agent/issues" \
    -f title="pmr-action-item failure: $stage" \
    -f body="Automated PMR action-item job failed.

- Stage: $stage

Captured error output:

\`\`\`
$detail
\`\`\`

🤖 Filed automatically by pmr-action-item." \
    >/dev/null 2>&1 \
    || echo "ALSO FAILED to file the failure Issue against openshift-online/rosa-agent" >&2
  echo "FAILED at stage '$stage':" >&2
  echo "$detail" >&2
}
