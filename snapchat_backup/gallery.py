"""Listing the photos and videos of a phone gallery (or a copy of it)."""

import os
from dataclasses import dataclass
from pathlib import Path

from .media import kind_from_ext

# Folders that never hold gallery media worth comparing against.
_SKIP_DIRS = {".thumbnails", ".trash", ".trashed", ".cache", "android", "lost.dir"}


@dataclass
class GalleryFile:
    path: Path
    rel: str  # path relative to the gallery root, with "/" separators
    kind: str
    size: int
    mtime: float


def scan_gallery(roots, exclude=()):
    """All photos/videos below `roots`. Files Android keeps in its trash
    (`.trashed-...`) or that are still being written (`.pending-...`) are
    ignored, as are hidden folders."""
    exclude = [Path(e).resolve() for e in exclude]
    files = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in _SKIP_DIRS
                and (here / d).resolve() not in exclude
            ]
            if (here / ".nomedia").exists() and here != root:
                dirnames[:] = []
                continue
            for name in filenames:
                if name.startswith("."):
                    continue
                kind = kind_from_ext(name)
                if kind is None:
                    continue
                path = here / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size == 0:
                    continue
                files.append(
                    GalleryFile(
                        path=path,
                        rel=str(path.relative_to(root)).replace("\\", "/"),
                        kind=kind,
                        size=st.st_size,
                        mtime=st.st_mtime,
                    )
                )
    return files
