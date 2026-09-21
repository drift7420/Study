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
    """The whole point is seeing what a real run never fetches, so the floor
    sorts the rows rather than filtering them out."""
    query = probe_floor.query_for(1800, 1801)
    assert "FILTER(?sitelinks" not in query
    assert f'IF(?sitelinks >= {probe_floor.FLOOR}, "visible", "hidden")' in query


def test_the_real_extraction_floor_is_untouched():
    """The probe looks below the floor; it must not move it."""
    query = extract.main_query("person", "P569", 1800, 1802)
    assert f"FILTER(?sitelinks >= {extract.MIN_SITELINKS['person']})" in query


def test_it_keeps_the_join_order_that_works():
    """Leading with the type scans every human in Wikidata before anything
    narrows it — the same rule every extraction query follows."""
    query = probe_floor.query_for(1800, 1801)
    assert query.index("FILTER(?driver") < query.index("wd:Q5")
    assert "YEAR(" not in query


def test_it_asks_for_nothing_it_does_not_need():
    """Two columns per person was still 20,000 rows and three megabytes, and
    WDQS cut the transfer three times. The server does the counting now."""
    query = probe_floor.query_for(1800, 1801)
    assert "COUNT(*)" in query and "GROUP BY" in query
    for extra in ("SERVICE", "OPTIONAL", "schema:isPartOf", "rdfs:label"):
        assert extra not in query, f"the probe is still asking for {extra}"


def region(name):
    return next(r.id for r in regions.REGIONS if r.name == name)


ITALY, CHINA = "Q38", "Q148"
PLACED = {ITALY: region("Southern Europe"), CHINA: region("East Asia")}


def test_the_floor_splits_each_region_in_two():
    counts = probe_floor.tally([(CHINA, "hidden", 61), (CHINA, "visible", 45)], PLACED)
    assert counts[region("East Asia")] == (61, 45)


def test_counts_from_several_countries_fold_into_one_region():
    placed = {ITALY: region("Southern Europe"), "Q172579": region("Southern Europe")}
    counts = probe_floor.tally(
        [(ITALY, "hidden", 10), ("Q172579", "hidden", 5)], placed)
    assert counts[region("Southern Europe")] == (15, 0)


def test_an_unplaceable_country_takes_its_people_with_it():
    """The pipeline drops anything it cannot put on the map; so does this,
    rather than quietly counting it somewhere."""
    assert probe_floor.tally([("Q999", "hidden", 400)], PLACED) == {}


def test_regions_are_counted_separately():
    counts = probe_floor.tally(
        [(CHINA, "hidden", 1), (ITALY, "hidden", 1)], PLACED)
    assert len(counts) == 2
    assert all(c == (1, 0) for c in counts.values())
