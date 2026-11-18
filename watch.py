#!/usr/bin/env python3
"""USD/JPY price watcher for GitHub Actions.

Runs once per invocation (scheduled about every 5 minutes). Checks configured
levels against the high/low of 1-minute bars since the last check, then POSTs
a webhook only when a level is reached or a spike occurs.

Secrets are read from the environment at runtime and must never be logged,
printed, or written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
LEVELS_PATH = ROOT / "levels.json"
STATE_PATH = ROOT / "state.json"

JST = timezone(timedelta(hours=9))
STALE_SECONDS = 10 * 60
COOLDOWN_SECONDS = 60 * 60
LOOKBACK_SECONDS = 20 * 60
BAR_OVERLAP_SECONDS = 60
PIP = 0.01
WEEKEND_CUTOVER_HOUR = 7
REQUEST_TIMEOUT = 12
WEBHOOK_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; fx-alert/1.0)"
YAHOO_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/JPY=X?interval=1m&range=1d",
    "https://query2.finance.yahoo.com/v8/finance/chart/JPY=X?interval=1m&range=1d",
)
FXRATES_URL = "https://api.fxratesapi.com/latest?base=USD&currencies=JPY"


def now_jst() -> datetime:
    return datetime.now(JST).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt.astimezone(JST)
    except Exception:
        return None


def log(message: str) -> None:
    line = f"{to_iso(now_jst())} {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log(f"failed to read {path.name}: {type(exc).__name__}")
        return default


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def is_weekend(dt: datetime) -> bool:
    """FX weekend: Saturday 07:00 JST through Monday 07:00 JST."""
    local = dt.astimezone(JST)
    wd = local.weekday()
    hour = local.hour
    if wd == 5 and hour >= WEEKEND_CUTOVER_HOUR:
        return True
    if wd == 6:
        return True
    if wd == 0 and hour < WEEKEND_CUTOVER_HOUR:
        return True
    return False


def session_headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def http_get_json(url: str, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=session_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("json root is not an object")
    return data


def _parse_yahoo(data: dict[str, Any]) -> Optional[tuple[float, datetime, list[dict[str, Any]]]]:
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        return None
    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    indicators = (result.get("indicators") or {}).get("quote") or [{}]
    quote = indicators[0] or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    bars: list[dict[str, Any]] = []
    last_close = None
    last_ts = None
    for row in zip(timestamps, opens, highs, lows, closes):
        t, o, h, l, c = row
        if t is None or c is None:
            continue
        close = float(c)
        high = float(h) if h is not None else close
        low = float(l) if l is not None else close
        opn = float(o) if o is not None else close
        ts = datetime.fromtimestamp(int(t), tz=JST)
        bars.append({"time": ts, "open": opn, "high": high, "low": low, "close": close})
        last_close, last_ts = close, ts
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is not None and ts is not None:
        price_f = float(price)
        ts_dt = datetime.fromtimestamp(int(ts), tz=JST)
        return price_f, ts_dt, bars
    if last_close is None or last_ts is None:
        return None
    return float(last_close), last_ts, bars


def fetch_yahoo(url: str, label: str) -> tuple[float, datetime, str, list[dict[str, Any]]]:
    parsed = _parse_yahoo(http_get_json(url))
    if not parsed:
        raise ValueError("yahoo payload missing price")
    price, ts, bars = parsed
    return price, ts, label, bars


def fetch_fxrates() -> tuple[float, datetime, str, list[dict[str, Any]]]:
    data = http_get_json(FXRATES_URL)
    price = (data.get("rates") or {}).get("JPY")
    ts = data.get("timestamp")
    if price is None or ts is None:
        raise ValueError("fxratesapi payload missing price")
    dt = datetime.fromtimestamp(int(ts), tz=JST)
    # Single tick only; no OHLC bars.
    return float(price), dt, "fxratesapi", []


def fetch_quote() -> Optional[tuple[float, datetime, str, list[dict[str, Any]]]]:
    errors: list[str] = []
    attempts = [
        ("yahoo", lambda: fetch_yahoo(YAHOO_URLS[0], "yahoo")),
        ("yahoo-q2", lambda: fetch_yahoo(YAHOO_URLS[1], "yahoo-q2")),
        ("fxratesapi", fetch_fxrates),
    ]
    for name, fn in attempts:
        try:
            result = fn()
            if result and result[0] > 0:
                return result
            errors.append(f"{name}:empty")
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    log("all quote sources failed: " + ",".join(errors))
    return None


def default_state() -> dict[str, Any]:
    return {
        "last_fire_time": None,
        "last_fire_type": None,
        "last_spike_fire_time": None,
        "last_checked_time": None,
        "samples": [],
        "fired_history": [],
    }


def load_state() -> dict[str, Any]:
    state = default_state()
    loaded = load_json(STATE_PATH, {})
    if isinstance(loaded, dict):
        state.update(loaded)
    if not isinstance(state.get("samples"), list):
        state["samples"] = []
    if not isinstance(state.get("fired_history"), list):
        state["fired_history"] = []
    return state


def save_state(state: dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def remember_sample(state: dict[str, Any], price: float, ts: datetime) -> None:
    cutoff = ts - timedelta(minutes=20)
    samples = []
    for item in state.get("samples") or []:
        if not isinstance(item, dict):
            continue
        dt = parse_iso(str(item.get("time") or ""))
        px = item.get("price")
        if dt is None or not isinstance(px, (int, float)):
            continue
        if dt >= cutoff:
            samples.append({"time": to_iso(dt), "price": float(px)})
    samples.append({"time": to_iso(ts), "price": float(price)})
    state["samples"] = samples[-40:]


def load_levels() -> Optional[dict[str, Any]]:
    if not LEVELS_PATH.exists():
        log("levels.json missing")
        return None
    try:
        data = json.loads(LEVELS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("levels"), list):
            raise ValueError("invalid schema")
        return data
    except Exception as exc:
        log(f"levels.json unreadable: {type(exc).__name__}")
        return None


def level_touched(level: dict[str, Any], period_high: float, period_low: float) -> bool:
    try:
        target = float(level["price"])
    except Exception:
        return False
    direction = str(level.get("direction") or "").strip().lower()
    if direction == "above":
        return period_high >= target
    if direction == "below":
        return period_low <= target
    return False


def remove_level(level_id: str) -> bool:
    data = load_levels()
    if not data:
        return False
    before = data.get("levels") or []
    after = [lv for lv in before if str(lv.get("id")) != str(level_id)]
    if len(after) == len(before):
        return False
    data["levels"] = after
    data["updated"] = to_iso(now_jst())
    write_json(LEVELS_PATH, data)
    return True


def cooldown_active(state: dict[str, Any], now: datetime) -> bool:
    last = parse_iso(str(state.get("last_fire_time") or ""))
    if last is None:
        return False
    return (now - last).total_seconds() < COOLDOWN_SECONDS


def cooldown_remaining(state: dict[str, Any], now: datetime) -> int:
    last = parse_iso(str(state.get("last_fire_time") or ""))
    if last is None:
        return 0
    left = COOLDOWN_SECONDS - (now - last).total_seconds()
    return max(0, int(left))


def auth_headers() -> Optional[dict[str, str]]:
    raw = os.environ.get("FX_ALERT_WEBHOOK_AUTH")
    if not raw:
        return None
    if ":" not in raw:
        log("webhook skipped: FX_ALERT_WEBHOOK_AUTH is not a header line")
        return None
    name, value = raw.split(":", 1)
    name = name.strip()
    value = value.lstrip()
    if not name or not value:
        log("webhook skipped: FX_ALERT_WEBHOOK_AUTH header empty after split")
        return None
    return {name: value, "Content-Type": "application/json"}


def post_webhook(payload: dict[str, Any]) -> tuple[Optional[int], bool]:
    """POST payload. Returns (status_code_or_None, success). Never logs URL or secrets."""
    url = os.environ.get("FX_ALERT_WEBHOOK_URL")
    headers = auth_headers()
    if not url or not headers:
        log("webhook skipped: missing FX_ALERT_WEBHOOK_URL or FX_ALERT_WEBHOOK_AUTH")
        return None, False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            return status, 200 <= int(status) < 300
    except urllib.error.HTTPError as exc:
        return int(exc.code), False
    except Exception as exc:
        log(f"webhook POST failed: {type(exc).__name__}")
        return None, False


def record_fire(state: dict[str, Any], fire_type: str, detail: dict[str, Any], now: datetime) -> None:
    state["last_fire_time"] = to_iso(now)
    state["last_fire_type"] = fire_type
    if fire_type == "spike":
        state["last_spike_fire_time"] = to_iso(now)
    history = state.get("fired_history") or []
    history.append({"time": to_iso(now), "type": fire_type, **detail})
    state["fired_history"] = history[-20:]


def window_start(state: dict[str, Any], now: datetime) -> datetime:
    cap = now - timedelta(seconds=LOOKBACK_SECONDS)
    last = parse_iso(str(state.get("last_checked_time") or ""))
    if last is None:
        return cap
    start = last - timedelta(seconds=BAR_OVERLAP_SECONDS)
    if start < cap:
        return cap
    return start


def period_extremes(
    price: float,
    bars: list[dict[str, Any]],
    start: datetime,
) -> tuple[float, float, int]:
    high = price
    low = price
    count = 0
    for bar in bars:
        ts = bar.get("time")
        if not isinstance(ts, datetime) or ts < start:
            continue
        high = max(high, float(bar["high"]))
        low = min(low, float(bar["low"]))
        count += 1
    return high, low, count


def spike_move(
    state: dict[str, Any],
    now: datetime,
    window_minutes: int,
    bars: list[dict[str, Any]],
    price: float,
    quote_ts: datetime,
) -> Optional[dict[str, Any]]:
    cutoff = now - timedelta(minutes=window_minutes)
    last_spike = parse_iso(str(state.get("last_spike_fire_time") or ""))
    # After a spike alert, only consider data after that fire so the same swing
    # does not re-arm once the cooldown ends.
    if last_spike is not None and last_spike > cutoff:
        cutoff = last_spike
    highs: list[float] = []
    lows: list[float] = []
    if bars:
        for bar in bars:
            ts = bar.get("time")
            if not isinstance(ts, datetime) or ts < cutoff:
                continue
            highs.append(float(bar["high"]))
            lows.append(float(bar["low"]))
    else:
        for item in state.get("samples") or []:
            dt = parse_iso(str(item.get("time") or ""))
            px = item.get("price")
            if dt is None or not isinstance(px, (int, float)):
                continue
            if dt >= cutoff:
                highs.append(float(px))
                lows.append(float(px))
        if quote_ts >= cutoff:
            highs.append(float(price))
            lows.append(float(price))
    if not highs:
        return None
    # A single 1-minute bar still has a high/low; two sparse ticks need both.
    if not bars and len(highs) < 2:
        return None
    high = max(highs)
    low = min(lows)
    move_pips = (high - low) / PIP
    return {"high": high, "low": low, "move_pips": move_pips, "samples": len(highs)}


def wants_test(args: argparse.Namespace) -> bool:
    if args.test_webhook:
        return True
    value = os.environ.get("SEND_TEST", "").strip().lower()
    return value in {"1", "true", "yes"}


def send_test_webhook() -> int:
    payload = {"type": "test", "note": "動作テスト"}
    status, ok = post_webhook(payload)
    if status is None:
        log("test webhook failed: no status")
        return 1
    log(f"webhook type=test status={status} ok={ok}")
    return 0 if ok else 2


def handle_once() -> int:
    quote = fetch_quote()
    if not quote:
        return 1
    price, ts, source, bars = quote
    now = now_jst()
    age = (now - ts.astimezone(JST)).total_seconds()
    log(
        f"SOURCE={source} PRICE={price:.3f} TIME={to_iso(ts)} "
        f"AGE_SEC={int(round(age))} BARS={len(bars)}"
    )
    return 0


def handle_tick() -> int:
    quote = fetch_quote()
    if not quote:
        return 1
    price, ts, source, bars = quote
    now = now_jst()
    age = (now - ts.astimezone(JST)).total_seconds()
    stale = age > STALE_SECONDS
    weekend = is_weekend(now)
    start = window_start(load_state(), now)
    high, low, bar_count = period_extremes(price, bars, start)

    state = load_state()
    remember_sample(state, price, ts)

    log(
        f"quote source={source} price={price:.3f} time={to_iso(ts)} "
        f"age={int(round(age))}s high={high:.3f} low={low:.3f} "
        f"bars={bar_count} lookback={to_iso(start)}"
        f"{' stale' if stale else ''}{' weekend' if weekend else ''}"
    )

    if stale:
        log(f"skip fire: stale quote age={int(age)}s source={source} price={price:.3f}")
        # Do not advance last_checked_time, so the next run can still see this window.
        save_state(state)
        return 0
    if weekend:
        state["last_checked_time"] = to_iso(ts)
        save_state(state)
        return 0

    levels_data = load_levels()
    if not levels_data:
        state["last_checked_time"] = to_iso(ts)
        save_state(state)
        return 0

    candidates: list[tuple[str, dict[str, Any]]] = []
    for level in levels_data.get("levels") or []:
        if not isinstance(level, dict):
            continue
        if "price" not in level or "direction" not in level:
            continue
        if level_touched(level, high, low):
            candidates.append(("level", level))

    spike_cfg = levels_data.get("spike") or {}
    try:
        spike_pips = float(spike_cfg.get("pips", 20))
        spike_minutes = int(spike_cfg.get("minutes", 15))
    except Exception:
        spike_pips, spike_minutes = 20.0, 15
    spike = spike_move(state, now, spike_minutes, bars, price, ts)
    if spike and spike["move_pips"] >= spike_pips:
        candidates.append(("spike", spike))

    if not candidates:
        state["last_checked_time"] = to_iso(ts)
        save_state(state)
        return 0

    if cooldown_active(state, now):
        left = cooldown_remaining(state, now)
        kinds = []
        for typ, obj in candidates:
            if typ == "level":
                kinds.append(f"level:{obj.get('id')}")
            else:
                kinds.append("spike")
        log(f"cooldown {left}s, holding {','.join(kinds)} price={price:.3f}")
        state["last_checked_time"] = to_iso(ts)
        save_state(state)
        return 0

    typ, obj = candidates[0]
    if typ == "level":
        payload = {
            "type": "level",
            "level": obj,
            "price": round(price, 3),
            "source": source,
            "time": to_iso(now),
        }
        status, ok = post_webhook(payload)
        log(
            f"webhook type=level id={obj.get('id')} price={price:.3f} "
            f"status={status if status is not None else 'error'} ok={ok}"
        )
        if ok:
            if obj.get("id") is not None:
                if not remove_level(str(obj.get("id"))):
                    log(f"level fired but could not remove id={obj.get('id')}")
            record_fire(state, "level", {"id": obj.get("id"), "price": obj.get("price")}, now)
        state["last_checked_time"] = to_iso(ts)
        save_state(state)
        return 0 if ok else 2

    payload = {
        "type": "spike",
        "price": round(price, 3),
        "move_pips": round(obj["move_pips"], 1),
        "high": round(obj["high"], 3),
        "low": round(obj["low"], 3),
        "minutes": spike_minutes,
        "source": source,
        "time": to_iso(now),
    }
    status, ok = post_webhook(payload)
    log(
        f"webhook type=spike move={obj['move_pips']:.1f}pips price={price:.3f} "
        f"status={status if status is not None else 'error'} ok={ok}"
    )
    if ok:
        record_fire(
            state,
            "spike",
            {"move_pips": round(obj["move_pips"], 1), "high": obj["high"], "low": obj["low"]},
            now,
        )
    state["last_checked_time"] = to_iso(ts)
    save_state(state)
    return 0 if ok else 2


def main() -> int:
    os.environ.setdefault("TZ", "Asia/Tokyo")
    try:
        time.tzset()
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="USD/JPY price watcher (GitHub Actions)")
    parser.add_argument("--once", action="store_true", help="fetch one quote and exit")
    parser.add_argument("--test-webhook", action="store_true", help="send one test POST and exit")
    args = parser.parse_args()
    if args.once:
        return handle_once()

    missing = [
        name
        for name in ("FX_ALERT_WEBHOOK_URL", "FX_ALERT_WEBHOOK_AUTH")
        if not os.environ.get(name)
    ]
    if missing:
        log("missing env: " + ", ".join(missing))

    if wants_test(args):
        return send_test_webhook()

    try:
        return handle_tick()
    except Exception:
        log("tick error:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
