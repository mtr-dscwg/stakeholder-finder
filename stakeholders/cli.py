"""Command line entry point.

    python -m stakeholders https://www.theguardian.com/...       # fetch and analyse
    python -m stakeholders --html saved.html --url https://...  # analyse a saved page
    python -m stakeholders URL --json                            # machine-readable output
    python -m stakeholders URL --offline                         # page metadata only
    python -m stakeholders --check-wikidata                      # confirm property IDs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import analyze_html, analyze_url
from .http import FetchError
from .report import to_json, to_text


def main(argv: list[str] | None = None) -> int:
    # Some Windows terminals use a legacy code page that can't show ● or ─.
    # Print UTF-8 anyway, and substitute anything the terminal still can't show.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="stakeholders",
                                 description="List the stakeholders connected to a news article.")
    ap.add_argument("url", nargs="?", help="article URL")
    ap.add_argument("--url", dest="url_flag", help="the article's address, when using --html")
    ap.add_argument("--html", help="read a saved HTML file instead of fetching (pass --url too)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a text report")
    ap.add_argument("--offline", action="store_true",
                    help="skip Wikidata and GLEIF; only read the page itself")
    ap.add_argument("--depth", type=int, default=3, help="ownership hops to follow (default 3)")
    ap.add_argument("--brief", action="store_true", help="hide the evidence lines")
    ap.add_argument("--check-wikidata", action="store_true",
                    help="check that the Wikidata property IDs still mean what the code expects")
    args = ap.parse_args(argv)

    if args.check_wikidata:
        from .wikidata import check_properties
        rows = check_properties()
        for prop, expected, actual, ok in rows:
            print(f"{'ok ' if ok else 'BAD'} {prop:7} expected '{expected}', Wikidata says '{actual}'")
        return 0 if all(r[3] for r in rows) else 1

    args.url = args.url or args.url_flag
    if not args.url:
        ap.error("give an article URL (or --html FILE --url URL)")
    try:
        if args.html:
            html = Path(args.html).read_text(encoding="utf-8", errors="replace")
            report = analyze_html(html, args.url, enrich=not args.offline, max_depth=args.depth)
        else:
            report = analyze_url(args.url, enrich=not args.offline, max_depth=args.depth)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("tip: some sites block scripts. Save the page from your browser and use --html.",
              file=sys.stderr)
        return 2
    print(to_json(report) if args.json else to_text(report, show_evidence=not args.brief))
    return 0
