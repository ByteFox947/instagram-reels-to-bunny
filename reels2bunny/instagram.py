"""Minimal client for the official Instagram Graph API (your own account's media)."""

from typing import Iterator

import requests

from .config import Config
from .errors import FatalError, RetryableError

MEDIA_FIELDS = ",".join(
    [
        "id",
        "caption",
        "media_type",
        "media_product_type",
        "media_url",
        "thumbnail_url",
        "permalink",
        "timestamp",
        "username",
        "like_count",
        "comments_count",
    ]
)


class InstagramClient:
    def __init__(self, cfg: Config, session: requests.Session):
        self.cfg = cfg
        self.session = session

    def _url(self, path: str) -> str:
        parts = [self.cfg.ig_api_base]
        if self.cfg.ig_api_version:
            parts.append(self.cfg.ig_api_version)
        parts.append(path.lstrip("/"))
        return "/".join(parts)

    def _get(self, url: str, params: dict | None = None) -> dict:
        try:
            resp = self.session.get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            raise RetryableError(f"Instagram API network error: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise RetryableError(f"Instagram API: non-JSON response (HTTP {resp.status_code})")
        if resp.status_code < 400 and "error" not in data:
            return data
        err = data.get("error", {}) if isinstance(data, dict) else {}
        msg = f"Instagram API error {resp.status_code}: {err.get('message', data)}"
        if err.get("code") == 190 or resp.status_code in (401, 403):
            raise FatalError(msg + "\n  -> access token invalid/expired; create a new one "
                             "or run `refresh-token`")
        if resp.status_code == 429 or resp.status_code >= 500 or err.get("code") in (4, 17, 32):
            raise RetryableError(msg)  # rate limited / server error
        raise FatalError(msg)

    def iter_media(self) -> Iterator[dict]:
        """Yield every media object on the account, following pagination."""
        url = self._url(f"{self.cfg.ig_user_id}/media")
        params: dict | None = {
            "fields": MEDIA_FIELDS,
            "limit": 50,
            "access_token": self.cfg.ig_access_token,
        }
        while url:
            data = self._get(url, params)
            yield from data.get("data", [])
            # `next` already contains all query params (incl. token)
            url = data.get("paging", {}).get("next")
            params = None

    def iter_reels(self) -> Iterator[dict]:
        for m in self.iter_media():
            product = m.get("media_product_type")
            if product == "REELS" or (product is None and m.get("media_type") == "VIDEO"):
                yield m

    def refresh_token(self) -> dict:
        """Refresh a long-lived Instagram Login token (valid 60 days)."""
        return self._get(
            f"{self.cfg.ig_api_base}/refresh_access_token",
            {"grant_type": "ig_refresh_token", "access_token": self.cfg.ig_access_token},
        )
