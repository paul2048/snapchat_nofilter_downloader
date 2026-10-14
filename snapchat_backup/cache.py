"""On-disk cache of probe results and fingerprints, so re-runs are fast."""

import json
import sqlite3
from pathlib import Path

import numpy as np

from .media import PhotoPrint, VideoInfo, VideoPrint

_VERSION = 1


class FingerprintCache:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS prints (key TEXT PRIMARY KEY, meta TEXT, a BLOB, b BLOB)"
        )

    def close(self):
        self.db.commit()
        self.db.close()

    @staticmethod
    def key(kind, path, variant=""):
        path = Path(path)
        st = path.stat()
        return f"v{_VERSION}|{kind}|{variant}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"

    def _get(self, key):
        return self.db.execute("SELECT meta, a, b FROM prints WHERE key = ?", (key,)).fetchone()

    def _put(self, key, meta, a=None, b=None):
        self.db.execute(
            "INSERT OR REPLACE INTO prints (key, meta, a, b) VALUES (?, ?, ?, ?)",
            (key, json.dumps(meta), a, b),
        )

    def commit(self):
        self.db.commit()

    # Video probe -------------------------------------------------------
    def get_info(self, key):
        row = self._get(key)
        return VideoInfo(**json.loads(row[0])) if row else None

    def put_info(self, key, info: VideoInfo):
        self._put(key, info.to_json())

    # Photo fingerprint ---------------------------------------------------
    def get_photo(self, key):
        row = self._get(key)
        if not row:
            return None
        meta = json.loads(row[0])
        return PhotoPrint(
            hash=np.frombuffer(row[1], dtype=np.uint8).copy(),
            thumb=np.frombuffer(row[2], dtype=np.uint8).reshape(32, 32, 3).copy(),
            std=meta["std"],
            width=meta["width"],
            height=meta["height"],
        )

    def put_photo(self, key, p: PhotoPrint):
        self._put(
            key,
            {"std": p.std, "width": p.width, "height": p.height},
            p.hash.tobytes(),
            p.thumb.tobytes(),
        )

    # Video fingerprint -------------------------------------------------
    def get_video(self, key):
        row = self._get(key)
        if not row:
            return None
        meta = json.loads(row[0])
        hashes = np.frombuffer(row[1], dtype=np.uint8).reshape(-1, 32).copy()
        std = np.frombuffer(row[2], dtype=np.float32).copy()
        return VideoPrint(hashes=hashes, std=std, fps=meta["fps"])

    def put_video(self, key, v: VideoPrint):
        self._put(key, {"fps": v.fps}, v.hashes.tobytes(), v.std.astype(np.float32).tobytes())

    # First/last frames of a video (media.edge_frames) ---------------------
    def get_edges(self, key):
        row = self._get(key)
        if not row:
            return None
        shape = tuple(json.loads(row[0])["shape"])
        return (np.frombuffer(row[1], dtype=np.uint8).reshape(shape).copy(),
                np.frombuffer(row[2], dtype=np.uint8).reshape(shape).copy())

    def put_edges(self, key, edges):
        first, last = edges
        self._put(key, {"shape": list(first.shape)}, first.tobytes(), last.tobytes())

    # Generic JSON values (sha256, ...) -------------------------------------
    def get_json(self, key):
        row = self._get(key)
        return json.loads(row[0]) if row else None

    def put_json(self, key, value):
        self._put(key, value)
