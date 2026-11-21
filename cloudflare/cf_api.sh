#!/usr/bin/env bash
# Worker の管理エンドポイントを呼ぶ小さなヘルパー（トークンは表示しない）
# 使い方: cf_api.sh METHOD PATH [curl の追加引数...]
set -euo pipefail
WORKER_URL="${FX_CF_WORKER_URL:-https://fx-alert.sakage21.workers.dev}"
TOKEN_FILE="${FX_CF_TOKEN_FILE:-$HOME/.config/fx-alert-cf/admin_token}"
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "管理トークンのファイルがありません: $TOKEN_FILE" >&2
  exit 1
fi
method="$1"; path="$2"; shift 2
# トークンはコマンドライン引数に出さず、curl の設定として標準入力から渡す
printf 'header = "Authorization: Bearer %s"\n' "$(cat "$TOKEN_FILE")" | \
  curl -sS --fail-with-body --max-time 40 -K - -X "$method" "$WORKER_URL$path" "$@"
