"""The whole job: export -> downloads -> re-joined videos -> dates ->
duplicates -> phone."""

import functools
import hashlib
import logging
import shutil
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .adb import PHONE_ROOT, Adb
from .cache import FingerprintCache
from .download import Downloader
from .export import discover_exports, read_export
from .gallery import scan_gallery
from .matching import LOOSE, STRICT, Entry, duplicate_groups, find_matches
from .media import (
    edge_frames, image_size, kind_from_ext, photo_print, probe_video, require_ffmpeg,
    run, sha256_file, sniff, video_print,
)
from .metadata import (
    TimezoneResolver, link_or_copy, set_file_time, write_photo_metadata, write_video_metadata,
)
from .report import write_reports
from .segments import SegmentConfig, concat_videos, find_chains

log = logging.getLogger("snapchat_backup")

MARKER = ".snapchat_backup_output"
REVIEW_FOLDER = "Snapchat duplicates (review)"
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class Options:
    exports: list = None
    out: Path = Path("snapchat_backup_output")
    gallery: list = None
    use_adb: bool = False
    adb_serial: str = None
    adb_folders: tuple = ("DCIM", "Pictures", "Movies")
    adb_refresh: bool = False
    phone_folder: str = "DCIM/Snapchat Memories"
    push: bool = True
    clean_phone: bool = False
    assume_yes: bool = False
    timezone: str = None
    location_timezone: bool = True
    gps: bool = True
    overlays: str = "separate"  # separate | burn | drop
    segments: SegmentConfig = field(default_factory=SegmentConfig)
    workers: int = 4
    download_workers: int = 6


@dataclass
class Item:
    """One output file: a photo, a video, or several video parts joined."""

    memories: list
    kind: str
    source: Path = None  # what the output is made from (main, burned or joined file)
    duration: float = 0.0
    size: tuple = None
    local_dt: datetime = None
    tz_name: str = ""
    lat: float = None
    lon: float = None
    out: Path = None
    status: str = "new"
    same_as: str = ""
    same_as_path: Path = None
    detail: str = ""
    notes: list = field(default_factory=list)
    entry: Entry = None
    sha: str = None

    @property
    def first(self):
        return self.memories[0]

    @property
    def date_utc(self):
        return self.first.date_utc

    def sort_key(self):
        return (self.date_utc or _FAR_FUTURE, self.first.index)


@functools.lru_cache(maxsize=None)
def encoder_args():
    encoders = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    video = (["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
             if b"libx264" in encoders else ["-q:v", "2"])
    return video + ["-c:a", "aac", "-b:a", "192k"]


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Backup:
    def __init__(self, opts: Options):
        self.o = opts
        self.out = Path(opts.out)
        self.work = self.out / ".work"
        self.all_dir = self.out / "all_memories"
        self.phone_dir = self.out / "to_phone"
        self.extras = self.out / "extras"
        self.cache = None
        self.stats = Counter()

    # ------------------------------------------------------------------
    def run(self):
        require_ffmpeg()
        phone = None
        if self.o.use_adb:
            phone = Adb(self.o.adb_serial)
            log.info("Phone connected: %s", phone.check_device())
        exports = self._find_exports()
        self._claim_output()
        self.cache = FingerprintCache(self.work / "fingerprints.sqlite")
        try:
            memories, orphans = self._load(exports)
            self._reset_output()
            self._inspect(memories)
            items = self._group(memories)
            self._dates(items)
            if self.o.overlays == "burn":
                self._burn(items)
            self._name(items)
            self._write(items, orphans)
            self._fingerprint_items(items)
            self._dedupe_export(items)
            roots = self._gallery_roots(phone)
            cleanup = self._dedupe_gallery(items, roots) if roots else []
            self._fill_phone_folder(items)
            summary = self._summary(memories, items, cleanup, roots)
            self._report(memories, items, cleanup, summary)
            if phone:
                self._phone_actions(phone, cleanup)
                self._report(memories, items, cleanup, summary)
        finally:
            self.cache.close()
        log.info("")
        for line in summary:
            log.info("  %s", line)
        log.info("")
        log.info("Report: %s", (self.out / "report.html").resolve())
        return 0

    # ------------------------------------------------------------------
    def _claim_output(self):
        """Refuse to write into a folder that has files we did not create."""
        self.out.mkdir(parents=True, exist_ok=True)
        marker = self.out / MARKER
        others = [p for p in self.out.iterdir() if p.name not in (".work", MARKER)]
        if others and not marker.exists():
            raise SystemExit(f"{self.out} already contains other files. Choose an empty or new folder with --out.")
        marker.write_text("Created by snapchat_backup. The folders here are rebuilt on every run.\n")
        self.work.mkdir(exist_ok=True)

    def _reset_output(self):
        """Results are rebuilt on every run (downloads and fingerprints in .work are kept)."""
        for d in (self.all_dir, self.phone_dir, self.extras):
            shutil.rmtree(d, ignore_errors=True)
        for name in ("report.csv", "report.html", "phone_cleanup.csv"):
            (self.out / name).unlink(missing_ok=True)
        for d in (self.all_dir, self.phone_dir):
            d.mkdir(parents=True)

    def _map(self, fn, jobs, label=None):
        """Run fn over jobs in threads. Returns [(ok, result_or_exception)]."""
        results = [None] * len(jobs)
        if not jobs:
            return results
        step = max(1, len(jobs) // 10)
        with ThreadPoolExecutor(self.o.workers) as pool:
            futures = {pool.submit(fn, job): i for i, job in enumerate(jobs)}
            for n, fut in enumerate(as_completed(futures), 1):
                i = futures[fut]
                try:
                    results[i] = (True, fut.result())
                except Exception as e:
                    results[i] = (False, e)
                if label and (n % step == 0 or n == len(jobs)) and len(jobs) >= 20:
                    log.info("  %s: %d/%d", label, n, len(jobs))
        return results

    def _cached(self, kind, jobs, compute, label=None):
        """jobs: [(path, variant, args)] -> {(path, variant): result or Exception}.
        Results are cached on disk keyed by the file's path, size and mtime."""
        get, put = {
            "probe": (self.cache.get_info, self.cache.put_info),
            "photo": (self.cache.get_photo, self.cache.put_photo),
            "video": (self.cache.get_video, self.cache.put_video),
            "json": (self.cache.get_json, self.cache.put_json),
        }[kind if kind in ("probe", "photo", "video") else "json"]
        out, todo = {}, []
        for path, variant, args in jobs:
            key = FingerprintCache.key(kind, path, variant)
            hit = get(key)
            if hit is not None:
                out[(path, variant)] = hit
            else:
                todo.append((key, (path, variant), args))
        for (key, ident, _), (ok, value) in zip(todo, self._map(lambda t: compute(*t[2]), todo, label)):
            out[ident] = value
            if ok:
                put(key, value)
        self.cache.commit()
        return out

    # ------------------------------------------------------------------
    def _find_exports(self):
        exports = self.o.exports or discover_exports(Path.cwd())
        if not exports:
            raise SystemExit(
                "No Snapchat export found. Pass your zip file(s) with --export, e.g. "
                "--export ~/Downloads/mydata~1700000000.zip")
        for p in exports:
            if not Path(p).exists():
                raise SystemExit(f"Export not found: {p}")
        return exports

    def _load(self, exports):
        log.info("Reading export: %s", ", ".join(Path(p).name for p in exports))
        memories, orphans = read_export([Path(p) for p in exports], self.work / "export_media")
        if not memories:
            raise SystemExit("No memories found in the export (memories_history.json / .html is missing or empty).")
        self.stats["bundled"] = sum(1 for m in memories if m.main)
        Downloader(self.work / "downloads", workers=self.o.download_workers).fetch_all(memories)
        self.stats["downloaded"] = sum(1 for m in memories if m.main) - self.stats["bundled"]
        return memories, orphans

    def _inspect(self, memories):
        have = [m for m in memories if m.main and not m.error]
        kinds = {}
        for m in have:
            with open(m.main, "rb") as f:
                ext = sniff(f.read(64))
            kinds[id(m)] = kind_from_ext(ext or m.main.suffix)

        def check_image(path):
            image_size(path)
            return {"ok": True}

        videos = [m for m in have if kinds[id(m)] == "video"]
        images = [m for m in have if kinds[id(m)] == "image"]
        infos = self._cached("probe", [(m.main, "", (m.main,)) for m in videos], probe_video, "checking videos")
        checks = self._cached("image-ok", [(m.main, "", (m.main,)) for m in images], check_image, "checking photos")
        for m in have:
            kind = kinds[id(m)]
            if kind is None:
                m.error = "file is not a photo or video (the download probably failed)"
                continue
            result = infos.get((m.main, "")) if kind == "video" else checks.get((m.main, ""))
            if isinstance(result, Exception):
                m.error = f"unreadable {kind}: {result}"
                continue
            m.kind = kind
            m.info = result if kind == "video" else None

    def _edges(self, mem):
        key = FingerprintCache.key("edges", mem.main)
        hit = self.cache.get_json(key)
        if hit is None:
            (fh, fs), (lh, ls) = edge_frames(mem.main)
            hit = {"first": fh.tobytes().hex(), "fs": fs, "last": lh.tobytes().hex(), "ls": ls}
            self.cache.put_json(key, hit)
        first = np.frombuffer(bytes.fromhex(hit["first"]), dtype=np.uint8)
        last = np.frombuffer(bytes.fromhex(hit["last"]), dtype=np.uint8)
        return (first, hit["fs"]), (last, hit["ls"])

    def _group(self, memories):
        ok = [m for m in memories if m.kind and not m.error]
        items = [Item([m], "image") for m in ok if m.kind == "image"]
        dated = [m for m in ok if m.kind == "video" and m.date_utc]
        undated = [m for m in ok if m.kind == "video" and not m.date_utc]
        if self.o.segments.enabled and dated:
            log.info("Looking for long videos that were split into parts...")
        chains = find_chains(dated, self.o.segments, self._edges)
        self.cache.commit()
        items += [Item(chain, "video") for chain in chains]
        items += [Item([m], "video") for m in undated]
        joined = [c for c in chains if len(c) > 1]
        self.stats["joined"] = len(joined)
        self.stats["joined_parts"] = sum(len(c) for c in joined)
        if joined:
            log.info("Re-joining %d videos that Snapchat stored as %d parts", len(joined), self.stats["joined_parts"])
        for it in items:
            it.source = it.first.main
            if it.kind == "video":
                it.duration = sum(m.info.duration for m in it.memories)
                it.size = (it.first.info.width, it.first.info.height)
        return items

    def _dates(self, items):
        resolver = TimezoneResolver(self.o.timezone, self.o.location_timezone)
        for it in items:
            loc = next(((m.lat, m.lon) for m in it.memories if m.lat is not None), (None, None))
            it.lat, it.lon = loc
            if it.date_utc:
                it.local_dt, it.tz_name = resolver.localize(it.date_utc, *loc)
            else:
                it.notes.append("no date in the export")
            if not all(m.time_exact for m in it.memories):
                it.notes.append("time approximate: matched to the export by day")

    def _burn(self, items):
        burned_dir = self.work / "burned"
        burned_dir.mkdir(exist_ok=True)
        jobs = [(it, m) for it in items for m in it.memories if m.overlay]

        def burn(job):
            it, m = job
            target = burned_dir / f"{m.main.stem}-burned{'.mp4' if it.kind == 'video' else m.main.suffix}"
            if not target.exists() or target.stat().st_mtime < m.main.stat().st_mtime:
                tmp = target.with_name("tmp-" + target.name)
                if it.kind == "image":
                    from PIL import Image

                    with Image.open(m.main) as im, Image.open(m.overlay) as ov:
                        base = im.convert("RGBA")
                        merged = Image.alpha_composite(base, ov.convert("RGBA").resize(base.size))
                        fmt = "JPEG" if m.main.suffix.lower() in (".jpg", ".jpeg") else None
                        merged.convert("RGB").save(tmp, format=fmt, quality=95)
                else:
                    w, h = m.info.width, m.info.height
                    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(m.main), "-i", str(m.overlay),
                         "-filter_complex", f"[1:v]scale={w}:{h}[ov];[0:v][ov]overlay=format=auto[v]",
                         "-map", "[v]", "-map", "0:a?", *encoder_args(), str(tmp)])
                tmp.replace(target)
            m.burned = target

        for (it, m), (ok, err) in zip(jobs, self._map(burn, jobs, "burning overlays")):
            if not ok:
                it.notes.append(f"overlay could not be burned in: {err}")
        for it in items:
            it.source = getattr(it.first, "burned", None) or it.first.main

    def _name(self, items):
        used = set()
        for it in sorted(items, key=Item.sort_key):
            if it.kind == "video":
                ext = it.source.suffix.lower() if len(it.memories) == 1 else ".mp4"
                ext = ext if ext in (".mp4", ".mov", ".m4v") else ".mp4"
            else:
                ext = it.source.suffix.lower().replace(".jpeg", ".jpg")
            base = f"Snapchat_{it.local_dt:%Y%m%d_%H%M%S}" if it.local_dt else f"Snapchat_undated_{it.first.index:05d}"
            name, n = base + ext, 2
            while name.lower() in used:
                name, n = f"{base}_{n}{ext}", n + 1
            used.add(name.lower())
            it.out = self.all_dir / name

    # ------------------------------------------------------------------
    def _joined_source(self, it):
        parts = [getattr(m, "burned", None) or m.main for m in it.memories]
        ident = "|".join(f"{p.resolve()}:{p.stat().st_size}" for p in parts)
        target = self.work / "joined" / f"{hashlib.sha1(ident.encode()).hexdigest()[:16]}.mp4"
        if not target.exists():
            target.parent.mkdir(exist_ok=True)
            tmp = target.with_name("tmp-" + target.name)
            how = concat_videos(parts, tmp, it.duration, encoder_args())
            tmp.replace(target)
            if how != "copy":
                it.notes.append("parts had to be re-encoded to be joined")
        return target

    def _write(self, items, orphans):
        gps = self.o.gps

        def write(it):
            if it.kind == "image":
                shutil.copyfile(it.source, it.out)
                if it.local_dt and not write_photo_metadata(it.out, it.local_dt, it.lat, it.lon, gps):
                    it.notes.append("format cannot store a date: only the file time is set")
            else:
                if len(it.memories) > 1:
                    it.source = self._joined_source(it)
                if it.date_utc:
                    write_video_metadata(it.source, it.out, it.date_utc, it.lat, it.lon, gps)
                else:
                    shutil.copyfile(it.source, it.out)
            if it.date_utc:
                set_file_time(it.out, it.date_utc)
            self._write_extras(it)

        log.info("Writing %d files with their original dates...", len(items))
        for it, (ok, err) in zip(items, self._map(write, items, "writing")):
            if not ok:
                it.status = "failed"
                it.detail = str(err)
                if it.out and it.out.exists():
                    it.out.unlink()
        for path in orphans if self.o.overlays == "separate" else []:
            target = self.extras / "overlays" / "without_media" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            link_or_copy(path, target)

    def _write_extras(self, it):
        many = len(it.memories) > 1
        for i, m in enumerate(it.memories, 1):
            if m.overlay and self.o.overlays == "separate":
                suffix = f"_part{i}" if many else ""
                target = self.extras / "overlays" / f"{it.out.stem}_overlay{suffix}{m.overlay.suffix.lower()}"
                target.parent.mkdir(parents=True, exist_ok=True)
                link_or_copy(m.overlay, target)
            if many:
                target = self.extras / "video_parts" / it.out.stem / f"part{i:02d}{m.main.suffix.lower()}"
                target.parent.mkdir(parents=True, exist_ok=True)
                link_or_copy(m.main, target)

    # ------------------------------------------------------------------
    def _fingerprint_items(self, items):
        live = [it for it in items if it.status != "failed"]
        log.info("Fingerprinting %d memories for duplicate detection...", len(live))
        # A memory with a caption is also fingerprinted with the caption drawn
        # on it: that is how Snapchat's own camera-roll copy looks.
        with_overlay = self.o.overlays != "burn"
        keep_overlays = self.o.overlays == "separate"
        singles = [it for it in live if len(it.memories) == 1]
        shas = self._cached("sha", [(it.source, "", (it.source,)) for it in singles], sha256_file, "hashing")
        photo_jobs, video_jobs = [], []
        for it in live:
            ov = it.first.overlay if with_overlay and len(it.memories) == 1 else None
            jobs = photo_jobs if it.kind == "image" else video_jobs
            jobs.append((it.source, "", (it.source,)))
            if ov:
                variant = FingerprintCache.key("overlay", ov)
                jobs.append((it.source, variant,
                             (it.source, ov) if it.kind == "image" else (it.source, ov, it.size)))
        by_source = {}
        for prints in (self._cached("photo", photo_jobs, photo_print, "photos"),
                       self._cached("video", video_jobs, video_print, "videos")):
            for (src, _), value in prints.items():
                by_source.setdefault(src, []).append(value)
        overlay_sha = {}
        for it in live:
            variants = [v for v in by_source.get(it.source, []) if not isinstance(v, Exception)]
            if not variants:
                it.notes.append("could not be fingerprinted: not checked for duplicates")
            sha = shas.get((it.source, ""))
            it.sha = sha if isinstance(sha, str) else None
            sig = ()
            if keep_overlays:
                for m in it.memories:
                    if m.overlay and m.overlay not in overlay_sha:
                        overlay_sha[m.overlay] = sha256_file(m.overlay)
                sig = tuple(overlay_sha.get(m.overlay) for m in it.memories)
            it.entry = Entry(
                key=it.out.name, kind=it.kind, path=it.out, shas={it.sha} - {None}, duration=it.duration,
                photos=variants if it.kind == "image" else [],
                videos=variants if it.kind == "video" else [],
                extra={"item": it, "overlays": sig},
            )

    def _dedupe_export(self, items):
        entries = [it.entry for it in items if it.entry]
        groups, _ = duplicate_groups(
            entries, STRICT, accept=lambda m: m.query.extra["overlays"] == m.target.extra["overlays"])
        dup_dir = self.extras / "duplicates_in_export"
        for group in groups:
            members = sorted((e.extra["item"] for e in group), key=lambda it: (it.sort_key(), -len(it.memories)))
            keep = members[0]
            for it in members[1:]:
                dup_dir.mkdir(parents=True, exist_ok=True)
                target = dup_dir / it.out.name
                it.out.replace(target)
                it.out = target
                it.status = "export_duplicate"
                it.same_as, it.same_as_path = keep.out.name, keep.out
                self.stats["export_duplicates"] += 1
        if self.stats["export_duplicates"]:
            log.info("%d memories were saved more than once (each is kept once)", self.stats["export_duplicates"])

    # ------------------------------------------------------------------
    def _gallery_roots(self, phone):
        if phone:
            mirror = self.work / "phone_gallery"
            for folder in self.o.adb_folders:
                local = mirror / folder.strip("/")
                if local.exists() and not self.o.adb_refresh:
                    log.info("Using the copy of %s pulled earlier (--adb-refresh to pull again)", folder)
                    continue
                shutil.rmtree(local, ignore_errors=True)  # drop files since moved/deleted on the phone
                log.info("Copying %s from the phone to compare (this can take a while)...", folder)
                if not phone.pull_folder(folder, mirror):
                    log.info("  (no %s folder on the phone)", folder)
            return [mirror]
        return [Path(g) for g in self.o.gallery or []]

    def _recording_limit(self, items):
        """Longest duration a phone video can have and still be a copy of a
        memory (a whole recording, even if we failed to re-join its parts)."""
        clips = sorted((m for it in items if it.kind == "video" for m in it.memories if m.date_utc),
                       key=lambda m: m.date_utc)
        best = run_ = 0.0
        prev = None
        for m in clips:
            close = prev and (m.date_utc - prev.date_utc).total_seconds() <= prev.info.duration + self.o.segments.slack
            run_ = run_ + m.info.duration if close else m.info.duration
            best = max(best, run_)
            prev = m
        return max([best] + [it.duration for it in items if it.kind == "video"]) + 2.0

    def _dedupe_gallery(self, items, roots):
        files = scan_gallery(roots, exclude=[self.out])
        photos = [f for f in files if f.kind == "image"]
        videos = [f for f in files if f.kind == "video"]
        log.info("Phone gallery: %d photos, %d videos", len(photos), len(videos))
        if not files:
            log.warning("The gallery folder is empty: nothing to compare against.")
            return []

        limit = self._recording_limit(items)
        infos = self._cached("probe", [(f.path, "", (f.path,)) for f in videos], probe_video, "reading phone videos")
        usable = []
        for f in videos:
            info = infos.get((f.path, ""))
            if isinstance(info, Exception):
                continue
            f.info = info
            if 0.3 <= info.duration <= limit:
                usable.append(f)
        log.info("Comparing against %d photos and %d videos (videos longer than %.0fs cannot be Snapchat copies)",
                 len(photos), len(usable), limit)

        live_items = [it for it in items if it.entry]
        # Byte-identical copies are found by SHA-256, computed only for files
        # whose size matches another one. The written files count too: they
        # are what an earlier run copied to the phone.
        phone_sizes = Counter(f.size for f in files)
        sizes = phone_sizes + Counter(it.source.stat().st_size for it in live_items if it.sha)
        written = [it for it in live_items if it.out.exists() and phone_sizes[it.out.stat().st_size]]
        for it, (ok, sha) in zip(written, self._map(lambda it: sha256_file(it.out), written)):
            if ok:
                it.entry.shas.add(sha)
                sizes[it.out.stat().st_size] += 1
        need_sha = [f for f in photos + usable if sizes[f.size] > 1]
        shas = self._cached("sha", [(f.path, "", (f.path,)) for f in need_sha], sha256_file, "hashing phone files")
        pprints = self._cached("photo", [(f.path, "", (f.path,)) for f in photos], photo_print, "phone photos")
        vprints = self._cached("video", [(f.path, "", (f.path,)) for f in usable], video_print, "phone videos")

        entries = []
        for f in photos + usable:
            pr = (pprints if f.kind == "image" else vprints).get((f.path, ""))
            sha = shas.get((f.path, ""))
            if isinstance(pr, Exception) or pr is None:
                pr = None
            entries.append(Entry(
                key=f.rel, kind=f.kind, path=f.path, shas={sha} if isinstance(sha, str) else set(),
                duration=f.info.duration if f.kind == "video" else 0.0,
                photos=[pr] if pr is not None and f.kind == "image" else [],
                videos=[pr] if pr is not None and f.kind == "video" else [],
                extra={"file": f},
            ))

        # 1. Memories that are already on the phone.
        queries = [it.entry for it in live_items if it.status == "new"]
        partial = {}
        for m in sorted(find_matches(queries, entries, LOOSE), key=lambda m: (m.method != "identical", m.score)):
            it, f = m.query.extra["item"], m.target.extra["file"]
            if m.relation == "target_inside_query":
                partial.setdefault(f.rel, (f, it, m))
                continue
            if it.status == "new":
                it.status = "on_phone"
                it.same_as, it.same_as_path = f.rel, f.path
                it.detail = m.detail + (" (inside a longer video on the phone)" if m.relation == "query_inside_target" else "")
        self.stats["on_phone"] = sum(1 for it in items if it.status == "on_phone")

        # 2. Redundant copies on the phone itself.
        cleanup = []
        claimed = set()
        for rel, (f, it, m) in partial.items():
            new = it.status == "new"
            cleanup.append({"phone file": rel, "reason": "part of a longer video",
                            "keep instead": it.out.name if new else it.same_as, "detail": m.detail, "moved": "",
                            "_rel": rel, "_safe": True,
                            # only once the full video has actually reached the phone
                            "_needs": it.out.name if new else None})
            claimed.add(rel)

        def quality(e):
            f = e.extra["file"]
            if f.kind == "video":
                pixels = f.info.width * f.info.height
            else:
                pixels = e.photos[0].width * e.photos[0].height if e.photos else 0
            return (-pixels, -f.size, f.mtime, "camera" not in f.rel.lower(), f.rel)

        for group in duplicate_groups(entries, STRICT)[0]:
            ordered = [e for e in sorted(group, key=quality) if e.key not in claimed]
            if len(ordered) < 2:
                continue
            keep = ordered[0]
            for e in ordered[1:]:
                exact = bool(e.shas & keep.shas)
                cleanup.append({
                    "phone file": e.key,
                    "reason": "identical copy" if exact else f"same {'video' if e.kind == 'video' else 'photo'} (visually)",
                    "keep instead": keep.key, "detail": "byte-identical" if exact else "near-identical",
                    "moved": "", "_rel": e.key, "_safe": exact or e.kind == "video", "_needs": None,
                })
                claimed.add(e.key)
        self.stats["phone_redundant"] = len(cleanup)
        return cleanup

    def _fill_phone_folder(self, items):
        for it in items:
            if it.status == "new" and it.out and it.out.exists():
                link_or_copy(it.out, self.phone_dir / it.out.name)

    # ------------------------------------------------------------------
    def _confirm(self, question):
        if self.o.assume_yes:
            return True
        if not sys.stdin or not sys.stdin.isatty():
            log.info("%s -> skipped (run with --yes to allow without asking)", question)
            return False
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")

    def _phone_actions(self, phone, cleanup):
        new = sorted(p for p in self.phone_dir.iterdir() if p.is_file())
        remote_dir = f"{PHONE_ROOT}/{self.o.phone_folder.strip('/')}"
        existing = phone.list_files(remote_dir)
        if self.o.push and new:
            todo = [p for p in new if p.name not in existing]
            clash = [p for p in new if p.name in existing and existing[p.name] != p.stat().st_size]
            if clash:
                log.warning("%d files already exist on the phone under the same name and were not overwritten: %s",
                            len(clash), ", ".join(p.name for p in clash[:5]))
            size = sum(p.stat().st_size for p in todo)
            if todo and self._confirm(f"Copy {len(todo)} new files ({_human_size(size)}) to {self.o.phone_folder} on the phone?"):
                phone.push_files(todo, remote_dir)
                phone.media_scan(f"{remote_dir}/{p.name}" for p in todo)
                self.stats["pushed"] = len(todo)
                log.info("Copied %d files to the phone.", len(todo))
                existing.update((p.name, p.stat().st_size) for p in todo)
        if self.o.clean_phone:
            safe = [c for c in cleanup if c["_safe"] and (c["_needs"] is None or c["_needs"] in existing)]
            if len(safe) < sum(1 for c in cleanup if c["_safe"]):
                log.info("Some redundant copies are kept because the full video was not copied to the phone.")
            review = f"{PHONE_ROOT}/{REVIEW_FOLDER}"
            if safe and self._confirm(
                    f"Move {len(safe)} redundant copies on the phone into '{REVIEW_FOLDER}' "
                    "(hidden from the gallery, nothing is deleted)?"):
                phone.mkdir(review)
                phone.touch(f"{review}/.nomedia")
                moved = []
                for c in safe:
                    flat = c["_rel"].replace("/", "__")
                    phone.move(f"{PHONE_ROOT}/{c['_rel']}", f"{review}/{flat}")
                    c["moved"] = f"{REVIEW_FOLDER}/{flat}"
                    moved.append(f"{PHONE_ROOT}/{c['_rel']}")
                phone.media_scan(moved)
                self.stats["moved"] = len(moved)
                log.info("Moved %d redundant copies to %s on the phone.", len(moved), REVIEW_FOLDER)

    # ------------------------------------------------------------------
    def _summary(self, memories, items, cleanup, roots):
        failed = [m for m in memories if m.error]
        n_img = sum(1 for m in memories if m.kind == "image")
        n_vid = sum(1 for m in memories if m.kind == "video")
        new = sum(1 for it in items if it.status == "new")
        missing = len(memories) - n_img - n_vid
        lines = [
            f"Memories in the export: {len(memories)} ({n_img} photos, {n_vid} video files"
            + (f", {missing} unavailable)" if missing else ")"),
            f"Media bundled in the export: {self.stats['bundled']}, downloaded: {self.stats['downloaded']}",
        ]
        if self.stats["joined"]:
            lines.append(f"Long videos re-joined: {self.stats['joined']} (from {self.stats['joined_parts']} parts)")
        if self.stats["export_duplicates"]:
            lines.append(f"Saved twice in Snapchat (kept once): {self.stats['export_duplicates']}")
        lines.append(f"Saved with their original date in all_memories/: "
                     f"{sum(1 for it in items if it.status in ('new', 'on_phone'))}")
        if roots:
            lines.append(f"Already in the phone gallery (not copied again): {self.stats['on_phone']}")
            lines.append(f"New for the phone, in to_phone/: {new}")
            if cleanup:
                lines.append(f"Redundant copies already on the phone: {len(cleanup)} (see phone_cleanup.csv)")
        else:
            lines.append(f"No gallery given, so nothing was compared with the phone: all {new} files are in to_phone/")
        failed_items = sum(1 for it in items if it.status == "failed")
        if failed or failed_items:
            lines.append(f"Problems: {len(failed) + failed_items} (listed at the top of the report)")
        return lines

    def _report(self, memories, items, cleanup, summary):
        rows = []
        for m in memories:
            if m.error:
                rows.append({"file": "", "status": "failed", "_status": "failed",
                             "taken (local time)": m.date_utc.strftime("%Y-%m-%d %H:%M:%S UTC") if m.date_utc else "",
                             "type": m.media_type or "", "memory #": m.index, "notes": m.error})
        for it in sorted(items, key=Item.sort_key):
            rows.append({
                "file": it.out.name if it.out else "",
                "_path_file": it.out if it.out and it.out.exists() else None,
                "status": {"new": "new", "on_phone": "already on phone", "export_duplicate": "duplicate in export",
                           "failed": "failed"}[it.status],
                "_status": it.status,
                "taken (local time)": it.local_dt.strftime("%Y-%m-%d %H:%M:%S %z") if it.local_dt else "",
                "time zone": it.tz_name,
                "type": "photo" if it.kind == "image" else "video",
                "duration (s)": f"{it.duration:.1f}" if it.kind == "video" else "",
                "parts": len(it.memories) if len(it.memories) > 1 else "",
                "overlay": "yes" if any(m.overlay for m in it.memories) else "",
                "same as": it.same_as,
                "match detail": it.detail,
                "memory #": ", ".join(str(m.index) for m in it.memories),
                "notes": "; ".join(it.notes),
            })
        write_reports(self.out, rows, cleanup, summary)
