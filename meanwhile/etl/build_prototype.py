"""Inline the real data into the HTML prototype.

The prototype ships with a few dozen hand-written entries, which is enough to
argue about a layout and not enough to find out whether the layout survives
contact with the dataset. build_db.py writes dist/mockup_data.json — a slice
sampled per region per era, so the nineteenth century doesn't crowd out
everything else. This puts that slice into a copy of the page.

Inlining rather than fetching: a page opened from the filesystem cannot fetch
a sibling file, and requiring a local web server to look at a mockup is a good
way to not look at the mockup.

Usage:
    python build_prototype.py
    python build_prototype.py --out dist/prototype.html
"""

import argparse
import json
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prototype", default="../prototype.html")
    parser.add_argument("--data", default="dist/mockup_data.json")
    parser.add_argument("--out", default="dist/prototype.html")
    args = parser.parse_args()

    source, data, out = Path(args.prototype), Path(args.data), Path(args.out)
    for path in (source, data):
        if not path.exists():
            raise SystemExit(f"{path} not found — run build_db.py first")

    rows = json.loads(data.read_text(encoding="utf-8"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inline(source.read_text(encoding="utf-8"), rows), encoding="utf-8")

    by_type = {}
    for row in rows:
        by_type[row[1]] = by_type.get(row[1], 0) + 1
    print(f"{out} — {len(rows)} entries "
          f"({', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))}), "
          f"{out.stat().st_size / 1024:.0f} KB")
    print("Open it in a browser; no server needed.")


if __name__ == "__main__":
    main()
