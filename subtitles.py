#!/usr/bin/env python3
"""
subtitles: subtitles out of the videos, and seek previews, for Movies and Shows.

It looks the card over, lists what each job would do, and you choose:

    Subtitles      text subtitle tracks inside a video (MKV, MP4) saved beside it as
                   Name.srt / Name.<lang>.srt, which Nomad's player shows. Seconds a
                   film. Picture subtitles (PGS, VobSub) are counted, not converted:
                   they would need text recognition.
    Seek previews  a small picture of where you'll land while you drag the progress
                   bar (Jellyfin calls these trickplay images). Nomad's player reads
                   them from Name.thumbs.vtt and sheets of 50 frames beside the video,
                   about 300 KB a film. Only keyframes are decoded, so it's a few seconds
                   a film, but every film is read once. Off unless you pick it.

Only adds files; a later run makes only what's missing, remakes previews for a video
that changed, and clears previews whose video is gone. Needs ffmpeg, and Pillow for
the previews.

    ./subtitles.py                          # pick a card, see the list, choose
    ./subtitles.py /media/me/NOMAD --check  # just the list
    ./subtitles.py /media/me/NOMAD --jobs subs,previews -y
"""
from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import nomad_common as nc
import fix_videos as fv
import pictures

FOLDERS = ("Movies", "Shows")
JOBS = ("subs", "previews")
PICTURE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
# seek previews: a frame every 10 s, at most 200 a film, 128 px wide, 50 to a sheet
TILE_W = 128
EVERY_S = 10
MAX_TILES = 200
COLS, ROWS = 10, 5
SHEET_Q = 70
EST_BYTES = 300 * 1024
THUMBS = ".thumbs"


def _videos(root: Path) -> list[Path]:
    tops = [root / d for d in FOLDERS if (root / d).is_dir()] if nc.looks_like_nomad(root) else [root]
    out = []
    for top in tops:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix.lower() in fv.VIDEO_EXTS and not name.startswith(".") and not name.endswith(fv.TMP_SUFFIX):
                    out.append(p)
    return out


# subtitles

def _sub_targets(v: Path, streams: list[dict]) -> tuple[list[tuple[int, Path]], int]:
    """(stream index, .srt to write) for text tracks with no .srt yet, and how many are pictures."""
    todo, pics, used = [], 0, set()
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        if s.get("codec_name") in PICTURE_SUBS:
            pics += 1
            continue
        if s.get("codec_name") not in fv.TEXT_SUBS:
            continue
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        stem = v.stem + (f".{lang}" if lang and lang != "und" else "")
        name, n = stem, 2
        while name in used:
            name, n = f"{stem}.{n}", n + 1
        used.add(name)
        out = v.parent / f"{name}.srt"
        if not out.exists():
            todo.append((s["index"], out))
    return todo, pics


def _save_subs(job) -> bool:
    v, targets = job
    made = 0
    for index, out in targets:
        tmp = out.with_name(out.name + fv.TMP_SUFFIX)
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(v), "-map", f"0:{index}",
                            "-c:s", "srt", "-f", "srt", str(tmp)], capture_output=True)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size:
            os.replace(tmp, out)
            made += 1
        else:
            tmp.unlink(missing_ok=True)
    return made > 0


# seek previews

def _thumbs_vtt(v: Path) -> Path:
    return v.with_name(v.stem + THUMBS + ".vtt")


def _sheets(v: Path) -> list[Path]:
    pat = re.compile(re.escape(v.stem + THUMBS) + r"\.\d+\.jpg$")
    return sorted(p for p in v.parent.glob(v.stem + THUMBS + ".*.jpg") if pat.match(p.name))


def _stamp(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _make_previews(v: Path) -> bool:
    """Sheets of frames and the WebVTT that points into them, written beside the video."""
    dur = pictures._duration(v)
    if not dur or dur < 1:
        return False
    n = max(1, min(MAX_TILES, math.ceil(dur / EVERY_S)))
    step = dur / n
    fv.WORK_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=fv.WORK_DIR) as td:
        # keyframes only: decoding every frame of a film takes minutes; this takes seconds
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-skip_frame", "nokey", "-i", str(v),
                            "-vf", f"fps=1/{step:.4f},scale={TILE_W}:-2", "-an", "-sn",
                            "-q:v", "4", "-frames:v", str(n), str(Path(td) / "%04d.jpg")],
                           capture_output=True, timeout=1800)
        frames = sorted(Path(td).glob("*.jpg"))
        if r.returncode != 0 or not frames:
            return False
        with pictures.Image.open(frames[0]) as first:
            tw, th = first.size
        per = COLS * ROWS
        names, cues = [], ["WEBVTT", ""]
        for k in range(math.ceil(len(frames) / per)):
            chunk = frames[k * per:(k + 1) * per]
            rows = math.ceil(len(chunk) / COLS)
            sheet = pictures.Image.new("RGB", (COLS * tw if len(chunk) > COLS else len(chunk) * tw, rows * th))
            for i, f in enumerate(chunk):
                with pictures.Image.open(f) as im:
                    sheet.paste(im.convert("RGB").resize((tw, th)), ((i % COLS) * tw, (i // COLS) * th))
            name = f"{v.stem}{THUMBS}.{k + 1}.jpg"
            pictures._save_jpeg(sheet, v.parent / name, SHEET_Q)
            names.append(name)
            for i in range(len(chunk)):
                t = k * per + i
                # file names are relative to the .vtt, so the page finds them in the same folder
                cues += [f"{_stamp(t * step)} --> {_stamp(min(dur, (t + 1) * step))}",
                         f"{quote(name)}#xywh={(i % COLS) * tw},{(i // COLS) * th},{tw},{th}", ""]
        # sheets left from a longer earlier version of this video
        for old in _sheets(v):
            if old.name not in names:
                old.unlink(missing_ok=True)
        vtt = _thumbs_vtt(v)
        tmp = vtt.with_name(vtt.name + fv.TMP_SUFFIX)
        tmp.write_text("\n".join(cues), encoding="utf-8")
        os.replace(tmp, vtt)    # written last: a .vtt means its sheets are all there
    return True


def _orphans(root: Path) -> list[Path]:
    """Previews whose video is gone."""
    out = []
    tops = [root / d for d in FOLDERS if (root / d).is_dir()] if nc.looks_like_nomad(root) else [root]
    for top in tops:
        for vtt in top.rglob("*" + THUMBS + ".vtt"):
            stem = vtt.name[:-len(THUMBS + ".vtt")]
            if not any((vtt.parent / (stem + e)).exists() or (vtt.parent / (stem + e.upper())).exists() for e in fv.VIDEO_EXTS):
                out.append(vtt)
                out += [p for p in vtt.parent.glob(stem + THUMBS + ".*.jpg")]
    return out


# the list and the run

def _plan(root: Path) -> dict:
    vids = _videos(root)
    subs, pics = [], 0
    with ThreadPoolExecutor(8) as ex:
        probes = list(ex.map(fv._probe, vids))
    for v, pr in zip(vids, probes):
        todo, p = _sub_targets(v, (pr or {}).get("streams") or [])
        pics += p
        if todo:
            subs.append((v, todo))
    stale = [v for v in vids if not _thumbs_vtt(v).exists() or _thumbs_vtt(v).stat().st_mtime < v.stat().st_mtime]
    return {"subs": subs, "pics": pics, "previews": stale, "have": len(vids) - len(stale), "orphans": _orphans(root)}


def _rows(plan: dict) -> list[tuple[str, str, int, str, str]]:
    s = plan["subs"]
    tracks = sum(len(t) for _, t in s)
    rows = [("subs", "Subtitles", len(s), f"{tracks} track(s) in {len(s)} video(s) to save as .srt" if s else "nothing to pull out",
             f"({plan['pics']} are pictures, not text, so they can't be pulled out)" if plan["pics"] else "")]
    p = plan["previews"]
    what = f"{len(p)} video(s) without them" + (f", {plan['have']} done" if plan["have"] else "")
    if plan["orphans"]:
        what += ", and old ones to clear"
    rows.append(("previews", "Seek previews", len(p) + len(plan["orphans"]), what if p or plan["orphans"] else "all done",
                 f"adds about {nc.human(len(p) * EST_BYTES)}, a few seconds a film" if p else ""))
    return rows


def run(dest: str | None = None, jobs: list[str] | None = None, assume_yes: bool = False, check: bool = False) -> int:
    nc.banner("Subtitles")
    if not fv._need_ffmpeg():
        return 1
    dest = nc.choose_card(dest, prompt="Which card?")
    if not dest:
        print("Cancelled.")
        return 1
    root = Path(dest)
    print(nc.c("2", "  Looking through the videos..."))
    plan = _plan(root)
    rows = _rows(plan)
    print()
    for i, (key, title, n, what, note) in enumerate(rows, 1):
        line = f"  {i}. {title:<16} {what:<52}"
        print(line if n else nc.c("2", line), nc.c("2", note) if note else "")
    if check:
        return 0
    live = [r for r in rows if r[2]]
    if not live:
        print("\nNothing to do.")
        return 0
    if jobs is not None:
        chosen = [r[0] for r in live if r[0] in jobs]
    else:
        # seek previews are the optional one: picked only when asked for
        default = [r[0] for r in live if r[0] != "previews"]
        nums = {str(i): r[0] for i, r in enumerate(rows, 1) if r[2]}
        hint = ", ".join(k for k, v in nums.items() if v in default)
        while True:
            raw = input(f"Choose ({'Enter for ' + hint + ', ' if hint else ''}a for all, numbers like 1,2, or 0 to cancel): ").strip().lower()
            if raw == "" and default:
                chosen = default
                break
            if raw in ("0", ""):
                chosen = []
                break
            if raw in ("a", "all"):
                chosen = [r[0] for r in live]
                break
            picks = [x for x in re.split(r"[,\s]+", raw) if x]
            if picks and all(x in nums for x in picks):
                chosen = [nums[x] for x in sorted(set(picks), key=int)]
                break
            print("  Type the number of a job that has something to do, or 'a' for all.")
    if not chosen:
        print("Nothing picked, nothing changed.")
        return 1
    if "previews" in chosen and not pictures._have_pillow():
        chosen = [j for j in chosen if j != "previews"]

    failed = 0
    label = lambda job: str((job[0] if isinstance(job, tuple) else job).relative_to(root))
    try:
        if "subs" in chosen:
            failed += pictures._pool("Subtitles", plan["subs"], _save_subs, label, workers=4)
        if "previews" in chosen:
            for p in plan["orphans"]:
                p.unlink(missing_ok=True)
            failed += pictures._pool("Seek previews", plan["previews"], _make_previews, label, workers=2)
    except KeyboardInterrupt:
        print(nc.c("1;31", "\n  Stopped. What's done so far is kept, run it again to finish."))
        return 130
    print(nc.c("1", "\nDone." if not failed else f"\nDone, with {failed} error(s)."))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull subtitles out of the videos on a Nomad card so Nomad can show them. Can also make seek previews (the little pictures when you drag the bar).")
    ap.add_argument("card", nargs="?", help="where the card is mounted, or a folder (skips the card picker)")
    ap.add_argument("--jobs", help="jobs to run instead of asking, split by commas: " + ",".join(JOBS))
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask, just go")
    ap.add_argument("--check", action="store_true", help="just show the list, don't change anything")
    args = ap.parse_args()
    jobs = None
    if args.jobs:
        jobs = [j.strip().lower() for j in args.jobs.split(",") if j.strip()]
        bad = [j for j in jobs if j not in JOBS]
        if bad:
            ap.error(f"don't know the job(s): {', '.join(bad)} (pick from {', '.join(JOBS)})")
    return run(dest=args.card, jobs=jobs, assume_yes=args.yes, check=args.check)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except EOFError:
        print("\nNo input, stopping.")
        sys.exit(1)
