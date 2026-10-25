#!/usr/bin/env python3
"""
media_types: what every nomad card folder accepts, in one place.

Each page on the device decides for itself what it will show, using a fixed list
of file extensions and a fixed idea of how deep to look. Those rules are not
written down anywhere a user can read them, which is why "I copied a file on and
nothing happened" is the most common question about the device.

This module is that list, transcribed from the pages themselves:

    Movies    SD_Card_Template/movies.html      VIDEO_EXTS / IMAGE_EXTS / SUB_EXTS
    Shows     SD_Card_Template/shows.html       same lists, but folders only
    Books     SD_Card_Template/books.html       BOOK_EXTS / AUDIO_EXTS / COMIC_EXTS
    Music     SD_Card_Template/songs.html       AUDIO_EXTS
    Gallery   SD_Card_Template/gallery.html     IMAGE_EXTS / VIDEO_EXTS
    Games     SD_Card_Template/games.html       deriveCore()
    Cookbook  SD_Card_Template/cookbook.html    RECIPE_RE / NOT_RECIPE
    Workshop  SD_Card_Template/workshop.html    VIEWABLE / OTHER / NOTE_RE
    Files     firmware file browser             anything

Keep it in step with those files. If a page learns a new extension and this list
doesn't, the tools will tell someone their file is unsupported when it isn't.

Archive (ZIM) and Maps are not here. Both need more than a copy
(an index build, a tile pyramid), so they belong to import_zim and map_tiles.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── the shared extension vocabularies ─────────────────────────────────────────
# Named per page rather than merged, because the pages disagree: Books
# accepts .gif and .bmp covers, Movies does not; Gallery plays .ogg video, Movies
# plays .mkv. Merging them would make the tools promise things the device won't do.

VIDEO_MOVIE = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"})
VIDEO_GALLERY = frozenset({".mp4", ".webm", ".ogg", ".mov"})
IMAGE_MOVIE = frozenset({".jpg", ".jpeg", ".png", ".webp", ".avif"})
IMAGE_GALLERY = frozenset({".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"})
IMAGE_BOOK = frozenset({".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"})
IMAGE_SIDECAR = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
SUBTITLE = frozenset({".srt", ".vtt", ".ass", ".ssa", ".sub"})
BOOK = frozenset({".pdf", ".epub", ".mobi", ".azw3", ".cbz", ".cbr",
                  ".cbt", ".cb7", ".djvu", ".txt"})
COMIC = frozenset({".cbz", ".cbr"})
AUDIOBOOK = frozenset({".mp3", ".m4b", ".m4a", ".ogg", ".wav", ".aac", ".flac"})
MUSIC = frozenset({".mp3", ".m4a", ".flac", ".aac", ".ogg", ".wav",
                   ".opus", ".alac", ".aiff"})
RECIPE = frozenset({".json", ".md", ".markdown", ".txt", ".cook"})
MODEL_3D = frozenset({".stl", ".obj", ".3mf"})
MODEL_OTHER = frozenset({".gcode", ".bgcode", ".gco", ".amf", ".ply", ".step",
                         ".stp", ".scad", ".f3d", ".sldprt", ".ipt", ".blend",
                         ".3ds", ".dae", ".dxf", ".svg", ".zip"})
NOTE = frozenset({".md", ".markdown", ".txt", ".nfo"})

# games.html deriveCore(): extension -> emulator core. A ROM whose extension
# isn't here is indexed but never offered, because nothing can run it.
ROM_CORES = {
    ".nes": "nes",
    ".sfc": "snes", ".smc": "snes",
    ".gba": "gba",
    ".gb": "gb",
    ".gbc": "gbc",
    ".n64": "n64",
    ".gen": "genesis", ".md": "genesis",
    ".sms": "sms",
    ".psx": "psx", ".ps1": "psx",
    ".wad": "prboom", ".iwad": "prboom",
}
# deriveCore peels one of these off and looks at what's underneath, so
# "Zelda.nes.zip" works. A bare "Zelda.zip" does not: there is no inner
# extension to read, and the game is silently skipped.
ROM_WRAPPERS = frozenset({".zip", ".iso", ".bin", ".7z", ".rar"})

# cookbook.html NOT_RECIPE: /^(readme|index|notes|_.*)$/i. Documentation that
# lives beside the recipes. Without this the folder's README shows up on the
# page as a dish called "README".
NOT_RECIPE_STEMS = frozenset({"readme", "index", "notes"})


def is_recipe(stem: str) -> bool:
    """False for the names cookbook.html treats as documentation, not a dish."""
    s = stem.lower()
    return s not in NOT_RECIPE_STEMS and not s.startswith("_")


FAT32_MAX = 4 * 1024 * 1024 * 1024   # a single file may not reach this on FAT32


@dataclass(frozen=True)
class Bucket:
    """One top-level card folder, and what its page will do with what's inside."""

    folder: str                 # the folder name on the card, e.g. "Movies"
    summary: str                # one line: what the page is for
    media: frozenset            # extensions the page will play, read or run
    layout: str                 # where files are meant to sit
    scan_depth: int             # levels below the folder the page actually reads
    cover: str = ""             # how art is attached, "" when the page has none
    covers: frozenset = frozenset()      # extensions accepted as art
    companions: frozenset = frozenset()  # subtitles, notes: kept, never listed alone
    folders_only: bool = False  # page lists directories, so loose files vanish
    notes: tuple = ()           # anything else worth saying before someone copies

    @property
    def path(self) -> str:
        return "/" + self.folder


BUCKETS: tuple[Bucket, ...] = (
    Bucket(
        folder="Movies",
        summary="films, each with an optional poster beside it",
        media=VIDEO_MOVIE,
        covers=IMAGE_MOVIE,
        companions=SUBTITLE,
        layout="a folder per film, or loose files at the top. Both work.",
        scan_depth=10,
        cover="an image named like the video (Dune.mp4 + Dune.jpg)",
        notes=(
            "Subtitles ride along with the film and may carry a language suffix, "
            "as in Dune.en.srt.",
        ),
    ),
    Bucket(
        folder="Shows",
        summary="series, browsed season by season",
        media=VIDEO_MOVIE,
        covers=IMAGE_MOVIE,
        companions=SUBTITLE,
        layout="a folder per show, seasons inside it. One show, one folder.",
        scan_depth=10,
        cover="an image inside the show folder named poster, cover, folder or art",
        folders_only=True,
        notes=(
            "The Shows page lists folders and nothing else. An episode dropped "
            "straight into /Shows is indexed but never appears.",
        ),
    ),
    Bucket(
        folder="Books",
        summary="ebooks, comics, PDFs and audiobooks",
        media=BOOK | AUDIOBOOK,
        covers=IMAGE_BOOK,
        layout="loose files, or a folder per book or series.",
        scan_depth=1,
        cover="an image named like the book (Dune.epub + Dune.jpg)",
        notes=(
            "A folder of 3 or more loose page images, with no ebook or audio "
            "file in it, is treated as one unpacked comic and is not opened "
            "further. A .cbz or .cbr is just a file and needs no such folder.",
            "The index reads one level down. A book buried deeper is not found.",
        ),
    ),
    Bucket(
        folder="Music",
        summary="albums, playlists and loose songs",
        media=MUSIC,
        covers=IMAGE_MOVIE,
        layout="Artist/Album/song, a playlist folder, or loose songs at the top.",
        scan_depth=10,
        cover="an image in the album folder",
    ),
    Bucket(
        folder="Gallery",
        summary="photos and short clips",
        media=IMAGE_GALLERY | VIDEO_GALLERY,
        layout="loose files, or folders to group them.",
        scan_depth=10,
        notes=(
            "Thumbnails are made in the browser from the full file, so very large "
            "photos make the grid slow to fill.",
        ),
    ),
    Bucket(
        folder="Games",
        summary="emulated ROMs and the built-in browser games",
        media=frozenset(ROM_CORES),
        covers=IMAGE_SIDECAR,
        layout="loose ROMs, or folders to group them.",
        scan_depth=10,
        cover="an image named like the ROM (Zelda.nes + Zelda.png)",
        notes=(
            "A zipped ROM must keep its real extension: Zelda.nes.zip is read, "
            "Zelda.zip is not.",
            "The .html files already in /Games are the built-in games. Leave them.",
        ),
    ),
    Bucket(
        folder="Cookbook",
        summary="recipes in Markdown, JSON or Cooklang",
        media=RECIPE,
        covers=IMAGE_SIDECAR,
        layout="loose recipes, or one folder per category. One level, no deeper.",
        scan_depth=1,
        cover="an image named like the recipe (Cornbread.md + Cornbread.jpg)",
        notes=(
            "The Cookbook page ships switched off. Turn it on in "
            "Admin > Menu Pages > Cookbook.",
            "Files called README, index or notes are treated as documentation, "
            "not as recipes.",
        ),
    ),
    Bucket(
        folder="Workshop",
        summary="3D models, with print notes",
        media=MODEL_3D | MODEL_OTHER,
        covers=IMAGE_SIDECAR,
        companions=NOTE,
        layout="loose models, or one folder per collection. One level, no deeper.",
        scan_depth=1,
        cover="an image named like the model (Bracket.stl + Bracket.jpg)",
        notes=(
            "Only .stl, .obj and .3mf get a 3D preview. The rest are listed and "
            "can be downloaded.",
            "A .md or .txt named like the model becomes its print notes.",
        ),
    ),
    Bucket(
        folder="Files",
        summary="anything at all, for the file browser",
        media=frozenset(),          # empty means "no extension rule"
        layout="whatever shape you like.",
        scan_depth=10,
        notes=("Nothing here is filtered. If it copies, it is listed.",),
    ),
)

BY_FOLDER = {b.folder.lower(): b for b in BUCKETS}


def bucket_for(name: str) -> Bucket | None:
    """Look up a bucket by folder name, case-insensitively."""
    return BY_FOLDER.get(name.strip().strip("/").lower())


def rom_core(filename: str) -> str:
    """The emulator core games.html would pick, or "" if it would skip the file.

    Mirrors deriveCore(): take the last extension, and if it is only a container
    take the one before it instead.
    """
    parts = filename.lower().split(".")
    if len(parts) < 2:
        return ""
    ext = "." + parts.pop()
    if ext in ROM_WRAPPERS and parts:      # `parts`, not `len(parts) > 1`: the
        ext = "." + parts.pop()            # device peels even a bare "nes.zip"
    return ROM_CORES.get(ext, "")
