"""Inline the real data into the HTML prototype.

The prototype ships with a few dozen hand-written entries, which is enough to
argue about a layout and not enough to find out whether the layout survives
contact with the dataset. This puts real rows into a copy of the page.

Two builds, because they answer different questions.

The default reads dist/mockup_data.json: twelve entries per region per era,
which is what keeps the nineteenth century from crowding out the Bronze Age.
It tests *spread* — whether the world shows up, whether sparse regions read as
sparse, whether the names and dates look like history.

--from-db reads the database instead and raises the cap, so a crowded cell
arrives crowded. That tests *density* — what Western Europe at 1850 actually
does to a bottom sheet when it is thirty thousand entries rather than twelve.
The default build cannot answer that, because flattening every crowded cell to
twelve is precisely what it does.

Inlining rather than fetching: a page opened from the filesystem cannot fetch
a sibling file, and requiring a local web server to look at a mockup is a good
way to not look at the mockup.

Usage:
    python build_prototype.py
    python build_prototype.py --from-db --per-cell 400 --out dist/dense.html
"""

import argparse
import json
import sqlite3
from pathlib import Path

import build_db

# The line build_prototype writes over. If the prototype stops containing it,
# the build should fail loudly rather than quietly produce the sample again.
MARKER = "  const LOADED = null; // %%MOCKUP_DATA%%"


def inline(html, rows):
    if MARKER not in html:
        raise SystemExit(
            f"marker not found in the prototype — looked for:\n{MARKER}\n"
            "If the prototype was restructured, update MARKER to match.")
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return html.replace(MARKER, f"  const LOADED = {data};", 1)


def rows_from_db(path, per_cell, era=250):
    """The same sampling build_db uses, at whatever cap is asked for."""
    connection = sqlite3.connect(path)
    rows = [list(r) for r in connection.execute(
        "SELECT name, type, active_start, active_end, region_id, notability, "
        "description, wiki_title FROM entries")]
    connection.close()
    print(f"  {len(rows)} entries in the database")
    return build_db.sample(rows, per_cell, era)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prototype", default="../prototype.html")
    parser.add_argument("--data", default="dist/mockup_data.json")
    parser.add_argument("--db", default="dist/meanwhile.db")
    parser.add_argument("--from-db", action="store_true",
                        help="Sample from the database at --per-cell instead of "
                             "reading the trimmed slice, to see real density.")
    parser.add_argument("--per-cell", type=int, default=400,
                        help="With --from-db: entries per region per 250 years "
                             "(default 400). The slice uses 12.")
    parser.add_argument("--out", default="dist/prototype.html")
    args = parser.parse_args()

    source, out = Path(args.prototype), Path(args.out)
    if not source.exists():
        raise SystemExit(f"{source} not found")

    if args.from_db:
        database = Path(args.db)
        if not database.exists():
            raise SystemExit(f"{database} not found — run build_db.py first")
        rows = rows_from_db(database, args.per_cell)
    else:
        data = Path(args.data)
        if not data.exists():
            raise SystemExit(f"{data} not found — run build_db.py first")
        rows = json.loads(data.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inline(source.read_text(encoding="utf-8"), rows), encoding="utf-8")

    by_type = {}
    for row in rows:
        by_type[row[1]] = by_type.get(row[1], 0) + 1
    size = out.stat().st_size / 1024
    print(f"{out} — {len(rows)} entries "
          f"({', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))}), "
          f"{size:.0f} KB")
    if not args.from_db:
        print("This is the 12-per-cell slice: it shows the world's spread, not "
              "its density.\nFor density: python build_prototype.py --from-db "
              "--out dist/dense.html")
    elif size > 8000:
        print("That is a big page; if the browser struggles, lower --per-cell.")
    print("Open it in a browser; no server needed.")


if __name__ == "__main__":
    main()
