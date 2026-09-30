"""Batch pipeline: download reels with yt-dlp, upload to Bunny, clean up, repeat.

Flow for N reels with --batch-size B:
    for each batch of B reels:
        download + upload them in parallel (--workers)
        each reel's local file is deleted right after its upload
        the batch's temp folder is removed (even on errors / Ctrl+C)
        pause --batch-pause seconds before the next batch

So local disk usage is at most ~`workers` videos at any time, no matter how many reels.

Error handling per reel:
    RetryableError -> retried with backoff (--retries)
    SkipError      -> reel skipped, recorded as failed
    FatalError     -> the whole run stops (bad Bunny password, expired cookies, ...)
Files are uploaded first and the .json metadata last, so the .json acts as the
"done" marker: an interrupted reel is simply redone on the next run.
"""

import hashlib
import json
import logging
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from .bunny import BunnyStorage
from .downloader import download_reel
from .errors import FatalError, RetryableError, SkipError

log = logging.getLogger("reels2bunny")
T = TypeVar("T")


class Stopped(Exception):
    """Raised inside workers when the run is being stopped."""


@dataclass
class Options:
    folder: str
    cookies: str | None = None
    workers: int = 3
    batch_size: int = 20
    batch_pause: float = 10.0
    retries: int = 3
    work_dir: Path | None = None


@dataclass
class Result:
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[dict, str]] = field(default_factory=list)
    not_started: int = 0
    fatal: str | None = None
    interrupted: bool = False


def reel_name(reel: dict) -> str:
    ts = reel.get("timestamp")
    if ts is None:
        date = "unknown-date"
    elif isinstance(ts, (int, float)):
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        date = str(ts)[:10]  # ISO string from the Graph API
    return f"{date}_{reel['shortcode']}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Pipeline:
    def __init__(self, storage: BunnyStorage, opts: Options):
        self.storage = storage
        self.opts = opts
        self.stop = threading.Event()

    # ---------------------------------------------------------------- helpers
    def _retry(self, what: str, fn: Callable[[], T]) -> T:
        """Run fn, retrying RetryableError with backoff 5s, 15s, 45s..."""
        attempts = self.opts.retries + 1
        for attempt in range(1, attempts + 1):
            if self.stop.is_set():
                raise Stopped()
            try:
                return fn()
            except RetryableError as exc:
                if attempt == attempts:
                    raise
                wait = min(120, 5 * 3 ** (attempt - 1))
                log.warning("  %s failed (attempt %d/%d): %s - retrying in %ds",
                            what, attempt, attempts, exc, wait)
                if self.stop.wait(wait):  # wake up early if the run is stopping
                    raise Stopped()
        raise AssertionError("unreachable")

    # -------------------------------------------------------------- one reel
    def process_reel(self, reel: dict, batch_dir: Path) -> str:
        name = reel_name(reel)
        reel_dir = batch_dir / name
        reel_dir.mkdir(parents=True, exist_ok=True)
        try:
            path, info = self._retry(
                f"download {name}",
                lambda: download_reel(reel["url"], reel_dir, self.opts.cookies))
            ext = path.suffix or ".mp4"
            remote = f"{self.opts.folder}/{name}{ext}"
            checksum = _sha256(path)
            size = path.stat().st_size

            self._retry(f"upload {name}{ext}",
                        lambda: self.storage.upload_file(remote, path, checksum, "video/mp4"))

            meta = {
                **reel,
                "title": info.get("title"),
                "duration": info.get("duration"),
                "width": info.get("width"),
                "height": info.get("height"),
                "like_count": info.get("like_count"),
                "comment_count": info.get("comment_count"),
                "file": f"{name}{ext}",
                "size_bytes": size,
                "sha256": checksum,
                "backed_up_at": datetime.now(timezone.utc).isoformat(),
            }
            body = json.dumps(meta, ensure_ascii=False, indent=2).encode()
            self._retry(f"upload {name}.json",
                        lambda: self.storage.upload_bytes(f"{self.opts.folder}/{name}.json",
                                                          body, "application/json"))
            return f"{remote} ({size / 1_048_576:.1f} MB)"
        finally:
            shutil.rmtree(reel_dir, ignore_errors=True)  # free disk right away

    # ----------------------------------------------------------------- batches
    def run(self, reels: list[dict]) -> Result:
        res = Result()
        bs = max(1, self.opts.batch_size)
        batches = [reels[i:i + bs] for i in range(0, len(reels), bs)]
        work_root = Path(tempfile.mkdtemp(prefix="reels2bunny-", dir=self.opts.work_dir))
        done = 0
        try:
            for n, batch in enumerate(batches, 1):
                log.info("── Batch %d/%d (%d reels) ──", n, len(batches), len(batch))
                ok_before = len(res.uploaded)
                transient_fails = self._run_batch(batch, work_root / f"batch-{n}", res)
                done += len(batch)
                batch_ok = len(res.uploaded) - ok_before
                log.info("Batch %d/%d finished: %d ok, %d failed | total %d/%d",
                         n, len(batches), batch_ok, len(batch) - batch_ok, done, len(reels))

                if res.fatal or res.interrupted:
                    break
                # whole batch failed with network/rate-limit errors (not just deleted reels)
                if batch_ok == 0 and len(batch) >= 3 and transient_fails == len(batch):
                    res.fatal = ("every reel in the last batch failed - Instagram is probably "
                                 "blocking/rate-limiting you. Wait a while (or refresh cookies) "
                                 "and run again; finished reels are skipped automatically.")
                    break
                if n < len(batches) and self.opts.batch_pause > 0:
                    log.info("Pausing %.0fs before next batch...", self.opts.batch_pause)
                    if self.stop.wait(self.opts.batch_pause):
                        break
        except KeyboardInterrupt:
            res.interrupted = True
        finally:
            shutil.rmtree(work_root, ignore_errors=True)
        res.not_started = len(reels) - len(res.uploaded) - len(res.failed)
        return res

    def _run_batch(self, batch: list[dict], batch_dir: Path, res: Result) -> int:
        """Process one batch. Returns how many reels failed with retryable errors."""
        batch_dir.mkdir(parents=True, exist_ok=True)
        transient = 0
        pool = ThreadPoolExecutor(max_workers=max(1, self.opts.workers))
        futures = {pool.submit(self.process_reel, r, batch_dir): r for r in batch}
        try:
            for fut in as_completed(futures):
                reel = futures[fut]
                try:
                    log.info("  ✔ %s", fut.result())
                    res.uploaded.append(reel["shortcode"])
                except Stopped:
                    res.failed.append((reel, "stopped before finishing"))
                except FatalError as exc:
                    res.failed.append((reel, str(exc)))
                    if not res.fatal:
                        res.fatal = str(exc)
                        log.error("  ✘ %s: %s", reel["url"], exc)
                        log.error("Stopping: this error needs fixing before continuing.")
                    self.stop.set()
                except RetryableError as exc:
                    transient += 1
                    res.failed.append((reel, str(exc)))
                    log.error("  ✘ %s: gave up after retries: %s", reel["url"], exc)
                except SkipError as exc:
                    res.failed.append((reel, str(exc)))
                    log.error("  ✘ %s: skipped: %s", reel["url"], exc)
                except Exception as exc:  # bug or unexpected library error: keep going
                    res.failed.append((reel, f"unexpected {type(exc).__name__}: {exc}"))
                    log.exception("  ✘ %s: unexpected error", reel["url"])
        except KeyboardInterrupt:
            log.warning("Ctrl+C - waiting for running downloads to stop, then cleaning up...")
            res.interrupted = True
            self.stop.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            shutil.rmtree(batch_dir, ignore_errors=True)
            # reels whose futures were cancelled never reported back
            reported = set(res.uploaded) | {r["shortcode"] for r, _ in res.failed}
            for r in batch:
                if r["shortcode"] not in reported:
                    res.failed.append((r, "not started (run stopped)"))
        return transient
