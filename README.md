
## Fixes in this build (code review pass)
- Removed ~160 lines of dead/unused legacy functions (`opening_range()`, `find_breakout()`, `paper_monitor()`) that were never called — the live flow uses `opening_range_for_key()`, `find_breakout_for_key()`, `paper_monitor_advanced()` instead.
- `get_ltp_batch()` (used by AUTO SELECT to compare NIFTY/BANKNIFTY/etc. together) now matches quotes the same reliable way as the single-symbol `get_ltp()` — by response key, then by `instrument_token`, then OHLC-close fallback — and returns prices keyed by the instrument_key you asked for, so AUTO SELECT can't silently miss a live quote due to a key-format mismatch.
- `select_atm_option()` now returns the option's real `tick_size` from Upstox instead of always silently assuming ₹0.05.
No trading logic, risk limits, or safety locks were changed — LIVE_TRADING stays hard-locked OFF everywhere.

## New features (v7.4 feature pass)
- **Weekly loss circuit breaker** — `WEEKLY_LOSS_LIMIT` (default ₹1,800, scaled to the ₹10,000 capital setting). Tracked in a separate weekly-state doc (Mongo `weekly_state` collection / local `.upstox_orb_weekly.json`) that survives across trading days within the same ISO week. Blocks new entries once hit; any open position still gets monitored through to its exit.
- **Partial profit booking** — `PARTIAL_BOOK_*` settings. Books `PARTIAL_BOOK_FRACTION` (default 50%) of the position once price is `PARTIAL_BOOK_TRIGGER_PCT` (default 50%) of the way to target, then moves SL on the remainder to breakeven. Automatically skipped if the lot can't be split into two whole lots (e.g. `LOTS=1`).
- **Weekly & monthly Telegram summaries** — sent automatically by the Railway supervisor (Friday after close for weekly; last trading day of the month for monthly, computed from the trade journal).
- **Upstox login reminder** — Telegram nudge between 8:30–9:10 AM IST on weekdays if the daily token (expires 3:30 AM) hasn't been refreshed yet.
- **Slippage & latency logging** — every trade record in the CSV/Mongo journal now includes `entry_slippage_pct` and `signal_latency_ms` (time from confirmed breakout to order fill).
- **Optional AI news analysis** — set `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY` as a fallback option) to have an LLM read the same headlines the keyword scorer uses and return a BULLISH/BEARISH/NEUTRAL call with a confidence and one-line reason (shown in the pre-trade Telegram message). Gemini is tried first if its key is present. This call happens once per day before entry — it never touches the fast 0.5s buy/sell monitoring loop. Without any key, behavior is unchanged (keyword scoring only).
- **Backtest mode** (`backtest.py`) — offline script that reuses the bot's own `signal_score` / `market_regime` / `calculate_trade_plan` functions against a historical 1-minute candle CSV you supply. Simulates futures-style point P&L (no historical option-chain data available), reports win rate / total PnL / max drawdown / a reason breakdown for no-trade days, and writes a full day-by-day CSV log. See the docstring at the top of `backtest.py` for CSV format and usage.

## Capital scaled to ₹10,000 (this build)
- `MAX_CAPITAL`: ₹5,000 → ₹10,000
- `MAX_RISK_PER_TRADE` / `RISK_RUPEES`: ₹250 → ₹500 (5% of capital, unchanged ratio)
- `DAILY_LOSS_LIMIT`: ₹300 → ₹600 (6% of capital, unchanged ratio)
- `WEEKLY_LOSS_LIMIT`: ₹900 → ₹1,800 (18% of capital, unchanged ratio)
- All other filters (signal score, regime, RR, ATR bounds, entry-quality range) are untouched.

## About the "AI analysis" — important, read before using
- This is still a **paper (simulated) trading bot**. `LIVE_TRADING` stays hard-locked `False` everywhere in the code. No real orders are ever placed, no real money is ever at risk from this bot.
- There is no way to guarantee profit. The bot combines rule-based technical scoring (EMA/VWAP/ATR/regime/breakout) with an *optional* LLM read of news headlines — it is a decision-support tool, not a certainty machine. Backtest it (`backtest.py`) on real historical data for your instrument before trusting any settings.
- To turn on the optional AI news layer, get a free API key from https://aistudio.google.com/app/apikey (Gemini) and put it in `.env` as `GEMINI_API_KEY=...`. Without a key, the bot works exactly as before using keyword-based news scoring — nothing breaks. (Anthropic's `ANTHROPIC_API_KEY` also works as a fallback if you'd rather use that.)
- Track real paper-trading results for a few weeks (win rate, RR, drawdown from the weekly/monthly summaries) before ever considering connecting this to real capital.
