#!/usr/bin/env python3
"""
map_packs: download map packs onto a Nomad card, and take them off again.

Packs come from Nomad's download site (map_catalog.py builds them). An area pack (a US
state, a Canadian province, a country or part of one) is its street map, turn by turn
directions and place search. It builds on packs every card with maps has: World (zoomed
out), its continent's overview (the map between the areas you have) and its continent's
highways (directions on the big roads everywhere, so trips cross areas you don't have).
Pick areas and those come along by themselves.

Start from a ready-made collection (the whole world, a continent, a country, a US region
like the Northeast or New England), or browse by continent and country.

Nothing is stored twice. An area's street map is a set of shared blocks (~50 km squares);
a block on a border is one file however many of your areas use it, so neighbours together
cost less than apart, and adding one next to what you have only downloads what is new.
Taking an area off removes only what no other pack on the card still uses.

Every file is checked against its SHA-256 before it counts, an interrupted download
picks up where it stopped, and /Maps/packs.json remembers what was checked so a card
is never hashed twice.

    ./map_packs.py                                        # pick a card, then packs
    ./map_packs.py /media/me/NOMAD --list                 # what's available, download nothing
    ./map_packs.py /media/me/NOMAD --get "US Northeast"   # a collection, by name or id
    ./map_packs.py /media/me/NOMAD --get us-washington,british-columbia
    ./map_packs.py /media/me/NOMAD --remove us-oregon
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

import add_media
import nomad_common as nc

INDEX = "https://nomadflash.jcorptech.net/packs/v2/index.json"
UA = "nomad-card-tools-map-packs/2.0"
FAT32_MAX = 4 * 1024 ** 3 - 1
RECORD = "Maps/packs.json"            # path -> sha256 of every pack file checked onto this card


def _open(url: str, offset: int = 0):
    headers = {"User-Agent": UA}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


class Catalog:
    """The pack list, and each pack's own file list when it is wanted. A local folder
    works as well as the site (testing a build before it goes up)."""

    def __init__(self, index: str):
        if index.startswith(("http://", "https://")):
            self.base = index.rsplit("/", 1)[0] + "/"
            self.local = None
            with _open(index) as r:
                self.idx = json.load(r)
        else:
            p = Path(index).expanduser().resolve()
            p = p / "index.json" if p.is_dir() else p
            self.local = p.parent
            self.base = p.parent.as_uri() + "/"
            self.idx = json.loads(p.read_text(encoding="utf-8"))
        if self.idx.get("format") != 2:
            raise ValueError("this pack list is in a format this version can't read. Update Nomad Tools and try again")
        self.packs = [p for p in self.idx["packs"] if p.get("kind") == "maps"]
        self.collections = self.idx.get("collections", [])
        self.by_id = {p["id"]: p for p in self.packs}
        self._full: dict[str, dict] = {}

    def url(self, path: str) -> str:
        return self.base + path.lstrip("/")

    def full(self, pid: str) -> dict:
        if pid not in self._full:
            rel = self.by_id[pid]["manifest"]
            if self.local:
                self._full[pid] = json.loads((self.local / rel).read_text(encoding="utf-8"))
            else:
                with _open(self.url(rel)) as r:
                    self._full[pid] = json.load(r)
        return self._full[pid]

    def closure(self, ids: list[str]) -> list[str]:
        """These packs and everything they build on, the foundations first."""
        out: list[str] = []
        def add(pid):
            if pid in out:
                return
            for r in self.by_id[pid].get("requires", []):
                add(r)
            out.append(pid)
        for pid in ids:
            add(pid)
        return out


class Card:
    def __init__(self, root: Path):
        self.root = root
        self.rec_path = root / RECORD
        try:
            self.rec = json.loads(self.rec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.rec = {"files": {}}

    def save(self) -> None:
        self.rec_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.rec_path.with_name("packs.json.partial")
        tmp.write_text(json.dumps(self.rec, indent=0), encoding="utf-8")
        os.replace(tmp, self.rec_path)

    def has(self, f: dict) -> bool:
        """This exact file is on the card: checked before (the record) and the same size now."""
        p = self.root / f["path"].lstrip("/")
        return self.rec["files"].get(f["path"]) == f["sha256"] and p.exists() and p.stat().st_size == f["size"]

    def manifest(self, pid: str) -> dict | None:
        try:
            return json.loads((self.root / "Maps" / pid / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def installed(self) -> dict[str, dict]:
        out = {}
        maps = self.root / "Maps"
        if maps.is_dir():
            for d in sorted(maps.iterdir()):
                m = self.manifest(d.name) if d.is_dir() else None
                if m and m.get("type") == "pack":
                    out[d.name] = m
        return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, size: int, sha: str, on_bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == size and _sha256(dest) == sha:
        on_bytes(size)
        return
    part = dest.with_name(dest.name + ".partial")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    on_bytes(have)
    if have < size:
        with _open(url, have) as r:
            if have and getattr(r, "status", 200) != 206:          # the server ignored the resume: start over
                on_bytes(-have)
                have = 0
            with open(part, "ab" if have else "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    on_bytes(len(chunk))
    if part.stat().st_size != size or _sha256(part) != sha:
        part.unlink()
        raise IOError(f"{dest.name} didn't download properly. Run it again to retry")
    os.replace(part, dest)


def plan(cat: Catalog, card: Card, ids: list[str]) -> tuple[list[str], list[tuple[str, dict]], int]:
    """(packs in order, [(pack, file)] still to fetch, bytes already on the card that
    these packs share). Blocks come before each pack's own files, its manifest last."""
    order = cat.closure(ids)
    todo, seen, shared = [], set(), 0
    for pid in order:
        full = cat.full(pid)
        for f in full["blocks"] + full["files"]:
            if f["path"] in seen:
                continue
            seen.add(f["path"])
            if card.has(f):
                shared += f["size"]
            else:
                todo.append((pid, f))
    return order, todo, shared


def status(cat: Catalog, card: Card, pid: str, installed: dict) -> str | None:
    m = installed.get(pid)
    if not m:
        return None
    return "current" if m.get("packVersion") == cat.by_id[pid].get("version") else "older"


def get(cat: Catalog, card: Card, ids: list[str], assume_yes: bool) -> int:
    order, todo, shared = plan(cat, card, ids)
    extra = [cat.by_id[p]["title"] for p in order if p not in ids and status(cat, card, p, card.installed()) != "current"]
    need = sum(f["size"] for _, f in todo)
    if not todo:
        print("Those packs are already on the card.")
        return 0
    print()
    if extra:
        print(f"These need {', '.join(extra)} too, so {'they come' if len(extra) > 1 else 'it comes'} along.")
    if shared:
        print(nc.c("2", f"{nc.human(shared)} of what these use is already on the card."))
    space = nc.CardSpace(card.root)
    if not space.show(adding=need, what="these packs"):
        print(nc.c("1;31", "Not enough room on the card for that. Pick fewer packs, or take some off first."))
        return 1
    if any(f["size"] > FAT32_MAX for _, f in todo) and add_media.is_fat32(add_media.filesystem_of(card.root)):
        print(nc.c("1;31", "This card is FAT32, which can't hold files of 4 GB or more. Format it as exFAT first (Format a card in the menu)."))
        return 1
    if not assume_yes and input("Download? [Y/n] ").strip().lower() in ("n", "no"):
        print("Cancelled.")
        return 1

    progress = nc.Progress()
    done = [0]
    pack_bytes: dict[str, int] = {}
    def on_bytes(n):
        done[0] += n
        progress.pct(done[0] * 100.0 / need, f"{nc.human(done[0])} of {nc.human(need)}")
    failed = set()
    try:
        for pid, f in todo:
            if pid in failed:
                continue
            try:
                before = done[0]
                space.need(f["size"], f"{cat.by_id[pid]['title']}: {f['path'].rsplit('/', 1)[-1]}")
                _download(cat.url(f["path"]), card.root / f["path"].lstrip("/"), f["size"], f["sha256"], on_bytes)
                done[0] = before + f["size"]
                pack_bytes[pid] = pack_bytes.get(pid, 0) + f["size"]
                card.rec["files"][f["path"]] = f["sha256"]
                card.save()
                if f["path"].endswith("/manifest.json"):                  # the pack's last file
                    progress.done()
                    space.added(cat.by_id[pid]["title"], pack_bytes.pop(pid, 0))
            except nc.CardFull as e:
                progress.done()
                card.save()
                print(nc.c("1;31", f"  Stopped: {e}. Nothing more was written."))
                return 1
            except KeyboardInterrupt:
                raise
            except Exception as e:                              # noqa: BLE001
                progress.done()
                print(nc.c("31", f"  {cat.by_id[pid]['title']}: {e}"))
                failed.add(pid)
    except KeyboardInterrupt:
        progress.done()
        card.save()
        print(nc.c("33", "  Stopped. Run it again to pick up where it left off."))
        return 130
    progress.done()
    ok = [p for p in order if p not in failed and (p in ids or p in {q for q, _ in todo})]
    print(nc.c("1", f"\nOn the card: {', '.join(cat.by_id[p]['title'] for p in ok)}."))
    space.summary()
    if failed:
        print(nc.c("31", f"Not finished: {', '.join(cat.by_id[p]['title'] for p in failed)}. Run it again to retry."))
    nc.hidden_page_note(card.root, "maps", "Maps")
    return 0 if not failed else 1


def remove(cat: Catalog | None, card: Card, ids: list[str], assume_yes: bool) -> int:
    """Take packs off, and the blocks nothing left on the card uses. A pack other packs
    on the card build on stays unless those go too."""
    have = card.installed()
    ids = [i for i in ids if i in have]
    if not ids:
        print("None of those are on the card.")
        return 0
    staying = {p: m for p, m in have.items() if p not in ids}
    held = [i for i in ids if any(i in m.get("requires", []) for m in staying.values())]
    if held:
        users = sorted({staying[p].get("name", p) for p in staying if set(held) & set(staying[p].get("requires", []))})
        print(nc.c("33", f"Keeping {', '.join(have[i].get('name', i) for i in held)}: "
                         f"{', '.join(users)} {'need' if len(users) > 1 else 'needs'} {'them' if len(held) > 1 else 'it'}."))
        ids = [i for i in ids if i not in held]
        staying.update({i: have[i] for i in held})
        if not ids:
            return 1
    used = set()
    for m in staying.values():
        bl = m.get("blocks")
        if bl:
            used |= {f"{bl['dir']}/{bl['zoom']}/{x}/{y}.pmtiles" for x, y in bl["tiles"]}
    drop_blocks = []
    for pid in ids:
        bl = have[pid].get("blocks")
        if bl:
            drop_blocks += [p for p in (f"{bl['dir']}/{bl['zoom']}/{x}/{y}.pmtiles" for x, y in bl["tiles"]) if p not in used]
    drop_blocks = sorted(set(drop_blocks))
    folders = [card.root / "Maps" / pid for pid in ids]
    size = sum(f.stat().st_size for d in folders for f in d.rglob("*") if f.is_file())
    size += sum((card.root / p.lstrip("/")).stat().st_size for p in drop_blocks if (card.root / p.lstrip("/")).exists())
    print(f"Taking off {', '.join(have[i].get('name', i) for i in ids)}: frees {nc.human(size)}.")
    if not assume_yes and input("Remove? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1
    for d in folders:
        prefix = "/" + d.relative_to(card.root).as_posix() + "/"
        shutil.rmtree(d, ignore_errors=True)
        for k in [k for k in card.rec["files"] if k.startswith(prefix)]:
            del card.rec["files"][k]
    for p in drop_blocks:
        f = card.root / p.lstrip("/")
        if f.exists():
            f.unlink()
        card.rec["files"].pop(p, None)
        for parent in (f.parent, f.parent.parent):          # .../blocks/9/<x>/ when it empties
            try:
                parent.rmdir()
            except OSError:
                break
    card.save()
    print(f"Done: {nc.human(size)} free again.")
    return 0


def _tag(cat: Catalog, card: Card, pid: str, have: dict) -> str:
    st = status(cat, card, pid, have)
    return nc.c("32", "  on the card") if st == "current" else nc.c("33", "  update") if st == "older" else ""


def _numbers(raw: str, n: int) -> list[int] | None:
    nums = [x for x in raw.replace(" ", ",").split(",") if x]
    if not nums or not all(x.isdigit() and 1 <= int(x) <= n for x in nums):
        return None
    return [int(x) - 1 for x in nums]


def resolve(cat: Catalog, names: list[str]) -> list[str] | None:
    """Pack or collection ids or titles -> pack ids."""
    out = []
    by_title = {p["title"].lower(): p["id"] for p in cat.packs}
    cols = {c["id"]: c for c in cat.collections}
    cols.update({c["title"].lower(): c for c in cat.collections})
    for w in names:
        key = w.strip().lower()
        if key in cols:
            out += cols[key]["packs"]
        elif w in cat.by_id or key in by_title:
            out.append(cat.by_id[w]["id"] if w in cat.by_id else by_title[key])
        else:
            print(nc.c("31", f"No pack or collection called {w}."))
            return None
    return list(dict.fromkeys(out))


def browse(cat: Catalog, card: Card, have: dict) -> list[str] | None:
    """Continent, then country, then areas."""
    areas = [p for p in cat.packs if p["group"] != "Start here"]
    conts = list(dict.fromkeys(p["group"] for p in areas))
    for i, c in enumerate(conts, 1):
        print(f"  {i:>2}. {c:<26} {sum(1 for p in areas if p['group'] == c):>4} areas")
    pick = _numbers(input("Which part of the world: ").strip(), len(conts))
    if not pick:
        return None
    mine = [p for p in areas if p["group"] == conts[pick[0]]]
    countries = list(dict.fromkeys(p["country"] or p["title"] for p in mine))
    if len(countries) > 1:
        print()
        for i, c in enumerate(countries, 1):
            n = [p for p in mine if (p["country"] or p["title"]) == c]
            print(f"  {i:>2}. {c:<26} {(str(len(n)) + ' areas') if len(n) > 1 else nc.human(n[0]['size']):>10}"
                  f"{_tag(cat, card, n[0]['id'], have) if len(n) == 1 else ''}")
        pick = _numbers(input("Which countries (like 2 or 2,5): ").strip(), len(countries))
        if not pick:
            return None
        chosen = [countries[i] for i in pick]
        mine = [p for p in mine if (p["country"] or p["title"]) in chosen]
        if all(sum(1 for p in mine if (p["country"] or p["title"]) == c) == 1 for c in chosen):
            return [p["id"] for p in mine]
    print()
    for i, p in enumerate(mine, 1):
        print(f"  {i:>2}. {p['title']:<26} {nc.human(p['size']):>9}{_tag(cat, card, p['id'], have)}")
    raw = input("Which ones (like 3,7), or 'all': ").strip().lower()
    if raw == "all":
        return [p["id"] for p in mine]
    pick = _numbers(raw, len(mine))
    return [mine[i]["id"] for i in pick] if pick else None


def run(dest: str | None = None, index: str = INDEX, want: list[str] | None = None, list_only: bool = False,
        drop: list[str] | None = None, assume_yes: bool = False) -> int:
    nc.banner("Download map packs")
    print("Maps for Nomad's Maps page, by area, with street maps, turn by turn directions and")
    print("place search. Packs join up into one map, and nothing is stored twice.")
    try:
        cat = Catalog(index)
    except Exception as e:                                        # noqa: BLE001
        if getattr(e, "code", None) == 404:
            # the server answered, the list just isn't up: not the person's connection
            print(nc.c("1;31", "The map pack list isn't online right now."))
            print("  Try again later, or use Build map packs yourself, which makes them from OpenStreetMap.")
            return 1
        print(nc.c("1;31", f"Couldn't read the map pack list ({e})."))
        if index.startswith(("http://", "https://")):
            print("  This needs the internet. Check your connection and try again.")
        return 1
    dest = nc.choose_card(dest, prompt="Which card should the maps go on?")
    if not dest:
        print("Cancelled.")
        return 1
    card = Card(Path(dest))
    if drop:
        names = resolve(cat, drop)
        return remove(cat, card, names, assume_yes) if names else 1
    have = card.installed()
    if want is not None:
        names = resolve(cat, want)
        return get(cat, card, names, assume_yes) if names else 1

    on = [p for p in cat.packs if p["id"] in have and p["group"] != "Start here"]
    if on:
        print(f"\nOn this card: {', '.join(p['title'] for p in on)}.")
    cols = cat.collections
    print(nc.c("1", "\nReady made collections:"))
    group = None
    for i, c in enumerate(cols, 1):
        if c["group"] != group:
            group = c["group"]
            print(nc.c("2", f"  {group}"))
        done = all(status(cat, card, p, have) == "current" for p in c["packs"])
        print(f"  {i:>2}. {c['title']:<28} {c['areas']:>4} area{'s' if c['areas'] > 1 else ' '} {nc.human(c['size']):>9}"
              + (nc.c("32", "  on the card") if done else ""))
    print(nc.c("1", "\nOr:"))
    print("   a. Pick areas yourself, by continent and country")
    print("   r. Take packs off this card")
    if list_only:
        print(nc.c("1", "\nAll packs:"))
        for p in cat.packs:
            where = p["group"] + (" / " + p["country"] if p["country"] else "")
            print(f"  {where[:34]:<34} {p['title']:<26} {nc.human(p['size']):>9}{_tag(cat, card, p['id'], have)}")
        return 0
    raw = input("\nPick collections (like 3 or 3,5), a, r, or Enter to cancel: ").strip().lower()
    if not raw:
        print("Cancelled.")
        return 1
    if raw == "r":
        mine = [p for p in cat.packs if p["id"] in have]
        if not mine:
            print("No packs on this card yet.")
            return 0
        for i, p in enumerate(mine, 1):
            print(f"  {i:>2}. {p['title']:<26}")
        pick = _numbers(input("Which to take off (like 2,4): ").strip(), len(mine))
        if not pick:
            print("  type numbers from the list, like 2,4.")
            return 1
        return remove(cat, card, [mine[i]["id"] for i in pick], assume_yes)
    if raw == "a":
        ids = browse(cat, card, have)
        if not ids:
            print("Cancelled.")
            return 1
        return get(cat, card, ids, assume_yes)
    pick = _numbers(raw, len(cols))
    if not pick:
        print("  type collection numbers like 3,5, or a, or r.")
        return 1
    return get(cat, card, list(dict.fromkeys(p for i in pick for p in cols[i]["packs"])), assume_yes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Download map packs onto a Nomad card, or take them off.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    ap.add_argument("--index", default=INDEX, help="pack list URL (or a local catalog folder, for testing)")
    ap.add_argument("--list", action="store_true", help="show what's available without downloading anything")
    ap.add_argument("--get", help="packs or collections to download, separated by commas (ids or names). Doesn't ask first")
    ap.add_argument("--remove", help="packs or collections to take off, separated by commas. Doesn't ask first")
    a = ap.parse_args()
    return run(a.card, a.index, a.get.split(",") if a.get else None, a.list,
               a.remove.split(",") if a.remove else None, assume_yes=bool(a.get or a.remove))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
