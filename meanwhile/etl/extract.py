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

*Halve a window that fails.* WDQS offers no way to ask what a query will cost,
and its budget moves with load, so the useful response to a failure is a
narrower window rather than another identical attempt.

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
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "MeanwhileETL/0.1 (https://github.com/drift7420/Study; contact: {contact})"

MIN_SITELINKS = {"person": 4, "polity": 3, "event": 4}
COORD_BATCH = 400

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

# Second pass: coordinates only, keyed by QID. Each type reaches its location
# by a different property.
COORD_QUERIES = {
    "person": """
SELECT ?item ?lat ?lng WHERE {
  VALUES ?item { %ITEMS% }
  ?item wdt:P19 ?place .
  ?place p:P625/psv:P625 ?cn .
  ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng .
}
""",
    "polity": """
SELECT ?item ?lat ?lng WHERE {
  VALUES ?item { %ITEMS% }
  ?item wdt:P36 ?place .
  ?place p:P625/psv:P625 ?cn .
  ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng .
}
""",
    "event": """
SELECT ?item ?lat ?lng WHERE {
  VALUES ?item { %ITEMS% }
  { ?item p:P625/psv:P625 ?cn }
  UNION
  { ?item wdt:P276 ?place . ?place p:P625/psv:P625 ?cn }
  ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng .
}
""",
}

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


def main_query(type_, driver, low, high):
    return (TEMPLATES[type_]
            .replace("%DRIVER%", driver)
            .replace("%DATEFILTER%", date_filter(low, high))
            .replace("%MIN%", str(MIN_SITELINKS[type_]))
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


def batched(items, size=COORD_BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                return json.loads(payload.decode())
        except Exception as exc:                       # noqa: BLE001 - report and back off
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} after {delay}s ({exc})")
            time.sleep(delay)
            delay *= 2
    return None


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


def merge_coordinates(records, coordinates):
    """Attach {qid: (lat, lng)} to records. Records without one keep lat/lng None
    and are dropped later by transform, which needs a region."""
    for record in records:
        found = coordinates.get(record["qid"])
        if found:
            record["lat"], record["lng"] = found
    return records


def fetch_coordinates(qids, type_, contact, cache_dir):
    coordinates = {}
    # The batch number alone is not a safe cache key: batches are slices of a
    # sorted list, so recovering one failed window shifts every boundary and
    # cached batches would no longer hold the items they are named for. Keying
    # on the set size too means a changed set refetches rather than silently
    # skipping the items that moved.
    population = len(qids)
    for index, batch in enumerate(batched(sorted(qids))):
        cached = cache_dir / f"{type_}-coords-{population}-{index}.json"
        if cached.exists():
            coordinates.update(json.loads(cached.read_text()))
            continue

        values = " ".join(f"wd:{q}" for q in batch)
        payload = run_query(COORD_QUERIES[type_].replace("%ITEMS%", values), contact)
        found = {}
        for binding in payload["results"]["bindings"]:
            qid = cell(binding, "item").rsplit("/", 1)[-1]
            found[qid] = (float(cell(binding, "lat")), float(cell(binding, "lng")))
        cached.write_text(json.dumps(found))
        print(f"  coords {index + 1}: {len(found)}/{len(batch)} located")
        coordinates.update(found)
        time.sleep(1)
    return coordinates


def fetch_chunk(label, type_, driver, low, high, contact, cache_dir, depth=0):
    """Run one window, halving it and retrying if WDQS won't deliver it whole.

    The windows that failed were the densest ones, so the useful response to a
    failure is a narrower window rather than another identical attempt.
    """
    cached = cache_dir / f"{label}.json"
    if cached.exists():
        return json.loads(cached.read_text()), []

    indent = "    " * depth
    print(f"  {indent}{label}: querying…")
    try:
        payload = run_query(main_query(type_, driver, low, high), contact)
    except Exception as exc:                           # noqa: BLE001
        halves = split_window(low, high)
        if not halves:
            print(f"  {indent}{label}: FAILED, cannot split further ({exc})")
            return [], [label]
        print(f"  {indent}{label}: splitting ({exc})")
        rows, failed = [], []
        for half_low, half_high in halves:
            half_rows, half_failed = fetch_chunk(
                f"{type_}-{driver}-{half_low}-{half_high}", type_, driver,
                half_low, half_high, contact, cache_dir, depth + 1)
            rows.extend(half_rows)
            failed.extend(half_failed)
        return rows, failed

    rows = [flatten(b, type_) for b in payload["results"]["bindings"]]
    cached.write_text(json.dumps(rows))
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

    print(f"  looking up coordinates for {len(records)} records…")
    coordinates = fetch_coordinates({r["qid"] for r in records if r["qid"]},
                                    type_, contact, cache_dir)
    merge_coordinates(records, coordinates)
    located = sum(1 for r in records if r["lat"] is not None)
    print(f"  {located}/{len(records)} have coordinates")
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
    out.write_text(json.dumps(records))
    print(f"{len(records)} raw {args.type} records -> {out}")


if __name__ == "__main__":
    main()
