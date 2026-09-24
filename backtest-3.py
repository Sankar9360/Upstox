"""
NIFTY ORB — Backtest Mode
=========================
Runs the SAME decision functions the live bot uses (signal_score,
market_regime, entry_quality, calculate_trade_plan, EMA/VWAP/ATR) against
historical 1-minute candles you supply in a CSV, so you can see how the
current settings (SIGNAL_MIN_SCORE, MIN_RR, ATR filters, etc.) would have
performed before risking anything live.

WHAT THIS DOES NOT DO
----------------------
- It does NOT call Upstox or Telegram. It is 100% offline.
- It does NOT backtest option premiums (no historical option-chain data is
  used). It simulates FUTURES-style point moves only: PnL = points * lot_size.
  Use this to validate the ORB + filter logic, not exact option P&L.
- Breakout confirmation uses 1-minute candle closes as the "tick" (since we
  don't have historical tick data). This is an approximation of the live
  bot's sub-second FAST_POLL_SECONDS polling — real intraday fills will
  differ slightly.
- News bias is NOT re-created historically; it defaults to NEUTRAL for every
  backtest day (i.e. that part of signal_score neither helps nor hurts).

CSV INPUT FORMAT
----------------
One row per 1-minute candle, any of these column layouts:
  timestamp,open,high,low,close,volume
  date,time,open,high,low,close,volume     (date=YYYY-MM-DD, time=HH:MM)
timestamp / date+time must be in IST (Asia/Kolkata), naive or tz-aware.

USAGE
-----
    python3 backtest.py historical_1min.csv --lot-size 75
    python3 backtest.py historical_1min.csv --lot-size 75 --out results.csv

Where to get historical 1-min candles: Upstox's historical-candle endpoint
(different from the "intraday" one the live bot uses) accepts a from/to date
range for past sessions — see Upstox API docs for
`/v3/historical-candle/{instrument_key}/minutes/1/{to_date}/{from_date}`.
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime

import NIFTY_ORB_ADVANCED_v7_4 as engine
from NIFTY_ORB_ADVANCED_v7_4 import (
    RANGE_START, RANGE_END, ENTRY_CUTOFF, SQUARE_OFF_TIME,
    MIN_BREAKOUT_POINTS, CONFIRM_CHECKS, SIGNAL_MIN_SCORE, REGIME_FILTER,
    MIN_RR, MAX_ORB_RANGE_PCT,
    ema, vwap_from, atr_from, market_regime, signal_score, entry_quality,
    calculate_trade_plan, direction_from_orb, pnl_rupees,
)


def load_candles(path):
    """Returns {date_str: [ {ts(datetime.time), o,h,l,c,v}, ... ]}"""
    by_day = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if "timestamp" in row and row["timestamp"]:
                    ts = datetime.fromisoformat(row["timestamp"])
                else:
                    ts = datetime.fromisoformat(f"{row['date']}T{row['time']}")
                day = ts.strftime("%Y-%m-%d")
                by_day[day].append({
                    "t": ts.time(), "o": float(row["open"]), "h": float(row["high"]),
                    "l": float(row["low"]), "c": float(row["close"]),
                    "v": float(row.get("volume", 0) or 0),
                })
            except Exception as e:
                print(f"Skipping bad row: {row} ({e})", file=sys.stderr)
    for day in by_day:
        by_day[day].sort(key=lambda x: x["t"])
    return by_day


def technical_bias_offline(candles_so_far):
    if len(candles_so_far) < 21:
        return {"ok": False}
    closes = [x["c"] for x in candles_so_far]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    vw, atr = vwap_from(candles_so_far), atr_from(candles_so_far, 14)
    if e9 is None or e21 is None or atr is None:
        return {"ok": False}
    last = closes[-1]
    bull = last > e9 > e21 and (vw is None or last > vw)
    bear = last < e9 < e21 and (vw is None or last < vw)
    vol_now = candles_so_far[-1]["v"]
    avg_vol = sum(x["v"] for x in candles_so_far[-11:-1]) / 10 if len(candles_so_far) >= 11 else 0
    volume_ok = bool(avg_vol and vol_now >= 1.10 * avg_vol)
    return {"ok": True, "last": last, "ema9": e9, "ema21": e21, "vwap": vw,
            "atr": atr, "bull": bull, "bear": bear, "volume_ok": volume_ok}


NEUTRAL_NEWS = {"bias": "NEUTRAL", "titles": []}


def simulate_day(day, candles, lot_size):
    orb_candles = [x for x in candles if RANGE_START <= x["t"] < RANGE_END]
    if not orb_candles:
        return None
    high = max(x["h"] for x in orb_candles)
    low = min(x["l"] for x in orb_candles)

    after_orb = [x for x in candles if x["t"] >= RANGE_END]
    up = down = 0
    breakout_idx = None
    direction = None
    for i, x in enumerate(after_orb):
        if x["t"] >= ENTRY_CUTOFF:
            break
        p = x["c"]
        if p >= high + MIN_BREAKOUT_POINTS:
            up += 1; down = 0
        elif p <= low - MIN_BREAKOUT_POINTS:
            down += 1; up = 0
        else:
            up = down = 0
        if up >= CONFIRM_CHECKS:
            breakout_idx, direction = i, "CE"; break
        if down >= CONFIRM_CHECKS:
            breakout_idx, direction = i, "PE"; break

    if breakout_idx is None:
        return {"date": day, "result": "NO_BREAKOUT"}

    # Mirrors the live bot: hold the confirmed breakout and keep waiting for
    # enough 1-min candles for technical_bias() to become valid, up to cutoff.
    spot = after_orb[breakout_idx]["c"]
    tech = {"ok": False}
    j = breakout_idx
    while j < len(after_orb) and after_orb[j]["t"] < ENTRY_CUTOFF:
        candles_so_far = [x for x in candles if x["t"] <= after_orb[j]["t"]]
        tech = technical_bias_offline(candles_so_far)
        if tech.get("ok"):
            break
        j += 1
    if not tech.get("ok"):
        return {"date": day, "result": "NO_TECH_DATA"}
    spot = after_orb[j]["c"]  # entry re-quoted at the moment tech data became valid

    regime = market_regime(tech)
    if REGIME_FILTER and regime in ("SIDEWAYS", "LOW_VOL", "HIGH_VOL", "UNKNOWN"):
        return {"date": day, "result": f"REGIME_BLOCK:{regime}"}

    good_range, reason = entry_quality(high, low, spot)
    if not good_range:
        return {"date": day, "result": f"RANGE_BLOCK:{reason}"}

    score, reasons, atr_pct = signal_score(direction, tech, NEUTRAL_NEWS, regime, high - low, spot)
    if score < SIGNAL_MIN_SCORE:
        return {"date": day, "result": f"SCORE_REJECTED:{score}"}

    side = direction_from_orb(direction)
    sl_points, target_points = calculate_trade_plan("FUTURE", spot, lot_size, spot, tech.get("atr"), 0.05)
    entry = spot
    sl = entry - sl_points if side == "BUY" else entry + sl_points
    target = entry + target_points if side == "BUY" else entry - target_points
    rr = target_points / max(sl_points, 0.05)
    if rr < MIN_RR:
        return {"date": day, "result": f"RR_REJECTED:{rr:.2f}"}

    # Walk forward candle-by-candle for the exit. SL checked before target
    # within the same candle (conservative assumption when both are touched).
    exit_price, exit_reason = None, "SQUARE_OFF"
    for x in after_orb[breakout_idx + 1:]:
        if x["t"] >= SQUARE_OFF_TIME:
            break
        if side == "BUY":
            if x["l"] <= sl:
                exit_price, exit_reason = sl, "SL_HIT"; break
            if x["h"] >= target:
                exit_price, exit_reason = target, "TARGET_HIT"; break
        else:
            if x["h"] >= sl:
                exit_price, exit_reason = sl, "SL_HIT"; break
            if x["l"] <= target:
                exit_price, exit_reason = target, "TARGET_HIT"; break
    if exit_price is None:
        eod = [x for x in candles if x["t"] <= SQUARE_OFF_TIME]
        exit_price = eod[-1]["c"] if eod else entry

    pnl = pnl_rupees(side, entry, exit_price, lot_size)
    return {
        "date": day, "result": exit_reason, "direction": direction, "side": side,
        "entry": round(entry, 2), "exit": round(exit_price, 2), "sl": round(sl, 2),
        "target": round(target, 2), "score": score, "regime": regime,
        "rr": round(rr, 2), "atr_pct": round(atr_pct, 2), "pnl": round(pnl, 2),
    }


def main():
    ap = argparse.ArgumentParser(description="Backtest the ORB strategy against historical 1-min candles.")
    ap.add_argument("csv_path")
    ap.add_argument("--lot-size", type=int, default=75, help="Contract lot size (default 75, NIFTY futures)")
    ap.add_argument("--out", default="backtest_results.csv")
    ap.add_argument("--min-score", type=int, default=None, help="Override SIGNAL_MIN_SCORE (live default: 8)")
    ap.add_argument("--min-rr", type=float, default=None, help="Override MIN_RR (live default: 1.50)")
    ap.add_argument("--no-regime-filter", action="store_true", help="Allow SIDEWAYS/HIGH_VOL/LOW_VOL regimes too, not just TREND")
    ap.add_argument("--min-atr-pct", type=float, default=None, help="Override MIN_ATR_PCT (live default: 0.12)")
    ap.add_argument("--max-atr-pct", type=float, default=None, help="Override MAX_ATR_PCT (live default: 1.50)")
    args = ap.parse_args()

    global SIGNAL_MIN_SCORE, MIN_RR, REGIME_FILTER
    if args.min_score is not None: SIGNAL_MIN_SCORE = args.min_score
    if args.min_rr is not None: MIN_RR = args.min_rr
    if args.no_regime_filter: REGIME_FILTER = False
    # signal_score() is defined in the engine module and reads these two from
    # ITS OWN globals — overriding our local copies wouldn't reach it, so we
    # patch the engine module's globals directly instead.
    if args.min_atr_pct is not None: engine.MIN_ATR_PCT = args.min_atr_pct
    if args.max_atr_pct is not None: engine.MAX_ATR_PCT = args.max_atr_pct
    print(f"Settings: SIGNAL_MIN_SCORE={SIGNAL_MIN_SCORE}, MIN_RR={MIN_RR}, REGIME_FILTER={REGIME_FILTER}, "
          f"ATR%% range=[{engine.MIN_ATR_PCT},{engine.MAX_ATR_PCT}]")

    by_day = load_candles(args.csv_path)
    if not by_day:
        print("No candles loaded — check the CSV format."); return

    rows = []
    for day in sorted(by_day):
        r = simulate_day(day, by_day[day], args.lot_size)
        if r:
            rows.append(r)

    traded = [r for r in rows if "pnl" in r]
    wins = [r for r in traded if r["pnl"] >= 0]
    losses = [r for r in traded if r["pnl"] < 0]
    total_pnl = sum(r["pnl"] for r in traded)

    # Max drawdown on the cumulative daily-pnl curve
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for r in traded:
        cum += r["pnl"]; peak = max(peak, cum); max_dd = min(max_dd, cum - peak)

    print(f"\nDays scanned      : {len(rows)}")
    print(f"Trades taken      : {len(traded)}")
    print(f"Wins / Losses     : {len(wins)} / {len(losses)}")
    if traded:
        print(f"Win rate          : {len(wins)/len(traded)*100:.1f}%")
        print(f"Avg RR (planned)  : {sum(r['rr'] for r in traded)/len(traded):.2f}")
    print(f"Total PnL         : ₹{total_pnl:+.2f}  (lot size {args.lot_size})")
    print(f"Max drawdown      : ₹{max_dd:.2f}")
    no_trade_reasons = defaultdict(int)
    for r in rows:
        if "pnl" not in r:
            no_trade_reasons[r["result"].split(":")[0]] += 1
    if no_trade_reasons:
        print("No-trade days by reason:")
        for k, v in sorted(no_trade_reasons.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")

    fields = ["date", "result", "direction", "side", "entry", "exit", "sl", "target", "score", "regime", "rr", "atr_pct", "pnl"]
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\nFull day-by-day log written to {args.out}")


if __name__ == "__main__":
    main()
