"""Tests for the sitelink-floor probe.

It asks one question, but the answer decides whether we re-extract the whole
dataset at a lower floor — so the question had better be the one we think
we're asking.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extract
import probe_floor
import regions
import transform as tf


def test_the_floor_is_read_from_extract_not_repeated():
    """If the two ever disagree, the probe reports a line that isn't the one
    the extraction actually draws."""
    assert probe_floor.FLOOR == extract.MIN_SITELINKS["person"]


def test_the_probe_asks_below_the_floor():
    """The whole point is seeing what a real run never fetches."""
    query = extract.main_query("person", "P569", 1800, 1802, min_sitelinks=1)
    assert "FILTER(?sitelinks >= 1)" in query


def test_the_real_extraction_floor_is_untouched():
    query = extract.main_query("person", "P569", 1800, 1802)
    assert f"FILTER(?sitelinks >= {extract.MIN_SITELINKS['person']})" in query


def test_it_reuses_the_query_shape_that_works():
    """Asking per Wikipedia edition cost what the edition cost, not what the
    window cost: Swahili answered ten years while English timed out on one."""
    query = extract.main_query("person", "P569", 1800, 1802, min_sitelinks=1)
    assert "schema:isPartOf" not in query.replace(
        "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/>", "")


def person(qid, sitelinks, lat, lng, year=1800):
    return {"qid": qid, "name": qid, "sitelinks": sitelinks, "lat": lat, "lng": lng,
            "birth": {"time": f"+{year:04d}-01-01T00:00:00Z", "precision": 9}}


ROME, XIAN = (41.9, 12.5), (34.3, 108.9)


def test_the_floor_splits_each_region_in_two():
    east_asia = next(r.id for r in regions.REGIONS if r.name == "East Asia")
    counts = probe_floor.tally([person("Q1", 1, *XIAN), person("Q2", 2, *XIAN),
                                person("Q3", 40, *XIAN)])
    assert counts[east_asia] == (2, 1)


def test_an_item_exactly_at_the_floor_counts_as_visible():
    """Off by one here would put the whole comparison one sitelink out."""
    southern_europe = next(r.id for r in regions.REGIONS if r.name == "Southern Europe")
    counts = probe_floor.tally([person("Q1", probe_floor.FLOOR, *ROME)])
    assert counts[southern_europe] == (0, 1)


def test_a_qid_seen_twice_is_counted_once_at_its_best():
    counts = probe_floor.tally([person("Q1", 1, *ROME), person("Q1", 40, *ROME)])
    assert sum(h + v for h, v in counts.values()) == 1
    assert sum(v for _, v in counts.values()) == 1


def test_an_unplaceable_person_is_not_counted():
    """No coordinate, no region — the same rule the pipeline uses, so the
    probe describes the population the app would actually have."""
    no_coordinates = {"qid": "Q1", "name": "x", "sitelinks": 1,
                      "birth": {"time": "+1800-01-01T00:00:00Z", "precision": 9}}
    assert probe_floor.tally([no_coordinates]) == {}


def test_regions_are_counted_separately():
    counts = probe_floor.tally([person("Q1", 1, *XIAN), person("Q2", 1, *ROME)])
    assert len(counts) == 2
    assert all(c == (1, 0) for c in counts.values())
