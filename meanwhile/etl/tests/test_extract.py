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


def test_person_windows_cover_the_range_without_gaps_or_overlap():
    windows = [(lo, hi) for _, lo, hi in extract.person_windows() if lo is not None]
    assert windows[0][0] == 1
    assert windows[-1][1] == 1900
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert end == start


def test_person_windows_include_a_bce_chunk():
    labels = [label for label, _, _ in extract.person_windows()]
    assert "bce" in labels


def test_person_chunks_are_all_distinctly_labelled():
    labels = [label for label, _ in extract.chunks_for("person")]
    assert len(labels) == len(set(labels))


def test_person_chunks_substitute_every_placeholder():
    for label, sparql in extract.chunks_for("person"):
        assert "%" not in sparql, f"{label} left a placeholder unsubstituted"


def test_polity_and_event_chunks_substitute_every_placeholder():
    for type_ in ("polity", "event"):
        for label, sparql in extract.chunks_for(type_):
            assert "%" not in sparql, f"{label} left a placeholder unsubstituted"


def test_window_queries_lead_with_the_date_filter():
    """The 502 that started this: putting wdt:P31 wd:Q5 first scans every human
    before anything narrows it."""
    _, sparql = extract.chunks_for("person")[1]
    assert sparql.index("wdt:P569") < sparql.index("wd:Q5")


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
