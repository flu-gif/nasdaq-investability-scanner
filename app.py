import os
import time
import math
import json
import threading
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from typing import Dict, Deque, List, Tuple

import requests
import yfinance as yf
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for
from zoneinfo import ZoneInfo

# ======================================================
#  ENV + CONSTANTS
# ======================================================

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Scanner settings
UNIVERSE_SIZE = 1000        # Top 1000 by volume
THREADS = 20                # 20 threads concurrent fetching
REFRESH_INTERVAL = 30       # seconds between full scans
COOLDOWN_MINUTES = 120      # 2 hours per symbol
SCORE_MIN = 60              # minimum investability score

# Momentum thresholds (same logic as before)
THRESHOLDS = {
    "1h": 3.0,
    "4h": 5.0,
    "1d": 8.0,
}

WINDOWS = {
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

# Weighting
W_1D = 0.6
W_4H = 0.3
W_1H = 0.1

# Timezones
ET = ZoneInfo("America/New_York")

# Storage structures
series: Dict[str, Deque[Tuple[dt.datetime, float]]] = defaultdict(lambda: deque(maxlen=2000))
vol_series: Dict[str, Deque[Tuple[dt.datetime, float]]] = defaultdict(lambda: deque(maxlen=2000))

prev_close: Dict[str, float] = {}
adv20: Dict[str, float] = {}
adv_dollar: Dict[str, float] = {}

last_alert_time: Dict[str, dt.datetime] = {}
recent_alerts: List[dict] = []

last_tick_time: dt.datetime | None = None

# ======================================================
#  UTILITY FUNCS
# ======================================================

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def iso8601(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

# ======================================================
#  TELEGRAM
# ======================================================

def send_telegram(text: str):
    """Send Telegram message silently (no crash on fail)."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except:
        pass

def telegram_get_updates():
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except:
        return []

# ======================================================
#  LOAD SYMBOL UNIVERSE
# ======================================================

def load_nasdaq_universe() -> List[str]:
    """
    Use yfinance to list all symbols in major US indexes, 
    then trim to top 1000 by volume.
    """
    # yfinance has built-in tickers for indices
    tickers = yf.Tickers("QQQ SPY IWM DIA")  # broad universe
    symbols = set()

    # Extract holdings if available
    for name, tk in tickers.tickers.items():
        if hasattr(tk, "info"):
            comps = tk.info.get("holdings", None)

    # Alternative: use static list of top 1000 Nasdaq tickers
    # Yahoo doesn't provide a clean list, so we fetch via Nasdaq screener (free)
    url = "https://api.nasdaq.com/api/screener/stocks?limit=5000&exchange=nasdaq"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        data = r.json()
        rows = data["data"]["table"]["rows"]
        syms = [row["symbol"] for row in rows if row["symbol"].isalpha()]
    except:
        syms = []

    # Filter down to first UNIVERSE_SIZE and uppercase
    symbols = [s.upper() for s in syms][:UNIVERSE_SIZE]

    return symbols
# ======================================================
#  YFINANCE PARALLEL FETCHING
# ======================================================

def fetch_symbol_minute(sym: str):
    """
    Fetch 1-minute data for a single symbol using yfinance.
    Returns (sym, df or None)
    """
    try:
        df = yf.download(
            tickers=sym,
            interval="1m",
            period="1d",
            progress=False,
            timeout=10,
        )
        if df is None or df.empty:
            return sym, None
        return sym, df
    except Exception:
        return sym, None


def parallel_fetch(symbols: List[str]):
    """
    Fetch 1000 tickers concurrently via ThreadPoolExecutor.
    Returns a dict: {symbol: df}
    """
    results = {}
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        futures = {executor.submit(fetch_symbol_minute, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                s, df = fut.result()
                results[s] = df
            except Exception:
                results[sym] = None
    return results


# ======================================================
#  UPDATE TIME SERIES
# ======================================================

def update_time_series(sym: str, df):
    """
    Update the global time series (price + volume) for one symbol.
    df is a yfinance DataFrame.
    """
    global last_tick_time

    if df is None or df.empty:
        return

    # YFinance timestamps are naïve but represent UTC time
    for ts, row in df.iterrows():
        price = float(row["Close"])
        vol = float(row["Volume"])
        ts = ts.to_pydatetime().replace(tzinfo=dt.timezone.utc)

        # Append only if new
        if series[sym] and ts <= series[sym][-1][0]:
            continue

        series[sym].append((ts, price))
        vol_series[sym].append((ts, vol))
        last_tick_time = ts


# ======================================================
#  FEATURE COMPUTATIONS
# ======================================================

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
    """
    RVOL = intraday volume / ADV20
    ADV20 must be computed from initial 20 daily bars (we handle that separately).
    """
    if sym not in adv20 or adv20[sym] <= 0:
        return float("nan")

    vols = vol_series[sym]
    if not vols:
        return float("nan")

    intraday = sum(v for _, v in vols)
    return intraday / adv20[sym]


def compute_gap_and_hold(sym: str):
    """
    Gap% = today's open vs yesterday close
    Hold% = current vs today's open
    """
    if sym not in prev_close or prev_close[sym] <= 0:
        return float("nan"), float("nan")

    arr = series[sym]
    if not arr:
        return float("nan"), float("nan")

    now_et = now_utc().astimezone(ET)
    today = now_et.date()

    # Market open ET = 9:30
    open_dt = dt.datetime.combine(today, dt.time(9, 30), tzinfo=ET).astimezone(dt.timezone.utc)

    today_open = None
    for ts, price in arr:
        if ts >= open_dt:
            today_open = price
            break

    if today_open is None:
        return float("nan"), float("nan")

    gap = (today_open - prev_close[sym]) / prev_close[sym] * 100.0
    current = arr[-1][1]
    hold = (current - today_open) / today_open * 100.0

    return gap, hold


def compute_rsi_14(sym: str):
    """
    Classic 14-period RSI from minute closes.
    """
    arr = series[sym]
    if len(arr) < 15:
        return float("nan"), 0

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
        return 50.0, 0

    avg_gain = sum(gains) / len(gains) if gains else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1 + rs))

    # Trend: last 5 candles
    if len(closes) > 5:
        if closes[-1] > closes[-6]:
            trend = 1
        elif closes[-1] < closes[-6]:
            trend = -1
        else:
            trend = 0
    else:
        trend = 0

    return rsi, trend
# ======================================================
#  SCORING SYSTEM
# ======================================================

def score_investability(sym: str, ret_map: dict, rvol: float, gap: float, hold: float, rsi: float, rsi_trend: int):
    """
    Computes final investability score 1–100.
    """
    # Momentum normalization based on thresholds
    def norm(window: str):
        r = ret_map.get(window, float("nan"))
        th = THRESHOLDS[window]
        if math.isnan(r) or th <= 0:
            return 0.0
        if r <= 0:
            return 0.0
        return min(2.0, r / th)

    # Weighted momentum
    total_w = W_1D + W_4H + W_1H
    m = (
        (W_1D / total_w) * norm("1d") +
        (W_4H / total_w) * norm("4h") +
        (W_1H / total_w) * norm("1h")
    )

    momentum_score = min(100.0, 50.0 + 25.0 * m)

    # Volume (RVOL)
    if math.isnan(rvol):
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
    if math.isnan(hold):
        hold_score = 50.0
    else:
        if hold < 0:
            hold_score = 30.0
        elif hold < 2:
            hold_score = 45.0
        elif hold < 5:
            hold_score = 60.0
        elif hold < 8:
            hold_score = 75.0
        else:
            hold_score = 90.0

    # Gap bonus
    if math.isnan(gap):
        gap_bonus = 0.0
    else:
        if gap >= 3:
            gap_bonus = 5.0
        elif gap >= 1:
            gap_bonus = 2.0
        elif gap <= -3:
            gap_bonus = -10.0
        else:
            gap_bonus = 0.0

    # RSI
    if math.isnan(rsi):
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
            rsi_score += 10
        elif rsi_trend < 0:
            rsi_score -= 10

        rsi_score = max(0, min(100, rsi_score))

    # Final score
    score = (
        0.35 * momentum_score +
        0.25 * vol_score +
        0.20 * hold_score +
        0.20 * rsi_score +
        gap_bonus
    )

    score = int(max(1, min(100, round(score))))

    # Longevity estimate
    if score >= 80 and not math.isnan(hold) and hold >= 5 and rsi_trend > 0:
        longevity = "2–5 days"
    elif score >= 60:
        longevity = "1–2 days"
    elif score >= 45:
        longevity = "hours–1 day"
    else:
        longevity = "low (<1 day)"

    return score, longevity

def safe_is_nan(x):
    return isinstance(x, float) and math.isnan(x)

def rvol_label(rvol: float) -> str:
    if rvol is None or safe_is_nan(rvol):
        return "no volume data"
    if rvol < 0.8:
        return "very low (weak interest)"
    if rvol < 1.0:
        return "below avg (meh)"
    if rvol < 1.5:
        return "normal to good"
    if rvol < 2.5:
        return "high (strong interest)"
    return "very high (aggressive volume)"

def gap_label(gap: float) -> str:
    if gap is None or safe_is_nan(gap):
        return "no gap data"
    if gap < -3:
        return "big negative gap"
    if gap < 0:
        return "small negative gap"
    if gap < 1:
        return "flat open"
    if gap < 3:
        return "positive gap"
    return "big positive gap"

def hold_label(hold: float) -> str:
    if hold is None or safe_is_nan(hold):
        return "no hold data"
    if hold < 0:
        return "fading after open"
    if hold < 2:
        return "weak hold"
    if hold < 5:
        return "decent hold"
    if hold < 8:
        return "strong hold"
    return "very strong hold"

def rsi_label(rsi: float) -> str:
    if rsi is None or safe_is_nan(rsi):
        return "no rsi data"
    if rsi < 30:
        return "oversold area"
    if rsi < 45:
        return "weak / cooling"
    if rsi < 55:
        return "neutral"
    if rsi < 70:
        return "bullish momentum"
    if rsi < 80:
        return "strong bullish / hot"
    return "very hot / overbought"


# ======================================================
#  SCANNER LOOP (MAIN ENGINE)
# ======================================================

def run_scanner():
    global last_tick_time

    print("[Scanner] Loading NASDAQ universe...")
    symbols = load_nasdaq_universe()
    print(f"[Scanner] Loaded {len(symbols)} symbols.")

    # -------------------------
    # Load ADV20 + prev_close
    # -------------------------
    print("[Scanner] Fetching ADV20 + previous close...")

    for sym in symbols:
        try:
            df = yf.download(sym, interval="1d", period="21d", progress=False)
            if df is None or df.empty:
                continue

            closes = df["Close"].tolist()
            vols = df["Volume"].tolist()

            if len(closes) >= 2:
                prev_close[sym] = float(closes[-2])
            else:
                prev_close[sym] = float(closes[-1])

            # ADV20
            if len(vols) >= 20:
                adv20[sym] = sum(vols[-20:]) / 20
            else:
                adv20[sym] = sum(vols) / len(vols)

            # ADV$ = adv20 * avgclose
            avg_close = sum(closes[-20:]) / min(20, len(closes))
            adv_dollar[sym] = adv20[sym] * avg_close

        except Exception:
            continue

    print("[Scanner] ADV20 + prev_close loaded.")

    # Sort symbols by liquidity (ADV$ descending)
    symbols = sorted(symbols, key=lambda s: adv_dollar.get(s, 0), reverse=True)[:UNIVERSE_SIZE]
    print(f"[Scanner] Using top {len(symbols)} by liquidity.")

    print("[Scanner] Starting main loop...")

    while True:
        cycle_start = time.time()

        # 1. Fetch all 1000 tickers in parallel
        fetch_results = parallel_fetch(symbols)

        # 2. Update timeseries
        for sym, df in fetch_results.items():
            update_time_series(sym, df)

        # 3. Compute features + alerts
        for sym in symbols:
            if len(series[sym]) == 0:
                continue

            # Returns
            r1 = compute_return_pct(sym, WINDOWS["1h"])
            r4 = compute_return_pct(sym, WINDOWS["4h"])
            r1d = compute_return_pct(sym, WINDOWS["1d"])

            # NEW: ignore any stock that is DOWN on the day
            if math.isnan(r1d) or r1d <= 0:
                continue

            ret_map = {"1h": r1, "4h": r4, "1d": r1d}

            # Trigger only if bullish AND positive on the day
            bullish = any(
                not math.isnan(ret_map[w]) and ret_map[w] >= THRESHOLDS[w]
                for w in WINDOWS
            )
            if not bullish:
                continue


            # Additional indicators
            gap, hold = compute_gap_and_hold(sym)
            rsi, rsi_trend = compute_rsi_14(sym)
            rvol = compute_rvol(sym)

            score, longevity = score_investability(sym, ret_map, rvol, gap, hold, rsi, rsi_trend)

            if score < SCORE_MIN:
                continue

            ts = series[sym][-1][0]

            # Cooldown
            last = last_alert_time.get(sym)
            if last is not None and (ts - last) < dt.timedelta(minutes=COOLDOWN_MINUTES):
                continue

            # Mark cooldown
            last_alert_time[sym] = ts

            # Save alert
            alert = {"time": ts, "symbol": sym, "score": score, "longevity": longevity}
            recent_alerts.append(alert)
            if len(recent_alerts) > 500:
                recent_alerts[:] = recent_alerts[-500:]

            emoji = "🚀" if score >= 85 else "📈"

            msg = (
                f"{emoji} {sym} score {score}/100, {longevity}\n"
                f"1h:{r1:.2f}%  4h:{r4:.2f}%  1d:{r1d:.2f}%\n"
                f"RVOL:{rvol:.2f}  Gap:{gap:.2f}%  Hold:{hold:.2f}%\n"
                f"RSI:{rsi:.2f} (trend:{rsi_trend})"
            )

            print("[ALERT]", msg)
            send_telegram(msg)

        # Sleep until next cycle
        elapsed = time.time() - cycle_start
        sleep_time = max(1, REFRESH_INTERVAL - elapsed)
        time.sleep(sleep_time)
# ======================================================


#  TELEGRAM COMMAND LISTENER
# ======================================================

def telegram_command_listener():
    print("[Telegram] Listener started.")
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

            tl = text.lower()

            # /help
            if tl == "/help":
                reply = (
                    "Commands:\n"
                    "/status – scanner status\n"
                    "/signal SYMBOL – detailed metrics\n"
                    "/top – top 10 highest scores\n"
                    "/today – alerts from today\n"
                    "/help – this help message"
                )
                send_telegram(reply)
                continue

            # /status
            if tl == "/status":
                if last_tick_time is None:
                    lt = "No ticks yet"
                else:
                    lt = iso8601(last_tick_time)

                reply = (
                    "Scanner Status:\n"
                    f"- Last tick: {lt}\n"
                    f"- Alerts stored: {len(recent_alerts)}\n"
                    f"- Universe size: {UNIVERSE_SIZE}\n"
                    f"- Score ≥ {SCORE_MIN}\n"
                    f"- Cooldown: {COOLDOWN_MINUTES} min"
                )
                send_telegram(reply)
                continue

            # /signal
            # /signal SYMBOL
            if tl.startswith("/signal"):
                parts = text.split()
                if len(parts) != 2:
                    send_telegram("Usage: /signal SYMBOL")
                    continue

                sym = parts[1].upper()

                if sym not in series or len(series[sym]) == 0:
                    send_telegram(f"No data for {sym}")
                    continue

                r1 = compute_return_pct(sym, WINDOWS["1h"])
                r4 = compute_return_pct(sym, WINDOWS["4h"])
                r1d = compute_return_pct(sym, WINDOWS["1d"])
                gap, hold = compute_gap_and_hold(sym)
                rsi, rsi_trend = compute_rsi_14(sym)
                rvol = compute_rvol(sym)

                score, longevity = score_investability(
                    sym,
                    {"1h": r1, "4h": r4, "1d": r1d},
                    rvol, gap, hold, rsi, rsi_trend
                )

                # Graceful formatting for NaN values
                def fmt_pct(x):
                    if x is None or safe_is_nan(x):
                        return "N/A"
                    return f"{x:.2f}%"

                def fmt_num(x):
                    if x is None or safe_is_nan(x):
                        return "N/A"
                    return f"{x:.2f}"

                rvol_str = fmt_num(rvol)
                gap_str = fmt_pct(gap)
                hold_str = fmt_pct(hold)
                rsi_str = fmt_num(rsi)

                msg = (
                    f"{sym} metrics:\n"
                    f"Score: {score} ({longevity})\n"
                    f"1h: {fmt_pct(r1)}  |  4h: {fmt_pct(r4)}  |  1d: {fmt_pct(r1d)}\n"
                    f"RVOL: {rvol_str}  → {rvol_label(rvol)}\n"
                    f"Gap:  {gap_str}   → {gap_label(gap)}\n"
                    f"Hold: {hold_str}  → {hold_label(hold)}\n"
                    f"RSI:  {rsi_str}   → {rsi_label(rsi)} (trend: {rsi_trend})"
                )
                send_telegram(msg)
                continue


            # /top
            if tl == "/top":
                ranking = []
                for sym in series:
                    if len(series[sym]) < 2:
                        continue

                    r1 = compute_return_pct(sym, WINDOWS["1h"])
                    r4 = compute_return_pct(sym, WINDOWS["4h"])
                    r1d = compute_return_pct(sym, WINDOWS["1d"])

                    # NEW: only show stocks that are UP on the day
                    if math.isnan(r1d) or r1d <= 0:
                        continue

                    rvol = compute_rvol(sym)
                    gap, hold = compute_gap_and_hold(sym)
                    rsi, rsi_trend = compute_rsi_14(sym)

                    score, lon = score_investability(
                        sym,
                        {"1h": r1, "4h": r4, "1d": r1d},
                        rvol, gap, hold, rsi, rsi_trend
                    )
                    ranking.append((score, sym, lon))

                ranking.sort(reverse=True)
                top10 = ranking[:10]
                if not top10:
                    send_telegram("No ranked symbols yet.")
                    continue

                lines = ["Top 10 scores:"]
                for score, sym, lon in top10:
                    lines.append(f"- {sym}: {score} ({lon})")

                send_telegram("\n".join(lines))
                continue

            # /today
            if tl == "/today":
                if not recent_alerts:
                    send_telegram("No alerts yet.")
                    continue

                today = now_utc().astimezone(ET).date()
                rows = [a for a in recent_alerts if a["time"].astimezone(ET).date() == today]

                if not rows:
                    send_telegram("No alerts today.")
                    continue

                lines = ["Today's alerts:"]
                for a in rows[-20:]:
                    t = a["time"].astimezone(ET).strftime("%H:%M")
                    lines.append(f"- {t} {a['symbol']} ({a['score']}) {a['longevity']}")

                send_telegram("\n".join(lines))
                continue

        time.sleep(5)


# ======================================================
#  FLASK DASHBOARD
# ======================================================

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

<h2>Recent Alerts</h2>
<ul>
{% for a in alerts %}
<li>{{a.time}} – {{a.symbol}} – score {{a.score}} – {{a.longevity}}</li>
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
    send_telegram("✅ Test message: scanner online")
    return redirect(url_for("index"))


@app.route("/simulate")
def simulate():
    """
    Synthetic symbol to test alerts without waiting for market conditions.
    """
    sym = "SIMUL"
    now = now_utc()

    series[sym].clear()
    vol_series[sym].clear()

    for i in range(120):
        ts = now - dt.timedelta(minutes=120 - i)
        price = 100 + i * 0.1
        vol = 1000 + i * 10
        series[sym].append((ts, price))
        vol_series[sym].append((ts, vol))

    prev_close[sym] = 99
    adv20[sym] = 50000

    r1 = compute_return_pct(sym, WINDOWS["1h"])
    r4 = compute_return_pct(sym, WINDOWS["4h"])
    r1d = compute_return_pct(sym, WINDOWS["1d"])
    gap, hold = compute_gap_and_hold(sym)
    rsi, rsi_trend = compute_rsi_14(sym)
    rvol = compute_rvol(sym)

    score, lon = score_investability(
        sym,
        {"1h": r1, "4h": r4, "1d": r1d},
        rvol,
        gap,
        hold,
        rsi,
        rsi_trend
    )

    emoji = "🚀" if score >= 85 else "📈"

    msg = (
        f"{emoji} {sym} (SIMULATION) score {score}/100, {lon}\n"
        f"1h:{r1:.2f}% 4h:{r4:.2f}% 1d:{r1d:.2f}%\n"
        f"RVOL:{rvol:.2f} Gap:{gap:.2f}% Hold:{hold:.2f}% RSI:{rsi:.2f}"
    )

    send_telegram(msg)
    recent_alerts.append({"time": now, "symbol": sym, "score": score, "longevity": lon})

    return redirect(url_for("index"))


# ======================================================
#  MAIN ENTRY
# ======================================================

if __name__ == "__main__":
    # Start scanner thread
    threading.Thread(target=run_scanner, daemon=True).start()

    # Start telegram listener
    threading.Thread(target=telegram_command_listener, daemon=True).start()

    # Start Flask
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
