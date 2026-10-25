#!/usr/bin/env python3
"""
route_tiles: partition a road graph into adaptive cells so the Maps app can
route across huge areas while loading only the cells along the path.

The flat graph.bin (osm_graph.py) loads whole into the phone, which is fine for
a county but impossible for a state or a country. This splits the same graph
into a quadtree of cells, each capped at a budget of intersections, packs them
into one blob (l0.bin) with a small index (l0.idx), and stores each cross-cell
edge's far endpoint as a "halo" entry so cells stitch together. The app loads
the tiny index up front, then fetches individual cells by HTTP byte-range as A*
reaches them, so a trip only ever pulls a corridor of cells, not the world.

Formats (little-endian):
  l0.idx  'NRGT' ver, region bbox f32x4, cellCount u32, nodeTotal u32,
          then cellCount x (cellId u32, min/max lat/lon i32 e6, offset u64,
          length u32, vertexCount u32)
  cell    'NCL2' cellId u32, vertexCount/edgeCount/haloCount/nameCount/geomInts u32,
          vertices (globalId u32, latE6 i32, lonE6 i32) sorted by id,
          edges (fromGlobal u32, toGlobal u32, dist u32, nameIdx u32, geomOff u32,
                 flags u16, geomLen u16),
          halo (globalId u32, cellId u32, latE6 i32, lonE6 i32) sorted by id,
          names (u16 len + utf8), geom (i32 e6 pairs)

    python route_tiles.py --bbox -75.70,39.89,-75.51,40.03 -o /tmp/wc --budget 6000
    python route_tiles.py --validate /tmp/wc/routing   # self-check lazy vs full
"""
from __future__ import annotations

import argparse
import heapq
import struct
import sys
from pathlib import Path

import osm_graph as og

MAGIC_IDX = b"NRGT"
MAGIC_CELL = b"NCL2"
NO_NAME = 0xFFFFFFFF
F_FOOT = og.F_FOOT_ONLY
F_AGAINST = og.F_AGAINST_ONEWAY
F_REV = og.F_GEOM_REVERSED
# class code (edge flags low nibble) -> km/h, in ROAD_CLASSES order (the dict in
# osm_graph is keyed by name; routing needs it keyed by the code we store)
SPEED_BY_CODE = [og.CLASS_SPEED[c] for c in og.ROAD_CLASSES]


# ---- partition ----

def partition(nodes, budget, min_span=0.0015):
    """Quadtree over vertex coordinates until each leaf holds <= budget vertices
    (or the cell gets too small to split). Returns (cell_of[], cells[])."""
    n = len(nodes)
    lats = [p[0] for p in nodes]
    lons = [p[1] for p in nodes]
    root = (min(lats), min(lons), max(lats), max(lons))
    leaves = []
    stack = [(root, list(range(n)))]
    while stack:
        (minlat, minlon, maxlat, maxlon), idxs = stack.pop()
        if (len(idxs) <= budget or (maxlat - minlat) < min_span
                or (maxlon - minlon) < min_span):
            leaves.append(idxs)
            continue
        midlat = (minlat + maxlat) / 2
        midlon = (minlon + maxlon) / 2
        quads = ([], [], [], [])
        for i in idxs:
            la, lo = nodes[i]
            quads[(0 if la < midlat else 2) + (0 if lo < midlon else 1)].append(i)
        childbb = [
            (minlat, minlon, midlat, midlon),
            (minlat, midlon, midlat, maxlon),
            (midlat, minlon, maxlat, midlon),
            (midlat, midlon, maxlat, maxlon),
        ]
        for q in range(4):
            if quads[q]:
                stack.append((childbb[q], quads[q]))

    cell_of = [0] * n
    cells = []
    for cid, idxs in enumerate(leaves):
        for i in idxs:
            cell_of[i] = cid
        la = [nodes[i][0] for i in idxs]
        lo = [nodes[i][1] for i in idxs]
        cells.append({"id": cid, "bbox": (min(la), min(lo), max(la), max(lo)),
                      "verts": idxs, "edges": []})
    return cell_of, cells


# ---- serialize ----

def _e6(v):
    return int(round(v * 1e6))


def serialize_cell(cell, cell_of, nodes, edges, geom, names):
    cid = cell["id"]
    vids = sorted(cell["verts"])
    vset = set(vids)
    local_names, name_map = [], {}
    local_geom = []                       # list of (lat, lon)
    out_edges = []                        # (fromG,toG,dist,localName,geomOff,flags,geomLen)
    halo = {}                             # globalId -> (cellId, lat, lon)
    for ei in cell["edges"]:
        frm, to, dist, nmi, goff, glen, flags = edges[ei]
        if nmi == NO_NAME:
            lni = NO_NAME
        else:
            if nmi not in name_map:
                name_map[nmi] = len(local_names)
                local_names.append(names[nmi])
            lni = name_map[nmi]
        lgoff = len(local_geom)
        for k in range(glen):
            local_geom.append(geom[goff + k])
        out_edges.append((frm, to, dist, lni, lgoff, flags, glen))
        if to not in vset:
            halo[to] = (cell_of[to], nodes[to][0], nodes[to][1])

    buf = bytearray()
    buf += MAGIC_CELL
    buf += struct.pack("<I", cid)
    buf += struct.pack("<IIIII", len(vids), len(out_edges), len(halo),
                       len(local_names), len(local_geom) * 2)
    for vid in vids:
        la, lo = nodes[vid]
        buf += struct.pack("<Iii", vid, _e6(la), _e6(lo))
    for frm, to, dist, lni, goff, flags, glen in out_edges:
        buf += struct.pack("<IIIIIHH", frm, to, dist, lni & 0xFFFFFFFF,
                           goff, flags & 0xFFFF, glen & 0xFFFF)
    for gid in sorted(halo):
        cellid, la, lo = halo[gid]
        buf += struct.pack("<IIii", gid, cellid, _e6(la), _e6(lo))
    for nm in local_names:
        b = nm.encode("utf-8")[:255]
        buf += struct.pack("<H", len(b)) + b
    for la, lo in local_geom:
        buf += struct.pack("<ii", _e6(la), _e6(lo))
    return bytes(buf)


def write_tiles(g, rdir: Path, budget=9000, log=print):
    nodes, edges, geom, names = g["nodes"], g["edges"], g["geom"], g["names"]
    cell_of, cells = partition(nodes, budget)
    for ei, e in enumerate(edges):
        cells[cell_of[e[0]]]["edges"].append(ei)

    rdir.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    index = []
    for cell in cells:
        data = serialize_cell(cell, cell_of, nodes, edges, geom, names)
        index.append((cell["id"], cell["bbox"], len(blob), len(data), len(cell["verts"])))
        blob += data
    (rdir / "l0.bin").write_bytes(blob)

    w, s, e, n = g["bbox"]
    idx = bytearray()
    idx += MAGIC_IDX
    idx += struct.pack("<I", 1)
    idx += struct.pack("<4f", w, s, e, n)
    idx += struct.pack("<II", len(cells), len(nodes))
    for cid, (mnla, mnlo, mxla, mxlo), off, length, vc in index:
        idx += struct.pack("<IiiiiQII", cid, _e6(mnla), _e6(mnlo), _e6(mxla),
                           _e6(mxlo), off, length, vc)
    (rdir / "l0.idx").write_bytes(idx)

    biggest = max(index, key=lambda c: c[3])
    log(f"  tiled: {len(cells):,} cells, {len(nodes):,} vertices, {len(edges):,} edges; "
        f"l0.bin {len(blob)/1048576:.1f}MB, l0.idx {len(idx)/1024:.1f}KB; "
        f"largest cell {biggest[3]/1024:.0f}KB ({biggest[4]:,} verts)")
    return {"cells": len(cells), "nodes": len(nodes), "edges": len(edges),
            "binBytes": len(blob), "idxBytes": len(idx),
            "maxCellBytes": biggest[3], "budget": budget}


def build_tiled_region(bbox, out_dir: Path, budget=9000, log=print):
    osm = og.fetch_osm(bbox, log)
    g = og.build_graph(osm, bbox, log)
    stats = write_tiles(g, out_dir / "routing", budget, log)
    # places index is the same flat search list as the non-tiled build
    import json
    (out_dir / "routing" / "places.json").write_text(
        json.dumps(g["places"], separators=(",", ":")))
    stats["places"] = len(g["places"])
    return stats


# ---- reference reader + lazy A* (validation mirror of the JS) ----

class TiledReader:
    """Reads l0.idx/l0.bin the way the app will: index up front, cells by
    byte-range on demand. Tracks how many cells a query actually loads."""

    def __init__(self, rdir: Path):
        data = (rdir / "l0.idx").read_bytes()
        assert data[:4] == MAGIC_IDX
        off = 4
        (self.ver,) = struct.unpack_from("<I", data, off); off += 4
        self.bbox = struct.unpack_from("<4f", data, off); off += 16
        cc, self.node_total = struct.unpack_from("<II", data, off); off += 8
        self.cells = {}
        self.cell_list = []
        for _ in range(cc):
            cid, mnla, mnlo, mxla, mxlo, coff, length, vc = struct.unpack_from("<IiiiiQII", data, off)
            off += 36
            rec = {"id": cid, "bbox": (mnla/1e6, mnlo/1e6, mxla/1e6, mxlo/1e6),
                   "off": coff, "len": length, "vc": vc}
            self.cells[cid] = rec
            self.cell_list.append(rec)
        self.fh = open(rdir / "l0.bin", "rb")
        self.cache = {}
        self.loads = 0

    def load_cell(self, cid):
        if cid in self.cache:
            return self.cache[cid]
        rec = self.cells[cid]
        self.fh.seek(rec["off"])
        raw = self.fh.read(rec["len"])          # <- the byte-range fetch
        cell = self._parse(raw)
        self.cache[cid] = cell
        self.loads += 1
        return cell

    @staticmethod
    def _parse(raw):
        assert raw[:4] == MAGIC_CELL
        off = 4
        (cid,) = struct.unpack_from("<I", raw, off); off += 4
        vc, ec, hc, nc, gi = struct.unpack_from("<IIIII", raw, off); off += 20
        verts = {}
        for _ in range(vc):
            gid, la, lo = struct.unpack_from("<Iii", raw, off); off += 12
            verts[gid] = (la/1e6, lo/1e6)
        adj = {}
        for _ in range(ec):
            frm, to, dist, nmi, goff, flags, glen = struct.unpack_from("<IIIIIHH", raw, off)
            off += 24
            adj.setdefault(frm, []).append((to, dist, nmi, goff, flags, glen))
        halo = {}
        for _ in range(hc):
            gid, hcid, la, lo = struct.unpack_from("<IIii", raw, off); off += 16
            halo[gid] = (hcid, la/1e6, lo/1e6)
        names = []
        for _ in range(nc):
            (ln,) = struct.unpack_from("<H", raw, off); off += 2
            names.append(raw[off:off+ln].decode("utf-8")); off += ln
        geom = []
        for _ in range(gi // 2):
            la, lo = struct.unpack_from("<ii", raw, off); off += 8
            geom.append((la/1e6, lo/1e6))
        return {"id": cid, "verts": verts, "adj": adj, "halo": halo,
                "names": names, "geom": geom}

    def _cells_near(self, lat, lon, k=4):
        def d(rec):
            mnla, mnlo, mxla, mxlo = rec["bbox"]
            dla = 0 if mnla <= lat <= mxla else min(abs(lat-mnla), abs(lat-mxla))
            dlo = 0 if mnlo <= lon <= mxlo else min(abs(lon-mnlo), abs(lon-mxlo))
            return dla*dla + dlo*dlo
        return [r["id"] for r in sorted(self.cell_list, key=d)[:k]]

    def _drivable(self, flags, drive):
        return not drive or (not (flags & F_FOOT) and not (flags & F_AGAINST))

    def nearest(self, lat, lon, drive):
        best, bd, bcell = None, 1e18, None
        for cid in self._cells_near(lat, lon):
            cell = self.load_cell(cid)
            for gid, (la, lo) in cell["verts"].items():
                # vertex must have a usable out-edge for the profile
                ok = any(self._drivable(e[4], drive) for e in cell["adj"].get(gid, []))
                if not ok:
                    continue
                dd = og.haversine_m(lat, lon, la, lo)
                if dd < bd:
                    bd, best, bcell = dd, gid, cid
        return best, bcell

    def astar(self, s_ll, t_ll, drive=True):
        s, scell = self.nearest(s_ll[0], s_ll[1], drive)
        t, tcell = self.nearest(t_ll[0], t_ll[1], drive)
        if s is None or t is None:
            return None
        tlat, tlon = self.load_cell(tcell)["verts"][t]
        node_cell = {s: scell, t: tcell}
        node_coord = {s: self.load_cell(scell)["verts"][s], t: (tlat, tlon)}
        g = {s: 0.0}
        came = {}
        closed = set()
        H = lambda ll: og.haversine_m(ll[0], ll[1], tlat, tlon) / (27.78 if drive else 1.4)
        pq = [(H(node_coord[s]), s)]
        while pq:
            _, u = heapq.heappop(pq)
            if u in closed:
                continue
            closed.add(u)
            if u == t:
                break
            cell = self.load_cell(node_cell[u])
            for (to, dist, nmi, goff, flags, glen) in cell["adj"].get(u, []):
                if not self._drivable(flags, drive):
                    continue
                if to in cell["verts"]:
                    node_cell.setdefault(to, cell["id"])
                    node_coord.setdefault(to, cell["verts"][to])
                elif to in cell["halo"]:
                    hcid, la, lo = cell["halo"][to]
                    node_cell.setdefault(to, hcid)
                    node_coord.setdefault(to, (la, lo))
                else:
                    continue
                cls = flags & 0x0F
                spd = (SPEED_BY_CODE[cls] * 1000/3600) if drive else 1.4
                ng = g[u] + dist/spd
                if ng < g.get(to, 1e18):
                    g[to] = ng
                    came[to] = (u, dist, nmi, cell["names"], flags)
                    heapq.heappush(pq, (ng + H(node_coord[to]), to))
        if t not in g:
            return None
        # rebuild road-name sequence + total distance
        seq = []
        dist_total = 0.0
        u = t
        while u in came:
            pu, dist, nmi, cnames, flags = came[u]
            nm = cnames[nmi] if nmi != NO_NAME else "unnamed road"
            seq.append(nm)
            dist_total += dist
            u = pu
        seq.reverse()
        return {"time": g[t], "dist": dist_total, "roads": seq, "loads": self.loads,
                "cells_total": len(self.cell_list)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build or validate a tiled routing graph.")
    ap.add_argument("--bbox", help="west,south,east,north (build mode)")
    ap.add_argument("-o", "--output", help="region dir to write routing/ into")
    ap.add_argument("--budget", type=int, default=9000, help="max vertices per cell")
    ap.add_argument("--validate", help="a routing/ dir to self-check")
    args = ap.parse_args()
    if args.validate:
        r = TiledReader(Path(args.validate))
        print(f"index: {len(r.cell_list)} cells, {r.node_total:,} nodes, bbox {r.bbox}")
        return 0
    if not (args.bbox and args.output):
        ap.error("build mode needs --bbox and -o")
    bbox = tuple(float(p) for p in args.bbox.split(","))
    import json
    print(json.dumps(build_tiled_region(bbox, Path(args.output), args.budget), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
