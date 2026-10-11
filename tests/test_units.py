from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from snapchat_backup.export import parse_date, parse_history_html, parse_history_json, parse_location
from snapchat_backup.metadata import TimezoneResolver
from snapchat_backup.segments import SegmentConfig, find_chains


def test_parse_history_json():
    data = {"Saved Media": [{
        "Date": "2023-07-15 18:42:11 UTC",
        "Media Type": "Video",
        "Location": "Latitude, Longitude: 51.50735, -0.12776",
        "Download Link": "https://app.snapchat.com/dmd/memories?uid=a&sid=b&mid=ABC-123&ts=1&sig=c",
    }, {
        "Date": "2020-01-02 23:08:19 UTC",
        "Media Type": "Image",
        "Location": "Latitude, Longitude: 0.0, 0.0",
        "Download Link": "https://x/y?uid=1",
        "Media Download Url": "https://cdn/062c8942-3124-a480-71fc-3c4833e3e56a.jpg",
    }]}
    a, b = parse_history_json(data)
    assert a.date_utc == datetime(2023, 7, 15, 18, 42, 11, tzinfo=timezone.utc)
    assert (a.media_type, a.lat, a.lon, a.mid) == ("video", 51.50735, -0.12776, "abc-123")
    assert (b.media_type, b.lat, b.mid) == ("image", None, "062c8942-3124-a480-71fc-3c4833e3e56a")


def test_parse_history_html_old_format():
    html = """<table><tr><th>Date</th><th>Media Type</th><th></th></tr>
    <tr><td>2019-05-04 10:11:12 UTC</td><td>PHOTO</td>
    <td><a href="javascript:downloadMemories('https://app.snapchat.com/dmd/memories?uid=1&mid=XY', this, true);">download</a></td></tr>
    </table>"""
    (m,) = parse_history_html(html)
    assert m.date_utc == datetime(2019, 5, 4, 10, 11, 12, tzinfo=timezone.utc)
    assert m.media_type == "image"
    assert m.download_link.endswith("mid=XY") and m.mid == "xy"


@pytest.mark.parametrize("text,expected", [
    ("Latitude, Longitude: 35.6762, 139.6503", (35.6762, 139.6503)),
    ("Latitude, Longitude: -33.86 , 151.2", (-33.86, 151.2)),
    ("Latitude, Longitude: 0.0, 0.0", (None, None)),
    ("", (None, None)),
])
def test_parse_location(text, expected):
    assert parse_location(text) == expected


def test_parse_date_variants():
    assert parse_date("2023-07-15 18:42:11 UTC").hour == 18
    assert parse_date("2023-07-15T18:42:11Z").tzinfo is not None
    assert parse_date("nonsense") is None


def test_timezone_from_location_and_fallback():
    r = TimezoneResolver(default="America/New_York")
    utc = datetime(2023, 1, 10, 12, 0, tzinfo=timezone.utc)
    local, name = r.localize(utc, 35.68, 139.65)
    if r.finder is not None:
        assert name == "Asia/Tokyo" and local.hour == 21
    local, name = r.localize(utc)
    assert name == "America/New_York" and local.hour == 7


# --- segment chaining logic, without real videos -----------------------------


@dataclass
class Info:
    duration: float
    sig: tuple = ("h264", 720, 1280)

    def signature(self):
        return self.sig


@dataclass(eq=False)
class Clip:
    name: str
    date_utc: datetime
    info: Info
    first: int  # "scene" id of the first frame
    last: int  # "scene" id of the last frame
    main: str = ""


def _hash(scene):
    rng = np.random.default_rng(scene)
    return rng.integers(0, 256, 32, dtype=np.uint8)


def _edges(clip):
    return (_hash(clip.first), 50.0), (_hash(clip.last), 50.0)


T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)


def names(chains):
    return [[c.name for c in chain] for chain in chains]


def test_chain_of_three_parts_and_a_separate_clip():
    clips = [
        Clip("a1", T0, Info(10), 1, 2),
        Clip("a2", T0 + timedelta(seconds=10), Info(10), 2, 3),
        Clip("a3", T0 + timedelta(seconds=20), Info(4), 3, 4),
        Clip("b", T0 + timedelta(seconds=30), Info(6), 9, 10),  # right after, but a new scene
    ]
    assert names(find_chains(clips, SegmentConfig(), _edges)) == [["a1", "a2", "a3"], ["b"]]


def test_same_timestamp_parts_are_ordered_by_content():
    clips = [Clip("second", T0, Info(8), 2, 3), Clip("first", T0, Info(10), 1, 2)]
    assert names(find_chains(clips, SegmentConfig(), _edges)) == [["first", "second"]]


def test_no_join_when_parameters_or_lengths_differ():
    other = Info(10, sig=("hevc", 1080, 1920))
    clips = [Clip("a", T0, Info(10), 1, 2), Clip("b", T0 + timedelta(seconds=10), other, 2, 3)]
    assert len(find_chains(clips, SegmentConfig(), _edges)) == 2
    short = [Clip("a", T0, Info(2), 1, 2), Clip("b", T0 + timedelta(seconds=2), Info(2), 2, 3)]
    assert len(find_chains(short, SegmentConfig(), _edges)) == 2  # 2 s is not a full part
    far = [Clip("a", T0, Info(10), 1, 2), Clip("b", T0 + timedelta(seconds=60), Info(10), 2, 3)]
    assert len(find_chains(far, SegmentConfig(), _edges)) == 2
    assert len(find_chains(clips[:1] + [Clip("b", T0 + timedelta(seconds=10), Info(10), 2, 3)],
                           SegmentConfig(enabled=False), _edges)) == 2


def test_photo_metadata_keeps_orientation_and_sets_offset(tmp_path):
    from zoneinfo import ZoneInfo

    import piexif
    from PIL import Image

    from snapchat_backup.metadata import write_photo_metadata

    path = tmp_path / "a.jpg"
    Image.new("RGB", (40, 20), (200, 0, 0)).save(path, exif=piexif.dump({"0th": {piexif.ImageIFD.Orientation: 6}}))
    local = datetime(2023, 7, 15, 18, 42, 11, tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/London"))
    assert write_photo_metadata(path, local, 51.5, -0.12)
    exif = piexif.load(str(path))
    assert exif["0th"][piexif.ImageIFD.Orientation] == 6
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2023:07:15 19:42:11"
    assert exif["Exif"][36881] == b"+01:00"
    assert exif["GPS"][piexif.GPSIFD.GPSLongitudeRef] == b"W"
