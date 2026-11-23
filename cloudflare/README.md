# FXデモ ドル円価格監視（Cloudflare Workers 版）

GitHub Actions 版（リポジトリ直下の `watch.py`）と同じ判定を、Cloudflare Workers の Cron Trigger で**1分おき**に行います。GitHub の予定実行のような遅れがありません。指定した価格に到達したとき、または急変動のときだけ、ルーチン「FXデモ価格アラート」を同じ Webhook で起こします。

- Worker 名: `fx-alert`（アカウント: sakage21@gmail.com の無料プラン）
- URL: `https://fx-alert.sakage21.workers.dev`（`/` 以外は管理トークンが必要）
- 作業コピー: `/workspace/fx-alert-cf`（= リポジトリの `cloudflare/`）

## 動き方

- Cron `* * * * *` で `scheduled()` が走り、Durable Object `Watcher`（SQLite 版、1個だけ）の `tick()` を呼びます。
- レート: Yahoo Finance `JPY=X` の1分足（`query1` → `query2`、直近40分を期間指定で取得）。Workers からも取得できることを確認済み（2026-11-21）。予備は fxratesapi（1点のみ・足なし。このときは急変動を判定しません）。
- 監視価格: **前回確認以降の高値・安値**（最大20分、1分の重なりあり）で判定し、書いた価格の**3銭手前**で発火します（above: 高値 ≥ price−0.03、below: 安値 ≤ price+0.03）。
- 発火した監視価格は Worker 側の levels から削除し、二度と届きません。1回の確認で送るのは1件（監視価格が書かれた順 → 急変動）。残りは次の分に送ります。
- 急変動: 毎回 Yahoo の1分足から、直近15分（`spike.minutes`）の高値−安値が20pips（`spike.pips`）以上かを直接計算します。通知後は、その時の価格から10pips以上離れるまで再通知しません（直前の通知以降の足だけで判定）。
- 静かな時間帯（JST）: 毎日 05:00〜07:59、土曜 07:00〜月曜 07:00。このあいだはレートを取らず、確認時刻だけ進めます（明けてから時間帯内のヒゲで通知しないため）。
- レートが10分以上古いときは通知せず、確認時刻も進めません。
- Webhook が失敗したときは確認時刻を進めず、次の分にもう一度判定します（`state.last_error` に記録）。
- Webhook の中身は `watch.py` と同じ形（`type` が `level` / `spike` / `test`）。認証ヘッダーは `FX_ALERT_WEBHOOK_AUTH`（`Authorization: Bearer …` の行そのもの）を最初の `:` で分けて付けます。

## 保存先と無料枠

Durable Object（SQLite）に `levels` / `state`（最後の通知、急変動の通知時刻と価格、発火履歴50件、DRY_RUN の記録50件）/ `heartbeat`（最後の実行時刻・直近レート）を置きます。

- KV の無料枠は書き込みが1日1,000回なので、毎分の「最後の実行時刻」を残せません。SQLite 版 Durable Object の無料枠は1日10万行の書き込み・10万リクエストなので、毎分1行（1日約1,440行）の記録と、通知時・levels 更新時の書き込みで十分に収まります。
- Worker の起動は Cron で1日約1,440回（無料枠は1日10万リクエスト）。CPU 時間は1回1ms前後（上限10ms）。

## DRY_RUN

`wrangler.jsonc` の `vars.DRY_RUN` が `"1"` のあいだは、判定・levels の削除・履歴の記録まですべて行いますが、Webhook は送らず `state.dry_run_log` に「送るはずだった中身」を残します。2026-11-23 に `cutover.sh` で `"0"`（本番送信）に切り替え、GitHub 版は無効化しました。切り替えは `cutover.sh`（中身を読んでから手で実行）。

## ファイル

- `src/index.js` … Worker 本体（Cron と管理用エンドポイント）
- `wrangler.jsonc` … 設定（Cron、Durable Object、DRY_RUN）
- `cf_api.sh` … 管理エンドポイントを呼ぶ（トークンは `~/.config/fx-alert-cf/admin_token`、表示しない）
- `sync_levels.sh` … levels.json を Worker へ書き込む。失敗したら終了コード1。`push_levels.sh` が最後に呼ぶ
- `cf_status.sh` … Worker の最後の実行時刻・直近レート・levels・発火履歴・DRY_RUN の記録を表示（`status.sh` の最後にも出ます）
- `cf_pull_levels.sh` … Worker 側の今の levels を表示。`--write` で `levels.json` に上書き
- `cutover.sh` … GitHub 版を止めて本番送信に切り替える手順（自動では実行しない）

## 点検側の使い方（変わりません）

```bash
cd /workspace/fx-alert-gh
git pull --rebase origin main
# levels.json を編集
/workspace/fx-alert-gh/push_levels.sh   # GitHub へプッシュ → Worker へも同期（失敗したら止まる）
```

切り替え後は GitHub 版が発火済みのレベルを levels.json から消さなくなるので、編集前に `/workspace/fx-alert-cf/cf_pull_levels.sh --write` で Worker 側の levels を取り込んでください。

## 管理エンドポイント（すべて `Authorization: Bearer <管理トークン>` が必要）

- `GET /status` … heartbeat・levels・state
- `GET /levels` / `PUT /levels` … levels の取得・上書き（形式チェックつき）
- `POST /run` … 試験用の1回分の判定。静かな時間帯と古さを無視し、送信も保存もしない。本文に `{"at": "2026-11-20T22:00:00+09:00", "levels": {...}, "ignore_state": true}` を渡すと過去時刻で試せる
- `POST /test-webhook` … `type=test` を**本当に**送る（DRY_RUN に関係なく）。ルーチンは `/workspace/fx-watch/test_received.txt` に記録する
- `POST /import-state` … GitHub 版の state.json から急変動の状態と履歴を取り込む

```bash
/workspace/fx-alert-cf/cf_api.sh POST /run
/workspace/fx-alert-cf/cf_api.sh POST /test-webhook
```

## シークレット

`FX_ALERT_WEBHOOK_URL`、`FX_ALERT_WEBHOOK_AUTH`、`ADMIN_TOKEN` は `wrangler secret put` で Worker に保存しています（`printf %s "$VAR" | wrangler secret put NAME`）。値はコード・ログ・リポジトリに載せません。

## デプロイ

```bash
export PATH=$HOME/.local/bin:$PATH
cd /workspace/fx-alert-cf && wrangler deploy
```
