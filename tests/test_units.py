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


def test_bundled_files_without_ids_are_matched_by_their_time_stamp(tmp_path):
    """Recent exports have no download links, so no ids: each file is matched
    to its memory by the time Snapchat stamps it with in the zip, even when a
    file of the same day is missing (pairing by order would shift them)."""
    import json
    import zipfile

    from snapchat_backup.export import read_export

    def entry(date, kind):
        return {"Date": f"{date} UTC", "Media Type": kind, "Location": "Latitude, Longitude: 0.0, 0.0",
                "Download Link": "", "Media Download Url": ""}

    history = {"Saved Media": [
        entry("2020-06-27 10:00:01", "Image"),
        entry("2020-06-27 09:00:00", "Image"),  # its file is missing from the export
        entry("2020-06-27 00:43:40", "Video"),
        entry("2020-06-27 00:43:40", "Video"),
    ]}
    photo = "2020-06-27_aaaaaaaa-0000-0000-0000-000000000001-main.jpg"
    videos = ["2020-06-27_aaaaaaaa-0000-0000-0000-000000000002-main.mp4",
              "2020-06-27_aaaaaaaa-0000-0000-0000-000000000003-main.mp4"]
    export = tmp_path / "mydata~1.zip"
    with zipfile.ZipFile(export, "w") as z:
        z.writestr("json/memories_history.json", json.dumps(history))
        z.writestr(zipfile.ZipInfo(f"memories/{photo}", (2020, 6, 27, 10, 0, 0)), b"x")  # 2 s resolution
        for name in videos:
            z.writestr(zipfile.ZipInfo(f"memories/{name}", (2020, 6, 27, 0, 43, 40)), b"x")
    memories, _ = read_export([export], tmp_path / "media")
    by_time = {}
    for m in memories:
        by_time.setdefault(m.date_utc.strftime("%H:%M:%S"), []).append(m.main.name if m.main else None)
    assert by_time == {"10:00:01": [photo], "09:00:00": [None], "00:43:40": videos}
    assert all(m.time_exact for m in memories)
    assert len(memories) == 4  # no file was left over as an extra memory


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


def test_time_zone_of_memories_without_location():
    r = TimezoneResolver(default="America/New_York")
    if r.finder is None:
        pytest.skip("timezonefinder not installed")
    day = timedelta(days=1)
    t0 = datetime(2018, 1, 1, 12, 0, tzinfo=timezone.utc)
    BUCHAREST, LONDON = (47.65, 23.58), (51.57, -0.40)
    points = [
        (t0, *BUCHAREST),
        (t0 + 40 * day, None, None),  # between two Bucharest memories, however far: Bucharest
        (t0 + 90 * day, *BUCHAREST),
        (t0 + 93 * day, None, None),  # the next one is London, but Bucharest is 3 days away
        (t0 + 140 * day, None, None),  # Bucharest before, London after, both far: the default
        (t0 + 180 * day, *LONDON),
        (t0 + 185 * day, None, None),  # nothing after, London 5 days before
        (t0 + 300 * day, None, None),  # nothing after, London far before: the default
        (None, None, None),
    ]
    zones = [(name, nearby) for _, name, nearby in r.localize_all(points)]
    assert zones == [
        ("Europe/Bucharest", False), ("Europe/Bucharest", True), ("Europe/Bucharest", False),
        ("Europe/Bucharest", True), ("America/New_York", False), ("Europe/London", False),
        ("Europe/London", True), ("America/New_York", False), ("", False),
    ]
    assert TimezoneResolver(default="America/New_York", use_location=False).localize_all(points[:2])[1][1] == \
        "America/New_York"


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
    pans: tuple = None  # (first two, last two) pan positions over scene `first`


def _thumb(scene, pan=0):
    """A smooth 32x32 picture; `pan` moves the camera sideways over it."""
    from PIL import Image

    rng = np.random.default_rng(scene)
    grid = rng.integers(0, 256, (3, 4, 3), dtype=np.uint8)
    wide = np.asarray(Image.fromarray(grid).resize((96, 32), Image.Resampling.BICUBIC))
    return wide[:, pan:pan + 32]


def _edges(clip):
    if clip.pans:
        (f0, f1), (l0, l1) = clip.pans
        return (np.stack([_thumb(clip.first, f0), _thumb(clip.first, f1)]),
                np.stack([_thumb(clip.first, l0), _thumb(clip.first, l1)]))
    first, last = _thumb(clip.first), _thumb(clip.last)
    return np.stack([first, first]), np.stack([last, last])


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


def test_parts_differ_in_frame_rate_and_length():
    """Phones record at a variable frame rate and Snapchat's full parts vary
    in length: neither may keep the parts of one recording apart."""
    from snapchat_backup.media import VideoInfo

    a = VideoInfo(10.0, 540, 960, 0, "h264", "yuv420p", 24.9, "aac", 44100, 1)
    b = VideoInfo(10.0, 540, 960, 0, "h264", "yuv420p", 20.7, "aac", 44100, 1)
    assert a.signature() == b.signature()
    clips = [
        Clip("a1", T0, Info(9.0), 1, 2),
        Clip("a2", T0 + timedelta(seconds=10), Info(10.8), 2, 3),
        Clip("a3", T0 + timedelta(seconds=20), Info(9.0), 3, 4),
    ]
    assert names(find_chains(clips, SegmentConfig(), _edges)) == [["a1", "a2", "a3"]]


def test_cut_is_judged_against_the_motion():
    """A shaky recording changes many pixels at the cut, as it does between
    any two frames: still joined. The same jump in a steady video is a
    different moment: not joined."""
    from snapchat_backup.media import frame_difference

    cfg = SegmentConfig()
    # The camera pans 3 px per frame; the cut continues the pan.
    shaky = [Clip("a", T0, Info(10), 5, 5, pans=((0, 3), (24, 27))),
             Clip("b", T0 + timedelta(seconds=10), Info(6), 5, 5, pans=((30, 33), (57, 60)))]
    jump = frame_difference(_thumb(5, 27), _thumb(5, 30))
    assert jump > cfg.still  # not a near-identical picture
    assert names(find_chains(shaky, cfg, _edges)) == [["a", "b"]]
    # Steady camera, then the picture jumps by the same amount.
    steady = [Clip("a", T0, Info(10), 5, 5, pans=((0, 0), (27, 27))),
              Clip("b", T0 + timedelta(seconds=10), Info(6), 5, 5, pans=((30, 30), (30, 30)))]
    assert len(find_chains(steady, cfg, _edges)) == 2


def test_same_timestamp_parts_of_a_steady_scene_keep_their_order():
    """Parts saved with one timestamp: a walk down the same street looks
    alike 10 s apart, but only the real cut is as smooth as the motion."""
    pans = {"p1": ((0, 1), (9, 10)), "p2": ((11, 12), (20, 21)), "p3": ((22, 23), (30, 31))}
    clips = [Clip(n, T0, Info(10 if n != "p3" else 4), 7, 7, pans=pans[n]) for n in ("p3", "p1", "p2")]
    assert names(find_chains(clips, SegmentConfig(), _edges)) == [["p1", "p2", "p3"]]


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
