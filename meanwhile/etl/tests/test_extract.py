"""Tests for the parts of extract.py that don't touch the network.

The queries themselves can't be tested without WDQS, but the windowing, the
subdivision, the batching and the shape of what comes back can be — and those
are where a bug costs a thirty-minute run.
"""

import sys
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


# ---------- subdivision ----------

def test_a_window_splits_into_two_halves_that_tile_it():
    (a_low, a_high), (b_low, b_high) = extract.split_window(1500, 1600)
    assert (a_low, a_high) == (1500, 1550)
    assert (b_low, b_high) == (1550, 1600)


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


# ---------- chunk planning ----------

def test_chunks_are_distinctly_labelled():
    for type_ in ("person", "polity", "event"):
        labels = [label for label, _, _, _ in extract.planned_chunks(type_)]
        assert len(labels) == len(set(labels)), f"{type_} has duplicate chunk labels"


def test_queries_substitute_every_placeholder():
    for type_ in ("person", "polity", "event"):
        for label, driver, low, high in extract.planned_chunks(type_):
            sparql = extract.main_query(type_, driver, low, high)
            assert "%" not in sparql, f"{label} left a placeholder unsubstituted"


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


@pytest.mark.parametrize("type_", ["person", "polity", "event"])
def test_the_main_pass_never_does_a_second_hop(type_):
    """Dates and coordinates belong in the detail pass; inline they turn a
    multi-million-row scan into a join."""
    _, driver, low, high = extract.planned_chunks(type_)[0]
    sparql = extract.main_query(type_, driver, low, high)
    for expensive in ("psv:", "wdt:P625", "wdt:P19", "wdt:P36"):
        assert expensive not in sparql, f"{type_} main query still does {expensive}"


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


def test_main_pass_shape():
    row = extract.flatten_main(binding(
        item="http://www.wikidata.org/entity/Q935",
        name="Isaac Newton", desc="English mathematician", sitelinks="180",
        article="https://en.wikipedia.org/wiki/Isaac_Newton"))
    assert row["qid"] == "Q935"
    assert row["sitelinks"] == 180
    assert row["wiki_title"] is None          # title matches the label
    assert row["lat"] is None                 # filled by the detail pass


def test_main_pass_keeps_a_wiki_title_that_differs_from_the_label():
    row = extract.flatten_main(binding(
        item="http://www.wikidata.org/entity/Q8011",
        name="Ibn Sina", sitelinks="90",
        article="https://en.wikipedia.org/wiki/Avicenna"))
    assert row["wiki_title"] == "Avicenna"


def test_detail_pass_shape():
    detail = extract.flatten_detail(binding(
        item="http://www.wikidata.org/entity/Q935",
        birth="1643-01-04T00:00:00Z", birthPrec="11",
        death="1727-03-31T00:00:00Z", deathPrec="11",
        lat="52.8", lng="-0.6"), "person")
    assert detail["birth"] == {"time": "1643-01-04T00:00:00Z", "precision": 11}
    assert (detail["lat"], detail["lng"]) == (52.8, -0.6)


def test_the_two_passes_join_into_something_transform_accepts():
    records = [extract.flatten_main(binding(
        item="http://www.wikidata.org/entity/Q935",
        name="Isaac Newton", desc="English mathematician", sitelinks="180"))]
    details = {"Q935": extract.flatten_detail(binding(
        birth="1643-01-04T00:00:00Z", birthPrec="11",
        death="1727-03-31T00:00:00Z", deathPrec="11",
        lat="52.8", lng="-0.6"), "person")}

    extract.merge_details(records, details)
    row = tf.build_row(records[0], tf.PERSON)
    assert row is not None
    assert (row.active_start, row.active_end) == (1643, 1727)
    assert row.region_id is not None


def test_an_item_the_detail_pass_missed_is_left_alone():
    records = [{"qid": "Q1", "lat": None, "lng": None},
               {"qid": "Q2", "lat": None, "lng": None}]
    extract.merge_details(records, {"Q1": {"lat": 41.9, "lng": 12.5}})
    assert records[0]["lat"] == 41.9
    assert records[1]["lat"] is None
    # No date and no region, so transform drops it rather than inventing one.
    assert tf.build_row({**records[1], "name": "x", "sitelinks": 50}, tf.PERSON) is None
