import base64
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import uuid
from queue import Queue, Empty as QueueEmpty

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import downloader

app = Flask(__name__)

SSE_CLIENTS = {}
SSE_LATEST = {}
TASKS = {}
FILE_STATE = {}
FINISHED_META = {}
SESSION_TASKS = {}
STATE_LOCK = threading.Lock()

# Global bounds: keep concurrent yt-dlp subprocesses + download threads small
# (a phone-class host), and refuse to start when the disk is nearly full.
MAX_CONCURRENT_DOWNLOADS = 2
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)
MIN_FREE_BYTES = 200 * 1024 * 1024
MAX_LATEST_STATES = 500
MAX_IMG_BYTES = 8 * 1024 * 1024

# Optional auth: set MD_TOKEN=<secret> to require a bearer token on every API
# call (except the /api/ffmpeg health probe). Token arrives via the
# Authorization header or a `token` query parameter (EventSource / sendBeacon
# cannot set headers).
TOKEN = (os.environ.get("MD_TOKEN") or "").strip()


def _auth_ok():
    if not TOKEN:
        return True
    if request.headers.get("Authorization") == f"Bearer {TOKEN}":
        return True
    return request.args.get("token", "") == TOKEN


def _require_auth():
    if not _auth_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _disk_ok():
    try:
        return shutil.disk_usage(downloader.DOWNLOAD_DIR).free >= MIN_FREE_BYTES
    except OSError:
        return True


def _kill_task(task_id):
    """Terminate a task's yt-dlp process (if running) and drop its server state.

    The yt-dlp subprocess owns the file paths for this task, so it must be
    stopped *before* any on-disk cleanup, otherwise it can re-create deleted
    files after the cleanup. A running task that is superseded would otherwise
    orphan its process, re-write its outputs, and later emit a stale ready
    event pointing at a deleted file.
    """
    with STATE_LOCK:
        entry = TASKS.pop(task_id, None)
    proc = entry.get("proc") if isinstance(entry, dict) else None
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass


def _delete_task_file(task_id):
    """Remove all on-disk files belonging to a task and drop its state."""
    _kill_task(task_id)
    count = 0
    with STATE_LOCK:
        try:
            for f in os.listdir(downloader.DOWNLOAD_DIR):
                if f.startswith(task_id + "_"):
                    try:
                        os.remove(os.path.join(downloader.DOWNLOAD_DIR, f))
                        count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        FILE_STATE.pop(task_id, None)
        SSE_LATEST.pop(task_id, None)
        FINISHED_META.pop(task_id, None)
        for sess in SESSION_TASKS.values():
            if task_id in sess:
                sess.remove(task_id)
    return count


def _clear_task_files(task_id):
    """Remove on-disk files for a task without dropping its state (restart)."""
    with STATE_LOCK:
        try:
            for f in os.listdir(downloader.DOWNLOAD_DIR):
                if f.startswith(task_id + "_"):
                    try:
                        os.remove(os.path.join(downloader.DOWNLOAD_DIR, f))
                    except OSError:
                        pass
        except OSError:
            pass


def _fetch_cover(task_id, url):
    """Download official album art to the task's cover slot; path or None."""
    if not url:
        return None
    dest = os.path.join(downloader.DOWNLOAD_DIR, f"{task_id}_cover.jpg")
    if downloader.fetch_artwork(url, dest):
        return dest
    return None


def _format_candidates(cands):
    return [
        {
            "tags": c.get("tags"),
            "source": c.get("source"),
            "score": round(c.get("score") or 0, 2),
            "artwork": c.get("artwork") or "",
        }
        for c in cands
    ]


def _clean_tag(v):
    """Normalize a user/catalog-supplied tag value or blank it.

    yt-dlp and some catalogs use "NA"-like sentinels for missing metadata;
    treat them as blank so a real name wins.
    """
    s = str(v or "").strip()
    if s.lower() in ("na", "n/a", "n\\a", "unknown", "-"):
        return ""
    return s


def _gen_is_current(task_id, gen):
    with STATE_LOCK:
        entry = TASKS.get(task_id)
        return isinstance(entry, dict) and entry.get("_gen") == gen


def _pic_dims(path):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            w, h = probe.stdout.strip().split(",")
            return int(w), int(h)
    except Exception:
        pass
    return None, None


def _embed_cover(task_id, media):
    """Attach the task's fetched album art to its final media file.

    Non-fatal by contract: any failure leaves the (already tagged) file alone.
    Returns True when the artwork was attached.
    """
    with STATE_LOCK:
        entry = TASKS.get(task_id)
        cover = entry.get("cover_path") if isinstance(entry, dict) else None
    if not isinstance(entry, dict) or not entry.get("embed"):
        return False
    if not cover or not os.path.exists(cover):
        return False
    try:
        with open(cover, "rb") as f:
            img = downloader.sniff_image(f.read(32))
    except OSError:
        return False
    if not img:
        return False
    ext = os.path.splitext(media)[1].lstrip(".").lower()
    try:
        if ext == "mp3":
            tmp = media + ".covertmp"
            subprocess.run(
                [downloader.FFMPEG_BIN, "-v", "error", "-y",
                 "-i", media, "-i", cover,
                 "-f", "mp3",
                 "-c", "copy", "-map", "0:0", "-map", "1:0",
                 "-disposition:1", "attached_pic",
                 "-id3v2_version", "3",
                 "-metadata:s:v", "title=Album cover",
                 "-metadata:s:v", "comment=Cover (front)",
                 tmp],
                capture_output=True, timeout=120, check=True,
            )
            os.replace(tmp, media)
        elif ext in ("m4a", "mp4", "m4v", "mov"):
            tmp = media + ".covertmp"
            subprocess.run(
                [downloader.FFMPEG_BIN, "-v", "error", "-y",
                 "-i", media, "-i", cover,
                 "-f", "mp4",
                 "-c", "copy", "-map", "0", "-map", "1:0",
                 "-disposition:1", "attached_pic", tmp],
                capture_output=True, timeout=120, check=True,
            )
            os.replace(tmp, media)
        elif ext in ("flac", "ogg", "opus"):
            from mutagen.flac import FLAC, Picture
            with open(cover, "rb") as f:
                data = f.read()
            pic = Picture()
            pic.data = data
            pic.type = 3  # front cover
            pic.mime = "image/" + img
            w, h = _pic_dims(cover)
            if w:
                pic.width, pic.height = w, h
            if ext == "flac":
                f = FLAC(media)
                f.add_picture(pic)
                f.save()
            else:
                if ext == "opus":
                    from mutagen.oggopus import OggOpus
                    f = OggOpus(media)
                else:
                    from mutagen.oggvorbis import OggVorbis
                    f = OggVorbis(media)
                f["METADATA_BLOCK_PICTURE"] = (
                    base64.b64encode(pic.write()).decode("ascii")
                )
                f.save()
        else:
            return False
        return True
    except Exception:
        return False


def _write_tags(task_id, media, overrides, cover_path=None):
    """Write a finished media file's song tags (+ optional artwork) in place.

    Uses mutagen for every supported container (mp3 / m4a / mp4 / flac / ogg /
    opus) so it works whether or not ffmpeg is installed. Non-fatal by
    contract: any failure leaves the file untouched. Returns True on success.

    cover_path: path to artwork to embed, or the sentinel "\x00remove" to
    strip existing artwork, or None to leave artwork untouched.
    """
    if not overrides and cover_path is None:
        return False
    if not media or not os.path.isfile(media):
        return False
    remove_art = cover_path == "\x00remove"
    ext = os.path.splitext(media)[1].lstrip(".").lower()
    year = _clean_tag(overrides.get("year") or "") if overrides else ""
    try:
        if ext in ("mp3",):
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TYER, TDRC
            try:
                tags = ID3(media)
            except Exception:
                from mutagen.id3 import ID3NoHeaderError
                tags = ID3()
            if overrides:
                for key, frame_cls in (("track", TIT2), ("artist", TPE1), ("album", TALB)):
                    v = _clean_tag(overrides.get(key) or "")
                    if v:
                        tags.delall(frame_cls.__name__)
                        tags.add(frame_cls(encoding=3, text=[v]))
                if year:
                    if tags.version >= (2, 4):
                        tags.delall("TDRC")
                        tags.add(TDRC(encoding=3, text=[year]))
                    else:
                        tags.delall("TYER")
                        tags.add(TYER(encoding=3, text=[year]))
            if remove_art:
                tags.delall("APIC")
            elif cover_path and os.path.exists(cover_path):
                from mutagen.id3 import APIC
                with open(cover_path, "rb") as f:
                    cover_bytes = f.read()
                with open(cover_path, "rb") as f:
                    mime = "image/" + (downloader.sniff_image(f.read(32)) or "jpg")
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes))
            tags.save(media)
        elif ext in ("m4a", "mp4", "m4v", "mov"):
            from mutagen.mp4 import MP4, MP4Cover
            tags = MP4(media)
            mapping = {
                "artist": "\xa9ART",
                "album": "\xa9alb",
                "track": "\xa9nam",
            }
            for key, atom in mapping.items():
                v = _clean_tag(overrides.get(key) or "")
                if v:
                    tags[atom] = [v]
            if year:
                tags["\xa9day"] = [year]
            tags.pop("covr", None)
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as f:
                    tags["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
            tags.save(media)
        elif ext == "flac":
            from mutagen.flac import FLAC, Picture
            tags = FLAC(media)
            if overrides:
                for key in ("artist", "album", "track", "year"):
                    v = _clean_tag(overrides.get(key) or "")
                    if v:
                        tags[key] = [v]
            tags.clear_pictures()
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as f:
                    data = f.read()
                pic = Picture()
                pic.type = 3
                pic.mime = "image/" + (downloader.sniff_image(data[:32]) or "jpg")
                pic.data = data
                w, h = _pic_dims(cover_path)
                if w:
                    pic.width, pic.height = w, h
                tags.add_picture(pic)
            tags.save(media)
        elif ext in ("ogg", "opus"):
            if ext == "opus":
                from mutagen.oggopus import OggOpus
                tags = OggOpus(media)
            else:
                from mutagen.oggvorbis import OggVorbis
                tags = OggVorbis(media)
            if overrides:
                for key in ("artist", "album", "track", "year"):
                    v = _clean_tag(overrides.get(key) or "")
                    if v:
                        tags[key] = [v]
            tags.pop("METADATA_BLOCK_PICTURE", None)
            if cover_path and os.path.exists(cover_path):
                from mutagen.flac import Picture
                with open(cover_path, "rb") as f:
                    data = f.read()
                pic = Picture()
                pic.type = 3
                pic.mime = "image/" + (downloader.sniff_image(data[:32]) or "jpg")
                pic.data = data
                w, h = _pic_dims(cover_path)
                if w:
                    pic.width, pic.height = w, h
                tags["METADATA_BLOCK_PICTURE"] = (
                    base64.b64encode(pic.write()).decode("ascii")
                )
            tags.save(media)
        else:
            return False
        return True
    except Exception:
        return False


def _clear_cover(task_id):
    """Delete the task's fetched cover file and forget it.

    User-uploaded covers (entry["_cover_uploaded"]) are kept until the task is
    deleted so the result card can keep showing them; only fetched covers are
    transient.
    """
    with STATE_LOCK:
        entry = TASKS.get(task_id)
        if isinstance(entry, dict):
            cover = entry.get("cover_path")
            uploaded = bool(entry.get("_cover_uploaded"))
            entry["cover_path"] = None
        else:
            cover = None
            uploaded = False
    if cover and not uploaded:
        try:
            os.remove(cover)
        except OSError:
            pass


def _existing_cover(task_id):
    """Return the path of the task's canonical cover on disk, or None."""
    try:
        names = os.listdir(downloader.DOWNLOAD_DIR)
    except OSError:
        return None
    for n in names:
        if n.startswith(f"{task_id}_cover."):
            p = os.path.join(downloader.DOWNLOAD_DIR, n)
            if os.path.isfile(p):
                return p
    return None


def _set_cover_uploaded(task_id, flag):
    with STATE_LOCK:
        entry = TASKS.get(task_id)
        if isinstance(entry, dict):
            entry["_cover_uploaded"] = bool(flag)


def _finished_media(task_id):
    """Return the finished media file path for a completed task, or None."""
    try:
        names = os.listdir(downloader.DOWNLOAD_DIR)
    except OSError:
        return None
    cands = []
    for n in names:
        if not n.startswith(task_id + "_"):
            continue
        if n.endswith((".part", ".covertmp", ".tmp", ".webp", ".png", ".jpg",
                       ".jpeg", ".gif", ".info.json")):
            continue
        p = os.path.join(downloader.DOWNLOAD_DIR, n)
        if os.path.isfile(p):
            cands.append(p)
    if not cands:
        return None
    cands.sort(key=os.path.getsize, reverse=True)
    return cands[0]


def _extract_ytdlp_error(err_lines, out_lines):
    """Return the most relevant yt-dlp error message, if any."""
    combined = list(err_lines) + list(out_lines)
    for line in reversed(combined):
        if line.startswith("ERROR") or "ERROR:" in line:
            msg = line.split("ERROR:", 1)[-1].strip() or line.strip()
            return msg[:400]
    return ""


def parse_progress_line(line, task_id):
    m = re.match(r"^\[download\]\s+([\d.]+)%", line)
    if m and "Finished downloading" not in line:
        update = {
            "task_id": task_id,
            "phase": "downloading",
            "indeterminate": False,
            "percent": float(m.group(1)),
            "eta": None,
            "speed": None,
            "status": "downloading",
        }
        eta = re.search(r"ETA ([\d:]+)", line)
        speed = re.search(r"at\s+([\d.]+[A-Za-z/]+)", line)
        if eta:
            update["eta"] = eta.group(1)
        if speed:
            update["speed"] = speed.group(1)
        return update
    if line.startswith("[download] Destination"):
        return {"task_id": task_id, "phase": "downloading",
                "indeterminate": False, "status": "started"}
    if "[extractinfo]" in line or "Extracting URL" in line:
        return {"task_id": task_id, "phase": "extracting",
                "indeterminate": True, "status": "extracting"}
    if line.startswith("[Merger]") or line.startswith("[ExtractAudio]") \
            or line.startswith("[ffmpeg]") or line.startswith("[VideoConvertor]"):
        return {"task_id": task_id, "phase": "processing",
                "indeterminate": True, "status": "processing"}
    if line.startswith("ERROR") or "ERROR:" in line:
        msg = line.split("ERROR:", 1)[-1].strip()
        return {"task_id": task_id, "phase": "error",
                "status": "error", "message": msg or line}
    return None


def notify(task_id, event, data=None):
    msg = {"event": event, "data": data or {}}
    with STATE_LOCK:
        SSE_LATEST[task_id] = msg
        if len(SSE_LATEST) > MAX_LATEST_STATES:
            # dict preserves insertion order; drop the oldest replay.
            SSE_LATEST.pop(next(iter(SSE_LATEST)), None)
        q = SSE_CLIENTS.get(task_id)
    if q is not None:
        try:
            q.put_nowait(msg)
        except Exception:
            pass


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/ffmpeg")
def api_ffmpeg():
    return jsonify({"available": downloader.get_ffmpeg_available()})


@app.route("/api/art")
def api_art():
    """Proxy a catalog album-cover URL so the client never depends on hotlink reachability.

    Restricted to iTunes/Deezer cover hosts. Fetches once, streams image bytes with
    long-lived caching. Any failure -> 404 (the card shows its placeholder).
    """
    denied = _require_auth()
    if denied:
        return denied
    url = request.args.get("u", "").strip()
    if not downloader.is_safe_url(url):
        return Response("", status=404)
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return Response("", status=404)
    if not (host.endswith(".mzstatic.com") or host.endswith(".dzcdn.net")):
        return Response("", status=404)
    # Re-check the allowlist on every redirect hop, not just the initial URL.
    data, kind = downloader.fetch_artwork_bytes(
        url,
        allowed_hosts=lambda h: h.endswith(".mzstatic.com") or h.endswith(".dzcdn.net"),
    )
    if data is None:
        return Response("", status=404)
    mime = {
        "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp",
    }.get(kind, "application/octet-stream")
    return Response(
        data, content_type=mime,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.route("/api/art-local/<task_id>")
def api_art_local_get(task_id):
    """Serve a task's canonical cover (uploaded custom art, or a fetched cover).

    Narrow scope: the filename is forced to the task's canonical cover name, so
    this can never serve a download. Auth-gated like every other endpoint.
    """
    denied = _require_auth()
    if denied:
        return denied
    if not task_id or not task_id.isalnum():
        return Response("", status=400)
    path = _existing_cover(task_id)
    if not path:
        return Response("", status=404)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
    return send_file(
        path, mimetype=mime, as_attachment=False, conditional=True,
        max_age=86400,
    )


@app.route("/api/art-local", methods=["POST"])
def api_art_local_upload():
    """Accept a user-uploaded cover image for a task (manual art, `file` mode).

    Guarded like the tags endpoint (auth + XHR header + session ownership).
    Validates size (8 MB cap) and image magic bytes, then stores it into the
    task's canonical cover slot so `_embed_cover` picks it up. Returns the URL
    the client uses as `art.url` / the ready-payload `artwork`.
    """
    denied = _require_auth()
    if denied:
        return denied
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "Forbidden"}), 403
    session = (request.args.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 403
    task_id = (request.args.get("task") or "").strip()
    if not task_id or not task_id.isalnum():
        return jsonify({"error": "Bad task id"}), 400
    with STATE_LOCK:
        owned = task_id in SESSION_TASKS.get(session, [])
    if not owned:
        return jsonify({"error": "Not your task"}), 403
    if not _disk_ok():
        return jsonify({"error": "Not enough free disk space"}), 507
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "No file provided"}), 400
    data = f.read(MAX_IMG_BYTES + 1)
    if not data or len(data) > MAX_IMG_BYTES:
        return jsonify({"error": "Image too large (max 8 MB)"}), 413
    kind = downloader.sniff_image(data)
    if kind is None:
        return jsonify({"error": "Not a supported image (JPEG, PNG, GIF, WebP or BMP)"}), 400
    ext = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}[kind]
    old = _existing_cover(task_id)
    if old:
        try:
            os.remove(old)
        except OSError:
            pass
    dest = os.path.join(downloader.DOWNLOAD_DIR, f"{task_id}_cover.{ext}")
    try:
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError:
        return jsonify({"error": "Could not store the image"}), 500
    with STATE_LOCK:
        entry = TASKS.get(task_id)
        if isinstance(entry, dict):
            entry["cover_path"] = dest
            entry["_cover_uploaded"] = True
    return jsonify({"url": f"/api/art-local/{task_id}", "ext": ext})


@app.route("/api/info")
def api_info():
    denied = _require_auth()
    if denied:
        return denied
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not downloader.is_safe_url(url):
        return jsonify({"error": "Only http:// or https:// URLs are accepted"}), 400
    info = downloader.extract_info(url)
    if "error" in info:
        return jsonify(info), 400
    if request.args.get("mode") == "audio" and not info.get("_is_playlist"):
        title = str(info.get("title") or "").strip()[:300]
        creator = str(info.get("creator") or "").strip()[:200] or str(
            info.get("uploader") or "").strip()[:200]
        result = downloader.lookup_song_info(title, creator)
        candidates = _format_candidates(result.get("candidates") or [])
        if not candidates:
            candidates = _format_candidates([{
                "tags": {"artist": creator, "track": title},
                "source": "Video title",
                "score": 1.0,
                "artwork": "",
            }])
        info["tag_candidates"] = candidates
    return jsonify(info)


@app.route("/api/formats")
def api_formats():
    denied = _require_auth()
    if denied:
        return denied
    return jsonify({
        "video_formats": downloader.VIDEO_FORMATS,
        "audio_formats": downloader.AUDIO_FORMATS,
    })


@app.route("/api/prepare", methods=["POST"])
def api_prepare():
    denied = _require_auth()
    if denied:
        return denied
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not downloader.is_safe_url(url):
        return jsonify({"error": "Only http:// or https:// URLs are accepted"}), 400

    mode = data.get("mode", "video")
    format_label = data.get("format", "best mp4")
    batch = data.get("batch", False)
    session = str(data.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 400
    task_id = uuid.uuid4().hex[:16]

    if not batch:
        if session:
            with STATE_LOCK:
                previous = list(SESSION_TASKS.get(session, []))
        else:
            previous = []
        for old in previous:
            if old != task_id:
                _delete_task_file(old)
        with STATE_LOCK:
            SESSION_TASKS[session] = [task_id]
    else:
        if session:
            with STATE_LOCK:
                SESSION_TASKS.setdefault(session, []).append(task_id)

    output_tpl = os.path.join(
        downloader.DOWNLOAD_DIR, f"{task_id}_%(title).200B.%(ext)s"
    )

    audio_convert = None
    format_spec = "best"

    if mode == "audio":
        format_spec = "bestaudio/best"
        for fmt in downloader.AUDIO_FORMATS:
            if len(fmt) == 4 and fmt[2] == "audio" and fmt[1] == format_label:
                audio_convert = fmt[3]
                break
        if not audio_convert:
            audio_labels = {
                fmt[1] for fmt in downloader.AUDIO_FORMATS
                if len(fmt) >= 3 and fmt[2] == "audio"
            }
            if format_label not in audio_labels:
                # Unknown label: degrade to a raw bestaudio download rather than
                # forcing an ffmpeg conversion the host may not support.
                audio_convert = None
    else:
        format_spec = "best"
        for fmt in downloader.VIDEO_FORMATS:
            if fmt[1] == format_label:
                format_spec = fmt[0]
                break
        if format_label == "MP4 (best)":
            format_spec = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/bestvideo+bestaudio/best"
            )
        h_match = re.search(r"MP4\s+(\d+)p", format_label)
        if h_match:
            h = h_match.group(1)
            format_spec = (
                f"bestvideo[height<={h}][ext=mp4]+"
                "bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+"
                "bestaudio/best"
            )

    embed_tags = bool(data.get("tags"))
    if mode != "audio" or not downloader.get_ffmpeg_available():
        embed_tags = False

    # All audio downloads keep the video's own metadata + video thumbnail
    # (tag_mode "keep"). Song tags and album art are applied afterwards via
    # the post-download retag flow, never block the download.
    tag_mode = "keep" if embed_tags else None
    tag_overrides = None
    tag_candidates = []
    artwork_url = ""
    cover_path = None
    cover = "video"

    cmd = downloader.build_download_command(
        url, format_spec, output_tpl, task_id, audio_convert,
        tag_overrides=tag_overrides, embed=embed_tags, cover=cover,
    )

    with STATE_LOCK:
        TASKS[task_id] = {
            "cmd": cmd,
            "proc": None,
            "_pending": bool(tag_candidates),
            "_gen": 0,
            "url": url,
            "format_spec": format_spec,
            "output_tpl": output_tpl,
            "audio_convert": audio_convert,
            "embed": embed_tags,
            "cover_path": cover_path,
            "artwork": artwork_url,
            "cover": cover,
            "tags": tag_overrides if batch else None,
            "tags_mode": tag_mode if batch else None,
            "_title": str(data.get("title") or "").strip()[:300],
            "_creator": str(data.get("creator") or "").strip()[:200],
        }

    if tag_candidates:
        return jsonify({
            "task_id": task_id,
            "needs_tags": True,
            "tag_candidates": tag_candidates,
        })

    _spawn_download(task_id)
    return jsonify({
        "task_id": task_id,
        "artwork": artwork_url,
        "tags": tag_overrides,
        "tag_candidates": tag_candidates,
    })


def _drop_task(task_id):
    """Forget a finished task's server state (success or terminal failure)."""
    with STATE_LOCK:
        TASKS.pop(task_id, None)


def _run_download(task_id, cmd, gen=0):
    """Run the yt-dlp subprocess for a task and stream its state via SSE."""
    DOWNLOAD_SLOTS.acquire()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with STATE_LOCK:
            entry = TASKS.get(task_id)
            if isinstance(entry, dict):
                entry["proc"] = proc
            else:
                TASKS[task_id] = {"cmd": cmd, "proc": proc, "_gen": gen}
        if not _gen_is_current(task_id, gen):
            return

        err_lines = []
        out_lines = []

        def stream_lines(stream, sink):
            for raw in iter(stream.readline, b""):
                try:
                    line = raw.decode("utf-8", "ignore").strip()
                except Exception:
                    continue
                if not line:
                    continue
                sink.append(line)
                upd = parse_progress_line(line, task_id)
                if upd:
                    notify(task_id, "progress", upd)

        err_thread = threading.Thread(
            target=stream_lines, args=(proc.stderr, err_lines), daemon=True
        )
        err_thread.start()
        stream_lines(proc.stdout, out_lines)
        err_thread.join()
        rc = proc.wait()

        files = [
            f for f in os.listdir(downloader.DOWNLOAD_DIR)
            if f.startswith(task_id + "_")
            and not f.endswith((".part", ".covertmp", ".tmp",
                               ".webp", ".png", ".jpg", ".jpeg", ".gif", ".info.json"))
        ]
        if files and rc == 0 and _gen_is_current(task_id, gen):
            files.sort(key=lambda f: os.path.getsize(
                os.path.join(downloader.DOWNLOAD_DIR, f)), reverse=True)
            real = os.path.join(downloader.DOWNLOAD_DIR, files[0])
            _embed_cover(task_id, real)
            for leftover in files[1:]:
                try:
                    os.remove(os.path.join(downloader.DOWNLOAD_DIR, leftover))
                except OSError:
                    pass
            _clear_cover(task_id)
            size = os.path.getsize(real)
            ext = os.path.splitext(real)[1].lstrip(".")
            name = files[0].split("_", 1)[1] if "_" in files[0] else files[0]
            with STATE_LOCK:
                FILE_STATE[task_id] = True
                tentry = TASKS.get(task_id) or {}
                artwork = tentry.get("artwork") or ""
                tags = tentry.get("tags")
                tags_mode = tentry.get("tags_mode")
                FINISHED_META[task_id] = {
                    "title": tentry.get("_title") or "",
                    "creator": tentry.get("_creator") or "",
                }
            notify(task_id, "ready", {
                "status": "ready",
                "filename": files[0],
                "display_name": name,
                "ext": ext,
                "size": size,
                "artwork": artwork,
                "cover": tentry.get("cover"),
                "tags": tags,
                "tags_mode": tags_mode,
                "file_url": f"/api/file/{task_id}/{urllib.parse.quote(files[0], safe='')}",
                "preview_url": f"/api/file/{task_id}/{urllib.parse.quote(files[0], safe='')}?inline=1",
            })
            _drop_task(task_id)
        else:
            if not _gen_is_current(task_id, gen):
                return
            msg = _extract_ytdlp_error(err_lines, out_lines)
            if rc != 0 and not msg:
                msg = f"yt-dlp exited with code {rc}"
            if "not available" in msg or "Requested format" in msg:
                # The chosen quality does not exist for this item: music mixes
                # (YouTube Music songs) intermittently expose only combined
                # streams to unauthenticated clients, so a height/ext-capped
                # video selector matches nothing. Retry once with the most
                # permissive selector -- bestvideo+bestaudio still prefers
                # separate DASH streams and falls back to a combined file which
                # always exists for a playable video (audio keeps bestaudio).
                with STATE_LOCK:
                    entry = TASKS.get(task_id)
                    can_fallback = isinstance(entry, dict) and not entry.get("_fallback_used")
                    if can_fallback:
                        entry["_fallback_used"] = True
                if can_fallback:
                    fallback = list(cmd)
                    try:
                        i = fallback.index("-f")
                        fallback[i + 1] = (
                            "bestaudio/best"
                            if "--extract-audio" in cmd
                            else "bestvideo+bestaudio/best"
                        )
                    except (ValueError, IndexError):
                        fallback = None
                    if fallback:
                        _clear_task_files(task_id)
                        with STATE_LOCK:
                            entry["cmd"] = fallback
                            entry["_gen"] = entry.get("_gen", 0) + 5
                            entry["proc"] = None
                            FILE_STATE.pop(task_id, None)
                        _spawn_download(task_id)
                        return
            _clear_cover(task_id)
            if "not available" in msg:
                msg = (msg + " — try a different quality option, or download in Audio mode.")
            notify(task_id, "failed", {
                "status": "error",
                "message": msg or "No output file found",
            })
            _drop_task(task_id)
    except Exception as e:
        if not _gen_is_current(task_id, gen):
            return
        _clear_cover(task_id)
        notify(task_id, "failed", {
            "status": "error",
            "message": str(e),
        })
        _drop_task(task_id)
    finally:
        DOWNLOAD_SLOTS.release()

def _spawn_download(task_id):
    """Start the download thread for a registered (possibly deferred) task."""
    with STATE_LOCK:
        entry = TASKS.get(task_id)
    if not entry or entry.get("_pending"):
        return False
    if not _disk_ok():
        notify(task_id, "failed", {
            "status": "error",
            "message": "Not enough free disk space to start the download",
        })
        return False
    cmd = entry["cmd"]
    with STATE_LOCK:
        entry["_gen"] = entry.get("_gen", 0) + 1
        gen = entry["_gen"]
    notify(task_id, "started")
    threading.Thread(target=_run_download, args=(task_id, cmd, gen), daemon=True).start()
    return True


@app.route("/api/progress/<task_id>")
def api_progress(task_id):
    denied = _require_auth()
    if denied:
        return denied

    def generate():
        q = Queue()
        with STATE_LOCK:
            SSE_CLIENTS[task_id] = q
            latest = SSE_LATEST.get(task_id)
            current = dict(latest) if latest else None
        if current:
            yield f"event: {current['event']}\ndata: {json.dumps(current['data'])}\n\n"
        finished = False
        while not finished:
            try:
                msg = q.get(timeout=15)
            except QueueEmpty:
                yield ": keepalive\n\n"
                continue
            yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
            if msg["event"] in ("ready", "failed"):
                finished = True
        with STATE_LOCK:
            if SSE_CLIENTS.get(task_id) is q:
                SSE_CLIENTS.pop(task_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/file/<task_id>/<filename>")
def api_file(task_id, filename):
    denied = _require_auth()
    if denied:
        return denied
    if not task_id or not task_id.isalnum():
        return "Bad task id", 400
    if not filename.startswith(task_id + "_"):
        return "Bad filename", 400
    real_dir = os.path.realpath(downloader.DOWNLOAD_DIR)
    filepath = os.path.realpath(os.path.join(real_dir, filename))
    if not (filepath == real_dir or filepath.startswith(real_dir + os.sep)):
        return "Bad filename", 400
    if not os.path.isfile(filepath):
        return "File not found", 404
    parts = filename.split("_", 1)
    friendly = parts[1] if len(parts) > 1 else filename
    inline = request.args.get("inline") == "1"
    ext = os.path.splitext(friendly)[1].lower().lstrip(".")
    mime_map = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "mkv": "video/x-matroska",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "opus": "audio/ogg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
    }
    return send_file(
        filepath,
        as_attachment=not inline,
        download_name=friendly,
        conditional=True,
        mimetype=mime_map.get(ext) if inline else None,
    )


@app.route("/api/task/<task_id>", methods=["GET", "DELETE", "POST"])
def api_task(task_id):
    if not task_id or not task_id.isalnum():
        return jsonify({"error": "Bad task id"}), 400
    denied = _require_auth()
    if denied:
        return denied
    session = (request.args.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 403
    with STATE_LOCK:
        owned = task_id in SESSION_TASKS.get(session, [])
    if not owned:
        return jsonify({"error": "Not your task"}), 403

    if request.method == "GET":
        # Poll-safe status snapshot (used by queues instead of the SSE stream,
        # which mobile browsers can silently stall). Reflects the latest
        # notified event, so the client needs no long-lived connection.
        with STATE_LOCK:
            snap = SSE_LATEST.get(task_id)
            entry = TASKS.get(task_id)
            finished = FILE_STATE.get(task_id)
        event = snap.get("event") if snap else None
        data = dict(snap.get("data") or {}) if snap else {}
        if finished:
            status = "ready"
        elif event == "failed":
            status = "failed"
        elif entry and entry.get("_pending"):
            status = "pending"
        else:
            status = "running"
        return jsonify({"status": status, "event": event, "data": data})

    # _delete_task_file terminates the yt-dlp process before touching files so a
    # superseded/aborted run can't re-create its outputs after deletion.
    count = _delete_task_file(task_id)
    return jsonify({"cleared": count > 0, "count": count})


@app.route("/api/task/<task_id>/tags", methods=["POST"])
def api_task_tags(task_id):
    """Apply a metadata set + album-art choice to a task.

    Body: {"mode":"candidate","tags":{...},"art":{type,url}} to embed the
          chosen overrides + artwork, {"mode":"manual","tags":{...},"art":{type,url}}
          to embed hand-typed overrides + artwork (same path as candidate),
          {"mode":"keep"} to embed yt-dlp's own
          fields, {"mode":"skip"} to disable embedding entirely.
    art.type: "album" (fetch url) | "file" (a cover already uploaded to this
              task via POST /api/art-local) | "video" (video-thumbnail fallback) |
              "none" (no artwork at all).

    Deferred tasks are started; already-running tasks are restarted with the
    new settings (the current download is terminated and re-run).
    """
    if not task_id or not task_id.isalnum():
        return jsonify({"error": "Bad task id"}), 400
    denied = _require_auth()
    if denied:
        return denied
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "Forbidden"}), 403
    session = (request.args.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 403
    with STATE_LOCK:
        owned = task_id in SESSION_TASKS.get(session, [])
        finished = FILE_STATE.get(task_id)
        entry = TASKS.get(task_id)
    if not owned:
        return jsonify({"error": "Not your task"}), 403
    if finished or entry is None:
        return jsonify({"error": "Task already finished"}), 400

    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    overrides = None
    embed = True
    if mode == "skip":
        embed = False

    cover_path = None
    artwork_url = ""
    cover = "video"
    if embed:
        overrides = {}
        if data.get("tags") and isinstance(data.get("tags"), dict):
            info_title = str(entry.get("_title") or "").strip()
            info_creator = str(entry.get("_creator") or "").strip()

            for k, v in data["tags"].items():
                if k not in downloader.ALLOWED_TAG_KEYS:
                    # Never let a client field name reach --parse-metadata.
                    continue
                if not isinstance(v, (str, int)):
                    continue
                s = _clean_tag(v)
                if not s:
                    if k == "artist":
                        # Never let the embedded ID3 artist be blank/NA: fall back
                        # to the uploader, then to a neutral label.
                        s = info_creator or "Unknown Artist"
                    elif k == "track":
                        s = info_title or "Unknown Track"
                    else:
                        continue
                overrides[k] = s
        art = data.get("art") if isinstance(data.get("art"), dict) else {}
        art_type = art.get("type")
        if art_type not in ("album", "video", "none", "file"):
            art_type = "video"
        art_url = (art.get("url") or "").strip()
        if art_type == "album" and downloader.is_safe_url(art_url):
            _clear_cover(task_id)  # drop a previously uploaded custom cover
            cover_path = _fetch_cover(task_id, art_url)
            if cover_path:
                cover = "art"
                artwork_url = art_url
                _set_cover_uploaded(task_id, False)
        elif art_type == "file":
            local = _existing_cover(task_id)
            if local:
                cover_path = local
                cover = "art"
                artwork_url = f"/api/art-local/{task_id}"
                _set_cover_uploaded(task_id, True)
        elif art_type == "none":
            cover = "none"
        if cover_path is None:
            # video / none / album-without-result: drop any leftover cover so it
            # cannot leak into _embed_cover (an uploaded one is kept on disk for
            # the result card but unlinked from this run).
            _clear_cover(task_id)

    cmd = downloader.build_download_command(
        entry.get("url"), entry.get("format_spec"), entry.get("output_tpl"),
        task_id, entry.get("audio_convert"),
        tag_overrides=overrides, embed=embed, cover=cover,
    )

    running = not entry.get("_pending")
    if running:
        # Restart semantics: fully exit the current run (its .part cleanup must
        # finish before clearing files, otherwise it can delete the new run's
        # half-written output on identical paths), invalidate its thread, clear
        # on-disk files, and re-run with the new settings.
        proc = entry.get("proc")
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=2)
            except Exception:
                pass
        with STATE_LOCK:
            entry["_gen"] = entry.get("_gen", 0) + 5
            entry["proc"] = None
        _clear_task_files(task_id)
        with STATE_LOCK:
            FILE_STATE.pop(task_id, None)

    with STATE_LOCK:
        entry["cmd"] = cmd
        entry["_pending"] = False
        entry["embed"] = embed
        entry["cover_path"] = cover_path
        entry["artwork"] = artwork_url
        entry["cover"] = cover
        entry["tags"] = overrides
        entry["tags_mode"] = mode
    _spawn_download(task_id)
    return jsonify({"task_id": task_id, "started": True})


@app.route("/api/task/<task_id>/candidates", methods=["GET"])
def api_task_candidates(task_id):
    """Return tag candidates for a finished task (for the after-download picker)."""
    if not task_id or not task_id.isalnum():
        return jsonify({"error": "Bad task id"}), 400
    denied = _require_auth()
    if denied:
        return denied
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "Forbidden"}), 403
    session = (request.args.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 403
    with STATE_LOCK:
        owned = task_id in SESSION_TASKS.get(session, [])
        finished = FILE_STATE.get(task_id)
    if not owned:
        return jsonify({"error": "Not your task"}), 403
    if not finished:
        return jsonify({"error": "Task not finished yet"}), 400
    meta = FINISHED_META.get(task_id) or {}
    title = meta.get("title") or ""
    creator = meta.get("creator") or ""
    if not title:
        # Task finished before FINISHED_META existed (migrated code); try
        # to find a finished file and return an empty set gracefully.
        return jsonify({"candidates": []})
    result = downloader.lookup_song_info(title, creator)
    candidates = result.get("candidates") or []
    if not candidates:
        candidates = [{
            "tags": {"artist": creator, "track": title},
            "source": "Video title",
            "score": 1.0,
            "artwork": "",
        }]
    return jsonify({"candidates": _format_candidates(candidates)})


@app.route("/api/task/<task_id>/retag", methods=["POST"])
def api_task_retag(task_id):
    """Rewrite tags + artwork on a finished file (no re-download).

    Body: same shape as /api/task/<id>/tags:
        {"mode":"candidate","tags":{...},"art":{type,url}} or
        {"mode":"manual","tags":{...},"art":{type,url}} or
        {"mode":"keep"} (leave existing tags, optionally update art) or
        {"mode":"skip"} (leave everything untouched).
    """
    if not task_id or not task_id.isalnum():
        return jsonify({"error": "Bad task id"}), 400
    denied = _require_auth()
    if denied:
        return denied
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "Forbidden"}), 403
    session = (request.args.get("session") or "").strip()[:64]
    if not session:
        return jsonify({"error": "Session required"}), 403
    with STATE_LOCK:
        owned = task_id in SESSION_TASKS.get(session, [])
        finished = FILE_STATE.get(task_id)
    if not owned:
        return jsonify({"error": "Not your task"}), 403
    if not finished:
        return jsonify({"error": "Task not finished yet"}), 400

    media = _finished_media(task_id)
    if not media:
        return jsonify({"error": "No finished file found"}), 400

    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "keep").strip()

    meta = FINISHED_META.get(task_id) or {}
    info_title = meta.get("title") or ""
    info_creator = meta.get("creator") or ""

    overrides = {}
    if mode in ("candidate", "manual") and isinstance(data.get("tags"), dict):
        for k, v in data["tags"].items():
            if k not in downloader.ALLOWED_TAG_KEYS:
                continue
            if not isinstance(v, (str, int)):
                continue
            s = _clean_tag(v)
            if not s:
                if k == "artist":
                    s = info_creator or "Unknown Artist"
                elif k == "track":
                    s = info_title or "Unknown Track"
                else:
                    continue
            overrides[k] = s

    cover_path = None
    artwork_url = ""
    cover = "video"
    art = data.get("art") if isinstance(data.get("art"), dict) else {}
    art_type = art.get("type")
    if art_type not in ("album", "video", "none", "file"):
        art_type = "video"
    art_url = (art.get("url") or "").strip()
    if art_type == "album" and downloader.is_safe_url(art_url):
        _clear_cover(task_id)
        cover_path = _fetch_cover(task_id, art_url)
        if cover_path:
            cover = "art"
            artwork_url = art_url
    elif art_type == "file":
        local = _existing_cover(task_id)
        if local:
            cover_path = local
            cover = "art"
            artwork_url = f"/api/art-local/{task_id}"
    elif art_type == "none":
        cover = "none"
        # Strip existing artwork by writing to a special sentinel; _write_tags
        # treats cover_path == "\x00remove" as "delete all pictures".
        cover_path = "\x00remove"

    # For "skip" mode, leave everything untouched; for "keep" with no
    # meaningful art change, skip the write.
    if mode == "skip":
        _clear_cover(task_id)
        return jsonify({"ok": True, "changed": False,
                        "tags": {}, "tags_mode": "keep",
                        "artwork": "", "cover": "video"})
    if mode == "keep" and not overrides and not cover_path:
        _clear_cover(task_id)
        return jsonify({"ok": True, "changed": False,
                        "tags": {}, "tags_mode": "keep",
                        "artwork": artwork_url, "cover": cover})

    ok = _write_tags(task_id, media, overrides or None, cover_path)
    _clear_cover(task_id)
    return jsonify({"ok": ok, "changed": True,
                    "tags": overrides or {}, "tags_mode": mode,
                    "artwork": artwork_url, "cover": cover})


if __name__ == "__main__":
    print("Starting Media Downloader on http://localhost:5000")
    print(f"ffmpeg available: {downloader.get_ffmpeg_available()}")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
