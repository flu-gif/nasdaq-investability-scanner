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
    raise SystemExit("Please set APCA_API_KEY_ID and APCA_API_SECRET_KEY in Render/Railway Variables.")

# Optional Telegram (set both to enable)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "Europe/Zurich")
DATA_BASE = os.getenv("ALPACA_DATA_BASE", "https://data.alpaca.markets/v2")
TRADING_BASE = os.getenv("ALPACA_TRADING_BASE", "https://paper-api.alpaca.markets/v2")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "100"))
CACHE_SYMBOLS_HOURS = int(os.getenv("CACHE_SYMBOLS_HOURS", "24"))
UNIVERSE_SIZE = int(os.getenv("UNIVERSE_SIZE", "1000"))         # NEW
SCORE_MIN = int(os.getenv("SCORE_MIN", "60"))                    # NEW
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "120"))     # NEW (2 hours)
# Momentum weighting: 1d > 4h > 1h (must sum to 1.0)
W_1D = float(os.getenv("WEIGHT_1D", "0.6"))                      # NEW
W_4H = float(os.getenv("WEIGHT_4H", "0.3"))                      # NEW
W_1H = float(os.getenv("WEIGHT_1H", "0.1"))                      # NEW

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

# Sector / market ETF basket (broad + industry)
ETF_BASKET = ["QQQ","XLK","XLF","XLY","XLE","IBB","XBI","SMH","SOXX"]

# ================== State ==================
PriceSeries = Dict[str, Deque[Tuple[dt.datetime, float]]]
VolSeries   = Dict[str, Deque[Tuple[dt.datetime, int]]]
series: PriceSeries = defaultdict(lambda: deque(maxlen=max(WINDOWS.values()) + 600))
vol_series: VolSeries = defaultdict(lambda: deque(maxlen=600))
cooldowns = {}  # (symbol) -> last_sent_dt  (per-stock)
last_returns = { "1h": {}, "4h": {}, "1d": {} }  # window -> {symbol: return_pct}
recent_alerts: List[dict] = []  # trigger log (also in CSV)
signals: Dict[str, dict] = defaultdict(dict)
prev_close: Dict[str, float] = {}
adv20: Dict[str, float] = {}            # 20-day average daily volume
adv_dollar: Dict[str, float] = {}       # ADV$ = adv20 * prev_close
ETF_RET_1D: Dict[str, float] = {}       # cached 1d returns for ETF basket
ETF_RET_TS: float = 0.0

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
    """List active NASDAQ US equities only (no crypto pairs)."""
    url = f"{TRADING_BASE}/assets"
    params = {"status": "active"}  # we'll filter ourselves
    out = []
    r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    for a in r.json():
        # Keep ONLY US equities on NASDAQ, tradable, and no "/" in symbol
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
    # compute ADV$
    for s in symbols:
        pc = prev_close.get(s)
        av = adv20.get(s)
        if pc and av:
            adv_dollar[s] = pc * av

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
    frac = 0.05
    if now_et > start:
        frac = max(0.05, min(1.0, (now_et - start).total_seconds() / (end - start).total_seconds()))
    est_full_day = intraday / frac
    return round(est_full_day / adv, 2)

# -------- Sector context (ETF 1d returns; additive bonus) --------
def refresh_etf_returns():
    global ETF_RET_TS
    now = time.time()
    if now - ETF_RET_TS < 300 and ETF_RET_1D:  # cache 5 minutes
        return
    try:
        bars = fetch_bars(ETF_BASKET, limit_per_symbol=2, timeframe="1Day")
        for etf, blist in bars.items():
            if len(blist) >= 2:
                y = float(blist[-2]["c"]); t = float(blist[-1]["c"])
                ETF_RET_1D[etf] = (t - y) / y * 100.0 if y > 0 else float("nan")
        ETF_RET_TS = now
    except Exception as e:
        print("ETF returns fetch error:", e)

def sector_bonus_for(bullish: bool) -> int:
    """Additive bonus using the strongest ETF in our basket (no per-stock mapping needed)."""
    refresh_etf_returns()
    vals = [v for v in ETF_RET_1D.values() if not math.isnan(v)]
    if not vals:
        return 0
    m = max(vals) if bullish else min(vals)
    # Use QQQ/basket context: only give positive bonus for bullish alignment
    if bullish:
        if m > 0.8: return 15
        if m > 0.3: return 8
        return 0
    else:
        # for completeness; we don't alert on bearish anyway
        if m < -0.5: return -8
        return 0

# ================== Scoring ==================
def score_investability(sym: str, returns: dict, rvol: float, gap_pct: float, hold_pct: float, rsi: float, rsi_trend: str):
    """
    Weighted momentum (1d>4h>1h) + volume + hold + RSI + sector bonus -> score 1..100 and longevity.
    """
    # Normalize weights
    total_w = max(1e-9, W_1D + W_4H + W_1H)
    w1d, w4h, w1h = W_1D/total_w, W_4H/total_w, W_1H/total_w

    # Only bullish momentum contributes
    def ratio(label):
        r = returns[label]; th = THRESHOLDS[label]
        return max(0.0, min(2.0, (r/th) if (not math.isnan(r) and th>0) else 0.0))
    m = w1d*ratio("1d") + w4h*ratio("4h") + w1h*ratio("1h")
    momentum_score = int(min(100, 50 + 25*m))  # 0..2 -> 50..100

    # Volume score (RVOL)
    if rvol and not math.isnan(rvol):
        if rvol < 0.8: vol_score = 20
        elif rvol < 1.0: vol_score = 40
        elif rvol < 1.5: vol_score = 60
        elif rvol < 2.5: vol_score = 80
        else: vol_score = 95
    else:
        vol_score = 50

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

    # RSI
    rsi_score = 50
    if not math.isnan(rsi):
        if 55 <= rsi <= 80: rsi_score = 75
        elif rsi > 85:      rsi_score = 55
        elif 45 <= rsi < 55:rsi_score = 60
        elif rsi < 40:      rsi_score = 30
    if rsi_trend == "rising": rsi_score += 10
    elif rsi_trend == "falling": rsi_score -= 10
    rsi_score = max(0, min(100, rsi_score))

    # Additive sector bonus (bullish only)
    bullish = (max(returns.get("1d",-1e9), returns.get("4h",-1e9), returns.get("1h",-1e9)) >= 0)
    sector_bonus = sector_bonus_for(bullish)

    score = int(round(
        0.35*momentum_score +
        0.25*vol_score +
        0.20*hold_score +
        0.20*rsi_score + gap_bonus + sector_bonus
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
    symbols = [s for s in symbols if s and "/" not in s]
    # Reduce to top UNIVERSE_SIZE by ADV dollars
    ranked = [(s, adv_dollar.get(s, 0.0)) for s in symbols]
    ranked.sort(key=lambda x: x[1], reverse=True)
    sel = [s for s,_ in ranked[:UNIVERSE_SIZE]]
    save_cached_symbols(sel)
    print(f"Universe limited to top {len(sel)} by ADV$.")

    # Warm history for selected symbols
    for batch in chunked(sel, BATCH_SIZE):
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

        # ---- TRIGGER: bullish only (no negative alerts) ----
        bullish_trigger = (
            (not math.isnan(ret_map["1h"]) and ret_map["1h"] >= THRESHOLDS["1h"]) or
            (not math.isnan(ret_map["4h"]) and ret_map["4h"] >= THRESHOLDS["4h"]) or
            (not math.isnan(ret_map["1d"]) and ret_map["1d"] >= THRESHOLDS["1d"])
        )

        if bullish_trigger:
            gap_pct, hold_pct = compute_gap_and_hold(sym)
            rsi, rsi_trend = compute_rsi_14(sym)
            rvol = compute_rvol(sym)

            score, longevity = score_investability(sym, ret_map, rvol, gap_pct, hold_pct, rsi, rsi_trend)
            # store for dashboard regardless; alert only if score>=SCORE_MIN
            signals[sym] = {
                "score": score, "longevity": longevity,
                "ret_1h": round(ret_map["1h"],2) if not math.isnan(ret_map["1h"]) else None,
                "ret_4h": round(ret_map["4h"],2) if not math.isnan(ret_map["4h"]) else None,
                "ret_1d": round(ret_map["1d"],2) if not math.isnan(ret_map["1d"]) else None,
                "rvol": rvol, "gap_pct": gap_pct, "hold_pct": hold_pct,
                "rsi": rsi, "rsi_trend": rsi_trend, "asof": latest_ts.isoformat()
            }

            # Cooldown per stock
            last = cooldowns.get(sym)
            if (last is None) or ((latest_ts - last) >= dt.timedelta(minutes=COOLDOWN_MINUTES)):
                if score >= SCORE_MIN:
                    cooldowns[sym] = latest_ts
                    maxret = max([v for v in [ret_map["1h"], ret_map["4h"], ret_map["1d"]] if not math.isnan(v)] or [0.0])
                    recent_alerts.append({
                        "symbol": sym, "window": "any",
                        "return_pct": maxret,
                        "asof": latest_ts.isoformat()
                    })
                    if len(recent_alerts) > 200:
                        del recent_alerts[:len(recent_alerts)-200]
                    # Emoji per score
                    emoji = "🚀" if score >= 85 else "📈"
                    msg = f"{emoji} {sym} score {score}/100, {longevity} | 1h:{ret_map['1h']:.2f}% 4h:{ret_map['4h']:.2f}% 1d:{ret_map['1d']:.2f}% RVOL:{rvol} RSI:{rsi}({rsi_trend})"
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
    # Build initial state & reduce to top 1000 by ADV$
    initial_load(symbols)
    # Reload reduced universe for polling
    symbols = load_cached_symbols()
    print(f"Tracking {len(symbols)} NASDAQ symbols (filtered).")
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
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px; border-bottom: 1px solid #eee; text-align: left; }
    a.button { display:inline-block; padding:8px 12px; border:1px solid #ddd; border-radius:6px; text-decoration:none; margin-right:8px;}
    .muted { color:#666; font-size: 12px;}
    .score { font-weight: 700; }
    .pos { color:#0a7; font-weight:600;}
    .neg { color:#d33; font-weight:600;}
  </style>
  <meta http-equiv="refresh" content="30">
</head>
<body>
  <h1>📊 NASDAQ Investability Scanner</h1>
  <div>
    <a class="button" href="/simulate">Simulate now (no waiting)</a>
    <a class="button" href="/test">Telegram test</a>
    <a class="button" href="/health">Health</a>
    <span class="muted">Auto-refresh every 30s | Universe: top {{univ}} by ADV$ | Score filter ≥ {{score_min}} | Cooldown {{cooldown}} min</span>
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
    <thead><tr><th>Time (UTC)</th><th>Symbol</th><th>Max +Δ%</th></tr></thead>
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
    return render_template_string(TEMPLATE, ranked=ranked, alerts=alert_rows,
                                  univ=UNIVERSE_SIZE, score_min=SCORE_MIN, cooldown=COOLDOWN_MINUTES)

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
