# YouTube Comment Scraper

Scrape comments from any public YouTube video — **no YouTube Data API, no API
key, no browser, free, terminal-only**.

The whole tool is one standalone Python script (`scripts/scrape_comments.py`)
that runs on the Python 3 standard library alone. It optionally uses
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) when installed for maximum
robustness. Works in any environment with Python 3 — and ships with a Claude
Code [Agent Skill](SKILL.md) wrapper for auto-discovery inside Claude.

## How it works

Two interchangeable engines that emit the **same** normalized record:

- **`ytdlp`** — shells out to `yt-dlp` (default when present). Most robust;
  auto-handles consent / age / region walls.
- **`innertube`** — pure Python stdlib. Fetches the watch page, drives YouTube's
  internal `youtubei/v1/next` endpoint with continuation tokens, paginates.
  **Zero dependencies, nothing to install.** Fallback when `yt-dlp` is absent.

Neither uses the official YouTube Data API or needs a key.

## Requirements

- **Python 3.8+** (standard library only — no `pip install` for the innertube
  engine).
- *Optional:* **`yt-dlp`** on `PATH` for the default/most-robust engine
  (`brew install yt-dlp`, or `pipx install yt-dlp`).

## Install

### As a Claude Code skill (recommended)

Clone the repo into your Claude skills directory — the folder name **must** be
the skill name:

```bash
git clone https://github.com/godemodegame/youtube-comment-scraper.git \
  ~/.claude/skills/youtube-comment-scraper
```

Restart Claude Code so it picks up the new skill. Then just ask in plain
language — *"scrape the youtube comments from `<url>`"*, *"download youtube
comments without the api"* — or invoke it directly with
`/youtube-comment-scraper`. Claude reads [`SKILL.md`](SKILL.md) and runs the
bundled script for you.

To install for a **single project** instead of globally, clone into that
project's skills directory:

```bash
git clone https://github.com/godemodegame/youtube-comment-scraper.git \
  <your-project>/.claude/skills/youtube-comment-scraper
```

Update later with `git -C ~/.claude/skills/youtube-comment-scraper pull`.

> Requires Python 3.8+ (standard library only). `yt-dlp` is optional but
> recommended — see [Requirements](#requirements).

### As a plain CLI (any tool / no Claude)

No build step — clone anywhere and run the script:

```bash
git clone https://github.com/godemodegame/youtube-comment-scraper.git
cd youtube-comment-scraper
python3 scripts/scrape_comments.py <video-url-or-id>
```

(Optional) make it directly executable:

```bash
chmod +x scripts/scrape_comments.py
./scripts/scrape_comments.py <video-url-or-id>
```

## Usage

```bash
# CSV to stdout, including replies (all defaults)
python3 scripts/scrape_comments.py dQw4w9WgXcQ

# Top 100 comments only, to a CSV file
python3 scripts/scrape_comments.py dQw4w9WgXcQ \
  --limit 100 --sort top --no-replies -o comments.csv

# 500 newest comments as pretty JSON
python3 scripts/scrape_comments.py "https://youtu.be/dQw4w9WgXcQ" \
  --limit 500 --sort new --format json -o comments.json

# One JSON object per line (large videos / streaming)
python3 scripts/scrape_comments.py <id> --format jsonl -o comments.jsonl

# Force the zero-dependency path (never touch yt-dlp)
python3 scripts/scrape_comments.py <id> --engine innertube
```

Accepted URL forms: bare 11-char id, `watch?v=…`, `youtu.be/…`, `shorts/…`,
`embed/…`, `live/…`, with or without extra query parameters.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--limit N` | `100` | Max top-level comments. `0` = all. |
| `--sort {top,new}` | `top` | Top (relevance) or newest first. |
| `--no-replies` | off | Skip nested replies (replies **included** by default). |
| `--reply-limit N` | `20` | Max replies per comment. `0` = all. |
| `--format {csv,json,jsonl}` | `csv` | Output format. |
| `--output FILE`, `-o` | stdout | Write to a file. |
| `--engine {auto,ytdlp,innertube}` | `auto` | `auto` = yt-dlp if present, else innertube. |
| `--max-pages N` | `0` | Safety cap on continuation pages (`0` = unlimited). |
| `--sleep SEC` | `0.6` | Base delay between innertube requests (jittered). |
| `--hl HL` / `--gl GL` | `en` / `US` | Language / country (helps bypass consent walls). |
| `--quiet` / `--verbose` | — | Less / more progress on stderr. |

## Output schema

One record per comment (top-level or reply):

| field | example | notes |
|-------|---------|-------|
| `cid` | `Ugz…AaABAg` | comment id |
| `text` | `"can confirm…"` | comment body |
| `author` | `"@RickAstleyYT"` | handle / display name |
| `author_channel_id` | `UC…` | channel id |
| `author_url` | `https://www.youtube.com/@…` | channel URL |
| `author_thumbnail` | `https://yt3.ggpht.com/…` | avatar URL |
| `like_count` | `262000` | **approximate** int parsed from the display string |
| `like_count_text` | `"262K"` | raw string (null via yt-dlp) |
| `reply_count` | `961` | replies on a comment (null via yt-dlp) |
| `published_text` | `"1 year ago"` | relative time string |
| `published_epoch` | `1751328000` | **approximate** epoch; may be null |
| `is_reply` | `false` | true for replies |
| `parent_cid` | `null` | parent comment id for replies |
| `is_pinned` | `true` | pinned by the channel |
| `is_hearted` | `true` | hearted/favorited by the creator |
| `is_author_reply` | `false` | author is the video's channel owner |
| `is_verified` | `true` | author is verified |

Formats: **csv** = header + one row per comment · **json** = single array ·
**jsonl** = one object per line. Replies are emitted directly after their parent.

### Recipes

```bash
# Most-liked 100 comments (YouTube "top" != strict likes — sort yourself)
python3 scripts/scrape_comments.py <id> --limit 300 --sort top --no-replies \
  --engine ytdlp --format jsonl -o c.jsonl
jq -s 'sort_by(-.like_count) | .[:100]' c.jsonl > most_liked100.json

# Top replies by likes (no native YouTube "top replies" sort)
python3 scripts/scrape_comments.py <id> --limit 200 --reply-limit 0 \
  --format jsonl -o all.jsonl
jq -s 'map(select(.is_reply)) | sort_by(-.like_count) | .[:100]' all.jsonl
```

## Exit codes

| code | meaning |
|------|---------|
| `0` | success |
| `2` | bad argument / unparseable URL |
| `3` | video unavailable (private / removed) |
| `4` | comments disabled (or live-chat-only stream) |
| `5` | consent / age / members wall, or `yt-dlp` requested but missing |
| `6` | network / HTTP failure (after retries) |
| `7` | parse error / engine error (e.g. YouTube format change) |

Partial results collected before a mid-run network failure are still written.

## Troubleshooting

- **Comments disabled** (exit 4): comments off, or it's a live stream.
- **Consent / region wall** (exit 5): add `--gl US --hl en`, or `--engine ytdlp`.
- **Age-restricted / members-only** (exit 5): use `--engine ytdlp` (may need cookies).
- **Rate limited / HTTP 429** (exit 6): raise `--sleep` (e.g. `--sleep 2`), lower `--limit`.
- **Parse error** (exit 7): YouTube changed format — use `--engine ytdlp` while the
  innertube parser is updated.

## Using it from other tools / agents

- **Claude Code / Claude Agent SDK / claude.ai** — install the folder as a skill
  (it auto-loads via [`SKILL.md`](SKILL.md)); ask Claude to "scrape youtube comments".
- **Any other agent or harness (Codex, etc.)** — there is no special integration
  needed: it's a normal CLI. Run `python3 scripts/scrape_comments.py …` from a
  shell tool. For Codex, add a line to your `AGENTS.md` pointing at the command.
- **Plain scripting / cron** — call it like any CLI; parse the CSV/JSON output.

## Notes & limits

- **Like counts are approximate** — logged-out clients only get display strings
  (`"262K"`); both the raw string and a parsed int are emitted.
- **Timestamps are relative** (`"1 year ago"`) → approximate `published_epoch`.
- **`reply_count`** is only populated by the innertube engine.
- **Be polite** — default pacing inserts jittered delays; raise `--sleep` for big
  jobs. Intended for personal / research use; respect YouTube's Terms of Service
  and content owners.

## License

MIT
