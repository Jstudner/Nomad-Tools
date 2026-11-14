#!/usr/bin/env python3
"""
Shared bits every Nomad card tool uses: the colours, the live progress bar,
and the "pick a card" menu.

Card discovery works on Linux, Windows and macOS (see cards.py). This folder is
self-contained. It needs only Python (plus Pillow for image work and Node for
ZIM work), not a Nomad Manager install, so it can be copied and run anywhere.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import cards


# terminal styling

def _prepare_console() -> None:
    """Make this tool's output safe on Windows.

    1. The box-drawing and tick characters aren't in cp1252 or cp437. A console
       window copes (Python writes UTF-16), but once output is redirected or
       piped Python falls back to the ANSI code page and the first banner dies
       with UnicodeEncodeError. With UTF-8 and errors="replace" the worst case
       is a "?".
    2. Colours are ANSI escapes, and conhost ignores those unless the process
       opts in. Without this the menu prints literal <-[1;36m codes.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass                                  # already fine, or not a real stream
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            for handle_id in (-11, -12):          # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = k32.GetStdHandle(handle_id)
                mode = ctypes.c_uint32()
                if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                    # ENABLE_VIRTUAL_TERMINAL_PROCESSING, OR'd in so the rest stays
                    k32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:                         # noqa: BLE001
            pass                                  # older Windows: colours off, still readable


_prepare_console()

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
    can be handed straight to Nomad Manager's sd/archives.py, which drives those.
    Falls back to plain prints when stdout isn't a terminal.
    """

    BAR_W = 32

    def __init__(self) -> None:
        self._pct: float | None = None
        self._detail: str | None = None
        self._active = False   # a bar is currently drawn on the line

    # log a line
    def __call__(self, line: str) -> None:
        self._clear()
        print(line)
        self._redraw()

    # sd/archives.py interface
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

    # rendering
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


# card space

class CardFull(Exception):
    """A step would leave the card with less than its reserve."""


class CardSpace:
    """How full a card is, what a job will add, and a stop before it overfills.

    Every tool that puts files on a card can use it the same way:

        space = CardSpace(card)
        space.show()                                   # where the card stands
        space.show(adding=plan_bytes, what="maps")     # ... and after the plan
        space.need(file_size, "Washington map")        # raises CardFull if it won't fit
        space.added("Washington map", n)               # one line per step: what went on, what's left

    A reserve stays free (1% of the card, at least 256 MB). A card filled to the last
    byte is slow to write and leaves no room for Nomad's own index files."""

    BAR_W = 30

    def __init__(self, root, reserve: int | None = None) -> None:
        self.root = Path(root)
        self.refresh()
        self.reserve = reserve if reserve is not None else max(256 * 1024 ** 2, self.total // 100)
        self.tally = 0                 # what added() has reported: the files this job wrote

    def refresh(self) -> None:
        du = shutil.disk_usage(self.root)
        self.total, self.used, self.free = du.total, du.used, du.free

    @property
    def room(self) -> int:
        """What can still go on before the reserve."""
        self.refresh()
        return max(0, self.free - self.reserve)

    def bar(self, adding: int = 0) -> str:
        w = self.BAR_W
        used = int(w * self.used / self.total) if self.total else 0
        add = min(w - used, int(round(w * adding / self.total))) if self.total and adding else 0
        if adding and not add:
            add = 1 if used < w else 0
        over = adding > self.free - self.reserve
        return (c("36", "█" * used) + c("31" if over else "33", "█" * add) + c("2", "░" * (w - used - add)))

    def show(self, adding: int = 0, what: str = "this") -> bool:
        """Print where the card stands (and after `adding` more). False if it won't fit."""
        self.refresh()
        print(f"  {self.bar(adding)}  {human(self.used)} used of {human(self.total)}, {human(self.free)} free")
        if adding:
            after = self.free - adding
            ok = adding <= self.free - self.reserve
            line = f"  {'+' if ok else '!'} {human(adding)} for {what} → {human(max(0, after))} free after"
            print(c("33" if ok else "1;31", line) if ok else c("1;31", line + ", more than the card has room for"))
            return ok
        return True

    def need(self, n: int, what: str) -> None:
        if n > self.room:
            raise CardFull(f"{what} needs {human(n)}, the card has {human(self.room)} to spare")

    def added(self, what: str, n: int) -> None:
        """One line per finished step: n is the size of the files the step wrote (the card's
        own used figure moves with filesystem rounding and anything else writing to it)."""
        self.tally += n
        self.refresh()
        print(f"    {c('32', '+' + human(n)):>18}  {what:<34} {c('2', f'{human(self.free)} free')}")

    def summary(self) -> None:
        self.refresh()
        print(f"  {self.bar()}  {human(self.used)} used of {human(self.total)}, {human(self.free)} free"
              + (f"  ({human(self.tally)} added)" if self.tally else ""))


# card discovery and selection

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
            print(c("1;31", f"Can't find {mp}. Is the card plugged in?"))
            return None
        return str(mp)

    entries = cards.list_cards()
    print(c("1", f"\n{prompt}"))
    if entries:
        for i, e in enumerate(entries, 1):
            star = c("1;33", " ★ NOMAD") if e["is_nomad"] else ""
            state = e["mountpoint"] or c("2", "not mounted, will mount")
            free = f"free {human(e['free'])}" if e["free"] is not None else human(e["size"])
            print(f"  {i:>2}. {e['label']:<14} {(e['model'] or ''):<20} {free:<14} {state}{star}")
    else:
        print(c("2", "  (no cards found. Plug one in, or type where it is)"))
    manual_n = len(entries) + 1
    print(f"  {manual_n:>2}. Type where the card is")
    print(f"   0. Cancel")

    while True:
        raw = input("Choose a number: ").strip()
        if not raw.isdigit():
            print("  Type one of the numbers above.")
            continue
        n = int(raw)
        if n == 0:
            return None
        if 1 <= n <= len(entries):
            e = entries[n - 1]
            if e["mountpoint"]:
                return e["mountpoint"]
            print(f"  Mounting {e['partition']}...")
            try:
                return cards.mount_partition(e["partition"], log=lambda s: print(f"    {s}"))
            except Exception as exc:
                print(c("1;31", f"  Couldn't mount that card: {exc}"))
                continue
        if n == manual_n:
            p = Path(input("  Where is the card (for example E:\\ or /media/you/NOMAD): ").strip()).expanduser()
            if p.is_dir():
                return str(p)
            print("  That isn't a folder.")
            continue
        print("  That number isn't on the list.")


def looks_like_nomad(root: str | Path) -> bool:
    """True if `root` has the shape of a Nomad card (best-effort)."""
    try:
        return bool(cards.is_nomad_mount(root))
    except Exception:
        return any((Path(root) / d).is_dir() for d in ("Movies", "Shows", "Books"))


def page_hidden(root: str | Path, page: str) -> bool:
    """True if the card's menu hides `page` (the admin panel's Pages, kept in /.system-ui.json).

    The card template ships Games, Maps, Archive, Chat, Translate, Cookbook and
    Workshop hidden, so content added for them is invisible until switched on.
    """
    try:
        cfg = json.loads((Path(root) / ".system-ui.json").read_text(encoding="utf-8"))
        return page in (cfg.get("hiddenPages") or [])
    except (OSError, ValueError, AttributeError):
        return False


def hidden_page_note(root: str | Path, page: str, label: str) -> None:
    """Say where to turn a page on, if it is hidden on this card."""
    if page_hidden(root, page):
        print(c("1;33", f"  The {label} page is switched off on this card. Turn it on in "
                        f"the admin panel, under Pages."))
