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


def test_the_floor_is_read_from_extract_not_repeated():
    """If the two ever disagree, the probe reports a line that isn't the one
    the extraction actually draws."""
    assert probe_floor.FLOOR == extract.MIN_SITELINKS["person"]


def test_the_window_is_bounded_on_both_sides():
    query = probe_floor.query_for("zh", 1800, 10)
    assert '?driver >= "1800-01-01T00:00:00Z"' in query
    assert '?driver < "1810-01-01T00:00:00Z"' in query


def test_it_leads_with_the_date_filter():
    """Same reason every other query does: starting from wd:Q5 scans every
    human in Wikidata before anything narrows it."""
    query = probe_floor.query_for("zh", 1800, 10)
    assert query.index("FILTER(?driver") < query.index("wd:Q5")


def test_the_edition_is_substituted_everywhere():
    query = probe_floor.query_for("ja", 1800, 10)
    assert "https://ja.wikipedia.org/" in query
    assert "%" not in query


def test_both_buckets_are_read_back():
    payload = {"results": {"bindings": [
        {"bucket": {"value": "visible"}, "n": {"value": "1108"}},
        {"bucket": {"value": "hidden"}, "n": {"value": "3102"}}]}}
    assert probe_floor.counts(payload) == {"visible": 1108, "hidden": 3102}


def test_a_missing_bucket_is_zero_not_absent():
    """An edition where every biography clears the floor returns one row, and
    the share calculation still has to work."""
    payload = {"results": {"bindings": [
        {"bucket": {"value": "visible"}, "n": {"value": "40"}}]}}
    assert probe_floor.counts(payload) == {"visible": 40, "hidden": 0}
