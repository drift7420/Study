"""Stage 1: pull raw records from the Wikidata Query Service.

This is the only stage that needs network, and the only one not covered by
tests — it could not be run in the environment it was written in. Treat the
chunk sizes as a starting point: WDQS enforces a 60-second timeout, so if a
chunk times out, split it further rather than retrying it unchanged.

Results are cached per chunk under --cache, so a re-run only fetches what is
missing and an interrupted run resumes where it stopped.

Usage:
    python extract.py --type person --out raw/person.json
    python extract.py --type polity --out raw/polity.json
    python extract.py --type event  --out raw/event.json
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://query.wikidata.org/sparql"

# WDQS blocks anonymous clients; put a real contact address here before running.
USER_AGENT = "MeanwhileETL/0.1 (https://github.com/drift7420/study; contact: you@example.com)"

# Sitelink buckets keep each query bounded without any date arithmetic, which
# Blazegraph handles poorly for BCE years. Widen the floor to trade size for
# coverage.
BUCKETS = [(200, None), (100, 200), (60, 100), (40, 60),
           (28, 40), (20, 28), (15, 20), (10, 15), (6, 10), (4, 6)]

PERSON_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?birth ?birthPrec ?death ?deathPrec
       ?floruit ?floruitPrec ?lat ?lng ?article
WHERE {
  ?item wdt:P31 wd:Q5 ; wikibase:sitelinks ?sitelinks .
  %BUCKET%
  { ?item wdt:P569 ?anyBirth . FILTER(YEAR(?anyBirth) < 1900) }
  UNION
  { ?item wdt:P570 ?anyDeath . FILTER(YEAR(?anyDeath) < 1900) FILTER NOT EXISTS { ?item wdt:P569 [] } }

  OPTIONAL { ?item p:P569/psv:P569 ?bn .
             ?bn wikibase:timeValue ?birth ; wikibase:timePrecision ?birthPrec . }
  OPTIONAL { ?item p:P570/psv:P570 ?dn .
             ?dn wikibase:timeValue ?death ; wikibase:timePrecision ?deathPrec . }
  OPTIONAL { ?item p:P1317/psv:P1317 ?fn .
             ?fn wikibase:timeValue ?floruit ; wikibase:timePrecision ?floruitPrec . }
  OPTIONAL { ?item wdt:P19 ?place . ?place p:P625/psv:P625 ?cn .
             ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" .
                           ?item rdfs:label ?name ; schema:description ?desc . }
}
"""

POLITY_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?inception ?inceptionPrec ?dissolved ?dissolvedPrec
       ?lat ?lng ?article
WHERE {
  VALUES ?class { wd:Q3624078 wd:Q3024240 wd:Q417175 wd:Q48349 wd:Q164950
                  wd:Q133156 wd:Q1250464 wd:Q28171280 wd:Q11514315 }
  ?item wdt:P31/wdt:P279* ?class ; wikibase:sitelinks ?sitelinks .
  %BUCKET%
  ?item p:P571/psv:P571 ?in .
  ?in wikibase:timeValue ?inception ; wikibase:timePrecision ?inceptionPrec .
  OPTIONAL { ?item p:P576/psv:P576 ?dn .
             ?dn wikibase:timeValue ?dissolved ; wikibase:timePrecision ?dissolvedPrec . }
  OPTIONAL { ?item wdt:P36 ?capital . ?capital p:P625/psv:P625 ?cn .
             ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" .
                           ?item rdfs:label ?name ; schema:description ?desc . }
}
"""

EVENT_QUERY = """
SELECT ?item ?name ?desc ?sitelinks ?point ?pointPrec ?start ?startPrec ?end ?endPrec
       ?lat ?lng ?article
WHERE {
  VALUES ?class { wd:Q1190554 wd:Q178561 wd:Q198 wd:Q131569 wd:Q13418847 wd:Q1656682 }
  ?item wdt:P31/wdt:P279* ?class ; wikibase:sitelinks ?sitelinks .
  %BUCKET%
  { ?item wdt:P585 ?anyPoint . FILTER(YEAR(?anyPoint) < 1950) }
  UNION
  { ?item wdt:P580 ?anyStart . FILTER(YEAR(?anyStart) < 1950) }

  OPTIONAL { ?item p:P585/psv:P585 ?pn .
             ?pn wikibase:timeValue ?point ; wikibase:timePrecision ?pointPrec . }
  OPTIONAL { ?item p:P580/psv:P580 ?sn .
             ?sn wikibase:timeValue ?start ; wikibase:timePrecision ?startPrec . }
  OPTIONAL { ?item p:P582/psv:P582 ?en .
             ?en wikibase:timeValue ?end ; wikibase:timePrecision ?endPrec . }
  OPTIONAL { ?item p:P625/psv:P625 ?cn .
             ?cn wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng . }
  OPTIONAL { ?item wdt:P276 ?loc . ?loc p:P625/psv:P625 ?cn2 .
             ?cn2 wikibase:geoLatitude ?lat ; wikibase:geoLongitude ?lng . }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" .
                           ?item rdfs:label ?name ; schema:description ?desc . }
}
"""

QUERIES = {"person": PERSON_QUERY, "polity": POLITY_QUERY, "event": EVENT_QUERY}

FIELD_MAP = {
    "person": {"birth": ("birth", "birthPrec"), "death": ("death", "deathPrec"),
               "floruit": ("floruit", "floruitPrec")},
    "polity": {"inception": ("inception", "inceptionPrec"),
               "dissolved": ("dissolved", "dissolvedPrec")},
    "event": {"point_in_time": ("point", "pointPrec"), "start_time": ("start", "startPrec"),
              "end_time": ("end", "endPrec")},
}


def bucket_filter(low, high):
    clause = f"FILTER(?sitelinks >= {low}"
    if high is not None:
        clause += f" && ?sitelinks < {high}"
    return clause + ")"


def run_query(sparql, retries=4):
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": sparql, "format": "json"})
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
    delay = 5
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode())
        except Exception as exc:                       # noqa: BLE001 - report and back off
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} after {delay}s ({exc})")
            time.sleep(delay)
            delay *= 2
    return None


def flatten(binding, type_):
    """One SPARQL result row to the shape transform.py expects."""
    def value(key):
        cell = binding.get(key)
        return cell.get("value") if cell else None

    qid_uri = value("item")
    record = {
        "qid": qid_uri.rsplit("/", 1)[-1] if qid_uri else None,
        "name": value("name"),
        "desc": value("desc"),
        "sitelinks": int(value("sitelinks") or 0),
        "lat": float(value("lat")) if value("lat") else None,
        "lng": float(value("lng")) if value("lng") else None,
    }

    article = value("article")
    if article:
        title = urllib.parse.unquote(article.rsplit("/", 1)[-1]).replace("_", " ")
        record["wiki_title"] = title if title != record["name"] else None

    for field, (time_key, precision_key) in FIELD_MAP[type_].items():
        raw_time = value(time_key)
        if raw_time:
            record[field] = {"time": raw_time, "precision": int(value(precision_key) or 9)}

    return record


def extract(type_, cache_dir, buckets=BUCKETS):
    cache_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for low, high in buckets:
        label = f"{type_}-{low}-{high or 'max'}"
        cached = cache_dir / f"{label}.json"
        if cached.exists():
            print(f"  {label}: cached")
            records.extend(json.loads(cached.read_text()))
            continue

        print(f"  {label}: querying…")
        sparql = QUERIES[type_].replace("%BUCKET%", bucket_filter(low, high))
        payload = run_query(sparql)
        rows = [flatten(b, type_) for b in payload["results"]["bindings"]]
        cached.write_text(json.dumps(rows))
        print(f"  {label}: {len(rows)} rows")
        records.extend(rows)
        time.sleep(2)          # be a good citizen on a shared public endpoint

    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", required=True, choices=sorted(QUERIES))
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", default="raw/cache")
    args = parser.parse_args()

    records = extract(args.type, Path(args.cache))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records))
    print(f"{len(records)} raw {args.type} records -> {out}")


if __name__ == "__main__":
    main()
