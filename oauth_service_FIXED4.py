import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import json
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from db import (
    save_oauth_state, consume_oauth_state, save_token, get_token, logout,
    load_state, set_paper_enabled, get_telegram_offset, save_telegram_offset,
    monthly_stats,
)

app = FastAPI(title="NIFTY ORB V7.4")

CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
AUTHORIZED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAPER_MODE = os.getenv("PAPER_MODE", "True").lower() == "true"
LIVE_TRADING = False  # hard safety lock

_engine_thread = None
_engine_lock = threading.Lock()


def tg(method, data=None):
    """Call Telegram without letting a Telegram 4xx kill OAuth/engine flow."""
    if not BOT_TOKEN:
        return {}
    payload = dict(data or {})
    # Telegram expects reply_markup as a JSON string when sent as form data.
    if isinstance(payload.get("reply_markup"), (dict, list)):
        payload["reply_markup"] = json.dumps(payload["reply_markup"], separators=(",", ":"))
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=payload, timeout=20,
        )
        try:
            body = r.json()
        except Exception:
            body = {"ok": False, "description": r.text[:500]}
        if not r.ok or not body.get("ok", False):
            print(f"[Telegram] {method} HTTP {r.status_code}: {body}")
            return body
        return body
    except requests.RequestException as e:
        print(f"[Telegram] network error: {e}")
        return {"ok": False, "description": str(e)}


def send(chat_id, text, reply_markup=None):
    data = {"chat_id": str(chat_id), "text": str(text)}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return tg("sendMessage", data)


def instrument_keyboard():
    # Inline buttons let the user select the instrument directly from Telegram.
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 NIFTY", "callback_data": "sel:NIFTY:OPTION"},
                {"text": "🟢 NIFTY FUTURE", "callback_data": "sel:NIFTY:FUTURE"},
            ],
            [
                {"text": "🔵 BANKNIFTY", "callback_data": "sel:BANKNIFTY:OPTION"},
                {"text": "🔵 BANKNIFTY FUTURE", "callback_data": "sel:BANKNIFTY:FUTURE"},
            ],
            [
                {"text": "🤖 AUTO SELECT", "callback_data": "sel:AUTO:AUTO"},
            ],
        ]
    }


def selection_text(st):
    sel = st.get("instrument_selection") if isinstance(st, dict) else None
    if isinstance(sel, dict):
        mode = str(sel.get("mode", "")).upper()
        symbol = str(sel.get("symbol", "")).upper()
        if symbol and mode and mode != "AUTO":
            return f"🎯 Selected: {symbol} | {mode}"
    return "🤖 Selected: AUTO SELECT"


def send_instrument_menu(chat_id):
    st = state_for(chat_id)
    send(
        chat_id,
        "📊 SELECT INSTRUMENT\n\n"
        "Choose what the PAPER bot should trade next session:\n"
        "• NIFTY / BANKNIFTY = ATM Option\n"
        "• FUTURE = nearest eligible Future\n"
        "• AUTO = bot decides using breakout + ₹10,000 limit\n\n"
        + selection_text(st),
        instrument_keyboard(),
    )


def save_selection(chat_id, symbol, mode):
    st = state_for(chat_id) or {}
    if symbol == "AUTO":
        st["instrument_selection"] = {"mode": "AUTO", "symbol": ""}
    else:
        st["instrument_selection"] = {"mode": mode, "symbol": symbol}
    set_paper_enabled(chat_id, True)
    from db import save_state as db_save_state
    db_save_state(chat_id, st)
    return st


def login_url(chat_id):
    state = secrets.token_urlsafe(32)
    save_oauth_state(state, chat_id)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return "https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params)


def engine_is_alive():
    return bool(_engine_thread and _engine_thread.is_alive())


def _today_key():
    return datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

def start_engine(explicit=False, force=False):
    """Start at most one normal engine session per trading day.

    A clean engine exit (for example LOW_VOL / NO TRADE) is terminal for the
    current session, so the supervisor must not restart it in a loop. Explicit
    /start, /resume or /paper_on can intentionally start a fresh session.
    """
    global _engine_thread
    with _engine_lock:
        if engine_is_alive() or not AUTHORIZED_CHAT_ID or not get_token(AUTHORIZED_CHAT_ID):
            return False
        st = state_for(AUTHORIZED_CHAT_ID)
        today = _today_key()
        session_date = st.get("engine_session_date")
        finished_date = st.get("engine_finished_date")
        crash_date = st.get("engine_crash_date")
        crash_count = int(st.get("engine_crash_count", 0) or 0)

        if explicit:
            st["engine_session_date"] = today
            st["engine_finished_date"] = None
            st["engine_crash_date"] = None
            st["engine_crash_count"] = 0
        elif not force:
            if session_date == today:
                return False
            st["engine_session_date"] = today
            st["engine_finished_date"] = None
            st["engine_crash_date"] = None
            st["engine_crash_count"] = 0
        else:
            # Supervisor may recover one unexpected crash per day.
            if session_date != today or finished_date == today or crash_date != today or crash_count >= 1:
                return False
            st["engine_crash_count"] = crash_count + 1

        from db import save_state as db_save_state
        db_save_state(AUTHORIZED_CHAT_ID, st)

        def runner():
            try:
                os.environ["RAILWAY_MODE"] = "1"
                from NIFTY_ORB_ADVANCED_v7_4 import run_advanced_bot
                run_advanced_bot()
                # Normal return means the day's engine cycle completed.
                done = state_for(AUTHORIZED_CHAT_ID)
                done["engine_finished_date"] = _today_key()
                from db import save_state as db_save_state
                db_save_state(AUTHORIZED_CHAT_ID, done)
            except Exception as e:
                failed = state_for(AUTHORIZED_CHAT_ID)
                failed["engine_crash_date"] = _today_key()
                failed["engine_crash_count"] = int(failed.get("engine_crash_count", 0) or 0)
                from db import save_state as db_save_state
                db_save_state(AUTHORIZED_CHAT_ID, failed)
                send(AUTHORIZED_CHAT_ID, f"❌ PAPER ENGINE STOPPED\n{type(e).__name__}: {e}")

        _engine_thread = threading.Thread(target=runner, name="paper-engine", daemon=True)
        _engine_thread.start()
        return True


def state_for(chat_id):
    st = load_state(chat_id) or {}
    return st


@app.get("/")
@app.get("/health")
def health():
    return {
        "status": "ok",
        "paper_mode": PAPER_MODE,
        "live_trading": LIVE_TRADING,
        "engine_alive": engine_is_alive(),
        "oauth_configured": bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI),
        "mongodb_configured": bool(os.getenv("MONGODB_URI")),
    }


@app.get("/login")
def browser_login(chat_id: str):
    if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
        return HTMLResponse("Upstox OAuth variables are missing.", status_code=500)
    if AUTHORIZED_CHAT_ID and str(chat_id) != AUTHORIZED_CHAT_ID:
        return HTMLResponse("Unauthorized.", status_code=403)
    return RedirectResponse(login_url(chat_id), status_code=302)


@app.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(f"Upstox authorization failed: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse("Missing code/state.", status_code=400)
    oauth = consume_oauth_state(state)
    if not oauth:
        return HTMLResponse("Invalid or expired OAuth state.", status_code=400)
    chat_id = str(oauth["telegram_chat_id"])
    if AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID:
        return HTMLResponse("Unauthorized.", status_code=403)
    try:
        r = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
            }, timeout=20,
        )
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text}
        if r.status_code >= 400 or "access_token" not in payload:
            send(chat_id, f"❌ Upstox login failed\nHTTP {r.status_code}\n{str(payload)[:800]}")
            return HTMLResponse("Token exchange failed.", status_code=400)
        save_token(chat_id, payload)
        set_paper_enabled(chat_id, True)
        send(chat_id, "✅ Upstox login successful.\n📄 PAPER MODE: ON\n🔒 LIVE TRADING: OFF\n🤖 Paper engine will start automatically.")
        send_instrument_menu(chat_id)
        start_engine(explicit=True)
        return HTMLResponse("<h2>Upstox login successful</h2><p>Paper engine will start automatically. You can close this page.</p>")
    except requests.RequestException as e:
        send(chat_id, f"❌ Upstox connection error: {e}")
        return HTMLResponse("Upstox connection error.", status_code=502)
    except Exception as e:
        print(f"OAuth callback error: {type(e).__name__}: {e}")
        try:
            send(chat_id, f"⚠️ Bot notification/setup error: {type(e).__name__}")
        except Exception:
            pass
        return HTMLResponse("Upstox login succeeded, but bot setup reported an error. Check Railway logs.", status_code=200)


def handle_update(update):
    # Handle inline-button clicks first.
    cb = update.get("callback_query") or {}
    if cb:
        msg = cb.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id or (AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID):
            return
        data = str(cb.get("data") or "")
        if data.startswith("sel:"):
            parts = data.split(":")
            if len(parts) == 3:
                symbol, mode = parts[1].upper(), parts[2].upper()
                if symbol == "AUTO":
                    save_selection(chat_id, "AUTO", "AUTO")
                    send(chat_id, "🤖 AUTO SELECT enabled.\nBot will choose the strongest market breakout. FUTURE is preferred for paper testing; live margin is checked and shown.\n📄 PAPER MODE: ON\n🔒 LIVE TRADING: OFF")
                elif symbol in ("NIFTY", "BANKNIFTY") and mode in ("OPTION", "FUTURE"):
                    save_selection(chat_id, symbol, mode)
                    label = f"{symbol} {'ATM OPTION' if mode == 'OPTION' else 'FUTURE'}"
                    send(chat_id, f"✅ INSTRUMENT SELECTED\n🎯 {label}\n📄 PAPER MODE: ON\n🔒 LIVE TRADING: OFF\n\nUse /start to start now, or /status to check.")
                    # Start only during the active/pre-market window. After cutoff it will start next session.
                    try:
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        if datetime.now(ZoneInfo("Asia/Kolkata")).time().replace(tzinfo=None) < __import__("datetime").time(11, 30):
                            start_engine(explicit=False)
                        else:
                            send(chat_id, "⏳ Entry window is closed. Selection saved for the next trading session.")
                    except Exception:
                        pass
            try:
                tg("answerCallbackQuery", {"callback_query_id": cb.get("id", "")})
            except Exception:
                pass
        return

    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip().lower().split()[0] if (msg.get("text") or "").strip() else ""
    if not chat_id or not text or (AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID):
        return
    if text == "/login":
        if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
            send(chat_id, "❌ Upstox OAuth is not configured on Railway.")
            return
        url = login_url(chat_id)
        send(chat_id, f"🔐 Upstox login:\n{url}")
    elif text in ("/select", "/instrument"):
        send_instrument_menu(chat_id)
    elif text == "/auto":
        save_selection(chat_id, "AUTO", "AUTO")
        send(chat_id, "🤖 AUTO SELECT enabled.\nBot will choose the strongest market breakout. FUTURE is preferred for paper testing; live margin is checked and shown.\n📄 PAPER MODE: ON\n🔒 LIVE TRADING: OFF")
    elif text == "/logout":
        set_paper_enabled(chat_id, False)
        logout(chat_id)
        send(chat_id, "🚪 Upstox session removed. PAPER entries OFF. Use /login to reconnect.")
    elif text in ("/paper_on", "/resume", "/start"):
        if not get_token(chat_id):
            send(chat_id, "🔐 Upstox not connected. Use /login first.")
            return
        set_paper_enabled(chat_id, True)
        start_engine(explicit=True)
        send(chat_id, "🟢 PAPER MODE: ON\nNew paper entries enabled.\n🔒 LIVE TRADING: OFF")
    elif text in ("/paper_off", "/pause", "/stop"):
        set_paper_enabled(chat_id, False)
        send(chat_id, "⏸️ PAPER ENTRIES: OFF\nExisting paper position is not force-closed.\n🔒 LIVE TRADING: OFF")
    elif text == "/mode":
        st = state_for(chat_id); token = get_token(chat_id)
        send(chat_id, f"⚙️ MODE\n📄 PAPER: {'ON' if st.get('paper_enabled', PAPER_MODE) else 'OFF'}\n🔒 LIVE: OFF\n🔑 Upstox: {'CONNECTED' if token else 'NOT CONNECTED'}\n🤖 Engine: {'RUNNING' if engine_is_alive() else 'STOPPED'}")
    elif text in ("/status", "/today"):
        st = state_for(chat_id); token = get_token(chat_id)
        send(chat_id, f"📊 NIFTY ORB V7.4\nTrades: {st.get('trades',0)}/1\nDaily PnL: ₹{float(st.get('realized_pnl',0)):+.2f}\nWins/Losses: {st.get('wins',0)}/{st.get('losses',0)}\nPosition: {'OPEN' if st.get('position') else 'NONE'}\nPaper: {'ON' if st.get('paper_enabled', PAPER_MODE) else 'OFF'}\nUpstox: {'CONNECTED' if token else 'NOT CONNECTED'}\nEngine: {'RUNNING' if engine_is_alive() else 'STOPPED'}\nLive: OFF")
    elif text == "/risk":
        send(chat_id, "🛡️ RISK\nMax trade risk: ₹500\nDaily loss limit: ₹600\nWeekly loss limit: ₹1,800\nMax capital: ₹10,000\nMax trades/day: 1\n🔒 LIVE TRADING: OFF")
    elif text == "/help":
        send(chat_id, "/login\n/select - instrument buttons\n/auto - auto select\n/start or /resume\n/stop or /pause\n/paper_on\n/paper_off\n/status\n/today\n/risk\n/mode\n/logout\n/help\n\n📄 PAPER ONLY — LIVE TRADING LOCKED OFF")


def telegram_worker():
    if not BOT_TOKEN:
        return
    offset = get_telegram_offset()
    while True:
        try:
            data = tg("getUpdates", {"timeout": 50, "offset": offset, "limit": 20})
            for u in data.get("result", []):
                offset = int(u["update_id"]) + 1
                save_telegram_offset(offset)
                handle_update(u)
        except Exception:
            time.sleep(2)


def send_end_of_day_summary():
    """Send one Telegram summary after the paper market session closes."""
    if not AUTHORIZED_CHAT_ID:
        return
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    current = datetime.now(ist)
    if current.time() < dtime(15, 20):
        return
    st = state_for(AUTHORIZED_CHAT_ID)
    if st.get("date") != current.strftime("%Y-%m-%d"):
        return
    if st.get("daily_summary_sent"):
        return
    pnl = float(st.get("realized_pnl", 0.0))
    trades = int(st.get("trades", 0))
    wins = int(st.get("wins", 0))
    losses = int(st.get("losses", 0))
    position = "OPEN" if st.get("position") else "CLOSED"
    text = (
        "📊 TODAY'S MARKET CLOSE SUMMARY\n"
        f"Date: {st.get('date')}\n"
        f"Trades: {trades}/1\n"
        f"Wins/Losses: {wins}/{losses}\n"
        f"Today's P&L: ₹{pnl:+.2f}\n"
        f"Position: {position}\n"
        "📄 PAPER MODE: ON\n"
        "🔒 LIVE TRADING: OFF"
    )
    try:
        send(AUTHORIZED_CHAT_ID, text)
        st["daily_summary_sent"] = True
        from db import save_state as db_save_state
        db_save_state(AUTHORIZED_CHAT_ID, st)
    except Exception as e:
        print("Daily summary error:", e)


def send_weekly_summary_service():
    """Friday after market close: one weekly PnL summary. Reads the same
    weekly_state doc the trading engine writes to via add_weekly_pnl()."""
    if not AUTHORIZED_CHAT_ID:
        return
    from datetime import datetime as dt, time as dtime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    current = dt.now(ist)
    if current.weekday() != 4 or current.time() < dtime(15, 25):  # Friday, after close
        return
    from db import load_weekly_state as db_load_weekly, save_weekly_state as db_save_weekly
    year, week, _ = current.isocalendar()
    wk_key = f"{year}-W{week:02d}"
    wk = db_load_weekly(AUTHORIZED_CHAT_ID) or {}
    if wk.get("week") != wk_key or wk.get("summary_sent"):
        return
    pnl = float(wk.get("pnl", 0.0))
    text = (
        "📅 WEEKLY SUMMARY\n"
        f"Week: {wk_key}\n"
        f"Week P&L: ₹{pnl:+.2f}\n"
        "📄 PAPER MODE — no real money involved\n"
        "🔒 LIVE TRADING: OFF"
    )
    try:
        send(AUTHORIZED_CHAT_ID, text)
        wk["summary_sent"] = True
        db_save_weekly(AUTHORIZED_CHAT_ID, wk)
    except Exception as e:
        print("Weekly summary error:", e)


def send_monthly_summary_service():
    """Last calendar day of the month, after market close: one monthly PnL
    rollup computed from the paper_trades journal (independent of daily/weekly
    state, so it works even across a Railway restart)."""
    if not AUTHORIZED_CHAT_ID:
        return
    from datetime import datetime as dt, time as dtime, timedelta
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    current = dt.now(ist)
    tomorrow = current + timedelta(days=1)
    is_last_day = tomorrow.month != current.month
    if not is_last_day or current.time() < dtime(15, 30):
        return
    ym = current.strftime("%Y-%m")
    st = state_for(AUTHORIZED_CHAT_ID)
    if st.get("monthly_summary_month") == ym:
        return
    stats = monthly_stats(AUTHORIZED_CHAT_ID, ym)
    win_rate = (stats["wins"] / stats["trades"] * 100) if stats["trades"] else 0.0
    text = (
        "🗓️ MONTHLY SUMMARY\n"
        f"Month: {ym}\n"
        f"Trades: {stats['trades']}\n"
        f"Wins/Losses: {stats['wins']}/{stats['losses']} ({win_rate:.0f}% win rate)\n"
        f"Month P&L: ₹{stats['pnl']:+.2f}\n"
        "📄 PAPER MODE — no real money involved\n"
        "🔒 LIVE TRADING: OFF"
    )
    try:
        send(AUTHORIZED_CHAT_ID, text)
        st["monthly_summary_month"] = ym
        from db import save_state as db_save_state
        db_save_state(AUTHORIZED_CHAT_ID, st)
    except Exception as e:
        print("Monthly summary error:", e)


def send_login_reminder():
    """Upstox access tokens expire at 3:30 AM daily, so a fresh /login is
    needed every trading day. Reminds once per day, before market open, if
    the bot isn't already connected."""
    if not AUTHORIZED_CHAT_ID or not BOT_TOKEN:
        return
    from datetime import datetime as dt, time as dtime
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    current = dt.now(ist)
    if current.weekday() >= 5:  # skip weekends
        return
    if not (dtime(8, 30) <= current.time() <= dtime(9, 10)):
        return
    if get_token(AUTHORIZED_CHAT_ID):
        return
    st = state_for(AUTHORIZED_CHAT_ID)
    today = current.strftime("%Y-%m-%d")
    if st.get("login_reminder_date") == today:
        return
    try:
        send(AUTHORIZED_CHAT_ID, "🔐 MORNING REMINDER\nUpstox token has expired (daily 3:30 AM reset).\nUse /login to reconnect before today's session starts.")
        st["login_reminder_date"] = today
        from db import save_state as db_save_state
        db_save_state(AUTHORIZED_CHAT_ID, st)
    except Exception as e:
        print("Login reminder error:", e)


def supervisor():
    # Keep the web service alive without restarting a normally-completed engine.
    # This prevents LOW_VOL / NO TRADE from causing an endless auto-select loop.
    while True:
        try:
            if AUTHORIZED_CHAT_ID and get_token(AUTHORIZED_CHAT_ID):
                st = state_for(AUTHORIZED_CHAT_ID)
                current = datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
                today = current.strftime("%Y-%m-%d")
                has_position = bool(st.get("position"))
                session_date = st.get("engine_session_date")
                finished_date = st.get("engine_finished_date")
                crash_date = st.get("engine_crash_date")
                crash_count = int(st.get("engine_crash_count", 0) or 0)

                if st.get("paper_enabled", PAPER_MODE) and not engine_is_alive():
                    # Start once per day before the entry cutoff. If the engine
                    # already completed normally, do not restart it.
                    if current.time() < __import__("datetime").time(11, 30) and session_date != today:
                        start_engine(explicit=False)
                    # Recover one unexpected crash, but never restart a clean exit.
                    elif (current.time() < __import__("datetime").time(11, 30)
                          and session_date == today
                          and crash_date == today
                          and finished_date != today
                          and crash_count < 1):
                        start_engine(force=True)
        except Exception as e:
            # Never kill the web service because the worker failed.
            if BOT_TOKEN and AUTHORIZED_CHAT_ID:
                try: send(AUTHORIZED_CHAT_ID, f"⚠️ Engine supervisor warning: {type(e).__name__}")
                except Exception: pass
        try:
            send_end_of_day_summary()
        except Exception:
            pass
        try:
            send_weekly_summary_service()
        except Exception:
            pass
        try:
            send_monthly_summary_service()
        except Exception:
            pass
        try:
            send_login_reminder()
        except Exception:
            pass
        time.sleep(30)


threading.Thread(target=telegram_worker, name="telegram-worker", daemon=True).start()
threading.Thread(target=supervisor, name="engine-supervisor", daemon=True).start()
