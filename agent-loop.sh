#!/usr/bin/env bash
set -Eeuo pipefail

MAX_ROUNDS="${MAX_ROUNDS:-8}"
SLEEP_AFTER_REREQUEST="${SLEEP_AFTER_REREQUEST:-45}"
STATE_DIR="${STATE_DIR:-.git/copilot-freebuff-loop}"
STATE_FILE="$STATE_DIR/seen-fingerprints.json"
LAST_FETCH_FILE="$STATE_DIR/last-comments.json"
PROMPT_FILE="$STATE_DIR/freebuff-prompt.txt"
LOG_FILE="$STATE_DIR/loop.log"
TEST_CMD="${TEST_CMD:-}"
FREEBUFF_CMD="${FREEBUFF_CMD:-freebuff}"
AUTO_COMMIT="${AUTO_COMMIT:-1}"
REQUEST_INITIAL_REVIEW_IF_NONE="${REQUEST_INITIAL_REVIEW_IF_NONE:-1}"
FREEBUFF_TIMEOUT="${FREEBUFF_TIMEOUT:-600}"       # max seconds per freebuff run
FREEBUFF_IDLE_TIMEOUT="${FREEBUFF_IDLE_TIMEOUT:-20}" # seconds of stable output before declaring done

# Track active tmux sessions for cleanup on exit.
_TMUX_SESSION=""
_cleanup() {
  trap - INT TERM  # prevent re-entrant cleanup from signal propagation
  if [[ -n "${_TMUX_SESSION:-}" ]] && tmux has-session -t "$_TMUX_SESSION" 2>/dev/null; then
    tmux send-keys -t "$_TMUX_SESSION" C-c 2>/dev/null || true
    sleep 1
    tmux send-keys -t "$_TMUX_SESSION" C-c 2>/dev/null || true
    sleep 1
    tmux kill-session -t "$_TMUX_SESSION" 2>/dev/null || true
  fi
  _TMUX_SESSION=""
}
trap _cleanup EXIT

mkdir -p "$STATE_DIR"
[[ -f "$STATE_FILE" ]] || echo '[]' > "$STATE_FILE"
: > "$LOG_FILE"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

fail() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

require_cmd gh
require_cmd jq
require_cmd git
require_cmd tmux
require_cmd "$FREEBUFF_CMD"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Run this inside a git repo."

if ! gh auth status >/dev/null 2>&1; then
  fail "gh is not authenticated. Run: gh auth login"
fi

REPO_FULL="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
OWNER="${REPO_FULL%%/*}"
REPO="${REPO_FULL#*/}"
PR_NUMBER="${PR_NUMBER:-$(gh pr view --json number -q .number 2>/dev/null || true)}"

[[ -n "${PR_NUMBER:-}" ]] || fail "No PR found for current branch. Open a PR first or set PR_NUMBER."

normalize_body_jq='
  gsub("```.*?```"; " ")
  | gsub("\\s+"; " ")
  | gsub("^[[:space:]]+|[[:space:]]+$"; "")
  | ascii_downcase
'

fetch_all_review_comments() {
  gh api \
    -H "Accept: application/vnd.github+json" \
    --paginate \
    "/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments"
}

fetch_copilot_comments() {
  fetch_all_review_comments | jq '
    map(
      select(
        (.user.login // "" | ascii_downcase | test("copilot"))
      )
    )
  '
}

comment_fingerprints() {
  jq "
    map({
      id,
      url: .html_url,
      created_at,
      updated_at,
      path: (.path // \"\"),
      line: (.line // .original_line // null),
      start_line: (.start_line // .original_start_line // null),
      side: (.side // \"RIGHT\"),
      subject_type: (.subject_type // \"line\"),
      body: (.body // \"\"),
      body_norm: ((.body // \"\") | $normalize_body_jq),
      diff_hunk: (.diff_hunk // \"\"),
      in_reply_to_id: (.in_reply_to_id // null)
    })
    | map(
        . + {
          fingerprint: (
            [
              .path,
              (.line | tostring),
              (.start_line | tostring),
              .side,
              .subject_type,
              .body_norm,
              (.diff_hunk | .[0:200])
            ] | join(\"|\")
          )
        }
      )
  "
}

top_level_actionable_comments() {
  jq '
    map(select(.in_reply_to_id == null))
    | map(select(.body_norm != ""))
  '
}

load_seen() {
  cat "$STATE_FILE"
}

save_seen() {
  local merged="$1"
  printf '%s\n' "$merged" > "$STATE_FILE"
}

new_comments_only() {
  local comments_json="$1"
  jq -n \
    --argjson comments "$comments_json" \
    --argjson seen "$(load_seen)" '
      $comments
      | map(select(.fingerprint as $fp | $seen | index($fp) | not))
    '
}

mark_seen() {
  local comments_json="$1"
  local merged
  merged="$(jq -n \
    --argjson seen "$(load_seen)" \
    --argjson comments "$comments_json" '
      ($seen + ($comments | map(.fingerprint))) | unique
    ')"
  save_seen "$merged"
}

request_copilot_review() {
  log "Requesting Copilot review on PR #$PR_NUMBER"
  gh pr edit "$PR_NUMBER" --add-reviewer @copilot >/dev/null
}

copilot_comment_count() {
  local raw
  raw="$(fetch_copilot_comments)"
  jq 'length' <<<"$raw"
}

ensure_initial_review_requested_if_needed() {
  local count
  count="$(copilot_comment_count)"
  if [[ "$count" -eq 0 && "$REQUEST_INITIAL_REVIEW_IF_NONE" == "1" ]]; then
    log "No Copilot comments found yet; requesting initial Copilot review."
    request_copilot_review
    log "Sleeping ${SLEEP_AFTER_REREQUEST}s to allow Copilot review to appear."
    sleep "$SLEEP_AFTER_REREQUEST"
  fi
}

build_prompt() {
  local comments_json="$1"
  cat > "$PROMPT_FILE" <<EOF
You are fixing GitHub Copilot pull request review feedback in the current repository.

Rules:
- Only address the review items listed below.
- Make the smallest correct changes.
- Preserve existing behavior unless the review feedback requires a change.
- After changes, run the project's relevant tests/lint only if straightforward.
- Do not create or switch branches.
- Do not open editors or ask interactive questions.
- When done, stop.

Pull request: #$PR_NUMBER
Repository: $REPO_FULL

GitHub Copilot review items to fix:
EOF

  jq -r '
    .[]
    | "- File: \(.path)\n  Line: \(.line // .start_line // "n/a")\n  Comment: \(.body)\n"
  ' <<<"$comments_json" >> "$PROMPT_FILE"

  cat >> "$PROMPT_FILE" <<'EOF'

Implementation notes:
- If a review comment appears stale because the code already changed, inspect the intent and fix only if still relevant.
- Prefer minimal diffs.
- Keep formatting/style consistent with the repository.
EOF
}

run_freebuff() {
  local session="freebuff-loop-$$"
  local poll_interval=3
  # ceil division so we wait at least FREEBUFF_IDLE_TIMEOUT seconds.
  local needed_stable=$(( (FREEBUFF_IDLE_TIMEOUT + poll_interval - 1) / poll_interval ))
  [[ "$needed_stable" -lt 1 ]] && needed_stable=1
  local signal_name="freebuff-done-$$"

  log "Starting Freebuff in tmux session '$session'."

  # Start freebuff in a detached tmux session with a real terminal.
  # The wait-for signal fires when the shell command exits, giving us
  # a reliable way to detect natural termination AND capture exit code.
  tmux new-session -d -s "$session" -x 220 -y 50 \
    "cd '$(pwd)' && '$FREEBUFF_CMD'; _fb_rc=\$?; tmux wait-for -S '$signal_name'; exit \$_fb_rc"
  _TMUX_SESSION="$session"

  # Give the TUI time to initialise.
  sleep 5

  if ! tmux has-session -t "$session" 2>/dev/null; then
    _TMUX_SESSION=""
    fail "Freebuff exited during startup. Is it authenticated? ($FREEBUFF_CMD login)"
  fi

  # Send the prompt via tmux paste-buffer (handles multi-line correctly).
  tmux load-buffer -b fb-prompt "$PROMPT_FILE"
  tmux paste-buffer -b fb-prompt -t "$session"
  sleep 1
  tmux send-keys -t "$session" Enter

  log "Prompt sent. Waiting for Freebuff to finish (idle=${FREEBUFF_IDLE_TIMEOUT}s, max=${FREEBUFF_TIMEOUT}s)..."

  # Poll the pane: when output stops changing for FREEBUFF_IDLE_TIMEOUT
  # seconds we assume the agent finished processing and is idle.
  local elapsed=0
  local prev_output=""
  local stable=0
  local exited_naturally=0

  while [[ "$elapsed" -lt "$FREEBUFF_TIMEOUT" ]]; do
    sleep "$poll_interval"
    elapsed=$(( elapsed + poll_interval ))

    # If the session died, freebuff exited on its own.
    if ! tmux has-session -t "$session" 2>/dev/null; then
      exited_naturally=1
      log "Freebuff exited on its own after ~${elapsed}s."
      break
    fi

    local cur_output
    cur_output=$(tmux capture-pane -t "$session" -p 2>/dev/null || true)

    if [[ "$cur_output" == "$prev_output" ]]; then
      stable=$(( stable + 1 ))
      if [[ "$stable" -ge "$needed_stable" ]]; then
        log "Freebuff output stable for ~${FREEBUFF_IDLE_TIMEOUT}s — done."
        break
      fi
    else
      stable=0
    fi
    prev_output="$cur_output"
  done

  if [[ "$elapsed" -ge "$FREEBUFF_TIMEOUT" && "$exited_naturally" -eq 0 ]]; then
    log "WARNING: Freebuff timed out after ${FREEBUFF_TIMEOUT}s."
  fi

  # Capture the full scrollback for the log.
  tmux capture-pane -t "$session" -p -S -500 >> "$LOG_FILE" 2>/dev/null || true

  if [[ "$exited_naturally" -eq 0 ]]; then
    # Send Ctrl+C twice (some TUIs require a second press to confirm exit).
    tmux send-keys -t "$session" C-c 2>/dev/null || true
    sleep 1
    tmux send-keys -t "$session" C-c 2>/dev/null || true
    sleep 1
  fi

  # If the session is still alive, force-kill it.
  tmux kill-session -t "$session" 2>/dev/null || true
  _TMUX_SESSION=""

  # Check exit code via the wait-for signal (if the session exited naturally).
  if [[ "$exited_naturally" -eq 1 ]]; then
    if tmux wait-for -t 5 "$signal_name" 2>/dev/null; then
      log "Freebuff exited cleanly."
    else
      log "WARNING: Could not retrieve Freebuff exit code (wait-for timed out)."
    fi
  fi

  log "Freebuff session ended."
}

run_tests_if_configured() {
  if [[ -n "$TEST_CMD" ]]; then
    log "Running TEST_CMD: $TEST_CMD"
    bash -lc "$TEST_CMD"
  else
    log "TEST_CMD not set; skipping tests."
  fi
}

working_tree_has_changes() {
  ! git diff --quiet || ! git diff --cached --quiet
}

commit_and_push_if_needed() {
  if working_tree_has_changes; then
    if [[ "$AUTO_COMMIT" == "1" ]]; then
      log "Committing and pushing changes."
      git add -A
      git commit -m "Address GitHub Copilot review feedback"
      git push
    else
      fail "Changes detected but AUTO_COMMIT=0. Commit/push manually, then rerun."
    fi
  else
    log "No code changes detected after Freebuff."
  fi
}

collect_new_actionable_comments() {
  local comments
  comments="$(fetch_copilot_comments | comment_fingerprints | top_level_actionable_comments)"
  printf '%s\n' "$comments" > "$LAST_FETCH_FILE"
  new_comments_only "$comments"
}

print_round_summary() {
  local comments_json="$1"
  jq -r '
    if length == 0 then
      "No new actionable Copilot comments."
    else
      .[] | "* " + .path + ":" + ((.line // .start_line // "n/a")|tostring) + " — " + (.body | gsub("\\s+";" "))
    end
  ' <<<"$comments_json"
}

preflight_checks() {
  if ! git diff --quiet || ! git diff --cached --quiet; then
    fail "Working tree is not clean. Commit, stash, or discard changes before running."
  fi
}

main() {
  preflight_checks
  ensure_initial_review_requested_if_needed

  for round in $(seq 1 "$MAX_ROUNDS"); do
    log "========== ROUND $round/$MAX_ROUNDS =========="
    local_new="$(collect_new_actionable_comments)"
    new_count="$(jq 'length' <<<"$local_new")"

    log "New actionable Copilot comments this round: $new_count"
    print_round_summary "$local_new" | tee -a "$LOG_FILE"

    if [[ "$new_count" -eq 0 ]]; then
      log "No new comments remain. Exiting successfully."
      exit 0
    fi

    mark_seen "$local_new"
    build_prompt "$local_new"
    run_freebuff
    run_tests_if_configured
    commit_and_push_if_needed
    request_copilot_review

    log "Sleeping ${SLEEP_AFTER_REREQUEST}s before next fetch."
    sleep "$SLEEP_AFTER_REREQUEST"
  done

  fail "Reached MAX_ROUNDS=$MAX_ROUNDS before clearing all new Copilot comments."
}

main "$@"
