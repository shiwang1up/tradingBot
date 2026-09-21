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
import zlib
from datetime import date

ANCHOR_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
EXPECTED = 200


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("anchor", help="fetch today's NIFTY 200 list and print the anchor block")
    p = sub.add_parser("parse", help="print the NIFTY 200 portion of one release PDF")
    p.add_argument("url")
    args = ap.parse_args(argv)
    if args.cmd == "anchor":
        anchor()
    elif args.cmd == "parse":
        parse(args.url)
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
