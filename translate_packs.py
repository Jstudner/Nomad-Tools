#!/usr/bin/env python3
"""
Add offline translation to a nomad card.

Nomad's Translate page runs entirely in the browser of whoever connects to it,
so nothing is installed on the device itself. This tool's only job is to put
the right files in /Translate on the card: the small browser runtime (once) and
one folder per language model.

Two way is the default. Asking for Spanish gets you English into Spanish *and*
Spanish into English, because a phrasebook that only works one way is half a
phrasebook.

Sizes are per language pair, roughly 115 MB each way. That is what a phone
copies over WiFi the first time it uses that pair, not every time. Bundles are
much better value: one ~115 MB model covers all the Romance languages.

    ./translate_packs.py                      pick from a menu
    ./translate_packs.py --list               show packs and sizes, install nothing
    ./translate_packs.py --add spanish french
    ./translate_packs.py --add world -o /media/you/NOMAD -y
    ./translate_packs.py --remove spanish
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import nomad_common as nc

HF = "https://huggingface.co"
# The browser runtime, taken straight from the published npm package.
RUNTIME_BASE = "https://unpkg.com/@xenova/transformers@2.17.2/dist"
RUNTIME_FILES = [
    # the library itself
    "transformers.min.js",
    # SIMD build, no threads: nomad is served over plain http, which is not a
    # secure context, so SharedArrayBuffer (and the threaded build) is absent
    "ort-wasm-simd.wasm",
    # fallback for the rare browser without SIMD; only fetched if needed
    "ort-wasm.wasm",
]

# Only the files transformers.js actually loads. Anything else in the model
# repo is wasted card space and wasted minutes over the nomad's WiFi.
MODEL_FILES = ["config.json", "tokenizer.json", "tokenizer_config.json",
               "generation_config.json"]
ONNX_FILES = ["onnx/encoder_model_quantized.onnx",
              "onnx/decoder_model_merged_quantized.onnx"]

LANG_NAMES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ro": "Romanian", "ca": "Catalan",
    "gl": "Galician", "oc": "Occitan", "la": "Latin",
    "ru": "Russian", "uk": "Ukrainian", "pl": "Polish", "cs": "Czech",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese",
    "id": "Indonesian", "hi": "Hindi", "ar": "Arabic", "he": "Hebrew",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "el": "Greek", "tr": "Turkish", "th": "Thai",
    "hu": "Hungarian", "bg": "Bulgarian", "mul": "Any other language",
}

# opus-mt-en-mul is multi target but wants 3 letter ISO 639-3 tokens
# (>>spa<<, >>deu<<), unlike the Romance bundle which uses 2 letter ones.
# Without the right token it answers in an arbitrary language, so this mapping
# rides along in the manifest and the page applies it at translation time.
EN_MUL_TARGETS = {
    "es": "spa", "fr": "fra", "de": "deu", "it": "ita", "pt": "por",
    "ru": "rus", "zh": "zho", "ja": "jpn", "ar": "ara", "hi": "hin",
    "nl": "nld", "sv": "swe", "pl": "pol", "tr": "tur", "vi": "vie",
    "id": "ind", "el": "ell", "he": "heb", "th": "tha", "uk": "ukr",
    "cs": "ces", "da": "dan", "fi": "fin", "ro": "ron", "hu": "hun",
    "bg": "bul",
}

ROMANCE = ["es", "fr", "it", "pt", "ro", "ca", "gl", "oc", "la"]

# A pack is one model folder. Rows are (repo, from, to[, prefix map]); a `to`
# with more than one entry means the model is multi target.
CATALOG: dict[str, list[tuple]] = {
    "spanish":    [("Xenova/opus-mt-en-es", ["en"], ["es"]),
                   ("Xenova/opus-mt-es-en", ["es"], ["en"])],
    "french":     [("Xenova/opus-mt-en-fr", ["en"], ["fr"]),
                   ("Xenova/opus-mt-fr-en", ["fr"], ["en"])],
    "german":     [("Xenova/opus-mt-en-de", ["en"], ["de"]),
                   ("Xenova/opus-mt-de-en", ["de"], ["en"])],
    "italian":    [("Xenova/opus-mt-en-it", ["en"], ["it"]),
                   ("Xenova/opus-mt-it-en", ["it"], ["en"])],
    "russian":    [("Xenova/opus-mt-en-ru", ["en"], ["ru"]),
                   ("Xenova/opus-mt-ru-en", ["ru"], ["en"])],
    "chinese":    [("Xenova/opus-mt-en-zh", ["en"], ["zh"]),
                   ("Xenova/opus-mt-zh-en", ["zh"], ["en"])],
    "arabic":     [("Xenova/opus-mt-en-ar", ["en"], ["ar"]),
                   ("Xenova/opus-mt-ar-en", ["ar"], ["en"])],
    "hindi":      [("Xenova/opus-mt-en-hi", ["en"], ["hi"]),
                   ("Xenova/opus-mt-hi-en", ["hi"], ["en"])],
    "dutch":      [("Xenova/opus-mt-en-nl", ["en"], ["nl"]),
                   ("Xenova/opus-mt-nl-en", ["nl"], ["en"])],
    "swedish":    [("Xenova/opus-mt-en-sv", ["en"], ["sv"]),
                   ("Xenova/opus-mt-sv-en", ["sv"], ["en"])],
    "ukrainian":  [("Xenova/opus-mt-en-uk", ["en"], ["uk"]),
                   ("Xenova/opus-mt-uk-en", ["uk"], ["en"])],
    "vietnamese": [("Xenova/opus-mt-en-vi", ["en"], ["vi"]),
                   ("Xenova/opus-mt-vi-en", ["vi"], ["en"])],
    "indonesian": [("Xenova/opus-mt-en-id", ["en"], ["id"]),
                   ("Xenova/opus-mt-id-en", ["id"], ["en"])],
    "japanese":   [("Xenova/opus-mt-en-jap", ["en"], ["ja"]),
                   ("Xenova/opus-mt-ja-en", ["ja"], ["en"])],
    # into English only: no English to X build exists for these
    "korean":     [("Xenova/opus-mt-ko-en", ["ko"], ["en"])],
    "polish":     [("Xenova/opus-mt-pl-en", ["pl"], ["en"])],
    "turkish":    [("Xenova/opus-mt-tr-en", ["tr"], ["en"])],
    # bundles: one model, many languages
    "romance":    [("Xenova/opus-mt-en-ROMANCE", ["en"], ROMANCE),
                   ("Xenova/opus-mt-ROMANCE-en", ROMANCE, ["en"])],
    "everything": [("Xenova/opus-mt-mul-en", ["mul"], ["en"]),
                   ("Xenova/opus-mt-en-mul", ["en"], list(EN_MUL_TARGETS),
                    EN_MUL_TARGETS)],
}

# Collections are just lists of the packs above.
COLLECTIONS = {
    "starter":  ["spanish", "french"],
    "asia":     ["chinese", "japanese", "korean", "hindi", "vietnamese",
                 "indonesian"],
    "traveler": ["spanish", "french", "german", "italian", "chinese",
                 "japanese", "arabic", "russian"],
    "europe":   ["spanish", "french", "german", "italian", "dutch", "swedish",
                 "polish", "ukrainian", "russian"],
    "world":    ["spanish", "french", "german", "italian", "dutch", "swedish",
                 "russian", "ukrainian", "polish", "chinese", "japanese",
                 "korean", "turkish", "arabic", "hindi", "vietnamese",
                 "indonesian", "romance", "everything"],
}

NOTES = {
    "romance": "One model each way covering Spanish, French, Italian, Portuguese "
               "and Romanian. Far smaller than separate packs, a little less "
               "accurate than a dedicated pair.",
    "everything": "Very wide coverage, noticeably lower quality.",
    "korean": "Korean into English only. There is no English into Korean build.",
    "polish": "Polish into English only. There is no English into Polish build.",
    "turkish": "Turkish into English only. There is no English into Turkish build.",
    "japanese": "Japanese into English is good. English into Japanese is weaker.",
}

# menu order
COLLECTION_ORDER = ["starter", "asia", "traveler", "europe", "world"]
PACK_ORDER = ["spanish", "french", "german", "italian",
              "dutch", "swedish", "russian", "ukrainian", "polish", "chinese",
              "japanese", "korean", "turkish", "arabic", "hindi", "vietnamese",
              "indonesian", "romance", "everything"]
PACK_ORDER = [p for p in PACK_ORDER if p in CATALOG]


# ── catalog helpers ───────────────────────────────────────────────────────────

def entries(name: str) -> list[tuple]:
    """CATALOG rows as (repo, from, to, prefix_map)."""
    out = []
    for row in CATALOG[name]:
        out.append((row[0], row[1], row[2], row[3] if len(row) > 3 else None))
    return out


def expand(names: list[str]) -> list[str]:
    """Turn collection names into the individual packs they stand for."""
    out: list[str] = []
    for n in names:
        n = n.lower()
        if n in COLLECTIONS:
            out.extend(COLLECTIONS[n])
        elif n in CATALOG:
            out.append(n)
        else:
            raise KeyError(n)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


_size_cache: dict[str, int | None] = {}


def repo_size(repo: str) -> int | None:
    """Bytes of just the files we ship. None if there's no usable build."""
    if repo in _size_cache:
        return _size_cache[repo]
    try:
        url = f"{HF}/api/models/{repo}/tree/main?recursive=true"
        with urllib.request.urlopen(url, timeout=45) as r:
            tree = json.load(r)
    except Exception:
        _size_cache[repo] = None
        return None
    if isinstance(tree, dict):
        _size_cache[repo] = None
        return None
    by = {f["path"]: (f.get("size") or 0) for f in tree if f.get("type") == "file"}
    if not all(p in by for p in ONNX_FILES):
        _size_cache[repo] = None
        return None
    total = sum(by[p] for p in ONNX_FILES)
    total += sum(by[p] for p in MODEL_FILES if p in by)
    _size_cache[repo] = total
    return total


def pack_total(name: str) -> tuple[int, set[str], bool]:
    """(bytes, languages covered, translates both ways).

    "Both ways" is about direction, not availability: Korean only has a Korean
    into English model, so it is one way even though that model downloads fine.
    """
    total, langs = 0, set()
    into_en, from_en = set(), set()
    for repo, src, dst, _ in entries(name):
        if repo_size(repo) is None:
            continue
        total += repo_size(repo)
        langs |= set(src) | set(dst)
        if "en" in dst:
            into_en |= set(src) - {"en"}
        if "en" in src:
            from_en |= set(dst) - {"en"}
    # Both ways means you can get into English and back out again. The two
    # sides need not cover identical language sets: the "everything" bundle
    # takes almost anything into English and sends English back out to 26
    # languages, which is two way even though the lists differ.
    two_way = bool(into_en) and bool(from_en)
    return total, langs, two_way


def covers(langs: set[str], width: int = 34) -> str:
    names = [LANG_NAMES.get(c, c) for c in sorted(langs) if c not in ("en", "mul")]
    s = ", ".join(names)
    return s if len(s) <= width else s[:width - 3] + "..."


# ── card layout ───────────────────────────────────────────────────────────────

def card_paths(card: str) -> tuple[Path, Path, Path]:
    root = Path(card) / "Translate"
    return root, root / "models", root / "manifest.json"


def load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"version": 2, "engine": "transformers.js@2.17.2", "names": {}, "packs": []}


def write_manifest(path: Path, man: dict) -> None:
    used: set[str] = set()
    for p in man["packs"]:
        used |= set(p["from"]) | set(p["to"])
    man["names"] = {c: LANG_NAMES.get(c, c) for c in sorted(used)}
    man["packs"].sort(key=lambda p: p["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(man, indent=2) + "\n")
    os.replace(tmp, path)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# ── downloading ───────────────────────────────────────────────────────────────

def fetch(url: str, dest: Path, log: nc.Progress,
          label: str, done_before: int, grand_total: int) -> int:
    """Download one file, reporting progress against the whole job."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if grand_total:
                    pct = (done_before + got) * 100.0 / grand_total
                    log.pct(pct, f"{label}  {nc.human(done_before + got)} of "
                                 f"{nc.human(grand_total)}")
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"could not fetch {url} ({e})") from e
    os.replace(tmp, dest)
    return got


def ensure_runtime(root: Path, log: nc.Progress) -> None:
    """Put the browser runtime on the card. Once per card, shared by all packs."""
    rt = root / "runtime"
    missing = [f for f in RUNTIME_FILES if not (rt / f).exists()]
    if not missing:
        return
    log.step("Browser runtime")
    log(nc.c("2", "  the small library that runs the models in the browser"))
    for f in missing:
        fetch(f"{RUNTIME_BASE}/{f}", rt / f, log, f, 0, 0)
        log(f"  {f}  {nc.human((rt / f).stat().st_size)}")
    log.done()


def fetch_model(repo: str, models: Path, log: nc.Progress,
                done_before: int, grand_total: int) -> tuple[int, int]:
    """Download one model folder. Returns (bytes on disk, bytes transferred)."""
    dest = models.joinpath(*repo.split("/"))
    moved = 0
    for rel in MODEL_FILES:
        try:
            moved += fetch(f"{HF}/{repo}/resolve/main/{rel}", dest / rel, log,
                           repo.split("/")[-1], done_before + moved, grand_total)
        except RuntimeError:
            # generation_config.json is absent on some repos
            if rel == "generation_config.json":
                continue
            raise
    for rel in ONNX_FILES:
        moved += fetch(f"{HF}/{repo}/resolve/main/{rel}", dest / rel, log,
                       repo.split("/")[-1], done_before + moved, grand_total)
    return dir_size(dest), moved


# ── the interactive menu ──────────────────────────────────────────────────────

def show_menu(installed: set[str]) -> None:
    mark = lambda name: nc.c("1;33", " ✓ on card") if all(
        repo.split("/")[-1] in installed for repo, _, _, _ in entries(name)) else ""

    print(nc.c("1", "\nCollections") + nc.c("2", "  (several languages at once)"))
    for i, name in enumerate(COLLECTION_ORDER, 1):
        total, langs = 0, set()
        for sub in COLLECTIONS[name]:
            t, l, _ = pack_total(sub)
            total += t
            langs |= l
        n = len([c for c in langs if c not in ("en", "mul")])
        print(f"  {i:>2}. {nc.c('1', name):<22} {nc.human(total):>9}   "
              f"{n} languages")
        print(nc.c("2", textwrap.fill(", ".join(p.title() for p in COLLECTIONS[name]),
                                      74, initial_indent=" " * 8,
                                      subsequent_indent=" " * 8)))

    print(nc.c("1", "\nSingle languages") + nc.c("2", "  (both directions unless noted)"))
    base = len(COLLECTION_ORDER)
    for j, name in enumerate(PACK_ORDER, base + 1):
        total, langs, ok = pack_total(name)
        if total == 0:
            continue
        note = "" if ok else nc.c("2", "  one direction only")
        print(f"  {j:>2}. {nc.c('1', name):<22} {nc.human(total):>9}   "
              f"{covers(langs)}{note}{mark(name)}")
        if name in NOTES and name in ("romance", "everything"):
            print(nc.c("2", f"        {NOTES[name]}"))


def menu_names() -> list[str]:
    return COLLECTION_ORDER + PACK_ORDER


def ask_selection() -> list[str] | None:
    names = menu_names()
    print(nc.c("1", "\nWhich would you like?"))
    print(nc.c("2", "  numbers separated by commas, e.g. 1,7,9"))
    print(nc.c("2", "  or a name, e.g. spanish   or 'all' for everything"))
    while True:
        raw = input("Choose (0 to cancel): ").strip().lower()
        if raw in ("0", "", "q", "cancel"):
            return None
        if raw == "all":
            return list(CATALOG)
        picked: list[str] = []
        bad = None
        for part in (p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()):
            if part.isdigit() and 1 <= int(part) <= len(names):
                picked.append(names[int(part) - 1])
            elif part in CATALOG or part in COLLECTIONS:
                picked.append(part)
            else:
                bad = part
                break
        if bad:
            print(f"  don't know '{bad}'. Use the numbers above, a name, or 'all'.")
            continue
        if not picked:
            print("  nothing selected.")
            continue
        return picked


# ── the tool ──────────────────────────────────────────────────────────────────

def run(dest: str | None = None, add: list[str] | None = None,
        remove: list[str] | None = None, list_only: bool = False,
        assume_yes: bool = False) -> int:
    """Interactive language pack installer. Keyword args skip the prompts."""
    nc.banner("Offline translation packs")

    if list_only:
        print(nc.c("2", "\nChecking sizes, this needs the internet and takes a moment."))
        show_menu(set())
        print(nc.c("2", "\nSizes are card space. Each phone only copies the one pair"))
        print(nc.c("2", "it uses, about 115 MB per direction, and keeps it after that."))
        return 0

    dest = nc.choose_card(dest, prompt="Which card are we adding languages to?")
    if not dest:
        print("cancelled.")
        return 1
    root, models, manifest_path = card_paths(dest)

    # The page itself ships with the card frontend, not with this tool.
    if not (Path(dest) / "translate.html").exists():
        print(nc.c("1;31", "\n  note: translate.html isn't on this card yet."))
        print(nc.c("1;31", "  Language packs will install fine, but the Translate page"))
        print(nc.c("1;31", "  won't appear until the card's frontend is updated."))

    man = load_manifest(manifest_path)
    installed = {p["id"] for p in man["packs"]}
    if installed:
        have = sum(p["bytes"] for p in man["packs"])
        print(f"\nAlready on this card: {nc.c('1', str(len(installed)))} models, "
              f"{nc.human(have)}")

    if remove:
        try:
            wanted = expand(remove)
        except KeyError as e:
            print(nc.c("1;31", f"unknown pack {e}."))
            return 1
        for name in wanted:
            for repo, _, _, _ in entries(name):
                pid = repo.split("/")[-1]
                d = models.joinpath(*repo.split("/"))
                if d.is_dir():
                    shutil.rmtree(d)
                    print(f"  removed {pid}")
                man["packs"] = [p for p in man["packs"] if p["id"] != pid]
        write_manifest(manifest_path, man)
        summarize(man)
        return 0

    if add:
        try:
            wanted = expand(add)
        except KeyError as e:
            print(nc.c("1;31", f"unknown pack {e}. Try --list."))
            return 1
    else:
        print(nc.c("2", "\nChecking sizes, this needs the internet and takes a moment."))
        show_menu(installed)
        picked = ask_selection()
        if not picked:
            print("cancelled.")
            return 1
        wanted = expand(picked)

    # ── plan: what's new, how big, does it fit ────────────────────────────────
    todo: list[tuple[str, list, list, dict | None]] = []
    skipped, unavailable = [], []
    for name in wanted:
        for repo, src, dst, pmap in entries(name):
            pid = repo.split("/")[-1]
            if pid in installed:
                skipped.append(pid)
                continue
            if repo_size(repo) is None:
                unavailable.append(repo)
                continue
            todo.append((repo, src, dst, pmap))

    if skipped:
        print(nc.c("2", f"\n  already on the card, skipping: {len(skipped)} model(s)"))
    if unavailable:
        print(nc.c("2", f"  no browser ready build, skipping: {', '.join(unavailable)}"))
    if not todo:
        print(nc.c("1", "\nNothing left to do, everything you picked is already there."))
        summarize(man)
        return 0

    grand_total = sum(repo_size(r) or 0 for r, _, _, _ in todo)
    langs: set[str] = set()
    for _, src, dst, _ in todo:
        langs |= set(src) | set(dst)

    print(nc.c("1", f"\nTo install: {len(todo)} model(s), {nc.human(grand_total)}"))
    print(f"  languages: {covers(langs, 60)}")
    print(f"  going to: {nc.c('1', str(root))}")
    try:
        free = shutil.disk_usage(dest).free
        print(f"  free space: {nc.human(free)}")
        if free < grand_total * 1.05:
            print(nc.c("1;31", "  warning: the card may not have room for this."))
            if free < grand_total:
                print(nc.c("1;31", "  it almost certainly does not. Free space or pick less."))
    except OSError:
        pass

    if not assume_yes:
        if input("\nDownload these now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled.")
            return 1

    # ── do it ────────────────────────────────────────────────────────────────
    log = nc.Progress()
    log.plan(["runtime", "models", "manifest"])
    try:
        ensure_runtime(root, log)
        log.step(f"Downloading {len(todo)} model(s), {nc.human(grand_total)}")
        moved = 0
        for repo, src, dst, pmap in todo:
            size, got = fetch_model(repo, models, log, moved, grand_total)
            moved += got
            pid = repo.split("/")[-1]
            man["packs"] = [p for p in man["packs"] if p["id"] != pid]
            man["packs"].append({
                "id": pid, "model": repo, "from": src, "to": dst,
                "bytes": size,
                "targetPrefix": len(dst) > 1,
                "prefixMap": pmap or None,
                "license": "CC-BY-4.0", "source": "Helsinki-NLP OPUS-MT",
            })
            # Written after every model, not once at the end: the world
            # collection is several gigabytes and losing the bookkeeping
            # because the last one failed would mean fetching it all again.
            write_manifest(manifest_path, man)
            log.done()
            log(f"  {nc.c('1', pid):<34} {nc.human(size)}")
    except KeyboardInterrupt:
        log.done()
        write_manifest(manifest_path, man)
        print(nc.c("1;31", "\n  stopped. What finished is saved, run again to carry on."))
        return 130
    except Exception as exc:
        log.done()
        write_manifest(manifest_path, man)
        print(nc.c("1;31", f"\n  failed: {exc}"))
        print(nc.c("2", "  what finished is saved. Run again to pick up the rest."))
        return 1
    log.done()

    summarize(man)
    print(nc.c("2", "\nEject the card safely, then open Translate on the nomad."))
    nc.hidden_page_note(dest, "translate", "Translate")
    return 0


def summarize(man: dict) -> None:
    """What a user can actually pick in the Translate page now."""
    if not man["packs"]:
        print(nc.c("1", "\nNo language packs on this card."))
        return
    total = sum(p["bytes"] for p in man["packs"])
    print(nc.c("1", f"\nOn the card: {len(man['packs'])} models, {nc.human(total)}"))

    pairs = set()
    for p in man["packs"]:
        for a in p["from"]:
            for b in p["to"]:
                if a != b:
                    pairs.add((a, b))
    two_way = sorted({tuple(sorted(x)) for x in pairs if (x[1], x[0]) in pairs})
    one_way = sorted(x for x in pairs if (x[1], x[0]) not in pairs)
    name = lambda c: LANG_NAMES.get(c, c)
    if two_way:
        print("  both directions: " + ", ".join(
            f"{name(a)} and {name(b)}" for a, b in two_way))
    if one_way:
        print("  one direction:   " + ", ".join(
            f"{name(a)} to {name(b)}" for a, b in one_way))


def rebuild(dest: str | None = None) -> int:
    """Rewrite the manifest from the models already on a card.

    Re-derives every entry from the current catalog without downloading
    anything, which repairs a card written by an older version of this tool.
    """
    nc.banner("Repair translation manifest")
    dest = nc.choose_card(dest, prompt="Which card?")
    if not dest:
        return 1
    root, models, manifest_path = card_paths(dest)
    if not models.is_dir():
        print(nc.c("1;31", f"no models folder on that card: {models}"))
        return 1

    known = {}
    for name in CATALOG:
        for repo, src, dst, pmap in entries(name):
            known[repo] = (src, dst, pmap)

    packs, unknown = [], []
    for owner in sorted(p for p in models.iterdir() if p.is_dir()):
        for mdl in sorted(p for p in owner.iterdir() if p.is_dir()):
            repo = f"{owner.name}/{mdl.name}"
            if repo not in known:
                unknown.append(repo)
                continue
            src, dst, pmap = known[repo]
            packs.append({
                "id": mdl.name, "model": repo, "from": src, "to": dst,
                "bytes": dir_size(mdl),
                "targetPrefix": len(dst) > 1,
                "prefixMap": pmap or None,
                "license": "CC-BY-4.0", "source": "Helsinki-NLP OPUS-MT",
            })

    man = load_manifest(manifest_path)
    man["version"] = 2
    man["packs"] = packs
    write_manifest(manifest_path, man)
    print(f"Rebuilt {manifest_path} from {len(packs)} models on the card.")
    if unknown:
        print(nc.c("2", f"  not in the catalog, left alone: {', '.join(unknown)}"))
    summarize(man)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add offline translation language packs to a nomad card.")
    ap.add_argument("--list", action="store_true",
                    help="show packs and sizes, install nothing")
    ap.add_argument("--add", nargs="+", metavar="PACK",
                    help="packs or collections to install, e.g. spanish world")
    ap.add_argument("--remove", nargs="+", metavar="PACK",
                    help="packs or collections to delete from the card")
    ap.add_argument("--rebuild", action="store_true",
                    help="rewrite the manifest from models already on the card")
    ap.add_argument("-o", "--output", help="card mountpoint (skips the picker)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="don't ask before downloading")
    args = ap.parse_args()

    if args.rebuild:
        return rebuild(dest=args.output)
    return run(dest=args.output, add=args.add, remove=args.remove,
               list_only=args.list, assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye.")
        sys.exit(130)
