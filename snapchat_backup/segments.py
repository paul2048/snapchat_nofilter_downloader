"""Re-joining long videos that Snapchat stored as consecutive short segments.

A recording longer than one snap is saved as several memories (typically
~10 s each). Two clips A -> B are considered parts of the same recording when
  * they were saved close together: B starts at most A's duration (+ slack)
    after A,
  * they share every encoding parameter (size, rotation, codecs, fps, audio),
  * A is a "full" segment (at least `min_segment` seconds, and not shorter
    than B), and
  * the last frame of A looks like the first frame of B (the camera kept
    rolling across the cut).
The visual check is what keeps two separate snaps of different scenes from
being glued together. Clips are joined losslessly with ffmpeg's concat
demuxer; if that fails they are re-encoded.
"""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .media import FLAT_STD, MediaError, hamming, run

log = logging.getLogger(__name__)


@dataclass
class SegmentConfig:
    enabled: bool = True
    min_segment: float = 4.0  # seconds; every segment but the last is at least this long
    slack: float = 3.0  # seconds of tolerance on the time between two segments
    max_edge_distance: int = 48  # of 256 bits, last frame of A vs first frame of B


def find_chains(clips, cfg: SegmentConfig, edge_frames):
    """Group clips into recordings.

    `clips` are objects with .date_utc, .info (VideoInfo) and .main.
    `edge_frames(clip)` returns ((first_hash, first_std), (last_hash, last_std)).
    Returns a list of chains (lists of clips, in playback order); clips that are
    not part of any recording come back as single-element chains.
    """
    clips = sorted(clips, key=lambda c: c.date_utc)
    if not cfg.enabled:
        return [[c] for c in clips]

    candidates = []
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            gap = (b.date_utc - a.date_utc).total_seconds()
            if gap > a.info.duration + cfg.slack:
                break
            for x, y in ((a, b), (b, a)):
                dt = (y.date_utc - x.date_utc).total_seconds()
                if not (-cfg.slack <= dt <= x.info.duration + cfg.slack):
                    continue
                if x.info.duration < cfg.min_segment or y.info.duration > x.info.duration + 1.0:
                    continue
                if x.info.signature() != y.info.signature():
                    continue
                candidates.append((x, y))

    edges = []
    for x, y in candidates:
        try:
            (_, _), (last_hash, last_std) = edge_frames(x)
            (first_hash, first_std), (_, _) = edge_frames(y)
        except MediaError as e:
            log.debug("edge frames failed: %s", e)
            continue
        if last_std < FLAT_STD or first_std < FLAT_STD:
            continue  # dark/flat frames prove nothing
        dist = int(hamming(last_hash[None], first_hash[None])[0, 0])
        if dist <= cfg.max_edge_distance:
            edges.append((dist, abs((y.date_utc - x.date_utc).total_seconds() - x.info.duration), x, y))

    # Greedy matching: best edges first, each clip has at most one successor
    # and one predecessor, no cycles.
    edges.sort(key=lambda e: (e[0], e[1]))
    nxt, prv = {}, {}
    for _, _, x, y in edges:
        if id(x) in nxt or id(y) in prv:
            continue
        head = x
        while id(head) in prv:
            head = prv[id(head)]
        if head is y:
            continue
        nxt[id(x)], prv[id(y)] = y, x

    chains = []
    for c in clips:
        if id(c) in prv:
            continue
        chain = [c]
        while id(chain[-1]) in nxt:
            chain.append(nxt[id(chain[-1])])
        chains.append(chain)
    chains.sort(key=lambda ch: ch[0].date_utc)
    return chains


def _concat_list(paths):
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="concat_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for p in paths:
            escaped = str(Path(p).resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    return name


def concat_videos(paths, dst, expected_duration, encoder_args):
    """Join clips into dst. Lossless when possible."""
    from .media import probe_video

    listing = _concat_list(paths)
    try:
        try:
            run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", listing,
                 "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(dst)])
            got = probe_video(dst).duration
            if abs(got - expected_duration) <= 0.25 * len(paths) + 0.5:
                return "copy"
            log.debug("concat copy gave %.2fs instead of %.2fs, re-encoding", got, expected_duration)
        except MediaError as e:
            log.debug("concat copy failed (%s), re-encoding", e)
        info = probe_video(paths[0])
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
        for p in paths:
            cmd += ["-i", str(p)]
        streams = "".join(f"[{i}:v:0]" + (f"[{i}:a:0]" if info.has_audio else "") for i in range(len(paths)))
        a = 1 if info.has_audio else 0
        graph = f"{streams}concat=n={len(paths)}:v=1:a={a}[v]" + ("[a]" if a else "")
        cmd += ["-filter_complex", graph, "-map", "[v]"] + (["-map", "[a]"] if a else [])
        cmd += encoder_args + ["-movflags", "+faststart", str(dst)]
        run(cmd)
        return "re-encoded"
    finally:
        os.unlink(listing)
