"""Discover every reel on an Instagram profile using the instagram.com web endpoints.

yt-dlp can download a single reel reliably, but its "whole profile" extractor is
marked broken, so we enumerate the reels ourselves and hand each URL to yt-dlp.

These are the same (unofficial) endpoints the instagram.com website uses, so they
need a logged-in session (cookies.txt exported from your browser) and may change.
"""

import http.cookiejar
import time
from typing import Iterator

import requests

IG_WEB = "https://www.instagram.com"
# Public app id used by the instagram.com web client
IG_APP_ID = "936619743392459"


class ProfileError(RuntimeError):
    pass


def load_cookies(session: requests.Session, cookies_file: str) -> None:
    jar = http.cookiejar.MozillaCookieJar(cookies_file)
    jar.load(ignore_discard=True, ignore_expires=True)
    session.cookies.update(jar)


class ProfileReels:
    def __init__(self, session: requests.Session, cookies_file: str | None, delay: float = 1.5):
        self.session = session
        self.delay = delay
        if cookies_file:
            load_cookies(session, cookies_file)
        self.headers = {
            "x-ig-app-id": IG_APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-csrftoken": session.cookies.get("csrftoken", domain=".instagram.com") or "",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

    def _check(self, resp: requests.Response, what: str) -> dict:
        if resp.status_code in (401, 403) or "login" in resp.url:
            raise ProfileError(f"{what}: not logged in / blocked. Export fresh cookies.txt.")
        if resp.status_code == 404:
            raise ProfileError(f"{what}: not found")
        try:
            data = resp.json()
        except ValueError:
            raise ProfileError(f"{what}: unexpected non-JSON response ({resp.status_code})")
        if resp.status_code >= 400 or data.get("status") == "fail":
            raise ProfileError(f"{what}: {resp.status_code} {data.get('message', '')}")
        return data

    def user_id(self, username: str) -> str:
        resp = self.session.get(
            f"{IG_WEB}/api/v1/users/web_profile_info/",
            params={"username": username},
            headers={**self.headers, "referer": f"{IG_WEB}/{username}/"},
            timeout=30,
        )
        data = self._check(resp, f"profile '{username}'")
        user = (data.get("data") or {}).get("user")
        if not user:
            raise ProfileError(f"profile '{username}' not found")
        if user.get("is_private") and not user.get("followed_by_viewer"):
            raise ProfileError(f"profile '{username}' is private and you don't follow it")
        return str(user["id"])

    def iter_reels(self, username: str) -> Iterator[dict]:
        """Yield {'shortcode','id','timestamp','caption','url'} for every reel on the profile."""
        uid = self.user_id(username)
        max_id = ""
        seen: set[str] = set()
        while True:
            form = {"target_user_id": uid, "page_size": "12", "include_feed_video": "true"}
            if max_id:
                form["max_id"] = max_id
            resp = self.session.post(
                f"{IG_WEB}/api/v1/clips/user/",
                data=form,
                headers={**self.headers, "referer": f"{IG_WEB}/{username}/reels/"},
                timeout=30,
            )
            data = self._check(resp, f"reels of '{username}'")
            for item in data.get("items", []):
                media = item.get("media") or item
                code = media.get("code")
                if not code or code in seen:
                    continue
                seen.add(code)
                caption = media.get("caption") or {}
                yield {
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
