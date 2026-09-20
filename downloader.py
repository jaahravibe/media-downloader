import difflib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

FFMPEG_AVAILABLE = False
FFMPEG_BIN = None
_ffmpeg_env = os.environ.get("FFMPEG_PATH")
for _bin in (_ffmpeg_env, "ffmpeg", "/data/data/com.termux/files/usr/bin/ffmpeg"):
    if not _bin:
        continue
    if shutil.which(_bin):
        try:
            subprocess.run(
                [_bin, "-version"], capture_output=True, check=True, timeout=30
            )
            FFMPEG_AVAILABLE = True
            FFMPEG_BIN = _bin
            break
        except Exception:
            continue

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Metadata fields the tags endpoint / --parse-metadata may write. Anything else
# arriving from the client is ignored so a crafted key can't inject a field name
# into the parse-metadata TO template.
ALLOWED_TAG_KEYS = frozenset({
    "artist", "album", "track", "track_number", "track_total",
    "disc_number", "disc_total", "genre", "year", "comment",
})

INFO_FIELDS = (
    "id,title,thumbnail,webpage_url,description,duration,duration_string,"
    "view_count,uploader,uploader_id,is_live,upload_date,channel"
)

# Codec + container selectors exposed to the UI. NOTE: `-f "best mp4"` (space
# form) is invalid — yt-dlp treats it as two formats (`best` plus an id literally
# named `mp4`) and errors with "Requested format is not available". Must use a
# bracketed selector or a full A+B merge spec.
VIDEO_FORMATS = [
    ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best", "MP4 (best quality)", "video"),
    ("mp4[height<=?1080]/best[height<=?1080]", "MP4 1080p", "video"),
    ("mp4[height<=?720]/best[height<=?720]", "MP4 720p", "video"),
    ("mp4[height<=?480]/best[height<=?480]", "MP4 480p", "video"),
    ("bestvideo+bestaudio/best", "Best (may need ffmpeg)", "video"),
    ("bestaudio[ext=m4a]+bestvideo[ext=mp4]/best[ext=mp4]/best", "MP4 (aac+h264)", "video"),
]
AUDIO_FORMATS = [
    ("bestaudio/best", "Audio (original)", "audio"),
    ("bestaudio[ext=m4a]/bestaudio/best", "Audio (m4a)", "audio"),
]

# When ffmpeg is present, add conversion-based options
if FFMPEG_AVAILABLE:
    AUDIO_FORMATS += [
        ("bestaudio/best", "MP3 320kbps", "audio", {"postprocessor": "ffmpeg", "acodec": "mp3", "abr": "320"}),
        ("bestaudio/best", "MP3 128kbps", "audio", {"postprocessor": "ffmpeg", "acodec": "mp3", "abr": "128"}),
        ("bestaudio/best", "M4A 192kbps", "audio", {"postprocessor": "ffmpeg", "acodec": "m4a", "abr": "192"}),
        ("bestaudio[ext=mp3]/bestaudio/best", "MP3 (native)", "audio"),
        ("bestaudio[ext=opus]/bestaudio/best", "Opus", "audio"),
        ("bestaudio[ext=wav]/bestaudio/best", "WAV", "audio"),
    ]


def get_ffmpeg_available():
    return FFMPEG_AVAILABLE


def is_safe_url(url):
    """Only accept network URLs. Prevents yt-dlp flag injection via crafted args."""
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _run_ytdlp(args, timeout=120):
    return subprocess.run(
        ["yt-dlp", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def extract_info(url, audio_only=False):
    """Return lightweight + full format info for a single URL."""
    cmd = [
        "-J",
        "--no-playlist",
        "--flat-playlist",
        "--no-warnings",
        "--js-runtimes", "node",
        "--socket-timeout", "20",
        "--retries", "2",
        url,
    ]
    proc = _run_ytdlp(cmd, timeout=180)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip() or "yt-dlp failed"}

    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    keep = {k: info.get(k) for k in INFO_FIELDS.split(",") if k in info}
    keep["_ffmpeg"] = FFMPEG_AVAILABLE
    keep["_is_playlist"] = info.get("_type") == "playlist"

    if keep["_is_playlist"]:
        entries = []
        for i, e in enumerate(info.get("entries") or []):
            entries.append({
                "index": i + 1,
                "id": e.get("id"),
                "title": e.get("title") or f"Video {i + 1}",
                "url": e.get("webpage_url") or e.get("url") or e.get("id", ""),
                "duration": e.get("duration"),
            })
        keep["entries"] = entries[:500]
        keep["entries_total"] = len(entries)
        if len(entries) > 500:
            keep["entries_truncated"] = True
        return keep

    # Available video formats (unique by height)
    videos = []
    seen = set()
    for f in info.get("formats") or []:
        if not f.get("vcodec") or f.get("vcodec") == "none":
            continue
        if f.get("format_id") == "sb0":
            continue
        h = f.get("height") or 0
        key = (h, f.get("ext"))
        if key in seen:
            continue
        seen.add(key)
        videos.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": h,
                "width": f.get("width"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }
        )
    videos.sort(key=lambda x: (x["height"] or 0), reverse=True)
    keep["video_formats"] = videos

    audio = []
    seen_a = set()
    for f in info.get("formats") or []:
        if not f.get("acodec") or f.get("acodec") == "none":
            continue
        if f.get("format_id") == "sb0":
            continue
        key = f.get("ext")
        abr = f.get("abr") or 0
        k2 = (key, abr)
        if k2 in seen_a:
            continue
        seen_a.add(k2)
        audio.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "abr": abr,
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }
        )
    audio.sort(key=lambda x: x.get("abr") or 0, reverse=True)
    keep["audio_formats"] = audio
    return keep


# --- Song metadata lookup (iTunes — Deezer fallback) -------------------------

_TITLE_NOISE = re.compile(
    r"\([^)]*(official|lyrics?|audio|video|remaster|hd|4k|slowed|sped)[^)]*\)"
    r"|\[[^\]]*(official|lyrics?|audio|video|remaster|hd|4k|slowed|sped)[^\]]*\]"
    r"|\bfeat(?:uring)?.{0,40}$|\bft\..*$",
    re.IGNORECASE,
)


def _normalize_for_match(text):
    s = _TITLE_NOISE.sub(" ", str(text or ""))
    s = re.sub(r"[|\/_\-#()\[\]]+", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def _match_score(title, track):
    a, b = _normalize_for_match(title), _normalize_for_match(track)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        return 0.8 + 0.2 * (len(short) / len(long_))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _catalog_query(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; MediaDownloader/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _itunes_artwork(r):
    art = r.get("artworkUrl100") or r.get("artworkUrl60") or ""
    if not art:
        return ""
    # iTunes artwork URLs carry a `/<W>x<H>bb.ext` size token; bump it.
    return re.sub(r"/\d+x\d+\w*(\.\w+)$", r"/600x600bb\1", art)


def _itunes_candidates(title):
    url = (
        "https://itunes.apple.com/search?media=music&entity=song&limit=10&term="
        + urllib.parse.quote(title)
    )
    try:
        data = _catalog_query(url)
    except Exception:
        return []
    out = []
    for r in data.get("results") or []:
        tags = {
            "artist": r.get("artistName"),
            "album": r.get("collectionName"),
            "track": r.get("trackName"),
            "track_number": r.get("trackNumber"),
            "track_total": r.get("trackCount"),
            "year": str(r.get("releaseDate") or "")[:4],
        }
        out.append({
            "tags": tags,
            "source": "iTunes",
            "score": _match_score(title, tags.get("track") or ""),
            "artwork": _itunes_artwork(r),
        })
    return out


def _deezer_candidates(title):
    url = "https://api.deezer.com/search?limit=10&q=" + urllib.parse.quote(title)
    try:
        data = _catalog_query(url)
    except Exception:
        return []
    out = []
    for r in data.get("data") or []:
        album = r.get("album") or {}
        tags = {
            "artist": (r.get("artist") or {}).get("name"),
            "album": album.get("title"),
            "track": r.get("title"),
            "track_number": r.get("track_position"),
            "track_total": album.get("nb_tracks"),
            "year": str(album.get("release_date") or "")[:4],
        }
        out.append({
            "tags": tags,
            "source": "Deezer",
            "score": _match_score(title, tags.get("track") or ""),
            "artwork": album.get("cover_xl") or album.get("cover_big")
            or album.get("cover_medium") or "",
        })
    return out


def lookup_song_info(title, uploader=None):
    """Find real song metadata for an audio rip via the iTunes/Deezer catalogs.

    Both catalogs are queried **in parallel** (each with its own 8s timeout;
    total wait is about the slower of the two, not their sum). Failures are
    silent: audio downloads must never depend on the catalogs.

    Returns:
      {"status": "auto",   "tags": {...}, "artwork": url,
       "candidates": [...]}                   one confident match (≤ 10 cards)
      {"status": "review", "candidates": [...],
       "fallback_tags": {...}}                several plausible matches (≤ 10)
      {"status": "none"}                      nothing usable

    `artwork` is a sibling of `tags`; it is never fed into --parse-metadata.
    Catalog failures are silent: audio downloads must never depend on this.
    """
    if not title:
        return {"status": "none"}
    with ThreadPoolExecutor(max_workers=2) as ex:
        itunes = ex.submit(_itunes_candidates, title)
        deezer = ex.submit(_deezer_candidates, title)
        candidates = itunes.result() + deezer.result()
    candidates = [c for c in candidates if c["score"] > 0.0]
    if not candidates:
        return {"status": "none"}
    seen = set()
    uniq = []
    for c in candidates:
        key = (
            (c["tags"].get("artist") or "").lower().strip(),
            (c["tags"].get("track") or "").lower().strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=lambda c: c["score"], reverse=True)
    best = uniq[0]
    if best["score"] >= 0.82:
        return {
            "status": "auto",
            "tags": best["tags"],
            "artwork": best.get("artwork") or "",
            "candidates": uniq[:10],
        }
    review = [c for c in uniq if c["score"] >= 0.4][:10]
    if not review:
        return {"status": "none"}
    first = review[0]["tags"]
    return {
        "status": "review",
        "candidates": review,
        "fallback_tags": {
            "artist": uploader or first.get("artist"),
            "track": title,
            "album": None,
            "track_number": None,
            "year": None,
        },
    }


def sniff_image(data):
    """Return a stable image type name from magic bytes, or None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


class _HostRestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses any hop leaving the allowlisted hosts.

    A bare `urlopen` follows up to 10 redirects without re-validating the
    destination, turning a catalog art URL's allowlist check into a one-shot
    gateway (redirect to any internal/hostile host). This handler makes every
    hop pay the same host check.
    """

    def __init__(self, allowed_hosts):
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            host = urllib.parse.urlsplit(newurl).hostname or ""
        except ValueError:
            host = ""
        if not self._allowed_hosts(host):
            raise urllib.error.HTTPError(
                newurl, code, f"Redirect to disallowed host: {host or 'unknown'}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_artwork_bytes(url, max_size=8 * 1024 * 1024, allowed_hosts=None):
    """Fetch official album art into memory. Returns (data, image_kind) or (None, None).

    Non-fatal by contract: any failure (network, size cap, non-image) -> (None, None).
    `allowed_hosts` is an optional predicate checked against the initial URL's
    host and, when given, re-checked on every redirect hop.
    """
    if not is_safe_url(url):
        return None, None
    if allowed_hosts is not None:
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except ValueError:
            return None, None
        if not allowed_hosts(host):
            return None, None
    try:
        handlers = []
        if allowed_hosts is not None:
            handlers.append(_HostRestrictedRedirectHandler(allowed_hosts))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; MediaDownloader/1.0)"}
        )
        with opener.open(req, timeout=8) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype and not ctype.startswith("image/"):
                return None, None
            data = b""
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > max_size:
                    return None, None
    except Exception:
        return None, None
    kind = sniff_image(data)
    if kind is None:
        return None, None
    return data, kind


def fetch_artwork(url, dest, max_size=8 * 1024 * 1024):
    """Download official album art to `dest`. Returns True on success.

    Non-fatal by contract: any failure (network, size cap, non-image) -> False.
    """
    data, _kind = fetch_artwork_bytes(url, max_size)
    if data is None:
        return False
    try:
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except OSError:
        return False


def _sanitize_tag(value):
    """Collapse control chars and cap length so a tag can't break metadata parsing."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return s[:200]


def build_download_command(url, format_spec, output_path, task_id=None,
                           audio_convert=None, tag_overrides=None, embed=True,
                           cover="video"):
    """Construct the yt-dlp subprocess command.

    audio_convert: dict {postprocessor: 'ffmpeg', out_ext: str, abr: str}.
        A present out_ext means ffmpeg extraction/conversion; an absent one
        (raw cards like "Audio (original)") means remux-embedding only.
    tag_overrides: dict {artist/album/track/track_number/year: value} applied
        as --parse-metadata literal overrides before --embed-metadata.
    embed: emit --embed-metadata (and yt-dlp thumbnail embedding for
        converted outputs requires cover == "video").
    cover: "video" -> yt-dlp embeds the video thumbnail (fallback, default);
        "art" -> suppress video-thumbnail fetch, the caller attaches official
        album art after the download; "none" -> no artwork at all.
    """
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--js-runtimes", "node",
        "--socket-timeout", "20",
        "--retries", "2",
        "-f", format_spec,
        "-o", output_path,
        "--newline",
        "--progress",
        "--write-info-json",
    ]
    if audio_convert and audio_convert.get("postprocessor") == "ffmpeg":
        out_ext = audio_convert.get("out_ext") or audio_convert.get("acodec")
        if out_ext:
            cmd += [
                "--extract-audio",
                "--audio-format", out_ext,
                "--audio-quality", audio_convert.get("abr", "128"),
            ]
            if embed and cover == "video":
                # Cover art is only embeddable into container formats yt-dlp
                # supports (mp3/m4a/opus/flac/mkv/m4v); webm aborts pp. So the
                # video-thumbnail fallback is bound to converted outputs only.
                cmd += ["--embed-thumbnail"]
    if embed:
        cmd += ["--embed-metadata"]
        for key, value in (tag_overrides or {}).items():
            if key not in ALLOWED_TAG_KEYS:
                # Defensive: never let a client field name reach the TO template.
                continue
            literal = _sanitize_tag(value)
            if literal:
                # yt-dlp splits --parse-metadata on the LAST colon and wraps a
                # bare field name into %(field)s, so "LITERAL:%(field)s" assigns
                # the literal to that metadata field. BUT a FROM that is a pure
                # [A-Za-z_]+ word (e.g. the artist "KPHK") is wrapped too, and an
                # unknown field evaluates to the NA placeholder -- so the literal
                # would be written as "NA". Prefix a space so the FROM stays a
                # plain literal and use a regex TO that drops the leading space.
                if re.fullmatch(r"[A-Za-z_]+", literal):
                    cmd += ["--parse-metadata", f" {literal}:(?s)^ ?(?P<{key}>.+)$"]
                else:
                    cmd += ["--parse-metadata", f"{literal}:%({key})s"]
                if key == "year":
                    # FFmpegMetadataPP writes the ID3 date/TDRC frame straight
                    # from info['upload_date'] (add('date', 'upload_date')); the
                    # `year` field alone is never read for embedding. So two
                    # overrides: meta_date carries the bare year (copied
                    # verbatim into the date tag by the meta_* loop), and
                    # upload_date keeps a strptime-safe YYYYMMDD form for the
                    # built-in date-range check.
                    ym = re.fullmatch(r"(\d{4})", literal)
                    if ym:
                        cmd += ["--parse-metadata", f"{literal}:%(meta_date)s"]
                        cmd += ["--parse-metadata", f"{literal}0101:%(upload_date)s"]
                    else:
                        cmd += ["--parse-metadata", f"{literal}:%(upload_date)s"]
    cmd.append(url)
    return cmd