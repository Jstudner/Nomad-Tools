"""
Cross-platform card discovery for nomad's card tools.

Finds plugged-in removable cards and flags which look like a nomad card, on:
  • Linux  — lsblk (removable/USB disks only; never the system disk), and
             udisksctl to mount a card that isn't mounted yet;
  • Windows — removable drive letters (plus any fixed drive that looks like a
             nomad card), already mounted by the OS;
  • macOS  — volumes under /Volumes.

list_cards() returns a list of dicts shaped for the picker:
    {label, model, mountpoint|None, partition|None, is_nomad, free, size}
A mountpoint of None means "not mounted yet" (Linux) — mount_partition() mounts it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

Log = Callable[[str], None]

# Signature of a nomad card's frontend at the SD root.
_SIG_FILES = ("index.html", "menu.html")
_SIG_DIRS = ("Movies", "Shows")


def is_nomad_mount(mountpoint: str | Path) -> bool:
    root = Path(mountpoint)
    try:
        if not root.is_dir():
            return False
        files_ok = all((root / f).exists() for f in _SIG_FILES)
        dirs_ok = all((root / d).is_dir() for d in _SIG_DIRS)
        return files_ok and dirs_ok
    except OSError:
        return False


def _free_size(mountpoint: Optional[str]):
    if not mountpoint:
        return None, None
    try:
        u = shutil.disk_usage(mountpoint)
        return u.free, u.total
    except OSError:
        return None, None


# ── Linux ─────────────────────────────────────────────────────────────────────

_LSBLK_COLS = "NAME,PATH,SIZE,TYPE,MOUNTPOINT,LABEL,FSTYPE,RM,RO,MODEL,TRAN,UUID"


def _lsblk() -> dict:
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", _LSBLK_COLS],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, check=True).stdout
        import json
        return json.loads(out)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return {"blockdevices": []}


def _root_disk() -> Optional[str]:
    """PATH of the disk hosting '/', to exclude it from the card list."""
    try:
        src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "/"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, check=True).stdout.strip()
        if not src:
            return None
        pk = subprocess.run(["lsblk", "-no", "PKNAME", src],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True).stdout.strip().splitlines()
        return f"/dev/{pk[0]}" if pk and pk[0] else src
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _list_linux() -> List[dict]:
    tree = _lsblk()
    root_disk = _root_disk()
    cards: List[dict] = []
    for dev in tree.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        removable = bool(dev.get("rm")) or (dev.get("tran") == "usb")
        if not removable or dev.get("path") == root_disk:
            continue  # only removable/USB, never the system disk
        model = (dev.get("model") or dev.get("name") or "").strip()
        for child in dev.get("children", []) or []:
            mp = child.get("mountpoint")
            free, size = _free_size(mp)
            cards.append({
                "label": child.get("label") or "(no label)",
                "model": model,
                "mountpoint": mp,
                "partition": child.get("path"),
                "is_nomad": bool(mp and (child.get("label") == "NOMAD" or is_nomad_mount(mp))),
                "free": free,
                "size": size if size is not None else int(child.get("size") or 0) or None,
            })
    return cards


def _mount_linux(partition: str, log: Log) -> str:
    import re
    cur = subprocess.run(["lsblk", "-no", "MOUNTPOINT", partition],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    existing = (cur.stdout or "").strip().splitlines()
    if existing and existing[0]:
        return existing[0]
    r = subprocess.run(["udisksctl", "mount", "-b", partition],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log(r.stdout.strip())
    m = re.search(r"at (.+?)\.?\s*$", r.stdout.strip())
    if m:
        return m.group(1).strip()
    import time
    for _ in range(10):
        c = subprocess.run(["lsblk", "-no", "MOUNTPOINT", partition],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        mp = (c.stdout or "").strip().splitlines()
        if mp and mp[0]:
            return mp[0]
        time.sleep(0.5)
    raise RuntimeError(f"Could not determine mountpoint for {partition}")


# ── Windows ───────────────────────────────────────────────────────────────────

def _list_windows() -> List[dict]:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()

    cards: List[dict] = []
    bitmask = k32.GetLogicalDrives()
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = f"{chr(65 + i)}:"
        root = f"{letter}\\"
        dtype = k32.GetDriveTypeW(root)
        nomad = is_nomad_mount(root)
        # Show removable cards always; a fixed drive only if it looks like nomad
        # (built-in SD readers sometimes report FIXED). Never the system drive.
        if letter.upper() == system_drive:
            continue
        if dtype == DRIVE_REMOVABLE or (dtype == DRIVE_FIXED and nomad):
            name_buf = ctypes.create_unicode_buffer(261)
            try:
                k32.GetVolumeInformationW(root, name_buf, 261, None, None, None, None, 0)
            except Exception:
                pass
            label = name_buf.value or "(no label)"
            free, size = _free_size(root)
            cards.append({
                "label": label, "model": "removable" if dtype == DRIVE_REMOVABLE else "drive",
                "mountpoint": root, "partition": letter,
                "is_nomad": bool(nomad or label.upper() == "NOMAD"),
                "free": free, "size": size,
            })
    return cards


# ── macOS ─────────────────────────────────────────────────────────────────────

def _list_macos() -> List[dict]:
    cards: List[dict] = []
    vroot = Path("/Volumes")
    if not vroot.is_dir():
        return cards
    for entry in sorted(vroot.iterdir()):
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
        except OSError:
            continue
        mp = str(entry)
        free, size = _free_size(mp)
        cards.append({
            "label": entry.name, "model": "volume",
            "mountpoint": mp, "partition": None,
            "is_nomad": bool(is_nomad_mount(mp) or entry.name.upper() == "NOMAD"),
            "free": free, "size": size,
        })
    return cards


# ── public API ────────────────────────────────────────────────────────────────

def list_cards() -> List[dict]:
    """Removable cards currently visible, most-likely-nomad first."""
    try:
        if sys.platform.startswith("linux"):
            cards = _list_linux()
        elif sys.platform == "win32":
            cards = _list_windows()
        elif sys.platform == "darwin":
            cards = _list_macos()
        else:
            cards = []
    except Exception:
        cards = []
    cards.sort(key=lambda c: (not c["is_nomad"], c["mountpoint"] is None))
    return cards


def mount_partition(partition: str, log: Log = print) -> str:
    """Mount an unmounted partition and return its mountpoint (Linux only)."""
    if sys.platform.startswith("linux"):
        return _mount_linux(partition, log)
    raise RuntimeError("Automatic mounting is only supported on Linux; "
                       "mount the card yourself and pick it, or enter its path.")
