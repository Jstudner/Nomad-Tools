#!/usr/bin/env python3
"""
pmtiles_extract: cuts an area out of a PMTiles archive (the Protomaps daily planet build,
or any local .pmtiles file) into a new, smaller one. It does the same job as
`pmtiles extract`, written in Python, so building packs only needs Python and the
`pmtiles` package.

It only reads what it needs: the directories that cover the area, then the tile data.
Nearby byte ranges are merged into bigger requests (a planet build keeps an area's tiles
mostly together) and a few are fetched at a time.

    pmtiles_extract.py https://build.protomaps.com/20261026.pmtiles wa.pmtiles \\
        --bbox=-124.9,45.5,-116.9,49.1 --maxzoom 14
    pmtiles_extract.py world.pmtiles us-overview.pmtiles --bbox=-180,-85,180,85 --maxzoom 6
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import gzip
import json
import math
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from pmtiles.tile import (Compression, deserialize_directory, deserialize_header,
                          zxy_to_tileid)
from pmtiles.writer import Writer

UA = "nomad-map-packs/1.0 (offline map packs for Jcorp Nomad)"
GAP = 512 * 1024          # merge ranges closer than this; the gap is read and thrown away
CHUNK = 48 * 1024 * 1024  # but never fetch more than this in one request
THREADS = 4


class Source:
    """get(offset, length) over a local file or an HTTP URL, with retries."""

    def __init__(self, src: str):
        self.src = src
        self.http = src.startswith(("http://", "https://"))
        self.fetched = 0
        self.requests = 0
        if not self.http:
            self.f = open(src, "rb")

    def get(self, offset: int, length: int) -> bytes:
        self.requests += 1
        self.fetched += length
        if not self.http:
            return os.pread(self.f.fileno(), length, offset)
        for attempt in range(6):
            try:
                req = urllib.request.Request(self.src, headers={
                    "Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": UA})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read()
                if len(data) != length:
                    raise IOError(f"short read {len(data)} of {length}")
                return data
            except Exception as e:                    # noqa: BLE001
                if attempt == 5:
                    raise
                wait = 2 ** attempt
                print(f"  trying again in {wait}s ({e})", file=sys.stderr)
                time.sleep(wait)
        raise AssertionError


def _decomp(data: bytes, comp) -> bytes:
    return gzip.decompress(data) if comp == Compression.GZIP else data


def wanted_ids(bbox, minz: int, maxz: int) -> list[int]:
    w, s, e, n = bbox
    def tx(lon, z): return min((1 << z) - 1, max(0, int((lon + 180) / 360 * (1 << z))))
    def ty(lat, z):
        lat = max(-85.0511, min(85.0511, lat))
        r = math.radians(lat)
        return min((1 << z) - 1, max(0, int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z))))
    ids = []
    for z in range(minz, maxz + 1):
        for x in range(tx(w, z), tx(e, z) + 1):
            for y in range(ty(n, z), ty(s, z) + 1):
                ids.append(zxy_to_tileid(z, x, y))
    ids.sort()
    return ids


def merge_ranges(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(offset, length) list -> fewer, larger (offset, length) reads."""
    out = []
    for off, ln in sorted(spans):
        if out and off - (out[-1][0] + out[-1][1]) <= GAP and off + ln - out[-1][0] <= CHUNK:
            start = out[-1][0]
            out[-1] = (start, max(out[-1][0] + out[-1][1], off + ln) - start)
        else:
            out.append((off, ln))
    return out


def resolve(src: Source, hdr, ids: list[int], log) -> dict[int, tuple[int, int]]:
    """tile id -> (offset in tile data, length), for every wanted id that has a tile."""
    found: dict[int, tuple[int, int]] = {}
    if hdr["internal_compression"] != Compression.GZIP:
        raise ValueError("only gzip-compressed directories are supported")
    # deserialize_directory gunzips by itself (the planet builds use gzip throughout)
    root = deserialize_directory(src.get(hdr["root_offset"], hdr["root_length"]))
    level = [(root, ids)]
    depth = 0
    while level:
        depth += 1
        leaves: dict[tuple[int, int], list[int]] = {}
        for entries, want in level:
            starts = [e.tile_id for e in entries]
            for tid in want:
                i = bisect.bisect_right(starts, tid) - 1
                if i < 0:
                    continue
                e = entries[i]
                if e.run_length == 0:                  # a leaf directory covers it
                    leaves.setdefault((e.offset, e.length), []).append(tid)
                elif tid < e.tile_id + e.run_length:
                    found[tid] = (e.offset, e.length)
        if not leaves:
            break
        base = hdr["leaf_directory_offset"]
        blobs = fetch_ranges(src, [(base + o, ln) for o, ln in leaves], log, f"directories (level {depth})")
        level = []
        for (o, ln), want in leaves.items():
            level.append((deserialize_directory(blobs(base + o, ln)), want))
    return found


def fetch_ranges(src: Source, spans, log, what: str, tmpdir=None):
    """Fetch spans (merged into big reads); returns get(offset, length) over what came back.
    The data waits in a temporary file (in tmpdir: a whole country is GBs, not for /tmp)."""
    reads = merge_ranges(spans)
    total = sum(ln for _, ln in reads)
    store = tempfile.TemporaryFile(dir=tmpdir)
    index = []                                        # (src offset, length, store offset)
    done = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(THREADS if src.http else 1) as ex:
        futs = {ex.submit(src.get, o, ln): (o, ln) for o, ln in reads}
        for fut in cf.as_completed(futs):
            o, ln = futs[fut]
            data = fut.result()
            pos = store.seek(0, 2)
            store.write(data)
            index.append((o, ln, pos))
            done += ln
            rate = done / max(0.1, time.time() - t0) / 1e6
            if sys.stdout.isatty():                    # a log file gets the one line at the end
                log(f"\r  {what}: {done / 1e6:,.0f} of {total / 1e6:,.0f} MB in {len(reads)} reads, {rate:.1f} MB/s", end="")
    log(f"\r  {what}: {total / 1e6:,.0f} MB in {len(reads)} reads, {time.time() - t0:.0f} s")
    index.sort()
    starts = [i[0] for i in index]

    def get(offset: int, length: int) -> bytes:
        i = bisect.bisect_right(starts, offset) - 1
        o, ln, pos = index[i]
        assert offset + length <= o + ln, "span not fetched"
        store.seek(pos + offset - o)
        return store.read(length)
    return get


def extract(source: str, out: Path, bbox, minzoom: int = 0, maxzoom: int = 14,
            extra_meta: dict | None = None, log=print, ids: list[int] | None = None) -> dict:
    """bbox and zooms, or exactly these tile ids (then bbox is only the header's bounds)."""
    src = Source(source)
    hdr = deserialize_header(src.get(0, 127))
    maxzoom = min(maxzoom, hdr["max_zoom"])
    ids = sorted(ids) if ids is not None else wanted_ids(bbox, minzoom, maxzoom)
    log(f"  {len(ids):,} tile slots in the area, zoom {minzoom}-{maxzoom}")
    meta_raw = src.get(hdr["metadata_offset"], hdr["metadata_length"])
    meta = json.loads(_decomp(meta_raw, hdr["internal_compression"]))

    found = resolve(src, hdr, ids, log)
    log(f"  {len(found):,} tiles to copy")
    base = hdr["tile_data_offset"]
    spans = {(base + o, ln) for o, ln in found.values()}
    get = fetch_ranges(src, list(spans), log, "tiles")

    tmp = out.with_name(out.name + ".partial")
    with open(tmp, "wb") as f:
        w = Writer(f)
        for tid in sorted(found):
            o, ln = found[tid]
            w.write_tile(tid, get(base + o, ln))
        w_bbox = [int(round(v * 1e7)) for v in bbox]
        header = {
            "tile_type": hdr["tile_type"], "tile_compression": hdr["tile_compression"],
            "min_zoom": minzoom, "max_zoom": maxzoom,
            "min_lon_e7": w_bbox[0], "min_lat_e7": w_bbox[1],
            "max_lon_e7": w_bbox[2], "max_lat_e7": w_bbox[3],
            "center_zoom": max(minzoom, min(maxzoom, 8)),
            "center_lon_e7": (w_bbox[0] + w_bbox[2]) // 2, "center_lat_e7": (w_bbox[1] + w_bbox[3]) // 2,
        }
        meta = {**meta, **(extra_meta or {})}
        w.finalize(header, meta)
    os.replace(tmp, out)
    return {"tiles": len(found), "bytes": out.stat().st_size, "fetched": src.fetched, "requests": src.requests}


def extract_groups(source: str, groups: dict, out_of, minzoom: int, maxzoom: int,
                   extra_meta: dict | None = None, log=print, tmpdir=None) -> dict:
    """Many archives from one pass: groups maps a name to (tile ids, bbox). The directories
    and tile data are read once for all of them, then each group is written to out_of(name).
    Returns name -> bytes written (groups with no tiles are skipped)."""
    src = Source(source)
    hdr = deserialize_header(src.get(0, 127))
    meta = json.loads(_decomp(src.get(hdr["metadata_offset"], hdr["metadata_length"]), hdr["internal_compression"]))
    meta = {**meta, **(extra_meta or {})}
    every = sorted({t for ids, _ in groups.values() for t in ids})
    log(f"  {len(every):,} tile slots in {len(groups):,} groups, zoom {minzoom}-{maxzoom}")
    found = resolve(src, hdr, every, log)
    log(f"  {len(found):,} tiles to copy")
    base = hdr["tile_data_offset"]
    get = fetch_ranges(src, list({(base + o, ln) for o, ln in found.values()}), log, "tiles", tmpdir)
    sizes = {}
    for name, (ids, bbox) in groups.items():
        have = [t for t in sorted(ids) if t in found]
        if not have:
            continue
        out = Path(out_of(name))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".partial")
        with open(tmp, "wb") as f:
            w = Writer(f)
            for tid in have:
                o, ln = found[tid]
                w.write_tile(tid, get(base + o, ln))
            b = [int(round(v * 1e7)) for v in bbox]
            w.finalize({"tile_type": hdr["tile_type"], "tile_compression": hdr["tile_compression"],
                        "min_zoom": minzoom, "max_zoom": maxzoom,
                        "min_lon_e7": b[0], "min_lat_e7": b[1], "max_lon_e7": b[2], "max_lat_e7": b[3],
                        "center_zoom": minzoom, "center_lon_e7": (b[0] + b[2]) // 2,
                        "center_lat_e7": (b[1] + b[3]) // 2}, meta)
        os.replace(tmp, out)
        sizes[name] = out.stat().st_size
    return sizes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="PMTiles URL or path")
    ap.add_argument("out", help="the new .pmtiles file to write")
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("--minzoom", type=int, default=0, help="lowest zoom to keep (default 0)")
    ap.add_argument("--maxzoom", type=int, default=14, help="deepest zoom to keep (default 14)")
    a = ap.parse_args()
    bbox = [float(v) for v in a.bbox.split(",")]
    t0 = time.time()
    st = extract(a.source, Path(a.out), bbox, a.minzoom, a.maxzoom,
                 log=lambda s, end="\n": print(s, end=end, flush=True))
    print(f"{st['tiles']:,} tiles, {st['bytes'] / 1e6:,.1f} MB written, "
          f"{st['fetched'] / 1e6:,.1f} MB fetched in {st['requests']} requests, {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
