"""Finding the same photo/video in two collections.

Exact copies are found by SHA-256. Re-encoded copies (e.g. the copy Snapchat
saved to the camera roll, which has the caption burned in) are found
visually:
  * photos: 256-bit dHash to find candidates, then a robust thumbnail
    comparison (75th percentile of pixel differences, so a caption covering
    part of the picture does not prevent a match while a different shot of
    the same scene does);
  * videos: sequences of frame hashes sampled at 2 fps, aligned against each
    other, so a clip is also found *inside* a longer video.
Frames/photos with almost no detail (black, plain colour) never match
visually.
"""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .media import PhotoPrint, VideoPrint, align, hamming, popcount, thumb_difference


@dataclass
class Entry:
    key: str
    kind: str  # "image" or "video"
    path: Path
    shas: set = field(default_factory=set)  # SHA-256 of every byte-exact form of the file
    duration: float = 0.0
    photos: list = field(default_factory=list)  # PhotoPrint variants
    videos: list = field(default_factory=list)  # VideoPrint variants
    extra: dict = field(default_factory=dict)


@dataclass
class Thresholds:
    photo_hash: int  # max dHash distance (of 256) to consider two photos
    photo_thumb: float  # max robust thumbnail difference (0-255)
    aspect_tol: float  # max relative aspect-ratio difference
    video_frame: int  # a frame pair "matches" under this distance
    video_mean: float  # max mean distance over the aligned frames
    video_good: float  # min fraction of aligned frames that match


# Calibrated on real photos/videos: re-encoded, resized and captioned copies
# score photo_hash <= 20 and photo_thumb <= 4 and video mean <= 13, while a
# second shot of the same scene framed 3% differently scores photo_thumb >= 7
# (mostly far more) and other videos score a mean > 100.
# Measured on a real export (635 memories, 2017-2020, mostly dark and shaky
# phone videos): copies re-encoded at 75% size and CRF 28 score a video mean
# of 0-53 (8 of 37 above LOOSE, so they are copied again), while the closest
# of 57,000 pairs of different memories scores 63. The margin is thin, so the
# thresholds stay on the safe side: a missed copy costs space, a false match
# would leave a memory off the phone.
#
# Near-identical files (same media saved twice, byte-different copies).
STRICT = Thresholds(photo_hash=16, photo_thumb=3, aspect_tol=0.01, video_frame=28, video_mean=14, video_good=0.9)
# Same media after re-encoding, resizing or with a caption burned in.
LOOSE = Thresholds(photo_hash=40, photo_thumb=6, aspect_tol=0.03, video_frame=56, video_mean=34, video_good=0.7)


@dataclass
class Match:
    query: Entry
    target: Entry
    method: str  # "identical", "visual"
    relation: str = "same"  # same | query_inside_target | target_inside_query
    score: float = 0.0
    detail: str = ""


def _pairs_by_sha(queries, targets, same_set):
    by_sha = {}
    for t in targets:
        for sha in t.shas:
            by_sha.setdefault(sha, []).append(t)
    out = {}
    for q in queries:
        for sha in q.shas:
            for t in by_sha.get(sha, []):
                if not (same_set and t is q):
                    out[(id(q), id(t))] = Match(q, t, "identical", "same", 0.0, "byte-identical")
    return list(out.values())


def match_photos(queries, targets, th: Thresholds, same_set=False):
    owners, prints = [], []
    for ti, t in enumerate(targets):
        for p in t.photos:
            if p.informative:
                owners.append(ti)
                prints.append(p)
    if not prints:
        return []
    table = np.stack([p.hash for p in prints])
    best = {}
    for q in queries:
        for qp in q.photos:
            if not qp.informative:
                continue
            dist = popcount(table ^ qp.hash).sum(axis=1, dtype=np.int32)
            for j in np.nonzero(dist <= th.photo_hash)[0]:
                t, tp = targets[owners[j]], prints[j]
                if same_set and t is q:
                    continue
                if abs(qp.aspect - tp.aspect) > th.aspect_tol * max(qp.aspect, tp.aspect):
                    continue
                diff = thumb_difference(qp, tp)
                if diff > th.photo_thumb:
                    continue
                score = float(dist[j]) / 256 + diff / 255
                k = (id(q), id(t))
                if k not in best or score < best[k].score:
                    best[k] = Match(q, t, "visual", "same", score, f"hash {int(dist[j])}/256, pixels {diff:.0f}")
    return list(best.values())


def _frame_table(targets):
    owners, hashes, informative = [], [], []
    for ti, t in enumerate(targets):
        for vi, v in enumerate(t.videos):
            owners.extend([(ti, vi)] * len(v))
            hashes.append(v.hashes)
            informative.append(v.informative)
    if not hashes:
        return None, None, None
    return owners, np.concatenate(hashes), np.concatenate(informative)


def match_videos(queries, targets, th: Thresholds, same_set=False, chunk=100_000):
    owners, frames, frame_ok = _frame_table(targets)
    if frames is None:
        return []
    best = {}
    for q in queries:
        for qv in q.videos:
            usable = np.nonzero(qv.informative)[0]
            if len(usable) == 0:
                continue
            anchors = usable[np.unique(np.linspace(0, len(usable) - 1, min(6, len(usable))).astype(int))]
            candidates = set()
            for start in range(0, len(frames), chunk):
                d = hamming(qv.hashes[anchors], frames[start:start + chunk])
                hit = (d <= th.video_frame).any(axis=0) & frame_ok[start:start + chunk]
                candidates.update(owners[start + j] for j in np.nonzero(hit)[0])
            for ti, vi in candidates:
                t = targets[ti]
                if same_set and t is q:
                    continue
                tv = t.videos[vi]
                short, long_ = (qv, tv) if len(qv) <= len(tv) else (tv, qv)
                al = align(short, long_, th.video_frame)
                if al is None or al.mean > th.video_mean or al.good < th.video_good:
                    continue
                relation = _relation(q.duration, t.duration)
                score = al.mean / 256
                k = (id(q), id(t))
                if k not in best or score < best[k].score:
                    best[k] = Match(
                        q, t, "visual", relation, score,
                        f"mean frame distance {al.mean:.0f}/256 over {al.informative} frames",
                    )
    return list(best.values())


def _relation(dq, dt):
    if abs(dq - dt) <= max(1.0, 0.08 * max(dq, dt)):
        return "same"
    return "query_inside_target" if dq < dt else "target_inside_query"


def find_matches(queries, targets, th: Thresholds, same_set=False):
    """All matches between queries and targets (photos with photos, videos with videos)."""
    matches = {}
    for m in _pairs_by_sha(queries, targets, same_set):
        matches[(id(m.query), id(m.target))] = m
    for kind, fn in (("image", match_photos), ("video", match_videos)):
        qs = [q for q in queries if q.kind == kind]
        ts = [t for t in targets if t.kind == kind]
        if qs and ts:
            for m in fn(qs, ts, th, same_set):
                matches.setdefault((id(m.query), id(m.target)), m)
    return list(matches.values())


def duplicate_groups(entries, th: Thresholds, accept=lambda m: True):
    """Union-find groups of entries that are copies of each other."""
    parent = {id(e): id(e) for e in entries}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reasons = {}
    for m in find_matches(entries, entries, th, same_set=True):
        if m.relation != "same" or not accept(m):
            continue
        a, b = root(id(m.query)), root(id(m.target))
        if a != b:
            parent[a] = b
        reasons[(id(m.query), id(m.target))] = m
    groups = {}
    for e in entries:
        groups.setdefault(root(id(e)), []).append(e)
    return [g for g in groups.values() if len(g) > 1], reasons


__all__ = [
    "Entry", "Thresholds", "STRICT", "LOOSE", "Match", "find_matches", "duplicate_groups",
    "PhotoPrint", "VideoPrint",
]
