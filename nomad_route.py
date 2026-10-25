#!/usr/bin/env python3
"""
nomad_route: build a full offline routing region for a nomad card.

One region, one folder under /Maps/<id>/, three parts stitched together:

  tiles/        the map you see, a tile pyramid (reuses the map_tiles puller,
                same sources and size estimate)
  routing/      graph.bin + places.json, the road network and place index the
                app routes over (built by osm_graph from OpenStreetMap)
  index.html    a self-contained Leaflet app: search, tap or GPS to set a start
                and destination, and turn-by-turn directions, all offline

Pick an area and zoom levels once; tiles and road data are pulled for the same
box so the picture and the routing always agree. The result opens straight from
nomad's Maps page.

    ./nomad_route.py                         # interactive
    ./nomad_route.py --name "Pittsburgh" --bbox -80.10,40.36,-79.86,40.50 \
        --zooms 11-15 -o /media/CARD --yes
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import nomad_common as nc
import map_buckets
import map_tiles
import optimize_tiles
import osm_graph
import route_tiles

APP_TEMPLATE = Path(__file__).with_name("assets") / "route-app.html"


def run(dest: str | None = None, name: str | None = None, region_id: str | None = None,
        bbox=None, zooms=None, url_tpl=None, attribution=None, fmt=None,
        assume_yes: bool = False, optimize: bool = True, quality: int = 80,
        tiled: bool = False, budget: int = 9000, buckets=None,
        work_dir=None) -> int:
    nc.banner("Build a routing map")
    print("This builds a full offline map for the areas you choose: the picture you")
    print("look at, plus the road network and a place search behind it, so Nomad")
    print("gives you real turn by turn directions with no internet at all.")
    print(nc.c("2", "\nIt runs in three steps and tells you the size before it starts."))
    print(nc.c("2", "Safe to interrupt at any point, re run it and it picks up again.\n"))

    # Two ways in: choose ready made areas, or type a box for somewhere small.
    coverage = None
    if buckets is None and bbox is None and not assume_yes:
        print("  1. Choose areas    " + nc.c("2", "states, big regions and ready made sets, with sizes"))
        print("  2. Bounding box    " + nc.c("2", "four numbers, for a town or a county"))
        if input("Choose [1]: ").strip() != "2":
            picked = map_buckets.pick(*(zooms or (6, 14)))
            if not picked:
                print("cancelled.")
                return 1
            buckets, zooms = picked

    if buckets:
        buckets, unknown = map_buckets.resolve(buckets)
        if unknown:
            print(nc.c("1;31", "don't know: " + ", ".join(unknown)))
            return 1
        buckets, _ = map_buckets.dedupe(buckets)
        names = [map_buckets.get(b)["name"] for b in buckets]
        name = name or (names[0] if len(names) == 1 else
                        f"{names[0]} and {len(names) - 1} more")
        bbox = map_buckets.merged_bbox(buckets)
        coverage = map_buckets.coverage_for(buckets)
        # anything state-sized needs the on-demand graph, so don't ask
        big = sum(map_buckets.get(b).get("pbfBytes") or 0 for b in buckets)
        tiled = tiled or len(buckets) > 1 or big > 60 * 1024 ** 2

    name = name or input("Name for this map (this is what the Maps page shows): ").strip()
    if not name:
        print("cancelled.")
        return 1
    region_id = region_id or map_tiles.slugify(name)

    bbox = bbox or map_tiles.ask_bbox()
    if not bbox:
        print("cancelled.")
        return 1
    if zooms:
        z_min, z_max = zooms
    elif assume_yes:
        z_min, z_max = (6, 14)
    elif buckets:
        z_min, z_max = map_buckets.ask_zooms(buckets)
    else:
        z_min, z_max = map_tiles.ask_zooms()

    if buckets:
        est = map_buckets.estimate(buckets, z_min, z_max)
        print(nc.c("1", "\n" + ", ".join(names[:8]) +
                   (f" and {len(names) - 8} more" if len(names) > 8 else "")))
        print(map_buckets.format_estimate(est))
        for w in map_buckets.warnings(est):
            print(nc.c("1;33", f"  ! {w}"))
        if not assume_yes and input("\nGo ahead with this? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            print("cancelled.")
            return 1

    # a state or bigger won't fit as one graph the phone loads whole; tiled mode
    # packs adaptive cells and streams only the ones along each trip
    if not tiled and not assume_yes and not buckets:
        ans = input("\nIs this a large area, a whole state or bigger? Tiled routing then\n"
                    "loads only the roads along each trip so it works at that scale. [y/N] ")
        tiled = ans.strip().lower() in ("y", "yes")

    if url_tpl is None:
        if assume_yes:
            _, url_tpl, attribution, fmt, _ = map_tiles.SOURCES[0]
        else:
            url_tpl, attribution, fmt = map_tiles.ask_source()
    attribution = attribution or url_tpl.split("/")[2]
    fmt = fmt or "jpg"

    dest = nc.choose_card(dest, prompt="Which card should this region go on?")
    if not dest:
        print("cancelled.")
        return 1
    region_dir = Path(dest) / "Maps" / region_id

    # 1. tiles (map_tiles writes tiles/ and an initial manifest.json)
    print(nc.c("1", "\nStep 1 of 3: map tiles"))
    rc = map_tiles.run(dest=dest, name=name, region_id=region_id, bbox=bbox,
                       zooms=(z_min, z_max), url_tpl=url_tpl,
                       attribution=attribution, fmt=fmt, assume_yes=assume_yes,
                       coverage=coverage, final_message=False)
    if rc not in (0, 2):        # 130 = interrupted, 1 = cancelled
        print("stopped before building routing data.")
        return rc

    # 1b. shrink the tiles (WebP, ~40% smaller) so bigger/closer regions fit
    tile_fmt = fmt or "jpg"
    if optimize and optimize_tiles.Image is not None:
        print(nc.c("1", "\n  Optimizing tiles (WebP)"))
        optimize_tiles.optimize_region(region_dir, "webp", quality)
        tile_fmt = "webp"
    elif optimize:
        print(nc.c("1;31", "  Pillow not installed, keeping original tiles (pip install Pillow)."))

    # 2. road network + places. Flat = one graph.bin loaded whole (best for a
    # county); tiled = adaptive cells fetched on demand (needed for a state or
    # bigger, where the whole graph won't fit in a phone).
    print(nc.c("1", "\nStep 2 of 3: road network and places (from OpenStreetMap)"))
    print(nc.c("2", "  Downloading and building the routing graph. This runs once and"))
    print(nc.c("2", "  is quick for a town, a few minutes for a metro area.\n"))
    places_path = region_dir / "routing" / "places.json"
    try:
        if buckets:
            # bulk extracts from geofabrik, one per region, merged into one graph
            try:
                import osm_pbf
            except ImportError as exc:
                # the most common failure here, and the bare exception message
                # ("No module named 'osmium'") tells nobody what to do
                print(nc.c("1;31", f"  a map of this size needs two extra packages ({exc})."))
                print(nc.c("1", "      pip3 install numpy osmium"))
                print(nc.c("2", "  they read the bulk map file from geofabrik."))
                print(nc.c("2", "  the tiles are already downloaded; re-run to add routing."))
                return 1
            prog = nc.Progress()
            pbfs = map_buckets.fetch_all(buckets, work_dir, log=prog, progress=prog)
            prog.done()
            rdir = region_dir / "routing"
            rdir.mkdir(parents=True, exist_ok=True)
            g = osm_pbf.build_graph_from_pbf(pbfs, log=lambda s: print(nc.c("2", s)))
            if tiled:
                stats = route_tiles.write_tiles(g, rdir, budget)
            else:
                stats = {"graphBytes": osm_graph.write_graph_bin(g, rdir / "graph.bin")}
            places_path.write_text(json.dumps(g["places"], separators=(",", ":")))
            stats["places"] = len(g["places"]["items"])
        elif tiled:
            stats = route_tiles.build_tiled_region(bbox, region_dir, budget)
        else:
            stats = osm_graph.build_region_data(bbox, region_dir)
    except Exception as e:                                  # noqa: BLE001
        print(nc.c("1;31", f"  routing build failed: {e}"))
        print(nc.c("1;31", "  the tiles are fine; re-run to retry just the routing step."))
        return 1
    places_bytes = places_path.stat().st_size if places_path.is_file() else 0
    if tiled:
        routing_block = {
            "format": "tiled",
            "index": "routing/l0.idx",
            "graph": "routing/l0.bin",
            "places": "routing/places.json",
            "cells": stats["cells"],
            "nodes": stats["nodes"],
            "edges": stats["edges"],
            "places_count": stats["places"],
        }
        routing_bytes = stats["binBytes"] + stats["idxBytes"] + places_bytes
        print(nc.c("1", f"  tiled routing: {stats['cells']:,} cells, "
                  f"{stats['nodes']:,} intersections, {stats['edges']:,} segments "
                  f"({nc.human(stats['binBytes'])}, loads on demand)"))
    else:
        routing_block = {
            "format": "flat",
            "graph": "routing/graph.bin",
            "places": "routing/places.json",
            "nodes": stats["nodes"],
            "edges": stats["edges"],
            "places_count": stats["places"],
        }
        routing_bytes = stats["graphBytes"] + places_bytes
        print(nc.c("1", f"  routing graph: {stats['nodes']:,} intersections, "
                  f"{stats['edges']:,} road segments, {stats['places']:,} places "
                  f"({nc.human(stats['graphBytes'])})"))

    # 3. app + finalized manifest
    print(nc.c("1", "\nStep 3 of 3: map app"))
    if not APP_TEMPLATE.is_file():
        print(nc.c("1;31", f"  app template missing: {APP_TEMPLATE}"))
        return 1
    shutil.copyfile(APP_TEMPLATE, region_dir / "index.html")

    man_path = region_dir / "manifest.json"
    manifest = json.loads(man_path.read_text()) if man_path.is_file() else {}
    manifest.update({
        "id": region_id,
        "name": name,
        "type": "route",
        "kind": "Routing map",
        "app": "index.html",
        "bounds": list(bbox),
        "minZoom": z_min,
        "maxZoom": z_max,
        "format": tile_fmt,
        "tilePath": f"tiles/{{z}}/{{x}}/{{y}}.{tile_fmt}",
        # the roads and places are OpenStreetMap data (ODbL), which needs crediting
        "attribution": f"{attribution}, roads and places © OpenStreetMap contributors",
        "routing": routing_block,
        "sizeBytes": manifest.get("sizeBytes", 0) + routing_bytes,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    man_path.write_text(json.dumps(manifest, indent=2))

    total = manifest["sizeBytes"]
    print(nc.c("1", f"\nDone. '{name}' is ready on the card ({nc.human(total)} total)."))
    # maps.html skips any region carrying its own app, so a routing build does
    # not show up in the region list. Say so here, or someone waits hours for a
    # map and then thinks the build failed.
    print(nc.c("1;33", "  Routing maps do not appear in the Maps page region list yet."))
    print(nc.c("2", "  Open it directly at:"))
    print(nc.c("2", f"    http://192.168.4.1/Maps/{region_id}/index.html   (on the nomad's own Wi-Fi)"))
    print(nc.c("2", f"    http://nomad.local/Maps/{region_id}/index.html   (in WiFi Mode)"))
    print(nc.c("2", "  Tap a start and a destination, or search, for directions."))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build an offline routing map (tiles + roads + app) onto a nomad card.")
    ap.add_argument("--name", help="region name shown in the Maps page")
    ap.add_argument("--id", dest="region_id", help="folder name under /Maps")
    ap.add_argument("--bbox", help="west,south,east,north")
    ap.add_argument("--zooms", help="min-max zoom levels, e.g. 11-15")
    ap.add_argument("--url", help="custom XYZ tile URL template with {z}/{x}/{y}")
    ap.add_argument("--attribution", help="attribution text")
    ap.add_argument("--format", dest="fmt", help="tile file extension (default jpg)")
    ap.add_argument("-o", "--output", help="destination card mountpoint")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before downloading")
    ap.add_argument("--no-optimize", dest="optimize", action="store_false",
                    help="keep original JPEG tiles (skip the WebP shrink)")
    ap.add_argument("--quality", type=int, default=80, help="WebP quality 1-100 (default 80)")
    ap.add_argument("--tiled", action="store_true",
                    help="build an on-demand tiled routing graph (for a state or bigger)")
    ap.add_argument("--budget", type=int, default=9000,
                    help="max intersections per cell in tiled mode (default 9000)")
    ap.add_argument("--areas", nargs="+",
                    help="ready made areas instead of a bbox, e.g. pa nj de, or east-coast")
    ap.add_argument("--work", help="where to keep the downloaded map data")
    # a leading-minus bbox looks like a flag to argparse; fold it into --bbox=
    argv = sys.argv[1:]
    for i, a in enumerate(argv[:-1]):
        if a == "--bbox" and argv[i + 1].startswith("-"):
            argv[i:i + 2] = [f"--bbox={argv[i + 1]}"]
            break
    args = ap.parse_args(argv)

    bbox = None
    if args.bbox:
        try:
            west, south, east, north = (float(p) for p in args.bbox.replace(" ", "").split(","))
            bbox = (west, south, east, north)
        except ValueError:
            ap.error("--bbox needs four numbers: west,south,east,north")
    zooms = None
    if args.zooms:
        try:
            lo, hi = (int(p) for p in args.zooms.split("-")) if "-" in args.zooms \
                else (int(args.zooms), int(args.zooms))
            zooms = (lo, hi)
        except ValueError:
            ap.error("--zooms format: min-max, e.g. 11-15")

    return run(dest=args.output, name=args.name, region_id=args.region_id,
               bbox=bbox, zooms=zooms, url_tpl=args.url,
               attribution=args.attribution, fmt=args.fmt, assume_yes=args.yes,
               optimize=args.optimize, quality=args.quality,
               tiled=args.tiled, budget=args.budget, buckets=args.areas,
               work_dir=args.work)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
