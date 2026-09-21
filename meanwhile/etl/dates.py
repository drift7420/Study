"""Wikidata time values to astronomical years and bounded intervals.

Wikidata writes years with a sign and the year as conventionally numbered, so
"-0551" means 551 BCE. We store astronomical years instead (1 BCE = 0,
2 BCE = -1), which lets plain integer comparison do interval arithmetic.

An imprecise date becomes a wider interval rather than a guessed point: a
century-precision value covers its whole century. The overlap queries then
handle vagueness without any special cases.
"""

from dataclasses import dataclass

DAY, MONTH, YEAR, DECADE, CENTURY, MILLENNIUM = 11, 10, 9, 8, 7, 6

PRECISION_LABEL = {
    DAY: "year", MONTH: "year", YEAR: "year",
    DECADE: "decade", CENTURY: "century", MILLENNIUM: "millennium",
}


@dataclass(frozen=True)
class TimePoint:
    year: int         # astronomical, best single estimate
    precision: str    # year | decade | century | millennium
    start: int        # earliest year the value could mean
    end: int          # latest year the value could mean


def to_astronomical(signed_year: int, negative: bool) -> int:
    """551 BCE -> -550. 1893 CE -> 1893."""
    if not negative:
        return signed_year
    return 1 - signed_year


def parse_time(value) -> TimePoint | None:
    """Parse a Wikidata time claim. Returns None if unusably coarse or malformed."""
    if not value:
        return None
    raw = value.get("time")
    precision = value.get("precision")
    if not raw or precision is None:
        return None
    try:
        precision = int(precision)
    except (TypeError, ValueError):
        return None
    if precision < MILLENNIUM:
        return None

    negative = raw.startswith("-")
    body = raw[1:] if raw[0] in "+-" else raw
    head = body.split("-", 1)[0]
    try:
        written = int(head)
    except ValueError:
        return None

    year = to_astronomical(written, negative)
    label = PRECISION_LABEL.get(precision, "year")

    if precision >= YEAR:
        start = end = year
    else:
        width = {DECADE: 10, CENTURY: 100, MILLENNIUM: 1000}[precision]
        start = (year // width) * width
        end = start + width - 1

    return TimePoint(year=year, precision=label, start=start, end=end)


def format_year(year: int) -> str:
    """Astronomical year to human form. 0 -> '1 BCE'."""
    return f"{1 - year} BCE" if year <= 0 else str(year)
