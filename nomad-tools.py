#!/usr/bin/env python3
"""
nomad-tools — one friendly menu for every nomad card tool.

Run this and it walks you through the three jobs you might want to do to a card:

    1. Import ZIM archives   copy Wikipedia/Gutenberg/etc. onto the card and
                             build the search index nomad uses to find articles.
    2. Optimize images       shrink cover art in Movies/Shows/Books so pages
                             load fast on-device (you pick which folders).
    3. Rebuild media index   refresh the browse index after you add or remove
                             media, so nomad shows the right files.

Pick a card once; then do as many jobs as you like before quitting. Each tool
can also be run on its own — see the README.

    ./nomad-tools.py
"""
from __future__ import annotations

import sys

import nomad_common as nc
import import_zim
import optimize_images
import build_index

MENU = [
    ("Import ZIM archives",
     "Copy .zim archives onto the card and build the article search index.",
     import_zim.run),
    ("Optimize cover images",
     "Shrink Movies/Shows/Books cover art (you choose which). Overwrites in place.",
     optimize_images.run),
    ("Rebuild media index",
     "Refresh the browse index after adding or removing media. Non-destructive.",
     build_index.run),
]


def _pick_card() -> str | None:
    """Choose one card up front; tools reuse it so you're not asked each time."""
    card = nc.choose_card(prompt="Which nomad card are we working on?")
    if card:
        tag = "  ★ looks like nomad" if nc.looks_like_nomad(card) else ""
        print(nc.c("2", f"  using {card}{tag}"))
    return card


def main() -> int:
    print(nc.c("1;36", "\n╔════════════════════════════════╗"))
    print(nc.c("1;36",   "║        nomad card tools        ║"))
    print(nc.c("1;36",   "╚════════════════════════════════╝"))

    card = _pick_card()
    if not card:
        print("No card selected — bye.")
        return 0

    while True:
        print(nc.c("1", "\nWhat would you like to do?"))
        for i, (title, blurb, _) in enumerate(MENU, 1):
            print(f"  {i}. {nc.c('1', title)}")
            print(nc.c("2", f"       {blurb}"))
        print(f"  c. Switch to a different card")
        print(f"  0. Quit")

        choice = input("Choose: ").strip().lower()
        if choice in ("0", "q", "quit", "exit"):
            print("Done. Eject the card safely before removing it.")
            return 0
        if choice == "c":
            card = _pick_card() or card
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            _, _, action = MENU[int(choice) - 1]
            try:
                action(dest=card)
            except KeyboardInterrupt:
                print(nc.c("1;31", "\n  interrupted — back to the menu."))
            continue
        print("  type 1, 2, 3, c, or 0.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye.")
        sys.exit(130)
