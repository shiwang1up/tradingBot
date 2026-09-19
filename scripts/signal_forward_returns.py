"""Signed forward return (bps) after each stored signal, by horizon, direction and previous-bar direction.
Usage: .venv/bin/python scripts/signal_forward_returns.py <run_id> [--db data/tradebot.db]. The candle
interval comes from the run's stored config, so 5-minute and 15-minute runs both work. A zero row means
the entry carries no information at that horizon; judge a new signal here before paying for a backtest."""
import argparse
import json
import sqlite3
from collections import defaultdict

HORIZONS = [1, 2, 3, 6, 12, 24]


def run_interval(db: sqlite3.Connection, run_id: str) -> int:
    row = db.execute("select config_json from runs where run_id=?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown run: {run_id}")
    try:
        return int(json.loads(row[0])["execution"]["interval_minutes"])
    except (KeyError, TypeError, ValueError):
        return 5


def forward_returns(db: sqlite3.Connection, run_id: str) -> dict:
    """{group: {horizon: [bps, ...]}} over the run's signals, same session only."""
    interval = run_interval(db, run_id)
    closes, order = defaultdict(dict), defaultdict(list)
    for r in db.execute("select symbol, ts, c as close from candles where interval=? order by symbol, ts", (interval,)):
        closes[r["symbol"]][r["ts"]] = r["close"]
        order[r["symbol"]].append(r["ts"])
    idx = {s: {t: i for i, t in enumerate(ts)} for s, ts in order.items()}
    acc = defaultdict(lambda: defaultdict(list))
    for r in db.execute("select symbol, bar_ts, direction, entry from signals where run_id=?", (run_id,)):
        s = r["symbol"]
        i = idx.get(s, {}).get(r["bar_ts"])
        if i is None or i == 0:
            continue
        sign = 1 if r["direction"] == "LONG" else -1
        day = order[s][i] // 86400
        prev = "prev_bar_up" if sign * (r["entry"] / closes[s][order[s][i - 1]] - 1) > 0 else "prev_bar_dn"
        for h in HORIZONS:
            j = i + h
            if j >= len(order[s]) or order[s][j] // 86400 != day:
                continue
            ret = sign * (closes[s][order[s][j]] / r["entry"] - 1) * 1e4
            for g in ("ALL", r["direction"], prev):
                acc[g][h].append(ret)
    return acc


def format_table(acc: dict) -> str:
    lines = [f"{'group':<14}" + "".join(f"{'h' + str(h):>9}" for h in HORIZONS) + "   n"]
    for g, d in acc.items():
        cells = "".join(f"{(sum(d[h]) / len(d[h])) if d[h] else float('nan'):>9.1f}" for h in HORIZONS)
        lines.append(f"{g:<14}{cells}   {len(d[HORIZONS[0]])}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--db", default="data/tradebot.db")
    a = ap.parse_args(argv)
    db = sqlite3.connect(a.db)
    db.row_factory = sqlite3.Row
    print(f"run {a.run_id}: {run_interval(db, a.run_id)}-minute bars")
    print(format_table(forward_returns(db, a.run_id)))


if __name__ == "__main__":
    main()
