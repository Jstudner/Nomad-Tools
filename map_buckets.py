#!/usr/bin/env python3
"""
map_buckets: the list of US areas you can put on a card as an offline map.

Ready made areas instead of a bounding box: all 50 states plus DC and the
territories, five big multi state regions, the whole country, and some hand
picked sets like the East Coast and Route 66. Pick as many as you like and they
join into one map.

Every area shows what it would cost on the card before anything downloads. Tile
counts are traced around each state's outline and its coastline, and the bytes
per tile and graph sizes were measured from finished builds (size notes below).

US only. The tile service is USGS, which is public domain and has no bulk
download limit but only covers the United States.

    python map_buckets.py                       # browse areas and sizes
    python map_buckets.py --list states         # every state in a table
    python map_buckets.py east-coast --zooms 6-13
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import zlib
from pathlib import Path

import nomad_common as nc

CATALOG_PATH = Path(__file__).with_name("assets") / "map-buckets.json"
COVERAGE_PATH = Path(__file__).with_name("assets") / "map-coverage.json"

# Size notes. Measured off the finished Pennsylvania region (55,298 tiles, a
# 343 MB map data file, a 404 MB road graph), with Delaware as a second point.
# Expect to be within about 15%. A denser or emptier state moves it.

# WebP quality 80 bytes per tile from the USGS topo service, measured at each
# zoom level. Zoomed out tiles are bigger than street level ones because each
# one covers a whole region's worth of detail.
TILE_BYTES = {0: 20000, 1: 20000, 2: 20000, 3: 20000, 4: 20000, 5: 20000,
              6: 19900, 7: 21900, 8: 18500, 9: 23600, 10: 19700, 11: 18400,
              12: 13000, 13: 11100, 14: 10400, 15: 10000, 16: 9800}
JPEG_FACTOR = 1.96          # skipping the WebP shrink roughly doubles the tiles

ROUTING_PER_PBF = 1.22      # road graph bytes per byte of downloaded map data
PLACES_PER_PBF_MB = 245     # named, sorted places per MB of map data
PLACES_BYTES = 46           # bytes per place in places.json
PLACES_CAP = 50_000         # how many places a region keeps

TILES_PER_SEC = 11.0        # USGS with 4 workers: 31,355 tiles in 46.4 minutes
BUILD_SEC_PER_PBF_MB = 0.36 # PA 343 MB in 124 s, DE 22 MB in 8 s
BUILD_RAM_PER_PBF = 18      # peak memory runs about 150 MB plus 18x the data
BUILD_RAM_BASE = 150 * 1024 * 1024

# The five big regions come from the same source as the states and overlap them,
# so picking one plus a state inside it must not count that state twice. These
# lists were checked against the source: each region's tile count and download
# size match the states below it to within 3%. Two aren't what the names
# suggest, so don't tidy them. "The West" includes the coast states, and
# "Alaska and Hawaii" is the whole of what the source calls Pacific.
US_SUPER = {
    "us-midwest": "il in ia ks mi mn mo ne nd oh sd wi",
    "us-northeast": "ct me ma nh nj ny pa ri vt",
    "us-pacific": "ak hi",
    "us-south": "al ar de dc fl ga ky la md ms nc ok sc tn tx va wv",
    "us-west": "az co id mt nv nm ut wy ca or wa",
}

STATE_BY_CODE = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "dc": "district-of-columbia", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana",
    "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new-hampshire", "nj": "new-jersey",
    "nm": "new-mexico", "ny": "new-york", "nc": "north-carolina",
    "nd": "north-dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon",
    "pa": "pennsylvania", "pr": "puerto-rico", "ri": "rhode-island",
    "sc": "south-carolina", "sd": "south-dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia",
    "wa": "washington", "wv": "west-virginia", "wi": "wisconsin",
    "wy": "wyoming", "vi": "us-virgin-islands",
}
CONUS = [c for c in STATE_BY_CODE if c not in ("ak", "hi", "pr", "vi")]


def _us(codes: str) -> list[str]:
    return ["us/" + STATE_BY_CODE[c] for c in codes.split()]


# Hand picked sets, for groupings the map data source doesn't offer on its own.
# Each is a list of areas and gets priced like any other pick.
GROUPS: dict[str, tuple[str, str, list[str]]] = {
    "east-coast": ("East Coast", "Maine down to Florida, every coastal state",
                   _us("me nh ma ri ct ny nj pa de md dc va nc sc ga fl")),
    "new-england": ("New England", "the six states above New York",
                    _us("me nh vt ma ri ct")),
    "mid-atlantic": ("Mid Atlantic", "New York down through Virginia",
                     _us("ny nj pa de md dc va wv")),
    "southeast": ("Southeast", "Virginia and Kentucky down to Florida",
                  _us("va nc sc ga fl al ms tn ky")),
    "gulf-coast": ("Gulf Coast", "Texas around to Florida",
                   _us("tx la ms al fl")),
    "great-lakes": ("Great Lakes", "the eight states touching the lakes",
                    _us("mn wi mi il in oh pa ny")),
    "great-plains": ("Great Plains", "the Dakotas down through Texas",
                     _us("nd sd ne ks ok tx")),
    "four-corners": ("Four Corners", "Colorado, Utah, Arizona, New Mexico",
                     _us("co ut az nm")),
    "southwest": ("Southwest", "Arizona, New Mexico, Nevada, Utah",
                  _us("az nm nv ut")),
    "rockies": ("The Rockies", "Montana and Idaho down to New Mexico",
                _us("mt id wy nv ut co az nm")),
    "pacific-northwest": ("Pacific Northwest", "Washington, Oregon, Idaho",
                          _us("wa or id")),
    "west-coast": ("West Coast", "California, Oregon, Washington",
                   _us("ca or wa")),
    "route-66": ("Route 66", "Chicago to Santa Monica, the eight states it crosses",
                 _us("il mo ks ok tx nm az ca")),
    "conus": ("Lower 48", "the whole country minus Alaska and Hawaii",
              _us(" ".join(CONUS))),
}


# the catalog

_CACHE: dict | None = None
_COV: dict | None = None


def load() -> dict:
    """Every area, keyed by id."""
    global _CACHE
    if _CACHE is None:
        if not CATALOG_PATH.is_file():
            raise SystemExit(f"The area list is missing: {CATALOG_PATH}")
        _CACHE = json.loads(CATALOG_PATH.read_text())
    return _CACHE


def load_coverage() -> dict:
    """The tile coverage maps, read only when a download actually needs them."""
    global _COV
    if _COV is None:
        _COV = json.loads(COVERAGE_PATH.read_text()) if COVERAGE_PATH.is_file() else {}
    return _COV


def get(bid: str) -> dict | None:
    return load().get(bid)


def by_kind(kind: str) -> list[dict]:
    return sorted((b for b in load().values() if b.get("kind") == kind),
                  key=lambda b: b["name"].lower())


def states() -> list[dict]:
    return by_kind("state")


def regions() -> list[dict]:
    """The big multi state areas, whole country first."""
    return [b for b in (get("us"),) if b] + by_kind("region")


def tile_count(b: dict, z: int) -> int:
    """How many tiles this area needs at one zoom level.

    Levels up to 12 were traced against the outline. Past that the outline
    is already far bigger than a tile, so each level is a clean four times the
    one below it.
    """
    t = b.get("tiles") or {}
    if not t:
        return 0
    if str(z) in t:
        return t[str(z)]
    top = max(int(k) for k in t)
    return t[str(top)] * (4 ** (z - top))


# which tiles an area needs

class Coverage:
    """The tiles a pick really needs, held as a map of the country at zoom 12.

    A bounding box is a bad fit: the one around the whole country is mostly
    Pacific Ocean, and the one around Alaska wraps the planet. So every area
    carries a traced outline trimmed to the coastline, and downloads follow
    that. The size estimate counts the same tiles, so the number shown up front
    matches what you end up with.

    The map is 2 MB of bits for the whole world at zoom 12, so it stays cheap
    however much gets picked.
    """

    Z = 12
    N = 1 << Z

    def __init__(self) -> None:
        self.bits = bytearray((self.N * self.N) // 8)

    def add(self, bid: str) -> bool:
        rec = load_coverage().get(bid)
        if not rec:
            return False
        raw = zlib.decompress(base64.b64decode(rec["bits"]))
        w, h, ox, oy = rec["w"], rec["h"], rec["x"], rec["y"]
        stride = (w + 7) // 8
        for row in range(h):
            base = (oy + row) * self.N
            chunk = raw[row * stride:(row + 1) * stride]
            for col in range(w):
                if chunk[col >> 3] & (0x80 >> (col & 7)):
                    i = base + ((ox + col) % self.N)
                    self.bits[i >> 3] |= 0x80 >> (i & 7)
        return True

    def cells(self):
        """Every tile in the map, at zoom 12, as (x, y)."""
        for i, byte in enumerate(self.bits):
            if not byte:
                continue
            for b in range(8):
                if byte & (0x80 >> b):
                    n = i * 8 + b
                    yield n % self.N, n // self.N

    def count(self) -> int:
        return sum(bin(b).count("1") for b in self.bits)

    def tiles(self, z: int):
        """The tiles to fetch at one zoom level, taken from the outline."""
        if z >= self.Z:
            k = z - self.Z
            span = 1 << k
            for x, y in self.cells():
                bx, by = x << k, y << k
                for dy in range(span):
                    for dx in range(span):
                        yield bx + dx, by + dy
        else:
            k = self.Z - z
            seen = set()
            for x, y in self.cells():
                seen.add((x >> k, y >> k))
            yield from sorted(seen)


def coverage_for(ids: list[str]) -> Coverage:
    cov = Coverage()
    for i in ids:
        cov.add(i)
    return cov


# working out what was picked

def covers(bid: str) -> set[str]:
    """Areas swallowed by this one, so an overlapping pick is not counted twice."""
    out: set[str] = set()
    if bid == "us":
        out |= {b["id"] for b in states()} | set(US_SUPER)
    if bid in US_SUPER:
        out |= set(_us(US_SUPER[bid]))
    return out


def dedupe(ids: list[str]) -> tuple[list[str], list[str]]:
    """Drop anything already inside a bigger pick. Returns (kept, dropped)."""
    swallowed: set[str] = set()
    for i in ids:
        swallowed |= covers(i)
    kept, dropped, seen = [], [], set()
    for i in ids:
        if i in swallowed or i in seen:
            dropped.append(i)
        else:
            kept.append(i)
            seen.add(i)
    return kept, dropped


def resolve(tokens) -> tuple[list[str], list[str]]:
    """Turn typed words into areas. Returns (areas, anything not recognised)."""
    cat = load()
    ids, unknown = [], []
    for raw in tokens:
        t = raw.strip().lower()
        if not t:
            continue
        if t in GROUPS:
            ids.extend(GROUPS[t][2])
        elif t in cat:
            ids.append(t)
        elif t in STATE_BY_CODE and "us/" + STATE_BY_CODE[t] in cat:
            ids.append("us/" + STATE_BY_CODE[t])
        elif "us/" + t in cat:
            ids.append("us/" + t)
        else:
            hits = [b["id"] for b in cat.values() if b["name"].lower() == t]
            if len(hits) == 1:
                ids.append(hits[0])
            else:
                unknown.append(raw)
    return ids, unknown


def search(term: str, limit: int = 20) -> list[dict]:
    t = term.strip().lower()
    if not t:
        return []
    cat = load()
    exact = [b for b in cat.values() if b["name"].lower() == t]
    starts = [b for b in cat.values() if b["name"].lower().startswith(t) and b not in exact]
    inside = [b for b in cat.values()
              if t in b["name"].lower() and b not in exact and b not in starts]
    return (exact + starts + inside)[:limit]


def merged_bbox(ids: list[str]) -> tuple[float, float, float, float]:
    w, s, e, n = 180.0, 90.0, -180.0, -90.0
    for i in ids:
        b = get(i)
        if not b:
            continue
        bb = b["bbox"]
        w, s = min(w, bb[0]), min(s, bb[1])
        e, n = max(e, bb[2]), max(n, bb[3])
    return (w, s, e, n)


# the size estimate

def estimate(ids: list[str], z_min: int = 6, z_max: int = 14,
             webp: bool = True) -> dict:
    """Price a pick: tiles, road graph, places, time and memory to build.

    Two neighbouring states share a border, so the tiles along it get counted
    for both. That runs a percent or two high, which is the safe side to be on.
    """
    ids, _ = dedupe(ids)
    tiles = tile_bytes = pbf_bytes = 0
    per_zoom: dict[int, int] = {}
    for i in ids:
        b = get(i)
        if not b:
            continue
        pbf_bytes += b.get("pbfBytes") or 0
        for z in range(z_min, z_max + 1):
            n = tile_count(b, z)
            per_zoom[z] = per_zoom.get(z, 0) + n
            tiles += n
            tile_bytes += n * TILE_BYTES.get(z, TILE_BYTES[16])
    if not webp:
        tile_bytes = int(tile_bytes * JPEG_FACTOR)

    pbf_mb = pbf_bytes / (1024 * 1024)
    places = min(PLACES_CAP, int(pbf_mb * PLACES_PER_PBF_MB))
    return {
        "ids": ids,
        "areas": len(ids),
        "bbox": merged_bbox(ids),
        "zooms": (z_min, z_max),
        "tiles": tiles,
        "perZoom": per_zoom,
        "tileBytes": tile_bytes,
        "pbfBytes": pbf_bytes,
        "routingBytes": int(pbf_bytes * ROUTING_PER_PBF),
        "places": places,
        "placesBytes": places * PLACES_BYTES,
        "totalBytes": tile_bytes + int(pbf_bytes * ROUTING_PER_PBF) + places * PLACES_BYTES,
        "tileHours": tiles / TILES_PER_SEC / 3600.0,
        "buildMinutes": pbf_mb * BUILD_SEC_PER_PBF_MB / 60.0,
        "buildRam": BUILD_RAM_BASE + pbf_bytes * BUILD_RAM_PER_PBF,
    }


def _hours(h: float) -> str:
    if h * 60 < 1:
        return "under a minute"
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.1f} hours"
    return f"{h / 24:.0f} days"


def format_estimate(est: dict, indent: str = "  ") -> str:
    z0, z1 = est["zooms"]
    return "\n".join([
        f"{indent}{est['areas']} area(s), detail level {z0} to {z1}",
        f"{indent}{est['tiles']:,} tiles, {nc.human(est['tileBytes'])} of map picture",
        f"{indent}{nc.human(est['routingBytes'])} of road data, "
        f"{est['places']:,} searchable places ({nc.human(est['placesBytes'])})",
        f"{indent}{nc.c('1', 'about ' + nc.human(est['totalBytes']) + ' on the card')}",
        f"{indent}{nc.c('2', 'the tiles take about ' + _hours(est['tileHours']) + ' to download, ')}"
        + nc.c("2", f"plus {nc.human(est['pbfBytes'])} of map data "
                    f"and about {est['buildMinutes']:.0f} min to build"),
        f"{indent}{nc.c('2', 'this computer needs about ' + nc.human(est['buildRam']) + ' of free memory while it builds')}",
    ])


def warnings(est: dict, free: int | None = None) -> list[str]:
    """Warnings to show before a long download starts."""
    out = []
    if est["buildRam"] > 48 * 1024 ** 3:
        out.append(f"Building this in one go needs about {nc.human(est['buildRam'])} of "
                   "memory, which is more than most computers have. Do it as two or "
                   "three smaller picks instead, each its own map on the card.")
    elif est["buildRam"] > 16 * 1024 ** 3:
        out.append(f"Building this needs about {nc.human(est['buildRam'])} of memory. "
                   "Close your other programs first.")
    if est["tileHours"] > 24:
        out.append(f"The map picture alone is about {_hours(est['tileHours'])} of "
                   "downloading. Dropping the detail level by one cuts that to roughly "
                   "a quarter.")
    if free is not None and est["totalBytes"] > free:
        out.append(f"This needs {nc.human(est['totalBytes'])} but the card only has "
                   f"{nc.human(free)} free.")
    return out


# browsing

def _header() -> None:
    print(nc.c("2", f"       {'area':<24}{'tiles':>11}{'picture':>11}"
                    f"{'roads':>11}{'total':>11}"))


def _row(n: int, name: str, est: dict, note: str = "") -> None:
    # the colour codes are invisible but still count toward the field width, so
    # the last column is padded by however many characters they add
    total = nc.human(est["totalBytes"])
    pad = 11 + len(nc.c("1", ""))
    print(f"  {n:>3}. {name[:24]:<24}{est['tiles']:>11,}"
          f"{nc.human(est['tileBytes']):>11}{nc.human(est['routingBytes']):>11}"
          f"{nc.c('1', total):>{pad}}")
    if note:
        print(nc.c("2", f"       {note}"))


def print_areas(areas: list[dict], z_min: int, z_max: int) -> None:
    _header()
    for i, b in enumerate(areas, 1):
        _row(i, b["name"], estimate([b["id"]], z_min, z_max))


def print_groups(z_min: int, z_max: int) -> list[str]:
    keys = list(GROUPS)
    _header()
    for i, k in enumerate(keys, 1):
        name, note, members = GROUPS[k]
        _row(i, name, estimate(members, z_min, z_max), note)
    return keys


def _parse_picks(raw: str, n: int) -> list[int] | None:
    """Accepts '1 4 7', '1,4,7', '2-9' and 'all'."""
    raw = raw.strip().lower()
    if raw == "all":
        return list(range(1, n + 1))
    out: list[int] = []
    for part in raw.replace(",", " ").split():
        if "-" in part and not part.startswith("-"):
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a), int(b) + 1))
                continue
            return None
        if not part.isdigit():
            return None
        out.append(int(part))
    return [i for i in out if 1 <= i <= n] or None


def _add_from(areas: list[dict], title: str, blurb: str, sel: list[str],
              z_min: int, z_max: int) -> None:
    """Show a table and fold whatever gets picked into sel."""
    while True:
        print(nc.c("1", f"\n{title}") + nc.c("2", f"   detail level {z_min} to {z_max}"))
        print(nc.c("2", f"  {blurb}"))
        print_areas(areas, z_min, z_max)
        print(nc.c("2", "\n  picture = the map you look at, roads = what gives directions"))
        print(nc.c("2", "  Type the numbers you want (1 4 7, or 2-9, or all), or b to go back"))
        raw = input("Add: ").strip()
        if not raw or raw.lower() in ("b", "back"):
            return
        picks = _parse_picks(raw, len(areas))
        if not picks:
            print(nc.c("1;31", "  Didn't catch that. Try 1 4 7, or 2-9, or all."))
            continue
        for i in picks:
            bid = areas[i - 1]["id"]
            if bid not in sel:
                sel.append(bid)
        return


TOP_LEVELS = [
    (11, "highways and towns, no street names"),
    (12, "towns and main roads, good for driving between them"),
    (13, "most streets"),
    (14, "every street"),
    (15, "rarely worth it, since you can already zoom in past what you downloaded"),
]
RECOMMENDED_TOP = 14


def ask_zooms(sel: list[str] | None = None, z_min: int = 6,
              z_max: int = 14) -> tuple[int, int]:
    """Ask for the detail level, priced against whatever is picked."""
    shown = [i for i in (sel or []) if get(i)]
    ref = shown or ["us/pennsylvania"]
    label = (", ".join(get(i)["name"] for i in shown[:3]) +
             (f" and {len(shown) - 3} more" if len(shown) > 3 else "")) if shown \
        else "Pennsylvania, for reference"

    print(nc.c("1", "\nDetail level"))
    print(nc.c("2", "  Level 6 fits a whole state on one screen. Level 12 shows towns and"))
    print(nc.c("2", "  main roads, level 14 shows every street. Each level past 12 is about"))
    print(nc.c("2", "  four times as many tiles as the one below it, so this decides most"))
    print(nc.c("2", "  of the map's size."))
    print(nc.c("2", "  You can still zoom in past the last level on Nomad. The picture just"))
    print(nc.c("2", "  gets softer. Directions aren't affected because the road data is kept"))
    print(nc.c("2", "  separate from the picture."))

    print(nc.c("1", f"\n  {label}:"))
    for top, note in TOP_LEVELS:
        e = estimate(ref, z_min, top)
        mark = nc.c("1;32", "  (recommended)") if top == RECOMMENDED_TOP else ""
        print(f"    {z_min}-{top}  {nc.human(e['totalBytes']):>9}  "
              f"{_hours(e['tileHours']):>14} of tiles   " + nc.c("2", note) + mark)
    print(nc.c("2", "\n  Type any range you like, such as 8-13 to skip the far out levels."))

    while True:
        raw = input(f"\nLevels [{z_min}-{z_max}]: ").strip()
        if not raw:
            return z_min, z_max
        try:
            lo, hi = (int(p) for p in raw.split("-")) if "-" in raw \
                else (int(raw), int(raw))
        except ValueError:
            print(nc.c("1;31", "  Type it like 6-14."))
            continue
        if not (0 <= lo <= hi <= 16):
            print(nc.c("1;31", "  Levels go from 0 to 16, and the first number can't be bigger than the second."))
            continue
        if hi > 15:
            print(nc.c("1;33", "  Heads up: past 15 the tile count gets huge, and Nomad can already"))
            print(nc.c("1;33", "  zoom in further than what you downloaded."))
        return lo, hi


def pick(z_min: int = 6, z_max: int = 14) -> tuple[list[str], tuple[int, int]] | None:
    """Browse the areas and build up a pick. Returns (areas, levels)."""
    sel: list[str] = []
    chose_levels = False
    while True:
        nc.banner("Map areas and sizes")
        print("Choose the parts of the country you want on the card. Pick as many as")
        print("you like and they join into one map.\n")
        print("  1. States and territories   " + nc.c("2", "all 50, plus DC, Puerto Rico, Virgin Islands"))
        print("  2. Big regions              " + nc.c("2", "the Northeast, the South, the whole country"))
        print("  3. Ready made sets          " + nc.c("2", "East Coast, New England, Route 66, Lower 48"))
        print("  4. Find one by name")
        print("  z. Detail level             " + nc.c("2", f"currently {z_min} to {z_max}"))

        if sel:
            est = estimate(sel, z_min, z_max)
            print(nc.c("1", f"\n  Picked so far ({len(est['ids'])}): ") +
                  ", ".join(get(i)["name"] for i in est["ids"][:8]) +
                  (f" and {len(est['ids']) - 8} more" if len(est["ids"]) > 8 else ""))
            print(format_estimate(est, "    "))
            for w in warnings(est):
                print(nc.c("1;33", f"    ! {w}"))
            print("\n  c. Start over")
            print(nc.c("1", "  d. Done, use this"))
        print("  0. Cancel")

        choice = input("\nChoose: ").strip().lower()
        if choice in ("0", "q"):
            return None
        if choice == "d" and sel:
            ids, _ = dedupe(sel)
            # last chance to set the level, now that it can be priced properly
            if not chose_levels:
                z_min, z_max = ask_zooms(ids, z_min, z_max)
            return ids, (z_min, z_max)
        if choice == "c":
            sel = []
            continue
        if choice == "z":
            z_min, z_max = ask_zooms(sel, z_min, z_max)
            chose_levels = True
            continue
        if choice == "1":
            _add_from(states(), "States and territories",
                      "one state is a comfortable size for a card",
                      sel, z_min, z_max)
            continue
        if choice == "2":
            _add_from(regions(), "Big regions",
                      "these come as one download each, so they build faster than "
                      "the same states picked one by one",
                      sel, z_min, z_max)
            continue
        if choice == "3":
            while True:
                print(nc.c("1", "\nReady made sets") +
                      nc.c("2", f"   detail level {z_min} to {z_max}"))
                keys = print_groups(z_min, z_max)
                print(nc.c("2", "\n  type the numbers you want, or b to go back"))
                raw = input("Add: ").strip()
                if not raw or raw.lower() in ("b", "back"):
                    break
                picks = _parse_picks(raw, len(keys))
                if not picks:
                    print(nc.c("1;31", "  Didn't catch that. Try 1 4 7, or 2-9, or all."))
                    continue
                for i in picks:
                    for m in GROUPS[keys[i - 1]][2]:
                        if m not in sel:
                            sel.append(m)
                break
            continue
        if choice == "4":
            hits = search(input("  Name: ").strip())
            if not hits:
                print(nc.c("2", "  Nothing matched that."))
                continue
            _add_from(hits, "Matches", "", sel, z_min, z_max)
            continue
        print("  Type 1, 2, 3, 4, z, c, d or 0.")


# fetching the map data

def default_work_dir() -> Path:
    """Where downloaded map data is kept. It sits next to the tools so a copy of
    this folder on a USB stick carries its own data with it."""
    return Path(__file__).with_name("map-data")


def fetch_pbf(bid: str, work_dir: Path | str | None = None, log=print,
              progress=None) -> Path:
    """Download one area's map data, picking up a part finished file if there is one."""
    import urllib.request

    b = get(bid)
    if not b or not b.get("pbf"):
        raise ValueError(f"no map data for {bid}")
    work = Path(work_dir or default_work_dir())
    work.mkdir(parents=True, exist_ok=True)
    dest = work / (bid.replace("/", "-") + ".osm.pbf")
    want = b.get("pbfBytes") or 0
    have = dest.stat().st_size if dest.is_file() else 0
    if want and have == want:
        log(f"  {b['name']}: already downloaded ({nc.human(have)})")
        return dest

    headers = {"User-Agent": "nomad-card-tools/1.0 (offline media server)"}
    mode = "wb"
    if have and want and have < want:
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
        log(f"  {b['name']}: carrying on from {nc.human(have)} of {nc.human(want)}")
    else:
        have = 0
        log(f"  {b['name']}: downloading {nc.human(want)}")

    req = urllib.request.Request(b["pbf"], headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, mode) as f:
        if r.status == 200 and mode == "ab":       # server ignored the resume
            f.close()
            dest.unlink(missing_ok=True)
            return fetch_pbf(bid, work, log, progress)
        got = have
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and want:
                progress.pct(100.0 * got / want,
                             f"{b['name']}  {nc.human(got)}/{nc.human(want)}")
    return dest


def fetch_all(ids: list[str], work_dir=None, log=print, progress=None) -> list[Path]:
    return [fetch_pbf(i, work_dir, log, progress) for i in ids]


# menu entry

def run(dest: str | None = None) -> int:
    """Browse and price areas. Downloads nothing at all."""
    picked = pick()
    if not picked:
        print("Cancelled.")
        return 1
    ids, zooms = picked
    est = estimate(ids, *zooms)
    print(nc.c("1", "\n" + ", ".join(get(i)["name"] for i in est["ids"])))
    print(format_estimate(est))
    for w in warnings(est):
        print(nc.c("1;33", f"  ! {w}"))
    print(nc.c("2", "\n  Nothing was downloaded. To build this map, run:"))
    print(nc.c("2", f"    ./nomad_route.py --areas {' '.join(est['ids'])} "
                    f"--zooms {zooms[0]}-{zooms[1]}"))
    return 0


# command line

def main() -> int:
    ap = argparse.ArgumentParser(
        description="See how much space US map areas would take on a card. Doesn't download anything.")
    ap.add_argument("areas", nargs="*",
                    help="state codes, area ids or set names, like pa nj de or east-coast")
    ap.add_argument("--list", dest="listing",
                    help="print a table: states, regions or sets")
    ap.add_argument("--zooms", default="6-14", help="detail levels, min-max (default 6-14)")
    ap.add_argument("--json", action="store_true", help="print the estimate as JSON")
    args = ap.parse_args()

    try:
        z_min, z_max = (int(p) for p in args.zooms.split("-"))
    except ValueError:
        ap.error("--zooms should look like 6-14")

    if args.listing:
        key = args.listing.lower()
        if key in ("states", "state"):
            print_areas(states(), z_min, z_max)
        elif key in ("regions", "region"):
            print_areas(regions(), z_min, z_max)
        elif key in ("sets", "set", "groups", "bundles"):
            print_groups(z_min, z_max)
        else:
            print("Use one of these: states, regions, sets")
            return 1
        return 0

    if not args.areas:
        picked = pick(z_min, z_max)
        if not picked:
            print("Cancelled.")
            return 1
        ids, (z_min, z_max) = picked
    else:
        ids, unknown = resolve(args.areas)
        if unknown:
            print(nc.c("1;31", "Don't know these areas: " + ", ".join(unknown)))
            return 1

    est = estimate(ids, z_min, z_max)
    if args.json:
        print(json.dumps(est, indent=2))
        return 0
    print(nc.c("1", "\n" + ", ".join(get(i)["name"] for i in est["ids"])))
    print(format_estimate(est))
    for w in warnings(est):
        print(nc.c("1;33", f"  ! {w}"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
