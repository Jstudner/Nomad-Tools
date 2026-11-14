#!/usr/bin/env python3
"""
optimize_tiles: shrink a map region's tiles by re-encoding them to WebP.

Map tiles come off the source as JPEG; WebP at quality ~80 stores the same
picture in roughly 40% less space, which matters a lot once a region reaches
tens of thousands of tiles. This walks a region's tiles/ folder, converts each
tile to WebP, drops the old file, and updates manifest.json so the Maps app
loads the new format. It is resumable (already-converted tiles are skipped) and
non-destructive to the routing data.

WebP is supported by every current phone and desktop browser (Chrome, Safari 14+,
Firefox). If you need to serve very old browsers, pass --format jpeg for an
optimized-JPEG pass instead (smaller savings, universal support).

    python optimize_tiles.py                 # pick a card, optimize each region
    python optimize_tiles.py -o /media/CARD --region west-chester --quality 80
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import threading
from pathlib import Path

import nomad_common as nc

try:
    from PIL import Image
except ImportError:
    Image = None

WORKERS = 8
TILE_EXTS = (".jpg", ".jpeg", ".png")


class _Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_in = 0
        self.bytes_out = 0


def _convert_one(src: Path, out_fmt: str, quality: int, ctr: _Counters) -> None:
    dest = src.with_suffix("." + out_fmt)
    if dest.is_file() and dest.stat().st_size > 0 and dest != src:
        with ctr.lock:
            ctr.skipped += 1
        return
    try:
        in_size = src.stat().st_size
        with Image.open(src) as im:
            im = im.convert("RGB")
            if out_fmt == "webp":
                im.save(dest, "WEBP", quality=quality, method=6)
            else:  # optimized jpeg
                im.save(dest, "JPEG", quality=quality, optimize=True)
        out_size = dest.stat().st_size
        if dest != src:
            src.unlink()
        with ctr.lock:
            ctr.done += 1
            ctr.bytes_in += in_size
            ctr.bytes_out += out_size
    except Exception:  # noqa: BLE001  a single bad tile shouldn't stop the run
        with ctr.lock:
            ctr.failed += 1


def optimize_region(region_dir: Path, out_fmt: str = "webp", quality: int = 80,
                    log=print) -> dict:
    """Re-encode one region's tiles and rewrite its manifest. Returns stats."""
    tiles_dir = region_dir / "tiles"
    if not tiles_dir.is_dir():
        log(nc.c("2", f"  {region_dir.name}: no tiles folder, skipping"))
        return {}
    jobs = [p for p in tiles_dir.glob("*/*/*")
            if p.is_file() and p.suffix.lower() in TILE_EXTS]
    if not jobs:
        log(nc.c("2", f"  {region_dir.name}: nothing to convert (it might already be {out_fmt})"))
        # still make sure the manifest matches what's on disk
        _rewrite_manifest(region_dir, out_fmt)
        return {}

    ctr = _Counters()
    progress = nc.Progress()
    progress(f"{region_dir.name}: converting {len(jobs):,} tiles to {out_fmt} "
             f"(quality {quality})")
    total = len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = set()
        for src in jobs:
            pending.add(pool.submit(_convert_one, src, out_fmt, quality, ctr))
            if len(pending) >= WORKERS * 16:
                _, pending = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                n = ctr.done + ctr.skipped + ctr.failed
                progress.pct(100.0 * n / total, f"{n:,}/{total:,}")
        concurrent.futures.wait(pending)
    progress.pct(100.0, f"{total:,}/{total:,}")
    progress.done()

    _rewrite_manifest(region_dir, out_fmt)
    saved = ctr.bytes_in - ctr.bytes_out
    pct = (100.0 * saved / ctr.bytes_in) if ctr.bytes_in else 0.0
    log(nc.c("1", f"  {region_dir.name}: {ctr.done:,} converted, {ctr.skipped:,} skipped, "
             f"{ctr.failed:,} failed"))
    if ctr.bytes_in:
        log(nc.c("1", f"  tiles {nc.human(ctr.bytes_in)} -> {nc.human(ctr.bytes_out)} "
                 f"({pct:.0f}% smaller)"))
    return {"converted": ctr.done, "skipped": ctr.skipped, "failed": ctr.failed,
            "bytes_in": ctr.bytes_in, "bytes_out": ctr.bytes_out, "format": out_fmt}


def _rewrite_manifest(region_dir: Path, out_fmt: str) -> None:
    """Point the manifest at the new tile format and refresh the on-disk size."""
    man_path = region_dir / "manifest.json"
    if not man_path.is_file():
        return
    man = json.loads(man_path.read_text())
    man["format"] = out_fmt
    man["tilePath"] = f"tiles/{{z}}/{{x}}/{{y}}.{out_fmt}"
    tiles_dir = region_dir / "tiles"
    tile_bytes = sum(f.stat().st_size for f in tiles_dir.glob("*/*/*") if f.is_file())
    routing_bytes = 0
    rdir = region_dir / "routing"
    if rdir.is_dir():
        routing_bytes = sum(f.stat().st_size for f in rdir.glob("*") if f.is_file())
    man["sizeBytes"] = tile_bytes + routing_bytes
    man_path.write_text(json.dumps(man, indent=2))


def run(dest: str | None = None, region: str | None = None,
        out_fmt: str = "webp", quality: int = 80, assume_yes: bool = False) -> int:
    nc.banner("Shrink map tiles")
    if Image is None:
        print(nc.c("1;31", "error: this needs Pillow (pip install Pillow)."))
        return 1

    dest = nc.choose_card(dest, prompt="Which card has the maps to shrink?")
    if not dest:
        print("Cancelled.")
        return 1
    maps_dir = Path(dest) / "Maps"
    if not maps_dir.is_dir():
        print(nc.c("1;31", f"No Maps folder on {dest}."))
        return 1

    regions = ([maps_dir / region] if region
               else [p for p in sorted(maps_dir.iterdir())
                     if p.is_dir() and (p / "manifest.json").is_file()])
    regions = [r for r in regions if r.is_dir()]
    if not regions:
        print(nc.c("1;31", "No map areas in the Maps folder."))
        return 1

    # Re-encoding replaces each tile and deletes the original. That is lossy on
    # top of lossy, and putting a big region back means downloading it again, so
    # ask first. optimize_images does the same for cover art.
    print(f"\nShrinking {len(regions)} map area(s) to {out_fmt} at quality {quality}:")
    for r in regions:
        print(f"   - {r.name}")
    print(nc.c("1;33", "  This replaces the tiles and deletes the originals. You can't undo it."))
    print(nc.c("2", "  Getting a big area back means downloading it again, which can take hours."))
    if not assume_yes and input("\nGo ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1

    total_in = total_out = 0
    for r in regions:
        stats = optimize_region(r, out_fmt, quality)
        total_in += stats.get("bytes_in", 0)
        total_out += stats.get("bytes_out", 0)
    if total_in:
        saved = total_in - total_out
        print(nc.c("1", f"\nAll done: tiles {nc.human(total_in)} -> {nc.human(total_out)}, "
                 f"saved {nc.human(saved)} ({100.0*saved/total_in:.0f}%)."))
    print(nc.c("2", "  Eject the card safely. The Maps page picks up the new tiles by itself."))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Shrink a map area's tiles by converting them to WebP. Replaces the originals.")
    ap.add_argument("-o", "--output", help="where the card is mounted (skips the card picker)")
    ap.add_argument("--region", help="just this map area's folder name (default: every area in /Maps)")
    ap.add_argument("--format", dest="out_fmt", default="webp", choices=["webp", "jpeg"],
                    help="format to convert to (default webp)")
    ap.add_argument("--quality", type=int, default=80, help="picture quality 1-100 (default 80)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before replacing the tiles")
    args = ap.parse_args()
    return run(dest=args.output, region=args.region, out_fmt=args.out_fmt,
               quality=args.quality, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except EOFError:
        print("\nNo input, stopping.")
        sys.exit(1)
