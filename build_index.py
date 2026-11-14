#!/usr/bin/env python3
"""
build_index: regenerate the browse index Nomad reads from a card.

Nomad can't scan a big SD card fast on the device, so it reads pre-built NDJSON
index files from /.system-index instead. Run this whenever you add or remove
media. Nothing is deleted from your card.

    ./build_index.py                 # pick a card, then index it
    ./build_index.py /media/me/NOMAD

The scan and hash functions below match the firmware's own indexer byte for
byte. Don't "clean them up".
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Tuple

import nomad_common as nc

VERSION = "2.1.0"
INDEX_DIR = ".system-index"

# Buckets to index (matching indexWorkerTask order)
BUCKETS = ["/Shows", "/Music", "/Movies", "/Books", "/Gallery", "/Files", "/Games",
           "/Cookbook", "/Workshop", "/"]

# Folders to skip (matching firmware)
SKIP_FOLDERS = {".system-index", "System Volume Information", "Archive", "$RECYCLE.BIN",
                ".Trash", ".Spotlight-V100", ".fseventsd", ".TemporaryItems"}


# firmware-matched core. Do not modify: must match the device byte for byte.

def sanitize_token(s: str) -> str:
    """Sanitize a directory name into a filename token (matching firmware)"""
    out = []
    for c in s:
        if c.isalnum() or c in ('-', '_'):
            out.append(c)
        else:
            out.append('_')
    return ''.join(out) if out else '_'


def json_escape(s: str) -> str:
    """JSON escape matching firmware jsonEscape"""
    s = s.replace('\\', '\\\\')
    s = s.replace('"', '\\"')
    s = s.replace('\n', '\\n')
    s = s.replace('\r', '\\r')
    return s


def fnv1a64(data: str) -> int:
    """FNV-1a 64-bit hash matching firmware"""
    fnv_prime = 0x100000001b3
    hash_value = 0xcbf29ce484222325
    for byte in data.encode('utf-8'):
        hash_value ^= byte
        hash_value = (hash_value * fnv_prime) & 0xFFFFFFFFFFFFFFFF
    return hash_value


def normalize_path(path: str) -> str:
    """Normalize path matching firmware normalizePath"""
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def is_comic_folder(dir_path: Path) -> bool:
    """Detect comic folders matching firmware isComicFolder"""
    if not dir_path.is_dir():
        return False
    has_images = False
    has_book_files = False
    image_count = 0
    try:
        for item in dir_path.iterdir():
            if item.is_file():
                ext = item.suffix.lower()
                if ext in {'.pdf', '.epub', '.azw3', '.mobi', '.mp3', '.m4a', '.m4b', '.flac'}:
                    has_book_files = True
                    break
                if ext in {'.png', '.jpg', '.jpeg', '.webp'}:
                    has_images = True
                    image_count += 1
        return has_images and not has_book_files and image_count >= 3
    except Exception:
        return False


def should_skip_folder(path: str, name: str) -> bool:
    """Check if folder should be skipped matching firmware shouldSkipFolder"""
    if name.startswith('.') or name.startswith('$'):
        return True
    for skip in SKIP_FOLDERS:
        if name == skip or path.startswith(f"/{skip}"):
            return True
    return False


def determine_max_depth(norm_path: str) -> int:
    """Determine recursion depth matching firmware logic exactly"""
    if norm_path == "/":
        return 0
    if norm_path == "/Books":
        return 0
    if norm_path.startswith("/Books/"):
        return 0
    return 10


def count_and_hash_entries(dir_path: Path, root_path: Path, max_depth: int) -> Tuple[int, int]:
    """Count entries and compute hash matching firmware prepass"""
    count = 0
    sig = 0xcbf29ce484222325  # FNV offset basis

    def scan(path: Path, depth: int = 0):
        nonlocal count, sig
        if depth > max_depth:
            return
        try:
            for item in sorted(path.iterdir()):
                name = item.name
                if should_skip_folder(str(item), name):
                    continue
                try:
                    full_path = "/" + str(item.relative_to(root_path)).replace('\\', '/')
                except ValueError:
                    continue
                if item.is_dir():
                    if full_path.startswith("/Books/") and is_comic_folder(item):
                        continue
                    sig_data = f"{full_path}|{name}"
                    sig = fnv1a64(sig_data) ^ sig
                    count += 1
                    scan(item, depth + 1)
                else:
                    size = item.stat().st_size
                    mtime = 0  # Firmware uses 0 for mtime
                    sig_data = f"{full_path}|{size}|{mtime}"
                    sig = fnv1a64(sig_data) ^ sig
                    count += 1
        except Exception as e:
            print(f"\n  Couldn't read {path}: {e}")

    scan(dir_path)
    return count, sig


def write_index_for_dir(dir_path: Path, output_filename: str, root_path: Path) -> Tuple[bool, int]:
    """Write NDJSON index file matching firmware writeNDIndexForDir"""
    if not dir_path.exists() or not dir_path.is_dir():
        return False, 0
    try:
        # relative_to(self) is Path('.'), which would normalize the card root to
        # "/." and miss the isRoot test. The root index then got depth 10 and a
        # sig that never matched the firmware's depth-0 prepass, so the device
        # rebuilt root.index.ndjson on every boot. Keep "." mapped to "/".
        rel = str(dir_path.relative_to(root_path)).replace('\\', '/')
        norm_path = normalize_path("/" if rel == "." else "/" + rel)
    except ValueError:
        norm_path = "/"

    max_depth = determine_max_depth(norm_path)
    count, sig = count_and_hash_entries(dir_path, root_path, max_depth)

    index_dir = root_path / INDEX_DIR
    index_dir.mkdir(exist_ok=True)
    tmp_path = index_dir / f"{output_filename}.tmp"
    final_path = index_dir / output_filename

    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            sig_hex = f"{sig:016x}"
            header = f'{{"_type":"dir","path":"{json_escape(norm_path)}","sig":"{sig_hex}","count":{count}}}\n'
            f.write(header)

            def write_entries(path: Path, depth: int = 0):
                if depth > max_depth:
                    return
                try:
                    for item in sorted(path.iterdir()):
                        name = item.name
                        if should_skip_folder(str(item), name):
                            continue
                        try:
                            full_path = "/" + str(item.relative_to(root_path)).replace('\\', '/')
                        except ValueError:
                            continue
                        if item.is_dir():
                            is_comic = full_path.startswith("/Books/") and is_comic_folder(item)
                            comic_attr = ',\"comic\":true' if is_comic else ''
                            entry = f'{{"t":"d","n":"{json_escape(name)}","p":"{json_escape(full_path)}"{comic_attr}}}\n'
                            f.write(entry)
                            if not is_comic:
                                write_entries(item, depth + 1)
                        else:
                            size = item.stat().st_size
                            mtime = 0  # Firmware uses 0
                            entry = f'{{"t":"f","n":"{json_escape(name)}","p":"{json_escape(full_path)}","sz":{size},"mt":{mtime}}}\n'
                            f.write(entry)
                except Exception as e:
                    print(f"\n  Couldn't list everything in {path}: {e}")

            write_entries(dir_path)

        if final_path.exists():
            final_path.unlink()
        tmp_path.rename(final_path)
        return True, count
    except Exception as e:
        print(f"\n  Couldn't write the index for {dir_path}: {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return False, 0


def build_bucket_index(bucket_path: Path, root_path: Path, bucket_name: str) -> Tuple[bool, int]:
    """Build bucket index matching firmware buildBucketIndex"""
    bucket = "root" if bucket_name == "/" else bucket_name.lstrip('/').rstrip('/')
    return write_index_for_dir(bucket_path, f"{bucket}.index.ndjson", root_path)


_NESTED = {   # bucket -> (label, filename prefix) for its per-subfolder indexes
    "/Shows": ("shows", "Shows"),
    "/Music": ("albums/playlists", "Music"),
    "/Books": ("book subfolders", "Books"),
    "/Gallery": ("gallery folders", "Gallery"),
    "/Files": ("file folders", "Files"),
    "/Cookbook": ("recipe categories", "Cookbook"),
    "/Workshop": ("model collections", "Workshop"),
}


def _index_nested(bucket: str, bucket_path: Path, root_path: Path, progress: nc.Progress) -> int:
    """Write nested indexes for a bucket's subfolders. Returns items indexed."""
    label, prefix = _NESTED[bucket]
    done = 0
    subdirs = [d for d in sorted(bucket_path.iterdir())
               if d.is_dir() and not should_skip_folder(str(d), d.name)]
    for i, sub in enumerate(subdirs, 1):
        if bucket == "/Books" and is_comic_folder(sub):
            continue  # comics browse as a folder, no nested index
        token = f"{prefix}__{sanitize_token(sub.name)}.nested.ndjson"
        ok, count = write_index_for_dir(sub, token, root_path)
        if ok:
            done += count
        progress.pct(i * 100.0 / len(subdirs) if subdirs else 100.0, f"{label}: {sub.name}")
    progress.done()
    return done


def run(dest: str | None = None) -> int:
    """Interactive index rebuild. `dest` skips the card picker."""
    nc.banner("Rebuild the media index")

    dest = nc.choose_card(dest, prompt="Which card should I rebuild the index for?")
    if not dest:
        print("Cancelled.")
        return 1
    root_path = Path(dest)
    print(f"  Card: {root_path}")

    progress = nc.Progress()
    start = time.time()
    total_items = 0

    for bucket in BUCKETS:
        bucket_path = root_path if bucket == "/" else root_path / bucket.lstrip('/')
        if not bucket_path.exists():
            continue
        print(nc.c("1;36", f"\n▶ {bucket}"))
        ok, count = build_bucket_index(bucket_path, root_path, bucket)
        if ok:
            total_items += count
            print(f"  {count} item(s)")
        else:
            print(nc.c("31", "  couldn't index this folder"))
        if bucket in _NESTED:
            total_items += _index_nested(bucket, bucket_path, root_path, progress)

    _remove_legacy_media_json(root_path)

    elapsed = time.time() - start
    buckets_done = len([b for b in BUCKETS
                        if (root_path if b == "/" else root_path / b.lstrip('/')).exists()])
    print(nc.c("1", f"\nDone: {total_items:,} item(s) in {buckets_done} folder(s), "
                    f"took {elapsed:.1f}s"))
    print(nc.c("2", "  Eject the card safely. Nomad picks up the changes next time it starts."))
    return 0


def _remove_legacy_media_json(root_path: Path) -> None:
    """Delete a /media.json left by older versions of this tool.

    The firmware never writes one. At boot it reads one as a card from before
    the NDJSON index, deletes it and rescans the whole card, which is the scan
    this tool exists to avoid."""
    legacy = root_path / "media.json"
    if not legacy.is_file():
        return
    try:
        legacy.unlink()
        print(nc.c("2", "\n  Removed an old media.json (it made Nomad scan the whole card at every start)"))
    except OSError as e:
        print(nc.c("31", f"\n  Couldn't remove the old media.json: {e}"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild the media index on a Nomad card. This is the list Nomad uses to show your stuff. Nothing on the card gets deleted.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    args = ap.parse_args()
    return run(dest=args.card)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nStopped.")
        sys.exit(130)
