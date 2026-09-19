"""The 22 regions, and assigning a coordinate to one of them.

Boxes are checked in the order listed and the first hit wins, so the list is
ordered by specificity: East Asia before Southeast Asia (or Guangzhou lands in
the wrong one), Southeast Asia before Oceania (or Indonesia does), the Andes
before Amazonia. A coordinate that hits no box falls back to the nearest
centroid, and anything further than FAR_KM from every centroid is left
unassigned rather than guessed into a region it doesn't belong to.
"""

from dataclasses import dataclass, field
from math import radians, sin, cos, asin, sqrt

FAR_KM = 2500.0


@dataclass(frozen=True)
class Region:
    id: int
    name: str
    lng: float
    lat: float
    # (lat_min, lat_max, lng_min, lng_max)
    boxes: tuple = field(default=())


# Ordered by check priority, not by id.
REGIONS = [
    # Egypt proper stops at 34E so it doesn't reach across Sinai into the
    # Levant; the second box is Nubia, whose history belongs with Egypt's.
    Region(8,  "Egypt & the Nile",     31,  26, ((22, 32, 24, 34), (15, 22, 24, 38))),
    # Anatolia and the Levant are split so the box stops short of Mesopotamia:
    # a single box reaching to 45E swallows Baghdad.
    Region(5,  "Anatolia & Levant",    35,  37, ((36, 43, 25, 45), (29, 37, 33, 40))),
    Region(6,  "Mesopotamia & Persia", 49,  33, ((25, 40, 38, 63),)),
    # Two boxes to keep the Red Sea's African shore out: a single box down to
    # 12N/34E puts Aksum in Arabia.
    Region(7,  "Arabia",               45,  22, ((17, 32, 34, 60), (12, 17, 42, 60))),
    Region(16, "Japan & Korea",       134,  37, ((30, 46, 124, 146),)),
    Region(15, "East Asia",           110,  35, ((22, 54, 95, 126),)),
    Region(17, "Southeast Asia",      105,  11, ((-11, 24, 92, 141),)),
    Region(13, "South Asia",           78,  22, ((5, 37, 60, 92),)),
    Region(14, "Central Asia",         66,  43, ((35, 56, 46, 95),)),
    Region(20, "Mesoamerica",         -96,  18, ((7, 23, -118, -77),)),
    Region(21, "The Andes",           -73, -12, ((-56, 12, -82, -66),)),
    Region(22, "Amazonia & the Cone", -57, -25, ((-56, 12, -75, -34),)),
    Region(19, "North America",       -99,  40, ((23, 72, -170, -52),)),
    Region(18, "Oceania",             142, -24, ((-50, 0, 110, 180), (-30, 30, -180, -140))),
    Region(9,  "West Africa",          -2,  13, ((4, 20, -18, 16),)),
    Region(10, "East Africa",          39,   4, ((-12, 15, 28, 52),)),
    Region(11, "Central Africa",       19,  -4, ((-10, 8, 8, 32),)),
    Region(12, "Southern Africa",      26, -26, ((-35, -10, 10, 41),)),
    Region(2,  "Southern Europe",      14,  41, ((35, 45, -10, 30),)),
    Region(1,  "Western Europe",        4,  48, ((45, 55, -6, 9), (49, 59, -11, 2))),
    Region(3,  "Northern Europe",      16,  61, ((54, 72, 4, 32),)),
    Region(4,  "Eastern Europe",       31,  52, ((44, 62, 22, 50),)),
]

BY_ID = {r.id: r for r in REGIONS}


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lng2 - lng1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def assign(lat, lng) -> tuple[int | None, str]:
    """Return (region_id, confidence) where confidence is box|nearest|unassigned."""
    if lat is None or lng is None:
        return None, "unassigned"

    for r in REGIONS:
        for lat_min, lat_max, lng_min, lng_max in r.boxes:
            if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
                return r.id, "box"

    nearest, best = None, float("inf")
    for r in REGIONS:
        d = haversine_km(lat, lng, r.lat, r.lng)
        if d < best:
            nearest, best = r, d

    if best > FAR_KM:
        return None, "unassigned"
    return nearest.id, "nearest"
