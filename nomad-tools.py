#!/usr/bin/env python3
"""
nomad-tools: one friendly menu for every nomad card tool.

Pick a card once, then do as many jobs as you like before quitting. The jobs
are the MENU list below, and each tool can also be run on its own, see the
README.

    ./nomad-tools.py
"""
from __future__ import annotations

import sys

import nomad_common as nc
import add_media
import import_zim
import optimize_images
import build_index
import map_buckets
import map_tiles
import nomad_route
import optimize_tiles
import translate_packs
import format_card

MENU = [
    ("Add media to the card",
     "Films, shows, books, music, photos, games, recipes, models. Checks the format first.",
     add_media.run),
    ("Check what nomad will show",
     "Walk a card you already filled in and find what the device ignores. Changes nothing.",
     add_media.run_check),
    ("Import ZIM archives",
     "Copy .zim archives onto the card and build the article search index.",
     import_zim.run),
    ("Optimize cover images",
     "Shrink Movies/Shows/Books cover art (you choose which). Overwrites in place.",
     optimize_images.run),
    ("Rebuild media index",
     "Refresh the browse index after adding or removing media. Non-destructive.",
     build_index.run),
    ("Download offline maps",
     "Pull map tiles for a region into /Maps, with a size estimate and space check first.",
     map_tiles.run),
    ("Build a routing map",
     "Pick states or regions for offline turn by turn directions. Opened by URL, not the Maps list.",
     nomad_route.run),
    ("See map areas and sizes",
     "What every state, region and set would cost on the card. Downloads nothing.",
     map_buckets.run),
    ("Optimize map tiles",
     "Re-encode a region's tiles to WebP, about 40% smaller. Replaces the original tiles.",
     optimize_tiles.run),
    ("Add translation languages",
     "Language packs for the Translate page. Each language installs both directions.",
     translate_packs.run),
    ("Format a card as exFAT",
     "Erase a card and set it up as exFAT (Linux only). Destroys everything on it.",
     format_card.run),
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
        print("No card selected, bye.")
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
                print(nc.c("1;31", "\n  interrupted, back to the menu."))
            except EOFError:
                raise
            except Exception as e:       # one tool failing shouldn't close the menu
                print(nc.c("1;31", f"\n  error: {e}"))
                print(nc.c("2", "  back to the menu."))
            continue
        print(f"  type 1-{len(MENU)}, c, or 0.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye.")
        sys.exit(130)
    except EOFError:                     # stdin closed or piped: not a crash
        print("\nno input, stopping.")
        sys.exit(1)
