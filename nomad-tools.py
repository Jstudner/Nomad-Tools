#!/usr/bin/env python3
"""
nomad-tools: one menu for every Nomad card tool.

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
import zim_prepare
import pictures
import fix_videos
import subtitles
import games_prepare
import build_index
import map_buckets
import map_tiles
import map_packs
import map_build
import optimize_tiles
import translate_packs
import format_card

MENU = [
    ("Add media",
     "Copy films, shows, music, books, photos, games and more onto the card. It tells you first if anything won't show up.",
     add_media.run),
    ("Check the card",
     "Goes through a card you filled yourself and lists anything Nomad won't show. Doesn't change anything.",
     add_media.run_check),
    ("Add ZIM archives",
     "Copy offline libraries like Wikipedia onto the card and build their search.",
     import_zim.run),
    ("Speed up archives",
     "Reworks archives already on the card so they load faster for everyone. Nothing in them changes.",
     zim_prepare.run),
    ("Pictures",
     "Gallery previews, missing book covers, film stills and smaller covers. Asks before replacing anything.",
     pictures.run),
    ("Fix videos",
     "Finds videos that won't play or are slow to start on Nomad, and fixes them. Asks first.",
     fix_videos.run),
    ("Subtitles",
     "Pulls subtitles out of your videos so Nomad can show them. Can also make seek previews (the little pictures when you drag the bar).",
     subtitles.run),
    ("Speed up games",
     "Compresses the games so they start about twice as fast. Saves are kept. Needs up to date Nomad firmware.",
     games_prepare.run),
    ("Rebuild the media index",
     "Updates the list Nomad uses to show your stuff. Run it after adding or removing things, it's safe to run any time.",
     build_index.run),
    ("Download map packs",
     "Ready made maps by area, with directions and search. Add them or take them off.",
     map_packs.run),
    ("Download map tiles",
     "Map tiles for any area you pick. Checks the size and your free space first.",
     map_tiles.run),
    ("Build map packs yourself",
     "Same as the map packs, but built from the newest OpenStreetMap data. Slow, and it needs a lot of memory.",
     map_build.run),
    ("Map areas and sizes",
     "How much space each state, region or set would take. Doesn't download anything.",
     map_buckets.run),
    ("Shrink map tiles",
     "Converts a map area's tiles to WebP, about 40% smaller. Replaces the originals.",
     optimize_tiles.run),
    ("Translation languages",
     "Language packs for the Translate page. Each language works both ways.",
     translate_packs.run),
    ("Format a card",
     "Wipes a card and sets it up as exFAT. Linux only, and it erases everything on the card!",
     format_card.run),
]


def _pick_card() -> str | None:
    """Choose one card up front; tools reuse it so you're not asked each time."""
    card = nc.choose_card(prompt="Which card are we working on?")
    if card:
        tag = "  ★ looks like a Nomad card" if nc.looks_like_nomad(card) else ""
        print(nc.c("2", f"  using {card}{tag}"))
    return card


def main() -> int:
    print(nc.c("1;36", "\n╔════════════════════════════════╗"))
    print(nc.c("1;36",   "║        Nomad Card Tools        ║"))
    print(nc.c("1;36",   "╚════════════════════════════════╝"))

    card = _pick_card()
    if not card:
        print("No card picked, see you next time.")
        return 0

    while True:
        print(nc.c("1", "\nWhat do you want to do?"))
        for i, (title, blurb, _) in enumerate(MENU, 1):
            print(f"  {i}. {nc.c('1', title)}")
            print(nc.c("2", f"       {blurb}"))
        print(f"  c. Use a different card")
        print(f"  0. Quit")

        choice = input("Choose: ").strip().lower()
        if choice in ("0", "q", "quit", "exit"):
            print("All done. Eject the card safely before you unplug it.")
            return 0
        if choice == "c":
            card = _pick_card() or card
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            _, _, action = MENU[int(choice) - 1]
            try:
                action(dest=card)
            except KeyboardInterrupt:
                print(nc.c("1;31", "\n  Stopped, back to the menu."))
            except EOFError:
                raise
            except Exception as e:       # one tool failing shouldn't close the menu
                print(nc.c("1;31", f"\n  Something went wrong: {e}"))
                print(nc.c("2", "  Back to the menu."))
            continue
        print(f"  Type a number from 1 to {len(MENU)}, c, or 0.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except EOFError:                     # stdin closed or piped: not a crash
        print("\nNo input, stopping.")
        sys.exit(1)
