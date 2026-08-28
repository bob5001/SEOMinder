# Local (macOS) scheduling — interim, while running on the Mac Studio

This is a **parallel path to `deploy/`**, not a replacement. `deploy/*.service` + `*.timer` +
`docker-compose.yml` stay as-is for the eventual Linux/Proxmox migration (see `INFRA.md`). This
directory exists because two things don't carry over to Mac as-is until then:

- Docker Desktop on Mac doesn't support `network_mode: host` the way Linux does, and Loop A's
  production route (`config/models.yaml` → `loop_a_meta`) needs to reach Ollama on
  `localhost:11434` — containerizing here would reintroduce the exact bug that fix closed for
  Linux.
- macOS doesn't ship `flock(1)` (Linux/util-linux only). `lockf(1)` is the native equivalent —
  `lockf -t 0 <lockfile> <command>` fails immediately rather than waiting, matching `flock -n`.

So instead of a container, these run `.venv/bin/python3` directly, scheduled by `launchd`
(macOS's systemd-timer equivalent) as **LaunchAgents** (per-user session — appropriate here
since Ollama runs under the logged-in user, not system-wide).

## Files
- `run-loop-b.sh` / `run-loop-a-on-publish.sh` — wrapper scripts (log to `logs/`), mirroring
  `deploy/run-loop-b.sh` but calling the venv instead of `docker compose run`.
- `com.seominder.loop-b.plist` — weekly, Monday 06:00 (matches `seo-loop-b.timer`'s
  `OnCalendar`).
- `com.seominder.loop-a-on-publish.plist` — every 10 minutes (matches
  `seo-loop-a-on-publish.timer`'s `OnUnitActiveSec`). **Auto-applies to WordPress** on anything
  new it finds, unattended — same behavior as running it manually, just recurring.

Both plists set `RunAtLoad` true, so loading either fires an immediate first run — useful for
verifying the setup without waiting for the next scheduled time, but know that loading
`com.seominder.loop-a-on-publish` will immediately poll and, if there's anything new to
publish, write to WordPress right away.

## Install
```
cp deploy/local/com.seominder.loop-b.plist deploy/local/com.seominder.loop-a-on-publish.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seominder.loop-b.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seominder.loop-a-on-publish.plist
```

## Verify
```
launchctl list | grep seominder             # both should be listed; exit code of last run
tail -f logs/loop-b.$(date +%F).log
tail -f logs/loop-a-on-publish.$(date +%F).log
tail -f logs/launchd-loop-a-on-publish.err.log   # stderr launchd couldn't route into the above
```

## Uninstall (when migrating, or to pause)
```
launchctl bootout gui/$(id -u)/com.seominder.loop-b
launchctl bootout gui/$(id -u)/com.seominder.loop-a-on-publish
rm ~/Library/LaunchAgents/com.seominder.loop-b.plist \
   ~/Library/LaunchAgents/com.seominder.loop-a-on-publish.plist
```
