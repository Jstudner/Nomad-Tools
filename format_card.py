#!/usr/bin/env python3
"""
format_card: erase a card and set it up as an exFAT nomad card (Linux).

exFAT has no 4GB per-file limit and copes with the huge number of small
files a map region holds. Clusters are 4KB so those little files don't
waste space, or 8KB on cards over about 540GB: at 4KB, mkfs.exfat
(exfatprogs 1.1.3) marks too few clusters in use on a 1TB card, and the
first big write lands on the root directory.

Everything on the chosen card is destroyed. The tool only ever offers disks
the kernel marks removable (RM=1, so not USB hard drives), never the disk
your system is running from, and it makes you type the device path back
before it touches anything.

    python format_card.py                      # interactive, pick a card
    python format_card.py --device /dev/sdX --yes
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import nomad_common as nc

LABEL = "NOMAD"
SIZE_CAP_GB = 2048      # refuse anything bigger, it isn't a card
BIG_CARD_BYTES = 540_000_000_000   # above this, 8K clusters (same cutoff as provision-card.sh)


def cluster_for(size_bytes: int) -> str:
    """4K keeps map tiles from wasting space; 8K on big cards, where 4K comes out broken."""
    return "8K" if size_bytes > BIG_CARD_BYTES else "4K"


class FormatError(RuntimeError):
    pass


def _run(cmd: list[str], log) -> None:
    """Run a privileged step through sudo. In a terminal sudo asks for a
    password once and remembers it; there is nothing to answer here."""
    log("  $ sudo " + " ".join(cmd))
    r = subprocess.run(["sudo", *cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    if r.stdout:
        for line in r.stdout.rstrip().splitlines():
            log("    " + line)
    if r.returncode != 0:
        raise FormatError(f"step failed: {' '.join(cmd)}")


def _is_removable(dev: dict) -> bool:
    # RM=1 only. Being on USB is not enough: external hard drives are USB too
    # and report RM=0. Older lsblk prints "0"/"1" instead of a JSON boolean.
    rm = dev.get("rm")
    return rm if isinstance(rm, bool) else str(rm).strip() == "1"


def _removable_disks() -> list[dict]:
    """Whole removable disks with a card in them, never the system disk."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,PATH,TYPE,SIZE,MODEL,RM,TRAN,MOUNTPOINT,PKNAME"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True).stdout
        tree = json.loads(out)
    except Exception as e:
        raise FormatError(f"could not list disks (is lsblk present?): {e}")

    # disk hosting '/', so we never offer it
    root_disk = None
    try:
        src = subprocess.run(["findmnt", "-no", "SOURCE", "/"],
                            stdout=subprocess.PIPE, text=True).stdout.strip()
        if src:
            pk = subprocess.run(["lsblk", "-no", "PKNAME", src],
                               stdout=subprocess.PIPE, text=True).stdout.strip().splitlines()
            root_disk = f"/dev/{pk[0]}" if pk and pk[0] else src
    except Exception:
        pass

    disks = []
    for dev in tree.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        if not _is_removable(dev) or dev.get("path") == root_disk:
            continue
        if not int(dev.get("size") or 0):       # an empty card reader slot
            continue
        mounts = [c.get("mountpoint") for c in (dev.get("children") or [])
                  if c.get("mountpoint")]
        disks.append({
            "path": dev.get("path"),
            "model": (dev.get("model") or "").strip() or "disk",
            "size": int(dev.get("size") or 0),
            "mounts": mounts,
        })
    return disks


def _pick_disk(disks: list[dict]) -> dict | None:
    print(nc.c("1", "\nRemovable disks I can format:"))
    for i, d in enumerate(disks, 1):
        where = ("  mounted at " + ", ".join(d["mounts"])) if d["mounts"] else ""
        print(f"  {i}. {nc.c('1', d['path'])}  {d['model']}  {nc.human(d['size'])}{where}")
    print("  0. cancel")
    while True:
        pick = input("Which disk? ").strip()
        if pick in ("0", "", "q", "cancel"):
            return None
        if pick.isdigit() and 1 <= int(pick) <= len(disks):
            return disks[int(pick) - 1]
        print(f"  type 1-{len(disks)} or 0.")


def format_card(device: str, assume_yes: bool = False, log=print) -> int:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        print(nc.c("1;33",
            "Formatting is Linux only for now. On Windows use 'Format...' in "
            "Explorer, choose exFAT and an allocation unit size of 4096 bytes "
            "(8192 on cards over 512 GB); on a Mac use Disk Utility, erase as "
            "exFAT. Then run the other tools against the card."))
        return 1

    if subprocess.run(["which", "mkfs.exfat"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL).returncode != 0:
        print(nc.c("1;31",
            "mkfs.exfat isn't installed. Install exfatprogs first:\n"
            "  Debian/Ubuntu/Pop: sudo apt install exfatprogs\n"
            "  Fedora:            sudo dnf install exfatprogs\n"
            "  Arch:              sudo pacman -S exfatprogs"))
        return 1

    disks = _removable_disks()
    if device:
        target = next((d for d in disks if d["path"] == device), None)
        if not target:
            print(nc.c("1;31",
                f"{device} isn't in the removable-disk list. For safety I only "
                "format disks marked removable, like memory cards, never USB "
                "hard drives or the system disk."))
            return 1
    else:
        if not disks:
            print(nc.c("1;33", "No removable disks found. Plug a card in and try again."))
            return 1
        target = _pick_disk(disks)
        if not target:
            print("Cancelled.")
            return 1

    dev = target["path"]
    gb = target["size"] / (1024 ** 3)
    if gb > SIZE_CAP_GB:
        print(nc.c("1;31", f"{dev} is {gb:.0f} GB, too big to be a card. Refusing."))
        return 1

    cluster = cluster_for(target["size"])

    print(nc.c("1;31", f"\n  This ERASES EVERYTHING on {dev} "
                       f"({target['model']}, {nc.human(target['size'])})."))
    if target["mounts"]:
        print(nc.c("1;33", "  It's mounted at " + ", ".join(target["mounts"]) +
                          " and will be unmounted first."))
    print("  There is no undo. Files, other partitions, all of it goes.")
    if not assume_yes:
        typed = input(f"  Type the device path {nc.c('1', dev)} to confirm: ").strip()
        if typed != dev:
            print("  Didn't match. Nothing was touched.")
            return 1

    log = nc.Progress() if log is print else log
    log(f"Formatting {dev} as exFAT '{LABEL}' with {cluster} clusters.")

    # Unmount anything currently mounted on this disk.
    for mp in target["mounts"]:
        subprocess.run(["udisksctl", "unmount", "-b", dev + _partsep(dev) + "*"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for part in _partitions(dev):
        subprocess.run(["udisksctl", "unmount", "-b", part],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    _run(["wipefs", "-a", dev], log)
    _run(["parted", "-s", dev, "mklabel", "msdos"], log)
    # fs-type 'ntfs' gives the partition MBR type 0x07, which readers treat as
    # exFAT once mkfs.exfat writes the filesystem over it.
    _run(["parted", "-s", dev, "mkpart", "primary", "ntfs", "1MiB", "100%"], log)

    part = _wait_for_partition(dev, log)
    _run(["mkfs.exfat", "-c", cluster, "-L", LABEL, part], log)
    _run(["partprobe", dev], log)
    log(f"Done. {part} is exFAT, labeled {LABEL}, {cluster} clusters.")
    if hasattr(log, "done"):
        log.done()
    print(nc.c("1;32", "\n  Card ready. It should remount on its own; if not, "
                      "unplug and replug it."))
    return 0


def _partsep(dev: str) -> str:
    # /dev/sdb -> "", nvme/mmc -> "p": sdb1 vs mmcblk0p1
    name = dev.rsplit("/", 1)[-1]
    return "p" if name and name[-1].isdigit() else ""


def _partitions(dev: str) -> list[str]:
    try:
        out = subprocess.run(["lsblk", "-nlo", "PATH,TYPE", dev],
                            stdout=subprocess.PIPE, text=True).stdout
        return [ln.split()[0] for ln in out.splitlines()
                if ln.split()[1:2] == ["part"]]
    except Exception:
        return []


def _wait_for_partition(dev: str, log) -> str:
    want = dev + _partsep(dev) + "1"
    for _ in range(15):
        if Path(want).exists():
            return want
        time.sleep(1)
    parts = _partitions(dev)
    if parts:
        return parts[0]
    raise FormatError(f"partition {want} never appeared")


def run(dest: str | None = None, device: str | None = None,
        assume_yes: bool = False) -> int:
    # dest (a mounted card path) is ignored: formatting works on the whole disk,
    # which the tool picks itself.
    nc.banner("Format a card as exFAT")
    try:
        return format_card(device or "", assume_yes=assume_yes)
    except FormatError as e:
        print(nc.c("1;31", f"\n  {e}"))
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Format a card as an exFAT nomad card (Linux).")
    ap.add_argument("--device", help="whole disk to format, e.g. /dev/sdb (skips the picker)")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the typed confirmation")
    args = ap.parse_args()
    try:
        return format_card(args.device or "", assume_yes=args.yes)
    except FormatError as e:
        print(nc.c("1;31", f"\n  {e}"))
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncancelled.")
        sys.exit(130)
