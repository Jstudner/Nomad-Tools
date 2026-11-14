#!/usr/bin/env python3
"""
zim_prepare: rewrite ZIM archives so Nomad serves them fast, to every phone at once.

A ZIM keeps its text (pages, stylesheets, scripts) in compressed clusters of ~2 MB. To
show one page Nomad has to unpack the cluster it sits in, as far as that page, and an
S3 only has room for a couple of those at a time. This rewrites the archive with small
clusters (128 KB), so a page costs a few dozen KB of unpacking and dozens of them fit in
memory together. Phones that read archives themselves fetch less, too.

Nothing else changes. Every entry keeps its address, title, type and bytes. Pictures,
video and Kiwix's own title and full-text search entries are copied as they are, so the
file is still a normal ZIM that Kiwix opens. Old archives (xz, the format before 2021)
come out as zstd, which Nomad unpacks faster.

Nomad's own search index points at clusters, so it is updated afterwards in place
(update_index: only the moved numbers change, journaled so a stopped run is finished by
the next one). A card without one gets the full build Add ZIM archives runs.

    python zim_prepare.py                        # pick a card, see each archive, choose
    python zim_prepare.py -o E:\\ --all --yes     # every archive on that card
    python zim_prepare.py wiki.zim -O out/        # one file on the PC, written to out/

Size: text costs a little more in small clusters (measured: +0.5 % to +7 % of the text on
most archives, more on text-only ones like Wikivoyage). The tool says what it wrote.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import lzma
import os
import shutil
import struct
import sys
import time
import zlib
import bz2
from pathlib import Path

import nomad_common as nc
import zim_engine

try:                                    # Python 3.14+
    from compression import zstd as _zstd

    def zstd_compress(data: bytes, level: int) -> bytes:
        return _zstd.compress(data, level=level)          # writes the content size

    def zstd_decompress(data: bytes) -> bytes:
        return _zstd.decompress(data)
except ImportError:                     # older Pythons: the zstandard package (requirements.txt)
    import zstandard as _zs

    def zstd_compress(data: bytes, level: int) -> bytes:
        return _zs.ZstdCompressor(level=level, write_content_size=True).compress(data)

    def zstd_decompress(data: bytes) -> bytes:
        out = bytearray()
        d = _zs.ZstdDecompressor().decompressobj()
        out += d.decompress(data)
        return bytes(out)

MAGIC = 0x044D495A
TARGET = 128 * 1024              # cluster size Nomad is tuned for
SIZES = (128 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024)
GROWTH = 0.08                    # the most an archive may grow before bigger clusters are used
SKIP = 0.15                      # past this even at 1 MB: left as it is (it still works, slower)
LEVEL = 19                       # same as libzim's own
NONE, NONE2, ZLIB, BZ2, XZ, ZSTD = 0, 1, 2, 3, 4, 5
NO_TITLES = 0xFFFFFFFFFFFFFFFF
PART = zim_engine.SPLIT_PART_SIZE
FAT32_MAX = zim_engine.FAT32_MAX - 16 * 1024 * 1024


# reading

class Source:
    """A ZIM on disk, whole or split (.zimaa, .zimab...), with a block cache."""
    BLOCK = 1 << 20

    def __init__(self, paths: list[Path]):
        self.paths = paths
        self.files = [open(p, "rb") for p in paths]
        self.sizes = [os.path.getsize(p) for p in paths]
        self.size = sum(self.sizes)
        self._cache: dict[int, bytes] = {}

    def close(self) -> None:
        for f in self.files:
            f.close()

    def _raw(self, off: int, n: int) -> bytes:
        out = bytearray()
        base = 0
        for f, s in zip(self.files, self.sizes):
            if n and off < base + s:
                local = off - base
                take = min(s - local, n)
                f.seek(local)
                out += f.read(take)
                off += take
                n -= take
            base += s
        return bytes(out)

    def read(self, off: int, n: int) -> bytes:
        if n > self.BLOCK:
            return self._raw(off, n)
        out = bytearray()
        while n > 0:
            b = off // self.BLOCK
            blk = self._cache.get(b)
            if blk is None:
                if len(self._cache) > 64:
                    self._cache.clear()
                blk = self._cache[b] = self._raw(b * self.BLOCK, self.BLOCK)
            at = off - b * self.BLOCK
            piece = blk[at:at + n]
            if not piece:
                break
            out += piece
            off += len(piece)
            n -= len(piece)
        return bytes(out)


class Zim:
    def __init__(self, src: Source):
        self.src = src
        h = src.read(0, 80)
        if len(h) < 80 or struct.unpack_from("<I", h)[0] != MAGIC:
            raise ValueError("this isn't a ZIM file")
        self.header = bytearray(h)
        self.major, self.minor = struct.unpack_from("<HH", h, 4)
        self.entry_count, self.cluster_count = struct.unpack_from("<II", h, 24)
        (self.url_ptr, self.title_ptr, self.cluster_ptr, self.mime_ptr,
         ) = struct.unpack_from("<QQQQ", h, 32)
        self.main_page, self.layout_page, self.checksum_pos = struct.unpack_from("<IIQ", h, 64)
        # the mime list: NUL-terminated strings, ended by an empty one
        raw = src.read(self.mime_ptr, 64 * 1024)
        end = raw.find(b"\0\0")
        if end < 0:
            raise ValueError("couldn't read this archive, it may be damaged")
        self.mime_bytes = raw[:end + 2] if end > 0 else b"\0"
        self.cluster_offsets = list(struct.unpack(
            "<%dQ" % self.cluster_count, src.read(self.cluster_ptr, 8 * self.cluster_count)))

    def cluster_end(self, c: int) -> int:
        """Where cluster c's bytes stop. libzim puts the directory after the clusters,
        so the last one ends at the lowest structure that follows it."""
        if c + 1 < self.cluster_count:
            return self.cluster_offsets[c + 1]
        start = self.cluster_offsets[c]
        after = [p for p in (self.url_ptr, self.cluster_ptr, self.checksum_pos or self.src.size,
                             self.first_dirent, self.src.size) if p > start]
        return min(after)

    def dirent_at(self, off: int) -> bytes:
        """One directory entry's exact bytes (fixed part, url, title, parameters)."""
        for want in (512, 4096, 65536):
            b = self.src.read(off, want)
            mime, plen = struct.unpack_from("<HB", b, 0)
            fixed = 12 if mime == 0xFFFF else 16
            if mime in (0xFFFE, 0xFFFD):
                raise ValueError("this archive is from before 2012, which this can't handle")
            u = b.find(b"\0", fixed)
            t = b.find(b"\0", u + 1) if u >= 0 else -1
            if t >= 0 and t + 1 + plen <= len(b):
                return b[:t + 1 + plen]
        raise ValueError("couldn't read this archive, it may be damaged (at byte %d)" % off)


def open_zim(paths: list[Path]) -> Zim:
    z = Zim(Source(paths))
    # the lowest directory entry: in libzim's layout the directory starts right after the
    # clusters, and that's where the last cluster ends
    low = z.url_ptr
    step = 1 << 20
    for i in range(0, z.entry_count, step):
        n = min(step, z.entry_count - i)
        ptrs = struct.unpack("<%dQ" % n, z.src._raw(z.url_ptr + 8 * i, 8 * n))
        low = min(low, min(ptrs))
    z.first_dirent = low
    return z


def unpack(info: int, data: bytes) -> bytes:
    comp = info & 0x0F
    if comp in (NONE, NONE2):
        return data
    if comp == ZSTD:
        return zstd_decompress(data)
    if comp == XZ:
        d = lzma.LZMADecompressor()
        return d.decompress(data)
    if comp == ZLIB:
        return zlib.decompress(data)
    if comp == BZ2:
        return bz2.decompress(data)
    raise ValueError("this archive is packed in a way this can't read (type %d)" % comp)


def blob_table(info: int, data: bytes) -> list[int]:
    os_ = 8 if info & 0x10 else 4
    fmt = "<Q" if os_ == 8 else "<I"
    first = struct.unpack_from(fmt, data, 0)[0]
    n = first // os_
    return list(struct.unpack_from("<%d%s" % (n, fmt[1]), data, 0))


# state

def status(paths: list[Path]) -> str:
    """'prepared', 'old' (xz/zlib/bzip2 clusters), or 'ready' (zstd, big clusters)."""
    z = open_zim(paths)
    try:
        compressed = seen = 0
        step = max(1, z.cluster_count // 24)
        for c in range(0, z.cluster_count, step):
            info = z.src.read(z.cluster_offsets[c], 1)[0]
            comp = info & 0x0F
            if comp in (XZ, ZLIB, BZ2):
                return "old"
            if comp != ZSTD:
                continue
            seen += 1
            frame = z.src.read(z.cluster_offsets[c] + 1, 18)
            fhd = frame[4]
            fcs_flag, single = fhd >> 6, (fhd >> 5) & 1
            if fcs_flag or single:                       # content size in the header
                size_bytes = {0: 1, 1: 2, 2: 4, 3: 8}[fcs_flag]
                at = 5 + (0 if single else 1) + [0, 1, 2, 4][fhd & 3]
                fcs = int.from_bytes(frame[at:at + size_bytes], "little") + (256 if size_bytes == 2 else 0)
                # libzim never writes the content size; we always do (and never past 512 KB
                # unless one entry is bigger)
                if fcs <= 2 * SIZES[-1]:
                    compressed += 1
        if seen == 0:
            return "prepared"                            # nothing compressed: nothing to gain
        return "prepared" if compressed * 2 >= seen else "ready"
    finally:
        z.src.close()


# writing

class PartWriter:
    """One output file, or FAT32-sized parts (<name>aa, ab...) when it has to be."""

    def __init__(self, base: Path, split: bool):
        self.base = base
        self.split = split
        self.paths: list[Path] = []
        self.files = []
        self.pos = 0

    def _file_for(self, idx: int):
        while len(self.files) <= idx:
            p = Path(str(self.base) + zim_engine._split_suffix(len(self.files))) if self.split else self.base
            self.paths.append(p)
            self.files.append(open(p, "w+b"))
        return self.files[idx]

    def pwrite(self, off: int, data: bytes) -> None:
        while data:
            idx, local = (off // PART, off % PART) if self.split else (0, off)
            f = self._file_for(idx)
            take = len(data) if not self.split else min(len(data), PART - local)
            f.seek(local)
            f.write(data[:take])
            data = data[take:]
            off += take

    def write(self, data: bytes) -> int:
        at = self.pos
        self.pwrite(at, data)
        self.pos += len(data)
        return at

    def read(self, off: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            idx, local = (off // PART, off % PART) if self.split else (0, off)
            f = self._file_for(idx)
            f.seek(local)
            take = n if not self.split else min(n, PART - local)
            piece = f.read(take)
            if not piece:
                break
            out += piece
            off += len(piece)
            n -= len(piece)
        return bytes(out)

    def close(self) -> None:
        for f in self.files:
            f.flush()
            os.fsync(f.fileno())
            f.close()


def _compress_one(args):
    data, level = args
    if level == "xz":                   # test archives in the pre-2021 format (--test-xz)
        return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32, preset=9)
    return zstd_compress(data, level)


def _split(table: list[int], target: int) -> list[tuple[int, int]]:
    """Blobs, in order, grouped into runs of about `target` bytes."""
    n = len(table) - 1
    groups, b = [], 0
    while b < n:
        first, size = b, 0
        while b < n:
            blen = table[b + 1] - table[b]
            if b > first and size + blen > target:
                break
            size += blen
            b += 1
        groups.append((first, b))
    return groups


def _piece(data: bytes, table: list[int], first: int, last: int) -> bytes:
    offs = [4 * (last - first + 1)]
    for i in range(first, last):
        offs.append(offs[-1] + table[i + 1] - table[i])
    if offs[-1] >= 1 << 32:
        raise ValueError("one item in this archive is over 4 GB, which can't be sped up")
    return struct.pack("<%dI" % len(offs), *offs) + data[table[first]:table[last]]


def choose_size(paths: list[Path], level: int = LEVEL, progress=print) -> tuple[int, float]:
    """The smallest cluster size that keeps the archive within GROWTH, from a sample:
    text that repeats across pages (Wikivoyage, website copies like CDC's) compresses worse
    in small pieces. Past SKIP even at the biggest size, the caller leaves it as it is.
    Returns (size, estimated growth of the whole file)."""
    z = open_zim(paths)
    try:
        n = z.cluster_count
        if n == 0:
            return SIZES[0], 0.0
        # how much of the file is compressed clusters: every cluster's first byte (a few
        # thousand spread over a huge archive). Pictures sit in big uncompressed clusters,
        # so a guess from a handful of clusters was twice the truth on CDC's
        step = 1 if n <= 50000 else n // 5000
        comp_seen = all_seen = 0
        compressed = []
        for c in range(0, n, step):
            s, e = z.cluster_offsets[c], z.cluster_end(c)
            all_seen += e - s
            if z.src._raw(s, 1)[0] & 0x0F not in (NONE, NONE2):
                comp_seen += e - s
                compressed.append(c)
        if not compressed:
            return SIZES[0], 0.0
        cluster_bytes = z.cluster_end(n - 1) - z.cluster_offsets[0]
        comp_total = cluster_bytes * comp_seen / max(1, all_seen)
        # how much those grow in small pieces: 24 of them, spread out
        sample = []
        for c in compressed[::max(1, len(compressed) // 24)][:24]:
            s, e = z.cluster_offsets[c], z.cluster_end(c)
            raw = z.src.read(s, e - s)
            data = unpack(raw[0], raw[1:])
            sample.append((e - s, data, blob_table(raw[0], data)))
        before = sum(x[0] for x in sample)
        with cf.ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
            for size in SIZES:
                pieces = [_piece(d, t, a, b) for _, d, t in sample for a, b in _split(t, size)]
                after = sum(len(x) + 1 for x in pool.map(_compress_one, [(p, level) for p in pieces]))
                growth = comp_total * (after / before - 1) / max(1, z.src.size)
                if growth <= GROWTH or size == SIZES[-1]:
                    return size, growth
    finally:
        z.src.close()
    return SIZES[-1], 0.0


def rewrite(paths: list[Path], out_base: Path, split: bool, progress=print,
            level: int = LEVEL, target: int = TARGET, workers: int | None = None) -> dict:
    """Write the prepared archive at out_base (or out_base + aa/ab... parts). Returns
    {"paths": [...], "size": n, "clusters": (old, new)}."""
    z = open_zim(paths)
    out = PartWriter(out_base, split)
    pct = getattr(progress, "pct", None)
    try:
        # header now, filled in at the end; mime list right after it, as libzim has it
        out.write(bytes(80))
        mime_pos = out.write(z.mime_bytes)
        pad = (-out.pos) % 8
        if pad:
            out.write(bytes(pad))

        # clusters: each compressed one cut into ~target pieces, the rest as they are
        new_offsets: list[int] = []
        # old cluster -> (first new cluster, [first blob of each new cluster]) or (new, None)
        mapping: list[tuple[int, list[int] | None]] = []
        total_in = z.cluster_end(z.cluster_count - 1) - (z.cluster_offsets[0] if z.cluster_count else 0) if z.cluster_count else 0
        done_in = 0
        t0 = time.time()
        workers = workers or max(1, (os.cpu_count() or 2) - 1)
        pool = cf.ThreadPoolExecutor(max_workers=workers)
        try:
            for c in range(z.cluster_count):
                start, end = z.cluster_offsets[c], z.cluster_end(c)
                raw = z.src.read(start, end - start)
                info = raw[0]
                comp = info & 0x0F
                if comp in (NONE, NONE2):
                    # pictures, video, search indexes: byte for byte
                    mapping.append((len(new_offsets), None))
                    new_offsets.append(out.write(raw))
                else:
                    data = unpack(info, raw[1:])
                    table = blob_table(info, data)
                    groups = _split(table, target)
                    pieces = [_piece(data, table, a, b) for a, b in groups]
                    mapping.append((len(new_offsets), [g[0] for g in groups]))
                    for comp_bytes in pool.map(_compress_one, [(p, level) for p in pieces]):
                        new_offsets.append(out.write(bytes([XZ if level == "xz" else ZSTD]) + comp_bytes))
                done_in += end - start
                if pct and (c % 16 == 0 or c + 1 == z.cluster_count):
                    rate = done_in / max(0.1, time.time() - t0)
                    left = (total_in - done_in) / max(1.0, rate)
                    pct(round(done_in * 100.0 / max(1, total_in), 1),
                        "%s / %s, about %s left" % (nc.human(done_in), nc.human(total_in), _mins(left)))
        finally:
            pool.shutdown()

        # directory: every entry as it was, pointing at its new cluster and blob
        url_ptrs = []
        for i in range(z.entry_count):
            old = struct.unpack("<Q", z.src.read(z.url_ptr + 8 * i, 8))[0]
            d = bytearray(z.dirent_at(old))
            mime = struct.unpack_from("<H", d)[0]
            if mime != 0xFFFF:
                oc, ob = struct.unpack_from("<II", d, 8)
                struct.pack_into("<II", d, 8, *remap(mapping, oc, ob))
            url_ptrs.append(out.write(bytes(d)))
        url_pos = out.write(struct.pack("<%dQ" % len(url_ptrs), *url_ptrs))
        title_pos = NO_TITLES
        if z.title_ptr != NO_TITLES and z.title_ptr < z.src.size:
            # an older archive's title list (entry numbers: they don't change)
            title_pos = out.write(z.src.read(z.title_ptr, 4 * z.entry_count))
        cluster_pos = out.write(struct.pack("<%dQ" % len(new_offsets), *new_offsets))
        checksum_pos = out.pos

        # header, then the checksum over everything before it
        h = bytearray(z.header)
        struct.pack_into("<II", h, 24, z.entry_count, len(new_offsets))
        struct.pack_into("<QQQQ", h, 32, url_pos, title_pos, cluster_pos, mime_pos)
        struct.pack_into("<IIQ", h, 64, z.main_page, z.layout_page, checksum_pos)
        out.pwrite(0, bytes(h))
        progress("  checking what was written")
        md5 = hashlib.md5()
        at = 0
        while at < checksum_pos:
            n = min(8 << 20, checksum_pos - at)
            md5.update(out.read(at, n))
            at += n
        out.pwrite(checksum_pos, md5.digest())
        out.close()
        size = checksum_pos + 16
        return {"paths": out.paths, "size": size, "clusters": (z.cluster_count, len(new_offsets)),
                "mapping": mapping}
    except BaseException:
        out.close()
        for p in out.paths:
            try:
                p.unlink()
            except OSError:
                pass
        raise
    finally:
        z.src.close()


def remap(mapping, c: int, b: int) -> tuple[int, int]:
    """Where blob b of old cluster c went."""
    first_new, starts = mapping[c]
    if starts is None:
        return first_new, b
    k = 0
    lo, hi = 0, len(starts) - 1
    while lo <= hi:                      # the last start <= b
        mid = (lo + hi) // 2
        if starts[mid] <= b:
            k, lo = mid, mid + 1
        else:
            hi = mid - 1
    return first_new + k, b - starts[k]


def _mins(s: float) -> str:
    return "%d min" % max(1, round(s / 60)) if s >= 90 else "%d s" % max(1, round(s))


# checking

def verify(old_paths: list[Path], new_paths: list[Path], samples: int = 400, progress=print) -> None:
    """The new archive opens, its checksum is right, and entries read back the same as
    in the old one (every entry for small archives, `samples` spread over big ones)."""
    a, b = open_zim(old_paths), open_zim(new_paths)
    try:
        if (a.entry_count, a.main_page, a.mime_bytes) != (b.entry_count, b.main_page, b.mime_bytes):
            raise ValueError("the new copy doesn't match the original")
        step = max(1, a.entry_count // samples) if a.entry_count > 20000 else 1
        cache_a: dict[int, tuple[int, bytes]] = {}
        cache_b: dict[int, tuple[int, bytes]] = {}

        def content(z: Zim, cache, c: int, blob: int) -> bytes:
            if c not in cache:
                if len(cache) > 8:
                    cache.clear()
                s, e = z.cluster_offsets[c], z.cluster_end(c)
                raw = z.src.read(s, e - s)
                cache[c] = (raw[0], unpack(raw[0], raw[1:]))
            info, data = cache[c]
            t = blob_table(info, data)
            return data[t[blob]:t[blob + 1]]

        for i in range(0, a.entry_count, step):
            da = a.dirent_at(struct.unpack("<Q", a.src.read(a.url_ptr + 8 * i, 8))[0])
            db = b.dirent_at(struct.unpack("<Q", b.src.read(b.url_ptr + 8 * i, 8))[0])
            if da[:8] != db[:8] or da[16 if da[0:2] != b"\xff\xff" else 12:] != db[16 if db[0:2] != b"\xff\xff" else 12:]:
                raise ValueError("the new copy doesn't match the original (item %d)" % i)
            if struct.unpack_from("<H", da)[0] == 0xFFFF:
                if da[8:12] != db[8:12]:
                    raise ValueError("the new copy doesn't match the original (link %d)" % i)
                continue
            ca = content(a, cache_a, *struct.unpack_from("<II", da, 8))
            cb = content(b, cache_b, *struct.unpack_from("<II", db, 8))
            if ca != cb:
                raise ValueError("the new copy doesn't read back the same (item %d)" % i)
        # the checksum libzim and Kiwix check
        md5 = hashlib.md5()
        at, end = 0, b.checksum_pos
        while at < end:
            n = min(8 << 20, end - at)
            md5.update(b.src.read(at, n) if n <= Source.BLOCK else b.src._raw(at, n))
            at += n
        if md5.digest() != b.src.read(end, 16):
            raise ValueError("the new copy failed its checksum")
    finally:
        a.src.close()
        b.src.close()


# the search index

SKIP_STEP = 512          # zim_indexer.js's


def update_index(card: Path, changed: dict | None = None, progress=print, words: bool = True) -> bool:
    """Point Nomad's search index (Archive/.nomad-zim, built by zim_indexer.js) at the
    rewritten archives' new clusters, without rebuilding it: a full rebuild re-reads every
    archive on the card (hours on a big card), and only cluster and blob numbers moved.

    changed: {"/Archive/<old first part>": (mapping, [(new "/Archive/..." path, size), ...])}
    The work is journaled in remap-pending.json beside the index, step by step, so a run
    that stops part way (or words=False: just what Nomad needs to open pages, the word
    shards left for later) is finished by the next one and nothing is moved twice.
    False when the index can't be updated this way (none yet, another format, an archive it
    doesn't list): build it from scratch then."""
    import json
    d = card / "Archive" / ".nomad-zim"
    mpath, jpath = d / "manifest.json", d / "remap-pending.json"
    try:
        man = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if man.get("version") != 2 or not man.get("sharded"):
        return False

    def save(obj, path):
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(json.dumps(obj, indent=1 if path == jpath else 2), encoding="utf-8")
        os.replace(tmp, path)

    job = None
    if jpath.is_file():
        job = json.loads(jpath.read_text(encoding="utf-8"))
        if changed:
            # an earlier update still open: finish it first
            if not update_index(card, None, progress, True):
                return False
            job = None
    if job is None:
        if not changed:
            return True
        arcs = {}
        for a in man.get("archives", []):
            first = a["parts"][0]["path"] if a.get("parts") else None
            if first in changed:
                mapping, parts = changed[first]
                arcs[str(a["idx"])] = {"mapping": [[f, st] for f, st in mapping], "parts": parts}
        if len(arcs) != len(changed):
            return False
        job = {"version": 1, "archives": arcs, "done": {"lists": False, "manifest": False, "shards": []}}
        save(job, jpath)
    maps = {int(k): [(f, st) for f, st in v["mapping"]] for k, v in job["archives"].items()}
    pct = getattr(progress, "pct", None)

    def fix(c: str, b: str, mapping) -> tuple[str, str]:
        nc_, nb = remap(mapping, int(c), int(b))
        return str(nc_), str(nb)

    # 1. per-archive browse lists: title \t cluster \t blob \t ns/url
    if not job["done"]["lists"]:
        for idx, mapping in maps.items():
            lp = d / ("%d.list" % idx)
            if lp.is_file():
                out = []
                for line in lp.read_text(encoding="utf-8").split("\n"):
                    f = line.split("\t")
                    if len(f) >= 3 and f[1].lstrip("-").isdigit():
                        f[1], f[2] = fix(f[1], f[2], mapping)
                    out.append("\t".join(f))
                tmp = lp.with_name(lp.name + ".new")
                tmp.write_text("\n".join(out), encoding="utf-8")
                os.replace(tmp, lp)
        job["done"]["lists"] = True
        save(job, jpath)

    # 2. the manifest: main pages, sizes, part names
    if not job["done"]["manifest"]:
        for a in man.get("archives", []):
            k = str(a["idx"])
            if k not in job["archives"]:
                continue
            if a.get("mainCluster") is not None and a["mainCluster"] >= 0:
                a["mainCluster"], a["mainBlob"] = remap(maps[a["idx"]], a["mainCluster"], a["mainBlob"])
            parts = job["archives"][k]["parts"]
            a["parts"] = [{"path": p, "size": sz} for p, sz in parts]
            a["split"] = len(parts) > 1
            a["totalSize"] = sum(sz for _, sz in parts)
        save(man, mpath)
        job["done"]["manifest"] = True
        save(job, jpath)
    if not words:
        zim_engine._sync()
        return True

    # 3. word shards: word \t title \t idx \t cluster \t blob, and their skip tables
    shards = man.get("shards", {})
    total = sum(v.get("bytes", 0) for k, v in shards.items() if k not in job["done"]["shards"]) or 1
    done = 0
    for ch, info in sorted(shards.items()):
        if ch in job["done"]["shards"]:
            continue
        dat, skp = d / info["dat"], d / info["skp"]
        tmp = dat.with_name(dat.name + ".new")
        keys, at, n, touched = [], 0, 0, False
        with open(dat, "rb") as fin, open(tmp, "wb") as fout:
            for raw in fin:
                line = raw.rstrip(b"\n")
                if not line:
                    continue
                f = line.split(b"\t")
                if len(f) >= 5 and f[2].isdigit() and int(f[2]) in maps:
                    c, b = fix(f[3].decode(), f[4].decode(), maps[int(f[2])])
                    f[3], f[4] = c.encode(), b.encode()
                    line = b"\t".join(f)
                    touched = True
                if n % SKIP_STEP == 0:
                    keys.append([f[0].decode("utf-8"), at])
                fout.write(line + b"\n")
                at += len(line) + 1
                n += 1
                done += len(raw)
                if pct and n % 200000 == 0:
                    pct(round(done * 100.0 / total, 1), "search index, part %s" % ch)
        if touched:
            os.replace(tmp, dat)
            skp_tmp = skp.with_name(skp.name + ".new")
            skp_tmp.write_text(json.dumps({"step": SKIP_STEP, "keys": keys}, separators=(",", ":")), encoding="utf-8")
            os.replace(skp_tmp, skp)
            info["bytes"] = at
            save(man, mpath)
        else:
            tmp.unlink()
        job["done"]["shards"].append(ch)
        save(job, jpath)
    jpath.unlink()
    zim_engine._sync()
    return True


# the card

def archives_on(card: Path) -> list[tuple[str, list[Path]]]:
    """(name.zim, [parts]) for every archive in <card>/Archive."""
    adir = card / "Archive"
    if not adir.is_dir():
        return []
    whole, split = {}, {}
    for p in sorted(adir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        low = p.name.lower()
        if low.endswith(".zim"):
            whole[p.name] = [p]
        elif len(low) > 6 and low[-6:-2] == ".zim" and low[-2:].isalpha():
            split.setdefault(p.name[:-2], []).append(p)
    out = [(n, ps) for n, ps in whole.items()]
    out += [(n, sorted(ps)) for n, ps in split.items() if n not in whole]
    return sorted(out, key=lambda x: x[0].lower())


def fat32(path: Path) -> bool:
    """True when the card can't hold a file over 4 GB."""
    try:
        if sys.platform.startswith("linux"):
            best, fs = "", ""
            target = str(path.resolve())
            with open("/proc/mounts") as m:
                for line in m:
                    dev, mnt, typ = line.split()[:3]
                    mnt = mnt.replace("\\040", " ")
                    if target.startswith(mnt) and len(mnt) > len(best):
                        best, fs = mnt, typ
            return fs in ("vfat", "msdos", "fat")
        if sys.platform == "win32":
            import ctypes
            buf = ctypes.create_unicode_buffer(32)
            root = os.path.splitdrive(str(path.resolve()))[0] + "\\"
            ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 32)
            return buf.value.upper().startswith("FAT")
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["diskutil", "info", str(path)], capture_output=True, text=True).stdout
            return "FAT32" in out or "MS-DOS" in out
    except Exception:
        pass
    return True          # can't tell: split, which works everywhere


class Skipped(Exception):
    """Not worth rewriting (it would grow too much)."""


def prepare_in_place(name: str, parts: list[Path], progress=print, level: int = LEVEL, force: bool = False) -> dict:
    """Rewrite one archive on the card, then swap it in. The original stays until the
    new one has been checked."""
    adir = parts[0].parent
    size = sum(p.stat().st_size for p in parts)
    free = shutil.disk_usage(adir).free
    if free < size * 1.15 + (64 << 20):
        raise RuntimeError("needs about %s free on the card to do this (it has %s). Speed it up on your "
                           "computer first (zim_prepare.py file.zim -O folder), then add it with Add ZIM archives."
                           % (nc.human(int(size * 1.15)), nc.human(free)))
    tmp = adir / (".preparing-" + name)
    big = size * 1.15 > FAT32_MAX
    split = big and fat32(adir)
    target, growth = choose_size(parts, level)
    if growth > SKIP and not force:
        raise Skipped("would grow about %+.0f%% (its pages repeat each other a lot, so it doesn't "
                      "pack down well). Nomad still shows it, just slower when several phones "
                      "use it at once" % (100 * growth))
    progress("  should end up about %+.0f%% in size" % (100 * growth))
    res = rewrite(parts, tmp, split, progress, level, target)
    progress("  checking it against the original")
    verify(parts, res["paths"], progress=progress)
    # swap: the original goes, the new one takes its name (and part names)
    for p in parts:
        p.unlink()
    final = []
    for p in res["paths"]:
        dst = adir / (name + p.name[len(tmp.name):])
        os.replace(p, dst)
        final.append(dst)
    zim_engine._sync()
    res["paths"] = final
    res["before"] = size
    return res


# menu

def run(dest: str | None = None, names: list[str] | None = None, everything: bool = False,
        assume_yes: bool = False, level: int = LEVEL, force: bool = False) -> int:
    nc.banner("Speed up archives")
    print("Reworks archives so Nomad loads them faster for everyone at once. Pages, pictures")
    print("and search stay exactly the same, they just take a little more space. Kiwix still")
    print("opens them. The search index gets updated at the end.")
    print("Use a card reader for this. With Nomad plugged in as a USB drive it only goes about 1 MB/s.\n")
    card = nc.choose_card(dest, prompt="Which card?")
    if not card:
        print("Cancelled.")
        return 1
    if (Path(card) / "Archive" / ".nomad-zim" / "remap-pending.json").is_file():
        print("Finishing a search index update that got stopped last time...")
        progress0 = nc.Progress()
        update_index(Path(card), None, progress0)
        progress0.done()
    found = archives_on(Path(card))
    if not found:
        print("No archives in %s/Archive." % card)
        return 1
    rows = []
    for name, parts in found:
        try:
            st = status(parts)
        except Exception as e:
            st = "unreadable (%s)" % e
        rows.append((name, parts, st, sum(p.stat().st_size for p in parts)))
    label = {"prepared": nc.c("32", "done"), "ready": nc.c("33", "can speed up"),
             "old": nc.c("33", "old format, can speed up")}
    for i, (name, parts, st, size) in enumerate(rows, 1):
        print("  %3d. %-52s %10s  %s" % (i, name[:52], nc.human(size), label.get(st, nc.c("31", st))))
    todo = [r for r in rows if r[2] in ("ready", "old")]
    if names:
        todo = [r for r in todo if r[0] in names or r[0][:-4] in names]
    elif not everything:
        if not todo:
            print("\nEvery archive here is already sped up.")
            return 0
        raw = input("\nWhich ones? (numbers like 1,4,7, 'all' for everything not done yet, or Enter to cancel): ").strip().lower()
        if not raw:
            print("Cancelled.")
            return 1
        if raw != "all":
            pick = set()
            for part in raw.replace(" ", "").split(","):
                if "-" in part:
                    a, b = part.split("-", 1)
                    pick.update(range(int(a), int(b) + 1))
                elif part.isdigit():
                    pick.add(int(part))
            todo = [rows[i - 1] for i in sorted(pick) if 1 <= i <= len(rows) and rows[i - 1][2] in ("ready", "old")]
    if not todo:
        print("Nothing to do.")
        return 0
    total = sum(r[3] for r in todo)
    print("\n%d archive(s), %s. Big ones take a while (all of Wikipedia takes hours)." % (len(todo), nc.human(total)))
    if not assume_yes and input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1
    progress = nc.Progress()
    failures = []
    changed = skipped = 0
    moved = {}
    for i, (name, parts, st, size) in enumerate(todo, 1):
        print(nc.c("1;36", "\n[%d/%d] %s" % (i, len(todo), name)))
        try:
            res = prepare_in_place(name, parts, progress, level, force)
            progress.done()
            moved["/Archive/" + parts[0].name] = (res["mapping"], [("/Archive/" + p.name, p.stat().st_size) for p in res["paths"]])
            delta = res["size"] - res["before"]
            print(nc.c("1;32", "  ✓ %s -> %s (%+.1f%%)" % (
                nc.human(res["before"]), nc.human(res["size"]), 100.0 * delta / max(1, res["before"]))))
            changed += 1
        except Skipped as e:
            progress.done()
            print(nc.c("33", "  left as it is: %s" % e))
            skipped += 1
        except KeyboardInterrupt:
            progress.done()
            print(nc.c("1;31", "\n  Stopped. The archive is as it was. A .preparing- file may be left in the Archive folder, it's safe to delete."))
            break
        except Exception as e:
            progress.done()
            print(nc.c("1;31", "  ✗ %s" % e))
            failures.append((name, str(e)))
    if changed:
        print(nc.c("1", "\nUpdating the search index..."))
        try:
            ok = update_index(Path(card), moved, progress)
        except Exception as e:
            progress.done()
            print(nc.c("33", "  couldn't update it (%s)" % e))
            ok = False
        progress.done()
        if not ok:
            print("  building it from scratch instead (this reads every archive on the card, so it can take a while)")
            zim_engine.index_archives(card, progress)
            progress.done()
    print(nc.c("1", "\nDone: %d sped up%s%s." % (changed, ", %d left as they are" % skipped if skipped else "",
                                                ", %d failed" % len(failures) if failures else "")))
    if changed:
        print(nc.c("2", "  Eject the card safely, then put it back in Nomad."))
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Speed up ZIM archives so they load faster on Nomad. Same content, a little more space.")
    ap.add_argument("source", nargs="?", help="a .zim on your computer (use with -O). Leave it out to work on a card")
    ap.add_argument("-O", "--out-dir", help="where to put the sped up copy of `source`")
    ap.add_argument("-o", "--output", help="where the card is mounted (skips the card picker)")
    ap.add_argument("--all", action="store_true", help="every archive on the card that isn't sped up yet")
    ap.add_argument("--name", action="append", help="just this archive on the card (you can use it more than once)")
    ap.add_argument("--level", type=int, default=LEVEL, help="compression level (default %d)" % LEVEL)
    ap.add_argument("--force", action="store_true", help="do it even for archives that would grow more than %d%%%%" % int(SKIP * 100))
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before starting")
    ap.add_argument("--test-xz", action="store_true", help=argparse.SUPPRESS)   # pre-2021-style test archive
    args = ap.parse_args()
    if args.source:
        src = Path(args.source).expanduser()
        out_dir = Path(args.out_dir or src.parent / "prepared")
        out_dir.mkdir(parents=True, exist_ok=True)
        progress = nc.Progress()
        st = status([src])
        if st == "prepared" and not args.yes:
            print("%s is already sped up." % src.name)
            return 0
        t0 = time.time()
        if args.test_xz:
            res = rewrite([src], out_dir / src.name, False, progress, "xz", 2 * 1024 * 1024)
            progress.done()
            verify([src], res["paths"], progress=progress)
            print("%s: xz test archive, %s" % (src.name, nc.human(res["size"])))
            return 0
        target, growth = choose_size([src], args.level)
        print("Should end up about %+.0f%% in size" % (100 * growth))
        if growth > SKIP and not args.force:
            print("That's a lot for this one (its pages repeat each other a lot). Left as it is. Use --force to do it anyway.")
            return 0
        res = rewrite([src], out_dir / src.name, False, progress, args.level, target)
        progress.done()
        verify([src], res["paths"], progress=progress)
        before = src.stat().st_size
        print("%s: %s -> %s (%+.1f%%), took %.0f s" % (
            src.name, nc.human(before), nc.human(res["size"]), 100.0 * (res["size"] - before) / before,
            time.time() - t0))
        return 0
    return run(dest=args.output, names=args.name, everything=args.all, assume_yes=args.yes, level=args.level,
               force=args.force)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
