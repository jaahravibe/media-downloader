---
name: audio-tagging
description: Album art modes, iTunes/Deezer catalog lookups, custom cover uploads, cover embedding (.covertmp), and post-download retagging.
---

# Album art + metadata modes

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
- The picker's art choice rides the tags endpoints with
  `art:{type:'album'|'video'|'none''|'file', url}`;
  `"album"` re-fetches the URL server-side (never trusted as a local path).

## Post-download retagging

**Song tags are attached *after* the download, from the result card.** Downloads
first (native yt-dlp metadata + video thumbnail by default), then the user can
open the tag picker on a finished audio file and apply the tags + artwork *in
place* (no re-download, filename unchanged). Two post-download entry points:
  - Single result: `#editTagsBtn` on the result card (`editTagsForResult()`,
    shown in `showReady` for `mp3/m4a/ogg/opus/wav`).
  - Playlist rows: an `Edit tags` button per finished audio row in
    `renderPlaylistResult` (wired to the same picker).
Both fetch fresh candidates from `GET /api/task/<task_id>/candidates` (auth +
`X-Requested-With` + owning-session + finished-task guards; `lookup_song_info`
on the stashed `FINISHED_META[task_id]` `{title, creator}`; synth fallback card
when catalogs miss) and open `#screenTags` in retag mode
(`enterTags(taskId, cands, true)` sets `retagTaskId`). Submitting goes to
`POST /api/task/<task_id>/retag` (same guards) instead of `/tags`, with the
same body shape (`mode: candidate|manual|keep|skip`, `tags`, `art`); the
response `{ok, changed, tags, tags_mode, artwork, cover}` re-renders the
result card (`applyRetag` updates `lastReadyData` + `showReady`) or the
playlist row. `retagTaskId` primes the picker controls to "Apply tags / Apply
my tags / Keep current tags"; `tagsSkip` in retag mode just closes the picker
(no write); `tagsCancel` in retag mode does **not** DELETE the finished task —
it returns to the result screen. Retag requires the pending/flags unchanged:
it uses `_finished_media` (largest non-image, non-parts file) and `_write_tags`
via **mutagen** (mp3/m4a/mp4/flac/ogg/opus), so it works with or without ffmpeg;
cover `"none"` writes a `\x00remove` sentinel to strip embedded art; `"file"`
reuses the canonical `downloads/<task_id>_cover.<ext>` (user upload), `"album"`
fetches official art into the same transient slot. The ready payload stashes
`FINISHED_META[task_id]` in `_run_download` (before `_drop_task`), popped by
`_delete_task_file`.

## Deferred input is gone

The (older, pre-download) deferred input is gone: `api_prepare` in audio mode
always starts immediately with `tags_mode="keep"` (native metadata + video
thumbnail — no catalog lookup at prepare time, no `needs_tags`), for single and
batch alike. Re-tagging any finished audio row after the download is the
place where song tags and album art get applied.
The quality grid on Configure is **collapsible** (`.quality-toggle`, starts collapsed, shows
`Quality · <selected> · N options`, auto-folds on pick via the `addQCard` click
handler; `showEmpty` disables it). The card element itself stays an overall target
hidden on SSE `ready`/`failed`.