#!/usr/bin/env python3
"""
games_prepare: keep the games on a card gzipped, so they start sooner on Nomad.

A game's first start downloads the whole ROM to the phone (later starts use the copy
the browser kept). Over Nomad's WiFi that is about 0.5 MB/s, so a 16 MB Game Boy
Advance game took 34 s before it ran. Gzipped it is 7 MB, about 14 s.

Each ROM in /Games becomes Game.gba.gz and the original goes. The Games page still
lists Game.gba and asks for it by that name. Nomad sends the gzipped copy and the
browser unpacks it, so the game, its cover and the saves on people's phones are the
same as before. A ROM that saves less than 10 % is left as it is (and remembered, so
it isn't tried again).

Needs Nomad firmware 4.8 or newer, which sends the gzipped copies. On an older
Nomad these games won't start, and --undo puts every ROM back as it was.

    ./games_prepare.py                          # pick a card, see the list, confirm
    ./games_prepare.py /media/me/NOMAD --check  # just the list
    ./games_prepare.py /media/me/NOMAD --yes
    ./games_prepare.py /media/me/NOMAD --undo
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import media_types as mt
import nomad_common as nc

MIN_SAVING = 0.10
# archives already: gzip would only add a few bytes
PACKED = {".zip", ".7z", ".rar", ".gz"}
SKIP_FILE = ".nomad-games.json"   # {path under /Games: size} of ROMs that don't shrink


def _roms(games: Path):
    """ROMs the Games page runs that aren't packed already."""
    for dirpath, dirnames, filenames in os.walk(games):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for n in sorted(filenames):
            p = Path(dirpath) / n
            if n.startswith(".") or p.suffix.lower() in PACKED or not mt.rom_core(n):
                continue
            yield p


def _gzipped(games: Path):
    for dirpath, dirnames, filenames in os.walk(games):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for n in sorted(filenames):
            if n.lower().endswith(".gz") and mt.rom_core(n[:-3]):
                yield Path(dirpath) / n


def _load_skip(games: Path) -> dict:
    try:
        d = json.loads((games / SKIP_FILE).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_skip(games: Path, skip: dict) -> None:
    tmp = games / (SKIP_FILE + ".nomad-tmp")
    tmp.write_text(json.dumps(dict(sorted(skip.items())), ensure_ascii=False), "utf-8")
    os.replace(tmp, games / SKIP_FILE)


def _sha(path: Path, opener=open) -> str:
    h = hashlib.sha256()
    with opener(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _pack(rom: Path) -> tuple[str, int, int]:
    """Gzip one ROM beside itself, read it back, and swap it in. ('packed'|'kept', before, after)."""
    gz = rom.with_name(rom.name + ".gz")
    tmp = rom.with_name(rom.name + ".gz.nomad-tmp")
    before = rom.stat().st_size
    try:
        with open(rom, "rb") as src, open(tmp, "wb") as raw, \
                gzip.GzipFile(filename=rom.name, mode="wb", fileobj=raw, compresslevel=9, mtime=0) as out:
            shutil.copyfileobj(src, out, 1 << 20)
        after = tmp.stat().st_size
        if after > before * (1 - MIN_SAVING):
            return "kept", before, after
        # read back from the card before the original goes
        if _sha(tmp, gzip.open) != _sha(rom):
            raise OSError("the compressed copy didn't match the original, so it was left alone")
        os.replace(tmp, gz)
        rom.unlink()
        return "packed", before, after
    finally:
        if tmp.exists():
            tmp.unlink()


def _unpack(gz: Path) -> tuple[int, int]:
    rom = gz.with_name(gz.name[:-3])
    tmp = rom.with_name(rom.name + ".nomad-tmp")
    try:
        with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        os.replace(tmp, rom)
        size = gz.stat().st_size
        gz.unlink()
        return size, rom.stat().st_size
    finally:
        if tmp.exists():
            tmp.unlink()


def run(dest: str | None = None, assume_yes: bool = False, check: bool = False, undo: bool = False) -> int:
    nc.banner("Speed up games" if not undo else "Speed up games: undo")
    dest = nc.choose_card(dest, prompt="Which card are the games on?")
    if not dest:
        print("Cancelled.")
        return 1
    games = Path(dest) / "Games"
    if not games.is_dir():
        print("This card doesn't have a /Games folder.")
        return 1

    if undo:
        packed = list(_gzipped(games))
        print(f"  {len(packed)} compressed game(s)")
        if check or not packed:
            return 0
        if not assume_yes and input("  Put them all back as they were? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
        progress = nc.Progress()
        for i, gz in enumerate(packed, 1):
            progress.pct(100.0 * i / len(packed), gz.name)
            _unpack(gz)
        progress.done()
        print(nc.c("1", f"\nDone: {len(packed)} game(s) back as they were."))
        return 0

    skip = _load_skip(games)
    todo = [r for r in _roms(games) if skip.get(r.relative_to(games).as_posix()) != r.stat().st_size]
    total = sum(r.stat().st_size for r in todo)
    done_already = sum(1 for _ in _gzipped(games))
    print(f"  {len(todo)} game(s) to try, {nc.human(total)}"
          + (f" ({done_already} done already)" if done_already else ""))
    if check or not todo:
        if not todo:
            print("  Nothing to do.")
        return 0
    print(nc.c("1;33", "\n  This needs Nomad firmware from 2026-10-28 or newer. On older firmware"))
    print(nc.c("1;33", "  the games won't start until you run this again with --undo."))
    if not assume_yes and input("  Go ahead? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print("Cancelled.")
        return 1

    progress = nc.Progress()
    saved = packed = kept = 0
    errors: list[str] = []
    with ThreadPoolExecutor(min(4, os.cpu_count() or 2)) as ex:   # zlib lets go of the GIL
        futs = {ex.submit(_pack, r): r for r in todo}
        try:
            for i, f in enumerate(as_completed(futs), 1):
                rom = futs[f]
                try:
                    what, before, after = f.result()
                    if what == "packed":
                        packed += 1
                        saved += before - after
                    else:
                        kept += 1
                        skip[rom.relative_to(games).as_posix()] = before
                except Exception as exc:
                    errors.append(f"{rom.name}: {exc}")
                progress.pct(100.0 * i / len(todo), rom.name[-50:])
        except KeyboardInterrupt:
            ex.shutdown(wait=True, cancel_futures=True)
            progress.done()
            _save_skip(games, skip)
            print(nc.c("1;31", "\n  Stopped. Each game is either done or untouched, run it again to finish."))
            return 130
    progress.done()
    _save_skip(games, skip)
    print(nc.c("1", f"\nDone: {packed} compressed, {nc.human(saved)} smaller in all")
          + (f", {kept} left as they were (they'd only shrink under {int(MIN_SAVING * 100)} %)" if kept else ""))
    for e in errors[:5]:
        print(nc.c("31", f"  Failed: {e}"))
    if packed:
        print(nc.c("2", "  Run 'Rebuild the media index' before you eject, so the Games page lists them."))
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Compress the games on a Nomad card so they start about twice as fast. Saves are kept.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    ap.add_argument("-o", "--output", help="same as giving the card on its own")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask, just go")
    ap.add_argument("--check", action="store_true", help="show what it would do, don't change anything")
    ap.add_argument("--undo", action="store_true", help="put every game back as it was (for older Nomad firmware)")
    args = ap.parse_args()
    return run(dest=args.card or args.output, assume_yes=args.yes, check=args.check, undo=args.undo)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
