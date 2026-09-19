"""What each notability threshold admits, per region.

RELAXED_REGIONS in transform.py lowers the sitelink bar where a flat cutoff
would misrepresent the world. Its multipliers were guessed before there was
any data to check them against, and one guess has already proved wrong: East
Asia is not in the list, and holds 1.2% of the 1500-1999 rows.

This prints the numbers those multipliers should be chosen from — how many
entries each region would keep at each cutoff, and what share of the world
that leaves it with. It reads the same raw files build_db.py reads and changes
nothing.

A caveat worth keeping in view: relaxing a threshold does not find more
history, it admits less-linked entries. That is the right trade for an app
whose premise is that something was happening everywhere, but it is a
judgement about what to show, not a discovery about the world.

The floor is 4. extract.py filters at MIN_SITELINKS, so entries below that
were never fetched and no threshold here can bring them back.

Usage:
    python thresholds.py --raw raw
    python thresholds.py --raw raw --type event
    python thresholds.py --raw raw --era 1500       # only rows alive 1500-1999
"""

import argparse
import json
from pathlib import Path

import regions as regions_mod
import transform as tf

CANDIDATES = (4, 5, 6, 8, 10, 15, 20, 30)

# Thresholds off, so build_row keeps everything it can place in time and space
# and the cutoff becomes something we apply here instead.
KEEP_ALL = {tf.PERSON: 0, tf.POLITY: 0, tf.EVENT: 0}


def tally(records, type_, era=None, span=500):
    """{region_id: [notability, ...]} for rows that clear everything but the bar."""
    best = {}
    for record in records:
        row = tf.build_row(record, type_, KEEP_ALL)
        if not row:
            continue
        if era is not None and not (row.active_start < era + span and row.active_end >= era):
            continue
        seen = best.get(row.qid)
        if seen is None or row.notability > seen[1]:
            best[row.qid] = (row.region_id, row.notability)

    scores = {}
    for region_id, notability in best.values():
        scores.setdefault(region_id, []).append(notability)
    return scores


def kept(scores, region_id, threshold):
    return sum(1 for n in scores.get(region_id, ()) if n >= threshold)


def report(scores, type_, thresholds=CANDIDATES):
    """Per region: what it keeps now, and what each cutoff would keep instead."""
    now = {region.id: tf.threshold_for(type_, region.id)
           for region in regions_mod.REGIONS}
    current = {rid: kept(scores, rid, bar) for rid, bar in now.items()}
    total_now = sum(current.values()) or 1

    print(f"\n{sum(len(v) for v in scores.values())} {type_} rows placed in time "
          f"and space, before any notability cutoff\n")
    print("region".ljust(22) + "bar".rjust(6) + "now".rjust(8) + "share".rjust(7)
          + "".join(f">={t}".rjust(8) for t in thresholds))

    for region in sorted(regions_mod.REGIONS, key=lambda r: r.id):
        counts = "".join(str(kept(scores, region.id, t)).rjust(8) for t in thresholds)
        print(region.name.ljust(22)
              + f"{now[region.id]:.1f}".rjust(6)
              + str(current[region.id]).rjust(8)
              + f"{current[region.id] / total_now:.1%}".rjust(7)
              + counts)

    print(f"\n{'total':22}{'':6}{total_now:8}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", default="raw")
    parser.add_argument("--type", default=tf.PERSON, choices=[tf.PERSON, tf.POLITY, tf.EVENT])
    parser.add_argument("--era", type=int,
                        help="Only count rows alive during the 500 years from here, "
                             "e.g. 1500 for 1500-1999.")
    args = parser.parse_args()

    source = Path(args.raw) / f"{args.type}.json"
    if not source.exists():
        raise SystemExit(f"{source} not found — run extract.py first")

    records = json.loads(source.read_text(encoding="utf-8"))
    if args.era is not None:
        print(f"rows alive during {args.era}-{args.era + 499}")
    report(tally(records, args.type, args.era), args.type)


if __name__ == "__main__":
    main()
