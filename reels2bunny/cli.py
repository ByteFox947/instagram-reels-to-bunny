"""CLI: download every reel of an Instagram profile with yt-dlp and upload to Bunny Storage."""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .bunny import REGION_TO_PREFIX, BunnyStorage, create_storage_zone
from .config import Config
from .errors import FatalError, Reels2BunnyError
from .http import make_session
from .instagram import InstagramClient
from .profile import ProfileReels
from .sync import Options, Pipeline, reel_name

log = logging.getLogger("reels2bunny")


def _collect_reels(cfg: Config, args) -> tuple[str, list[dict], str | None]:
    """Return (folder_name, reels, listing_error).

    If listing breaks part-way (rate limit, cookies expire...) we keep what was found
    so far and report the error, instead of throwing away the whole run.
    """
    session = make_session()
    reels: list[dict] = []
    if args.source == "profile":
        folder = args.username
        source = ProfileReels(session, args.cookies, delay=args.delay).iter_reels(args.username)
    else:
        cfg.require("ig_access_token")
        folder = None
        source = InstagramClient(cfg, session).iter_reels()

    try:
        for m in source:
            if args.source == "graph":
                permalink = m.get("permalink") or ""
                folder = folder or m.get("username")
                m = {"shortcode": permalink.rstrip("/").rsplit("/", 1)[-1] or m["id"],
                     "id": m["id"], "url": permalink, "timestamp": m.get("timestamp"),
                     "caption": m.get("caption"), "username": m.get("username")}
            reels.append(m)
            if len(reels) % 100 == 0:
                log.info("  ...%d reels found so far", len(reels))
    except Reels2BunnyError as exc:
        if not reels:
            raise
        return folder or "me", reels, str(exc)
    return folder or "me", reels, None


def _write_failed_report(username: str, failed: list[tuple[dict, str]]) -> Path:
    path = Path(f"failed-{username}-{datetime.now():%Y%m%d-%H%M%S}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        for reel, err in failed:
            fh.write(f"{reel['url']}\t{err.splitlines()[0]}\n")
    return path


def cmd_sync(cfg: Config, args) -> int:
    if args.source == "profile" and not args.username:
        raise FatalError("--username is required with --source profile")
    if args.username:
        args.username = args.username.strip().lstrip("@").strip("/")
    if not args.dry_run:
        cfg.require("bunny_zone", "bunny_password")

    log.info("Finding reels (%s)...", args.source)
    user_folder, reels, listing_error = _collect_reels(cfg, args)
    if listing_error:
        log.warning("Listing stopped early after %d reels: %s", len(reels), listing_error)
        log.warning("Continuing with the reels found; run again later to get the rest.")
    if args.limit:
        reels = reels[: args.limit]
    log.info("Found %d reels", len(reels))

    folder = "/".join(p for p in (cfg.bunny_base_path, user_folder) if p)
    if args.dry_run:
        for r in reels:
            print(f"{reel_name(r)}  {r['url']}")
        return 1 if listing_error else 0

    storage = BunnyStorage(cfg.bunny_zone, cfg.bunny_password, cfg.bunny_region, make_session())
    existing = storage.list_files(folder)  # also validates Bunny credentials up front
    todo = [r for r in reels if args.force or f"{reel_name(r)}.json" not in existing]
    skipped = len(reels) - len(todo)
    log.info("%d already in Bunny, %d to upload -> %s/%s/ (batches of %d, %d workers)",
             skipped, len(todo), cfg.bunny_zone, folder, args.batch_size, args.workers)
    if not todo:
        log.info("Nothing to do - everything is already backed up.")
        return 1 if listing_error else 0

    opts = Options(folder=folder, cookies=args.cookies, workers=args.workers,
                   batch_size=args.batch_size, batch_pause=args.batch_pause,
                   retries=args.retries, work_dir=args.work_dir)
    res = Pipeline(storage, opts).run(todo)

    log.info("══════════ Summary ══════════")
    log.info("Uploaded:        %d", len(res.uploaded))
    log.info("Already in Bunny: %d", skipped)
    log.info("Failed:          %d", len(res.failed))
    if res.failed:
        report = _write_failed_report(user_folder, res.failed)
        log.info("Failed list:     %s  (run the same command again to retry them)", report)
    if listing_error:
        log.warning("Listing was incomplete - run again later to pick up remaining reels.")
    if res.interrupted:
        log.warning("Interrupted by user. Run again to continue where it stopped.")
        return 130
    if res.fatal:
        log.error("Stopped early: %s", res.fatal)
        return 2
    return 1 if (res.failed or listing_error) else 0


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


def _positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


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
    s.add_argument("--batch-size", type=_positive_int, default=20,
                   help="Reels per batch; temp files are cleaned after each batch (default 20)")
    s.add_argument("--batch-pause", type=float, default=10,
                   help="Seconds to wait between batches (default 10)")
    s.add_argument("--workers", type=_positive_int, default=3,
                   help="Parallel downloads/uploads inside a batch (default 3)")
    s.add_argument("--retries", type=int, default=3,
                   help="Retries per download/upload on temporary errors (default 3)")
    s.add_argument("--limit", type=int, default=0, help="Only process the N newest reels")
    s.add_argument("--delay", type=float, default=1.5, help="Seconds between listing pages")
    s.add_argument("--work-dir", type=Path, default=None,
                   help="Where to put temporary downloads (default: system temp)")
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
    try:
        return args.func(Config.from_env(), args)
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130
    except Reels2BunnyError as exc:  # expected problems: clean message, no traceback
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
