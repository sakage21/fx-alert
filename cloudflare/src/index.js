// FXデモ ドル円価格監視（Cloudflare Workers 版）
// watch.py（GitHub Actions 版）と同じ判定を、Cron Trigger で1分おきに行う。
// 状態は SQLite 版 Durable Object「Watcher」1個に保存する。
// 秘密の値（Webhook URL / 認証ヘッダー / 管理トークン）はログにも保存にも出さない。

import { DurableObject } from "cloudflare:workers";

const PIP = 0.01;
const LEVEL_MARGIN_PIPS = 3;        // 監視価格の3銭手前で発火
const STALE_SECONDS = 10 * 60;      // 10分より古いレートでは通知しない
const LOOKBACK_SECONDS = 20 * 60;   // 前回確認からの期間は最大20分
const BAR_OVERLAP_SECONDS = 60;     // 1分の重なり
const SPIKE_HYSTERESIS_PIPS = 10;
const WEEKEND_CUTOVER_HOUR = 7;
const BLACKOUT_START_HOUR = 5;      // 05:00〜07:59 JST
const BLACKOUT_END_HOUR = 8;
const HISTORY_MAX = 50;
const FETCH_TIMEOUT_MS = 10000;
const WEBHOOK_TIMEOUT_MS = 15000;
const USER_AGENT = "Mozilla/5.0 (compatible; fx-alert/1.0)";
const YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"];
const FXRATES_URL = "https://api.fxratesapi.com/latest?base=USD&currencies=JPY";

// ---------- time helpers (JST) ----------
function jstParts(ms) {
  const d = new Date(ms + 9 * 3600 * 1000);
  return { wd: d.getUTCDay(), hour: d.getUTCHours() }; // wd: 0=Sun .. 6=Sat
}
function toIso(ms) {
  const d = new Date(Math.floor(ms / 1000) * 1000 + 9 * 3600 * 1000);
  return d.toISOString().replace(/\.\d{3}Z$/, "+09:00");
}
function parseIso(s) {
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}
function isWeekend(ms) {
  const { wd, hour } = jstParts(ms);
  if (wd === 6 && hour >= WEEKEND_CUTOVER_HOUR) return true;
  if (wd === 0) return true;
  if (wd === 1 && hour < WEEKEND_CUTOVER_HOUR) return true;
  return false;
}
function isBlackout(ms) {
  const { hour } = jstParts(ms);
  return hour >= BLACKOUT_START_HOUR && hour < BLACKOUT_END_HOUR;
}
const r3 = (x) => Math.round(x * 1000) / 1000;

// ---------- quote sources ----------
async function getJson(url) {
  const resp = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!resp.ok) throw new Error(`HTTP${resp.status}`);
  return await resp.json();
}

async function fetchYahoo(host, nowMs) {
  // 直近40分ぶんの1分足だけを取る（range=1d と同じエンドポイント、軽くするため期間指定）
  const p2 = Math.floor(nowMs / 1000) + 60;
  const p1 = p2 - 40 * 60;
  const url = `https://${host}/v8/finance/chart/JPY=X?interval=1m&period1=${p1}&period2=${p2}`;
  const data = await getJson(url);
  const result = data?.chart?.result?.[0];
  if (!result) throw new Error("no-result");
  const meta = result.meta || {};
  const ts = result.timestamp || [];
  const q = result.indicators?.quote?.[0] || {};
  const bars = [];
  let lastClose = null, lastTs = null;
  for (let i = 0; i < ts.length; i++) {
    const c = q.close?.[i];
    if (ts[i] == null || c == null) continue;
    const h = q.high?.[i] ?? c, l = q.low?.[i] ?? c;
    const t = ts[i] * 1000;
    bars.push({ t, high: +h, low: +l, close: +c });
    lastClose = +c; lastTs = t;
  }
  let price, time;
  if (meta.regularMarketPrice != null && meta.regularMarketTime != null) {
    price = +meta.regularMarketPrice; time = meta.regularMarketTime * 1000;
  } else if (lastClose != null) {
    price = lastClose; time = lastTs;
  } else throw new Error("no-price");
  return { price, time, bars };
}

async function fetchFxrates() {
  const data = await getJson(FXRATES_URL);
  const price = data?.rates?.JPY, ts = data?.timestamp;
  if (price == null || ts == null) throw new Error("no-price");
  return { price: +price, time: ts * 1000, bars: [] }; // 1点のみ（足なし）
}

async function fetchQuote(nowMs) {
  const errors = [];
  const attempts = [
    ["yahoo", () => fetchYahoo(YAHOO_HOSTS[0], nowMs)],
    ["yahoo-q2", () => fetchYahoo(YAHOO_HOSTS[1], nowMs)],
    ["fxratesapi", () => fetchFxrates()],
  ];
  for (const [name, fn] of attempts) {
    try {
      const r = await fn();
      if (r && r.price > 0) return { ...r, source: name, errors };
      errors.push(`${name}:empty`);
    } catch (e) {
      errors.push(`${name}:${e?.message || e?.name || "error"}`);
    }
  }
  return { errors };
}

// ---------- judging ----------
function levelTouched(level, high, low) {
  const target = Number(level.price);
  if (!Number.isFinite(target)) return false;
  const dir = String(level.direction || "").trim().toLowerCase();
  const m = LEVEL_MARGIN_PIPS * PIP;
  if (dir === "above") return high >= target - m;
  if (dir === "below") return low <= target + m;
  return false;
}

function validateLevels(data) {
  if (!data || typeof data !== "object" || !Array.isArray(data.levels)) return "levels 配列が必要です";
  for (const [i, lv] of data.levels.entries()) {
    if (!lv || typeof lv !== "object") return `levels[${i}] がオブジェクトではありません`;
    if (!("price" in lv) || !("direction" in lv)) return `levels[${i}] に price または direction がありません`;
    if (!["above", "below"].includes(String(lv.direction).trim().toLowerCase())) return `levels[${i}] の direction が不正`;
    if (!Number.isFinite(Number(lv.price))) return `levels[${i}] の price が数値ではありません`;
  }
  return null;
}

function authHeaders(env) {
  const raw = env.FX_ALERT_WEBHOOK_AUTH;
  if (!raw || !raw.includes(":")) return null;
  const idx = raw.indexOf(":");
  const name = raw.slice(0, idx).trim();
  const value = raw.slice(idx + 1).replace(/^\s+/, "");
  if (!name || !value) return null;
  return { [name]: value, "Content-Type": "application/json" };
}

async function postWebhook(env, payload) {
  const url = env.FX_ALERT_WEBHOOK_URL;
  const headers = authHeaders(env);
  if (!url || !headers) return { status: null, ok: false, error: "missing-secret" };
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(WEBHOOK_TIMEOUT_MS),
    });
    return { status: resp.status, ok: resp.status >= 200 && resp.status < 300 };
  } catch (e) {
    return { status: null, ok: false, error: e?.name || "error" };
  }
}

const DEFAULT_STATE = () => ({
  last_fire_time: null,
  last_fire_type: null,
  last_spike_fire_time: null,
  last_spike_price: null,
  fired_history: [],
  dry_run_log: [],
  last_error: null,
});

// ---------- Durable Object ----------
export class Watcher extends DurableObject {
  async load() {
    const [levels, state, hb] = await Promise.all([
      this.ctx.storage.get("levels"),
      this.ctx.storage.get("state"),
      this.ctx.storage.get("heartbeat"),
    ]);
    return { levels: levels || null, state: { ...DEFAULT_STATE(), ...(state || {}) }, hb: hb || {} };
  }

  // test=true: 静かな時間帯・古いレートを無視し、送信も保存もしない（取得・判定の確認用）
  // test 時のみ at（過去の時刻）と levels / state の差し替えで判定を試せる
  async tick({ test = false, atMs = null, levelsOverride = null, ignoreState = false } = {}) {
    const env = this.env;
    const nowMs = (test && atMs) ? atMs : Date.now();
    const dryRun = test || String(env.DRY_RUN || "") === "1";
    const loaded = await this.load();
    let { levels, state, hb } = loaded;
    if (test && levelsOverride) levels = levelsOverride;
    if (test && ignoreState) { state = DEFAULT_STATE(); hb = {}; }
    const out = { time: toIso(nowMs), dry_run: dryRun, test };
    const saveHb = async (extra) => {
      if (test) return;
      // 1分に1行の書き込み（SQLite DO 無料枠は1日10万行）
      await this.ctx.storage.put("heartbeat", { ...hb, last_run: toIso(nowMs), ...extra });
    };

    const weekend = isWeekend(nowMs), blackout = isBlackout(nowMs);
    if (!test && (weekend || blackout)) {
      // 取得せずに確認時刻だけ進める（静かな時間帯に触れた価格を明けてから通知しないため）
      out.result = weekend ? "quiet:weekend" : "quiet:blackout";
      await saveHb({ last_checked_time: toIso(nowMs), last_result: out.result });
      return out;
    }

    const q = await fetchQuote(nowMs);
    out.errors = q.errors;
    if (!q.source) {
      out.result = "error:all-sources-failed";
      await saveHb({ last_result: out.result, last_source_errors: q.errors });
      return out;
    }
    if (test && atMs) {
      // 過去時刻での試験: その時刻までの足だけを使い、価格も最後の足の終値にする
      q.bars = q.bars.filter((b) => b.t <= nowMs);
      if (q.bars.length) { const lb = q.bars[q.bars.length - 1]; q.price = lb.close; q.time = lb.t; }
    }
    const age = Math.round((nowMs - q.time) / 1000);
    const lastChecked = parseIso(hb.last_checked_time);
    const cap = nowMs - LOOKBACK_SECONDS * 1000;
    const start = lastChecked == null ? cap : Math.max(cap, lastChecked - BAR_OVERLAP_SECONDS * 1000);
    let high = q.price, low = q.price, nbars = 0;
    for (const b of q.bars) {
      if (b.t < start) continue;
      high = Math.max(high, b.high); low = Math.min(low, b.low); nbars++;
    }
    const quote = { source: q.source, price: r3(q.price), time: toIso(q.time), age_sec: age, high: r3(high), low: r3(low), bars: nbars, total_bars: q.bars.length, lookback: toIso(start) };
    out.quote = quote;
    if (test) out.flags = { weekend, blackout, stale: age > STALE_SECONDS };

    if (!test && age > STALE_SECONDS) {
      out.result = "skip:stale"; // 確認時刻は進めない
      await saveHb({ last_quote: quote, last_result: out.result });
      return out;
    }

    // 候補: 監視価格（書かれた順）→ 急変動
    const candidates = [];
    const lvList = (levels && Array.isArray(levels.levels)) ? levels.levels : [];
    for (const lv of lvList) {
      if (lv && typeof lv === "object" && "price" in lv && "direction" in lv && levelTouched(lv, high, low)) {
        candidates.push(["level", lv]);
      }
    }
    const spikeCfg = (levels && levels.spike) || {};
    let spikePips = Number(spikeCfg.pips ?? 20), spikeMin = parseInt(spikeCfg.minutes ?? 15, 10);
    if (!Number.isFinite(spikePips) || !Number.isFinite(spikeMin)) { spikePips = 20; spikeMin = 15; }
    let cutoff = nowMs - spikeMin * 60 * 1000;
    const lastSpike = parseIso(state.last_spike_fire_time);
    if (lastSpike != null && lastSpike > cutoff) cutoff = lastSpike; // 同じ振れで再発火しない
    let sh = -Infinity, sl = Infinity;
    for (const b of q.bars) { if (b.t >= cutoff) { sh = Math.max(sh, b.high); sl = Math.min(sl, b.low); } }
    if (Number.isFinite(sh)) {
      const move = (sh - sl) / PIP;
      out.spike_window = { high: r3(sh), low: r3(sl), move_pips: Math.round(move * 10) / 10, minutes: spikeMin };
      if (move >= spikePips) {
        const lp = state.last_spike_price;
        if (typeof lp === "number" && Math.abs(q.price - lp) + 1e-12 < SPIKE_HYSTERESIS_PIPS * PIP) {
          out.spike_blocked = `hysteresis: ${(Math.abs(q.price - lp) / PIP).toFixed(1)}pips from ${lp}`;
        } else {
          candidates.push(["spike", { high: sh, low: sl, move_pips: move }]);
        }
      }
    }

    if (!candidates.length) {
      out.result = "no-fire";
      await saveHb({ last_checked_time: toIso(q.time), last_quote: quote, last_result: out.result });
      return out;
    }

    // 1回の確認で送るのは1件（watch.py と同じ。残りは次の分に送る）
    const [typ, obj] = candidates[0];
    let payload;
    if (typ === "level") {
      payload = { type: "level", level: obj, price: r3(q.price), source: q.source, time: toIso(nowMs) };
    } else {
      payload = { type: "spike", price: r3(q.price), move_pips: Math.round(obj.move_pips * 10) / 10, high: r3(obj.high), low: r3(obj.low), minutes: spikeMin, source: q.source, time: toIso(nowMs) };
    }
    out.would_send = payload;
    if (test) { out.result = `test:would-fire:${typ}`; return out; }

    let sent;
    if (dryRun) {
      sent = { status: "dry-run", ok: true };
      state.dry_run_log = [...(state.dry_run_log || []), { time: toIso(nowMs), payload }].slice(-HISTORY_MAX);
    } else {
      sent = await postWebhook(env, payload);
    }
    out.webhook = { status: sent.status, ok: sent.ok };

    if (!sent.ok) {
      // 失敗時は確認時刻を進めず、次の分にもう一度判定する
      state.last_error = { time: toIso(nowMs), type: typ, status: sent.status, error: sent.error || null };
      await this.ctx.storage.put("state", state);
      out.result = `error:webhook:${typ}`;
      await saveHb({ last_quote: quote, last_result: out.result });
      return out;
    }

    const nowIso = toIso(nowMs);
    state.last_fire_time = nowIso;
    state.last_fire_type = typ;
    let detail;
    if (typ === "spike") {
      state.last_spike_fire_time = nowIso;
      state.last_spike_price = r3(q.price);
      detail = { move_pips: Math.round(obj.move_pips * 10) / 10, high: obj.high, low: obj.low, price: r3(q.price) };
    } else {
      detail = { id: obj.id ?? null, price: obj.price };
      if (obj.id != null && levels) {
        levels.levels = levels.levels.filter((lv) => String(lv.id) !== String(obj.id));
        levels.updated = nowIso;
        await this.ctx.storage.put("levels", levels);
      }
    }
    state.fired_history = [...(state.fired_history || []), { time: nowIso, type: typ, ...(dryRun ? { dry_run: true } : {}), ...detail }].slice(-HISTORY_MAX);
    await this.ctx.storage.put("state", state);
    out.result = `fired:${typ}${dryRun ? ":dry-run" : ""}`;
    await saveHb({ last_checked_time: toIso(q.time), last_quote: quote, last_result: out.result });
    return out;
  }

  async putLevels(data) {
    const err = validateLevels(data);
    if (err) return { ok: false, error: err };
    const { state } = await this.load();
    await this.ctx.storage.put("levels", data);
    const firedIds = new Set((state.fired_history || []).filter((h) => h.type === "level" && h.id != null).map((h) => String(h.id)));
    const already = data.levels.filter((lv) => lv.id != null && firedIds.has(String(lv.id))).map((lv) => lv.id);
    return { ok: true, count: data.levels.length, already_fired_ids: already };
  }

  async importState(src) {
    const { state } = await this.load();
    for (const k of ["last_fire_time", "last_fire_type", "last_spike_fire_time", "last_spike_price"]) {
      if (src && k in src) state[k] = src[k];
    }
    if (src && Array.isArray(src.fired_history)) state.fired_history = src.fired_history.slice(-HISTORY_MAX);
    await this.ctx.storage.put("state", state);
    return { ok: true };
  }

  async status() {
    const { levels, state, hb } = await this.load();
    return { now: toIso(Date.now()), dry_run: String(this.env.DRY_RUN || "") === "1", heartbeat: hb, levels, state };
  }

  async sendTest() {
    const r = await postWebhook(this.env, { type: "test", note: "動作テスト（Cloudflare Worker）" });
    return { status: r.status, ok: r.ok, error: r.error || null };
  }
}

// ---------- entry points ----------
function stub(env) {
  return env.WATCHER.get(env.WATCHER.idFromName("main"));
}

function timingSafeEq(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj, null, 2), { status, headers: { "content-type": "application/json; charset=utf-8" } });

export default {
  async scheduled(controller, env, ctx) {
    const out = await stub(env).tick({ test: false });
    // ログには価格と結果だけ（秘密は出さない）
    console.log(JSON.stringify({ result: out.result, quote: out.quote, errors: out.errors }));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/") return new Response("fx-alert worker\n");
    const auth = request.headers.get("Authorization") || "";
    const token = env.ADMIN_TOKEN;
    if (!token || !timingSafeEq(auth, `Bearer ${token}`)) return json({ error: "unauthorized" }, 401);

    const s = stub(env);
    const m = request.method;
    if (url.pathname === "/status" && m === "GET") return json(await s.status());
    if (url.pathname === "/levels" && m === "GET") return json((await s.status()).levels);
    if (url.pathname === "/levels" && m === "PUT") {
      let data;
      try { data = await request.json(); } catch { return json({ ok: false, error: "JSONが壊れています" }, 400); }
      const r = await s.putLevels(data);
      return json(r, r.ok ? 200 : 400);
    }
    if (url.pathname === "/import-state" && m === "POST") return json(await s.importState(await request.json()));
    if (url.pathname === "/run" && m === "POST") {
      // 本文（任意）: {"at": "2026-11-20T22:00:00+09:00", "levels": {...}, "ignore_state": true}
      let body = {};
      try { const t = await request.text(); if (t) body = JSON.parse(t); } catch { return json({ error: "JSONが壊れています" }, 400); }
      const atMs = body.at ? parseIso(body.at) : null;
      return json(await s.tick({ test: true, atMs, levelsOverride: body.levels || null, ignoreState: !!body.ignore_state }));
    }
    if (url.pathname === "/test-webhook" && m === "POST") {
      const r = await s.sendTest();
      return json(r, r.ok ? 200 : 502);
    }
    return json({ error: "not found" }, 404);
  },
};
