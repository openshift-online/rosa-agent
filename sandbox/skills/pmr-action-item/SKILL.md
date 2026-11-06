---
name: pmr-action-item
description: Investigate and resolve a PMR (Post-Mortem Review) action-item Jira ticket (labels pmr-ai and pmr-ai-ready) assigned to the rosa-agent user - read the ticket, fork the repo it concerns, implement the described work on a feature branch, run security review, then critical code review with a different model, implement review findings, re-run security check, and open a PR. Also handles revision runs when job-pmr-pr-review sets pmr-pr-changes-requested after a reviewer requests changes - in that mode, checks out the existing branch, reads reviewer feedback from Jira comments and GitHub, addresses each point, re-runs security and code review, pushes, and flips the label back to pmr-pr-pending. Built for per-ticket invocation (one issue key per run). Trigger keywords - PMR, PMR action item, pmr-ai-ready, pmr-pr-changes-requested, post-mortem action item, investigate ticket, action item ticket, address review feedback.
---

# PMR action item: investigate a ticket, fix it, PR it

This skill handles one Jira ticket per run - a PMR (Post-Mortem Review)
action item labeled `pmr-ai` and `pmr-ai-ready`, assigned to the `rosa-agent` user. It is not
itself a scheduled job: it takes a single issue key and works that one
ticket end to end. A future enumerator/CronJob could discover every open
`pmr-ai-ready` ticket assigned to `rosa-agent` and invoke this skill once per key
(mirroring how `job-image-vuln-check` fans out over its target list), but
that enumeration is out of scope here.

This skill runs in two modes, detected automatically in step 2:

- **Initial run** (`pmr-ai-ready` label present, no `pmr-pr-changes-requested`):
  fresh implementation on a new branch ending with an open PR.
- **Revision run** (`pmr-pr-changes-requested` label present): a reviewer
  requested changes on an existing PR. The agent returns to the branch,
  addresses each piece of feedback, and re-opens the review cycle.

**The `pmr-ai-ready` label indicates:**

- The ticket has been through the proposal stage (`pmr-remediation-analysis` skill)
- A structured remediation proposal exists
- Human grooming gate has approved (or is pending)
- The ticket is ready for implementation

Retrieval/mechanics that are genuinely deterministic (fork sync, failure
reporting) are scripted below, exactly like the other job skills in this
repo. Everything else - reading the ticket, finding the right repo, writing
the fix, deciding whether it's safe to ship - is your own judgment, not
scripted.

**This entire run is non-interactive.** It's invoked as `claude
--dangerously-skip-permissions --print "<prompt>"` (directly by a human, or
later by a cron-driven sandbox) with no one watching and no terminal to type
into. There is no human to answer a question or approve a step at any point
before step 8 - the PR itself is the first and only place a human reviews
this run's work. Never pause expecting input, and never phrase anything as a
question you expect a reply to. Every "I don't know X" case below has exactly
two non-interactive outs: a missing/invalid invocation (no issue key, an
unrecognized key, etc.) is a usage error - stop and `file_failure_issue`
(the Failures section below); anything ticket-specific you can't resolve on your own (which
repo, an unsafe finding, failing tests, a blocked endpoint) is a blocker -
stop and comment on the ticket (step 7). Never guess past either case just to
keep moving.

## 1. Get your target

You need a single Jira issue key (e.g. `/pmr-action-item ROSAENG-62312`, or
named in the invoking prompt). **Never guess a key.** If none was given, this
is a usage error, not a blocker to negotiate with the ticket - there's no
ticket yet to comment on. Stop and report it via `file_failure_issue` (see
the Failures section below): a human reads that later, not a live prompt.

## 2. Read the ticket and detect run mode

Per `AGENTS.md`, any Jira call MUST go through the `jira` skill - load it now
and run its auth-detection block once. Then:

`GET $JB/rest/api/3/issue/<ISSUE-KEY>`

- Confirm the `pmr-ai` label is present. If not → stop and report via
  `file_failure_issue` (bad invocation - this skill is only for PMR action items).
- **Detect run mode from the labels:**
  - `pmr-pr-changes-requested` present → **revision run**. Skip to
    [Revision mode](#revision-mode) below; do not follow steps 3–12.
  - `pmr-ai-ready` present, `pmr-pr-changes-requested` absent → **initial run**.
    Continue with step 3.
  - Neither `pmr-ai-ready` nor `pmr-pr-changes-requested` → the ticket hasn't
    been through the proposal stage. Stop and report via `file_failure_issue` -
    this is a workflow violation, not a ticket blocker.
- Read the full description and every comment - the work to do is described
  there, not inferred from the summary line alone.
- **Do not self-assign the ticket.** Assignment to `rosa-agent` is expected
  to already be set by whatever triggered this run - never call `PUT
  .../assignee` yourself. If the ticket is assigned to someone else, stop
  there - note it in the final output and do not touch their ticket; don't
  `file_failure_issue` for this, it isn't a failure.

## 3. Identify the target repository

This is inference, not a script: look for an explicit `owner/repo` or GitHub
URL in the description/comments, the Jira project/component naming
conventions, or linked issues. If you genuinely cannot determine which repo
the ticket concerns, that's a blocker - go to step 7 (comment on the ticket
asking for the repo) rather than guessing one.

## 4. Fork and sync, then branch

Before touching anything in the resolved repo, sync your fork to its
upstream default branch (this never assumes `main` - it discovers the real
default branch, and never force-merges a divergence):

```shell
FORK=rosa-agent/<repo-name> UPSTREAM=<owner>/<repo> \
  bash /sandbox/.claude/skills/pmr-action-item/scripts/sync_fork.sh
```

- Exit code 2 means the fork has diverged from upstream; a failure Issue is
  already filed against `openshift-online/rosa-agent`. Stop - don't force a
  merge.
- Do this fresh every run, even against a repo you've synced before.

Then check out a feature branch named from the issue key, e.g.:

```shell
git checkout -b "pmr-action-item/<ISSUE-KEY>"
```

## 5. Implement the work described in the ticket

Your own inference - there is no script for this step. Before changing
anything, run the target repo's own test suite (however it's invoked there -
`make test`, `go test ./...`, etc.) so you know the baseline is green. Make
the change the ticket describes. Run the same test suite again - it must
still pass. Commit, with a message that references the issue key.

For how the code itself is written and tested - TDD per task, pure functions
behind thin I/O shells, hermetic tests, typed errors, security defaults -
follow sections 2 and 3 of the `implement-ticket` skill. This skill owns the
run, so its mechanics win wherever the two differ: the branch name above,
the step 6 fan-out and its CONFIRMED/PLAUSIBLE finding format, the
non-interactive rules, blockers going on the ticket, and how the PR is
opened in step 8.

## 6. Security fan-out - mandatory, both of the following

Before opening any PR, vet the diff two ways:

1. **Fan out parallel subagents** (the Agent/Task tool) over the diff, each
   with a distinct, narrow focus:
   - secrets or credential leakage (anything that could expose a token,
     password, or the credential placeholders (openshell resolve-env) themselves);
   - injection or unsafe shell/eval/deserialization;
   - authorization or permission widening (e.g. a new endpoint, RBAC rule,
     or policy change that grants more than the ticket called for);
   - supply-chain/dependency risk (a new or bumped dependency, where it
     comes from, whether it's pinned).

   Each subagent reports findings as CONFIRMED or PLAUSIBLE, with an exact
   file/line and a concrete failure scenario - not vague suspicion.

2. **Separately, invoke this harness's own `security-review` skill** against
   the same diff, as a second, independent pass.

Any CONFIRMED finding from either pass means: **do not open the PR.** Either
fix it and re-run both passes until clean, or - if you can't safely fix it
yourself - treat it as a blocker (step 10): comment on the ticket describing
the exact finding, and stop. Never open a PR carrying an unresolved CONFIRMED
finding.

## 7. Critical code review with independent model - mandatory

After the security fan-out passes, perform a comprehensive code review using a
**different model** than the one that implemented the change. This provides an
independent perspective on code quality, correctness, and best practices.

**Use the Agent tool with `model="fable"` parameter** to spawn a code-review
subagent running Claude Fable (or `model="opus"` if Fable is unavailable, or
`model="haiku"` as a fallback). The key is using a different model than Sonnet
for independent perspective.

**To change the review model**: Edit the `model` parameter in the Agent call
below. Current default is `"fable"`. Change to `"opus"` or `"haiku"` based on
what's available in Vertex AI.

Give the reviewing agent:

- The full diff (`git diff main...HEAD` or equivalent)
- The original Jira ticket context (what was requested)
- The target repository's conventions (language, style, patterns observed)

Ask the reviewing agent to critically assess:

1. **Correctness** - does the code actually solve the ticket's requirement?
   Does it handle edge cases? Are there logic errors or off-by-one mistakes?
2. **Robustness** - error handling, nil/null checks, boundary conditions
3. **Maintainability** - code clarity, naming, structure, unnecessary complexity
4. **Testing** - are the existing tests sufficient? Are new tests needed for
   new code paths or edge cases?
5. **Integration** - does this fit the existing codebase patterns and
   conventions? Does it break encapsulation or introduce coupling?
6. **Performance** - obvious inefficiencies, unnecessary allocations, N+1
   queries

The reviewer must return **structured findings** with:

- **Severity**: CRITICAL (blocks PR), IMPORTANT (should fix), MINOR (nice-to-have)
- **File and line number**
- **Specific issue** - not vague ("improve error handling") but concrete
  ("function X doesn't check for nil Y before calling Y.Method()")
- **Suggested fix** - what specific change resolves it

**CRITICAL findings block the PR** - proceed to step 8 to implement them.
IMPORTANT findings should be implemented if straightforward. MINOR findings can
be noted but don't block.

## 8. Implement code review findings

For each CRITICAL and IMPORTANT finding from step 7:

1. Read the specific file and line
2. Implement the suggested fix (or a better alternative if you have one)
3. Run the test suite again - it must still pass
4. Commit the fix with a message like: `Address code review: <brief description>`

If you cannot safely implement a CRITICAL finding (e.g. it requires domain
knowledge you don't have, or the suggested fix itself seems wrong), treat it as
a blocker (step 10): comment on the ticket with the specific review finding and
why you can't resolve it, then stop.

## 9. Re-run security fan-out after implementing review findings

After implementing code review changes, **re-run the full security fan-out from
step 6** (parallel subagents + security-review skill) against the updated diff.

The review fixes may have introduced new code that needs security vetting. Any
CONFIRMED security finding at this stage means: fix it and loop back through
steps 7-9 until both security and code review are clean, or treat it as a
blocker (step 10) if you can't safely fix it.

**Only proceed to step 11 (open PR) when BOTH security review AND code review
are clean with no unresolved CONFIRMED/CRITICAL findings.**

## 10. Blockers - especially access issues - go on the ticket

You are running in an OpenShell sandbox behind a proxy-enforced,
allow-listed egress policy (`policies/default.yaml`) with the credential and
skill constraints in `AGENTS.md`. **Any access issue that stops you doing
the work is a blocker, not something to route around:**

- a `403` / `policy_denied` / `credential_endpoint_mismatch` response from
  anywhere (Jira, GitHub, the target repo's own CI/registry/etc.);
- a host or endpoint you'd need that isn't allow-listed;
- missing permissions on the target repo or fork (e.g. can't push, can't
  open a PR, can't attach a label - compare `job-image-vuln-check`'s and
  `github-labels`' own "report and stop on 403" convention);
- an expired/invalid credential placeholder;

and so are these, non-access blockers that equally stop forward progress:

- an ambiguous or underspecified requirement in the ticket (including "I
  can't tell which repo this is about" from step 3);
- a security finding from step 6 or 9 you can't safely resolve yourself;
- a critical code review finding from step 7 you can't safely resolve yourself;
- failing tests you can't get green;
- anything else that stops you from completing the ticket.

**Do not retry around it, guess past it, or silently stop.** Instead, add a
comment to the ticket (via the `jira` skill) stating plainly what you were
doing and the exact cause of the issue - the literal error, HTTP status, and
endpoint/host where relevant - so the engineers watching the ticket can
triage it and figure out what to unblock, and the work can resume on a later
run. This is a normal, expected outcome of hitting the edge of what this
sandbox can reach - not a failure to hide or apologize for.

## 11. Open the PR

Push the branch, then open the PR with this skill's attribution label via
the `github-labels` skill (never construct the label-attach calls yourself):

```shell
git push origin "pmr-action-item/<ISSUE-KEY>"
FORK_OWNER=rosa-agent UPSTREAM=<owner>/<repo> \
  BRANCH="pmr-action-item/<ISSUE-KEY>" \
  TITLE="<ISSUE-KEY>: <short description>" \
  BODY="<summary of the change, referencing <ISSUE-KEY>>" \
  bash /sandbox/.claude/skills/github-labels/open-labeled-pr.sh
```

## 12. Link back to the ticket and signal review-pending

If step 10 didn't already fire for this run: add a Jira remote (web) link on
the ticket pointing at the new PR (`POST .../remotelink`), post a short
comment summarizing what was changed and why, then add the `pmr-pr-pending`
label to signal that the PR is open and awaiting review.

The `pmr-pr-pending` label is the handoff to the `job-pmr-pr-review` cron job,
which monitors open PRs and nudges reviewers when they stall. Add it only
after the PR URL is confirmed open — never add it if this run ends in a
blocker comment (step 10).

```shell
# After adding the remotelink and comment:
jira -H "Content-Type: application/json" \
  -X PUT "$JB/rest/api/3/issue/<ISSUE-KEY>" \
  -d '{"update":{"labels":[{"add":"pmr-pr-pending"}]}}'
```

## Revision mode

**Entry point:** ticket has `pmr-pr-changes-requested` label (set by
`job-pmr-pr-review` after detecting a reviewer's CHANGES_REQUESTED state).

The PR branch already exists on the fork. The reviewer's feedback is already
captured as a Jira comment by `job-pmr-pr-review` — your Jira read in step 2
already loaded it. This mode's job is to address that feedback and re-enter
the review cycle.

### R1. Identify the existing PR

Fetch remote links on the ticket and find the GitHub PR URL:

```shell
jira -H "Accept: application/json" "$JB/rest/api/3/issue/<KEY>/remotelink"
# Extract: https://github.com/<owner>/<repo>/pull/<pr-number>
```

If no PR URL is found, this is a blocker — comment on the ticket and stop
(same blocker rule as step 10 of the initial run).

### R2. Re-fetch reviewer feedback from GitHub

Belt-and-suspenders: even though `job-pmr-pr-review` already wrote the
feedback to Jira, also fetch it directly from GitHub to ensure no inline
comments were missed:

```shell
gh api "repos/<owner>/<repo>/pulls/<pr-number>/reviews" \
  --jq '[.[] | select(.state == "CHANGES_REQUESTED") | {user: .user.login, body, submitted_at}]'

gh api "repos/<owner>/<repo>/pulls/<pr-number>/comments" \
  --jq '[.[] | {user: .user.login, path, line, body}]'
```

Merge the GitHub feedback with the Jira comment you already read. If there
is any discrepancy, trust GitHub (it is authoritative). Build a complete,
deduplicated list of every change requested.

### R3. Check out the existing branch

Sync the fork as in step 4, then check out the branch that already exists
(do not create a new one):

```shell
FORK=rosa-agent/<repo-name> UPSTREAM=<owner>/<repo> \
  bash /sandbox/.claude/skills/pmr-action-item/scripts/sync_fork.sh

# The branch name follows the initial-run convention:
git checkout "pmr-action-item/<ISSUE-KEY>"
```

If the branch does not exist on the fork, that is a blocker — comment on the
ticket and stop. Do not create a new branch.

Run the test suite to establish a clean baseline before making any changes.

### R4. Address each piece of reviewer feedback

Work through every CHANGES_REQUESTED item methodically:

- Read the specific file and line the reviewer pointed to.
- Understand the concern before changing anything — if the reviewer's
  suggested fix seems wrong or unsafe, treat it as a blocker (comment on the
  ticket explaining why, and stop).
- Implement the change. Run the test suite after each logical group of
  changes — it must stay green.
- Commit with a message like: `<ISSUE-KEY>: address review: <brief description>`

Do not make unrelated changes in this pass. Address only what reviewers
asked for.

### R5. Re-run the full security fan-out and code review

After addressing all reviewer feedback, repeat steps 6–9 of the initial run
in full:

1. Security fan-out (parallel subagents: secrets, injection, auth widening,
   supply-chain) — any CONFIRMED finding blocks the push.
2. Independent code review with a different model (the same fable/opus/haiku
   rule as step 7).
3. Implement any CRITICAL/IMPORTANT findings from the code review.
4. Re-run security fan-out against the updated diff.

Only proceed when both passes are clean.

### R6. Push and update the PR

Push the new commits to the existing branch on the fork:

```shell
git push origin "pmr-action-item/<ISSUE-KEY>"
```

The push updates the existing PR automatically — do not open a new PR.

Post a PR comment summarising what was addressed:

```shell
gh api repos/<owner>/<repo>/issues/<pr-number>/comments \
  -f body="Revision pushed addressing the requested changes:

<bullet list of each reviewer point and how it was addressed>

Security fan-out and independent code review passed on the updated diff.

🤖 pmr-action-item revision run"
```

### R7. Update Jira and flip the label back

Post a Jira comment summarising what changed and linking to the new commits.

Then swap the label back to `pmr-pr-pending` to re-enter the review cycle:

```shell
jira -H "Content-Type: application/json" \
  -X PUT "$JB/rest/api/3/issue/<KEY>" \
  -d '{"update":{"labels":[{"remove":"pmr-pr-changes-requested"},{"add":"pmr-pr-pending"}]}}'
```

The `job-pmr-pr-review` cron job will now monitor the PR again and nudge
reviewers if it stalls.

**Revision run complete.** Do not follow steps 3–12 of the initial run.

---

## Guardrails

- Never force-push, and never force a fork-sync past a divergence.
- Never skip or weaken the security fan-out in step 6 or 9 to save time.
- Never skip the critical code review in step 7 - the independent model review
  is mandatory, not optional.
- Never invent a target repository, a Jira host/email, or any credential -
  if you don't know it, that's a blocker (step 10), not something to guess.
- Never retry past a `403` or any other access issue - report the exact
  cause (via a ticket comment if it's ticket-specific per step 10, via
  `file_failure_issue` if it's this job's own mechanics) and stop.
- One ticket, one PR per run.

## Failures

A genuine technical failure in this job's own mechanics (not a ticket-specific
blocker - see step 7 for those) gets reported via `file_failure_issue`
(sourced from `scripts/common.sh`) against `openshift-online/rosa-agent` -
never retried, never routed around. `sync_fork.sh` already calls it on its
own failure; call it directly for anything else:

```shell
source /sandbox/.claude/skills/pmr-action-item/scripts/common.sh
file_failure_issue "<stage>" "<exact captured error>"
```
