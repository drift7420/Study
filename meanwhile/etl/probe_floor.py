"""How much of each language's history the sitelink floor hides.

extract.py fetches nothing below MIN_SITELINKS, so the coverage table can only
ever describe the part of Wikidata above that line. That matters because the
line is drawn in a unit — sitelinks — that measures how many language editions
wrote about someone, and a figure covered only by zh.wikipedia has one.

So the 40:1 Western Europe : East Asia ratio thresholds.py found has two very
different possible explanations, and the coverage table cannot tell them apart:

  1. Wikidata really does hold ~40x more early-modern Europeans.
  2. The floor cuts hardest exactly where one language covers a subject alone.

This asks Wikidata directly. For one decade of births, for each Wikipedia
edition: how many biographies does that edition have, and what share of them
clear the floor — that is, what share we can currently see at all.

If zh and fr come back with similar visible shares, the floor is not what is
emptying East Asia and the gap is Wikidata's own. If zh is far lower, the
floor is ours and lowering it would recover real history.

The cost of the query scales with how much the edition has written, so the
big editions are exactly the ones that time out — zh, fr, de and en all did,
which is most of the comparison. Each edition therefore narrows its own window
until WDQS will answer, and reports the window it used. Shares stay comparable
across different windows; the totals do not, and the table says so.

Nothing here writes to the database; it prints a table and exits.

Usage:
    python probe_floor.py --contact you@example.com
    python probe_floor.py --contact you@example.com --decade 1600
    python probe_floor.py --contact you@example.com --editions zh ja fr de
"""

import argparse
import os

import extract

# Chosen to contrast, not to be complete: three editions whose subjects are
# largely covered in that language alone, against three that share their
# subjects with most of Europe.
EDITIONS = ("zh", "ja", "hi", "ar", "sw", "fr", "de", "en")

FLOOR = extract.MIN_SITELINKS["person"]

MIN_SPAN = 1

# COUNT(*) rather than COUNT(DISTINCT ?item): an item has at most one article
# per edition, so the distinct was paying for a guarantee the data already
# gives — and paying for it on exactly the queries that were too slow.
QUERY = """
SELECT ?bucket (COUNT(*) AS ?n) WHERE {
  ?item wdt:P569 ?driver .
  %DATEFILTER%
  ?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sitelinks .
  ?article schema:about ?item ; schema:isPartOf <https://%EDITION%.wikipedia.org/> .
  BIND(IF(?sitelinks >= %FLOOR%, "visible", "hidden") AS ?bucket)
}
GROUP BY ?bucket
"""


def query_for(edition, decade, span):
    return (QUERY
            .replace("%DATEFILTER%", extract.date_filter(decade, decade + span))
            .replace("%EDITION%", edition)
            .replace("%FLOOR%", str(FLOOR)))


def counts(payload):
    """{'visible': n, 'hidden': n} from a grouped result."""
    out = {"visible": 0, "hidden": 0}
    for binding in payload["results"]["bindings"]:
        bucket = extract.cell(binding, "bucket")
        if bucket in out:
            out[bucket] = int(extract.cell(binding, "n") or 0)
    return out


def probe_edition(edition, decade, span, contact):
    """Narrow the window until WDQS will answer. Returns (counts, years, error)."""
    while True:
        try:
            return counts(extract.run_query(
                query_for(edition, decade, span), contact)), span, None
        except Exception as exc:                       # noqa: BLE001
            if span <= MIN_SPAN:
                return None, span, exc
            span //= 2
            print(f"  {edition}: narrowing to {span} year(s)")


def probe(editions, decade, span, contact):
    print(f"\nBiographies of people born from {decade}, by Wikipedia edition.\n"
          f"'visible' means {FLOOR}+ sitelinks — the ones extract.py fetches at all.\n"
          f"An edition that timed out narrowed its own window, so compare the\n"
          f"shares, not the totals.\n")
    print("edition".ljust(10) + "years".rjust(6) + "total".rjust(9) + "visible".rjust(9)
          + "hidden".rjust(9) + "  share visible")

    for edition in editions:
        found, years, error = probe_edition(edition, decade, span, contact)
        if found is None:
            print(f"{edition.ljust(10)}{years:6}{'failed'.rjust(9)}  ({error})")
            continue

        total = found["visible"] + found["hidden"]
        if not total:
            print(f"{edition.ljust(10)}{years:6}{0:9}  (nothing in this window)")
            continue
        print(f"{edition.ljust(10)}{years:6}{total:9}{found['visible']:9}"
              f"{found['hidden']:9}{found['visible'] / total:>15.0%}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decade", type=int, default=1800,
                        help="First year of the window to sample (default 1800).")
    parser.add_argument("--span", type=int, default=10,
                        help="Years to sample (default 10). An edition that times "
                             "out halves this for itself until WDQS answers.")
    parser.add_argument("--editions", nargs="+", default=list(EDITIONS))
    parser.add_argument("--contact", default=os.environ.get("MEANWHILE_CONTACT"),
                        help="Contact address for the User-Agent header, which "
                             "Wikidata requires.")
    args = parser.parse_args()

    if not args.contact:
        parser.error("Wikidata rejects anonymous queries. Pass --contact your@email.com, "
                     "or set the MEANWHILE_CONTACT environment variable.")

    probe(args.editions, args.decade, args.span, args.contact)


if __name__ == "__main__":
    main()
