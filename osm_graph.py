#!/usr/bin/env python3
"""
osm_graph: turn a bounding box of OpenStreetMap data into a compact routing
graph and a place-search index that Nomad's offline Maps app reads directly.

Two products come out of one download:

  graph.bin   a small binary road network (nodes, directed edges with road
              class + oneway + shape geometry, street names). The Maps app
              runs A* over it in the browser, so directions work with no server.
  places.json named features (towns, parks, shops, junctions) for the search
              box, so you can route to a place by name.

Data source: Overpass first (server-side filtered, no node cap, what you want
for anything bigger than a town). If Overpass can't be reached it falls back to
the plain OpenStreetMap /map API, splitting the box into tiles small enough to
stay under that API's 50k-node limit and stitching the pieces together. Both
speak the same JSON, so the rest of the pipeline doesn't care which ran.

Standalone-testable:

    python osm_graph.py --bbox -80.010,40.435,-79.985,40.450 -o /tmp/pgh
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "nomad-card-tools-osm-graph/1.0 (offline media server)"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OSM_API = "https://api.openstreetmap.org/api/0.6/map.json"
OSM_API_NODE_CAP = 45_000   # the /map call rejects boxes over 50k nodes; stay under

# Road classes we keep, in priority order. Index into this list is the class
# code stored in each edge. _link ramps fold onto their parent class.
ROAD_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street", "service", "road",
    "footway", "path", "steps", "cycleway", "pedestrian", "track",
]
CLASS_CODE = {name: i for i, name in enumerate(ROAD_CLASSES)}
LINK_PARENT = {
    "motorway_link": "motorway", "trunk_link": "trunk", "primary_link": "primary",
    "secondary_link": "secondary", "tertiary_link": "tertiary",
}
# classes a car may not drive on (foot/bike only). Bit 4 of edge flags.
FOOT_ONLY = {"footway", "path", "steps", "cycleway", "pedestrian"}

# default km/h per class, for the drive-time estimate when maxspeed is absent
CLASS_SPEED = {
    "motorway": 100, "trunk": 90, "primary": 60, "secondary": 50, "tertiary": 45,
    "unclassified": 40, "residential": 30, "living_street": 12, "service": 20,
    "road": 40, "footway": 5, "path": 5, "steps": 4, "cycleway": 15,
    "pedestrian": 5, "track": 20,
}

# flag bits
F_FOOT_ONLY = 1 << 4    # cars can't use this edge
F_AGAINST_ONEWAY = 1 << 5  # this direction is forbidden to cars (reverse of a oneway)
F_GEOM_REVERSED = 1 << 6   # read the shared geometry span back-to-front

MAGIC = b"NRG1"

# named feature kinds worth putting in search, most useful first
PLACE_KEYS = ["place", "amenity", "shop", "tourism", "leisure", "natural",
              "historic", "office", "building", "aeroway", "railway"]


# geometry helpers

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# fetching

def _http_get(url: str, data: bytes | None = None, timeout: float = 180.0) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_overpass(bbox, log=print) -> dict | None:
    """Roads + named features in the box from Overpass. None if unreachable."""
    west, south, east, north = bbox
    b = f"{south},{west},{north},{east}"
    # ways with highway (with their nodes), plus any named node/way for search
    q = (
        "[out:json][timeout:180];("
        f'way["highway"]({b});'
        f'node["place"]({b});node["amenity"]({b});node["shop"]({b});'
        f'node["tourism"]({b});node["leisure"]({b});node["historic"]({b});'
        f'way["name"]["building"]({b});'
        ");(._;>;);out body;"
    )
    payload = urllib.parse.urlencode({"data": q}).encode()
    for ep in OVERPASS_ENDPOINTS:
        try:
            log(f"  Overpass: {ep.split('/')[2]} ...")
            raw = _http_get(ep, data=payload, timeout=180.0)
            j = json.loads(raw)
            if "elements" in j:
                log(f"  Overpass returned {len(j['elements']):,} elements")
                return j
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            log(f"    couldn't reach it ({type(e).__name__}), trying the next one")
    return None


def _osm_api_tile(bbox, timeout=120.0):
    west, south, east, north = bbox
    url = f"{OSM_API}?bbox={west},{south},{east},{north}"
    return json.loads(_http_get(url, timeout=timeout))


def fetch_osm_api(bbox, log=print) -> dict:
    """Fallback: the plain OSM /map API, recursively splitting the box until
    each tile fits under the node cap, then merging by element id.

    This API clips ways hard at the box edge, which would fragment roads that
    cross the boundary, so fetch a slightly padded area; the extra roads just
    beyond the tiles only help routing near the edges."""
    west, south, east, north = bbox
    pad = 0.008
    bbox = (west - pad, south - pad, east + pad, north + pad)
    merged: dict[tuple, dict] = {}
    stack = [tuple(bbox)]
    tiles = 0
    while stack:
        b = stack.pop()
        try:
            j = _osm_api_tile(b)
        except urllib.error.HTTPError as e:
            # 400 = too many nodes for this box; halve it along the long side
            if e.code in (400, 509) and (b[2] - b[0]) > 0.002 and (b[3] - b[1]) > 0.002:
                stack.extend(_split_bbox(b))
                continue
            raise
        n_nodes = sum(1 for el in j["elements"] if el["type"] == "node")
        if n_nodes >= OSM_API_NODE_CAP and (b[2] - b[0]) > 0.002:
            stack.extend(_split_bbox(b))
            continue
        tiles += 1
        for el in j["elements"]:
            merged[(el["type"], el["id"])] = el
        log(f"  OSM API tile {tiles}: {n_nodes:,} nodes  "
            f"[{b[0]:.4f},{b[1]:.4f},{b[2]:.4f},{b[3]:.4f}]  ({len(merged):,} total)")
        time.sleep(1.0)   # be polite to the shared API
    return {"elements": list(merged.values())}


def _split_bbox(b):
    west, south, east, north = b
    if (east - west) >= (north - south):
        mid = (west + east) / 2
        return [(west, south, mid, north), (mid, south, east, north)]
    mid = (south + north) / 2
    return [(west, south, east, mid), (west, mid, east, north)]


def fetch_osm(bbox, log=print) -> dict:
    j = fetch_overpass(bbox, log)
    if j is None:
        log("  Overpass isn't available, using the OpenStreetMap API instead "
            "(slower, it splits the area into tiles)")
        j = fetch_osm_api(bbox, log)
    return j


# graph construction

def _road_class(tags):
    hw = tags.get("highway")
    hw = LINK_PARENT.get(hw, hw)
    return hw if hw in CLASS_CODE else None


def _oneway(tags):
    """Return +1 forward-only, -1 backward-only, 0 both ways."""
    v = tags.get("oneway", "")
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if tags.get("junction") == "roundabout" or tags.get("highway") == "motorway":
        return 1
    return 0


def build_graph(osm: dict, bbox, log=print) -> dict:
    """OSM elements -> routing graph structures ready to serialize.

    A node is a graph vertex if two or more kept ways touch it, or it's a way
    endpoint. Nodes strictly interior to a single way become shape geometry on
    the edge, not vertices, which keeps the vertex count (and the file) small
    while preserving the road's true shape for turn cues.
    """
    nodes = {}       # osm node id -> (lat, lon)
    ways = []
    for el in osm["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif el["type"] == "way" and _road_class(el.get("tags", {})):
            ways.append(el)

    # how many kept ways touch each node -> intersections become vertices
    use = {}
    for w in ways:
        nd = w["nodes"]
        for nid in nd:
            use[nid] = use.get(nid, 0) + 1
        # endpoints are always vertices even if used once
        use[nd[0]] = max(use.get(nd[0], 0), 2)
        use[nd[-1]] = max(use.get(nd[-1], 0), 2)

    def is_vertex(nid):
        return use.get(nid, 0) >= 2

    # intern street names
    names = []
    name_idx = {}

    def name_for(tags):
        nm = tags.get("name") or tags.get("ref")
        if not nm:
            return 0xFFFFFFFF
        if nm not in name_idx:
            name_idx[nm] = len(names)
            names.append(nm)
        return name_idx[nm]

    # assign contiguous vertex indices
    vid = {}

    def vindex(nid):
        if nid not in vid:
            vid[nid] = len(vid)
        return vid[nid]

    edges = []       # (frm, to, dist_m, name_idx, geom_off, geom_len, flags)
    geom = []        # flat [ (lat,lon), ... ] shape points, referenced by span

    for w in ways:
        tags = w.get("tags", {})
        cls = _road_class(tags)
        code = CLASS_CODE[cls]
        foot_only = cls in FOOT_ONLY
        ow = _oneway(tags)
        nmi = name_for(tags)
        nd = [n for n in w["nodes"] if n in nodes]
        if len(nd) < 2:
            continue
        # walk the way, cutting a new edge every time we hit a vertex
        seg_start = 0
        for i in range(1, len(nd)):
            if is_vertex(nd[i]) or i == len(nd) - 1:
                a, b = nd[seg_start], nd[i]
                if a == b:
                    seg_start = i
                    continue
                pts = nd[seg_start:i + 1]
                # length along the real shape
                dist = 0.0
                for k in range(1, len(pts)):
                    la1, lo1 = nodes[pts[k - 1]]
                    la2, lo2 = nodes[pts[k]]
                    dist += haversine_m(la1, lo1, la2, lo2)
                dist_m = max(1, round(dist))
                # interior shape points (exclude the two endpoints)
                inner = pts[1:-1]
                goff = len(geom)
                for nid in inner:
                    geom.append(nodes[nid])
                glen = len(inner)
                va, vb = vindex(a), vindex(b)
                base = code | (F_FOOT_ONLY if foot_only else 0)
                # forward a->b
                fwd = base | (F_AGAINST_ONEWAY if ow == -1 else 0)
                edges.append((va, vb, dist_m, nmi, goff, glen, fwd))
                # backward b->a shares the same geometry, read reversed
                bwd = base | F_GEOM_REVERSED | (F_AGAINST_ONEWAY if ow == 1 else 0)
                edges.append((vb, va, dist_m, nmi, goff, glen, bwd))
                seg_start = i

    # vertex coordinates in index order
    inv = [None] * len(vid)
    for nid, idx in vid.items():
        inv[idx] = nodes[nid]

    places = _build_places(osm, nodes)
    log(f"  graph: {len(inv):,} vertices, {len(edges):,} directed edges, "
        f"{len(geom):,} shape points, {len(names):,} names, {len(places):,} places")
    return {"nodes": inv, "edges": edges, "geom": geom, "names": names,
            "places": places, "bbox": bbox}


def _way_centroid(way, nodes):
    pts = [nodes[n] for n in way.get("nodes", []) if n in nodes]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _build_places(osm, nodes) -> list:
    """Named features for the search box. Nodes give a point directly; named
    ways (a park, a campus) use their centroid."""
    out = []
    seen = set()
    for el in osm["elements"]:
        tags = el.get("tags", {})
        nm = tags.get("name")
        if not nm:
            continue
        kind = next((tags[k] for k in PLACE_KEYS if k in tags), None)
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        elif el["type"] == "way":
            c = _way_centroid(el, nodes)
            if not c:
                continue
            lat, lon = c
        else:
            continue
        key = (nm, round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append({"n": nm, "y": round(lat, 6), "x": round(lon, 6),
                    "k": kind or ""})
    # towns/cities first, then everything else, capped so the index stays small
    rank = {"city": 0, "town": 1, "village": 2, "suburb": 3, "neighbourhood": 4}
    out.sort(key=lambda p: rank.get(p["k"], 9))
    return out[:8000]


# serialization

def write_graph_bin(g: dict, path: Path):
    nodes, edges, geom, names = g["nodes"], g["edges"], g["geom"], g["names"]
    w, s, e, n = g["bbox"]
    buf = bytearray()
    buf += MAGIC
    buf += struct.pack("<I", 1)                       # version
    buf += struct.pack("<4f", w, s, e, n)
    buf += struct.pack("<IIII", len(nodes), len(edges), len(geom), len(names))
    for lat, lon in nodes:
        buf += struct.pack("<ii", round(lat * 1e6), round(lon * 1e6))
    for frm, to, dist, nmi, goff, glen, flags in edges:
        buf += struct.pack("<IIIIIHH", frm, to, dist, nmi, goff,
                           flags & 0xFFFF, glen & 0xFFFF)
    for lat, lon in geom:
        buf += struct.pack("<ii", round(lat * 1e6), round(lon * 1e6))
    for nm in names:
        b = nm.encode("utf-8")[:255]
        buf += struct.pack("<H", len(b)) + b
    path.write_bytes(buf)
    return len(buf)


def read_graph_bin(path: Path) -> dict:
    """Reference reader, used by the validator and to mirror the JS parser."""
    data = Path(path).read_bytes()
    assert data[:4] == MAGIC, "bad magic"
    off = 4
    (ver,) = struct.unpack_from("<I", data, off); off += 4
    w, s, e, n = struct.unpack_from("<4f", data, off); off += 16
    nc, ec, gc, nmc = struct.unpack_from("<IIII", data, off); off += 16
    nodes = []
    for _ in range(nc):
        la, lo = struct.unpack_from("<ii", data, off); off += 8
        nodes.append((la / 1e6, lo / 1e6))
    edges = []
    for _ in range(ec):
        frm, to, dist, nmi, goff, flags, glen = struct.unpack_from("<IIIIIHH", data, off)
        off += 24
        edges.append((frm, to, dist, nmi, goff, glen, flags))
    geom = []
    for _ in range(gc):
        la, lo = struct.unpack_from("<ii", data, off); off += 8
        geom.append((la / 1e6, lo / 1e6))
    names = []
    for _ in range(nmc):
        (ln,) = struct.unpack_from("<H", data, off); off += 2
        names.append(data[off:off + ln].decode("utf-8")); off += ln
    return {"version": ver, "bbox": (w, s, e, n), "nodes": nodes,
            "edges": edges, "geom": geom, "names": names}


def build_region_data(bbox, out_dir: Path, log=print) -> dict:
    """Fetch, build, and write graph.bin + places.json into out_dir/routing.
    Returns a small stats dict for the manifest."""
    osm = fetch_osm(bbox, log)
    g = build_graph(osm, bbox, log)
    rdir = out_dir / "routing"
    rdir.mkdir(parents=True, exist_ok=True)
    gsize = write_graph_bin(g, rdir / "graph.bin")
    (rdir / "places.json").write_text(json.dumps(g["places"], separators=(",", ":")))
    return {
        "nodes": len(g["nodes"]),
        "edges": len(g["edges"]),
        "places": len(g["places"]),
        "graphBytes": gsize,
        "placesBytes": (rdir / "places.json").stat().st_size,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a Nomad routing graph for a small area from OpenStreetMap.")
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("-o", "--output", required=True, help="region folder to write into")
    args = ap.parse_args()
    west, south, east, north = (float(p) for p in args.bbox.split(","))
    stats = build_region_data((west, south, east, north), Path(args.output))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
