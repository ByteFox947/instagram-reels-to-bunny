"""CLI: download every reel of an Instagram profile with yt-dlp and upload to Bunny Storage."""

import argparse
import json
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
from .publisher import (History, InstagramPublisher, PublishOptions, PublishPipeline,
                        build_candidates, fit_caption)
from .sync import Options, Pipeline, reel_name

log = logging.getLogger("reels2bunny")


def _collect_items(cfg: Config, args, finder: ProfileReels | None
                   ) -> tuple[str, list[dict], str | None]:
    """Return (folder_name, items, listing_error). Items are reels and/or posts.

    If listing breaks part-way (rate limit, cookies expire...) we keep what was found
    so far and report the error, instead of throwing away the whole run.
    """
    items: list[dict] = []
    if args.source == "graph":
        if args.type != "reels":
            raise FatalError("--type posts/all needs --source profile (the default)")
        cfg.require("ig_access_token")
        folder = None
        try:
            for m in InstagramClient(cfg, make_session()).iter_reels():
                permalink = m.get("permalink") or ""
                folder = folder or m.get("username")
                items.append({"kind": "reel",
                              "shortcode": permalink.rstrip("/").rsplit("/", 1)[-1] or m["id"],
                              "id": m["id"], "url": permalink, "timestamp": m.get("timestamp"),
                              "caption": m.get("caption"), "username": m.get("username")})
        except Reels2BunnyError as exc:
            if not items:
                raise
            return folder or "me", items, str(exc)
        return folder or "me", items, None

    sources = []
    if args.type in ("reels", "all"):
        sources.append(("reels", finder.iter_reels))
    if args.type in ("posts", "all"):
        sources.append(("posts", finder.iter_posts))
    seen: set[str] = set()
    errors: list[str] = []
    for label, list_fn in sources:
        log.info("Listing %s of @%s...", label, args.username)
        try:
            for m in list_fn(args.username):
                # the posts grid also contains reels: skip them for --type posts,
                # and don't save a reel twice for --type all
                if m["shortcode"] in seen or (args.type == "posts" and m["kind"] == "reel"):
                    continue
                seen.add(m["shortcode"])
                items.append(m)
                if len(items) % 100 == 0:
                    log.info("  ...%d found so far", len(items))
        except Reels2BunnyError as exc:
            if isinstance(exc, FatalError) and not items:
                raise
            errors.append(f"{label}: {exc}")
    if errors and not items:
        raise FatalError("; ".join(errors))
    return args.username, items, "; ".join(errors) or None


def _describe(items: list[dict]) -> str:
    reels = sum(1 for i in items if i["kind"] == "reel")
    posts = [i for i in items if i["kind"] == "post"]
    photos = sum(1 for p in posts if p.get("post_type") == "image")
    carousels = sum(1 for p in posts if p.get("post_type") == "carousel")
    videos = len(posts) - photos - carousels
    parts = [f"{reels} reels"] if reels else []
    if posts:
        parts.append(f"{len(posts)} posts ({photos} photos, {carousels} carousels, "
                     f"{videos} videos)")
    return ", ".join(parts) or "nothing"


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

    session = make_session()
    finder = (ProfileReels(session, args.cookies, delay=args.delay)
              if args.source == "profile" else None)
    user_folder, reels, listing_error = _collect_items(cfg, args, finder)
    if listing_error:
        log.warning("Listing stopped early after %d items: %s", len(reels), listing_error)
        log.warning("Continuing with what was found; run again later to get the rest.")
    if args.limit:
        reels = reels[: args.limit]
    log.info("Found %s", _describe(reels))

    folder = "/".join(p for p in (cfg.bunny_base_path, user_folder) if p)
    if args.dry_run:
        for r in reels:
            kind = r.get("post_type", "reel") if r["kind"] == "post" else "reel"
            count = f" ({len(r['items'])} items)" if r["kind"] == "post" else ""
            cap = (r.get("caption") or "").replace("\n", " ")[:50]
            print(f"{reel_name(r)}  {kind:<8}{count:<11} {r['url']}  {cap}")
        return 1 if listing_error else 0

    storage = BunnyStorage(cfg.bunny_zone, cfg.bunny_password, cfg.bunny_region, session)
    existing = storage.list_files(folder)  # also validates Bunny credentials up front
    if any(r["kind"] == "post" for r in reels):
        existing_posts = storage.list_files(f"{folder}/posts")
    else:
        existing_posts = set()
    todo = [r for r in reels if args.force or f"{reel_name(r)}.json" not in
            (existing_posts if r["kind"] == "post" else existing)]
    skipped = len(reels) - len(todo)
    log.info("%d already in Bunny, %d to upload -> %s/%s/ (batches of %d, %d workers)",
             skipped, len(todo), cfg.bunny_zone, folder, args.batch_size, args.workers)
    if not todo:
        log.info("Nothing to do - everything is already backed up.")
        return 1 if listing_error else 0

    opts = Options(folder=folder, cookies=args.cookies, workers=args.workers,
                   batch_size=args.batch_size, batch_pause=args.batch_pause,
                   retries=args.retries, work_dir=args.work_dir)
    res = Pipeline(storage, opts, session=session,
                   refresh=finder.refresh_post if finder else None).run(todo)

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


def _caption_template(args) -> str:
    if args.caption_file:
        try:
            return Path(args.caption_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise FatalError(f"Can't read caption file {args.caption_file}: {exc}")
    if args.caption is not None:
        return args.caption
    return "{original}" if args.original_caption else ""


def _render_caption(template: str, video: dict, storage: BunnyStorage) -> str:
    """Fill {original} (caption saved by `sync` in the .json next to the video) and {name}."""
    text = template.replace("{name}", Path(video["name"]).stem)
    if "{original}" in text:
        original = ""
        raw = storage.read_bytes(video["path"].rsplit(".", 1)[0] + ".json")
        if raw:
            try:
                original = json.loads(raw).get("caption") or ""
            except ValueError:
                log.warning("  [%s] metadata .json is not valid JSON", video["name"])
        text = text.replace("{original}", original)
    return fit_caption(text)


def _check_cdn(session, video: dict) -> None:
    """Instagram fetches the video from the CDN URL, so it must be publicly reachable."""
    try:
        resp = session.head(video["cdn_url"], timeout=20, allow_redirects=True)
    except Exception as exc:  # network trouble here shouldn't block the run
        log.warning("Couldn't check CDN URL %s: %s", video["cdn_url"], exc)
        return
    if resp.status_code >= 400:
        raise FatalError(f"CDN URL not reachable (HTTP {resp.status_code}): {video['cdn_url']}\n"
                         "  -> BUNNY_CDN_HOSTNAME must be a pull zone linked to this storage "
                         "zone, without token authentication")


def _connect_account(cfg: Config, args, session) -> tuple[InstagramPublisher, str]:
    """The access token decides which Instagram account gets the posts."""
    ig = InstagramPublisher(cfg.ig_access_token, cfg.ig_user_id, cfg.ig_api_base,
                            cfg.ig_api_version, session, poll_interval=args.poll_interval,
                            max_wait=args.max_wait)
    account = ig.resolve_user()
    username = account.get("username") or "?"
    if args.expect_username and username.lower() != args.expect_username.lstrip("@").lower():
        raise FatalError(f"This access token belongs to @{username}, not "
                         f"@{args.expect_username.lstrip('@')} - nothing was posted")
    log.info("Target Instagram account: @%s (id %s)", username, ig.user_id)
    return ig, username


def cmd_publish(cfg: Config, args) -> int:
    if args.ig_token:
        cfg.ig_access_token = args.ig_token.strip()
    if args.ig_user_id:
        cfg.ig_user_id = args.ig_user_id.strip()
    cfg.require("ig_access_token", "bunny_zone", "bunny_password", "bunny_cdn_hostname")
    history = History(Path(args.history))
    session = make_session()
    ig, username = _connect_account(cfg, args, session)

    folder = "/".join(p for p in (cfg.bunny_base_path, (args.folder or "").strip("/")) if p)
    storage = BunnyStorage(cfg.bunny_zone, cfg.bunny_password, cfg.bunny_region, session)
    videos = build_candidates(storage.list_entries(folder), folder, cfg.bunny_cdn_hostname,
                              cfg.bunny_zone)
    videos.sort(key=lambda v: v["name"], reverse=args.newest_first)
    log.info("Found %d videos in %s/%s/", len(videos), cfg.bunny_zone, folder)
    if args.video_name:
        videos = [v for v in videos if v["name"].lower() == args.video_name.lower()]
        if not videos:
            raise FatalError(f"Video '{args.video_name}' not found in {folder or 'zone root'}/")
    for v in videos:  # history is per account: the same video can go to several accounts
        v["key"] = f"{ig.user_id}:{v['key']}"
        v["account"] = username

    done = history.published()
    pending = [v for v in videos if args.force or v["key"] not in done]
    skipped = len(videos) - len(pending)
    if args.limit:
        pending = pending[: args.limit]
    log.info("%d already published to @%s, %d to publish", skipped, username, len(pending))
    if not pending:
        log.info("Nothing to do - everything in this folder is already on @%s.", username)
        return 0

    template = _caption_template(args)
    if args.dry_run:
        print(f"Would publish to @{username}:")
        for i, v in enumerate(pending, 1):
            print(f"{i:>4}. {v['name']}  ({v['size_bytes'] / 1_048_576:.1f} MB)  {v['cdn_url']}")
        print(f"\nBatches of {args.batch_size}: {-(-len(pending) // args.batch_size)} batch(es)")
        print(f"Caption preview for {pending[0]['name']}:\n---\n"
              f"{_render_caption(template, pending[0], storage) or '(empty)'}\n---")
        return 0

    _check_cdn(session, pending[0])

    for v in pending:  # render captions up front so a caption problem shows before posting
        v["caption"] = _render_caption(template, v, storage)
        v["force"] = args.force

    opts = PublishOptions(workers=args.workers, batch_size=args.batch_size,
                          batch_pause=args.batch_pause, retries=args.retries,
                          share_to_feed=not args.reels_tab_only)
    res = PublishPipeline(ig, history, opts).run(pending)

    log.info("══════════ Publish summary ══════════")
    log.info("Published:         %d", len(res.published))
    log.info("Already published: %d", skipped)
    log.info("Failed:            %d", len([f for f in res.failed
                                             if not f[1].startswith("not started")]))
    log.info("Not started:       %d", res.not_started)
    for v in res.published:
        log.info("  • %s -> %s", v["name"], v.get("permalink") or v.get("media_id"))
    for v, err in res.failed:
        if not err.startswith("not started"):
            log.info("  ✘ %s: %s", v["name"], err.splitlines()[0])
    log.info("History: %s", Path(args.history).resolve())
    if res.interrupted:
        return 130
    if res.fatal:
        (log.warning if res.quota_stop else log.error)("Stopped: %s", res.fatal)
        return 3 if res.quota_stop else 2
    return 1 if res.failed else 0


def cmd_links(cfg: Config, args) -> int:
    published = History(Path(args.history)).published()
    if args.account:
        want = args.account.lstrip("@").lower()
        published = {k: v for k, v in published.items()
                     if (v.get("account") or "").lower() == want}
    if args.format == "csv":
        print(", ".join(v["permalink"] for v in published.values() if v.get("permalink")))
    else:
        print(json.dumps([{"account": v.get("account"), "video": k.split(":", 1)[-1],
                           "reel_link": v.get("permalink"), "media_id": v.get("media_id"),
                           "published_at": v.get("published_at")}
                          for k, v in published.items()], indent=2, ensure_ascii=False))
    return 0


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
    s.add_argument("-t", "--type", choices=["reels", "posts", "all"], default="reels",
                   help="reels = Reels tab (default); posts = photos, carousels and videos "
                        "from the grid; all = both")
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

    pb = sub.add_parser("publish", help="Publish videos from Bunny Storage as Instagram Reels")
    pb.add_argument("--ig-token", help="Access token of the account to post to "
                    "(overrides IG_ACCESS_TOKEN). The token decides the target account")
    pb.add_argument("--ig-user-id", help="Numeric IG user id (default: looked up from the token)")
    pb.add_argument("--expect-username",
                    help="Safety check: stop unless the token belongs to this username")
    pb.add_argument("-f", "--folder", default="",
                    help="Bunny folder with the videos, e.g. the username used by `sync`")
    cap = pb.add_mutually_exclusive_group()
    cap.add_argument("--caption", help="Caption text. Supports {original} and {name}")
    cap.add_argument("--caption-file", help="Read caption from a UTF-8 text file")
    cap.add_argument("--original-caption", action="store_true",
                     help="Use the original caption saved by `sync` in the .json metadata")
    pb.add_argument("--batch-size", type=_positive_int, default=10,
                    help="Videos per batch (default 10); shrunk to the remaining 24h quota")
    pb.add_argument("--batch-pause", type=float, default=60,
                    help="Seconds to wait between batches (default 60)")
    pb.add_argument("--workers", type=_positive_int, default=2,
                    help="Videos published in parallel inside a batch (default 2)")
    pb.add_argument("--retries", type=int, default=3,
                    help="Retries on temporary Instagram errors (default 3)")
    pb.add_argument("--limit", type=int, default=0, help="Publish at most N videos this run")
    pb.add_argument("--video-name", help="Publish only this file name")
    pb.add_argument("--newest-first", action="store_true",
                    help="Publish newest files first (default: oldest first, by file name)")
    pb.add_argument("--reels-tab-only", action="store_true",
                    help="Don't also show the Reel in the main feed grid")
    pb.add_argument("--poll-interval", type=float, default=6,
                    help="Seconds between processing-status checks (default 6)")
    pb.add_argument("--max-wait", type=float, default=600,
                    help="Max seconds to wait for Instagram to process a video (default 600)")
    pb.add_argument("--history", default="instagram_uploads.json",
                    help="History file that prevents duplicate posts")
    pb.add_argument("--force", action="store_true", help="Publish again even if already posted")
    pb.add_argument("--dry-run", action="store_true",
                    help="Show what would be published (and the caption), post nothing")
    pb.set_defaults(func=cmd_publish)

    ln = sub.add_parser("links", help="Print links of published Reels from the history")
    ln.add_argument("--format", choices=["json", "csv"], default="json")
    ln.add_argument("--history", default="instagram_uploads.json")
    ln.add_argument("--account", help="Only links posted to this username")
    ln.set_defaults(func=cmd_links)

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
