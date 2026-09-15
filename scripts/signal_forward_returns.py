"""Signed forward return (bps) after each stored signal, by horizon, direction and previous-bar direction.
Usage: .venv/bin/python scripts/signal_forward_returns.py <run_id>. A zero row means the entry carries no
information at that horizon; judge a new signal here before paying for a full backtest."""
import sqlite3, sys
from collections import defaultdict
run = sys.argv[1]
db = sqlite3.connect("data/tradebot.db"); db.row_factory = sqlite3.Row
closes = defaultdict(dict); order = defaultdict(list)
for r in db.execute("select symbol, ts, c as close from candles where interval=5 order by symbol, ts"):
    closes[r["symbol"]][r["ts"]] = r["close"]; order[r["symbol"]].append(r["ts"])
idx = {s: {t: i for i, t in enumerate(ts)} for s, ts in order.items()}
H = [1, 2, 3, 6, 12, 24]
acc = defaultdict(lambda: defaultdict(list))
for r in db.execute("select symbol, bar_ts, direction, entry from signals where run_id=?", (run,)):
    s = r["symbol"]; i = idx[s].get(r["bar_ts"])
    if i is None: continue
    sign = 1 if r["direction"] == "LONG" else -1
    day = order[s][i] // 86400
    for h in H:
        j = i + h
        if j >= len(order[s]) or order[s][j] // 86400 != day: continue   # same session only
        ret = sign * (closes[s][order[s][j]] / r["entry"] - 1) * 1e4
        acc["ALL"][h].append(ret); acc[r["direction"]][h].append(ret)
        acc["prev_bar_up" if sign*(r["entry"]/closes[s][order[s][i-1]]-1) > 0 else "prev_bar_dn"][h].append(ret)
print(f"{'group':<14}" + "".join(f"{'h'+str(h):>9}" for h in H) + "   n")
for g, d in acc.items():
    print(f"{g:<14}" + "".join(f"{sum(d[h])/len(d[h]):>9.1f}" for h in H) + f"   {len(d[H[0]])}")
