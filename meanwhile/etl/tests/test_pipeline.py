import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from dates import parse_time, format_year, to_astronomical
import regions
import transform as tf


def wd(time, precision):
    return {"time": time, "precision": precision}


# ---------- dates ----------

def test_ce_year():
    p = parse_time(wd("+1893-12-26T00:00:00Z", 11))
    assert (p.year, p.precision, p.start, p.end) == (1893, "year", 1893, 1893)


def test_bce_year_is_shifted_by_one():
    # Wikidata "-0551" means 551 BCE, which is astronomical -550.
    p = parse_time(wd("-0551-01-01T00:00:00Z", 9))
    assert p.year == -550
    assert format_year(p.year) == "551 BCE"


def test_one_bce_is_year_zero():
    assert to_astronomical(1, negative=True) == 0
    assert format_year(0) == "1 BCE"


def test_century_precision_widens_to_the_century():
    p = parse_time(wd("+1200-01-01T00:00:00Z", 7))
    assert p.precision == "century"
    assert (p.start, p.end) == (1200, 1299)


def test_bce_century_widens_downward():
    p = parse_time(wd("-0550-01-01T00:00:00Z", 7))   # 550 BCE -> astronomical -549
    assert p.start <= p.year <= p.end
    assert p.end - p.start == 99


def test_decade_precision():
    p = parse_time(wd("+1840-01-01T00:00:00Z", 8))
    assert (p.start, p.end) == (1840, 1849)


def test_too_coarse_is_dropped():
    assert parse_time(wd("+10000-01-01T00:00:00Z", 3)) is None


def test_malformed_is_dropped():
    assert parse_time(wd("not-a-date", 9)) is None
    assert parse_time(None) is None
    assert parse_time({"time": "+1893-01-01T00:00:00Z"}) is None


# ---------- regions ----------

@pytest.mark.parametrize("name,lat,lng,expected", [
    ("Rome",         41.9,  12.5, "Southern Europe"),
    ("Paris",        48.9,   2.4, "Western Europe"),
    ("London",       51.5,  -0.1, "Western Europe"),
    ("Stockholm",    59.3,  18.1, "Northern Europe"),
    ("Kyiv",         50.5,  30.5, "Eastern Europe"),
    ("Istanbul",     41.0,  29.0, "Anatolia & Levant"),
    ("Baghdad",      33.3,  44.4, "Mesopotamia & Persia"),
    ("Mecca",        21.4,  39.8, "Arabia"),
    ("Sana'a",       15.3,  44.2, "Arabia"),
    ("Jerusalem",    31.8,  35.2, "Anatolia & Levant"),
    ("Cairo",        30.0,  31.2, "Egypt & the Nile"),
    ("Meroe",        16.9,  33.7, "Egypt & the Nile"),
    ("Timbuktu",     16.8,  -3.0, "West Africa"),
    ("Aksum",        14.1,  38.7, "East Africa"),
    ("Great Zimbabwe", -20.3, 30.9, "Southern Africa"),
    ("Delhi",        28.6,  77.2, "South Asia"),
    ("Samarkand",    39.6,  66.9, "Central Asia"),
    ("Xi'an",        34.3, 108.9, "East Asia"),
    ("Guangzhou",    23.1, 113.3, "East Asia"),
    ("Kyoto",        35.0, 135.8, "Japan & Korea"),
    ("Angkor",       13.4, 103.9, "Southeast Asia"),
    ("Jakarta",      -6.2, 106.8, "Southeast Asia"),
    ("Sydney",      -33.9, 151.2, "Oceania"),
    ("Honolulu",     21.3, -157.9, "Oceania"),
    ("Cahokia",      38.7, -90.1, "North America"),
    ("Tenochtitlan", 19.4, -99.1, "Mesoamerica"),
    ("Cusco",       -13.5, -72.0, "The Andes"),
    ("Buenos Aires",-34.6, -58.4, "Amazonia & the Cone"),
])
def test_known_cities_land_in_the_right_region(name, lat, lng, expected):
    rid, confidence = regions.assign(lat, lng)
    assert rid is not None, f"{name} was unassigned"
    assert regions.BY_ID[rid].name == expected, (
        f"{name} -> {regions.BY_ID[rid].name} ({confidence}), expected {expected}")


def test_missing_coordinates_are_unassigned():
    assert regions.assign(None, None) == (None, "unassigned")


def test_deep_ocean_is_unassigned_not_guessed():
    rid, confidence = regions.assign(-40.0, -120.0)   # South Pacific, nothing near
    assert rid is None and confidence == "unassigned"


# ---------- transform ----------

def person(**kw):
    base = {"qid": "Q1", "name": "Test", "sitelinks": 50, "lat": 41.9, "lng": 12.5}
    base.update(kw)
    return base


def test_person_with_both_dates():
    row = tf.build_row(person(birth=wd("+1643-01-04T00:00:00Z", 11),
                              death=wd("+1727-03-31T00:00:00Z", 11)), tf.PERSON)
    assert (row.active_start, row.active_end) == (1643, 1727)
    assert row.date_confidence == "exact"


def test_person_with_only_a_birth_gets_an_inferred_span():
    row = tf.build_row(person(birth=wd("+1200-01-01T00:00:00Z", 9)), tf.PERSON)
    assert row.active_start == 1200
    assert row.active_end == 1200 + tf.ASSUMED_LIFESPAN
    assert row.date_confidence == "inferred"
    assert row.death_year is None


def test_person_with_only_a_death_gets_an_inferred_span():
    row = tf.build_row(person(death=wd("+1200-01-01T00:00:00Z", 9)), tf.PERSON)
    assert row.active_end == 1200
    assert row.active_start == 1200 - tf.ASSUMED_PRE_DEATH


def test_person_with_only_floruit():
    row = tf.build_row(person(floruit=wd("+0850-01-01T00:00:00Z", 9)), tf.PERSON)
    assert row.date_confidence == "inferred"
    assert row.active_end - row.active_start == tf.FLORUIT_SPAN


def test_person_with_no_dates_is_dropped():
    assert tf.build_row(person(), tf.PERSON) is None


def test_absurd_lifespan_is_clamped():
    row = tf.build_row(person(birth=wd("+1000-01-01T00:00:00Z", 9),
                              death=wd("+1400-01-01T00:00:00Z", 9)), tf.PERSON)
    assert row.active_end - row.active_start == tf.MAX_LIFESPAN


def test_person_without_coordinates_is_dropped():
    assert tf.build_row(person(birth=wd("+1643-01-01T00:00:00Z", 11), lat=None, lng=None),
                        tf.PERSON) is None


def test_below_threshold_is_dropped():
    rec = person(birth=wd("+1643-01-01T00:00:00Z", 11), sitelinks=3)
    assert tf.build_row(rec, tf.PERSON) is None


def test_thin_regions_get_a_relaxed_threshold():
    # Same sitelink count, one in Italy and one in West Africa.
    kw = dict(birth=wd("+1300-01-01T00:00:00Z", 9), sitelinks=5)
    in_europe = tf.build_row(person(**kw), tf.PERSON)
    in_west_africa = tf.build_row(person(lat=16.8, lng=-3.0, **kw), tf.PERSON)
    assert in_europe is None
    assert in_west_africa is not None


def test_polity_without_an_end_runs_to_the_present():
    rec = {"qid": "Q2", "name": "Somewhere", "sitelinks": 20, "lat": 41.9, "lng": 12.5,
           "inception": wd("+1800-01-01T00:00:00Z", 9)}
    row = tf.build_row(rec, tf.POLITY)
    assert row.active_end == tf.PRESENT
    assert row.date_confidence == "inferred"


def test_event_point_in_time_is_a_single_year():
    rec = {"qid": "Q3", "name": "A battle", "sitelinks": 20, "lat": 41.0, "lng": 29.0,
           "point_in_time": wd("+1453-05-29T00:00:00Z", 11)}
    row = tf.build_row(rec, tf.EVENT)
    assert (row.active_start, row.active_end) == (1453, 1453)


def test_duplicates_keep_the_better_sourced_row():
    a = person(qid="Q9", birth=wd("+1500-01-01T00:00:00Z", 9), sitelinks=20)
    b = person(qid="Q9", birth=wd("+1500-01-01T00:00:00Z", 9), sitelinks=60)
    rows = tf.transform([a, b], tf.PERSON)
    assert len(rows) == 1 and rows[0].notability == 60


def test_coverage_counts_a_long_span_in_every_bucket_it_touches():
    rec = {"qid": "Q4", "name": "Long empire", "sitelinks": 40, "lat": 41.9, "lng": 12.5,
           "inception": wd("+0100-01-01T00:00:00Z", 9),
           "dissolved": wd("+1100-01-01T00:00:00Z", 9)}
    rows = tf.transform([rec], tf.POLITY)
    assert len(tf.coverage(rows)) == 3      # 0-500, 500-1000, 1000-1500


# ---------- timeline bounds ----------

def test_a_palaeolithic_date_is_dropped():
    """Wikidata holds dates at 72,000 BCE. They are unreachable in an app whose
    timeline starts at 3000 BCE, and they stretch the coverage report across
    dozens of empty columns."""
    rec = {"qid": "Q9", "name": "Toba eruption", "sitelinks": 40, "lat": 2.6, "lng": 98.8,
           "point_in_time": wd("-74000-01-01T00:00:00Z", 6)}
    assert tf.build_row(rec, tf.EVENT) is None


def test_a_span_straddling_the_floor_is_clamped_not_dropped():
    rec = {"qid": "Q10", "name": "A long culture", "sitelinks": 40, "lat": 41.9, "lng": 12.5,
           "start_time": wd("-4000-01-01T00:00:00Z", 9),
           "end_time": wd("-2000-01-01T00:00:00Z", 9)}
    row = tf.build_row(rec, tf.EVENT)
    assert row is not None
    assert row.active_start == tf.FLOOR
    assert row.active_end == -1999


def test_an_end_beyond_the_present_is_clamped():
    rec = {"qid": "Q11", "name": "Somewhere", "sitelinks": 40, "lat": 41.9, "lng": 12.5,
           "inception": wd("+1900-01-01T00:00:00Z", 9)}
    row = tf.build_row(rec, tf.POLITY)
    assert row.active_end == tf.CEILING
