import os
"""
NIFTY ORB ADVANCED v7.4
=====================
One file only. login.py is NOT required.

First run:
  python orb_onefile.py

The script asks once for:
  - Upstox Client ID
  - Upstox Client Secret
  - Redirect URI

Those are saved locally in ~/.upstox_orb_config.json.

Every day:
  1. Run the same command.
  2. Open the displayed Upstox URL.
  3. Login.
  4. Paste the one-time code here.
  5. The script saves the fresh access token and starts the bot.

Default is PAPER_MODE=True.
Do not enable live orders until the paper workflow is thoroughly tested.
"""

import json
import logging
import time
import urllib.parse
import csv
import math
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

INTEGRATED_MODE = os.getenv("RAILWAY_MODE", "0") == "1"
TELEGRAM_BOT_TOKEN_ENV = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "").strip()
MONGODB_URI_ENV = os.getenv("MONGODB_URI", "").strip()

# ===================== SETTINGS =====================
PAPER_MODE = True

LOTS = 1
MAX_TRADES_PER_DAY = 1
MAX_CAPITAL = 10000.0  # User capital reference (₹) — scaled up from ₹5,000
PAPER_FUTURES_IGNORE_MARGIN = True  # Paper testing may simulate futures even when live margin > MAX_CAPITAL

STOPLOSS_PCT = 12.0
TARGET_PCT = 20.0
RISK_RUPEES = 500.0
TRAIL_TRIGGER_PCT = 10.0
TRAIL_GIVEBACK_PCT = 5.0
DAILY_LOSS_LIMIT = 600.0
COOLDOWN_MINUTES = 30
MAX_RISK_PER_TRADE = 500.0
MARGIN_BUFFER_PCT = 10.0
MAX_SLIPPAGE_PCT = 0.75
MIN_RR = 1.50
ATR_SL_MULT = 1.50
ATR_TARGET_MULT = 2.25
NEWS_FAIL_CLOSED = True
REGIME_FILTER = True
USE_NEWS_FILTER = True
NEWS_MIN_SCORE = 1

# ---- New: weekly circuit breaker, partial booking, AI news, slippage log ----
WEEKLY_LOSS_LIMIT = 1800.0         # hard-stop new entries if this week's realized PnL <= -this
PARTIAL_BOOK_ENABLE = True         # book part of the position once price is partway to target
PARTIAL_BOOK_TRIGGER_PCT = 50.0    # % of the distance to target that triggers the partial book
PARTIAL_BOOK_FRACTION = 0.5        # fraction of quantity to book at the trigger (rest trails to target)
PARTIAL_BOOK_SL_TO_ENTRY = True    # move SL on the remaining qty to entry (breakeven) after partial book
ANTHROPIC_API_KEY_ENV = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_NEWS_MODEL = "claude-sonnet-4-6"
GEMINI_API_KEY_ENV = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_NEWS_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()

RANGE_START = dtime(9, 15)
RANGE_END = dtime(9, 20)
ENTRY_CUTOFF = dtime(11, 30)
SQUARE_OFF_TIME = dtime(15, 15)

CONFIRM_CHECKS = 3
MIN_BREAKOUT_POINTS = 5.0
POLL_SECONDS = 1
# Fast quote loop: sub-second polling for paper breakout/exit simulation.
# This is not a latency guarantee; actual Upstox/network latency still applies.
FAST_POLL_SECONDS = 0.5
API_RETRIES = 3
SIGNAL_MIN_SCORE = 8
MAX_ENTRY_CHASE_PCT = 0.50
MAX_ORB_RANGE_PCT = 1.50
MIN_ATR_PCT = 0.12
MAX_ATR_PCT = 1.50
TRADE_JOURNAL_FILE = Path.home() / "upstox_orb_trades.csv"
DAILY_SUMMARY_TIME = dtime(15, 20)
TELEGRAM_OFFSET_FILE = Path.home() / ".upstox_orb_tg_offset"

NIFTY_KEY = None  # resolved dynamically from Upstox Instrument Search

BASE_URL = "https://api.upstox.com/v2"
V3_URL = "https://api.upstox.com/v3"

CONFIG_FILE = Path.home() / ".upstox_orb_config.json"
TOKEN_FILE = Path.home() / ".upstox_access_token"
STATE_FILE = Path.home() / ".upstox_orb_state.json"
WEEKLY_STATE_FILE = Path.home() / ".upstox_orb_weekly.json"
LOG_FILE = Path.home() / "upstox_orb.log"
TELEGRAM_CONFIG_FILE = Path.home() / ".upstox_orb_telegram.json"

IST = ZoneInfo("Asia/Kolkata")
session = requests.Session()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("upstox_orb")


# ===================== CONFIG / LOGIN =====================
def load_config():
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            if all(data.get(k) for k in ("client_id", "client_secret", "redirect_uri")):
                return data
        except Exception:
            pass

    print("\nFirst-time setup.")
    print("You need the Client ID/API Key and Client Secret/API Secret")
    print("from your Upstox Developer App.")
    print("The redirect URI must exactly match the one registered in Upstox.\n")

    client_id = input("Upstox Client ID: ").strip()
    client_secret = input("Upstox Client Secret: ").strip()
    redirect_uri = input("Registered Redirect URI: ").strip()

    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("Client ID, Client Secret and Redirect URI are required.")

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }

    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass

    print(f"\nSaved local config: {CONFIG_FILE}")
    return data


def auth_url(config):
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
    }
    return "https://api.upstox.com/v2/login/authorization/dialog?" + urllib.parse.urlencode(params)


def generate_access_token(config, code):
    url = f"{BASE_URL}/login/authorization/token"

    payload = {
        "code": code,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "redirect_uri": config["redirect_uri"],
        "grant_type": "authorization_code",
    }

    r = requests.post(
        url,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=payload,
        timeout=15,
    )

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code != 200 or "access_token" not in data:
        raise RuntimeError(f"Token generation failed: {data}")

    return data["access_token"]


def get_token(config):
    # Always ask for a fresh token at startup.
    # Upstox access tokens expire at 3:30 AM the following day.
    print("\n" + "=" * 65)
    print("UPSTOX LOGIN")
    print("=" * 65)
    print("\nOpen this URL in your phone browser:\n")
    print(auth_url(config))
    print("\nAfter login, Upstox redirects to your registered redirect URI.")
    print("Copy ONLY the value after '?code=' (before '&state=' if present).")

    code = input("\nPaste fresh Upstox code: ").strip()

    if not code:
        raise RuntimeError("No authorization code entered.")

    token = generate_access_token(config, code)

    TOKEN_FILE.write_text(token)
    try:
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass

    print("\nSUCCESS: Fresh access token saved locally.")
    return token


def load_token():
    if INTEGRATED_MODE and MONGODB_URI_ENV and TELEGRAM_CHAT_ID_ENV:
        try:
            from db import get_token
            doc = get_token(TELEGRAM_CHAT_ID_ENV)
            if doc and doc.get("access_token"):
                return doc["access_token"]
        except Exception as e:
            log.warning("Mongo token read failed: %s", e)
    if not TOKEN_FILE.exists():
        return None
    value = TOKEN_FILE.read_text().strip()
    return value or None


# ===================== TELEGRAM =====================
def load_telegram_config():
    if TELEGRAM_BOT_TOKEN_ENV and TELEGRAM_CHAT_ID_ENV:
        return {"bot_token": TELEGRAM_BOT_TOKEN_ENV, "chat_id": TELEGRAM_CHAT_ID_ENV}
    if TELEGRAM_CONFIG_FILE.exists():
        try:
            data = json.loads(TELEGRAM_CONFIG_FILE.read_text())
            if data.get("bot_token") and data.get("chat_id"):
                return data
        except Exception:
            pass
    if INTEGRATED_MODE:
        return None
    print("\nFirst-time Telegram setup.")
    bot_token = input("Telegram Bot Token: ").strip()
    chat_id = input("Telegram Chat ID: ").strip()
    if not bot_token or not chat_id:
        return None
    data = {"bot_token": bot_token, "chat_id": chat_id}
    TELEGRAM_CONFIG_FILE.write_text(json.dumps(data, indent=2))
    try: TELEGRAM_CONFIG_FILE.chmod(0o600)
    except Exception: pass
    return data


def telegram_send(message):
    config = load_telegram_config()
    if not config:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
            data={"chat_id": config["chat_id"], "text": message},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        log.warning("Telegram error: %s", r.text)
    except requests.RequestException as e:
        log.warning("Telegram network error: %s", e)
    return False


# ===================== GENERAL HELPERS =====================
def now():
    return datetime.now(IST)


def state_key():
    return now().strftime("%Y-%m-%d")


def _mongo_state():
    if not INTEGRATED_MODE or not MONGODB_URI_ENV or not TELEGRAM_CHAT_ID_ENV:
        return None
    try:
        from db import load_state as db_load_state
        return db_load_state(TELEGRAM_CHAT_ID_ENV)
    except Exception as e:
        log.warning("Mongo state read failed: %s", e)
        return None

def load_state():
    remote = _mongo_state()
    if remote:
        remote.pop("_id", None)
        remote.setdefault("date", state_key())
        if remote.get("date") == state_key():
            remote.setdefault("trades", 0); remote.setdefault("realized_pnl", 0.0); remote.setdefault("last_exit_ts", None); remote.setdefault("wins", 0); remote.setdefault("losses", 0); remote.setdefault("position", None); remote.setdefault("paper_enabled", PAPER_MODE); remote.setdefault("daily_summary_sent", False)
            return remote
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if data.get("date") == state_key():
                data.setdefault("trades", 0)
                data.setdefault("realized_pnl", 0.0)
                data.setdefault("last_exit_ts", None)
                data.setdefault("wins", 0)
                data.setdefault("losses", 0)
                data.setdefault("position", None)
                data.setdefault("daily_summary_sent", False)
                return data
        except Exception:
            pass
    return {
        "date": state_key(), "trades": 0, "position": None,
        "realized_pnl": 0.0, "last_exit_ts": None, "wins": 0, "losses": 0, "daily_summary_sent": False
    }

def save_state(state):
    if INTEGRATED_MODE and MONGODB_URI_ENV and TELEGRAM_CHAT_ID_ENV:
        try:
            from db import save_state as db_save_state
            db_save_state(TELEGRAM_CHAT_ID_ENV, state)
        except Exception as e:
            log.warning("Mongo state write failed: %s", e)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    try:
        STATE_FILE.chmod(0o600)
    except Exception:
        pass


# ===================== WEEKLY CIRCUIT BREAKER =====================
def week_key():
    y, w, _ = now().isocalendar()
    return f"{y}-W{w:02d}"


def load_weekly_state():
    """Separate from the daily state (which resets every day) so the weekly
    loss total survives across trading days within the same ISO week."""
    if INTEGRATED_MODE and MONGODB_URI_ENV and TELEGRAM_CHAT_ID_ENV:
        try:
            from db import load_weekly_state as db_load_weekly
            remote = db_load_weekly(TELEGRAM_CHAT_ID_ENV)
            if remote and remote.get("week") == week_key():
                remote.pop("_id", None)
                remote.setdefault("pnl", 0.0)
                remote.setdefault("summary_sent", False)
                return remote
        except Exception as e:
            log.warning("Mongo weekly state read failed: %s", e)
    if WEEKLY_STATE_FILE.exists():
        try:
            data = json.loads(WEEKLY_STATE_FILE.read_text())
            if data.get("week") == week_key():
                data.setdefault("pnl", 0.0)
                data.setdefault("summary_sent", False)
                return data
        except Exception:
            pass
    return {"week": week_key(), "pnl": 0.0, "summary_sent": False}


def save_weekly_state(data):
    if INTEGRATED_MODE and MONGODB_URI_ENV and TELEGRAM_CHAT_ID_ENV:
        try:
            from db import save_weekly_state as db_save_weekly
            db_save_weekly(TELEGRAM_CHAT_ID_ENV, data)
        except Exception as e:
            log.warning("Mongo weekly state write failed: %s", e)
    try:
        WEEKLY_STATE_FILE.write_text(json.dumps(data, indent=2, default=str))
        WEEKLY_STATE_FILE.chmod(0o600)
    except Exception:
        pass


def add_weekly_pnl(delta):
    """Call this every time realized_pnl changes so the weekly total stays in sync."""
    wk = load_weekly_state()
    wk["pnl"] = float(wk.get("pnl", 0.0)) + float(delta)
    save_weekly_state(wk)
    return wk["pnl"]


def weekly_loss_limit_hit():
    return float(load_weekly_state().get("pnl", 0.0)) <= -WEEKLY_LOSS_LIMIT


def headers():
    token = load_token()
    if not token:
        raise RuntimeError("No access token. Restart the program and login.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def api_get(url, params=None):
    for attempt in range(1, API_RETRIES + 1):
        try:
            r = session.get(
                url,
                headers=headers(),
                params=params or {},
                timeout=8,
            )

            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            if r.status_code == 200:
                return data

            log.error(
                "GET %s/%s -> %s %s",
                attempt, API_RETRIES, r.status_code, data
            )

        except requests.RequestException as e:
            log.error("Network error %s/%s: %s", attempt, API_RETRIES, e)

        time.sleep(attempt)

    return None


def _quote_last_price(data, instrument_key=None):
    """Extract last_price from a V3 quote response safely."""
    try:
        items = (data or {}).get("data", {})
        if not items:
            return None
        wanted = str(instrument_key or "").replace("|", ":")
        item = items.get(wanted) if wanted else None
        if item is None and instrument_key:
            for value in items.values():
                if str(value.get("instrument_token", "")) == instrument_key:
                    item = value
                    break
        if item is None:
            item = next(iter(items.values()))
        if item.get("last_price") is not None:
            return float(item["last_price"])
        ohlc = item.get("ohlc") or {}
        if ohlc.get("close") is not None:
            return float(ohlc["close"])
    except Exception as e:
        log.warning("V3 quote parse failed for %s: %s", instrument_key, e)
    return None


def get_ltp_batch(instrument_keys):
    """Read multiple current prices in one Upstox V3 request.

    Returns a dict keyed by the ORIGINAL instrument_key passed in, so callers
    can look up prices the same way regardless of how Upstox formats the
    response dict's own keys (":" vs "|") or whether instrument_token exactly
    matches instrument_key.
    """
    keys = [k for k in instrument_keys if k]
    if not keys:
        return {}
    data = api_get(
        f"{V3_URL}/market-quote/quotes",
        {"instrument_key": ",".join(keys)},
    )
    items = (data or {}).get("data", {}) or {}
    out = {}
    if not items:
        return out
    # Index by instrument_token too, since the response dict's own keys don't
    # always match the instrument_key format we sent (":" vs "|").
    by_token = {}
    try:
        for value in items.values():
            tok = str(value.get("instrument_token", ""))
            if tok:
                by_token[tok] = value
    except Exception as e:
        log.warning("V3 batch quote index failed: %s", e)
        return out
    for key in keys:
        try:
            wanted = key.replace("|", ":")
            item = items.get(wanted) or by_token.get(key)
            if item is None:
                continue
            price = item.get("last_price")
            if price is None:
                ohlc = item.get("ohlc") or {}
                price = ohlc.get("close")
            if price is not None:
                out[key] = float(price)
        except Exception as e:
            log.warning("V3 batch quote parse failed for %s: %s", key, e)
    return out


def get_ltp(instrument_key):
    """Read current price using Upstox V3 LTP, then full quote fallback."""
    if not instrument_key:
        return None
    data = api_get(f"{V3_URL}/market-quote/ltp", {"instrument_key": instrument_key})
    price = _quote_last_price(data, instrument_key)
    if price is not None:
        return price
    data = api_get(f"{V3_URL}/market-quote/quotes", {"instrument_key": instrument_key})
    return _quote_last_price(data, instrument_key)

def wait_until(target):
    while now().time() < target:
        log.info("Waiting: %s", now().strftime("%H:%M:%S"))
        time.sleep(5)


# ===================== OPTIONS =====================
def nearest_expiry(index_key=None):
    index_key = index_key or NIFTY_KEY
    if not index_key:
        return None
    data = api_get(
        f"{BASE_URL}/option/contract",
        {"instrument_key": index_key},
    )

    if not data:
        return None

    today = state_key()

    expiries = sorted({
        item.get("expiry")
        for item in data.get("data", [])
        if item.get("expiry") and item["expiry"] >= today
    })

    return expiries[0] if expiries else None


def select_atm_option(direction, spot, expiry, index_key=None):
    index_key = index_key or NIFTY_KEY
    if not index_key:
        return None
    data = api_get(
        f"{BASE_URL}/option/chain",
        {
            "instrument_key": index_key,
            "expiry_date": expiry,
        },
    )

    if not data:
        return None

    chain = data.get("data", [])

    if not chain:
        return None

    row = min(
        chain,
        key=lambda item: abs(float(item["strike_price"]) - spot)
    )

    side = "call_options" if direction == "CE" else "put_options"
    option = row.get(side)

    if not option:
        return None

    instrument_key = option.get("instrument_key")

    if not instrument_key:
        return None

    contracts = api_get(
        f"{BASE_URL}/option/contract",
        {
            "instrument_key": index_key,
            "expiry_date": expiry,
        },
    )

    lot_size = None
    tick_size = None

    if contracts:
        for item in contracts.get("data", []):
            if item.get("instrument_key") == instrument_key:
                lot_size = int(item["lot_size"])
                tick_size = item.get("tick_size")
                break

    if not lot_size:
        log.error("Lot size unavailable. NO TRADE.")
        return None

    return {
        "instrument_key": instrument_key,
        "symbol": option.get("trading_symbol", ""),
        "strike": float(row["strike_price"]),
        "lot_size": lot_size,
        "tick_size": float(tick_size) if tick_size else 0.05,
    }


# ===================== ADVANCED FILTERS =====================
INDEX_SEARCH_QUERIES={
    "NIFTY":["Nifty 50","NIFTY"],
    "BANKNIFTY":["Nifty Bank","BANKNIFTY"],
    "FINNIFTY":["Nifty Fin Service","FINNIFTY"],
    "MIDCPNIFTY":["Nifty Midcap Select","MIDCPNIFTY"],
    "NIFTYNXT50":["Nifty Next 50","NIFTYNXT50"],
}
INDEX_KEY_CACHE={}
FUTURE_UNDERLYINGS={
    "NIFTY FUTURE":("NIFTY",None),
    "BANKNIFTY FUTURE":("BANKNIFTY",None),
    "FINNIFTY FUTURE":("FINNIFTY",None),
    "MIDCPNIFTY FUTURE":("MIDCPNIFTY",None),
    "NIFTYNXT50 FUTURE":("NIFTYNXT50",None),
}
AUTO_ORB_CACHE={}

def resolve_index_key(symbol):
    global NIFTY_KEY
    if symbol in INDEX_KEY_CACHE: return INDEX_KEY_CACHE[symbol]
    for query in INDEX_SEARCH_QUERIES.get(symbol,[symbol]):
        data=api_get(f"{BASE_URL}/instruments/search",{
            "query":query,"exchanges":"NSE","segments":"INDEX","instrument_types":"INDEX","page_number":1,"records":30})
        rows=data.get("data",[]) if data else []
        matches=[x for x in rows if x.get("segment")=="NSE_INDEX" and x.get("instrument_type")=="INDEX" and x.get("instrument_key")]
        if matches:
            def rank(x):
                ts=str(x.get("trading_symbol","")).upper(); name=str(x.get("name","")).upper(); q=query.upper()
                return (0 if ts==symbol else 1,0 if ts==q else 1,0 if q in name else 1)
            matches.sort(key=rank); key=matches[0]["instrument_key"]
            INDEX_KEY_CACHE[symbol]=key
            if symbol=="NIFTY": NIFTY_KEY=key
            log.info("Resolved %s index key: %s (%s)",symbol,key,matches[0].get("trading_symbol"))
            return key
    log.error("Could not resolve current Upstox index key for %s",symbol); return None

def resolved_underlyings():
    out={}
    for name,(symbol,_) in FUTURE_UNDERLYINGS.items():
        key=resolve_index_key(symbol)
        if key: out[name]=(symbol,key)
    return out

def orb_from_1m_candles(index_key):
    cs=candles(index_key,1); high=low=None
    for x in cs:
        if RANGE_START<=x["ts"].time()<RANGE_END:
            high=x["h"] if high is None else max(high,x["h"]); low=x["l"] if low is None else min(low,x["l"])
    return high,low

def auto_select_mode():
    """Market-driven AUTO selection.

    After the 09:15-09:20 ORB is complete, compare all supported underlyings.
    The bot waits for a confirmed ORB breakout and picks the strongest one.
    If a futures contract fits the ₹10,000 capital limit it uses the future;
    otherwise it uses the ATM option of the strongest underlying.
    """
    if now().time() < RANGE_END:
        print("\nAUTO SELECT: waiting until 09:20 for ORB completion...")
        telegram_send("⏳ AUTO SELECT\nWaiting for 09:20 AM ORB completion.")
        wait_until(RANGE_END)

    telegram_send(
        "🤖 AUTO SELECT ACTIVE\n"
        "Comparing NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / NIFTYNXT50\n"
        "Choosing the strongest confirmed ORB breakout.\n"
        "Capital limit: ₹10,000 | PAPER MODE"
    )

    # Cache each day's ORB once. This avoids changing the reference range while
    # the auto-selector polls the market.
    orb_map = {}
    for name, (symbol, _) in FUTURE_UNDERLYINGS.items():
        key = resolve_index_key(symbol)
        if not key:
            print(f"- {name}: index key unavailable")
            continue
        high, low = orb_from_1m_candles(key)
        if high is None or low is None or high <= low:
            print(f"- {name}: ORB unavailable")
            continue
        orb_map[symbol] = {"name": name, "key": key, "high": high, "low": low}
        AUTO_ORB_CACHE[symbol] = (high, low)

    if not orb_map:
        nifty_key = resolve_index_key("NIFTY")
        print("⛔ AUTO SELECT: no ORB data available")
        return "OPTION", "NIFTY", nifty_key

    # First verify that the quote API is actually returning live prices.
    # This prevents a silent multi-hour "waiting for market quotes" state.
    initial_quotes = get_ltp_batch([x["key"] for x in orb_map.values()])
    live_quote_count = sum(1 for x in orb_map.values() if x["key"] in initial_quotes)
    if live_quote_count == 0:
        telegram_send(
            "⚠️ QUOTE API HEALTH CHECK FAILED\n"
            "Upstox V3 returned no live quotes for the monitored indices.\n"
            "Bot will retry automatically; PAPER MODE only.\n"
            "Check Railway logs for the exact HTTP/API error if this persists."
        )
        log.error("QUOTE API HEALTH CHECK: 0/%d monitored index quotes", len(orb_map))
    else:
        telegram_send(f"✅ QUOTE API HEALTH: {live_quote_count}/{len(orb_map)} live index quotes")
        log.info("QUOTE API HEALTH CHECK: %d/%d monitored index quotes", live_quote_count, len(orb_map))

    # Keep looking until a real, confirmed breakout appears or the entry window
    # closes. This makes AUTO genuinely market-driven instead of always falling
    # back to NIFTY simply because futures margin is unavailable.
    confirmation = {symbol: {"up": 0, "down": 0} for symbol in orb_map}
    last_status = 0.0

    while now().time() < ENTRY_CUTOFF:
        candidates = []
        batch_quotes = get_ltp_batch([x["key"] for x in orb_map.values()])
        for symbol, info in orb_map.items():
            p = batch_quotes.get(info["key"])
            if p is None:
                continue

            high, low = info["high"], info["low"]
            rng = max(high - low, 0.01)
            c = confirmation[symbol]

            if p >= high + MIN_BREAKOUT_POINTS:
                c["up"] += 1
                c["down"] = 0
                direction = "CE"
                strength = (p - high) / rng
            elif p <= low - MIN_BREAKOUT_POINTS:
                c["down"] += 1
                c["up"] = 0
                direction = "PE"
                strength = (low - p) / rng
            else:
                c["up"] = 0
                c["down"] = 0
                direction = None
                strength = 0.0

            score = (4 if direction else 0) + min(6, max(0, int(strength * 6)))
            confirmed = (
                c["up"] >= CONFIRM_CHECKS or c["down"] >= CONFIRM_CHECKS
            )
            candidates.append({
                "symbol": symbol,
                "name": info["name"],
                "key": info["key"],
                "high": high,
                "low": low,
                "ltp": p,
                "direction": direction,
                "strength": strength,
                "score": score,
                "confirmed": confirmed,
                "up": c["up"],
                "down": c["down"],
            })

        confirmed = [x for x in candidates if x["confirmed"] and x["direction"]]
        if confirmed:
            # Prefer the strongest breakout. On ties prefer larger relative
            # breakout strength, then the confirmation count.
            best = max(
                confirmed,
                key=lambda x: (x["score"], x["strength"], max(x["up"], x["down"]))
            )

            # Prefer the underlying FUTURE in AUTO mode whenever the current
            # contract can be resolved.  In PAPER mode we allow simulation even
            # when live margin is above ₹10,000; the Telegram message clearly shows
            # the live-margin requirement so there is no false impression that a
            # ₹10,000 live account can necessarily place the order.
            fut = select_future(best["symbol"])
            future_margin = None
            if fut:
                mb = margin_required(fut["instrument_key"], fut["lot_size"] * LOTS, "BUY")
                ms = margin_required(fut["instrument_key"], fut["lot_size"] * LOTS, "SELL")
                margins = [x for x in (mb, ms) if x is not None]
                future_margin = max(margins) if margins else None

            if fut and (PAPER_MODE and PAPER_FUTURES_IGNORE_MARGIN or
                        future_margin is not None and future_margin <= MAX_CAPITAL):
                mode = "FUTURE"
                margin_text = f"₹{future_margin:.0f}" if future_margin is not None else "unavailable"
                live_note = ("\n⚠️ Live margin is above ₹10,000" if future_margin is not None and future_margin > MAX_CAPITAL else "")
                msg = (
                    f"🤖 AUTO SELECTED\n"
                    f"🎯 {best['name']} FUTURE\n"
                    f"Breakout: {best['direction']}\n"
                    f"Score: {best['score']}\n"
                    f"Live margin: {margin_text}{live_note}\n"
                    f"Mode: FUTURE\n"
                    f"📄 PAPER MODE: ON"
                )
                print(f"AUTO SELECTED: {best['name']} FUTURE | score={best['score']} margin={margin_text}")
            else:
                mode = "OPTION"
                msg = (
                    f"🤖 AUTO SELECTED\n"
                    f"🎯 {best['symbol']} ATM OPTION\n"
                    f"Breakout: {best['direction']}\n"
                    f"Score: {best['score']}\n"
                    f"Future contract unavailable\n"
                    f"Mode: OPTION\n"
                    f"📄 PAPER MODE: ON"
                )
                print(f"AUTO SELECTED: {best['symbol']} ATM OPTION | score={best['score']}")

            telegram_send(msg)
            return mode, best["symbol"], best["key"]

        # Lightweight progress update every ~30 seconds, not every poll.
        if time.time() - last_status >= 30:
            active = [
                f"{x['symbol']} {x['direction'] or '-'} {max(x['up'], x['down'])}/{CONFIRM_CHECKS}"
                for x in candidates
            ]
            telegram_send(
                "👀 AUTO SELECT WATCHING\n" +
                (" | ".join(active) if active else "Waiting for market quotes...")
            )
            last_status = time.time()

        time.sleep(POLL_SECONDS)

    # No confirmed breakout anywhere by cutoff: safely choose NIFTY only as the
    # fallback instrument, but the engine will still require a valid breakout
    # before taking any paper trade.
    nifty = orb_map.get("NIFTY")
    if nifty:
        telegram_send(
            "⏰ AUTO SELECT\n"
            "No confirmed breakout across monitored indices before entry cutoff.\n"
            "Fallback: NIFTY ATM OPTION — NO TRADE unless a valid breakout exists."
        )
        return "OPTION", "NIFTY", nifty["key"]

    nifty_key = resolve_index_key("NIFTY")
    telegram_send("⛔ AUTO SELECT FAILED\nNo valid ORB data available.")
    return "OPTION", "NIFTY", nifty_key

def choose_mode():
    if INTEGRATED_MODE:
        # Telegram selection is persisted in MongoDB and reused after Railway restarts.
        st = load_state()
        sel = st.get("instrument_selection") if isinstance(st, dict) else None
        if isinstance(sel, dict) and sel.get("mode") and sel.get("symbol"):
            mode = str(sel["mode"]).upper()
            symbol = str(sel["symbol"]).upper()
            key = resolve_index_key(symbol)
            if key:
                print(f"TELEGRAM SELECTED: {symbol} | Mode: {mode}")
                telegram_send(f"🎯 TELEGRAM SELECTION\n{symbol}\nMode: {mode}\n📄 PAPER MODE: ON")
                return mode, symbol, key
        return auto_select_mode()
    print("\nSELECT INSTRUMENT"); print("1. AUTO SELECT (recommended)"); print("2. NIFTY ATM OPTION")
    for i,name in enumerate(FUTURE_UNDERLYINGS,3): print(f"{i}. {name}")
    choice=input("Choice [1-7] (Enter=Auto): ").strip() or "1"
    try: n=int(choice)
    except ValueError: n=1
    if n==1: return auto_select_mode()
    if n==2: return "OPTION","NIFTY",resolve_index_key("NIFTY")
    items=list(FUTURE_UNDERLYINGS.items())
    if 3<=n<=len(items)+2:
        name,(symbol,_)=items[n-3]; return "FUTURE",symbol,resolve_index_key(symbol)
    return auto_select_mode()

def instrument_search(query, instrument_type="FUT"):
    data = api_get(f"{BASE_URL}/instruments/search", {
        "query": query, "exchanges": "NSE", "segments": "FO",
        "instrument_types": instrument_type, "expiry": "current_month",
        "page_number": 1, "records": 30,
    })
    return data.get("data", []) if data else []

def select_future(symbol):
    data = instrument_search(symbol, "FUT")
    today = state_key()
    valid = [x for x in data if x.get("expiry") and x["expiry"] >= today
             and x.get("instrument_type") == "FUT"]
    if not valid:
        # fallback: search without current_month filter
        data = api_get(f"{BASE_URL}/instruments/search", {
            "query": symbol, "exchanges": "NSE", "segments": "FO",
            "instrument_types": "FUT", "page_number": 1, "records": 30,
        })
        valid = [x for x in (data.get("data", []) if data else [])
                 if x.get("expiry") and x["expiry"] >= today and x.get("instrument_type") == "FUT"]
    if not valid:
        return None
    valid.sort(key=lambda x: x["expiry"])
    x = valid[0]
    return {"instrument_key": x["instrument_key"], "symbol": x.get("trading_symbol", symbol),
            "lot_size": int(x.get("lot_size", 1)), "expiry": x.get("expiry"),
            "tick_size": float(x.get("tick_size", 0.05))}

def candles(instrument_key, minutes):
    data = api_get(f"{V3_URL}/historical-candle/intraday/{urllib.parse.quote(instrument_key, safe='')}/minutes/{minutes}")
    if not data:
        return []
    out=[]
    for c in data.get("data", {}).get("candles", []):
        try:
            ts=datetime.fromisoformat(c[0])
            if ts.tzinfo is None: ts=ts.replace(tzinfo=IST)
            out.append({"ts":ts.astimezone(IST),"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),"c":float(c[4]),"v":float(c[5])})
        except Exception:
            continue
    return sorted(out,key=lambda x:x["ts"])

def ema(values, period):
    if len(values)<period: return None
    k=2/(period+1); e=sum(values[:period])/period
    for v in values[period:]: e=v*k+e*(1-k)
    return e

def vwap_from(c):
    if not c: return None
    pv=sum(((x["h"]+x["l"]+x["c"])/3)*x["v"] for x in c)
    vol=sum(x["v"] for x in c)
    return pv/vol if vol else None

def atr_from(c, period=14):
    if len(c)<period+1: return None
    trs=[]
    for i in range(1,len(c)):
        trs.append(max(c[i]["h"]-c[i]["l"],abs(c[i]["h"]-c[i-1]["c"]),abs(c[i]["l"]-c[i-1]["c"])))
    return sum(trs[-period:])/period

def technical_bias(index_key):
    # Fast early-session validation: 1m data is primary; 15m is optional until enough candles exist.
    c1 = candles(index_key, 1)
    c5 = candles(index_key, 5)
    c15 = candles(index_key, 15)
    if len(c1) < 21:
        return {"ok": False, "reason": "Need at least 21 one-minute candles"}
    closes1 = [x["c"] for x in c1]
    e9 = ema(closes1, 9); e21 = ema(closes1, 21)
    vw = vwap_from(c1); atr = atr_from(c1, 14)
    if e9 is None or e21 is None or atr is None:
        return {"ok": False, "reason": "1m indicators unavailable"}
    e15 = ema([x["c"] for x in c15], 9) if len(c15) >= 9 else None
    last = closes1[-1]
    bull = (last > e9 > e21) and (vw is None or last > vw) and (e15 is None or last > e15)
    bear = (last < e9 < e21) and (vw is None or last < vw) and (e15 is None or last < e15)
    vol_now = c1[-1]["v"]; avg_vol = sum(x["v"] for x in c1[-11:-1]) / 10 if len(c1) >= 11 else 0
    volume_ok = bool(avg_vol and vol_now >= 1.10 * avg_vol)
    return {"ok": True, "last": last, "ema9": e9, "ema21": e21, "ema15": e15, "vwap": vw, "atr": atr, "bull": bull, "bear": bear, "volume_ok": volume_ok}


def _keyword_news_score(titles):
    positive=("surge","gain","rally","cut","easing","bullish","inflow","strong")
    negative=("fall","drop","crash","war","tariff","inflation","sell","outflow","hawkish")
    score=0
    for title in titles:
        t=title.lower()
        score += sum(1 for w in positive if w in t)-sum(1 for w in negative if w in t)
    return score


def gemini_news_analysis(titles):
    """Same job as ai_news_analysis() but calls Google's Gemini API instead of
    Anthropic's. Tried first if GEMINI_API_KEY is set. Called ONCE per day
    right before entry — never inside the fast tick loop, so it doesn't slow
    down breakout detection or SL/target monitoring. Returns None on any
    failure so the caller falls back to Anthropic (if configured) or the
    plain keyword score.
    """
    if not GEMINI_API_KEY_ENV or not titles:
        return None
    try:
        prompt = (
            "You are a terse markets analyst. Given these Indian/global market "
            "news headlines from the last few hours, classify the near-term "
            "bias for NIFTY as exactly one word: BULLISH, BEARISH, or NEUTRAL. "
            "Then give a confidence from 0.0 to 1.0. Reply ONLY as JSON: "
            '{"bias":"...","confidence":0.0,"reason":"<one short sentence>"}\n\n'
            "Headlines:\n" + "\n".join(f"- {t}" for t in titles[:10])
        )
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_NEWS_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY_ENV},
            headers={"content-type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 200},
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("Gemini news analysis HTTP %s: %s", r.status_code, r.text[:300])
            return None
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[4:] if text.lower().startswith("json") else text
            text = text.strip()
        parsed = json.loads(text)
        bias = str(parsed.get("bias", "")).upper()
        if bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
            return None
        return {"bias": bias, "confidence": float(parsed.get("confidence", 0.5)), "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as e:
        log.warning("Gemini news analysis failed, falling back: %s", e)
        return None


def ai_news_analysis(titles):
    """Optional LLM read of the same headlines used by the keyword scorer.

    Only runs if ANTHROPIC_API_KEY is set on the environment. Called ONCE per
    day right before entry (not inside the fast tick loop), so it never slows
    down breakout detection or SL/target monitoring — those still run on
    FAST_POLL_SECONDS regardless of this. Returns None on any failure so the
    caller can fall back to the plain keyword score.
    """
    if not ANTHROPIC_API_KEY_ENV or not titles:
        return None
    try:
        prompt = (
            "You are a terse markets analyst. Given these Indian/global market "
            "news headlines from the last few hours, classify the near-term "
            "bias for NIFTY as exactly one word: BULLISH, BEARISH, or NEUTRAL. "
            "Then give a confidence from 0.0 to 1.0. Reply ONLY as JSON: "
            '{"bias":"...","confidence":0.0,"reason":"<one short sentence>"}\n\n'
            "Headlines:\n" + "\n".join(f"- {t}" for t in titles[:10])
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY_ENV,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_NEWS_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("AI news analysis HTTP %s: %s", r.status_code, r.text[:300])
            return None
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        text = text.strip("`").replace("json", "", 1).strip() if text.startswith("```") else text
        parsed = json.loads(text)
        bias = str(parsed.get("bias", "")).upper()
        if bias not in ("BULLISH", "BEARISH", "NEUTRAL"):
            return None
        return {"bias": bias, "confidence": float(parsed.get("confidence", 0.5)), "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as e:
        log.warning("AI news analysis failed, falling back to keyword score: %s", e)
        return None


def news_snapshot():
    # Public Google News RSS. Missing news can fail closed via NEWS_FAIL_CLOSED.
    # If ANTHROPIC_API_KEY is set, an LLM read of the same headlines is used for
    # the bias call; otherwise (or if that call fails) the keyword scorer decides.
    if not USE_NEWS_FILTER: return {"score":0,"headline":"News filter disabled"}
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    queries=["Nifty India stock market", "RBI India markets", "Fed US markets", "crude oil India markets"]
    titles=[]
    for q in queries:
        try:
            u="https://news.google.com/rss/search?q="+quote(q)+"&hl=en-IN&gl=IN&ceid=IN:en"
            r=requests.get(u,timeout=6); r.raise_for_status()
            root=ET.fromstring(r.text)
            for item in root.findall("./channel/item")[:3]:
                titles.append(item.findtext("title") or "")
        except Exception as e:
            log.warning("News fetch failed: %s",e)
    score = _keyword_news_score(titles)
    bias = "BULLISH" if score>=NEWS_MIN_SCORE else "BEARISH" if score<=-NEWS_MIN_SCORE else "NEUTRAL"
    ai = gemini_news_analysis(titles) or ai_news_analysis(titles)
    source = "keyword"
    if ai:
        bias = ai["bias"]
        source = f"ai({ai['confidence']:.2f})"
    return {"score":score,"bias":bias,"titles":titles[:6],"source":source,"ai_reason":(ai or {}).get("reason","")}

def margin_required(instrument_key, quantity, transaction_type="BUY"):
    data=None
    try:
        r=requests.post(f"{BASE_URL}/charges/margin",headers=headers(),json={"instruments":[{"instrument_key":instrument_key,"quantity":quantity,"transaction_type":transaction_type,"product":"D"}]},timeout=8)
        if r.status_code==200: data=r.json()
    except requests.RequestException as e: log.warning("Margin check failed: %s",e)
    try: return float(data["data"]["required_margin"]) if data else None
    except Exception: return None

def cooldown_ok(state):
    ts = state.get("last_exit_ts")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=IST)
        elapsed = (now() - last.astimezone(IST)).total_seconds() / 60.0
        return elapsed >= COOLDOWN_MINUTES
    except Exception:
        return True


def risk_guard(state, estimated_capital):
    realized = float(state.get("realized_pnl", 0.0))
    if realized <= -DAILY_LOSS_LIMIT:
        return False, f"daily loss limit hit: ₹{realized:.2f}"
    weekly_pnl = float(load_weekly_state().get("pnl", 0.0))
    if weekly_pnl <= -WEEKLY_LOSS_LIMIT:
        return False, f"weekly loss limit hit: ₹{weekly_pnl:.2f} (cap ₹{WEEKLY_LOSS_LIMIT:.2f})"
    if not cooldown_ok(state):
        return False, f"cooldown active ({COOLDOWN_MINUTES} min)"
    if estimated_capital > MAX_CAPITAL * (1.0 + MARGIN_BUFFER_PCT/100.0):
        return False, "capital/margin buffer exceeded"
    return True, "OK"


def calculate_trade_plan(mode, entry, quantity, spot, atr, tick_size=0.05):
    """Return side, SL, target, expected max loss and reward/risk."""
    atr = float(atr or 0)
    if mode == "FUTURE":
        # For futures, risk is defined in rupees, not a premium percentage.
        max_points = MAX_RISK_PER_TRADE / max(quantity, 1)
        atr_points = atr * ATR_SL_MULT
        sl_points = min(max_points, atr_points) if atr_points > 0 else max_points
        sl_points = max(sl_points, tick_size)
        target_points = max(sl_points * MIN_RR, atr * ATR_TARGET_MULT if atr else sl_points * MIN_RR)
        return sl_points, target_points
    else:
        # Option premium risk cannot exceed the configured rupee budget.
        max_loss_per_unit = MAX_RISK_PER_TRADE / max(quantity, 1)
        sl_points = min(entry * STOPLOSS_PCT/100.0, max_loss_per_unit)
        sl_points = max(sl_points, tick_size)
        target_points = max(sl_points * MIN_RR, entry * TARGET_PCT/100.0)
        return sl_points, target_points


def market_regime(tech):
    atr = tech.get("atr") or 0
    last = tech.get("last") or 0
    if not atr or not last:
        return "UNKNOWN"
    pct = atr / last * 100
    if pct > 1.5:
        return "HIGH_VOL"
    if pct < 0.12:
        return "LOW_VOL"
    if tech.get("bull") or tech.get("bear"):
        return "TREND"
    return "SIDEWAYS"


def direction_from_orb(direction):
    return "BUY" if direction == "CE" else "SELL"


def pnl_rupees(side, entry, ltp, quantity):
    return (ltp-entry)*quantity if side == "BUY" else (entry-ltp)*quantity


def _partial_book_qty(quantity, lot_size):
    """Whole-lot quantity to book at the partial-profit trigger, or 0 if the
    position is too small to split into two whole-lot pieces (e.g. LOTS=1)."""
    if not PARTIAL_BOOK_ENABLE or not lot_size:
        return 0
    lots_total = quantity // lot_size
    lots_book = int(lots_total * PARTIAL_BOOK_FRACTION)
    return lots_book * lot_size if lots_total >= 2 and lots_book >= 1 else 0


def _record_exit(state, option, mode, side, entry, ltp, qty, pnl, reason, pos_meta, extra=None):
    """Shared bookkeeping for every exit event (partial, SL, target, square-off)."""
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
    state["last_exit_ts"] = now().isoformat()
    add_weekly_pnl(pnl)
    if pnl >= 0: state["wins"] = state.get("wins", 0) + 1
    else: state["losses"] = state.get("losses", 0) + 1
    record = {"date":state.get("date"),"time":now().strftime("%H:%M:%S"),"symbol":option["symbol"],"mode":mode,
              "side":side,"quantity":qty,"entry":entry,"exit":ltp,"pnl":pnl,"reason":reason,
              "score":pos_meta.get("score",""),"regime":pos_meta.get("regime",""),"news":pos_meta.get("news",""),
              "rr":pos_meta.get("rr",""),"max_loss":pos_meta.get("max_loss",""),
              "entry_slippage_pct":pos_meta.get("entry_slippage_pct",""),
              "signal_latency_ms":pos_meta.get("signal_latency_ms","")}
    if extra: record.update(extra)
    journal_trade(record)


def paper_monitor_advanced(option, entry, quantity, mode, side, sl, target, state):
    peak = entry
    trough = entry
    remaining_qty = quantity
    partial_done = False
    partial_pnl_total = 0.0
    lot_size = int((state.get("position") or {}).get("lot_size") or option.get("lot_size") or quantity)
    partial_qty = _partial_book_qty(quantity, lot_size)
    partial_trigger = (entry + (target - entry) * PARTIAL_BOOK_TRIGGER_PCT/100) if side == "BUY" else \
                       (entry - (entry - target) * PARTIAL_BOOK_TRIGGER_PCT/100)

    while now().time() < SQUARE_OFF_TIME:
        cmd = telegram_poll_commands(state)
        if cmd == "/status":
            telegram_status(state)
        elif cmd == "/risk":
            telegram_send(f"🛡️ RISK\nPer trade: ₹{MAX_RISK_PER_TRADE:.2f}\nDaily limit: ₹{DAILY_LOSS_LIMIT:.2f}\nWeekly limit: ₹{WEEKLY_LOSS_LIMIT:.2f}\nDaily PnL: ₹{float(state.get('realized_pnl',0)):+.2f}")
        elif cmd == "/today":
            telegram_status(state)
        elif cmd == "/pause":
            state["paper_enabled"] = False
            save_state(state)
            telegram_send("⏸️ PAPER ENTRY PAUSED. Existing paper position will continue to be monitored.")
        ltp = get_ltp(option["instrument_key"])
        if ltp is None:
            time.sleep(FAST_POLL_SECONDS)
            continue
        peak = max(peak, ltp)
        trough = min(trough, ltp)
        pnl = pnl_rupees(side, entry, ltp, remaining_qty)

        # Partial profit booking: lock in gains on part of the position once
        # price is partway to target, then move SL on the rest to breakeven.
        if partial_qty and not partial_done:
            reached = (side == "BUY" and ltp >= partial_trigger) or (side == "SELL" and ltp <= partial_trigger)
            if reached:
                partial_pnl = pnl_rupees(side, entry, ltp, partial_qty)
                remaining_qty -= partial_qty
                partial_done = True
                partial_pnl_total = partial_pnl
                pos_meta = dict(state.get("position") or {})
                _record_exit(state, option, mode, side, entry, ltp, partial_qty, partial_pnl,
                             "PARTIAL_BOOK", pos_meta, extra={"note": f"{PARTIAL_BOOK_FRACTION*100:.0f}% booked at {PARTIAL_BOOK_TRIGGER_PCT:.0f}% to target"})
                if PARTIAL_BOOK_SL_TO_ENTRY:
                    sl = entry
                save_state(state)
                telegram_send(f"💰 PARTIAL BOOK\n{option['symbol']}\nBooked qty: {partial_qty}\nLTP ₹{ltp:.2f}\nPartial PnL ₹{partial_pnl:+.2f}\nRemaining qty: {remaining_qty}\nSL moved to breakeven: {'yes' if PARTIAL_BOOK_SL_TO_ENTRY else 'no'}")
                pnl = pnl_rupees(side, entry, ltp, remaining_qty)

        # Trailing stop, direction-aware.
        if side == "BUY":
            if ltp >= entry * (1 + TRAIL_TRIGGER_PCT/100):
                sl = max(sl, peak * (1 - TRAIL_GIVEBACK_PCT/100))
            hit_sl = ltp <= sl
            hit_target = ltp >= target
        else:
            if ltp <= entry * (1 - TRAIL_TRIGGER_PCT/100):
                sl = min(sl, trough * (1 + TRAIL_GIVEBACK_PCT/100))
            hit_sl = ltp >= sl
            hit_target = ltp <= target

        if hit_sl or hit_target:
            pos_meta = dict(state.get("position") or {})
            state["position"] = None
            label = "TARGET HIT" if hit_target else "SL HIT"
            _record_exit(state, option, mode, side, entry, ltp, remaining_qty, pnl, label, pos_meta,
                         extra={"note": "post-partial" if partial_done else ""})
            save_state(state)
            total_pnl = pnl + partial_pnl_total
            telegram_send(f"{'🎯' if hit_target else '🔴'} {mode} {label}\n{option['symbol']}\nSide: {side}\nLTP ₹{ltp:.2f}\nLeg PnL ₹{pnl:+.2f}\nTrade total PnL ₹{total_pnl:+.2f}\nDaily PnL ₹{state['realized_pnl']:+.2f}")
            return
        if state.get("realized_pnl", 0) <= -DAILY_LOSS_LIMIT:
            telegram_send(f"🛑 DAILY KILL SWITCH\nDaily PnL ₹{state['realized_pnl']:+.2f}\nNo more trades today.")
            return
        if weekly_loss_limit_hit():
            telegram_send(f"🛑 WEEKLY KILL SWITCH\nThis week's PnL ≤ -₹{WEEKLY_LOSS_LIMIT:.0f}.\nNo more new trades this week; existing position keeps being monitored to exit.")
        time.sleep(FAST_POLL_SECONDS)
    ltp = get_ltp(option["instrument_key"])
    if ltp:
        pnl = pnl_rupees(side, entry, ltp, remaining_qty)
        pos_meta = dict(state.get("position") or {})
        state["position"] = None
        _record_exit(state, option, mode, side, entry, ltp, remaining_qty, pnl, "SQUARE_OFF", pos_meta,
                     extra={"note": "post-partial" if partial_done else ""})
        save_state(state)
        total_pnl = pnl + partial_pnl_total
        telegram_send(f"⏰ {mode} SQUARE-OFF\n{option['symbol']}\nSide: {side}\nLTP ₹{ltp:.2f}\nLeg PnL ₹{pnl:+.2f}\nTrade total PnL ₹{total_pnl:+.2f}\nDaily PnL ₹{state['realized_pnl']:+.2f}")


# ===================== V7 SAFETY / ANALYTICS =====================
def journal_trade(record):
    fields = [
        "date","time","symbol","mode","side","quantity","entry","exit",
        "pnl","reason","score","regime","news","rr","max_loss",
        "entry_slippage_pct","signal_latency_ms","note"
    ]
    exists = TRADE_JOURNAL_FILE.exists()
    try:
        with TRADE_JOURNAL_FILE.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if not exists:
                w.writeheader()
            w.writerow({k: record.get(k, "") for k in fields})
        if INTEGRATED_MODE and MONGODB_URI_ENV and TELEGRAM_CHAT_ID_ENV:
            try:
                from db import save_paper_trade
                remote = dict(record)
                remote["telegram_chat_id"] = TELEGRAM_CHAT_ID_ENV
                save_paper_trade(remote)
            except Exception as e:
                log.warning("Mongo journal write failed: %s", e)
    except Exception as e:
        log.warning("Journal write failed: %s", e)


def signal_score(direction, tech, news, regime, orb_range, spot):
    """Deterministic score; not a probability."""
    bullish = direction == "CE"
    score = 0
    reasons = []
    if (bullish and tech.get("bull")) or ((not bullish) and tech.get("bear")):
        score += 2; reasons.append("MTF trend")
    if tech.get("volume_ok"):
        score += 2; reasons.append("volume")
    vw = tech.get("vwap")
    if vw is None or (bullish and spot > vw) or ((not bullish) and spot < vw):
        score += 1; reasons.append("VWAP")
    e9, e21 = tech.get("ema9"), tech.get("ema21")
    if e9 and e21 and ((bullish and e9 > e21) or ((not bullish) and e9 < e21)):
        score += 1; reasons.append("EMA")
    if regime == "TREND":
        score += 1; reasons.append("trend regime")
    if news.get("bias") == "NEUTRAL":
        score += 1; reasons.append("neutral news")
    elif (bullish and news.get("bias") == "BULLISH") or ((not bullish) and news.get("bias") == "BEARISH"):
        score += 1; reasons.append("news aligned")
    atr = tech.get("atr") or 0
    atr_pct = (atr / spot * 100) if atr and spot else 0
    if MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT:
        score += 1; reasons.append("ATR normal")
    return score, reasons, atr_pct


def entry_quality(high, low, spot):
    rng = high - low
    if rng <= 0 or spot <= 0:
        return False, "invalid ORB range"
    pct = rng / spot * 100
    if pct > MAX_ORB_RANGE_PCT:
        return False, f"ORB range too wide ({pct:.2f}%)"
    return True, f"ORB range {pct:.2f}%"


def slippage_ok(instrument_key, reference, max_pct=MAX_ENTRY_CHASE_PCT):
    """Re-quote immediately before paper entry to avoid chasing stale LTP."""
    fresh = get_ltp(instrument_key)
    if fresh is None or fresh <= 0:
        return False, None, "fresh LTP unavailable"
    deviation = abs(fresh - reference) / reference * 100 if reference else 999
    if deviation > max_pct:
        return False, fresh, f"entry chase {deviation:.2f}% > {max_pct:.2f}%"
    return True, fresh, f"entry deviation {deviation:.2f}%"


def emergency_data_ok(tech, spot):
    if not tech.get("ok") or not spot or spot <= 0:
        return False, "bad market data"
    if not all(math.isfinite(float(tech.get(k) or 0)) for k in ("last", "ema9", "ema21")):
        return False, "non-finite technical data"
    return True, "OK"


def telegram_poll_commands(state=None):
    """Local CLI mode command polling; Railway integrated mode is handled by oauth_service."""
    if INTEGRATED_MODE:
        return None
    cfg = load_telegram_config()
    if not cfg:
        return None
    try:
        offset = int(TELEGRAM_OFFSET_FILE.read_text().strip()) if TELEGRAM_OFFSET_FILE.exists() else 0
    except Exception:
        offset = 0
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{cfg['bot_token']}/getUpdates",
            params={"timeout": 1, "offset": offset + 1, "limit": 20}, timeout=4
        )
        data = r.json()
        last_cmd = None
        allowed_chat = str(cfg.get("chat_id", "")).strip()
        for u in data.get("result", []):
            TELEGRAM_OFFSET_FILE.write_text(str(u["update_id"]))
            msg = u.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", "")).strip()
            if allowed_chat and chat_id != allowed_chat:
                continue
            text = (msg.get("text") or "").strip().lower().split()[0] if (msg.get("text") or "").strip() else ""
            if text in ("/status", "/today", "/risk", "/pause", "/resume", "/paper_on", "/paper_off", "/mode", "/help"):
                last_cmd = text
                if state is not None:
                    if text == "/paper_on":
                        state["paper_enabled"] = True
                        save_state(state)
                        telegram_send("🟢 PAPER MODE: ON\nNew paper entries are ENABLED. Live trading remains OFF.")
                    elif text == "/paper_off":
                        state["paper_enabled"] = False
                        save_state(state)
                        telegram_send("🔴 PAPER MODE: OFF\nNew entries are DISABLED. This does NOT enable live trading.")
                    elif text == "/resume":
                        state["paper_enabled"] = True
                        save_state(state)
                        telegram_send("▶️ RESUMED\nPAPER MODE: ON\nLive trading remains OFF.")
                    elif text == "/mode":
                        enabled = state.get("paper_enabled", PAPER_MODE)
                        telegram_send(f"⚙️ MODE\nPAPER MODE: {'ON' if enabled else 'OFF'}\nLIVE MODE: OFF")
                    elif text == "/help":
                        telegram_send("🤖 V7.4 COMMANDS\n/status - bot/trade status\n/today - today's stats\n/risk - risk limits\n/paper_on - enable paper entries\n/paper_off - disable new paper entries\n/mode - show mode\n/pause - pause new entry\n/resume - resume paper mode\n/help - commands")
        return last_cmd
    except Exception as e:
        log.debug("Telegram command poll: %s", e)
    return None


def send_daily_summary(force=False):
    """Send one end-of-day summary per IST trading day."""
    if now().time() < DAILY_SUMMARY_TIME and not force:
        return False
    state = load_state()
    if state.get("daily_summary_sent") and not force:
        return False
    pnl = float(state.get("realized_pnl", 0.0))
    trades = int(state.get("trades", 0))
    wins = int(state.get("wins", 0))
    losses = int(state.get("losses", 0))
    pos = state.get("position")
    weekly_pnl = float(load_weekly_state().get("pnl", 0.0))
    text = (
        "📊 TODAY'S MARKET SUMMARY\n"
        f"Date: {state.get('date', state_key())}\n"
        f"Trades: {trades}/{MAX_TRADES_PER_DAY}\n"
        f"Wins/Losses: {wins}/{losses}\n"
        f"Today's P&L: ₹{pnl:+.2f}\n"
        f"Week-to-date P&L: ₹{weekly_pnl:+.2f} (limit ₹{WEEKLY_LOSS_LIMIT:.0f})\n"
        f"Position: {'OPEN' if pos else 'CLOSED'}\n"
        "📄 PAPER MODE: ON\n"
        "🔒 LIVE TRADING: OFF"
    )
    if telegram_send(text):
        state["daily_summary_sent"] = True
        save_state(state)
        return True
    return False


def send_weekly_summary(force=False):
    """Send one summary per ISO week, meant to be called after Friday's close."""
    wk = load_weekly_state()
    if wk.get("summary_sent") and not force:
        return False
    pnl = float(wk.get("pnl", 0.0))
    text = (
        "📅 WEEKLY SUMMARY\n"
        f"Week: {wk.get('week', week_key())}\n"
        f"Week P&L: ₹{pnl:+.2f}\n"
        f"Weekly loss limit: ₹{WEEKLY_LOSS_LIMIT:.0f}\n"
        "📄 PAPER MODE — no real money involved\n"
        "🔒 LIVE TRADING: OFF"
    )
    if telegram_send(text):
        wk["summary_sent"] = True
        save_weekly_state(wk)
        return True
    return False


def telegram_status(state):
    weekly_pnl = float(load_weekly_state().get("pnl", 0.0))
    telegram_send(
        "📊 V7 STATUS\n"
        f"Trades: {state.get('trades',0)}/{MAX_TRADES_PER_DAY}\n"
        f"Daily PnL: ₹{float(state.get('realized_pnl',0)):+.2f}\n"
        f"Week-to-date PnL: ₹{weekly_pnl:+.2f}\n"
        f"Wins/Losses: {state.get('wins',0)}/{state.get('losses',0)}\n"
        f"Position: {'OPEN' if state.get('position') else 'NONE'}\n"
        f"Paper mode: {'ON' if state.get('paper_enabled', PAPER_MODE) else 'OFF'}\n"
        "Live mode: OFF"
    )


# ===================== MAIN =====================
def run_advanced_bot():
    print("\n"+"="*65); print("SELECT INSTRUMENT"); print("="*65)
    if now().time()<RANGE_START:
        print(f"\nBot started at {now().strftime('%H:%M:%S')}. Waiting until 09:15...")
        telegram_send("⏳ V7.4 WAITING\nBot started before 09:15. Waiting for 09:15 ORB start.")
        wait_until(RANGE_START)
    mode,symbol,index_key=choose_mode(); print(f"\nSelected: {symbol} | Mode: {mode}")
    if not index_key: telegram_send(f"⛔ {symbol}: Upstox index key unavailable. NO TRADE."); return
    state=load_state(); state.setdefault("paper_enabled", PAPER_MODE); save_state(state); print(f"State: date={state.get('date')} trades={state.get('trades',0)} daily_pnl=₹{state.get('realized_pnl',0):+.2f}"); print(f"Current IST time: {now().strftime('%Y-%m-%d %H:%M:%S')}")
    telegram_poll_commands(state)
    if not state.get("paper_enabled", PAPER_MODE):
        telegram_send("🔴 V7.4 STARTED\nPAPER MODE: OFF\nNo new entries. Live trading is OFF.")
        return
    if state.get("position") or state.get("trades",0)>=MAX_TRADES_PER_DAY: telegram_send("🛑 DAILY TRADE STATE BLOCKED\nExisting position or daily trade limit reached."); return
    if float(state.get("realized_pnl",0.0))<=-DAILY_LOSS_LIMIT: telegram_send("🛑 DAILY KILL SWITCH ACTIVE. NO TRADE."); return
    if weekly_loss_limit_hit(): telegram_send(f"🛑 WEEKLY KILL SWITCH ACTIVE\nThis week's PnL ≤ -₹{WEEKLY_LOSS_LIMIT:.0f}. NO TRADE."); return
    if not cooldown_ok(state): print(f"❄️ Cooldown active. Wait {COOLDOWN_MINUTES} min after last exit."); return
    if now().time()>=ENTRY_CUTOFF: telegram_send(f"ℹ️ V7.4 CHECKED\nSelected: {symbol}\nMode: {mode}\nEntry window closed — no trade today."); return
    telegram_send(f"🤖 V7.4 STARTED\nSelected: {symbol}\nMode: {mode}\nPAPER MODE: {'ON' if state.get('paper_enabled', PAPER_MODE) else 'OFF'}\nLIVE MODE: OFF")
    cached=AUTO_ORB_CACHE.get(symbol)
    high,low=cached if cached else opening_range_for_key(index_key)
    if high is None or low is None: return
    if cached: telegram_send(f"📊 ORB RANGE\n{symbol}\nHigh: {high:.2f}\nLow: {low:.2f}")
    # After a confirmed breakout, keep the signal alive while technical data loads.
    direction, spot = find_breakout_for_key(high, low, index_key)
    breakout_ts = time.perf_counter()  # moment the breakout got confirmed -> used for signal-to-fill latency
    if not direction:
        telegram_send(f"ℹ️ {symbol}\nNo valid ORB breakout before {ENTRY_CUTOFF.strftime('%H:%M')}.")
        return

    tech_wait_notice_sent = False
    tech = None
    while now().time() < ENTRY_CUTOFF:
        tech = technical_bias(index_key)
        if tech.get("ok"):
            break
        if not tech_wait_notice_sent:
            telegram_send(
                f"⏳ {symbol}\nTechnical data temporarily insufficient.\n"
                f"WAITING — keeping the confirmed {direction} breakout until {ENTRY_CUTOFF.strftime('%H:%M')}."
            )
            tech_wait_notice_sent = True
        time.sleep(10)
    if not tech or not tech.get("ok"):
        telegram_send(f"⛔ {symbol}\nTechnical data still unavailable by {ENTRY_CUTOFF.strftime('%H:%M')}. NO TRADE.")
        return

    regime = market_regime(tech)
    if REGIME_FILTER and regime in ("SIDEWAYS","LOW_VOL","HIGH_VOL","UNKNOWN"): telegram_send(f"⛔ {symbol}\nMarket regime: {regime}. NO TRADE."); return
    good_data,data_reason=emergency_data_ok(tech,spot)
    if not good_data: telegram_send(f"🛑 DATA SAFETY\n{symbol}: {data_reason}\nNO TRADE."); return
    good_range,range_reason=entry_quality(high,low,spot)
    if not good_range: telegram_send(f"⛔ {symbol}\n{range_reason}\nNO TRADE."); return
    news=news_snapshot()
    if USE_NEWS_FILTER and NEWS_FAIL_CLOSED and not news.get("titles"): telegram_send("⛔ NEWS DATA UNAVAILABLE\nFail-closed safety: NO TRADE."); return
    score,reasons,atr_pct=signal_score(direction,tech,news,regime,high-low,spot)
    news_note = f" ({news.get('source')})" if news.get("source") else ""
    news_reason = f"\n🤖 {news['ai_reason']}" if news.get("ai_reason") else ""
    telegram_send("📰 NEWS: %s%s%s\n%s"%(news.get("bias","NEUTRAL"),news_note,news_reason,"\n".join(news.get("titles",[])[:4]) or "No headlines"))
    if score<SIGNAL_MIN_SCORE: telegram_send(f"⛔ {symbol} SIGNAL REJECTED\nScore: {score}/{SIGNAL_MIN_SCORE}\nATR: {atr_pct:.2f}%\nReasons: {', '.join(reasons) or 'none'}"); return
    if mode=="OPTION":
        expiry=nearest_expiry(index_key); option=select_atm_option(direction,spot,expiry,index_key) if expiry else None
        if not option: telegram_send("⛔ ATM option contract unavailable. NO TRADE."); return
        quantity=option["lot_size"]*LOTS; reference_entry=get_ltp(option["instrument_key"])
        if reference_entry is None or reference_entry<=0: telegram_send("⛔ Option LTP unavailable. NO TRADE."); return
        ok_quote,entry,quote_reason=slippage_ok(option["instrument_key"],reference_entry)
        if not ok_quote: telegram_send(f"⛔ {symbol} ENTRY QUALITY\n{quote_reason}\nNO TRADE."); return
        entry_slippage_pct=abs(entry-reference_entry)/reference_entry*100 if reference_entry else 0.0
        estimated=entry*quantity; tick=0.05
    else:
        option=select_future(symbol)
        if not option: telegram_send(f"⛔ {symbol}: nearest FUT contract not found."); return
        quantity=option["lot_size"]*LOTS; reference_entry=get_ltp(option["instrument_key"])
        if reference_entry is None or reference_entry<=0: telegram_send(f"⛔ {symbol}: futures LTP unavailable. NO TRADE."); return
        ok_quote,entry,quote_reason=slippage_ok(option["instrument_key"],reference_entry)
        if not ok_quote: telegram_send(f"⛔ {symbol} ENTRY QUALITY\n{quote_reason}\nNO TRADE."); return
        entry_slippage_pct=abs(entry-reference_entry)/reference_entry*100 if reference_entry else 0.0
        estimated=margin_required(option["instrument_key"],quantity)
        if estimated is None:
            if PAPER_MODE and PAPER_FUTURES_IGNORE_MARGIN:
                estimated = 0.0
            else:
                telegram_send(f"⛔ {symbol}: margin check failed. NO TRADE."); return
        tick=option.get("tick_size",0.05)
    if not state.get("paper_enabled", PAPER_MODE):
        telegram_send("🔴 PAPER MODE OFF\nEntry cancelled. Live trading remains OFF.")
        return
    # Futures are simulated in PAPER mode even when their live margin is above
    # the user's ₹10,000 reference capital.  Risk is still hard-capped by the
    # ₹500 max-loss rule below.  Live trading remains locked OFF.
    cap_check = not (mode == "FUTURE" and PAPER_MODE and PAPER_FUTURES_IGNORE_MARGIN)
    ok,reason=risk_guard(state, float(estimated) if cap_check else 0.0)
    if not ok or (cap_check and float(estimated)>MAX_CAPITAL):
        msg=reason if not ok else f"Capital/margin ₹{estimated:.2f} > limit ₹{MAX_CAPITAL:.2f}"; telegram_send(f"🛑 RISK GUARD\n{symbol}\n{msg}\nNO TRADE."); return
    sl_points,target_points=calculate_trade_plan(mode,entry,quantity,spot,tech.get("atr"),tick); side=direction_from_orb(direction)
    sl=entry-sl_points if side=="BUY" else entry+sl_points; target=entry+target_points if side=="BUY" else entry-target_points
    max_loss=sl_points*quantity; rr=target_points/max(sl_points,tick)
    if max_loss>MAX_RISK_PER_TRADE+1e-6 or rr<MIN_RR: telegram_send(f"🛑 RISK REJECTED\nMax loss ₹{max_loss:.2f} / RR 1:{rr:.2f}\nNO TRADE."); return
    fill_ts = time.perf_counter()
    latency_ms = (fill_ts - breakout_ts) * 1000.0
    telegram_send(f"🚨 V7.4 PAPER MARKET ORDER\n{side} {option['symbol']}\nQty: {quantity}\nEntry: ₹{entry:.2f}\nSL: ₹{sl:.2f}\nTarget: ₹{target:.2f}\nMax loss: ₹{max_loss:.2f}\nRR: 1:{rr:.2f}\nORB: {direction}\nRegime: {regime}\nNews: {news.get('bias')}\nSlippage: {entry_slippage_pct:.2f}%\nMode: PAPER")
    state["trades"]=state.get("trades",0)+1; state["position"]={"symbol":option["symbol"],"instrument_key":option["instrument_key"],"quantity":quantity,"entry":entry,"side":side,"sl":sl,"target":target,"score":score,"regime":regime,"news":news.get("bias"),"rr":rr,"max_loss":max_loss,"lot_size":option.get("lot_size",quantity),"entry_slippage_pct":round(entry_slippage_pct,4),"signal_latency_ms":round(latency_ms,1)}; save_state(state)
    paper_monitor_advanced(option,entry,quantity,mode,side,sl,target,state)
    send_daily_summary()

def opening_range_for_key(index_key):
    log.info("ORB: using instrument key %s",index_key)
    if now().time()<RANGE_START: wait_until(RANGE_START)
    high=low=None
    if RANGE_START<=now().time()<RANGE_END:
        log.info("Tracking ORB 09:15-09:20")
        while now().time()<RANGE_END:
            p=get_ltp(index_key)
            if p is not None: high=p if high is None else max(high,p); low=p if low is None else min(low,p)
            time.sleep(POLL_SECONDS)
    elif now().time()>=RANGE_END:
        high,low=orb_from_1m_candles(index_key)
    if high is None or low is None: telegram_send("⛔ Opening range unavailable. NO TRADE."); return None,None
    telegram_send(f"📊 ORB RANGE\nHigh: {high:.2f}\nLow: {low:.2f}"); return high,low

def find_breakout_for_key(high,low,index_key):
    up=down=0; log.info("Watching ORB breakout until %s",ENTRY_CUTOFF.strftime("%H:%M"))
    while now().time()<ENTRY_CUTOFF:
        p=get_ltp(index_key)
        if p is None: time.sleep(FAST_POLL_SECONDS); continue
        if p>=high+MIN_BREAKOUT_POINTS: up+=1; down=0; log.info("UP breakout candidate %.2f (%d/%d)",p,up,CONFIRM_CHECKS)
        elif p<=low-MIN_BREAKOUT_POINTS: down+=1; up=0; log.info("DOWN breakout candidate %.2f (%d/%d)",p,down,CONFIRM_CHECKS)
        else: up=down=0
        if up>=CONFIRM_CHECKS: return "CE",p
        if down>=CONFIRM_CHECKS: return "PE",p
        cmd=telegram_poll_commands(load_state())
        if cmd=="/status": telegram_status(load_state())
        elif cmd=="/risk": telegram_send(f"🛡️ RISK\nMax trade risk: ₹{MAX_RISK_PER_TRADE:.0f}\nDaily loss limit: ₹{DAILY_LOSS_LIMIT:.0f}\nWeekly loss limit: ₹{WEEKLY_LOSS_LIMIT:.0f}")
        elif cmd=="/pause": telegram_send("⏸️ PAUSE ACKNOWLEDGED\nNo new paper entry will be taken. Use /paper_on or /resume to continue."); return None,None
        elif cmd=="/paper_off": return None,None
        if not load_state().get("paper_enabled", PAPER_MODE):
            return None,None
        time.sleep(FAST_POLL_SECONDS)
    return None,None

def main():
    print("\n"+"="*65); print("NIFTY ORB ADVANCED v7.4 | PAPER MODE"); print("="*65)
    config=load_config()
    print("[1/3] Config loaded.")
    tg_cfg = load_telegram_config()
    print("[2/3] Telegram setup checked.")
    if tg_cfg:
        if telegram_send("🔔 V7.4 Telegram connection test OK\nPAPER MODE: ON"):
            print("[TG] Telegram notification test: OK")
        else:
            print("[TG] Telegram notification test: FAILED — check Bot Token, Chat ID, and internet")
    else:
        print("[TG] Telegram disabled — Bot Token/Chat ID missing")
    if INTEGRATED_MODE:
        if not load_token():
            telegram_send("🔐 Upstox not connected. Use /login first.")
            return
        print("[3/3] MongoDB Upstox token loaded. Starting integrated paper bot...")
    else:
        get_token(config)
        print("[3/3] Upstox login complete. Starting instrument menu...")
    run_advanced_bot()

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: log.warning("Stopped manually.")
    except Exception as e: log.exception("FATAL ERROR: %s",e); print("\nERROR:",e)
