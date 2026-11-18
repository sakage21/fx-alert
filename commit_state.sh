#!/usr/bin/env bash
# Commit levels.json / state.json if the watcher changed them, then push.
# Retries rebase+push so a routine's push_levels.sh can land first.
set -euo pipefail
cd "$(dirname "$0")"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

changed=0
if ! git diff --quiet -- state.json 2>/dev/null; then
  git add state.json
  changed=1
fi
if ! git diff --quiet -- levels.json 2>/dev/null; then
  git add levels.json
  changed=1
fi

if [[ "$changed" -eq 0 ]]; then
  echo "no changes to commit"
  exit 0
fi

git commit -m "chore: update watcher state"

for i in 1 2 3 4 5; do
  git fetch origin
  if ! git rebase origin/main; then
    echo "rebase failed (attempt $i)"
    git rebase --abort || true
    sleep 3
    continue
  fi
  if git push origin HEAD:main; then
    echo "pushed state/levels"
    exit 0
  fi
  echo "push failed (attempt $i)"
  sleep 3
done

echo "failed to push after retries" >&2
exit 1
