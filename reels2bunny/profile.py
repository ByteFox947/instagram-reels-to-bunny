"""Discover every reel on an Instagram profile using the instagram.com web endpoints.

yt-dlp can download a single reel reliably, but its "whole profile" extractor is
marked broken, so we enumerate the reels ourselves and hand each URL to yt-dlp.

These are the same (unofficial) endpoints the instagram.com website uses, so they
need a logged-in session (cookies.txt exported from your browser) and may change.
"""

import http.cookiejar
import logging
import time
from typing import Iterator

import requests

from .errors import FatalError, RetryableError, SkipError

log = logging.getLogger("reels2bunny")

IG_WEB = "https://www.instagram.com"
# Public app id used by the instagram.com web client
IG_APP_ID = "936619743392459"


def load_cookies(session: requests.Session, cookies_file: str) -> None:
    jar = http.cookiejar.MozillaCookieJar(cookies_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except FileNotFoundError:
        raise FatalError(f"Cookies file not found: {cookies_file}")
    except (http.cookiejar.LoadError, OSError) as exc:
        raise FatalError(f"Can't read cookies file {cookies_file} (must be Netscape "
                         f"cookies.txt format): {exc}")
    session.cookies.update(jar)


class ProfileReels:
    def __init__(self, session: requests.Session, cookies_file: str | None,
                 delay: float = 1.5, retries: int = 5):
        self.session = session
        self.delay = delay
        self.retries = retries
        self._uid: dict[str, str] = {}
        if cookies_file:
            load_cookies(session, cookies_file)
        else:
            log.warning("No --cookies given: Instagram usually requires login to list reels")
        self.headers = {
            "x-ig-app-id": IG_APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-csrftoken": session.cookies.get("csrftoken", domain=".instagram.com") or "",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

    def _call(self, method: str, url: str, what: str, **kwargs) -> dict:
        """Request with retry/backoff on rate limits and network errors."""
        for attempt in range(1, self.retries + 1):
            try:
                return self._once(method, url, what, **kwargs)
            except RetryableError as exc:
                if attempt == self.retries:
                    raise
                wait = min(300, self.delay * 2 ** attempt * 5)  # 15s, 30s, 60s, ...
                log.warning("%s (attempt %d/%d) - waiting %.0fs", exc, attempt,
                            self.retries, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _once(self, method: str, url: str, what: str, **kwargs) -> dict:
        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise RetryableError(f"{what}: network error: {exc}") from exc
        code = resp.status_code
        if code == 429:
            raise RetryableError(f"{what}: rate limited by Instagram")
        if code >= 500:
            raise RetryableError(f"{what}: Instagram server error {code}")
        if code in (401, 403) or "accounts/login" in (resp.url or ""):
            raise FatalError(f"{what}: not logged in or blocked (HTTP {code})\n"
                             "  -> export fresh cookies.txt from a logged-in browser")
        if code == 404:
            raise FatalError(f"{what}: not found (check the username)")
        try:
            data = resp.json()
        except ValueError:
            # HTML instead of JSON usually means a login wall / challenge page
            raise FatalError(f"{what}: Instagram returned a web page instead of data "
                             "(login/challenge required) -> refresh your cookies")
        if data.get("require_login") or data.get("message") == "checkpoint_required":
            raise FatalError(f"{what}: Instagram wants you to log in / confirm a checkpoint "
                             "in the browser, then export fresh cookies")
        if "wait a few minutes" in str(data.get("message", "")).lower():
            raise RetryableError(f"{what}: {data['message']}")
        if code >= 400 or data.get("status") == "fail":
            raise FatalError(f"{what}: HTTP {code} {data.get('message', '')}")
        return data

    def user_id(self, username: str) -> str:
        if username in self._uid:
            return self._uid[username]
        what = f"profile '{username}'"
        data = self._call("GET", f"{IG_WEB}/api/v1/users/web_profile_info/", what,
                          params={"username": username},
                          headers={**self.headers, "referer": f"{IG_WEB}/{username}/"})
        user = (data.get("data") or {}).get("user")
        if not user:
            raise FatalError(f"{what} not found")
        if user.get("is_private") and not user.get("followed_by_viewer"):
            raise FatalError(f"{what} is private and the logged-in account doesn't follow it")
        self._uid[username] = str(user["id"])
        return self._uid[username]

    def iter_reels(self, username: str) -> Iterator[dict]:
        """Yield {'shortcode','id','timestamp','caption','url'} for every reel on the profile."""
        uid = self.user_id(username)
        max_id = ""
        seen: set[str] = set()
        page = 0
        while True:
            page += 1
            form = {"target_user_id": uid, "page_size": "12", "include_feed_video": "true"}
            if max_id:
                form["max_id"] = max_id
            data = self._call("POST", f"{IG_WEB}/api/v1/clips/user/",
                              f"reels of '{username}' (page {page})", data=form,
                              headers={**self.headers, "referer": f"{IG_WEB}/{username}/reels/"})
            for item in data.get("items") or []:
                media = item.get("media") or item
                code = media.get("code")
                if not code or code in seen:
                    continue
                seen.add(code)
                caption = media.get("caption") or {}
                yield {
                    "kind": "reel",
                    "shortcode": code,
                    "id": str(media.get("pk") or media.get("id") or code),
                    "timestamp": media.get("taken_at"),
                    "caption": caption.get("text") if isinstance(caption, dict) else None,
                    "url": f"{IG_WEB}/reel/{code}/",
                    "username": username,
                }
            paging = data.get("paging_info") or {}
            max_id = paging.get("max_id") or ""
            if not paging.get("more_available") or not max_id:
                break
            time.sleep(self.delay)  # be gentle, avoid rate limits

    # ------------------------------------------------------------ feed posts
    @staticmethod
    def _best(candidates: list[dict] | None) -> dict | None:
        """Pick the largest image/video version."""
        cands = [c for c in candidates or [] if c.get("url")]
        if not cands:
            return None
        return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))

    def _media_item(self, m: dict) -> dict | None:
        """One photo or video (a single post, or one slide of a carousel)."""
        if m.get("media_type") == 2 or m.get("video_versions"):
            best = self._best(m.get("video_versions"))
            kind = "video"
        else:
            best = self._best((m.get("image_versions2") or {}).get("candidates"))
            kind = "image"
        if not best:
            return None
        return {"type": kind, "url": best["url"], "width": best.get("width"),
                "height": best.get("height")}

    def parse_post(self, media: dict, username: str) -> dict | None:
        code = media.get("code")
        if not code:
            return None
        caption = media.get("caption") or {}
        base = {
            "shortcode": code,
            "id": str(media.get("pk") or media.get("id") or code),
            "timestamp": media.get("taken_at"),
            "caption": caption.get("text") if isinstance(caption, dict) else None,
            "like_count": media.get("like_count"),
            "comment_count": media.get("comment_count"),
            "username": username,
        }
        if media.get("product_type") == "clips":  # a reel that also shows in the grid
            return {**base, "kind": "reel", "url": f"{IG_WEB}/reel/{code}/"}
        slides = media.get("carousel_media") or [media]
        items = [i for i in (self._media_item(s) for s in slides) if i]
        if not items:
            return None
        return {**base, "kind": "post", "url": f"{IG_WEB}/p/{code}/",
                "post_type": "carousel" if media.get("media_type") == 8 else items[0]["type"],
                "items": items}

    def iter_posts(self, username: str) -> Iterator[dict]:
        """Yield every post of the profile grid (photos, carousels, videos, grid reels)."""
        uid = self.user_id(username)
        max_id = ""
        seen: set[str] = set()
        page = 0
        while True:
            page += 1
            params = {"count": "12"}
            if max_id:
                params["max_id"] = max_id
            data = self._call("GET", f"{IG_WEB}/api/v1/feed/user/{uid}/",
                              f"posts of '{username}' (page {page})", params=params,
                              headers={**self.headers, "referer": f"{IG_WEB}/{username}/"})
            for media in data.get("items") or []:
                post = self.parse_post(media, username)
                if post and post["shortcode"] not in seen:
                    seen.add(post["shortcode"])
                    yield post
            max_id = data.get("next_max_id") or ""
            if not data.get("more_available") or not max_id:
                break
            time.sleep(self.delay)

    def refresh_post(self, post: dict) -> dict:
        """Get fresh media URLs for a post (Instagram's CDN links expire after a while)."""
        data = self._call("GET", f"{IG_WEB}/api/v1/media/{post['id']}/info/",
                          f"refresh post {post['shortcode']}", headers=self.headers)
        media = (data.get("items") or [None])[0]
        fresh = self.parse_post(media, post["username"]) if media else None
        if not fresh or fresh["kind"] != "post":
            raise SkipError(f"post {post['shortcode']} is no longer available")
        return fresh
