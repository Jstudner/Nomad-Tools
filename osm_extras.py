#!/usr/bin/env python3
"""
osm_extras: the extra parts of an area pack, beside its roads and place search, built
from the same Geofabrik extract:

  essentials/   the nearest of what matters when things go wrong: hospitals, clinics,
                pharmacies, water, shelter, fuel, food stores, police, fire stations,
                toilets, ranger stations, campsites, schools, helipads, boat ramps, and
                peaks and passes with their heights. Named or not, since most water taps
                and shelters have no name and place search never had them. Grouped by
                squares about 10 km across (index.json: square -> byte range), so
                "nearby" only reads a few KB.
  addresses/    house numbers, for "1234 Main Street". streets.tsv lists each street (one
                line per word of its name, sorted, sliced by word start like place search)
                with where its numbers are in numbers.bin: a count, then each number and
                its position, packed.
  trails/       named hiking and foot routes (OSM route relations like the Pacific Crest
                Trail or a park's loop), simplified to 5 m and grouped by squares about
                25 km across, so the map only draws what's in view.

Measured on Washington (2026-10-27): 23,070 essentials, 1.62 million addresses on 41,310
streets, 1,251 routes over 10,338 ways.

    python osm_extras.py --pbf washington-latest.osm.pbf --out wa
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import unicodedata
from pathlib import Path

import osmium

SCALE = 1e5
SLICE = 48 * 1024

# essential: (tag key, value) -> category shown in the Nearby list, in this order
ESSENTIALS = [
    ("hospital", "Hospitals", [("amenity", "hospital"), ("healthcare", "hospital")]),
    ("clinic", "Clinics and doctors", [("amenity", "clinic"), ("amenity", "doctors"), ("healthcare", "clinic"),
                                       ("healthcare", "doctor")]),
    ("pharmacy", "Pharmacies", [("amenity", "pharmacy"), ("healthcare", "pharmacy"), ("shop", "chemist")]),
    ("water", "Water", [("amenity", "drinking_water"), ("amenity", "water_point"), ("natural", "spring"),
                        ("man_made", "water_well"), ("man_made", "water_tap")]),
    ("shelter", "Shelter", [("amenity", "shelter"), ("tourism", "alpine_hut"), ("tourism", "wilderness_hut"),
                            ("amenity", "emergency_shelter"), ("emergency", "assembly_point")]),
    ("food", "Food stores", [("shop", "supermarket"), ("shop", "convenience"), ("shop", "general")]),
    ("fuel", "Fuel", [("amenity", "fuel")]),
    ("police", "Police", [("amenity", "police")]),
    ("fire", "Fire stations", [("amenity", "fire_station")]),
    ("ranger", "Ranger stations", [("amenity", "ranger_station")]),
    ("aed", "Defibrillators", [("emergency", "defibrillator")]),
    ("phone", "Emergency phones", [("emergency", "phone")]),
    ("camp", "Campsites", [("tourism", "camp_site"), ("tourism", "caravan_site")]),
    ("school", "Schools (common shelters)", [("amenity", "school")]),
    ("helipad", "Helipads", [("aeroway", "helipad"), ("emergency", "landing_site")]),
    ("boat", "Boat ramps", [("leisure", "slipway")]),
    ("toilets", "Toilets", [("amenity", "toilets")]),
    ("peak", "Peaks", [("natural", "peak"), ("natural", "volcano")]),
    ("pass", "Passes and saddles", [("mountain_pass", "yes"), ("natural", "saddle")]),
]
ESS_OF = {kv: i for i, (_, _, kvs) in enumerate(ESSENTIALS) for kv in kvs}
ESS_KEYS = sorted({k for _, _, kvs in ESSENTIALS for k, _ in kvs})
NEAR_CELL = 0.1          # degrees: ~10 km squares for essentials
TRAIL_CELL = 0.25        # ~25 km squares for trails
STREET_CELL = 0.2        # same-named streets this far apart are different streets
STOP = {"the", "of", "and", "de", "la", "el", "st", "street", "ave", "avenue", "rd", "road", "dr", "drive",
        "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west", "way", "ln", "lane",
        "blvd", "boulevard", "ct", "court", "pl", "place"}


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _varint(buf: bytearray, v: int) -> None:
    while v >= 0x80:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v)


def _zz(v: int) -> int:
    return v * 2 if v >= 0 else -v * 2 - 1


def _cell(lat: float, lon: float, size: float) -> str:
    return f"{math.floor(lat / size)}_{math.floor(lon / size)}"


def _simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker on (lat, lon) e5 ints; tol in e5 units."""
    if len(pts) < 3:
        return pts
    kx = math.cos(math.radians(pts[0][0] / SCALE))
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (y1, x1), (y2, x2) = pts[i], pts[j]
        x1, x2 = x1 * kx, x2 * kx
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1.0
        far, fi = -1.0, -1
        for k in range(i + 1, j):
            d = abs(dy * (pts[k][1] * kx - x1) - dx * (pts[k][0] - y1)) / L
            if d > far:
                far, fi = d, k
        if far > tol:
            keep[fi] = True
            stack += [(i, fi), (fi, j)]
    return [p for p, k in zip(pts, keep) if k]


def _word_slices(words: list[str], offs: list[int]) -> dict:
    """word start -> [offset, length] of its lines, two letters first and longer where two
    would be a big read (the same rule as place search)."""
    out = {}

    def split(lo, hi, n):
        total = offs[hi] - offs[lo]
        if total <= SLICE or all(len(w) <= n for w in words[lo:hi]) or n >= 8:
            out[words[lo][:n]] = [offs[lo], total]
            return
        i = lo
        while i < hi:
            key, j = words[i][:n + 1], i
            while j < hi and words[j][:n + 1] == key:
                j += 1
            split(i, j, n + 1)
            i = j

    i = 0
    while i < len(words):
        key, j = words[i][:2], i
        while j < len(words) and words[j][:2] == key:
            j += 1
        split(i, j, 2)
        i = j
    return out


def read(pbfs, log=print) -> dict:
    t0 = time.time()
    routes: dict[int, tuple[str, str]] = {}           # way id -> (route name, kind)
    for path in pbfs:
        for r in osmium.FileProcessor(str(path), osmium.osm.RELATION):
            t = r.tags
            if t.get("type") == "route" and t.get("route") in ("hiking", "foot"):
                nm = t.get("name") or t.get("ref") or ""
                for m in r.members:
                    if m.type == "w" and (m.ref not in routes or (nm and not routes[m.ref][0])):
                        routes[m.ref] = (nm, t.get("route"))
    log(f"  extras: {len(routes):,} trail ways in {time.time() - t0:.0f}s")

    ess, streets, trails = [], {}, []
    for path in pbfs:
        fp = osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY).with_locations()
        for o in fp:
            t = o.tags
            is_way = o.is_way()
            if is_way and o.id in routes:
                pts = [(round(n.lat * SCALE), round(n.lon * SCALE)) for n in o.nodes if n.location.valid()]
                if len(pts) >= 2:
                    trails.append((routes[o.id], pts))
            if not len(t):
                continue
            hn = t.get("addr:housenumber")
            cat = None
            for k in ESS_KEYS:
                v = t.get(k)
                if v and (k, v) in ESS_OF:
                    cat = ESS_OF[(k, v)]
                    break
            if hn is None and cat is None:
                continue
            if is_way:
                locs = [n for n in o.nodes if n.location.valid()]
                if not locs:
                    continue
                lat, lon = sum(n.lat for n in locs) / len(locs), sum(n.lon for n in locs) / len(locs)
            else:
                if not o.location.valid():
                    continue
                lat, lon = o.location.lat, o.location.lon
            if cat is not None:
                ele = t.get("ele") or ""
                try:
                    ele = str(round(float(ele.split()[0].replace(",", ".")))) if ele else ""
                except ValueError:
                    ele = ""
                ess.append((cat, t.get("name") or "", lat, lon, ele))
            st = t.get("addr:street") or t.get("addr:place")
            if hn and st:
                key = (st, t.get("addr:city") or "", _cell(lat, lon, STREET_CELL))
                streets.setdefault(key, []).append((hn[:16], round(lat * SCALE), round(lon * SCALE)))
    n_addr = sum(len(v) for v in streets.values())
    log(f"  extras: {len(ess):,} essentials, {n_addr:,} addresses on {len(streets):,} streets, "
        f"{len(trails):,} trail ways ({time.time() - t0:.0f}s)")
    return {"ess": ess, "streets": streets, "trails": trails}


PARTS = ("essentials", "addresses", "trails")


def write(x: dict, out: Path, log=print, parts=PARTS) -> dict:
    sizes = {}
    if "essentials" in parts:
        sizes["essentials"] = _write_essentials(x, out)
    if "addresses" in parts:
        sizes["addresses"] = _write_addresses(x, out)
    if "trails" in parts:
        sizes["trails"] = _write_trails(x, out)
    log("  extras written: " + ", ".join(f"{k} {v / 1e6:.1f} MB" for k, v in sizes.items()))
    return sizes


def _write_essentials(x: dict, out: Path) -> int:
    d = out / "essentials"
    d.mkdir(parents=True, exist_ok=True)
    rows = sorted(((_cell(lat, lon, NEAR_CELL), cat, name.replace("\t", " ").replace("\n", " "), lat, lon, ele)
                   for cat, name, lat, lon, ele in x["ess"]), key=lambda r: (r[0], r[1]))
    data, cells = bytearray(), {}
    for cell, cat, name, lat, lon, ele in rows:
        line = f"{cat}\t{name}\t{lat:.5f}\t{lon:.5f}" + (f"\t{ele}" if ele else "") + "\n"
        line = line.encode("utf-8")
        c = cells.setdefault(cell, [len(data), 0])
        c[1] += len(line)
        data += line
    (d / "near.tsv").write_bytes(bytes(data))
    (d / "index.json").write_text(json.dumps({"cell": NEAR_CELL, "cats": [[k, label] for k, label, _ in ESSENTIALS],
                                              "cells": cells}, separators=(",", ":")), encoding="utf-8")
    return len(data)


def _write_addresses(x: dict, out: Path) -> int:
    """House numbers per street, streets by the words of their names."""
    d = out / "addresses"
    d.mkdir(parents=True, exist_ok=True)
    nums, lines = bytearray(), []
    for (st, city, _), items in streets_sorted(x["streets"]):
        items.sort(key=lambda h: (_num_key(h[0]), h[0]))
        clat = sum(i[1] for i in items) // len(items)
        clon = sum(i[2] for i in items) // len(items)
        off = len(nums)
        _varint(nums, len(items))
        for hn, la, lo in items:
            b = hn.encode("utf-8")
            _varint(nums, len(b))
            nums += b
            _varint(nums, _zz(la - clat))
            _varint(nums, _zz(lo - clon))
        ln = len(nums) - off
        words = {w for w in "".join(ch if ch.isalnum() else " " for ch in fold(st)).split() if len(w) >= 2 and w not in STOP}
        if not words:                                       # "North Street": keep its main word anyway
            words = {w for w in "".join(ch if ch.isalnum() else " " for ch in fold(st)).split() if len(w) >= 2}
        for w in words:
            lines.append((w, st, city, clat, clon, off, ln, len(items)))
    lines.sort(key=lambda r: (r[0], r[1]))
    enc = [f"{w}\t{st}\t{city}\t{la / SCALE:.5f}\t{lo / SCALE:.5f}\t{off}\t{ln}\t{n}\n".encode("utf-8")
           for w, st, city, la, lo, off, ln, n in lines]
    offs = [0]
    for e in enc:
        offs.append(offs[-1] + len(e))
    (d / "streets.tsv").write_bytes(b"".join(enc))
    (d / "numbers.bin").write_bytes(bytes(nums))
    (d / "index.json").write_text(json.dumps({"slice": SLICE, "count": sum(len(v) for v in x["streets"].values()),
                                              "streets": len(x["streets"]),
                                              "prefixes": _word_slices([r[0] for r in lines], offs)},
                                             separators=(",", ":")), encoding="utf-8")
    return offs[-1] + len(nums)


def _write_trails(x: dict, out: Path) -> int:
    """Shapes by square, names shared."""
    d = out / "trails"
    d.mkdir(parents=True, exist_ok=True)
    names, name_ix, by_cell = [], {}, {}
    for (nm, kind), pts in x["trails"]:
        pts = _simplify(pts, 5 / 1.11)
        if nm not in name_ix:
            name_ix[nm] = len(names)
            names.append(nm)
        by_cell.setdefault(_cell(pts[0][0] / SCALE, pts[0][1] / SCALE, TRAIL_CELL), []).append((name_ix[nm], kind, pts))
    data, cells = bytearray(), {}
    for cell in sorted(by_cell):
        blob = bytearray()
        _varint(blob, len(by_cell[cell]))
        for ni, kind, pts in by_cell[cell]:
            _varint(blob, ni)
            blob.append(1 if kind == "hiking" else 2)
            _varint(blob, len(pts))
            pla = plo = 0
            for la, lo in pts:
                _varint(blob, _zz(la - pla))
                _varint(blob, _zz(lo - plo))
                pla, plo = la, lo
        cells[cell] = [len(data), len(blob)]
        data += blob
    (d / "trails.bin").write_bytes(bytes(data))
    (d / "index.json").write_text(json.dumps({"cell": TRAIL_CELL, "names": names, "cells": cells},
                                             separators=(",", ":")), encoding="utf-8")
    return len(data)


def streets_sorted(streets: dict):
    return sorted(streets.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))


def _num_key(hn: str) -> int:
    digits = ""
    for ch in hn:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


def build(pbfs, out: Path, log=print, parts=PARTS) -> dict:
    return write(read([str(p) for p in pbfs], log), out, log, parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True, nargs="+", help="one or more .osm.pbf extracts")
    ap.add_argument("--out", required=True, help="folder to write into")
    a = ap.parse_args()
    print(json.dumps(build(a.pbf, Path(a.out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
