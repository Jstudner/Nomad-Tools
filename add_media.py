#!/usr/bin/env python3
"""
add_media: copy any kind of media onto a Nomad card, and check what the device
will show.

Each page has a fixed list of extensions and a fixed depth it reads to.
Anything outside that is indexed but never displayed, and nothing says why.
A file that copied on but never shows up is almost always one of these:

    the extension isn't on that page's list
    the file sits at a depth the page doesn't read
    the page lists folders and the file was left loose (Shows)
    the media index hasn't been rebuilt since the copy

`add` checks each file before it copies it and puts it where the page expects.
`check` walks a card you already filled and says, per folder, what will show
up, what won't, and why.

    python add_media.py                        # fully interactive
    python add_media.py ~/Downloads/Dune.mp4   # pick the folder, choose a card
    python add_media.py ~/rips --to Shows -o /media/me/NOMAD --yes
    python add_media.py --check                # report only, copies nothing

The rules live in media_types.py, transcribed from the card's own pages.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import nomad_common as nc
import media_types as mt
import build_index
import pictures


def filesystem_of(path: str | Path) -> str | None:
    """Best-effort filesystem name for whatever `path` lives on, else None.

    Only used to warn before a >4 GB copy that FAT32 will refuse. When it can't
    tell, the warning says "if this is FAT32" instead.
    """
    path = str(Path(path).resolve())
    try:
        if sys.platform.startswith("linux"):
            best, fs = "", None
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    bits = line.split()
                    if len(bits) < 3:
                        continue
                    mnt = bits[1].replace("\\040", " ")
                    if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) \
                            and len(mnt) > len(best):
                        best, fs = mnt, bits[2]
            return fs
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
            best, fs = "", None
            for line in out.splitlines():
                if " on " not in line or "(" not in line:
                    continue
                mnt = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) \
                        and len(mnt) > len(best):
                    best = mnt
                    fs = line.rsplit("(", 1)[1].split(",")[0].strip(") ")
            return fs
        if os.name == "nt":
            import ctypes
            drive = os.path.splitdrive(path)[0] + "\\"
            buf = ctypes.create_unicode_buffer(64)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive), None, 0, None, None, None, buf, 64)
            return buf.value if ok else None
    except Exception:                                        # noqa: BLE001
        return None
    return None


def is_fat32(fs: str | None) -> bool:
    return bool(fs) and fs.lower() in ("vfat", "fat32", "msdos", "fat")


# verdicts, in the order a report should show them
SHOWN = "shown"          # the page will list this
COMPANION = "companion"  # kept, attaches to something else (subtitle, notes)
ART = "art"              # cover image for a neighbour
BUILTIN = "builtin"      # the built-in browser games
SKIPPED = "skipped"      # copied fine, but the page will never show it


def classify(bucket: mt.Bucket, name: str, depth: int) -> tuple[str, str]:
    """Return (verdict, reason) for a file `depth` levels below the bucket."""
    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name[1:] else ""
    stem = name.rsplit(".", 1)[0] if ext else name

    if name.startswith(".") or name.startswith("$"):
        return SKIPPED, "hidden files are skipped"

    if depth > bucket.scan_depth:
        return SKIPPED, (f"{depth} folders deep. The {bucket.folder} page only looks "
                         f"{bucket.scan_depth} level(s) down")

    # Games is the one page that decides by emulator, not by extension list
    if bucket.folder == "Games":
        if ext in (".html", ".htm"):
            return BUILTIN, "a built-in game"
        if ext in bucket.covers:
            return ART, "cover art"
        core = mt.rom_core(name)
        if core:
            return SHOWN, f"runs on the {core} core"
        if ext in mt.ROM_WRAPPERS:
            return SKIPPED, (f"a zip with no console in the name. Rename it to something "
                             f"like {stem}.nes{ext} so Nomad knows what it's for")
        return SKIPPED, "no emulator for this kind of file"

    if bucket.folder == "Cookbook" and ext in mt.RECIPE and not mt.is_recipe(stem):
        return COMPANION, "notes for the folder, kept but not listed as a recipe"

    if ext in bucket.media or not bucket.media:
        return SHOWN, ""
    if ext in bucket.covers:
        return ART, "cover art"
    if ext in bucket.companions:
        return COMPANION, "goes with the file it's named after"
    if not ext:
        return SKIPPED, "no file extension, so no page picks it up"
    return SKIPPED, f"the {bucket.folder} page doesn't show {ext} files"


def walk_bucket(root: Path, bucket: mt.Bucket):
    """Yield (relative_path, depth, verdict, reason) for everything in a bucket."""
    base = root / bucket.folder
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        rel_dir = here.relative_to(base)
        depth = len(rel_dir.parts)
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and not d.startswith("$"))
        # An unpacked comic (3+ loose page images, no ebook/audio file) is one
        # item and the indexer never descends into it. build_index's detector
        # mirrors the firmware exactly, so use that. A folder holding a .cbz is
        # an ordinary Books subfolder and its contents do get listed.
        if bucket.folder == "Books" and depth >= 1 and build_index.is_comic_folder(here):
            dirnames[:] = []
            yield rel_dir, depth - 1, SHOWN, "an unpacked comic, opened as one item"
            continue
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("$"):
                continue      # hidden, like the folders above: Nomad's own .nomad-* files live here
            verdict, reason = classify(bucket, name, depth)
            yield rel_dir / name, depth, verdict, reason


def _index_is_stale(root: Path, newest_media: float | None = None) -> tuple[bool, str]:
    """True when media has been added since the index was last written.

    `newest_media` lets a caller that has already walked the card hand over the
    newest mtime it saw. Without it this walks every bucket again, which is slow
    on a full card over a USB reader.
    """
    index_dir = root / ".system-index"
    if not index_dir.is_dir():
        return True, "it's never been built on this card"
    try:
        newest_index = max((p.stat().st_mtime for p in index_dir.glob("*.ndjson")),
                           default=0.0)
    except OSError:
        return True, "it couldn't be read"
    if not newest_index:
        return True, "it's empty"
    if newest_media is None:
        newest_media = 0.0
        for bucket in mt.BUCKETS:
            base = root / bucket.folder
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    try:
                        newest_media = max(newest_media,
                                           (Path(dirpath) / name).stat().st_mtime)
                    except OSError:
                        pass
    if newest_media > newest_index + 1:
        return True, "files were added since it was last built"
    return False, ""



def _check_maps(root: Path) -> list[tuple[str, str]]:
    """Report /Maps the way the device reads it: folders holding a manifest.json."""
    base = root / "Maps"
    if not base.is_dir():
        return []
    out, regions, routing, bad = [], 0, 0, []
    packs, stores = 0, set()             # map packs, and the shared folders they keep tiles in
    no_manifest = []
    for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        man = d / "manifest.json"
        if not man.is_file():
            no_manifest.append(d.name)
            continue
        regions += 1
        try:
            m = json.loads(man.read_text())
            if m.get("type") == "pack":
                # a map pack: its picture is a .pmtiles file and/or squares in a shared store
                packs += 1
                f = (m.get("map") or {}).get("file")
                if f and not (d / f).is_file():
                    bad.append(f"{d.name} ({f} is missing)")
                bl = m.get("blocks")
                if bl:
                    store = base / Path(str(bl.get("dir", "/Maps/blocks"))).name
                    stores.add(store.name)
                    gone = sum(1 for x, y in bl.get("tiles", [])
                               if not (store / str(bl.get("zoom")) / str(x) / f"{y}.pmtiles").is_file())
                    if gone:
                        bad.append(f"{d.name} ({gone} map square(s) missing from {store.name})")
                continue
            # maps.html skips any region carrying its own app
            if m.get("type") == "streetzim" or m.get("app"):
                routing += 1
            tile_path = m.get("tilePath", "tiles/{z}/{x}/{y}.jpg")
            suffix = tile_path.rsplit(".", 1)[-1].lower()
            got = any(base.glob(f"{d.name}/tiles/*/*/*.{suffix}"))
            if not got:
                bad.append(f"{d.name} (no .{suffix} tiles found)")
        except (OSError, ValueError) as exc:
            bad.append(f"{d.name} (can't read its manifest: {exc})")
    bad = [n for n in no_manifest if n not in stores] + bad
    shown = regions - routing
    out.append((f"  /{'Maps':<9} {shown:>4} shown"
                + (nc.c("2", f"   {packs} map pack(s)") if packs else "")
                + (nc.c("2", f"   {routing} with their own page, not in the Maps list") if routing else ""), ""))
    for b in bad[:4]:
        out.append((nc.c("33", f"       not a map area: {b}"),
                    f"/Maps/{b}: a map area needs a manifest.json and its tiles or map files."))
    if regions > 64:
        out.append((nc.c("33", f"       {regions} map areas"),
                    "Nomad only lists 64 map areas. The rest won't show."))
    return out


def _check_archive(root: Path) -> list[tuple[str, str]]:
    """Report /Archive: .zim files are useless to the device without their index."""
    base = root / "Archive"
    if not base.is_dir():
        return []
    zims = sorted(p for p in base.iterdir()
                  if p.is_file() and p.name.lower().endswith(".zim"))
    parts = [p for p in base.iterdir()
             if p.is_file() and ".zim" in p.name.lower() and not p.name.lower().endswith(".zim")]
    out = [(f"  /{'Archive':<9} {len(zims) + (1 if parts else 0):>4} shown"
            + (nc.c("2", f"   {len(parts)} split part(s)") if parts else ""), "")]
    if not (zims or parts):
        return out
    fix = ("Run 'Add ZIM archives' and point it at the card's /Archive folder, or "
           "the Archive page can't search them.")
    # the index lives in /Archive/.nomad-zim, and its manifest lists every file it read
    try:
        man = json.loads((base / ".nomad-zim" / "manifest.json").read_text(encoding="utf-8"))
        covered = {Path(p.get("path", "")).name for a in man.get("archives") or []
                   for p in a.get("parts") or []}
    except (OSError, ValueError, AttributeError):
        out.append((nc.c("33", "       no search set up for these"),
                    "/Archive has archives with no search set up. " + fix))
        return out
    missing = [p.name for p in zims + parts if p.name not in covered]
    if missing:
        out.append((nc.c("33", f"       not in the search: {', '.join(missing[:3])}"
                               + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")),
                    "/Archive has archives that were added after the search was built. " + fix))
    return out


def run_check(dest: str | None = None) -> int:
    """Walk a filled-in card and report what each page will and will not show."""
    nc.banner("Check the card")

    dest = nc.choose_card(dest, prompt="Which card should I check?")
    if not dest:
        print("Cancelled.")
        return 1
    root = Path(dest)
    fs = filesystem_of(dest)
    print(f"\nCard: {nc.c('1', dest)}" + (f"   format: {fs}" if fs else "") + "\n")

    problems: list[str] = []
    any_media = False
    newest_media = 0.0          # fed to the staleness check, so the card is walked once

    for bucket in mt.BUCKETS:
        base = root / bucket.folder
        if not base.is_dir():
            continue
        counts = {SHOWN: 0, ART: 0, COMPANION: 0, BUILTIN: 0, SKIPPED: 0}
        skipped: list[tuple[Path, str]] = []
        loose: list[Path] = []

        for rel, depth, verdict, reason in walk_bucket(root, bucket):
            counts[verdict] += 1
            try:
                newest_media = max(newest_media, (base / rel).stat().st_mtime)
            except OSError:
                pass
            if verdict == SKIPPED:
                skipped.append((rel, reason))
            if verdict == SHOWN:
                any_media = True
                if bucket.folders_only and depth == 0:
                    loose.append(rel)

        # a loose file in a folders-only bucket was counted as shown, but the
        # page will not list it. Report what actually appears.
        shown = counts[SHOWN] + counts[BUILTIN] - len(loose)
        head = f"  /{bucket.folder:<9} {shown:>4} shown"
        extras = []
        if counts[ART]:
            extras.append(f"{counts[ART]} cover")
        if counts[COMPANION]:
            extras.append(f"{counts[COMPANION]} companion")
        if counts[SKIPPED]:
            extras.append(nc.c("33", f"{counts[SKIPPED]} not shown"))
        print(head + (("   " + ", ".join(extras)) if extras else ""))

        if loose:
            problems.append(
                f"/{bucket.folder}: {len(loose)} file(s) are sitting loose in the folder. The "
                f"{bucket.folder} page only lists folders, so these won't appear. "
                f"Put each show in its own folder.")
            for rel in loose[:4]:
                print(nc.c("33", f"       loose: {rel}"))
            if len(loose) > 4:
                print(nc.c("2", f"       ...and {len(loose) - 4} more"))

        for rel, reason in skipped[:4]:
            print(nc.c("2", f"       skipped: {rel}  ({reason})"))
        if len(skipped) > 4:
            print(nc.c("2", f"       ...and {len(skipped) - 4} more not shown"))

    # Maps and Archive need more than a copy, so they aren't buckets
    for line, issue in _check_maps(root) + _check_archive(root):
        print(line)
        if issue:
            problems.append(issue)

    if not any_media:
        print(nc.c("33", "\n  No media found in any folder yet."))

    if is_fat32(fs):
        problems.append("This card is FAT32, so it can't hold a file of 4 GB or more. "
                        "Most films are fine, but a long one in high quality won't fit. "
                        "Use 'Format a card' to make it exFAT if you run into that.")

    stale, why = _index_is_stale(root, newest_media)
    print()
    if stale:
        print(nc.c("1;33", f"  The media index is out of date, {why}."))
        print(nc.c("2", "  Nothing new shows up on Nomad until it's rebuilt."))
        problems.append("Run 'Rebuild the media index' before you eject the card.")
    else:
        print(nc.c("32", "  The media index is up to date."))

    if problems:
        print(nc.c("1", "\nWorth fixing:"))
        for p in problems:
            print(f"  - {p}")
    else:
        print(nc.c("32", "\nEverything looks good. Eject the card safely and put it back in Nomad."))
    return 0


def _pick_bucket(preselect: str | None) -> mt.Bucket | None:
    _ELSEWHERE = {
        "archive": "ZIM archives need their search built too. Use 'Add ZIM "
                   "archives' (python3 import_zim.py).",
        "maps": "Maps need more than a copy. Use 'Download map packs' or "
                "'Download map tiles' (python3 map_tiles.py).",
    }
    if preselect:
        key = preselect.strip().strip("/").lower()
        if key in _ELSEWHERE:
            print(nc.c("1;33", f"  /{key.title()} can't just be copied. {_ELSEWHERE[key]}"))
            return None
        b = mt.bucket_for(preselect)
        if not b:
            names = ", ".join(x.folder for x in mt.BUCKETS)
            print(nc.c("1;31", f"'{preselect}' isn't a card folder. Try one of these: {names}"))
        return b
    print(nc.c("1", "\nWhere on the card should this go?"))
    for i, b in enumerate(mt.BUCKETS, 1):
        print(f"  {i:>2}. {nc.c('1', b.folder):<20} {nc.c('2', b.summary)}")
    print(nc.c("2", "\n   ZIM archives and maps have their own menu items,"))
    print(nc.c("2", "   because they need more than a copy."))
    while True:
        raw = input("Choose [1-%d, 0 to cancel]: " % len(mt.BUCKETS)).strip()
        if raw in ("0", "q", ""):
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(mt.BUCKETS):
            return mt.BUCKETS[int(raw) - 1]
        b = mt.bucket_for(raw)
        if b:
            return b
        print(f"  Type a number from 1 to {len(mt.BUCKETS)}, a folder name, or 0.")


def _describe(bucket: mt.Bucket) -> None:
    print(nc.c("1", f"\n/{bucket.folder}"), nc.c("2", f" {bucket.summary}"))
    print(f"  layout:  {bucket.layout}")
    if bucket.media:
        exts = " ".join(sorted(e.lstrip('.') for e in bucket.media))
        print(f"  accepts: {exts}")
    if bucket.cover:
        print(f"  art:     {bucket.cover}")
    for note in bucket.notes:
        print(nc.c("2", f"  note:    {note}"))


def _gather(source: Path) -> list[tuple[Path, Path]]:
    """[(absolute source file, path relative to what the user pointed at)]."""
    source = source.expanduser()
    if source.is_file():
        return [(source, Path(source.name))]
    if source.is_dir():
        out = []
        for dirpath, dirnames, filenames in os.walk(source):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                p = Path(dirpath) / name
                out.append((p, p.relative_to(source)))
        return out
    return []


def run(dest: str | None = None, source: str | None = None,
        to: str | None = None, assume_yes: bool = False) -> int:
    """Interactive media import. `dest`/`source`/`to` skip the matching prompt."""
    nc.banner("Add media")

    # 1. source
    source = source or input("File or folder to add: ").strip().strip('"').strip("'")
    if not source:
        print("Cancelled.")
        return 1
    src = Path(source).expanduser()
    items = _gather(src)
    if not items:
        print(nc.c("1;31", f"There's nothing to copy at {source}"))
        return 1

    # 2. where it goes
    bucket = _pick_bucket(to)
    if not bucket:
        print("Cancelled.")
        return 1
    _describe(bucket)

    # 3. what the device will make of it
    plan: list[tuple[Path, Path, str, str]] = []   # src, rel, verdict, reason
    for abs_path, rel in items:
        depth = len(rel.parts) - 1
        verdict, reason = classify(bucket, rel.name, depth)
        plan.append((abs_path, rel, verdict, reason))

    shown = [p for p in plan if p[2] in (SHOWN, BUILTIN)]
    helpers = [p for p in plan if p[2] in (ART, COMPANION)]
    skipped = [p for p in plan if p[2] == SKIPPED]
    total = sum(p[0].stat().st_size for p in plan)

    print(nc.c("1", f"\n{len(plan)} file(s), {nc.human(total)} total:"))
    print(f"  {len(shown):>4} will show on the {bucket.folder} page")
    if helpers:
        print(f"  {len(helpers):>4} cover art and companions")
    if skipped:
        print(nc.c("33", f"  {len(skipped):>4} will copy but won't show up:"))
        for _, rel, _, reason in skipped[:6]:
            print(nc.c("2", f"         {rel}  ({reason})"))
        if len(skipped) > 6:
            print(nc.c("2", f"         ...and {len(skipped) - 6} more"))
    if not shown:
        print(nc.c("1;31", "\n  None of this would show up on Nomad."))
        if not assume_yes and input("  Copy it anyway? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            print("Stopped, nothing was copied.")
            return 1

    # Shows is folders-only: offer to wrap loose episodes in a folder
    wrap: str | None = None
    if bucket.folders_only and any(len(rel.parts) == 1 for _, rel, v, _ in plan if v == SHOWN):
        default = src.stem if src.is_file() else src.name
        print(nc.c("1;33", f"\n  The {bucket.folder} page only lists folders."))
        print(nc.c("2", "  Files copied straight into it won't show up."))
        if assume_yes:
            wrap = default
        else:
            raw = input(f"  Put them in a folder named [{default}] (blank to accept, "
                        f"'-' to copy as-is): ").strip()
            wrap = default if not raw else (None if raw == "-" else raw)
        if wrap:
            print(nc.c("2", f"  -> /{bucket.folder}/{wrap}/"))

    # 4. destination
    dest = nc.choose_card(dest, prompt="Which card should this go on?")
    if not dest:
        print("Cancelled.")
        return 1
    root = Path(dest)
    target = root / bucket.folder / wrap if wrap else root / bucket.folder
    print(f"\nDestination: {nc.c('1', str(target))}")

    fs = filesystem_of(dest)
    oversize = [(rel, p.stat().st_size) for p, rel, _, _ in plan
                if p.stat().st_size >= mt.FAT32_MAX]
    if oversize:
        if is_fat32(fs):
            print(nc.c("1;31", f"  This card is {fs}, which can't hold a file of 4 GB "
                               f"or more. These won't copy:"))
        else:
            print(nc.c("1;33", "  Files of 4 GB or more need an exFAT card. If this one "
                               "is FAT32, these won't copy:"))
        for rel, size in oversize[:4]:
            print(nc.c("33", f"         {rel}  ({nc.human(size)})"))

    try:
        free = shutil.disk_usage(dest).free
        print(f"  Free space: {nc.human(free)}   (needs about {nc.human(total)})")
        if free < total:
            print(nc.c("1;31", "  There isn't enough free space for all of it."))
    except OSError:
        pass

    # 5. confirm
    if not assume_yes and input("\nGo ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Stopped, nothing was copied.")
        return 1

    # 6. copy
    progress = nc.Progress()
    failures: list[tuple[str, str]] = []
    copied = 0
    done_bytes = 0
    for abs_path, rel, _, _ in plan:
        out = target / rel
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, out)
            copied += 1
            done_bytes += abs_path.stat().st_size
            progress.pct(100.0 * done_bytes / total if total else 100.0,
                         f"{copied}/{len(plan)}  {rel}")
        except KeyboardInterrupt:
            progress.done()
            print(nc.c("1;31", "\n  Stopped. The last file on the card may only be half copied."))
            return 130
        except OSError as exc:
            failures.append((str(rel), str(exc)))
    progress.done()

    print(nc.c("1", f"\nCopied {copied}/{len(plan)} file(s) to {target}"))
    for name, err in failures:
        print(nc.c("31", f"  x {name}: {err}"))

    # small previews for the Gallery grid, so it doesn't read every whole photo
    if bucket.folder == "Gallery" and copied:
        pictures.make_gallery_previews(root)

    # 7. the index
    print(nc.c("1;33", "\n  Nothing new shows up on Nomad until the media index "
                       "is rebuilt."))
    if assume_yes or input("  Rebuild it now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        build_index.run(dest=dest)
    else:
        print(nc.c("2", "  Run 'Rebuild the media index' before you eject."))
    nc.hidden_page_note(dest, bucket.folder.lower(), bucket.folder)
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Copy media onto a Nomad card, in the folders Nomad expects.")
    ap.add_argument("source", nargs="?", help="a file, or a folder of files")
    ap.add_argument("--to", help="card folder to copy into, like Movies, Shows or Games")
    ap.add_argument("-o", "--output", help="where the card is mounted (skips the card picker)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before copying")
    ap.add_argument("--check", action="store_true",
                    help="list what Nomad will show from the card, without changing anything")
    ap.add_argument("--rules", action="store_true",
                    help="show every folder's file types and layout, then stop")
    args = ap.parse_args()

    if args.rules:
        for b in mt.BUCKETS:
            _describe(b)
        return 0
    if args.check:
        return run_check(dest=args.output)
    return run(dest=args.output, source=args.source, to=args.to, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except EOFError:                     # stdin closed or piped: not a crash
        print("\nNo input, stopping.")
        sys.exit(1)
