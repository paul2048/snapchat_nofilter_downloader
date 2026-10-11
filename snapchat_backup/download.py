"""Downloading memories that are not bundled in the export.

Snapchat has used several schemes for the links in memories_history:
  * newer exports: "Media Download Url" is fetched directly with GET;
  * "Download Link" answered with GET (header X-Snap-Route-Tag: mem-dmd);
  * older exports: POST the link's query string to the link's base URL, the
    response body is a signed CDN URL which is then fetched with GET.
Each is tried in turn. The media can come back as a zip holding the main file
and its overlay. Links expire roughly a week after the export is created.
"""

import hashlib
import logging
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .media import sniff

log = logging.getLogger(__name__)

_TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


class DownloadError(RuntimeError):
    pass


class LinkExpired(DownloadError):
    pass


def cache_stem(mem):
    ident = mem.mid or (mem.download_link or mem.media_url or "").split("?")[0] + str(mem.index)
    digest = hashlib.sha1(ident.encode()).hexdigest()[:10]
    when = mem.date_utc.strftime("%Y%m%d_%H%M%S") if mem.date_utc else "nodate"
    return f"{when}_{digest}"


class Downloader:
    def __init__(self, cache_dir: Path, workers=6, timeout=120, retries=4):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self._local = threading.local()

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
            self._local.session = s
        return self._local.session

    # ------------------------------------------------------------------
    def fetch_all(self, memories):
        todo = [m for m in memories if m.main is None and (m.download_link or m.media_url)]
        for m in memories:
            if m.main is None and not (m.download_link or m.media_url):
                m.error = "no media in export and no download link"
        if not todo:
            return
        log.info("Downloading %d memories (%d at a time)...", len(todo), self.workers)
        done = failed = expired = 0
        with ThreadPoolExecutor(self.workers) as pool:
            futures = {pool.submit(self.fetch, m): m for m in todo}
            for fut in as_completed(futures):
                mem = futures[fut]
                try:
                    mem.main, mem.overlay = fut.result()
                    done += 1
                except LinkExpired as e:
                    mem.error = str(e)
                    expired += 1
                except Exception as e:  # keep going, report at the end
                    mem.error = f"download failed: {e}"
                    failed += 1
                total = done + failed + expired
                if total % 25 == 0 or total == len(todo):
                    log.info("  %d/%d downloaded (%d failed, %d expired)", done, len(todo), failed, expired)
        if expired:
            log.warning(
                "%d download links have expired. Request a new export from Snapchat and run again "
                "(already downloaded files are kept).", expired)

    def cached(self, mem):
        stem = cache_stem(mem)
        mains = [p for p in self.cache_dir.glob(stem + "-main.*") if not p.name.endswith(".part")]
        if not mains:
            return None
        overlays = list(self.cache_dir.glob(stem + "-overlay.*"))
        return mains[0], overlays[0] if overlays else None

    def fetch(self, mem):
        hit = self.cached(mem)
        if hit:
            return hit
        stem = cache_stem(mem)
        tmp = self.cache_dir / f"{stem}.part"
        ext = self._download_to(mem, tmp)
        if ext == ".zip":
            main, overlay = self._unzip(tmp, stem)
            tmp.unlink()
            return main, overlay
        main = self.cache_dir / f"{stem}-main{ext}"
        tmp.replace(main)
        return main, None

    def _unzip(self, path, stem):
        main = overlay = None
        with zipfile.ZipFile(path) as z:
            infos = sorted((i for i in z.infolist() if not i.is_dir()), key=lambda i: -i.file_size)
            for info in infos:
                data = z.read(info)
                ext = sniff(data[:16]) or Path(info.filename).suffix.lower() or ".bin"
                if "overlay" in info.filename.lower():
                    if overlay is None:
                        overlay = self.cache_dir / f"{stem}-overlay{ext}"
                        overlay.write_bytes(data)
                elif main is None:
                    target = self.cache_dir / f"{stem}-main{ext}"
                    target.with_suffix(".tmp").write_bytes(data)
                    target.with_suffix(".tmp").replace(target)
                    main = target
        if main is None:
            raise DownloadError("zip without media")
        return main, overlay

    # ------------------------------------------------------------------
    def _download_to(self, mem, dest):
        attempts = []
        if mem.media_url:
            attempts.append(("GET", mem.media_url, {}))
        if mem.download_link:
            attempts.append(("GET", mem.download_link, {"X-Snap-Route-Tag": "mem-dmd"}))
            base, _, query = mem.download_link.partition("?")
            attempts.append(("POST", base, {"Content-Type": "application/x-www-form-urlencoded", "_body": query}))
        errors = []
        for method, url, headers in attempts:
            try:
                ext = self._try(method, url, dict(headers), dest, follow=True)
                if ext:
                    return ext
            except DownloadError as e:
                errors.append(e)
        expired = [e for e in errors if isinstance(e, LinkExpired)]
        if expired:
            raise expired[0]
        raise errors[-1] if errors else DownloadError("no usable response")

    def _request(self, method, url, headers):
        body = headers.pop("_body", None)
        if not url.lower().startswith(("http://", "https://")):
            raise DownloadError(f"unsupported URL {url[:40]}")
        delay = 2
        for attempt in range(self.retries + 1):
            try:
                r = self.session.request(method, url, headers=headers, data=body, timeout=self.timeout, stream=True)
                if r.status_code in _TRANSIENT and attempt < self.retries:
                    r.close()
                    time.sleep(delay)
                    delay *= 2
                    continue
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt >= self.retries:
                    raise DownloadError(f"network error: {e}") from e
                time.sleep(delay)
                delay *= 2
        raise DownloadError("too many retries")

    def _try(self, method, url, headers, dest, follow):
        r = self._request(method, url, headers)
        with r:
            if r.status_code >= 400:
                text = r.content[:2000].decode("utf-8", "replace")
                if r.status_code in (400, 403, 404, 410) and re.search(
                    r"expired|AccessDenied|SignatureDoesNotMatch|Request has expired", text, re.I
                ):
                    raise LinkExpired("download link expired")
                raise DownloadError(f"HTTP {r.status_code}")
            chunks = r.iter_content(1 << 16)
            head = b""
            for chunk in chunks:
                head += chunk
                if len(head) >= 64:
                    break
            ext = sniff(head)
            if ext:
                with open(dest, "wb") as f:
                    f.write(head)
                    for chunk in chunks:
                        f.write(chunk)
                return ext
            # Not media: maybe the body is (or contains) the real media URL.
            text = (head + b"".join(c for _, c in zip(range(16), chunks))).decode("utf-8", "replace")
        if follow:
            m = _URL_RE.search(text.strip())
            if m:
                return self._try("GET", m.group(0), {}, dest, follow=False)
        raise DownloadError("response was not a photo or video")
