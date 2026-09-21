"""What the sitelink floor hides, by region.

extract.py fetches nothing below MIN_SITELINKS, so the coverage table can only
ever describe the part of Wikidata above that line. That matters because the
line is drawn in a unit — sitelinks — that counts how many language editions
wrote about someone, and a figure covered by one language has one.

So the 40:1 Western Europe : East Asia ratio thresholds.py found has two very
different possible explanations, and the coverage table cannot tell them apart:

  1. Wikidata really does hold ~40x more early-modern Europeans.
  2. The floor cuts hardest exactly where one language covers a subject alone.

This fetches a short window of births with the floor dropped to 1, places them
by region the same way the pipeline does, and reports what share of each region
sits below the real floor. If the hidden share is roughly even across regions,
the floor is even-handed and the gap is Wikidata's own. If East Asia's hidden
share is far higher than Western Europe's, the floor is ours to fix.

An earlier version asked this per Wikipedia edition, joining each item to
`?article schema:isPartOf <https://xx.wikipedia.org/>`. That join costs what
the *edition* costs, not what the window costs: Swahili answered a ten-year
window while English timed out on a single year. Narrowing the window could
not help, and asking per region rather than per edition is the better question
anyway — regions are what the app shows.

Nothing here streams a row per person. Two columns for every human born in
a single year is still some twenty thousand rows and three megabytes, and
WDQS cut the transfer three times at three different points — a successful
query whose answer would not arrive, which is a different failure from the
refusals before it and not one a smaller SELECT fixes.

So the counting happens on the server. One aggregate query returns a few
hundred rows: per country of citizenship, how many people fall on each side
of the floor. A second small query places those countries, and the regions
are folded up here.

Two caveats that come with counting by citizenship, both worth holding in
mind when reading the table. People with no P27 recorded are missing
entirely, and if citizenship is itself recorded less often for the
thinly-covered, that biases the very population being measured. And every
citizen of a state lands wherever that state's coordinate puts it, so a large
or mobile polity concentrates its people in one region. Neither distorts the
*share* on each side of the floor within a region, which is what the question
asks.

Usage:
    python probe_floor.py --contact you@example.com
    python probe_floor.py --contact you@example.com --from 1500 --span 2
"""

import argparse
import json
import os
from pathlib import Path

import extract
import regions as regions_mod

FLOOR = extract.MIN_SITELINKS["person"]

# Counted on the server, grouped by citizenship: a few hundred rows instead
# of one per person. The join order is still the extraction query's — lead
# with the date, then narrow — because that is the part that makes WDQS
# willing to answer at all.
QUERY = """
SELECT ?country ?bucket (COUNT(*) AS ?n) WHERE {
  ?item wdt:P569 ?driver .
  %DATEFILTER%
  ?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sitelinks ; wdt:P27 ?country .
  BIND(IF(?sitelinks >= %FLOOR%, "visible", "hidden") AS ?bucket)
}
GROUP BY ?country ?bucket
"""


def query_for(low, high):
    return (QUERY
            .replace("%DATEFILTER%", extract.date_filter(low, high))
            .replace("%FLOOR%", str(FLOOR)))


def fetch_window(low, high, contact, cache_dir):
    """[(country qid, bucket, count)] for births in [low, high)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"floor-{low}-{high}.json"
    if cached.exists():
        print(f"  {low}-{high}: cached")
        return json.loads(cached.read_text(encoding="utf-8"))

    print(f"  {low}-{high}: counting births by citizenship…")
    payload = extract.run_query(query_for(low, high), contact)
    rows = [(extract.cell(b, "country").rsplit("/", 1)[-1],
             extract.cell(b, "bucket"),
             int(extract.cell(b, "n") or 0))
            for b in payload["results"]["bindings"]]
    cached.write_text(json.dumps(rows), encoding="utf-8")
    print(f"  {low}-{high}: {len(rows)} country/bucket rows, "
          f"{sum(n for _, _, n in rows)} people")
    return rows


def place(rows, contact, cache_dir):
    """{country qid: region id}, by the polity rule — capital, own
    coordinate, then country. A handful of requests, not thousands."""
    countries = sorted({country for country, _, _ in rows})
    print(f"  placing {len(countries)} countries…")
    coordinates, failed = extract.fetch_coordinates(
        countries, "polity", contact, cache_dir)
    if failed:
        print(f"  {len(failed)} could not be asked about; their people are left out")

    placed = {}
    for country, point in coordinates.items():
        region_id, _ = regions_mod.assign(point[0], point[1])
        if region_id is not None:
            placed[country] = region_id
    print(f"  {len(placed)}/{len(countries)} countries placed")
    return placed


def tally(rows, placed):
    """{region_id: (below the floor, at or above it)}.

    A country the coordinate pass could not place takes its people with it,
    the same way the pipeline drops anything it cannot put on the map.
    """
    counts = {}
    for country, bucket, n in rows:
        region_id = placed.get(country)
        if region_id is None:
            continue
        hidden, visible = counts.get(region_id, (0, 0))
        counts[region_id] = ((hidden + n, visible) if bucket == "hidden"
                             else (hidden, visible + n))
    return counts


def report(counts, low, high):
    total_hidden = sum(h for h, _ in counts.values())
    total_visible = sum(v for _, v in counts.values())
    print(f"\npeople born {low}-{high - 1}, by the region of their citizenship\n"
          f"'hidden' means fewer than {FLOOR} sitelinks — never fetched by a real run\n")
    print("region".ljust(22) + "total".rjust(8) + "hidden".rjust(8)
          + "visible".rjust(9) + "  share hidden")

    for region in sorted(regions_mod.REGIONS, key=lambda r: r.id):
        hidden, visible = counts.get(region.id, (0, 0))
        total = hidden + visible
        share = f"{hidden / total:.0%}" if total else "-"
        print(region.name.ljust(22) + str(total).rjust(8) + str(hidden).rjust(8)
              + str(visible).rjust(9) + share.rjust(15))

    total = total_hidden + total_visible
    print("\n" + "world".ljust(22) + str(total).rjust(8) + str(total_hidden).rjust(8)
          + str(total_visible).rjust(9)
          + (f"{total_hidden / total:.0%}" if total else "-").rjust(15))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", type=int, default=1800,
                        help="First year of the window to sample (default 1800).")
    parser.add_argument("--span", type=int, default=1,
                        help="Years to sample (default 1). The query cost scales "
                             "with this, and a wide window is what WDQS refuses.")
    parser.add_argument("--cache", default="raw/floor-cache")
    parser.add_argument("--contact", default=os.environ.get("MEANWHILE_CONTACT"),
                        help="Contact address for the User-Agent header, which "
                             "Wikidata requires.")
    args = parser.parse_args()

    if not args.contact:
        parser.error("Wikidata rejects anonymous queries. Pass --contact your@email.com, "
                     "or set the MEANWHILE_CONTACT environment variable.")

    low, high = args.start, args.start + args.span
    cache_dir = Path(args.cache)
    rows = fetch_window(low, high, args.contact, cache_dir)
    report(tally(rows, place(rows, args.contact, cache_dir)), low, high)


if __name__ == "__main__":
    main()
