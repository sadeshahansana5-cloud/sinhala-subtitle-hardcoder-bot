# Frame Studio — current bot integration

## Current delivery

The five Frame Studio-style screens now call the existing Python bot backend instead of keeping demo-only operations in browser state. Home, Process, Subtitles, Jobs and Tools use authenticated routes on the same origin. A normal Vite build inlines the compiled JS, CSS and preview image into `webapp/dist/index.html`; the bot serves that page for `/dashboard` and client-side subroutes.

## Authenticated API map

Every JSON/multipart operation except signed media GETs includes `X-Telegram-Init-Data`. `main.py` validates its freshness and Telegram HMAC with the server-side bot token; the HMAC data-check string excludes `hash` but retains Telegram’s optional `signature` field. No browser-provided user ID is trusted. A missing or rejected session is not bypassed.

| API | Mini App actions |
|---|---|
| `GET /dashboard/api/state` | User-scoped video/SRT/font/job/history state, actual progress, signed preview URLs and limits |
| `POST /dashboard/api/upload` | Video or SRT upload, server validation/probing |
| `POST /dashboard/api/start` | Existing hardcode/embed or supported track extraction |
| `POST /dashboard/api/cancel`, `/retry` | Existing job cancellation and supported retry |
| `POST /dashboard/api/sync/start`, `/sync/upload` | English/Sinhala SRT comparison using existing sync analyzer |
| `POST /dashboard/api/style`, `/style/preview`, `/watermark` | Existing per-user subtitle style and watermark controls |
| `POST /dashboard/api/font`, `/font/delete` | Existing owner-scoped font upload/delete functions |
| `POST /dashboard/api/name`, `/delivery/cancel`, `/delete` | Output rename/skip/discard and scoped temporary file deletion |
| `POST /dashboard/api/intro` | Existing intro-SRT generator |
| `GET /dashboard/media/...` | Short-lived HMAC-signed source-video or pending-output preview for its owner |

The client polls the real state endpoint every second while its tab is visible and prevents overlapping requests. Polling stops after an auth/network failure until a manual retry to avoid repeating unauthorized requests. Browser upload progress reflects actual bytes sent; job percent/stage/ETA come from the server. Local preview remains available before upload; server playback depends on browser codec support.

## Sticker lifecycle

`STICKER_PACK_NAMES` defaults to `FunkyGoose,HotCherry`; the running bot uses its own Bot API token to call `getStickerSet`, matches available sticker emoji against each lifecycle stage, then selects an explicitly described visual fallback if no event match exists. Sending uses `sendSticker` only—there is no GIF/MP4 fallback. Temporary transfer/processing stickers are deleted after the corresponding step where possible. Welcome/menu reactions are intentionally not transient. Public set access and visual suitability must be tested using the deployed bot token; this workspace has no real bot credentials.

## Boundaries and limitations

- Video encoding, quality measurement, FFmpeg commands and style-rendering engine remain in the existing engine files; they were not changed in this update.
- The existing one-active-job-per-user policy remains; no queue is introduced.
- The quality report is still sent in Telegram because the backend exposes no Mini App report endpoint.
- Completed sync jobs cannot be retried because their temporary SRT inputs are deleted.
- Full Telegram Android/WebView verification (initData on-device, keyboard/safe areas, real sticker delivery, encode/upload, cancellation recovery and playback) remains a deployment task.

## Checks run

Vite production build and strict TypeScript check pass. Mobile browser renders cover all five routes using mock state; these do not count as a real Telegram session. Backend tests exercise the static route, signed media tamper rejection, intro-SRT route and mocked pack resolution/send/delete. Python syntax passes. Live Bot API behavior has not been tested with the user's bot token.
