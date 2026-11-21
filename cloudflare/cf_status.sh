#!/usr/bin/env bash
# Cloudflare Worker 版の状態（最後の確認時刻・直近レート・levels・発火履歴・DRY_RUN の記録）を表示する。
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
echo "time: $(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S %Z')"
data="$("$here/cf_api.sh" GET /status)" || { echo "Worker の状態を取得できませんでした" >&2; exit 1; }
python3 - "$data" <<'PY'
import json, sys
from datetime import datetime, timezone, timedelta
d = json.loads(sys.argv[1])
hb = d.get("heartbeat") or {}
print(f"DRY_RUN: {'有効（Webhookは送らず記録だけ）' if d.get('dry_run') else '無効（本番送信）'}")
lr = hb.get("last_run")
if lr:
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(lr)).total_seconds()
    mark = "" if age < 180 else "  ← 3分以上動いていません"
    print(f"最後の実行: {lr}（{int(age)}秒前）{mark}")
else:
    print("最後の実行: まだ記録がありません")
print(f"最後の結果: {hb.get('last_result')}")
print(f"確認済みの時刻: {hb.get('last_checked_time')}")
q = hb.get("last_quote")
if q:
    print(f"直近レート: {q.get('price')} ({q.get('source')}, {q.get('time')}, 期間高値 {q.get('high')} / 安値 {q.get('low')})")
if hb.get("last_source_errors"):
    print(f"取得エラー: {hb['last_source_errors']}")
print("\n=== levels（Worker） ===")
print(json.dumps(d.get("levels"), ensure_ascii=False, indent=2))
st = d.get("state") or {}
print("\n=== state（Worker） ===")
for k in ("last_fire_time", "last_fire_type", "last_spike_fire_time", "last_spike_price", "last_error"):
    print(f"{k}: {st.get(k)}")
print("fired_history（新しい5件）:")
for h in (st.get("fired_history") or [])[-5:]:
    print("  " + json.dumps(h, ensure_ascii=False))
dr = st.get("dry_run_log") or []
if dr:
    print(f"dry_run_log（送るはずだった通知・新しい5件／全{len(dr)}件）:")
    for h in dr[-5:]:
        print("  " + json.dumps(h, ensure_ascii=False))
PY
