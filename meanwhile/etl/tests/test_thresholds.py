"""Tests for the threshold scan.

It only counts, but it counts the numbers a data decision will be made from,
so the counting needs to be right — particularly the two things that would
quietly inflate a thin region: duplicate QIDs, and rows outside the era.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import thresholds
import transform as tf


def person(qid, sitelinks, lat=41.9, lng=12.5, year=1500):
    return {"qid": qid, "name": qid, "sitelinks": sitelinks, "lat": lat, "lng": lng,
            "birth": {"time": f"+{year:04d}-01-01T00:00:00Z", "precision": 9}}


ROME, XIAN = (41.9, 12.5), (34.3, 108.9)


def test_it_counts_below_the_live_threshold():
    """The whole point: seeing what the current cutoff is throwing away."""
    scores = thresholds.tally([person("Q1", 5, *ROME)], tf.PERSON)
    southern_europe = next(r.id for r in thresholds.regions_mod.REGIONS
                           if r.name == "Southern Europe")
    assert scores[southern_europe] == [5]
    # ...which the real threshold (10, strict in Europe) would drop.
    assert tf.build_row(person("Q1", 5, *ROME), tf.PERSON) is None


def test_a_qid_seen_twice_is_counted_once():
    scores = thresholds.tally([person("Q1", 5), person("Q1", 40)], tf.PERSON)
    assert sum(len(v) for v in scores.values()) == 1
    assert max(next(iter(scores.values()))) == 40


def test_rows_outside_the_era_are_not_counted():
    records = [person("Q1", 50, *ROME, year=1500), person("Q2", 50, *ROME, year=200)]
    scores = thresholds.tally(records, tf.PERSON, era=1500)
    assert sum(len(v) for v in scores.values()) == 1


def test_a_span_straddling_the_era_counts():
    """A person born in 1480 was alive in 1500; the era filter is an overlap,
    not a containment, for the same reason the app's year query is."""
    scores = thresholds.tally([person("Q1", 50, *ROME, year=1480)], tf.PERSON, era=1500)
    assert sum(len(v) for v in scores.values()) == 1


def test_kept_counts_at_or_above_the_bar():
    scores = {1: [3, 4, 10, 40]}
    assert thresholds.kept(scores, 1, 4) == 3
    assert thresholds.kept(scores, 1, 10) == 2
    assert thresholds.kept(scores, 1, 99) == 0
    assert thresholds.kept(scores, 99, 4) == 0


def test_an_unplaceable_row_is_not_counted():
    no_coordinates = {"qid": "Q1", "name": "x", "sitelinks": 50,
                      "birth": {"time": "+1500-01-01T00:00:00Z", "precision": 9}}
    assert thresholds.tally([no_coordinates], tf.PERSON) == {}
