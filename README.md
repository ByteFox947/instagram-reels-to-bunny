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

## Publish videos from Bunny to Instagram (access token, in batches)

`publish` takes the videos in a Bunny folder and posts them as Reels to **your** Instagram
Business/Creator account through the official Content Publishing API. Only post videos you
own or have the rights to.

Setup in `.env`:
- `IG_ACCESS_TOKEN` (or `INSTAGRAM_TOKEN`): a token with `instagram_business_content_publish`
- `BUNNY_CDN_HOSTNAME`: a pull zone linked to the storage zone, e.g. `my-reels.b-cdn.net`.
  Instagram downloads each video from `https://<host>/<folder>/<file>`, so the pull zone
  must not use token authentication.

```bash
# Preview: list the videos, batch count and caption, without posting anything
python -m reels2bunny publish -f <username> --caption-file caption.txt --dry-run

# Publish in batches of 5, 2 at a time, 2 minutes between batches
python -m reels2bunny publish -f <username> --caption-file caption.txt \
    --batch-size 5 --workers 2 --batch-pause 120

# Reuse the original caption that `sync` saved, and add your own text
python -m reels2bunny publish -f <username> --caption "{original}

#myhashtag"

# Print the links of every Reel published so far
python -m reels2bunny links --format csv      # or --format json
```

| Option | Default | Meaning |
|---|---|---|
| `-f, --folder` | zone root | Bunny folder that holds the videos (the username from `sync`) |
| `--caption` / `--caption-file` / `--original-caption` | empty | Caption text. `{original}` = original caption from the `.json`, `{name}` = file name |
| `--batch-size` | 10 | Videos per batch |
| `--workers` | 2 | Videos processed in parallel inside a batch |
| `--batch-pause` | 60 | Seconds between batches |
| `--limit` | – | Publish at most N videos in this run |
| `--retries` | 3 | Retries on temporary Instagram errors (10s, 30s, 90s) |
| `--newest-first` | off | Default order is oldest first, by file name |
| `--reels-tab-only` | off | Don't also show the Reel in the main feed grid |
| `--max-wait` | 600 | Max seconds to wait for Instagram to process a video |
| `--video-name` | – | Publish a single file |
| `--history` | `instagram_uploads.json` | Record of what was posted, used to prevent duplicates |
| `--force` | off | Post again even if it's already in the history |

**Which account gets the posts:** the access token decides it. Each token belongs to one
Instagram account, and the tool looks up that account before posting. To post to another
account, pass its token:

```bash
python -m reels2bunny publish -f <folder> --ig-token "<TOKEN_OF_ACCOUNT_2>" \
    --expect-username account2 --caption "{original}"
```

`--expect-username` is a safety check: if the token belongs to a different account, nothing
is posted. The history is kept per account, so the same videos can go to several accounts
without affecting each other. Use `links --account account2` to see one account's Reels.

**Batches and the daily limit:** before each batch, the tool asks Instagram how many API posts
are left in the rolling 24-hour window. If fewer are left than `--batch-size`, the batch is
shrunk to fit. When none are left, the run stops with exit code `3`. Run the same command
later and it continues where it stopped.

**No duplicate posts:** each video's progress is saved in the history file straight away. If
a run crashes or the publish reply is lost, the next run checks the saved Instagram upload
and reuses it, or marks it done if it's already live. A publish request is only retried after
confirming the Reel isn't already live.

**Errors:** a bad or expired token, a missing permission or an unreachable CDN URL stops the
run (exit `2`). A video Instagram can't process is skipped and marked `FAILED` in the history,
so the next run tries it again. Network errors and rate limits are retried.

### Alternative: official Graph API (your own Business/Creator account)

This mode needs no cookies or scraping. It lists your reels with the Instagram Graph API and
still downloads them with yt-dlp:

```bash
# set IG_ACCESS_TOKEN in .env
python -m reels2bunny sync --source graph -c cookies.txt
python -m reels2bunny refresh-token   # long-lived tokens expire after 60 days
```

## Run on Google Colab

Open `colab.ipynb` in Colab: File → Open notebook → GitHub tab → paste the repo URL. Then:

1. Add your Bunny details and one `IG_TOKEN_…` per Instagram account as **Colab Secrets** (🔑).
   If the repo is private, also add `GITHUB_TOKEN`.
2. Run the cells from top to bottom. They clone the repo, install yt-dlp (ffmpeg is already in
   Colab) and mount Google Drive. The history file and `cookies.txt` are kept in
   `MyDrive/reels2bunny`, so they survive Colab restarts.
3. Fill in the form fields (username, token secret name, batch size…). Run once with
   `DRY_RUN` ticked, then untick it.

If Colab disconnects, run the cells again. Finished work is skipped.

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `BUNNY_STORAGE_ZONE` | Storage zone name |
| `BUNNY_STORAGE_PASSWORD` | Zone password (FTP & API Access) |
| `BUNNY_STORAGE_REGION` | Empty = Falkenstein, or `uk`, `ny`, `la`, `sg`, `se`, `br`, `jh`, `syd` |
| `BUNNY_BASE_PATH` | Optional parent folder for the `<username>/` folders (default: none, so the username folder sits at the zone root) |
| `BUNNY_API_KEY` | Account API key, only for `create-zone` |
| `BUNNY_CDN_HOSTNAME` | Pull zone hostname, only for `publish` |
| `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_API_BASE`, `IG_API_VERSION` | Only for `--source graph` |

## Troubleshooting

- **"not logged in / blocked"**: export fresh cookies and raise `--delay`.
- **Private profile**: the logged-in account has to follow it.
- **yt-dlp errors**: Instagram changes often. Update yt-dlp with `pip install -U yt-dlp`.
- **401 from Bunny**: check the zone name, password and `BUNNY_STORAGE_REGION`
  (it must match the zone's main region).
