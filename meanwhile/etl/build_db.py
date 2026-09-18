"""Stage 3: normalised rows to the SQLite file the app ships, plus a coverage report.

Also writes mockup_data.json, which the HTML prototype can load in place of its
hand-written sample — the cheapest way to find out whether the design survives
real data before any Kotlin gets written.

Usage:
    python build_db.py --raw raw/ --out dist/meanwhile.db
"""

import argparse
import json
import sqlite3
from pathlib import Path

import regions as regions_mod
import transform as tf
from dates import format_year

SCHEMA = """
PRAGMA journal_mode = OFF;

CREATE TABLE regions (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  lng       REAL NOT NULL,
  lat       REAL NOT NULL
);

CREATE TABLE entries (
  id               INTEGER PRIMARY KEY,
  qid              TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL,
  type             TEXT NOT NULL CHECK (type IN ('person','polity','event')),
  description      TEXT NOT NULL DEFAULT '',
  birth_year       INTEGER,
  birth_precision  TEXT,
  death_year       INTEGER,
  death_precision  TEXT,
  active_start     INTEGER NOT NULL,
  active_end       INTEGER NOT NULL,
  date_confidence  TEXT NOT NULL,
  region_id        INTEGER NOT NULL REFERENCES regions(id),
  notability       INTEGER NOT NULL DEFAULT 0,
  wiki_title       TEXT,
  lat              REAL,
  lng              REAL
);

CREATE INDEX idx_entries_start  ON entries(active_start);
CREATE INDEX idx_entries_end    ON entries(active_end);
CREATE INDEX idx_entries_region ON entries(region_id, notability DESC);
CREATE INDEX idx_entries_type   ON entries(type);

CREATE VIRTUAL TABLE entries_fts USING fts5(
  name, description, content='entries', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, name, description)
  VALUES (new.id, new.name, new.description);
END;
"""

# The app's two core reads, kept here so the schema and the queries that depend
# on it stay together.
QUERY_YEAR_WINDOW = """
SELECT * FROM entries
WHERE active_start < :year + :step AND active_end >= :year
ORDER BY region_id, notability DESC;
"""

QUERY_CONTEMPORARIES = """
SELECT * FROM entries
WHERE type = 'person' AND id != :id
  AND active_start + 15 <= :active_end
  AND active_end >= :active_start + 15
ORDER BY region_id, notability DESC;
"""


def build(rows, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    connection = sqlite3.connect(out_path)
    connection.executescript(SCHEMA)

    connection.executemany(
        "INSERT INTO regions (id, name, lng, lat) VALUES (?, ?, ?, ?)",
        [(r.id, r.name, r.lng, r.lat) for r in regions_mod.REGIONS])

    connection.executemany("""
        INSERT INTO entries
          (qid, name, type, description, birth_year, birth_precision,
           death_year, death_precision, active_start, active_end,
           date_confidence, region_id, notability, wiki_title, lat, lng)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(r.qid, r.name, r.type, r.description, r.birth_year, r.birth_precision,
           r.death_year, r.death_precision, r.active_start, r.active_end,
           r.date_confidence, r.region_id, r.notability, r.wiki_title, r.lat, r.lng)
          for r in rows])

    connection.commit()
    connection.execute("VACUUM")
    connection.close()
    return out_path.stat().st_size


def report(rows):
    """Print what the dataset actually looks like — the point of the whole run."""
    by_type, by_confidence, by_region_confidence = {}, {}, {}
    for row in rows:
        by_type[row.type] = by_type.get(row.type, 0) + 1
        by_confidence[row.date_confidence] = by_confidence.get(row.date_confidence, 0) + 1
        by_region_confidence[row.region_confidence] = by_region_confidence.get(row.region_confidence, 0) + 1

    print(f"\n{len(rows)} entries")
    print("  by type:  " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
    print("  dates:    " + ", ".join(f"{k} {v}" for k, v in sorted(by_confidence.items())))
    print("  regions:  " + ", ".join(f"{k} {v}" for k, v in sorted(by_region_confidence.items())))

    table = tf.coverage(rows)
    buckets = sorted({bucket for _, bucket in table})
    print("\nrows per region per 500 years — a row of zeros is a hole in the premise\n")
    header = "region".ljust(22) + "".join(format_year(b).rjust(9) for b in buckets)
    print(header)
    for region in sorted(regions_mod.REGIONS, key=lambda r: r.id):
        cells = "".join(str(table.get((region.id, b), 0)).rjust(9) for b in buckets)
        print(region.name.ljust(22) + cells)


def write_mockup_json(rows, path, per_cell=12, era=250):
    """A trimmed slice the HTML prototype can eat, so the design can be tested
    against real data before any app code exists.

    Sampled per region *per era*, not per region: the dataset is so weighted
    towards recent Europe that a flat per-region cap would hand the prototype
    the nineteenth century and nothing before it, which is the one thing the
    prototype is meant to test.
    """
    kept, counts = [], {}
    for row in sorted(rows, key=lambda r: -r.notability):
        for bucket in range(row.active_start // era, row.active_end // era + 1):
            key = (row.region_id, bucket)
            if counts.get(key, 0) < per_cell:
                counts[key] = counts.get(key, 0) + 1
                kept.append([row.name, row.type, row.active_start, row.active_end,
                             row.region_id, row.notability, row.description, row.wiki_title])
                break
    path.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(kept)} entries -> {path} (for the HTML prototype)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", default="raw")
    parser.add_argument("--out", default="dist/meanwhile.db")
    args = parser.parse_args()

    raw_dir = Path(args.raw)
    rows = []
    for type_ in (tf.PERSON, tf.POLITY, tf.EVENT):
        source = raw_dir / f"{type_}.json"
        if not source.exists():
            print(f"skipping {type_}: {source} not found")
            continue
        records = json.loads(source.read_text(encoding="utf-8"))
        produced = tf.transform(records, type_)
        print(f"{type_}: {len(records)} raw -> {len(produced)} kept")
        rows.extend(produced)

    if not rows:
        raise SystemExit("no rows — run extract.py first")

    deduplicated = tf.deduplicate(rows)
    if len(deduplicated) != len(rows):
        print(f"{len(rows) - len(deduplicated)} item(s) matched more than one "
              f"type and were resolved to one row")
    rows = deduplicated

    out = Path(args.out)
    size = build(rows, out)
    report(rows)
    print(f"\n{out} — {size / 1_048_576:.1f} MB")
    write_mockup_json(rows, out.parent / "mockup_data.json")


if __name__ == "__main__":
    main()
