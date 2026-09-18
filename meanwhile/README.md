# Meanwhile — data pipeline

A synchronic view of world history: pick a moment, see what was happening
everywhere at once, and look up a person to find who else was alive.

This directory holds the ETL that turns Wikidata into the SQLite file the
Android app will ship, plus the HTML prototype the design was worked out in.

## Why the pipeline comes before the app

The riskiest assumption in the project is not the UI — it's that Wikidata
yields a usable ~20MB dataset with tolerable coverage outside Europe. The
pipeline answers that question, and `build_db.py` prints a coverage table
(rows per region per 500 years) that says plainly where the holes are. Run it
before writing Kotlin, load its output into the prototype, and find out whether
the design survives real data.

## Running it

```bash
cd etl
pip install pytest          # only needed for the tests

# 1. pull raw records (needs network; put a contact address in USER_AGENT first)
python extract.py --type person --out raw/person.json
python extract.py --type polity --out raw/polity.json
python extract.py --type event  --out raw/event.json

# 2 + 3. normalise, build the database, print the coverage report
python build_db.py --raw raw --out dist/meanwhile.db
```

`build_db.py` also writes `dist/mockup_data.json`, a trimmed slice the HTML
prototype can load in place of its hand-written sample.

## Stages

| File | Does | Tested |
|---|---|---|
| `extract.py` | SPARQL against WDQS, cached per chunk, resumable | **No — see below** |
| `dates.py` | Wikidata time values to astronomical years and intervals | Yes |
| `regions.py` | Coordinates to one of 22 regions | Yes |
| `transform.py` | Raw records to rows: spans, confidence, thresholds | Yes |
| `build_db.py` | SQLite + FTS5 + indexes, coverage report, prototype JSON | Yes |

```bash
python -m pytest tests/ -q      # 59 tests
```

### extract.py is the untested stage

It was written in an environment whose network policy blocks
`query.wikidata.org`, so it has never been run. Everything downstream of it is
covered by tests against fixtures shaped like real WDQS output. Expect to tune
it on first use:

- **Timeouts.** WDQS cuts queries off at 60 seconds. `BUCKETS` splits the work
  by sitelink count; if a bucket times out, split it further rather than
  retrying it unchanged.
- **User agent.** WDQS blocks anonymous clients. Put a real contact address in
  `USER_AGENT` before running.
- **Class lists.** The `VALUES ?class { … }` sets in the polity and event
  queries are a first guess at how Wikidata types states and occurrences, which
  it does inconsistently. Check what comes back and adjust.

## Decisions worth knowing

**Astronomical years.** 1 BCE is 0, 2 BCE is −1, so plain integer comparison
does interval arithmetic. Wikidata writes 551 BCE as `-0551`, which is
astronomical −550 — an off-by-one that `dates.py` handles and a test pins down.

**Imprecision widens the interval.** A century-precision date covers its whole
century rather than being guessed to a point, so vagueness flows through the
overlap queries without special cases.

**Every row gets a span.** A person with only a birth year still gets an
`active_start`/`active_end`, inferred and flagged. Dropping partial records
would quietly delete exactly the thin-coverage regions the app exists to show.

**Thresholds relax by region.** Sitelink counts measure who writes Wikipedia,
so one global cutoff empties sub-Saharan Africa and pre-Columbian America while
keeping European minor nobility. `RELAXED_REGIONS` in `transform.py` lowers the
bar where a flat cutoff would misrepresent the world.

**Region boxes are ordered.** First match wins, so the list runs specific to
general: East Asia before Southeast Asia or Guangzhou lands in the wrong one;
Egypt's box stops at 34°E so it doesn't reach across Sinai and claim Jerusalem;
Arabia is split in two so Aksum stays on the African side of the Red Sea. Each
of those is a test.

**The year view queries a window, not a point.** At 10-year resolution the
screen shows a decade, so the query is `active_start < :year + :step AND
active_end >= :year`. A point query silently loses every single-year event that
doesn't fall on a slider step — the Fall of Constantinople in 1453 is invisible
when the steps are 1450 and 1460. Both the working query and the broken one are
in the tests.

## Prototype

`prototype.html` is the design prototype — plain HTML/CSS/JS, not Android code.
It settled the variable-rate time scale, the region ordering, the map
treatment, and the detail and search screens. None of it ships; what carries
over is the schema, the segment table, the region list, the projection maths
and the query logic.
