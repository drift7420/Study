"""Tests for the parts of extract.py that don't touch the network.

The queries themselves can't be tested without WDQS, but the windowing, the
subdivision, the batching and the shape of what comes back can be — and those
are where a bug costs a half-hour run.
"""

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import extract
import transform as tf


# ---------- windows ----------

def test_windows_cover_the_range_without_gaps_or_overlap():
    windows = [(lo, hi) for _, lo, hi in extract.date_windows(1900, 50) if lo is not None]
    assert windows[0][0] == 1
    assert windows[-1][1] == 1900
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert end == start


def test_the_first_window_is_bce_and_has_no_lower_bound():
    label, low, high = extract.date_windows(1900, 50)[0]
    assert (label, low, high) == ("bce", None, 1)
    assert extract.date_filter(low, high) == \
        'FILTER(?driver < "0001-01-01T00:00:00Z"^^xsd:dateTime)'


def test_a_bounded_window_filters_on_both_sides():
    assert extract.date_filter(1500, 1550) == (
        'FILTER(?driver >= "1500-01-01T00:00:00Z"^^xsd:dateTime && '
        '?driver < "1550-01-01T00:00:00Z"^^xsd:dateTime)')


def test_chunk_labels_are_stable():
    """Cache files are named after these labels, so changing them silently
    discards a completed run. A rename should have to fail a test first."""
    labels = [label for label, _, _, _ in extract.planned_chunks("person")]
    for expected in ("person-P569-bce", "person-P569-1-51", "person-P569-1851-1900",
                     "person-P570-bce", "person-P1317-bce"):
        assert expected in labels


# ---------- subdivision ----------

def test_a_window_splits_into_two_halves_that_tile_it():
    first, second = extract.split_window(1750, 1800)
    assert first == (1750, 1775)
    assert second == (1775, 1800)


def test_an_odd_window_still_tiles_without_gaps():
    first, second = extract.split_window(1000, 1025)
    assert first[1] == second[0]
    assert (first[0], second[1]) == (1000, 1025)


def test_subdivision_bottoms_out():
    assert extract.split_window(1500, 1501) is None
    assert extract.split_window(None, 1) is None       # the BCE window


def test_repeated_subdivision_terminates():
    pending, seen = [(1, 1900)], 0
    while pending:
        low, high = pending.pop()
        seen += 1
        assert seen < 10000, "subdivision did not terminate"
        halves = extract.split_window(low, high)
        if halves:
            pending.extend(halves)


# ---------- queries ----------

def test_chunks_are_distinctly_labelled():
    for type_ in ("person", "polity", "event"):
        labels = [label for label, _, _, _ in extract.planned_chunks(type_)]
        assert len(labels) == len(set(labels)), f"{type_} has duplicate chunk labels"


def test_queries_substitute_every_placeholder():
    for type_ in ("person", "polity", "event"):
        for label, driver, low, high in extract.planned_chunks(type_):
            assert "%" not in extract.main_query(type_, driver, low, high), \
                f"{label} left a placeholder unsubstituted"


def test_no_query_calls_YEAR():
    """A function call can't use the date index, so YEAR(?d) < 1 scans every
    date in Wikidata and times the query out."""
    for type_ in ("person", "polity", "event"):
        for _, driver, low, high in extract.planned_chunks(type_):
            assert "YEAR(" not in extract.main_query(type_, driver, low, high)


def test_every_query_leads_with_its_date_filter():
    """Putting the type or class pattern first scans every human, or walks the
    whole subclass tree, before anything narrows it."""
    for type_ in ("person", "polity", "event"):
        for label, driver, low, high in extract.planned_chunks(type_):
            sparql = extract.main_query(type_, driver, low, high)
            filter_at = sparql.index("FILTER(?driver")
            for late in ("wd:Q5", "wdt:P279*", "wikibase:sitelinks"):
                if late in sparql:
                    assert filter_at < sparql.index(late), \
                        f"{label}: {late} precedes the date filter"


def test_death_and_floruit_drive_chunks_so_undated_births_survive():
    labels = [label for label, _, _, _ in extract.planned_chunks("person")]
    assert any("P570" in label for label in labels)
    assert any("P1317" in label for label in labels)


# ---------- batching and shaping ----------

def test_batched_covers_everything_exactly_once():
    items = list(range(1000))
    batches = list(extract.batched(items, size=400))
    assert [len(b) for b in batches] == [400, 400, 200]
    assert [x for b in batches for x in b] == items


def binding(**kw):
    return {k: {"value": v} for k, v in kw.items()}


def test_flatten_produces_what_transform_consumes():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q935",
        name="Isaac Newton", desc="English mathematician", sitelinks="180",
        birth="1643-01-04T00:00:00Z", birthPrec="11",
        death="1727-03-31T00:00:00Z", deathPrec="11",
        article="https://en.wikipedia.org/wiki/Isaac_Newton"), "person")

    assert row["qid"] == "Q935"
    assert row["sitelinks"] == 180
    assert row["birth"] == {"time": "1643-01-04T00:00:00Z", "precision": 11}
    assert row["wiki_title"] is None          # title matches the label

    extract.merge_coordinates([row], {"Q935": (52.8, -0.6, "birthplace")})
    built = tf.build_row(row, tf.PERSON)
    assert built is not None
    assert (built.active_start, built.active_end) == (1643, 1727)
    assert built.region_id is not None


def test_flatten_keeps_a_wiki_title_that_differs_from_the_label():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q8011",
        name="Ibn Sina", sitelinks="90",
        article="https://en.wikipedia.org/wiki/Avicenna"), "person")
    assert row["wiki_title"] == "Avicenna"


def test_flatten_survives_missing_optionals():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q1", name="Someone", sitelinks="5"), "person")
    assert row["desc"] is None
    assert row["lat"] is None and row["lng"] is None
    assert "birth" not in row


def test_an_item_the_coordinate_pass_missed_is_left_alone():
    records = [{"qid": "Q1", "lat": None, "lng": None, "location_source": None},
               {"qid": "Q2", "lat": None, "lng": None, "location_source": None}]
    extract.merge_coordinates(records, {"Q1": (41.9, 12.5, "birthplace")})
    assert records[0]["lat"] == 41.9
    assert records[1]["lat"] is None
    # No region, so transform drops it rather than inventing one.
    assert tf.build_row({**records[1], "name": "x", "sitelinks": 50}, tf.PERSON) is None


# ---------- coordinate fallback ----------

def test_the_most_specific_coordinate_wins():
    """Birthplace beats place of death beats country: all three are real
    locations, but a country centroid can land someone in the wrong region."""
    rows = [(3, 35.0, 105.0), (1, 30.6, 114.3), (2, 39.9, 116.4)]
    assert extract.best_coordinate(rows) == (1, 30.6, 114.3)


def test_a_country_centroid_is_kept_when_it_is_all_there_is():
    assert extract.best_coordinate([(3, 35.0, 105.0)]) == (3, 35.0, 105.0)


def test_every_type_ranks_its_sources():
    for type_ in ("person", "polity", "event"):
        query = extract.COORD_QUERIES[type_]
        labels = extract.COORD_SOURCE[type_]
        for rank in labels:
            assert f"BIND({rank} AS ?rank)" in query, f"{type_} rank {rank} unranked"
        assert set(labels) == {1, 2, 3}


def test_merge_records_the_source_alongside_the_coordinate():
    records = [{"qid": "Q1", "lat": None, "lng": None, "location_source": None}]
    extract.merge_coordinates(records, {"Q1": (30.6, 114.3, "country")})
    assert records[0]["location_source"] == "country"
    built = tf.build_row({**records[0], "name": "X", "sitelinks": 50,
                          "birth": {"time": "+1500-01-01T00:00:00Z", "precision": 9}},
                         tf.PERSON)
    assert built.location_source == "country"


def test_an_unplaced_record_reports_unknown_rather_than_crashing():
    row = tf.build_row({"qid": "Q2", "name": "X", "sitelinks": 50, "lat": 41.9, "lng": 12.5,
                        "birth": {"time": "+1500-01-01T00:00:00Z", "precision": 9}},
                       tf.PERSON)
    assert row.location_source == "unknown"


# ---------- throttling ----------

def http_error(code, headers=None):
    return urllib.error.HTTPError("https://query.wikidata.org/sparql", code,
                                  "refused", headers or {}, None)


def test_retry_after_is_honoured():
    assert extract.retry_after(http_error(429, {"Retry-After": "90"})) == 90


def test_a_missing_retry_after_waits_a_minute_not_five_seconds():
    """The old backoff was 5/10/20s, which is nothing to a throttle that lasts
    minutes — we just burned the retry budget and failed."""
    assert extract.retry_after(http_error(429)) == extract.THROTTLE_PAUSE
    assert extract.THROTTLE_PAUSE >= 60


def test_an_http_date_retry_after_falls_back_rather_than_crashing():
    header = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    assert extract.retry_after(http_error(429, header)) == extract.THROTTLE_PAUSE


def test_an_absurd_retry_after_is_capped():
    assert extract.retry_after(http_error(429, {"Retry-After": "100000"})) \
        == extract.MAX_THROTTLE_PAUSE


def refuse_with(monkeypatch, error, slept):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        raise error

    monkeypatch.setattr(extract.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(extract.time, "sleep", slept.append)
    return calls


def test_a_429_waits_it_out_without_spending_the_retry_budget(monkeypatch):
    slept = []
    calls = refuse_with(monkeypatch, http_error(429, {"Retry-After": "1"}), slept)
    with pytest.raises(extract.Throttled):
        extract.run_query("SELECT 1", "me@example.com", retries=2)
    assert len(calls) == extract.MAX_THROTTLE_WAITS + 1     # not `retries`
    assert slept == [1] * extract.MAX_THROTTLE_WAITS


def test_an_ordinary_failure_still_gives_up_after_its_retries(monkeypatch):
    slept = []
    calls = refuse_with(monkeypatch, http_error(504), slept)
    with pytest.raises(urllib.error.HTTPError):
        extract.run_query("SELECT 1", "me@example.com", retries=3)
    assert len(calls) == 3
    assert slept == [5, 10]


# ---------- remembering a window that had to split ----------

def only_halves_answer(asked, too_big=('>= "1800-01-01', '< "1900-01-01')):
    def fake(sparql, contact, retries=4):
        asked.append(sparql)
        if all(fragment in sparql for fragment in too_big):
            raise RuntimeError("504 Gateway Timeout")
        return {"results": {"bindings": []}}
    return fake


def test_a_window_that_split_is_not_attempted_whole_again(tmp_path, monkeypatch):
    """Re-running used to spend three retries — about thirty-five seconds —
    per known-too-big window before falling back to its cached halves."""
    asked = []
    monkeypatch.setattr(extract, "run_query", only_halves_answer(asked))
    monkeypatch.setattr(extract.time, "sleep", lambda _: None)

    args = ("person-P569-1800-1900", "person", "P569", 1800, 1900, "c", tmp_path)
    extract.fetch_chunk(*args)
    assert len(asked) == 3                      # the whole, then both halves
    assert (tmp_path / "person-P569-1800-1900.split").exists()

    asked.clear()
    rows, failed = extract.fetch_chunk(*args)
    assert asked == []                          # halves cached, whole not retried
    assert (rows, failed) == ([], [])


def test_the_marker_still_sends_us_to_the_halves_when_they_are_not_cached(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(extract, "run_query", only_halves_answer(asked))
    monkeypatch.setattr(extract.time, "sleep", lambda _: None)

    args = ("person-P569-1800-1900", "person", "P569", 1800, 1900, "c", tmp_path)
    extract.fetch_chunk(*args)
    for half in tmp_path.glob("*-18*.json"):
        half.unlink()

    asked.clear()
    extract.fetch_chunk(*args)
    assert len(asked) == 2                      # both halves, never the whole


def test_throttling_does_not_get_mistaken_for_a_window_being_too_big(tmp_path, monkeypatch):
    """Splitting on a 429 would shred the cache into fragments that were never
    too large, and quadruple the request count while we are being throttled."""
    def throttled(sparql, contact, retries=4):
        raise extract.Throttled("throttled 4 times running")
    monkeypatch.setattr(extract, "run_query", throttled)

    rows, failed = extract.fetch_chunk("person-P569-1800-1900", "person", "P569",
                                       1800, 1900, "c", tmp_path)
    assert (rows, failed) == ([], ["person-P569-1800-1900"])
    assert list(tmp_path.glob("*.split")) == []
    assert list(tmp_path.glob("*.json")) == []  # nothing cached, so a re-run retries


# ---------- a failed coordinate batch ----------

def coord_binding(qid, lat, lng, rank):
    return binding(item=f"http://www.wikidata.org/entity/{qid}",
                   lat=str(lat), lng=str(lng), rank=str(rank))


def test_a_failed_coordinate_batch_costs_one_batch_not_the_whole_run(tmp_path, monkeypatch):
    """This is the bug that made a two-hour run produce nothing: the exception
    escaped extract(), so the raw file was never rewritten and the build read
    the previous run's records — indistinguishable from a run that did nothing."""
    monkeypatch.setattr(extract, "COORD_BATCH", 2)
    monkeypatch.setattr(extract.time, "sleep", lambda _: None)

    def fake(sparql, contact, retries=4):
        if "wd:Q1 " in sparql:
            raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
        return {"results": {"bindings": [coord_binding("Q3", 30.6, 114.3, 1)]}}

    monkeypatch.setattr(extract, "run_query", fake)
    coordinates, failed = extract.fetch_coordinates(
        {"Q1", "Q2", "Q3", "Q4"}, "person", "c", tmp_path)

    assert coordinates == {"Q3": (30.6, 114.3, "birthplace")}
    assert failed == [0]


def test_a_failed_batch_is_not_cached_so_a_re_run_retries_it(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "COORD_BATCH", 2)
    monkeypatch.setattr(extract.time, "sleep", lambda _: None)

    attempts = []

    def fake(sparql, contact, retries=4):
        attempts.append(sparql)
        if "wd:Q1 " in sparql and len(attempts) == 1:
            raise RuntimeError("502 Bad Gateway")
        return {"results": {"bindings": [coord_binding("Q1", 41.9, 12.5, 2)]}}

    monkeypatch.setattr(extract, "run_query", fake)
    qids = {"Q1", "Q2", "Q3", "Q4"}
    extract.fetch_coordinates(qids, "person", "c", tmp_path)
    coordinates, failed = extract.fetch_coordinates(qids, "person", "c", tmp_path)

    assert failed == []
    assert coordinates["Q1"] == (41.9, 12.5, "death place")


def test_the_coordinate_cache_key_changes_with_the_batch_size(tmp_path, monkeypatch):
    """Batches are named by index, so 1000-item batches must not read caches
    written when they held 400."""
    monkeypatch.setattr(extract.time, "sleep", lambda _: None)
    monkeypatch.setattr(extract, "run_query",
                        lambda *a, **k: {"results": {"bindings": []}})
    extract.fetch_coordinates({"Q1"}, "person", "c", tmp_path)
    assert (tmp_path / f"v{extract.COORD_VERSION}-person-coords-1-0.json").exists()
