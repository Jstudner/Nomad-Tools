#!/usr/bin/env python3
"""
optimize_images: shrink the cover art in Movies, Shows and Books, in place.

This is the "Shrink covers" job of pictures.py (Pictures in the menu), kept so
the commands below still work. pictures.py also makes
Gallery previews, covers for books that have none, and stills for films.

    ./optimize_images.py                      # pick a card, confirm
    ./optimize_images.py -o /media/me/NOMAD --dirs Movies,Books
    ./optimize_images.py /media/me/NOMAD --all --yes
"""
from __future__ import annotations

import argparse
import sys

import pictures


def run(dest: str | None = None, dirs: list[str] | None = None,
        assume_yes: bool = False) -> int:
    return pictures.run(dest=dest, jobs=["shrink"], assume_yes=assume_yes, shrink_dirs=dirs)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shrink the covers in Movies, Shows and Books on a Nomad card. Replaces them in place.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    ap.add_argument("-o", "--output", help="same as giving the card on its own")
    ap.add_argument("--dirs", help="folders split by commas, like Movies,Books (all three if you leave it out)")
    ap.add_argument("--all", action="store_true", help="all three folders (this is the default)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before replacing them")
    args = ap.parse_args()
    dirs = args.dirs.split(",") if args.dirs and not args.all else None
    return run(dest=args.card or args.output, dirs=dirs, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
