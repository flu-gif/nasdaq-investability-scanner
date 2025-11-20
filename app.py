import os
import time
import json
import math
import threading
import datetime as dt
from collections import defaultdict, deque
from typing import Dict, Deque, List, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for
from zoneinfo import ZoneInfo

# ================== ENV & CONSTANTS ==================
load_dotenv()

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
if not API_KEY or not API_SECRET:
    raise SystemExit("Missing Alpaca API keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY)")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DATA_BASE = "https://data.alpaca.markets/v2"
TRADING_BASE = "https://paper-api.alpaca.markets/v2"

UNIVERSE_SIZE = int(os.getenv("UNIVERSE_SIZE", "1000"))
SCORE_MIN = int(os.getenv("SCORE_MIN", "60"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "120"))
CACHE_SYMBOLS_HOURS = int(os.getenv("CACHE_SYMBOLS_HOURS", "24"))

# momentum weights: 1d > 4h > 1h
W_1D = float(os.getenv("WEIGHT_1D", "0.6"))
W_4H = float(os.getenv("WEIGHT_4H", "0.3"))
W_1H = float(os.getenv("WEIGHT_1H", "0.1"))

POLL_INTERVAL = 60  # seconds between full passes
BATCH_SIZE = 100

WINDOWS = {
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
THRESHOLDS = {
    "1h": 3.0,
    "4h": 5.0,
    "1d": 8.0,
}

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

CACHE_DIR = ".cache"
os.makedirs(CACHE_DIR, exist_ok=True)
SYMBOLS_CACHE = os.path.join(CACHE_DIR, "nasdaq_symbols.json")

ET = ZoneInfo("America/New_York")
OPEN_ET = dt.time(9, 30)
H1_ET = dt.time(10, 30)

ETF_BASKET = ["QQQ", "XLK", "XLF", "XLY", "XLE", "IBB", "XBI", "SMH", "SOXX"]

# ================== STATE ==================
# symbol -> deque[(timestamp, close)]
series: Dict[str, Deque[Tuple[dt.datetime, float]]] = defaultdict(lambda: deque(maxlen=3000))
# symbol -> deque[(timestamp, volume)]
vol_series: Dict[str, Deque[Tuple[dt.datetime, float]]] = defaultdict(lambda: deque(maxlen=3000))

prev_close: Dict[str, float] = {}
adv20: Dict[str, float] = {}
adv_dollar: Dict[str, float] = {}

ETF_RET_1D: Dict[str, float] = {}
ETF_RET_TS: float = 0.0

last_alert_time: Dict[str, dt.datetime] = {}
recent_alerts: List[dict] = []  # {"time": dt, "symbol": str, "score": int, "longevity": str}

last_tick_time: dt.datetime | None = None

# ================== UTILS ==================
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso8601(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_alpaca_time(t: str) -> dt.datetime:
    return dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def chunked(seq, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def load_cached_symbols() -> List[str]:
    if not os.path.exists(SYMBOLS_CACHE):
        return []
    try:
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(SYMBOLS_CACHE), tz=dt.timezone.utc)
        if (now_utc() - mtime) > dt.timedelta(hours=CACHE_SYMBOLS_HOURS):
            return []
        with open(SYMBOLS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("symbols", [])
    except Exception:
        return []


def save_cached_symbols(symbols: List[str]):
    with open(SYMBOLS_CACHE, "w", encoding="utf-8") as f:
        json.dump({"symbols": symbols, "cached_at": iso8601(now_utc())}, f)


# ================== TELEGRAM ==================
def send_telegram(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            # no parse_mode -> plain text
        }
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def telegram_get_updates() -> List[dict]:
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def telegram_command_listener():
    print("[Telegram] Command listener started")
    last_update_id = None

    while True:
        updates = telegram_get_updates()
        for u in updates:
            uid = u.get("update_id")
            if last_update_id is not None and uid <= last_update_id:
                continue
            last_update_id = uid

            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            if not text:
                continue

            text_lower = text.lower()

            # --- /help ---
            if text_lower == "/help":
                reply = (
                    "Commands:\n"
                    "/status  - scanner status\n"
                    "/signal SYMBOL  - metrics for a ticker (e.g. /signal NVDA)\n"
                    "/top     - top 10 highest-score symbols\n"
                    "/today   - alerts seen today\n"
                    "/help    - this message"
                )
                send_telegram(reply)
                continue

            # --- /status ---
            if text_lower == "/status":
                if last_tick_time is None:
                    lt = "No ticks yet"
                else:
                    lt = iso8601(last_tick_time)
                reply = (
                    "Scanner Status:\n"
                    f"- Last tick: {lt}\n"
                    f"- Tracked symbols: {UNIVERSE_SIZE}\n"
                    f"- Alerts recorded: {len(recent_alerts)}\n"
                    f"- Cooldown: {COOLDOWN_MINUTES} minutes\n"
                    f"- Score filter: >= {SCORE_MIN}"
                )
                send_telegram(reply)
                continue

            # --- /signal SYMBOL ---
            if text_lower.startswith("/signal"):
                parts = text.split()
                if len(parts) != 2:
                    send_telegram("Usage: /signal SYMBOL  (example: /signal NVDA)")
                    continue
                sym = parts[1].upper()
                if sym not in series or len(series[sym]) == 0:
                    send_telegram(f"No data yet for {sym}.")
                    continue

                r1 = compute_return_pct(sym, WINDOWS["1h"])
                r4 = compute_return_pct(sym, WINDOWS["4h"])
                r1d = compute_return_pct(sym, WINDOWS["1d"])
                rvol = compute_rvol(sym)
                gap, hold = compute_gap_and_hold(sym)
                rsi, rsi_trend = compute_rsi_14(sym)

                fetch_etf_returns()
                score, longevity = score_investability(sym, {"1h": r1, "4h": r4, "1d": r1d}, rvol, gap, hold, rsi, rsi_trend)

                reply = (
                    f"{sym} metrics:\n"
                    f"- Score: {score} ({longevity})\n"
                    f"- 1h: {r1:.2f}%\n"
                    f"- 4h: {r4:.2f}%\n"
                    f"- 1d: {r1d:.2f}%\n"
                    f"- RVOL: {rvol:.2f}\n"
                    f"- Gap: {gap:.2f}%\n"
                    f"- Hold: {hold:.2f}%\n"
                    f"- RSI: {rsi:.2f} (trend: {rsi_trend})"
                )
                send_telegram(reply)
                continue

            # --- /top ---
            if text_lower == "/top":
                ranking = []
                fetch_etf_returns()
                for sym in list(series.keys()):
                    if len(series[sym]) == 0:
                        continue
                    r1 = compute_return_pct(sym, WINDOWS["1h"])
                    r4 = compute_return_pct(sym, WINDOWS["4h"])
                    r1d = compute_return_pct(sym, WINDOWS["1d"])
                    if math.isnan(r1d):
                        continue
                    rvol = compute_rvol(sym)
                    gap, hold = compute_gap_and_hold(sym)
                    rsi, rsi_trend = compute_rsi_14(sym)
                    score, longevity = score_investability(
                        sym, {"1h": r1, "4h": r4, "1d": r1d}, rvol, gap, hold, rsi, rsi_trend
                    )
                    ranking.append((score, sym, longevity))

                ranking.sort(reverse=True)  # highest score first
                top10 = ranking[:10]
                if not top10:
                    send_telegram("No data yet. Try again later.")
                    continue

                lines = ["Top 10 scores:"]
                for score, sym, longevity in top10:
                    lines.append(f"- {sym}: {score} ({longevity})")
                send_telegram("\n".join(lines))
                continue

            # --- /today ---
            if text_lower == "/today":
                if not recent_alerts:
                    send_telegram("No alerts recorded yet.")
                    continue
                today_et = now_utc().astimezone(ET).date()
                rows = [
                    a for a in recent_alerts
                    if a["time"].astimezone(ET).date() == today_et
                ]
                if not rows:
                    send_telegram("No alerts today.")
                    continue
                lines = ["Today's alerts:"]
                for a in rows[-20:]:
                    t_str = a["time"].astimezone(ET).strftime("%H:%M")
                    lines.append(f"- {t_str} {a['symbol']}: {a['score']} ({a['longevity']})")
                send_telegram("\n".join(lines))
                continue

        time.sleep(5)


# ================== DATA FETCHING ==================
def fetch_nasdaq_symbols() -> List[str]:
    """Return tradable US NASDAQ equities (no crypto)."""
    url = f"{TRADING_BASE}/assets"
    out: List[str] = []
    try:
        r = requests.get(url, headers=HEADERS, params={"status": "active"}, timeout=60)
        r.raise_for_status()
        for a in r.json():
            if a.get("asset_class") != "us_equity":
                continue
            if a.get("exchange") != "NASDAQ":
                continue
            if not a.get("tradable", True):
                continue
            sym = (a.get("symbol") or "").strip()
            if not sym or "/" in sym:
                continue
            out.append(sym)
    except Exception as e:
        print("[WARN] fetch_nasdaq_symbols:", e)
    return sorted(set(out))


def fetch_prev_close_and_adv20(symbols: List[str]):
    """Compute prev_close, adv20, adv_dollar for all symbols."""
    prev_close.clear()
    adv20.clear()
    adv_dollar.clear()

    for batch in chunked(symbols, 200):
        url = f"{DATA_BASE}/stocks/bars"
        params = {"timeframe": "1Day", "symbols": ",".join(batch), "limit": 21}
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=60)
            r.raise_for_status()
            bars = r.json().get("bars", {})
            for sym, arr in bars.items():
                if not arr:
                    continue
                arr = sorted(arr, key=lambda x: x["t"])
                # last full close is last element
                prev_close[sym] = float(arr[-1]["c"])
                vols = [float(b["v"]) for b in arr[-20:]] if len(arr) >= 20 else [float(b["v"]) for b in arr]
                if vols:
                    adv20[sym] = sum(vols) / len(vols)
                    avg_close = sum(float(b["c"]) for b in arr[-20:]) / min(20, len(arr))
                    adv_dollar[sym] = adv20[sym] * avg_close
        except Exception as e:
            print("[WARN] fetch_prev_close_and_adv20:", e)
        time.sleep(0.35)


def initial_load(symbols: List[str]):
    """Warm up with recent minute bars."""
    print("[Init] Loading initial minute bars...")
    for batch in chunked(symbols, BATCH_SIZE):
        url = f"{DATA_BASE}/stocks/bars"
        params = {"timeframe": "1Min", "symbols": ",".join(batch), "limit": 2000}
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=60)
            r.raise_for_status()
            data = r.json().get("bars", {})
            for sym, arr in data.items():
                if not arr:
                    continue
                arr = sorted(arr, key=lambda x: x["t"])
                for b in arr:
                    ts = parse_alpaca_time(b["t"])
                    c = float(b["c"])
                    v = float(b["v"])
                    series[sym].append((ts, c))
                    vol_series[sym].append((ts, v))
        except Exception as e:
            print("[WARN] initial_load:", e)
        time.sleep(0.35)
    print("[Init] Done.")


def fetch_etf_returns():
    """Update 1-day returns for sector ETFs (cached)."""
    global ETF_RET_TS, ETF_RET_1D
    now = now_utc()
    if ETF_RET_TS and (now - dt.datetime.fromtimestamp(ETF_RET_TS, tz=dt.timezone.utc)) < dt.timedelta(minutes=20):
        return
    res: Dict[str, float] = {}
    for batch in chunked(ETF_BASKET, 10):
        url = f"{DATA_BASE}/stocks/bars"
        params = {"timeframe": "1Day", "symbols": ",".join(batch), "limit": 2}
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            r.raise_for_status()
            data = r.json().get("bars", {})
            for sym, arr in data.items():
                if len(arr) >= 2:
                    arr = sorted(arr, key=lambda x: x["t"])
                    p = float(arr[-2]["c"])
                    c = float(arr[-1]["c"])
                    if p > 0:
                        res[sym] = (c - p) / p * 100.0
        except Exception as e:
            print("[WARN] fetch_etf_returns:", e)
    ETF_RET_1D = res
    ETF_RET_TS = now.timestamp()


# ================== FEATURE COMPUTATIONS ==================
def compute_return_pct(sym: str, minutes: int) -> float:
    arr = series[sym]
    if len(arr) < 2:
        return float("nan")
    latest_ts, latest_price = arr[-1]
    cutoff = latest_ts - dt.timedelta(minutes=minutes)
    base_price = None
    for ts, price in arr:
        if ts >= cutoff:
            base_price = price
            break
    if base_price is None:
        base_price = arr[0][1]
    if base_price <= 0:
        return float("nan")
    return (latest_price - base_price) / base_price * 100.0


def compute_rvol(sym: str) -> float:
    if sym not in adv20 or adv20[sym] <= 0:
        return float("nan")
    vols = vol_series[sym]
    if not vols:
        return float("nan")
    intraday_vol = sum(v for _, v in vols)
    return intraday_vol / adv20[sym]


def compute_gap_and_hold(sym: str) -> Tuple[float, float]:
    """Gap% = open vs prev_close; Hold% = current vs open."""
    if sym not in prev_close or prev_close[sym] <= 0:
        return (float("nan"), float("nan"))
    arr = series[sym]
    if not arr:
        return (float("nan"), float("nan"))

    today = now_utc().astimezone(ET).date()
    open_dt = dt.datetime.combine(today, OPEN_ET, tzinfo=ET).astimezone(dt.timezone.utc)

    today_open = None
    for ts, price in arr:
        if ts >= open_dt:
            today_open = price
            break
    if today_open is None:
        return (float("nan"), float("nan"))

    gap_pct = (today_open - prev_close[sym]) / prev_close[sym] * 100.0
    current_price = arr[-1][1]
    hold_pct = (current_price - today_open) / today_open * 100.0
    return (gap_pct, hold_pct)


def compute_rsi_14(sym: str) -> Tuple[float, int]:
    arr = series[sym]
    if len(arr) < 15:
        return (float("nan"), 0)
    closes = [p for _, p in arr][-200:]
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    if not gains and not losses:
        return (50.0, 0)
    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # trend via last few closes
    trend = 0
    if len(closes) >= 6:
        if closes[-1] > closes[-6]:
            trend = 1
        elif closes[-1] < closes[-6]:
            trend = -1
    return (rsi, trend)


# ================== SCORING ==================
def sector_bonus() -> int:
    if not ETF_RET_1D:
        return 0
    vals = [v for v in ETF_RET_1D.values() if not math.isnan(v)]
    if not vals:
        return 0
    m = max(vals)
    if m > 0.8:
        return 15
    if m > 0.3:
        return 8
    if m < -0.5:
        return -8
    return 0


def score_investability(
    sym: str,
    ret_map: Dict[str, float],
    rvol: float,
    gap_pct: float,
    hold_pct: float,
    rsi: float,
    rsi_trend: int,
) -> Tuple[int, str]:
    # Normalize weights
    total_w = max(1e-9, W_1D + W_4H + W_1H)
    w1d, w4h, w1h = W_1D / total_w, W_4H / total_w, W_1H / total_w

    def norm_m(window: str) -> float:
        r = ret_map.get(window, float("nan"))
        th = THRESHOLDS[window]
        if math.isnan(r) or th <= 0:
            return 0.0
        if r <= 0:
            return 0.0
        return min(2.0, r / th)

    m_raw = w1d * norm_m("1d") + w4h * norm_m("4h") + w1h * norm_m("1h")
    momentum_score = min(100.0, 50.0 + 25.0 * m_raw)

    # RVOL
    if rvol is None or math.isnan(rvol):
        vol_score = 50.0
    else:
        if rvol < 0.8:
            vol_score = 20.0
        elif rvol < 1.0:
            vol_score = 40.0
        elif rvol < 1.5:
            vol_score = 60.0
        elif rvol < 2.5:
            vol_score = 80.0
        else:
            vol_score = 95.0

    # Hold%
    if hold_pct is None or math.isnan(hold_pct):
        hold_score = 50.0
    else:
        if hold_pct < 0:
            hold_score = 30.0
        elif hold_pct < 2:
            hold_score = 45.0
        elif hold_pct < 5:
            hold_score = 60.0
        elif hold_pct < 8:
            hold_score = 75.0
        else:
            hold_score = 90.0

    # Gap%
    gap_bonus = 0.0
    if gap_pct is not None and not math.isnan(gap_pct):
        if gap_pct >= 3.0:
            gap_bonus = 5.0
        elif gap_pct >= 1.0:
            gap_bonus = 2.0
        elif gap_pct <= -3.0:
            gap_bonus = -10.0

    # RSI
    if rsi is None or math.isnan(rsi):
        rsi_score = 50.0
    else:
        if 55 <= rsi <= 80:
            rsi_score = 75.0
        elif rsi > 85:
            rsi_score = 55.0
        elif 45 <= rsi < 55:
            rsi_score = 60.0
        elif rsi < 40:
            rsi_score = 30.0
        else:
            rsi_score = 50.0
        if rsi_trend > 0:
            rsi_score += 10.0
        elif rsi_trend < 0:
            rsi_score -= 10.0
        rsi_score = max(0.0, min(100.0, rsi_score))

    # Sector
    s_bonus = sector_bonus()

    score = (
        0.35 * momentum_score
        + 0.25 * vol_score
        + 0.20 * hold_score
        + 0.20 * rsi_score
        + gap_bonus
        + s_bonus
    )
    score = int(round(max(1.0, min(100.0, score))))

    # Longevity
    longevity = "low (<1 day)"
    if score >= 80 and (rvol or 0) >= 1.5 and (hold_pct or 0) >= 5.0 and rsi_trend > 0:
        longevity = "2–5 days"
    elif score >= 60:
        longevity = "1–2 days"
    elif score >= 45:
        longevity = "hours–1 day"

    return score, longevity


# ================== SCANNER ==================
def process_batch(batch_syms: List[str]):
    global last_tick_time
    if not batch_syms:
        return
    url = f"{DATA_BASE}/stocks/bars"
    params = {"timeframe": "1Min", "symbols": ",".join(batch_syms), "limit": 1}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("bars", {})
    except Exception as e:
        print("[WARN] process_batch:", e)
        return

    fetch_etf_returns()

    for sym, arr in data.items():
        if not arr:
            continue
        b = arr[0]
        ts = parse_alpaca_time(b["t"])
        price = float(b["c"])
        vol = float(b["v"])

        if series[sym] and ts <= series[sym][-1][0]:
            continue

        series[sym].append((ts, price))
        vol_series[sym].append((ts, vol))
        last_tick_time = ts

        # compute returns
        ret_map = {label: compute_return_pct(sym, mins) for label, mins in WINDOWS.items()}

        # bullish-only trigger: any window >= threshold
        bullish_trigger = any(
            (not math.isnan(ret_map[w]) and ret_map[w] >= THRESHOLDS[w]) for w in WINDOWS
        )
        if not bullish_trigger:
            continue

        if sym not in prev_close or prev_close[sym] <= 0:
            continue

        gap, hold = compute_gap_and_hold(sym)
        rsi, rsi_trend = compute_rsi_14(sym)
        rvol = compute_rvol(sym)

        score, longevity = score_investability(sym, ret_map, rvol, gap, hold, rsi, rsi_trend)
        if score < SCORE_MIN:
            continue

        last = last_alert_time.get(sym)
        if last is not None and (ts - last) < dt.timedelta(minutes=COOLDOWN_MINUTES):
            continue

        last_alert_time[sym] = ts

        alert = {"time": ts, "symbol": sym, "score": score, "longevity": longevity}
        recent_alerts.append(alert)
        if len(recent_alerts) > 500:
            del recent_alerts[: len(recent_alerts) - 500]

        emoji = "🚀" if score >= 85 else "📈"
        msg = (
            f"{emoji} {sym} score {score}/100, {longevity}\n"
            f"1h:{ret_map['1h']:.2f}%  4h:{ret_map['4h']:.2f}%  1d:{ret_map['1d']:.2f}%\n"
            f"RVOL:{rvol:.2f}  Gap:{gap:.2f}%  Hold:{hold:.2f}%  RSI:{rsi:.2f} (trend:{rsi_trend})"
        )
        print("[ALERT]", msg)
        send_telegram(msg)


def run_scanner():
    print("[Scanner] Starting up...")
    syms = load_cached_symbols()
    if not syms:
        all_syms = fetch_nasdaq_symbols()
        print(f"[Scanner] Found {len(all_syms)} NASDAQ symbols.")
        fetch_prev_close_and_adv20(all_syms)
        ranked = sorted(all_syms, key=lambda s: adv_dollar.get(s, 0.0), reverse=True)
        syms = ranked[:UNIVERSE_SIZE]
        save_cached_symbols(syms)
    else:
        print(f"[Scanner] Loaded cached universe of {len(syms)} symbols.")

    print("[Scanner] Initial load of minute bars...")
    initial_load(syms)
    print("[Scanner] Entering main loop...")

    while True:
        for batch in chunked(syms, BATCH_SIZE):
            process_batch(batch)
            time.sleep(0.3)
        time.sleep(POLL_INTERVAL)


# ================== FLASK APP ==================
app = Flask(__name__)

HTML = """
<!doctype html>
<html>
<head><title>NASDAQ Scanner</title></head>
<body>
<h1>NASDAQ Investability Scanner</h1>
<p>
  <a href="{{ url_for('simulate') }}">Simulate alert</a> |
  <a href="{{ url_for('health') }}">Health</a> |
  <a href="{{ url_for('index') }}">Home</a>
</p>
<h2>Recent alerts</h2>
<ul>
{% for a in alerts %}
  <li>{{a.time}} — {{a.symbol}} — score {{a.score}} — {{a.longevity}}</li>
{% else %}
  <li>No alerts yet</li>
{% endfor %}
</ul>
</body>
</html>
"""

@app.route("/")
def index():
    rows = sorted(recent_alerts, key=lambda a: a["time"], reverse=True)[:50]
    alerts = [
        {
            "time": iso8601(a["time"]),
            "symbol": a["symbol"],
            "score": a["score"],
            "longevity": a["longevity"],
        }
        for a in rows
    ]
    return render_template_string(HTML, alerts=alerts)


@app.route("/health")
def health():
    return {"status": "ok", "time": iso8601(now_utc())}


@app.route("/test")
def test():
    send_telegram("✅ Test: scanner online")
    return redirect(url_for("index"))


@app.route("/simulate")
def simulate():
    # Simple synthetic alert to verify pipeline
    now = now_utc()
    sym = "SIMUL"
    series[sym].clear()
    vol_series[sym].clear()
    for i in range(120):
        ts = now - dt.timedelta(minutes=120 - i)
        price = 100 + i * 0.1
        vol = 1000 + 10 * i
        series[sym].append((ts, price))
        vol_series[sym].append((ts, vol))
    prev_close[sym] = 99.0
    adv20[sym] = 100000.0

    r1 = compute_return_pct(sym, WINDOWS["1h"])
    r4 = compute_return_pct(sym, WINDOWS["4h"])
    r1d = compute_return_pct(sym, WINDOWS["1d"])
    rvol = compute_rvol(sym)
    gap, hold = compute_gap_and_hold(sym)
    rsi, rsi_trend = compute_rsi_14(sym)
    fetch_etf_returns()
    score, longevity = score_investability(sym, {"1h": r1, "4h": r4, "1d": r1d}, rvol, gap, hold, rsi, rsi_trend)
    emoji = "🚀" if score >= 85 else "📈"
    msg = (
        f"{emoji} {sym} (SIMULATION) score {score}/100, {longevity}\n"
        f"1h:{r1:.2f}% 4h:{r4:.2f}% 1d:{r1d:.2f}% RVOL:{rvol:.2f} Gap:{gap:.2f}% Hold:{hold:.2f}% RSI:{rsi:.2f}"
    )
    send_telegram(msg)
    return redirect(url_for("index"))


# ================== MAIN ==================
if __name__ == "__main__":
    threading.Thread(target=run_scanner, daemon=True).start()
    threading.Thread(target=telegram_command_listener, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

