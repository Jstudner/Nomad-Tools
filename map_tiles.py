#!/usr/bin/env python3
"""
map_tiles: download offline map tiles for a region onto a nomad card.

Pick an area (bounding box) and zoom levels, and the tile pyramid is pulled
into <card>/Maps/<id>/ in the layout nomad's Maps page reads, manifest
included. Before anything downloads it fetches a few sample tiles to estimate
the total size, shows the tile count per zoom level, and checks the card has
room, so you know what you're in for up front.

Defaults to the USGS National Map topo service: public domain US government
data, no API key, no bulk download restrictions. Any XYZ tile server can be
substituted with --url (respect the provider's policy; the main
openstreetmap.org server forbids bulk downloading).

Resumable: tiles already on the card are skipped, so re-running finishes an
interrupted pull. Python stdlib only.

    python map_tiles.py                                     # fully interactive
    python map_tiles.py --id pennsylvania --name "Pennsylvania" \
        --bbox -80.52,39.72,-74.69,42.27 --zooms 6-12 -o /media/CARD --yes
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import nomad_common as nc

UA = "nomad-card-tools-map-tiles/1.0 (offline media server)"

# name, url template, attribution, tile format, coverage note
SOURCES = [
    ("USGS Topo",
     "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
     "USGS The National Map", "jpg",
     "topographic map, US only, public domain"),
    ("USGS Imagery + Topo",
     "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
     "USGS The National Map", "jpg",
     "aerial imagery with labels, US only, public domain"),
    ("USGS Imagery",
     "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
     "USGS The National Map", "jpg",
     "aerial imagery, US only, public domain"),
]

CONCURRENCY = 4          # polite parallelism against the tile server
SAMPLES_PER_ZOOM = 3     # tiles fetched up front per zoom level for the estimate
WARN_TILES = 50_000      # past this the download takes hours and wears the card


# tile math

def lon2tile(lon: float, z: int) -> int:
    return int(((lon + 180.0) / 360.0) * (2 ** z))


def lat2tile(lat: float, z: int) -> int:
    r = math.radians(lat)
    return int(((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0) * (2 ** z))


def zoom_ranges(bbox: tuple[float, float, float, float],
                z_min: int, z_max: int) -> list[tuple[int, int, int, int, int]]:
    """Per zoom level: (z, x0, x1, y0, y1) tile ranges covering the bbox."""
    west, south, east, north = bbox
    out = []
    for z in range(z_min, z_max + 1):
        x0, x1 = lon2tile(west, z), lon2tile(east, z)
        y0, y1 = lat2tile(north, z), lat2tile(south, z)   # y grows southward
        n = 2 ** z - 1
        out.append((z, max(0, x0), min(n, x1), max(0, y0), min(n, y1)))
    return out


def count_tiles(ranges) -> int:
    return sum((x1 - x0 + 1) * (y1 - y0 + 1) for _, x0, x1, y0, y1 in ranges)


# download plumbing

def fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sample_tile_size(url_tpl: str, ranges) -> int | None:
    """Average tile size in bytes, from a few tiles spread across each zoom.

    Center and corner tiles per level so land and edge tiles both count.
    Returns None when nothing could be fetched.
    """
    sizes: list[int] = []
    for z, x0, x1, y0, y1 in ranges:
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        picks = [(cx, cy), (x0, y0), (x1, y1)][:SAMPLES_PER_ZOOM]
        for x, y in picks:
            url = url_tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            try:
                sizes.append(len(fetch(url, timeout=15.0)))
            except (urllib.error.URLError, OSError):
                pass
    if not sizes:
        return None
    return sum(sizes) // len(sizes)


class _Counters:
    """Shared tallies for the worker pool."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = 0
        self.skipped = 0
        self.failed = 0
        self.bytes = 0
        self.failures: list[str] = []


def download_tile(url_tpl: str, tiles_dir: Path, fmt: str,
                  z: int, x: int, y: int, ctr: _Counters) -> None:
    dest = tiles_dir / str(z) / str(x) / f"{y}.{fmt}"
    if dest.is_file() and dest.stat().st_size > 0:
        with ctr.lock:
            ctr.skipped += 1
        return
    url = url_tpl.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    for attempt in range(4):
        try:
            data = fetch(url)
            if len(data) < 100:
                raise OSError("suspiciously small tile")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            with ctr.lock:
                ctr.done += 1
                ctr.bytes += len(data)
            return
        except (urllib.error.URLError, OSError) as exc:
            if attempt < 3:
                time.sleep(0.5 * (attempt + 1))
                continue
            with ctr.lock:
                ctr.failed += 1
                if len(ctr.failures) < 20:
                    ctr.failures.append(f"{z}/{x}/{y}: {exc}")


# interactive prompts

def ask_bbox() -> tuple[float, float, float, float] | None:
    print("\nDescribe the area as a bounding box: west,south,east,north")
    print(nc.c("2", "  (easy way: openstreetmap.org, Export tab, 'Manually select a"))
    print(nc.c("2", "   different area', then copy the four numbers: left,bottom,right,top)"))
    print(nc.c("2", "  example, Pennsylvania: -80.52,39.72,-74.69,42.27"))
    while True:
        raw = input("bbox: ").strip()
        if not raw:
            return None
        parts = raw.replace(" ", "").split(",")
        try:
            west, south, east, north = (float(p) for p in parts)
        except ValueError:
            print("  need exactly four numbers separated by commas.")
            continue
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            print("  that box is inside out or off the map. Check the order: west,south,east,north.")
            continue
        return (west, south, east, north)


def ask_zooms() -> tuple[int, int]:
    print("\nZoom levels (min-max). Each level has roughly 4x the tiles of the one before.")
    print(nc.c("2", "  6-12 is a good regional map (state roads down to town streets)."))
    print(nc.c("2", "  13-14 add street detail but grow fast; trim the box if you go there."))
    while True:
        raw = input("zooms [6-12]: ").strip() or "6-12"
        try:
            lo, hi = (int(p) for p in raw.split("-")) if "-" in raw else (int(raw), int(raw))
        except ValueError:
            print("  format: min-max, e.g. 6-12")
            continue
        if not (0 <= lo <= hi <= 16):
            print("  levels must be 0-16 with min at or below max.")
            continue
        return lo, hi


def ask_source() -> tuple[str, str, str]:
    print("\nTile source:")
    for i, (name, _, _, _, note) in enumerate(SOURCES, 1):
        print(f"  {i}. {nc.c('1', name)}  {nc.c('2', '(' + note + ')')}")
    print(f"  {len(SOURCES) + 1}. Custom XYZ URL template")
    while True:
        raw = input("Choose [1]: ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(SOURCES):
            name, url, attr, fmt, _ = SOURCES[int(raw) - 1]
            return url, attr, fmt
        if raw == str(len(SOURCES) + 1):
            url = input("  URL template (with {z}/{x}/{y}): ").strip()
            if "{z}" not in url or "{x}" not in url or "{y}" not in url:
                print("  template must contain {z}, {x} and {y}.")
                continue
            print(nc.c("2", "  check the provider allows bulk download. openstreetmap.org does not."))
            attr = input("  attribution text: ").strip() or url.split("/")[2]
            fmt = input("  tile format [jpg]: ").strip().lstrip(".") or "jpg"
            return url, attr, fmt
        print("  pick one of the numbers above.")


def slugify(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "region"


# main flow

def run(dest: str | None = None, name: str | None = None, region_id: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        zooms: tuple[int, int] | None = None,
        url_tpl: str | None = None, attribution: str | None = None,
        fmt: str | None = None, assume_yes: bool = False, coverage=None,
        final_message: bool = True) -> int:
    """Interactive tile pull. Keyword args skip the matching prompt.

    `coverage` is an optional map_buckets.Coverage. With one, tiles come from
    the region's outline rather than its bounding box, so open ocean inside
    the box is skipped.
    """
    nc.banner("Download offline maps")

    # what and where on the map
    name = name or input("\nRegion name (shown in the Maps page): ").strip()
    if not name:
        print("cancelled.")
        return 1
    region_id = region_id or slugify(name)

    bbox = bbox or ask_bbox()
    if not bbox:
        print("cancelled.")
        return 1
    z_min, z_max = zooms or ask_zooms()

    if url_tpl is None:
        if assume_yes:
            _, url_tpl, attribution, fmt, _ = SOURCES[0]
        else:
            url_tpl, attribution, fmt = ask_source()
    attribution = attribution or url_tpl.split("/")[2]
    fmt = fmt or "jpg"

    # plan and size estimate
    ranges = zoom_ranges(bbox, z_min, z_max)
    if coverage is not None:
        per_level = {z: sum(1 for _ in coverage.tiles(z)) for z in range(z_min, z_max + 1)}
        total_tiles = sum(per_level.values())
    else:
        per_level = {z: (x1 - x0 + 1) * (y1 - y0 + 1) for z, x0, x1, y0, y1 in ranges}
        total_tiles = count_tiles(ranges)

    print(nc.c("1", f"\n{name}") + f"  ({region_id})")
    print(f"  bbox [{', '.join(f'{v:.4f}' for v in bbox)}], zoom {z_min}-{z_max}")
    if coverage is not None:
        print(nc.c("2", "  following the region outline, so ocean and neighbours are skipped"))
    print("  tiles per level: " + ", ".join(f"z{z}:{n:,}" for z, n in sorted(per_level.items())))
    print(f"  total: {nc.c('1', f'{total_tiles:,} tiles')}")
    if total_tiles > WARN_TILES:
        print(nc.c("1;31", f"  warning: that is a lot of tiles (over {WARN_TILES:,}). This will"))
        print(nc.c("1;31", "  take hours. Consider a lower max zoom or a smaller box."))

    print("\nFetching a few sample tiles to estimate the download size...")
    avg = sample_tile_size(url_tpl, ranges)
    if avg is None:
        print(nc.c("1;31", "error: could not fetch any sample tile. The server may be"))
        print(nc.c("1;31", "       unreachable, or this source has no coverage for that area."))
        return 1
    est = avg * total_tiles
    print(f"  average tile {nc.human(avg)}, estimated download {nc.c('1', '~' + nc.human(est))}")

    # destination and space check
    dest = nc.choose_card(dest, prompt="Which card should this region go on?")
    if not dest:
        print("cancelled.")
        return 1
    region_dir = Path(dest) / "Maps" / region_id
    print(f"\nDestination: {nc.c('1', str(region_dir))}")
    existing = sum(1 for _ in region_dir.glob("tiles/*/*/*")) if region_dir.is_dir() else 0
    if existing:
        print(nc.c("2", f"  {existing:,} tiles already there. They'll be kept, the rest filled in."))
    try:
        free = shutil.disk_usage(dest).free
        print(f"  free space: {nc.human(free)}   (estimated need ~{nc.human(est)})")
        if free < est * 1.1:
            print(nc.c("1;31", "  warning: the card may not have room for this download."))
            if free < est:
                print(nc.c("1;31", "  it almost certainly does not. Free space or shrink the region."))
    except OSError:
        pass

    # confirm
    if not assume_yes and input("\nStart the download? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted.")
        return 1

    # download
    tiles_dir = region_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    ctr = _Counters()
    progress = nc.Progress()
    progress(f"downloading {total_tiles:,} tiles with {CONCURRENCY} workers "
             "(safe to interrupt, re-run to resume)")
    t0 = time.time()

    def jobs():
        if coverage is not None:
            for z in range(z_min, z_max + 1):
                for x, y in coverage.tiles(z):
                    yield z, x, y
            return
        for z, x0, x1, y0, y1 in ranges:
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    yield z, x, y

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            pending = set()
            for z, x, y in jobs():
                pending.add(pool.submit(download_tile, url_tpl, tiles_dir, fmt, z, x, y, ctr))
                if len(pending) >= CONCURRENCY * 8:
                    _, pending = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    n = ctr.done + ctr.skipped + ctr.failed
                    progress.pct(100.0 * n / total_tiles,
                                 f"{n:,}/{total_tiles:,}  {nc.human(ctr.bytes)} new"
                                 + (f"  {ctr.failed} failed" if ctr.failed else ""))
            concurrent.futures.wait(pending)
    except KeyboardInterrupt:
        progress.done()
        print(nc.c("1;31", "\ninterrupted. Progress is saved; re-run with the same "
                           "region to resume."))
        return 130
    progress.pct(100.0, f"{total_tiles:,}/{total_tiles:,}")
    progress.done()

    # manifest, using the real on-disk size so resumes stay accurate
    size_bytes = sum(f.stat().st_size for f in tiles_dir.glob("*/*/*") if f.is_file())
    manifest = {
        "id": region_id,
        "name": name,
        "bounds": list(bbox),
        "minZoom": z_min,
        "maxZoom": z_max,
        "format": fmt,
        "tilePath": f"tiles/{{z}}/{{x}}/{{y}}.{fmt}",
        "attribution": attribution,
        "source": url_tpl,
        "tileCount": ctr.done + ctr.skipped,
        "sizeBytes": size_bytes,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (region_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # summary
    mins = (time.time() - t0) / 60.0
    print(nc.c("1", f"\nDone in {mins:.1f} min: ") +
          f"{ctr.done:,} downloaded, {ctr.skipped:,} already present, "
          f"{ctr.failed:,} failed. The region is {nc.human(size_bytes)} on the card.")
    if ctr.failed:
        print(nc.c("31", "  some tiles failed (shown below). Re-run to retry just those:"))
        for f in ctr.failures:
            print(nc.c("31", f"    {f}"))
    elif final_message:
        # suppressed when nomad_route drives this: a routing region is NOT listed
        # on the Maps page, and saying so here would contradict its own sign-off
        print(nc.c("2", f"  Eject safely; '{name}' will appear on nomad's Maps page."))
        nc.hidden_page_note(dest, "maps", "Maps")
    return 2 if ctr.failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download an offline map region onto a nomad card.")
    ap.add_argument("--name", help="region name shown in the Maps page")
    ap.add_argument("--id", dest="region_id", help="folder name under /Maps (default: from name)")
    ap.add_argument("--bbox", help="west,south,east,north")
    ap.add_argument("--zooms", help="min-max zoom levels, e.g. 6-12")
    ap.add_argument("--url", help="custom XYZ URL template with {z}/{x}/{y}")
    ap.add_argument("--attribution", help="attribution text for the map page")
    ap.add_argument("--format", dest="fmt", help="tile file extension (default jpg)")
    ap.add_argument("-o", "--output", help="destination card mountpoint (skips the picker)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before downloading")
    # bboxes west of Greenwich start with "-", which argparse mistakes for a
    # flag, so fold "--bbox -80,..." into "--bbox=-80,..." before parsing
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
            ap.error("--zooms format: min-max, e.g. 6-12")

    return run(dest=args.output, name=args.name, region_id=args.region_id,
               bbox=bbox, zooms=zooms, url_tpl=args.url,
               attribution=args.attribution, fmt=args.fmt, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
