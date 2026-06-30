#!/usr/bin/env python3
"""Scrape YouTube comments without the official YouTube Data API.

Terminal-only, no browser, no API key, free. Two interchangeable engines that
emit the SAME normalized record:

  * ytdlp     -- shell out to yt-dlp (most robust; auto-handles consent/age).
  * innertube -- pure Python stdlib (urllib/json/re/gzip); zero dependencies.

Default engine is "auto": yt-dlp if it is on PATH, else the stdlib scraper.
"""

import argparse
import csv
import gzip
import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Output column order; also the per-record key set.
FIELDS = [
    "cid", "text", "author", "author_channel_id", "author_url", "author_thumbnail",
    "like_count", "like_count_text", "reply_count", "published_text", "published_epoch",
    "is_reply", "parent_cid", "is_pinned", "is_hearted", "is_author_reply", "is_verified",
]


# --------------------------------------------------------------------------- #
# Typed errors -> exit codes
# --------------------------------------------------------------------------- #
class ScrapeError(Exception):
    pass


class VideoUnavailable(ScrapeError):
    pass


class CommentsDisabled(ScrapeError):
    pass


class ConsentWall(ScrapeError):
    pass


class NetworkError(ScrapeError):
    pass


class ParseError(ScrapeError):
    pass


class EngineUnavailable(ScrapeError):
    pass


class EngineError(ScrapeError):
    pass


class HttpError(ScrapeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def deep_get(obj, *keys, default=None):
    """Walk nested dicts (string key) / lists (int index)."""
    cur = obj
    for k in keys:
        if isinstance(k, int):
            if isinstance(cur, (list, tuple)) and -len(cur) <= k < len(cur):
                cur = cur[k]
            else:
                return default
        else:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return default
    return cur


def find_all(obj, key):
    """Yield every value stored under `key` anywhere in a nested structure."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def parse_like_count(s):
    """'262K' -> 262000, '1.2M' -> 1200000, '1,234' -> 1234, 5 -> 5."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    m = re.match(r"^([\d.]+)\s*([KkMmBb]?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(num * mult)


def relative_time_to_epoch(s):
    """Best-effort '1 year ago' -> approximate epoch. May return None."""
    if not s:
        return None
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", s)
    if not m:
        return None
    n = int(m.group(1))
    mult = {
        "second": 1, "minute": 60, "hour": 3600, "day": 86400,
        "week": 604800, "month": 2_592_000, "year": 31_536_000,
    }[m.group(2)]
    return int(time.time()) - n * mult


def parse_video_id(s):
    s = s.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    if "://" not in s and "/" not in s and "?" not in s and not s.startswith("www."):
        raise ValueError(f"not a valid video id or URL: {s!r}")
    u = urllib.parse.urlparse(s if "://" in s else "https://" + s)
    host = u.netloc.lower()
    if "youtu.be" in host:
        vid = u.path.lstrip("/").split("/")[0]
        if re.fullmatch(r"[0-9A-Za-z_-]{11}", vid):
            return vid
    qs = urllib.parse.parse_qs(u.query)
    if "v" in qs and re.fullmatch(r"[0-9A-Za-z_-]{11}", qs["v"][0]):
        return qs["v"][0]
    m = re.search(r"/(?:shorts|embed|live|v)/([0-9A-Za-z_-]{11})", u.path)
    if m:
        return m.group(1)
    seg = u.path.rstrip("/").split("/")[-1]
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", seg):
        return seg
    raise ValueError(f"could not extract a video id from: {s!r}")


def order_threads(records):
    """Place each reply immediately after its parent; keep top-level order."""
    tops = [r for r in records if not r.get("is_reply")]
    replies = {}
    for r in records:
        if r.get("is_reply"):
            replies.setdefault(r.get("parent_cid"), []).append(r)
    out = []
    seen = set()
    for t in tops:
        out.append(t)
        seen.add(t.get("cid"))
        out.extend(replies.get(t.get("cid"), []))
    for pid, reps in replies.items():
        if pid not in seen:
            out.extend(reps)
    return out


# --------------------------------------------------------------------------- #
# HTTP (stdlib, with retry/backoff + gzip)
# --------------------------------------------------------------------------- #
def _request(req, retries=4):
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                delay = float(ra) if (ra and ra.isdigit()) else min(30.0, 2 ** attempt + random.random())
                time.sleep(delay)
                last = e
                continue
            raise HttpError(e.code, f"HTTP {e.code} for {req.full_url}")
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(min(30.0, 2 ** attempt + random.random()))
                last = e
                continue
            raise NetworkError(str(getattr(e, "reason", e)))
    raise NetworkError(str(last))


def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _request(req).decode("utf-8", errors="replace")


def http_post_json(url, obj, headers):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    raw = _request(req)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ParseError(f"non-JSON response from youtubei: {e}")


# --------------------------------------------------------------------------- #
# InnerTube engine (zero dependencies)
# --------------------------------------------------------------------------- #
def _balanced_json(text, start):
    """Return the JSON object substring starting at `text[start] == '{'`."""
    depth, i, n = 0, start, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def extract_json_after(text, marker):
    idx = text.find(marker)
    if idx == -1:
        return None
    start = text.find("{", idx + len(marker))
    if start == -1:
        return None
    return _balanced_json(text, start)


def fetch_watch_page(video_id, hl, gl):
    url = (
        f"https://www.youtube.com/watch?v={video_id}"
        f"&hl={hl}&gl={gl}&has_verified=1&bpctr=9999999999"
    )
    headers = {
        "User-Agent": UA,
        "Accept-Language": f"{hl}-{gl},{hl};q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Cookie": "SOCS=CAI; CONSENT=YES+1",
    }
    html = http_get(url, headers)
    if "INNERTUBE_API_KEY" not in html and "consent.youtube.com" in html:
        raise ConsentWall("YouTube returned a consent page")
    return html


def extract_config(html):
    m_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    m_ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    ctx_str = extract_json_after(html, '"INNERTUBE_CONTEXT":')
    yid_str = (
        extract_json_after(html, "ytInitialData =")
        or extract_json_after(html, '"ytInitialData":')
    )
    context = json.loads(ctx_str) if ctx_str else None
    yt_initial_data = json.loads(yid_str) if yid_str else None
    if yt_initial_data is None and context is None:
        raise VideoUnavailable("could not parse the watch page (video private/removed?)")
    if not (m_key and m_ver and context and yt_initial_data):
        raise ParseError("watch page missing InnerTube config (YouTube format may have changed)")
    return {
        "api_key": m_key.group(1),
        "client_version": m_ver.group(1),
        "context": context,
        "yt_initial_data": yt_initial_data,
    }


def innertube_headers(cfg):
    return {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": cfg["client_version"],
    }


def call_next(cfg, token):
    body = {"context": cfg["context"], "continuation": token}
    headers = innertube_headers(cfg)
    try:
        return http_post_json(
            "https://www.youtube.com/youtubei/v1/next?prettyPrint=false", body, headers
        )
    except HttpError as e:
        if e.code in (400, 401, 403):
            url = (
                "https://www.youtube.com/youtubei/v1/next?key="
                f"{cfg['api_key']}&prettyPrint=false"
            )
            return http_post_json(url, body, headers)
        raise


def _title_text(t):
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return t.get("simpleText") or "".join(r.get("text", "") for r in t.get("runs", []))
    return ""


def find_sort_tokens(data):
    tokens = {}
    for sub in find_all(data, "sortFilterSubMenuRenderer"):
        for item in (sub.get("subMenuItems") or []):
            title = _title_text(item.get("title"))
            tok = deep_get(item, "serviceEndpoint", "continuationCommand", "token")
            if tok and title:
                tokens[title] = tok
    return tokens


def pick_sort(sort_tokens, sort):
    want_new = sort == "new"
    for title, tok in sort_tokens.items():
        t = title.lower()
        if want_new and ("new" in t or "recent" in t):
            return tok
        if not want_new and "top" in t:
            return tok
    return None


def get_base_comment_token(data):
    for isr in find_all(data, "itemSectionRenderer"):
        if isr.get("sectionIdentifier") == "comment-item-section":
            for cir in find_all(isr, "continuationItemRenderer"):
                tok = deep_get(cir, "continuationEndpoint", "continuationCommand", "token")
                if tok:
                    return tok
    return None


def resolve_start_token(cfg, data, sort, log):
    base = get_base_comment_token(data)
    if base is None:
        return None
    sort_tokens = find_sort_tokens(data)
    if not sort_tokens:
        try:
            probe = call_next(cfg, base)
            sort_tokens = find_sort_tokens(probe)
        except ScrapeError:
            sort_tokens = {}
    picked = pick_sort(sort_tokens, sort) if sort_tokens else None
    if picked:
        return picked
    if sort == "new":
        log("warning: could not find a 'newest' sort token; using default order")
    return base


def build_entity_maps(response):
    entities, toolbar_states, surfaces = {}, {}, {}
    mutations = deep_get(response, "frameworkUpdates", "entityBatchUpdate", "mutations") or []
    for m in mutations:
        payload = m.get("payload", {})
        if "commentEntityPayload" in payload:
            ce = payload["commentEntityPayload"]
            cid = deep_get(ce, "properties", "commentId")
            if cid:
                entities[cid] = ce
        elif "engagementToolbarStateEntityPayload" in payload:
            ts = payload["engagementToolbarStateEntityPayload"]
            if ts.get("key"):
                toolbar_states[ts["key"]] = ts
        elif "commentSurfaceEntityPayload" in payload:
            sp = payload["commentSurfaceEntityPayload"]
            if sp.get("key"):
                surfaces[sp["key"]] = sp
    return entities, toolbar_states, surfaces


def extract_continuation_items(response):
    out = []
    for key in ("onResponseReceivedEndpoints", "onResponseReceivedActions"):
        for ep in response.get(key, []) or []:
            action = (
                ep.get("reloadContinuationItemsCommand")
                or ep.get("appendContinuationItemsAction")
            )
            if action:
                out.extend(action.get("continuationItems", []) or [])
    return out


def find_replies_token(comment_thread_renderer):
    rr = deep_get(comment_thread_renderer, "replies", "commentRepliesRenderer")
    if not rr:
        return None
    for cc in find_all(rr, "continuationCommand"):
        if cc.get("token"):
            return cc["token"]
    return None


def _author_url(handle, channel_id):
    if handle and handle.startswith("@"):
        return "https://www.youtube.com/" + handle
    if channel_id:
        return "https://www.youtube.com/channel/" + channel_id
    return None


def parse_entity_comment(vm, entities, toolbar_states, surfaces, parent_cid):
    cid = vm.get("commentId")
    ent = entities.get(cid, {})
    props = ent.get("properties", {})
    author = ent.get("author", {})
    tb = ent.get("toolbar", {})
    tstate = toolbar_states.get(vm.get("toolbarStateKey"), {})

    like_text = tb.get("likeCountNotliked") or tb.get("likeCountLiked")
    reply_count_text = tb.get("replyCount")
    hearted = (tstate.get("heartState") == "TOOLBAR_HEART_STATE_HEARTED") or bool(
        tb.get("heartActiveTooltip")
    )
    reply_level = props.get("replyLevel") or 0
    handle = author.get("displayName", "")
    channel_id = author.get("channelId")
    return {
        "cid": cid,
        "text": deep_get(props, "content", "content") or "",
        "author": handle,
        "author_channel_id": channel_id,
        "author_url": _author_url(handle, channel_id),
        "author_thumbnail": deep_get(ent, "avatar", "image", "sources", 0, "url"),
        "like_count": parse_like_count(like_text),
        "like_count_text": like_text or None,
        "reply_count": parse_like_count(reply_count_text) if reply_count_text else None,
        "published_text": props.get("publishedTime"),
        "published_epoch": relative_time_to_epoch(props.get("publishedTime")),
        "is_reply": parent_cid is not None or reply_level > 0,
        "parent_cid": parent_cid,
        "is_pinned": bool(vm.get("pinnedText")),
        "is_hearted": hearted,
        "is_author_reply": bool(author.get("isCreator")),
        "is_verified": bool(author.get("isVerified")),
    }


def parse_legacy_comment(cr, parent_cid):
    runs = deep_get(cr, "contentText", "runs") or []
    text = "".join(r.get("text", "") for r in runs) or deep_get(cr, "contentText", "simpleText") or ""
    handle = deep_get(cr, "authorText", "simpleText") or ""
    channel_id = deep_get(cr, "authorEndpoint", "browseEndpoint", "browseId")
    action_buttons = cr.get("actionButtons")
    hearted = bool(
        deep_get(action_buttons, "commentActionButtonsRenderer", "creatorHeart")
        if isinstance(action_buttons, dict)
        else cr.get("creatorHeart")
    )
    like_text = deep_get(cr, "voteCount", "simpleText")
    published = deep_get(cr, "publishedTimeText", "runs", 0, "text")
    reply_count = cr.get("replyCount")
    return {
        "cid": cr.get("commentId"),
        "text": text,
        "author": handle,
        "author_channel_id": channel_id,
        "author_url": _author_url(handle, channel_id),
        "author_thumbnail": deep_get(cr, "authorThumbnail", "thumbnails", -1, "url"),
        "like_count": parse_like_count(like_text),
        "like_count_text": like_text or None,
        "reply_count": int(reply_count) if isinstance(reply_count, int) else None,
        "published_text": published,
        "published_epoch": relative_time_to_epoch(published),
        "is_reply": parent_cid is not None,
        "parent_cid": parent_cid,
        "is_pinned": bool(cr.get("pinnedCommentBadge")),
        "is_hearted": hearted,
        "is_author_reply": bool(cr.get("authorIsChannelOwner")),
        "is_verified": bool(
            any(
                deep_get(b, "metadataBadgeRenderer", "tooltip") in ("Verified", "Official Artist Channel")
                for b in (cr.get("authorCommentBadge") and [cr["authorCommentBadge"]] or [])
            )
        ),
    }


def parse_response_page(response, parent_cid):
    """Return (list[record], next_page_token, {cid: replies_token})."""
    entities, toolbar_states, surfaces = build_entity_maps(response)
    records, reply_tokens, next_token = [], {}, None
    for it in extract_continuation_items(response):
        if "commentThreadRenderer" in it:
            ctr = it["commentThreadRenderer"]
            vm = deep_get(ctr, "commentViewModel", "commentViewModel")
            if vm:
                rec = parse_entity_comment(vm, entities, toolbar_states, surfaces, parent_cid)
            else:
                cr = deep_get(ctr, "comment", "commentRenderer")
                rec = parse_legacy_comment(cr, parent_cid) if cr else None
            if rec:
                records.append(rec)
                tok = find_replies_token(ctr)
                if tok:
                    reply_tokens[rec["cid"]] = tok
        elif "commentViewModel" in it:
            vm = it["commentViewModel"]
            if isinstance(vm, dict) and "commentViewModel" in vm:
                vm = vm["commentViewModel"]
            records.append(parse_entity_comment(vm, entities, toolbar_states, surfaces, parent_cid))
        elif "commentRenderer" in it:
            records.append(parse_legacy_comment(it["commentRenderer"], parent_cid))
        elif "continuationItemRenderer" in it:
            tok = deep_get(it, "continuationItemRenderer", "continuationEndpoint", "continuationCommand", "token")
            if tok:
                next_token = tok
    return records, next_token, reply_tokens


def paginate(cfg, start_token, opts, log, parent_cid=None, prefetched=None):
    """Loop continuation pages. Returns (records, {cid: replies_token})."""
    records, reply_tokens = [], {}
    limit = opts.reply_limit if parent_cid is not None else opts.limit
    token, page = start_token, 0
    while token:
        if opts.max_pages and page >= opts.max_pages:
            break
        if prefetched is not None and page == 0:
            response = prefetched
        else:
            try:
                response = call_next(cfg, token)
            except (NetworkError, HttpError) as e:
                if page == 0:
                    raise
                log(f"warning: stopping early after error: {e}")
                break
        page += 1
        page_records, next_token, page_reply_tokens = parse_response_page(response, parent_cid)
        reply_tokens.update(page_reply_tokens)
        for rec in page_records:
            records.append(rec)
            if limit and len(records) >= limit:
                next_token = None
                break
        if opts.verbose:
            log(f"page {page}: +{len(page_records)} ({len(records)} so far)")
        token = next_token
        if token:
            _polite_sleep(opts.sleep)
    return records, reply_tokens


def _polite_sleep(base):
    if base and base > 0:
        time.sleep(base + random.uniform(0, base))


def run_innertube(video_id, opts, log):
    html = fetch_watch_page(video_id, opts.hl, opts.gl)
    cfg = extract_config(html)
    start = resolve_start_token(cfg, cfg["yt_initial_data"], opts.sort, log)
    if start is None:
        raise CommentsDisabled("comments are disabled or unavailable for this video")

    tops, reply_tokens = paginate(cfg, start, opts, log, parent_cid=None)
    if opts.limit:
        tops = tops[:opts.limit]

    out = []
    for rec in tops:
        out.append(rec)
        if opts.replies:
            tok = reply_tokens.get(rec["cid"])
            if tok:
                _polite_sleep(opts.sleep)
                try:
                    reps, _ = paginate(cfg, tok, opts, log, parent_cid=rec["cid"])
                except (NetworkError, HttpError) as e:
                    log(f"warning: replies for {rec['cid']} failed: {e}")
                    reps = []
                if opts.reply_limit:
                    reps = reps[:opts.reply_limit]
                out.extend(reps)
    return out


# --------------------------------------------------------------------------- #
# yt-dlp engine
# --------------------------------------------------------------------------- #
def map_ytdlp_comment(c):
    parent = c.get("parent", "root")
    is_reply = parent not in (None, "root")
    return {
        "cid": c.get("id"),
        "text": c.get("text"),
        "author": c.get("author") or "",
        "author_channel_id": c.get("author_id"),
        "author_url": c.get("author_url"),
        "author_thumbnail": c.get("author_thumbnail"),
        "like_count": c.get("like_count"),
        "like_count_text": None,
        "reply_count": None,
        "published_text": c.get("_time_text"),
        "published_epoch": c.get("timestamp"),
        "is_reply": is_reply,
        "parent_cid": parent if is_reply else None,
        "is_pinned": bool(c.get("is_pinned")),
        "is_hearted": bool(c.get("is_favorited")),
        "is_author_reply": bool(c.get("author_is_uploader")),
        "is_verified": bool(c.get("author_is_verified")),
    }


def run_ytdlp(video_id, opts, log):
    if not shutil.which("yt-dlp"):
        raise EngineUnavailable("yt-dlp is not installed (PATH); use --engine innertube")
    parents = str(opts.limit) if opts.limit else "all"
    if opts.replies:
        per_thread = str(opts.reply_limit) if opts.reply_limit else "all"
        max_comments = f"all,{parents},all,{per_thread}"
    else:
        max_comments = f"all,{parents},0,0"
    sort = "new" if opts.sort == "new" else "top"
    ea = f"youtube:max_comments={max_comments};comment_sort={sort}"
    cmd = [
        "yt-dlp", "--dump-single-json", "--no-warnings", "--skip-download",
        "--write-comments", "--extractor-args", ea, video_id,
    ]
    log(f"running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if any(w in low for w in ("private video", "video unavailable", "does not exist", "removed")):
            raise VideoUnavailable(err[:300])
        if "comments are turned off" in low or "disabled comments" in low:
            raise CommentsDisabled(err[:300])
        raise EngineError(err[:300] or f"yt-dlp exited {proc.returncode}")
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ParseError(f"could not parse yt-dlp JSON: {e}")
    return [map_ytdlp_comment(c) for c in (info.get("comments") or [])]


# --------------------------------------------------------------------------- #
# Engine selection + output
# --------------------------------------------------------------------------- #
def select_engine(engine):
    if engine in ("ytdlp", "innertube"):
        return engine
    return "ytdlp" if shutil.which("yt-dlp") else "innertube"


def scrape(video_id, opts, log):
    engine = select_engine(opts.engine)
    log(f"engine: {engine}")
    if engine == "ytdlp":
        try:
            return run_ytdlp(video_id, opts, log)
        except (CommentsDisabled, VideoUnavailable):
            raise
        except ScrapeError as e:
            if opts.engine == "auto":
                log(f"yt-dlp failed ({e}); falling back to innertube engine")
                return run_innertube(video_id, opts, log)
            raise
    return run_innertube(video_id, opts, log)


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def write_output(records, opts):
    fmt = opts.format
    if opts.output:
        newline = "" if fmt == "csv" else None
        fh = open(opts.output, "w", encoding="utf-8", newline=newline)
        close = True
    else:
        fh = sys.stdout
        close = False
    try:
        if fmt == "csv":
            w = csv.writer(fh)
            w.writerow(FIELDS)
            for r in records:
                w.writerow([_csv_value(r.get(k)) for k in FIELDS])
        elif fmt == "jsonl":
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            json.dump(records, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    finally:
        if close:
            fh.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="scrape_comments.py",
        description="Scrape YouTube comments without the official API (terminal, free, no browser).",
    )
    p.add_argument("url", help="YouTube video URL or 11-char video id")
    p.add_argument("--limit", type=int, default=100,
                   help="max top-level comments (default 100; 0 = all)")
    p.add_argument("--sort", choices=("top", "new"), default="top", help="sort order (default top)")
    p.add_argument("--no-replies", dest="replies", action="store_false",
                   help="skip nested replies (replies are INCLUDED by default)")
    p.add_argument("--reply-limit", type=int, default=20,
                   help="max replies per comment when replies are on (default 20; 0 = all)")
    p.add_argument("--format", choices=("csv", "json", "jsonl"), default="csv",
                   help="output format (default csv)")
    p.add_argument("--output", "-o", help="write to FILE (default stdout)")
    p.add_argument("--engine", choices=("auto", "ytdlp", "innertube"), default="auto",
                   help="auto = yt-dlp if present, else stdlib innertube (default auto)")
    p.add_argument("--max-pages", type=int, default=0,
                   help="safety cap on continuation pages (0 = unlimited)")
    p.add_argument("--sleep", type=float, default=0.6,
                   help="base delay (s) between innertube requests (default 0.6)")
    p.add_argument("--hl", default="en", help="interface language (default en)")
    p.add_argument("--gl", default="US", help="geo/country (default US)")
    p.add_argument("--quiet", action="store_true", help="suppress progress on stderr")
    p.add_argument("--verbose", action="store_true", help="per-page progress on stderr")
    p.set_defaults(replies=True)
    return p.parse_args(argv)


def main(argv=None):
    opts = parse_args(argv)

    def log(msg):
        if not opts.quiet:
            print(f"[scrape] {msg}", file=sys.stderr)

    try:
        video_id = parse_video_id(opts.url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        records = scrape(video_id, opts, log)
    except VideoUnavailable as e:
        print(f"error: video unavailable: {e}", file=sys.stderr)
        return 3
    except CommentsDisabled as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    except (ConsentWall, EngineUnavailable) as e:
        print(f"error: {e} (try --engine ytdlp)", file=sys.stderr)
        return 5
    except NetworkError as e:
        print(f"error: network failure: {e}", file=sys.stderr)
        return 6
    except HttpError as e:
        print(f"error: HTTP {e.code}: {e}", file=sys.stderr)
        return 6
    except (ParseError, EngineError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 7

    records = order_threads(records)
    write_output(records, opts)
    log(f"done: {len(records)} comments "
        f"({sum(1 for r in records if not r['is_reply'])} top-level, "
        f"{sum(1 for r in records if r['is_reply'])} replies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
