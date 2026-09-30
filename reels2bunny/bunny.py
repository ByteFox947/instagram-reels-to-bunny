"""Bunny.net Storage API + zone management helpers."""

import hashlib
from pathlib import Path
from urllib.parse import quote

import requests

from .errors import FatalError, Reels2BunnyError, RetryableError

BUNNY_API = "https://api.bunny.net"

# Main-region codes accepted by POST /storagezone
ZONE_REGIONS = {"DE", "UK", "NY", "LA", "SG", "SE", "BR", "JH", "SYD"}
# Region code -> storage endpoint prefix
REGION_TO_PREFIX = {
    "DE": "",
    "UK": "uk",
    "NY": "ny",
    "LA": "la",
    "SG": "sg",
    "SE": "se",
    "BR": "br",
    "JH": "jh",
    "SYD": "syd",
}


def storage_host(region_prefix: str) -> str:
    region_prefix = (region_prefix or "").lower()
    if region_prefix in ("", "de", "falkenstein"):
        return "storage.bunnycdn.com"
    return f"{region_prefix}.storage.bunnycdn.com"


def _raise_for(resp: requests.Response, what: str) -> None:
    """Turn a non-2xx Bunny response into the right error type."""
    code = resp.status_code
    if 200 <= code < 300:
        return
    body = (resp.text or "")[:300]
    msg = f"{what}: HTTP {code} {body}".strip()
    if code in (401, 403):
        raise FatalError(f"{msg}\n  -> check BUNNY_STORAGE_ZONE, BUNNY_STORAGE_PASSWORD "
                         "and BUNNY_STORAGE_REGION (must match the zone's main region)")
    if code == 429 or code >= 500 or (code == 400 and "checksum" in body.lower()):
        raise RetryableError(msg)  # rate limit, server error or corrupted upload
    raise Reels2BunnyError(msg)


class BunnyStorage:
    def __init__(self, zone: str, password: str, region: str, session: requests.Session):
        self.zone = zone
        self.base = f"https://{storage_host(region)}/{quote(zone)}"
        self.session = session
        self.headers = {"AccessKey": password}

    def _url(self, path: str) -> str:
        return f"{self.base}/{quote(path.strip('/'))}"

    def _request(self, method: str, url: str, what: str, **kwargs) -> requests.Response:
        try:
            return self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:  # DNS, reset, timeout...
            raise RetryableError(f"{what}: network error: {exc}") from exc

    def list_files(self, folder: str) -> set[str]:
        """Return names of files in a folder (empty set if the folder doesn't exist)."""
        return {e["ObjectName"] for e in self.list_entries(folder)}

    def list_entries(self, folder: str) -> list[dict]:
        """Return Bunny's file entries (ObjectName, Length, DateCreated...) in a folder."""
        what = f"list Bunny folder '{folder}/'"
        resp = self._request("GET", self._url(folder) + "/", what,
                             headers={**self.headers, "Accept": "application/json"}, timeout=60)
        if resp.status_code == 404:
            return []
        _raise_for(resp, what)
        try:
            return [i for i in resp.json() if not i.get("IsDirectory") and i["ObjectName"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise RetryableError(f"{what}: unexpected response") from exc

    def read_bytes(self, remote_path: str) -> bytes | None:
        """Download a (small) file from storage; None if it doesn't exist."""
        what = f"read {remote_path}"
        resp = self._request("GET", self._url(remote_path), what,
                             headers=self.headers, timeout=60)
        if resp.status_code == 404:
            return None
        _raise_for(resp, what)
        return resp.content

    def upload_file(self, remote_path: str, local_path: Path, sha256_hex: str | None = None,
                    content_type: str = "application/octet-stream") -> None:
        """Upload a file. The file is reopened on every call, so it is safe to retry."""
        headers = {**self.headers, "Content-Type": content_type}
        if sha256_hex:
            headers["Checksum"] = sha256_hex.upper()  # Bunny rejects corrupted uploads
        what = f"upload {remote_path}"
        with open(local_path, "rb") as fh:
            resp = self._request("PUT", self._url(remote_path), what,
                                 data=fh, headers=headers, timeout=(30, 900))
        _raise_for(resp, what)

    def upload_bytes(self, remote_path: str, data: bytes,
                     content_type: str = "application/octet-stream") -> None:
        headers = {
            **self.headers,
            "Content-Type": content_type,
            "Checksum": hashlib.sha256(data).hexdigest().upper(),
        }
        what = f"upload {remote_path}"
        resp = self._request("PUT", self._url(remote_path), what,
                             data=data, headers=headers, timeout=(30, 120))
        _raise_for(resp, what)


def create_storage_zone(api_key: str, name: str, region: str = "DE",
                        replication: list[str] | None = None, ssd: bool = False,
                        session: requests.Session | None = None) -> dict:
    """Create a new storage zone. Returns the zone JSON (includes the storage Password)."""
    region = region.upper()
    replication = [r.upper() for r in (replication or [])]
    for r in [region, *replication]:
        if r not in ZONE_REGIONS:
            raise FatalError(f"Unknown region '{r}'. Valid: {', '.join(sorted(ZONE_REGIONS))}")
    s = session or requests.Session()
    try:
        resp = s.post(
            f"{BUNNY_API}/storagezone",
            headers={"AccessKey": api_key, "Content-Type": "application/json",
                     "Accept": "application/json"},
            json={
                "Name": name,
                "Region": region,
                "ReplicationRegions": replication,
                "ZoneTier": 1 if ssd else 0,  # 0 = Standard (HDD), 1 = Edge (SSD)
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        raise FatalError(f"Create storage zone: network error: {exc}") from exc
    if resp.status_code in (401, 403):
        raise FatalError("Create storage zone: unauthorized - check BUNNY_API_KEY "
                         "(account API key, not the storage zone password)")
    if resp.status_code >= 400:
        raise FatalError(f"Create storage zone failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()
