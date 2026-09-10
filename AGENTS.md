# AGENTS.md

Guidance for AI agents working on this codebase. The README.md is the human-facing
documentation; this file is the operational cheat-sheet for making changes and
verifying them.

## Orientation

Single Flask process. It serves both the single-page UI and the JSON API.

- `app.py` — backend: routes, task orchestration, background download threads,
  Server-Sent Events progress, file serving.
- `downloader.py` — yt-dlp integration: metadata extraction, format menu, command
  building, ffmpeg detection.
- `static/index.html` — the **only** frontend file (HTML + CSS + JS all inline).
- `downloads/` — finished files (gitignored).

Environment: Alpine proot container on Android/Termux (`apk` package manager).

## Verify changes

```bash
# Python syntax
python3 -m py_compile app.py downloader.py

# Inline JS (no separate JS files): extract the <script> and syntax-check it
python3 - <<'PY'
import re
html=open('static/index.html').read()
m=re.search(r'<script>(.*?)</script>',html,re.S)
open('/tmp/c.js','w').write(m.group(1))
PY
node --check /tmp/c.js

# Health
curl http://localhost:5000/api/ffmpeg                    # expect 200 {"available": bool}
curl http://localhost:5000/api/formats                   # expect 200 {video_formats, audio_formats}
```

## Restart the server (critical)

**Kill**: scan `/proc/[0-9]*/exe` for a `*/python3*` whose `/proc/<pid>/cmdline`
contains `app.py`, then `kill -9` that pid.

**Never** use `pgrep -f "python3 app.py"` or `lsof -t -i :5000` — in this proot
environment they match the wrapping shell or dump unrelated system file descriptors.

```bash
for p in /proc/[0-9]*/exe; do
  exe=$(readlink "$p" 2>/dev/null)
  case "$exe" in */python3*)
    cmd=$(tr '\0' ' ' </proc/$(basename "$(dirname "$p")")/cmdline 2>/dev/null)
    case "$cmd" in *app.py*) kill -9 "$(basename "$(dirname "$p")")" 2>/dev/null;; esac
  ;; esac
done
```

**Start** (detached, logged):

```bash
: > /tmp/app.log
( setsid /usr/bin/python3 app.py >/tmp/app.log 2>&1 </dev/null & )
sleep 3
```

Then verify the log contains the startup lines
(`Starting Media Downloader on http://localhost:5000` and `ffmpeg available: ...`)
and the health endpoint returns 200. The startup lines are printed before the bind
attempt, so they appear either way — a successful start additionally requires that
the log does **not** end with a Werkzeug `Address already in use` traceback.

## File-change rule

- Backend edits (`app.py`, `downloader.py`) require a **restart**.
- Frontend edits (`static/index.html`) require **only a browser refresh** — the server
  reads `index.html` from disk on every request.

## Invariants to preserve (do not break)

- **Output template** must remain `f"{task_id}_%(title).200B.%(ext)s"` in `app.py`.
  The `.200B` form is byte-aware truncation (multibyte-safe, leaves short titles
  untouched). Truncation relies **only** on this template — do **not** re-add
  `--trim-filenames`, which did NOT truncate in the yt-dlp version in use.
- **File URLs must be URL-encoded** with `urllib.parse.quote(fn, safe='')`. A raw
  `#` in a title otherwise truncates the URL as a fragment and breaks the download.
- **Never** derive a height cap with a bare `(\d+)` regex on the format spec — it
  matches the `4` inside `mp4`/`m4a`, producing nonsense like `height<=4`. Heights
  come **only** from explicit `MP4 <h>p` labels.
- **Keep `threaded=True`** in `app.run(...)` and the **per-session clear** logic
  (`SESSION_TASKS` keyed by a client session token) so simultaneous users don't
  delete each other's files. Shared dicts are guarded by `STATE_LOCK`.
- **Security invariants** — only `http://`/`https://` URLs reach the API
  (`is_safe_url` in `downloader.py` rejects crafted `-args` flag injection);
  `/api/file/<task_id>/<filename>` keeps verifying that the filename belongs to
  the task and resolves inside `downloads/` (path traversal blocked);
  `DELETE/POST /api/task/<task_id>` requires the owning session token (403
  otherwise); `POST /api/task/<task_id>/tags` has the same header + owning-session
  guards; it also requires the task to not have reached the `FILE_STATE` ready
  flag (400 "already finished") and, for a running task, performs a **restart**
  (terminate the yt-dlp process, `_gen` invalidation, `_clear_task_files`, re-run);
  `POST /api/prepare`
  requires the `X-Requested-With: XMLHttpRequest` header and a non-empty `session`;
  task ids are `uuid.uuid4().hex[:16]`; and `app.run(...)` must keep `debug=False`
  so the Werkzeug debugger is never exposed on `0.0.0.0`.
  **Hardening:** `MD_TOKEN` env var enables an optional token gate on every
  endpoint **except `/api/ffmpeg`** (the health probe) — accepted as
  `Authorization: Bearer <token>` **or** a `?token=` query param (EventSource
  and `sendBeacon` can't set headers; the UI adds the param). The UI stores the
  token in `sessionStorage`, prompts on 401, and auto-sends it. Concurrency is
  capped at `MAX_CONCURRENT_DOWNLOADS` (~2) via a `BoundedSemaphore`, and
  downloads are refused with a `failed` SSE event when free disk below
  `MIN_FREE_BYTES`. `/api/art` restricts every redirect hop to iTunes/Deezer
  hosts, and `fetch_artwork_bytes` re-checks the host at each hop. `/api/art-local`
  (POST upload + GET serve) is auth + XHR + session-ownership gated, 8MB-capped,
  magic-byte sniffed, and the GET forces the task's canonical cover name. Tag keys are
  whitelisted (`ALLOWED_TAG_KEYS` in `downloader.py`).
- **SSE uses the `failed` event name — never `error`** — because EventSource
  reserves `error` for connection failures and drops the payload. Client
  listeners (`listenProgress`, `waitForTask`) subscribe to `failed`. `_run_download`
  sends ready/{failed} and does **not** pop `SSE_LATEST[task]` itself (late
  reconnect replays it); it is popped by `_delete_task_file` and LRU-capped
  (`MAX_LATEST_STATES`) in `notify`. `_run_download` also only pops `TASKS`
  (via `_drop_task`) in terminal branches when the generation is still current —
  the tags-restart path reuses the same `TASKS` entry.
- **Format-unavailable fallback**: when yt-dlp dies with "Requested format is not
  available" (music-mix song videos intermittently expose only combined streams to
  unauthenticated clients, so a height/ext-capped `bestvideo[...]` selector matches
  nothing), `_run_download` retries **once** with `bestvideo+bestaudio/best`
  (`bestaudio/best` if the cmd has `--extract-audio`) — guarded by the per-entry
  `_fallback_used` flag, reusing the restart pattern (`_gen` bump, `_clear_task_files`,
  `_spawn_download`). The failure message still appends the "try a different quality
  option" hint when even the fallback loses.

## Album art + metadata modes

- `build_download_command(..., cover='video'|'art'|'none')`: `video` lets yt-dlp
  embed the video thumbnail (the fallback), `art`/`none` suppress it — official
  album art is attached **after** the download by the app (yt-dlp can only ever
  embed an extractor-provided thumbnail; `--parse-metadata` cannot inject a
  `thumbnails` list).
- Prepare for audio + ffmpeg **defers**: `api_prepare` always looks up
  iTunes/Deezer **in parallel** (2-worker `ThreadPoolExecutor`; each catalog has
  its own 8s timeout so the wait is ~the slower call, not the sum), builds the
  ranked candidate cards — capped at **10** after combining (10-item queries per
  catalog, deduped on `(artist, track)`, sorted by score desc) — plus a
  synth-fallback card with `artist=uploader/creator, track=title`, source
  `Video title`, score 1.0 when the catalog misses, so the picker always has
  content. It stores the task with `_pending=True` and returns
  `{task_id, needs_tags: true, tag_candidates}`.
  Nothing downloads until `POST /api/task/<task_id>/tags` starts it. The tags
  endpoint builds the command from the picked candidate and fetches the chosen
  cover once. The running-task **restart** path (terminate + reap, `_gen`
  invalidation, `_clear_task_files`, re-run) is still in the endpoint for
  robustness, but the UI never triggers it: `_pending` starts `True` and is only
  cleared there, so the first pick always takes the start path.
- Video / batch / audio-without-ffmpeg prepare: no candidates, `_pending=False`,
  auto-start immediately. Audio format menus are served from `/api/formats`
  (live, server-driven) — the UI must not hardcode cards, or it breaks when
  ffmpeg is absent (nothing can be transcoded to MP3/M4A).
- Auto flows fetch official art to `downloads/<task_id>_cover.jpg`
  (`fetch_artwork`: `is_safe_url` guard, ~8s timeout, 8MB cap, `image/*` +
  magic-byte gate) and the ready payload carries the `artwork` URL, `cover`
  (`art`/`video`/`none`), and the applied `tags` + `tags_mode`
  (`candidate`/`manual`/`keep`/`skip`) so the result card shows cover + song tags. The cover is
  always deleted afterwards (success or failure via `_clear_cover`), so
  `/api/file/<task_id>/...` never serves it.
- **Custom cover upload** (`manual` pane): `POST /api/art-local` takes a multipart
  image for the task (auth + `X-Requested-With` + session ownership guard, 8MB cap,
  `sniff_image` magic-byte gate), stores it to the task's canonical
  `downloads/<task_id>_cover.<ext>` slot and returns
  `{url: "/api/art-local/<task_id>", ext}`. `GET /api/art-local/<task_id>` serves
  that canonical cover (auth-gated, forced filename — never a download). A tags
  `art.type` of `"file"` embeds it (`cover="art"`). Uploaded covers set
  `entry["_cover_uploaded"]=True`, so `_clear_cover` **keeps** them (unlinks only)
  until the task is deleted (`_delete_task_file`), letting the result card show the
  user's image; fetched covers are still transient. `art.type` values:
  `album` | `file` | `video` | `none`.
- `_embed_cover` branches on the final file's extension:
  - mp3 → ffmpeg `-f mp3 -c copy -map 0:0 -map 1:0 -disposition:1 attached_pic
    -id3v2_version 3`, written to `media + ".covertmp"` then `os.replace`.
    Do **not** add `-write_id3v1 1` — it suppresses the APIC entirely in this
    ffmpeg build.
  - m4a/mp4/m4v/mov → ffmpeg `-f mp4 ... -disposition:1 attached_pic`.
  - flac/ogg/opus → mutagen `Picture` / `METADATA_BLOCK_PICTURE` (base64).
- **The `.covertmp` extension defeats ffmpeg's output-format autodetection**
  (rc 234: "Unable to choose an output format"), so the forced `-f mp3` / `-f mp4`
  is mandatory. `.covertmp`/`.tmp` are excluded from the final-file scan.
- The picker's art choice rides the same tags endpoint as `art:{type:'album'|'video'|'none', url}`;
  `"album"` re-fetches the URL server-side (never trusted as a local path). The
  picker lives on its **own optional step** (`#screenTags`, audio+ffmpeg only) and
  is **deferred**: after `api_prepare` returns `needs_tags`, `prepareVideo` calls
  `enterTags(taskId, cands)` (fresh candidates, **not** pre-populated from fetch —
  the old `refreshCands` fetch-time lookup was dropped) instead of starting.
  The step is split into three exclusive sections: a **Match** tab (default:
  `paneMatch`, the candidate carousel + `artSeg` + "Download with this match" →
  `tagsStart()` → `startWithSelection`, `mode: candidate` with the selected card +
  art), a **Manual** tab (`tagPane('manual')` shows `paneManual`, the `#manualForm`
  artist/title/album/year form made of full-width stacked fields (Artist, Title,
  Album, Year) + its own cover choice (`manualArtSeg`: **Video thumb** →
  `selArt={type:'video'}` or **Upload image** → `coverFile` picker; `uploadCover()`
  posts the file to `/api/art-local` and sets `selArt={type:'file', url}` with a
  blob preview; at least one field required, `year` must be 4 digits; `file` art
  requires an uploaded `url`); `tagsManualSubmit()` → `mode: manual`.
  `tagPane('manual')` resets a stale `album`/`none` art choice back to video, and
  a separate **Skip** section
  (`paneSkip`/`.tag-skip`) with just a button (`tagsSkip()` → `mode: keep`, native
  metadata + thumbnail) plus `tagsCancel()` (DELETE the deferred task → back to
  Configure). `startWithSelection`
  re-syncs `selCand` by `(artist, track)` into the fresh candidate list; `postAndListen`
  returns `true`/`false` so the stop buttons re-enable on failure. `tagsManualSubmit`
  is in `setTagsBusy`'s disable list (with the tab buttons), and both `tagsCancel` and
  `homeReset` call `tagPane('match')` to reset the panes and toggle labels. The quality grid on
  Configure is **collapsible** (`.quality-toggle`, starts collapsed, shows
  `Quality · <selected> · N options`, auto-folds on pick via the `addQCard` click
  handler; `showEmpty` disables it). The card element itself stays an overall target
  hidden on SSE `ready`/`failed`.

## Gotchas

- `MP4 (best)` maps to a merge spec
  (`bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best`);
  it requires ffmpeg for sites that only expose separate video-only + audio streams.
- **Tag/art injection** uses `--parse-metadata "LITERAL:%(field)s"` (splits on the
  **last** colon; safeties in `_sanitize_tag`, argv list so no shell injection).
  **Gotcha:** a literal that is a pure `[A-Za-z_]+` word (e.g. the artist "KPHK")
  is wrapped by yt-dlp into `%(literal)s`, i.e. read as a *field reference* — and
  an unknown field evaluates to the `NA` placeholder, so the tag would be written
  as "NA". `build_download_command` therefore emits such values as
  `f" {literal}:(?s)^ ?(?P<{field}>.+)$"` (leading space keeps the FROM a plain
  literal; the regex TO drops that space from the captured value).
  For a `year` override, two overrides are emitted: `%(meta_date)s` (the bare
  year, copied verbatim into the `date`/TDRC tag by FFmpegMetadataPP's `meta_*`
  loop) and `%(upload_date)s` (YYYYMMDD — a bare year breaks yt-dlp's built-in
  date-range check `strptime('%Y%m%d')`). The ID3 date tag must be the bare
  year, not `YYYY0101`, even though that keeps the range check happy.
- `--embed-thumbnail` is appended **only** for converted outputs (`out_ext` or the
  audio-format dict's `acodec` key) **and** `cover == 'video'` — raw formats like
  `.webm` cannot hold cover art and the embed aborts yt-dlp's whole
  post-processing chain.
- `build_download_command` reads the audio-format dict's `out_ext` **or** its
  `acodec` key; never assume `out_ext` is present.
- `yt-dlp` is invoked as a CLI by `downloader.py`; `node` is used as JS runtime
  (`--js-runtimes node`) for some site extractions.
