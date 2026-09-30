"""Download a single reel with yt-dlp."""

import shutil
from pathlib import Path

from yt_dlp import YoutubeDL

from .errors import FatalError, RetryableError, SkipError

# Substrings of yt-dlp error messages -> how to treat them
_SKIP = ("not available", "unavailable", "has been removed", "does not exist",
         "no video formats", "unsupported url", "404")
# NOTE: yt-dlp's generic "rate-limit reached or login required" message matches
# _RETRY first (checked before _FATAL), so it is retried rather than aborting the run.
_FATAL = ("login required", "checkpoint", "cookies are no longer valid")
_RETRY = ("rate-limit", "rate limit", "429", "timed out", "timeout", "connection",
          "temporarily", "5xx", "500", "502", "503", "504", "incomplete", "reset")

_FFMPEG = shutil.which("ffmpeg") is not None

_EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/heic": ".heic", "video/mp4": ".mp4",
                "video/quicktime": ".mov"}
CONTENT_TYPES = {v: k for k, v in _EXT_BY_TYPE.items() if k != "image/jpg"}


class UrlExpired(RetryableError):
    """Instagram's signed CDN link stopped working; fetch fresh links and retry."""


def download_url(session, url: str, dest_stem: Path, default_ext: str) -> Path:
    """Download a photo/video from a direct CDN URL to dest_stem + extension."""
    try:
        resp = session.get(url, stream=True, timeout=(15, 180))
    except Exception as exc:
        raise RetryableError(f"download failed: network error: {exc}") from exc
    try:
        code = resp.status_code
        if code in (403, 410) or (code == 400 and "expired" in (resp.text or "").lower()):
            raise UrlExpired(f"media link expired (HTTP {code})")
        if code == 404:
            raise SkipError("media file not found on Instagram (HTTP 404)")
        if code == 429 or code >= 500:
            raise RetryableError(f"download failed: HTTP {code}")
        if code >= 400:
            raise SkipError(f"download failed: HTTP {code}")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = _EXT_BY_TYPE.get(ctype) or Path(url.split("?", 1)[0]).suffix.lower() or default_ext
        if ext not in CONTENT_TYPES:
            ext = default_ext
        path = dest_stem.with_suffix(ext)
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    fh.write(chunk)
    except (RetryableError, SkipError):
        raise
    except Exception as exc:  # connection dropped mid-download
        raise RetryableError(f"download interrupted: {exc}") from exc
    finally:
        resp.close()
    if path.stat().st_size == 0:
        raise RetryableError("downloaded file is empty")
    return path


def _classify(url: str, exc: Exception) -> Exception:
    msg = str(exc)
    low = msg.lower()
    if any(s in low for s in _RETRY):
        return RetryableError(f"yt-dlp: {msg}")
    if any(s in low for s in _FATAL):
        return FatalError(f"yt-dlp needs a valid login for {url}: {msg}\n"
                          "  -> pass fresh cookies with --cookies")
    if any(s in low for s in _SKIP):
        return SkipError(f"yt-dlp: {msg}")
    return RetryableError(f"yt-dlp: {msg}")  # unknown: retry, then give up on this reel


def download_reel(url: str, out_dir: Path, cookies_file: str | None = None) -> tuple[Path, dict]:
    """Download `url` into out_dir. Returns (file_path, yt-dlp info dict)."""
    opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        # Best video+audio merged to mp4 when ffmpeg exists, otherwise best single file
        "format": "bv*+ba/b" if _FFMPEG else "b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # yt-dlp raises DownloadError/ExtractorError wrapping the cause
        raise _classify(url, exc) from exc

    if not info:
        raise SkipError(f"yt-dlp returned no info for {url}")
    if info.get("_type") == "playlist":
        info = next((e for e in info.get("entries") or [] if e), None)
        if not info:
            raise SkipError(f"no video found at {url}")

    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else None
    if not path or not path.exists():
        candidates = sorted(p for p in out_dir.glob(f"{info.get('id', '*')}.*")
                            if not p.name.endswith((".part", ".ytdl")))
        if not candidates:
            raise RetryableError(f"yt-dlp finished but produced no file for {url}")
        path = candidates[0]
    if path.stat().st_size == 0:
        raise RetryableError(f"downloaded file is empty for {url}")
    return path, info
