# Frame Studio Telegram Mini App frontend

This folder contains the Frame Studio-based React/Vite client used by the parent Python Telegram bot. It is configured as a static client-side app; the bot server owns the authenticated API, upload storage and media processing.

## Build for the bot

```bash
pnpm install --frozen-lockfile
pnpm run build
```

The build runs Vite and then `scripts/inline-html.mjs`, which embeds the compiled JS, CSS and preview image into `../dist/index.html`. The parent bot serves that file for `/dashboard` and nested routes. The same HTML is also delivered separately as `FrameStudioMiniApp.html` for convenient inspection/copying. Keep `pnpm-lock.yaml` aligned with `package.json`.

## Local development

```bash
pnpm install --frozen-lockfile
pnpm run dev
```

For live bot operations, serve the built page from the same public HTTPS origin as the Python API and open it through Telegram so a valid Telegram WebApp `initData` is present. A regular browser page without that signed context cannot use private bot APIs. UI screenshots/code checks with mocked state are not live Telegram validation.

The processing engine remains in the parent project (`video_engine.py`, `quality_engine.py`); this frontend only submits supported operations and renders their server-side state.
