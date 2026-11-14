#!/usr/bin/env python3
"""
route_pack: turns the road network of one area into an NRG3 routing pack for the Maps
app on Nomad.

It's built for Nomad's card, where every read costs about 25 ms plus 4.5 ms per KB, and
four reads can run at once (measured 2026-10-27). So the data is cut into many small
cells, each packed with varints and 1 m coordinates, and kept at three levels:

  level 0   every road and path, in ~32 KB cells: the start and end of a trip, and
            the whole of a short one
  level 1   motorway to secondary only, in bigger cells: the middle of a long trip
  level 2   motorway and trunk only: the middle of a trip across states

Points where a road just carries on into the next piece of itself are folded into its
shape. The big road levels keep shapes to within 10 m, since they're only drawn zoomed
out over a whole trip.

Vertices are OSM node ids, so packs built from different extracts (two states, say)
join up in the app by themselves. Where a route search reaches a spot two packs cover,
it loads both packs' cells there and the shared intersections line up by id.

Files, in <out>/routing/:
  roads.idx   'NRI3' u8 version, u32 cellCount, then per cell, grouped by level (the
              manifest says where each level's entries are, so an app reads only those):
              u8 level, i32 box e5 (south, west, north, east: the quadtree leaf, so a
              point is in exactly one cell per level), u64 offset, u32 length
  roads.bin   the cells, each:
              'NC3' u8 level, then varints: cellId, own V, foreign F, edges E, names N,
              own ids (ascending, delta), own coords (zigzag delta e5 lat,lon pairs),
              foreign ids (ascending, delta), foreign cell ids, foreign coords,
              names (len + utf8), edges: a, b (local: own 0..V-1, foreign V..),
              length m, flags u8, name+1 (0 = none), geometry count, then zigzag
              delta e5 points from a's position
  flags       bits 0-3 road class (osm_graph.ROAD_CLASSES), 4 car a->b allowed,
              5 car b->a allowed, 6 foot only (no cars)
  places/     search: places.tsv, one line per word of each place name
              (word, name, lat, lon, kind, category), sorted by word. index.json maps
              word starts (two letters, longer where two would be a big read) to
              the byte range of their lines, so a keystroke reads one slice of
              48 KB at most instead of the whole list

    python route_pack.py --pbf washington-latest.osm.pbf --out wa
    python route_pack.py --pbf oregon-latest.osm.pbf --out or --major-dump or.npz
    python route_pack.py --merge wa.npz or.npz --out highways     # big roads only, both
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

import osm_graph as og
import osm_pbf

SCALE = 1e5                  # coordinates are stored as e5 (about 1.1 m)
L1_MAX_CLASS = 3             # motorway, trunk, primary, secondary
L2_MAX_CLASS = 1             # motorway, trunk
BUDGET = {0: 32 * 1024, 1: 96 * 1024, 2: 96 * 1024}
SIMPLIFY_M = 10              # shape tolerance on levels 1 and 2
F_CAR_FWD, F_CAR_BWD, F_FOOT = 1 << 4, 1 << 5, 1 << 6


def _varint(buf: bytearray, v: int) -> None:
    while v >= 0x80:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v)


def _zz(v: int) -> int:
    return v * 2 if v >= 0 else -v * 2 - 1


def build_edges(R, log=print):
    """Undirected road segments between intersections, with their shapes."""
    refs, coords, offs = R["refs"], R["coords"], R["offs"]
    t0 = time.time()
    uniq, first_idx, counts = np.unique(refs, return_index=True, return_counts=True)
    pos = np.searchsorted(uniq, refs)
    is_vertex = counts[pos] >= 2
    is_vertex[offs[:-1]] = True
    is_vertex[offs[1:] - 1] = True
    vrefs = np.unique(refs[is_vertex])
    vpos = np.searchsorted(uniq, vrefs)
    vlat = np.round(coords[first_idx[vpos], 0] * SCALE).astype(np.int32)
    vlon = np.round(coords[first_idx[vpos], 1] * SCALE).astype(np.int32)
    vidx_of_ref = np.searchsorted(vrefs, refs)            # valid where is_vertex
    seg = osm_pbf._haversine_vec(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    clat = np.round(coords[:, 0] * SCALE).astype(np.int32).tolist()
    clon = np.round(coords[:, 1] * SCALE).astype(np.int32).tolist()
    vflag = is_vertex.tolist()
    vix = vidx_of_ref.tolist()
    refl = refs.tolist()
    cuml = cum.tolist()

    ea, eb, elen, eflag, ename, egoff, eglen = [], [], [], [], [], [], []
    glat, glon = [], []
    cls_a, name_a, one_a = R["cls"].tolist(), R["name"].tolist(), R["oneway"].tolist()
    offl = offs.tolist()
    for w in range(len(offl) - 1):
        a0, a1 = offl[w], offl[w + 1]
        code = cls_a[w]
        foot = og.ROAD_CLASSES[code] in og.FOOT_ONLY
        ow = one_a[w]
        flags = code
        if foot:
            flags |= F_FOOT
        else:
            if ow != -1:
                flags |= F_CAR_FWD
            if ow != 1:
                flags |= F_CAR_BWD
        nm = name_a[w]
        s = a0
        for i in range(a0 + 1, a1):
            if not (vflag[i] or i == a1 - 1):
                continue
            if refl[s] == refl[i]:
                s = i
                continue
            ea.append(vix[s])
            eb.append(vix[i])
            elen.append(max(1, int(round(cuml[i] - cuml[s]))))
            eflag.append(flags)
            ename.append(nm)
            egoff.append(len(glat))
            eglen.append(i - s - 1)
            glat.extend(clat[s + 1:i])
            glon.extend(clon[s + 1:i])
            s = i
    E = {
        "a": np.array(ea, dtype=np.int64), "b": np.array(eb, dtype=np.int64),
        "len": np.array(elen, dtype=np.int64), "flag": np.array(eflag, dtype=np.int64),
        "name": np.array(ename, dtype=np.int64), "goff": np.array(egoff, dtype=np.int64),
        "glen": np.array(eglen, dtype=np.int64),
        "glat": np.array(glat, dtype=np.int32), "glon": np.array(glon, dtype=np.int32),
    }
    log(f"  {len(vrefs):,} intersections, {len(ea):,} road segments, {len(glat):,} shape points "
        f"({time.time() - t0:.0f}s)")
    return vrefs, vlat, vlon, E


def _swap_dirs(flag: int) -> int:
    """The same edge walked the other way: car a->b and b->a trade places."""
    return (flag & ~(F_CAR_FWD | F_CAR_BWD)) | ((flag & F_CAR_FWD) << 1) | ((flag & F_CAR_BWD) >> 1)


def contract(vrefs, vlat, vlon, E, log=print):
    """Fold away the points where one road just carries on into the next (OSM splits ways
    at bridges, speed changes and so on) and nothing else joins: the two segments become
    one, the point becomes shape. Only where both sides are the same road (class, name,
    which way cars may go), so turns and names come out the same. Every junction stays,
    so packs still join up by OSM id."""
    t0 = time.time()
    a, b = E["a"], E["b"]
    n_e = len(a)
    deg = np.bincount(a, minlength=len(vrefs)) + np.bincount(b, minlength=len(vrefs))
    # the two edges at every degree-2 point
    ends = np.concatenate([a, b])
    eids = np.concatenate([np.arange(n_e), np.arange(n_e)])
    order = np.argsort(ends, kind="stable")
    ends_s, eids_s = ends[order], eids[order]
    starts = np.searchsorted(ends_s, np.arange(len(vrefs)))
    al, bl, ln, fl, nm = a.tolist(), b.tolist(), E["len"].tolist(), E["flag"].tolist(), E["name"].tolist()
    goff, glen = E["goff"].tolist(), E["glen"].tolist()
    glat_np, glon_np = E["glat"], E["glon"]
    degl, startl, eidl = deg.tolist(), starts.tolist(), eids_s.tolist()

    def other_edge(v, e):
        s = startl[v]
        e1, e2 = eidl[s], eidl[s + 1]
        return e2 if e1 == e else e1

    def oriented(e, frm):
        """flag of e walked from vertex frm"""
        return fl[e] if al[e] == frm else _swap_dirs(fl[e])

    def joinable(v):
        if degl[v] != 2:
            return False
        s = startl[v]
        e1, e2 = eidl[s], eidl[s + 1]
        if e1 == e2 or nm[e1] != nm[e2]:
            return False
        x = bl[e1] if al[e1] == v else al[e1]
        y = bl[e2] if al[e2] == v else al[e2]
        if x == y:
            return False
        # walking x -> v -> y, both pieces must allow the same travel
        return oriented(e1, x) == oriented(e2, v)

    keep = [not joinable(v) for v in range(len(vrefs))]
    used = bytearray(n_e)
    na, nb, nlen, nflag, nname, ngoff, nglen = [], [], [], [], [], [], []
    olat, olon = [], []

    def shape(e, frm):
        g0, g = goff[e], glen[e]
        la, lo = glat_np[g0:g0 + g].tolist(), glon_np[g0:g0 + g].tolist()
        if al[e] != frm:
            la.reverse()
            lo.reverse()
        return la, lo

    for e0 in range(n_e):
        if used[e0]:
            continue
        s = al[e0] if keep[al[e0]] else (bl[e0] if keep[bl[e0]] else -1)
        if s < 0:                    # middle of a chain: reached from its kept end, or a closed loop
            continue
        e, cur = e0, s
        flag = oriented(e, s)
        total, la_all, lo_all = 0, [], []
        while True:
            used[e] = 1
            nxt = bl[e] if al[e] == cur else al[e]
            la, lo = shape(e, cur)
            la_all += la
            lo_all += lo
            total += ln[e]
            if keep[nxt]:
                break
            la_all.append(int(vlat[nxt]))
            lo_all.append(int(vlon[nxt]))
            cur, e = nxt, other_edge(nxt, e)
        na.append(s)
        nb.append(nxt)
        nlen.append(total)
        nflag.append(flag)
        nname.append(nm[e0])
        ngoff.append(len(olat))
        nglen.append(len(la_all))
        olat += la_all
        olon += lo_all
    # closed loops of joinable points (a roundabout nothing else meets): left as they were
    for e in range(n_e):
        if not used[e]:
            na.append(al[e]); nb.append(bl[e]); nlen.append(ln[e]); nflag.append(fl[e]); nname.append(nm[e])
            la, lo = shape(e, al[e])
            ngoff.append(len(olat)); nglen.append(len(la)); olat += la; olon += lo
    E2 = {
        "a": np.array(na, dtype=np.int64), "b": np.array(nb, dtype=np.int64),
        "len": np.array(nlen, dtype=np.int64), "flag": np.array(nflag, dtype=np.int64),
        "name": np.array(nname, dtype=np.int64), "goff": np.array(ngoff, dtype=np.int64),
        "glen": np.array(nglen, dtype=np.int64),
        "glat": np.array(olat, dtype=np.int32), "glon": np.array(olon, dtype=np.int32),
    }
    log(f"  joined through roads: {n_e:,} -> {len(na):,} segments ({time.time() - t0:.0f}s)")
    return E2


def _simplify(la: list, lo: list, tol: float) -> list:
    """Douglas-Peucker on e5 points (lon scaled for latitude): indexes to keep."""
    n = len(la)
    if n < 3:
        return list(range(n))
    kx = math.cos(math.radians(la[0] / SCALE))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        y1, x1, y2, x2 = la[i], lo[i] * kx, la[j], lo[j] * kx
        dy, dx = y2 - y1, x2 - x1
        L = math.hypot(dx, dy) or 1.0
        far, fi = -1.0, -1
        for k in range(i + 1, j):
            d = abs(dy * (lo[k] * kx - x1) - dx * (la[k] - y1)) / L
            if d > far:
                far, fi = d, k
        if far > tol:
            keep[fi] = True
            stack.append((i, fi))
            stack.append((fi, j))
    return [k for k in range(n) if keep[k]]


def simplified(E, edges, vlat, vlon, tol_m: float):
    """A copy of E whose shapes for these edges are cut down to within tol_m metres: the
    big-road levels only draw the middle of a long trip, where 10 m doesn't show."""
    tol = tol_m / 1.11                   # metres -> e5 units (1 e5 ~ 1.11 m)
    glat, glon = E["glat"], E["glon"]
    goff, glen = E["goff"].copy(), E["glen"].copy()
    olat, olon = [], []
    al, bl = E["a"].tolist(), E["b"].tolist()
    for e in edges.tolist():
        g0, g = int(E["goff"][e]), int(E["glen"][e])
        if not g:
            goff[e] = len(olat); glen[e] = 0
            continue
        la = [int(vlat[al[e]])] + glat[g0:g0 + g].tolist() + [int(vlat[bl[e]])]
        lo = [int(vlon[al[e]])] + glon[g0:g0 + g].tolist() + [int(vlon[bl[e]])]
        ks = _simplify(la, lo, tol)[1:-1]
        goff[e] = len(olat); glen[e] = len(ks)
        olat += [la[k] for k in ks]
        olon += [lo[k] for k in ks]
    return {**E, "goff": goff, "glen": glen,
            "glat": np.array(olat, dtype=np.int32), "glon": np.array(olon, dtype=np.int32)}


def partition(vlat, vlon, verts, weight, budget):
    """Quadtree over these vertices until each leaf's weight fits; returns leaf boxes
    (south, west, north, east in e5) and each vertex's leaf."""
    leaves = []
    cell_of = np.full(len(vlat), -1, dtype=np.int64)
    la, lo = vlat[verts], vlon[verts]
    stack = [((int(la.min()), int(lo.min()), int(la.max()) + 1, int(lo.max()) + 1), verts)]
    while stack:
        (s, w, n, e), idx = stack.pop()
        if len(idx) == 0:
            continue
        if weight[idx].sum() <= budget or (n - s) < 20 or (e - w) < 20:
            cell_of[idx] = len(leaves)
            leaves.append((s, w, n, e))
            continue
        ms, mw = (s + n) // 2, (w + e) // 2
        top = vlat[idx] >= ms
        right = vlon[idx] >= mw
        for q, box in ((~top & ~right, (s, w, ms, mw)), (~top & right, (s, mw, ms, e)),
                       (top & ~right, (ms, w, n, mw)), (top & right, (ms, mw, n, e))):
            stack.append((box, idx[q]))
    return leaves, cell_of


def encode_cell(level, cid, own, cell_of, vrefs, vlat, vlon, edges, E, names):
    """One cell: own vertices, the foreign ends of its edges, names, edges."""
    own = np.sort(own)                                    # vertex index order = OSM id order
    a, b = E["a"][edges], E["b"][edges]
    ends = np.unique(np.concatenate([a, b]))
    foreign = ends[cell_of[ends] != cid]
    local = {int(v): i for i, v in enumerate(own.tolist())}
    for i, v in enumerate(foreign.tolist()):
        local[v] = len(own) + i
    name_local, name_list = {}, []
    out = bytearray(b"NC3")
    out.append(level)
    body = bytearray()

    def ids_coords(vs):
        prev = 0
        for r in vrefs[vs].tolist():
            _varint(body, r - prev)
            prev = r
        pla = plo = 0
        for la_, lo_ in zip(vlat[vs].tolist(), vlon[vs].tolist()):
            _varint(body, _zz(la_ - pla))
            _varint(body, _zz(lo_ - plo))
            pla, plo = la_, lo_

    ids_coords(own)
    ids_coords(foreign)
    for c in cell_of[foreign].tolist():
        _varint(body, c)
    edge_bytes = bytearray()
    flag, ln, nm, goff, glen = (E["flag"][edges].tolist(), E["len"][edges].tolist(), E["name"][edges].tolist(),
                                E["goff"][edges].tolist(), E["glen"][edges].tolist())
    glat, glon = E["glat"], E["glon"]
    for i, (ea, eb) in enumerate(zip(a.tolist(), b.tolist())):
        _varint(edge_bytes, local[ea])
        _varint(edge_bytes, local[eb])
        _varint(edge_bytes, ln[i])
        edge_bytes.append(flag[i])
        n = nm[i]
        if n < 0:
            _varint(edge_bytes, 0)
        else:
            if n not in name_local:
                name_local[n] = len(name_list)
                name_list.append(names[n])
            _varint(edge_bytes, name_local[n] + 1)
        g = glen[i]
        _varint(edge_bytes, g)
        pla, plo = int(vlat[ea]), int(vlon[ea])
        if g:
            for la_, lo_ in zip(glat[goff[i]:goff[i] + g].tolist(), glon[goff[i]:goff[i] + g].tolist()):
                _varint(edge_bytes, _zz(la_ - pla))
                _varint(edge_bytes, _zz(lo_ - plo))
                pla, plo = la_, lo_
    for s in name_list:
        bs = s.encode("utf-8")[:255]
        _varint(body, len(bs))
        body += bs
    head = bytearray()
    for v in (cid, len(own), len(foreign), len(edges), len(name_list)):
        _varint(head, v)
    return bytes(out + head + body + edge_bytes)


def write_level(level, mask, vrefs, vlat, vlon, E, names, fbin, index, log):
    edges = np.nonzero(mask)[0]
    if not len(edges):                    # Liechtenstein has no motorway: nothing at level 2
        log(f"  level {level}: no roads of this class")
        return 0
    a, b = E["a"][edges], E["b"][edges]
    verts = np.unique(np.concatenate([a, b]))
    # what a vertex costs: its share of the edges touching it, in rough encoded bytes
    est = 9 + 3.2 * E["glen"][edges]
    weight = np.zeros(len(vlat))
    np.add.at(weight, a, est / 2)
    np.add.at(weight, b, est / 2)
    leaves, cell_of = partition(vlat, vlon, verts, weight, BUDGET[level])
    # each edge goes to both of its ends' cells
    ca, cb = cell_of[a], cell_of[b]
    order = np.argsort(ca, kind="stable")
    by_cell: dict[int, list] = {}
    for c, e in zip(ca[order].tolist(), edges[order].tolist()):
        by_cell.setdefault(c, []).append(e)
    cross = np.nonzero(ca != cb)[0]
    for c, e in zip(cb[cross].tolist(), edges[cross].tolist()):
        by_cell.setdefault(c, []).append(e)
    own_of: dict[int, list] = {}
    for v, c in zip(verts.tolist(), cell_of[verts].tolist()):
        own_of.setdefault(c, []).append(v)
    sizes = []
    for cid, box in enumerate(leaves):
        es = np.array(sorted(by_cell.get(cid, [])), dtype=np.int64)
        blob = encode_cell(level, cid, np.array(own_of.get(cid, []), dtype=np.int64), cell_of,
                           vrefs, vlat, vlon, es, E, names)
        off = fbin.tell()
        fbin.write(blob)
        index.append((level, box, off, len(blob)))
        sizes.append(len(blob))
    sizes = np.array(sizes)
    log(f"  level {level}: {len(leaves):,} cells, {sizes.sum() / 1e6:.1f} MB, "
        f"median {np.median(sizes) / 1024:.0f} KB, largest {sizes.max() / 1024:.0f} KB, "
        f"{len(cross):,} edges cross cells")
    return sizes.sum()


STOP_WORDS = {"the", "of", "and", "at", "on", "in", "de", "la", "el"}


def fold(s: str) -> str:
    """Lower case, accents off: what search compares on."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


PLACES_SLICE = 48 * 1024    # what one search keystroke may read: ~0.2 s on a Nomad


def _slices(words: list[str], sizes: list[int], lo: int, hi: int, n: int, out: dict, offs: list[int]) -> None:
    """Rows lo..hi share their first n letters. Split them by one more letter until each
    group is small enough to read for one keystroke."""
    total = offs[hi] - offs[lo]
    if total <= PLACES_SLICE or all(len(w) <= n for w in words[lo:hi]) or n >= 8:
        out[words[lo][:n]] = [offs[lo], total]
        return
    i = lo
    while i < hi:
        key = words[i][:n + 1]
        j = i
        while j < hi and words[j][:n + 1] == key:
            j += 1
        _slices(words, sizes, i, j, n + 1, out, offs)
        i = j


def write_places(places: dict, pdir: Path, log=print) -> int:
    pdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, lat, lon, kind, cat in places["items"]:
        words = {w for w in "".join(ch if ch.isalnum() else " " for ch in fold(name)).split()
                 if len(w) >= 2 and w not in STOP_WORDS}
        for w in words:
            rows.append((w, name.replace("\t", " ").replace("\n", " "), lat, lon, kind, cat))
    rows.sort(key=lambda r: (r[0], r[4], r[1]))
    lines = [f"{w}\t{name}\t{lat:.5f}\t{lon:.5f}\t{kind}\t{cat}\n".encode("utf-8")
             for w, name, lat, lon, kind, cat in rows]
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len(ln))
    words = [r[0] for r in rows]
    prefixes: dict = {}
    i = 0
    while i < len(rows):                  # two letters first, longer where that is too much
        key = words[i][:2]
        j = i
        while j < len(rows) and words[j][:2] == key:
            j += 1
        _slices(words, [], i, j, 2, prefixes, offs)
        i = j
    (pdir / "places.tsv").write_bytes(b"".join(lines))
    (pdir / "index.json").write_text(json.dumps({"kinds": places["kinds"], "cats": places["cats"],
                                                 "count": len(places["items"]), "slice": PLACES_SLICE,
                                                 "prefixes": prefixes}, separators=(",", ":")), encoding="utf-8")
    big = max(v[1] for v in prefixes.values()) if prefixes else 0
    log(f"  places: {len(places['items']):,} places, {len(rows):,} words, {offs[-1] / 1e6:.1f} MB, "
        f"{len(prefixes):,} slices, largest {big / 1024:.0f} KB")
    return offs[-1]


def level_masks(E):
    """Which edges each level holds."""
    cls = E["flag"] & 0x0F
    car = (E["flag"] & F_FOOT) == 0
    return {0: np.ones(len(E["a"]), dtype=bool), 1: (cls <= L1_MAX_CLASS) & car, 2: (cls <= L2_MAX_CLASS) & car}


def write_pack(rdir: Path, vrefs, vlat, vlon, E, names, levels=(0, 1, 2), log=print) -> dict:
    """roads.bin + roads.idx for these levels. Returns where each level's entries sit in
    roads.idx, so the app can read only the levels it needs."""
    rdir.mkdir(parents=True, exist_ok=True)
    index = []
    masks = level_masks(E)
    with open(rdir / "roads.bin.partial", "wb") as fbin:
        total = 0
        for lv in levels:
            ed = np.nonzero(masks[lv])[0]
            EL = simplified(E, ed, vlat, vlon, SIMPLIFY_M) if lv >= 1 else E
            total += write_level(lv, masks[lv], vrefs, vlat, vlon, EL, names, fbin, index, log)
    idx = bytearray(b"NRI3")
    idx.append(3)
    idx += struct.pack("<I", len(index))
    ranges = {}
    for level, (s, w, n, e), off, ln in index:
        r = ranges.setdefault(str(level), [len(idx), 0])
        r[1] += 29
        idx += struct.pack("<BiiiiQI", level, s, w, n, e, off, ln)
    (rdir / "roads.idx").write_bytes(bytes(idx))
    (rdir / "roads.bin.partial").replace(rdir / "roads.bin")
    return {"bytes": int(total), "idx_bytes": len(idx), "cells": len(index), "levels": ranges}


def routing(pbfs, out: Path, log=print, major_dump: Path | None = None) -> dict:
    """Directions only: routing/roads.idx + roads.bin (and the big roads for --merge)."""
    t0 = time.time()
    R = osm_pbf.read_roads(pbfs, log)
    vrefs, vlat, vlon, E = build_edges(R, log)
    names = R["names"]
    del R
    E = contract(vrefs, vlat, vlon, E, log)
    st = write_pack(out / "routing", vrefs, vlat, vlon, E, names, log=log)
    if major_dump:
        dump_major(major_dump, vrefs, vlat, vlon, E, names)
    bbox = [float(vlon.min()) / SCALE, float(vlat.min()) / SCALE, float(vlon.max()) / SCALE, float(vlat.max()) / SCALE]
    return {**st, "seconds": round(time.time() - t0), "bbox": bbox}


def places(pbfs, out: Path, log=print, places_cap=150000) -> int:
    """Place search only: places/index.json + places.tsv."""
    return write_places(osm_pbf._places_from_pbf(pbfs, places_cap, log), out / "places", log)


def build(pbfs, out: Path, log=print, places_cap=150000, major_dump: Path | None = None, extras: bool = True) -> dict:
    """Directions, search and (osm_extras) essentials, addresses and trails."""
    t0 = time.time()
    st = routing(pbfs, out, log, major_dump)
    places(pbfs, out, log, places_cap)
    if extras:
        import osm_extras
        osm_extras.build(pbfs, out, log)
    stats = {**st, "seconds": round(time.time() - t0)}
    log(f"  done: {st['bytes'] / 1e6:.1f} MB of cells, index {st['idx_bytes'] / 1024:.0f} KB, {stats['seconds']} s")
    return stats


def dump_major(path: Path, vrefs, vlat, vlon, E, names) -> None:
    """The big roads of one area, kept so a highways-only pack for many areas can be made
    without reading every extract again (merge_major)."""
    m = level_masks(E)[1]
    ed = np.nonzero(m)[0]
    used = np.unique(np.concatenate([E["a"][ed], E["b"][ed]]))
    remap = np.full(len(vrefs), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    gl = E["glen"][ed]
    go = E["goff"][ed]
    gidx = np.concatenate([np.arange(o, o + n) for o, n in zip(go.tolist(), gl.tolist())]) if len(ed) else np.zeros(0, np.int64)
    nm = E["name"][ed]
    keep_names = np.unique(nm[nm >= 0])
    nmap = {int(o): i for i, o in enumerate(keep_names.tolist())}
    np.savez_compressed(path, vrefs=vrefs[used], vlat=vlat[used], vlon=vlon[used],
                        a=remap[E["a"][ed]], b=remap[E["b"][ed]], len=E["len"][ed], flag=E["flag"][ed],
                        name=np.array([nmap.get(int(x), -1) for x in nm.tolist()], dtype=np.int64),
                        glen=gl, glat=E["glat"][gidx].astype(np.int32), glon=E["glon"][gidx].astype(np.int32),
                        names=np.array([names[i] for i in keep_names.tolist()], dtype=object))


def merge_major(dumps, out: Path, log=print) -> dict:
    """One highways-only pack (levels 1 and 2) from many areas' dump_major files: routes
    between any of them, with street-level packs only where you want them."""
    vr, la, lo, A, B, L, F, N, GL, GA, GO, names = [], [], [], [], [], [], [], [], [], [], [], []
    base_v = base_n = 0
    for p in dumps:
        d = np.load(p, allow_pickle=True)
        vr.append(d["vrefs"]); la.append(d["vlat"]); lo.append(d["vlon"])
        A.append(d["a"] + base_v); B.append(d["b"] + base_v)
        L.append(d["len"]); F.append(d["flag"]); GL.append(d["glen"]); GA.append(d["glat"]); GO.append(d["glon"])
        nm = d["name"]; N.append(np.where(nm >= 0, nm + base_n, -1))
        names += d["names"].tolist()
        base_v += len(d["vrefs"]); base_n += len(d["names"])
    vr = np.concatenate(vr)
    uniq, first = np.unique(vr, return_index=True)          # a border junction is in both areas
    new_of = np.searchsorted(uniq, vr)
    vlat, vlon = np.concatenate(la)[first], np.concatenate(lo)[first]
    a, b = new_of[np.concatenate(A)], new_of[np.concatenate(B)]
    ln, fl, glen = np.concatenate(L), np.concatenate(F), np.concatenate(GL)
    goff = np.concatenate([[0], np.cumsum(glen)[:-1]]).astype(np.int64)
    # a road that crosses a border is in both extracts: keep one copy
    key = np.stack([np.minimum(a, b), np.maximum(a, b), ln], axis=1)
    _, keep = np.unique(key, axis=0, return_index=True)
    keep.sort()
    E = {"a": a[keep], "b": b[keep], "len": ln[keep], "flag": fl[keep], "name": np.concatenate(N)[keep],
         "goff": goff[keep], "glen": glen[keep], "glat": np.concatenate(GA), "glon": np.concatenate(GO)}
    log(f"  {len(dumps)} areas: {len(uniq):,} junctions, {len(keep):,} road segments")
    st = write_pack(out / "routing", uniq, vlat, vlon, E, names, levels=(1, 2), log=log)
    st["bbox"] = [float(vlon.min()) / SCALE, float(vlat.min()) / SCALE, float(vlon.max()) / SCALE, float(vlat.max()) / SCALE]
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", nargs="+", help="one or more Geofabrik .osm.pbf extracts")
    ap.add_argument("--out", required=True, help="folder to build into")
    ap.add_argument("--major-dump", help="also keep this area's big roads here, for --merge")
    ap.add_argument("--merge", nargs="+", help="make a highways-only pack from these --major-dump files")
    a = ap.parse_args()
    if not (a.pbf or a.merge):
        ap.error("use --pbf or --merge")
    st = (merge_major(a.merge, Path(a.out)) if a.merge else
          build(a.pbf, Path(a.out), major_dump=Path(a.major_dump) if a.major_dump else None))
    print(json.dumps(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
