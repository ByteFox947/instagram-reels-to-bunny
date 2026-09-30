"""CLI: download every reel of an Instagram profile with yt-dlp and upload to Bunny Storage."""

import argparse
import hashlib
import json
import logging
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .bunny import REGION_TO_PREFIX, BunnyStorage, create_storage_zone
from .config import Config
from .downloader import download_reel
from .http import make_session
from .instagram import InstagramClient
from .profile import ProfileReels

log = logging.getLogger("reels2bunny")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _date(ts) -> str:
    if ts is None:
        return "unknown-date"
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(ts)[:10]  # ISO string from Graph API


def _collect_reels(cfg: Config, args) -> tuple[str, list[dict]]:
    """Return (folder_name, reels). Each reel has: shortcode/id, url, timestamp, caption."""
    session = make_session()
    if args.source == "profile":
        finder = ProfileReels(session, args.cookies, delay=args.delay)
        return args.username, list(finder.iter_reels(args.username))

    cfg.require("ig_access_token")
    reels = []
    for m in InstagramClient(cfg, session).iter_reels():
        permalink = m.get("permalink") or ""
        code = permalink.rstrip("/").rsplit("/", 1)[-1] or m["id"]
        reels.append({
            "shortcode": code, "id": m["id"], "url": permalink,
            "timestamp": m.get("timestamp"), "caption": m.get("caption"),
            "username": m.get("username"),
        })
    return (reels[0]["username"] if reels and reels[0].get("username") else "me"), reels


def _process(reel: dict, folder: str, storage: BunnyStorage, cookies: str | None,
             tmp_root: Path) -> str:
    name = f"{_date(reel['timestamp'])}_{reel['shortcode']}"
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        path, info = download_reel(reel["url"], Path(tmp), cookies)
        ext = path.suffix or ".mp4"
        remote = f"{folder}/{name}{ext}"
        storage.upload_file(remote, path, _sha256(path), content_type="video/mp4")
        meta = {
            **reel,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "file": f"{name}{ext}",
            "size_bytes": path.stat().st_size,
            "backed_up_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.upload_bytes(f"{folder}/{name}.json",
                             json.dumps(meta, ensure_ascii=False, indent=2).encode(),
                             content_type="application/json")
    return remote


def cmd_sync(cfg: Config, args) -> int:
    if args.source == "profile" and not args.username:
        raise SystemExit("--username is required with --source profile")
    if not args.dry_run:
        cfg.require("bunny_zone", "bunny_password")

    log.info("Finding reels (%s)...", args.source)
    user_folder, reels = _collect_reels(cfg, args)
    if args.limit:
        reels = reels[: args.limit]
    log.info("Found %d reels", len(reels))

    folder = "/".join(p for p in (cfg.bunny_base_path, user_folder) if p)
    if args.dry_run:
        for r in reels:
            print(f"{_date(r['timestamp'])}  {r['url']}")
        return 0

    storage = BunnyStorage(cfg.bunny_zone, cfg.bunny_password, cfg.bunny_region, make_session())
    existing = storage.list_files(folder)
    todo = [r for r in reels
            if args.force or f"{_date(r['timestamp'])}_{r['shortcode']}.json" not in existing]
    log.info("%d already in Bunny, %d to upload -> %s/%s/",
             len(reels) - len(todo), len(todo), cfg.bunny_zone, folder)

    ok = failed = 0
    tmp_root = Path(tempfile.gettempdir())
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process, r, folder, storage, args.cookies, tmp_root): r
                   for r in todo}
        for fut in as_completed(futures):
            reel = futures[fut]
            try:
                log.info("✔ %s", fut.result())
                ok += 1
            except Exception as exc:
                log.error("✘ %s: %s", reel["url"], exc)
                failed += 1

    log.info("Done: %d uploaded, %d failed, %d skipped", ok, failed, len(reels) - len(todo))
    return 1 if failed else 0


def cmd_create_zone(cfg: Config, args) -> int:
    cfg.require("bunny_api_key")
    zone = create_storage_zone(cfg.bunny_api_key, args.name, args.region,
                               args.replicate, ssd=args.ssd)
    prefix = REGION_TO_PREFIX.get(args.region.upper(), "")
    print("Storage zone created. Add these to your .env:\n")
    print(f"BUNNY_STORAGE_ZONE={zone.get('Name', args.name)}")
    print(f"BUNNY_STORAGE_PASSWORD={zone.get('Password', '<see Bunny dashboard>')}")
    print(f"BUNNY_STORAGE_REGION={prefix}")
    return 0


def cmd_refresh_token(cfg: Config, args) -> int:
    cfg.require("ig_access_token")
    data = InstagramClient(cfg, make_session()).refresh_token()
    print(f"IG_ACCESS_TOKEN={data['access_token']}")
    print(f"# expires in {int(data.get('expires_in', 0)) // 86400} days")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reels2bunny", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="Download all reels and upload them to Bunny Storage")
    s.add_argument("--source", choices=["profile", "graph"], default="profile",
                   help="profile = find reels from a profile username (default); "
                        "graph = official Graph API for your own Business/Creator account")
    s.add_argument("-u", "--username", help="Instagram username (for --source profile)")
    s.add_argument("-c", "--cookies", help="Netscape cookies.txt exported from a logged-in browser")
    s.add_argument("--workers", type=int, default=3, help="Parallel downloads/uploads (default 3)")
    s.add_argument("--limit", type=int, default=0, help="Only process the N newest reels")
    s.add_argument("--delay", type=float, default=1.5, help="Seconds between listing pages")
    s.add_argument("--force", action="store_true", help="Re-upload even if already in Bunny")
    s.add_argument("--dry-run", action="store_true", help="Only list the reels found")
    s.set_defaults(func=cmd_sync)

    z = sub.add_parser("create-zone", help="Create a new Bunny storage zone")
    z.add_argument("--name", required=True, help="Zone name (globally unique)")
    z.add_argument("--region", default="DE", help="Main region: DE, UK, NY, LA, SG, SE, BR, JH, SYD")
    z.add_argument("--replicate", nargs="*", default=[], help="Replication regions, e.g. NY SG")
    z.add_argument("--ssd", action="store_true", help="Use Edge (SSD) tier")
    z.set_defaults(func=cmd_create_zone)

    r = sub.add_parser("refresh-token", help="Refresh a long-lived Instagram Graph API token")
    r.set_defaults(func=cmd_refresh_token)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return args.func(Config.from_env(), args)


if __name__ == "__main__":
    sys.exit(main())
