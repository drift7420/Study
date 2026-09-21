"""Tests for inlining real data into the prototype.

The failure that matters here is silent: producing a page that looks fine and
is still showing the hand-written sample.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

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
