"""Tests for inlining real data into the prototype.

The failure that matters here is silent: producing a page that looks fine and
is still showing the hand-written sample.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import build_db
import build_prototype

PROTOTYPE = Path(__file__).resolve().parents[2] / "prototype.html"


def test_the_prototype_still_contains_the_marker():
    """If this fails, the build would silently ship the sample instead."""
    assert build_prototype.MARKER in PROTOTYPE.read_text(encoding="utf-8")


def test_the_prototype_alone_falls_back_to_its_sample():
    html = PROTOTYPE.read_text(encoding="utf-8")
    assert "const LOADED = null;" in html
    assert "LOADED || RAW.map" in html


def test_inlining_replaces_the_marker():
    out = build_prototype.inline(f"before\n{build_prototype.MARKER}\nafter", [["X"]])
    assert build_prototype.MARKER not in out
    assert 'const LOADED = [["X"]];' in out


def test_a_missing_marker_fails_loudly():
    with pytest.raises(SystemExit):
        build_prototype.inline("no marker here", [["X"]])


def test_non_ascii_names_survive():
    """Most of the dataset outside Europe has them."""
    out = build_prototype.inline(build_prototype.MARKER, [["杜甫", "person"]])
    assert "杜甫" in out


def test_the_data_round_trips():
    rows = [["Ibn Sina", "person", 980, 1037, 6, 90, "Persian polymath", "Avicenna"]]
    out = build_prototype.inline(build_prototype.MARKER, rows)
    inlined = out[out.index("[["):out.rindex("]]") + 2]
    assert json.loads(inlined) == rows


# ---------- the two builds answer different questions ----------

def row(name, start, end, region, notability):
    return [name, "person", start, end, region, notability, "", None]


def test_the_cap_is_per_era_not_per_region():
    """A flat per-region cap would hand the prototype the nineteenth century
    and nothing before it."""
    rows = [row(f"early{i}", 1000, 1010, 1, 100 - i) for i in range(20)]
    rows += [row(f"late{i}", 1800, 1810, 1, 100 - i) for i in range(20)]
    kept = build_db.sample(rows, per_cell=5)
    assert len([r for r in kept if r[0].startswith("early")]) == 5
    assert len([r for r in kept if r[0].startswith("late")]) == 5


def test_the_cap_keeps_the_most_notable():
    rows = [row(f"p{n}", 1800, 1810, 1, n) for n in range(10)]
    kept = build_db.sample(rows, per_cell=3)
    assert sorted(r[5] for r in kept) == [7, 8, 9]


def test_a_raised_cap_is_what_shows_density():
    """The twelve-per-cell slice flattens every crowded cell to twelve, so it
    cannot answer what a crowded region does to the layout."""
    rows = [row(f"p{n}", 1800, 1810, 1, n) for n in range(300)]
    assert len(build_db.sample(rows, per_cell=12)) == 12
    assert len(build_db.sample(rows, per_cell=400)) == 300


def test_a_long_span_is_counted_once():
    """A polity spanning five eras should not appear five times."""
    kept = build_db.sample([row("Empire", 1000, 2000, 1, 50)], per_cell=12)
    assert len(kept) == 1


def test_the_db_shape_matches_what_the_prototype_eats():
    """build_prototype reads columns out of SQLite in the order the page
    indexes them; a reordering here would silently mislabel everything."""
    bd = build_db
    columns = ["name", "type", "active_start", "active_end", "region_id",
               "notability", "description", "wiki_title"]
    for column in columns:
        assert column in bd.SCHEMA, f"{column} is not a column of entries"
