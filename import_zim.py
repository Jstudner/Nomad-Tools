#!/usr/bin/env python3
"""
import_zim — put ZIM archives onto a nomad card and build the on-card index.

Point it at a .zim file or a folder of them, pick the destination card, and it
copies each archive into <card>/Archive (auto-splitting anything over FAT32's
4 GB limit) then rebuilds the search index nomad needs to find articles.

The copy / split / index work lives in zim_engine.py (a self-contained port of
nomad Manager's archive code) and the bundled zim/ indexer, so no nomad Manager
install is needed and it runs on Linux, Windows and macOS.

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
    nc.banner("Import ZIM archives")

    if not shutil.which("node") and not shutil.which("nodejs"):
        print(nc.c("1;31", "warning:") + " node isn't on PATH — the index step will fail.")
        print("         install Node.js (or `nvm use`) before continuing.\n")

    # 1. source ---------------------------------------------------------------
    source = source or input("Source .zim file or folder: ").strip()
    zims = find_zims(Path(source))
    if not zims:
        print(nc.c("1;31", f"error: no .zim files found at {source}"))
        return 1
    total = sum(z.stat().st_size for z in zims)
    print(f"\nFound {len(zims)} archive(s), {nc.human(total)} total:")
    for z in zims:
        print(f"  • {z.name}  ({nc.human(z.stat().st_size)})")

    # 2. destination ----------------------------------------------------------
    dest = nc.choose_card(dest, prompt="Which card should these go on?")
    if not dest:
        print("cancelled.")
        return 1
    archive_dir = Path(dest) / "Archive"
    print(f"\nDestination: {nc.c('1', dest)}")
    print(f"  archives land in {archive_dir}")
    try:
        free = shutil.disk_usage(dest).free
        print(f"  free space: {nc.human(free)}   (need ~{nc.human(total)})")
        if free < total:
            print(nc.c("1;31", "  warning: not enough free space for every archive."))
    except OSError:
        pass

    # 3. confirm --------------------------------------------------------------
    if not assume_yes and input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted.")
        return 1

    # 4. import each ----------------------------------------------------------
    progress = nc.Progress()
    failures: list[tuple[str, str]] = []
    for i, z in enumerate(zims, 1):
        print(nc.c("1;36", f"\n[{i}/{len(zims)}] {z.name}"))
        try:
            archives.import_zim(str(z), dest, progress=progress)
            progress.done()
            print(nc.c("1;32", f"  ✓ {z.name} imported"))
        except KeyboardInterrupt:
            progress.done()
            print(nc.c("1;31", "\n  interrupted — the card may hold a partial copy."))
            return 130
        except Exception as exc:
            progress.done()
            print(nc.c("1;31", f"  ✗ {z.name} failed: {exc}"))
            failures.append((z.name, str(exc)))

    # 5. summary --------------------------------------------------------------
    ok = len(zims) - len(failures)
    print(nc.c("1", f"\nDone: {ok}/{len(zims)} archive(s) imported to {dest}"))
    for name, err in failures:
        print(nc.c("31", f"  ✗ {name}: {err}"))
    if not failures:
        print(nc.c("2", "  Eject the card safely, then pop it back into nomad."))
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copy ZIM archive(s) onto a nomad card and build the search index.")
    ap.add_argument("source", nargs="?", help="a .zim file, or a folder of .zim files")
    ap.add_argument("-o", "--output", help="destination card mountpoint (skips the picker)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before writing")
    args = ap.parse_args()
    return run(dest=args.output, source=args.source, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
