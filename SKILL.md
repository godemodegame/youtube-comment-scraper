---
name: youtube-comment-scraper
description: >-
  Scrape the comments under a YouTube video without the official YouTube Data
  API, without an API key, and without a browser — entirely from the terminal,
  and completely free. This skill should be used when the user asks to "scrape
  youtube comments", "get comments from a youtube video", "download youtube
  comments without an api key", "export a video's comments to CSV or JSON", or
  "analyze a youtube comment section". Handles nested replies, top/newest
  sorting, and pinned and hearted comments.
argument-hint: "<video-url-or-id> [--limit N] [--sort top|new] [--format csv|json|jsonl] [--output FILE]"
allowed-tools: Bash, Read
version: 0.1.0
license: MIT
user-invocable: true
---

# YouTube Comment Scraper

Scrape comments from any public YouTube video straight from the terminal — no
official YouTube Data API, no API key, no quota, no browser, and free. The
single script `scripts/scrape_comments.py` runs on Python 3 (standard library
only) and optionally uses `yt-dlp` when it is installed.

## When to use

Use this skill when the user wants the comments under a YouTube video — to read
them, count them, export them to a file, or feed them into further analysis
(sentiment, summaries, finding questions, etc.). Trigger phrases include "scrape
youtube comments", "get the comments from this video", "download youtube
comments without the api", and "export comments to CSV".

Do **not** use it for live-stream chat (that is a different endpoint — the script
reports comments as disabled for active live chats), and remember that exact
like counts are not exposed by YouTube to logged-out clients (see
[Notes & limits](#notes--limits)).

## How it works

Two interchangeable engines produce the **same normalized record**, so the
output is identical regardless of which one runs:

- **yt-dlp** (default when installed) shells out to `yt-dlp`, which is the most
  robust extractor and automatically handles consent, age, and region walls.
- **innertube** is a pure Python standard-library scraper. It fetches the watch
  page, reads YouTube's internal `youtubei/v1/next` endpoint with continuation
  tokens, and paginates — **zero dependencies, nothing to install**. This is the
  fallback when `yt-dlp` is absent, and it keeps the skill portable.

Neither engine uses the official YouTube Data API or needs a key.

## Quick start

Run the script with `python3`. The first positional argument is a video URL or
an 11-character video id.

```bash
# CSV to stdout, including replies (all defaults)
python3 scripts/scrape_comments.py dQw4w9WgXcQ

# 500 newest comments to a CSV file
python3 scripts/scrape_comments.py "https://youtu.be/dQw4w9WgXcQ" \
  --limit 500 --sort new --output comments.csv

# Top-level comments only, as pretty JSON
python3 scripts/scrape_comments.py <id> --format json --no-replies --output top.json

# One JSON object per line (good for very large comment sets / streaming)
python3 scripts/scrape_comments.py <id> --format jsonl --output comments.jsonl

# Force the zero-dependency path (never touch yt-dlp)
python3 scripts/scrape_comments.py <id> --engine innertube
```

Accepted URL forms: bare id (`dQw4w9WgXcQ`), `watch?v=…`, `youtu.be/…`,
`shorts/…`, `embed/…`, `live/…`, with or without extra query parameters.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--limit N` | `100` | Max top-level comments. `0` = all of them. |
| `--sort {top,new}` | `top` | Sort by top (most relevant) or newest first. |
| `--no-replies` | off | Skip nested replies. Replies are **included by default**. |
| `--reply-limit N` | `20` | Max replies fetched per comment. `0` = all. |
| `--format {csv,json,jsonl}` | `csv` | Output format. |
| `--output FILE`, `-o` | stdout | Write to a file instead of stdout. |
| `--engine {auto,ytdlp,innertube}` | `auto` | `auto` = yt-dlp if present, else innertube. |
| `--max-pages N` | `0` | Safety cap on continuation pages (`0` = unlimited). |
| `--sleep SEC` | `0.6` | Base delay between innertube requests (jittered). |
| `--hl HL` / `--gl GL` | `en` / `US` | Interface language / country (helps bypass consent walls). |
| `--quiet` / `--verbose` | — | Suppress, or expand, progress logging on stderr. |

## Output format

Every comment — top-level or reply — is one record with these fields:

| field | example | notes |
|-------|---------|-------|
| `cid` | `Ugz…AaABAg` | comment id; replies use `<parent>.<reply>` via yt-dlp |
| `text` | `"can confirm…"` | the comment body |
| `author` | `"@RickAstleyYT"` | handle / display name |
| `author_channel_id` | `UCuAXFkgsw1L7xaCfnd5JJOw` | channel id |
| `author_url` | `https://www.youtube.com/@…` | channel URL |
| `author_thumbnail` | `https://yt3.ggpht.com/…` | avatar URL |
| `like_count` | `262000` | **approximate** integer parsed from the display string |
| `like_count_text` | `"262K"` | raw display string (null via yt-dlp) |
| `reply_count` | `961` | replies on a top-level comment (null via yt-dlp) |
| `published_text` | `"1 year ago"` | relative time string |
| `published_epoch` | `1751328000` | **approximate** epoch derived from the relative string; may be null |
| `is_reply` | `false` | true for replies |
| `parent_cid` | `null` | parent comment id for replies |
| `is_pinned` | `true` | pinned by the channel |
| `is_hearted` | `true` | hearted/favorited by the creator |
| `is_author_reply` | `false` | author is the video's channel owner |
| `is_verified` | `true` | author is verified |

- **csv** — header row + one row per comment, columns in the order above.
- **json** — a single pretty-printed array of records.
- **jsonl** — one JSON object per line; best for streaming and huge videos.

Replies are emitted immediately after their parent comment, preserving thread
order.

## Engine selection

- `auto` (default): use `yt-dlp` if it is on `PATH`, otherwise the stdlib
  innertube scraper. In `auto`, if yt-dlp fails for a recoverable reason, it
  automatically retries with the innertube engine.
- `--engine ytdlp`: force yt-dlp (errors if not installed). Most robust; best
  for age-restricted, members-only, or consent/region-gated videos.
- `--engine innertube`: force the zero-dependency path. Use when yt-dlp is not
  available or for maximum portability.

## Troubleshooting

- **"comments are disabled"** (exit 4): the video has comments turned off, or it
  is a live stream (live chat is a different endpoint and is not scraped).
- **Consent / region wall** (exit 5): add `--gl US --hl en`, or switch to
  `--engine ytdlp`, which handles consent automatically.
- **Age-restricted or members-only** (exit 5): use `--engine ytdlp` (it may also
  need YouTube cookies for members-only content).
- **Rate limited / HTTP 429** (exit 6): increase `--sleep` (e.g. `--sleep 2`)
  and/or lower `--limit`. The script already retries with exponential backoff.
- **Parse error / schema drift** (exit 7): YouTube changed its response format;
  try `--engine ytdlp` (kept up to date) while the innertube parser is updated.

Exit codes: `0` success · `2` bad argument/URL · `3` video unavailable · `4`
comments disabled · `5` consent/age/engine-unavailable · `6` network/HTTP ·
`7` parse/engine error. Partial results collected before a mid-run network
failure are still written.

## Notes & limits

- **Like counts are approximate.** Logged-out clients only receive display
  strings like `"262K"`; the script emits both the raw string and a parsed
  integer (`262000`).
- **Timestamps are relative.** `"1 year ago"` is converted to an approximate
  `published_epoch`; it is not an exact post time and may be null.
- **`reply_count`** is only populated by the innertube engine (yt-dlp does not
  expose it).
- **Be polite.** Default pacing inserts a jittered delay between requests; raise
  `--sleep` for large jobs. Intended for personal/research use — respect
  YouTube's Terms of Service and the video owners' content.

## Requirements

- Python 3.8+ (standard library only — no `pip install` needed for the
  innertube engine).
- Optional: `yt-dlp` on `PATH` for the default/most-robust engine
  (`brew install yt-dlp` or `pipx install yt-dlp`).
