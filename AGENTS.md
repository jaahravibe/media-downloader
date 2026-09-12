# AGENTS.md

Operational cheat-sheet for AI agents. Domain details live in skill files under
`.opencode/skills/` — load them per-task to avoid bloating the system prompt.

## Orientation

Single Flask process serving both the single-page UI and the JSON API.

- `app.py` — backend: routes, task orchestration, background download threads,
  Server-Sent Events progress, file serving.
- `downloader.py` — yt-dlp integration: metadata extraction, format menu, command
  building, ffmpeg detection.
- `static/` — frontend split across three files: `index.html`, `app.css`, `app.js`.
- `downloads/` — finished files (gitignored).

Environment: Alpine proot container on Android/Termux (`apk` package manager).

## Verify changes

```bash
# Python syntax
python3 -m py_compile app.py downloader.py

# Frontend JS
node --check static/app.js

# Health
curl http://localhost:5000/api/ffmpeg                    # expect 200 {"available": bool}
curl http://localhost:5000/api/formats                   # expect 200 {video_formats, audio_formats}
```

## Critical invariants (do not break)

- **Output template** stays `f"{task_id}_%(title).200B.%(ext)s"` in `app.py`; file
  URLs must be `urllib.parse.quote(fn, safe='')`.
- **SSE uses the `failed` event name — never `error`** — EventSource reserves
  `error` for connection failures. Listeners subscribe to `failed`.
- **Security**: keep `app.run(..., threaded=True, debug=False)`, per-session
  `SESSION_TASKS` clear logic under `STATE_LOCK`, `is_safe_url` URL gate,
  `MD_TOKEN` token gate on every endpoint except `/api/ffmpeg`, and path-traversal
  checks on `/api/file/<task_id>/<filename>`.
- **Format-unavailable fallback**: on "Requested format is not available", retry
  once with `bestvideo+bestaudio/best` (`bestaudio/best` for audio), guarded by
  `_fallback_used`.
- Do **not** re-add `--trim-filenames`; do **not** derive a height cap with a bare
  `(\d+)` regex (use explicit `MP4 <h>p` labels).

## Skills (load for domain tasks)

- `.opencode/skills/server-management.md` — restart/start commands, proot
  environment, startup checks, file-change rules (`app.py`/`downloader.py` edits
  need a restart; `static/app.css`/`app.js` need only a refresh).
- `.opencode/skills/audio-tagging.md` — album art modes, iTunes/Deezer catalog
  lookups, custom cover upload, cover embedding (`.covertmp`), post-download
  retagging.
- `.opencode/skills/yt-dlp-commands.md` — output template, format selection +
  fallback, `--parse-metadata` gotchas, `--embed-thumbnail` constraints.