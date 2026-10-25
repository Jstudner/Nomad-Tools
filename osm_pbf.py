#!/usr/bin/env python3
"""
osm_pbf: build a routing graph straight from a Geofabrik .osm.pbf extract.

The Overpass/OSM-API paths in osm_graph are fine for a town or a county, but a
whole state has to come from a bulk extract: one download from geofabrik.de,
parsed locally, no API hammering. This reads the extract with pyosmium and
produces exactly the structures osm_graph.build_graph returns, so route_tiles
can tile it unchanged.

The heavy lifting is vectorized: every way's node refs and coordinates land in
flat numpy arrays, intersections are found with one np.unique, and all segment
lengths come from a single vectorized haversine plus a running sum. That keeps a
state-sized extract to a few GB of RAM instead of tens.

    python osm_pbf.py --pbf pennsylvania.osm.pbf --out /tmp/pa --tiled
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import osmium

import osm_graph as og

NO_NAME = 0xFFFFFFFF
PLACE_RANK = {"city": 0, "town": 1, "village": 2, "suburb": 3, "neighbourhood": 4}

# Browsable categories, in the order the app shows its chips. A named feature is
# filed under the first group its OSM value matches; anything else is "other" and
# is only kept if there is budget left, so a state's parks and restaurants never
# get crowded out by 30k unclassified nodes.
CATEGORIES = [
    ("town",    {"city", "town", "village", "suburb", "neighbourhood", "locality"}),
    ("food",    {"restaurant", "cafe", "fast_food", "bar", "pub", "biergarten",
                 "ice_cream", "bakery", "food_court"}),
    ("park",    {"park", "garden", "nature_reserve", "playground", "recreation_ground",
                 "pitch", "sports_centre", "swimming_pool", "picnic_site", "viewpoint",
                 "camp_site", "beach"}),
    ("shop",    {"supermarket", "convenience", "mall", "department_store", "marketplace",
                 "greengrocer", "butcher", "clothes", "hardware", "doityourself", "yes"}),
    ("fuel",    {"fuel", "charging_station"}),
    ("stay",    {"hotel", "motel", "hostel", "guest_house", "chalet"}),
    ("health",  {"hospital", "pharmacy", "clinic", "doctors", "dentist", "veterinary"}),
    ("transit", {"station", "bus_station", "aerodrome", "helipad", "ferry_terminal",
                 "parking", "stop", "halt"}),
    ("fun",     {"museum", "attraction", "theatre", "cinema", "artwork", "zoo",
                 "theme_park", "gallery", "monument", "memorial", "castle", "ruins"}),
    ("civic",   {"school", "university", "college", "library", "townhall", "police",
                 "fire_station", "post_office", "bank", "place_of_worship", "courthouse",
                 "community_centre", "kindergarten"}),
]
CAT_NAMES = [c[0] for c in CATEGORIES] + ["other"]
CAT_OTHER = len(CATEGORIES)


def _category_of(kind):
    for i, (_, vals) in enumerate(CATEGORIES):
        if kind in vals:
            return i
    return CAT_OTHER


def _haversine_vec(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _as_list(paths):
    return [paths] if isinstance(paths, (str, Path)) else list(paths)


def read_roads(pbf_paths, log=print):
    """One streaming pass per extract: every routable way's refs, coords and tags.

    Several extracts merge into one graph here. Geofabrik clips on admin
    boundaries but keeps whole ways that cross them, so the same way turns up in
    both neighbours; skipping ids already seen is what stops a state line from
    becoming a doubled road. Node ids are global to OSM, so vertices on either
    side line up by themselves.
    """
    t0 = time.time()
    paths = _as_list(pbf_paths)
    ref_chunks, coord_chunks, offs = [], [], [0]
    w_cls, w_name, w_one = [], [], []
    names, name_idx = [], {}
    seen_ways: set[int] = set()
    multi = len(paths) > 1
    kept = dupes = 0
    for path in paths:
        fp = (osmium.FileProcessor(str(path))
              .with_locations()
              .with_filter(osmium.filter.KeyFilter("highway")))
        for obj in fp:
            if not obj.is_way():
                continue
            if multi:
                if obj.id in seen_ways:
                    dupes += 1
                    continue
                seen_ways.add(obj.id)
            tags = obj.tags
            cls = og._road_class(tags)
            if cls is None:
                continue
            rs, cs = [], []
            for n in obj.nodes:
                if not n.location.valid():
                    continue
                rs.append(n.ref)
                cs.append((n.lat, n.lon))
            if len(rs) < 2:
                continue
            ref_chunks.append(np.fromiter(rs, dtype=np.int64, count=len(rs)))
            coord_chunks.append(np.array(cs, dtype=np.float64))
            offs.append(offs[-1] + len(rs))
            w_cls.append(og.CLASS_CODE[cls])
            nm = tags.get("name") or tags.get("ref")
            if nm:
                if nm not in name_idx:
                    name_idx[nm] = len(names)
                    names.append(nm)
                w_name.append(name_idx[nm])
            else:
                w_name.append(-1)
            w_one.append(og._oneway(tags))
            kept += 1
        if multi:
            log(f"  {Path(path).name}: {kept:,} ways so far")
    del seen_ways
    if not kept:
        raise RuntimeError("no routable highways found in that extract")
    if dupes:
        log(f"  skipped {dupes:,} ways shared across extract borders")
    refs = np.concatenate(ref_chunks)
    coords = np.concatenate(coord_chunks)
    del ref_chunks, coord_chunks
    log(f"  read {kept:,} routable ways, {len(refs):,} node refs, "
        f"{len(names):,} street names in {time.time()-t0:.1f}s")
    return {"refs": refs, "coords": coords, "offs": np.array(offs, dtype=np.int64),
            "cls": np.array(w_cls, dtype=np.uint8),
            "name": np.array(w_name, dtype=np.int64),
            "oneway": np.array(w_one, dtype=np.int8), "names": names}


def build_graph_from_pbf(pbf_paths, log=print, places_cap=50000):
    """Same output shape as osm_graph.build_graph: nodes/edges/geom/names/places/bbox.

    Takes one .osm.pbf or a list of them; a list becomes a single graph.
    """
    R = read_roads(pbf_paths, log)
    refs, coords, offs = R["refs"], R["coords"], R["offs"]
    t0 = time.time()

    # intersections: a ref used by 2+ ways, plus every way endpoint
    uniq, first_idx, counts = np.unique(refs, return_index=True, return_counts=True)
    pos = np.searchsorted(uniq, refs)
    is_vertex = counts[pos] >= 2
    is_vertex[offs[:-1]] = True                 # first node of each way
    is_vertex[offs[1:] - 1] = True              # last node of each way

    vertex_refs = np.unique(refs[is_vertex])
    vpos = np.searchsorted(uniq, vertex_refs)
    vlat = coords[first_idx[vpos], 0]
    vlon = coords[first_idx[vpos], 1]
    nodes = list(zip(vlat.tolist(), vlon.tolist()))
    vid_of = lambda ref_arr: np.searchsorted(vertex_refs, ref_arr)
    log(f"  {len(nodes):,} intersections of {len(uniq):,} road nodes "
        f"({time.time()-t0:.1f}s)")

    # every consecutive-segment length in one shot; edge length = running-sum delta
    seg = _haversine_vec(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    cum = np.concatenate([[0.0], np.cumsum(seg)])

    edges, geom = [], []
    coords_l = coords.tolist()
    vflags = is_vertex.tolist()
    cls_a, name_a, one_a = R["cls"], R["name"], R["oneway"]
    nways = len(offs) - 1
    t1 = time.time()
    for w in range(nways):
        a0, a1 = offs[w], offs[w + 1]
        code = int(cls_a[w])
        foot = og.ROAD_CLASSES[code] in og.FOOT_ONLY
        ow = int(one_a[w])
        nmi = int(name_a[w])
        nmi = NO_NAME if nmi < 0 else nmi
        base = code | (og.F_FOOT_ONLY if foot else 0)
        seg_start = a0
        for i in range(a0 + 1, a1):
            if not (vflags[i] or i == a1 - 1):
                continue
            if refs[seg_start] == refs[i]:
                seg_start = i
                continue
            dist_m = max(1, int(round(cum[i] - cum[seg_start])))
            goff = len(geom)
            for k in range(seg_start + 1, i):
                geom.append(coords_l[k])
            glen = i - seg_start - 1
            va = int(np.searchsorted(vertex_refs, refs[seg_start]))
            vb = int(np.searchsorted(vertex_refs, refs[i]))
            fwd = base | (og.F_AGAINST_ONEWAY if ow == -1 else 0)
            bwd = base | og.F_GEOM_REVERSED | (og.F_AGAINST_ONEWAY if ow == 1 else 0)
            edges.append((va, vb, dist_m, nmi, goff, glen, fwd))
            edges.append((vb, va, dist_m, nmi, goff, glen, bwd))
            seg_start = i
    log(f"  {len(edges):,} directed edges, {len(geom):,} shape points "
        f"({time.time()-t1:.1f}s)")

    places = _places_from_pbf(pbf_paths, places_cap, log)
    lat_min, lat_max = float(coords[:, 0].min()), float(coords[:, 0].max())
    lon_min, lon_max = float(coords[:, 1].min()), float(coords[:, 1].max())
    return {"nodes": nodes, "edges": edges, "geom": geom, "names": R["names"],
            "places": places, "bbox": (lon_min, lat_min, lon_max, lat_max)}


def _places_from_pbf(pbf_paths, cap, log=print):
    """Named nodes worth searching for, filed into browsable categories.

    Everything that lands in a real category is kept; only the unclassified
    leftovers compete for whatever budget remains. Emitted in a compact shape
    (shared kind/category tables + one array per place) because a state's worth
    of objects-with-keys is roughly a third bigger over the wire.
    """
    t0 = time.time()
    keep, filler, seen = [], [], set()
    kinds, kind_idx = [], {}
    for path in _as_list(pbf_paths):
        fp = osmium.FileProcessor(str(path)).with_filter(osmium.filter.KeyFilter("name"))
        for obj in fp:
            if not obj.is_node():
                continue
            tags = obj.tags
            nm = tags.get("name")
            if not nm:
                continue
            kind = next((tags[k] for k in og.PLACE_KEYS if k in tags), None)
            if kind is None:
                continue
            lat, lon = obj.location.lat, obj.location.lon
            key = (nm, round(lat, 4), round(lon, 4))
            if key in seen:
                continue
            seen.add(key)
            if kind not in kind_idx:
                kind_idx[kind] = len(kinds)
                kinds.append(kind)
            cat = _category_of(kind)
            rec = [nm, round(lat, 6), round(lon, 6), kind_idx[kind], cat]
            (filler if cat == CAT_OTHER else keep).append(rec)

    room = max(0, cap - len(keep))
    items = keep + filler[:room]
    counts = {}
    for r in items:
        counts[CAT_NAMES[r[4]]] = counts.get(CAT_NAMES[r[4]], 0) + 1
    log(f"  {len(keep)+len(filler):,} named places -> keeping {len(items):,} "
        f"({time.time()-t0:.1f}s)")
    log("  " + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    return {"kinds": kinds, "cats": CAT_NAMES, "items": items}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a routing graph from .osm.pbf extracts.")
    ap.add_argument("--pbf", required=True, nargs="+",
                    help="one or more extracts; several merge into one graph")
    ap.add_argument("--out", required=True, help="region dir (routing/ is written inside)")
    ap.add_argument("--tiled", action="store_true", help="write tiled cells instead of flat")
    ap.add_argument("--budget", type=int, default=9000)
    args = ap.parse_args()

    import route_tiles
    t0 = time.time()
    g = build_graph_from_pbf(args.pbf)
    out = Path(args.out)
    rdir = out / "routing"
    rdir.mkdir(parents=True, exist_ok=True)
    if args.tiled:
        stats = route_tiles.write_tiles(g, rdir, args.budget)
    else:
        stats = {"graphBytes": og.write_graph_bin(g, rdir / "graph.bin")}
    (rdir / "places.json").write_text(json.dumps(g["places"], separators=(",", ":")))
    stats["places"] = len(g["places"]["items"]) if isinstance(g["places"], dict) else len(g["places"])
    stats["bbox"] = g["bbox"]
    print(json.dumps(stats, indent=2))
    print(f"total {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
