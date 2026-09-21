"""Build index_membership.yaml from NSE's published constituent list and press releases.

    .venv/bin/python scripts/build_membership.py anchor      # fetch today's NIFTY 200 list
    .venv/bin/python scripts/build_membership.py parse URL   # print NIFTY 200 rows from a release

Sources of record are NSE's own documents. niftyhistory.in publishes the same data but
discloses no operator, source or methodology, so it is a cross-check only -- never the
authority. Any disagreement is resolved in favour of the NSE release and recorded.
"""
import argparse
import csv
import io
import re
import subprocess
import sys
from pathlib import Path
import zlib
from datetime import date

ANCHOR_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
EXPECTED = 200
DAILY = 1440                             # candle interval in minutes
RETRIES = 3                              # per request, against Groww's rate limiter


def fetch(url, binary=False):
    out = subprocess.run(["curl", "-sS", "--max-time", "45", "-A", UA, url],
                         capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8", "replace")


def anchor():
    for url in ANCHOR_URLS:
        try:
            text = fetch(url)
        except subprocess.CalledProcessError:
            continue
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) != EXPECTED:
            print("%s returned %d rows, expected %d" % (url, len(rows), EXPECTED),
                  file=sys.stderr)
            continue
        syms = sorted(r["Symbol"].strip().upper() for r in rows)
        isins = {r["Symbol"].strip().upper(): r["ISIN Code"].strip() for r in rows}
        if len(set(syms)) != EXPECTED:
            sys.exit("duplicate symbols in %s" % url)
        print("index: NIFTY 200")
        print("anchor:")
        print("  as_of: %s" % date.today().isoformat())
        print("  source: %s" % url)
        print("  fetched: %s" % date.today().isoformat())
        print("  size: %d" % EXPECTED)
        print("  symbols: [%s]" % ", ".join(syms))
        print("  isins:")
        for s in syms:
            print("    %s: %s" % (s, isins[s]))
        return
    sys.exit("no anchor URL returned %d rows; refusing to fall back to a third party"
             % EXPECTED)


def pdf_text(data):
    """Text from a PDF's content streams. NSE releases are not scanned images, so the text
    layer is present and this is enough -- no PDF library is needed."""
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            parts.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except Exception:
            continue
    blob = "\n".join(parts)
    shown = re.findall(r"\((?:[^()\\]|\\.)*\)", blob)
    return "".join(x[1:-1] for x in shown).replace("\\(", "(").replace("\\)", ")")


def parse(url):
    """Print the NIFTY 200 inclusions and exclusions in one release, for hand-checking
    before they go into the timeline."""
    text = pdf_text(fetch(url, binary=True))
    text = re.sub(r"en-(US|IN)", " ", text)
    print(text[:4000])
    print("\n--- rows mentioning Nifty 200 ---")
    for m in re.finditer(r"Nifty 200(.{0,400}?)(?=Nifty |\Z)", text, re.S | re.I):
        print(re.sub(r"\s+", " ", m.group(0))[:400])


def symbols_needed(timeline_path="index_membership.yaml"):
    """Every symbol the timeline can ever return: the anchor, both sides of every event, and
    each alias's canonical target. Alias KEYS are deliberately excluded -- they are resolved at
    load time and can never be returned, so fetching them would be fetching a name that no query
    produces."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from tradebot.data.membership import load_timeline
    t = load_timeline(timeline_path)
    need = set(t.anchor)
    for e in t.events:
        need |= set(e.include) | set(e.exclude)
    for _, canonical in t.aliases:
        need.add(canonical)
    return sorted(need)


def fetch(db_path, start=date(2020, 1, 1), pause=1.0, only=None):
    """Daily candles for every symbol the timeline names.

    Deliberately NOT `tradebot fetch-data`: that path applies a SessionClock filter, and daily
    bars are stamped 00:00 IST, so every one of them would be dropped. This reuses the same
    `fetch_incremental` the CLI uses, with `keep=None`.

    Resumable by construction -- `fetch_incremental` starts from each symbol's stored high-water
    mark, so re-running after a failure costs one redundant request per finished symbol and
    nothing more. One bad symbol never stops the rest; failures are written to
    data/missing_symbols.txt so a later screen can see what is absent rather than inferring it
    from a gap.
    """
    import time
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from tradebot.config import load_config
    from tradebot.data.historical import fetch_incremental
    from tradebot.execution.groww_adapter import GrowwAdapter
    from tradebot.store.db import connect
    from tradebot.store.repo import Repo

    cfg = load_config("config.yaml")
    adapter = GrowwAdapter(cfg.secrets.groww_api_key, cfg.secrets.groww_totp_secret,
                           cfg.secrets.groww_api_secret)
    adapter.client                                  # fail fast if the key is not approved today
    repo = Repo(connect(db_path))
    now = int(time.time())
    lookback = (date.today() - start).days + 1
    wanted = only if only else symbols_needed()

    def throttled(sym, exch, a, b, interval):
        """Groww rate-limits by returning a non-JSON body, which surfaces as a JSONDecodeError
        rather than an HTTP error, so a burst looks like a dead symbol. Measured: 0.4s between
        requests trips it within one symbol's chunks, 1.0s does not. Retry with backoff anyway --
        over a multi-hour unattended run a transient block must not cost a whole symbol."""
        last = None
        for attempt in range(RETRIES):
            try:
                return adapter.fetch_candles(sym, exch, a, b, interval)
            except Exception as e:                  # noqa: BLE001 - retry any transport fault
                last = e
                time.sleep(pause * (2 ** attempt) + 1.0)
            finally:
                time.sleep(pause)
        raise last

    total, failed = 0, []
    for i, sym in enumerate(wanted, 1):
        try:
            n = fetch_incremental(repo, throttled, [sym], "NSE", DAILY, lookback, now,
                                  keep=None)[sym]
            total += n
            print("[%d/%d] %s +%d" % (i, len(wanted), sym, n), flush=True)
        except Exception as e:                      # noqa: BLE001 - isolate per symbol
            failed.append((sym, "%s: %s" % (type(e).__name__, e)))
            print("[%d/%d] %s FAILED %s: %s" % (i, len(wanted), sym, type(e).__name__, e),
                  flush=True)
    print("\ninserted %d rows across %d symbols; %d failed" % (total, len(wanted), len(failed)))
    if failed:
        with open("data/missing_symbols.txt", "w") as fh:
            for sym, why in failed:
                fh.write("%s %s\n" % (sym, why))
        print("failures written to data/missing_symbols.txt")
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("anchor", help="fetch today's NIFTY 200 list and print the anchor block")
    p = sub.add_parser("parse", help="print the NIFTY 200 portion of one release PDF")
    p.add_argument("url")
    f = sub.add_parser("fetch", help="daily candles for every symbol the timeline names")
    f.add_argument("--db", default="data/tradebot.db")
    f.add_argument("--pause", type=float, default=1.0)
    f.add_argument("--only", nargs="*", help="fetch just these symbols (for a smoke test)")
    sub.add_parser("needed", help="list the symbols the timeline names")
    args = ap.parse_args(argv)
    if args.cmd == "anchor":
        anchor()
    elif args.cmd == "parse":
        parse(args.url)
    elif args.cmd == "needed":
        for x in symbols_needed():
            print(x)
    elif args.cmd == "fetch":
        return fetch(args.db, pause=args.pause, only=args.only)
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
