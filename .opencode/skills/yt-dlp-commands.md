---
name: yt-dlp-commands
description: Output template rules, format selection fallbacks, --parse-metadata gotchas, --embed-thumbnail constraints, and format-spec regex invariants.
---

# yt-dlp command building invariants

## Output template

- **Output template** must remain `f"{task_id}_%(title).200B.%(ext)s"` in `app.py`.
  The `.200B` form is byte-aware truncation (multibyte-safe, leaves short titles
  untouched). Truncation relies **only** on this template — do **not** re-add
  `--trim-filenames`, which did NOT truncate in the yt-dlp version in use.
- **File URLs must be URL-encoded** with `urllib.parse.quote(fn, safe='')`. A raw
  `#` in a title otherwise truncates the URL as a fragment and breaks the download.

## Format selection

- **Never** derive a height cap with a bare `(\d+)` regex on the format spec — it
  matches the `4` inside `mp4`/`m4a`, producing nonsense like `height<=4`. Heights
  come **only** from explicit `MP4 <h>p` labels.
- `MP4 (best)` maps to a merge spec
  (`bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best`);
  it requires ffmpeg for sites that only expose separate video-only + audio streams.
- `build_download_command` reads the audio-format dict's `out_ext` **or** its
  `acodec` key; never assume `out_ext` is present.
- `yt-dlp` is invoked as a CLI by `downloader.py`; `node` is used as JS runtime
  (`--js-runtimes node`) for some site extractions.
- `build_download_command` always adds `--write-info-json` so every download
  emits `<task_id>_*.info.json`. `_run_download` reads it for the ready payload's
  `thumbnail`/`title`/`creator` (video fallback art + tags when no album-art
  match was applied), then deletes the file. It's already excluded from the
  final-file scan.

## Format-unavailable fallback

When yt-dlp dies with "Requested format is not available" (music-mix song videos
intermittently expose only combined streams to unauthenticated clients, so a
height/ext-capped `bestvideo[...]` selector matches nothing), `_run_download`
retries **once** with `bestvideo+bestaudio/best` (`bestaudio/best` if the cmd has
`--extract-audio`) — guarded by the per-entry `_fallback_used` flag, reusing the
restart pattern (`_gen` bump, `_clear_task_files`, `_spawn_download`). The failure
message still appends the "try a different quality option" hint when even the
fallback loses.

## --parse-metadata gotchas

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

## --embed-thumbnail

`--embed-thumbnail` is appended **only** for converted outputs (`out_ext` or the
audio-format dict's `acodec` key) **and** `cover == 'video'` — raw formats like
`.webm` cannot hold cover art and the embed aborts yt-dlp's whole
post-processing chain.