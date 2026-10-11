"""Reading a Snapchat "My Data" export (zip files or extracted folders).

The export contains `json/memories_history.json` (or the older
`html/memories_history.html`) with one entry per memory:

    {"Date": "2023-07-15 18:42:11 UTC", "Media Type": "Video",
     "Location": "Latitude, Longitude: 51.50735, -0.12776",
     "Download Link": "https://app.snapchat.com/dmd/memories?...&mid=<id>&...",
     "Media Download Url": "https://..."}            # newer exports only

When "Export your Memories" was selected, the media itself is bundled in a
`memories/` folder, named `<date>_<id>-main.<ext>` with an optional
`<date>_<id>-overlay.png` holding the caption/sticker layer.
"""

import io
import json
import logging
import re
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .media import IMAGE_EXTS, VIDEO_EXTS, kind_from_ext, sniff

log = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<id>.+?)(?:[-_](?P<kind>main|overlay))?$", re.I)
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Memory:
    index: int
    date_utc: datetime = None  # timezone-aware, UTC
    media_type: str = None  # "image" / "video" as declared by Snapchat
    lat: float = None
    lon: float = None
    download_link: str = None
    media_url: str = None
    mid: str = None
    main: Path = None
    overlay: Path = None
    time_exact: bool = True
    source: str = "history"
    error: str = None
    kind: str = None  # "image" / "video", from the actual file content
    info: object = None  # media.VideoInfo for videos

    @property
    def label(self):
        when = self.date_utc.strftime("%Y-%m-%d %H:%M:%S UTC") if self.date_utc else "unknown date"
        return f"#{self.index} {self.media_type or 'media'} {when}"


# --------------------------------------------------------------------------
# Locating export files
# --------------------------------------------------------------------------


def discover_exports(folder="."):
    """Snapchat export zips/folders in `folder` (mydata~123.zip, mydata~123-2.zip, ...)."""
    found = []
    for p in sorted(Path(folder).iterdir()):
        if p.name.lower().startswith("mydata") and (p.suffix.lower() == ".zip" or p.is_dir()):
            found.append(p)
    return found


class _Source:
    """Uniform access to files inside a zip or a folder."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if path.is_file() else None

    def names(self):
        if self.zip:
            return [i.filename for i in self.zip.infolist() if not i.is_dir()]
        return [str(p.relative_to(self.path)).replace("\\", "/") for p in self.path.rglob("*") if p.is_file()]

    def open(self, name):
        if self.zip:
            return self.zip.open(name)
        return open(self.path / name, "rb")

    def size(self, name):
        if self.zip:
            return self.zip.getinfo(name).file_size
        return (self.path / name).stat().st_size

    def mtime(self, name):
        if self.zip:
            return datetime(*self.zip.getinfo(name).date_time)
        return datetime.fromtimestamp((self.path / name).stat().st_mtime)


# --------------------------------------------------------------------------
# Parsing the memories history
# --------------------------------------------------------------------------


def _field(entry, *names):
    lowered = {str(k).strip().lower(): v for k, v in entry.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def parse_date(text):
    if not text:
        return None
    text = str(text).strip()
    cleaned = re.sub(r"\s*(UTC|GMT|Z)$", "", text, flags=re.I)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_location(text):
    if not text:
        return None, None
    numbers = [float(x) for x in _FLOAT_RE.findall(str(text))]
    if len(numbers) < 2:
        return None, None
    lat, lon = numbers[-2], numbers[-1]
    if (abs(lat) < 1e-6 and abs(lon) < 1e-6) or abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def _media_type(text):
    text = str(text or "").lower()
    if "video" in text:
        return "video"
    if "image" in text or "photo" in text:
        return "image"
    return None


def _memory_id(*urls):
    for url in urls:
        if not url:
            continue
        mid = parse_qs(urlparse(url).query).get("mid")
        if mid:
            return mid[0].lower()
    for url in urls:
        m = _UUID_RE.search(url or "")
        if m:
            return m.group(0).lower()
    return None


def parse_history_json(data):
    if isinstance(data, dict):
        entries = data.get("Saved Media")
        if entries is None:
            entries = next(
                (v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)
                 and _field(v[0], "Date") is not None),
                [],
            )
    else:
        entries = data
    memories = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        link = _field(entry, "Download Link", "Download URL")
        media_url = _field(entry, "Media Download Url", "Media Download URL")
        lat, lon = parse_location(_field(entry, "Location"))
        memories.append(
            Memory(
                index=len(memories) + 1,
                date_utc=parse_date(_field(entry, "Date", "Created")),
                media_type=_media_type(_field(entry, "Media Type", "Type")),
                lat=lat,
                lon=lon,
                download_link=link,
                media_url=media_url,
                mid=_memory_id(link, media_url),
            )
        )
    return memories


def parse_history_html(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    memories = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date = parse_date(cells[0].get_text(strip=True))
        if date is None:
            continue
        link = None
        for tag in row.find_all(True):
            for attr in ("href", "onclick"):
                value = tag.get(attr) or ""
                m = re.search(r"downloadMemories\(\s*'([^']+)'", value) or re.match(r"https?://\S+", value)
                if m and link is None:
                    link = m.group(1) if m.groups() else m.group(0)
        location = next((c.get_text(" ", strip=True) for c in cells if "atitude" in c.get_text()), None)
        lat, lon = parse_location(location)
        memories.append(
            Memory(
                index=len(memories) + 1,
                date_utc=date,
                media_type=_media_type(cells[1].get_text(strip=True)),
                lat=lat,
                lon=lon,
                download_link=link,
                mid=_memory_id(link),
            )
        )
    return memories


# --------------------------------------------------------------------------
# Reading an export
# --------------------------------------------------------------------------


def _safe_name(name):
    return re.sub(r"[^\w.\-]+", "_", Path(name).name)[:180]


def _is_memory_media(name):
    parts = [p.lower() for p in name.replace("\\", "/").split("/")]
    if "chat_media" in parts or "__macosx" in parts:
        return False
    ext = Path(name).suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS | {".zip"}:
        return False
    return "memories" in parts[:-1] or bool(_NAME_RE.match(Path(name).stem))


def _extract_nested_zip(blob, stem, dest_dir):
    """A memory with an overlay is sometimes delivered as a zip of main+overlay."""
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        for info in members:
            data = z.read(info)
            kind = "overlay" if "overlay" in info.filename.lower() else "main"
            ext = sniff(data[:16]) or Path(info.filename).suffix.lower()
            target = dest_dir / f"{stem}-{kind}{ext}"
            if kind == "main" and target.exists():
                continue
            target.write_bytes(data)
            out.append(target)
    return out


def read_export(paths, extract_dir: Path):
    """Parse the memories history and extract bundled media.

    Returns the list of memories (with `.main`/`.overlay` set where the media
    is bundled in the export) and a list of orphan overlay files."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    sources = [_Source(Path(p)) for p in paths]
    memories = None
    bundled = []  # (extracted path, zip mtime)

    for src in sources:
        names = src.names()
        if memories is None:
            json_name = next((n for n in names if n.lower().endswith("memories_history.json")), None)
            html_name = next((n for n in names if n.lower().endswith("memories_history.html")), None)
            if json_name:
                with src.open(json_name) as f:
                    memories = parse_history_json(json.load(io.TextIOWrapper(f, encoding="utf-8-sig")))
                log.info("Read %d memories from %s", len(memories), json_name)
            elif html_name:
                with src.open(html_name) as f:
                    memories = parse_history_html(f.read().decode("utf-8", "replace"))
                log.info("Read %d memories from %s", len(memories), html_name)

        media_names = [n for n in names if _is_memory_media(n)]
        for i, name in enumerate(media_names, 1):
            target = extract_dir / _safe_name(name)
            if Path(name).suffix.lower() == ".zip":
                with src.open(name) as f:
                    for p in _extract_nested_zip(f.read(), _safe_name(Path(name).stem), extract_dir):
                        bundled.append((p, src.mtime(name)))
                continue
            if not (target.exists() and target.stat().st_size == src.size(name)):
                with src.open(name) as f, open(target, "wb") as out:
                    shutil.copyfileobj(f, out, 1 << 20)
            bundled.append((target, src.mtime(name)))
            if i % 500 == 0:
                log.info("  extracted %d/%d files from %s", i, len(media_names), src.path.name)
        if media_names:
            log.info("Found %d files in the memories folder of %s", len(media_names), src.path.name)

    memories = memories or []
    orphans = _attach_bundled(memories, bundled)
    return memories, orphans


def _attach_bundled(memories, bundled):
    groups = defaultdict(dict)  # (date, id) -> {"main": (path, mtime), "overlay": ...}
    for path, mtime in bundled:
        m = _NAME_RE.match(path.stem)
        if m:
            key = (m.group("date"), m.group("id").lower())
            kind = (m.group("kind") or "main").lower()
        else:
            key = ("", path.stem.lower())
            kind = "overlay" if "overlay" in path.stem.lower() else "main"
        groups[key][kind] = (path, mtime)

    by_id = {m.mid: m for m in memories if m.mid}
    unmatched_groups = []
    for key, files in groups.items():
        if "main" not in files:
            continue
        mem = by_id.get(key[1])
        if mem is None:
            uuid = _UUID_RE.search(key[1])
            mem = by_id.get(uuid.group(0).lower()) if uuid else None
        if mem is not None and mem.main is None:
            mem.main = files["main"][0]
            mem.overlay = files.get("overlay", (None,))[0]
        else:
            unmatched_groups.append((key, files))

    # Fallback: pair remaining files with remaining memories of the same day
    # and kind, in chronological order.
    if unmatched_groups:
        pending = defaultdict(list)
        for mem in memories:
            if mem.main is None and mem.date_utc is not None:
                pending[(mem.date_utc.strftime("%Y-%m-%d"), mem.media_type)].append(mem)
        leftovers = []
        files_by_day = defaultdict(list)
        for key, files in unmatched_groups:
            kind = kind_from_ext(files["main"][0])
            files_by_day[(key[0], kind)].append(files)
        for day_kind, file_groups in files_by_day.items():
            mems = sorted(pending.get(day_kind, []), key=lambda m: m.date_utc)
            file_groups.sort(key=lambda f: (f["main"][1], f["main"][0].name))
            for mem, files in zip(mems, file_groups):
                mem.main = files["main"][0]
                mem.overlay = files.get("overlay", (None,))[0]
                mem.time_exact = len(mems) == 1 and len(file_groups) == 1
            leftovers.extend(file_groups[len(mems):])

        # Files that match no history entry at all: keep them, dated from the
        # file name (day precision).
        for files in leftovers:
            path, mtime = files["main"]
            m = _NAME_RE.match(path.stem)
            date = parse_date(m.group("date")) if m else None
            if date is not None and mtime and mtime.date() == date.date():
                date = mtime.replace(tzinfo=timezone.utc)
            elif date is not None:
                date = date.replace(hour=12)
            memories.append(
                Memory(
                    index=len(memories) + 1,
                    date_utc=date,
                    media_type=kind_from_ext(path),
                    main=path,
                    overlay=files.get("overlay", (None,))[0],
                    time_exact=False,
                    source="bundled file without history entry",
                )
            )

    return [files["overlay"][0] for _, files in groups.items() if "main" not in files and "overlay" in files]
