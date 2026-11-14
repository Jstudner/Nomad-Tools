#!/usr/bin/env python3
"""
pictures: previews and smaller pictures, so Nomad's pages fill fast.

The tool looks the card over, says how much each job would do, and you choose.
The first three only add files. The last two overwrite pictures and ask first.

    Gallery previews      a small copy of every photo and a still from every clip,
                          kept in /Gallery/.previews, which the grid doesn't list.
                          The Gallery loads these instead of each whole photo, so a
                          page of 40 is about 1 MB instead of 20. Originals are
                          never touched. Run it again after adding photos: it only
                          makes what's missing and clears previews of deleted ones.
    Covers for books      comics, ebooks, PDFs and audiobooks with no cover get one
                          from their first page or the art inside them, saved
                          beside them with the same name (Book.cbz + Book.jpg).
    Stills for films      films and shows with no cover get a still from the film.
                          Only when chosen.
    Shrink covers         Movies, Shows, Books and Games covers wider than 200 px, in place.
                          Comics kept as a folder of pages are left alone.
    Shrink recipe photos  Cookbook photos wider than 800 px, in place.

Needs Pillow. Clips, film stills and audiobook art need ffmpeg. PDF covers need
PyMuPDF or poppler's pdftoppm. Without them those items are skipped and the
list says so.

    ./pictures.py                                   # pick a card, see the list, choose
    ./pictures.py /media/me/NOMAD --check           # just the list, change nothing
    ./pictures.py /media/me/NOMAD --jobs previews,covers --yes
"""
from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

import build_index
import media_types as mt
import nomad_common as nc

Image = ImageOps = None   # Pillow, loaded by _have_pillow() so a missing Pillow can't stop the menu

# Gallery previews: the shorter side, so a square card (the grid crops to fill) gets
# 360 px across. Cards are 95 to 318 px wide on a phone; most people use the medium
# 151 px one. About 25 KB each.
PREVIEW_DIR = ".previews"
PREVIEW_PX = 360
PREVIEW_Q = 72
PREVIEW_EST = 25 * 1024
# covers show at 109x163 on a phone; 200 wide is what Optimize cover images made
COVER_W = 200
COVER_Q = 75
# recipe photos show across the recipe's header, the width of the phone
RECIPE_W = 800
RECIPE_Q = 78

# where covers are looked for, and how deep (-1 every subfolder, 1 one level down).
# Games shows its covers at the same 109x163 as Movies.
# Shows is 1: shows.html takes the poster inside the show folder; season art a
# level further down is left alone.
COVER_FOLDERS = [("Movies", -1), ("Shows", 1), ("Books", 1), ("Games", -1)]
SHRINKABLE = {".jpg", ".jpeg", ".png", ".webp"}
SHOW_COVER_WORDS = ("poster", "cover", "folder", "art")
AUDIO_ART = {".mp3", ".m4a", ".m4b", ".flac"}
JOBS = ["previews", "covers", "stills", "shrink", "recipes"]
OVERWRITES = {"shrink", "recipes"}


def _have_pillow() -> bool:
    global Image, ImageOps, _LANCZOS
    try:
        from PIL import Image as _I, ImageOps as _O
    except ImportError:
        # nearly always "two Pythons": Pillow went into a different interpreter
        exe = sys.executable or "python"
        print(nc.c("1;31", "This needs Pillow, and the Python running these tools doesn't have it."))
        print(f"  Python in use: {exe}")
        print("  Install Pillow into that Python with:")
        print(nc.c("1", f'      "{exe}" -m pip install Pillow'))
        return False
    Image, ImageOps = _I, _O
    # Image.Resampling arrived in Pillow 9.1; Ubuntu 22.04 ships 9.0.1
    _LANCZOS = getattr(Image, "Resampling", Image).LANCZOS
    return True


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _visible(names):
    return sorted((n for n in names if not n.startswith(".") and not n.startswith("$")), key=_natural)


# pictures

def _upright(im, want: int | None = None):
    """The picture loaded and turned the way the camera held it. With `want`, a JPEG
    is decoded at the smallest scale still at least that big (far faster for photos)."""
    if want and im.format == "JPEG":
        im.draft("RGB", (want, want))
    return ImageOps.exif_transpose(im)   # a loaded copy; an animation gives its first frame


def _fit_short(img, short: int):
    w, h = img.size
    k = short / min(w, h)
    return img.resize((max(1, round(w * k)), max(1, round(h * k))), _LANCZOS) if k < 1 else img


def _fit_width(img, width: int):
    w, h = img.size
    return img.resize((width, max(1, round(h * width / w))), _LANCZOS) if w > width else img


def _rgb(img):
    """JPEG holds only RGB and grey: transparency goes onto white."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    return img if img.mode in ("RGB", "L") else img.convert("RGB")


def _write(dst: Path, save) -> None:
    """Write beside dst and swap it in only once the write worked: a failed save
    must never leave a broken file where a good one was."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".nomad-tmp")
    try:
        save(tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()


def _save_jpeg(img, dst: Path, quality: int) -> None:
    img = _rgb(img)
    _write(dst, lambda t: img.save(t, "JPEG", quality=quality, optimize=True))


def _cover_from(src, dst: Path) -> bool:
    """A cover (COVER_W wide) from a picture file, bytes or an open image."""
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    with (src if isinstance(src, Image.Image) else Image.open(src)) as im:
        img = _fit_width(_upright(im, COVER_W), COVER_W)
        _save_jpeg(img, dst, COVER_Q)
    return True


# video (ffmpeg)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _duration(src: Path) -> float | None:
    if not FFPROBE:
        return None
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def _frame(src: Path, dst: Path, vf: str, at: float | None, art: bool = False) -> bool:
    """One frame of src through the filter vf, as a JPEG at dst. art: the picture an
    audio file carries instead of a moment of video."""
    tmp = dst.with_name(dst.name + ".nomad-tmp.jpg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        for start in ([None] if art else [at, 0.0] if at else [0.0]):
            cmd = [FFMPEG, "-v", "error", "-y"]
            if start:
                cmd += ["-ss", f"{start:.2f}"]
            cmd += ["-i", str(src)]
            if art:
                cmd += ["-an", "-map", "0:v:0"]
            cmd += ["-frames:v", "1", "-vf", vf, "-q:v", "5", str(tmp)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=180)
            except subprocess.TimeoutExpired:
                continue
            if tmp.is_file() and tmp.stat().st_size > 0:   # a seek past a short clip's end makes none
                os.replace(tmp, dst)
                return True
        return False
    finally:
        if tmp.exists():
            tmp.unlink()


# square pixels first (DVDs aren't), then the shorter side to PREVIEW_PX, never up
_VF_PREVIEW = (f"scale=iw*sar:ih,setsar=1,scale='if(gt(iw,ih),-2,min({PREVIEW_PX},iw))'"
               f":'if(gt(iw,ih),min({PREVIEW_PX},ih),-2)'")
# the most typical of 60 frames, cut to a 2:3 cover from the middle
_VF_STILL = f"scale=iw*sar:ih,setsar=1,thumbnail=60,crop='min(iw,ih*2/3)':ih,scale={COVER_W}:-2"
_VF_ART = f"scale='min({COVER_W},iw)':-2"


# Gallery previews

def _gallery_media(root: Path):
    """(relative path, file) for every photo and clip the Gallery lists."""
    base = root / "Gallery"
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = _visible(dirnames)
        for n in _visible(filenames):
            ext = os.path.splitext(n)[1].lower()
            if ext in mt.IMAGE_GALLERY or ext in mt.VIDEO_GALLERY:
                p = Path(dirpath) / n
                yield p.relative_to(base).as_posix(), p


def _preview_path(root: Path, rel: str) -> Path:
    # the whole name plus .jpg, so a.png and a.jpg don't share one
    return root / "Gallery" / PREVIEW_DIR / (rel + ".jpg")


def _manifest_path(root: Path) -> Path:
    return root / "Gallery" / PREVIEW_DIR / "index.json"


def _load_manifest(root: Path) -> dict:
    """{relative path: [size of the photo the preview was made from]}. The Gallery
    uses a preview only while the photo's size still matches."""
    try:
        d = json.loads(_manifest_path(root).read_text("utf-8"))
        if d.get("v") == 1 and d.get("px") == PREVIEW_PX and isinstance(d.get("items"), dict):
            return d["items"]
    except (OSError, ValueError):
        pass
    return {}


def _save_manifest(root: Path, items: dict) -> None:
    body = json.dumps({"v": 1, "px": PREVIEW_PX, "items": dict(sorted(items.items()))},
                      separators=(",", ":"), ensure_ascii=False)
    _write(_manifest_path(root), lambda t: t.write_text(body, "utf-8"))


def _plan_previews(root: Path) -> dict:
    have = _load_manifest(root)
    todo, keep, clips_skipped = [], {}, 0
    for rel, p in _gallery_media(root):
        sig = [p.stat().st_size]
        if have.get(rel) == sig and _preview_path(root, rel).is_file():
            keep[rel] = sig
        elif p.suffix.lower() in mt.VIDEO_GALLERY and not FFMPEG:
            clips_skipped += 1
        else:
            todo.append((rel, p, sig))
    seen = set(keep) | {t[0] for t in todo}
    gone = [rel for rel in have if rel not in seen]
    return {"todo": todo, "keep": keep, "gone": gone, "clips_skipped": clips_skipped}


def _make_preview(root: Path, rel: str, src: Path) -> bool:
    dst = _preview_path(root, rel)
    if src.suffix.lower() in mt.VIDEO_GALLERY:
        d = _duration(src)
        return _frame(src, dst, _VF_PREVIEW, min(3.0, d / 3) if d else 1.0)
    with Image.open(src) as im:
        img = _fit_short(_upright(im, PREVIEW_PX), PREVIEW_PX)
        _save_jpeg(img, dst, PREVIEW_Q)
    return True


def _do_previews(root: Path, plan: dict) -> int:
    items = dict(plan["keep"])
    for rel in plan["gone"]:
        try:
            _preview_path(root, rel).unlink()
        except OSError:
            pass
    done = [0]

    def made(item, ok):
        if ok:
            items[item[0]] = item[2]
            done[0] += 1
            if done[0] % 100 == 0:   # a run stopped part way keeps what it made
                _save_manifest(root, items)

    failed = _pool("Gallery previews", plan["todo"], lambda it: _make_preview(root, it[0], it[1]),
                   lambda it: it[0], made)
    _save_manifest(root, items)
    if plan["gone"]:
        print(f"  Cleared {len(plan['gone'])} preview(s) of photos that aren't on the card any more")
    return failed


def make_gallery_previews(root: Path) -> int:
    """Previews for whatever the Gallery has that lacks one. For add_media."""
    if not _have_pillow():
        return 1
    plan = _plan_previews(root)
    if not plan["todo"] and not plan["gone"]:
        return 0
    print(nc.c("1", f"\nMaking Gallery previews ({len(plan['todo'])} new)"))
    return _do_previews(root, plan)


# covers for books

def _image_stems(d: Path, exts=mt.IMAGE_BOOK) -> set[str]:
    try:
        return {f.stem.lower() for f in d.iterdir() if f.is_file() and f.suffix.lower() in exts}
    except OSError:
        return set()


def _plan_book_covers(root: Path) -> list[tuple[str, Path, Path]]:
    """(kind, source, cover to write). Books looks for Book.jpg beside Book.epub, and
    for a folder at the top, Folder.jpg beside the folder."""
    base = root / "Books"
    jobs: dict[Path, tuple[str, Path, Path]] = {}
    if not base.is_dir():
        return []

    def scan(d: Path, depth: int):
        stems = _image_stems(d)
        try:
            entries = sorted(d.iterdir(), key=lambda p: _natural(p.name))
        except OSError:
            return
        for f in entries:
            if f.name.startswith(".") or not f.is_file():
                continue
            ext, dst = f.suffix.lower(), d / (f.stem + ".jpg")
            if f.stem.lower() in stems or dst in jobs:
                continue
            kind = {".cbz": "comic", ".epub": "ebook", ".pdf": "pdf"}.get(ext)
            # chapters in a folder share the folder's cover (below), not one each
            if not kind and ext in AUDIO_ART and (depth == 0 or ext == ".m4b"):
                kind = "audiobook"
            if kind:
                jobs[dst] = (kind, f, dst)
        if depth:
            return
        for sub in entries:
            if sub.name.startswith(".") or not sub.is_dir():
                continue
            dst = d / (sub.name + ".jpg")
            if sub.name.lower() not in stems and dst not in jobs:
                if build_index.is_comic_folder(sub):
                    jobs[dst] = ("comic folder", sub, dst)
                else:
                    files = [f for f in sorted(sub.iterdir(), key=lambda p: _natural(p.name)) if f.is_file()]
                    exts = {f.suffix.lower() for f in files}
                    if exts & AUDIO_ART and not exts & mt.BOOK:
                        jobs[dst] = ("audiobook", next(f for f in files if f.suffix.lower() in AUDIO_ART), dst)
            scan(sub, 1)

    scan(base, 0)
    none = _load_none(root)

    def tried(src: Path) -> bool:
        try:
            return none.get(src.relative_to(base).as_posix()) == src.stat().st_size
        except OSError:
            return False
    return [j for j in jobs.values() if not tried(j[1])]


_ZIP_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


# books already looked in that hold no picture (an MP3 without art), so a later run
# doesn't offer them again: {path under /Books: size it had}
def _none_path(root: Path) -> Path:
    return root / "Books" / ".nomad-no-cover.json"


def _load_none(root: Path) -> dict:
    try:
        d = json.loads(_none_path(root).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_none(root: Path, none: dict) -> None:
    body = json.dumps(dict(sorted(none.items())), ensure_ascii=False)
    _write(_none_path(root), lambda t: t.write_text(body, "utf-8"))


def _zip_first_image(z: zipfile.ZipFile) -> bytes | None:
    names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n
             and not posixpath.basename(n).startswith(".")
             and posixpath.splitext(n)[1].lower() in _ZIP_IMAGES]
    if not names:
        return None
    names.sort(key=_natural)
    named = [n for n in names if "cover" in posixpath.basename(n).lower()]
    return z.read((named or names)[0])


def _epub_cover(path: Path) -> bytes | None:
    """The cover the book names (EPUB 3 cover-image, or EPUB 2's <meta name="cover">),
    else an image called cover, else the first image."""
    with zipfile.ZipFile(path) as z:
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            pkg = ET.fromstring(z.read(opf))
        except (KeyError, StopIteration, ET.ParseError):
            return _zip_first_image(z)
        items = {e.get("id"): e for e in pkg.iter() if e.tag.endswith("}item") or e.tag == "item"}
        meta_id = next((e.get("content") for e in pkg.iter()
                        if e.tag.endswith("meta") and e.get("name") == "cover"), None)
        # (an Element with no children is falsy, so every test here is "is None")
        pick = next((e for e in items.values() if "cover-image" in (e.get("properties") or "").split()), None)
        if pick is None:
            pick = items.get(meta_id)
        if pick is None:
            pick = next((e for i, e in items.items() if (e.get("media-type") or "").startswith("image/")
                         and "cover" in ((i or "") + (e.get("href") or "")).lower()), None)
        if pick is not None and (pick.get("media-type") or "image/").startswith("image/"):
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf), unquote(pick.get("href") or "")))
            try:
                return z.read(name)
            except KeyError:
                pass
        return _zip_first_image(z)


def _pdf_tool() -> str | None:
    for mod in ("pymupdf", "fitz"):
        try:
            __import__(mod)
            return mod
        except ImportError:
            pass
    return "pdftoppm" if shutil.which("pdftoppm") else None


def _pdf_cover(path: Path, dst: Path) -> bool:
    tool = _pdf_tool()
    if tool in ("pymupdf", "fitz"):
        fitz = __import__(tool)
        with fitz.open(str(path)) as doc:
            page = doc[0]
            zoom = 2 * COVER_W / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return _cover_from(img, dst)
    with tempfile.TemporaryDirectory() as t:
        subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-jpeg",
                        "-scale-to", str(2 * COVER_W), str(path), os.path.join(t, "c")],
                       capture_output=True, timeout=120)
        out = Path(t) / "c.jpg"
        return out.is_file() and _cover_from(out, dst)


def _make_book_cover(job) -> bool:
    kind, src, dst = job
    if kind == "comic":
        with zipfile.ZipFile(src) as z:
            data = _zip_first_image(z)
        return bool(data) and _cover_from(data, dst)
    if kind == "ebook":
        data = _epub_cover(src)
        return bool(data) and _cover_from(data, dst)
    if kind == "pdf":
        return _pdf_cover(src, dst)
    if kind == "comic folder":
        pages = [f for f in sorted(src.iterdir(), key=lambda p: _natural(p.name))
                 if f.is_file() and f.suffix.lower() in _ZIP_IMAGES and not f.name.startswith(".")]
        return bool(pages) and _cover_from(pages[0], dst)
    if kind == "audiobook":
        return _frame(src, dst, _VF_ART, None, art=True)
    return False


# stills for films

def _plan_stills(root: Path) -> list[tuple[str, Path, Path]]:
    """(kind, video, cover to write) for films and shows with no cover. Movies pairs
    Dune.mp4 with Dune.jpg in the same folder; Shows takes poster.jpg in the show."""
    jobs = []
    movies = root / "Movies"
    if movies.is_dir():
        for dirpath, dirnames, filenames in os.walk(movies):
            dirnames[:] = _visible(dirnames)
            names = _visible(filenames)
            stems = {os.path.splitext(n)[0].lower() for n in names
                     if os.path.splitext(n)[1].lower() in mt.IMAGE_MOVIE}
            for n in names:
                stem, ext = os.path.splitext(n)
                if ext.lower() in mt.VIDEO_MOVIE and stem.lower() not in stems:
                    jobs.append(("film", Path(dirpath) / n, Path(dirpath) / (stem + ".jpg")))
    shows = root / "Shows"
    if shows.is_dir():
        top = _image_stems(shows, mt.IMAGE_MOVIE)
        for show in sorted((d for d in shows.iterdir() if d.is_dir() and not d.name.startswith(".")),
                           key=lambda p: _natural(p.name)):
            own = _image_stems(show, mt.IMAGE_MOVIE)
            lower = show.name.lower()
            if any(w in s for s in own for w in SHOW_COVER_WORDS) or \
               {lower, lower.replace(" ", "-"), lower.replace(" ", "_")} & top:
                continue
            first = None
            for dirpath, dirnames, filenames in os.walk(show):
                dirnames[:] = _visible(dirnames)
                first = next((Path(dirpath) / n for n in _visible(filenames)
                              if os.path.splitext(n)[1].lower() in mt.VIDEO_MOVIE), None)
                if first:
                    break
            if first:
                jobs.append(("show", first, show / "poster.jpg"))
    return jobs


def _make_still(job) -> bool:
    _, src, dst = job
    d = _duration(src)
    # past the opening titles; a short clip from its first third
    return _frame(src, dst, _VF_STILL, d * 0.15 if d else 60.0)


# shrinking in place

def _walk_images(folder: Path, max_depth: int, exts=SHRINKABLE, depth: int = 0):
    """Pictures under folder, to max_depth. A comic kept as a folder of pages is
    skipped: its pages are the book, not covers."""
    try:
        items = sorted(folder.iterdir(), key=lambda p: _natural(p.name))
    except OSError:
        return
    for item in items:
        if item.name.startswith("."):
            continue
        if item.is_file() and item.suffix.lower() in exts:
            yield item
        elif item.is_dir() and (max_depth == -1 or depth < max_depth) \
                and not build_index.is_comic_folder(item):
            yield from _walk_images(item, max_depth, exts, depth + 1)


def _wider_than(paths, width: int) -> list[Path]:
    out = []
    for p in paths:
        try:
            with Image.open(p) as im:   # reads the header only
                if im.width > width:
                    out.append(p)
        except Exception:
            pass
    return out


def _plan_shrink(root: Path, dirs: list[str] | None) -> list[Path]:
    wanted = {d.strip().lower() for d in dirs} if dirs else None
    paths = []
    for name, depth in COVER_FOLDERS:
        if (root / name).is_dir() and (wanted is None or name.lower() in wanted):
            paths += _walk_images(root / name, depth)
    return _wider_than(paths, COVER_W)


def _plan_recipes(root: Path) -> list[Path]:
    folder = root / "Cookbook"
    return _wider_than(_walk_images(folder, 1), RECIPE_W) if folder.is_dir() else []


def _shrink(path: Path, width: int, quality: int) -> bool:
    """Narrower, in its own format, kept only if it came out smaller."""
    with Image.open(path) as im:
        fmt = im.format
        if im.width <= width or fmt not in ("JPEG", "PNG", "WEBP"):
            return False
        img = _fit_width(_upright(im), width)
    before = path.stat().st_size
    tmp = path.with_name(path.name + ".nomad-tmp")
    try:
        if fmt == "JPEG":
            _rgb(img).save(tmp, "JPEG", quality=quality, optimize=True)
        elif fmt == "WEBP":
            img.save(tmp, "WEBP", quality=quality)
        else:
            img.save(tmp, "PNG", optimize=True)
        if tmp.stat().st_size >= before:
            return False
        os.replace(tmp, path)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


# running

def _pool(title: str, items: list, fn, label, after=None, workers: int | None = None) -> int:
    """fn over items a few at a time with a progress bar. Returns how many failed."""
    if not items:
        return 0
    workers = workers or min(8, os.cpu_count() or 2)
    progress = nc.Progress()
    made = skipped = 0
    errors: list[str] = []
    print(nc.c("1;36", f"\n▶ {title} ({len(items)})"))
    ex = ThreadPoolExecutor(workers)
    try:
        futs = {ex.submit(fn, it): it for it in items}
        for i, f in enumerate(as_completed(futs), 1):
            it = futs[f]
            try:
                ok = bool(f.result())
            except Exception as exc:     # one unreadable file shouldn't stop the rest
                ok = None
                errors.append(f"{label(it)}: {exc}")
            made += ok is True
            skipped += ok is False
            if after:   # ok: True made, False nothing usable, None an error
                after(it, ok)
            progress.pct(100.0 * i / len(items), label(it)[-50:])
    except KeyboardInterrupt:
        ex.shutdown(wait=True, cancel_futures=True)
        progress.done()
        raise
    ex.shutdown(wait=True)
    progress.done()
    line = f"  made {made}"
    if skipped:
        line += f" · nothing to use in {skipped}"
    if errors:
        line += nc.c("31", f" · {len(errors)} failed")
    print(line)
    for e in errors[:5]:
        print(nc.c("31", f"    {e}"))
    if len(errors) > 5:
        print(nc.c("2", f"    ...and {len(errors) - 5} more"))
    return len(errors)


def _plan(root: Path, shrink_dirs: list[str] | None) -> dict:
    covers = _plan_book_covers(root)
    # what this computer can't make is left out, and the list says why
    cant = {"pdf": not _pdf_tool(), "audiobook": not FFMPEG}
    return {"previews": _plan_previews(root),
            "covers": [j for j in covers if not cant.get(j[0])],
            "covers_cant": {k: _count(covers, k) for k, no in cant.items() if no and _count(covers, k)},
            "stills": _plan_stills(root) if FFMPEG else [],
            "stills_cant": not FFMPEG and bool(_plan_stills(root)),
            "shrink": _plan_shrink(root, shrink_dirs), "recipes": _plan_recipes(root)}


def _count(jobs, kind):
    return sum(1 for j in jobs if j[0] == kind)


def _rows(plan: dict) -> list[tuple[str, str, int, str]]:
    """(job, what it would do, how many, note) for the list."""
    p = plan["previews"]
    n = len(p["todo"])
    what = f"{n} to make" + (f", {len(p['keep'])} already made" if p["keep"] else "")
    if p["gone"]:
        what += f", {len(p['gone'])} of deleted photos to clear"
    note = f"adds about {nc.human(n * PREVIEW_EST)}" if n else ""
    if p["clips_skipped"]:
        note += f"  ({p['clips_skipped']} clips need ffmpeg)"
    rows = [("previews", "Gallery previews", n + len(p["gone"]), what, note)]

    covers = plan["covers"]
    names = {"comic": "comic", "comic folder": "comic folder", "ebook": "ebook", "pdf": "PDF", "audiobook": "audiobook"}
    parts = [f"{_count(covers, k)} {v}{'s' if _count(covers, k) != 1 else ''}"
             for k, v in names.items() if _count(covers, k)]
    why = {"pdf": "PDFs need PyMuPDF or pdftoppm", "audiobook": "audiobooks need ffmpeg"}
    note = ", ".join(f"{n} {why[k]}" for k, n in plan["covers_cant"].items())
    rows.append(("covers", "Covers for books", len(covers),
                 (", ".join(parts) + " with no cover") if parts else "nothing to make",
                 f"({note})" if note else ""))

    st = plan["stills"]
    films, shows = _count(st, "film"), _count(st, "show")
    rows.append(("stills", "Stills for films", len(st),
                 " and ".join(f"{n} {w}{'s' if n != 1 else ''}" for n, w in ((films, "film"), (shows, "show")) if n)
                 + " with no cover" if st else "nothing to make",
                 "(films without a cover need ffmpeg)" if plan["stills_cant"] else ""))

    rows.append(("shrink", "Shrink covers", len(plan["shrink"]),
                 f"{len(plan['shrink'])} wider than {COVER_W} px", "replaces them" if plan["shrink"] else ""))
    rows.append(("recipes", "Shrink recipe photos", len(plan["recipes"]),
                 f"{len(plan['recipes'])} wider than {RECIPE_W} px", "replaces them" if plan["recipes"] else ""))
    return rows


def _choose(rows) -> list[str]:
    live = [r for r in rows if r[2]]
    default = [r[0] for r in live if r[0] not in OVERWRITES and r[0] != "stills"]
    nums = {str(i): r[0] for i, r in enumerate(rows, 1) if r[2]}
    hint = ", ".join(k for k, v in nums.items() if v in default)
    while True:
        raw = input(f"Choose ({'Enter for ' + hint + ', ' if hint else ''}a for all, numbers like 1,4, or 0 to cancel): ").strip().lower()
        if raw == "" and default:
            return default
        if raw in ("0", ""):
            return []
        if raw in ("a", "all"):
            return [r[0] for r in live]
        picks = [p for p in re.split(r"[,\s]+", raw) if p]
        if picks and all(p in nums for p in picks):
            return [nums[p] for p in sorted(set(picks), key=int)]
        print("  Type the number of a job that has something to do, or 'a' for all.")


def run(dest: str | None = None, jobs: list[str] | None = None, assume_yes: bool = False,
        check: bool = False, shrink_dirs: list[str] | None = None) -> int:
    nc.banner("Pictures")
    if not _have_pillow():
        return 1
    dest = nc.choose_card(dest, prompt="Which card?")
    if not dest:
        print("Cancelled.")
        return 1
    root = Path(dest)

    print(nc.c("2", "  Looking through the card..."))
    plan = _plan(root, shrink_dirs)
    rows = _rows(plan)
    print()
    for i, (key, title, n, what, note) in enumerate(rows, 1):
        mark = f"  {i}." if n else nc.c("2", f"  {i}.")
        line = f"{mark} {title:<22} {what:<44}"
        print(line if n else nc.c("2", line), nc.c("33" if key in OVERWRITES else "2", note) if note else "")

    if check:
        return 0
    if not any(r[2] for r in rows if jobs is None or r[0] in jobs):
        print("\nNothing to do.")
        return 0
    chosen = [r[0] for r in rows if r[2] and r[0] in jobs] if jobs is not None else _choose(rows)
    if not chosen:
        print("Nothing picked, nothing changed.")
        return 1

    if set(chosen) & OVERWRITES and not assume_yes:
        print(nc.c("1;33", "\nThese replace the pictures on the card. There's no backup!"))
        if "shrink" in chosen:
            print(f"   • {len(plan['shrink'])} covers in Movies, Shows, Books and Games  → {COVER_W} px wide")
        if "recipes" in chosen:
            print(f"   • {len(plan['recipes'])} recipe photos in Cookbook  → {RECIPE_W} px wide")
        if input("Type 'YES' to go ahead, anything else to skip them: ").strip() != "YES":
            chosen = [j for j in chosen if j not in OVERWRITES]
            print("  Skipping those.")

    failed = 0
    try:
        if "previews" in chosen:
            failed += _do_previews(root, plan["previews"])
        if "covers" in chosen:
            none = _load_none(root)

            def empty(job, ok):
                if ok is False:
                    none[job[1].relative_to(root / "Books").as_posix()] = job[1].stat().st_size
            failed += _pool("Covers for books", plan["covers"], _make_book_cover,
                            lambda j: str(j[1].relative_to(root)), empty, workers=4)
            _save_none(root, none)
        if "stills" in chosen:
            failed += _pool("Stills for films", plan["stills"], _make_still,
                            lambda j: str(j[1].relative_to(root)), workers=2)
        if "shrink" in chosen:
            failed += _pool("Shrink covers", plan["shrink"], lambda p: _shrink(p, COVER_W, COVER_Q),
                            lambda p: str(p.relative_to(root)))
        if "recipes" in chosen:
            failed += _pool("Shrink recipe photos", plan["recipes"], lambda p: _shrink(p, RECIPE_W, RECIPE_Q),
                            lambda p: str(p.relative_to(root)))
    except KeyboardInterrupt:
        print(nc.c("1;31", "\n  Stopped. What's done so far is kept, run it again to finish."))
        return 130

    if {"covers", "stills"} & set(chosen):
        print(nc.c("1;33", "\n  New covers show up once the media index is rebuilt."))
        if assume_yes or input("  Rebuild it now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            build_index.run(dest=dest)
    print(nc.c("1", "\nDone." if not failed else f"\nDone, with {failed} error(s)."))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Gallery previews, missing covers and smaller pictures on a Nomad card. Asks before replacing anything.")
    ap.add_argument("card", nargs="?", help="where the card is mounted (skips the card picker)")
    ap.add_argument("-o", "--output", help="same as giving the card on its own")
    ap.add_argument("--jobs", help="jobs to run instead of asking, split by commas: " + ",".join(JOBS))
    ap.add_argument("--dirs", help="for shrink, only these folders out of Movies,Shows,Books,Games")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask, even before replacing pictures")
    ap.add_argument("--check", action="store_true", help="just show the list, don't change anything")
    args = ap.parse_args()
    jobs = None
    if args.jobs:
        jobs = [j.strip().lower() for j in args.jobs.split(",") if j.strip()]
        bad = [j for j in jobs if j not in JOBS]
        if bad:
            ap.error(f"don't know the job(s): {', '.join(bad)} (pick from {', '.join(JOBS)})")
    return run(dest=args.card or args.output, jobs=jobs, assume_yes=args.yes, check=args.check,
               shrink_dirs=args.dirs.split(",") if args.dirs else None)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except EOFError:
        print("\nNo input, stopping.")
        sys.exit(1)
