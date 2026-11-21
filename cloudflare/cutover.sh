#!/usr/bin/env bash
# GitHub Actions 版 → Cloudflare Worker 版への切り替え手順（手で確認しながら実行する。自動では実行しない）。
#
# 前提の確認:
#   1. /workspace/fx-alert-cf/cf_status.sh で「最後の実行」が1分以内に更新され続けている
#   2. 平日の取引時間に、dry_run_log の「送るはずだった通知」が GitHub 版の実通知と同じ内容になっている
#   3. 切り替えの直前に push_levels.sh（または sync_levels.sh）で levels を揃えておく
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")"

read -r -p "本当に切り替えますか？（GitHub版を止め、Worker版を本番送信にします）[yes/NO] " ans
[[ "$ans" == "yes" ]] || { echo "中止しました"; exit 1; }

# 1) GitHub の levels.json を Worker に最終同期
./sync_levels.sh /workspace/fx-alert-gh/levels.json

# 2) GitHub Actions のワークフローを無効化（進行中のジョブは最大55分で終わる。急ぐなら下の cancel も）
gh workflow disable watch.yml --repo sakage21/fx-alert
# 進行中のジョブも止める場合:
# gh run list --repo sakage21/fx-alert --workflow watch.yml --status in_progress --json databaseId -q '.[].databaseId' \
#   | xargs -r -n1 gh run cancel --repo sakage21/fx-alert

# 3) Worker の DRY_RUN を無効にして再デプロイ（wrangler.jsonc の vars を書き換える）
sed -i 's/"DRY_RUN": "1"/"DRY_RUN": "0"/' wrangler.jsonc
grep -q '"DRY_RUN": "0"' wrangler.jsonc || { echo "wrangler.jsonc の DRY_RUN を書き換えられませんでした" >&2; exit 1; }
wrangler deploy

# 4) 確認
./cf_status.sh | head -8
echo "切り替え完了。以後、点検は levels.json の編集前に ./cf_pull_levels.sh --write で Worker 側の levels（発火済みが消えたもの）を取り込むこと。"
echo "元に戻す: wrangler.jsonc の DRY_RUN を \"1\" に戻して wrangler deploy、gh workflow enable watch.yml --repo sakage21/fx-alert"
