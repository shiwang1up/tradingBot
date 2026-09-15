# Confluence tuning against the stored window (2026-06-17 to 2026-09-11)

Date: 2026-09-15. Tool: `scripts/sweep_confluence.py` with the grids in `scripts/grids/`.
Baseline: `real-conf-2` (1,018 trades, PnL -65.1k on 1 lakh, avg R -0.26, 5 bps slippage per side).

## Result: no parameter fixes the strategy

Stage 1 changed one parameter at a time from the config defaults (24 variants). Every variant lost
money; average R ran from -0.19 to -0.37. The least bad were threshold 4.5 (289 trades, -29.9k) and
trend weight 2.0 (994 trades, -41.7k). In-sample and the held-out last 19 days disagreed on which
variants were "better", so the ranking is noise.

## Diagnosis: no edge, and the stop is inside the cost band

- Signed forward return after all 19,555 signals is within 2 bps of zero at 1, 2, 3, 6, 12 and 24
  bars. The entry carries no information at the 5-minute horizon. The EMA/RSI strategy's 6,410
  signals show the same.
- Average stop distance is 0.29% of price. Modelled slippage is 0.10% round trip, about 0.34 R per
  trade, which is the whole deficit.
- With slippage set to 0 the baseline is avg R +0.03 (PnL -10k); with 0.10% per side it is -0.41.
  PnL is a linear function of cost on a zero-edge signal.
- Stops hit inside two bars are 249 trades and -52k of the -65k: entries chase a 5-minute spike and
  get reverted at once. Stops average -1.15 R (slip through) and targets +1.57 R (of a nominal 2).

Real Groww intraday costs (brokerage, STT on the sell side, exchange and stamp charges) add roughly
another 0.05% per round trip on top of slippage, so the live figure would be worse than the backtest.

## What would have to change

Widening the stop alone does not help (ATR x3.0 still -57k with slippage, -30k without) because the
signal has no edge to protect. The next attempt needs a different signal or horizon, not new weights:
a longer bar (15- or 60-minute) or swing holding so the stop is several times the cost band, entries on
a pullback into the level rather than at the breakout close, or a time-of-day and volatility filter.
Any of these should be judged first on the forward-return table (`scripts/signal_forward_returns.py`), which is free, before
a full backtest.
