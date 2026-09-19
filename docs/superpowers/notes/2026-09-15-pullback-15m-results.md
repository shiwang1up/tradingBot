# 15-minute pullback: first results

Date: 2026-09-15. Run `pb-15m-1` (`config-15m.yaml`, strategy `pullback`, defaults: EMA20/EMA50,
ATR14, max_pullback_bars 8, 2R target, stub filter, 5 bps slippage per side).

## Data

Official 15-minute Groww candles, 49 symbols, 2026-06-18 to 2026-09-15: 75,948 bars (1,550 per
symbol, 62 trading days). Groww's 15-minute history covers the same window as the 5-minute cache, not
the extra month the spec hoped for.

## Signals: no edge

2,548 signals (1,170 long, 1,378 short). Average stop distance 0.41% of price, against a round-trip
cost of about 0.10%: better than the 0.29% of the 5-minute strategies, still only about four times the
cost band.

Forward return in basis points after each signal, same session only (`scripts/signal_forward_returns.py pb-15m-1`):

```
group                h1       h2       h3       h6      h12      h24   n
ALL                -0.5     -0.5     -0.2     -1.4     -2.5      nan   2424
SHORT               0.1      0.7      0.9      0.4      2.9      nan   1308
prev_bar_up        -0.3     -0.4     -0.1     -0.8     -0.3      nan   1988
LONG               -1.1     -1.8     -1.6     -3.5     -8.6      nan   1116
prev_bar_dn        -1.2     -0.8     -1.0     -3.9    -13.4      nan   436
```

The bar was h6 and h12 clearly above about 15 bp. Every cell is within a few bp of zero; longs are
mildly negative at every horizon (buying the EMA20 bounce in an uptrend is faded, not followed), shorts
are mildly positive but nowhere near cost. (h24 is empty because a session has only 25 bars.)

## Backtest, for the record

| | |
|---|---|
| Trades | 512 (240 long, 272 short) |
| Win rate | 34.2% |
| Total PnL | -50,199.65 on 1 lakh |
| Avg R | -0.26 (long -0.40, short -0.14) |
| Exits | STOP 237 (avg -1.14 R), TARGET 76 (+1.52 R), SQUARE_OFF 199 (+0.09 R) |
| Average bars held | 7.3 (about 110 minutes) |
| Risk rejects | insufficient_size 1,142, max_open_positions 443, entries_closed 296, daily_loss_cap 105 |

Two things the report adds to the forward-return verdict. First, 39% of trades end at the 15:10
square-off, because a 2R target on a 15-minute pullback is rarely reached inside the session: the
reward side of the trade is structurally capped by the intraday product. Second, stops outnumber
targets three to one, worse than the two to one a random walk gives at 2:1, which is what the negative
long forward returns look like once a stop is attached.

## Conclusion

Same verdict as the 5-minute strategies, from a different entry on a different bar: the entries do
not predict the next hours of price, so no stop or target arrangement can make them pay. Not worth
tuning. If this line of work continues, the next candidates should be judged on the forward-return
table before any backtest: a swing (CNC, multi-day) holding so the target is not cut off at 15:10 and
the stop can be 1 to 2% of price; a fade of these same setups on the short side (the only cell above
zero, and still under cost); or a signal built on something other than the recent price path, since
every strategy tried so far is a function of it.
