"""Re-joining long videos that Snapchat stored as consecutive short segments.

A recording longer than one snap is saved as several memories (typically
~10 s each). Two clips A -> B are considered parts of the same recording when
  * they were saved close together: B starts at most A's duration (+ slack)
    after A,
  * they share every encoding parameter (size, rotation, codecs, audio; not
    the average frame rate, which varies between segments),
  * A is a "full" segment (at least `min_segment` seconds, and not much
    shorter than B), and
  * the last frame of A looks like the first frame of B (the camera kept
    rolling across the cut): the picture jumps no more across the cut than
    it moves from one frame to the next at the two ends.
The visual check is what keeps two separate snaps of different scenes from
being glued together, and what puts parts saved with the same timestamp in
the right order (part 3 does not follow part 1 seamlessly). It is relative
to the motion because the cut of a shaky, dark or blurry recording can
change a lot of pixels; a fixed threshold either misses those or accepts
the wrong order of a steady one. Clips are joined losslessly with ffmpeg's
concat demuxer; if that fails they are re-encoded.
"""

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media import FLAT_STD, MediaError, frame_difference, run

log = logging.getLogger(__name__)


@dataclass
class SegmentConfig:
    enabled: bool = True
    min_segment: float = 4.0  # seconds; every segment but the last is at least this long
    slack: float = 3.0  # seconds of tolerance on the time between two segments
    # A segment is at most this much longer than the one before it: full
    # segments of one recording vary (9.0 s, 10.8 s, 9.0 s, ...).
    length_slack: float = 2.5
    # Last frame of A vs first frame of B (robust pixel difference, 0-255):
    still: float = 12.0  # at most this: the same picture, whatever the motion
    max_jump: float = 80.0  # more than this: a different picture, whatever the motion
    motion_ratio: float = 3.0  # in between: at most this many times the frame-to-frame motion


def candidate_pairs(clips, cfg: SegmentConfig):
    """(A, B) pairs whose timing, length and encoding allow B to continue A.
    Only these need their edge frames compared."""
    clips = sorted(clips, key=lambda c: c.date_utc)
    pairs = []
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            gap = (b.date_utc - a.date_utc).total_seconds()
            if gap > a.info.duration + cfg.slack:
                break
            for x, y in ((a, b), (b, a)):
                dt = (y.date_utc - x.date_utc).total_seconds()
                if not (-cfg.slack <= dt <= x.info.duration + cfg.slack):
                    continue
                if x.info.duration < cfg.min_segment or y.info.duration > x.info.duration + cfg.length_slack:
                    continue
                if x.info.signature() != y.info.signature():
                    continue
                pairs.append((x, y))
    return pairs


def find_chains(clips, cfg: SegmentConfig, edge_frames):
    """Group clips into recordings.

    `clips` are objects with .date_utc, .info (VideoInfo) and .main.
    `edge_frames(clip)` returns (first two frames, last two frames) as RGB
    thumbnails (see media.edge_frames).
    Returns a list of chains (lists of clips, in playback order); clips that are
    not part of any recording come back as single-element chains.
    """
    clips = sorted(clips, key=lambda c: c.date_utc)
    if not cfg.enabled:
        return [[c] for c in clips]

    edges = []
    for x, y in candidate_pairs(clips, cfg):
        try:
            _, (x_prev, x_last) = edge_frames(x)
            (y_first, y_next), _ = edge_frames(y)
        except MediaError as e:
            log.debug("edge frames failed: %s", e)
            continue
        if _flat(x_last) or _flat(y_first):
            continue  # dark/flat frames prove nothing
        jump = frame_difference(x_last, y_first)
        motion = max(frame_difference(x_prev, x_last), frame_difference(y_first, y_next), 2.0)
        if jump <= cfg.still or (jump <= cfg.max_jump and jump <= cfg.motion_ratio * motion):
            timing = abs((y.date_utc - x.date_utc).total_seconds() - x.info.duration)
            edges.append((jump / motion, jump, timing, x, y))

    # Greedy matching: smoothest cuts first, each clip has at most one
    # successor and one predecessor, no cycles.
    edges.sort(key=lambda e: e[:3])
    nxt, prv = {}, {}
    for *_, x, y in edges:
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


def _flat(frame):
    gray = np.asarray(frame, dtype=np.float32).mean(axis=-1)
    return float(gray.std()) < FLAT_STD


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
