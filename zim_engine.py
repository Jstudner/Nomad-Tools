"""
Self-contained ZIM import: validate, copy (splitting >4 GB), then build the
on-card search index. Ported from Nomad Manager's sd/archives.py so this tools
folder needs only Python and Node, not a Nomad Manager install.

  copy/split is pure Python, so it works everywhere
  the index is built by the bundled zim/zim_indexer.js (needs Node on PATH),
  which uses its own sort on Windows
  the Unix-only `sync` flush is skipped off Linux and macOS

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
            "Node.js isn't installed (or isn't on PATH). It's needed to build the archive "
            "search. Install Node.js and try again."
        )
    return exe


def _validate_zim(src: Path) -> int:
    """Confirm the file exists and looks like a ZIM (magic number). Returns size."""
    if not src.is_file():
        raise RuntimeError(f"Not a file: {src}")
    with src.open("rb") as f:
        head = f.read(4)
    if head != ZIM_MAGIC:
        raise RuntimeError(f"{src.name} isn't a ZIM file")
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


def import_zim(src_path: str, mountpoint: str | Path, progress: Log = print, prepare: bool = False,
               index: bool = True) -> None:
    """Import one ZIM onto the card: validate -> copy (splitting if >4GB) -> index.

    prepare: write it with small clusters as it goes onto the card (zim_prepare), instead
    of a plain copy. `progress` is the shared Progress object (callable + .plan/.step/.pct)."""
    src = Path(src_path).expanduser()
    archive_dir = Path(mountpoint) / "Archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    plan = getattr(progress, "plan", None)
    step = getattr(progress, "step", None)
    if plan:
        plan(["Check the file", "Speed up and copy to card" if prepare else "Copy to card", "Build search index"])

    # 1. validate
    if step:
        step("Check the file")
    size = _validate_zim(src)
    name = src.name  # e.g. wikipedia_en_all.zim
    if not name.lower().endswith(".zim"):
        raise RuntimeError("That needs to be a .zim file")
    progress(f"{name}: looks like a good ZIM, {_human(size)}")

    # Already in this card's /Archive. Copying it over itself would delete it
    # first (_remove_existing) and then have nothing to copy, so only index it.
    in_place = src.resolve().parent == archive_dir.resolve()

    # Free-space guard on the card.
    try:
        free = shutil.disk_usage(str(archive_dir)).free
        if not in_place and free < size + (64 * 1024 * 1024):
            raise RuntimeError(f"Not enough space on the card: it needs {_human(size)} and has {_human(free)} free")
    except OSError:
        pass

    # 2. copy / split
    if step:
        step("Speed up and copy to card" if prepare else "Copy to card")
    if prepare and not in_place:
        import zim_prepare   # imports this module too
        if zim_prepare.status([src]) == "prepared":
            prepare = False
            progress(f"{name} is already sped up, copying it as it is")
    if prepare and not in_place:
        import zim_prepare
        target, growth = zim_prepare.choose_size([src])
        if growth > zim_prepare.SKIP:
            prepare = False
            progress(f"{name} would grow about {growth * 100:+.0f}% if sped up (its pages repeat each other a lot), so it's copied as it is")
    if prepare and not in_place:
        progress(f"Speeding it up as it copies (about {growth * 100:+.0f}% in size)")
        tmp = archive_dir / (".preparing-" + name)
        split = size * 1.15 > FAT32_MAX - (16 * 1024 * 1024) and zim_prepare.fat32(archive_dir)
        res = zim_prepare.rewrite([src], tmp, split, progress, target=target)
        progress("  checking it against the original")
        zim_prepare.verify([src], res["paths"], progress=progress)
        _remove_existing(archive_dir, name)
        for p in res["paths"]:
            os.replace(p, archive_dir / (name + p.name[len(tmp.name):]))
        progress(f"  wrote {name} ({_human(res['size'])}, was {_human(size)})")
    elif in_place:
        progress(f"{name} is already on the card, so it just gets added to the search")
    elif size <= FAT32_MAX - (16 * 1024 * 1024):
        dst = archive_dir / name
        progress(f"Copying {dst.name} as one file (it's under 4 GB)")
        _remove_existing(archive_dir, name)
        _copy_with_progress(src, dst, size, 0, size, progress)
        progress(f"  copied {name} ({_human(size)})")
    else:
        nparts = (size + SPLIT_PART_SIZE - 1) // SPLIT_PART_SIZE
        progress(f"It's over 4 GB, so it's split into {nparts} parts of about {_human(SPLIT_PART_SIZE)} (FAT32 cards can't hold bigger files)")
        _remove_existing(archive_dir, name)
        parts = _write_split(src, archive_dir, name, size, progress)
        progress(f"  wrote {len(parts)} parts: {', '.join(parts)}")

    _sync()

    # 3. index
    if not index:
        return
    if step:
        step("Build search index")
    index_archives(mountpoint, progress)
    progress("Archive added.")


def index_archives(mountpoint: str | Path, log: Log = print) -> None:
    root = str(mountpoint)
    archive_dir = Path(root) / "Archive"
    if not archive_dir.is_dir():
        log("No Archive folder on this card, so there's no search to build.")
        return

    zims = [p for p in archive_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and ".zim" in p.name.lower()]
    if not zims:
        log("No .zim files in the Archive folder, so there's no search to build.")
        return

    node = _node()
    if not INDEXER.exists():
        raise RuntimeError(f"The search builder is missing from the tools folder ({INDEXER}). Try downloading the tools again.")

    cmd = [node, str(INDEXER), root]
    log("$ " + " ".join(cmd))
    # encoding pinned: article titles are often non-Latin, and the Windows
    # locale codec would raise UnicodeDecodeError part way through an import
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
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
        raise RuntimeError(f"Building the archive search failed (exit {rc})")
    # a fresh index: an unfinished in-place update (zim_prepare) no longer applies
    pending = archive_dir / ".nomad-zim" / "remap-pending.json"
    if pending.exists():
        pending.unlink()
    _sync()
    log("Archive search is built.")
