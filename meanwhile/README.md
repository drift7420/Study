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

Python 3.10 or newer. The pipeline itself needs no third-party packages;
`pytest` is only for the tests.

Wikidata rejects anonymous queries, so pass a contact address with
`--contact` (or set `MEANWHILE_CONTACT` once in your shell).

```bash
cd etl

# 1. pull raw records — the only stage that needs network
python extract.py --type person --out raw/person.json --contact you@example.com
python extract.py --type polity --out raw/polity.json --contact you@example.com
python extract.py --type event  --out raw/event.json  --contact you@example.com

# 2 + 3. normalise, build the database, print the coverage report
python build_db.py --raw raw --out dist/meanwhile.db

# 4. put real rows into a copy of the prototype and open it
python build_prototype.py                                    # spread
python build_prototype.py --from-db --out dist/dense.html    # density
```

`build_db.py` also writes `dist/mockup_data.json`, a slice sampled per region
per era; `build_prototype.py` inlines it into `dist/prototype.html`, which
opens from the filesystem with no server. `prototype.html` itself still runs on
its hand-written sample, so it stays viewable without a build.

**The two builds answer different questions, and the default cannot answer
both.** The slice caps every region at twelve entries per 250 years, which is
what stops the nineteenth century crowding out the Bronze Age — so it shows
whether the world's *spread* survives the design, and nothing about density,
because flattening crowded cells is exactly what it does. `--from-db` samples
from the database at a much higher cap, so a crowded cell arrives crowded:
that is the build that shows what Western Europe in 1850 does to a bottom
sheet when it is tens of thousands of entries rather than twelve.

## Stages

| File | Does | Tested |
|---|---|---|
| `extract.py` | SPARQL against WDQS, cached per chunk, resumable | Everything but the network |
| `dates.py` | Wikidata time values to astronomical years and intervals | Yes |
| `regions.py` | Coordinates to one of 22 regions | Yes |
| `transform.py` | Raw records to rows: spans, confidence, thresholds | Yes |
| `build_db.py` | SQLite + FTS5 + indexes, coverage report, prototype JSON | Yes |
| `thresholds.py` | What each notability cutoff would admit, per region | Yes |
| `probe_floor.py` | What the sitelink floor hides, by region — **unanswered** | Yes |
| `build_prototype.py` | Inlines real rows into a copy of the prototype | Yes |

```bash
python -m pytest tests/ -q      # 137 tests
```

### extract.py is the stage the network shapes

It was written where `query.wikidata.org` is blocked, so its queries could not
be tried until it ran on a real machine. The windowing, subdivision, batching
and shaping are tested; the SPARQL is not, and every constant in the file is
there because WDQS refused the work some particular way:

- **Lead with the date filter.** Starting from `?item wdt:P31 wd:Q5` scans every
  human before anything narrows it, and the optimiser will not reliably push a
  sitelink filter ahead of that. A 502 is what that looks like.
- **Range comparisons, never `YEAR()`.** A function call cannot use the date
  index. The BCE window carries only an upper bound, which covers everything
  before year 1 without negative date literals.
- **Ask for gzip.** The densest windows return 40MB+ of JSON, and those were
  the ones that arrived truncated — a JSON parse error tens of thousands of
  lines in, not a network error.
- **Halve a window that fails, and remember it.** There is no way to ask what a
  query will cost, and the budget moves with load, so the answer to a failure
  is a narrower window. A window that split leaves a `.split` marker, so later
  runs skip straight to the halves.
- **A 429 is not a query that is too big.** It means we asked too fast. Waiting
  out `Retry-After` is the fix; splitting the window is not, and neither is a
  five-second retry.
- **One failed request should cost one request.** The coordinate pass makes
  hundreds of them. Letting one raise discarded a two-hour extraction before
  anything reached disk, which looks exactly like a run that did nothing. A
  batch that fails is halved, like a date window.
- **One property per query.** Asking for three location properties at once —
  three UNION branches, each joining through a statement node — failed on 288
  batches out of 288, at 1000 items per batch and at 400. Asked one at a time
  it is a single property path returning a WKT literal, and the second and
  third passes only run over what the first could not place.
- **Most notable first, and interruptible.** Coordinates are fetched in
  notability tiers, so a pass stopped with Ctrl-C leaves a smaller database
  rather than an arbitrary slice of one. Everything fetched is cached, so a
  re-run picks up where it stopped.
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

**The bar is held only where sitelinks mean notability.** A sitelink count
measures how many language editions wrote about someone, which is a fair proxy
in a region several editions cover and a poor one in a region covered mostly
by its own language. `STRICT_REGIONS` in `transform.py` names the six where
the full cutoff applies — the four Europes, North America and Oceania —
and everywhere else drops to the extraction floor.

That list replaced nine multipliers picked by intuition, after `thresholds.py`
showed what they were doing: Oceania, relaxed to 4, held 3.7% of all 1500-1999
entries, more than East Asia and Japan & Korea together, while a Qing official
needed ten sitelinks. Australia and New Zealand are English-language subjects
with dense coverage. In the rebuild, Western Europe : East Asia for 1500-1999
went from 30.5:1 to 11.6:1, East Asia's first five centuries CE from 519 rows
to 1,136, and the database from 112,000 entries to 130,300.

Oceania is still one region holding both Sydney and Vanuatu, and the strict bar
is right for the first and wrong for the second. Splitting it is the better
fix; the region boundaries are the limitation, not the multiplier.

**Two things that turned out not to explain the coverage gap.** Requiring place
of birth was dropping a third of all people, and the guess was that it did so
unevenly enough to explain East Asia's thinness. Falling back to place of death
and country raised placement from 66% to 95% and added 33,000 entries — but it
raised every region by about the same third, so Western Europe : East Asia for
1500-1999 moved from 30.9:1 to 30.5:1.

The notability cutoff was the next suspect, and it isn't that either: the ratio
is ~40:1 at *every* cutoff from 4 sitelinks to 30. Relaxing the bar scales both
sides equally.

What remains untested is the extraction floor itself. `extract.py` fetches
nothing below 4 sitelinks, and a figure covered by one language has one, so the
whole coverage table describes only the part of Wikidata above a line drawn in
a unit that counts languages. `probe_floor.py` fetches a short window of births
with the floor dropped to 1, places them by region, and reports what share of
each region sits below it.

It asked this per Wikipedia edition first, joining each item to
`?article schema:isPartOf <https://xx.wikipedia.org/>`. That join costs what
the *edition* costs rather than what the window costs: Swahili answered a
ten-year window while English timed out on a single year, so narrowing the
window — the obvious fix, and the one tried first — could not have worked.
Asking per region is both cheaper and the better question, since regions are
what the app shows.

Its second version borrowed the extraction query whole, and that was still too
much: the extraction query carries labels, descriptions, an article link and
three optional dates because the app needs them, and at a floor of 1 there are
several times as many rows to carry all of it on. Cut to two columns, the query
finally ran — and WDQS cut the transfer three times mid-stream, twenty thousand
rows in. A successful query whose answer will not arrive is a different problem
from a refusal, and not one a smaller SELECT solves.

So the counting moved to the server: one aggregate query returning a few
hundred rows, grouped by country of citizenship, and a second small query to
place those countries. Two caveats come with that. People with no citizenship
recorded are missing, and if citizenship is recorded less often for the thinly
covered, that biases the population being measured. And every citizen of a
state lands where that state's coordinate puts it. Neither distorts the share
on each side of the floor within a region, which is the question.

**That version was refused too, and the question is still open.** Five shapes
were tried over three days — per edition at a decade, per edition narrowing to
one year, the extraction query borrowed whole, two bare columns, and finally a
server-side aggregate of a few hundred rows. The last of them is small by any
measure and WDQS returned 504, 502 and 503 to it. Over the same days the
endpoint also began refusing extraction windows it had answered earlier in the
week, so the most likely reading is that the service is unwell rather than that
the query is wrong. `probe_floor.py` is left in the repository because the
question is worth answering when WDQS recovers, and because what it costs is
now three small requests.

Until then, what the coverage table shows should be read as *the part of
Wikidata above four sitelinks*, and the 40:1 early-modern gap between Western
Europe and East Asia is not known to be the source's rather than the floor's.

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
