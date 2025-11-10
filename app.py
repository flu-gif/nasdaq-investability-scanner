import os, time, json, math, threading
import datetime as dt
from collections import deque, defaultdict
from typing import Dict, Deque, Tuple, List
import requests
from dotenv import load_dotenv
from flask import Flask, render_template_string, redirect, url_for
from zoneinfo import ZoneInfo

# ================== Config & Env ==================
load_dotenv()

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
if not API_KEY or not API_SECRET:
    raise SystemExit("Please set APCA_API_KEY_ID and APCA_API_SECRET_KEY in Railway Variables.")

# Optional Telegram (set both to enable)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "Europe/Zurich")
DATA_BASE = os.getenv("ALPACA_DATA_BASE", "https://data.alpaca.markets/v2")
TRADING_BASE = os.getenv("ALPACA_TRADING_BASE", "https://paper-api.alpaca.markets/v2")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))
CACHE_SYMBOLS_HOURS = int(os.getenv("CACHE_SYMBOLS_HOURS", "24"))

WINDOWS = {
    "1h": int(os.getenv("WINDOW_MINUTES_SHORT", "60")),
    "4h": int(os.getenv("WINDOW_MINUTES_MEDIUM", "240")),
    "1d": int(os.getenv("WINDOW_MINUTES_LONG", "1440")),
}
THRESHOLDS = {
    "1h": float(os.getenv("THRESHOLD_PCT_SHORT", "3.0")),
    "4h": float(os.getenv("THRESHOLD_PCT_MEDIUM", "5.0")),
    "1d": float(os.getenv("THRESHOLD_PCT_LONG", "8.0")),
}

LOG_FILE = os.getenv("LOG_FILE", "alerts.csv")

HEADERS = { "APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET }

CACHE_DIR = ".cache"
os.makedirs(CACHE_DIR, exist_ok=True)
SYMBOLS_CACHE = os.path.join(CACHE_DIR, "nasdaq_symbols.json")

# US market session (Eastern Time)
ET = ZoneInfo("America/New_York")
OPEN_ET = dt.time(9, 30)   # 09:30
H1_ET   = dt.time(10, 30)  # 10:30

# ================== State ==================
PriceSeries = Dict[str, Deque[Tuple[dt.datetime, float]]]
VolSeries   = Dict[str, Deque[Tuple[dt.datetime, int]]]
series: PriceSeries = defaultdict(lambda: deque(maxlen=max(WINDOWS.values()) + 600))
vol_series: VolSeries = defaultdict(lambda: deque(maxlen=600))
cooldowns = {}  # (symbol, window) -> last_sent_dt
last_returns = { "1h": {}, "4h": {}, "1d": {} }  # window -> {symbol: return_pct}
recent_alerts: List[dict] = []  # trigger log (also in CSV)
signals: Dict[str, dict] = defaultdict(dict)
prev_close: Dict[str, float] = {}
adv20: Dict[str, float] = {}  # 20-day average daily volume

# ================== Helpers ==================
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def iso8601(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def parse_alpaca_time(t_iso: str) -> dt.datetime:
    # "2025-11-10T13:45:00Z"
    return dt.datetime.strptime(t_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def load_cached_symbols() -> List[str]:
    try:
        if os.path.exists(SYMBOLS_CACHE):
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(SYMBOLS_CACHE), tz=dt.timezone.utc)
            if (now_utc() - mtime) < dt.timedelta(hours=CACHE_SYMBOLS_HOURS):
                with open(SYMBOLS_CACHE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("symbols", [])
    except Exception:
        pass
    return []

def save_cached_symbols(symbols: List[str]):
    with open(SYMBOLS_CACHE, "w", encoding="utf-8") as f:
        json.dump({"symbols": symbols, "cached_at": iso8601(now_utc())}, f, ensure_ascii=False)

# ================== Telegram ==================
def send_telegram(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("[WARN] Telegram send failed:", e)

# ================== Alpaca API ==================
def fetch_nasdaq_symbols() -> List[str]:
    url = f"{TRADING_BASE}/assets"
    params = {"status": "active", "exchange": "NASDAQ"}
    out = []
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    for a in r.json():
        sym = a.get("symbol")
        if sym and a.get("tradable", True):
            out.append(sym)
    return sorted(set(out))

def fetch_bars(symbols: List[str], limit_per_symbol: int, timeframe="1Min"):
    if not symbols:
        return {}
    url = f"{DATA_BASE}/stocks/bars"
    params = {"timeframe": timeframe, "symbols": ",".join(symbols), "limit": limit_per_symbol}
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("bars") or {}

def fetch_prev_close_and_adv20(symbols: List[str]):
    need_prev = [s for s in symbols if s not in prev_close]
    need_adv  = [s for s in symbols if s not in adv20]
    for batch in chunked(list(set(need_prev + need_adv)), 50):
        try:
            bars = fetch_bars(batch, limit_per_symbol=21, timeframe="1Day")
            for sym, blist in bars.items():
                if len(blist) >= 2:
                    prev_close[sym] = float(blist[-2]["c"])
                if len(blist) >= 21:
                    vols = [float(b["v"]) for b in blist[-21:-1]]  # last 20 complete days
                    adv20[sym] = sum(vols)/len(vols) if vols else float("nan")
        except Exception as e:
            print("prev/adv20 batch error:", e)
        time.sleep(0.2)

# ================== Feature Computations ==================
def append_bar(sym: str, bar: dict):
    ts = parse_alpaca_time(bar["t"])
    close = float(bar["c"])
    vol = int(bar["v"])
    dq = series[sym]
    vq = vol_series[sym]
    if not dq or dq[-1][0] < ts:
        dq.append((ts, close))
        vq.append((ts, vol))

def compute_return_pct(sym: str, minutes_window: int) -> float:
    dq = series[sym]
    if not dq:
        return float("nan")
    latest_ts = dq[-1][0]
    cutoff = latest_ts - dt.timedelta(minutes=minutes_window)
    base_price = None
    for ts, px in dq:
        if ts >= cutoff:
            base_price = px
            break
    if base_price is None:
        base_price = dq[0][1]
    last_price = dq[-1][1]
    if base_price <= 0:
        return float("nan")
    return (last_price - base_price) / base_price * 100.0

def compute_rsi_14(sym: str) -> Tuple[float, str]:
    dq = series[sym]
    if len(dq) < 20:
        return (float("nan"), "")
    closes = [px for _, px in dq][-200:]
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i-1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    if len(gains) < 14:
        return (float("nan"), "")
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        avg_gain = (avg_gain*13 + gains[i]) / 14
        avg_loss = (avg_loss*13 + losses[i]) / 14
    rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
    # RSI trend via last ~10 values
    rsi_line = []
    ag, al = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        ag = (ag*13 + gains[i]) / 14
        al = (al*13 + losses[i]) / 14
        val = 100.0 if al == 0 else 100 - (100 / (1 + ag/al))
        rsi_line.append(val)
    trend = ""
    if len(rsi_line) >= 10:
        slope = rsi_line[-1] - rsi_line[-10]
        trend = "rising" if slope > 1.0 else ("falling" if slope < -1.0 else "flat")
    return (round(rsi, 1), trend)

def compute_gap_and_hold(sym: str) -> Tuple[float, float]:
    dq = series[sym]
    if len(dq) < 2 or sym not in prev_close:
        return (float("nan"), float("nan"))
    today = now_utc().astimezone(ET).date()
    open_dt = dt.datetime.combine(today, OPEN_ET, tzinfo=ET).astimezone(dt.timezone.utc)
    h1_dt   = dt.datetime.combine(today, H1_ET,   tzinfo=ET).astimezone(dt.timezone.utc)
    today_open = None
    first_hour_prices = []
    p_1030 = None
    for ts, px in dq:
        if ts >= open_dt and ts <= h1_dt:
            if today_open is None:
                today_open = px
            first_hour_prices.append(px)
            if ts == h1_dt:
                p_1030 = px
    gap_pct = float("nan")
    hold_pct = float("nan")
    yclose = prev_close.get(sym)
    if today_open and yclose and yclose > 0:
        gap_pct = (today_open - yclose) / yclose * 100.0
    if first_hour_prices:
        hi = max(first_hour_prices); lo = min(first_hour_prices)
        if p_1030 is None: p_1030 = first_hour_prices[-1]
        if hi > lo:
            hold_pct = (p_1030 - lo) / (hi - lo) * 100.0
    return (round(gap_pct, 2) if not math.isnan(gap_pct) else gap_pct,
            round(hold_pct, 1) if not math.isnan(hold_pct) else hold_pct)

def compute_intraday_volume(sym: str) -> int:
    vq = vol_series[sym]
    return sum(v for _, v in vq)

def compute_rvol(sym: str) -> float:
    """Time-adjusted intraday RVOL vs 20-day ADV."""
    intraday = compute_intraday_volume(sym)
    adv = adv20.get(sym, float("nan"))
    if not adv or math.isnan(adv) or adv <= 0:
        return float("nan")
    # fraction of regular session elapsed (09:30-16:00 ET = 390 min)
    now_et = now_utc().astimezone(ET)
    start = dt.datetime.combine(now_et.date(), OPEN_ET, tzinfo=ET)
    end   = start + dt.timedelta(minutes=390)
    frac = max(0.05, min(1.0), (now_et - start).total_seconds() / (end - start).total_seconds()) if now_et > start else 0.05
    est_full_day = intraday / frac
    return round(est_full_day / adv, 2)

# ================== Scoring ==================
def score_investability(sym: str, returns: dict, rvol: float, gap_pct: float, hold_pct: float, rsi: float, rsi_trend: str):
    # Momentum score: best of windows vs thresholds
    best = 0.0
    for label, ret in returns.items():
        th = THRESHOLDS[label]
        if not math.isnan(ret) and th > 0:
            best = max(best, min(2.0, abs(ret)/th))  # capped at 2x threshold
    momentum_score = int(min(100, 50 + 25*best))  # at threshold ~75, at 2x ~100

    # Volume score via RVOL
    if rvol and not math.isnan(rvol):
        if rvol < 0.8: vol_score = 20
        elif rvol < 1.0: vol_score = 40
        elif rvol < 1.5: vol_score = 60
        elif rvol < 2.5: vol_score = 80
        else: vol_score = 95
    else:
        vol_score = 50  # neutral if unknown

    # First-hour hold & gap
    hold_score = 50
    if not math.isnan(hold_pct):
        if hold_pct >= 80: hold_score = 90
        elif hold_pct >= 60: hold_score = 75
        elif hold_pct >= 40: hold_score = 60
        elif hold_pct >= 20: hold_score = 45
        else: hold_score = 30
    gap_bonus = 0
    if not math.isnan(gap_pct):
        if gap_pct >= 3: gap_bonus = 5
        elif gap_pct >= 1: gap_bonus = 2
        elif gap_pct <= -3: gap_bonus = -10

    # RSI score
    rsi_score = 50
    if not math.isnan(rsi):
        if 55 <= rsi <= 80:
            rsi_score = 75
        elif rsi > 85:
            rsi_score = 55
        elif 45 <= rsi < 55:
            rsi_score = 60
        elif rsi < 40:
            rsi_score = 30
    if rsi_trend == "rising": rsi_score += 10
    elif rsi_trend == "falling": rsi_score -= 10
    rsi_score = max(0, min(100, rsi_score))

    score = int(round(
        0.35*momentum_score +
        0.25*vol_score +
        0.20*hold_score +
        0.20*rsi_score + gap_bonus
    ))
    score = max(1, min(100, score))

    # Longevity estimate
    longevity = "low (<1 day)"
    if score >= 80 and (rvol or 0) >= 1.5 and (hold_pct or 0) >= 60 and rsi_trend == "rising":
        longevity = "2–5 days"
    elif score >= 60:
        longevity = "1–2 days"
    elif score >= 45:
        longevity = "hours–1 day"

    return score, longevity

# ================== Scanner ==================
def log_alerts(rows: List[dict], log_file: str):
    if not rows:
        return
    new_file = not os.path.exists(log_file)
    import csv
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol","window","return_pct","asof"])
        if new_file:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)

def initial_load(symbols: List[str]):
    fetch_prev_close_and_adv20(symbols)
    for batch in chunked(symbols, BATCH_SIZE):
        try:
            bars = fetch_bars(batch, limit_per_symbol=120, timeframe="1Min")
            for sym, blist in bars.items():
                for b in blist:
                    append_bar(sym, b)
        except Exception as e:
            print("Warm start batch error:", e)
        time.sleep(0.3)

def process_batch(batch_syms: List[str]):
    bars = fetch_bars(batch_syms, limit_per_symbol=1, timeframe="1Min")
    to_log = []
    for sym, blist in bars.items():
        if not blist: continue
        b = blist[-1]
        append_bar(sym, b)
        latest_ts = parse_alpaca_time(b["t"])

        # returns
        ret_map = {label: compute_return_pct(sym, m) for label, m in WINDOWS.items()}
        for label, r in ret_map.items():
            if not math.isnan(r): last_returns[label][sym] = round(r, 2)

        # trigger
        triggered = any(not math.isnan(r) and abs(r) >= THRESHOLDS[label] for label, r in ret_map.items())

        if triggered:
            gap_pct, hold_pct = compute_gap_and_hold(sym)
            rsi, rsi_trend = compute_rsi_14(sym)
            rvol = compute_rvol(sym)

            score, longevity = score_investability(sym, ret_map, rvol, gap_pct, hold_pct, rsi, rsi_trend)
            signals[sym] = {
                "score": score, "longevity": longevity,
                "ret_1h": round(ret_map["1h"],2) if not math.isnan(ret_map["1h"]) else None,
                "ret_4h": round(ret_map["4h"],2) if not math.isnan(ret_map["4h"]) else None,
                "ret_1d": round(ret_map["1d"],2) if not math.isnan(ret_map["1d"]) else None,
                "rvol": rvol, "gap_pct": gap_pct, "hold_pct": hold_pct,
                "rsi": rsi, "rsi_trend": rsi_trend, "asof": latest_ts.isoformat()
            }

            key = (sym, "any")
            last = cooldowns.get(key)
            if last is None or (latest_ts - last) >= dt.timedelta(minutes=10):
                cooldowns[key] = latest_ts
                recent_alerts.append({
                    "symbol": sym, "window": "any",
                    "return_pct": max(abs(v) for v in [ret_map["1h"], ret_map["4h"], ret_map["1d"]] if not math.isnan(v)),
                    "asof": latest_ts.isoformat()
                })
                if len(recent_alerts) > 200:
                    del recent_alerts[:len(recent_alerts)-200]
                msg = f"📈 {sym} score {score}/100, {longevity} | 1h:{ret_map['1h']:.2f}% 4h:{ret_map['4h']:.2f}% 1d:{ret_map['1d']:.2f}% RVOL:{rvol} RSI:{rsi}({rsi_trend})"
                print("[ALERT]", msg)
                send_telegram(msg)
                to_log.append(recent_alerts[-1])
    log_alerts(to_log, LOG_FILE)

def run_scanner_forever():
    symbols = load_cached_symbols()
    if not symbols:
        print("Fetching NASDAQ symbols from Alpaca…")
        symbols = fetch_nasdaq_symbols()
        if not symbols:
            print("Could not retrieve NASDAQ symbol list.")
            return
        save_cached_symbols(symbols)

    print(f"Tracking {len(symbols)} NASDAQ symbols.")
    initial_load(symbols)
    print("Starting polling…")

    last_idx = 0
    while True:
        start_time = time.time()
        ordered = symbols[last_idx:] + symbols[:last_idx]
        for batch in chunked(ordered, BATCH_SIZE):
            try:
                if not prev_close or not adv20:
                    fetch_prev_close_and_adv20(batch)
                process_batch(batch)
            except Exception as e:
                print("Polling batch error:", e)
            time.sleep(0.75)
        last_idx = (last_idx + BATCH_SIZE) % max(1, len(symbols))
        elapsed = time.time() - start_time
        time.sleep(max(0, POLL_INTERVAL - elapsed))

# ================== Flask App (Dashboard) ==================
app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NASDAQ Investability Scanner</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 20px; }
    h1, h2 { margin: 0.4em 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px; border-bottom: 1px solid #eee; text-align: left; }
    .badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #eee; }
    .pos { color: #0a7; font-weight: 600; }
    .neg { color: #d33; font-weight: 600; }
    a.button { display:inline-block; padding:8px 12px; border:1px solid #ddd; border-radius:6px; text-decoration:none; }
    .bar { display:flex; gap:8px; align-items:center; margin: 10px 0 20px; flex-wrap: wrap;}
    .muted { color:#666; font-size: 12px;}
    .score { font-weight: 700; }
  </style>
  <meta http-equiv="refresh" content="30">
</head>
<body>
  <h1>📊 NASDAQ Investability Scanner</h1>
  <div class="bar">
    <a class="button" href="/simulate">Simulate now (no waiting)</a>
    <a class="button" href="/test">Telegram test</a>
    <a class="button" href="/health">Health</a>
    <span class="muted">Auto-refresh every 30s | Delayed data is OK for multi-hour/day signals</span>
  </div>

  <h2>Triggered (ranked by score)</h2>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Score</th><th>Longevity</th>
        <th>1h</th><th>4h</th><th>1d</th>
        <th>RVOL</th><th>Gap%</th><th>Hold%</th>
        <th>RSI</th><th>Trend</th><th>As of (UTC)</th>
      </tr>
    </thead>
    <tbody>
    {% for sym,row in ranked %}
      <tr>
        <td>{{ sym }}</td>
        <td class="score">{{ row.score }}</td>
        <td>{{ row.longevity }}</td>
        <td class="{{ 'pos' if (row.ret_1h or 0)>=0 else 'neg' }}">{{ row.ret_1h if row.ret_1h is not none else '' }}</td>
        <td class="{{ 'pos' if (row.ret_4h or 0)>=0 else 'neg' }}">{{ row.ret_4h if row.ret_4h is not none else '' }}</td>
        <td class="{{ 'pos' if (row.ret_1d or 0)>=0 else 'neg' }}">{{ row.ret_1d if row.ret_1d is not none else '' }}</td>
        <td>{{ row.rvol if row.rvol is not none else '' }}</td>
        <td>{{ row.gap_pct if row.gap_pct is not none else '' }}</td>
        <td>{{ row.hold_pct if row.hold_pct is not none else '' }}</td>
        <td>{{ row.rsi if row.rsi is not none else '' }}</td>
        <td>{{ row.rsi_trend }}</td>
        <td>{{ row.asof }}</td>
      </tr>
    {% else %}
      <tr><td colspan="12"><em>No triggers yet (or warming up). Use “Simulate now”.</em></td></tr>
    {% endfor %}
    </tbody>
  </table>

  <h2>Recent triggers</h2>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Symbol</th><th>Max |Δ%|</th></tr></thead>
    <tbody>
      {% for a in alerts %}
        <tr><td>{{ a.asof }}</td><td>{{ a.symbol }}</td><td>{{ '%.2f'|format(a.return_pct) }}%</td></tr>
      {% else %}
        <tr><td colspan="3"><em>None yet</em></td></tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""

@app.route("/")
def home():
    items = list(signals.items())
    items.sort(key=lambda kv: kv[1].get("score", 0), reverse=True)
    class Row: pass
    ranked = []
    for sym, d in items[:200]:
        r = Row()
        for k,v in d.items():
            setattr(r, k, v)
        ranked.append((sym, r))
    alerts = list(reversed(recent_alerts[-100:]))
    class A: pass
    alert_rows = []
    for x in alerts:
        a = A(); a.symbol=x["symbol"]; a.return_pct=x["return_pct"]; a.asof=x["asof"]; alert_rows.append(a)
    return render_template_string(TEMPLATE, ranked=ranked, alerts=alert_rows)

@app.route("/test")
def test():
    send_telegram("✅ Test: scanner online")
    return redirect(url_for('home'))

@app.route("/simulate")
def simulate():
    syms = ["AAPL","MSFT","NVDA","AMZN","META"]
    try:
        fetch_prev_close_and_adv20(syms)
        bars = fetch_bars(syms, limit_per_symbol=120, timeframe="1Min")
        for sym, blist in bars.items():
            for b in blist:
                append_bar(sym, b)
        for sym in syms:
            ret_map = {label: compute_return_pct(sym, m) for label, m in WINDOWS.items()}
            gap_pct, hold_pct = compute_gap_and_hold(sym)
            rsi, rsi_trend = compute_rsi_14(sym)
            rvol = compute_rvol(sym)
            score, longevity = score_investability(sym, ret_map, rvol, gap_pct, hold_pct, rsi, rsi_trend)
            signals[sym] = {
                "score": score, "longevity": longevity,
                "ret_1h": None if math.isnan(ret_map["1h"]) else round(ret_map["1h"],2),
                "ret_4h": None if math.isnan(ret_map["4h"]) else round(ret_map["4h"],2),
                "ret_1d": None if math.isnan(ret_map["1d"]) else round(ret_map["1d"],2),
                "rvol": rvol, "gap_pct": gap_pct, "hold_pct": hold_pct,
                "rsi": rsi, "rsi_trend": rsi_trend, "asof": iso8601(now_utc())
            }
    except Exception as e:
        print("simulate error:", e)
    return redirect(url_for('home'))

@app.route("/health")
def health():
    return {"status": "ok", "time": iso8601(now_utc())}

def run_scanner_thread():
    try:
        run_scanner_forever()
    except Exception as e:
        print("[FATAL] scanner stopped:", e)

if __name__ == "__main__":
    threading.Thread(target=run_scanner_thread, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
