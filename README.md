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
3. **Upload:** sends the file to Bunny Storage with a SHA-256 `Checksum` header, so Bunny
   rejects corrupted uploads. A `.json` metadata file goes next to it.

Result layout in the zone:

```
instagram/reels/<username>/2024-02-01_C2xYz123.mp4
instagram/reels/<username>/2024-02-01_C2xYz123.json
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

# Options
#   --workers 3    parallel downloads/uploads
#   --limit 10     only the 10 newest reels
#   --force        re-upload reels that are already in Bunny
#   --delay 1.5    seconds between listing requests (raise it if rate-limited)
```

The command exits with a non-zero code if any reel fails. Re-run it to retry only the missing ones.

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
| `BUNNY_BASE_PATH` | Folder in the zone (default `instagram/reels`) |
| `BUNNY_API_KEY` | Account API key, only for `create-zone` |
| `IG_ACCESS_TOKEN`, `IG_USER_ID`, `IG_API_BASE`, `IG_API_VERSION` | Only for `--source graph` |

## Troubleshooting

- **"not logged in / blocked"**: export fresh cookies and raise `--delay`.
- **Private profile**: the logged-in account has to follow it.
- **yt-dlp errors**: Instagram changes often. Update yt-dlp with `pip install -U yt-dlp`.
- **401 from Bunny**: check the zone name, password and `BUNNY_STORAGE_REGION`
  (it must match the zone's main region).
