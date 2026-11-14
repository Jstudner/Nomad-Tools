#!/usr/bin/env python3
"""
fix_videos: find the videos that will play badly on Nomad and fix them.

The same checks as Check videos in Nomad's admin panel, run on your computer, plus
the fixes. Cheapest first:

    Fast start     the film's index made smaller and put first, so it starts
                   sooner (a 92 min film: 10.4 s to 6.5 s). A copy: the picture
                   and sound are the same bytes, and it plays everywhere it did.
    To MP4         MKV or AVI whose video is already H.264, copied into an MP4.
                   Text subtitles are saved beside it as .srt, which Nomad reads.
    Audio to AAC   Dolby, DTS and other sound browsers can't play. Video copied.
    Convert        video no browser plays (old DivX/Xvid, 10-bit, MPEG-2 ...),
                   re-encoded with Nomad's 720p settings. Slow.
    Optional       HEVC, AV1, 4K and over 2.5 Mbps. They play on some devices,
                   so these are only converted if you ask.

Originals are replaced once the new file has been checked. Needs ffmpeg.

    ./fix_videos.py                          # pick a card, see the list, choose
    ./fix_videos.py /media/me/NOMAD --check  # just the list, change nothing
    ./fix_videos.py ~/Videos/to-copy         # a folder on this computer works too
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import mp4_rechunk
import nomad_common as nc

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm"}
MP4_EXTS = {".mp4", ".m4v", ".mov"}
# where the pages look for video on a card; anywhere else on a card is left alone
CARD_FOLDERS = ("Movies", "Shows", "Gallery", "Files")

# same lines Nomad draws (NomadHealth.h): over these it marks the file
BIG_INDEX_BYTES = 2 * 1024 * 1024
HEAVY_KBPS = 2500

# Every MP4 this writes goes through mp4_rechunk (one chunk a second, index first) with an
# exact video timescale, which together take a HandBrake film's index from ~0.8 KB to
# ~0.45 KB a second of film. Fragmented MP4 would start in a second, but Firefox can't seek
# in one (it reads from the start), so this tool never writes one and un-fragments any it
# finds.

# Nomad's 720p settings, as ffmpeg options
ENCODE_VIDEO = ["-c:v", "libx264", "-preset", "slow", "-profile:v", "main", "-level:v", "3.1",
                "-crf", "22", "-maxrate", "1350k", "-bufsize", "2700k", "-pix_fmt", "yuv420p",
                "-x264-params", "keyint=60:min-keyint=30:scenecut=40",
                "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease"
                       ":force_divisible_by=2"]
ENCODE_AUDIO = ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]

OK_AUDIO = {"aac", "mp3"}
# text subtitles that can be saved as .srt; picture subtitles (PGS, VobSub) can't
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

TMP_SUFFIX = ".nomad-tmp"
# ffmpeg's output before the re-lay; kept on this computer when there's room, so the card
# is written once. Not the system temp folder: on many Linux setups that's RAM.
WORK_DIR = Path(__file__).resolve().parent / ".nomad-work"


def _need_ffmpeg() -> bool:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    print(nc.c("1;31", "This needs ffmpeg, and it isn't installed (or your computer can't find it)."))
    if os.name == "nt":
        print("  Install it with:  " + nc.c("1", "winget install Gyan.FFmpeg"))
        print(nc.c("2", "  Then close this window and start the tools again."))
    elif sys.platform == "darwin":
        print("  Install it with:  " + nc.c("1", "brew install ffmpeg"))
    else:
        print("  Install it with:  " + nc.c("1", "sudo apt install ffmpeg") +
              nc.c("2", "  (or however your Linux installs things)"))
    return False


def _probe(path: Path) -> dict | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


# the MP4 index, read the way Nomad reads it

def _boxes(f, pos: int, end: int):
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return
        size, kind, body = int.from_bytes(hdr[:4], "big"), hdr[4:8], pos + 8
        if size == 1 and len(hdr) == 16:
            size, body = int.from_bytes(hdr[8:16], "big"), pos + 16
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield kind, pos, size, body
        pos += size


def _loose_video(f, trak_body: int, trak_end: int) -> bool | None:
    """For the video track: is it written a chunk a frame (an index twice its size)?"""
    handler, samples, chunks = b"", 0, 0
    stack = [(trak_body, trak_end)]
    while stack:
        pos, end = stack.pop()
        for kind, bp, sz, body in _boxes(f, pos, end):
            if kind in (b"mdia", b"minf", b"stbl"):
                stack.append((body, bp + sz))
            elif kind == b"hdlr" and not handler:
                f.seek(body)
                handler = f.read(12)[8:12]
            elif kind in (b"stsz", b"stz2"):
                f.seek(body)
                samples = int.from_bytes(f.read(12)[8:12], "big")
            elif kind in (b"stco", b"co64"):
                f.seek(body)
                chunks = int.from_bytes(f.read(8)[4:8], "big")
    return chunks * 2 > samples if handler == b"vide" else None


def mp4_index(path: Path) -> dict:
    """Where the index (moov) is, how big, whether it's loose, and whether it's fragmented."""
    size = path.stat().st_size
    info = {"moov": 0, "at_end": False, "fragmented": False, "loose": False}
    with open(path, "rb") as f:
        seen_mdat = False
        for kind, pos, sz, body in _boxes(f, 0, size):
            if kind == b"mdat":
                seen_mdat = True
            if kind == b"moov":
                info["moov"], info["at_end"] = sz, seen_mdat
                for k, p, s, b in _boxes(f, body, pos + sz):
                    if k == b"mvex":
                        info["fragmented"] = True
                    elif k == b"trak" and _loose_video(f, b, p + s):
                        info["loose"] = True
                break
    return info


# what's wrong with one video

class Video:
    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.ext = path.suffix.lower()
        self.problems: list[str] = []     # what Nomad would say, in its words
        self.details: list[str] = []      # the numbers behind them, for the listing
        self.fix: str | None = None       # faststart | remux | audio | convert | None
        self.optional = False             # HEVC, AV1, 4K or heavy: a convert, only on request
        self.vstream = self.astream = None
        self.subs: list[dict] = []
        self.duration = 0.0
        self.note = ""

    def label(self) -> str:
        return ", ".join(self.problems)


def _video_stream(streams: list[dict]) -> dict | None:
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    return None


def _audio_stream(streams: list[dict]) -> dict | None:
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    for s in audio:
        if (s.get("disposition") or {}).get("default"):
            return s
    return audio[0] if audio else None


def check(path: Path) -> Video:
    v = Video(path)
    meta = _probe(path)
    if not meta:
        v.problems.append("Can't read")
        return v
    streams = meta.get("streams") or []
    v.vstream, v.astream = _video_stream(streams), _audio_stream(streams)
    v.subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    try:
        v.duration = float((meta.get("format") or {}).get("duration") or 0)
    except ValueError:
        v.duration = 0.0
    if not v.vstream:
        v.problems.append("Can't read")
        return v

    vcodec = v.vstream.get("codec_name", "")
    acodec = (v.astream or {}).get("codec_name", "")
    profile = (v.vstream.get("profile") or "").lower()
    pix = v.vstream.get("pix_fmt") or ""
    width = int(v.vstream.get("width") or 0)
    kbps = int(v.size * 8 / 1000 / v.duration) if v.duration > 1 else 0

    needs_convert = False
    if vcodec == "h264":
        if "10" in profile or "4:2:2" in profile or "4:4:4" in profile or pix not in ("yuv420p", "yuvj420p", ""):
            v.problems.append("Won't play (10-bit)")
            needs_convert = True
    elif vcodec in ("hevc", "av1", "vp8", "vp9"):
        pass
    else:
        v.problems.append("Won't play (old format)")
        needs_convert = True

    if v.ext == ".avi":
        v.problems.append("Won't play (AVI)")
    elif v.ext == ".mkv":
        v.problems.append("No iPhone (MKV)")

    bad_audio = bool(acodec) and acodec not in OK_AUDIO and not (v.ext == ".webm" and acodec in ("opus", "vorbis"))
    if acodec in ("ac3", "eac3"):
        v.problems.append("No sound in Chrome")
    elif bad_audio:
        v.problems.append(f"No sound in browsers ({acodec.upper()})")

    if v.ext in MP4_EXTS:
        try:
            idx = mp4_index(path)
        except OSError:
            idx = {"moov": 0, "at_end": False, "fragmented": False, "loose": False}
        if idx["fragmented"]:
            v.problems.append("Won't seek in Firefox")
        elif idx["at_end"]:
            v.problems.append("Slow to start (index at end)")
        elif idx["moov"] > BIG_INDEX_BYTES and idx["loose"]:
            # Nomad marks the same: big AND a chunk a frame, the case Fast start helps
            v.problems.append("Slow to start (large index)")
            v.details.append(f"index {idx['moov'] / 1048576:.1f} MB")
        slow_start = idx["fragmented"] or idx["at_end"] or "Slow to start (large index)" in v.problems
    else:
        slow_start = False

    optional = []
    if vcodec == "hevc":
        optional.append("Not every browser (HEVC)")
    elif vcodec == "av1":
        optional.append("Not older iPhones (AV1)")
    if width > 1920:
        optional.append("4K")
    if kbps > HEAVY_KBPS:
        optional.append("Heavy to stream")
        v.details.append(f"{kbps / 1000:.1f} Mbps")

    # the cheapest fix that clears the must-fix problems
    if needs_convert:
        v.fix = "convert"
    elif v.ext in (".mkv", ".avi"):
        # H.264, HEVC and AV1 all go into MP4 as they are; anything else is re-encoded
        v.fix = "remux" if vcodec in ("h264", "hevc", "av1") else "convert"
    elif bad_audio:
        v.fix = "audio"
    elif slow_start:
        v.fix = "faststart"
    v.optional = bool(optional) and v.fix != "convert" and v.ext != ".webm"
    v.problems += optional
    return v


# fixing

def _out_path(v: Video) -> Path:
    return v.path if v.ext in (".mp4", ".m4v") else v.path.with_suffix(".mp4")


def _timescale(v: Video) -> list[str]:
    """An exact clock for the video: every frame the same length makes the timing table
    one row instead of one a frame or two (HandBrake's 90 kHz clock jitters 3753/3754)."""
    try:
        num, den = (int(x) for x in (v.vstream.get("r_frame_rate") or "0/0").split("/"))
    except ValueError:
        return []
    if den == 1001 and num >= 1000:
        return ["-video_track_timescale", str(num)]        # 24000, 30000, 60000
    if den == 1 and 0 < num < 1000:
        return ["-video_track_timescale", str(num * 1000)]  # 25 -> 25000
    return []


def _command(v: Video, tmp: Path) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(v.path),
           "-map", f"0:{v.vstream['index']}"]
    if v.astream:
        cmd += ["-map", f"0:{v.astream['index']}"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-sn", "-dn"]
    if v.fix == "convert":
        cmd += ENCODE_VIDEO + ENCODE_AUDIO
    else:
        cmd += ["-c:v", "copy"]
        if v.vstream.get("codec_name") == "hevc":
            cmd += ["-tag:v", "hvc1"]       # Safari only plays HEVC tagged hvc1
        acodec = (v.astream or {}).get("codec_name", "")
        cmd += ["-c:a", "copy"] if acodec in OK_AUDIO else ENCODE_AUDIO
    cmd += _timescale(v) + ["-progress", "pipe:1", "-nostats", "-f", "mp4", str(tmp)]
    return cmd


def _save_subtitles(v: Video) -> int:
    """Text subtitles inside an MKV go beside the MP4 as Name.srt / Name.<lang>.srt."""
    saved, used = 0, set()
    for s in v.subs:
        if s.get("codec_name") not in TEXT_SUBS:
            continue
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        stem = v.path.stem + (f".{lang}" if lang and lang != "und" else "")
        name, n = stem, 2
        while name in used or (v.path.parent / f"{name}.srt").exists():
            name, n = f"{stem}.{n}", n + 1
        used.add(name)
        out = v.path.parent / f"{name}.srt"
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(v.path),
                            "-map", f"0:{s['index']}", "-c:s", "srt", str(out)],
                           capture_output=True)
        if r.returncode == 0 and out.exists() and out.stat().st_size:
            saved += 1
        elif out.exists():
            out.unlink()
    return saved


def _run_ffmpeg(cmd: list[str], duration: float, progress: nc.Progress, detail: str) -> tuple[bool, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us=") and duration:
                try:
                    done = int(line.split("=", 1)[1]) / 1e6
                    progress.pct(min(100.0, done * 100.0 / duration), detail)
                except ValueError:
                    pass
    except KeyboardInterrupt:
        proc.kill()                       # stop it before the half-written copy is removed
        proc.wait()
        raise
    err = proc.stderr.read()
    return proc.wait() == 0, err.strip().splitlines()[-1] if err.strip() else ""


def fix(v: Video, progress: nc.Progress, detail: str) -> tuple[bool, str]:
    out = _out_path(v)
    if out != v.path and out.exists():
        return False, f"there's already a {out.name}"
    free = shutil.disk_usage(v.path.parent).free
    if free < v.size * 1.1 + 50 * 1024 * 1024:
        return False, f"not enough free space (needs about {nc.human(int(v.size * 1.1))})"

    tmp = out.with_name(out.name + TMP_SUFFIX)
    # ffmpeg writes here first, then the re-lay writes tmp on the card
    WORK_DIR.mkdir(exist_ok=True)
    local = shutil.disk_usage(WORK_DIR).free > v.size * 1.2 + 200 * 1024 * 1024
    work = (WORK_DIR if local else out.parent) / (out.name + ".work" + TMP_SUFFIX)
    try:
        ok, err = _run_ffmpeg(_command(v, work), v.duration, progress, detail)
        if not ok:
            return False, err or "ffmpeg stopped with an error"
        progress.pct(100.0, detail + " index")
        try:
            mp4_rechunk.rechunk(work, tmp)
        except (ValueError, IndexError, struct.error):
            # something the re-lay doesn't handle: fall back to ffmpeg's own index-first copy
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(work), "-map", "0",
                                "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return False, "couldn't make it start faster"
        work.unlink()
        # check the copy before anything is replaced: it opens, it has video, it's as long
        meta = _probe(tmp)
        got = float(((meta or {}).get("format") or {}).get("duration") or 0)
        if not meta or not _video_stream(meta.get("streams") or []):
            return False, "the new copy couldn't be opened, so the original was kept"
        if v.duration and abs(got - v.duration) > max(2.0, v.duration * 0.01):
            return False, f"the new copy is {got:.0f}s long but the original is {v.duration:.0f}s, so the original was kept"
        # and its last seconds decode, so nothing was cut short on the way
        end = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-sseof", "-3", "-i", str(tmp),
                              "-f", "null", "-"], capture_output=True, text=True)
        if end.returncode != 0 or end.stderr.strip():
            return False, "the end of the new copy didn't play back, so the original was kept"
        subs = _save_subtitles(v) if out != v.path else 0
        os.replace(tmp, out)
        if out != v.path:
            v.path.unlink()
        return True, f"{subs} subtitle file(s) saved" if subs else ""
    finally:
        for leftover in (tmp, work):
            if leftover.exists():
                leftover.unlink()


# finding the videos

def _videos_under(root: Path) -> list[Path]:
    is_card = nc.looks_like_nomad(root)
    tops = [root / d for d in CARD_FOLDERS if (root / d).is_dir()] if is_card else [root]
    found = []
    for top in tops:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if name.endswith(TMP_SUFFIX):
                    p.unlink(missing_ok=True)          # left by an interrupted run
                elif p.suffix.lower() in VIDEO_EXTS and not name.startswith("."):
                    found.append(p)
    return found


FIX_TITLES = {
    "faststart": ("Fast start", "starts sooner, picture and sound untouched, a minute or two a film"),
    "remux": ("To MP4", "copied into an MP4, a few seconds a film"),
    "audio": ("Audio to AAC", "video kept, sound redone, a minute or two a film"),
    "convert": ("Convert", "redone at 720p, takes about as long as the film or longer"),
}


def _report(root: Path, videos: list[Video]) -> None:
    flagged = [v for v in videos if v.problems]
    counts: dict[str, int] = {}
    for v in flagged:
        for p in v.problems:
            counts[p] = counts.get(p, 0) + 1
    issues = sum(counts.values())
    print(nc.c("1", f"\n{len(videos)} {'video' if len(videos) == 1 else 'videos'}, "
                    f"{issues or 'no'} {'issue' if issues == 1 else 'issues'}."))
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {k}")
    cant = [v for v in flagged if v.problems == ["Can't read"]]
    if cant:
        print(nc.c("2", f"\n  {len(cant)} can't be read at all, so they're left alone:"))
        for v in cant[:10]:
            print(nc.c("2", f"    {v.path.relative_to(root)}"))


def _plan(videos: list[Video]) -> tuple[list[Video], list[Video]]:
    """What has to be fixed, and what could be converted on request (some of both)."""
    return [v for v in videos if v.fix], [v for v in videos if v.optional]


def _print_plan(must: list[Video], opt: list[Video]) -> None:
    print(nc.c("1", "\nWhat can be fixed:"))
    for kind in ("faststart", "remux", "audio", "convert"):
        group = [v for v in must if v.fix == kind]
        if group:
            title, how = FIX_TITLES[kind]
            print(f"  {len(group):>5}  {title:<14}" + nc.c("2", how))
    if opt:
        print(f"  {len(opt):>5}  {'Optional':<14}" +
              nc.c("2", "HEVC, AV1, 4K or big files. They play on some devices, convert them if you want"))


def run(dest: str | None = None, check_only: bool = False, include_optional: bool | None = None,
        assume_yes: bool = False) -> int:
    nc.banner("Fix videos")
    if not _need_ffmpeg():
        return 1
    dest = nc.choose_card(dest, prompt="Which card (or folder) are the videos on?")
    if not dest:
        print("Cancelled.")
        return 1
    root = Path(dest)

    if WORK_DIR.is_dir():
        for stale in WORK_DIR.glob("*" + TMP_SUFFIX):    # an interrupted run's work file
            stale.unlink(missing_ok=True)
    paths = _videos_under(root)
    if not paths:
        print("No videos found.")
        return 0
    progress = nc.Progress()
    videos = []
    for i, p in enumerate(paths, 1):
        progress.pct(i * 100.0 / len(paths), f"checking {i} of {len(paths)}")
        videos.append(check(p))
    progress.done()
    _report(root, videos)

    must, opt = _plan(videos)
    if not must and not opt:
        print(nc.c("32", "\nNothing to fix."))
        return 0
    _print_plan(must, opt)
    if check_only:
        return 0

    if include_optional is None and opt and not assume_yes:
        r = input(f"\nConvert the {len(opt)} optional one(s) too? [y/N] ").strip().lower()
        include_optional = r in ("y", "yes")
    if include_optional:
        for v in opt:
            v.fix = "convert"             # a convert clears whatever cheap fix it also needed
    todo = [v for v in videos if v.fix]
    if not todo:
        print("Nothing to do.")
        return 0

    if not assume_yes:
        print(nc.c("1;33", "\nEach video gets replaced by its fixed copy once the copy checks out."))
        print("  If you might want the originals back, copy them somewhere else first.")
        r = input(f"Fix {len(todo)} video(s)? Type 'YES' to go ahead: ").strip()
        if r != "YES":
            print("Cancelled.")
            return 1

    # quick ones first, so a long convert doesn't hold up the rest
    order = {"faststart": 0, "remux": 1, "audio": 2, "convert": 3}
    todo.sort(key=lambda v: (order[v.fix], v.size))
    done = failed = 0
    t0 = time.time()
    for i, v in enumerate(todo, 1):
        rel = v.path.relative_to(root)
        title = FIX_TITLES[v.fix][0]
        progress(f"  {title}: {rel}")
        ok, msg = fix(v, progress, f"{i}/{len(todo)}")
        progress.done()
        if ok:
            done += 1
            if msg:
                progress(nc.c("2", f"    {msg}"))
        else:
            failed += 1
            progress(nc.c("31", f"    Skipped: {msg}"))

    secs = time.time() - t0
    took = f"{secs:.0f} s" if secs < 90 else f"{secs / 60:.0f} min"
    print(nc.c("1", f"\nFixed {done}, skipped {failed}, in {took}."))
    if done and nc.looks_like_nomad(root):
        print("Sizes and names changed, so run 'Rebuild the media index' next.")
        print("Then press Check videos in the admin panel on Nomad to clear the warnings.")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Find and fix videos that won't play or are slow to start on Nomad.")
    ap.add_argument("card", nargs="?", help="where the card is mounted, or any folder (skips the card picker)")
    ap.add_argument("--check", action="store_true", help="just list what's wrong, don't change anything")
    ap.add_argument("--optional", action="store_true",
                    help="also convert HEVC, AV1, 4K and big videos")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask, just go")
    args = ap.parse_args()
    return run(dest=args.card, check_only=args.check,
               include_optional=True if args.optional else (False if args.yes else None),
               assume_yes=args.yes)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
