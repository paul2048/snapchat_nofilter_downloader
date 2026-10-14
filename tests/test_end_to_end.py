import csv
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import piexif

from snapchat_backup.cli import main
from snapchat_backup.media import probe_video, run

FAKE_ADB = Path(__file__).with_name("fake_adb.py")


def read_report(out):
    with open(out / "report.csv", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def by_status(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["status"], set()).add(r["file"])
    return groups


def creation_time(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time", "-of",
               "default=nw=1:nk=1", str(path)]).stdout.decode().strip()
    return out


def test_gallery_folder_mode(world, tmp_path):
    out = tmp_path / "out"
    code = main(["--export", str(world["export"]), "--gallery", str(world["phone"]), "--out", str(out),
                 "--timezone", "Europe/Paris"])
    assert code == 0
    rows = read_report(out)
    groups = by_status(rows)
    expect = world["expect"]
    assert groups["new"] == expect["new"]
    assert groups["already on phone"] == expect["on_phone"]
    assert groups["duplicate in export"] == expect["export_duplicate"]
    failed = [r for r in rows if r["status"] == "failed"]
    assert len(failed) == 1 and "expired" in failed[0]["notes"]

    # Only new files go to the phone folder; everything is kept in all_memories.
    assert {p.name for p in (out / "to_phone").iterdir()} == expect["new"]
    assert {p.name for p in (out / "all_memories").iterdir()} == expect["new"] | expect["on_phone"]

    # Photo dates: EXIF local time + offset, and the file time.
    tokyo = out / "all_memories" / "Snapchat_20230801_120000.jpg"
    exif = piexif.load(str(tokyo))
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2023:08:01 12:00:00"
    assert exif["Exif"][36881] == b"+09:00"
    assert exif["GPS"][piexif.GPSIFD.GPSLatitudeRef] == b"N"
    expected_ts = datetime(2023, 8, 1, 3, 0, tzinfo=timezone.utc).timestamp()
    assert abs(tokyo.stat().st_mtime - expected_ts) < 1

    # The re-joined video: 25 s, correct creation time, parts kept in extras.
    joined = out / "all_memories" / "Snapchat_20231001_130000.mp4"
    assert abs(probe_video(joined).duration - 25.0) < 0.5
    assert creation_time(joined).startswith("2023-10-01T12:00:00")
    assert len(list((out / "extras" / "video_parts" / joined.stem).iterdir())) == 3

    # Parts saved with identical timestamps were put back in the right order.
    v2 = out / "all_memories" / "Snapchat_20231002_100000.mp4"
    assert abs(probe_video(v2).duration - 18.0) < 0.5

    # Overlays are kept next to the media, not lost.
    overlays = {p.name for p in (out / "extras" / "overlays").iterdir() if p.is_file()}
    assert "Snapchat_20231003_190000_overlay.png" in overlays
    assert "Snapchat_20230715_194211_overlay.png" in overlays

    with open(out / "phone_cleanup.csv", encoding="utf-8-sig") as f:
        cleanup = {r["phone file"]: r for r in csv.DictReader(f)}
    assert set(cleanup) == expect["cleanup"]
    assert cleanup["Pictures/Snapchat/Snapchat-555.mp4"]["keep instead"] == joined.name

    # Second run: nothing is downloaded again and the result is the same.
    hits = world["server"].hits
    assert main(["--export", str(world["export"]), "--gallery", str(world["phone"]), "--out", str(out),
                 "--timezone", "Europe/Paris"]) == 0
    assert by_status(read_report(out)) == groups
    assert world["server"].hits - hits <= 3  # only the expired link is retried


def _fake_adb(tmp_path, phone_root):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "adb"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_ADB} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return bindir


def test_adb_mode(world, tmp_path, monkeypatch):
    import shutil

    phone = tmp_path / "phone"
    shutil.copytree(world["phone"], phone)
    bindir = _fake_adb(tmp_path, phone)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ADB_ROOT", str(phone))
    out = tmp_path / "out"
    args = ["--export", str(world["export"]), "--adb", "--clean-phone", "--yes", "--out", str(out),
            "--timezone", "Europe/Paris"]
    assert main(args) == 0

    pushed = phone / "DCIM" / "Snapchat Memories"
    expect = world["expect"]
    assert {p.name for p in pushed.iterdir()} == expect["new"]
    v1 = pushed / "Snapchat_20231001_130000.mp4"
    assert abs(v1.stat().st_mtime - datetime(2023, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()) < 1

    review = phone / "Snapchat duplicates (review)"
    assert (review / ".nomedia").exists()
    assert {p.name for p in review.iterdir()} == {
        ".nomedia", "Pictures__Snapchat__Snapchat-555.mp4", "DCIM__Camera__IMG_20230101_100000(1).jpg"}
    assert not (phone / "Pictures/Snapchat/Snapchat-555.mp4").exists()
    # A different shot of the same scene is never touched.
    assert (phone / "DCIM/Camera/IMG_20230802_120001.jpg").exists()

    # Running again finds everything on the phone and copies nothing.
    before = sorted(p.name for p in pushed.iterdir())
    assert main(args + ["--adb-refresh"]) == 0
    assert sorted(p.name for p in pushed.iterdir()) == before
    groups = by_status(read_report(out))
    assert "new" not in groups
    assert groups["already on phone"] == expect["new"] | expect["on_phone"]


def test_cleanup_waits_for_the_full_video(world, tmp_path, monkeypatch):
    """With --no-push the full video never reaches the phone, so its partial
    copy must stay visible; plain identical copies are still tidied up."""
    import shutil

    phone = tmp_path / "phone"
    shutil.copytree(world["phone"], phone)
    monkeypatch.setenv("PATH", f"{_fake_adb(tmp_path, phone)}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ADB_ROOT", str(phone))
    assert main(["--export", str(world["export"]), "--adb", "--no-push", "--clean-phone", "--yes",
                 "--out", str(tmp_path / "out"), "--timezone", "Europe/Paris"]) == 0
    assert not (phone / "DCIM" / "Snapchat Memories").exists()
    assert (phone / "Pictures/Snapchat/Snapchat-555.mp4").exists()
    assert not (phone / "DCIM/Camera/IMG_20230101_100000(1).jpg").exists()


def test_burn_overlays_and_no_join(world, tmp_path):
    import numpy as np
    from PIL import Image

    out = tmp_path / "out"
    assert main(["--export", str(world["export"]), "--out", str(out), "--timezone", "Europe/Paris",
                 "--overlays", "burn", "--no-join"]) == 0
    files = {p.name for p in (out / "to_phone").iterdir()}
    # The three parts stay separate.
    assert {"Snapchat_20231001_130000.mp4", "Snapchat_20231001_130010.mp4", "Snapchat_20231001_130020.mp4"} <= files
    assert not (out / "extras" / "overlays").exists()
    # The caption bar is now part of the photo.
    burned = np.asarray(Image.open(out / "all_memories" / "Snapchat_20230715_194211.jpg").convert("L"), dtype=float)
    h = burned.shape[0]
    bar = burned[int(h * 0.56):int(h * 0.60)].mean()
    above = burned[int(h * 0.45):int(h * 0.50)].mean()
    assert bar < above * 0.8
