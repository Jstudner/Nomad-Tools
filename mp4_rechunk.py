#!/usr/bin/env python3
"""
mp4_rechunk: lay an MP4 out in one-second chunks so its index (moov) is smaller.

A browser plays nothing until it has the whole index, and Nomad sends ~0.4 MB/s to
one viewer, so a 4 MB index is 10 s of black screen. Encoders usually write every
frame as its own chunk, which costs two table rows a frame (chunk offset, chunk map)
in each track. One chunk a second per track holds the same samples in the same order
with a small fraction of that table. Nothing else in the index changes, and the
picture and sound are byte-for-byte the same. The result is an ordinary MP4 with the
index first.

Handles what ffmpeg writes: ftyp, moov, mdat; tracks with stsz/stz2, stsc, stco/co64.

    ./mp4_rechunk.py in.mp4 out.mp4
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

CHUNK_SECONDS = 1.0
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"udta"}


class Box:
    def __init__(self, kind: bytes, data: bytes = b"", children: list | None = None):
        self.kind, self.data, self.children = kind, data, children

    def encode(self) -> bytes:
        body = self.data if self.children is None else b"".join(c.encode() for c in self.children)
        return struct.pack(">I4s", 8 + len(body), self.kind) + body

    def find(self, kind: bytes) -> "Box | None":
        return next((c for c in self.children or [] if c.kind == kind), None)


def _parse(buf: bytes, pos: int, end: int) -> list[Box]:
    out = []
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", buf[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise ValueError(f"bad box {kind!r} at {pos}")
        if kind in CONTAINERS and kind != b"udta":
            out.append(Box(kind, children=_parse(buf, pos + hdr, pos + size)))
        else:
            out.append(Box(kind, buf[pos + hdr:pos + size]))
        pos += size
    return out


def _top_level(f) -> list[tuple[bytes, int, int, int]]:
    f.seek(0, 2)
    end, pos, out = f.tell(), 0, []
    while pos + 8 <= end:
        f.seek(pos)
        h = f.read(16)
        size, kind = struct.unpack(">I4s", h[:8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", h[8:16])[0], 16
        elif size == 0:
            size = end - pos
        out.append((kind, pos, size, hdr))
        pos += size
    return out


def _samples(stbl: Box) -> tuple[list[int], list[int]]:
    """(offsets, sizes) of every sample in the track, in decode order."""
    if stbl.find(b"stsz"):
        d = stbl.find(b"stsz").data
        fixed, n = struct.unpack(">II", d[4:12])
        sizes = [fixed] * n if fixed else list(struct.unpack(f">{n}I", d[12:12 + 4 * n]))
    else:
        d = stbl.find(b"stz2").data
        field, n = d[7], struct.unpack(">I", d[8:12])[0]
        if field == 16:
            sizes = list(struct.unpack(f">{n}H", d[12:12 + 2 * n]))
        elif field == 8:
            sizes = list(d[12:12 + n])
        else:
            sizes = [(d[12 + i // 2] >> (0 if i % 2 else 4)) & 15 for i in range(n)]
    if stbl.find(b"stco"):
        d = stbl.find(b"stco").data
        n = struct.unpack(">I", d[4:8])[0]
        chunks = list(struct.unpack(f">{n}I", d[8:8 + 4 * n]))
    else:
        d = stbl.find(b"co64").data
        n = struct.unpack(">I", d[4:8])[0]
        chunks = list(struct.unpack(f">{n}Q", d[8:8 + 8 * n]))
    d = stbl.find(b"stsc").data
    n = struct.unpack(">I", d[4:8])[0]
    runs = [struct.unpack(">III", d[8 + 12 * i:20 + 12 * i]) for i in range(n)]
    if any(desc != 1 for _, _, desc in runs):
        raise ValueError("more than one sample description")
    offsets, s = [], 0
    for r, (first, per, _) in enumerate(runs):
        last = runs[r + 1][0] - 1 if r + 1 < len(runs) else len(chunks)
        for c in range(first - 1, last):
            pos = chunks[c]
            for _ in range(per):
                if s >= len(sizes):
                    break
                offsets.append(pos)
                pos += sizes[s]
                s += 1
    if len(offsets) != len(sizes):
        raise ValueError("chunk table doesn't cover every sample")
    return offsets, sizes


def _times(stbl: Box) -> list[int]:
    """Decode time of every sample, in the track's timescale."""
    d = stbl.find(b"stts").data
    n = struct.unpack(">I", d[4:8])[0]
    out, t = [], 0
    for i in range(n):
        count, delta = struct.unpack(">II", d[8 + 8 * i:16 + 8 * i])
        for _ in range(count):
            out.append(t)
            t += delta
    return out


def rechunk(src: Path, dst: Path, progress=None) -> tuple[int, int]:
    """Write dst; returns (old index bytes, new index bytes)."""
    with open(src, "rb") as f:
        tops = _top_level(f)
        kinds = [k for k, *_ in tops]
        if b"moof" in kinds or kinds.count(b"moov") != 1:
            raise ValueError("fragmented or unusual MP4")
        _, mpos, msize, mhdr = next(t for t in tops if t[0] == b"moov")
        f.seek(mpos)
        moov = Box(b"moov", children=_parse(f.read(msize), mhdr, msize))
        ftyp = next((t for t in tops if t[0] == b"ftyp"), None)
        f.seek(ftyp[1]) if ftyp else None
        ftyp_bytes = f.read(ftyp[2]) if ftyp else b""

        tracks = []
        for trak in [c for c in moov.children if c.kind == b"trak"]:
            mdia = trak.find(b"mdia")
            scale = struct.unpack(">I", mdia.find(b"mdhd").data[12:16] if mdia.find(b"mdhd").data[0] == 0
                                  else mdia.find(b"mdhd").data[20:24])[0]
            stbl = mdia.find(b"minf").find(b"stbl")
            offsets, sizes = _samples(stbl)
            times = _times(stbl)
            if len(times) != len(sizes):
                raise ValueError("timing table doesn't match the sample count")
            tracks.append({"stbl": stbl, "scale": scale, "off": offsets, "size": sizes,
                           "sec": [t / scale for t in times], "chunks": [], "next": 0})

        # one chunk a second per track, tracks taking turns, so playback reads forward
        layout = []                       # (track, first sample, count)
        window = CHUNK_SECONDS
        while any(t["next"] < len(t["size"]) for t in tracks):
            for ti, t in enumerate(tracks):
                i = t["next"]
                j = i
                while j < len(t["size"]) and t["sec"][j] < window:
                    j += 1
                if j > i:
                    layout.append((ti, i, j - i))
                    t["next"] = j
            window += CHUNK_SECONDS

        mdat_payload = sum(sum(t["size"]) for t in tracks)
        old_index = msize

        # new chunk tables; co64 only when the file needs it
        def tables(data_start: int, wide: bool) -> None:
            pos = data_start
            for t in tracks:
                t["chunks"] = []
            for ti, first, count in layout:
                t = tracks[ti]
                t["chunks"].append((pos, count))
                pos += sum(t["size"][first:first + count])
            for t in tracks:
                stbl = t["stbl"]
                runs = []
                for c, (_, count) in enumerate(t["chunks"], 1):
                    if not runs or runs[-1][1] != count:
                        runs.append((c, count))
                stsc = struct.pack(">4sI", b"\0\0\0\0", len(runs)) + b"".join(
                    struct.pack(">III", c, n, 1) for c, n in runs)
                offs = [p for p, _ in t["chunks"]]
                if wide:
                    co = Box(b"co64", struct.pack(">4sI", b"\0\0\0\0", len(offs)) + struct.pack(f">{len(offs)}Q", *offs))
                else:
                    co = Box(b"stco", struct.pack(">4sI", b"\0\0\0\0", len(offs)) + struct.pack(f">{len(offs)}I", *offs))
                stbl.children = [c for c in stbl.children if c.kind not in (b"stco", b"co64", b"stsc")]
                stbl.children += [Box(b"stsc", stsc), co]

        wide = len(ftyp_bytes) + old_index + mdat_payload + 16 > 0xFFFFFFFF
        tables(0, wide)
        mdat_hdr = 16 if mdat_payload + 8 > 0xFFFFFFFF else 8
        data_start = len(ftyp_bytes) + len(moov.encode()) + mdat_hdr
        tables(data_start, wide)          # same sizes, real offsets
        moov_bytes = moov.encode()
        assert len(ftyp_bytes) + len(moov_bytes) + mdat_hdr == data_start

        with open(dst, "wb") as o:
            o.write(ftyp_bytes)
            o.write(moov_bytes)
            if mdat_hdr == 16:
                o.write(struct.pack(">I4sQ", 1, b"mdat", mdat_payload + 16))
            else:
                o.write(struct.pack(">I4s", mdat_payload + 8, b"mdat"))
            done = 0
            for n, (ti, first, count) in enumerate(layout):
                t = tracks[ti]
                k = first
                while k < first + count:
                    # read runs of samples that sit next to each other in the source
                    start, length = t["off"][k], t["size"][k]
                    k += 1
                    while k < first + count and t["off"][k] == start + length:
                        length += t["size"][k]
                        k += 1
                    f.seek(start)
                    o.write(f.read(length))
                    done += length
                if progress and n % 50 == 0:
                    progress(done * 100.0 / mdat_payload)
        return old_index, len(moov_bytes)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    old, new = rechunk(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"index {old / 1048576:.2f} MB -> {new / 1048576:.2f} MB")
