#!/usr/bin/env python3
"""
import_zim: put ZIM archives onto a Nomad card and build the on-card index.

Point it at a .zim file or a folder of them, pick the destination card, and it
copies each archive into <card>/Archive (splitting anything over FAT32's 4 GB
limit), then rebuilds the search index Nomad needs to find articles.

The copy, split and index work lives in zim_engine.py (a self-contained port of
Nomad Manager's archive code) and the bundled zim/ indexer, so it doesn't need
Nomad Manager installed and runs on Linux, Windows and macOS.

    python import_zim.py                    # fully interactive
    python import_zim.py ~/Downloads        # pick source folder, choose card
    python import_zim.py wiki.zim -o E:\\ --yes

Requires: Node.js on PATH (the indexer runs on Node).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import nomad_common as nc
import zim_engine as archives   # self-contained copy/split/index (was sd/archives.py)


def find_zims(source: Path) -> list[Path]:
    """Every whole .zim under `source` (a file or a directory)."""
    source = source.expanduser()
    if source.is_file():
        return [source]
    if source.is_dir():
        # Whole .zim files only; kiwix split parts end in .zimaa/.zimab etc.
        return sorted(p for p in source.iterdir()
                      if p.is_file() and p.name.lower().endswith(".zim"))
    return []


def run(dest: str | None = None, source: str | None = None, assume_yes: bool = False) -> int:
    """Interactive ZIM import. `dest`/`source` skip the matching prompt."""
    nc.banner("Add ZIM archives")

    if not shutil.which("node") and not shutil.which("nodejs"):
        print(nc.c("1;31", "warning:") + " Node.js isn't installed (or isn't on PATH), so the search can't be built.")
        print("         Install Node.js first, then run this again.\n")

    # 1. source
    source = source or input(".zim file or folder to add: ").strip()
    zims = find_zims(Path(source))
    if not zims:
        print(nc.c("1;31", f"error: no .zim files at {source}"))
        return 1
    total = sum(z.stat().st_size for z in zims)
    print(f"\nFound {len(zims)} archive(s), {nc.human(total)} total:")
    for z in zims:
        print(f"  • {z.name}  ({nc.human(z.stat().st_size)})")

    # 2. destination
    dest = nc.choose_card(dest, prompt="Which card should these go on?")
    if not dest:
        print("Cancelled.")
        return 1
    archive_dir = Path(dest) / "Archive"
    print(f"\nDestination: {nc.c('1', dest)}")
    print(f"  archives go in {archive_dir}")
    # archives already in this card's /Archive are indexed where they are, not copied
    to_copy = sum(z.stat().st_size for z in zims
                  if z.resolve().parent != archive_dir.resolve())
    try:
        free = shutil.disk_usage(dest).free
        print(f"  free space: {nc.human(free)}   (needs about {nc.human(to_copy)})")
        if free < to_copy:
            print(nc.c("1;31", "  warning: there isn't enough free space for all of them."))
    except OSError:
        pass

    # 3. speed up? then confirm
    print("\nThey can be sped up while they copy, so Nomad loads them faster for everyone at once.")
    print("Same pages and search, they just take a little more space, and Kiwix still opens them.")
    print("It takes longer than a plain copy (all of Wikipedia takes hours).")
    prepare = True if assume_yes else input("Speed them up? [Y/n] ").strip().lower() not in ("n", "no")
    if not assume_yes and input("\nGo ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1

    # 4. import each
    progress = nc.Progress()
    failures: list[tuple[str, str]] = []
    for i, z in enumerate(zims, 1):
        print(nc.c("1;36", f"\n[{i}/{len(zims)}] {z.name}"))
        try:
            # the search index is built once, after the last one
            archives.import_zim(str(z), dest, progress=progress, prepare=prepare, index=False)
            progress.done()
            print(nc.c("1;32", f"  ✓ {z.name} added"))
        except KeyboardInterrupt:
            progress.done()
            print(nc.c("1;31", "\n  Stopped. The card may have a half copied archive on it."))
            return 130
        except Exception as exc:
            progress.done()
            print(nc.c("1;31", f"  ✗ {z.name} failed: {exc}"))
            failures.append((z.name, str(exc)))

    if len(failures) < len(zims):
        print(nc.c("1;36", "\nBuilding the search index"))
        try:
            archives.index_archives(dest, progress)
            progress.done()
        except Exception as exc:
            progress.done()
            print(nc.c("1;31", f"  ✗ building the search failed: {exc}"))
            failures.append(("search index", str(exc)))

    # 5. summary
    ok = len(zims) - len(failures)
    print(nc.c("1", f"\nDone: {ok}/{len(zims)} archive(s) added to {dest}"))
    for name, err in failures:
        print(nc.c("31", f"  ✗ {name}: {err}"))
    if not failures:
        print(nc.c("2", "  Eject the card safely, then pop it back into Nomad."))
        nc.hidden_page_note(dest, "archive", "Archive")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copy ZIM archives onto a Nomad card and build their search.")
    ap.add_argument("source", nargs="?", help="a .zim file, or a folder of .zim files")
    ap.add_argument("-o", "--output", help="where the card is mounted (skips the card picker)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before writing")
    args = ap.parse_args()
    return run(dest=args.output, source=args.source, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
