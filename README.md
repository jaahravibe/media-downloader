# Media Downloader

A self-hosted web app for downloading video and audio from YouTube, Facebook, and
other supported sites, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp).

You run a small Flask server on your own machine, open a single-page web UI in the
browser, paste a link, pick a quality, and the download happens on the server. It
works over your local network out of the box, so any device on the same network can
use it.

This README is written to give you a clear mental model of how the app is put
together before you run it, then walk you through setup and the parts that are easy
to get wrong.

---

## Features

- **Quality selector cards** — pick the video resolution or audio bitrate you want.
  The grid is collapsed into a summary row (`Quality · <pick> · N options`) and
  unfolds on tap; it folds back automatically once you pick a card.
- **Audio conversion** — extract to MP3 / M4A / Opus / WAV when ffmpeg is installed.
- **Song tags + album art** *(audio)* — a music download is matched against the
  iTunes/Deezer catalogs for real `artist`/`track`/`album`/`year` tags. The picker is
  its own optional step: after **Start download** you get split into a **Match** tab
  (default: a score/tags/art carousel + **Download with this match**), a **Manual**
  tab (an inline artist/title/album/year form — `mode: manual` — for when no match is
  right; cover art can be the video thumbnail or your own uploaded image), and a
  separate **Skip** section
  (**Skip tags & download** keeps the video's own metadata and thumbnail). Cancel
  returns you to the settings screen.
- **Live progress** — progress (phase, percent, ETA, speed) updates the UI while a
  healthy connection to the status endpoint keeps flowing; the frontend *polls*
  per-task state so updates survive flaky mobile connections.
- **In-browser preview player** — watch or listen to the finished file in the page,
  streaming over HTTP range requests so even big files play before they finish.
- **Playlist support** — fetch a playlist, see the entries, prepare them all
  sequentially, then review a results list (Downloaded N of M) with per-track
  Save, album-art + tags, and a one-at-a-time preview player.
- **Dark / light theme** — preference is remembered per browser.
- **Multi-session safe** — two people can use it at once without deleting each other's
  files (details in *Architecture*).

---

## How it's built (read this first)

The whole thing is one small project:

```
media-downloader/
├── app.py              # Flask server: UI + JSON API + download orchestration
├── downloader.py       # yt-dlp integration: metadata, format menu, command building
├── static/
│   ├── index.html      # Frontend markup
│   ├── app.css         # Frontend styles
│   └── app.js          # Frontend logic
├── downloads/          # Finished files land here (gitignored)
├── requirements.txt    # Python dependencies
└── README.md
```

There are two moving parts that run at the same time:

1. **The Flask server** (one process). It serves the single-page UI **and** the JSON
   API endpoints. It is not a separate frontend/backend deployment — it is all one
   origin. When the browser opens `/`, Flask reads `static/index.html` from disk and
   returns it; the browser's JavaScript then calls the `/api/*` endpoints on the same
   server.

2. **Background download threads.** When you ask the server to prepare a download, it
   starts a background Python thread that shells out to `yt-dlp`. That thread parses
   yt-dlp's progress output and keeps a per-task status snapshot the frontend polls.

The frontend lives in the three files under `static/` and talks to the server
exclusively through `/api/*` endpoints. All state worth sharing lives on the server
(tasks, progress, files).

---

## Requirements

- **Python 3.9+** (developed and tested on 3.14).
- **Node.js** — used as a JavaScript runtime for sites whose extraction logic needs
  it (`yt-dlp --js-runtimes node`). Almost always present on dev machines.
- **ffmpeg** *(optional)* — required for audio conversion (MP3/Opus/etc.) and for
  merging separate video + audio streams into a single file. Without it, the app
  still works, but you only get the original containers and no conversion options.
- **yt-dlp** — the download engine (installed via `requirements.txt`).

A supported setup is a Termux Alpine proot container on Android, which is where this
was developed. The instructions below are identical for a normal desktop Linux box;
outside of Android you can ignore the proot references.

---

## Installation

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

This pulls in Flask and yt-dlp. Then make sure the external tools are reachable on
your `PATH`:

```bash
yt-dlp --version      # must print a version
node --version        # must print a version
ffmpeg -version       # optional; only needed for conversion/merging
```

On Alpine/Termux you may need to install node and ffmpeg explicitly, e.g.:

```bash
apk add nodejs ffmpeg
```

---

## Running

From the project directory:

```bash
python3 app.py
```

The server binds to `0.0.0.0:5000`, so it is reachable on the machine itself and on
the LAN. Open the UI:

```
http://localhost:5000
```

**Sanity check:** hit the health endpoint.

```bash
curl http://localhost:5000/api/ffmpeg
# -> {"available": true}   (or false if ffmpeg is missing)
```

### The one restart rule you must remember

- **Backend changes** (anything in `app.py` or `downloader.py`) require **restarting
  the server**.
- **Frontend changes** (only the files under `static/`) require **only a browser refresh**.

Why the difference? The server reads the files under `static/` from disk on every
request, so the latest HTML/CSS/JS is picked up immediately. The Python code, on the
other hand, is already loaded into the running interpreter, so a restart is needed to
pick up edits.

---

## Usage

1. Open the UI and paste a link into the URL field (there is a Paste button that fills
   the box from your clipboard).
2. Click **Fetch info**. The server extracts metadata and lists the available quality
   options on the Configure screen — a summary row (`Quality · <pick> · N options`)
   that unfolds into the card grid (video resolutions or audio formats; audio options
   depend on what the site provides and whether ffmpeg is present).
3. Tap a quality card to select it (the grid folds back up).
4. Click **Start download**. Everything starts immediately — video, audio, and
   playlists. Music files always begin with the video's own metadata + thumbnail;
   there is no pre-download tag step anymore.
5. A progress bar shows the current phase (extracting, downloading with %,
   processing/merging).
6. When it finishes, a preview player appears. You can play it in the page or click
   **Download** to save the file, **Fetch another**, or **← Back to settings**. For
   audio, the result card (and each finished playlist row) also shows an **Edit
   tags** button: it runs a fresh iTunes/Deezer catalog lookup against the stashed
   title/uploader and opens the tag picker in *retag mode*, where you can apply an
   official match, type your own tags (cover: the video thumbnail, official art, or
   your own uploaded image), or keep the current tags.
7. **Playlists:** fetching a playlist shows a card with its entries and a button to
   prepare them all.
8. **Home** (button below the topbar, visible on every step except the fetch/import
   screen) deletes the session's prepared files, cancels any running task, and
   resets the page back to the start.
9. While preparing/downloading, the Execute screen shows a **now-downloading**
   context card (thumbnail + title + format) above the progress bar so you always
   know which video is being processed; the card is replaced by the preview
   player once the file is ready.

Files are written to the `downloads/` directory, named like
`<taskid>_<title>.<ext>`.

---

## Architecture

### Request flow

- `GET /api/info?url=...` — runs `yt-dlp -J` to dump metadata, then filters it down
  to the fields the UI needs (title, thumbnail, duration, uploader) plus the deduped
  lists of available video and audio formats. Playlists return a truncated list of
  `entries` (up to 500, plus `entries_total`) so one fetch is enough to render the
  whole playlist card.
- `GET /api/formats` — serves the audio/video format menus. The frontend builds its
  quality cards from this live list (instead of hardcoding them) so the UI stays
  correct when ffmpeg is absent and nothing can be transcoded.
- `POST /api/prepare` — takes `{url, mode, format, session, batch, tags, title,
  creator}`, turns it into a yt-dlp command, generates a unique `task_id`, and
  returns immediately while a background thread does the actual downloading. Audio
  downloads **start immediately** with the video's own metadata +
  `tags_mode="keep"`. There is no pre-download tag selection anymore; song tags are
  applied *after* the file exists (see *Song tags and album art*).
- `POST /api/task/<task_id>/tags` — legacy pre-download endpoint: resolves a picked
  metadata set + album-art choice (`{mode: candidate|keep|skip|manual, tags, art:
  {type: album|file|video|none, url}}`) and starts the download. Only exercised by
  the old deferred flow; the current UI routes its picker through `/retag` instead.
- `GET /api/task/<task_id>/candidates?session=...&q=...` — returns a fresh ranked
  candidate list for a **finished** audio task (Auth + `X-Requested-With` +
  owning-session + finished-task guards), built live from the stashed
  `FINISHED_META[task_id]` `{title, creator}`. Optional `q` overrides the video
  title as the catalog search term (the picker's custom search); the response
  echoes back the effective `query` for prefilling. Used by the after-download
  result picker.
- `POST /api/task/<task_id>/retag` — the after-download tag editor. Same body shape
  and guards as `/tags`, but it targets the **finished file**: rewrites the
  embedded tags + artwork in place via mutagen (no re-download, filename
  unchanged). All provided fields are written — artist, album, track (title),
  year, track/disc number+total, genre, comment (container-standard frames/atoms;
  flac/ogg/opus use spec Vorbis names). Returns
  `{ok, changed, tags, tags_mode, artwork, cover}` so the UI
  re-renders the result card or playlist row. `mode: "skip"` leaves everything
  untouched; `mode: "keep"` with no tags/art change is a no-op; art `type: "none"`
  strips embedded artwork (`\x00remove` sentinel); `"file"` reuses the uploaded
  canonical cover, `"album"` fetches official art into the transient slot.
- `GET /api/task/<task_id>?session=...` — the client **polls** this for live status.
  It returns the latest notified event + a snapshot:
  `{status: pending|running|ready|failed, event, data}` where `data` holds the
  progress fields (phase, percent, speed, eta) or, on `ready`, the final file
  payload (`file_url`, `preview_url`, tags, artwork, …). Polling replaced the SSE
  stream because mobile browsers can silently stall long-lived connections. The
  legacy SSE route `/api/progress/<task_id>` still exists server-side but is no
  longer used by the UI.
- `GET /api/file/<task_id>/<filename>` — serves the finished file. Default is an
  attachment download. With `?inline=1` it serves inline for preview and supports
  HTTP range requests, which is what lets the browser player stream.
- `POST /api/art-local?task=<task_id>&session=...` — uploads a cover image the user
  picked (multipart `file`, `image/jpeg|png|gif|webp|bmp`, 8 MB cap, magic-byte
  sniffed). Stored into the task's canonical cover slot; returns
  `{url: "/api/art-local/<task_id>", ext}`. `GET /api/art-local/<task_id>` serves
  that cover inline. Both are auth + XHR + session-ownership gated. The URL is sent
  as `art: {type: "file", url}` to the tags endpoint.

### Song tags and album art

For audio downloads the file **always starts with the video's own native metadata**
(no catalog lookup blocks the download), whether single or a playlist batch, and
presents an **Edit tags** button on the finished result card (and per finished
playlist row).

When the user clicks *Edit tags* — on the single result card or per row in a
playlist — the server runs a **fresh** catalog lookup against the stashed
`{title, creator}` (`GET /api/task/<task_id>/candidates`, auth + XHR + session +
finished-task guards) and opens the same tag picker in *retag mode*. The picker
layout is unchanged (Match / Manual tabs, album art / video thumb / no art / upload
image); button labels switch to **Apply tags to file** / **Apply my tags** / **Keep
current tags**; *← Cancel* just returns to the result without touching the file.

A pick goes to `POST /api/task/<task_id>/retag` (same body shape as `/tags`:
`{mode: candidate|manual|keep|skip, tags, art: {type, url}}`). The endpoint
rewrites the tags + artwork **in place** via mutagen (mp3/m4a/mp4/flac/ogg/opus,
works with or without ffmpeg; filename unchanged). `mode: "skip"` closes the
picker without writing; `"none"` art strips embedded artwork; `"file"` reuses the
uploaded cover; `"album"` fetches official art into the transient slot. The
response (`{ok, changed, tags, tags_mode, artwork, cover}`) re-renders the result
card (single) or playlist row in place.

Album art comes from the **catalog** (iTunes `600x600bb`; Deezer `cover_xl` down to
`cover_medium`), not from the video. During the initial download, the video
thumbnail is always embedded as the fallback. When official art is available the
`Edit tags` picker fetches it server-side (`fetch_artwork` in `downloader.py` —
size-capped, only `image/*`, magic-byte sniffed) and embeds it via ffmpeg
(mp3/mp4-family) or mutagen (flac/ogg/opus). The temp file is atomically swapped
in and the cover file is always deleted afterwards. If no official art is available
or the user picks "video", the video thumbnail stays; "no art" strips it. On the
**Manual** tab the cover can also be **your own image**: the client uploads it to
`POST /api/art-local` (8 MB cap, magic-byte sniffed, session/XHR-guarded) and sends
`art.type: "file"`; the uploaded cover is served back at
`GET /api/art-local/<task_id>` and kept until the task is deleted so the result
card can show it. The `ready` event carries the `artwork` URL plus `cover`
(`art`/`video`/`none`) and `tags` + `tags_mode`
(`candidate`/`manual`/`keep`/`skip`), so the result card can show the cover and
any embedded song tags (or fall back to the video thumbnail and uploader/title for
context).

Tags are written by yt-dlp's `--embed-metadata` with `--parse-metadata` literal
overrides for the matched fields. The `year` matches the release year in the `date`
tag (a bare year breaks yt-dlp's internal date-range check, so it is also emitted as
a `YYYYMMDD` upload-date form that the check accepts while the ID3 tag keeps the bare
year via `meta_date`).

### Download progress states

Downloads move through states in order: `extracting` (indeterminate) → `downloading`
(percent/ETA/speed) → `processing` (merging or audio conversion) → `ready`/`failed`. The
`ready` event carries `file_url` and `preview_url`, so the frontend always knows where
to get the output. The `GET /api/task/<task_id>` snapshot above is how the browser
follows along.

### Concurrency and multi-session safety

Each download runs in its own background thread, so requests are never blocked waiting
on a slow download. The server runs with `threaded=True`, meaning Flask can handle
concurrent HTTP requests in separate threads instead of serializing them. A bounded
semaphore caps how many downloads run at once (two), so many users can't pile up
unbounded yt-dlp subprocesses on a phone-class host.

Files belong to whoever created them. Every browser sends a session token (generated
once and kept in `localStorage`). When you prepare a new non-batch download, the
server only clears files that belong to *your* session, never another user's. Shared
state (tasks, active files, latest states, sessions) is guarded with a lock so two
threads can't corrupt it.

---

## Security

- **URL scheme whitelist** — the API only accepts `http://` or `https://` URLs
  (`is_safe_url` in `downloader.py`) and rejects anything else. This prevents
  yt-dlp flag injection: a crafted URL starting with `-` can no longer be parsed as
  a command-line option.
- **File serving is path-safe** — `/api/file/<task_id>/<filename>` serves only files
  whose name belongs to `task_id` and whose resolved path stays inside `downloads/`,
  so path traversal is blocked.
- **Deletion is session-scoped** — `DELETE/POST /api/task/<task_id>` requires the
  session token that owns the task; without it the call is refused with 403.
  Clearing also terminates the download process so partially-written files can't
  reappear after deletion.
- **Metadata edits are guarded** — the tags endpoint requires the owning session and
  the `X-Requested-With` header, refuses tasks already marked finished, and only
  ever fetches artwork URLs that pass `is_safe_url` (fixed timeout/size caps, so a
  hostile `art.url` can't be abused as a local path or an unbounded upload).
- **Prepare requires a session** — `POST /api/prepare` accepts the
  `X-Requested-With` header (defends against cross-site drive-by POSTs) and
  requires a non-empty `session`; task ids are 16 hex chars so they can't be
  brute-forced by another session.
- **Optional token gate** — set `MD_TOKEN=<secret>` to require an
  `Authorization: Bearer <token>` header (or a `?token=` query parameter for
  EventSource/sendBeacon) on every endpoint except `/api/ffmpeg`, the health
  probe. On 401 the UI prompts for the token and remembers it for the session.
- **Bound the blast radius** — download concurrency is capped (a semaphore, so
  simultaneous users can't spawn unbounded yt-dlp processes) and new downloads
  are refused with a `failed` event when free disk is below a floor.
- **Album-art fetches are host-restricted** — `/api/art` and the internal cover
  fetcher re-validate the host on *every* redirect hop, so a smuggled URL can't
  bounce the request to an arbitrary server. Tag keys are additionally filtered
  through a whitelist.
- **No debug server** — the app runs with `debug=False`, so the Werkzeug debugger is
  never exposed on `0.0.0.0`.

---

## Things that will bite you (learned the hard way)

### Long and unusual filenames

yt-dlp can produce very long titles that blow past filesystem name limits. The output
template in `app.py` truncates the title **byte-wise** with `%(title).200B`:

```
downloads/<taskid>_%(title).200B.%(ext)s
```

The `.200B` form is byte-aware, so it never splits a multi-byte character in half, and
it leaves short titles untouched. Truncation relies solely on this template: a
`--trim-filenames` flag was tried first and did not actually truncate in the yt-dlp
version in use here, so that flag is no longer passed.

Also note that titles containing `#` used to silently truncate the download URL (the
filename was embedded unencoded). The file URL is now URL-encoded on the server
(`urllib.parse.quote`) so `#` and other special characters are harmless.

### `MP4 (best)` on sites that only offer separate streams

Some videos (YouTube is a common case) do not expose a single combined MP4. They have
a video-only MP4 stream and a separate audio stream. The `MP4 (best)` card therefore
maps to a merge-spec:

```
bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best
```

This requires ffmpeg to merge. If you see "Requested format is not available" for the
default card, that is usually the video lacking a combined MP4 rather than an app bug.

**Selector grammar gotcha:** `-f "best mp4"` (a bare selector *with a space*) is
invalid — yt-dlp treats it as two formats (`best` plus an id literally named `mp4`)
and fails with exactly "Requested format is not available". Every video selector is
therefore a bracketed expression (`best[ext=mp4]`) or a full `A+B/…` merge spec;
the UI never sends the space-separated form. The height cards send real labels
(`MP4 1080p`), so the backend height regex actually matches instead of silently
degrading to `best`.

One subtle bug worth knowing about: an earlier version tried to read a requested height
out of the format spec with a `(\d+)` regex. That regex accidentally matched the `4`
inside the literal string `mp4`/`m4a`, producing a nonsense `height<=4` and failing
every default download. Height caps now come **only** from explicit labels like
`MP4 720p`, never from scanning the spec for digits.

### Stopping / restarting the server cleanly

To restart, kill the process whose command line is the server (the app logs its output
to a file such as `/tmp/app.log` — on startup it prints
`Starting Media Downloader on http://localhost:5000` and `ffmpeg available: ...`
before binding; a successful start is confirmed by the health endpoint returning 200,
whereas a taken port shows up as a Werkzeug `Address already in use` traceback after
those lines). Don't rely on `pgrep -f "python3 app.py"` or
`lsof -t -i :5000` inside this proot environment — they can match the wrapping shell or
dump unrelated system file descriptors.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No audio conversion options in the UI | ffmpeg not installed or not on `PATH` | Install ffmpeg, verify `ffmpeg -version`, restart |
| `MP4 (best)` prepare fails with "Requested format is not available" | Site only has separate video+audio streams and no combined MP4 | Install ffmpeg (needed to merge), or pick another card. When ffmpeg is missing, the card itself shows a "merging needs ffmpeg" hint |
| A playlist item fails with "Requested format is not available" | Music-mix song videos intermittently expose only combined streams, so the chosen resolution selector matches nothing | The server auto-retries that item once with `bestvideo+bestaudio/best` (falls back to its combined file); if it still fails, pick another quality or use Audio mode |
| Server won't start / "Address in use" | Port 5000 already taken | Kill the old server process, then start again |
| Progress bar never moves | Connection to the server dropped | Force-refresh the page and re-prepare (a replacement session re-polls the task) |

---

## Disclaimer

This tool is for personal and educational use. Only download content you have the
right to download, and respect the terms of service of the sites you use it with.
