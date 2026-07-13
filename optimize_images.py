#!/usr/bin/env python3
"""
optimize_images — shrink cover art on a nomad card so it loads fast on-device.

Cover images are resized to 200 px wide and saved at 75% JPEG quality, in place.
Only three folders are ever touched — Movies, Shows and Books — and you choose
which of them to run on, so you can skip anything you've already done.

    /Movies  — all subfolders        (posters live per-movie)
    /Shows   — top level only         (one poster per show)
    /Books   — one level deep         (cover per book folder)

⚠  This rewrites the original files. There is no backup. Read the warning.

    ./optimize_images.py                      # fully interactive
    ./optimize_images.py -o /media/me/NOMAD --dirs Movies,Books
    ./optimize_images.py /media/me/NOMAD --all --yes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nomad_common as nc

try:
    from PIL import Image
except ImportError:
    # Almost always a "two Pythons" mismatch: Pillow was installed into a
    # different interpreter than the one running this script. Install it into
    # THIS exact interpreter with its own -m pip.
    exe = sys.executable or "python"
    print(nc.c("1;31", "error: Pillow isn't available to the Python running this tool."))
    print(f"  running: {exe}")
    print("  install Pillow into THAT Python with:")
    print(nc.c("1", f'      "{exe}" -m pip install Pillow'))
    print(nc.c("2", "  (if you 'pip install'ed already, it went to a different Python — the"))
    print(nc.c("2", "   line above targets the right one. On Windows you can also try: py -m pip install Pillow)"))
    sys.exit(1)

IMAGE_EXTS = {".jpg", ".jpeg"}
MAX_WIDTH = 200
JPEG_QUALITY = 75

# The ONLY folders this tool will ever write to, with how deep it recurses:
#   -1 = every subfolder,  0 = that folder only,  1 = one level down.
FOLDERS = [("Movies", -1), ("Shows", 0), ("Books", 1)]
_DEPTH_DESC = {-1: "all subfolders", 0: "top level only", 1: "one level deep"}


def _iter_images(folder: Path, max_depth: int, depth: int = 0):
    """Yield jpg/jpeg files under `folder`, honouring the depth limit."""
    try:
        items = sorted(folder.iterdir())
    except (PermissionError, OSError):
        return
    for item in items:
        if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
            yield item
        elif item.is_dir() and not item.name.startswith("."):
            if max_depth == -1 or depth < max_depth:
                yield from _iter_images(item, max_depth, depth + 1)


def _resize(path: Path) -> str:
    """Resize one image in place. Returns 'ok' | 'skip' | 'error'."""
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w <= MAX_WIDTH:
                return "skip"
            new_h = int((MAX_WIDTH / w) * h)
            img.resize((MAX_WIDTH, new_h), Image.Resampling.LANCZOS).save(
                path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return "ok"
    except Exception as exc:
        print(nc.c("31", f"  error: {path.name}: {exc}"))
        return "error"


def _pick_folders(root: Path, preselect: list[str] | None) -> list[tuple[str, int]]:
    """Return the [(name, depth)] to process — from `preselect` or a menu."""
    present = [(name, depth) for name, depth in FOLDERS if (root / name).is_dir()]
    if not present:
        return []

    if preselect is not None:
        wanted = {p.strip().lower() for p in preselect}
        return [(n, d) for n, d in present if n.lower() in wanted]

    print(nc.c("1", "\nWhich folders should be optimized?"))
    for i, (name, depth) in enumerate(present, 1):
        n_imgs = sum(1 for _ in _iter_images(root / name, depth))
        print(f"  {i}. {name:<8} {nc.c('2', _DEPTH_DESC[depth]):<24} "
              f"{n_imgs} image(s)")
    print(f"  a. All of the above")
    print(f"  0. Cancel")

    while True:
        raw = input("Choose (e.g. 1,3  or  a): ").strip().lower()
        if raw in ("0", ""):
            return []
        if raw in ("a", "all"):
            return present
        picks = {p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()}
        if picks and all(p.isdigit() and 1 <= int(p) <= len(present) for p in picks):
            return [present[int(p) - 1] for p in sorted(picks, key=int)]
        print("  type folder numbers like 1,3 — or 'a' for all.")


def _confirm(root: Path, chosen: list[tuple[str, int]]) -> bool:
    print(nc.c("1;33", "\n⚠  DESTRUCTIVE — images are overwritten in place, no backup."))
    print(f"   card: {root}")
    for name, depth in chosen:
        print(f"   • /{name}  ({_DEPTH_DESC[depth]})  → {MAX_WIDTH}px, {JPEG_QUALITY}% quality")
    while True:
        r = input("Type 'YES' to proceed, anything else to cancel: ").strip()
        return r == "YES"


def run(dest: str | None = None, dirs: list[str] | None = None,
        assume_yes: bool = False) -> int:
    """Interactive image optimize. `dest` skips the card picker; `dirs` the menu."""
    nc.banner("Optimize cover images")

    dest = nc.choose_card(dest, prompt="Which card holds the images?")
    if not dest:
        print("cancelled.")
        return 1
    root = Path(dest)

    chosen = _pick_folders(root, dirs)
    if not chosen:
        print("Nothing selected — nothing to do.")
        return 1

    if not assume_yes and not _confirm(root, chosen):
        print("cancelled.")
        return 1

    progress = nc.Progress()
    grand = {"ok": 0, "skip": 0, "error": 0}
    for name, depth in chosen:
        folder = root / name
        images = list(_iter_images(folder, depth))
        print(nc.c("1;36", f"\n▶ /{name}  ({len(images)} image(s), {_DEPTH_DESC[depth]})"))
        tally = {"ok": 0, "skip": 0, "error": 0}
        for i, img in enumerate(images, 1):
            progress.pct(i * 100.0 / len(images) if images else 100.0, img.name)
            tally[_resize(img)] += 1
        progress.done()
        for k in grand:
            grand[k] += tally[k]
        print(f"  compressed {tally['ok']} · skipped {tally['skip']} · errors {tally['error']}")

    print(nc.c("1", f"\nDone: compressed {grand['ok']}, skipped {grand['skip']}, "
                    f"errors {grand['error']}"))
    return 1 if grand["error"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compress cover images in Movies/Shows/Books on a nomad card.")
    ap.add_argument("card", nargs="?", help="card mountpoint (skips the picker)")
    ap.add_argument("-o", "--output", help="same as the positional card argument")
    ap.add_argument("--dirs", help="comma list to skip the folder menu, e.g. Movies,Books")
    ap.add_argument("--all", action="store_true", help="select all present folders")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    dirs = None
    if args.all:
        dirs = [n for n, _ in FOLDERS]
    elif args.dirs:
        dirs = args.dirs.split(",")
    return run(dest=args.card or args.output, dirs=dirs, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
