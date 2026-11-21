#!/usr/bin/env bash
# levels.json を Worker（Durable Object）へ書き込む。失敗したら終了コード1で止まる。
# 使い方: sync_levels.sh [levels.json のパス]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
file="${1:-/workspace/fx-alert-gh/levels.json}"
if [[ ! -f "$file" ]]; then
  echo "Worker 同期失敗: $file がありません" >&2
  exit 1
fi
if ! resp="$("$here/cf_api.sh" PUT /levels -H 'content-type: application/json' --data-binary "@$file" 2>&1)"; then
  echo "Worker 同期失敗（Cloudflare へ levels.json を書き込めませんでした）:" >&2
  echo "$resp" >&2
  exit 1
fi
python3 - "$resp" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
if not r.get("ok"):
    print(f"Worker 同期失敗: {r.get('error')}", file=sys.stderr); sys.exit(1)
print(f"Worker へ levels.json を同期しました（{r.get('count')}件）")
if r.get("already_fired_ids"):
    print("注意: Worker で発火済みの id がまた入っています: " + ", ".join(map(str, r["already_fired_ids"])) + "（意図どおりなら問題ありません）")
PY
