#!/usr/bin/env python3
"""
map_sizes: how much space a map pack will take on a card, before anything downloads.

  map        exact for the Protomaps build of 2026-10-26, from a table that covers the
             whole planet in squares of about 50 km (assets/map-sizes.bin.z). Newer
             builds come out a few percent different.
  the rest   worked out from the size of the area's OpenStreetMap download, using
             ratios measured on real builds (Washington and Florida, 2026-10-27)

Other tools use it like this:
    import map_sizes
    map_sizes.blocks_bytes(blocks, maxzoom=14)       blocks: [(x, y)] on the zoom 9 grid
    map_sizes.parts(extract_bytes)                   {"directions": n, "search": n, ...}

To make the table again: map_sizes.py --table PMTILES_URL (it only reads the build's
index, about 0.35 GB, and takes about an hour).
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

TABLE = Path(__file__).resolve().parent / "assets" / "map-sizes.bin.z"
# bytes of each part per byte of OpenStreetMap extract (Washington 365 MB, Florida 660 MB)
RATIO = {"directions": 0.18, "search": 0.055, "addresses": 0.058, "essentials": 0.002, "trails": 0.0015}
WORLD = 45_101_942          # zoom 0-6, the whole world (built 2026-10-27)

_t = None


def _load():
    global _t
    if _t is None:
        blocks, mid, low = {}, {}, WORLD
        if TABLE.exists():
            raw = zlib.decompress(TABLE.read_bytes())
            if raw[:4] == b"NMS1":
                nb, nm, low = struct.unpack_from("<IIQ", raw, 4)
                o = 20
                for _ in range(nb):
                    x, y, *v = struct.unpack_from("<HH5I", raw, o)
                    blocks[(x, y)] = v
                    o += 24
                for _ in range(nm):
                    x, y, *v = struct.unpack_from("<HH3I", raw, o)
                    mid[(x, y)] = v
                    o += 16
        _t = (blocks, mid, low)
    return _t


def have_table() -> bool:
    return bool(_load()[0])


def blocks_bytes(blocks, maxzoom: int = 14, minzoom: int = 10) -> int:
    """Zoom minzoom..maxzoom of these zoom-9 blocks."""
    table = _load()[0]
    return sum(sum(table.get(tuple(b), (0,) * 5)[minzoom - 10:maxzoom - 9]) for b in blocks)


def overview_bytes(blocks, maxzoom: int = 9) -> int:
    """Zoom 7..maxzoom over these blocks only (what an own build cuts), as the blocks' share
    of each zoom-5 square the table has: at zoom 9 a square holds 256 block-sized tiles, at
    zoom 8 64, at zoom 7 16."""
    mid = _load()[1]
    per: dict = {}
    for x, y in blocks:
        per.setdefault((x >> 4, y >> 4), set()).add((x, y))
    total = 0.0
    for sq, bs in per.items():
        sizes = mid.get(sq, (0, 0, 0))
        for i, z in enumerate(range(7, maxzoom + 1)):
            k = 9 - z
            total += sizes[i] * len({(x >> k, y >> k) for x, y in bs}) / (4 ** (z - 5))
    return int(total)


def world_bytes() -> int:
    return _load()[2]


def parts(extract_bytes: int) -> dict:
    return {k: int(extract_bytes * r) for k, r in RATIO.items()}


def make_table(url: str, out: Path = TABLE) -> None:
    """The table, from a Protomaps build's directories (each tile counted once: open sea
    is one shared tile, so sea squares come out near nothing)."""
    import pmtiles_extract as px
    from pmtiles.tile import deserialize_directory, deserialize_header, tileid_to_zxy
    src = px.Source(url)
    h = deserialize_header(src.get(0, 127))
    root = deserialize_directory(src.get(h["root_offset"], h["root_length"]))
    leaves = src.get(h["leaf_directory_offset"], h["leaf_directory_length"])
    first = lambda z: ((1 << (2 * z)) - 1) // 3
    lo, hi = first(7), first(15)
    seen, blocks, mid, low = set(), {}, {}, [0]

    def take(e):
        if e.offset in seen:
            return
        seen.add(e.offset)
        if e.tile_id < lo:
            low[0] += e.length
            return
        if e.tile_id >= hi:
            return
        z, x, y = tileid_to_zxy(e.tile_id)
        if z >= 10:
            blocks.setdefault((x >> (z - 9), y >> (z - 9)), [0] * 5)[z - 10] += e.length
        else:
            mid.setdefault((x >> (z - 5), y >> (z - 5)), [0] * 3)[z - 7] += e.length

    for e in root:
        if e.run_length:
            take(e)
            continue
        for le in deserialize_directory(leaves[e.offset:e.offset + e.length]):
            if le.tile_id >= hi:
                break
            if le.run_length:
                take(le)
    raw = bytearray(b"NMS1") + struct.pack("<IIQ", len(blocks), len(mid), low[0])
    for (x, y), v in sorted(blocks.items()):
        raw += struct.pack("<HH5I", x, y, *v)
    for (x, y), v in sorted(mid.items()):
        raw += struct.pack("<HH3I", x, y, *v)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(zlib.compress(bytes(raw), 9))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--table":
        make_table(sys.argv[2])
        print(f"Wrote {TABLE}")
    else:
        print(__doc__)
