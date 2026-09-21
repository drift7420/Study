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

This uses the extraction query itself, which is the one shape WDQS reliably
answers, so it should cost about what one chunk of a real run costs, plus a
coordinate pass over a few thousand items.

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
import transform as tf

FLOOR = extract.MIN_SITELINKS["person"]

# Thresholds off: we want everything that can be placed, and will apply the
# floor here so both sides of it are visible.
KEEP_ALL = {tf.PERSON: 0, tf.POLITY: 0, tf.EVENT: 0}


def fetch_window(low, high, contact, cache_dir):
    """Every person born in [low, high) with at least one sitelink."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"floor-person-{low}-{high}.json"
    if cached.exists():
        print(f"  {low}-{high}: cached")
        return json.loads(cached.read_text(encoding="utf-8"))

    print(f"  {low}-{high}: querying every birth with 1+ sitelinks…")
    payload = extract.run_query(
        extract.main_query("person", "P569", low, high, min_sitelinks=1), contact)
    records = [extract.flatten(b, "person") for b in payload["results"]["bindings"]]
    cached.write_text(json.dumps(records), encoding="utf-8")
    print(f"  {low}-{high}: {len(records)} people")
    return records


def place(records, contact, cache_dir):
    ordered = extract.by_notability(records)
    print(f"  placing {len(ordered)} people…")
    coordinates, failed = extract.fetch_coordinates(ordered, "person", contact, cache_dir)
    extract.merge_coordinates(records, coordinates)
    if failed:
        print(f"  {len(failed)} could not be asked about; they are left out")
    return records


def tally(records):
    """{region_id: (below the floor, at or above it)}."""
    best = {}
    for record in records:
        row = tf.build_row(record, tf.PERSON, KEEP_ALL)
        if row and row.notability > best.get(row.qid, (None, -1))[1]:
            best[row.qid] = (row.region_id, row.notability)

    counts = {}
    for region_id, notability in best.values():
        hidden, visible = counts.get(region_id, (0, 0))
        counts[region_id] = ((hidden + 1, visible) if notability < FLOOR
                             else (hidden, visible + 1))
    return counts


def report(counts, low, high):
    total_hidden = sum(h for h, _ in counts.values())
    total_visible = sum(v for _, v in counts.values())
    print(f"\npeople born {low}-{high - 1}, placed by region\n"
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
    parser.add_argument("--span", type=int, default=2,
                        help="Years to sample (default 2). The query cost scales "
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
    records = fetch_window(low, high, args.contact, cache_dir)
    report(tally(place(records, args.contact, cache_dir)), low, high)


if __name__ == "__main__":
    main()
