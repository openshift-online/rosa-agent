---
name: github-labels
description: Deterministic script that forks upstream, opens a PR, creates a label on upstream if it doesn't already exist, and attaches it to the PR. Use whenever a job needs to open a PR that must carry a specific label (e.g. ROSA-Agent) against a repo that may never have seen that label before. Trigger keywords - github label, create label, PR label, ROSA-Agent label, label does not exist, fork and PR with label.
---

# github-labels: deterministic fork -> PR -> create-label -> attach-label

This skill is a single script, not instructions to interpret. Every step in
`open-labeled-pr.sh` (in this skill's directory) is a fixed `gh api`
(REST-only) call with explicit success/failure handling — there is no
judgment call left for the agent once the required inputs are set.

It replaces the ad hoc English fallback previously used inline in jobs (e.g.
`job-sop-improve`'s "if `--label` fails because the label does not yet exist,
add it after" note) with one reusable, generalized script.

## Precondition

Commit your changes and push the feature branch to your fork **before**
calling this script. It does not create branches, commit, or push — only
forks, opens the PR, creates the label if missing, and attaches it.

## Usage

```shell
FORK_OWNER=<your-account> \
UPSTREAM=<org>/<repo> \
BRANCH=<branch-already-pushed-to-your-fork> \
TITLE="<pr title>" \
BODY="<pr body>" \
bash sandbox/skills/github-labels/open-labeled-pr.sh
```

Prints the new PR number on stdout. `LABEL`, `LABEL_COLOR`, and `LABEL_DESC`
are optional and default to the `ROSA-Agent` label convention already used
on `openshift/ops-sop` and `openshift-online/rosa-agent` (color `793B90`,
description `ROSA-Agent-involvement label`) — override them for a different
label:

```shell
LABEL=needs-review LABEL_COLOR=fbca04 LABEL_DESC="Needs a maintainer look" \
  ... bash sandbox/skills/github-labels/open-labeled-pr.sh
```

## What it does, in order

1. `POST repos/$UPSTREAM/forks` — fork upstream into `$FORK_OWNER`. Forking
   an already-forked repo is a no-op in the GitHub API, so this is always
   safe to call.
2. `GET repos/$UPSTREAM` then `POST repos/$UPSTREAM/pulls` — discover
   upstream's actual default branch (never hard-code `main` vs `master`),
   then open the PR from `$FORK_OWNER:$BRANCH` against it.
3. `POST repos/$UPSTREAM/labels` — **create `$LABEL` on upstream.** A `422`
   here means it already exists, which is success, not failure — an
   existing label's color/description is never overwritten. This is the
   step that makes step 4 work reliably even the first time a job runs
   against a repo that has never had `$LABEL`.
4. `POST repos/$UPSTREAM/issues/$PR_NUMBER/labels` — attach `$LABEL` to the
   PR (the issues endpoint works for PRs too).

## On failure

Any failed step (1, 2, or 4 — step 3's `422` is not a failure) files a
GitHub Issue against `openshift-online/rosa-agent` — this job's own repo —
via `POST repos/openshift-online/rosa-agent/issues`, then exits non-zero.
This mirrors `job-sop-improve`'s failure-reporting convention: failures are
always surfaced against `openshift-online/rosa-agent`, never against the
upstream repo being contributed to.

## What this skill does NOT do

- Does not create the branch, commit, or push — do that with plain `git`
  first.
- Does not decide whether a PR needs a label or what it should be — that is
  the caller's decision.
- Does not retry on policy-denied (`403`) responses from the sandbox proxy.
  A `403` is treated the same as any other failure: file the Issue, stop.
  Do not retry variations or attempt to bypass the proxy.
