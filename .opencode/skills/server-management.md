---
name: server-management
description: Restart, start, and verify the Media Downloader Flask server in the Android/Termux proot environment; file-change rules.
---

# Server management (proot environment)

Environment: Alpine proot container on Android/Termux (`apk` package manager). The
app runs as a single Flask process (pinned to `python3`).

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
- Frontend edits (`static/index.html`, `static/app.css`, `static/app.js`) require **only
  a browser refresh** — the server reads files from disk on every request.