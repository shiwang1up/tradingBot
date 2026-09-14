"""Rewrite universe.yaml from NSE's official NIFTY 200 constituent CSV.

Usage: .venv/bin/python scripts/update_universe.py [--index nifty200]
"""
import argparse
import csv
import io
import sys

import requests
import yaml

URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="nifty200", choices=URLS)
    ap.add_argument("--out", default="universe.yaml")
    args = ap.parse_args()
    resp = requests.get(URLS[args.index], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    symbols = sorted(r["Symbol"].strip() for r in rows if r.get("Symbol"))
    if not symbols:
        print("no symbols parsed; NSE may have blocked the request", file=sys.stderr)
        return 1
    header = ("# Survivorship bias: this is today's constituent list. Backtests over past\n"
              "# months overstate results. See spec section 4.1.\n")
    with open(args.out, "w") as f:
        f.write(header)
        yaml.safe_dump({"exchange": "NSE", "symbols": symbols}, f, sort_keys=False)
    print(f"wrote {len(symbols)} symbols to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
