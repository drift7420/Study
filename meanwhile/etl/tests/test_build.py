"""End-to-end check of everything downstream of the network: transform -> SQLite
-> the two queries the app actually runs."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_db
import transform as tf


def wd(time, precision=9):
    return {"time": time, "precision": precision}


PEOPLE = [
    {"qid": "Q935", "name": "Isaac Newton", "desc": "English mathematician", "sitelinks": 180,
     "lat": 52.8, "lng": -0.6, "birth": wd("+1643-01-04T00:00:00Z", 11),
     "death": wd("+1727-03-31T00:00:00Z", 11), "wiki_title": None},
    {"qid": "Q619", "name": "Nicolaus Copernicus", "desc": "Renaissance astronomer",
     "sitelinks": 170, "lat": 53.0, "lng": 18.6,
     "birth": wd("+1473-02-19T00:00:00Z", 11), "death": wd("+1543-05-24T00:00:00Z", 11)},
    {"qid": "Q4085", "name": "Confucius", "desc": "Chinese philosopher", "sitelinks": 160,
     "lat": 35.6, "lng": 116.9, "birth": wd("-0551-01-01T00:00:00Z", 9),
     "death": wd("-0479-01-01T00:00:00Z", 9)},
    {"qid": "Q332874", "name": "Mansa Musa", "desc": "ruler of the Mali Empire",
     "sitelinks": 7, "lat": 16.8, "lng": -3.0, "birth": wd("+1280-01-01T00:00:00Z", 9),
     "death": wd("+1337-01-01T00:00:00Z", 9)},
]

POLITIES = [
    {"qid": "Q12560", "name": "Ottoman Empire", "desc": "former empire", "sitelinks": 150,
     "lat": 41.0, "lng": 29.0, "inception": wd("+1299-01-01T00:00:00Z", 9),
     "dissolved": wd("+1922-01-01T00:00:00Z", 9)},
    {"qid": "Q7462", "name": "Mali Empire", "desc": "West African empire", "sitelinks": 60,
     "lat": 16.8, "lng": -3.0, "inception": wd("+1235-01-01T00:00:00Z", 9),
     "dissolved": wd("+1670-01-01T00:00:00Z", 9)},
]

EVENTS = [
    {"qid": "Q83164", "name": "Fall of Constantinople", "desc": "1453 siege", "sitelinks": 90,
     "lat": 41.0, "lng": 29.0, "point_in_time": wd("+1453-05-29T00:00:00Z", 11)},
]


def build(tmp_path):
    rows = (tf.transform(PEOPLE, tf.PERSON)
            + tf.transform(POLITIES, tf.POLITY)
            + tf.transform(EVENTS, tf.EVENT))
    db = tmp_path / "test.db"
    build_db.build(rows, db)
    return rows, sqlite3.connect(db)


def test_every_fixture_survives_transform(tmp_path):
    rows, _ = build(tmp_path)
    # Mansa Musa has only 7 sitelinks but sits in a relaxed region.
    assert {r.name for r in rows} == {
        "Isaac Newton", "Nicolaus Copernicus", "Confucius", "Mansa Musa",
        "Ottoman Empire", "Mali Empire", "Fall of Constantinople"}


def test_regions_and_entries_are_written(tmp_path):
    _, connection = build(tmp_path)
    assert connection.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 22
    assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 7


def test_full_text_search_finds_by_name_and_description(tmp_path):
    _, connection = build(tmp_path)
    found = connection.execute(
        "SELECT name FROM entries WHERE id IN "
        "(SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)", ("astronomer",)
    ).fetchall()
    assert [r[0] for r in found] == ["Nicolaus Copernicus"]


def test_year_window_catches_a_single_year_event(tmp_path):
    """The bug the prototype surfaced: at 10-year resolution a 1453 event must
    still appear when the slider sits on 1450."""
    _, connection = build(tmp_path)
    names = [r[0] for r in connection.execute(
        "SELECT name FROM entries WHERE active_start < :year + :step AND active_end >= :year",
        {"year": 1450, "step": 10})]
    assert "Fall of Constantinople" in names


def test_point_query_would_have_missed_it(tmp_path):
    """Same data, the old point query — kept so the regression stays visible."""
    _, connection = build(tmp_path)
    names = [r[0] for r in connection.execute(
        "SELECT name FROM entries WHERE active_start <= ? AND active_end >= ?", (1450, 1450))]
    assert "Fall of Constantinople" not in names


def test_contemporaries_uses_the_adult_overlap_rule(tmp_path):
    _, connection = build(tmp_path)
    newton = connection.execute(
        "SELECT id, active_start, active_end FROM entries WHERE name = 'Isaac Newton'"
    ).fetchone()
    peers = [r[0] for r in connection.execute(
        "SELECT name FROM entries WHERE type = 'person' AND id != :id "
        "AND active_start + 15 <= :active_end AND active_end >= :active_start + 15",
        {"id": newton[0], "active_start": newton[1], "active_end": newton[2]})]
    # Copernicus died a century before Newton was born.
    assert peers == []


def test_bce_dates_round_trip_through_the_database(tmp_path):
    _, connection = build(tmp_path)
    start, end = connection.execute(
        "SELECT active_start, active_end FROM entries WHERE name = 'Confucius'").fetchone()
    assert (start, end) == (-550, -478)      # 551 BCE to 479 BCE
    assert start < end < 0


def test_an_item_matching_two_class_trees_yields_one_row(tmp_path):
    """Wikidata's hierarchies overlap, so the same QID can arrive from two
    extractions. entries.qid is UNIQUE, so the build fails unless one wins."""
    shared = {"qid": "Q7462", "name": "Mali Empire", "sitelinks": 60,
              "lat": 16.8, "lng": -3.0}
    as_polity = tf.transform([{**shared, "inception": wd("+1235-01-01T00:00:00Z"),
                               "dissolved": wd("+1670-01-01T00:00:00Z")}], tf.POLITY)
    as_event = tf.transform([{**shared, "start_time": wd("+1235-01-01T00:00:00Z"),
                              "end_time": wd("+1670-01-01T00:00:00Z")}], tf.EVENT)

    rows = tf.deduplicate(as_polity + as_event)
    assert len(rows) == 1
    assert rows[0].type == tf.POLITY      # the more specific reading wins

    build_db.build(rows, tmp_path / "dedup.db")      # would raise without it


def test_deduplicate_prefers_the_better_sourced_row_within_a_type(tmp_path):
    quiet = tf.transform([{"qid": "Q1", "name": "X", "sitelinks": 10, "lat": 41.9,
                           "lng": 12.5, "inception": wd("+1500-01-01T00:00:00Z")}], tf.POLITY)
    loud = tf.transform([{"qid": "Q1", "name": "X", "sitelinks": 90, "lat": 41.9,
                          "lng": 12.5, "inception": wd("+1500-01-01T00:00:00Z")}], tf.POLITY)
    rows = tf.deduplicate(quiet + loud)
    assert len(rows) == 1 and rows[0].notability == 90


def test_mockup_json_is_written(tmp_path):
    rows, _ = build(tmp_path)
    out = tmp_path / "mockup_data.json"
    build_db.write_mockup_json(rows, out)
    assert out.exists() and out.stat().st_size > 0
