import os
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
DB_NAME = os.getenv("MONGODB_DB", "nifty_orb")

_client = None
_db = None

def db():
    global _client, _db
    if not MONGODB_URI:
        return None
    if _db is None:
        _client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        _db = _client[DB_NAME]
        _db.oauth_states.create_index([("created_at", ASCENDING)])
        _db.oauth_states.create_index([("state", ASCENDING)], unique=True)
        _db.users.create_index([("telegram_chat_id", ASCENDING)], unique=True)
        _db.paper_trades.create_index([("created_at", ASCENDING)])
        _db.paper_state.create_index([("telegram_chat_id", ASCENDING)], unique=True)
        _db.weekly_state.create_index([("telegram_chat_id", ASCENDING)], unique=True)
    return _db

def save_oauth_state(state, chat_id):
    d = db()
    if d is None:
        return
    d.oauth_states.insert_one({
        "state": state,
        "telegram_chat_id": str(chat_id),
        "created_at": datetime.now(timezone.utc),
    })

def consume_oauth_state(state):
    d = db()
    if d is None:
        return None
    doc = d.oauth_states.find_one_and_delete({"state": state})
    return doc

def save_token(chat_id, token_payload):
    d = db()
    if d is None:
        return
    d.users.update_one(
        {"telegram_chat_id": str(chat_id)},
        {"$set": {
            "telegram_chat_id": str(chat_id),
            "access_token": token_payload.get("access_token"),
            "token_type": token_payload.get("token_type"),
            "user_id": token_payload.get("user_id"),
            "expires_in": token_payload.get("expires_in"),
            "expires_at": token_payload.get("expires_at"),
            "login_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

def get_token(chat_id):
    d = db()
    if d is None:
        return None
    return d.users.find_one({"telegram_chat_id": str(chat_id)}, {"access_token": 1, "user_id": 1})

def logout(chat_id):
    d = db()
    if d is None:
        return
    d.users.update_one(
        {"telegram_chat_id": str(chat_id)},
        {"$unset": {"access_token": "", "token_type": "", "user_id": ""}}
    )


def load_state(chat_id):
    d = db()
    if d is None:
        return None
    return d.paper_state.find_one({"telegram_chat_id": str(chat_id)}, {"_id": 0})

def save_state(chat_id, state):
    d = db()
    if d is None:
        return False
    doc = dict(state)
    doc["telegram_chat_id"] = str(chat_id)
    doc["updated_at"] = datetime.now(timezone.utc)
    d.paper_state.update_one({"telegram_chat_id": str(chat_id)}, {"$set": doc}, upsert=True)
    return True

def set_paper_enabled(chat_id, enabled):
    d = db()
    if d is None:
        return False
    d.paper_state.update_one({"telegram_chat_id": str(chat_id)}, {"$set": {"telegram_chat_id": str(chat_id), "paper_enabled": bool(enabled), "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return True

def get_telegram_offset():
    d = db()
    if d is None:
        return 0
    doc = d.bot_meta.find_one({"key": "telegram_offset"}, {"value": 1})
    return int(doc.get("value", 0)) if doc else 0

def save_telegram_offset(offset):
    d = db()
    if d is None:
        return
    d.bot_meta.update_one({"key": "telegram_offset"}, {"$set": {"key": "telegram_offset", "value": int(offset), "updated_at": datetime.now(timezone.utc)}}, upsert=True)

def save_paper_trade(trade):
    d = db()
    if d is None:
        return
    doc = dict(trade)
    doc.setdefault("created_at", datetime.now(timezone.utc))
    d.paper_trades.insert_one(doc)

def recent_paper_trades(chat_id, limit=20):
    d = db()
    if d is None:
        return []
    return list(d.paper_trades.find({"telegram_chat_id": str(chat_id)}).sort("created_at", -1).limit(limit))


def monthly_stats(chat_id, year_month):
    """Aggregate paper_trades for one calendar month, e.g. year_month='2026-09'."""
    d = db()
    if d is None:
        return {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
    docs = d.paper_trades.find({
        "telegram_chat_id": str(chat_id),
        "date": {"$regex": f"^{year_month}"},
    })
    pnl, trades, wins, losses = 0.0, 0, 0, 0
    for t in docs:
        try:
            p = float(t.get("pnl", 0) or 0)
        except Exception:
            p = 0.0
        pnl += p
        trades += 1
        if p >= 0: wins += 1
        else: losses += 1
    return {"pnl": pnl, "trades": trades, "wins": wins, "losses": losses}


def load_weekly_state(chat_id):
    d = db()
    if d is None:
        return None
    return d.weekly_state.find_one({"telegram_chat_id": str(chat_id)}, {"_id": 0})


def save_weekly_state(chat_id, data):
    d = db()
    if d is None:
        return False
    doc = dict(data)
    doc["telegram_chat_id"] = str(chat_id)
    doc["updated_at"] = datetime.now(timezone.utc)
    d.weekly_state.update_one({"telegram_chat_id": str(chat_id)}, {"$set": doc}, upsert=True)
    return True
