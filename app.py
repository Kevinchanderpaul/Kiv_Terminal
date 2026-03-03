"""
Kiv's RSI Live — The Kiv Terminal

Architecture
────────────
Three background threads:
  price_worker      — raw Yahoo v7 HTTP every 500ms → _price_cache
  bar_download_worker — full 5d×1m download only when new bar forms → _bar_cache
  indicator_worker  — pure pandas recompute every 500ms using live price → _ind_cache

Flask runs with threaded=True so /api/price and /api/indicators
never queue behind each other.

No network I/O ever happens inside a request handler.
"""

import sys, os, socket, threading, webbrowser, time
import requests

# ── Freeze-safe paths ─────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
    os.chdir(os.path.dirname(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.join(BASE_DIR, "static")

# ── Dependency check ──────────────────────────────────────────────────
try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as e:
    try:
        import tkinter as tk
        r = tk.Tk(); r.title("Missing dependency"); r.geometry("520x120")
        r.configure(bg="#0d1117")
        tk.Label(r, text=f"Missing library:\n\n{e}\n\nRun:  pip install yfinance pandas flask requests",
                 font=("Courier New", 11), fg="#f85149", bg="#0d1117",
                 justify="left", padx=20, pady=20).pack()
        r.mainloop()
    except Exception:
        print(f"Missing library: {e}\nRun: pip install yfinance pandas flask requests")
    sys.exit(1)

# ════════════════════════════════════════════════════════════════════
#  SETTINGS
# ════════════════════════════════════════════════════════════════════
RSI_PERIOD      = 14
RSI_LOW         = 30
RSI_HIGH        = 70
PORT            = 7432
INTERVAL        = "1m"
PERIOD          = "5d"
PRICE_REFRESH_S = 0.25  # poll price 4x per second (fastest without rate limits)
IND_REFRESH_S   = 0.25  # recompute indicators 4x per second
BAR_CHECK_S     = 3     # check for new bars every 3 seconds

# ── Options / RapidAPI ────────────────────────────────────────────
RAPIDAPI_KEY    = "2970871a13msh90f6efb2c3c2165p180f50jsnc9a6dc732d59"  # paste your key here
RAPIDAPI_HOST   = "apidojo-yahoo-finance-v1.p.rapidapi.com"

# ════════════════════════════════════════════════════════════════════
#  SHARED STATE
# ════════════════════════════════════════════════════════════════════
_lock        = threading.Lock()
_price_cache = {}   # sym -> {"price": float, "ts": str, "updated_at": float}
_ind_cache   = {}   # sym -> {"result": dict, "updated_at": float}

_bar_lock    = threading.Lock()
_bar_cache   = {}   # sym -> {"close": np.ndarray, "times": list, "updated_at": float}

_symbol      = "ES=F"

# ════════════════════════════════════════════════════════════════════
#  FLASK — threaded so /api/price never waits behind /api/indicators
# ════════════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder=STATIC_DIR)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/price")
def api_price():
    global _symbol
    sym = request.args.get("symbol", _symbol).strip().upper()
    _symbol = sym
    with _lock:
        entry = _price_cache.get(sym)
    if entry is None:
        return jsonify({"error": "Price loading..."}), 503
    return jsonify({"symbol": sym, "price": entry["price"],
                    "ts": entry["ts"],
                    "price_age_s": round(time.time() - entry["updated_at"], 2)})


@app.route("/api/indicators")
def api_indicators():
    global _symbol
    sym = request.args.get("symbol", _symbol).strip().upper()
    _symbol = sym

    # Cold start: block once to download bars
    with _bar_lock:
        has_bars = sym in _bar_cache
    if not has_bars:
        _do_bar_download(sym)

    with _lock:
        entry = _ind_cache.get(sym)
    if entry is None:
        return jsonify({"error": "Indicators loading..."}), 503

    result = dict(entry["result"])
    result["cache_age_s"] = round(time.time() - entry["updated_at"], 2)
    return jsonify(result)


@app.route("/api/meta")
def api_meta():
    """Returns company name, logo URL and exchange for a symbol."""
    sym = request.args.get("symbol", _symbol).strip().upper()
    try:
        ticker = yf.Ticker(sym)
        info   = ticker.info or {}
        name   = info.get("longName") or info.get("shortName") or sym
        logo   = info.get("logo_url") or ""
        exch   = info.get("exchange") or info.get("fullExchangeName") or ""
        sector = info.get("sector") or ""
        return jsonify({"symbol": sym, "name": name, "logo": logo,
                        "exchange": exch, "sector": sector})
    except Exception as e:
        return jsonify({"symbol": sym, "name": sym, "logo": "",
                        "exchange": "", "sector": "", "error": str(e)})


@app.route("/api/news")
def api_news():
    """Returns latest news for a symbol using multiple sources."""
    sym = request.args.get("symbol", _symbol).strip().upper()
    news_items = []
    
    # Method 1: Try yfinance first
    try:
        ticker = yf.Ticker(sym)
        news = ticker.news
        if news and isinstance(news, list):
            for article in news[:5]:
                if isinstance(article, dict):
                    title = article.get("title", article.get("headline", ""))
                    link = article.get("link", article.get("url", ""))
                    if title and link:
                        news_items.append({
                            "title": title,
                            "publisher": article.get("publisher", article.get("source", "Yahoo Finance")),
                            "link": link,
                            "timestamp": article.get("providerPublishTime", int(time.time())),
                        })
    except Exception as e:
        print(f"[news] yfinance failed: {e}")
    
    # Method 2: Fallback to Yahoo Finance search endpoint
    if not news_items:
        try:
            url = f"https://query2.finance.yahoo.com/v1/finance/search?q={sym}"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                for article in data.get("news", [])[:5]:
                    title = article.get("title", "")
                    link = article.get("link", "")
                    if title and link:
                        news_items.append({
                            "title": title,
                            "publisher": article.get("publisher", "Yahoo Finance"),
                            "link": link,
                            "timestamp": article.get("providerPublishTime", int(time.time())),
                        })
        except Exception as e:
            print(f"[news] Yahoo search failed: {e}")
    
    # Method 3: Try Google Finance RSS as last resort
    if not news_items:
        try:
            # Google Finance has an RSS feed we can parse
            url = f"https://news.google.com/rss/search?q={sym}+stock&hl=en-US&gl=US&ceid=US:en"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.content)
                
                for item in root.findall(".//item")[:5]:
                    title_elem = item.find("title")
                    link_elem = item.find("link")
                    pub_elem = item.find("pubDate")
                    source_elem = item.find("source")
                    
                    if title_elem is not None and link_elem is not None:
                        title = title_elem.text
                        link = link_elem.text
                        
                        # Parse pubDate to timestamp
                        timestamp = int(time.time())
                        if pub_elem is not None:
                            try:
                                from email.utils import parsedate_to_datetime
                                dt = parsedate_to_datetime(pub_elem.text)
                                timestamp = int(dt.timestamp())
                            except:
                                pass
                        
                        publisher = "Google News"
                        if source_elem is not None:
                            publisher = source_elem.text
                        
                        news_items.append({
                            "title": title,
                            "publisher": publisher,
                            "link": link,
                            "timestamp": timestamp,
                        })
        except Exception as e:
            print(f"[news] Google RSS failed: {e}")
    
    print(f"[news] {sym}: found {len(news_items)} articles")
    return jsonify({"symbol": sym, "news": news_items})



@app.route("/api/options")
def api_options():
    """
    Fetch options chain from Yahoo Finance via RapidAPI proxy (primary)
    with direct Yahoo HTTP + yfinance as fallbacks.
    """
    sym        = request.args.get("symbol", _symbol).strip().upper()
    expiry_req = request.args.get("expiry", "")

    from datetime import datetime, timezone

    # ── RapidAPI Yahoo Finance (primary — bypasses Yahoo rate limits) ─
    def rapidapi_fetch(symbol, date_ts=None):
        url    = f"https://{RAPIDAPI_HOST}/v7/finance/options/{symbol}"
        params = {}
        if date_ts:
            params["date"] = date_ts
        hdrs = {
            "X-RapidAPI-Key":  RAPIDAPI_KEY,
            "X-RapidAPI-Host": RAPIDAPI_HOST,
            "Accept":          "application/json",
        }
        try:
            r = requests.get(url, headers=hdrs, params=params, timeout=10)
            print(f"[options] RapidAPI -> {r.status_code}")
            if r.status_code == 200:
                return r.json()
            print(f"[options] RapidAPI body: {r.text[:200]}")
        except Exception as e:
            print(f"[options] RapidAPI error: {e}")
        return None

    # ── Direct Yahoo v7 with crumb (fallback 1) ───────────────────────
    def get_crumb_session():
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/122.0.0.0 Safari/537.36",
        })
        try:
            s.get("https://finance.yahoo.com/quote/" + sym, timeout=8)
            for host in ["query1", "query2"]:
                r = s.get(f"https://{host}.finance.yahoo.com/v1/test/getcrumb",
                          headers={"Accept": "text/plain"}, timeout=5)
                if r.status_code == 200 and r.text.strip():
                    return s, r.text.strip()
        except Exception as e:
            print(f"[options] crumb error: {e}")
        return s, None

    def direct_yahoo_fetch(symbol, crumb=None, sess=None, date_ts=None):
        sess = sess or requests.Session()
        params = {}
        if crumb:
            params["crumb"] = crumb
        if date_ts:
            params["date"] = date_ts
        hdrs = {"Accept": "application/json", "Referer": "https://finance.yahoo.com"}
        for host in ["query2", "query1"]:
            url = f"https://{host}.finance.yahoo.com/v7/finance/options/{symbol}"
            try:
                r = sess.get(url, params=params, headers=hdrs, timeout=10)
                print(f"[options] direct/{host} -> {r.status_code}")
                if r.status_code == 200:
                    return r.json()
            except Exception as e:
                print(f"[options] direct/{host} error: {e}")
        return None

    def extract_r0(data):
        if not data:
            return None
        res = data.get("optionChain", {}).get("result", [])
        return res[0] if res else None

    def parse_contracts(lst):
        out = []
        for c in (lst or []):
            iv = c.get("impliedVolatility")
            out.append({
                "strike": c.get("strike"),
                "bid":    c.get("bid"),
                "ask":    c.get("ask"),
                "last":   c.get("lastPrice"),
                "volume": c.get("volume"),
                "oi":     c.get("openInterest"),
                "iv":     round(float(iv) * 100, 2) if iv else None,
                "itm":    bool(c.get("inTheMoney", False)),
                "delta":  c.get("delta"),
                "gamma":  c.get("gamma"),
                "theta":  c.get("theta"),
                "vega":   c.get("vega"),
            })
        return out

    def compute_iv_rank(current_iv, sym):
        try:
            hist = yf.download(sym, period="1y", interval="1d",
                               progress=False, auto_adjust=True)
            if hist is not None and len(hist) >= 22:
                closes  = hist["Close"].squeeze().dropna().values.astype(float)
                lr      = np.log(closes[1:] / closes[:-1])
                hvs     = [float(np.std(lr[i-20:i], ddof=1)) * np.sqrt(252) * 100
                           for i in range(20, len(lr) + 1)]
                if hvs:
                    lo, hi = min(hvs), max(hvs)
                    if hi > lo:
                        rank = round((current_iv - lo) / (hi - lo) * 100, 1)
                        pct  = round(sum(1 for h in hvs if h < current_iv) / len(hvs) * 100, 1)
                        return rank, pct
        except Exception as e:
            print(f"[options] IV rank error: {e}")
        return None, None

    def build_response(sym, expiry, exp_dates, calls_list, puts_list,
                       raw_calls=None, raw_puts=None):
        if raw_calls is not None:
            cv = sum(c.get("volume") or 0 for c in raw_calls)
            pv = sum(c.get("volume") or 0 for c in raw_puts)
            co = sum(c.get("openInterest") or 0 for c in raw_calls)
            po = sum(c.get("openInterest") or 0 for c in raw_puts)
        else:
            cv = sum((c.get("volume") or 0) for c in calls_list)
            pv = sum((c.get("volume") or 0) for c in puts_list)
            co = sum((c.get("oi") or 0) for c in calls_list)
            po = sum((c.get("oi") or 0) for c in puts_list)
        all_ivs    = [c["iv"] for c in calls_list + puts_list if c.get("iv")]
        current_iv = round(float(pd.Series(all_ivs).median()), 2) if all_ivs else None
        iv_rank, iv_pct = compute_iv_rank(current_iv, sym) if current_iv else (None, None)
        return {
            "symbol": sym, "expiry": expiry, "expirations": exp_dates[:12],
            "calls": calls_list, "puts": puts_list,
            "pc_vol": round(pv / cv, 3) if cv > 0 else None,
            "pc_oi":  round(po / co, 3) if co > 0 else None,
            "call_vol": int(cv), "put_vol": int(pv),
            "call_oi":  int(co), "put_oi":  int(po),
            "current_iv": current_iv, "iv_rank": iv_rank, "iv_pct": iv_pct,
        }

    try:
        r0 = None

        # ── Primary: RapidAPI ─────────────────────────────────────────
        if RAPIDAPI_KEY:
            r0 = extract_r0(rapidapi_fetch(sym))

        # ── Fallback 1: direct Yahoo with crumb ───────────────────────
        if not r0:
            sess, crumb = get_crumb_session()
            r0 = extract_r0(direct_yahoo_fetch(sym, crumb=crumb, sess=sess))

        # ── Fallback 2: direct Yahoo no crumb ────────────────────────
        if not r0:
            r0 = extract_r0(direct_yahoo_fetch(sym))

        # ── Fallback 3: yfinance ──────────────────────────────────────
        if not r0:
            print("[options] all HTTP failed, trying yfinance...")
            try:
                ticker = yf.Ticker(sym)
                exps   = ticker.options
                if not exps:
                    return jsonify({"error": "No options data for this symbol"}), 404
                expiry = expiry_req if expiry_req in exps else exps[0]
                chain  = ticker.option_chain(expiry)
                def yf_row(df, idx):
                    def g(col):
                        try:
                            v = df.loc[idx, col]
                            if isinstance(v, float) and v != v: return None
                            return v.item() if hasattr(v, "item") else v
                        except Exception: return None
                    iv = g("impliedVolatility")
                    return {
                        "strike": g("strike"), "bid": g("bid"), "ask": g("ask"),
                        "last": g("lastPrice"), "volume": g("volume"), "oi": g("openInterest"),
                        "iv": round(float(iv)*100,2) if iv else None,
                        "itm": bool(g("inTheMoney")),
                        "delta": g("delta"), "gamma": g("gamma"),
                        "theta": g("theta"), "vega": g("vega"),
                    }
                calls_list = [yf_row(chain.calls, i) for i in chain.calls.index]
                puts_list  = [yf_row(chain.puts,  i) for i in chain.puts.index]
                return jsonify(build_response(sym, expiry, list(exps[:12]),
                                              calls_list, puts_list))
            except Exception as yfe:
                return jsonify({"error": f"All sources failed: {yfe}"}), 503

        # ── Parse v7/RapidAPI result ──────────────────────────────────
        expirations_ts = r0.get("expirationDates", [])
        if not expirations_ts:
            return jsonify({"error": "No expiration dates in response"}), 404

        exp_dates = [datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                     for ts in expirations_ts]

        # Re-fetch specific expiry
        if expiry_req and expiry_req in exp_dates:
            ts  = expirations_ts[exp_dates.index(expiry_req)]
            r0b = extract_r0(rapidapi_fetch(sym, ts)) if RAPIDAPI_KEY else None
            if not r0b:
                sess, crumb = get_crumb_session()
                r0b = extract_r0(direct_yahoo_fetch(sym, crumb=crumb, sess=sess, date_ts=ts))
            if r0b:
                r0 = r0b

        expiry_ts  = r0.get("expirationDates", [expirations_ts[0]])[0]
        expiry     = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        chain_data = (r0.get("options") or [{}])[0]
        calls_raw  = chain_data.get("calls", [])
        puts_raw   = chain_data.get("puts",  [])
        calls_list = parse_contracts(calls_raw)
        puts_list  = parse_contracts(puts_raw)

        return jsonify(build_response(sym, expiry, exp_dates,
                                      calls_list, puts_list,
                                      raw_calls=calls_raw, raw_puts=puts_raw))

    except Exception as e:
        print(f"[options] unhandled: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

def api_validate():
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"valid": False, "error": "empty symbol"}), 400
    try:
        t = yf.download(sym, period="5d", interval="1d", progress=False)
        if t is None or t.empty:
            return jsonify({"valid": False, "error": "no data"}), 404
        return jsonify({"valid": True})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════
#  HTTP SESSION  (persistent — reuses TCP connection)
# ════════════════════════════════════════════════════════════════════
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
})
_QUOTE_URLS = [
    "https://query1.finance.yahoo.com/v7/finance/quote",
    "https://query2.finance.yahoo.com/v7/finance/quote",
]


def _get_live_price(sym):
    """
    Get most recent price by downloading the latest 1-minute bar.
    This is more accurate than the quote API for some symbols.
    """
    from datetime import datetime, timezone, timedelta
    
    # Check if US market is open (9:30 AM - 4:00 PM ET, Mon-Fri)
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_est = month < 3 or month > 11
    et_offset = -5 if is_est else -4
    et_now = utc_now + timedelta(hours=et_offset)
    
    is_weekday = et_now.weekday() < 5  # Mon-Fri
    is_market_hours = (9 <= et_now.hour < 16) or (et_now.hour == 9 and et_now.minute >= 30)
    
    if not (is_weekday and is_market_hours):
        print(f"[price] WARNING: Market closed - data may be stale (current ET: {et_now.strftime('%a %I:%M %p')})")
    
    try:
        # Get the last completed 1-minute bar
        df = yf.download(sym, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            # Get the most recent close price
            latest_close = float(df['Close'].iloc[-1])
            latest_time = df.index[-1]
            if latest_close > 0:
                print(f"[price] {sym} from 1m bar: ${latest_close:.2f} at {latest_time}")
                return latest_close
    except Exception as e:
        print(f"[price] bar fetch error: {e}")
    
    # Fallback 1: Try chart endpoint
    try:
        import time as time_module
        cache_buster = int(time_module.time() * 1000)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        
        r = _session.get(url,
            params={"interval": "1m", "range": "1d", "_": cache_buster},
            headers={"Cache-Control": "no-cache"},
            timeout=1)
        
        if r.status_code == 200:
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                # Get meta price
                meta = result[0].get("meta", {})
                meta_price = meta.get("regularMarketPrice")
                
                # Get last close from quote data
                quote = result[0].get("indicators", {}).get("quote", [])
                if quote and quote[0].get("close"):
                    closes = [c for c in quote[0]["close"] if c is not None]
                    if closes:
                        last_close = float(closes[-1])
                        print(f"[price] {sym} from chart: ${last_close:.2f} (meta says: ${meta_price})")
                        return last_close
    except Exception as e:
        print(f"[price] chart endpoint error: {e}")
    
    # Fallback 2: Quote API
    for url in _QUOTE_URLS:
        try:
            r = _session.get(url,
                params={"symbols": sym, "fields": "regularMarketPrice,regularMarketTime"},
                headers={"Cache-Control": "no-cache"},
                timeout=0.8)
            if r.status_code == 200:
                res = r.json().get("quoteResponse", {}).get("result", [])
                if res:
                    p = res[0].get("regularMarketPrice")
                    t = res[0].get("regularMarketTime", 0)
                    quote_time = datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "unknown"
                    if p and float(p) > 0:
                        print(f"[price] {sym} from quote API: ${float(p):.2f} at {quote_time}")
                        return float(p)
        except Exception as e:
            print(f"[price] quote API error: {e}")
            continue
    
    print(f"[price] {sym}: ALL METHODS FAILED")
    return None


# ════════════════════════════════════════════════════════════════════
#  BAR DOWNLOAD  — stores only the Close array (tiny, fast to copy)
# ════════════════════════════════════════════════════════════════════
def _to_series(x):
    try:    x = x.squeeze("columns")
    except Exception:
        try: x = x.squeeze()
        except Exception: pass
    if hasattr(x, "ndim") and x.ndim != 1:
        x = x.iloc[:, 0]
    return x


def _do_bar_download(sym):
    """Download bars and store Close array + timestamps for ORB calculation."""
    try:
        df = yf.download(sym, period=PERIOD, interval=INTERVAL,
                         progress=False, auto_adjust=True)
        if df is not None and not df.empty and len(df) >= 30:
            close_series = _to_series(df[["Close"]])
            
            # Convert index to Unix timestamps for ORB calculation
            timestamps = [int(ts.timestamp()) for ts in df.index]
            
            with _bar_lock:
                _bar_cache[sym] = {
                    "close":      close_series.values.astype(float),
                    "timestamps": timestamps,
                    "bar_time":   df.index[-1].to_pydatetime().strftime("%Y-%m-%d %H:%M:%S"),
                    "updated_at": time.time(),
                }
            print(f"[bars] {sym} — {len(df)} bars cached")
            return True
    except Exception as e:
        print(f"[bars] error: {e}")
    return False


# ════════════════════════════════════════════════════════════════════
#  INDICATOR COMPUTE  — works directly on the numpy Close array
#  Live price replaces the last element — no DataFrame copy needed
# ════════════════════════════════════════════════════════════════════
def _compute(close_arr, live_price, bar_time, timestamps=None, sym=None):
    """
    All indicator math on a numpy array — no pandas overhead, no copy.
    live_price replaces close_arr[-1] so all indicators reflect the
    current tick rather than the last closed bar.
    timestamps: optional list of Unix timestamps for ORB calculation
    sym: symbol name for logging
    """
    n = len(close_arr)
    if n < RSI_PERIOD + 2:
        return None

    # Inject live price into current bar
    c = close_arr.copy()   # only copying ~1950 floats — <1ms
    if live_price and live_price > 0:
        c[-1] = live_price
    price = float(c[-1])

    # ── RSI (Wilder EMA) ─────────────────────────────────
    delta = np.diff(c)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / RSI_PERIOD
    avg_g = gain[0]
    avg_l = loss[0]
    for i in range(1, len(delta)):
        avg_g = alpha * gain[i] + (1 - alpha) * avg_g
        avg_l = alpha * loss[i] + (1 - alpha) * avg_l
    rsi = round(100.0 - (100.0 / (1.0 + avg_g / avg_l)) if avg_l > 0 else 100.0, 2)

    # ── SMA ──────────────────────────────────────────────
    sma8  = float(np.mean(c[-8:]))
    sma20 = float(np.mean(c[-20:]))

    # ── MACD (12/26/9 EMA) ───────────────────────────────
    def ema(arr, span):
        a = 2.0 / (span + 1)
        e = arr[0]
        for v in arr[1:]:
            e = a * v + (1 - a) * e
        return e

    def ema_series(arr, span):
        a = 2.0 / (span + 1)
        out = np.empty(len(arr))
        out[0] = arr[0]
        for i in range(1, len(arr)):
            out[i] = a * arr[i] + (1 - a) * out[i-1]
        return out

    ema12_s     = ema_series(c, 12)
    ema26_s     = ema_series(c, 26)
    macd_s      = ema12_s - ema26_s
    signal_s    = ema_series(macd_s, 9)
    hist_s      = macd_s - signal_s
    macd_val    = float(macd_s[-1])
    sig_val     = float(signal_s[-1])
    hist_val    = float(hist_s[-1])

    # ── Bollinger Bands (20, 2σ) ──────────────────────────
    bb_mid   = float(np.mean(c[-20:]))
    bb_std   = float(np.std(c[-20:], ddof=1))
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    band_w   = bb_upper - bb_lower
    bb_pos   = ((price - bb_lower) / band_w * 100) if band_w > 0 else 50.0
    bb_signal = "above" if price > bb_upper else ("below" if price < bb_lower else "inside")

    # ── 5-Minute Opening Range Breakout (ORB) ─────────────────
    orb_high = None
    orb_low = None
    orb_signal = "no_data"
    orb_range = 0.0
    
    if timestamps and len(timestamps) == len(close_arr):
        try:
            from datetime import datetime, timezone, timedelta
            
            # Get current time in ET
            utc_now = datetime.now(timezone.utc)
            # ET is UTC-5 (EST) or UTC-4 (EDT) - approximate based on current date
            # Simple heuristic: EST from Nov-Mar, EDT from Mar-Nov
            month = utc_now.month
            is_est = month < 3 or month > 11
            et_offset = -5 if is_est else -4
            et_now = utc_now + timedelta(hours=et_offset)
            
            # Only calculate ORB if we're past 9:35 AM ET today
            market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_open_end = et_now.replace(hour=9, minute=35, second=0, microsecond=0)
            
            if et_now < market_open_end:
                # Too early - market hasn't finished first 5 minutes yet
                if sym:
                    print(f"[orb] {sym}: too early, wait until 9:35 AM ET")
            else:
                # Find bars from TODAY's 9:30-9:34 AM ET window
                today_start = et_now.replace(hour=0, minute=0, second=0, microsecond=0)
                orb_indices = []
                orb_times_found = []
                
                for i, ts in enumerate(timestamps):
                    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
                    dt_et = dt_utc + timedelta(hours=et_offset)
                    
                    # Only bars from TODAY
                    if dt_et.date() != et_now.date():
                        continue
                    
                    # Only bars in the 9:30-9:34 window (5 one-minute bars)
                    if dt_et.hour == 9 and 30 <= dt_et.minute <= 34:
                        orb_indices.append(i)
                        orb_times_found.append(dt_et.strftime("%H:%M"))
                
                if len(orb_indices) >= 1:
                    orb_bars = close_arr[orb_indices]
                    orb_high = float(np.max(orb_bars))
                    orb_low = float(np.min(orb_bars))
                    orb_range = orb_high - orb_low
                    
                    # Debug output
                    if sym:
                        print(f"[orb] {sym}: found {len(orb_indices)} bars from today at {orb_times_found}")
                        print(f"[orb] {sym}: high={orb_high:.2f}, low={orb_low:.2f}, range={orb_range:.2f}")
                    
                    if price > orb_high:
                        orb_signal = "breakout_long"
                    elif price < orb_low:
                        orb_signal = "breakout_short"
                    else:
                        orb_signal = "inside_range"
                elif sym:
                    # No bars found in opening range
                    print(f"[orb] {sym}: no bars found in today's 9:30-9:35 AM ET window")
                
        except Exception as e:
            if sym:
                print(f"[orb] {sym} error: {e}")

    # ── EMA 9 / 21 Cross (Scalp trend) ───────────────────────
    ema9_s  = ema_series(c, 9)
    ema21_s = ema_series(c, 21)
    ema9    = float(ema9_s[-1])
    ema21   = float(ema21_s[-1])

    # Detect fresh cross: did the relationship flip on the last bar?
    ema9_prev  = float(ema9_s[-2])  if len(ema9_s)  > 1 else ema9
    ema21_prev = float(ema21_s[-2]) if len(ema21_s) > 1 else ema21
    ema_cross_now  = "above" if ema9 >= ema21 else "below"
    ema_cross_prev = "above" if ema9_prev >= ema21_prev else "below"
    ema_fresh_cross = ema_cross_now != ema_cross_prev  # just crossed this bar
    ema_gap_pct = round(abs(ema9 - ema21) / ema21 * 100, 4) if ema21 else 0.0

    # ── Volume Delta proxy (buy vs sell pressure) ─────────────
    # We approximate volume delta using price momentum:
    # bars where close > open (or prior close) = buy pressure
    # We use the last 20 bars of close to compute a momentum ratio
    # (true volume delta requires tick data — unavailable from yfinance 1m)
    # Instead: delta = fraction of last N bars that were "up" bars × 200 - 100
    # Range: -100 (all down) to +100 (all up)
    vol_lookback = min(20, len(c) - 1)
    up_bars   = int(np.sum(np.diff(c[-vol_lookback - 1:]) > 0))
    down_bars = vol_lookback - up_bars
    vol_delta = round((up_bars - down_bars) / vol_lookback * 100, 1)  # -100 to +100
    vol_delta_signal = "buying" if vol_delta > 20 else ("selling" if vol_delta < -20 else "neutral")

    # ── Scalp Signal (combined) ───────────────────────────────
    # BUY:  EMA9 above EMA21  AND  vol_delta > 20 (buying pressure)
    # SELL: EMA9 below EMA21  AND  vol_delta < -20 (selling pressure)
    # WAIT: anything else (conflicting signals)
    if ema_cross_now == "above" and vol_delta > 20:
        scalp_signal = "BUY"
    elif ema_cross_now == "below" and vol_delta < -20:
        scalp_signal = "SELL"
    else:
        scalp_signal = "WAIT"

    # ── TRIX (Triple EMA Oscillator) ──────────────────────────
    # TRIX is the 1-period rate of change of a triple-smoothed EMA
    # Typical period is 14 for TRIX
    trix_period = 14
    
    # Calculate triple-smoothed EMA
    ema1 = c.copy()
    ema2 = c.copy()
    ema3 = c.copy()
    
    alpha = 2.0 / (trix_period + 1)
    
    # First EMA
    for i in range(1, len(c)):
        ema1[i] = alpha * c[i] + (1 - alpha) * ema1[i-1]
    
    # Second EMA (EMA of EMA)
    for i in range(1, len(ema1)):
        ema2[i] = alpha * ema1[i] + (1 - alpha) * ema2[i-1]
    
    # Third EMA (EMA of EMA of EMA)
    for i in range(1, len(ema2)):
        ema3[i] = alpha * ema2[i] + (1 - alpha) * ema3[i-1]
    
    # TRIX = 1-period percent change of the triple EMA
    if len(ema3) >= 2 and ema3[-2] != 0:
        trix_value = ((ema3[-1] - ema3[-2]) / ema3[-2]) * 10000  # multiply by 10000 for readability
        trix_value = round(trix_value, 4)
        
        # TRIX signal line (9-period EMA of TRIX)
        # For simplicity, we'll calculate TRIX for last 30 bars and get signal
        trix_history = []
        for i in range(max(0, len(ema3) - 30), len(ema3)):
            if i > 0 and ema3[i-1] != 0:
                trix_val = ((ema3[i] - ema3[i-1]) / ema3[i-1]) * 10000
                trix_history.append(trix_val)
        
        # Signal line = 9-EMA of TRIX
        if len(trix_history) >= 9:
            sig_alpha = 2.0 / 10
            trix_signal = trix_history[0]
            for tv in trix_history[1:]:
                trix_signal = sig_alpha * tv + (1 - sig_alpha) * trix_signal
            trix_signal = round(trix_signal, 4)
        else:
            trix_signal = trix_value
        
        trix_cross = "bullish" if trix_value > trix_signal else "bearish"
    else:
        trix_value = 0.0
        trix_signal = 0.0
        trix_cross = "neutral"

    return {
        "price":          round(price, 2),
        "bar_time":       bar_time,
        "interval":       INTERVAL,
        "period":         PERIOD,
        "rsi":            rsi,
        "rsi_low":        RSI_LOW,
        "rsi_high":       RSI_HIGH,
        "rsi_period":     RSI_PERIOD,
        "sma8":           round(sma8, 2),
        "sma20":          round(sma20, 2),
        "price_vs_sma8":  "above" if price >= sma8  else "below",
        "price_vs_sma20": "above" if price >= sma20 else "below",
        "macd":           round(macd_val, 4),
        "macd_signal":    round(sig_val,  4),
        "macd_hist":      round(hist_val, 4),
        "macd_cross":     "bullish" if hist_val >= 0 else "bearish",
        "bb_upper":       round(bb_upper, 2),
        "bb_mid":         round(bb_mid,   2),
        "bb_lower":       round(bb_lower, 2),
        "bb_position":    round(bb_pos,   1),
        "bb_signal":      bb_signal,
        "orb_high":       round(orb_high, 2) if orb_high else None,
        "orb_low":        round(orb_low, 2) if orb_low else None,
        "orb_range":      round(orb_range, 2),
        "orb_signal":     orb_signal,
        "trix":              trix_value,
        "trix_signal":       trix_signal,
        "trix_cross":        trix_cross,
        # Scalp
        "ema9":              round(ema9, 2),
        "ema21":             round(ema21, 2),
        "ema_cross":         ema_cross_now,
        "ema_fresh_cross":   ema_fresh_cross,
        "ema_gap_pct":       ema_gap_pct,
        "vol_delta":         vol_delta,
        "vol_delta_signal":  vol_delta_signal,
        "scalp_signal":      scalp_signal,
    }


# ════════════════════════════════════════════════════════════════════
#  BACKGROUND WORKERS
# ════════════════════════════════════════════════════════════════════
def price_worker():
    while True:
        sym = _symbol
        p   = _get_live_price(sym)
        if p is not None:
            with _lock:
                _price_cache[sym] = {
                    "price":      round(p, 2),
                    "ts":         pd.Timestamp.now().strftime("%H:%M:%S"),
                    "updated_at": time.time(),
                }
        time.sleep(PRICE_REFRESH_S)


def bar_download_worker():
    """Downloads only when a new 1m bar has formed."""
    last_bar_minute = None
    while True:
        sym = _symbol
        cur_minute = pd.Timestamp.now().floor("1min")
        if last_bar_minute is None or cur_minute > last_bar_minute:
            if _do_bar_download(sym):
                last_bar_minute = cur_minute
        time.sleep(BAR_CHECK_S)


def indicator_worker():
    """Recomputes all indicators every 500ms using the latest live price."""
    while True:
        sym = _symbol
        with _bar_lock:
            entry = _bar_cache.get(sym)
        if entry is not None:
            with _lock:
                pe = _price_cache.get(sym)
            live_price = pe["price"] if pe else None
            timestamps = entry.get("timestamps")
            result = _compute(entry["close"], live_price, entry["bar_time"], 
                            timestamps=timestamps, sym=sym)
            if result:
                result["symbol"] = sym
                with _lock:
                    _ind_cache[sym] = {"result": result, "updated_at": time.time()}
        time.sleep(IND_REFRESH_S)


# ════════════════════════════════════════════════════════════════════
#  PORT HELPER
# ════════════════════════════════════════════════════════════════════
def find_free_port(start=PORT, tries=10):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p)); return p
            except OSError:
                continue
    return start


# ════════════════════════════════════════════════════════════════════
#  LAUNCH
# ════════════════════════════════════════════════════════════════════
def open_browser(port):
    time.sleep(2.5)
    webbrowser.open(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    port = find_free_port()
    print("=" * 55)
    print("  THE KIV TERMINAL")
    print("=" * 55)
    print(f"  STATIC_DIR : {STATIC_DIR}")
    print(f"  index.html : {'FOUND' if os.path.isfile(os.path.join(STATIC_DIR,'index.html')) else '*** MISSING ***'}")
    print(f"  URL        : http://127.0.0.1:{port}")
    print(f"  Price      : every {PRICE_REFRESH_S}s")
    print(f"  Indicators : every {IND_REFRESH_S}s (numpy, no DataFrame copy)")
    print(f"  Bar DL     : on each new 1m bar (check every {BAR_CHECK_S}s)")
    print("=" * 55)

    threading.Thread(target=price_worker,        daemon=True).start()
    threading.Thread(target=bar_download_worker, daemon=True).start()
    threading.Thread(target=indicator_worker,    daemon=True).start()
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # threaded=True — critical: price and indicator requests run in parallel
    app.run(host="127.0.0.1", port=port, debug=False,
            use_reloader=False, threaded=True)
