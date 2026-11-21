#!/usr/bin/env bash
# Worker 側の今の levels（発火して消えた分を反映済み）を表示する。
# --write を付けると /workspace/fx-alert-gh/levels.json に上書きする（切り替え後に点検が編集を始める前に使う）。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
data="$("$here/cf_api.sh" GET /levels)"
if [[ "${1:-}" == "--write" ]]; then
  python3 - "$data" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
if not isinstance(d, dict) or not isinstance(d.get("levels"), list):
    print("Worker に levels がありません", file=sys.stderr); sys.exit(1)
open("/workspace/fx-alert-gh/levels.json", "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print(f"levels.json を Worker の内容（{len(d['levels'])}件）で上書きしました")
PY
else
  printf '%s\n' "$data"
fi
