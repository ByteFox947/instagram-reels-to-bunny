# instagram-reels-to-bunny

Finds **every reel on an Instagram profile**, downloads each one with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and uploads it to a **Bunny.net Storage zone**
with a JSON metadata file next to it. Runs are incremental: reels already in Bunny are skipped.

> Only use this on accounts you own or have permission to archive. Respect Instagram's
> Terms of Use and the creators' copyright.

## How it works

1. **Find reels:** looks up the profile and pages through its Reels tab using the same
   endpoints instagram.com uses (needs your logged-in browser cookies).
2. **Download:** passes each `https://www.instagram.com/reel/<code>/` URL to yt-dlp. With
   `ffmpeg` installed you get merged video and audio in the best quality.
3. **Upload in batches:** reels are processed in batches (default 20). Each video is sent
   to Bunny Storage with a SHA-256 `Checksum` header, so Bunny rejects corrupted uploads, and
   a `.json` metadata file goes next to it. Local files are deleted as soon as they're uploaded.

Result layout in the zone (one folder per username):

```
<username>/2024-02-01_C2xYz123.mp4
<username>/2024-02-01_C2xYz123.json
```

## Setup

```bash
pip install -r requirements.txt     # requests, python-dotenv, yt-dlp
# optional but recommended for best quality
sudo apt install ffmpeg             # or: brew install ffmpeg
cp .env.example .env
```

### 1. Export Instagram cookies

Log in to instagram.com in your browser. Export the cookies in Netscape format as
`cookies.txt`, for example with the "Get cookies.txt LOCALLY" extension. Never commit this file.

### 2. Create a new Bunny storage zone

Put your account API key (bunny.net → Account settings → API) in `.env` as `BUNNY_API_KEY`, then run:

```bash
python -m reels2bunny create-zone --name my-insta-reels --region DE
# optional: --replicate NY SG   --ssd
```

Copy the printed `BUNNY_STORAGE_ZONE`, `BUNNY_STORAGE_PASSWORD` and `BUNNY_STORAGE_REGION`
into `.env`. If you'd rather create the zone in the dashboard, the password is under
Storage → your zone → FTP & API Access.

## Usage

```bash
# See which reels would be backed up
python -m reels2bunny sync -u instagram_username -c cookies.txt --dry-run

# Download all reels and upload them to Bunny
python -m reels2bunny sync -u instagram_username -c cookies.txt

# Big profile: smaller batches, longer pauses
python -m reels2bunny sync -u instagram_username -c cookies.txt --batch-size 10 --batch-pause 30
```

| Option | Default | Meaning |
|---|---|---|
| `--batch-size` | 20 | Reels per batch. Each batch is downloaded, uploaded, then its temp files are deleted |
| `--batch-pause` | 10 | Seconds to wait between batches (helps avoid Instagram rate limits) |
| `--workers` | 3 | Parallel downloads/uploads inside a batch |
| `--retries` | 3 | Retries per download/upload on temporary errors (backoff 5s, 15s, 45s) |
| `--delay` | 1.5 | Seconds between reel-listing requests |
| `--limit` | – | Only the N newest reels |
| `--work-dir` | system temp | Where temporary downloads go |
| `--force` | off | Re-upload reels that are already in Bunny |

### Batches and disk usage

Each reel's video is deleted locally as soon as it's uploaded. Each batch's temp folder is
also removed after the batch, including after errors or Ctrl+C. At any moment the disk holds
at most about `--workers` videos, however many reels the profile has.

### Error handling

| Situation | What happens |
|---|---|
| Network error, timeout, HTTP 429/5xx, bad upload checksum | Retried with backoff, reopening the file on each attempt |
| Reel deleted or unavailable | Skipped and recorded as failed, then the run continues |
| Wrong Bunny password or region, expired cookies, login wall, private profile | **Run stops straight away** with a message saying what to fix |
| An entire batch fails with network or rate-limit errors | Run stops (Instagram is probably blocking you). Wait a while and run again |
| Listing breaks partway through the profile | Uploads the reels found so far, warns you, and exits with code 1 |
| Ctrl+C | Stops, cleans up temp files, prints a summary |

The `.json` metadata is uploaded last, so it marks a reel as finished. Running the same
command again only processes reels without a `.json`, which includes failed and interrupted ones.
Failed reels are also written to `failed-<username>-<time>.txt`.

Exit codes: `0` all done · `1` some reels failed or listing incomplete · `2` stopped on a
fatal error · `130` interrupted.

### Alternative: official Graph API (your own Business/Creator account)

This mode needs no cookies or scraping. It lists your reels with the Instagram Graph API and
still downloads them with yt-dlp:

```bash
# set IG_ACCESS_TOKEN in .env
python -m reels2bunny sync --source graph -c cookies.txt
python -m reels2bunny refresh-token   # long-lived tokens expire after 60 days
```

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `BUNNY_STORAGE_ZONE` | Storage zone name |
| `BUNNY_STORAGE_PASSWORD` | Zone password (FTP & API Access) |
| `BUNNY_STORAGE_REGION` | Empty = Falkenstein, or `uk`, `ny`, `la`, `sg`, `se`, `br`, `jh`, `syd` |
| `BUNNY_BASE_PATH` | Optional parent folder for the `<username>/` folders (default: none, so the username folder sits at the zone root) |
| `BUNNY_API_KEY` | Account API key, only for `create-zone` |
| `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_API_BASE`, `IG_API_VERSION` | Only for `--source graph` |

## Troubleshooting

- **"not logged in / blocked"**: export fresh cookies and raise `--delay`.
- **Private profile**: the logged-in account has to follow it.
- **yt-dlp errors**: Instagram changes often. Update yt-dlp with `pip install -U yt-dlp`.
- **401 from Bunny**: check the zone name, password and `BUNNY_STORAGE_REGION`
  (it must match the zone's main region).
