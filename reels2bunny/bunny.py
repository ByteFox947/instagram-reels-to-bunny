"""Bunny.net Storage API + zone management helpers."""

import hashlib
from pathlib import Path
from urllib.parse import quote

import requests

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


class BunnyError(RuntimeError):
    pass


def storage_host(region_prefix: str) -> str:
    region_prefix = (region_prefix or "").lower()
    if region_prefix in ("", "de", "falkenstein"):
        return "storage.bunnycdn.com"
    return f"{region_prefix}.storage.bunnycdn.com"


class BunnyStorage:
    def __init__(self, zone: str, password: str, region: str, session: requests.Session):
        self.zone = zone
        self.base = f"https://{storage_host(region)}/{quote(zone)}"
        self.session = session
        self.headers = {"AccessKey": password}

    def _url(self, path: str) -> str:
        return f"{self.base}/{quote(path.strip('/'))}"

    def list_files(self, folder: str) -> set[str]:
        """Return names of files in a folder (empty set if the folder doesn't exist)."""
        resp = self.session.get(
            self._url(folder) + "/",
            headers={**self.headers, "Accept": "application/json"},
            timeout=60,
        )
        if resp.status_code == 404:
            return set()
        if resp.status_code == 401:
            raise BunnyError("Bunny Storage: unauthorized - check zone name/password/region")
        resp.raise_for_status()
        return {item["ObjectName"] for item in resp.json() if not item.get("IsDirectory")}

    def upload_file(self, remote_path: str, local_path: Path, sha256_hex: str | None = None,
                    content_type: str = "application/octet-stream") -> None:
        headers = {**self.headers, "Content-Type": content_type}
        if sha256_hex:
            # Bunny verifies the upload against this checksum
            headers["Checksum"] = sha256_hex.upper()
        with open(local_path, "rb") as fh:
            resp = self.session.put(self._url(remote_path), data=fh, headers=headers, timeout=600)
        if resp.status_code not in (200, 201):
            raise BunnyError(f"Upload failed ({resp.status_code}) for {remote_path}: {resp.text}")

    def upload_bytes(self, remote_path: str, data: bytes,
                     content_type: str = "application/octet-stream") -> None:
        headers = {
            **self.headers,
            "Content-Type": content_type,
            "Checksum": hashlib.sha256(data).hexdigest().upper(),
        }
        resp = self.session.put(self._url(remote_path), data=data, headers=headers, timeout=120)
        if resp.status_code not in (200, 201):
            raise BunnyError(f"Upload failed ({resp.status_code}) for {remote_path}: {resp.text}")


def create_storage_zone(api_key: str, name: str, region: str = "DE",
                        replication: list[str] | None = None, ssd: bool = False,
                        session: requests.Session | None = None) -> dict:
    """Create a new storage zone. Returns the zone JSON (includes the storage Password)."""
    region = region.upper()
    replication = [r.upper() for r in (replication or [])]
    for r in [region, *replication]:
        if r not in ZONE_REGIONS:
            raise BunnyError(f"Unknown region '{r}'. Valid: {', '.join(sorted(ZONE_REGIONS))}")
    s = session or requests.Session()
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
    if resp.status_code >= 400:
        raise BunnyError(f"Create storage zone failed ({resp.status_code}): {resp.text}")
    return resp.json()
