#!/usr/bin/env python3
"""
map_catalog: builds the catalog that Download map packs reads, for any part of the world.
Packs in it never repeat each other on a card.

Each piece of data lives in one place only:

  world                  zoom 0-6, the whole world
  overview-<continent>   zoom 7-9 of one continent, the map between the areas you have
  highways-<continent>   directions on its big roads (motorway to secondary), so trips
                         can cross borders and areas you don't have
  blocks                 zoom 10-14 on one fixed grid for the whole planet (zoom 9 tiles,
                         about 50 km across). An area's map is the list of blocks it
                         touches. A block on a border is one file that every neighbour
                         lists, stored once.
  <area>                 a Geofabrik region (a US state, a Canadian province, a country,
                         a German state): its blocks by reference, street level
                         directions and place search

An area needs the world pack and its continent's overview and highways. The downloader
brings those along. Collections are ready made picks: the whole world, a continent, a
country, the US census regions and divisions (Northeast, New England, Pacific...). Their
sizes count a shared file once.

What lands on the card matches the site: /Maps/<pack>/... and /Maps/blocks/9/<x>/<y>.pmtiles.
The catalog is a folder you put on the web as it is (nomadflash.jcorptech.net/packs/v2/).
index.json (format 2) lists the packs and collections (title, size, what each needs), and
packs/<id>.json lists each pack's files and blocks with their size and SHA-256. Nomad-Tools
reads it, and so could a phone fetching packs for Nomad, since it only reads the lists.

    map_catalog.py areas --geofabrik index-v1.json --pick north-america,europe -o areas.json
    map_catalog.py roads --areas areas.json --roads ~/maps/roads           # directions + search
    map_catalog.py build --areas areas.json --geofabrik index-v1.json --roads ~/maps/roads \\
        --world ~/maps/packs/world --work ~/maps/cat-work --out ~/maps/catalog
    map_catalog.py sizes --out ~/maps/catalog                             # what sharing saves

`areas` goes down Geofabrik's list to regions small enough to build (route_pack needs about
15 times the extract's size in memory). It skips regions made of other regions (dach, alps,
us-west...). `roads` downloads and builds each one, several at once within a memory budget.
More than one computer can work on the same folder at once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pmtiles.tile import zxy_to_tileid

import pmtiles_extract as px

BLOCK_Z = 9                       # the grid blocks are cut on: zoom-9 tiles, ~50 km across
# (8 gives a quarter of the files but a state carries ~15% more of its neighbours;
#  10 another ~15% less for 3.5x the files. Measured on WA/OR/ID, 2026-10-27.)
TILES = "https://build.protomaps.com/20261026.pmtiles"
ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
SEA_CONTENTS = 24                 # a block with fewer different tiles than this is open water
BATCH = 3000                      # blocks cut per pass over the planet build
# osm_extras output an area pack carries, and its files (the index first)
EXTRAS = {"essentials": {"index": "index.json", "data": "near.tsv"},
          "addresses": {"index": "index.json", "streets": "streets.tsv", "numbers": "numbers.bin"},
          "trails": {"index": "index.json", "data": "trails.bin"}}
UA = "nomad-map-packs/1.0 (offline map packs for Jcorp Nomad)"
# Geofabrik regions made of others: building them too would put the same roads on a card twice
# shown as a country to open (its parts are the areas); the other composites are left out
GROUPS = {"us", "united-kingdom"}
COMPOSITE = {"us", "us-midwest", "us-northeast", "us-pacific", "us-south", "us-west", "alps", "dach",
             "britain-and-ireland", "united-kingdom", "great-britain"}
# (united-kingdom stands for its parts: England, Scotland, Wales, Bermuda, the Falklands;
#  great-britain is the first three again, as one extract too big to build)
SPLIT_AT = 1.5e9                  # an extract bigger than this is built as its sub-regions
CONTINENTS = {"africa": "Africa", "antarctica": "Antarctica", "asia": "Asia", "australia-oceania": "Australia and Oceania",
              "central-america": "Central America", "europe": "Europe", "north-america": "North America",
              "russia": "Russia", "south-america": "South America"}
# US Census Bureau regions and divisions, as collections
US_DIVISIONS = {
    "New England": ["connecticut", "maine", "massachusetts", "new-hampshire", "rhode-island", "vermont"],
    "Mid-Atlantic": ["new-jersey", "new-york", "pennsylvania"],
    "Great Lakes": ["illinois", "indiana", "michigan", "ohio", "wisconsin"],
    "Great Plains": ["iowa", "kansas", "minnesota", "missouri", "nebraska", "north-dakota", "south-dakota"],
    "South Atlantic": ["delaware", "district-of-columbia", "florida", "georgia", "maryland", "north-carolina",
                       "south-carolina", "virginia", "west-virginia"],
    "Deep South": ["alabama", "kentucky", "mississippi", "tennessee"],
    "South Central": ["arkansas", "louisiana", "oklahoma", "texas"],
    "Mountain West": ["arizona", "colorado", "idaho", "montana", "nevada", "new-mexico", "utah", "wyoming"],
    "Pacific": ["alaska", "california", "hawaii", "oregon", "washington"],
}
US_REGIONS = {
    "US Northeast": ["New England", "Mid-Atlantic"],
    "US Midwest": ["Great Lakes", "Great Plains"],
    "US South": ["South Atlantic", "Deep South", "South Central"],
    "US West": ["Mountain West", "Pacific"],
}
EXTRA = {"Pacific Northwest": ["us/washington", "us/oregon", "us/idaho", "british-columbia"]}


# tiles and areas

def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    lat = max(-85.0511, min(85.0511, lat))
    r = math.radians(lat)
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def _in_ring(lon, lat, ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_area(lon, lat, polys) -> bool:
    """polys: GeoJSON MultiPolygon coordinates."""
    for poly in polys:
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def area_blocks(polys, bz: int = BLOCK_Z) -> list[tuple[int, int]]:
    """The grid blocks an area touches: a corner of the area inside the block, or one of
    a 5x5 grid of points in the block inside the area."""
    pts = [pt for poly in polys for ring in poly for pt in ring]
    w, e = min(p[0] for p in pts), max(p[0] for p in pts)
    s, n = min(p[1] for p in pts), max(p[1] for p in pts)
    x0, y0 = tile_xy(w, n, bz)
    x1, y1 = tile_xy(e, s, bz)
    out = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            bw, bs, be, bn = tile_bounds(bz, x, y)
            hit = any(bw <= p[0] <= be and bs <= p[1] <= bn for p in pts)
            if not hit:
                hit = any(in_area(bw + (be - bw) * i / 4, bs + (bn - bs) * j / 4, polys)
                          for i in range(5) for j in range(5))
            if hit:
                out.append((x, y))
    return out


def block_tiles(bx: int, by: int, zmin: int, zmax: int, bz: int = BLOCK_Z) -> list[int]:
    """Every tile from zmin to zmax inside one block, exactly: no tile of a neighbour."""
    ids = []
    for z in range(zmin, zmax + 1):
        k = z - bz
        for x in range(bx << k, (bx + 1) << k):
            for y in range(by << k, (by + 1) << k):
                ids.append(zxy_to_tileid(z, x, y))
    return ids


def under_blocks(blocks, zmin: int, zmax: int, bz: int = BLOCK_Z) -> set[int]:
    """Tiles from zmin to zmax (all at or above the block zoom) over these blocks."""
    ids = set()
    for z in range(zmin, zmax + 1):
        k = bz - z
        for bx, by in blocks:
            if k >= 0:
                ids.add(zxy_to_tileid(z, bx >> k, by >> k))
            else:
                for x in range(bx << -k, (bx + 1) << -k):
                    for y in range(by << -k, (by + 1) << -k):
                        ids.add(zxy_to_tileid(z, x, y))
    return ids


def block_path(bx: int, by: int, bz: int = BLOCK_Z) -> str:
    return f"/Maps/blocks/{bz}/{bx}/{by}.pmtiles"


# pieces

def street_level(src: Path, dst: Path) -> dict:
    """A route_pack routing folder with only level 0 (the highways pack has the rest)."""
    idx = (src / "roads.idx").read_bytes()
    n = struct.unpack_from("<I", idx, 5)[0]
    keep, end = [], 0
    for i in range(n):
        e = idx[9 + 29 * i: 9 + 29 * (i + 1)]
        lv, _, _, _, _, off, ln = struct.unpack("<BiiiiQI", e)
        if lv == 0:
            keep.append(e)
            end = max(end, off + ln)
    dst.mkdir(parents=True, exist_ok=True)
    out = bytearray(idx[:5]) + struct.pack("<I", len(keep)) + b"".join(keep)
    (dst / "roads.idx").write_bytes(bytes(out))
    with open(src / "roads.bin", "rb") as fi, open(dst / "roads.bin", "wb") as fo:
        left = end
        while left:
            b = fi.read(min(left, 8 << 20))
            fo.write(b)
            left -= len(b)
    return {"bytes": end, "idx_bytes": len(out), "levels": {"0": [9, 29 * len(keep)]}}


def _link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack_id(gid: str) -> str:
    """Geofabrik ids are unique but US states carry a us/ prefix (Georgia is also a country)."""
    return gid.replace("/", "-")


# which areas

class Tree:
    def __init__(self, geofabrik: Path):
        d = json.loads(Path(geofabrik).read_text(encoding="utf-8"))
        self.f = {x["properties"]["id"]: x for x in d["features"]}
        self.kids: dict[str, list[str]] = {}
        for gid, x in self.f.items():
            p = x["properties"].get("parent")
            if p:
                self.kids.setdefault(p, []).append(gid)
        # Geofabrik hangs the US states off North America itself (us/washington); as a list
        # to pick from they belong under the United States
        if "us" in self.f and "north-america" in self.kids:
            states = [g for g in self.kids["north-america"] if g.startswith("us/")]
            self.kids["us"] = states
            self.kids["north-america"] = [g for g in self.kids["north-america"] if g not in states]

    def name(self, gid: str) -> str:
        n = self.f[gid]["properties"].get("name") or gid
        if "/" in n or n == n.lower():                 # US states are named by their id: us/new-york
            n = " ".join(w if w == "of" else "US" if w == "us" else w.capitalize() for w in n.split("/")[-1].split("-"))
        return n

    def continent(self, gid: str) -> str:
        while self.f[gid]["properties"].get("parent"):
            gid = self.f[gid]["properties"]["parent"]
        return gid

    def country(self, gid: str) -> str:
        """The region right under the continent (US states hang off the continent as us/...)."""
        if gid.startswith("us/"):
            return "us"
        chain = [gid]
        while self.f[chain[-1]]["properties"].get("parent"):
            chain.append(self.f[chain[-1]]["properties"]["parent"])
        return chain[-2] if len(chain) >= 2 else gid

    def polys(self, gid: str):
        g = self.f[gid]["geometry"]
        return g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]


def _head_size(url: str) -> int:
    """A file's size without fetching it. Geofabrik redirects to mirrors, and urllib
    follows a redirect with GET (the whole file), so ask for one byte and read the total."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                cr = r.headers.get("Content-Range")
                return int(cr.rsplit("/", 1)[1]) if cr else int(r.headers["Content-Length"])
        except OSError:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError


def pick_areas(tree: Tree, picks: list[str], log=print) -> list[dict]:
    """Buildable regions under these: an extract over SPLIT_AT is replaced by its sub-
    regions (when it has them), and composite regions are left out. Sizes are asked for a
    level of the tree at a time, eight at once (Geofabrik takes seconds per answer)."""
    out, seen = [], set()

    def expand(gid):
        """continents and composites stand for their parts"""
        if gid in COMPOSITE:
            return [x for k in sorted(tree.kids.get(gid, [])) for x in expand(k)]
        if gid in CONTINENTS and tree.kids.get(gid):
            return [x for k in sorted(tree.kids[gid]) for x in expand(k)]
        return [gid]

    level = [g for p in picks for g in expand(p)]
    with ThreadPoolExecutor(8) as ex:
        while level:
            level = [g for g in dict.fromkeys(level) if g not in seen]
            seen.update(level)
            sizes = dict(zip(level, ex.map(lambda g: _head_size(tree.f[g]["properties"]["urls"]["pbf"]), level)))
            nxt = []
            for gid in level:
                kids = [k for k in tree.kids.get(gid, []) if k not in COMPOSITE]
                if sizes[gid] > SPLIT_AT and kids:
                    log(f"  {gid}: {sizes[gid] / 1e9:.1f} GB, built as its {len(kids)} parts")
                    nxt += sorted(kids)
                    continue
                out.append({"gid": gid, "id": pack_id(gid), "name": tree.name(gid),
                            "pbf": tree.f[gid]["properties"]["urls"]["pbf"], "size": sizes[gid],
                            "continent": tree.continent(gid), "country": tree.country(gid)})
            level = nxt
    return sorted(out, key=lambda a: (a["continent"], a["country"], a["id"]))


# directions and search, per area

def build_roads(areas: list[dict], roads: Path, budget: float = 30e9, parallel: int = 4, downloads: int = 1,
                log=print) -> None:
    """Download each area and build it with route_pack (directions, search, and its big
    roads for the continent's highways pack). Several at once, within a memory budget;
    under systemd each build is capped so a big one can't push the machine over.
    Downloads one at a time by default: Geofabrik slows a machine that takes a lot in a
    day to ~50 KB/s (seen after ~30 GB), so a big run is best spread over nights."""
    pbfdir = roads / ".pbf"
    pbfdir.mkdir(parents=True, exist_ok=True)
    lock = threading.Condition()
    used = [0.0]
    capped = shutil.which("systemd-run") is not None
    here = Path(__file__).resolve().parent

    def fetch(a):
        dest = pbfdir / f"{a['id']}.osm.pbf"
        if dest.exists():                 # kept: a catalog stays on the day its extracts were fetched
            return dest
        part = dest.with_name(dest.name + ".part")
        with urllib.request.urlopen(urllib.request.Request(a["pbf"], headers={"User-Agent": UA}), timeout=120) as r, \
                open(part, "wb") as f:
            while (b := r.read(1 << 20)):
                f.write(b)
        if part.stat().st_size != a["size"]:
            raise IOError(f"{a['id']}: the download came up short")
        os.replace(part, dest)
        return dest

    def build(a, pbf):
        out = roads / a["id"]
        if (out / "stats.json").exists():
            if not (out / "essentials" / "index.json").exists():          # built before extras existed
                r = subprocess.run([sys.executable, str(here / "osm_extras.py"), "--pbf", str(pbf), "--out", str(out)],
                                   capture_output=True, text=True)
                (roads / f"{a['id']}.extras.log").write_text(r.stdout + r.stderr, encoding="utf-8")
                log(f"  {'extras for' if r.returncode == 0 else 'FAILED extras for'} {a['id']}")
            return
        need = min(18 * a["size"] + 1.5e9, budget)
        with lock:
            while used[0] and used[0] + need > budget:
                lock.wait()
            used[0] += need
        try:
            t = time.time()
            cmd = [sys.executable, str(here / "route_pack.py"), "--pbf", str(pbf), "--out", str(out),
                   "--major-dump", str(out / "major.npz")]
            if capped:
                cmd = ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={int(min(need * 1.6, budget))}",
                       "-p", "MemorySwapMax=0"] + cmd
            r = subprocess.run(cmd, capture_output=True, text=True)
            (roads / f"{a['id']}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            if r.returncode == 0 and last.startswith("{"):
                (out / "stats.json").write_text(last, encoding="utf-8")
                log(f"  built {a['id']} in {time.time() - t:.0f} s")
            else:
                log(f"  FAILED {a['id']} (exit {r.returncode}), see {a['id']}.log")
        finally:
            with lock:
                used[0] -= need
                lock.notify_all()

    todo = [a for a in sorted(areas, key=lambda a: -a["size"])
            if not (roads / a["id"] / "stats.json").exists() or not (roads / a["id"] / "essentials" / "index.json").exists()]
    log(f"{len(todo)} of {len(areas)} areas to build, {sum(a['size'] for a in todo) / 1e9:.1f} GB of extracts")
    with ThreadPoolExecutor(downloads) as dl, ThreadPoolExecutor(parallel) as bp:
        jobs = [(a, dl.submit(fetch, a)) for a in todo]
        builds = []
        for a, f in jobs:
            try:
                builds.append(bp.submit(build, a, f.result()))
            except Exception as e:                       # noqa: BLE001
                log(f"  DOWNLOAD FAILED {a['id']}: {e}")
        for b in builds:
            b.result()


# the catalog

def block_maxzoom(path: Path) -> int:
    """The deepest zoom a block file holds (0 if it isn't there)."""
    try:
        with open(path, "rb") as f:
            return px.deserialize_header(f.read(127))["max_zoom"]
    except (OSError, ValueError, KeyError):
        return 0


def cut_maps(areas: list[dict], tree: Tree, work: Path, out: Path, tiles: str = TILES, overview_max: int = 9,
             bz: int = BLOCK_Z, log=print, maxzoom: int = 14, overviews: bool = True) -> set:
    """The map part of the catalog: every area's blocks and each continent's overview, cut
    from one Protomaps build. Runs on its own (no roads needed), so a catalog's map can be
    pinned to a build before Protomaps thins its old ones; blocks and overviews already
    there are kept, unless they stop short of maxzoom (then they are cut again, deeper:
    a block never loses detail another pack put there). Returns the blocks that have land."""
    if overview_max + 1 < bz:
        raise ValueError("blocks start one zoom past the overview, which has to be at or past the grid's zoom")
    work.mkdir(parents=True, exist_ok=True)
    maps = out / "Maps"
    for a in areas:
        a["blocks"] = area_blocks(tree.polys(a["gid"]), bz)
    every = sorted({b for a in areas for b in a["blocks"]})
    log(f"{len(areas)} areas, {len(every):,} blocks of zoom {overview_max + 1}-14")

    # blocks: one pass over the planet build for all of them; blocks of open sea are dropped
    sea_file = work / f"sea-blocks-{bz}.json"
    sea = {tuple(b) for b in json.loads(sea_file.read_text())} if sea_file.exists() else set()
    bpath = lambda b: out / block_path(b[0], b[1], bz).lstrip("/")
    todo = [b for b in every if b not in sea and block_maxzoom(bpath(b)) < maxzoom]
    # a batch at a time: every tile slot costs ~1 KB of memory while it is looked up, so
    # 3,000 blocks (4 million slots) stay near 4 GB; a continent at once would not fit
    for i in range(0, len(todo), BATCH):
        batch = {b: (block_tiles(b[0], b[1], overview_max + 1, maxzoom, bz), tile_bounds(bz, *b)) for b in todo[i:i + BATCH]}
        log(f"blocks: {i + len(batch):,} of {len(todo):,}")
        px.extract_groups(tiles, batch, bpath, overview_max + 1, maxzoom, log=log, tmpdir=work)
        for b in batch:
            p = bpath(b)
            if not p.exists():
                sea.add(b)
                continue
            with open(p, "rb") as f:
                hdr = px.deserialize_header(f.read(127))
            if hdr["tile_contents_count"] < SEA_CONTENTS:
                p.unlink()
                sea.add(b)
        sea_file.write_text(json.dumps(sorted(sea)))            # a stopped run resumes from here
    have = {b for b in every if bpath(b).exists()}
    log(f"blocks: {len(have):,} with land, {len(sea & set(every)):,} only sea")
    for c in (sorted({a["continent"] for a in areas}) if overviews else []):
        land = {b for a in areas if a["continent"] == c for b in a["blocks"] if b in have}
        ov = maps / f"overview-{c}" / "overview.pmtiles"
        if not ov.exists():
            log(f"{CONTINENTS[c]} overview")
            ov.parent.mkdir(parents=True, exist_ok=True)
            px.extract(tiles, ov, _bbox_of([tile_bounds(bz, x, y) for x, y in land]), 7, overview_max, log=log,
                       ids=sorted(under_blocks(land, 7, overview_max, bz)))
    return have


def build(areas: list[dict], tree: Tree, roads: Path, world: Path, work: Path, out: Path, version: str,
          tiles: str = TILES, overview_max: int = 9, bz: int = BLOCK_Z, log=print) -> dict:
    t0 = time.time()
    maps = out / "Maps"
    areas = [a for a in areas if (roads / a["id"] / "stats.json").exists()]
    have = cut_maps(areas, tree, work, out, tiles, overview_max, bz, log)
    every = sorted({b for a in areas for b in a["blocks"]})
    conts = sorted({a["continent"] for a in areas})
    bpath = lambda b: out / block_path(b[0], b[1], bz).lstrip("/")

    mans: dict[str, dict] = {}
    # world: zoom 0-6, as built
    wman = json.loads((world / "manifest.json").read_text())
    _link(world / wman["map"]["file"], maps / "world" / "world.pmtiles")
    wman.update({"type": "pack", "version": 2, "id": "world", "name": "World", "minZoom": 0, "packVersion": version})
    wman["map"]["file"] = "world.pmtiles"
    mans["world"] = wman

    for c in conts:
        mine = [a for a in areas if a["continent"] == c]
        land = {b for a in mine for b in a["blocks"] if b in have}
        # overview: zoom 7..overview_max over the continent's land blocks. `cover` says where it
        # has tiles (zoom-5 squares): a bounding box can run around the world (Alaska, Russia)
        oid = f"overview-{c}"
        ov = maps / oid / "overview.pmtiles"
        box = _bbox_of([tile_bounds(bz, x, y) for x, y in land])
        cover = sorted({(x >> (bz - 5), y >> (bz - 5)) for x, y in land})
        mans[oid] = {"type": "pack", "version": 2, "id": oid, "name": f"{CONTINENTS[c]} overview", "bounds": box,
                     "minZoom": 7, "maxZoom": overview_max, "cover": {"zoom": 5, "tiles": [list(t) for t in cover]},
                     "map": {"file": "overview.pmtiles", "kind": "vector", "schema": "protomaps-v4",
                             "bytes": ov.stat().st_size, "source": tiles.rsplit("/", 1)[-1]},
                     "attribution": ATTRIBUTION, "packVersion": version}
        # highways: every area's big roads, merged (route_pack --merge), so borders join
        hid = f"highways-{c}"
        hdir = work / hid
        if not (hdir / "stats.json").exists():
            log(f"{CONTINENTS[c]} highways from {len(mine)} areas")
            import route_pack
            st = route_pack.merge_major([roads / a["id"] / "major.npz" for a in mine], hdir, log=log)
            (hdir / "stats.json").write_text(json.dumps(st), encoding="utf-8")
        st = json.loads((hdir / "stats.json").read_text())
        for f in ("roads.idx", "roads.bin"):
            _link(hdir / "routing" / f, maps / hid / "routing" / f)
        mans[hid] = {"type": "pack", "version": 2, "id": hid, "name": f"{CONTINENTS[c]} highways", "bounds": st["bbox"],
                     "routing": {"format": "nrg3", "index": "routing/roads.idx", "data": "routing/roads.bin",
                                 "bytes": st["bytes"] + st["idx_bytes"], "bounds": st["bbox"], "levels": st["levels"]},
                     "attribution": "© OpenStreetMap contributors", "packVersion": version}

    # areas: blocks by reference, streets, places
    for a in areas:
        aid, d = a["id"], roads / a["id"]
        st0 = json.loads((d / "stats.json").read_text())
        st = street_level(d / "routing", work / "streets" / aid)
        for f in ("roads.idx", "roads.bin"):
            _link(work / "streets" / aid / f, maps / aid / "routing" / f)
        for f in ("index.json", "places.tsv"):
            _link(d / "places" / f, maps / aid / "places" / f)
        extras = {}
        for sub, files in EXTRAS.items():
            if (d / sub / "index.json").exists():
                for f in files.values():
                    _link(d / sub / f, maps / aid / sub / f)
                extras[sub] = {k: f"{sub}/{f}" for k, f in files.items()}
        blocks = sorted(b for b in a["blocks"] if b in have)
        c = a["continent"]
        mans[aid] = {"type": "pack", "version": 2, "id": aid, "name": tree.name(a["gid"]), "bounds": st0["bbox"],
                     "minZoom": overview_max + 1, "maxZoom": 14,
                     "blocks": {"zoom": bz, "minZoom": overview_max + 1, "maxZoom": 14, "dir": "/Maps/blocks",
                                "tiles": [list(b) for b in blocks]},
                     "requires": ["world", f"overview-{c}", f"highways-{c}"],
                     "routing": {"format": "nrg3", "index": "routing/roads.idx", "data": "routing/roads.bin",
                                 "bytes": st["bytes"] + st["idx_bytes"], "bounds": st0["bbox"], "levels": st["levels"]},
                     "places": {"index": "places/index.json", "data": "places/places.tsv"},
                     **extras,
                     "attribution": ATTRIBUTION, "packVersion": version}
    for pid, m in mans.items():
        (maps / pid).mkdir(parents=True, exist_ok=True)
        own = sum(p.stat().st_size for p in (maps / pid).rglob("*") if p.is_file() and p.name != "manifest.json")
        m["sizeBytes"] = own + sum(bpath(tuple(b)).stat().st_size for b in m.get("blocks", {}).get("tiles", []))
        (maps / pid / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    # an area's pack folder from an earlier catalog that this one no longer has
    for d in maps.iterdir():
        if d.is_dir() and d.name != "blocks" and d.name not in mans:
            shutil.rmtree(d)

    # the lists: every pack with its own files and the blocks it uses, and the collections
    log("hashing files")
    blocks_index = {}
    for b in sorted(have):
        blocks_index[b] = {"path": block_path(b[0], b[1], bz), "size": bpath(b).stat().st_size, "sha256": _sha(bpath(b))}

    def files_of(pid):
        fs = sorted(p for p in (maps / pid).rglob("*") if p.is_file())
        fs.sort(key=lambda p: p.name == "manifest.json")           # the manifest last
        return [{"path": "/" + p.relative_to(out).as_posix(), "size": p.stat().st_size, "sha256": _sha(p)} for p in fs]

    by_area = {a["id"]: a for a in areas}
    full, listing = {}, []
    for pid, m in mans.items():
        a = by_area.get(pid)
        fs = files_of(pid)
        bl = [blocks_index[tuple(b)] for b in m.get("blocks", {}).get("tiles", [])]
        if a:
            group, sub = CONTINENTS[a["continent"]], tree.name(a["country"]) if a["country"] != a["gid"] else ""
            desc = "Street map, turn by turn directions and place search."
        elif pid == "world":
            group, sub, desc = "Start here", "", "The whole world, zoomed out. Every other pack builds on it."
        elif pid.startswith("overview-"):
            group, sub, desc = "Start here", "", f"{CONTINENTS[pid[9:]]} between the areas you download: towns and main roads."
        else:
            group, sub, desc = "Start here", "", f"Directions on the big roads of {CONTINENTS[pid[9:]]}, so trips can cross areas you don't have."
        entry = {"id": pid, "title": m["name"], "group": group, "country": sub, "kind": "maps", "version": version,
                 "description": desc, "requires": m.get("requires", []),
                 "size": sum(f["size"] for f in fs) + sum(b["size"] for b in bl), "manifest": f"packs/{pid}.json"}
        listing.append(entry)
        full[pid] = {**entry, "files": fs, "blocks": bl}
    (out / "packs").mkdir(parents=True, exist_ok=True)
    for p in full.values():
        (out / "packs" / f"{p['id']}.json").write_text(json.dumps(p, separators=(",", ":")), encoding="utf-8")
    for old in (out / "packs").glob("*.json"):
        if old.stem not in full:
            old.unlink()

    bundles = collections(areas, tree, full)
    base_order = ["world"] + [f"{k}-{c}" for c in conts for k in ("overview", "highways")]
    listing.sort(key=lambda p: (p["group"] != "Start here", base_order.index(p["id"]) if p["id"] in base_order else 0,
                                p["group"], p["country"], p["title"]))
    index = {"format": 2, "version": version, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "blockZoom": bz, "collections": bundles, "packs": listing}
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    total = sum(f["size"] for p in full.values() for f in p["files"]) + sum(b["size"] for b in blocks_index.values())
    log(f"catalog: {len(full)} packs, {len(bundles)} collections, {len(blocks_index):,} blocks, "
        f"{total / 1e9:.2f} GB, {time.time() - t0:.0f} s")
    return index


def collections(areas: list[dict], tree: Tree, full: dict) -> list[dict]:
    """Ready-made picks, each a list of area packs (the base packs come with them). A
    collection is listed when the catalog has at least one of its areas; its size counts
    every file once, the way it lands on a card."""
    ids = {a["id"] for a in areas}
    out = []

    def add(cid, title, group, members, desc=""):
        members = [m for m in members if m in ids]
        if not members:
            return
        need, seen = 0, set()
        for pid in members + sorted({r for m in members for r in full[m]["requires"]}):
            for f in full[pid]["files"] + full[pid]["blocks"]:
                if f["path"] not in seen:
                    seen.add(f["path"])
                    need += f["size"]
        out.append({"id": cid, "title": title, "group": group, "description": desc, "packs": sorted(members),
                    "areas": len(members), "size": need})

    add("world-all", "Whole world", "World", sorted(ids), "Every area there is a pack for.")
    for c, name in CONTINENTS.items():
        add(f"all-{c}", f"All of {name}", "Continents", [a["id"] for a in areas if a["continent"] == c])
    countries = {}
    for a in areas:
        if a["country"] != a["gid"]:
            countries.setdefault(a["country"], []).append(a["id"])
    for cc, members in sorted(countries.items()):
        if len(members) > 1:
            add(f"country-{cc}", "All of the USA" if cc == "us" else f"All of {tree.name(cc)}", "Countries", members)
    parts = {}                                          # England within the UK, and the like
    for a in areas:
        p = tree.f[a["gid"]]["properties"].get("parent")
        if p and p != a["country"] and p not in CONTINENTS:
            parts.setdefault(p, []).append(a["id"])
    for pp, members in sorted(parts.items()):
        if len(members) > 1:
            add(f"part-{pp}", f"All of {tree.name(pp)}", "Countries", members)
    st = lambda names: [f"us-{n}" for n in names]
    for region, divs in US_REGIONS.items():
        add(_slug(region), region, "US regions", st([s for d in divs for s in US_DIVISIONS[d]]),
            ", ".join(f"{d} ({len(US_DIVISIONS[d])} states)" for d in divs))
    for div, states in US_DIVISIONS.items():
        add("us-div-" + _slug(div), div, "US regions", st(states))
    for name, gids in EXTRA.items():
        add(_slug(name), name, "Regions", [pack_id(g) for g in gids])
    return out


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")


def _bbox_of(boxes) -> list[float]:
    return [round(min(b[0] for b in boxes), 5), round(min(b[1] for b in boxes), 5),
            round(max(b[2] for b in boxes), 5), round(max(b[3] for b in boxes), 5)]


def sizes(out: Path) -> None:
    """What the shared blocks save: each area on its own, against all of them together."""
    idx = json.loads((out / "index.json").read_text())
    full = {p["id"]: json.loads((out / p["manifest"]).read_text()) for p in idx["packs"]}
    areas = [p for p in full.values() if p["group"] != "Start here"]
    union = {b["path"]: b["size"] for p in areas for b in p["blocks"]}
    alone = sum(b["size"] for p in areas for b in p["blocks"])
    print(f"map blocks: {sum(union.values()) / 1e9:.2f} GB for every area, {alone / 1e9:.2f} GB if each carried its own")
    for p in sorted(areas, key=lambda p: (p["group"], p["country"], p["title"])):
        own = sum(f["size"] for f in p["files"])
        shared = sum(b["size"] for b in p["blocks"])
        print(f"  {p['group'][:13]:<13} {p['title']:<26} {len(p['blocks']):>4} blocks {shared / 1e6:>7,.0f} MB"
              f" + streets/places {own / 1e6:>5,.0f} MB")
    print("collections:")
    for c in idx.get("collections", []):
        print(f"  {c['group']:<12} {c['title']:<26} {c['areas']:>3} areas  {c['size'] / 1e9:>6.2f} GB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("areas", help="pick the regions to build")
    s.add_argument("--geofabrik", required=True, help="Geofabrik index-v1.json (with outlines)")
    s.add_argument("--pick", required=True, help="Geofabrik ids, comma separated: continents, countries, regions")
    s.add_argument("-o", "--out", required=True)
    s = sub.add_parser("roads", help="download and build directions and search for each area")
    s.add_argument("--areas", required=True, help="the areas.json made by the areas step")
    s.add_argument("--roads", required=True, help="folder for the built directions and search")
    s.add_argument("--only", help="area ids, comma separated")
    s.add_argument("--budget-gb", type=float, default=30, help="memory the builds can use in all, in GB (default 30)")
    s.add_argument("--parallel", type=int, default=4, help="how many areas to build at once (default 4)")
    s.add_argument("--downloads", type=int, default=1, help="how many extracts to download at once (keep it low, Geofabrik asks for fair use)")
    m = sub.add_parser("maps", help="cut only the map part (blocks and overviews), to lock in one Protomaps build")
    m.add_argument("--areas", required=True)
    m.add_argument("--geofabrik", required=True)
    m.add_argument("--work", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--tiles", default=TILES)
    m.add_argument("--overview-max", type=int, default=9)
    m.add_argument("--block-zoom", type=int, default=BLOCK_Z)
    b = sub.add_parser("build", help="build the catalog")
    b.add_argument("--areas", required=True)
    b.add_argument("--geofabrik", required=True)
    b.add_argument("--roads", required=True)
    b.add_argument("--world", required=True, help="a built world pack (build_map_pack --maxzoom 6)")
    b.add_argument("--work", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--version", default=time.strftime("%Y-%m-%d"))
    b.add_argument("--tiles", default=TILES)
    b.add_argument("--overview-max", type=int, default=9, help="deepest zoom of the overviews. Blocks start one zoom deeper")
    b.add_argument("--block-zoom", type=int, default=BLOCK_Z, help="zoom of the grid the blocks are cut on")
    b.add_argument("--only", help="area ids, comma separated (for a test catalog)")
    s = sub.add_parser("sizes", help="show what the shared blocks save")
    s.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "sizes":
        sizes(Path(a.out))
    elif a.cmd == "areas":
        tree = Tree(Path(a.geofabrik))
        areas = pick_areas(tree, a.pick.split(","))
        Path(a.out).write_text(json.dumps(areas, indent=1), encoding="utf-8")
        print(f"{len(areas)} areas, {sum(x['size'] for x in areas) / 1e9:.1f} GB of extracts -> {a.out}")
    elif a.cmd == "maps":
        have = cut_maps(json.loads(Path(a.areas).read_text()), Tree(Path(a.geofabrik)), Path(a.work), Path(a.out),
                        a.tiles, a.overview_max, a.block_zoom)
        print(f"{len(have):,} blocks with land in {a.out}")
    elif a.cmd == "roads":
        areas = json.loads(Path(a.areas).read_text())
        if a.only:
            areas = [x for x in areas if x["id"] in a.only.split(",")]
        build_roads(areas, Path(a.roads), a.budget_gb * 1e9, a.parallel, a.downloads)
    else:
        areas = json.loads(Path(a.areas).read_text())
        if a.only:
            areas = [x for x in areas if x["id"] in a.only.split(",")]
        build(areas, Tree(Path(a.geofabrik)), Path(a.roads), Path(a.world), Path(a.work), Path(a.out),
              a.version, a.tiles, a.overview_max, a.block_zoom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
