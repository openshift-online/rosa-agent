---
name: job-pmr-pr-review
description: Scheduled job that scans Jira for PMR action-item tickets labeled pmr-pr-pending (set by pmr-action-item after opening a PR), checks each linked GitHub PR's review status, and takes tiered action to prevent stalled PRs: no action under 48h; PR comment + Jira status comment at 48h–7 days; escalate to CODEOWNERS review request + urgent PR comment at 7–14 days; file a failure issue against openshift-online/rosa-agent at 14+ days. Closes out tickets (removes pmr-pr-pending, adds pmr-pr-merged, transitions to Done) when PRs merge. Deduplicates nudges to avoid spamming reviewers. Use when asked to run the PMR PR review job, check for stalled PMR PRs, or when triggered by a cron/scheduled invocation referencing job-pmr-pr-review. Trigger keywords - pmr-pr-pending, stalled PMR PR, PR review nudge, job-pmr-pr-review.
---

# Job: nudge reviewers on stalled PMR action-item PRs

This job is **Stage 7 automation** of the PMR action pipeline (MCS-46). It
closes the gap between a PR being opened by `pmr-action-item` and the PR
actually being reviewed and merged. Without it, PRs sit indefinitely because
no human is specifically watching for them.

This job makes no code changes and opens no PRs. Its outputs are:
- GitHub comments on stalled PRs
- Jira status comments on pending tickets
- Jira label + transition updates when a PR merges
- A GitHub Issue against `openshift-online/rosa-agent` for escalations or
  failures

Credentials are pre-injected. Do NOT run `gh auth login`. Per `AGENTS.md`,
all Jira calls go through the `jira` skill and all GitHub calls use
REST-only `gh api` (GraphQL is blocked).

## Label protocol

| Label | Meaning | Set by |
|---|---|---|
| `pmr-ai` | Is a PMR action item | Humans / intake |
| `pmr-ai-ready` | Proposal approved, ready to implement | `pmr-remediation-analysis` |
| `pmr-pr-pending` | PR is open, awaiting review | `pmr-action-item` |
| `pmr-pr-changes-requested` | Reviewer left feedback; agent must revise | This job |
| `pmr-pr-merged` | PR merged, work complete | This job |

## 1. Auth setup

Load the `jira` skill and run its auth-detection block once. Per `AGENTS.md`,
every Jira call in this skill MUST go through the resulting `$JB` base URL
and `jira` helper function.

For GitHub, all calls use `gh api` with REST endpoints only. No `gh pr view`,
`gh issue list`, or any other subcommand that uses GraphQL.

## 2. Scan Jira for pending tickets

Query for all tickets that have a PR open and awaiting review:

```shell
jira -H "Content-Type: application/json" \
  -X POST "$JB/rest/api/3/search/jql" \
  -d '{
    "jql": "labels = \"pmr-pr-pending\" AND statusCategory != Done ORDER BY updated ASC",
    "fields": ["summary", "labels", "status", "comment", "remoteLinks"],
    "maxResults": 50
  }'
```

If no tickets match, log "0 tickets with pmr-pr-pending found" and stop. This
is a normal, successful outcome.

## 3. For each ticket, resolve the PR URL

Fetch remote links on the ticket:

```shell
jira -H "Accept: application/json" \
  "$JB/rest/api/3/issue/<KEY>/remotelink"
```

Scan the returned links for a GitHub PR URL matching the pattern
`https://github.com/<owner>/<repo>/pull/<number>`. Use the first match.

If no PR URL is found:
- Post a Jira comment: "job-pmr-pr-review could not find a linked GitHub PR
  on this ticket. Manual triage required."
- Skip to the next ticket. Do not remove `pmr-pr-pending` — a human needs
  to investigate.

## 4. Check PR state via GitHub REST

Parse `<owner>`, `<repo>`, and `<pr-number>` from the PR URL, then:

```shell
gh api repos/<owner>/<repo>/pulls/<pr-number> \
  --jq '{state, merged, merged_at, created_at, updated_at,
         title, html_url, draft,
         requested_reviewers: [.requested_reviewers[].login],
         requested_teams: [.requested_teams[].slug]}'
```

Also fetch the review list to find the latest review activity and whether any
reviewer has requested changes:

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/reviews" \
  --jq '[.[] | {user: .user.login, state, submitted_at, body}]'
```

For each reviewer, only their **most recent** review counts — earlier reviews
by the same person are superseded. A PR has `CHANGES_REQUESTED` state if any
reviewer's most-recent review state is `CHANGES_REQUESTED` (and they have not
subsequently approved).

Also fetch inline review comments (line-level feedback, distinct from top-level
review bodies):

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/comments" \
  --jq '[.[] | {user: .user.login, path, line, body, created_at}]'
```

## 5. Branch on PR state

### 5a. Merged

When `merged == true`:

1. Remove `pmr-pr-pending` and add `pmr-pr-merged` to the Jira ticket:

   ```shell
   jira -H "Content-Type: application/json" \
     -X PUT "$JB/rest/api/3/issue/<KEY>" \
     -d '{"update":{"labels":[{"remove":"pmr-pr-pending"},{"add":"pmr-pr-merged"}]}}'
   ```

2. Fetch available transitions and transition the ticket to Done:

   ```shell
   jira -H "Accept: application/json" "$JB/rest/api/3/issue/<KEY>/transitions"
   # Pick the transition whose name matches "Done" or "Closed" (case-insensitive)
   jira -H "Content-Type: application/json" \
     -X POST "$JB/rest/api/3/issue/<KEY>/transitions" \
     -d '{"transition":{"id":"<done-transition-id>"}}'
   ```

3. Post an ADF Jira comment:

   ```text
   PR merged: <PR title> (<PR URL>). Ticket is now complete.
   🤖 job-pmr-pr-review
   ```

4. Skip nudge logic — this ticket is done.

### 5b. Closed without merge

When `state == "closed"` and `merged == false`:

1. Remove `pmr-pr-pending` from the Jira ticket (do NOT add `pmr-pr-merged`):

   ```shell
   jira -H "Content-Type: application/json" \
     -X PUT "$JB/rest/api/3/issue/<KEY>" \
     -d '{"update":{"labels":[{"remove":"pmr-pr-pending"}]}}'
   ```

2. Post an ADF Jira comment explaining the PR was closed unmerged and that
   manual triage is needed to decide whether to re-implement or close the
   ticket.

3. Skip nudge logic — do not reopen the PR or re-implement.

### 5c. Open with CHANGES_REQUESTED

When the PR is open and any reviewer's most-recent review state is
`CHANGES_REQUESTED`:

**Before acting, check for a recent bot comment** (same 24h dedup as step 6)
with sentinel `🤖 PMR PR changes-requested`. Skip all actions if already
flagged within the last 24 hours.

1. **Fetch the full review feedback** to pass into Jira context. Build a
   combined summary of all CHANGES_REQUESTED reviews (top-level body) and
   all inline comments (file, line, body), grouped by reviewer:

   ```shell
   # Already fetched in step 4 — reviews list and comments list
   ```

2. **Post a structured Jira comment** that captures the reviewer feedback
   verbatim. This is the context-preservation step: `pmr-action-item`'s
   Step 2 reads all Jira comments, so writing the feedback here means the
   agent has everything it needs in the ticket without needing to re-query
   GitHub on its next run.

   The comment should include:
   - PR URL and title
   - For each reviewer who left CHANGES_REQUESTED: their login, review body,
     and any inline comments (file:line → comment body)
   - A closing line: "Review feedback captured for revision run."

   Use ADF format via the `jira` skill. Plain text is fine for the body
   content — wrap each reviewer block in a paragraph node.

3. **Swap the Jira label** — replace `pmr-pr-pending` with
   `pmr-pr-changes-requested`:

   ```shell
   jira -H "Content-Type: application/json" \
     -X PUT "$JB/rest/api/3/issue/<KEY>" \
     -d '{"update":{"labels":[{"remove":"pmr-pr-pending"},{"add":"pmr-pr-changes-requested"}]}}'
   ```

4. **Post a PR comment** acknowledging that the feedback has been received
   and a revision run will address it:

   ```shell
   gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
     -f body="$(cat <<'EOF'
   Thanks for the review. This PR was opened by the rosa-agent PMR automation.
   The requested changes have been logged on the Jira ticket and a revision
   run will be triggered to address them.

   🤖 PMR PR changes-requested — job-pmr-pr-review
   EOF
   )"
   ```

5. **Do not apply nudge tiers** for this ticket on this run — it is no
   longer in a "waiting for review" state. Skip to the next ticket.

### 5d. Open (awaiting review or approved, no changes requested)

Calculate staleness:

```shell
# hours_open = now - pr.created_at
# latest_activity = max(pr.updated_at, most-recent review's submitted_at if any)
# hours_stale = now - latest_activity
```

Apply nudge tiers below.

## 6. Nudge tiers (open PRs awaiting review — step 5d only)

Before posting any PR or Jira comment, check for a recent bot comment to
avoid spamming. Fetch existing PR comments:

```shell
gh api "repos/<owner>/<repo>/issues/<pr-number>/comments" \
  --jq '[.[] | select(.user.login == "rosa-agent") | {created_at, body}]'
```

If a comment whose body contains the sentinel `🤖 PMR PR review nudge` was
posted within the last 24 hours, skip all nudge actions for this ticket this
run.

### Tier 0 — Under 48 hours

No action. Too early to nudge. Log "skipped: PR too new".

### Tier 1 — 48 hours to 7 days

**Post a PR comment** (via the issues comments endpoint):

```shell
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="$(cat <<'EOF'
This PR was opened by the rosa-agent PMR automation and is awaiting review.

**Ticket:** <JIRA-KEY> — <ticket summary>
**PR age:** <N> days

If you have capacity, a review here would prevent this PMR action item from
stalling. If this needs to be routed to a specific reviewer, please add them.

🤖 PMR PR review nudge — job-pmr-pr-review
EOF
)"
```

**Post a Jira status comment:**

```shell
jira -H "Content-Type: application/json" \
  -X POST "$JB/rest/api/3/issue/<KEY>/comment" \
  -d '{
    "body": {
      "type": "doc", "version": 1,
      "content": [{
        "type": "paragraph",
        "content": [{"type": "text", "text": "PR review nudge sent (Tier 1). PR has been open N days with no review. PR: <URL>\n🤖 job-pmr-pr-review"}]
      }]
    }
  }'
```

### Tier 2 — 7 to 14 days

**Request review from CODEOWNERS** (if present in the repo) or the repo's
default team. Fetch CODEOWNERS:

```shell
gh api repos/<owner>/<repo>/contents/CODEOWNERS \
  | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['content']).decode())"
```

Parse the `*` catch-all line to find owner logins/teams. Then request
review (users only — teams require org admin and may be blocked):

```shell
gh api repos/<owner>/<repo>/pulls/<pr-number>/requested_reviewers \
  -f "reviewers[]=<login>"
```

If CODEOWNERS is absent or the request returns a non-200, skip the reviewer
request (log it) and continue with the comment.

**Post an escalated PR comment** making clear the urgency:

```shell
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="$(cat <<'EOF'
**[Escalation — 7+ days without review]**

This PMR action-item PR has been open for N days with no review activity.
It was opened automatically by the rosa-agent PMR pipeline.

**Ticket:** <JIRA-KEY> — <ticket summary>

A review is needed to close this post-mortem action item. If you are not
the right reviewer, please route to the appropriate team.

🤖 PMR PR review nudge — job-pmr-pr-review
EOF
)"
```

**Post an escalated Jira comment** (same ADF structure as Tier 1, with
Tier 2 noted).

### Tier 3 — Over 14 days

All Tier 2 actions, **plus** file a GitHub Issue against
`openshift-online/rosa-agent` to surface the stall for human escalation:

```shell
gh api repos/openshift-online/rosa-agent/issues \
  -f title="PMR PR stalled >14 days: <JIRA-KEY>" \
  -f body="$(cat <<'EOF'
A PMR action-item PR has been open for over 14 days with no review.
Automated nudges have been sent but the PR remains unreviewed.

- **Jira ticket:** <JIRA-KEY> — <ticket summary>
- **PR:** <PR URL>
- **PR age:** N days
- **Nudges sent:** Tier 1 (48h), Tier 2 (7d), Tier 3 (14d)

Human escalation is needed to either review, re-route, or close this PR.

🤖 Filed by job-pmr-pr-review
EOF
)"
```

## 7. Output summary

After processing all tickets, log:

```
job-pmr-pr-review summary:
  Tickets scanned:        N
  Merged (closed):        K
  Closed no-merge:        J
  Changes requested:      X
  Tier 0 (too new):       P
  Tier 1 nudged:          Q
  Tier 2 escalated:       R
  Tier 3 escalated:       S
  Deduped (skipped):      D
  Errors:                 E
```

## On any failure

If any step fails in a way that stops the job (Jira auth failure, an
unexpected HTTP error, `gh api` returning non-200 on a required call):

```shell
gh api repos/openshift-online/rosa-agent/issues \
  -f title="job-pmr-pr-review failure: <one-line summary>" \
  -f body="The PMR PR review job failed.

- Stage: <auth | scan | resolve-pr | check-state | nudge | ...>
- Ticket (if any): <JIRA-KEY>
- PR (if any): <URL>
- Command that failed: <command>
- Error output: <verbatim error, including HTTP status>

🤖 Filed automatically by a failed job-pmr-pr-review run."
```

Then stop. Never retry around a failure.

A run that finds **no tickets** with `pmr-pr-pending` is NOT a failure.

## Guardrails

- Never edit, merge, close, or reopen a PR. This job only comments and
  requests reviewers.
- Never remove `pmr-pr-pending` unless the PR is merged or closed. If the
  PR URL is missing, leave the label and comment for human triage.
- Never nudge a PR more than once per 24 hours (the dedup check in step 6).
- Never retry past a `403` or `policy_denied` — report and stop.
- One summary issue per run at most; do not file per-ticket failure issues
  (those go as Jira comments per step 3/5b).

## Related

- **`pmr-action-item`**: Opens the PR (sets `pmr-pr-pending`) and handles
  revision runs when this job sets `pmr-pr-changes-requested` (Stage 6)
- **`pmr-remediation-analysis`**: Sets `pmr-ai-ready` (Stage 2)
- **MCS-1516**: Design ticket for this job
- **MCS-46**: PMR action pipeline (parent epic)

---

🤖 *MCS-1516: PMR PR review nudge job*
