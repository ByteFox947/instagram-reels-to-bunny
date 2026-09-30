"""Download a single reel with yt-dlp."""

import shutil
from pathlib import Path

from yt_dlp import YoutubeDL


class DownloadError(RuntimeError):
    pass


def download_reel(url: str, out_dir: Path, cookies_file: str | None = None) -> tuple[Path, dict]:
    """Download `url` into out_dir. Returns (file_path, yt-dlp info dict)."""
    has_ffmpeg = shutil.which("ffmpeg") is not None
    opts = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        # Best video+audio merged to mp4 when ffmpeg exists, otherwise best single file
        "format": "bv*+ba/b" if has_ffmpeg else "b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "overwrites": True,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # yt-dlp raises its own DownloadError/ExtractorError
        raise DownloadError(f"yt-dlp failed for {url}: {exc}") from exc

    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)

    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads else None
    if not path or not path.exists():
        candidates = sorted(out_dir.glob(f"{info.get('id', '*')}.*"))
        if not candidates:
            raise DownloadError(f"yt-dlp produced no file for {url}")
        path = candidates[0]
    return path, info
