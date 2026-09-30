"""Publish videos stored in Bunny Storage to Instagram as Reels (official Graph API).

Per video (same flow as Instagram's Content Publishing API):
    1. POST /{ig-user-id}/media        media_type=REELS, video_url=<Bunny CDN URL>
    2. poll GET /{container-id}         until status_code == FINISHED
    3. POST /{ig-user-id}/media_publish creation_id=<container-id>
    4. GET /{media-id}?fields=permalink

Batches: videos are published --batch-size at a time (--workers in parallel), with
--batch-pause between batches. Before every batch the account's 24h publishing quota
(content_publishing_limit) is checked, and the batch is shrunk / the run stops when
the quota is used up.

Duplicate protection: every step is written to the history file. A container id is
saved as soon as it's created and reused on the next run. Publishing is never retried
blindly; the container is checked first, and if it's already PUBLISHED, it counts as done.
"""

import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import quote

import requests

from .errors import FatalError, Reels2BunnyError, RetryableError, SkipError

log = logging.getLogger("reels2bunny")
T = TypeVar("T")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}  # formats Instagram accepts for Reels
CAPTION_MAX = 2200
CONTAINER_TTL = 23 * 3600  # containers expire after 24h; don't reuse older ones


class QuotaReached(FatalError):
    """The account hit Instagram's 24h API publishing limit."""


class Stopped(Exception):
    pass


# --------------------------------------------------------------------------- API
class InstagramPublisher:
    def __init__(self, token: str, user_id: str, api_base: str, api_version: str,
                 session: requests.Session, poll_interval: float = 6,
                 max_wait: float = 600):
        self.token = token
        self.user_id = user_id
        self.base = "/".join(p for p in (api_base.rstrip("/"), api_version) if p)
        self.session = session
        self.poll_interval = poll_interval
        self.max_wait = max_wait

    # ---- low level
    def _call(self, method: str, path: str, what: str, **params) -> dict:
        params["access_token"] = self.token
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            if method == "GET":
                resp = self.session.request("GET", url, params=params, timeout=45)
            else:
                resp = self.session.request("POST", url, data=params, timeout=90)
        except requests.RequestException as exc:
            raise RetryableError(f"{what}: network error: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            if resp.status_code >= 500:
                raise RetryableError(f"{what}: Instagram server error {resp.status_code}")
            raise RetryableError(f"{what}: non-JSON response (HTTP {resp.status_code})")
        if resp.status_code < 400 and not (isinstance(data, dict) and "error" in data):
            return data
        raise self._classify(what, resp.status_code, data.get("error", {}) if
                             isinstance(data, dict) else {})

    @staticmethod
    def _classify(what: str, status: int, err: dict) -> Exception:
        code, sub = err.get("code"), err.get("error_subcode")
        text = err.get("error_user_msg") or err.get("message") or "unknown error"
        msg = f"{what}: {text} (HTTP {status}, code {code}" + (f"/{sub})" if sub else ")")
        if code == 190:
            return FatalError(msg + "\n  -> access token invalid/expired: create a new one "
                              "or run `refresh-token`")
        if code in (10, 200) or (code and 200 <= code < 300):
            return FatalError(msg + "\n  -> the token is missing the content publishing "
                              "permission (instagram_business_content_publish)")
        if sub == 2207042 or (code == 9 and "limit" in text.lower()):
            return QuotaReached(msg + "\n  -> 24h publishing limit reached; run again later")
        if (err.get("is_transient") or code in (1, 2, 4, 17, 32, 341, 613)
                or status == 429 or status >= 500):
            return RetryableError(msg)
        return SkipError(msg)  # bad video / URL / caption: this video can't be published

    # ---- account
    def resolve_user(self) -> dict:
        """Resolve 'me' to the numeric IG account id and return account info."""
        if self.user_id in ("", "me"):
            data = self._call("GET", "me", "look up Instagram account",
                              fields="user_id,id,username,account_type")
            self.user_id = str(data.get("user_id") or data["id"])
            return data
        return self._call("GET", self.user_id, "look up Instagram account",
                          fields="id,username")

    def quota_left(self) -> tuple[int, int] | None:
        """(remaining, total) posts for the rolling 24h window, or None if unknown."""
        try:
            data = self._call("GET", f"{self.user_id}/content_publishing_limit",
                              "check publishing quota", fields="config,quota_usage")
            row = (data.get("data") or [{}])[0]
            total = int((row.get("config") or {}).get("quota_total") or 0)
            used = int(row.get("quota_usage") or 0)
            return (max(0, total - used), total) if total else None
        except (RetryableError, SkipError) as exc:
            log.warning("Couldn't check publishing quota (%s) - continuing", exc)
            return None

    # ---- publishing steps
    def create_container(self, video_url: str, caption: str, share_to_feed: bool,
                         what: str) -> str:
        data = self._call("POST", f"{self.user_id}/media", f"{what}: create container",
                          media_type="REELS", video_url=video_url, caption=caption,
                          share_to_feed="true" if share_to_feed else "false")
        if not data.get("id"):
            raise RetryableError(f"{what}: create container returned no id: {data}")
        return str(data["id"])

    def container_status(self, container_id: str, what: str) -> tuple[str, str]:
        data = self._call("GET", container_id, f"{what}: container status",
                          fields="status_code,status")
        return data.get("status_code") or "UNKNOWN", data.get("status") or ""

    def wait_until_ready(self, container_id: str, what: str, stop: threading.Event) -> str:
        """Wait for the container. Returns FINISHED or PUBLISHED."""
        start = time.monotonic()
        errors = 0
        while True:
            try:
                code, status = self.container_status(container_id, what)
                errors = 0
            except RetryableError as exc:  # a failed poll isn't a failed upload
                errors += 1
                if errors >= 5:
                    raise
                log.warning("  %s", exc)
                code, status = "IN_PROGRESS", ""
            elapsed = time.monotonic() - start
            if code in ("FINISHED", "PUBLISHED"):
                log.info("  [%s] processed by Instagram in %.0fs", what, elapsed)
                return code
            if code == "ERROR":
                raise SkipError(f"{what}: Instagram couldn't process the video: {status}")
            if code == "EXPIRED":  # a new container is created on the next run
                raise SkipError(f"{what}: container expired before publishing")
            if elapsed > self.max_wait:
                raise RetryableError(f"{what}: still processing after {self.max_wait:.0f}s")
            log.debug("  [%s] processing... %.0fs (%s)", what, elapsed, code)
            if stop.wait(self.poll_interval):
                raise Stopped()

    def publish(self, container_id: str, what: str) -> str | None:
        data = self._call("POST", f"{self.user_id}/media_publish", f"{what}: publish",
                          creation_id=container_id)
        return data.get("id")

    def permalink(self, media_id: str, stop: threading.Event) -> str | None:
        for _ in range(5):  # the permalink can take a few seconds to exist
            try:
                data = self._call("GET", media_id, "get permalink", fields="id,permalink")
                if data.get("permalink"):
                    return data["permalink"]
            except Reels2BunnyError:
                pass
            if stop.wait(2):
                break
        return None


# ----------------------------------------------------------------------- history
class History:
    """Thread-safe JSON record of every publish, keyed by the Bunny file path."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FatalError(f"Can't read history file {path}: {exc}\n  -> fix or move "
                                 "it (it prevents duplicate posts, so don't just delete it)")

    def get(self, key: str) -> dict:
        with self.lock:
            return dict(self.data.get(key) or {})

    def update(self, key: str, **fields) -> None:
        with self.lock:
            entry = self.data.setdefault(key, {})
            entry.update(fields, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            # atomic write: never leave a half-written history file
            fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def published(self) -> dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self.data.items() if v.get("status") == "PUBLISHED"}


# ---------------------------------------------------------------------- pipeline
@dataclass
class PublishOptions:
    workers: int = 2
    batch_size: int = 10
    batch_pause: float = 60
    retries: int = 3
    share_to_feed: bool = True


@dataclass
class PublishResult:
    published: list[dict] = field(default_factory=list)
    failed: list[tuple[dict, str]] = field(default_factory=list)
    not_started: int = 0
    fatal: str | None = None
    quota_stop: bool = False
    interrupted: bool = False


class PublishPipeline:
    def __init__(self, ig: InstagramPublisher, history: History, opts: PublishOptions):
        self.ig = ig
        self.history = history
        self.opts = opts
        self.stop = threading.Event()

    def _retry(self, what: str, fn: Callable[[], T]) -> T:
        attempts = self.opts.retries + 1
        for attempt in range(1, attempts + 1):
            if self.stop.is_set():
                raise Stopped()
            try:
                return fn()
            except RetryableError as exc:
                if attempt == attempts:
                    raise
                wait = min(180, 10 * 3 ** (attempt - 1))  # 10s, 30s, 90s...
                log.warning("  %s (attempt %d/%d) - retrying in %ds", exc, attempt,
                            attempts, wait)
                if self.stop.wait(wait):
                    raise Stopped()
        raise AssertionError("unreachable")

    # ---- one video
    def publish_video(self, video: dict) -> dict:
        key, name = video["key"], video["name"]
        prev = self.history.get(key)

        # 1. reuse a recent container from an earlier (interrupted) run if possible
        container_id = None
        if (not video.get("force") and prev.get("container_id")
                and time.time() - prev.get("container_at", 0) < CONTAINER_TTL):
            try:
                code, _ = self.ig.container_status(prev["container_id"], name)
                if code == "PUBLISHED":
                    log.info("  [%s] was already published in an earlier run", name)
                    self.history.update(key, status="PUBLISHED", account=video.get("account"),
                                        note="found published on rerun")
                    return {**video, "media_id": prev.get("media_id"),
                            "permalink": prev.get("permalink")}
                if code in ("FINISHED", "IN_PROGRESS"):
                    container_id = prev["container_id"]
                    log.info("  [%s] reusing container %s", name, container_id)
            except (RetryableError, SkipError):
                pass

        if not container_id:
            log.info("  [%s] creating Reel container (%.1f MB)", name,
                     video["size_bytes"] / 1_048_576)
            container_id = self._retry(
                f"{name}: create container",
                lambda: self.ig.create_container(video["cdn_url"], video["caption"],
                                                 self.opts.share_to_feed, name))
            self.history.update(key, status="CONTAINER_CREATED", container_id=container_id,
                                container_at=time.time(), cdn_url=video["cdn_url"], error=None)

        # 2. wait for Instagram to download and process the video
        state = self._retry(f"{name}: processing",
                            lambda: self.ig.wait_until_ready(container_id, name, self.stop))

        # 3. publish, checking the container before every retry so it's never posted twice
        media_id = None
        if state != "PUBLISHED":
            for attempt in range(1, self.opts.retries + 2):
                try:
                    media_id = self.ig.publish(container_id, name)
                    break
                except RetryableError as exc:
                    try:
                        code, _ = self.ig.container_status(container_id, name)
                    except Reels2BunnyError:
                        code = "UNKNOWN"
                    if code == "PUBLISHED":
                        log.info("  [%s] publish call errored but the Reel is live", name)
                        break
                    if attempt > self.opts.retries or code not in ("FINISHED", "UNKNOWN"):
                        raise
                    log.warning("  %s - retrying publish in 15s", exc)
                    if self.stop.wait(15):
                        raise Stopped()

        # 4. permalink
        permalink = self.ig.permalink(media_id, self.stop) if media_id else None
        self.history.update(key, status="PUBLISHED", account=video.get("account"),
                            media_id=media_id, permalink=permalink,
                            container_id=container_id, cdn_url=video["cdn_url"],
                            published_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return {**video, "media_id": media_id, "permalink": permalink}

    # ---- batches
    def run(self, videos: list[dict]) -> PublishResult:
        res = PublishResult()
        queue = list(videos)
        n = 0
        try:
            while queue and not self.stop.is_set():
                size = self.opts.batch_size
                quota = self.ig.quota_left()
                if quota is not None:
                    left, total = quota
                    log.info("Publishing quota: %d of %d left in the last 24h", left, total)
                    if left <= 0:
                        res.quota_stop = True
                        res.fatal = (f"Instagram's 24h publishing limit ({total} posts) is used "
                                     "up. Run the same command again later to continue.")
                        break
                    size = min(size, left)
                batch, queue = queue[:size], queue[size:]
                n += 1
                remaining_batches = -(-len(queue) // self.opts.batch_size)
                log.info("── Batch %d (%d videos, ~%d batches after this) ──", n, len(batch),
                         remaining_batches)
                ok_before = len(res.published)
                transient = self._run_batch(batch, res)
                ok = len(res.published) - ok_before
                log.info("Batch %d finished: %d published, %d failed | total %d/%d",
                         n, ok, len(batch) - ok, len(res.published) + len(res.failed),
                         len(videos))
                if res.fatal or res.interrupted:
                    break
                if ok == 0 and len(batch) >= 3 and transient == len(batch):
                    res.fatal = ("every video in the last batch failed with temporary errors - "
                                 "Instagram may be rate-limiting. Wait and run again.")
                    break
                if queue and self.opts.batch_pause > 0:
                    log.info("Pausing %.0fs before next batch...", self.opts.batch_pause)
                    if self.stop.wait(self.opts.batch_pause):
                        break
        except KeyboardInterrupt:
            res.interrupted = True
        res.not_started = len(videos) - len(res.published) - len(res.failed)
        return res

    def _run_batch(self, batch: list[dict], res: PublishResult) -> int:
        transient = 0
        pool = ThreadPoolExecutor(max_workers=max(1, min(self.opts.workers, len(batch))))
        futures = {pool.submit(self.publish_video, v): v for v in batch}
        try:
            for fut in as_completed(futures):
                v = futures[fut]
                try:
                    out = fut.result()
                    res.published.append(out)
                    log.info("  ✔ %s -> %s", v["name"], out.get("permalink") or
                             f"media {out.get('media_id') or '(id unknown)'}")
                    continue
                except Stopped:
                    err = "stopped before finishing"
                except FatalError as exc:
                    err = str(exc)
                    if not res.fatal:
                        res.fatal = err
                        res.quota_stop = isinstance(exc, QuotaReached)
                        log.error("  ✘ %s: %s", v["name"], exc)
                        log.error("Stopping: this needs fixing before continuing.")
                    self.stop.set()
                except RetryableError as exc:
                    transient += 1
                    err = str(exc)
                    log.error("  ✘ %s: gave up after retries: %s", v["name"], exc)
                except SkipError as exc:
                    err = str(exc)
                    log.error("  ✘ %s: skipped: %s", v["name"], exc)
                except Exception as exc:
                    err = f"unexpected {type(exc).__name__}: {exc}"
                    log.exception("  ✘ %s: unexpected error", v["name"])
                res.failed.append((v, err))
                if err != "stopped before finishing":
                    self.history.update(v["key"], status="FAILED", error=err.splitlines()[0])
        except KeyboardInterrupt:
            log.warning("Ctrl+C - stopping after the current Instagram calls...")
            res.interrupted = True
            self.stop.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            done = {v["key"] for v in res.published} | {v["key"] for v, _ in res.failed}
            for v in batch:
                if v["key"] not in done:
                    res.failed.append((v, "not started (run stopped)"))
        return transient


# ------------------------------------------------------------------- candidates
def cdn_url(host: str, path: str) -> str:
    return f"https://{host}/{quote(path.strip('/'))}"


def build_candidates(entries: list[dict], folder: str, cdn_host: str, zone: str) -> list[dict]:
    videos = []
    for e in entries:
        name = e["ObjectName"]
        if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        path = f"{folder}/{name}" if folder else name
        videos.append({
            "name": name,
            "path": path,
            "key": f"{zone}/{path}",
            "size_bytes": int(e.get("Length") or 0),
            "cdn_url": cdn_url(cdn_host, path),
        })
    return videos


def fit_caption(text: str) -> str:
    text = (text or "").strip()
    if len(text) > CAPTION_MAX:
        log.warning("Caption longer than %d chars - truncating", CAPTION_MAX)
        text = text[:CAPTION_MAX]
    if text.count("#") > 30:
        log.warning("Caption has more than 30 hashtags - Instagram may reject it")
    return text
