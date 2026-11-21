#!/usr/bin/env bash
# After editing levels.json, pull Actions commits, validate JSON, commit, and push.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f levels.json ]]; then
  echo "levels.json がありません" >&2
  exit 1
fi

python3 - <<'ENDJSONCHECK'
import json
import sys
from pathlib import Path
path = Path("levels.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"levels.json のJSONが壊れています: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict) or not isinstance(data.get("levels"), list):
    print("levels.json には levels 配列が必要です", file=sys.stderr)
    sys.exit(1)
for i, level in enumerate(data["levels"]):
    if not isinstance(level, dict):
        print(f"levels[{i}] がオブジェクトではありません", file=sys.stderr)
        sys.exit(1)
    if "price" not in level or "direction" not in level:
        print(f"levels[{i}] に price または direction がありません", file=sys.stderr)
        sys.exit(1)
    direction = str(level.get("direction") or "").strip().lower()
    if direction not in {"above", "below"}:
        print(f"levels[{i}] の direction は above か below にしてください", file=sys.stderr)
        sys.exit(1)
    try:
        float(level["price"])
    except Exception:
        print(f"levels[{i}] の price が数値ではありません", file=sys.stderr)
        sys.exit(1)
print(f"levels.json OK（{len(data['levels'])}件）")
ENDJSONCHECK

git fetch origin
if ! git pull --rebase --autostash origin main; then
  echo "git pull --rebase に失敗しました。衝突を解消してからやり直してください。" >&2
  exit 1
fi

# Cloudflare Worker 版へも同じ levels.json を書き込む（失敗したら終了コード1）
sync_cloudflare() {
  local cf_sync="/workspace/fx-alert-cf/sync_levels.sh"
  if [[ "${FX_SKIP_CF_SYNC:-}" == "1" ]]; then
    echo "FX_SKIP_CF_SYNC=1 のため Worker への同期を省略しました" >&2
    return 0
  fi
  if [[ ! -x "$cf_sync" ]]; then
    echo "Worker 同期失敗: $cf_sync がありません" >&2
    exit 1
  fi
  if ! "$cf_sync" "$PWD/levels.json"; then
    echo "!!! GitHub へのプッシュは済みましたが、Cloudflare Worker への同期に失敗しました。/workspace/fx-alert-cf/sync_levels.sh を再実行してください。" >&2
    exit 1
  fi
}

if git diff --quiet -- levels.json && git diff --cached --quiet -- levels.json; then
  echo "levels.json に変更はありません"
  sync_cloudflare
  exit 0
fi

git add levels.json
git commit -m "Update levels.json"
git push origin main
echo "levels.json をプッシュしました"
sync_cloudflare
