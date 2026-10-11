"""Builds a synthetic Snapchat export, a fake Snapchat download server and a
fake phone gallery that exercise every tricky case."""

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

W, H = 360, 640  # portrait, like snaps


def scene(seed, size=(1400, 1400)):
    """A detailed, photo-like random picture."""
    rng = np.random.default_rng(seed)
    w, h = size
    low = Image.fromarray(rng.integers(0, 256, (6, 6, 3), dtype=np.uint8)).resize(size, Image.Resampling.BICUBIC)
    mid = Image.fromarray(rng.integers(0, 256, (48, 48, 3), dtype=np.uint8)).resize(size, Image.Resampling.BICUBIC)
    img = Image.blend(low, mid, 0.35)
    draw = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rng.integers(0, w), rng.integers(0, h)
        r = int(rng.integers(15, 160))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            draw.ellipse([x, y, x + r, y + r], fill=color)
        else:
            draw.rectangle([x, y, x + r, y + r // 2], fill=color)
    return img


def photo(seed, shift=(0.0, 0.0)):
    big = scene(seed)
    x, y = int(200 + shift[0] * big.width), int(150 + shift[1] * big.height)
    return big.crop((x, y, x + 2 * W, y + 2 * H)).resize((W * 2, H * 2))


def caption_overlay(size, text="best day ever"):
    ov = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    y, h = int(size[1] * 0.55), int(size[1] * 0.07)
    d.rectangle([0, y, size[0], y + h], fill=(0, 0, 0, 150))
    d.text((size[0] * 0.2, y + h * 0.2), text, fill=(255, 255, 255, 255), font_size=int(h * 0.6))
    return ov


def burn(img, overlay):
    return Image.alpha_composite(img.convert("RGBA"), overlay.resize(img.size)).convert("RGB")


def jpeg(img, path, quality=92):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=quality)
    return path


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)], check=True)


def pan_video(seed, path, seconds, tmp, speed=30, size=(W, H)):
    """A camera-like pan over a random scene, with audio."""
    src = tmp / f"scene_{seed}.png"
    if not src.exists():
        scene(seed).save(src)
    path.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"crop={W}:{H}:x='40+t*{speed}':y='100+20*sin(t)',noise=alls=6:allf=t,"
          f"scale={size[0]}:{size[1]}")
    ffmpeg("-loop", "1", "-framerate", "30", "-i", src, "-f", "lavfi", "-i", "sine=f=440",
           "-vf", vf, "-t", seconds, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-shortest", path)
    return path


def cut(src, path, start, seconds, extra_vf=None):
    """Re-encode a part of a video, like Snapchat storing each part separately."""
    path.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", extra_vf] if extra_vf else []
    ffmpeg("-ss", start, "-i", src, "-t", seconds, *vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", path)
    return path


CAPTION_VF = ("scale=270:480,drawbox=y=ih*0.55:w=iw:h=ih*0.07:color=black@0.6:t=fill,"
              "drawtext=text='best day ever':x=w*0.2:y=h*0.565:fontsize=20:fontcolor=white")


def reencode(src, path, vf=CAPTION_VF):
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", src, "-vf", vf, "-c:v", "libx264", "-crf", "28", "-c:a", "aac", path)
    return path


def black_video(path, seconds, size=(W, H)):
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"color=black:s={size[0]}x{size[1]}:r=30", "-f", "lavfi", "-i", "anullsrc",
           "-t", seconds, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path)
    return path


def uid(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def build(root: Path, base_url: str):
    """Returns (export zip path, phone root, server routes, expectations)."""
    tmp = root / "tmp"
    media = root / "media"
    phone = root / "phone"
    for d in (tmp, media, phone):
        d.mkdir(parents=True, exist_ok=True)

    routes = {}  # path -> (status, body bytes, content type)
    entries = []
    bundled = {}  # zip member name -> file path

    def add(n, date, mtype, location=None, how="bundled", main=None, overlay=None):
        mid = uid(n)
        day = date[:10]
        entry = {"Date": f"{date} UTC", "Media Type": mtype,
                 "Location": f"Latitude, Longitude: {location[0]}, {location[1]}" if location else
                 "Latitude, Longitude: 0.0, 0.0"}
        if how == "bundled":
            entry["Download Link"] = f"{base_url}/dmd/memories?uid=u&sid=s&mid={mid}&ts=1&sig=x"
            bundled[f"memories/{day}_{mid}-main{main.suffix}"] = main
            if overlay:
                bundled[f"memories/{day}_{mid}-overlay.png"] = overlay
        elif how == "media_url":
            entry["Download Link"] = f"{base_url}/dmd/memories?uid=u&sid=s&mid={mid}&ts=1&sig=x"
            entry["Media Download Url"] = f"{base_url}/media/{mid}{main.suffix}"
            routes[f"/media/{mid}{main.suffix}"] = (200, main.read_bytes())
        elif how == "post":
            entry["Download Link"] = f"{base_url}/dmd/old?uid=u&sid=s&mid={mid}&ts=1&sig=x"
            routes[f"POST /dmd/old?mid={mid}"] = (200, f"{base_url}/cdn/{mid}{main.suffix}?sig=1".encode())
            routes[f"/cdn/{mid}{main.suffix}"] = (200, main.read_bytes())
        elif how == "zip":
            entry["Download Link"] = f"{base_url}/dmd/zip?uid=u&sid=s&mid={mid}&ts=1&sig=x"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.write(main, f"{mid}-main{main.suffix}")
                z.write(overlay, f"{mid}-overlay.png")
            routes[f"/dmd/zip?mid={mid}"] = (200, buf.getvalue())
        elif how == "expired":
            entry["Download Link"] = f"{base_url}/dmd/expired?uid=u&sid=s&mid={mid}&ts=1&sig=x"
            routes[f"/dmd/expired?mid={mid}"] = (
                403, b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>")
        entries.append(entry)
        return mid

    LONDON, TOKYO = (51.5074, -0.1278), (35.6762, 139.6503)

    # --- photos ---------------------------------------------------------
    p1 = jpeg(photo(1), media / "p1.jpg")
    ov1 = caption_overlay((W * 2, H * 2))
    ov1.save(media / "p1_overlay.png")
    add(1, "2023-07-15 18:42:11", "Image", LONDON, "bundled", p1, media / "p1_overlay.png")
    # Snapchat's own camera-roll copy: caption burned in, smaller, recompressed
    jpeg(burn(Image.open(p1), ov1).resize((W * 3 // 2, H * 3 // 2)), phone / "Pictures/Snapchat/Snapchat-1234.jpg", 75)

    p2 = jpeg(photo(2), media / "p2.jpg")
    add(2, "2023-08-01 03:00:00", "Image", TOKYO, "bundled", p2)

    p3 = jpeg(photo(3), media / "p3.jpg")
    add(3, "2023-08-02 10:00:00", "Image", None, "media_url", p3)
    # Another shot of the same scene on the phone: must NOT count as a copy
    jpeg(photo(3, shift=(0.03, 0.02)), phone / "DCIM/Camera/IMG_20230802_120001.jpg")

    p4 = jpeg(photo(4), media / "p4.jpg")
    add(4, "2023-08-03 10:00:00", "Image", None, "post", p4)

    p5 = media / "p5.jpg"
    shutil.copyfile(p2, p5)  # the same snap saved to Memories twice
    add(5, "2023-09-01 10:00:00", "Image", None, "bundled", p5)

    black = jpeg(Image.new("RGB", (W * 2, H * 2), (0, 0, 0)), media / "black.jpg")
    black2 = media / "black2.jpg"
    shutil.copyfile(black, black2)
    caption_overlay((W * 2, H * 2), "text snap one").save(media / "t1.png")
    caption_overlay((W * 2, H * 2), "text snap two!!").save(media / "t2.png")
    add(6, "2023-09-02 10:00:00", "Image", None, "bundled", black, media / "t1.png")
    add(7, "2023-09-03 10:00:00", "Image", None, "bundled", black2, media / "t2.png")

    # --- videos ---------------------------------------------------------
    v1 = pan_video(11, tmp / "v1_full.mp4", 25, tmp)
    for i, (start, length) in enumerate([(0, 10), (10, 10), (20, 5)]):
        part = cut(v1, media / f"v1_part{i + 1}.mp4", start, length)
        add(8 + i, f"2023-10-01 12:00:{start:02d}", "Video", LONDON, "bundled", part)
    # The phone only has the first 10 s (with caption): redundant once the full video is imported
    cut(v1, phone / "Pictures/Snapchat/Snapchat-555.mp4", 0, 10, CAPTION_VF)

    v2 = pan_video(12, tmp / "v2_full.mp4", 18, tmp, speed=40)
    v2a = cut(v2, media / "v2_part1.mp4", 0, 10)
    v2b = cut(v2, media / "v2_part2.mp4", 10, 8)
    # Same second for both parts, listed newest-first like Snapchat does
    add(12, "2023-10-02 09:00:00", "Video", None, "bundled", v2b)
    add(11, "2023-10-02 09:00:00", "Video", None, "bundled", v2a)
    reencode(v2, phone / "Movies/Snapchat-full-v2.mp4")

    v3 = pan_video(13, media / "v3.mp4", 6, tmp)
    caption_overlay((W, H), "zip snap").save(media / "v3_overlay.png")
    add(13, "2023-10-03 18:00:00", "Video", None, "zip", v3, media / "v3_overlay.png")

    v4a = pan_video(14, media / "v4a.mp4", 10, tmp)
    v4b = pan_video(15, media / "v4b.mp4", 7, tmp)
    add(14, "2023-10-04 08:00:00", "Video", None, "bundled", v4a)
    add(15, "2023-10-04 08:00:10", "Video", None, "bundled", v4b)

    v5 = pan_video(16, media / "v5.mp4", 9, tmp, speed=20)
    add(16, "2023-10-05 08:00:00", "Video", None, "bundled", v5)
    (phone / "DCIM/Camera").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(v5, phone / "DCIM/Camera/VID_20231005_090000.mp4")

    add(17, "2023-10-06 08:00:00", "Video", None, "expired", main=v5)

    bv = black_video(media / "black.mp4", 5)
    add(18, "2023-10-07 08:00:00", "Video", None, "bundled", bv)
    black_video(phone / "DCIM/Camera/VID_dark.mp4", 5, size=(W // 2, H // 2))

    # --- unrelated phone content ------------------------------------------
    own = jpeg(photo(40), phone / "DCIM/Camera/IMG_20230101_100000.jpg")
    shutil.copyfile(own, phone / "DCIM/Camera/IMG_20230101_100000(1).jpg")  # gallery-internal duplicate
    pan_video(41, phone / "Movies/long_phone_video.mp4", 40, tmp, size=(W // 2, H // 2))
    jpeg(photo(42), phone / "DCIM/.thumbnails/123.jpg")
    jpeg(photo(1), phone / "Pictures/Snapchat/.trashed-1700000000-old.jpg")

    # --- the export ---------------------------------------------------------
    entries.sort(key=lambda e: e["Date"], reverse=True)
    export = root / "mydata~1700000000.zip"
    with zipfile.ZipFile(export, "w") as z:
        z.writestr("json/memories_history.json", json.dumps({"Saved Media": entries}, indent=2))
        z.writestr("index.html", "<html></html>")
        for name, path in bundled.items():
            z.write(path, name)

    expect = {
        "new": {"Snapchat_20230801_120000.jpg",  # p2, Tokyo time
                "Snapchat_20230802_120000.jpg",  # p3 (Europe/Paris default)
                "Snapchat_20230803_120000.jpg",  # p4
                "Snapchat_20230902_120000.jpg", "Snapchat_20230903_120000.jpg",  # text snaps
                "Snapchat_20231001_130000.mp4",  # v1 re-joined (London, BST)
                "Snapchat_20231003_200000.mp4",  # v3 from zip
                "Snapchat_20231004_100000.mp4", "Snapchat_20231004_100010.mp4",  # two separate clips
                "Snapchat_20231007_100000.mp4"},  # black video
        "on_phone": {"Snapchat_20230715_194211.jpg", "Snapchat_20231002_110000.mp4", "Snapchat_20231005_100000.mp4"},
        "export_duplicate": {"Snapchat_20230901_120000.jpg"},
        "cleanup": {"Pictures/Snapchat/Snapchat-555.mp4", "DCIM/Camera/IMG_20230101_100000(1).jpg"},
    }
    return export, phone, routes, expect
