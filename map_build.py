#!/usr/bin/env python3
"""
map_build: build map packs yourself from the newest OpenStreetMap data, straight onto a
Nomad card. You choose what each area gets and see what it costs the card before and
while it builds.

  1. the card: how full it is, and the maps already on it
  2. areas: any Geofabrik region (a country, a state, a county), several at once
  3. what they get: map detail (street level, town level, or none past the overview),
     directions, place search, addresses, nearby essentials, hiking trails. Each with its
     size, and the total against the card's free space. Your choices are remembered.
  4. the build: one line per step, what it added and what is left on the card. It stops
     before anything would fill the card.

Packs built here and packs from *Download map packs* sit together on the card and share
the map along their borders.

    ./map_build.py                                    # pick a card, then areas
    ./map_build.py /media/me/NOMAD --area us/washington,british-columbia
    ./map_build.py /media/me/NOMAD --area us --plan    # what all of the US would take
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import map_catalog as mc
import map_sizes
import nomad_common as nc
import pmtiles_extract as px

GEOFABRIK = "https://download.geofabrik.de/index-v1.json"
BUILDS = "https://build-metadata.protomaps.dev/builds.json"
PREFS = Path.home() / ".config" / "nomad-tools" / "map-build.json"
WORK = Path.home() / ".cache" / "nomad-tools" / "map-build"      # not the card (slow), not /tmp (RAM on some systems)

DETAIL = {   # key: (deepest zoom, name, what it shows)
    "street": (14, "Street level", "every street and building (zoom 14)"),
    "town": (12, "Town level", "towns and main streets (zoom 12), about a fifth the size"),
    "none": (0, "No close-up map", "only the overview around the area (zoom 9)"),
}
PARTS = [   # key, name, what it is
    ("directions", "Directions", "turn by turn, driving and walking"),
    ("search", "Place search", "towns, shops, parks and landmarks by name"),
    ("addresses", "Addresses", "house numbers, like 1600 Pike Place"),
    ("essentials", "Nearby essentials", "hospitals, water, shelter, fuel and more, even unnamed ones"),
    ("trails", "Hiking trails", "named hiking routes drawn on the map"),
]
DEFAULTS = {"detail": "street", "directions": True, "search": True, "addresses": True, "essentials": True,
            "trails": True}


def _json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": mc.UA}), timeout=120) as r:
        return json.load(r)


def newest_tiles() -> str:
    keys = sorted(b["key"] for b in _json(BUILDS) if b["key"].endswith(".pmtiles"))
    return "https://build.protomaps.com/" + keys[-1]


def memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def load_prefs() -> dict:
    try:
        return {**DEFAULTS, **json.loads(PREFS.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_prefs(p: dict) -> None:
    try:
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(p, indent=1), encoding="utf-8")
    except OSError:
        pass


# what is on the card

def card_maps(card: Path) -> dict[str, dict]:
    """The packs on the card by id (what "requires" names; the folder is in "_dir")."""
    out = {}
    maps = card / "Maps"
    if maps.is_dir():
        for d in sorted(maps.iterdir()):
            try:
                m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if m.get("type") == "pack":
                out[m.get("id") or d.name] = dict(m, _dir=d.name)
    return out


def pack_dir(card: Path, pid: str) -> Path:
    """The folder a pack is built into: its own if it is on the card already, otherwise the
    first free one. Maps/<id> may hold something else (an older region, the old world
    viewer, the owner's own files), and a build must never write over that."""
    have = card_maps(card).get(pid)
    if have:
        return card / "Maps" / have["_dir"]
    for name in [pid] + [f"{pid}-{i}" for i in range(2, 100)]:
        if not (card / "Maps" / name).exists():
            return card / "Maps" / name
    raise RuntimeError(f"no free folder name for {pid} in {card / 'Maps'}")


def _folder_bytes(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


def blocks_on_card(card: Path, blocks, maxzoom: int) -> set:
    """Of these blocks, the ones the card already has at maxzoom or deeper."""
    return {b for b in blocks if mc.block_maxzoom(card / mc.block_path(*b).lstrip("/")) >= maxzoom}


# choosing

def choose_areas(tree: mc.Tree) -> list[str]:
    """Continent, then country, then (if it has them) its parts. Several at once."""
    gid, level = None, [c for c in mc.CONTINENTS if c in tree.f]
    shown = lambda gs: sorted((g for g in gs if g not in mc.COMPOSITE or (g in mc.GROUPS and tree.kids.get(g))), key=tree.name)
    while True:
        print()
        for i, g in enumerate(level, 1):
            kids = shown(tree.kids.get(g, [])) if g not in mc.CONTINENTS else []
            print(f"  {i:>3}. {tree.name(g)}" + (nc.c("2", f"  ({len(kids)} parts)") if kids else ""))
        raw = input("Which ones (numbers, like 3 or 3,7)" + (", or Enter to go back: " if gid else ": ")).strip()
        if not raw:
            return []
        nums = [x for x in raw.replace(" ", ",").split(",") if x]
        if not all(x.isdigit() and 1 <= int(x) <= len(level) for x in nums):
            print("  Type numbers from the list.")
            continue
        picked = [level[int(x) - 1] for x in nums]
        if len(picked) == 1:
            g = picked[0]
            kids = shown(tree.kids.get(g, []))
            if g in mc.CONTINENTS or (kids and input(f"{tree.name(g)}: all of it, or pick parts? [a/p] ").strip().lower() == "p"):
                gid, level = g, kids
                continue
        return picked


def plan(basket: list[dict], prefs: dict, card: Path, have: dict) -> dict:
    """Sizes of everything the choices add, per area and in all. Blocks two areas share,
    or that the card has, count once."""
    zmax = DETAIL[prefs["detail"]][0]
    counted, rows, total = set(), [], 0
    for a in basket:
        row = {"area": a, "parts": {}, "shared": 0}
        if zmax:
            mine = set(a["blocks"])
            on_card = blocks_on_card(card, mine, zmax)
            new = mine - on_card - counted
            row["shared"] = len(mine - new)
            row["parts"]["map"] = map_sizes.blocks_bytes(new, zmax)
            counted |= new
        est = map_sizes.parts(a["size"])
        for k, _, _ in PARTS:
            if prefs[k]:
                row["parts"][k] = est[k]
        # building an area again replaces its own files (its map squares stay: they are shared)
        row["replaces"] = _folder_bytes(card / "Maps" / have[a["id"]]["_dir"]) if a["id"] in have else 0
        row["total"] = sum(row["parts"].values())
        total += row["total"]
        rows.append(row)
    base = []
    if "world" not in have:
        base.append(("world", "World (zoomed out, needed once)", map_sizes.world_bytes()))
    for c in sorted({a["continent"] for a in basket}):
        if f"overview-{c}" not in have:
            blocks = [b for a in basket if a["continent"] == c for b in a["blocks"]]
            base.append((f"overview-{c}", f"{mc.CONTINENTS[c]} overview (needed once)", map_sizes.overview_bytes(blocks)))
    total += sum(n for _, _, n in base)
    freed = sum(r["replaces"] for r in rows)
    return {"rows": rows, "base": base, "total": total, "net": total - freed, "zmax": zmax}


def show_plan(p: dict, prefs: dict, space: nc.CardSpace) -> None:
    n = len(p["rows"])
    print(nc.c("1", f"\nWhat {'each area' if n > 1 else 'it'} gets") + nc.c("2", f"  (sizes for {'all ' + str(n) + ' areas' if n > 1 else 'this area'})"))
    zname = DETAIL[prefs["detail"]]
    tot = {k: sum(r["parts"].get(k, 0) for r in p["rows"]) for k in ["map"] + [k for k, _, _ in PARTS]}
    print(f"   m. Map detail: {zname[1]:<22} {nc.human(tot['map']) if p['zmax'] else '':>9}  " + nc.c("2", zname[2]))
    for i, (k, name, what) in enumerate(PARTS, 1):
        box = nc.c("32", "[x]") if prefs[k] else "[ ]"
        size = nc.human(tot[k]) if prefs[k] else ""
        print(f"   {i}. {box} {name:<22} {size:>9}  " + nc.c("2", what))
    print()
    for r in p["rows"]:
        a = r["area"]
        bits = []
        if "map" in r["parts"]:
            bits.append(f"map {nc.human(r['parts']['map'])}" + (f" ({r['shared']} squares already there)" if r["shared"] else ""))
        bits += [f"{k} {nc.human(v)}" for k, v in r["parts"].items() if k != "map"]
        tail = nc.c("2", "  replaces the one on the card") if r["replaces"] else ""
        print(f"  {a['name'][:24]:<24} {nc.human(r['total']):>9}  " + nc.c("2", ", ".join(bits)) + tail)
    for _, name, size in p["base"]:
        print(f"  {name[:40]:<40} {nc.human(size):>9}")
    print(nc.c("1", f"  {'Total':<24} {nc.human(p['total']):>9}") + nc.c("2", "  (close estimates until it's built)"))
    print()
    space.show(adding=max(0, p["net"]), what="these maps")


# building

def _download(url: str, dest: Path, size: int) -> None:
    if dest.exists() and dest.stat().st_size == size:
        return
    part = dest.with_name(dest.name + ".part")
    prog = nc.Progress()
    got = 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": mc.UA}), timeout=120) as r, \
            open(part, "wb") as f:
        while (b := r.read(1 << 20)):
            f.write(b)
            got += len(b)
            prog.pct(got * 100.0 / max(1, size), f"{nc.human(got)} of {nc.human(size)}")
    prog.done()
    os.replace(part, dest)


def _copy(src: Path, dst: Path, space: nc.CardSpace, what: str) -> int:
    """A built folder onto the card, if it fits; returns what it added."""
    size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    space.need(size, what)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return size


def build_area(a: dict, prefs: dict, tree: mc.Tree, card: Path, tiles: str, space: nc.CardSpace, quiet) -> None:
    aid = a["id"]
    out = pack_dir(card, aid)
    zmax = DETAIL[prefs["detail"]][0]
    wants = [k for k, _, _ in PARTS if prefs[k]]
    work = WORK / aid
    work.mkdir(parents=True, exist_ok=True)
    stage = work / "pack"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    man = {"type": "pack", "version": 2, "id": aid, "name": tree.name(a["gid"]), "requires": ["world", f"overview-{a['continent']}"],
           "attribution": mc.ATTRIBUTION, "packVersion": "own-" + time.strftime("%Y-%m-%d"),
           "source": {"tiles": tiles.rsplit("/", 1)[-1], "roads": a["pbf"].rsplit("/", 1)[-1]}, "built": dict(prefs)}

    if zmax:
        est = map_sizes.blocks_bytes(set(a["blocks"]) - blocks_on_card(card, a["blocks"], zmax), zmax)
        space.need(int(est * 1.1), f"{a['name']} map")
        paths = {b: card / mc.block_path(*b).lstrip("/") for b in a["blocks"]}
        before = {b: (p.stat().st_size if p.exists() else 0) for b, p in paths.items()}
        have = mc.cut_maps([dict(a)], tree, WORK, card, tiles, log=quiet, maxzoom=zmax, overviews=False)
        grew = sum(max(0, (p.stat().st_size if p.exists() else 0) - before[b]) for b, p in paths.items())
        blocks = sorted(b for b in a["blocks"] if b in have)
        man["blocks"] = {"zoom": mc.BLOCK_Z, "minZoom": 10, "maxZoom": zmax, "dir": "/Maps/blocks", "tiles": [list(b) for b in blocks]}
        man["minZoom"], man["maxZoom"] = 10, zmax
        new = sum(1 for b in blocks if not before[b])
        space.added(f"map, {len(blocks)} square{'s' if len(blocks) != 1 else ''}"
                    + (f" ({len(blocks) - new} already there)" if new < len(blocks) else ""), grew)

    if wants:
        pbf = work / "extract.osm.pbf"
        print(nc.c("2", f"    downloading the OpenStreetMap data ({nc.human(a['size'])}, deleted after the build)"))
        _download(a["pbf"], pbf, a["size"])
        import route_pack
        if "directions" in wants:
            st = route_pack.routing([pbf], stage, quiet)
            man["routing"] = {"format": "nrg3", "index": "routing/roads.idx", "data": "routing/roads.bin",
                              "bytes": st["bytes"] + st["idx_bytes"], "bounds": st["bbox"], "levels": st["levels"]}
            man["bounds"] = st["bbox"]
            space.added("directions", _copy(stage / "routing", out / "routing", space, f"{a['name']} directions"))
        if "search" in wants:
            route_pack.places([pbf], stage, quiet)
            man["places"] = {"index": "places/index.json", "data": "places/places.tsv"}
            space.added("place search", _copy(stage / "places", out / "places", space, f"{a['name']} search"))
        extras = [k for k in ("essentials", "addresses", "trails") if k in wants]
        if extras:
            import osm_extras
            osm_extras.write(osm_extras.read([str(pbf)], quiet), stage, quiet, parts=extras)
            names = {"essentials": "nearby essentials", "addresses": "addresses", "trails": "hiking trails"}
            for k in extras:
                man[k] = {f: f"{k}/{n}" for f, n in mc.EXTRAS[k].items()}
                space.added(names[k], _copy(stage / k, out / k, space, f"{a['name']} {names[k]}"))
        pbf.unlink(missing_ok=True)
    if "bounds" not in man:
        pts = [p for poly in tree.polys(a["gid"]) for ring in poly for p in ring]
        man["bounds"] = [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]
    out.mkdir(parents=True, exist_ok=True)
    # parts left over from an earlier build of this area with other choices
    for k in ("routing", "places", "essentials", "addresses", "trails"):
        if k not in man and (out / k).exists():
            shutil.rmtree(out / k)
    man["sizeBytes"] = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) + \
        sum((card / mc.block_path(x, y).lstrip("/")).stat().st_size for x, y in man.get("blocks", {}).get("tiles", []))
    (out / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)


def build_base(pid: str, basket: list[dict], card: Path, tiles: str, space: nc.CardSpace) -> None:
    """World (zoom 0-6), or a continent's overview (zoom 7-9) around the areas being built.
    A downloaded overview replaces it with the whole continent."""
    d = pack_dir(card, pid)
    d.mkdir(parents=True, exist_ok=True)
    if pid == "world":
        space.need(map_sizes.world_bytes(), "World")
        px.extract(tiles, d / "world.pmtiles", [-180, -85, 180, 85], 0, 6, log=lambda *a, **k: None)
        man = {"type": "pack", "version": 2, "id": "world", "name": "World", "bounds": [-180, -85, 180, 85],
               "minZoom": 0, "maxZoom": 6, "map": {"file": "world.pmtiles", "kind": "vector", "schema": "protomaps-v4"}}
        what = "World"
    else:
        c = pid[len("overview-"):]
        blocks = {b for a in basket if a["continent"] == c for b in a["blocks"]}
        space.need(map_sizes.overview_bytes(blocks), f"{mc.CONTINENTS[c]} overview")
        box = mc._bbox_of([mc.tile_bounds(mc.BLOCK_Z, x, y) for x, y in blocks])
        px.extract(tiles, d / "overview.pmtiles", box, 7, 9, log=lambda *a, **k: None, ids=sorted(mc.under_blocks(blocks, 7, 9)))
        cover = sorted({(x >> (mc.BLOCK_Z - 5), y >> (mc.BLOCK_Z - 5)) for x, y in blocks})
        man = {"type": "pack", "version": 2, "id": pid, "name": f"{mc.CONTINENTS[c]} overview", "bounds": box,
               "minZoom": 7, "maxZoom": 9, "cover": {"zoom": 5, "tiles": [list(t) for t in cover]},
               "map": {"file": "overview.pmtiles", "kind": "vector", "schema": "protomaps-v4"}}
        what = f"{mc.CONTINENTS[c]} overview"
    man.update({"attribution": mc.ATTRIBUTION, "packVersion": "own-" + time.strftime("%Y-%m-%d"),
                "sizeBytes": sum(f.stat().st_size for f in d.rglob("*") if f.is_file() and f.name != "manifest.json")})
    (d / "manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    space.added(what, _folder_bytes(d))


# the whole thing

def run(dest: str | None = None, areas: list[str] | None = None, assume_yes: bool = False, plan_only: bool = False) -> int:
    nc.banner("Build map packs yourself")
    print("Builds map packs on this computer from the newest OpenStreetMap data. Pick your")
    print("areas, choose what they get, and see how much space it takes before anything is built.")
    try:
        print(nc.c("2", "Getting the list of areas from Geofabrik..."))
        idx = Path(tempfile.gettempdir()) / "nomad-geofabrik-index.json"
        if not idx.exists() or time.time() - idx.stat().st_mtime > 86400:
            with urllib.request.urlopen(urllib.request.Request(GEOFABRIK, headers={"User-Agent": mc.UA}), timeout=120) as r:
                idx.write_bytes(r.read())
        tree = mc.Tree(idx)
        tiles = newest_tiles()
    except Exception as e:                                        # noqa: BLE001
        print(nc.c("1;31", f"Couldn't reach Geofabrik or Protomaps ({e}). Building map packs needs the internet."))
        return 1
    dest = nc.choose_card(dest, prompt="Which card should the maps go on?")
    if not dest:
        print("Cancelled.")
        return 1
    card = Path(dest)
    space = nc.CardSpace(card)
    have = card_maps(card)
    print(nc.c("1", "\nThe card"))
    space.show()
    if have:
        on = [m for m in have.values()]
        print(nc.c("2", f"  maps already on it: {', '.join(m.get('name', '?') for m in on[:8])}{'...' if len(on) > 8 else ''}"
                        f" ({nc.human(sum(m.get('sizeBytes', 0) for m in on))})"))

    # areas
    basket: list[dict] = []

    def add(gids):
        print(nc.c("2", "  checking sizes..."))
        for a in mc.pick_areas(tree, gids, log=lambda m: print(nc.c("2", m))):
            if not any(b["id"] == a["id"] for b in basket):
                a["blocks"] = mc.area_blocks(tree.polys(a["gid"]))
                basket.append(a)

    if areas:
        unknown = [g for g in areas if g not in tree.f]
        if unknown:
            print(nc.c("31", f"Geofabrik has no area called {', '.join(unknown)}."))
            return 1
        add(areas)
    while not areas:
        print(nc.c("1", "\nAreas to build"))
        if basket:
            for i, a in enumerate(basket, 1):
                print(f"  {i:>3}. {a['name']:<28} " + nc.c("2", f"{mc.CONTINENTS[a['continent']]}, {nc.human(a['size'])} of OpenStreetMap data"))
        else:
            print(nc.c("2", "  none yet"))
        raw = input("a) add areas" + ("   r) remove one   n) next" if basket else "") + "   (Enter cancels): ").strip().lower()
        if not raw:
            print("Cancelled.")
            return 1
        if raw == "a":
            picked = choose_areas(tree)
            if picked:
                add(picked)
        elif raw == "r" and basket:
            n = input("Which number to remove: ").strip()
            if n.isdigit() and 1 <= int(n) <= len(basket):
                basket.pop(int(n) - 1)
        elif raw == "n" and basket:
            break

    # what they get
    prefs = load_prefs()
    while True:
        p = plan(basket, prefs, card, have)
        show_plan(p, prefs, space)
        if plan_only:
            return 0
        if assume_yes:
            break
        raw = input("Type a number to turn it on or off, m for map detail" + ("" if areas else ", b to change areas")
                    + ", or Enter to build (q quits): ").strip().lower()
        if raw == "q":
            print("Cancelled.")
            return 1
        if not raw:
            if p["net"] > space.room:
                print(nc.c("1;31", "That won't fit. Turn something off, pick Town level, or pick fewer areas."))
                continue
            break
        if raw == "m":
            keys = list(DETAIL)
            for i, k in enumerate(keys, 1):
                print(f"   {i}. {DETAIL[k][1]:<18} " + nc.c("2", DETAIL[k][2]))
            n = input("Which one: ").strip()
            if n.isdigit() and 1 <= int(n) <= len(keys):
                prefs["detail"] = keys[int(n) - 1]
        elif raw == "b" and not areas:
            return run(str(card))
        elif raw.isdigit() and 1 <= int(raw) <= len(PARTS):
            k = PARTS[int(raw) - 1][0]
            prefs[k] = not prefs[k]
    if p["net"] > space.room:
        print(nc.c("1;31", "That won't fit on the card."))
        return 1
    save_prefs(prefs)

    # this computer: memory for directions, room for the extract while it builds
    biggest = max(a["size"] for a in basket)
    ram = memory_bytes()
    if prefs["directions"] and ram and biggest * 16 > ram * 0.8:
        print(nc.c("1;31", f"Directions for the biggest area need about {nc.human(biggest * 16)} of memory, and this computer "
                           f"has {nc.human(ram)}. Pick its parts instead, or turn directions off."))
        return 1
    WORK.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(WORK).free < biggest * 3:
        print(nc.c("1;31", f"This computer needs about {nc.human(biggest * 3)} free while it builds "
                           f"(in {WORK}). It has {nc.human(shutil.disk_usage(WORK).free)}."))
        return 1
    if not assume_yes and input("Build it? [Y/n] ").strip().lower() in ("n", "no"):
        print("Cancelled.")
        return 1

    quiet = lambda *a, **k: None
    done = 0
    try:
        for pid, name, _ in p["base"]:
            print(nc.c("1;36", f"\n▶ {name.split(' (')[0]}"))
            build_base(pid, basket, card, tiles, space)
        for a in basket:
            t0 = time.time()
            print(nc.c("1;36", f"\n▶ {a['name']}"))
            try:
                build_area(a, prefs, tree, card, tiles, space, quiet)
                done += 1
                print(nc.c("2", f"    done in {time.time() - t0:.0f} s"))
            except nc.CardFull as e:
                print(nc.c("1;31", f"    stopped: {e}. Nothing more was written."))
                break
            except Exception as e:                               # noqa: BLE001
                print(nc.c("31", f"    {a['name']}: {e}"))
    except KeyboardInterrupt:
        print(nc.c("33", "\n  Stopped. Run it again to build what's left."))
        return 130
    except nc.CardFull as e:
        print(nc.c("1;31", f"  stopped: {e}."))
    shutil.rmtree(WORK, ignore_errors=True)
    print(nc.c("1", f"\nBuilt {done} of {len(basket)} area(s)."))
    space.summary()
    nc.hidden_page_note(card, "maps", "Maps")
    return 0 if done == len(basket) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Build map packs yourself from the newest OpenStreetMap data. Slow, and it needs a lot of memory.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    ap.add_argument("--area", help="Geofabrik area ids, comma separated (us/washington, british-columbia, germany...)")
    ap.add_argument("--plan", action="store_true", help="with --area: show how much space it would take, then stop without building")
    a = ap.parse_args()
    return run(a.card, a.area.split(",") if a.area else None, assume_yes=bool(a.area), plan_only=a.plan)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
