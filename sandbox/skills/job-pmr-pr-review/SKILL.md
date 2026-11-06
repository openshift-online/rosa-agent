---
name: job-pmr-pr-review
description: Scheduled job that scans Jira for PMR action-item tickets labeled pmr-pr-pending (PR open, awaiting review) and pmr-pr-changes-requested (revision run needed), checks each linked GitHub PR via REST, and takes tiered action. For pending PRs: no action under 48h; PR comment + Jira status comment at 48h-7 days; CODEOWNERS review request + urgent comment at 7-14 days; failure issue at 14+ days. For stalled revision PRs (pmr-pr-changes-requested with no new commits): same tiers. Detects CHANGES_REQUESTED reviewer state, captures feedback into Jira for context, and swaps pmr-pr-pending to pmr-pr-changes-requested. Enforces a 3-cycle revision limit and sets pmr-ai-blocked on breach. Detects failing CI check runs: retests once first (flaky test guard), then on confirmed failure fetches the log via Prow Deck or GitHub Actions API, captures it into Jira, and sets pmr-pr-ci-failing for pmr-action-item to fix. Closes out tickets when PRs merge. Use when asked to run the PMR PR review job, check for stalled PMR PRs, or when triggered by a cron/scheduled invocation. Trigger keywords - pmr-pr-pending, pmr-pr-changes-requested, pmr-pr-ci-failing, stalled PMR PR, CI failure, PR review nudge, job-pmr-pr-review.
---

# Job: nudge reviewers on stalled PMR action-item PRs

This job is **Stage 7 automation** of the PMR action pipeline (MCS-46). It
closes the gap between a PR being opened by `pmr-action-item` and the PR
actually being reviewed and merged, and surfaces stalled revision runs so
they do not go unnoticed.

This job makes no code changes and opens no PRs. Its outputs are GitHub
comments, Jira label updates, Jira status comments, and — for escalations
or failures — GitHub Issues against `openshift-online/rosa-agent`.

Credentials are pre-injected. Do NOT run `gh auth login`. Per `AGENTS.md`,
all Jira calls go through the `jira` skill and all GitHub calls use
REST-only `gh api` (GraphQL is blocked).

## Label protocol

| Label | Meaning | Set by |
|---|---|---|
| `pmr-ai` | Is a PMR action item | Humans / intake |
| `pmr-ai-ready` | Proposal approved, ready to implement | `pmr-remediation-analysis` |
| `pmr-ai-wip` | Implementation in progress | `pmr-action-item` |
| `pmr-ai-blocked` | Unresolvable blocker or revision limit hit; human must re-queue | `pmr-action-item` / this job |
| `pmr-pr-pending` | PR open, awaiting review | `pmr-action-item` |
| `pmr-pr-changes-requested` | Reviewer left feedback; agent must revise | This job |
| `pmr-pr-ci-failing` | CI confirmed failing after retest; needs code fix | This job |
| `pmr-pr-merged` | PR merged, work complete | This job |

## 1. Auth setup

Load the `jira` skill and run its auth-detection block once. Per `AGENTS.md`,
every Jira call in this skill MUST go through the resulting `$JB` base URL
and `jira` helper function.

For GitHub, all calls use `gh api` with REST endpoints only. No `gh pr view`,
`gh issue list`, or any other subcommand that uses GraphQL.

## 2. Scan Jira — two separate queries

Run both queries and process the results independently.

**Query A — pending PRs:**

```shell
jira -H "Content-Type: application/json" \
  -X POST "$JB/rest/api/3/search/jql" \
  -d '{
    "jql": "labels = \"pmr-pr-pending\" AND statusCategory != Done ORDER BY updated ASC",
    "fields": ["summary", "labels", "status", "comment"],
    "maxResults": 50
  }'
```

**Query B — changes-requested (potential stalled revision runs):**

```shell
jira -H "Content-Type: application/json" \
  -X POST "$JB/rest/api/3/search/jql" \
  -d '{
    "jql": "labels = \"pmr-pr-changes-requested\" AND labels NOT IN (\"pmr-ai-blocked\") AND statusCategory != Done ORDER BY updated ASC",
    "fields": ["summary", "labels", "status", "comment"],
    "maxResults": 50
  }'
```

If both queries return zero tickets, log totals and stop. This is a normal,
successful outcome.

## 3. For each ticket, resolve the PR URL

Fetch remote links on the ticket:

```shell
jira -H "Accept: application/json" \
  "$JB/rest/api/3/issue/<KEY>/remotelink"
```

Scan for a GitHub PR URL matching `https://github.com/<owner>/<repo>/pull/<number>`.
Use the first match.

If no PR URL is found:

- Post a Jira comment: "job-pmr-pr-review could not find a linked GitHub PR
  on this ticket. Manual triage required."
- Skip to the next ticket. Do not modify labels — a human needs to investigate.

## 4. Check PR state via GitHub REST

Parse `<owner>`, `<repo>`, and `<pr-number>` from the PR URL, then:

```shell
gh api repos/<owner>/<repo>/pulls/<pr-number> \
  --jq '{state, merged, merged_at, created_at, updated_at,
         title, html_url, draft, head_sha: .head.sha,
         requested_reviewers: [.requested_reviewers[].login],
         requested_teams: [.requested_teams[].slug]}'
```

Fetch the review list (for CHANGES_REQUESTED detection and latest activity):

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/reviews" \
  --jq '[.[] | {user: .user.login, state, submitted_at, body}]'
```

For each reviewer, only their **most-recent** review counts — earlier reviews
are superseded. A PR has active CHANGES_REQUESTED if any reviewer's
most-recent review state is `CHANGES_REQUESTED` (and they have not since
approved).

Fetch inline review comments (line-level feedback):

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/comments" \
  --jq '[.[] | {user: .user.login, path, line, body, created_at}]'
```

Also fetch the commit list to detect revision activity (used in step 5d):

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/commits" \
  --jq '[.[] | {sha: .sha[:8], date: .commit.author.date}]'
```

Also fetch check runs for the PR's head SHA — used in step 5f to detect CI
failures:

```shell
gh api "repos/<owner>/<repo>/commits/<head_sha>/check-runs?per_page=100" \
  --jq '[.check_runs[] | {id, name, status, conclusion, details_url, app_slug: .app.slug}]'
```

A check run is a **confirmed failure** if `conclusion` is `"failure"` or
`"error"` and `status` is `"completed"`. Ignore check runs that are still
`in_progress` or `queued`.

## 5. Branch on PR state

### 5a. Merged

When `merged == true`:

1. Remove `pmr-pr-pending` (or `pmr-pr-changes-requested`) and add
   `pmr-pr-merged`:

   ```shell
   jira -H "Content-Type: application/json" \
     -X PUT "$JB/rest/api/3/issue/<KEY>" \
     -d '{"update":{"labels":[{"remove":"pmr-pr-pending"},{"add":"pmr-pr-merged"}]}}'
   ```

2. Transition the ticket to Done:

   ```shell
   jira -H "Accept: application/json" "$JB/rest/api/3/issue/<KEY>/transitions"
   # Pick the transition whose name matches "Done" or "Closed" (case-insensitive)
   jira -H "Content-Type: application/json" \
     -X POST "$JB/rest/api/3/issue/<KEY>/transitions" \
     -d '{"transition":{"id":"<done-transition-id>"}}'
   ```

3. Post an ADF Jira comment: "PR merged: `<PR title>` (`<PR URL>`). Ticket
   is now complete. 🤖 job-pmr-pr-review"

4. Skip all other logic for this ticket.

### 5b. Closed without merge

When `state == "closed"` and `merged == false`:

1. Remove `pmr-pr-pending` or `pmr-pr-changes-requested` from the ticket.
2. Post an ADF Jira comment explaining the PR was closed unmerged and that
   manual triage is needed to decide whether to re-implement or close the
   ticket.
3. Skip nudge logic.

### 5c. Open with active CHANGES_REQUESTED (from pending query only)

Applies only to tickets from **Query A** (`pmr-pr-pending`) where any
reviewer's most-recent review state is `CHANGES_REQUESTED`.

**Before acting**, check for a recent bot comment with sentinel
`🤖 PMR PR changes-requested` posted within the last 24 hours — skip if
already flagged.

**Check revision cycle count:** Count this job's prior Jira comments on
this ticket that contain the sentinel `🤖 PMR PR changes-requested`. If the
count is **3 or more**, the revision limit has been reached:

```shell
# Instead of setting changes-requested again:
# 1. Post escalation comment on the PR:
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="**[Revision limit reached]**

This PR has gone through 3 automated revision cycles without satisfying
reviewer requests. Further automated revision attempts have been stopped.
Human triage is required to decide whether to continue, reassign, or close.

🤖 PMR PR changes-requested — job-pmr-pr-review"

# 2. Post Jira comment explaining the limit
# 3. Remove pmr-pr-pending, add pmr-ai-blocked (stops all cron retries)
jira -H "Content-Type: application/json" \
  -X PUT "$JB/rest/api/3/issue/<KEY>" \
  -d '{"update":{"labels":[{"remove":"pmr-pr-pending"},{"add":"pmr-ai-blocked"}]}}'
```

If under the limit, proceed with the normal CHANGES_REQUESTED flow:

1. **Capture reviewer feedback into Jira** (context preservation for the
   revision run). Build a combined summary of all CHANGES_REQUESTED review
   bodies and inline comments, grouped by reviewer. Post as an ADF Jira
   comment including: PR URL and title; for each reviewer: login, review body,
   and inline comments (file:line → body). Close with:
   "Review feedback captured for revision run. 🤖 PMR PR changes-requested
   — job-pmr-pr-review"

2. **Swap the label:**

   ```shell
   jira -H "Content-Type: application/json" \
     -X PUT "$JB/rest/api/3/issue/<KEY>" \
     -d '{"update":{"labels":[{"remove":"pmr-pr-pending"},{"add":"pmr-pr-changes-requested"}]}}'
   ```

3. **Post PR acknowledgment comment:**

   ```shell
   gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
     -f body="Thanks for the review. This PR was opened by the rosa-agent PMR automation. The requested changes have been logged on the Jira ticket and a revision run will address them.

   🤖 PMR PR changes-requested — job-pmr-pr-review"
   ```

4. Skip nudge tiers for this ticket on this run.

### 5d. Open with CHANGES_REQUESTED stalled (from changes-requested query)

Applies to tickets from **Query B** (`pmr-pr-changes-requested`) where the
PR is still open.

Detect whether a revision run has started by comparing the PR's most-recent
commit timestamp against the timestamp of the Jira comment that set the
`pmr-pr-changes-requested` label (the comment containing the sentinel
`🤖 PMR PR changes-requested`).

```text
hours_since_label_set = now - timestamp_of_last_changes_requested_comment
most_recent_commit_date = latest commit's author date from step 4
revision_started = most_recent_commit_date > timestamp_of_last_changes_requested_comment
```

If `revision_started == true`: the agent is actively working the revision.
No action needed — skip this ticket.

If `revision_started == false`: the revision run has not yet started or
stalled. Apply the same tiered escalation as step 6 (nudge tiers), using
`hours_since_label_set` as the age:

- Under 48h: no action (too early).
- 48h–7 days: Tier 1 comment on the PR + Jira status comment.
- 7–14 days: Tier 2 (CODEOWNERS review request + escalated comment + Jira).
- Over 14 days: Tier 3 (all of Tier 2 + failure issue against
  `openshift-online/rosa-agent`).

Use the sentinel `🤖 PMR PR review nudge (revision stalled)` for dedup
checks on this path.

### 5f. Open with failing CI check runs

Applies to tickets from **Query A** (`pmr-pr-pending`) that are open, have no
active CHANGES_REQUESTED (would have been caught by 5c), and have one or more
completed-failed check runs from step 4.

Skip this step if `pmr-pr-ci-failing` is already on the ticket — that means
a fix run has already been triggered and `pmr-action-item` is handling it.

**Check for a prior retest comment** by this bot on the PR — fetch PR comments
and look for the sentinel `🤖 PMR CI retest triggered` posted by `rosa-agent`:

```shell
gh api "repos/<owner>/<repo>/issues/<pr-number>/comments" \
  --jq '[.[] | select(.user.login == "rosa-agent") | {created_at, body}]'
```

**Case A — No prior retest comment (first time seeing this failure):**

Trigger a retest. Determine check runner type from the failing check run's
`details_url`:

- **Prow** (`details_url` contains `prow.ci.openshift.org`): post a `/retest`
  comment on the PR. Prow bot picks this up and re-runs all failed jobs.

  ```shell
  gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
    -f body="/retest"
  ```

- **GitHub Actions** (`details_url` is a `github.com/...` URL): use the
  re-run API. Parse the `run_id` from `details_url`, then:

  ```shell
  gh api repos/<owner>/<repo>/actions/runs/<run_id>/rerun-failed-jobs -X POST
  ```

After triggering, post a PR comment and a Jira comment so the state is
recorded:

```shell
# PR comment
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="CI failure detected on this PMR automation PR. Triggering a retest to rule out flakiness before attempting a code fix.

Failing checks:
$(for each failing check: - <name>)

🤖 PMR CI retest triggered — job-pmr-pr-review"
```

```shell
# Jira comment (ADF)
# "CI failure detected on PR <URL>. Retest triggered for: <check names>.
#  If still failing on next run, failure log will be captured and a fix attempted."
```

Do not set any label yet. Skip remaining steps for this ticket this run.

**Case B — Prior retest comment exists AND checks are still failing:**

This is a confirmed real failure (not flaky). Now fetch the failure log.

**For Prow jobs** — parse `job_name` and `job_id` from `details_url`:

```text
https://prow.ci.openshift.org/view/gs/<bucket>/pr-logs/pull/<org_repo>/<pr>/<job_name>/<job_id>
```

Fetch the log:

```shell
curl -sS "https://prow.ci.openshift.org/log?job=<job_name>&id=<job_id>"
```

**For GitHub Actions jobs** — fetch via the jobs API (returns plain text, no
zip extraction needed):

```shell
gh api repos/<owner>/<repo>/actions/jobs/<job_id>/logs
```

Truncate log output to the last 200 lines if it exceeds that — the tail
contains the actual error.

**Capture the failure into Jira** (context preservation for the fix run, same
pattern as CHANGES_REQUESTED feedback):

Post an ADF Jira comment containing:

- PR URL and failing check names
- Truncated log tail (last 200 lines, in a code block)
- Closing line: "CI failure log captured for fix run.
  🤖 PMR CI retest triggered — job-pmr-pr-review"

**Set the label:**

```shell
jira -H "Content-Type: application/json" \
  -X PUT "$JB/rest/api/3/issue/<KEY>" \
  -d '{"update":{"labels":[{"add":"pmr-pr-ci-failing"}]}}'
```

Note: `pmr-pr-pending` stays on the ticket — the CI fix is an additional
state, not a replacement. `pmr-action-item`'s CI-fix mode will remove
`pmr-pr-ci-failing` and leave `pmr-pr-pending` when it pushes a fix.

**Post a PR comment** noting that the failure is confirmed and a fix run
will be triggered:

```shell
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="CI failure confirmed after retest. The failure log has been captured on the Jira ticket and an automated fix run will be triggered.

🤖 PMR CI retest triggered — job-pmr-pr-review"
```

**Case C — Prior retest comment exists AND checks are now green:**

The failure was flaky. No code fix needed. No action — skip this ticket.

### 5g. Open, CI green, no CHANGES_REQUESTED (awaiting initial review)

Applies to tickets from **Query A** where there is no active CHANGES_REQUESTED
and all check runs are green (or none have completed yet).

Calculate staleness:

```text
hours_open = now - pr.created_at
latest_activity = max(pr.updated_at, most-recent review's submitted_at if any)
hours_stale = now - latest_activity
```

Apply nudge tiers in step 6.

## 6. Nudge tiers (steps 5d and 5g)

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

**Post a PR comment:**

```shell
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="This PR was opened by the rosa-agent PMR automation and is awaiting review.

**Ticket:** <JIRA-KEY> — <ticket summary>
**PR age:** <N> days

If you have capacity, a review here would prevent this PMR action item from stalling.

🤖 PMR PR review nudge — job-pmr-pr-review"
```

**Post a Jira status comment** noting nudge sent and PR age.

### Tier 2 — 7 to 14 days

**Request review from CODEOWNERS** (if present). Fetch and parse:

```shell
gh api repos/<owner>/<repo>/contents/CODEOWNERS \
  | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['content']).decode())"
```

Parse the `*` catch-all line. Request review from individual logins
(team slugs require org admin and may be blocked):

```shell
gh api repos/<owner>/<repo>/pulls/<pr-number>/requested_reviewers \
  -f "reviewers[]=<login>"
```

If CODEOWNERS is absent or the reviewer request returns non-200, skip it
(log the reason) and continue with the comment.

**Post an escalated PR comment** noting urgency and PR age.

**Post an escalated Jira comment** noting Tier 2 escalation.

### Tier 3 — Over 14 days

All Tier 2 actions, plus file a GitHub Issue:

```shell
gh api repos/openshift-online/rosa-agent/issues \
  -f title="PMR PR stalled >14 days: <JIRA-KEY>" \
  -f body="A PMR action-item PR has been open for over 14 days with no review.

- **Jira ticket:** <JIRA-KEY> — <ticket summary>
- **PR:** <PR URL>
- **PR age:** N days
- **Nudges sent:** Tier 1 (48h), Tier 2 (7d), Tier 3 (14d)

Human escalation needed.

🤖 Filed by job-pmr-pr-review"
```

## 7. Output summary

```text
job-pmr-pr-review summary:
  Tickets scanned (pending):          N
  Tickets scanned (changes-req):      M
  Merged (closed):                    K
  Closed no-merge:                    J
  Changes-requested flagged:          X
  Revision limit hit (blocked):       L
  Revision stalled — nudged:          Y
  CI retest triggered:                C
  CI failure confirmed (fix queued):  F
  CI was flaky (resolved):            G
  Tier 0 (too new):                   P
  Tier 1 nudged:                      Q
  Tier 2 escalated:                   R
  Tier 3 escalated:                   S
  Deduped (skipped):                  D
  Errors:                             E
```

## On any failure

If any step fails in a way that stops the job:

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

Then stop. Never retry around a failure. A run finding no tickets is NOT a failure.

## Guardrails

- Never edit, merge, close, or reopen a PR. This job only comments and
  requests reviewers.
- Never remove `pmr-pr-pending` unless the PR is merged, closed, or has
  active CHANGES_REQUESTED. Never remove `pmr-pr-changes-requested` unless
  merged, closed, or revision limit hit.
- `pmr-pr-ci-failing` is added alongside `pmr-pr-pending` (not instead of it)
  — both labels coexist while the fix run is in progress. `pmr-action-item`
  removes `pmr-pr-ci-failing` when it pushes the fix.
- Never attempt a CI fix if `pmr-pr-ci-failing` is already on the ticket —
  a fix run is already in flight.
- Never nudge a PR more than once per 24 hours (the dedup checks in steps 5c
  and 6).
- Revision limit is 3 cycles — do not raise it without updating this skill.
- Never retry past a `403` or `policy_denied` — file failure issue and stop.

## Related

- **`pmr-action-item`**: Opens the PR (sets `pmr-pr-pending`), handles
  revision runs (`pmr-pr-changes-requested`), sets `pmr-ai-wip`/`pmr-ai-blocked`
- **`pmr-remediation-analysis`**: Sets `pmr-ai-ready` (Stage 2)
- **MCS-1516**: Initial design ticket for this job
- **MCS-1517**: Resilience improvements (this implementation)
- **MCS-46**: PMR action pipeline (parent epic)

---

🤖 *MCS-1516 / MCS-1517: PMR PR review nudge job*
