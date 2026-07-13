"""
Self-contained ZIM import: validate → copy (splitting >4 GB) → build the on-card
search index. Ported from nomad Manager's sd/archives.py so this tools folder
needs nothing but Python + Node — no nomad Manager install required.

Cross-platform:
  • copy/split is pure Python (works everywhere);
  • the index is built by the bundled zim/zim_indexer.js (needs Node on PATH),
    which sorts with a built-in cross-platform sort on Windows;
  • the Unix-only `sync` flush is guarded so it's a no-op off Linux/mac.

Public surface matches the old sd/archives.py, so callers are unchanged:
    import_zim(src_path, mountpoint, progress)
    index_archives(mountpoint, log)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List

Log = Callable[[str], None]

ZIM_MAGIC = bytes([0x5A, 0x49, 0x4D, 0x04])   # little-endian 0x044D495A
# FAT32 caps a single file at 4 GiB - 1. Split parts stay comfortably under it.
FAT32_MAX = 4 * 1024 * 1024 * 1024
SPLIT_PART_SIZE = 3_900_000_000               # ~3.9 GB per part
COPY_CHUNK = 8 * 1024 * 1024

HERE = Path(__file__).resolve().parent
INDEXER = HERE / "zim" / "zim_indexer.js"     # bundled; finds its nomad-zim.js beside it


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def _sync() -> None:
    """Flush pending writes to the card. Unix `sync` only; no-op on Windows."""
    if sys.platform == "win32":
        return
    try:
        subprocess.run(["sync"])
    except (FileNotFoundError, OSError):
        pass


def _split_suffix(i: int) -> str:
    # kiwix zimsplit naming: aa, ab, ... az, ba, ...  (part name = <foo.zim> + suffix)
    return chr(97 + i // 26) + chr(97 + i % 26)


def _node() -> str:
    exe = shutil.which("node") or shutil.which("nodejs")
    if not exe:
        raise RuntimeError(
            "Node.js not found on PATH. The ZIM indexer runs on Node "
            "(it reuses the device's nomad-zim.js reader). Install Node and retry."
        )
    return exe


def _validate_zim(src: Path) -> int:
    """Confirm the file exists and looks like a ZIM (magic number). Returns size."""
    if not src.is_file():
        raise RuntimeError(f"Not a file: {src}")
    with src.open("rb") as f:
        head = f.read(4)
    if head != ZIM_MAGIC:
        raise RuntimeError(f"{src.name} is not a ZIM file (bad magic {head!r})")
    return src.stat().st_size


def _copy_with_progress(src: Path, dst: Path, total: int, base: int, grand_total: int, progress) -> None:
    """Copy src -> dst reporting overall pct (base..base+total of grand_total)."""
    done = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            chunk = fin.read(COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            done += len(chunk)
            overall = base + done
            progress.pct(round(overall * 100.0 / grand_total, 1),
                         f"{_human(overall)} / {_human(grand_total)}")


def _write_split(src: Path, archive_dir: Path, name: str, size: int, progress) -> List[str]:
    """Split src into <name>aa/<name>ab/... parts <= SPLIT_PART_SIZE. Returns part names."""
    parts: List[str] = []
    written = 0
    with src.open("rb") as fin:
        idx = 0
        while written < size:
            part_name = name + _split_suffix(idx)
            part_path = archive_dir / part_name
            parts.append(part_name)
            part_written = 0
            with part_path.open("wb") as fout:
                while part_written < SPLIT_PART_SIZE:
                    chunk = fin.read(min(COPY_CHUNK, SPLIT_PART_SIZE - part_written))
                    if not chunk:
                        break
                    fout.write(chunk)
                    part_written += len(chunk)
                    written += len(chunk)
                    progress.pct(round(written * 100.0 / size, 1),
                                 f"part {idx + 1} · {_human(written)} / {_human(size)}")
            progress(f"  wrote {part_name} ({_human(part_written)})")
            idx += 1
    return parts


def _remove_existing(archive_dir: Path, name: str) -> None:
    """Remove a prior copy of this ZIM (whole file and any split parts) so a
    re-import or a whole<->split change doesn't leave a colliding pair."""
    base_no_ext = name[:-4] if name.lower().endswith(".zim") else name
    for p in archive_dir.iterdir():
        if not p.is_file():
            continue
        low = p.name.lower()
        if p.name == name:
            p.unlink()
        elif low.startswith(base_no_ext.lower() + ".zim") and len(p.name) == len(name) + 2:
            p.unlink()  # split part <name>aa


def import_zim(src_path: str, mountpoint: str | Path, progress: Log = print) -> None:
    """Import one ZIM onto the card: validate -> copy (splitting if >4GB) -> index.

    `progress` is the shared Progress object (callable + .plan/.step/.pct)."""
    src = Path(src_path).expanduser()
    archive_dir = Path(mountpoint) / "Archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    plan = getattr(progress, "plan", None)
    step = getattr(progress, "step", None)
    if plan:
        plan(["Validate", "Copy to card", "Build search index"])

    # ---- 1. validate ----
    if step:
        step("Validate")
    size = _validate_zim(src)
    name = src.name  # e.g. wikipedia_en_all.zim
    if not name.lower().endswith(".zim"):
        raise RuntimeError("Source must be a .zim file")
    progress(f"{name}: valid ZIM, {_human(size)}")

    # Free-space guard on the card.
    try:
        free = shutil.disk_usage(str(archive_dir)).free
        if free < size + (64 * 1024 * 1024):
            raise RuntimeError(f"Not enough space on card: need {_human(size)}, have {_human(free)} free")
    except OSError:
        pass

    # ---- 2. copy / split ----
    if step:
        step("Copy to card")
    if size <= FAT32_MAX - (16 * 1024 * 1024):
        dst = archive_dir / name
        progress(f"Copying whole file (under FAT32 4 GB limit) -> {dst.name}")
        _remove_existing(archive_dir, name)
        _copy_with_progress(src, dst, size, 0, size, progress)
        progress(f"  copied {name} ({_human(size)})")
    else:
        nparts = (size + SPLIT_PART_SIZE - 1) // SPLIT_PART_SIZE
        progress(f"Larger than FAT32 4 GB limit - splitting into {nparts} parts of ~{_human(SPLIT_PART_SIZE)}")
        _remove_existing(archive_dir, name)
        parts = _write_split(src, archive_dir, name, size, progress)
        progress(f"  wrote {len(parts)} parts: {', '.join(parts)}")

    _sync()

    # ---- 3. index ----
    if step:
        step("Build search index")
    index_archives(mountpoint, progress)
    progress("Import complete.")


def index_archives(mountpoint: str | Path, log: Log = print) -> None:
    root = str(mountpoint)
    archive_dir = Path(root) / "Archive"
    if not archive_dir.is_dir():
        log("No /Archive folder on this card - nothing to index.")
        return

    zims = [p for p in archive_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and ".zim" in p.name.lower()]
    if not zims:
        log("No .zim files under /Archive - nothing to index.")
        return

    node = _node()
    if not INDEXER.exists():
        raise RuntimeError(f"Bundled indexer missing: {INDEXER}")

    cmd = [node, str(INDEXER), root]
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pct = getattr(log, "pct", None)   # structured Progress → drives the UI bar
    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip()
        if not line:
            continue
        # Machine-readable progress from the indexer: "@@PCT <value> <detail>".
        if line.startswith("@@PCT "):
            body = line[6:].split(" ", 1)
            try:
                value = float(body[0])
            except ValueError:
                continue
            if pct:
                pct(value, body[1] if len(body) > 1 else None)
            continue
        log(line)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ZIM indexer failed (exit {rc})")
    _sync()
    log("Archive index complete (/Archive/.nomad-zim written).")
