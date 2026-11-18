#!/usr/bin/env bash
# Show recent GitHub Actions runs and the current levels/state.
set -euo pipefail
cd "$(dirname "$0")"

echo "time: $(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S %Z')"
echo
echo "=== 最近のワークフロー実行 ==="
if command -v gh >/dev/null 2>&1; then
  gh run list --repo sakage21/fx-alert --workflow watch.yml --limit 8 || gh run list --repo sakage21/fx-alert --limit 8
else
  echo "gh がありません"
fi

echo
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "=== ローカルの追従状況 ==="
  git fetch origin --quiet 2>/dev/null || true
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  local_sha="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  remote_sha="$(git rev-parse --short origin/main 2>/dev/null || echo '?')"
  echo "branch: $branch  local: $local_sha  origin/main: $remote_sha"
  dirty="$(git status --porcelain)"
  if [[ -n "$dirty" ]]; then
    echo "未コミットの変更があります:"
    echo "$dirty"
  fi
fi

echo
echo "=== levels.json ==="
if [[ -f levels.json ]]; then
  cat levels.json
else
  echo "(なし)"
fi

echo
echo "=== state.json ==="
if [[ -f state.json ]]; then
  cat state.json
else
  echo "(なし)"
fi
