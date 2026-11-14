#!/usr/bin/env python3
"""
build_map_pack: builds one map pack, ready to copy to /Maps/<id>/ on a card or to publish.

A pack is a folder that the Maps page on Nomad reads as one area:

    manifest.json        what the pack holds, its area and zoom range
    <id>.pmtiles         the map you see, vector tiles cut from the Protomaps planet build
    routing/roads.idx    directions (route_pack.py), if the pack has them
    routing/roads.bin
    places/index.json    search
    places/places.tsv

Any mix works. An overview pack is only a map (the world down to zoom 6), and a state
pack has all three. Packs work together on the card. "All maps" shows every map at once
and uses the most detailed one at each spot. Directions work across every pack that has
them, and states join up at their borders by OSM node id.

    build_map_pack.py --id washington --name Washington --bbox=-124.85,45.54,-116.91,49.01 \\
        --pbf https://download.geofabrik.de/north-america/us/washington-latest.osm.pbf -o packs
    build_map_pack.py --id world --name "World overview" --bbox=-180,-85,180,85 --maxzoom 6 -o packs
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pmtiles_extract
import route_pack

UA = "nomad-map-packs/1.0 (offline map packs for Jcorp Nomad)"
BUILDS = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BASE = "https://build.protomaps.com/"


def newest_build() -> str:
    req = urllib.request.Request(BUILDS, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        keys = sorted(b["key"] for b in json.load(r) if str(b.get("key", "")).endswith(".pmtiles"))
    return BUILD_BASE + keys[-1]


def fetch(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_name(dest.name + ".partial")
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 4 << 20)
    tmp.replace(dest)
    return dest


def build(pid: str, name: str, bbox, out: Path, pbf: str | None = None, maxzoom: int = 14,
          source: str | None = None, work: Path | None = None, log=print) -> dict:
    t0 = time.time()
    pdir = out / pid
    pdir.mkdir(parents=True, exist_ok=True)
    source = source or newest_build()
    log(f"{name}: map from {source}")
    tiles = pmtiles_extract.extract(source, pdir / f"{pid}.pmtiles", bbox, 0, maxzoom,
                                    extra_meta={"nomad:pack": pid},
                                    log=lambda s, end="\n": print(s, end=end, flush=True))
    man = {
        "type": "pack", "version": 1, "id": pid, "name": name,
        "bounds": bbox, "minZoom": 0, "maxZoom": maxzoom,
        "map": {"file": f"{pid}.pmtiles", "kind": "vector", "schema": "protomaps-v4",
                "bytes": tiles["bytes"], "source": source.rsplit("/", 1)[-1]},
        "attribution": "© OpenStreetMap contributors · Protomaps",
    }
    if pbf:
        work = work or out / ".work"
        work.mkdir(parents=True, exist_ok=True)
        path = Path(pbf) if not pbf.startswith("http") else fetch(pbf, work / pbf.rsplit("/", 1)[-1])
        log(f"{name}: roads from {path.name}")
        st = route_pack.build([path], pdir, log=log)
        man["routing"] = {"format": "nrg3", "index": "routing/roads.idx", "data": "routing/roads.bin",
                          "bytes": st["bytes"] + st["idx_bytes"], "bounds": st["bbox"], "levels": st["levels"]}
        man["places"] = {"index": "places/index.json", "data": "places/places.tsv"}
    man["sizeBytes"] = sum(f.stat().st_size for f in pdir.rglob("*") if f.is_file())
    man["built"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    man["packVersion"] = man["built"][:10]
    (pdir / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    log(f"{name}: {man['sizeBytes'] / 1e6:,.0f} MB in {time.time() - t0:.0f} s -> {pdir}")
    return man


def stage(pdir: Path, stage_dir: Path, description: str = "") -> Path:
    """Lay a built pack out the way the pack publishing script reads it:
    <stage>/<id>/pack.json and files/Maps/<id>/..., hard-linked, not copied."""
    man = json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))
    root = stage_dir / man["id"]
    files = root / "files" / "Maps" / man["id"]
    if root.exists():
        shutil.rmtree(root)
    files.mkdir(parents=True)
    for f in pdir.rglob("*"):
        if f.is_file():
            dst = files / f.relative_to(pdir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.hardlink_to(f)
            except OSError:
                shutil.copy2(f, dst)
    what = "map, directions and search" if man.get("routing") else "map"
    (root / "pack.json").write_text(json.dumps({
        "title": man["name"], "kind": "maps", "version": man.get("packVersion", ""),
        "description": description or f"{man['name']}: {what}, zoom {man['minZoom']}-{man['maxZoom']}",
    }, indent=1), encoding="utf-8")
    return root


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True, help="the pack's folder name, like washington")
    ap.add_argument("--name", required=True, help="the name shown on the Maps page")
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("--pbf", help="Geofabrik extract (URL or path) for directions and search")
    ap.add_argument("--maxzoom", type=int, default=14, help="deepest zoom level (default 14)")
    ap.add_argument("--source", help="PMTiles build to cut from (default: the newest Protomaps build)")
    ap.add_argument("-o", "--out", required=True, help="folder to build the pack in")
    ap.add_argument("--stage", help="also set the pack up for publishing, in this folder")
    a = ap.parse_args()
    build(a.id, a.name, [float(v) for v in a.bbox.split(",")], Path(a.out), a.pbf, a.maxzoom, a.source)
    if a.stage:
        print(f"ready to publish: {stage(Path(a.out) / a.id, Path(a.stage))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
