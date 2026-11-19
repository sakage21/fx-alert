#!/usr/bin/env bash
# Run the watcher repeatedly inside one Actions job.
# GitHub often delays schedule triggers to about once an hour, so a single
# short job every 5 minutes is not enough. This loop checks about every
# 5 minutes for ~55 minutes. Concurrency (cancel-in-progress: false) keeps
# jobs from overlapping; the next queued trigger starts when this job ends.
set -euo pipefail
cd "$(dirname "$0")"

LOOP_SECONDS="${LOOP_SECONDS:-3300}"  # 55 minutes
INTERVAL="${INTERVAL:-300}"           # 5 minutes

send_test_value="$(printf '%s' "${SEND_TEST:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "$send_test_value" == "1" || "$send_test_value" == "true" || "$send_test_value" == "yes" ]]; then
  echo "test webhook only; not looping"
  python3 watch.py
  exit $?
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

pull_latest() {
  # Re-read levels.json (and state) from origin so a mid-loop push_levels.sh
  # is picked up before the next check. Skip the pull if the working tree is
  # dirty, so an uncommitted fire is not overwritten.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree dirty; skip pull to preserve local state"
    return 0
  fi
  if ! git fetch origin main; then
    echo "fetch failed; continue with local files"
    return 0
  fi
  if ! git pull --rebase --autostash origin main; then
    echo "pull --rebase failed; continue with local files"
    git rebase --abort >/dev/null 2>&1 || true
  fi
}

START=$SECONDS
n=0
while true; do
  n=$((n + 1))
  echo "===== check ${n} at $(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S %Z') ====="
  pull_latest
  set +e
  python3 watch.py
  py_rc=$?
  set -e
  if [[ "$py_rc" -ne 0 ]]; then
    echo "watch.py exited ${py_rc}"
  fi
  set +e
  bash commit_state.sh
  commit_rc=$?
  set -e
  if [[ "$commit_rc" -ne 0 ]]; then
    echo "commit_state.sh exited ${commit_rc}"
  fi

  next=$((n * INTERVAL))
  if (( next >= LOOP_SECONDS )); then
    echo "loop finished after ${n} checks, $((SECONDS - START))s"
    break
  fi
  sleep_for=$((next - (SECONDS - START)))
  if (( sleep_for > 0 )); then
    echo "sleeping ${sleep_for}s until next check"
    sleep "$sleep_for"
  fi
done
