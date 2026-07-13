#!/usr/bin/env python3
"""
Shared bits every nomad card tool uses.

Every tool in this folder (import_zim, optimize_images, build_index) uses these
so they all look and behave the same: the same colours, the same live progress
bar, and the same "pick a card" menu.

Card discovery is cross-platform (see cards.py): Linux, Windows and macOS. This
folder is self-contained — it needs only Python (plus Pillow for image work and
Node for ZIM work), not a nomad Manager install — so it can be copied and run
anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cards


# ── terminal styling ──────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()


def c(code: str, s: str) -> str:
    """Wrap text in an ANSI colour (no-op when output isn't a terminal)."""
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def human(n: int | None) -> str:
    """Bytes as a friendly size, e.g. 2.4 GB."""
    if n is None:
        return "?"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{f:.1f} TB"


def banner(title: str) -> None:
    print(c("1;36", f"\n── {title} ──"))


class Progress:
    """A callable log with a live progress bar pinned to the current line.

    Ordinary log lines scroll above the bar. Also exposes .plan/.step/.pct so it
    can be handed straight to nomad Manager's sd/archives.py, which drives those.
    Falls back to plain prints when stdout isn't a terminal.
    """

    BAR_W = 32

    def __init__(self) -> None:
        self._pct: float | None = None
        self._detail: str | None = None
        self._active = False   # a bar is currently drawn on the line

    # log a line -------------------------------------------------------------
    def __call__(self, line: str) -> None:
        self._clear()
        print(line)
        self._redraw()

    # sd/archives.py interface ----------------------------------------------
    def plan(self, steps: list[str]) -> None:
        self.__call__("  steps: " + " → ".join(steps))

    def step(self, name: str) -> None:
        self._pct = None
        self._detail = None
        self.__call__(c("1;36", f"▶ {name}"))

    def pct(self, value: float | None, detail: str | None = None) -> None:
        self._pct = value
        self._detail = detail
        self._redraw()

    # rendering --------------------------------------------------------------
    def _clear(self) -> None:
        if _TTY and self._active:
            sys.stdout.write("\r\033[K")
            self._active = False

    def _redraw(self) -> None:
        if self._pct is None or not _TTY:
            return
        filled = int(self.BAR_W * min(max(self._pct, 0.0), 100.0) / 100.0)
        bar = "█" * filled + "░" * (self.BAR_W - filled)
        detail = f"  {self._detail}" if self._detail else ""
        sys.stdout.write(f"\r\033[K{c('32', bar)} {self._pct:5.1f}%{detail}")
        sys.stdout.flush()
        self._active = True

    def done(self) -> None:
        """End the current bar so the next print starts on a clean line."""
        if _TTY and self._active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._active = False


# ── card discovery / selection ────────────────────────────────────────────────

def choose_card(explicit: str | None = None,
                prompt: str = "Which card?") -> str | None:
    """Return a card mountpoint, prompting with a numbered menu.

    `explicit` (a mountpoint/path) skips the menu. Returns None if the user
    backs out. Entries with no mountpoint (Linux, not mounted yet) are mounted
    on demand when chosen.
    """
    if explicit:
        mp = Path(explicit).expanduser()
        if not mp.is_dir():
            print(c("1;31", f"error: {mp} is not a directory / not mounted."))
            return None
        return str(mp)

    entries = cards.list_cards()
    print(c("1", f"\n{prompt}"))
    if entries:
        for i, e in enumerate(entries, 1):
            star = c("1;33", " ★ NOMAD") if e["is_nomad"] else ""
            state = e["mountpoint"] or c("2", "not mounted — will mount")
            free = f"free {human(e['free'])}" if e["free"] is not None else human(e["size"])
            print(f"  {i:>2}. {e['label']:<14} {(e['model'] or ''):<20} {free:<14} {state}{star}")
    else:
        print(c("2", "  (no removable cards detected — plug one in, or type a path)"))
    manual_n = len(entries) + 1
    print(f"  {manual_n:>2}. Enter a path manually")
    print(f"   0. Cancel")

    while True:
        raw = input("Choose a number: ").strip()
        if not raw.isdigit():
            print("  please type one of the numbers above.")
            continue
        n = int(raw)
        if n == 0:
            return None
        if 1 <= n <= len(entries):
            e = entries[n - 1]
            if e["mountpoint"]:
                return e["mountpoint"]
            print(f"  mounting {e['partition']} …")
            try:
                return cards.mount_partition(e["partition"], log=lambda s: print(f"    {s}"))
            except Exception as exc:
                print(c("1;31", f"  couldn't mount that card: {exc}"))
                continue
        if n == manual_n:
            p = Path(input("  path to card root: ").strip()).expanduser()
            if p.is_dir():
                return str(p)
            print("  that path isn't a directory.")
            continue
        print("  number out of range.")


def looks_like_nomad(root: str | Path) -> bool:
    """True if `root` has the shape of a nomad card (best-effort)."""
    try:
        return bool(cards.is_nomad_mount(root))
    except Exception:
        return any((Path(root) / d).is_dir() for d in ("Movies", "Shows", "Books"))
