#!/usr/bin/env bash
# 価格監視の状態を表示する。2026-11-23 から本番は Cloudflare Worker 版（cloudflare/）。
# GitHub Actions 版（watch.yml）は無効化済みで、state.json はその時点で止まっている。
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Cloudflare Worker 版（本番） ==="
if [[ -x /workspace/fx-alert-cf/cf_status.sh ]]; then
  /workspace/fx-alert-cf/cf_status.sh || echo "!!! Worker の状態を取得できませんでした"
else
  echo "!!! cf_status.sh がありません"
fi

echo
echo "=== GitHub Actions 版（停止中） ==="
if command -v gh >/dev/null 2>&1; then
  gh workflow list --repo sakage21/fx-alert --all 2>/dev/null | grep -E '^watch' || true
else
  echo "gh がありません"
fi

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch origin --quiet 2>/dev/null || true
  echo "local: $(git rev-parse --short HEAD 2>/dev/null || echo '?')  origin/main: $(git rev-parse --short origin/main 2>/dev/null || echo '?')"
  dirty="$(git status --porcelain)"
  if [[ -n "$dirty" ]]; then
    echo "未コミットの変更があります:"
    echo "$dirty"
  fi
fi
echo "（levels.json は GitHub に記録として残すだけ。監視に使われるのは上の Worker の levels）"
