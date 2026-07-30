# Ani-Web Technical Decision Document: Building a Termux-Hosted Phone-as-Remote Anime Player for Android TV

## TL;DR
- **Real pause/seek/position-query control from Termux with no root is achievable only via one stack: `mpv` running *inside Termux* with `--input-ipc-server`, driven over its JSON IPC socket by your Python backend — BUT this route has a fatal caveat on Android TV: Termux-native mpv has no usable hardware-accelerated HDMI video output.** It only offers `null`/`image` video outputs natively and requires termux-x11/VNC (software-rendered, no hwdec) for actual video, which is not viable for smooth 1080p anime on a TV box.
- **The realistic architecture is a two-tier control model:** launch **mpv-android** (or VLC) via `am start` intent for real HDMI playback (fire-and-forget start with a start-position), and get *degraded* remote control (play/pause, and coarse position via polling `dumpsys media_session`) through a **self-adb / wireless-debugging channel** from Termux. Full arbitrary seek-to-timestamp and reliable live position query are **not** cleanly available for intent-launched players without root.
- **For the "resume episode" feature, the reliable data source is the exit-result intent** that both VLC-Android and mpv-android return (`extra_position`/`position` in ms) when the user quits — poll/capture that rather than trying to read live position mid-playback.

## Key Findings

1. **ani-cli is scriptable and has a built-in "print links only" mode** (`ANI_CLI_PLAYER=debug`), and combining `-S <index>` + `-e <episode>` bypasses the fzf TUI entirely. But debug mode prints only the stream URL(s) — **not** the Referer header the stream needs to play. You must supply the Referer yourself.
2. **No Android media player exposes a desktop-VLC-style HTTP/telnet RC interface.** VLC's web interface is desktop-only. The only true programmatic IPC on Android is Termux-native mpv's socket — which can't drive the TV's display.
3. **`am start` intents work well for launching playback with a start-position**, and both mpv-android and VLC-Android return a result intent with the final playback position on exit — this is the cleanest "resume" and "ended" signal.
4. **Self-adb over wireless debugging (127.0.0.1) is the only no-root channel for sending keyevents/media commands to a foreground player**, but it's fragile (re-pair after every reboot on many devices) and gives you coarse control at best.
5. **Termux on Android TV is installable via F-Droid/GitHub APK sideloading but faces real headwinds**: phantom-process killing (Android 12+), background activity-launch restrictions (Android 10+), and leanback/no-keyboard UX friction.
6. **AniList GraphQL and Jikan are both free, keyless for reads**, with 90 req/min and 60 req/min limits respectively; the hard part is fuzzy-matching their titles to what allanime expects.
7. **For the Python server, stdlib `http.server` or Flask are the low-pain choices in Termux; FastAPI+uvicorn's `[standard]` extras (uvloop, httptools) require compilation and are known to fail to build in Termux.**

## Details

### 1. ani-cli non-interactive / programmatic usage

ani-cli is a POSIX shell script that scrapes **allanime** (base domain `allanime.day`, API `https://api.allanime.day`). Note a **moving target on versioning and dependencies**: the source tree the research pulled reported `version_number="4.14.1"`, while pystardust/ani-cli's own README install instructions now reference the **v4.15 release** — and v4.15 newly requires the **Botan** crypto library ("Download botan-cli.exe and its SHA-256 checksum from the v4.15 release") to *decrypt video sources*. This new hard dependency on Botan is itself a fragility/setup factor you must satisfy inside Termux (`pkg install botan` or equivalent) or ani-cli's URL resolution will fail. Confirmed behavior from the source:

**(a) Search + parseable results, (b) select without TUI:** Providing `-S <index>` (`--select-nth`, 1-based, selects the nth search result via `sed -n "${index}p"`) together with `-e <episode>` (`--episode`, alias `-r`/`--range`) bypasses *both* interactive fzf prompts, making the run fully non-interactive. Flags can appear in any order; non-flag tokens become the search query (spaces converted to `+`).

- Note on `ANI_CLI_NON_INTERACTIVE`: This env var **does not exist in current pystardust/master** — it appears only in a third-party man-page mirror (Hexmos) reflecting an older/forked build. Do not depend on it; rely on `-S` + `-e` instead.

**(c) Print resolved URL without launching a player:** `ANI_CLI_PLAYER=debug`. The `debug` case runs:
```
printf "All links:\n%s\nSelected link:\n" "$links"
printf "%s\n" "$episode"
```
It prints all quality/URL lines (format `<quality> ><url>`) then the single selected URL, then exits immediately (`[ "$player_function" = "debug" ] && exit 0`). Other `ANI_CLI_PLAYER` values: `download`, `android_mpv`, `android_vlc`, `flatpak_mpv`, `catt`, or any binary that plays URLs.

**Confirmed one-shot command:**
```
ANI_CLI_PLAYER=debug ani-cli -q 1080 -S 1 -e 5 "search term"
```

**(d) Quality:** `-q`/`--quality <best|worst|360|480|720|1080>` (default `best`), or `ANI_CLI_QUALITY`.

**(e) Player binary:** `-v`/`--vlc` for VLC; otherwise `ANI_CLI_PLAYER` selects the player path.

**Time-limited URLs and required HTTP headers — critical:** The resolved stream URLs are scraped CDN links (wixmp, mp4upload, sharepoint, gogoanime/HLS, etc.) and are typically **time-limited / tokenized** — resolve them just-in-time, not ahead of a session. They **require a Referer header** to play. From the source, ani-cli sets:
- default `allanime_refr="https://youtu-chan.com"` → passed as `--referrer=$allanime_refr`
- mp4upload sources → `--referrer=https://www.mp4upload.com`
- sharepoint → no referer
- HLS master playlists use a separately-scraped `m3u8_refr` written to `$cache_dir/m3u8_refr`
- User-Agent used: a Firefox UA string (`Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) ... Firefox/150.0`)

**Consequence for Ani-Web:** `ANI_CLI_PLAYER=debug` gives you the URL but **not** the Referer, so debug output alone is insufficient to replay the stream. You must either (a) replicate the Referer logic in your backend, or (b) call ani-cli's own `android_mpv`/`android_vlc` player path so it constructs the intent with the correct `--http-referrer`/referrer for you. Option (b) is more robust against scraper changes.

### 2. Which player can be controlled programmatically (the critical question)

**VLC for Android**
- **Launch via intent:** Yes. `am start --user 0 -a android.intent.action.VIEW -d "<url>" -n org.videolan.vlc/org.videolan.vlc.gui.video.VideoPlayerActivity` (this is exactly what ani-cli's `android_vlc` path does). Supported extras: `title`, `from_start` (bool), `position` (long, ms — start offset), `subtitles_location` (local file URI only; remote subs historically broken).
- **Start position:** Yes, via `position` extra (milliseconds). Note Android 13+ intent-filter changes have broken intent playback for some users (VLC issue #2776) — test on the target OS.
- **HTTP/RC/telnet interface:** **Desktop-only.** VLC's web interface (`--http`/Lua) exists on Windows/macOS/Linux; the Android app does not expose it. All "VLC remote" Android apps are *remotes for desktop VLC*, not for VLC-Android.
- **External pause/stop/seek after launch:** Only via Android media-button/media-session routing (`KEYCODE_MEDIA_PLAY_PAUSE` etc.) sent via adb `input keyevent` or `ACTION_MEDIA_BUTTON` broadcast, or `media dispatch` (adb). This gives **play/pause** reasonably; **stop** works; **seek-to-arbitrary-timestamp does not** (no media-button semantics for absolute seek). No live position query API.
- **Exit / resume signal:** **Yes, and this is the key feature.** Since v1.4.1 VLC-Android returns a result intent on close: `resultIntent.putExtra(EXTRA_POSITION, mService.getTime())` and `EXTRA_DURATION`. Verbatim from `VideoPlayerActivity.java`. Capturable if launched with `startActivityForResult` (i.e., not a viable capture from a plain `am start` shell — see §4).
- **Verdict:** Pause ✅ (via media session), Seek-to-timestamp ❌, Position query ❌ live / ✅ on-exit, No root ✅.

**mpv-android (`is.xyz.mpv`)**
- **Launch via intent:** Yes. `am start -a android.intent.action.VIEW -t video/any -p is.xyz.mpv -d "<url>"`. Extras (all optional): `decode_mode` (Byte; `2` = disable hwdec), `subs` (ParcelableArray<Uri>), `subs.enable`, `position` (Int, **ms**, start point), `title` (String). Confirmed from `intent.html` and `MPVActivity.kt`.
- **Passing arbitrary mpv flags (e.g. `--http-header-fields`/Referer):** **Not supported via intent** — this is an open request (mpv-android issue #427). You cannot inject the Referer through the intent, which matters because anime streams need it. (ani-cli's `android_mpv` path passes referer as a positional arg to its bundled invocation, not general intent extras.)
- **Control after launch:** Same limitation as VLC — only Android media-session/media-button routing; no IPC socket exposed by the app.
- **Exit / resume signal:** **Yes.** On exit mpv-android delivers `is.xyz.mpv.MPVActivity.result` with position extras (present only if the user exited *before* the end; absent if playback finished normally — that absence is itself the "ended" signal). Result intents are **not** delivered when launched from HTML links, and capturing them from a shell `am start` is not straightforward.
- **Verdict:** Pause ✅ (media session), Seek ❌, Position ❌ live / ✅ on-exit, No root ✅. Slightly better than VLC for anime because of hardware decoding + `position` start extra, but Referer injection is the blocker unless ani-cli builds the command.

**mpv built/installed inside Termux (`pkg install mpv` / `mpv-x`)**
- **IPC control:** **Fully works in principle.** `mpv --idle --input-ipc-server=/data/data/com.termux/files/usr/tmp/mpvsocket` then the Python backend writes JSON: `{"command":["set_property","pause",true]}`, `{"command":["seek",120,"absolute"]}`, `{"command":["get_property","time-pos"]}`, `{"command":["quit"]}`. This is the *only* option giving true pause + absolute seek + live position query + clean process-exit detection. And you *can* pass the Referer here (`--http-header-fields`/`--referrer`).
- **The fatal problem — video output on Android TV:** Termux's base `mpv` package has **no working GUI video output**; `mpv --vo=help` on Termux lists only `null` and `image`. Real video requires the `mpv-x` (x11) variant plus a running X server (termux-x11 or a VNC server like tigervnc). Per Termux's own docs, that X11 stack has **no hardware acceleration and only basic OpenGL (no GLX)** — meaning software decoding of 1080p H.264/HEVC anime, which a low-end Android TV box will not do smoothly, and it renders into a VNC/X surface, **not** the TV's native HDMI video plane.
- **Verdict:** Pause ✅, Seek ✅, Position ✅, process-exit ✅, no root ✅ — **but not usable for actual TV video playback.** This is the cruel irony: the only fully-controllable option can't drive the screen acceptably.

**ExoPlayer/Media3 players & players with a built-in HTTP remote**
- No mainstream Android video player app ships a documented HTTP/remote-control server you can drive from the backend. (Kodi is the notable exception — it has a full JSON-RPC HTTP API — but it's a heavy platform, not a lightweight player, and its anime integration would be via add-ons, not ani-cli URLs. Worth noting as an alternative architecture, not part of the requested minimal stack.)

**adb over localhost from within Termux (self-adb)**
- **Viability:** This is the pivotal enabler for controlling a *foreground* intent-launched player without root. On Android 11+, Wireless Debugging lets Termux's `android-tools` (or `nohajc/termux-adb`) `adb pair localhost:<port>` then `adb connect 127.0.0.1:<port>`. Once connected you can run `adb shell input keyevent 85` (play/pause), `adb shell media dispatch pause`, and `adb shell dumpsys media_session` to read state.
- **Setup required:** Developer options → Wireless debugging on; pair once with a code; **re-pair/reconnect after every reboot** on most devices (the port randomizes). A useful side benefit: once you have self-adb you can also run `adb shell settings put global settings_enable_monitor_phantom_procs false` to fix Termux being killed (see §3).
- **Fragility:** High. Pairing is flaky ("turn wireless debugging off/on 3×"), survives poorly across reboots, and the connection daemon occasionally hangs. Treat it as a best-effort control channel, not a guaranteed one.
- **What it buys you:** play/pause ✅, stop ✅, coarse position read via `dumpsys media_session` ⚠️ (see §4 caveats), arbitrary seek ❌.

### 3. Termux constraints on Android TV specifically

- **Installability:** Termux must come from **F-Droid or the GitHub APK** — the Play Store build is unmaintained/incompatible. On Android TV this means sideloading (e.g. via a file manager + "install from unknown sources"); Play Protect will warn because Termux targets an old SDK (targetSdk 28) — you must "install anyway." Leanback launchers often hide sideloaded non-TV apps, so you may need a sideload launcher to open Termux, and Termux's terminal is painful without a keyboard (plan to drive everything from the phone web UI and use a Bluetooth keyboard only for first-time setup).
- **Phantom Process Killer (Android 12+):** The single biggest threat. Per Termux/agnostic-apollo's own documentation, Android "allow[s] up to 32 such [phantom] processes; the process with the worst oom adj score of their parents will be killed" — this limit is **system-wide across all apps**, not per-app, and it also kills processes using "excessive CPU" in the background. Termux child processes (your Python server, any spawned ani-cli/curl) are prime targets, producing `[Process completed (signal 9)]`. The fix is the `settings_enable_monitor_phantom_procs` feature flag (added by a Google engineer and available since the Android 12L beta), toggled via `adb shell "settings put global settings_enable_monitor_phantom_procs false"` and/or developer-options Feature-flags — i.e., you need the self-adb channel anyway. This is not optional for a long-running server on Android 12/13.
- **Background activity-launch restriction (Android 10+):** A background shell in Termux **cannot freely start an activity** — this directly threatens `am start` from your Python backend. Termux requests the **"Draw over other apps"** permission specifically to bypass this; granting it (Settings → Apps → Termux → Advanced → Draw over other apps) restores the ability to launch the player activity from a background script. Without it, the intent may be deferred until the user taps the Termux notification. **Grant Draw-over-apps** is a hard setup requirement.
- **Wakelock / battery:** Use `termux-wake-lock` (and Termux:Boot to auto-start with a wakelock) to keep the server alive; disable battery optimization for Termux.
- **Termux:API:** Installable from F-Droid; provides `termux-*` utilities. Not strictly required for this project but useful.

### 4. Getting playback-ended / stopped notification

- **Termux-native mpv:** Trivial and reliable — the `mpv` child process exits; your Python backend's `subprocess`/`asyncio` wait returns. Plus IPC `end-file` events. (But see §2 video-output blocker.)
- **Intent-launched VLC / mpv-android:** No push callback reaches a shell-launched activity. The result intent (with final position) is delivered to the *calling Activity* via `onActivityResult` — a plain `am start` from Termux is not an Activity and generally won't capture it. Practical routes:
  1. **Poll `dumpsys media_session`** via self-adb: `dumpsys media_session | grep "state=PlaybackState"` returns `state=3` (playing), `state=2` (paused/stopped), plus a `position=` field. This is how Home Assistant's `androidtv` library works. **Caveat:** the `position` field is **not updated in real time** — it only refreshes on play/pause transitions and manual seeks. So you can detect play/pause/stop transitions, but you cannot read a smoothly-advancing current timestamp.
  2. **Poll `dumpsys activity` / `dumpsys window`** to detect when the player activity leaves the foreground (a proxy for "user stopped/exited").
  3. **Have ani-cli's own player path or a tiny helper capture the exit** — the most robust option for "resume" is to let the player return its exit-position intent to a small wrapper Activity, but that requires building an APK, which is outside the "plain HTML/JS + Python" constraint.

**Bottom line for "ended" detection:** With intent-launched players you get *event-ish* detection by polling `dumpsys media_session` state transitions over self-adb — good enough to know play→pause→stop and roughly when playback ended, but not a precise second-by-second position.

### 5. Metadata/catalog APIs

- **AniList GraphQL** (`https://graphql.anilist.co`): No API key needed for public reads (OAuth only for user-specific/private data). Rate limit **90 requests/minute** — per the AniList API docs, exceeding it yields "a 1 minute timeout" and an `X-RateLimit-Limit: 90` header, and severe outages return "a 403 error code" with the message *"The AniList API has been temporarily disabled due to severe stability issues."* Single GraphQL endpoint; request exactly the fields you need: `title { romaji english native }`, `synonyms`, `coverImage { large medium }`, `episodes`, `status`, `season`, `format`, `startDate`. Ideal for a lightweight app because one query returns titles + synonyms + cover + episode count — and the catalogue is deep: AniList reports "over 20 thousand anime entries and over 100 thousand manga entries, with more constantly being added."
- **Jikan v4** (`https://api.jikan.moe/v4`, unofficial MyAnimeList): No auth, no signup. Per the official v4.0 release announcement, "v4 offers a rate limit of 60 requests/minute, whereas v3 will continue to be capped at 30 requests/minute" (plus roughly 3 req/sec in practice). The v4 docs add that "All requests are cached for 24 hours" server-side, and ETag/`If-None-Match` is supported. Endpoints: `/anime?q=<title>` (min 3 chars), `/anime/{id}`, `/seasons/{year}/{season}`, `/top/anime`. You can self-host Jikan via Docker to remove the shared limit.
- **Recommendation:** Use **AniList** as primary (richer single-call queries, explicit `synonyms` field which is gold for matching); fall back to Jikan for anything AniList lacks. Cache aggressively in local SQLite — anime metadata is near-static.

**The title-matching problem (AniList/Jikan title → allanime title):** This is the genuinely hard part and has no turnkey library. allanime's internal titles differ from AniList romaji/English. Practical approach:
- Query ani-cli/allanime search with the AniList **romaji** title first, then **english**, then each entry in **synonyms**, taking the first non-empty result set.
- Rank candidates with a fuzzy string ratio (Python `rapidfuzz` — pure-wheel, installs cleanly in Termux — using `token_sort_ratio`/`WRatio`) against all AniList title variants; accept above a threshold (e.g. 85), else surface the top N to the user in the web UI to disambiguate. Cache the confirmed AniList-ID→allanime-ID mapping so each show is matched once.
- Note the ecosystem precedent: projects like **jerry** and **ani-tui** already pair AniList metadata with ani-cli playback, and **Curd** does AniList tracking — worth reading their matching heuristics.

### 6. Minimal Python HTTP server in Termux

| Option | Install pain in Termux | Footprint | SSE | WebSocket | Verdict |
|---|---|---|---|---|---|
| stdlib `http.server` | none (built in) | tiny | manual (easy) | no (needs manual upgrade) | Great for the static UI + simple JSON endpoints; SSE is a few lines. Best MVP choice. |
| **Flask** | `pip install flask` — pure Python wheels, installs clean | small | yes (generator response) | via `flask-sock`/`simple-websocket` (pure Python) | **Recommended.** Minimal deps, SSE trivial, WebSocket available without compilation. |
| FastAPI + uvicorn | FastAPI is pure-Python and installs; **`uvicorn[standard]` pulls `uvloop`+`httptools` which require C compilation and are known to FAIL to build in Termux** — confirmed in Kludex/uvicorn issue #1249 ("Building wheel for uvloop … error … exit status 1" under `/data/data/com.termux`) and mirrored by uvloop issue #613. Install plain `uvicorn` (no `[standard]`) to avoid it. | medium | yes | yes (native ASGI) | Usable only if you install uvicorn *without* the standard extras; otherwise avoid. |
| aiohttp | has C extensions; may need `pkg install` build tools | small-medium | yes | yes | Works but more finicky than Flask; only pick if you want single-loop async. |

**Recommendation:** **Flask** (with `flask-sock` for a status WebSocket, or plain SSE via a generator) hits the sweet spot: pure-Python install, minimal deps, and push-to-phone status without polling. For an even leaner MVP, stdlib `http.server` + an SSE endpoint is enough — push playback-state updates (from your `dumpsys`/IPC poller) to the phone over SSE, which the plain-JS frontend consumes with `EventSource`.

### 7. Phone-as-remote UX patterns & existing projects to read/fork

- **rdmn7/ani-cli-webui** — a web UI for ani-cli, but architecturally different: it uses a Cloudflare Worker as a CORS proxy and plays the stream in the *browser's* HTML5 player, not on a TV. Useful for its allanime search/decoding JS and UI, not for the phone-as-remote model.
- **sdaqo/anipy-cli** + **anipy-gui** — `anipy-cli` has a clean non-interactive `-s "term:episode:sub"` mode and a Python codebase (a real alternative to shelling out to ani-cli; being Python it can be imported directly into your backend). **anipy-gui** shows a GUI-over-libmpv pattern.
- **ani-tui** (Rust) — wraps ani-cli, adds AniList metadata, local SQLite state, watch-history/resume, "because you watched" — the closest design reference for your catalog/history/resume features.
- **jerry**, **Curd**, **FastAnime** — CLI tools pairing AniList tracking/sync with ani-cli-style playback; read for title-matching and history logic.
- **Home Assistant `androidtv` library** (`androidtv.readthedocs.io`) — the reference implementation for controlling Android TV over adb: exact `dumpsys media_session`/`dumpsys audio`/`dumpsys window` command strings per Android version, keyevent codes, and state parsing. **This is the single most valuable thing to read for your control channel.**
- **UX pattern:** Serve a mobile-first single page; use `EventSource` (SSE) for live status (now-playing, play/pause state, coarse position); big touch buttons for play/pause/stop; a search box hitting AniList; a catalog grid with cover art; a "continue watching" row backed by SQLite. Add the page to the phone home screen (PWA-style) so it opens without typing the box's IP.

### 8. Legal/practical fragility of the ani-cli dependency

ani-cli's scrapers break **frequently and without warning** — the GitHub issue history is full of full outages: Cloudflare blocks on allanime (issue #1050), allanime domain changes breaking streaming (issue #1196), the shutdown of previous sources (animixplay), and provider-index churn. On top of that, v4.15 introduced a **new hard dependency on the Botan crypto library** to decrypt video sources — a change that both adds a setup step and is itself a class of breakage (a version bump can silently add a dependency your Termux install lacks). The site it scrapes is itself an unofficial/legally-grey aggregator, so both the tool and its target are inherently unstable.

**Architectural implication — isolate the dependency behind an interface.** Define a narrow internal contract in your backend, e.g.:
```
search(query) -> [ {provider_id, title, episodes} ]
resolve(provider_id, episode, quality) -> {stream_url, referer, headers, ttl}
```
Implement it as a `SourceProvider` abstraction with an **ani-cli adapter** (shelling out to `ANI_CLI_PLAYER=debug` + your own Referer logic, or calling `anipy-cli` as a Python library) behind it. Then:
- Keep ani-cli itself update-able (`ani-cli -U`) and pin/vendor a known-good copy so an upstream break doesn't silently take you down; verify Botan (and other deps) are present at startup and surface a clear error if not.
- Make the frontend/history/catalog layers depend only on your interface, never on ani-cli's output format directly, so swapping to `anipy-cli`, `ani-cli-rs`, or a future source costs one adapter.
- Expect to update the adapter every few weeks; surface a clear "source unavailable — update ani-cli" error to the UI rather than a cryptic failure.
- **Legal note:** allanime streams are unlicensed; this project is for personal/educational use only and should not be distributed as a service. Resolve URLs just-in-time (they're tokenized/time-limited anyway).

## Comparison Table — Control Stacks

| Stack | Plays on TV HDMI? | Pause | Seek to timestamp | Live position query | Resume (exit position) | Ended detection | Referer injection | Root? | Fragility |
|---|---|---|---|---|---|---|---|---|---|
| **Termux-native mpv + IPC socket** | ❌ (null/VNC only, no hwdec) | ✅ | ✅ | ✅ | ✅ | ✅ (process exit + events) | ✅ | No | Low (but unusable video) |
| **mpv-android via `am start` + self-adb** | ✅ (hwdec) | ✅ (media session) | ❌ | ⚠️ coarse (dumpsys, non-realtime) | ✅ (result intent, hard to capture from shell) | ⚠️ poll dumpsys | ❌ via intent | No | High (adb pairing) |
| **VLC-Android via `am start` + self-adb** | ✅ | ✅ (media session) | ❌ | ⚠️ coarse | ✅ (result intent since v1.4.1) | ⚠️ poll dumpsys | ✅ (`--http-referrer` via ani-cli path) | No | High (adb pairing; A13 intent bug) |
| **Kodi + JSON-RPC HTTP (alt architecture)** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | n/a (add-on handles) | No | Medium (heavy, not ani-cli-native) |

## Recommendations

**Bottom line on the core question:** *Full* pause + arbitrary-seek + live-position control on Android TV, from Termux, without root, in a single clean stack **is not achievable** with any player that actually renders to the TV's HDMI output. The only fully-controllable engine (Termux-native mpv over IPC) cannot produce acceptable TV video. So you must split "playback" from "control" and accept a degraded control set for the on-screen player.

**Recommended staged plan:**

**Stage 0 — Prove the pipeline (MVP priority #4, "playback working at all").** Termux + Flask server + plain HTML/JS remote. Backend shells out to ani-cli to resolve a URL and launches **mpv-android** (hardware decode) via `am start` with the `position` extra for resume. Grant Termux **Draw-over-apps** (so background `am start` works) and disable phantom-process-killing (via a one-time self-adb command). This delivers fire-and-forget playback with start-position resume. **Benchmark to pass:** an episode plays full-screen on the TV, launched from the phone.

**Stage 1 — Search + catalog (priority #2).** AniList GraphQL for search/metadata/covers; SQLite cache; `rapidfuzz` to map AniList titles → allanime, with a disambiguation fallback in the UI.

**Stage 2 — Control (priority #1).** Set up self-adb (wireless debugging). Wire play/pause/stop to `adb shell input keyevent` / `media dispatch`. Poll `dumpsys media_session` on a timer and push state to the phone via SSE. **Accept that "seek to timestamp" is not offered** (or offer only coarse ±10s skip via repeated media-key events, which is unreliable). If the self-adb channel proves too flaky on your specific box, **fall back to fire-and-forget** (start + rely on exit-position for resume) and hide the pause/seek controls.

**Stage 3 — History + resume (priority #3).** Capture the player's exit position. Because a shell `am start` can't cleanly receive the result intent, the pragmatic path is: (a) poll `dumpsys media_session` `position` at pause/stop and persist the last known value, or (b) if you're willing to ship one tiny APK, a 50-line "launcher/receiver" Activity that does `startActivityForResult` and reports the exit position back to your Python server over HTTP — this single small native component upgrades resume from "approximate" to "exact" and is the highest-leverage optional addition.

**Thresholds that change the recommendation:**
- **If you can require root** (many generic Chinese TV boxes are rooted): skip self-adb entirely, use `su` to send keyevents and read `/proc`/`dumpsys` freely, and Termux-native mpv could even output via the framebuffer/DRM on some boxes — root would make Termux-native mpv + IPC the winning stack and give you *full* control.
- **If the box runs Kodi comfortably:** consider driving Kodi's JSON-RPC HTTP API from Python instead — it gives true seek + live position + ended events over HTTP with no adb, at the cost of not being ani-cli-native (you'd feed resolved URLs to Kodi's player).
- **If self-adb is stable on your device across reboots:** you can promote coarse seek and live-ish status to first-class UI features.

## Caveats
- **Not personally tested on hardware:** these conclusions are synthesized from source code, official docs, and issue trackers (ani-cli source, mpv-android intent spec, VLC-Android source, Termux docs/issues, AniList/Jikan docs, Home Assistant androidtv lib). Behavior varies significantly across Android TV boxes, OS versions, and OEM background-management policies — validate on your specific device early.
- **The `dumpsys media_session` position field is not real-time** — it only updates on state transitions, so a live progress bar driven from it will be inaccurate between pause/seek events.
- **Android 13+ has broken VLC intent playback for some users** (VLC-Android issue #2776) due to intent-filter changes; mpv-android may be the safer intent target on newer OS.
- **Self-adb requires re-pairing after most reboots** and can hang; build retry/reconnect logic and a clear UI error state.
- **ani-cli WILL break periodically** (and now depends on Botan as of v4.15) — the isolation layer in §8 is not optional if you want the project to survive more than a few weeks between manual fixes.
- **mpv-android cannot receive a Referer via intent**, so for anime streams that need one, either let ani-cli's `android_vlc` path build the VLC intent (VLC accepts `--http-referrer`) or resolve+proxy the stream through your own Termux HTTP server that injects the Referer. The latter (a small local reverse-proxy that adds the Referer/UA header) is a clean way to make *any* intent-launched player work regardless of its header support — strongly worth considering.
- **Version/flag volatility:** ani-cli's flags and env vars are explicitly "subject to change" per its own man page; verify `ani-cli -h` against the installed version at build time rather than hardcoding assumptions.