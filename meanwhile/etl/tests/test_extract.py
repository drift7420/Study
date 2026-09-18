"""Tests for the parts of extract.py that don't touch the network.

The queries themselves can't be tested without WDQS, but the chunking, the
batching and the shape of what comes back can be — and those are where the
bugs that waste a 30-minute run live.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extract
import transform as tf


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


def test_chunks_are_distinctly_labelled_and_fully_substituted():
    for type_ in ("person", "polity", "event"):
        chunks = extract.chunks_for(type_)
        labels = [label for label, _ in chunks]
        assert len(labels) == len(set(labels)), f"{type_} has duplicate chunk labels"
        for label, sparql in chunks:
            assert "%" not in sparql, f"{label} left a placeholder unsubstituted"


def test_no_query_calls_YEAR():
    """A function call can't use the date index, so YEAR(?d) < 1 scans every
    date in Wikidata and times the query out. That 504 is why windows exist."""
    for type_ in ("person", "polity", "event"):
        for label, sparql in extract.chunks_for(type_):
            assert "YEAR(" not in sparql, f"{label} filters with YEAR()"


def test_every_query_leads_with_its_date_filter():
    """Putting the class or type pattern first scans every human, or walks the
    whole subclass tree, before anything narrows it."""
    for type_ in ("person", "polity", "event"):
        for label, sparql in extract.chunks_for(type_):
            filter_at = sparql.index("FILTER(?driver")
            for late in ("wd:Q5", "wdt:P279*", "wikibase:sitelinks"):
                if late in sparql:
                    assert filter_at < sparql.index(late), f"{label}: {late} precedes the date filter"


def test_death_driven_chunks_exist_so_people_with_no_birth_date_survive():
    labels = [label for label, _ in extract.chunks_for("person")]
    assert any("P570" in label for label in labels)
    assert any("P1317" in label for label in labels)   # floruit, for ancient figures


def test_batched_covers_everything_exactly_once():
    items = list(range(1000))
    batches = list(extract.batched(items, size=400))
    assert [len(b) for b in batches] == [400, 400, 200]
    assert [x for b in batches for x in b] == items


def test_coordinate_merge_attaches_and_leaves_misses_alone():
    records = [{"qid": "Q1", "lat": None, "lng": None},
               {"qid": "Q2", "lat": None, "lng": None}]
    extract.merge_coordinates(records, {"Q1": (41.9, 12.5)})
    assert (records[0]["lat"], records[0]["lng"]) == (41.9, 12.5)
    assert records[1]["lat"] is None


def binding(**kw):
    return {k: {"value": v} for k, v in kw.items()}


def test_flatten_produces_what_transform_consumes():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q935",
        name="Isaac Newton", desc="English mathematician", sitelinks="180",
        birth="1643-01-04T00:00:00Z", birthPrec="11",
        death="1727-03-31T00:00:00Z", deathPrec="11",
        article="https://en.wikipedia.org/wiki/Isaac_Newton",
    ), "person")

    assert row["qid"] == "Q935"
    assert row["sitelinks"] == 180
    assert row["birth"] == {"time": "1643-01-04T00:00:00Z", "precision": 11}
    # Title matches the label, so no override is stored.
    assert row["wiki_title"] is None

    row["lat"], row["lng"] = 52.8, -0.6
    built = tf.build_row(row, tf.PERSON)
    assert built is not None and built.active_start == 1643


def test_flatten_keeps_a_wiki_title_that_differs_from_the_label():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q8011",
        name="Ibn Sina", desc="Persian polymath", sitelinks="90",
        article="https://en.wikipedia.org/wiki/Avicenna",
    ), "person")
    assert row["wiki_title"] == "Avicenna"


def test_flatten_survives_missing_optionals():
    row = extract.flatten(binding(
        item="http://www.wikidata.org/entity/Q1", name="Someone", sitelinks="5"), "person")
    assert row["desc"] is None
    assert row["lat"] is None and row["lng"] is None
    assert "birth" not in row
