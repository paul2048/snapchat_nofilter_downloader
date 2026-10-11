"""Probing and visual fingerprinting of photos and videos.

Fingerprints are 256-bit difference hashes (dHash) computed on a 17x16
grayscale thumbnail. Videos are fingerprinted as a sequence of such hashes,
sampled at a fixed rate, so that a clip can be found inside a longer video.
"""

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:  # Optional HEIC support (phone camera photos on some devices)
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".mkv", ".webm", ".avi"}

HASH_BITS = 256
VIDEO_FPS = 2.0
# Frames/photos whose thumbnail is this flat (std of 0-255 gray values) carry
# too little information for their hash to be trusted (black frames, plain
# colour backgrounds of text snaps, ...).
FLAT_STD = 4.0

_GRAY_W, _GRAY_H = 17, 16
_FRAME_BYTES = _GRAY_W * _GRAY_H


class MediaError(RuntimeError):
    pass


def require_ffmpeg():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise MediaError(
            f"{' and '.join(missing)} not found. Install FFmpeg and make sure it is on "
            "your PATH (Windows: `winget install Gyan.FFmpeg`, macOS: `brew install "
            "ffmpeg`, Linux: `sudo apt install ffmpeg`)."
        )


def run(cmd, input_bytes=None):
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"{Path(cmd[0]).name} failed: {' | '.join(err[-3:]) or proc.returncode}")
    return proc


def kind_from_ext(path):
    """"image"/"video" from a file name or a bare extension such as ".jpg"."""
    ext = Path(path).suffix.lower() or str(path).lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def sniff(head: bytes):
    """Return a file extension for the given leading bytes, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
            return ".heic"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"
    if head[4:8] in (b"moov", b"mdat", b"free", b"wide"):
        return ".mov"
    return None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


@dataclass
class VideoInfo:
    duration: float
    width: int  # as displayed, i.e. after applying rotation
    height: int
    rotation: int
    vcodec: str
    pix_fmt: str
    fps: float
    acodec: str = None
    sample_rate: int = None
    channels: int = None

    @property
    def has_audio(self):
        return self.acodec is not None

    def signature(self):
        """Parameters that all segments of a single recording share."""
        return (
            self.width,
            self.height,
            self.rotation,
            self.vcodec,
            self.pix_fmt,
            round(self.fps, 1),
            self.acodec,
            self.sample_rate,
            self.channels,
        )

    def to_json(self):
        return dict(self.__dict__)


def _ratio(text):
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
        return value if value > 0 else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path) -> VideoInfo:
    out = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,"
            "avg_frame_rate,r_frame_rate,sample_rate,channels,duration:"
            "stream_tags=rotate:stream_side_data=rotation",
            "-of",
            "json",
            str(path),
        ]
    ).stdout
    data = json.loads(out or b"{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"no video stream in {path}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(round(float(side["rotation"])))
    if not rotation and video.get("tags", {}).get("rotate"):
        rotation = int(video["tags"]["rotate"])
    rotation %= 360
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if rotation in (90, 270):
        width, height = height, width
    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        rotation=rotation,
        vcodec=video.get("codec_name", ""),
        pix_fmt=video.get("pix_fmt", ""),
        fps=fps,
        acodec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
    )


def image_size(path):
    """(width, height) as displayed, honouring the EXIF orientation."""
    with Image.open(path) as im:
        w, h = im.size
        try:
            orientation = im.getexif().get(0x0112, 1)
        except Exception:
            orientation = 1
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    return w, h


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------


def _dhash(gray):
    """gray: (n, 16, 17) array -> ((n, 32) uint8 hashes, (n,) float32 std)."""
    gray = gray.astype(np.int16)
    bits = gray[:, :, 1:] > gray[:, :, :-1]
    hashes = np.packbits(bits.reshape(len(gray), -1), axis=1)
    std = gray.reshape(len(gray), -1).std(axis=1).astype(np.float32)
    return hashes, std


if hasattr(np, "bitwise_count"):

    def popcount(x):
        return np.bitwise_count(x)

else:  # numpy < 2.0
    _POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

    def popcount(x):
        return _POP[x]


def hamming(a, b):
    """Pairwise Hamming distances between hash arrays a (n, 32) and b (m, 32)."""
    a = np.asarray(a, dtype=np.uint8)
    b = np.asarray(b, dtype=np.uint8)
    return popcount(a[:, None, :] ^ b[None, :, :]).sum(axis=-1, dtype=np.int32)


@dataclass
class PhotoPrint:
    hash: np.ndarray  # (32,) uint8
    thumb: np.ndarray  # (32, 32, 3) uint8
    std: float
    width: int
    height: int

    @property
    def informative(self):
        return self.std >= FLAT_STD

    @property
    def aspect(self):
        return self.width / self.height if self.height else 0.0


def photo_print(path, overlay=None) -> PhotoPrint:
    with Image.open(path) as im:
        im.draft("RGB", (256, 256))  # fast reduced-size JPEG decoding
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if overlay is not None:
            with Image.open(overlay) as ov:
                ov = ov.convert("RGBA").resize(im.size, Image.Resampling.BILINEAR)
                im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        gray = np.asarray(im.convert("L").resize((_GRAY_W, _GRAY_H), Image.Resampling.BOX))
        thumb = np.asarray(im.resize((32, 32), Image.Resampling.BOX), dtype=np.uint8)
    hashes, std = _dhash(gray[None])
    w, h = image_size(path)
    return PhotoPrint(hash=hashes[0], thumb=thumb, std=float(std[0]), width=w, height=h)


def thumb_difference(a: PhotoPrint, b: PhotoPrint, percentile=75):
    """Robust pixel difference between two thumbnails (0-255).

    Uses a percentile instead of the mean so that a caption or sticker that
    only covers part of one image does not hide an otherwise identical photo.
    """
    diff = np.abs(a.thumb.astype(np.int16) - b.thumb.astype(np.int16)).max(axis=2)
    return float(np.percentile(diff, percentile))


@dataclass
class VideoPrint:
    hashes: np.ndarray  # (n, 32) uint8, one per sampled frame
    std: np.ndarray  # (n,) float32
    fps: float = VIDEO_FPS

    def __len__(self):
        return len(self.hashes)

    @property
    def informative(self):
        return self.std >= FLAT_STD


def _raw_gray_frames(cmd):
    raw = run(cmd).stdout
    n = len(raw) // _FRAME_BYTES
    return np.frombuffer(raw[: n * _FRAME_BYTES], dtype=np.uint8).reshape(n, _GRAY_H, _GRAY_W)


_SCALE = f"scale={_GRAY_W}:{_GRAY_H}:flags=area,format=gray"


def video_print(path, overlay=None, size=None, fps=VIDEO_FPS) -> VideoPrint:
    """Sample frames at `fps` and hash them. `size` is the display size of the
    video, needed to stretch an overlay over the frame."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path)]
    if overlay is not None and size:
        cmd += [
            "-i",
            str(overlay),
            "-filter_complex",
            f"[0:v]fps={fps}[v];[1:v]scale={size[0]}:{size[1]}[ov];"
            f"[v][ov]overlay=format=auto,{_SCALE}",
        ]
    else:
        cmd += ["-vf", f"fps={fps},{_SCALE}"]
    cmd += ["-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    frames = _raw_gray_frames(cmd)
    if len(frames) == 0:
        raise MediaError(f"could not decode any frame of {path}")
    hashes, std = _dhash(frames)
    return VideoPrint(hashes=hashes, std=std, fps=fps)


def edge_frames(path):
    """Hashes of the very first and very last frame: ((hash, std), (hash, std))."""
    first = _raw_gray_frames(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", _SCALE, "-frames:v", "1",
         "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    )
    last = np.empty((0, _GRAY_H, _GRAY_W), dtype=np.uint8)
    try:
        last = _raw_gray_frames(
            ["ffmpeg", "-v", "error", "-nostdin", "-sseof", "-1.5", "-i", str(path), "-vf", _SCALE,
             "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        )
    except MediaError:
        pass
    if len(last) == 0:  # very short clip: decode everything
        last = _raw_gray_frames(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf", _SCALE,
             "-an", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        )
    if len(first) == 0 or len(last) == 0:
        raise MediaError(f"could not decode {path}")
    fh, fs = _dhash(first[:1])
    lh, ls = _dhash(last[-1:])
    return (fh[0], float(fs[0])), (lh[0], float(ls[0]))


# --------------------------------------------------------------------------
# Sequence alignment
# --------------------------------------------------------------------------


@dataclass
class Alignment:
    offset: int  # position of short[0] inside long, in samples
    mean: float  # mean Hamming distance over informative aligned frames
    good: float  # fraction of informative aligned frames under the frame threshold
    overlap: int  # number of aligned samples
    informative: int  # number of aligned samples where both frames are informative
    coverage: float = field(default=0.0)  # overlap / len(long)


def align(short: VideoPrint, long: VideoPrint, frame_threshold, slack=2, min_overlap=0.8):
    """Find where `short` best fits inside `long` (or vice versa for equal
    lengths). Returns the best Alignment or None."""
    n, m = len(short), len(long)
    dist = hamming(short.hashes, long.hashes)
    informative = short.informative[:, None] & long.informative[None, :]
    need = max(1, int(np.ceil(min_overlap * n)))
    best = None
    for k in range(-slack, m - n + slack + 1):
        i0, i1 = max(0, -k), min(n, m - k)
        if i1 - i0 < need:
            continue
        idx = np.arange(i0, i1)
        d = dist[idx, idx + k]
        mask = informative[idx, idx + k]
        count = int(mask.sum())
        if count < min(3, len(idx)):
            continue
        d = d[mask]
        cand = Alignment(
            offset=k,
            mean=float(d.mean()),
            good=float((d <= frame_threshold).mean()),
            overlap=len(idx),
            informative=count,
            coverage=len(idx) / m if m else 0.0,
        )
        if best is None or cand.mean < best.mean:
            best = cand
    return best
