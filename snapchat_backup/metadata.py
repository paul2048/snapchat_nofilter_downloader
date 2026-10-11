"""Writing the capture date (and location) into photos and videos.

Android galleries (Google Photos, Samsung Gallery, MediaStore) sort photos by
EXIF DateTimeOriginal and videos by the MP4 `creation_time`, and fall back to
the file's modification time. All three are set, so the result is right
whichever way the files reach the phone.
"""

import logging
import os
import shutil
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import piexif

from .media import MediaError, run

log = logging.getLogger(__name__)

_OFFSET_TIME, _OFFSET_TIME_ORIGINAL, _OFFSET_TIME_DIGITIZED = 36880, 36881, 36882


class TimezoneResolver:
    """Snapchat dates are UTC. Photos store local time, so we need the time
    zone the snap was taken in: from its location when known, otherwise the
    one given by the user, otherwise this computer's."""

    def __init__(self, default=None, use_location=True):
        self.default = None
        if default:
            try:
                self.default = ZoneInfo(default)
            except ZoneInfoNotFoundError:
                raise SystemExit(f"Unknown time zone {default!r}. Use a name like Europe/London.")
        self.finder = None
        if use_location:
            try:
                from timezonefinder import TimezoneFinder

                self.finder = TimezoneFinder()
            except Exception:
                log.info("timezonefinder not installed: using a single time zone for all memories.")

    def localize(self, dt_utc, lat=None, lon=None):
        """Return (local aware datetime, zone name)."""
        if self.finder is not None and lat is not None and lon is not None:
            try:
                name = self.finder.timezone_at(lat=lat, lng=lon)
            except Exception:
                name = None
            if name:
                try:
                    return dt_utc.astimezone(ZoneInfo(name)), name
                except ZoneInfoNotFoundError:
                    pass
        if self.default is not None:
            return dt_utc.astimezone(self.default), self.default.key
        local = dt_utc.astimezone()
        return local, local.tzname() or "local"


def _offset(dt):
    off = dt.utcoffset()
    total = int(off.total_seconds()) if off else 0
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _dms(value):
    value = abs(value)
    deg = int(value)
    minutes = int((value - deg) * 60)
    seconds = round(((value - deg) * 60 - minutes) * 60 * 10000)
    return ((deg, 1), (minutes, 1), (seconds, 10000))


def _gps_ifd(lat, lon, dt_utc):
    return {
        piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _dms(lon),
        piexif.GPSIFD.GPSDateStamp: dt_utc.strftime("%Y:%m:%d").encode(),
        piexif.GPSIFD.GPSTimeStamp: ((dt_utc.hour, 1), (dt_utc.minute, 1), (dt_utc.second, 1)),
    }


def _exif_bytes(existing, local_dt, lat, lon, gps):
    stamp = local_dt.strftime("%Y:%m:%d %H:%M:%S").encode()
    offset = _offset(local_dt).encode()
    exif = existing or {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif.setdefault("0th", {})[piexif.ImageIFD.DateTime] = stamp
    sub = exif.setdefault("Exif", {})
    sub[piexif.ExifIFD.DateTimeOriginal] = stamp
    sub[piexif.ExifIFD.DateTimeDigitized] = stamp
    sub[_OFFSET_TIME] = offset
    sub[_OFFSET_TIME_ORIGINAL] = offset
    sub[_OFFSET_TIME_DIGITIZED] = offset
    if gps and lat is not None and lon is not None:
        exif["GPS"] = _gps_ifd(lat, lon, local_dt.astimezone(timezone.utc))
    return piexif.dump(exif)


def write_photo_metadata(path, local_dt, lat=None, lon=None, gps=True):
    """Embed the date (and GPS) without re-encoding. Returns False for formats
    that cannot carry EXIF here (the file time is still set by the caller)."""
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        existing = None
        try:
            existing = piexif.load(str(path))
            data = _exif_bytes(existing, local_dt, lat, lon, gps)
        except Exception:
            # Unwritable existing EXIF: start fresh but keep the orientation,
            # otherwise the photo could show up rotated.
            orientation = (existing or {}).get("0th", {}).get(piexif.ImageIFD.Orientation)
            fresh = {"0th": {piexif.ImageIFD.Orientation: orientation} if orientation else {},
                     "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
            data = _exif_bytes(fresh, local_dt, lat, lon, gps)
        piexif.insert(data, str(path))
        return True
    if ext == ".png":
        from PIL import Image

        tmp = path.with_suffix(".tmp.png")
        with Image.open(path) as im:
            im.save(tmp, format="PNG", exif=_exif_bytes(None, local_dt, lat, lon, gps))
        tmp.replace(path)
        return True
    return False


def _iso6709(lat, lon):
    return f"{lat:+08.4f}{lon:+09.4f}/"


def write_video_metadata(src, dst, dt_utc, lat=None, lon=None, gps=True):
    """Copy src to dst (no re-encoding) with creation_time set."""
    stamp = dt_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    tags = ["-metadata", f"creation_time={stamp}", "-metadata:s", f"creation_time={stamp}"]
    if gps and lat is not None and lon is not None:
        tags += ["-metadata", f"location={_iso6709(lat, lon)}"]
    base = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src)]
    tail = ["-c", "copy", "-map_metadata", "0", *tags, "-movflags", "+faststart", str(dst)]
    try:
        run(base + ["-map", "0"] + tail)
    except MediaError:
        # Some files carry data streams the MP4 muxer refuses: keep audio/video only.
        run(base + ["-map", "0:v", "-map", "0:a?"] + tail)


def set_file_time(path, dt_utc):
    ts = dt_utc.timestamp()
    os.utime(path, (ts, ts))


def link_or_copy(src, dst):
    """Hard link when possible (no extra disk space), else copy with times."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
