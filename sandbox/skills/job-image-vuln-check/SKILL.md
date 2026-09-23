---
name: job-image-vuln-check
description: Nightly job that checks a quay.io image for fixable CVEs and remediates them via PR. Retrieval is deterministic (wraps quay-vuln-report); remediation is agent-driven inference (locate the pin, verify a real fix exists, never downgrade, test before/after, split fix and new-tests into separate PRs). Use when asked to run the image vuln-check job, check an image for CVEs and fix them, or when triggered by a cron/scheduled invocation referencing job-image-vuln-check. Trigger keywords - image vuln check, nightly CVE job, fixable vulnerabilities, remediate CVE, job-image-vuln-check.
---

# Job: check a quay.io image for fixable CVEs and remediate them

Wrapper job. Retrieval is `quay-vuln-report`, invoked by the bundled
`job_image_vuln_check` package (never its internals - the CLI, exactly as
that skill's own SKILL.md documents). Remediation below is your own
inference, not scripted - the scripts only handle the deterministic parts.

## 1. Get your target

You need an image reference and (ideally) the GitHub repo that produced it.
Both come from the invocation (e.g. `/job-image-vuln-check --image
quay.io/ns/path:tag --repository owner/repo`, or a `--targets` JSON map for
several images). **Never hardcode an image or repo** - if none was given,
ask.

## 2. Retrieve

```shell
cd /sandbox/.claude/skills/job-image-vuln-check
python3 -m job_image_vuln_check --image <REF> [--repository <owner/repo>]
# or: --targets '{"<image-ref>": "<owner/repo-or-null>", ...}'
```

Output is a JSON array, one report per target, each with the CVE list
(`cve`, `severity`, `package`, `installed_version`, `fixed_in_version`,
`layer_introduced_in`, `cve_link`) plus `source_repository`.

- `vulnerability_count == 0` for a target → nothing to do there. If every
  target is 0, report success and stop. Normal outcome, not a failure.
- `source_repository: null` → a `note` explains why (no source label, or a
  skopeo error - e.g. quay.io's blob storage redirecting to a host this
  sandbox doesn't allow-list is a known, reportable case, see *Failures*).
  Use your own judgment (image name, quay.io repo description, web search)
  to identify the repo before acting on that target; skip it and say so in
  your summary if you genuinely can't.

## 3. Sync the target repo's fork

Before touching anything in a resolved repo:

```shell
FORK=<your-account>/<repo-name> UPSTREAM=<owner/repo> \
  bash /sandbox/.claude/skills/job-image-vuln-check/scripts/sync_fork.sh
```

Discovers the actual default branch itself - never assume `main`. Exit code
2 means the fork has diverged; a failure Issue is already filed, stop, don't
force a merge. Do this fresh for every run, even against a repo you synced
before - don't reuse a stale conclusion about what's still outstanding.

## 4. Remediate, one CVE-fix at a time

For each independently-fixable vulnerability (group multiple CVE IDs that
share the same package+fixed-version+layer into one fix):

1. Find where the version is actually pinned in the repo (e.g. a
   Containerfile `ARG`, a `go.mod`, a `requirements.txt` - search, don't
   guess). If the pin already meets or exceeds the fix version, it's
   already remediated - skip it, don't open a duplicate PR.
2. Verify the fix version against real upstream evidence (release notes,
   changelog, the dependency's own `go.mod`) - cite it in the PR body.
3. **Never downgrade.** If the only available fix is older than the current
   pin, don't apply it.
4. If nothing fixes it yet (e.g. a transitive dep of a third-party tool with
   no new release), don't force a change - note it as an unfixable
   follow-up and move on.
5. Run `make test` (repo root) - must pass before you touch anything.
6. Apply the single bump. Run `make test` again - must still pass.
7. Check coverage, but only if the bumped dependency is actually called by
   this repo's own code (not just an opaque pinned CLI-tool binary the
   build downloads and never calls into): does an existing test exercise
   that call site and its immediate caller? If there's a real gap, write a
   test against **this repo's own code** - never against the third-party
   library's internals. Say explicitly when this step is a no-op.
8. Commit, push, then open the fix-only PR:

   ```shell
   git push origin <branch>
   FORK_OWNER=<your-account> UPSTREAM=<owner/repo> BRANCH=<branch> \
     TITLE="<title naming the CVE(s)>" BODY="<summary + evidence>" \
     bash /sandbox/.claude/skills/job-image-vuln-check/scripts/open_pr.sh
   ```

9. If (and only if) step 7 added tests, open a **second, separate** PR
   (same script, new branch) containing only those tests, referencing the
   fix PR's number. Never combine a fix and new tests in one PR/commit.

Repeat per fixable CVE bucket. One fix per PR - never batch.

## Guardrails

- Never downgrade. Never batch multiple fixes in one PR. Never force-push.
- Verify version/evidence claims against something real - never invent one.
- Never print, commit, or put a literal credential anywhere - only the
  `openshell:resolve:env:*` / `${GITHUB_TOKEN}`-style placeholders already
  used throughout this repo.
- No `ROSA-Agent` label on these PRs (unlike `job-sop-improve`'s target).

## Failures

Any policy-denied (HTTP 403) response, or any other step that stops the
run cleanly, gets reported via `file_failure_issue` (sourced from
`scripts/common.sh`) against `openshift-online/rosa-agent` - never retried,
never routed around. `sync_fork.sh` and `open_pr.sh` already call it
themselves on their own failures; call it directly for anything else:

```shell
source /sandbox/.claude/skills/job-image-vuln-check/scripts/common.sh
file_failure_issue "<stage>" "<exact captured error>"
```
