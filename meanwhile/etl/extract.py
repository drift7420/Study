"""Stage 1: pull raw records from the Wikidata Query Service.

This is the only stage that needs network, and the only one not covered by
tests — it could not be run in the environment it was written in.

Two things keep queries inside WDQS's 60-second budget, both learned from it
returning 502 on the first attempt:

*Lead with the selective pattern.* Starting from `?item wdt:P31 wd:Q5` scans
every human in Wikidata before any filter narrows it, and the optimiser does
not reliably push a sitelink filter ahead of that. Leading with a birth-date
window cuts the candidate set first.

*Fetch coordinates separately.* Following place-of-birth to its coordinates is
a two-hop join; done inside the main query it multiplies the row count. It is
now a second pass over the QIDs already collected, joined in Python.

Queries go by POST — a long query in a GET URL can be refused by the proxy
in front of WDQS, which also surfaces as a 502.

WDQS blocks anonymous clients, so every run needs a contact address. Pass it
with --contact, or set MEANWHILE_CONTACT once in your shell.

Usage:
    python extract.py --probe --contact you@example.com
    python extract.py --type person --out raw/person.json --contact you@example.com
    python extract.py --type polity --out raw/polity.json --contact you@example.com
    python extract.py --type event  --out raw/event.json  --contact you@example.com
"""

import argparse
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


def chunks_for(type_):
    """One query per (driver property, date window). Each is small enough to
    finish; the whole set is deduplicated by QID in transform."""
    template = TEMPLATES[type_]
    minimum = str(MIN_SITELINKS[type_])
    windows = date_windows(LAST_YEAR[type_], DATE_WINDOWS[type_])

    out = []
    for driver, every_window in DRIVERS[type_]:
        for label, low, high in windows:
            if not every_window and label != "bce":
                continue
            sparql = (template
                      .replace("%DRIVER%", driver)
                      .replace("%DATEFILTER%", date_filter(low, high))
                      .replace("%MIN%", minimum)
                      .replace("%LABEL%", LABEL_SERVICE))
            out.append((f"{type_}-{driver}-{label}", sparql))
    return out


def batched(items, size=COORD_BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run_query(sparql, contact, retries=4):
    """POST, because a long query in a GET URL can be refused before it runs."""
    body = urllib.parse.urlencode({"query": sparql, "format": "json"}).encode()
    request = urllib.request.Request(ENDPOINT, data=body, headers={
        "User-Agent": USER_AGENT.format(contact=contact),
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    delay = 5
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode())
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
    for index, batch in enumerate(batched(sorted(qids))):
        cached = cache_dir / f"{type_}-coords-{index}.json"
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


def extract(type_, cache_dir, contact):
    cache_dir.mkdir(parents=True, exist_ok=True)
    records, failed = [], []
    chunks = chunks_for(type_)

    for index, (label, sparql) in enumerate(chunks, start=1):
        cached = cache_dir / f"{label}.json"
        if cached.exists():
            records.extend(json.loads(cached.read_text()))
            continue

        print(f"  [{index}/{len(chunks)}] {label}: querying…")
        try:
            payload = run_query(sparql, contact)
        except Exception as exc:                       # noqa: BLE001
            # One stubborn window shouldn't cost the whole run; everything that
            # succeeded is already cached, so a re-run only retries the rest.
            print(f"  [{index}/{len(chunks)}] {label}: FAILED ({exc})")
            failed.append(label)
            continue

        rows = [flatten(b, type_) for b in payload["results"]["bindings"]]
        cached.write_text(json.dumps(rows))
        print(f"  [{index}/{len(chunks)}] {label}: {len(rows)} rows")
        records.extend(rows)
        time.sleep(2)          # be a good citizen on a shared public endpoint

    if failed:
        print(f"\n  {len(failed)} chunk(s) failed and were skipped:")
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
