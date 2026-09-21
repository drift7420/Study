"""Stage 1: pull raw records from the Wikidata Query Service.

This is the only stage that needs network, and the only one not covered by
tests — it could not be run in the environment it was written in.

Four things, each learned from a way WDQS refused the work:

*Lead with the selective pattern.* Starting from `?item wdt:P31 wd:Q5` scans
every human in Wikidata before any filter narrows it, and the optimiser does
not reliably push a sitelink filter ahead of that. Leading with a date window
cuts the candidate set first.

*Range comparisons, never YEAR().* A function call cannot use the date index,
so YEAR(?d) < 1 scans every date Wikidata holds. The first window carries only
an upper bound, which covers everything BCE without negative date literals.

*Ask for gzip.* The densest windows return 40MB+ of JSON, and those were
exactly the ones that arrived truncated — surfacing as a JSON parse error
tens of thousands of lines in, not as a network error. Compressed they are a
few MB, and a truncated one now fails cleanly at decompression.

*Halve a window that fails, once.* WDQS offers no way to ask what a query
will cost, and its budget moves with load, so the useful response to a failure
is a narrower window rather than another identical attempt. A window that had
to split leaves a marker behind, so the next run goes straight to the halves
instead of spending three retries rediscovering that the whole is too big.

*A 429 is not a query that is too big.* It means we asked too fast, and the
answer is to wait — for as long as Retry-After says, or a minute — not to
halve the window or count the attempt against the retry budget.

*One failed batch should cost one batch.* The coordinate pass runs hundreds of
requests; letting any one of them raise discarded a two-hour extraction before
anything was written to disk. Every batch is now survivable on its own, and a
batch that fails is halved before it is given up on.

*One property per query.* Asking for three location properties at once — three
UNION branches, each joining through a statement node — failed on 288 batches
out of 288, at 1000 items and at 400. Asked one property at a time it is a
single cheap join, and the second and third passes only run over what the
first could not place.

Queries go by POST — a long query in a GET URL can be refused by the proxy
in front of WDQS, which surfaces as a 502.

WDQS blocks anonymous clients, so every run needs a contact address. Pass it
with --contact, or set MEANWHILE_CONTACT once in your shell.

Usage:
    python extract.py --probe --contact you@example.com
    python extract.py --type person --out raw/person.json --contact you@example.com
    python extract.py --type polity --out raw/polity.json --contact you@example.com
    python extract.py --type event  --out raw/event.json  --contact you@example.com
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "MeanwhileETL/0.1 (https://github.com/drift7420/Study; contact: {contact})"

MIN_SITELINKS = {"person": 4, "polity": 3, "event": 4}

# Coordinate lookups are cheap per item and expensive per request, so batches
# want to be large — but a batch that is too large for WDQS today is halved
# rather than abandoned, so this is a starting point, not a commitment.
COORD_BATCH = 500
MIN_COORD_BATCH = 25
COORD_PAUSE = 2
COORD_REPORT_EVERY = 5000

# WDQS throttles a client that has been running for hours. A 429 says nothing
# about the query, so it waits rather than retrying fast or giving up.
MAX_THROTTLE_WAITS = 4
THROTTLE_PAUSE = 60          # when the response doesn't say how long to wait
MAX_THROTTLE_PAUSE = 300     # ...and a ceiling on what it asks for

PROBE = "SELECT ?x WHERE { BIND(1 AS ?x) }"

LABEL_SERVICE = """
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" .
                           ?item rdfs:label ?name ; schema:description ?desc . }
"""

# Every query is driven by a range comparison on a truthy date property, never
# by YEAR(): a function call cannot use the date index, so YEAR(?d) < 1 scans
# every date in Wikidata and times the query out. A bare upper bound covers
# everything BCE without needing negative date literals, which Blazegraph
# handles unevenly.
DATE_WINDOWS = {"person": 50, "polity": 100, "event": 100}
LAST_YEAR = {"person": 1900, "polity": 1900, "event": 1950}
MIN_WINDOW = 1          # stop subdividing a stubborn window at one year

# (property, whether to run it across all windows or only the BCE chunk)
DRIVERS = {
    "person": [("P569", True), ("P570", True), ("P1317", False)],
    "polity": [("P571", True)],
    "event": [("P585", True), ("P580", True)],
}

PERSON_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?birth ?birthPrec ?death ?deathPrec ?floruit ?floruitPrec ?article
WHERE {
  ?item wdt:%DRIVER% ?driver .
  %DATEFILTER%
  ?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %MIN%)
  OPTIONAL { ?item p:P569/psv:P569 ?bn .
             ?bn wikibase:timeValue ?birth ; wikibase:timePrecision ?birthPrec . }
  OPTIONAL { ?item p:P570/psv:P570 ?dn .
             ?dn wikibase:timeValue ?death ; wikibase:timePrecision ?deathPrec . }
  OPTIONAL { ?item p:P1317/psv:P1317 ?fn .
             ?fn wikibase:timeValue ?floruit ; wikibase:timePrecision ?floruitPrec . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
%LABEL%
}
"""

POLITY_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?inception ?inceptionPrec ?dissolved ?dissolvedPrec ?article
WHERE {
  ?item wdt:%DRIVER% ?driver .
  %DATEFILTER%
  ?item wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %MIN%)
  ?item wdt:P31/wdt:P279* ?class .
  VALUES ?class { wd:Q3624078 wd:Q3024240 wd:Q417175 wd:Q48349 wd:Q164950
                  wd:Q133156 wd:Q1250464 wd:Q28171280 }
  OPTIONAL { ?item p:P571/psv:P571 ?in .
             ?in wikibase:timeValue ?inception ; wikibase:timePrecision ?inceptionPrec . }
  OPTIONAL { ?item p:P576/psv:P576 ?dn .
             ?dn wikibase:timeValue ?dissolved ; wikibase:timePrecision ?dissolvedPrec . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
%LABEL%
}
"""

EVENT_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?point ?pointPrec ?start ?startPrec ?end ?endPrec ?article
WHERE {
  ?item wdt:%DRIVER% ?driver .
  %DATEFILTER%
  ?item wikibase:sitelinks ?sitelinks .
  FILTER(?sitelinks >= %MIN%)
  ?item wdt:P31/wdt:P279* ?class .
  VALUES ?class { wd:Q1190554 wd:Q178561 wd:Q198 wd:Q13418847 }
  OPTIONAL { ?item p:P585/psv:P585 ?pn .
             ?pn wikibase:timeValue ?point ; wikibase:timePrecision ?pointPrec . }
  OPTIONAL { ?item p:P580/psv:P580 ?sn .
             ?sn wikibase:timeValue ?start ; wikibase:timePrecision ?startPrec . }
  OPTIONAL { ?item p:P582/psv:P582 ?en .
             ?en wikibase:timeValue ?end ; wikibase:timePrecision ?endPrec . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
%LABEL%
}
"""

TEMPLATES = {"person": PERSON_QUERY, "polity": POLITY_QUERY, "event": EVENT_QUERY}

# Second pass: coordinates only, keyed by QID.
#
# Place of birth alone lost a third of all people, and not evenly: Wikidata
# records P19 far more consistently for Europeans than for, say, Ming-dynasty
# officials, so requiring it quietly emptied whole regions. Each type now tries
# several properties, most specific first — a country centroid is a poor
# location but a far better one than dropping the person entirely.
#
# One property per query, not all three at once. The combined query (three
# UNION branches, each joining to a statement node) timed out on every batch
# we ever sent it. `wdt:P19/wdt:P625` is one property path returning a WKT
# literal, with no statement node to join to and no union to evaluate.
#
# A None property means the item carries the coordinate itself.
COORD_PROPERTIES = {
    "person": [("P19", "birthplace"), ("P20", "death place"), ("P27", "country")],
    "polity": [("P36", "capital"), (None, "own coordinates"), ("P17", "country")],
    "event": [(None, "own coordinates"), ("P276", "location"), ("P17", "country")],
}

COORD_QUERY = """
SELECT ?item ?coord WHERE {
  VALUES ?item { %ITEMS% }
  ?item %PATH% ?coord .
}
"""

# Wikidata returns coordinates as WKT, longitude first. Points on other globes
# are prefixed with the globe's URI, and those we skip — a crater on Mars is
# not a place in any region.
POINT = re.compile(r"^Point\(\s*(-?[0-9.eE+-]+)\s+(-?[0-9.eE+-]+)\s*\)$")

# Bumped when the coordinate queries, the property order, or the cache naming
# change, so results from an older scheme are not reused.
COORD_VERSION = 4

# Coordinates are fetched most-notable-first, so that a run cut short still
# places the entries most likely to be shown. Batches are cut within a tier,
# not across the whole sorted list: adding one record shifts every batch after
# it, and a shifted batch is a cache miss, so confining the churn to one tier
# keeps the other three usable.
NOTABILITY_TIERS = (50, 20, 10, 0)

FIELD_MAP = {
    "person": {"birth": ("birth", "birthPrec"), "death": ("death", "deathPrec"),
               "floruit": ("floruit", "floruitPrec")},
    "polity": {"inception": ("inception", "inceptionPrec"),
               "dissolved": ("dissolved", "dissolvedPrec")},
    "event": {"point_in_time": ("point", "pointPrec"), "start_time": ("start", "startPrec"),
              "end_time": ("end", "endPrec")},
}


def date_windows(end, width, start=1):
    """(label, from, to) windows. The first has no lower bound, which covers
    everything BCE with a plain upper-bound comparison."""
    windows = [("bce", None, start)]
    for lo in range(start, end, width):
        hi = min(lo + width, end)
        windows.append((f"{lo}-{hi}", lo, hi))
    return windows


def date_filter(low, high):
    bounds = []
    if low is not None:
        bounds.append(f'?driver >= "{low:04d}-01-01T00:00:00Z"^^xsd:dateTime')
    bounds.append(f'?driver < "{high:04d}-01-01T00:00:00Z"^^xsd:dateTime')
    return "FILTER(" + " && ".join(bounds) + ")"


def split_window(low, high):
    """Halve a window. Returns None when it can't usefully be split further."""
    if low is None or high - low <= MIN_WINDOW:
        return None
    middle = low + (high - low) // 2
    return (low, middle), (middle, high)


def main_query(type_, driver, low, high, min_sitelinks=None):
    """The extraction query. `min_sitelinks` overrides the floor, which
    probe_floor.py uses to look at what the floor is keeping out."""
    floor = MIN_SITELINKS[type_] if min_sitelinks is None else min_sitelinks
    return (TEMPLATES[type_]
            .replace("%DRIVER%", driver)
            .replace("%DATEFILTER%", date_filter(low, high))
            .replace("%MIN%", str(floor))
            .replace("%LABEL%", LABEL_SERVICE))


def planned_chunks(type_):
    """(label, driver, low, high) for every chunk, before any subdivision."""
    out = []
    for driver, every_window in DRIVERS[type_]:
        for label, low, high in date_windows(LAST_YEAR[type_], DATE_WINDOWS[type_]):
            if not every_window and label != "bce":
                continue
            out.append((f"{type_}-{driver}-{label}", driver, low, high))
    return out


def batched(items, size=None):
    size = size or COORD_BATCH
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Throttled(Exception):
    """WDQS refused because we asked too often, not because the query is too
    big. Splitting the window in response would be the wrong fix, and would
    quietly shred the cache into fragments that were never too large."""


def retry_after(error, fallback=THROTTLE_PAUSE):
    """Seconds to wait, from a 429's Retry-After header when it carries one.

    The header may also be an HTTP date; we don't parse those, we just wait
    the default, which is the right order of magnitude either way.
    """
    value = (getattr(error, "headers", None) or {}).get("Retry-After")
    try:
        return min(max(int(value), 1), MAX_THROTTLE_PAUSE)
    except (TypeError, ValueError):
        return fallback


def run_query(sparql, contact, retries=4):
    """POST, because a long query in a GET URL can be refused before it runs.

    Asks for gzip: the densest windows return 40MB+ of JSON, and those are
    exactly the responses that arrived truncated, surfacing as a JSON parse
    error thousands of lines in. Compressed, the same result is a few MB and
    a truncated one fails cleanly at decompression instead of half-parsing.
    """
    body = urllib.parse.urlencode({"query": sparql, "format": "json"}).encode()
    request = urllib.request.Request(ENDPOINT, data=body, headers={
        "User-Agent": USER_AGENT.format(contact=contact),
        "Accept": "application/sparql-results+json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    delay = 5
    attempts = throttles = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return json.loads(payload.decode())
        except Exception as exc:                       # noqa: BLE001 - report and back off
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                # Throttling is about our rate, not this query, so it neither
                # spends the retry budget nor justifies a five-second retry.
                throttles += 1
                if throttles > MAX_THROTTLE_WAITS:
                    raise Throttled(
                        f"throttled {MAX_THROTTLE_WAITS} times running") from exc
                pause = retry_after(exc)
                print(f"    throttled ({throttles}/{MAX_THROTTLE_WAITS}), "
                      f"waiting {pause}s")
                time.sleep(pause)
                continue
            attempts += 1
            if attempts >= retries:
                raise
            print(f"    retry {attempts} after {delay}s ({exc})")
            time.sleep(delay)
            delay *= 2


def cell(binding, key):
    value = binding.get(key)
    return value.get("value") if value else None


def flatten(binding, type_):
    """One SPARQL result row to the shape transform.py expects."""
    qid_uri = cell(binding, "item")
    record = {
        "qid": qid_uri.rsplit("/", 1)[-1] if qid_uri else None,
        "name": cell(binding, "name"),
        "desc": cell(binding, "desc"),
        "sitelinks": int(cell(binding, "sitelinks") or 0),
        "lat": None,
        "lng": None,
        "location_source": None,
    }

    article = cell(binding, "article")
    if article:
        title = urllib.parse.unquote(article.rsplit("/", 1)[-1]).replace("_", " ")
        record["wiki_title"] = title if title != record["name"] else None

    for field, (time_key, precision_key) in FIELD_MAP[type_].items():
        raw_time = cell(binding, time_key)
        if raw_time:
            record[field] = {"time": raw_time, "precision": int(cell(binding, precision_key) or 9)}

    return record


def parse_point(literal):
    """WKT to (lat, lng). None for anything that isn't a plain Earth point."""
    found = POINT.match(literal.strip())
    if not found:
        return None
    lng, lat = float(found.group(1)), float(found.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def coord_query(via, batch):
    path = "wdt:P625" if via is None else f"wdt:{via}/wdt:P625"
    return (COORD_QUERY
            .replace("%ITEMS%", " ".join(f"wd:{q}" for q in batch))
            .replace("%PATH%", path))


def by_notability(records):
    """QIDs most-notable-first, in tiers. See NOTABILITY_TIERS."""
    best = {}
    for record in records:
        qid = record.get("qid")
        if qid:
            best[qid] = max(best.get(qid, 0), int(record.get("sitelinks") or 0))

    ordered, placed = [], set()
    for floor in NOTABILITY_TIERS:
        tier = sorted(q for q, n in best.items() if n >= floor and q not in placed)
        ordered.extend(tier)
        placed.update(tier)
    return ordered


def merge_coordinates(records, coordinates):
    """Attach {qid: (lat, lng, source)} to records. Records without one keep
    lat/lng None and are dropped later by transform, which needs a region."""
    for record in records:
        found = coordinates.get(record["qid"])
        if found:
            record["lat"], record["lng"], record["location_source"] = found
    return records


def coord_cache_path(cache_dir, type_, via, batch):
    """Named by what is in the batch, not where it sits in the list.

    Batches are slices of an ordered list, so one recovered window shifts every
    boundary after it — and a cache entry named by index would then hold items
    it is no longer being asked for. A digest of the contents can only ever
    answer for the batch it was written from.
    """
    digest = hashlib.sha1(",".join(batch).encode()).hexdigest()[:16]
    return cache_dir / f"v{COORD_VERSION}-{type_}-{via or 'self'}-{len(batch)}-{digest}.json"


def fetch_by_property(qids, via, type_, contact, cache_dir):
    """One property over many batches.

    Returns (found, qids we couldn't ask about, interrupted). A batch that
    fails is halved and both halves retried, for the same reason a date window
    is: WDQS won't say what a query will cost, and its budget moves with load,
    so a narrower question is the only useful reply to a refusal.

    Ctrl-C stops the pass and keeps what it has. Items are asked about
    most-notable-first, so stopping early is a reasonable thing to want: it
    leaves a smaller database, not a broken one.
    """
    found, failed = {}, []
    pending = deque(batched(qids))
    # Progress is reported per N items rather than per batch: a pass over
    # 660,000 people is 1,300 batches, and a resumed run answers most of them
    # from cache in a second, which should not be 1,300 lines.
    seen, next_report = 0, COORD_REPORT_EVERY

    while pending:
        batch = pending.popleft()
        cached = coord_cache_path(cache_dir, type_, via, batch)
        if cached.exists():
            found.update({qid: tuple(point) for qid, point
                          in json.loads(cached.read_text(encoding="utf-8")).items()})
            seen += len(batch)
            if seen >= next_report:
                print(f"    {seen}/{len(qids)} done, {len(found)} placed (cached)")
                next_report = seen + COORD_REPORT_EVERY
            continue

        try:
            payload = run_query(coord_query(via, batch), contact)
        except KeyboardInterrupt:
            print(f"    stopped after {seen} items — keeping what is placed")
            return found, failed, True
        except Exception as exc:                       # noqa: BLE001
            if len(batch) > MIN_COORD_BATCH:
                middle = len(batch) // 2
                pending.appendleft(batch[middle:])
                pending.appendleft(batch[:middle])
                print(f"    batch of {len(batch)} failed, halving ({exc})")
            else:
                print(f"    batch of {len(batch)} FAILED ({exc})")
                failed.extend(batch)
                seen += len(batch)
            continue

        located = {}
        for binding in payload["results"]["bindings"]:
            point = parse_point(cell(binding, "coord") or "")
            if point:
                located[cell(binding, "item").rsplit("/", 1)[-1]] = point

        cached.write_text(json.dumps(located), encoding="utf-8")
        found.update(located)
        seen += len(batch)
        if seen >= next_report:
            print(f"    {seen}/{len(qids)} done, {len(found)} placed")
            next_report = seen + COORD_REPORT_EVERY
        time.sleep(COORD_PAUSE)

    return found, failed, False


def fetch_coordinates(qids, type_, contact, cache_dir):
    """Place items by the most specific property that answers.

    Returns (coordinates, qids that couldn't be asked about). A batch that
    fails costs that batch and nothing else. It used to cost the whole run: the
    exception propagated out of extract() before any records were written, so a
    two-hour extraction ended with the raw file still holding the *previous*
    run's output — which looks exactly like a run that did nothing, and is much
    harder to notice.
    """
    coordinates, failed = {}, set()
    remaining = list(qids)

    for via, source in COORD_PROPERTIES[type_]:
        if not remaining:
            break
        print(f"  by {source}: {len(remaining)} to place")
        found, could_not_ask, interrupted = fetch_by_property(
            remaining, via, type_, contact, cache_dir)
        for qid, (lat, lng) in found.items():
            coordinates[qid] = (lat, lng, source)
        failed.update(could_not_ask)
        # Whatever this property couldn't place falls through to the next one,
        # including the batches that failed outright.
        remaining = [q for q in remaining if q not in coordinates]
        if interrupted:
            break

    failed &= set(remaining)
    return coordinates, failed


def _fetch_halves(halves, type_, driver, contact, cache_dir, depth):
    rows, failed = [], []
    for half_low, half_high in halves:
        half_rows, half_failed = fetch_chunk(
            f"{type_}-{driver}-{half_low}-{half_high}", type_, driver,
            half_low, half_high, contact, cache_dir, depth + 1)
        rows.extend(half_rows)
        failed.extend(half_failed)
    return rows, failed


def fetch_chunk(label, type_, driver, low, high, contact, cache_dir, depth=0):
    """Run one window, halving it and retrying if WDQS won't deliver it whole.

    The windows that failed were the densest ones, so the useful response to a
    failure is a narrower window rather than another identical attempt. Only
    the halves get cached, though, so every later run rediscovered that the
    whole was too big — three retries and thirty-five seconds each, which is
    expensive when the endpoint is already throttling us. A window that split
    leaves a marker saying so.
    """
    cached = cache_dir / f"{label}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8")), []

    indent = "    " * depth
    halves = split_window(low, high)
    marker = cache_dir / f"{label}.split"
    if halves and marker.exists():
        print(f"  {indent}{label}: known too big, straight to halves")
        return _fetch_halves(halves, type_, driver, contact, cache_dir, depth)

    print(f"  {indent}{label}: querying…")
    try:
        payload = run_query(main_query(type_, driver, low, high), contact)
    except Throttled as exc:
        # Nothing to learn about the window's size from being throttled, so
        # no marker and no split — just leave it for the next run.
        print(f"  {indent}{label}: FAILED, {exc}")
        return [], [label]
    except Exception as exc:                           # noqa: BLE001
        if not halves:
            print(f"  {indent}{label}: FAILED, cannot split further ({exc})")
            return [], [label]
        print(f"  {indent}{label}: splitting ({exc})")
        marker.write_text("too big to fetch whole\n", encoding="utf-8")
        return _fetch_halves(halves, type_, driver, contact, cache_dir, depth)

    rows = [flatten(b, type_) for b in payload["results"]["bindings"]]
    cached.write_text(json.dumps(rows), encoding="utf-8")
    print(f"  {indent}{label}: {len(rows)} rows")
    time.sleep(2)              # be a good citizen on a shared public endpoint
    return rows, []


def extract(type_, cache_dir, contact):
    cache_dir.mkdir(parents=True, exist_ok=True)
    records, failed = [], []
    chunks = planned_chunks(type_)

    for index, (label, driver, low, high) in enumerate(chunks, start=1):
        print(f"[{index}/{len(chunks)}]", end=" ")
        rows, chunk_failed = fetch_chunk(label, type_, driver, low, high, contact, cache_dir)
        records.extend(rows)
        failed.extend(chunk_failed)

    if failed:
        print(f"\n  {len(failed)} window(s) failed even at one year:")
        for label in failed:
            print(f"    {label}")
        print("  Re-run to retry only these; everything else is cached.\n")

    ordered = by_notability(records)
    print(f"  looking up coordinates for {len(ordered)} items…")
    coordinates, coord_failed = fetch_coordinates(ordered, type_, contact, cache_dir)
    merge_coordinates(records, coordinates)
    located = sum(1 for r in records if r["lat"] is not None)
    print(f"  {located}/{len(records)} records have coordinates")
    if coord_failed:
        print(f"  {len(coord_failed)} item(s) could not be asked about at all — "
              f"nothing was cached for them, so a re-run retries exactly those.")
    return records


def main():
    parser = argparse.ArgumentParser(description="Pull raw records from Wikidata.")
    parser.add_argument("--type", choices=sorted(FIELD_MAP))
    parser.add_argument("--out")
    parser.add_argument("--cache", default="raw/cache")
    parser.add_argument("--probe", action="store_true",
                        help="Run a trivial query to check the endpoint answers at all.")
    parser.add_argument("--contact", default=os.environ.get("MEANWHILE_CONTACT"),
                        help="Contact address for the User-Agent header, which Wikidata "
                             "requires. Defaults to the MEANWHILE_CONTACT env var.")
    args = parser.parse_args()

    if not args.contact:
        parser.error("Wikidata rejects anonymous queries. Pass --contact your@email.com, "
                     "or set the MEANWHILE_CONTACT environment variable.")

    if args.probe:
        payload = run_query(PROBE, args.contact, retries=1)
        print("endpoint answered:", payload["results"]["bindings"])
        return

    if not args.type or not args.out:
        parser.error("--type and --out are required unless you pass --probe")

    records = extract(args.type, Path(args.cache), args.contact)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records), encoding="utf-8")
    print(f"{len(records)} raw {args.type} records -> {out}")


if __name__ == "__main__":
    main()
