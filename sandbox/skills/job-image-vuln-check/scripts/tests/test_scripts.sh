#!/usr/bin/env bash
# test_scripts.sh - hermetic tests for sync_fork.sh / open_pr.sh / common.sh.
#
# No real network calls anywhere: `gh` is replaced by a local stub on PATH
# that handles exactly the invocations these scripts issue (logging each one
# for assertions), and the one real `git` remote traffic these scripts
# generate (`https://github.com/<owner>/<repo>.git`) is transparently
# rewritten via `url.<local-bare-repo>.insteadOf`, scoped to an isolated
# GIT_CONFIG_GLOBAL/HOME per test - to a local bare repository created in a
# throwaway temp directory. `git` itself is exercised for real (fetch,
# ff-only merge, push) against those local-only repos, which is the actual
# logic worth testing; only the network boundary is faked.
#
# No test framework is installed in this image (no bats/shunit2) - this is
# a small self-contained harness: each `test_*` function runs in its own
# sandbox, assertions call `fail` on mismatch, and the script exits non-zero
# if any assertion failed.
#
# shellcheck disable=SC2317
# (every function here is invoked indirectly, by name, from the dispatch
# loop at the bottom of the file - shellcheck's reachability analysis can't
# see those call sites and misreports the entire body as unreachable; this
# is exactly the documented false-positive case for SC2317.)

set -uo pipefail  # not -e: keep going and report every failure, not just the first

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILURES=0

fail() {
  echo "NOT OK: $1" >&2
  FAILURES=$((FAILURES + 1))
}

ok() {
  echo "ok: $1"
}

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [ "$expected" = "$actual" ]; then
    ok "$msg"
  else
    fail "$msg (expected '$expected', got '$actual')"
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    ok "$msg"
  else
    fail "$msg (expected to find '$needle')"
  fi
}

# ---------------------------------------------------------------------------
# Sandbox setup: isolated $HOME/git-config/PATH, nothing shared across tests
# and nothing that touches the real network or the real gh CLI.
# ---------------------------------------------------------------------------

setup_sandbox() {
  SANDBOX="$(mktemp -d)"
  export HOME="$SANDBOX/home"
  mkdir -p "$HOME"
  export GIT_CONFIG_GLOBAL="$SANDBOX/gitconfig"
  : > "$GIT_CONFIG_GLOBAL"
  git config --global user.email "test@example.invalid"
  git config --global user.name "Test User"
  git config --global init.defaultBranch main

  GH_LOG="$SANDBOX/gh.log"
  : > "$GH_LOG"
  FAKE_BIN="$SANDBOX/bin"
  mkdir -p "$FAKE_BIN"
  export PATH="$FAKE_BIN:$PATH"
  export GH_LOG FAKE_BIN
  write_fake_gh
}

teardown_sandbox() {
  rm -rf "$SANDBOX"
}

# A single fake `gh` handling exactly the invocations sync_fork.sh,
# open_pr.sh, and common.sh issue. Every call is logged to $GH_LOG.
# Behavior is tuned per-test via FAKE_GH_* env vars (read at call time, not
# baked into the script, so one stub serves every test).
write_fake_gh() {
  cat > "$FAKE_BIN/gh" <<'FAKE_GH_EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"

case "$*" in
  "auth setup-git")
    exit 0
    ;;
  "api repos/"*"--jq .default_branch"*)
    if [ "${FAKE_GH_FAIL_DEFAULT_BRANCH:-0}" = "1" ]; then
      echo "simulated: could not look up default branch" >&2
      exit 1
    fi
    echo "${FAKE_GH_DEFAULT_BRANCH:-main}"
    ;;
  "repo clone "*)
    dir="$4"
    git clone --quiet "$FAKE_FORK_REMOTE" "$dir"
    ;;
  "api -X POST repos/"*"/pulls "*)
    if [ "${FAKE_GH_FAIL_PR_CREATE:-0}" = "1" ]; then
      echo "simulated: pr create rejected" >&2
      exit 1
    fi
    echo "${FAKE_GH_PR_NUMBER:-7}"
    ;;
  "api -X POST repos/openshift-online/rosa-agent/issues "*)
    exit 0
    ;;
  *)
    echo "fake gh: unhandled invocation: $*" >&2
    exit 99
    ;;
esac
FAKE_GH_EOF
  chmod +x "$FAKE_BIN/gh"
}

# Redirect git traffic for "https://github.com/$1.git" to local path $2 -
# scoped to this sandbox's isolated GIT_CONFIG_GLOBAL, never the real network.
redirect_github_url() {
  git config --global "url.$2.insteadOf" "https://github.com/$1.git"
}

git_bare_with_commit() {
  local dir="$1" branch="$2" message="$3"
  local work
  work="$(mktemp -d)"
  git init --quiet -b "$branch" "$work"
  git -C "$work" commit --quiet --allow-empty -m "$message"
  git init --quiet --bare "$dir"
  git -C "$work" push --quiet "$dir" "$branch"
  rm -rf "$work"
}

git_add_commit_to_bare() {
  local dir="$1" branch="$2" message="$3"
  local work
  work="$(mktemp -d)"
  git clone --quiet "$dir" "$work"
  git -C "$work" checkout --quiet "$branch"
  git -C "$work" commit --quiet --allow-empty -m "$message"
  git -C "$work" push --quiet origin "$branch"
  rm -rf "$work"
}

git_rev() {
  git -C "$1" rev-parse "$2"
}

# ---------------------------------------------------------------------------
# sync_fork.sh
# ---------------------------------------------------------------------------

test_sync_fork_fast_forwards_and_pushes() {
  setup_sandbox
  local upstream="$SANDBOX/upstream.git" fork="$SANDBOX/fork.git" work="$SANDBOX/work"

  git_bare_with_commit "$fork" main "initial"
  cp -r "$fork" "$upstream"
  git_add_commit_to_bare "$upstream" main "upstream is ahead by one commit"

  redirect_github_url "acme/rosa-agent" "$upstream"
  export FAKE_FORK_REMOTE="$fork"

  local out
  out="$(FORK=acme/rosa-agent UPSTREAM=acme/rosa-agent DIR="$work" bash "$SCRIPTS_DIR/sync_fork.sh" 2>"$SANDBOX/stderr")"
  local rc=$?

  assert_eq "0" "$rc" "sync_fork.sh exits 0 on a clean fast-forward"
  assert_eq "main" "$out" "sync_fork.sh prints the synced branch name"
  assert_eq "$(git_rev "$upstream" main)" "$(git_rev "$work" main)" "local checkout is fast-forwarded to upstream's tip"
  assert_eq "$(git_rev "$upstream" main)" "$(git_rev "$fork" main)" "the fork remote itself was pushed to upstream's tip"
  assert_contains "$(cat "$GH_LOG")" "--jq .default_branch" "looked up the default branch via the API, not a hardcoded 'main'"
  assert_contains "$(cat "$GH_LOG")" "auth setup-git" "wired git's credential helper before pushing (regression: plain git push otherwise fails with 'could not read Username')"
  assert_eq "" "$(grep -F "issues " "$GH_LOG" || true)" "no failure Issue filed on a clean sync"

  teardown_sandbox
}

test_sync_fork_refuses_to_force_a_diverged_fork() {
  setup_sandbox
  local upstream="$SANDBOX/upstream.git" fork="$SANDBOX/fork.git" work="$SANDBOX/work"

  git_bare_with_commit "$fork" main "initial"
  cp -r "$fork" "$upstream"
  git_add_commit_to_bare "$upstream" main "upstream moves on"
  git_add_commit_to_bare "$fork" main "fork diverges independently"
  local fork_tip_before_run
  fork_tip_before_run="$(git_rev "$fork" main)"

  redirect_github_url "acme/rosa-agent" "$upstream"
  export FAKE_FORK_REMOTE="$fork"

  FORK=acme/rosa-agent UPSTREAM=acme/rosa-agent DIR="$work" bash "$SCRIPTS_DIR/sync_fork.sh" >"$SANDBOX/stdout" 2>"$SANDBOX/stderr"
  local rc=$?

  assert_eq "2" "$rc" "sync_fork.sh exits 2 on a diverged fork"
  assert_eq "$fork_tip_before_run" "$(git_rev "$fork" main)" "the fork remote is left untouched - no forced push"
  assert_contains "$(cat "$GH_LOG")" "issues" "a failure Issue was filed against openshift-online/rosa-agent"

  teardown_sandbox
}

test_sync_fork_requires_fork_and_upstream() {
  setup_sandbox
  bash "$SCRIPTS_DIR/sync_fork.sh" >"$SANDBOX/stdout" 2>"$SANDBOX/stderr"
  assert_eq "1" "$?" "sync_fork.sh exits 1 when FORK/UPSTREAM are unset"
  teardown_sandbox
}

# ---------------------------------------------------------------------------
# open_pr.sh
# ---------------------------------------------------------------------------

test_open_pr_success_prints_pr_number() {
  setup_sandbox
  export FAKE_GH_PR_NUMBER=42
  local out rc
  out="$(FORK_OWNER=acme UPSTREAM=acme/rosa-agent BRANCH=my-branch TITLE="A fix" BODY="Details" \
    bash "$SCRIPTS_DIR/open_pr.sh" 2>"$SANDBOX/stderr")"
  rc=$?
  assert_eq "0" "$rc" "open_pr.sh exits 0 on success"
  assert_eq "42" "$out" "open_pr.sh prints the new PR number"
  assert_eq "" "$(grep -F "issues " "$GH_LOG" || true)" "no failure Issue filed on success"
  teardown_sandbox
}

test_open_pr_default_branch_failure_files_issue() {
  setup_sandbox
  export FAKE_GH_FAIL_DEFAULT_BRANCH=1
  FORK_OWNER=acme UPSTREAM=acme/rosa-agent BRANCH=my-branch TITLE="A fix" BODY="Details" \
    bash "$SCRIPTS_DIR/open_pr.sh" >"$SANDBOX/stdout" 2>"$SANDBOX/stderr"
  local rc=$?
  assert_eq "2" "$rc" "open_pr.sh exits 2 when the default-branch lookup fails"
  assert_contains "$(cat "$GH_LOG")" "issues" "a failure Issue was filed for the default-branch stage"
  teardown_sandbox
}

test_open_pr_create_failure_files_issue() {
  setup_sandbox
  export FAKE_GH_FAIL_PR_CREATE=1
  FORK_OWNER=acme UPSTREAM=acme/rosa-agent BRANCH=my-branch TITLE="A fix" BODY="Details" \
    bash "$SCRIPTS_DIR/open_pr.sh" >"$SANDBOX/stdout" 2>"$SANDBOX/stderr"
  local rc=$?
  assert_eq "2" "$rc" "open_pr.sh exits 2 when pr-create fails"
  assert_contains "$(cat "$GH_LOG")" "issues" "a failure Issue was filed for the pr-create stage"
  teardown_sandbox
}

test_open_pr_requires_all_env_vars() {
  setup_sandbox
  bash "$SCRIPTS_DIR/open_pr.sh" >"$SANDBOX/stdout" 2>"$SANDBOX/stderr"
  assert_eq "1" "$?" "open_pr.sh exits 1 when required env vars are unset"
  teardown_sandbox
}

# ---------------------------------------------------------------------------
# common.sh
# ---------------------------------------------------------------------------

test_common_file_failure_issue_posts_and_reports() {
  setup_sandbox
  local stderr_out
  stderr_out="$(bash -c "source '$SCRIPTS_DIR/common.sh'; file_failure_issue 'my-stage' 'exact error text'" 2>&1)"
  assert_contains "$(cat "$GH_LOG")" "issues" "file_failure_issue posts to the issues endpoint"
  assert_contains "$stderr_out" "my-stage" "file_failure_issue reports the stage on stderr"
  assert_contains "$stderr_out" "exact error text" "file_failure_issue reports the exact error on stderr"
  teardown_sandbox
}

# ---------------------------------------------------------------------------

for t in \
  test_sync_fork_fast_forwards_and_pushes \
  test_sync_fork_refuses_to_force_a_diverged_fork \
  test_sync_fork_requires_fork_and_upstream \
  test_open_pr_success_prints_pr_number \
  test_open_pr_default_branch_failure_files_issue \
  test_open_pr_create_failure_files_issue \
  test_open_pr_requires_all_env_vars \
  test_common_file_failure_issue_posts_and_reports \
  ; do
  echo "=== $t ==="
  "$t"
done

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All script tests passed."
  exit 0
else
  echo "$FAILURES script test(s) failed."
  exit 1
fi
