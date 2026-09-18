"""Raw Wikidata records to the rows the app's database stores.

The app queries `active_start`/`active_end`, never the display dates, so every
row must have a span even when the source is partial. A person with only a
birth year still gets one — inferred, and flagged as such, because dropping
them would quietly delete exactly the thin-coverage regions we care about.

Notability thresholds are per type and can be relaxed per region. Wikidata's
sitelink counts reflect who writes Wikipedia, so a single global cutoff empties
sub-Saharan Africa and pre-Columbian America while leaving European minor
nobility intact.
"""

from dataclasses import dataclass, asdict

from dates import parse_time
import regions as regions_mod

PRESENT = 2026

ASSUMED_LIFESPAN = 80      # birth known, death not
ASSUMED_PRE_DEATH = 60     # death known, birth not
FLORUIT_SPAN = 30          # only "active around" known
MAX_LIFESPAN = 120         # clamp nonsense

PERSON, POLITY, EVENT = "person", "polity", "event"

DEFAULT_THRESHOLDS = {PERSON: 10, POLITY: 4, EVENT: 5}

# Regions a single global cutoff would strip almost bare.
RELAXED_REGIONS = {9: 0.4, 10: 0.4, 11: 0.4, 12: 0.4, 18: 0.4,
                   20: 0.5, 21: 0.5, 22: 0.5, 17: 0.6}


@dataclass
class Row:
    qid: str
    name: str
    type: str
    description: str
    birth_year: int | None
    birth_precision: str | None
    death_year: int | None
    death_precision: str | None
    active_start: int
    active_end: int
    date_confidence: str
    region_id: int
    region_confidence: str
    notability: int
    wiki_title: str | None
    lat: float | None
    lng: float | None


def _span_for_person(rec):
    """Return (birth, death, active_start, active_end, confidence) or None."""
    birth = parse_time(rec.get("birth"))
    death = parse_time(rec.get("death"))

    if birth and death:
        start, end = birth.start, death.end
        confidence = "exact" if birth.precision == "year" and death.precision == "year" else "approximate"
    elif birth:
        start, end = birth.start, birth.end + ASSUMED_LIFESPAN
        confidence = "inferred"
    elif death:
        start, end = death.start - ASSUMED_PRE_DEATH, death.end
        confidence = "inferred"
    else:
        floruit = parse_time(rec.get("floruit"))
        if not floruit:
            return None
        start, end = floruit.start, floruit.end + FLORUIT_SPAN
        confidence = "inferred"

    if end < start:
        return None
    if end - start > MAX_LIFESPAN:
        end = start + MAX_LIFESPAN

    return birth, death, start, end, confidence


def _span_for_range(rec, start_keys, end_keys, point_keys=()):
    """Shared shape for polities and events: a start, an end, or a single point."""
    for key in point_keys:
        point = parse_time(rec.get(key))
        if point:
            return point.start, point.end, ("exact" if point.precision == "year" else "approximate")

    start = next((p for p in (parse_time(rec.get(k)) for k in start_keys) if p), None)
    end = next((p for p in (parse_time(rec.get(k)) for k in end_keys) if p), None)

    if start and end:
        confidence = "exact" if start.precision == "year" and end.precision == "year" else "approximate"
        return start.start, end.end, confidence
    if start:
        return start.start, PRESENT, "inferred"     # no recorded end: treat as ongoing
    if end:
        return end.start, end.end, "inferred"
    return None


def threshold_for(type_, region_id, thresholds=None):
    base = (thresholds or DEFAULT_THRESHOLDS)[type_]
    return base * RELAXED_REGIONS.get(region_id, 1.0)


def build_row(rec, type_, thresholds=None) -> Row | None:
    """Normalise one record. Returns None if it can't be placed in time or space."""
    if type_ == PERSON:
        span = _span_for_person(rec)
        if not span:
            return None
        birth, death, start, end, date_confidence = span
    else:
        birth = death = None
        if type_ == POLITY:
            span = _span_for_range(rec, ("inception", "start_time"), ("dissolved", "end_time"))
        else:
            span = _span_for_range(rec, ("start_time",), ("end_time",), ("point_in_time",))
        if not span:
            return None
        start, end, date_confidence = span

    region_id, region_confidence = regions_mod.assign(rec.get("lat"), rec.get("lng"))
    if region_id is None:
        return None

    notability = int(rec.get("sitelinks") or 0)
    if notability < threshold_for(type_, region_id, thresholds):
        return None

    return Row(
        qid=rec["qid"],
        name=rec["name"],
        type=type_,
        description=rec.get("desc") or "",
        birth_year=birth.year if birth else None,
        birth_precision=birth.precision if birth else None,
        death_year=death.year if death else None,
        death_precision=death.precision if death else None,
        active_start=start,
        active_end=end,
        date_confidence=date_confidence,
        region_id=region_id,
        region_confidence=region_confidence,
        notability=notability,
        wiki_title=rec.get("wiki_title") or None,
        lat=rec.get("lat"),
        lng=rec.get("lng"),
    )


def transform(records, type_, thresholds=None):
    """Normalise many records, dropping duplicates by QID (highest sitelinks wins)."""
    best = {}
    for rec in records:
        row = build_row(rec, type_, thresholds)
        if not row:
            continue
        seen = best.get(row.qid)
        if seen is None or row.notability > seen.notability:
            best[row.qid] = row
    return list(best.values())


# When one item matches more than one class tree, the more specific reading
# wins. A person is unambiguous; between a state and an occurrence, the state
# is the stronger claim about what the thing *is*.
TYPE_PRIORITY = {PERSON: 0, POLITY: 1, EVENT: 2}


def deduplicate(rows):
    """One row per QID across all types.

    Wikidata's class trees overlap — a dynasty can be reachable from both the
    state and the occurrence hierarchies — so the same item arrives twice from
    two extractions. The app shows one entry per thing, and the database
    enforces it, so the duplicate has to be resolved here.
    """
    best = {}
    for row in rows:
        seen = best.get(row.qid)
        if seen is None or ((TYPE_PRIORITY[row.type], -row.notability)
                            < (TYPE_PRIORITY[seen.type], -seen.notability)):
            best[row.qid] = row
    return list(best.values())


def coverage(rows):
    """Rows per region per 500-year bucket — the diagnostic that says whether
    the dataset actually supports the app's premise."""
    table = {}
    for row in rows:
        for bucket in range(row.active_start // 500, row.active_end // 500 + 1):
            key = (row.region_id, bucket * 500)
            table[key] = table.get(key, 0) + 1
    return table


def as_dict(row):
    return asdict(row)
