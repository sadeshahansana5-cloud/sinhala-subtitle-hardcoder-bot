import os
import re
import sys
import time
import math
import json
import shutil
import unicodedata
import subprocess
import asyncio
import logging
import hashlib
import hmac
import mimetypes
import sqlite3
import uuid
import io
import random
try:                                   # faster event loop = faster transfers (optional)
    import uvloop
    uvloop.install()
except Exception:
    pass
import config  # noqa: F401  (all editable settings live in config.py; applied before anything reads them)
import aiohttp
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import video_engine as ve
import quality_engine as qe

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    WebAppInfo,
    MenuButtonDefault,
    MenuButtonWebApp,
    BotCommand,
)
from pyrogram.errors import RPCError, FloodWait
from pyrogram.enums import ParseMode

# ------------------------------------------------------------------------------
# BOT TEXT STYLE  -  every bot message / button label is shown in small-caps letters
#   e.g.  "Your video / file is successfully deleted"  ->  "ʏᴏᴜʀ ᴠɪᴅᴇᴏ / ꜰɪʟᴇ ɪꜱ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ"
#   Code spans, HTML tags, links, /commands and @names are left untouched so Telegram
#   formatting, filenames and tappable commands keep working. Only text is styled -
#   no engine / processing code is involved.
# ------------------------------------------------------------------------------
_SC_SRC = "abcdefghijklmnopqrstuvwxyz"
_SC_DST = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
_SC_TABLE = {ord(a): b for a, b in zip(_SC_SRC, _SC_DST)}
_SC_TABLE.update({ord(a.upper()): b for a, b in zip(_SC_SRC, _SC_DST)})
_SC_SKIP = re.compile(
    r"```.*?```|`[^`\n]*`|<(code|pre)\b[^>]*>.*?</\1>|<[^>\n]+>|&[#\w]+;|https?://\S+"
    r"|(?<![\w/])/[A-Za-z_]\w*|@\w+|\{[^}\n]*\}", re.S)

def sc(text):
    """Small-caps styling for bot text (idempotent; markup, code, links and commands are preserved)."""
    if not isinstance(text, str) or not text:
        return text
    out, pos = [], 0
    for m in _SC_SKIP.finditer(text):
        out.append(text[pos:m.start()].translate(_SC_TABLE))
        out.append(m.group(0))
        pos = m.end()
    out.append(text[pos:].translate(_SC_TABLE))
    return "".join(out)

def _style_text_arg(args: tuple, kwargs: dict, index: int):
    args = list(args)
    if "text" in kwargs:
        kwargs["text"] = sc(kwargs["text"])
    elif len(args) > index:
        args[index] = sc(args[index])
    return tuple(args), kwargs

_orig_send_message = Client.send_message
_orig_edit_message_text = Client.edit_message_text
_orig_callback_answer = CallbackQuery.answer

async def _styled_send_message(self, *args, **kwargs):
    args, kwargs = _style_text_arg(args, kwargs, 1)
    return await _orig_send_message(self, *args, **kwargs)

async def _styled_edit_message_text(self, *args, **kwargs):
    args, kwargs = _style_text_arg(args, kwargs, 2)
    return await _orig_edit_message_text(self, *args, **kwargs)

async def _styled_callback_answer(self, *args, **kwargs):
    args, kwargs = _style_text_arg(args, kwargs, 0)
    return await _orig_callback_answer(self, *args, **kwargs)

Client.send_message = _styled_send_message
Client.edit_message_text = _styled_edit_message_text
CallbackQuery.answer = _styled_callback_answer

_PlainInlineKeyboardButton = InlineKeyboardButton

class InlineKeyboardButton(_PlainInlineKeyboardButton):
    """Same button, label shown in the bot's small-caps style."""
    def __init__(self, text, *args, **kwargs):
        super().__init__(sc(text), *args, **kwargs)

# ------------------------------------------------------------------------------
# 1. LOGGING & CONFIGURATION SETUP
# ------------------------------------------------------------------------------
def _log_level(name: str, default: str) -> int:
    return getattr(logging, os.environ.get(name, default).strip().upper(), getattr(logging, default))

_log_handlers = [logging.StreamHandler(sys.stdout)]
if os.environ.get("LOG_TO_FILE", "1") != "0":
    try:
        _log_handlers.append(logging.FileHandler(os.environ.get("LOG_FILE", "bot.log"), mode="a", encoding="utf-8"))
    except OSError:
        pass
logging.basicConfig(
    level=_log_level("LOG_LEVEL", "INFO"),
    format="[%(asctime)s - %(levelname)s] - %(name)s - %(message)s",
    handlers=_log_handlers,
)
for _noisy in ("pyrogram", "aiohttp", "asyncio"):
    logging.getLogger(_noisy).setLevel(_log_level("LOG_LIBRARIES_LEVEL", "WARNING"))
logger = logging.getLogger("SRT_Hardcoder_Bot")

# Environment Variables
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 7860))  # HF Space Default Port

# --- Encoding / watermark settings (all can be overridden with env variables) ---
DEFAULT_WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "Sadesha.Sub")
WATERMARK_COLOR = os.environ.get("WATERMARK_COLOR", "#87CEFA")      # light blue
WATERMARK_ALIGN = os.environ.get("WATERMARK_ALIGN", "1")            # 1 = bottom-left, 7 = top-left
WATERMARK_MARGIN = os.environ.get("WATERMARK_MARGIN", "8")          # distance from the screen corner
SUB_FONT_SIZE = os.environ.get("SUB_FONT_SIZE", "48")               # subtitle size (was 44)
WM_FONT_SIZE = os.environ.get("WM_FONT_SIZE", "28")
ENCODE_CODEC = os.environ.get("ENCODE_CODEC", "auto")               # auto = same codec as the source, or h264 / hevc
ENCODE_PRESET_HEVC = os.environ.get("ENCODE_PRESET_HEVC", "fast")           # HEVC Best quality
ENCODE_CRF_HEVC = os.environ.get("ENCODE_CRF_HEVC", "22")           # x265 CRF 22 ~ x264 CRF 18
ENCODE_PRESET_FAST = os.environ.get("ENCODE_PRESET_FAST", "veryfast")             # H.264 Fast
ENCODE_PRESET_BAL = os.environ.get("ENCODE_PRESET_BAL", "faster")                 # H.264 Balanced
ENCODE_PRESET_HEVC_FAST = os.environ.get("ENCODE_PRESET_HEVC_FAST", "superfast")  # HEVC Fast
ENCODE_PRESET_HEVC_BAL = os.environ.get("ENCODE_PRESET_HEVC_BAL", "veryfast")     # HEVC Balanced
DEFAULT_SPEED = os.environ.get("DEFAULT_SPEED", "fast")                           # fast | balanced | best
SEND_AS = os.environ.get("SEND_AS", "video")                        # video (default) or document
ENCODE_PRESET = os.environ.get("ENCODE_PRESET", "medium")            # H.264 Best quality
ENCODE_CRF = os.environ.get("ENCODE_CRF", "18")                     # quality (lower = better)
MAX_PARALLEL_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "1"))   # encodes at the same time
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "900"))         # kill ffmpeg if stuck (seconds)
TG_MAX_UPLOAD = int(float(os.environ.get("TG_MAX_UPLOAD_GB", "1.95")) * 1024 ** 3)

# Storage directories
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "outputs"
FONTS_DIR = BASE_DIR / "fonts"
STICKER_MIN_INTERVAL = max(0.0, float(os.environ.get("STICKER_MIN_INTERVAL", "0.8")))
# Search the user's own public pack first, then a known fallback; retain both even
# when an older deployment overrides STICKER_PACK_NAMES with an invalid name.
PUBLIC_STICKER_PACK_DEFAULTS = ("SubAnalyserRobot_by_fStikBot", "FunkyGoose")
_configured_sticker_packs = [
    name.strip().removeprefix("@").strip()
    for name in os.environ.get("STICKER_PACK_NAMES", ",".join(PUBLIC_STICKER_PACK_DEFAULTS)).split(",")
    if name.strip()
]
STICKER_PACK_NAMES = list(dict.fromkeys([*PUBLIC_STICKER_PACK_DEFAULTS, *_configured_sticker_packs]))
STICKER_EVENT_EMOJIS = {
    "received": ("👋", "😊", "😘", "🤗"),
    "download": ("📥", "⬇️", "💻", "🤓", "👍"),
    "encoding": ("⚙️", "🛠️", "🤔", "🤓", "😅"),
    "quality": ("🔎", "🤔", "🧐", "✅"),
    "upload": ("📤", "⬆️", "🚀", "💪", "🤷", "📲"),
    "extract": ("✂️", "📦", "💾", "🤓", "👍"),
    "sync": ("🔄", "🤔", "❓", "🧐"),
    "success": ("✅", "🎉", "🥳", "💯", "👍", "❤️"),
    "error": ("❌", "😨", "😡", "😭", "❓", "😢"),
    "menu": ("👋", "😊", "😘", "🦢"),
    "welcome": ("👋", "😊", "😘", "🦢"),
}
STICKER_EVENT_ORDER = tuple(STICKER_EVENT_EMOJIS)
STICKER_FILE_IDS = {
    event: os.environ.get(f"STICKER_{event.upper()}", "").strip()
    for event in STICKER_EVENT_EMOJIS
}
STICKER_CANDIDATES: Dict[str, list] = {event: ([file_id] if file_id else [])
                                       for event, file_id in STICKER_FILE_IDS.items()}

for d in [DOWNLOAD_DIR, OUTPUT_DIR, FONTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DASHBOARD_DIR = BASE_DIR / "webapp"
DASHBOARD_DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", str(BASE_DIR / "dashboard_history.sqlite3"))).expanduser()
DASHBOARD_UPLOAD_MAX_BYTES = int(float(os.environ.get("DASHBOARD_UPLOAD_MAX_GB", "1.9")) * 1024 ** 3)
DASHBOARD_SRT_MAX_BYTES = int(os.environ.get("DASHBOARD_SRT_MAX_MB", "20")) * 1024 * 1024
DASHBOARD_AUTH_MAX_AGE_SECONDS = max(300, int(os.environ.get("DASHBOARD_AUTH_MAX_AGE_SECONDS", "86400")))

# Fonts are sourced from the local fonts/ directory and explicit user uploads only.
def font_family_of(path: Path, default: str) -> str:
    """Real family name of a font file (via fc-scan), so the ASS style always matches."""
    try:
        out = subprocess.run(
            ["fc-scan", "--format", "%{family}\n", str(path)],
            capture_output=True, text=True, timeout=20
        ).stdout
        names = [n.strip() for n in out.replace("\n", ",").split(",") if n.strip()]
        if names:
            return min(names, key=len)
    except Exception:
        pass
    return default

def font_ok(name: str) -> bool:
    p = FONTS_DIR / name
    return p.exists() and p.stat().st_size > 10000


# The default font is the one you placed in the fonts/ folder (nothing is bundled or downloaded).
FONT_SETTINGS_FILE = BASE_DIR / "font_settings.json"

def _shared_font_files() -> list:
    """Fonts you placed in the fonts/ folder (not per-user uploads)."""
    files = []
    try:
        for p in sorted(FONTS_DIR.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf") and p.stat().st_size > 10000 and not re.match(r"^u\d+_", p.name):
                files.append(p)
    except Exception:
        pass
    return files

def _default_font_family() -> str:
    files = _shared_font_files()
    if files:
        return font_family_of(files[0], files[0].stem)
    logger.warning("No font found in the fonts/ folder - put your .ttf / .otf there.")
    return "Sans"

def _installed_families() -> set:
    fams = set()
    try:
        for p in FONTS_DIR.iterdir():
            if p.suffix.lower() in (".ttf", ".otf") and p.stat().st_size > 10000:
                fams.add(font_family_of(p, p.stem))
    except Exception:
        pass
    return fams

def _load_saved_font() -> str:
    """Saved subtitle font - ignored if that font file no longer exists (e.g. removed Abhaya / Noto)."""
    try:
        with open(FONT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            family = str(json.load(f).get("sub_family", ""))
        return family if family in _installed_families() else ""
    except Exception:
        return ""

def _save_font(family: str) -> None:
    try:
        with open(FONT_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"sub_family": family}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save font setting: {e}")

# Default: the font placed in the fonts/ folder.
# Change it live with /fonttest + /setfont, or with the SUB_FONT_FAMILY env variable.
SUB_FONT_FAMILY = os.environ.get("SUB_FONT_FAMILY") or _load_saved_font() or _default_font_family()
WM_FONT_FAMILY = os.environ.get("WM_FONT_FAMILY") or _default_font_family()
logger.info(f"Subtitle font: {SUB_FONT_FAMILY} | Watermark font: {WM_FONT_FAMILY}")

def _reset_missing_user_fonts() -> None:
    """Per-user styles may still name a removed font; clear those so the default font is used."""
    try:
        families = _installed_families()
        changed = False
        for st in ve.STYLES.values():
            if st.get("font") and st["font"] not in families:
                st["font"] = None
                changed = True
        if changed:
            with open(ve.STYLE_FILE, "w", encoding="utf-8") as f:
                json.dump(ve.STYLES, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Could not clean saved user fonts: %s", e)

_reset_missing_user_fonts()

USER_DATA: Dict[int, Dict[str, Any]] = {}
JOBS: Dict[int, Dict[str, Any]] = {}          # running jobs (for /cancel)
BACKGROUND_TASKS: set = set()
ENCODE_SEM = asyncio.Semaphore(MAX_PARALLEL_JOBS)

# ---- per-user watermark text (saved to a small json file) ----
WM_FILE = BASE_DIR / "watermark_settings.json"

def _load_wm_settings() -> Dict[str, str]:
    try:
        with open(WM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

WM_SETTINGS: Dict[str, str] = _load_wm_settings()

def clean_wm_text(text: str) -> str:
    text = re.sub(r"[{}\\\r\n]", "", text or "").strip()
    return text[:40]

def get_wm_text(user_id: int) -> str:
    return clean_wm_text(WM_SETTINGS.get(str(user_id), "")) or DEFAULT_WATERMARK_TEXT

def set_wm_text(user_id: int, text: str) -> None:
    WM_SETTINGS[str(user_id)] = text
    try:
        with open(WM_FILE, "w", encoding="utf-8") as f:
            json.dump(WM_SETTINGS, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save watermark settings: {e}")

try:
    bot = Client(
        "srt_hardcoder_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
        max_concurrent_transmissions=int(os.environ.get("TG_TRANSMISSIONS", "6"))   # faster big uploads / downloads
    )
except TypeError:                                                                  # older Pyrogram builds
    bot = Client("srt_hardcoder_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ------------------------------------------------------------------------------
# 2. HUGGINGFACE SPACES HEALTH CHECK SERVER
# ------------------------------------------------------------------------------
async def health_check(request: web.Request) -> web.Response:
    return web.Response(
        text="<html><body><h1>SRT Hardcoder Bot is Running Alive!</h1></body></html>",
        content_type="text/html",
        status=200
    )

@web.middleware
async def dashboard_enabled_middleware(request: web.Request, handler):
    if request.path == "/dashboard" or request.path.startswith("/dashboard/"):
        if not WEBAPP_ENABLED:
            raise web.HTTPNotFound(text="Not Found")
    return await handler(request)

async def start_web_server() -> None:
    app = web.Application(
        client_max_size=max(DASHBOARD_UPLOAD_MAX_BYTES, DASHBOARD_SRT_MAX_BYTES) + 2 * 1024 * 1024,
        middlewares=[dashboard_enabled_middleware],
    )
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_get("/sync/{token}", sync_page)
    app.router.add_get("/sync/{token}/state", sync_state)
    app.router.add_get("/page/{token}", page_view)
    app.router.add_get("/dashboard", dashboard_page)
    app.router.add_get("/dashboard/", dashboard_page)
    app.router.add_get("/dashboard/assets/{asset:.*}", dashboard_asset)
    app.router.add_get("/dashboard/media/{kind}/{uid}/{key}/{expiry}/{signature}", dashboard_media)
    app.router.add_get("/dashboard/api/state", dashboard_state)
    app.router.add_get("/dashboard/api/subtitle/cues", dashboard_subtitle_cues)
    app.router.add_post("/dashboard/api/upload", dashboard_upload)
    app.router.add_post("/dashboard/api/sync/start", dashboard_sync_start)
    app.router.add_post("/dashboard/api/sync/upload", dashboard_sync_upload)
    app.router.add_post("/dashboard/api/start", dashboard_start_job)
    app.router.add_post("/dashboard/api/cancel", dashboard_cancel_job)
    app.router.add_post("/dashboard/api/retry", dashboard_retry_job)
    app.router.add_post("/dashboard/api/name", dashboard_set_output_name)
    app.router.add_post("/dashboard/api/delivery/cancel", dashboard_cancel_delivery)
    app.router.add_post("/dashboard/api/style", dashboard_update_style)
    app.router.add_post("/dashboard/api/style/preview", dashboard_style_preview)
    app.router.add_post("/dashboard/api/intro", dashboard_intro)
    app.router.add_post("/dashboard/api/watermark", dashboard_update_watermark)
    app.router.add_post("/dashboard/api/font", dashboard_upload_font)
    app.router.add_post("/dashboard/api/font/delete", dashboard_delete_font)
    app.router.add_post("/dashboard/api/delete", dashboard_delete_library)
    # TanStack client-side routes such as /dashboard/process must return the SPA shell.
    app.router.add_get("/dashboard/{subpath:.*}", dashboard_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health check web server running on port {PORT}")

# ------------------------------------------------------------------------------
# 3. HELPER FUNCTIONS & PROGRESS BAR
# ------------------------------------------------------------------------------
def humanbytes(size: float) -> str:
    if not size:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

def time_formatter(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    res = []
    if days: res.append(f"{days}d")
    if hours: res.append(f"{hours}h")
    if minutes: res.append(f"{minutes}m")
    if seconds: res.append(f"{seconds}s")
    return " ".join(res) if res else "0s"

def premium_report_html(report: str) -> str:
    """Reflow the unchanged quality-engine report into a readable Telegram card without its box border."""
    title, rows = "📊 Video Report", []
    headings = {"VIDEO SOURCE ANALYSIS": "Source video analysis",
                "SOURCE VS OUTPUT": "Source vs output",
                "VALIDATION": "Output check", "QUALITY METRICS": "Quality metrics"}
    label_si = {"Codec": "Codec", "Resolution": "Resolution", "Frame Rate": "Frame rate",
                "Bit Depth": "Bit depth", "Colour": "Colour range", "Video rate": "Video bitrate",
                "Audio": "Audio", "Audio tracks": "Audio tracks", "Subtitles": "Subtitle tracks",
                "Mode": "Mode", "Encoder": "Encoder", "Bitrate cap": "Bitrate limit",
                "Output px": "Output pixel format", "Est. time": "Estimated time",
                "Est. size": "Estimated size", "Frame rate": "Frame rate",
                "Pixel fmt": "Pixel format", "Visual quality": "Visual quality"}
    for raw in str(report or "").splitlines():
        line = raw.strip()
        if line.startswith("╭─ "):
            section = re.sub(r"─+╮?$", "", line[3:]).strip()
            if section in headings:
                section = headings[section]
            if rows:
                rows.append("")
            rows.append(f"<b>🔎 {htmlmod.escape(section)}</b>")
            continue
        if line.startswith(("╰", "─")):
            continue
        if line.startswith("│"):
            line = line[1:].rsplit("│", 1)[0].strip()
        if not line:
            continue
        if ":" in line:
            label, value = line.split(":", 1)
            label = label.strip()
            rows.append(f"• <b>{htmlmod.escape(label_si.get(label, label))}:</b> {htmlmod.escape(value.strip())}")
        else:
            rows.append(f"• {htmlmod.escape(line)}")
    return f"<b>{htmlmod.escape(title)}</b>\n\n" + "\n".join(rows)

_STICKER_LAST_SENT: Dict[Tuple[int, str], float] = {}
STICKER_PACK_LOAD_REPORT: Dict[str, Dict[str, Any]] = {}
STICKER_SEND_ERRORS: Dict[str, str] = {}
_STICKER_WARNED_EVENTS: set = set()

def safe_sticker_error(exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    if BOT_TOKEN:
        detail = detail.replace(BOT_TOKEN, "[redacted token]")
    return detail[:180]

async def load_sticker_pack() -> int:
    """Refresh public packs via Telegram's Bot API; retain bot-specific manual file_id overrides."""
    if not STICKER_PACK_NAMES:
        logger.warning("No public sticker packs are configured")
        return 0
    for event, file_id in STICKER_FILE_IDS.items():
        STICKER_CANDIDATES[event] = [file_id] if file_id else []
    fallback_stickers = []
    matched_events = set()
    STICKER_PACK_LOAD_REPORT.clear()
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        error = "BOT_TOKEN is missing or still set to the placeholder"
        for pack_name in STICKER_PACK_NAMES:
            STICKER_PACK_LOAD_REPORT[pack_name] = {"ok": False, "error": error}
        return sum(len(ids) for ids in STICKER_CANDIDATES.values())
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for pack_name in STICKER_PACK_NAMES:
                try:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getStickerSet"
                    async with session.get(url, params={"name": pack_name}) as response:
                        payload = await response.json(content_type=None)
                        if not isinstance(payload, dict):
                            raise RuntimeError(f"HTTP {response.status}: invalid Telegram response")
                        if response.status != 200 or not payload.get("ok"):
                            detail = str(payload.get("description") or "Telegram rejected getStickerSet")
                            raise RuntimeError(f"HTTP {response.status}: {detail}")
                    stickers = list((payload.get("result") or {}).get("stickers") or ())
                    if not stickers:
                        STICKER_PACK_LOAD_REPORT[pack_name] = {"ok": False, "error": "empty sticker set"}
                        continue
                    STICKER_PACK_LOAD_REPORT[pack_name] = {"ok": True, "count": len(stickers)}
                    if not fallback_stickers:
                        fallback_stickers = stickers
                    for event, wanted in STICKER_EVENT_EMOJIS.items():
                        matches = []
                        for sticker in stickers:
                            emoji = str(sticker.get("emoji") or "")
                            file_id = str(sticker.get("file_id") or "").strip()
                            if file_id and emoji in wanted:
                                matches.append(file_id)
                        current = STICKER_CANDIDATES.setdefault(event, [])
                        for file_id in matches:
                            if file_id not in current:
                                current.append(file_id)
                        if matches:
                            matched_events.add(event)
                    logger.info("Resolved public sticker pack %s (%d stickers)", pack_name, len(stickers))
                except asyncio.TimeoutError as exc:
                    error = "Telegram Bot API timed out"
                    STICKER_PACK_LOAD_REPORT[pack_name] = {"ok": False, "error": error}
                    logger.warning("Sticker pack lookup timed out for %s", pack_name)
                except Exception as exc:
                    error = safe_sticker_error(exc)
                    STICKER_PACK_LOAD_REPORT[pack_name] = {"ok": False, "error": error}
                    logger.warning("Could not read public sticker set %s: %s", pack_name, error)
    except Exception as exc:
        error = safe_sticker_error(exc)
        for pack_name in STICKER_PACK_NAMES:
            STICKER_PACK_LOAD_REPORT.setdefault(pack_name, {"ok": False, "error": error})
        logger.warning("Could not create Telegram sticker API session: %s", error)
    # If neither configured pack contains an emoji match for an event, choose spread-out
    # positions from the first readable pack. This is a visual fallback, not a semantic match.
    if fallback_stickers:
        last = len(fallback_stickers) - 1
        span = max(1, len(STICKER_EVENT_ORDER) - 1)
        for event_index, event in enumerate(STICKER_EVENT_ORDER):
            if event in matched_events:
                continue
            index = round(event_index * last / span) if last else 0
            file_id = str(fallback_stickers[index].get("file_id") or "").strip()
            current = STICKER_CANDIDATES.setdefault(event, [])
            if file_id and file_id not in current:
                current.append(file_id)
    coverage = {event: len(ids) for event, ids in STICKER_CANDIDATES.items() if ids}
    loaded_ids = sum(len(ids) for ids in STICKER_CANDIDATES.values())
    if not coverage:
        logger.warning("No sticker IDs resolved. Check bot token, pack names, and Bot API access; no animation fallback is used.")
    else:
        logger.info("Sticker event coverage: %s", coverage)
    return loaded_ids

# ------------------------------------------------------------------------------
# USER STICKERS  -  /stickers  ->  step-by-step setup (one sticker per bot step)
#   * every step has its own sticker, its own ON/OFF switch and (for timed ones) its own auto-delete time
#   * saved in sticker_store.json (next to the code, or /data / STICKER_STORE_DIR when that exists)
#   * a backup copy is pinned in your chat and can be sent back to the bot to restore
# ------------------------------------------------------------------------------
STICKER_BACKUP_NAME = "stickers_backup.json"
# key, label, short label, timed (deletes by its own timer) , default seconds
STICKER_STEPS = [
    ("welcome", "Start message", "Start", True, int(os.environ.get("STICKER_TTL_WELCOME", "6") or 6)),
    ("menu", "Menu message", "Menu", True, int(os.environ.get("STICKER_TTL_MENU", "6") or 6)),
    ("download", "Receiving video / file", "Download", False, 0),
    ("encoding", "Encoding", "Encode", False, 0),
    ("quality", "Quality check", "Quality", False, 0),
    ("extract", "Extracting track", "Extract", False, 0),
    ("upload", "Uploading", "Upload", False, 0),
    ("sync", "Sync check", "Sync", False, 0),
    ("success", "Success", "Success", True, int(os.environ.get("STICKER_TTL_SUCCESS", "10") or 10)),
    ("error", "Error", "Error", True, int(os.environ.get("STICKER_TTL_ERROR", "10") or 10)),
]
STICKER_LABEL = {k: l for k, l, _, _, _ in STICKER_STEPS}
STICKER_SHORT = {k: s for k, _, s, _, _ in STICKER_STEPS}
STICKER_TIMED = {k for k, _, _, t, _ in STICKER_STEPS if t}
STICKER_TTL_DEFAULT = {k: d for k, _, _, _, d in STICKER_STEPS}
STICKER_TTL_CHOICES = (3, 5, 10, 30, 0)          # 0 = keep


def _env_int_set(name: str) -> set:
    values = set()
    for raw in os.environ.get(name, "").split(","):
        raw = raw.strip()
        if raw.lstrip("-").isdigit():
            values.add(int(raw))
    return values


STICKER_ADMIN_IDS = _env_int_set("STICKER_ADMIN_IDS")
STICKER_BACKUP_CHAT_IDS = _env_int_set("STICKER_BACKUP_CHAT_ID")
STICKER_STORE: Dict[str, Any] = {"events": {}, "settings": {}, "owner": 0, "backup_msg": 0, "backup_chat": 0}
STICKER_WIZ: Dict[int, Dict[str, Any]] = {}
ACCESS_DB: Dict[str, Any] = {"users": {}}      # users / credits / limits (see ACCESS CONTROL below)


def _sticker_store_dirs() -> list:
    dirs = []
    custom = os.environ.get("STICKER_STORE_DIR", "").strip()
    if custom:
        dirs.append(Path(custom))
    elif Path("/data").is_dir() and os.access("/data", os.W_OK):      # Hugging Face persistent storage
        dirs.append(Path("/data"))
    dirs.append(BASE_DIR)
    return dirs


def _norm_emoji(value: Any) -> str:
    return str(value or "").replace("\ufe0f", "").strip()


def _migrate_store(data: Dict[str, Any]) -> Dict[str, Any]:
    """Accept the new format, or the old flat pool (assign by emoji)."""
    out = {"events": {}, "settings": {}}
    if isinstance(data.get("events"), dict):
        for ev, items in data["events"].items():
            if ev in STICKER_LABEL and isinstance(items, list):
                out["events"][ev] = [i for i in items if isinstance(i, dict) and i.get("id")]
    elif isinstance(data.get("stickers"), dict):
        for item in data["stickers"].values():
            for ev, _, _, _, _ in STICKER_STEPS:
                if _norm_emoji(item.get("emoji")) in {_norm_emoji(e) for e in STICKER_EVENT_EMOJIS.get(ev, ())}:
                    out["events"].setdefault(ev, []).append({"id": item["id"], "u": "", "emoji": item.get("emoji", "")})
    if isinstance(data.get("settings"), dict):
        out["settings"] = {k: v for k, v in data["settings"].items() if k in STICKER_LABEL and isinstance(v, dict)}
    for key in ("owner", "backup_msg", "backup_chat"):
        out[key] = data.get(key, 0)
    return out


def load_sticker_store() -> None:
    for folder in _sticker_store_dirs():
        path = folder / "sticker_store.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                STICKER_STORE.update(_migrate_store(json.load(f)))
            logger.info("Loaded saved stickers from %s", path)
            return
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning("Could not read %s: %s", path, e)
    # Nothing saved on this machine (new deploy / restarted Space): use the stickers bundled with the code.
    seed = BASE_DIR / "default_stickers.json"
    try:
        with open(seed, "r", encoding="utf-8") as f:
            STICKER_STORE.update(_migrate_store(json.load(f)))
        logger.info("Loaded bundled stickers from %s", seed)
        save_sticker_store()
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not read %s: %s", seed, e)


def save_sticker_store() -> None:
    for folder in _sticker_store_dirs():
        try:
            tmp = folder / "sticker_store.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(STICKER_STORE, f, ensure_ascii=False)
            os.replace(tmp, folder / "sticker_store.json")
        except Exception as e:
            logger.warning("Could not save stickers in %s: %s", folder, e)


def store_has_stickers() -> bool:
    return any(STICKER_STORE.get("events", {}).values())


def sticker_setting(event: str) -> Dict[str, Any]:
    saved = (STICKER_STORE.get("settings") or {}).get(event) or {}
    return {"on": bool(saved.get("on", True)), "ttl": int(saved.get("ttl", STICKER_TTL_DEFAULT.get(event, 0)))}


def sticker_enabled(event: str) -> bool:
    return sticker_setting(event)["on"]


def saved_stickers_for(event: str) -> list:
    ids = [i["id"] for i in (STICKER_STORE.get("events", {}).get(event) or [])]
    random.shuffle(ids)
    return ids


def _bot_admin_ok(user_id: int) -> bool:
    allowed = STICKER_ADMIN_IDS | WEBAPP_ADMIN_IDS
    return not allowed or user_id in allowed


def _sticker_backup_targets(extra: Optional[int] = None) -> list:
    ordered = [extra, *sorted(STICKER_BACKUP_CHAT_IDS), STICKER_STORE.get("owner"), STICKER_STORE.get("backup_chat"),
               *sorted(WEBAPP_ADMIN_IDS), *sorted(STICKER_ADMIN_IDS)]
    return [int(c) for c in dict.fromkeys(c for c in ordered if c)]


async def backup_sticker_store(client: Client, chat_id: int) -> bool:
    """Send + pin a JSON copy of all stickers/settings. Send that file back to the bot to restore."""
    try:
        old_chat, old_msg = int(STICKER_STORE.get("backup_chat") or 0), int(STICKER_STORE.get("backup_msg") or 0)
        bundle = dict(STICKER_STORE)
        bundle["access"] = ACCESS_DB
        payload = io.BytesIO(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
        payload.name = STICKER_BACKUP_NAME
        sent = await client.send_document(chat_id, payload, caption=sc("Sticker backup - send this file to the bot to restore"),
                                          disable_notification=True)
        STICKER_STORE["backup_chat"], STICKER_STORE["backup_msg"] = int(chat_id), int(sent.id)
        save_sticker_store()
        try:
            await sent.pin(disable_notification=True)
        except Exception as e:
            logger.warning("Could not pin sticker backup: %s", e)
        if old_msg and old_chat:
            try:
                await client.delete_messages(old_chat, old_msg)
            except Exception:
                pass
        return True
    except Exception as e:
        logger.warning("Sticker backup failed: %s", e)
        return False


async def restore_sticker_store(client: Client) -> None:
    """Fresh disk (e.g. Space restart)? restore stickers and the user/credit data from the pinned backup."""
    need_st, need_ac = not store_has_stickers(), not ACCESS_DB.get("users")
    if not (need_st or need_ac):
        return
    for chat_id in _sticker_backup_targets():
        try:
            chat = await client.get_chat(chat_id)
            pinned = getattr(chat, "pinned_message", None)
            doc = getattr(pinned, "document", None)
            if not doc or doc.file_name != STICKER_BACKUP_NAME:
                continue
            data = await client.download_media(pinned, in_memory=True)
            obj = json.loads(bytes(data.getbuffer()).decode("utf-8"))
            if need_st:
                STICKER_STORE.update(_migrate_store(obj))
                if store_has_stickers():
                    save_sticker_store()
                    logger.info("Restored stickers from the pinned backup in chat %s", chat_id)
            if need_ac and isinstance(obj.get("access"), dict) and obj["access"].get("users"):
                ACCESS_DB.update(obj["access"])
                _save_access()
                logger.info("Restored %d users from the pinned backup", len(ACCESS_DB["users"]))
            if (store_has_stickers() or not need_st) and (ACCESS_DB.get("users") or not need_ac):
                return
        except Exception as e:
            logger.warning("Restore from chat %s failed: %s", chat_id, e)


# ---------------- panels ----------------
def sticker_panel_text() -> str:
    lines = ["🧩 **Sticker Setup**", ""]
    for key, label, _, timed, _ in STICKER_STEPS:
        st, n = sticker_setting(key), len(STICKER_STORE.get("events", {}).get(key) or [])
        extra = ""
        if timed:
            extra = f" • deletes after {st['ttl']}s" if st["ttl"] else " • kept"
        lines.append(f"{'✅' if st['on'] else '⛔'} {label}: {n}{extra}")
    lines += ["", "Steps that run with a job delete their sticker when the step ends."]
    return "\n".join(lines)


def sticker_panel_keyboard() -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [B("🎯 Set stickers", callback_data="stk:wiz")],
        [B("⚙️ Settings", callback_data="stk:set"), B("📦 Backup", callback_data="stk:bk")],
        [B("🗑 Clear all", callback_data="stk:clear")],
    ])


def sticker_settings_keyboard() -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    rows, row = [], []
    for key, _, short, _, _ in STICKER_STEPS:
        row.append(B(f"{'✅' if sticker_enabled(key) else '⛔'} {short}", callback_data=f"stk:ev:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("⬅️ Back", callback_data="stk:menu")])
    return InlineKeyboardMarkup(rows)


def sticker_event_text(event: str) -> str:
    st, n = sticker_setting(event), len(STICKER_STORE.get("events", {}).get(event) or [])
    lines = [f"🎯 **{STICKER_LABEL[event]}**", "", f"Status: {'ON ✅' if st['on'] else 'OFF ⛔'}", f"Stickers saved: {n}"]
    if event in STICKER_TIMED:
        lines.append(f"Auto delete: {str(st['ttl']) + ' seconds' if st['ttl'] else 'never (kept)'}")
    else:
        lines.append("Auto delete: when this step finishes")
    return "\n".join(lines)


def sticker_event_keyboard(event: str) -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    st = sticker_setting(event)
    rows = [[B("⛔ Turn off" if st["on"] else "✅ Turn on", callback_data=f"stk:tog:{event}")]]
    if event in STICKER_TIMED:
        rows.append([B(("✓ " if st["ttl"] == s else "") + (f"{s}s" if s else "Keep"), callback_data=f"stk:ttl:{event}:{s}")
                     for s in STICKER_TTL_CHOICES[:3]])
        rows.append([B(("✓ " if st["ttl"] == s else "") + (f"{s}s" if s else "Keep"), callback_data=f"stk:ttl:{event}:{s}")
                     for s in STICKER_TTL_CHOICES[3:]])
    rows.append([B("🎯 Set sticker", callback_data=f"stk:one:{event}"), B("🗑 Remove", callback_data=f"stk:rm:{event}")])
    rows.append([B("⬅️ Back", callback_data="stk:set")])
    return InlineKeyboardMarkup(rows)


# ---------------- step-by-step wizard ----------------
def _wiz_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="stk:skip"),
                                  InlineKeyboardButton("⏹ Stop", callback_data="stk:stop")]])


async def _edit_or_send(client: Client, chat_id: int, holder: Dict[str, Any], text: str, markup) -> None:
    try:
        if holder.get("msg"):
            await holder["msg"].edit_text(text, reply_markup=markup)
            return
    except RPCError:
        holder["msg"] = None
    holder["msg"] = await client.send_message(chat_id, text, reply_markup=markup)


async def wiz_show(client: Client, uid: int, note: str = "") -> None:
    wiz = STICKER_WIZ.get(uid)
    if not wiz:
        return
    chat_id = wiz["chat"]
    if wiz["i"] >= len(wiz["queue"]):
        STICKER_WIZ.pop(uid, None)
        STICKER_STORE["owner"] = uid
        save_sticker_store()
        backed = await backup_sticker_store(client, chat_id)
        await _edit_or_send(client, chat_id, wiz, (note + "\n\n" if note else "") + "✅ **Done.** Stickers are active and saved for every restart."
                            + ("\n📌 Backup pinned in this chat." if backed else "") + "\n\n" + sticker_panel_text(),
                            sticker_panel_keyboard())
        return
    event = wiz["queue"][wiz["i"]]
    n = len(STICKER_STORE.get("events", {}).get(event) or [])
    text = ((note + "\n\n") if note else "") + (
        f"🧩 **Step {wiz['i'] + 1}/{len(wiz['queue'])}: {STICKER_LABEL[event]}**\n\n"
        f"Send the sticker for this step.\nSaved now: {n}\nNot needed? Tap Skip.")
    await _edit_or_send(client, chat_id, wiz, text, _wiz_keyboard())


async def start_wizard(client: Client, uid: int, chat_id: int, queue: list) -> None:
    STICKER_WIZ[uid] = {"queue": queue, "i": 0, "chat": chat_id, "msg": None, "ts": time.time()}
    await wiz_show(client, uid)


async def sticker_callback(client: Client, callback: CallbackQuery, data: str, uid: int) -> None:
    if not _bot_admin_ok(uid):
        await callback.answer("You are not allowed to change stickers.", show_alert=True)
        return
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    msg = callback.message

    async def show(text, markup):
        try:
            await msg.edit_text(text, reply_markup=markup)
        except RPCError:
            pass

    if action == "wiz":
        STICKER_WIZ.pop(uid, None)
        await callback.answer()
        wiz = {"queue": [k for k, *_ in STICKER_STEPS], "i": 0, "chat": msg.chat.id, "msg": msg, "ts": time.time()}
        STICKER_WIZ[uid] = wiz
        await wiz_show(client, uid)
        return
    if action in ("skip", "stop"):
        wiz = STICKER_WIZ.get(uid)
        if not wiz:
            await show(sticker_panel_text(), sticker_panel_keyboard())
            await callback.answer()
            return
        wiz["msg"] = msg
        wiz["i"] = len(wiz["queue"]) if action == "stop" else wiz["i"] + 1
        await callback.answer()
        await wiz_show(client, uid)
        return
    if action == "one":
        event = parts[2]
        STICKER_WIZ.pop(uid, None)
        STICKER_WIZ[uid] = {"queue": [event], "i": 0, "chat": msg.chat.id, "msg": msg, "ts": time.time()}
        await callback.answer()
        await wiz_show(client, uid)
        return
    STICKER_WIZ.pop(uid, None)
    if action == "menu":
        await show(sticker_panel_text(), sticker_panel_keyboard())
    elif action == "set":
        await show("⚙️ **Sticker settings**\nChoose a step:", sticker_settings_keyboard())
    elif action == "ev":
        await show(sticker_event_text(parts[2]), sticker_event_keyboard(parts[2]))
    elif action == "tog":
        cfg = STICKER_STORE.setdefault("settings", {}).setdefault(parts[2], {})
        cfg["on"] = not sticker_setting(parts[2])["on"]
        save_sticker_store()
        await show(sticker_event_text(parts[2]), sticker_event_keyboard(parts[2]))
    elif action == "ttl":
        cfg = STICKER_STORE.setdefault("settings", {}).setdefault(parts[2], {})
        cfg["ttl"] = int(parts[3])
        save_sticker_store()
        await show(sticker_event_text(parts[2]), sticker_event_keyboard(parts[2]))
    elif action == "rm":
        STICKER_STORE.setdefault("events", {})[parts[2]] = []
        save_sticker_store()
        await show(sticker_event_text(parts[2]), sticker_event_keyboard(parts[2]))
    elif action == "clear":
        await show("🗑 **Remove ALL saved stickers?**", InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, remove", callback_data="stk:clearyes"),
            InlineKeyboardButton("↩️ No", callback_data="stk:menu")]]))
    elif action == "clearyes":
        STICKER_STORE["events"] = {}
        save_sticker_store()
        await show(sticker_panel_text(), sticker_panel_keyboard())
    elif action == "bk":
        ok = await backup_sticker_store(client, msg.chat.id)
        await callback.answer("Backup sent and pinned." if ok else "Backup failed.", show_alert=not ok)
        return
    await callback.answer()


STICKER_WAIT = float(os.environ.get("STICKER_WAIT_SECONDS", "6"))   # max seconds a step waits for its sticker
STICKER_LIVE: Dict[Tuple[int, int], Dict[str, Any]] = {}              # every sticker still on screen


async def _delete_sticker_now(chat_id: int, message_id: int) -> bool:
    """Delete one bot message. Normal client first (already connected = fast), Bot API as backup, 3 tries."""
    for _ in range(3):
        try:
            await bot.delete_messages(int(chat_id), int(message_id))
            return True
        except FloodWait as e:
            await asyncio.sleep(min(float(getattr(e, "value", 1) or 1), 5.0))
        except Exception:
            pass
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
                async with session.post(url, json={"chat_id": int(chat_id), "message_id": int(message_id)}) as response:
                    result = await response.json(content_type=None)
                    if result.get("ok") or "not found" in str(result.get("description", "")).lower():
                        return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    logger.warning("Could not delete sticker message %s in chat %s", message_id, chat_id)
    return False


def _track_sticker(chat_id: int, message_id: int, due: Optional[float] = None, job: Optional[Dict[str, Any]] = None) -> None:
    tracked_job = job if (job is not None and JOBS.get(int(chat_id)) is job) else None
    STICKER_LIVE[(int(chat_id), int(message_id))] = {"chat": int(chat_id), "due": due, "job": tracked_job, "born": time.time()}


async def sticker_janitor() -> None:
    """Safety net: deletes timed stickers on time, job stickers when their job is gone, and anything older than 30 min."""
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        for key, entry in list(STICKER_LIVE.items()):
            job = entry.get("job")
            expired = (entry["due"] is not None and now >= entry["due"]) \
                or (job is not None and JOBS.get(entry["chat"]) is not job) \
                or (now - entry["born"] > 1800)
            if expired:
                STICKER_LIVE.pop(key, None)
                await _delete_sticker_now(*key)


async def _http_send_sticker(chat_id: int, sticker_id: str, event: str) -> int:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendSticker"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(url, json={"chat_id": int(chat_id), "sticker": sticker_id}) as response:
                result = await response.json(content_type=None)
                if response.status != 200 or not result.get("ok"):
                    raise RuntimeError(str(result.get("description") or f"HTTP {response.status}"))
                return int((result.get("result") or {}).get("message_id") or 0)
    except Exception as exc:
        STICKER_SEND_ERRORS[event] = safe_sticker_error(exc)
        return 0


async def _deliver_event_sticker(client: Client, chat_id: int, event: str, job: Optional[Dict[str, Any]],
                                 candidates: list) -> None:
    for sticker_id in candidates[:4]:
        message_id = 0
        try:     # the bot's own open connection: no new TLS handshake, no download - it only sends the saved file_id
            sent = await client.send_sticker(int(chat_id), sticker_id)
            message_id = int(getattr(sent, "id", 0) or 0)
        except FloodWait as e:
            await asyncio.sleep(min(float(getattr(e, "value", 1) or 1), 5.0))
        except Exception as exc:
            STICKER_SEND_ERRORS[event] = safe_sticker_error(exc)
        if not message_id:
            message_id = await _http_send_sticker(chat_id, sticker_id, event)
        if not message_id:
            continue
        STICKER_SEND_ERRORS.pop(event, None)
        if event in STICKER_TIMED:
            ttl = sticker_setting(event)["ttl"]
            if ttl > 0:
                _track_sticker(chat_id, message_id, due=time.time() + ttl)
        else:
            _track_sticker(chat_id, message_id, job=job)
            if job is not None:
                job.setdefault("sticker_messages", {}).setdefault(event, []).append(message_id)
        return
    logger.warning("Sticker for event %s could not be sent: %s", event, STICKER_SEND_ERRORS.get(event))


async def send_event_sticker(client: Client, chat_id: int, event: str,
                             job: Optional[Dict[str, Any]] = None) -> bool:
    """Send the step sticker and WAIT until it is on screen (max STICKER_WAIT s), so the work starts after it."""
    if event not in STICKER_EVENT_EMOJIS or not sticker_enabled(event):
        return False
    sent_events = job.setdefault("sticker_events", set()) if job is not None else set()
    if event in sent_events:
        return False
    key = (int(chat_id), event)
    now = time.monotonic()
    if event not in ("welcome", "menu") and now - _STICKER_LAST_SENT.get(key, 0.0) < STICKER_MIN_INTERVAL:
        return False
    candidates = saved_stickers_for(event) if store_has_stickers() else list(dict.fromkeys(STICKER_CANDIDATES.get(event, [])))
    if not candidates:
        STICKER_SEND_ERRORS[event] = "No sticker saved for this step - send /stickers"
        return False
    sent_events.add(event)
    _STICKER_LAST_SENT[key] = now
    task = asyncio.ensure_future(_deliver_event_sticker(client, chat_id, event, job, candidates))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    if job is not None:
        job.setdefault("sticker_tasks", {})[event] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), STICKER_WAIT)
    except Exception:
        pass
    return True


def sticker_available(event: str) -> bool:
    if not sticker_enabled(event):
        return False
    return bool(saved_stickers_for(event)) if store_has_stickers() else bool(STICKER_CANDIDATES.get(event))


async def staged_send(client: Client, chat_id: int, event: str, text: str, markup=None,
                      temp_text: str = "⏳ System starting..."):
    """Temp message -> sticker arrives -> temp message removed -> real message. The sticker removes itself on its timer."""
    temp = None
    if sticker_available(event):
        try:
            temp = await client.send_message(chat_id, temp_text)
        except Exception:
            temp = None
        await send_event_sticker(client, chat_id, event, {"sticker_events": set(), "sticker_messages": {}})
        if temp is not None:
            try:
                await temp.delete()
            except Exception:
                pass
    return await client.send_message(chat_id, text, reply_markup=markup)


async def cleanup_event_sticker(chat_id: int, job: Optional[Dict[str, Any]], event: str) -> None:
    if not job or event in STICKER_TIMED:          # timed stickers delete themselves
        return
    task = (job.get("sticker_tasks") or {}).pop(event, None)
    if task is not None:                           # a sticker still being sent is waited for, then deleted
        try:
            await asyncio.wait_for(asyncio.shield(task), 30)
        except Exception:
            pass
    messages = job.get("sticker_messages") or {}
    ids = list(messages.pop(event, []) or [])
    if not messages:
        job.pop("sticker_messages", None)
    for message_id in ids:
        STICKER_LIVE.pop((int(chat_id), int(message_id)), None)
    if ids:
        await asyncio.gather(*(_delete_sticker_now(chat_id, m) for m in ids))

async def cleanup_job_stage_stickers(chat_id: int, job: Optional[Dict[str, Any]]) -> None:
    for event in ("received", "download", "encoding", "quality", "upload", "extract", "sync"):
        await cleanup_event_sticker(chat_id, job, event)

async def delete_status_message(message: Optional[Message]) -> bool:
    """Remove a progress/status message only after the associated Telegram transfer completed."""
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except Exception as exc:
        logger.debug("Completed status message could not be deleted: %s", exc)
        return False

CANCEL_KB = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]])

async def progress_bar(current: int, total: int, status_text: str, message: Message, start_time: float,
                       client: Optional[Client] = None, job: Optional[Dict[str, Any]] = None) -> None:
    if job is not None and job.get("cancelled"):          # Cancel pressed -> stop the transfer
        if client is not None:
            try:
                client.stop_transmission()
            except Exception:
                pass
        return
    now = time.time()
    diff = now - start_time
    if diff <= 0:
        return

    if not total:
        return
    percentage = (current / total) * 100
    speed = current / diff
    eta = round((total - current) / speed) if speed > 0 else 0
    if job is not None:
        job.update(stage=status_text, progress=max(0.0, min(100.0, percentage)),
                   current_bytes=int(current), total_bytes=int(total), speed_bytes_s=speed,
                   eta_seconds=max(0, int(eta)), elapsed=max(0.0, diff), updated_at=now)

    if getattr(message, "_last_update_time", 0) and (now - message._last_update_time) < 4:
        return
    message._last_update_time = now

    filled_blocks = math.floor(percentage / 10)
    bar = "█" * filled_blocks + "░" * (10 - filled_blocks)

    text = (
        f"📤 **{status_text}**\n\n"
        f"{bar}  **{percentage:.1f}%**\n\n"
        f"🚀 **Speed:** {humanbytes(speed)}/s\n"
        f"📦 **Done:** {humanbytes(current)} / {humanbytes(total)}\n"
        f"⏱️ **Time left:** {time_formatter(eta)}\n"
    )

    try:
        await message.edit_text(text, reply_markup=CANCEL_KB if job is not None else None)
    except RPCError:
        pass

# ------------------------------------------------------------------------------
# 4. SINHALA UNICODE NORMALIZER & ASS CONVERTER ENGINE
# ------------------------------------------------------------------------------
def normalize_sinhala_text(text: str) -> str:
    """Normalizes Sinhala Unicode characters to prevent combining mark displacements."""
    text = unicodedata.normalize('NFC', text)
    return text

def html_color_to_ass(hex_color: str) -> str:
    hex_color = hex_color.lstrip('#').strip()
    if len(hex_color) == 3:
        hex_color = "".join([c * 2 for c in hex_color])
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H00{b}{g}{r}&".upper()
    return "&H00FFFFFF&"

def convert_html_tags_to_ass(text: str) -> str:
    def replace_font(match):
        color = match.group(1)
        body = match.group(2)
        ass_color = html_color_to_ass(color)
        return f"{{\\c{ass_color}}}{body}{{\\c&H00FFFFFF&}}"

    pattern = re.compile(r'<font\s+color=["\']?([^"\'>]+)["\']?>(.*?)</font>', re.IGNORECASE | re.DOTALL)
    text = pattern.sub(replace_font, text)
    text = re.sub(r'</?(?:b|i|u|s|font)[^>]*>', '', text, flags=re.IGNORECASE)
    return text

def sanitize_and_reindex_srt(input_path: Path, output_path: Path) -> dict:
    content = ""
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            with open(input_path, "r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue

    if not content:
        with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())

    cleaned_blocks = []
    index = 1
    total_removed = 0

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_idx = -1
        for i, line in enumerate(lines):
            if "-->" in line:
                time_idx = i
                break

        if time_idx != -1:
            timestamp = lines[time_idx]
            text_lines = lines[time_idx + 1:]
            text_content = "\n".join(text_lines).strip()

            if not text_content:
                total_removed += 1
                continue

            cleaned_blocks.append(f"{index}\n{timestamp}\n{text_content}")
            index += 1

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(cleaned_blocks) + "\n")

    return {"total_subs": index - 1, "removed_empty": total_removed}

def ass_colour(hex_color: str, alpha: str = "20") -> str:
    h = hex_color.lstrip("#").strip()
    if len(h) != 6:
        h = "87CEFA"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()

def srt_time_to_ms(t: str) -> int:
    t = t.strip().replace(",", ".")
    h, m, rest = t.split(":")
    if "." in rest:
        sec, frac = rest.split(".")
        ms = int((frac + "000")[:3])
    else:
        sec, ms = rest, 0
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + ms

def ms_to_ass_time(ms: int) -> str:
    cs = int(round(ms / 10))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def default_style_line(family: str, style: Optional[Dict[str, Any]] = None) -> str:
    # Use the style serializer already shipped in the uploaded video_engine.py.
    return ve.default_style_line(family, style)

def convert_srt_to_ass(
    srt_path: Path,
    ass_path: Path,
    watermark_text: Optional[str] = None,
    offset_ms: int = 0,
    clip_len_ms: Optional[int] = None,
    shift_ms: int = 0,
    style: Optional[Dict[str, Any]] = None,
):
    """
    SRT -> ASS with flawless Sinhala complex shaping and colour retention.
    offset_ms  : the clip start. ffmpeg's input seeking (-ss) makes the clip start at 0:00,
                 so every subtitle has to be shifted back by the same amount to stay in sync.
    clip_len_ms: cues starting after the clip end are dropped.
    """
    with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    content = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())

    wm_col = ass_colour(WATERMARK_COLOR)

    ass_header = (
        "[Script Info]\n"
        "මාතෘකාව: Sinhala Hardcoded Subtitles\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.601\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + default_style_line(ve.style_font_family(style), style) + "\n"
        f"Style: WatermarkStyle,{WM_FONT_FAMILY},{WM_FONT_SIZE},{wm_col},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,1.5,0.8,{WATERMARK_ALIGN},{WATERMARK_MARGIN},{WATERMARK_MARGIN},{WATERMARK_MARGIN},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogues = []

    wm_text = clean_wm_text(watermark_text or "")
    if wm_text:
        dialogues.append(f"Dialogue: 0,0:00:00.00,9:59:59.99,WatermarkStyle,,0,0,0,,{wm_text}")

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        time_line_idx = -1
        for idx, l in enumerate(lines):
            if "-->" in l:
                time_line_idx = idx
                break

        if time_line_idx == -1 or len(lines) <= time_line_idx + 1:
            continue

        try:
            left, right = lines[time_line_idx].split("-->")
            start_ms = srt_time_to_ms(left.strip().split()[0]) + shift_ms - offset_ms
            end_ms = srt_time_to_ms(right.strip().split()[0]) + shift_ms - offset_ms
        except Exception:
            continue

        if end_ms <= 0:
            continue
        if clip_len_ms is not None and start_ms >= clip_len_ms:
            continue
        start_ms = max(start_ms, 0)

        raw_text = "\n".join(lines[time_line_idx + 1:])
        norm_text = normalize_sinhala_text(raw_text)
        formatted_text = convert_html_tags_to_ass(norm_text)
        formatted_text = formatted_text.replace("\n", "\\N")
        # ASS override tags typed inside the SRT (animations, colours...) pass straight through;
        # \fnROUND / \fnSINH are shortcuts for the watermark (rounded) and subtitle fonts
        formatted_text = formatted_text.replace("\\fnROUND", f"\\fn{WM_FONT_FAMILY}")
        formatted_text = formatted_text.replace("\\fnSINH", f"\\fn{ve.style_font_family(style)}")

        dialogues.append(
            f"Dialogue: 0,{ms_to_ass_time(start_ms)},{ms_to_ass_time(end_ms)},Default,,0,0,0,,{formatted_text}"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(dialogues) + "\n")

# ------------------------------------------------------------------------------
# 5. VIDEO ANALYSIS (ffprobe) & THUMBNAIL
# ------------------------------------------------------------------------------
def _to_int(v: Any) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0

def _to_fps(v: str) -> float:
    try:
        if "/" in v:
            a, b = v.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(v)
    except Exception:
        return 0.0

def _stream_duration(st: Dict[str, Any]) -> float:
    try:
        return float(st.get("duration"))
    except (TypeError, ValueError):
        pass
    d = str((st.get("tags") or {}).get("DURATION", ""))
    m_ = re.match(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", d)
    return int(m_.group(1)) * 3600 + int(m_.group(2)) * 60 + float(m_.group(3)) if m_ else 0.0

async def probe_video(video_path: Path) -> Dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(video_path)
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    stdout, _ = await process.communicate()

    info: Dict[str, Any] = {
        "width": 1280, "height": 720, "duration": 0.0, "fps": 0.0,
        "v_codec": "", "v_bitrate": 0, "pix_fmt": "", "audio": [], "subs": [], "has_audio": False,
        "a_codec": "", "a_bitrate": 0, "a_rate": 0, "a_channels": 0,
        "size": 0, "fmt_bitrate": 0,
    }
    try:
        data = json.loads(stdout.decode("utf-8"))
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration") or 0)
        info["size"] = _to_int(fmt.get("size"))
        info["fmt_bitrate"] = _to_int(fmt.get("bit_rate"))

        got_v = got_a = False
        av_dur = max([_stream_duration(x) for x in data.get("streams", []) if x.get("codec_type") in ("video", "audio")] or [0.0])
        if av_dur > 0 and info["duration"] > av_dur * 1.05 + 1:
            info["duration"] = av_dur             # a subtitle track longer than the movie must not stretch the timeline
        for st in data.get("streams", []):
            if st.get("codec_type") == "video" and not got_v:
                # skip embedded cover-art "videos"
                if st.get("disposition", {}).get("attached_pic"):
                    continue
                got_v = True
                info["width"] = _to_int(st.get("width")) or 1280
                info["height"] = _to_int(st.get("height")) or 720
                info["v_codec"] = st.get("codec_name", "")
                info["pix_fmt"] = st.get("pix_fmt", "")
                info["v_bitrate"] = _to_int(st.get("bit_rate"))
                info["fps"] = _to_fps(st.get("avg_frame_rate", "0")) or _to_fps(st.get("r_frame_rate", "0"))
                if not info["duration"]:
                    info["duration"] = float(st.get("duration") or 0)
            elif st.get("codec_type") == "audio":
                tl = {str(k).lower(): v for k, v in (st.get("tags") or {}).items()}
                track = {
                    "codec": st.get("codec_name", ""),
                    "bitrate": _to_int(st.get("bit_rate")) or _to_int(tl.get("bps")) or _to_int(tl.get("bps-eng")),
                    "rate": _to_int(st.get("sample_rate")),
                    "channels": _to_int(st.get("channels")),
                    "lang": str(tl.get("language", "")).lower(),
                    "title": str(tl.get("title", "")).strip(),
                }
                info["audio"].append(track)
                if not got_a:
                    got_a = True
                    info["has_audio"] = True
                    info["a_codec"] = track["codec"]
                    info["a_bitrate"] = track["bitrate"]
                    info["a_rate"] = track["rate"]
                    info["a_channels"] = track["channels"]
            elif st.get("codec_type") == "subtitle":
                tl = {str(k).lower(): v for k, v in (st.get("tags") or {}).items()}
                disp = st.get("disposition") or {}
                info["subs"].append({
                    "codec": st.get("codec_name", ""), "lang": str(tl.get("language", "")).lower(),
                    "title": str(tl.get("title", "")).strip(),
                    "forced": bool(disp.get("forced")), "default": bool(disp.get("default")),
                    "sdh": bool(disp.get("hearing_impaired")),
                })
    except Exception as e:
        logger.error(f"Error reading video metadata: {e}")

    return info

async def get_video_metadata(video_path: Path) -> Tuple[int, int, int]:
    info = await probe_video(video_path)
    return info["width"], info["height"], int(info["duration"])

async def generate_thumbnail(video_path: Path, thumb_path: Path, duration_secs: float = 0) -> bool:
    """Thumbnail from the MIDDLE of the video."""
    mid = max(int(duration_secs // 2), 1)
    if thumb_path.exists():
        thumb_path.unlink()
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", str(mid), "-i", str(video_path),
        "-vframes", "1", "-vf", "scale=320:-2", "-q:v", "4",
        str(thumb_path)
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await process.wait()
    return thumb_path.exists() and thumb_path.stat().st_size > 0

# ------------------------------------------------------------------------------
# 6. QUALITY-PRESERVING ENCODE PLAN + FFMPEG ENGINE
# ------------------------------------------------------------------------------
def fmt_hms(secs: float) -> str:
    secs = int(max(secs, 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_fps(fps: float) -> str:
    return f"{fps:.3f}".rstrip("0").rstrip(".") if fps else "?"

def source_video_bitrate(info: Dict[str, Any]) -> int:
    """Bitrate of the original video stream (bits/s). Used as the size ceiling."""
    if info["v_bitrate"] > 0:
        return info["v_bitrate"]
    a_total = sum((t["bitrate"] or 128_000) for t in info["audio"])
    if info["fmt_bitrate"] > 0:
        return max(info["fmt_bitrate"] - a_total, 300_000)
    if info["duration"] > 0 and info["size"] > 0:
        return max(int(info["size"] * 8 / info["duration"]) - a_total, 300_000)
    return 4_000_000

SUPPORTED_EXT = (".mkv", ".mp4", ".mov", ".m4v")
# audio codecs that can be copied bit-for-bit into each container (mkv accepts everything)
COPY_OK = {
    ".mp4": {"aac", "mp3", "ac3", "eac3"},
    ".m4v": {"aac", "mp3", "ac3", "eac3"},
    ".mov": {"aac", "mp3", "ac3", "eac3", "alac"},
}
LANG_NAMES = {
    "hin": "HINDI", "tel": "TELUGU", "tam": "TAMIL", "mal": "MALAYALAM", "kan": "KANNADA",
    "eng": "ENGLISH", "sin": "SINHALA", "jpn": "JAPANESE", "kor": "KOREAN", "urd": "URDU",
    "ben": "BENGALI", "mar": "MARATHI", "pan": "PUNJABI", "guj": "GUJARATI", "fra": "FRENCH",
    "fre": "FRENCH", "spa": "SPANISH", "deu": "GERMAN", "ger": "GERMAN", "rus": "RUSSIAN",
    "zho": "CHINESE", "chi": "CHINESE", "ara": "ARABIC", "tha": "THAI", "ita": "ITALIAN",
    "por": "PORTUGUESE", "tur": "TURKISH",
}
AUDIO_CODEC_LABEL = {
    "ac3": "DD", "eac3": "DDP", "aac": "AAC", "dts": "DTS", "truehd": "TRUEHD",
    "mp3": "MP3", "opus": "OPUS", "flac": "FLAC", "vorbis": "VORBIS",
}
CHANNEL_LABEL = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}

def audio_track_label(track: Dict[str, Any], codec: str, channels: int, bitrate: int, keep_title: bool) -> str:
    title = track["title"] if keep_title else ""
    # A track title that already describes the track (e.g. "ORG HINDI GTV DDP5.1 384KBPS") is used as it is
    if title and re.search(r"\d", title):
        return title
    name = title.upper() if title else LANG_NAMES.get(track["lang"][:3], "")
    codec_txt = AUDIO_CODEC_LABEL.get(codec, codec.upper())
    ch_txt = CHANNEL_LABEL.get(channels, f"{channels}.0" if channels else "")
    parts = [name, f"{codec_txt}{ch_txt}".strip()]
    if bitrate > 0:
        parts.append(f"{round(bitrate / 1000)}KBPS")
    return " ".join(p for p in parts if p)

def choose_container(orig_name: str) -> str:
    ext = Path(orig_name).suffix.lower()
    return ext if ext in SUPPORTED_EXT else ".mkv"

def preset_for(speed: str, hevc: bool) -> str:
    if hevc:
        return {"fast": ENCODE_PRESET_HEVC_FAST, "balanced": ENCODE_PRESET_HEVC_BAL}.get(speed, ENCODE_PRESET_HEVC)
    return {"fast": ENCODE_PRESET_FAST, "balanced": ENCODE_PRESET_BAL}.get(speed, ENCODE_PRESET)

def build_plan(info: Dict[str, Any], ext: str, speed: str = DEFAULT_SPEED, keep_audio=None, keep_subs=None,
               demo: bool = False) -> Dict[str, Any]:
    """Everything needed to keep the original quality: codec, bitrate cap and the CHOSEN audio / subtitle tracks."""
    speed = "fast" if demo else (speed if speed in ("fast", "balanced", "best") else "fast")
    v_br = source_video_bitrate(info)
    want_hevc = (ENCODE_CODEC == "hevc") or (ENCODE_CODEC == "auto" and info["v_codec"] == "hevc")
    ten_bit = want_hevc and "10" in info["pix_fmt"]
    preset = preset_for(speed, want_hevc)

    if want_hevc:
        vargs = [
            "-c:v", "libx265", "-preset", preset, "-crf", ENCODE_CRF_HEVC,
            "-maxrate", str(v_br), "-bufsize", str(v_br * 2),
            "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p",
            "-x265-params", "log-level=error",
        ]
        if ten_bit:
            vargs += ["-profile:v", "main10"]
        if ext in (".mp4", ".m4v", ".mov"):
            vargs += ["-tag:v", "hvc1"]
        vlabel = "HEVC"
    else:
        vargs = [
            "-c:v", "libx264", "-preset", preset, "-crf", ENCODE_CRF,
            "-maxrate", str(v_br), "-bufsize", str(v_br * 2),
            "-pix_fmt", "yuv420p",
        ]
        vlabel = "H.264"

    n_aud = len(info["audio"])
    if keep_audio is None:
        audio_idx = [0] if (demo and n_aud) else list(range(n_aud))
    else:
        audio_idx = [i for i in sorted(keep_audio) if 0 <= i < n_aud]

    aargs = ["-c:a", "copy"]
    labels, a_total, copied = [], 0, 0
    for k, i in enumerate(audio_idx):
        t = info["audio"][i]
        can_copy = (t["codec"] in ("aac", "mp3")) if demo else (ext not in COPY_OK or t["codec"] in COPY_OK[ext])
        if can_copy:
            codec, ch, br = t["codec"], t["channels"], t["bitrate"]
            copied += 1
        else:
            ch = min(t["channels"] or 2, 2) if demo else t["channels"]
            src = t["bitrate"] or (192_000 if (ch or 2) <= 2 else 384_000)
            br, codec = min(max(src, 128_000), 192_000 if demo else 640_000), "aac"
            aargs += [f"-c:a:{k}", "aac", f"-b:a:{k}", str(br)]
            if t["rate"]:
                aargs += [f"-ar:a:{k}", str(t["rate"])]
            if ch:
                aargs += [f"-ac:a:{k}", str(ch)]
        a_total += br or (192_000 if can_copy else br)
        labels.append(audio_track_label(t, codec, ch, br, keep_title=can_copy))
    if not audio_idx:
        aargs = ["-an"]

    sub_idx = []
    if not demo and keep_subs:
        for i in sorted(keep_subs):
            if not (0 <= i < len(info["subs"])):
                continue
            if ext != ".mkv" and info["subs"][i]["codec"] not in TEXT_SUB_CODECS:
                continue                      # image subtitles cannot live in mp4/mov
            sub_idx.append(i)

    return {
        "vargs": vargs, "aargs": aargs, "vlabel": vlabel, "v_br": v_br,
        "a_total": a_total, "audio_labels": labels, "audio_copied": copied,
        "audio_idx": audio_idx, "sub_idx": sub_idx, "sub_codec": "copy" if ext == ".mkv" else "mov_text",
        "ext": ext, "faststart": ext in (".mp4", ".m4v", ".mov"), "speed": speed,
    }

def make_title(filename: str) -> str:
    """'Movie.Name.2026.720p.WEB-DL.mkv' -> 'Movie Name 2026 720p WEB-DL' (5.1 / H.264 kept intact)."""
    stem = Path(filename).stem
    stem = re.sub(r"(?i)\b([hx])\.(26[45])\b", "\\1\x00\\2", stem)
    # a dot is kept only for short numbers like 5.1 / 2.0 (so "2026.720p" still becomes "2026 720p")
    stem = re.sub(r"\.(?!(?<=\d\.)\d{1,2}(?!\d))", " ", stem)
    stem = stem.replace("_", " ").replace("\x00", ".")
    return re.sub(r"\s+", " ", stem).strip()

def build_caption(orig_name: str, height: int, vlabel: str, clip_secs: float, audio_labels: list) -> str:
    lines = [
        f"{make_title(orig_name)[:400]} සිංහල උපසිරසි සමග",
        f"🎬 {height}p {vlabel} ⏳ {fmt_hms(round(clip_secs))}",
    ]
    if audio_labels:
        lines.append("🔊 " + " , ".join(audio_labels))
    lines.append("💬 Subtitle - සිංහල උපසිරැසි සහිතව.")
    return "\n".join(lines)[:1024]

async def update_encode_progress(
    message: Message, current_secs: float, total_secs: float,
    start_time: float, quality_line: str
) -> None:
    now = time.time()
    if getattr(message, "_last_update_time", 0) and (now - message._last_update_time) < 4:
        return
    message._last_update_time = now

    elapsed = max(now - start_time, 0.001)
    speed = current_secs / elapsed
    if total_secs > 0:
        pct = min(current_secs / total_secs * 100, 100)
        eta = round((total_secs - current_secs) / speed) if speed > 0 else 0
        done_line = f"🎞 **Done:** {time_formatter(current_secs)} / {time_formatter(total_secs)}"
        eta_line = f"⏱️ **ETA:** {time_formatter(eta)}"
    else:
        pct = 0
        done_line = f"🎞 **Done:** {time_formatter(current_secs)}"
        eta_line = "⏱️ **ETA:** ..."

    filled = math.floor(pct / 10)
    bar = "█" * filled + "░" * (10 - filled)

    text = (
        "🎬 **Encoding with subtitles...**\n\n"
        f"[{bar}] `{pct:.1f}%`\n\n"
        f"{done_line}\n"
        f"🚀 **Speed:** {speed:.2f}x\n"
        f"{eta_line}\n"
        f"🕒 **Elapsed:** {time_formatter(elapsed)}\n\n"
        f"{quality_line}"
    )
    await safe_edit(message, text, cancel=True)

class _VideoEngineUI:
    async def status(self, message, text, cancel=False, **kwargs):
        await safe_edit(message, text, cancel=cancel, **kwargs)

    async def progress(self, message, current_secs, total_secs, start_time, quality_line, extra=None):
        await update_encode_progress(message, current_secs, total_secs, start_time, quality_line)

    async def simple(self, message, title, current, total, start):
        await update_encode_progress(message, current, total, start, title)

ve.UI = _VideoEngineUI()

async def safe_edit(message: Message, text: str, cancel: bool = False, **kwargs) -> None:
    try:
        user_id = int(message.chat.id)
        job = JOBS.get(user_id)
        if job:
            plain = re.sub(r"<[^>]*>|[`*_]", "", str(text or ""))
            plain = re.sub(r"\s+", " ", plain).strip()
            job["status_text"] = plain[:240]
            job["stage"] = (plain.split("\n", 1)[0] if plain else job.get("stage", "processing"))[:100]
            job["updated_at"] = time.time()
            pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", plain)
            if pct:
                job["progress"] = max(0.0, min(100.0, float(pct.group(1))))
    except Exception:
        pass
    if cancel:
        kwargs.setdefault("reply_markup", CANCEL_KB)
    try:
        await message.edit_text(text, **kwargs)
    except RPCError:
        pass  # MessageNotModified / FloodWait etc. must never break the job

async def cut_tracks_copy(src: Path, dst: Path, audio_idx: list, sub_idx: list, start_secs: float,
                          clip_secs: float, use_t: bool, job: Dict[str, Any]) -> bool:
    """Bit-exact copy of the chosen audio / subtitle tracks for [start, start+clip] (output-side seek = packet accurate)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src.resolve())]
    for i in audio_idx:
        cmd += ["-map", f"0:a:{i}"]
    for i in sub_idx:
        cmd += ["-map", f"0:s:{i}"]
    cmd += ["-vn", "-dn", "-map_chapters", "-1", "-c", "copy", "-ss", f"{start_secs:.3f}"]
    if use_t:
        cmd += ["-t", f"{clip_secs:.3f}"]
    cmd.append(str(dst.resolve()))
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    job["proc"] = process
    try:
        await process.wait()
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        job["proc"] = None
    return (not job.get("cancelled")) and process.returncode == 0 and dst.exists() and dst.stat().st_size > 0

async def hardcode_video_ffmpeg(
    video_path: Path,
    srt_path: Path,
    output_path: Path,
    info: Dict[str, Any],
    plan: Dict[str, Any],
    start_secs: float,
    clip_secs: float,
    use_t: bool,
    wm_text: Optional[str],
    status_msg: Message,
    job: Dict[str, Any],
    delay_ms: int = 0,
    style: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    work_dir = video_path.parent
    ass_path = work_dir / f"sub_render_{time.time_ns()}.ass"

    convert_srt_to_ass(
        srt_path, ass_path,
        watermark_text=wm_text,
        offset_ms=int(start_secs * 1000),
        clip_len_ms=int(clip_secs * 1000) if clip_secs > 0 else None,
        shift_ms=int(delay_ms),
        style=style,
    )

    abs_output_path = output_path.resolve()
    ass_path_esc = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts_dir_esc = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
    vf_filter = f"ass='{ass_path_esc}':fontsdir='{fonts_dir_esc}':shaping=complex"

    # ---- keep the original quality: same resolution / fps / codec family, bitrate capped to the source ----
    v_br = plan["v_br"]

    # Input seeking (-ss) + stream-copied audio makes ffmpeg start the audio at the previous KEYFRAME while the
    # video starts exactly at -ss, so subtitles drift. For trimmed clips the audio is therefore cut first
    # (still a bit-exact copy) and used as a second input; the video then starts exactly at 0 like the audio.
    audio_cut = None
    if start_secs > 0 and (plan["audio_idx"] or plan["sub_idx"]):
        audio_cut = work_dir / f"audiocut_{time.time_ns()}.mkv"
        await safe_edit(status_msg, "🎧 Cutting audio / subtitle tracks...", cancel=True)
        ok_cut = await cut_tracks_copy(video_path, audio_cut, plan["audio_idx"], plan["sub_idx"],
                                       start_secs, clip_secs, use_t, job)
        if not ok_cut:
            _unlink(audio_cut)
            _unlink(ass_path)
            return False, ("cancelled" if job.get("cancelled") else "ffmpeg_error")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "warning",
           "-nostats", "-progress", "pipe:1"]
    if start_secs > 0:
        cmd += ["-ss", f"{start_secs:.3f}"]
    if use_t:
        cmd += ["-t", f"{clip_secs:.3f}"]
    cmd += ["-i", str(video_path.resolve())]
    if audio_cut:
        cmd += ["-i", str(audio_cut.resolve())]
    maps = ["-map", "0:v:0"]
    for k, i in enumerate(plan["audio_idx"]):
        maps += ["-map", f"1:a:{k}" if audio_cut else f"0:a:{i}"]
    for k, i in enumerate(plan["sub_idx"]):
        maps += ["-map", f"1:s:{k}" if audio_cut else f"0:s:{i}"]
    cmd += maps + ["-dn", "-map_chapters", "-1", "-vf", vf_filter, *plan["vargs"], *plan["aargs"]]
    if plan["sub_idx"]:
        cmd += ["-c:s", plan["sub_codec"]]
    if plan["faststart"]:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(abs_output_path)]

    logger.info(f"Running FFmpeg Command: {' '.join(cmd)}")

    n_audio = len(plan["audio_labels"])
    if n_audio:
        a_desc = f"{n_audio} audio track(s) • {plan['audio_copied']} original (copy)"
    else:
        a_desc = "No audio"
    quality_line = (
        f"📐 {info['width']}×{info['height']} • {fmt_fps(info['fps'])} fps • {plan['vlabel']} • "
        f"{v_br / 1_000_000:.2f} Mbps max\n🔊 {a_desc}\n"
        f"{'⚡ Fast mode' if plan['speed'] == 'fast' else '💎 Best quality mode'}"
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    job["proc"] = process

    state = {"last_progress": time.time(), "stalled": False}
    stderr_tail = {"text": ""}

    async def drain_stderr():
        # fixed-size reads: can never hit asyncio's line-length limit
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            stderr_tail["text"] = (stderr_tail["text"] + chunk.decode("utf-8", errors="ignore"))[-6000:]

    async def watchdog():
        while process.returncode is None:
            await asyncio.sleep(30)
            if time.time() - state["last_progress"] > STALL_TIMEOUT:
                state["stalled"] = True
                logger.error("FFmpeg stalled - killing process")
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return

    stderr_task = asyncio.create_task(drain_stderr())
    watchdog_task = asyncio.create_task(watchdog())

    start_proc_time = time.time()
    status_msg._last_update_time = 0
    await update_encode_progress(status_msg, 0, clip_secs, start_proc_time, quality_line)

    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="ignore").strip()
            if line_str.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    current_secs = int(line_str.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                state["last_progress"] = time.time()
                if current_secs >= 0:
                    await update_encode_progress(status_msg, current_secs, clip_secs, start_proc_time, quality_line)
        await process.wait()
        await stderr_task
    finally:
        watchdog_task.cancel()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if ass_path.exists():
            ass_path.unlink()
        _unlink(audio_cut)
        job["proc"] = None

    if job.get("cancelled"):
        return False, "cancelled"
    if state["stalled"]:
        return False, "stalled"
    if process.returncode != 0:
        logger.error("FFmpeg process failed:\n" + stderr_tail["text"][-3000:])
        return False, "ffmpeg_error"

    ok = abs_output_path.exists() and abs_output_path.stat().st_size > 0
    return ok, "ok" if ok else "no_output"

# The bot keeps its Telegram workflows; the new engines own the media/quality pipeline.
probe_video = ve.probe_video
choose_container = ve.choose_container
build_plan = ve.build_plan
hardcode_video_ffmpeg = ve.hardcode_video_ffmpeg
generate_thumbnail = ve.generate_thumbnail

# ------------------------------------------------------------------------------
# ANIMATED INTRO CARD (paste at the very start of an SRT; rendered by libass)
# ------------------------------------------------------------------------------
def _srt_ts(ms: int) -> str:
    h, rem = divmod(int(ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    sec, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{milli:03d}"

def build_intro_srt(brand: str, duration_s: float = 9.0, start_s: float = 0.3) -> str:
    """Five animated cues (title, promo, site, telegram, credit) that build up one after another."""
    brand = clean_wm_text(brand) or "Sadesha.Sub"
    duration_s = min(max(duration_s, 5.0), 30.0)
    end_ms = round((start_s + duration_s) * 1000)

    CY, YE, GR, RD = "&HEBD728&", "&H00EEFF&", "&H1EE146&", "&H2323EB&"      # cyan / yellow / green / red (BGR)
    common = r"\an5\3c&H000000&\4c&H000000&"
    promo1 = "සිංහල උපසිරැසි සහිත චිත්\u200dරපට සහ කතාමාලා"
    promo2 = "ඔන්ලයින් බලන්න, ඩවුන්ලෝඩ් කරන්න, අප හා එක්වන්න"
    credit = "සිංහල උපසිරැසි ගැන්වීම"

    cues = [
        # (start offset s, text)  – each line has its own entrance animation
        (0.0, "{" + common + r"\pos(960,230)\fnROUND\fs80\c" + CY + r"\bord5\shad3\blur0.8\fad(300,500)"
              r"\fscx40\fscy40\t(0,260,\fscx112\fscy112)\t(260,420,\fscx100\fscy100)}"
              "Movie Name (2026) Language WEB"),
        (0.6, "{" + common + r"\move(960,525,960,450,0,500)\q2\fnSINH\fs70\c" + YE + r"\bord4\shad2\blur0.6\fad(400,500)}"
              + promo1 + r"\N" + promo2),
        (1.4, "{" + common + r"\move(560,650,960,650,0,450)\fnROUND\fs68\c" + RD + r"\bord4\shad2\blur0.6\fad(350,500)}"
              "★ {\\c" + CY + "}WWW.YOURSITE.COM {\\c" + RD + "}★"),
        (2.0, "{" + common + r"\move(1360,795,960,795,0,450)\fnROUND\fs60\c" + RD + r"\bord4\shad2\blur0.6\fad(350,500)}"
              "★  {\\c" + GR + "}Official Telegram Channel - @YourChannel  {\\c" + RD + "}★"),
        (2.6, "{" + common + r"\pos(960,935)\fnSINH\fs46\c" + CY + r"\bord3\shad1\blur0.5\fad(600,500)}"
              + credit + " {\\fnROUND\\c" + RD + "}" + brand + "{\\fnSINH\\c" + CY + "} විසින්."),
    ]

    blocks = []
    for i, (off, text) in enumerate(cues, 1):
        st = round((start_s + off) * 1000)
        blocks.append(f"{i}\n{_srt_ts(st)} --> {_srt_ts(end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n"

def srt_first_cue_secs(srt_path: Path) -> Optional[float]:
    try:
        content = open(srt_path, "r", encoding="utf-8", errors="ignore").read()
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->", content)
        return srt_time_to_ms(m.group(1)) / 1000 if m else None
    except Exception:
        return None

def srt_has_intro(srt_path: Path) -> bool:
    try:
        return "\\fnROUND" in open(srt_path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return False

# ------------------------------------------------------------------------------
# FONT TOOLS: /fonttest renders a sample with the real pipeline, /setfont picks one
# ------------------------------------------------------------------------------
FONT_SAMPLE_LINES = [
    "කි කී කු කූ කෘ කෙ කේ කො කෝ කෞ කා කැ කෑ කං",
    "ක්\u200dර ක්\u200dය ශ්\u200dරී ප්\u200dරශ්නය වෛද්\u200dය ක්\u200dෂ",
    "ඔබට සුබ දවසක් වේවා! 1080p",
]

def uploaded_font_files(user_id: int) -> list:
    """Font files uploaded by one Telegram user, identified by a safe on-disk prefix."""
    prefix = f"u{int(user_id)}_"
    return sorted(p for p in FONTS_DIR.iterdir()
                  if p.is_file() and p.name.startswith(prefix) and p.suffix.lower() in (".ttf", ".otf"))

def _font_scan(path: Path) -> Tuple[str, bool]:
    try:
        result = subprocess.run(
            ["fc-scan", "--format", "%{family}|%{lang}", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        family, _, langs = result.stdout.partition("|")
        supported = result.returncode == 0 and "si" in langs.split("|")
        return (family.split(",")[0].strip() or path.stem), supported
    except Exception:
        return path.stem, False

def sinhala_font_files(user_id: Optional[int] = None) -> list:
    """Shared fonts plus this user's own uploaded fonts; hide other users' uploads."""
    found = []
    for p in sorted(FONTS_DIR.iterdir()):
        if p.suffix.lower() not in (".ttf", ".otf") or p.stat().st_size < 10000:
            continue
        if re.match(r"^u\d+_", p.name) and (user_id is None or not p.name.startswith(f"u{int(user_id)}_")):
            continue
        try:
            langs = subprocess.run(
                ["fc-scan", "--format", "%{lang}\n", str(p)],
                capture_output=True, text=True, timeout=20
            ).stdout.strip().split("|")
        except Exception:
            langs = ["si"]
        if "si" in langs:
            found.append(p)
    return found

def resolve_font_choice(arg: str, files: list) -> Optional[Path]:
    arg = (arg or "").strip()
    if arg.isdigit() and 1 <= int(arg) <= len(files):
        return files[int(arg) - 1]
    low = arg.lower()
    for p in files:
        if low and (low in p.name.lower() or low == font_family_of(p, p.stem).lower()):
            return p
    return None

def install_font_file(path: Path) -> None:
    user_fonts = Path.home() / ".fonts"
    user_fonts.mkdir(parents=True, exist_ok=True)
    shutil.copy(path, user_fonts / path.name)
    subprocess.run(["fc-cache", "-f", str(user_fonts)], check=False, capture_output=True)
    subprocess.run(["fc-cache", "-f", str(FONTS_DIR)], check=False, capture_output=True)

async def render_font_sample(family: str, out_png: Path, style: Optional[Dict[str, Any]] = None) -> bool:
    """Render the sample with the exact same ffmpeg + libass (complex shaping) pipeline as the real job."""
    ass_path = out_png.with_suffix(".ass")
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.601\nPlayResX: 1920\nPlayResY: 330\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + default_style_line(family, dict(style or {}, font=family)) + "\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    text = "\\N".join(normalize_sinhala_text(l) for l in FONT_SAMPLE_LINES)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + f"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{text}\n")

    ass_esc = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts_esc = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x2b2b2b:s=1920x330:d=1",
        "-vf", f"ass='{ass_esc}':fontsdir='{fonts_esc}':shaping=complex",
        "-frames:v", "1", str(out_png),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    if ass_path.exists():
        ass_path.unlink()
    return out_png.exists() and out_png.stat().st_size > 0

# -------------------- subtitle appearance and personal fonts --------------------
FONT_MAX_UPLOAD_BYTES = int(os.environ.get("FONT_MAX_UPLOAD_MB", "12")) * 1024 * 1024
FONT_MAX_FILES_PER_USER = int(os.environ.get("FONT_MAX_FILES_PER_USER", "20"))
FONT_DELETE_PENDING: Dict[str, Dict[str, Any]] = {}
STYLE_COLOURS = [("⚪", "FFFFFF"), ("🟡", "FFD400"), ("🔵", "5EE7FF"), ("🟢", "66FF66")]
OUTLINE_COLOURS = [("⚫", "000000"), ("🔷", "00334D"), ("◼️", "1E1E1E")]

def display_font_family(family: str) -> str:
    return re.sub(r"[\r\n`*_\[\]]", "", str(family))[:80]

def style_panel_text(user_id: int) -> str:
    style = ve.get_user_style(user_id)
    font = display_font_family(style.get("font") or SUB_FONT_FAMILY)
    align = {1: "Bottom left", 2: "Bottom center", 3: "Bottom right", 7: "Top left", 8: "Top center", 9: "Top right"}.get(int(style.get("alignment", 2)), "Bottom center")
    return (
        "🎨 **Subtitle Style**\n\n"
        f"🔤 Font: `{font}`\n📏 Size: `{style['size']}`\n🎨 Text colour: `{style['primary']}`\n"
        f"▰ Outline: `{style['outline_c']}` / `{style['outline']}` • Shadow: `{style['shadow']}`\n"
        f"📍 Position: `{align}`\n↔️ Side margin: `{style['margin_h']}` • ↕️ Edge margin: `{style['margin_v']}`\n"
        f"Line spacing: `{style['spacing']}` • Bold: `{'On' if style['bold'] else 'Off'}`\n\n"
        "Use the buttons to change the look, then tap Preview. This does not change video quality."
    )

def style_panel_keyboard() -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    rows = []
    presets = list(ve.STYLE_PRESETS.items())
    for i in range(0, len(presets), 2):
        rows.append([B(name, callback_data=f"style:preset:{i + j}")
                     for j, (name, _) in enumerate(presets[i:i + 2])])
    rows += [
        [B("📏 Size −", callback_data="style:change:size:-1"), B("📏 Size +", callback_data="style:change:size:1")],
        [B("▰ Outline −", callback_data="style:change:outline:-1"), B("▰ Outline +", callback_data="style:change:outline:1")],
        [B("🌑 Shadow −", callback_data="style:change:shadow:-1"), B("🌑 Shadow +", callback_data="style:change:shadow:1")],
        [B("↔️ Side −", callback_data="style:change:margin_h:-1"), B("↔️ Side +", callback_data="style:change:margin_h:1")],
        [B("↕️ Edge −", callback_data="style:change:margin_v:-1"), B("↕️ Edge +", callback_data="style:change:margin_v:1")],
        [B("Spacing −", callback_data="style:change:spacing:-1"), B("Spacing +", callback_data="style:change:spacing:1")],
        [B(f"▰{emoji}", callback_data=f"style:border:{colour}") for emoji, colour in OUTLINE_COLOURS],
        [B(f"{emoji}", callback_data=f"style:color:{colour}") for emoji, colour in STYLE_COLOURS],
        [B("⬆️ Up", callback_data="style:move:up"), B("⬇️ Down", callback_data="style:move:down")],
        [B("🔁 Top / Bottom", callback_data="style:position"), B("🔠 Bold", callback_data="style:bold")],
        [B("👀 Preview", callback_data="style:preview"), B("🔤 Fonts", callback_data="font:manager")],
        [B("↩️ Reset", callback_data="style:reset")],
    ]
    return InlineKeyboardMarkup(rows)

def _font_delete_token(user_id: int, path: Path) -> str:
    now = time.time()
    for old in list(FONT_DELETE_PENDING):
        if FONT_DELETE_PENDING[old].get("expires", 0) <= now:
            FONT_DELETE_PENDING.pop(old, None)
    token = secrets.token_hex(4)
    FONT_DELETE_PENDING[token] = {"user_id": int(user_id), "name": path.name, "expires": now + 600}
    return token

def font_manager_text(user_id: int) -> str:
    own = uploaded_font_files(user_id)
    files = sinhala_font_files(user_id)
    active = display_font_family(ve.get_user_style(user_id).get("font") or SUB_FONT_FAMILY)
    return (
        "🔤 **Font Manager**\n\n"
        f"Current font: `{active}`\nAvailable Sinhala fonts: `{len(files)}`\n"
        f"Your uploaded fonts: `{len(own)}/{FONT_MAX_FILES_PER_USER}`\n\n"
        "To add a font, send a `.ttf` or `.otf` file here. Other users cannot see or remove your fonts."
    )

def font_manager_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📤 Add font", callback_data="font:upload"),
             InlineKeyboardButton("🧪 Preview", callback_data="font:test")]]
    selectable = sinhala_font_files(user_id)
    current = ve.get_user_style(user_id).get("font") or SUB_FONT_FAMILY
    for i, path in enumerate(selectable[:35]):
        family = font_family_of(path, path.stem)
        label = ("✓ " if family == current else "") + family[:20]
        row = [InlineKeyboardButton(label, callback_data=f"font:set:{i}")]
        if path.name.startswith(f"u{int(user_id)}_"):
            row.append(InlineKeyboardButton("🗑", callback_data=f"font:delask:{_font_delete_token(user_id, path)}"))
        rows.append(row)
    for path in uploaded_font_files(user_id):
        if path not in selectable:
            rows.append([InlineKeyboardButton(f"⚠️ {path.name[:18]}", callback_data="font:manager"),
                         InlineKeyboardButton("🗑", callback_data=f"font:delask:{_font_delete_token(user_id, path)}")])
    rows.append([InlineKeyboardButton("🎨 Style", callback_data="style:open"),
                 InlineKeyboardButton("🔄 Refresh", callback_data="font:manager")])
    return InlineKeyboardMarkup(rows)

async def send_style_preview(client: Client, chat_id: int, user_id: int) -> bool:
    path = OUTPUT_DIR / f"style_preview_{user_id}_{time.time_ns()}.jpg"
    lib, sess = lib_get(user_id), USER_DATA.get(user_id) or {}
    video = Path(lib["path"]) if lib and Path(lib["path"]).exists() else None
    srt = Path(sess["srt_path"]) if sess.get("srt_path") and Path(sess["srt_path"]).exists() else None
    at = ve.srt_first_cue_secs(srt) if srt else 0.0
    if at is None:
        at = 0.0
    matrix = ve.matrix_for(lib["info"]) if lib else "TV.709"
    try:
        style = ve.get_user_style(user_id)
        if not style.get("font"):
            style["font"] = SUB_FONT_FAMILY
        ok = await ve.render_style_preview(path, style, video=video,
                                           at_secs=at, srt=srt, delay_ms=int(sess.get("delay_ms", 0)), matrix=matrix)
        if not ok:
            ok = await ve.render_font_sample(ve.style_font_family(style), path, style)
        if not ok:
            return False
        await client.send_photo(chat_id, str(path), caption=sc("🎨 Your subtitle style preview"))
        return True
    finally:
        _unlink(path)

async def style_font_callback(client: Client, callback: CallbackQuery, data: str, user_id: int) -> bool:
    if data == "style:open":
        await safe_edit(callback.message, style_panel_text(user_id), reply_markup=style_panel_keyboard())
        await callback.answer()
        return True
    if data.startswith("style:"):
        if data == "style:preview":
            await callback.answer("Making preview…")
            try:
                if not await send_style_preview(client, callback.message.chat.id, user_id):
                    await client.send_message(callback.message.chat.id, "⚠️ Could not make the preview. Check FFmpeg / libass and the font.")
            except Exception as e:
                logger.warning(f"Subtitle style preview failed: {e}", exc_info=True)
                await client.send_message(callback.message.chat.id, "⚠️ Could not make the preview.")
            return True
        style = ve.get_user_style(user_id)
        if data.startswith("style:preset:"):
            try:
                preset = list(ve.STYLE_PRESETS.values())[int(data.rsplit(":", 1)[1])]
                keep_font = style.get("font")
                style.update(preset)
                style["font"] = keep_font
            except (ValueError, IndexError):
                await callback.answer("Preset not found.", show_alert=True)
                return True
        elif data.startswith("style:change:"):
            _, _, key, direction = data.split(":", 3)
            step = int(direction)
            bounds = {"size": (18, 96, 2), "outline": (0, 8, 0.5), "shadow": (0, 8, 0.5),
                      "spacing": (-2, 8, 0.25), "margin_h": (0, 500, 10), "margin_v": (0, 500, 10)}
            if key in bounds:
                low, high, unit = bounds[key]
                value = float(style.get(key, 0)) + unit * step
                style[key] = int(value) if key in ("size", "margin_h", "margin_v") else round(value, 2 if key == "spacing" else 1)
                style[key] = max(low, min(high, style[key]))
        elif data.startswith("style:color:"):
            color = data.rsplit(":", 1)[1]
            if color in {c for _, c in STYLE_COLOURS}:
                style["primary"] = f"#{color}"
        elif data.startswith("style:border:"):
            color = data.rsplit(":", 1)[1]
            if color in {c for _, c in OUTLINE_COLOURS}:
                style["outline_c"] = f"#{color}"
        elif data.startswith("style:align:"):
            value = int(data.rsplit(":", 1)[1])
            if value in (1, 2, 3, 7, 8, 9):
                style["alignment"] = value
        elif data == "style:bold":
            style["bold"] = not bool(style.get("bold", True))
        elif data == "style:position":
            if int(style.get("alignment", 2)) == 2:
                style.update(alignment=8, margin_v=40)
            else:
                style.update(alignment=2, margin_v=50)
        elif data.startswith("style:move:"):
            bottom_aligned = int(style.get("alignment", 2)) in (1, 2, 3)
            moving_up = data.endswith(":up")
            delta = 10 if (moving_up == bottom_aligned) else -10
            style["margin_v"] = max(0, min(500, int(style.get("margin_v", 50)) + delta))
        elif data == "style:reset":
            keep_font = style.get("font")
            style = dict(ve.STYLE_DEFAULT)
            style["font"] = keep_font
        ve.save_user_style(user_id, style)
        await safe_edit(callback.message, style_panel_text(user_id), reply_markup=style_panel_keyboard())
        await callback.answer("✅ Style saved.")
        return True
    if data == "font:manager":
        await safe_edit(callback.message, font_manager_text(user_id), reply_markup=font_manager_keyboard(user_id))
        await callback.answer()
        return True
    if data == "font:upload":
        await callback.message.reply_text(
            f"📤 Send your font as a `.ttf` or `.otf` file. Max {FONT_MAX_UPLOAD_BYTES // (1024 * 1024)} MB, up to {FONT_MAX_FILES_PER_USER} fonts."
        )
        await callback.answer()
        return True
    if data == "font:test":
        files = sinhala_font_files(user_id)
        if not files:
            await callback.answer("No Sinhala font found.", show_alert=True)
            return True
        await callback.answer("Making font previews…")
        style = ve.get_user_style(user_id)
        for i, path in enumerate(files[:20], 1):
            family = font_family_of(path, path.stem)
            preview = OUTPUT_DIR / f"font_preview_{user_id}_{time.time_ns()}.png"
            try:
                if await render_font_sample(family, preview, dict(style, font=family)):
                    active = " ✓ in use" if family == (style.get("font") or SUB_FONT_FAMILY) else ""
                    await client.send_photo(callback.message.chat.id, str(preview),
                                            caption=f"{i}. {family}{active}", parse_mode=ParseMode.DISABLED)
            except Exception as e:
                logger.warning(f"Font sample failed for {path.name}: {e}")
            finally:
                _unlink(preview)
        return True
    if data.startswith("font:set:"):
        try:
            files = sinhala_font_files(user_id)
            path = files[int(data.rsplit(":", 1)[1])]
        except (ValueError, IndexError):
            await callback.answer("Font list changed. Refresh and choose again.", show_alert=True)
            return True
        style = ve.get_user_style(user_id)
        style["font"] = font_family_of(path, path.stem)
        ve.save_user_style(user_id, style)
        await safe_edit(callback.message, font_manager_text(user_id), reply_markup=font_manager_keyboard(user_id))
        await callback.answer("✅ Font set for you.")
        return True
    if data.startswith("font:delask:"):
        token = data.rsplit(":", 1)[1]
        pending = FONT_DELETE_PENDING.get(token)
        if not pending or pending.get("user_id") != int(user_id) or pending.get("expires", 0) <= time.time():
            await callback.answer("This delete request has expired.", show_alert=True)
            return True
        await safe_edit(callback.message, f"🗑 Delete your font `{pending['name']}`?",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, delete", callback_data=f"font:delete:{token}"),
                                                            InlineKeyboardButton("Cancel", callback_data="font:manager")]]))
        await callback.answer()
        return True
    if data.startswith("font:delete:"):
        token = data.rsplit(":", 1)[1]
        pending = FONT_DELETE_PENDING.get(token)
        if not pending or pending.get("user_id") != int(user_id) or pending.get("expires", 0) <= time.time():
            await callback.answer("This delete request has expired.", show_alert=True)
            return True
        name = pending["name"]
        if not name.startswith(f"u{int(user_id)}_"):
            await callback.answer("This font is not yours.", show_alert=True)
            return True
        path = FONTS_DIR / name
        family = font_family_of(path, path.stem)
        _unlink(path)
        _unlink(Path.home() / ".fonts" / name)
        subprocess.run(["fc-cache", "-f", str(FONTS_DIR)], capture_output=True, check=False)
        subprocess.run(["fc-cache", "-f", str(Path.home() / ".fonts")], capture_output=True, check=False)
        style = ve.get_user_style(user_id)
        if style.get("font") == family:
            style.pop("font", None)
            ve.save_user_style(user_id, style)
        FONT_DELETE_PENDING.pop(token, None)
        await safe_edit(callback.message, font_manager_text(user_id), reply_markup=font_manager_keyboard(user_id))
        await callback.answer("Font deleted.")
        return True
    return False

# ------------------------------------------------------------------------------
# JOBS, CANCEL BUTTONS & SERVER FILE LIBRARY (keep an uploaded video for a while)
# ------------------------------------------------------------------------------
LIBRARY_DIR = BASE_DIR / "library"
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
KEEP_MINUTES = int(os.environ.get("KEEP_MINUTES", "180"))
LIBRARY: Dict[int, Dict[str, Any]] = {}              # one stored video per user
SYNC_WAIT: Dict[int, Dict[str, Any]] = {}            # users in the middle of /sync
SYNC_REPORTS: Dict[str, Dict[str, Any]] = {}         # live / finished sync reports (served by the web server)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/") or (
    f"https://{os.environ['SPACE_HOST']}" if os.environ.get("SPACE_HOST") else ""
)
WEBAPP_CONTROL_PATH = BASE_DIR / "webapp_control.json"
WEBAPP_ADMIN_IDS = {
    int(value.strip()) for value in os.environ.get("WEBAPP_ADMIN_IDS", "").split(",")
    if value.strip().isdigit() and int(value.strip()) > 0
}

def _load_webapp_control() -> Tuple[bool, set]:
    enabled = os.environ.get("WEBAPP_ENABLED", "false").strip().lower() not in ("0", "false", "no", "off")
    chats = set()
    try:
        saved = json.loads(WEBAPP_CONTROL_PATH.read_text(encoding="utf-8"))
        enabled = bool(saved.get("enabled", enabled))
        chats = {int(value) for value in saved.get("menu_chat_ids", []) if int(value) > 0}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return enabled, chats

WEBAPP_ENABLED, WEBAPP_MENU_CHAT_IDS = _load_webapp_control()

def save_webapp_control() -> None:
    try:
        tmp = WEBAPP_CONTROL_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"enabled": WEBAPP_ENABLED,
                                   "menu_chat_ids": sorted(WEBAPP_MENU_CHAT_IDS)}), encoding="utf-8")
        tmp.replace(WEBAPP_CONTROL_PATH)
    except Exception as exc:
        logger.error("Could not save Mini App toggle state: %s", exc)

def register_webapp_chat(chat_id: int) -> None:
    WEBAPP_MENU_CHAT_IDS.add(int(chat_id))
    save_webapp_control()

def current_webapp_menu_button():
    if WEBAPP_ENABLED and PUBLIC_URL.startswith("https://"):
        return MenuButtonWebApp("Open", WebAppInfo(url=f"{PUBLIC_URL}/dashboard"))
    return MenuButtonDefault()

async def sync_webapp_menu_buttons(client: Client) -> Tuple[int, int]:
    """Set the default and all previously known private-chat buttons to the current toggle state."""
    button = current_webapp_menu_button()
    changed = failed = 0
    try:
        await client.set_chat_menu_button(menu_button=button)
        changed += 1
    except Exception as exc:
        failed += 1
        logger.warning("Could not update default Mini App menu button: %s", type(exc).__name__)
    for chat_id in sorted(WEBAPP_MENU_CHAT_IDS):
        try:
            await client.set_chat_menu_button(chat_id=chat_id, menu_button=button)
            changed += 1
        except Exception as exc:
            failed += 1
            logger.warning("Could not update Mini App menu button for chat %s: %s", chat_id, type(exc).__name__)
        await asyncio.sleep(0.05)
    return changed, failed

def dashboard_db_connect():
    DASHBOARD_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DASHBOARD_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
        input_name TEXT, input_size INTEGER DEFAULT 0, output_name TEXT,
        output_size INTEGER DEFAULT 0, elapsed REAL DEFAULT 0,
        error TEXT, retry_spec TEXT DEFAULT '{}'
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_user_started ON jobs(user_id, started_at DESC)")
    return conn

def dashboard_history_start(user_id: int, job: Dict[str, Any]) -> None:
    if job.get("history_disabled"):
        return
    lib = LIBRARY.get(int(user_id)) or {}
    try:
        with dashboard_db_connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs(job_id,user_id,kind,status,started_at,input_name,input_size) VALUES(?,?,?,?,?,?,?)",
                       (job["dashboard_id"], int(user_id), str(job.get("kind", "job")), "running",
                        float(job["started_at"]), lib.get("name"), int(lib.get("size", 0))))
    except Exception as e:
        logger.warning(f"Dashboard history start failed: {e}")

def dashboard_history_finish(user_id: int, job: Dict[str, Any]) -> None:
    if job.get("history_disabled"):
        return
    status = "cancelled" if job.get("cancelled") else str(job.get("outcome") or "completed")
    try:
        with dashboard_db_connect() as db:
            db.execute("""UPDATE jobs SET status=?,finished_at=?,output_name=?,output_size=?,elapsed=?,error=?,retry_spec=?
                          WHERE job_id=? AND user_id=?""",
                       (status, time.time(), job.get("output_name"), int(job.get("output_size", 0)),
                        max(0.0, time.time() - float(job.get("started_at", time.time()))),
                        str(job.get("error", ""))[:500] or None,
                        json.dumps(job.get("retry_spec") or {}, ensure_ascii=False),
                        job.get("dashboard_id"), int(user_id)))
    except Exception as e:
        logger.warning(f"Dashboard history finish failed: {e}")

def dashboard_history_update_output(user_id: int, job_id: Optional[str], name: str, size: int) -> None:
    if not job_id:
        return
    try:
        with dashboard_db_connect() as db:
            db.execute("UPDATE jobs SET output_name=?,output_size=? WHERE job_id=? AND user_id=?",
                       (Path(name).name, max(0, int(size)), job_id, int(user_id)))
    except Exception as e:
        logger.warning(f"Dashboard delivered-name update failed: {e}")

def dashboard_history_set_retry(job: Dict[str, Any], spec: Dict[str, Any]) -> None:
    job["retry_spec"] = spec
    try:
        with dashboard_db_connect() as db:
            db.execute("UPDATE jobs SET retry_spec=? WHERE job_id=?",
                       (json.dumps(spec, ensure_ascii=False), job.get("dashboard_id")))
    except Exception as e:
        logger.warning(f"Dashboard retry metadata save failed: {e}")

def begin_job(user_id: int, kind: str) -> Optional[Dict[str, Any]]:
    """Registers a running job (one at a time per user). Returns None if the user is busy."""
    if user_id in JOBS:
        return None
    now = time.time()
    job = {"proc": None, "cancelled": False, "kind": kind, "dashboard_id": uuid.uuid4().hex,
           "started_at": now, "updated_at": now, "stage": str(kind), "progress": 0.0,
           "outcome": "running", "status_text": "", "history_disabled": kind == "delivery"}
    JOBS[user_id] = job
    dashboard_history_start(user_id, job)
    return job

def record_job_finish(user_id: int) -> Optional[Dict[str, Any]]:
    job = JOBS.get(int(user_id))
    if job:
        if job.get("cancelled"):
            job["outcome"] = "cancelled"
        elif job.get("outcome") == "running":
            job["outcome"] = "completed"
        job["updated_at"] = time.time()
        dashboard_history_finish(int(user_id), job)
    JOBS.pop(int(user_id), None)
    return job

def request_cancel(user_id: int) -> bool:
    job = JOBS.get(user_id)
    if not job:
        return False
    job["cancelled"] = True
    proc = job.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    return True

def _unlink(p: Any) -> None:
    try:
        if p and Path(p).exists():
            Path(p).unlink()
    except Exception:
        pass

def clear_session_files(user_id: int) -> None:
    sess = USER_DATA.pop(user_id, None)
    if sess:
        _unlink(sess.get("srt_path"))
        _unlink(sess.get("raw_srt_path"))

def library_meta_path(user_id: int) -> Path:
    return LIBRARY_DIR / f".{int(user_id)}.library.json"

def persist_library_entry(user_id: int, entry: Dict[str, Any]) -> None:
    try:
        file_path = Path(entry["path"]).resolve()
        if file_path.parent != LIBRARY_DIR.resolve() or not file_path.is_file():
            raise ValueError("library entry is outside the managed directory")
        meta = library_meta_path(user_id)
        tmp = meta.with_name(meta.name + ".tmp")
        payload = {"file": file_path.name, "name": str(entry.get("name") or file_path.name),
                   "size": int(entry.get("size") or file_path.stat().st_size),
                   "info": entry.get("info") or {}, "raw": entry.get("raw") or {},
                   "added": float(entry.get("added") or time.time()),
                   "expires": float(entry.get("expires") or time.time() + KEEP_MINUTES * 60)}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta)
    except Exception as exc:
        logger.warning("Could not persist video library metadata for user %s: %s", user_id, exc)

async def restore_library() -> int:
    """Restore saved videos on restart; legacy files are probed once and indexed."""
    restored = 0
    now = time.time()
    root = LIBRARY_DIR.resolve()
    for meta in LIBRARY_DIR.glob(".*.library.json"):
        match = re.fullmatch(r"\.(\d+)\.library\.json", meta.name)
        if not match:
            continue
        uid = int(match.group(1))
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
            file_path = (LIBRARY_DIR / str(payload.get("file") or "")).resolve()
            if file_path.parent != root or not file_path.is_file():
                _unlink(meta)
                continue
            expires = float(payload.get("expires") or 0)
            if expires <= now:
                _unlink(file_path); _unlink(meta)
                continue
            LIBRARY[uid] = {"path": str(file_path), "name": str(payload.get("name") or file_path.name),
                            "size": int(payload.get("size") or file_path.stat().st_size),
                            "info": payload.get("info") or {}, "raw": payload.get("raw") or {},
                            "added": float(payload.get("added") or file_path.stat().st_mtime), "expires": expires}
            restored += 1
        except Exception as exc:
            logger.warning("Could not restore library record %s: %s", meta.name, exc)

    # Older deployments had no sidecar metadata. Preserve and index valid, unexpired files.
    for file_path in LIBRARY_DIR.iterdir():
        if not file_path.is_file() or file_path.name.startswith(".") or file_path.suffix.lower() not in VIDEO_EXTS:
            continue
        match = re.match(r"^(\d+)_(?:\d+|web_[A-Za-z0-9-]+)$", file_path.stem)
        if not match:
            continue
        uid = int(match.group(1))
        if uid in LIBRARY:
            continue
        try:
            stat = file_path.stat()
            added = float(stat.st_mtime)
            expires = added + KEEP_MINUTES * 60
            if expires <= now:
                continue
            info = await probe_video(file_path)
            raw = await probe_raw(file_path)
            entry = {"path": str(file_path.resolve()), "name": file_path.name, "size": stat.st_size,
                     "info": info, "raw": raw, "added": added, "expires": expires}
            LIBRARY[uid] = entry
            persist_library_entry(uid, entry)
            restored += 1
        except Exception as exc:
            logger.warning("Could not restore legacy video %s: %s", file_path.name, exc)
    if restored:
        logger.info("Restored %d video library entries after restart", restored)
    return restored

def lib_get(user_id: int) -> Optional[Dict[str, Any]]:
    e = LIBRARY.get(user_id)
    if not e:
        return None
    if not Path(e["path"]).exists() or (time.time() > e["expires"] and user_id not in JOBS):
        lib_remove(user_id, remove_srt=False)
        return None
    return e

def lib_remove(user_id: int, remove_srt: bool = True) -> None:
    e = LIBRARY.pop(user_id, None)
    if e:
        _unlink(e["path"])
    _unlink(library_meta_path(user_id))
    if remove_srt:
        clear_session_files(user_id)

def lib_touch(user_id: int, extra_minutes: int = 0) -> None:
    e = LIBRARY.get(user_id)
    if e:
        e["expires"] = max(e["expires"], time.time() + (KEEP_MINUTES + extra_minutes) * 60)

async def library_janitor() -> None:
    """Deletes expired server files / old sync reports, never while a job is using them."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for uid, e in list(LIBRARY.items()):
            if uid in JOBS:
                continue
            if now > e["expires"] or not Path(e["path"]).exists():
                lib_remove(uid)
        for token, r in list(SYNC_REPORTS.items()):
            if now - r["created"] > 6 * 3600:
                SYNC_REPORTS.pop(token, None)
        for token, r in list(PAGES.items()):
            if now - r["created"] > 6 * 3600:
                PAGES.pop(token, None)

def vlabel_of(info: Dict[str, Any]) -> str:
    return {"hevc": "HEVC", "h264": "H.264"}.get(info.get("v_codec", ""), (info.get("v_codec") or "VIDEO").upper())

# ---------------- library menu ----------------
def lib_menu_text(user_id: int) -> str:
    e = lib_get(user_id)
    if not e:
        return "📂 **No file is ready.** Send a video first."
    info = e["info"]
    sess = USER_DATA.get(user_id) or {}
    srt_line = f"📄 **SRT:** `{sess['srt_name']}`" if sess.get("srt_path") else "📄 **SRT:** — (not sent)"
    if sess.get("srt_path") and sess.get("delay_ms"):
        srt_line += f"   ⏱ Delay `{sess['delay_ms'] / 1000:+.2f}s`"
    left = time_formatter(max(int(e["expires"] - time.time()), 0))
    return (
        "📂 **Your Video**\n\n"
        f"🎬 `{e['name']}`\n"
        f"📦 {humanbytes(e['size'])} • ⏱ {fmt_hms(info['duration'])}\n"
        f"🎞 {info['width']}×{info['height']} • {vlabel_of(info)} • {fmt_fps(info['fps'])} fps\n"
        f"🔊 Audio: {len(info['audio'])}   💬 Subtitles: {len(info['subs'])}\n"
        f"{srt_line}\n"
        f"🕒 Kept on server for: **{left}**"
    )

def lib_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Hardcode", callback_data="m_hc"),
         InlineKeyboardButton("🧩 Embed Sub", callback_data="m_embed")],
        [InlineKeyboardButton("📋 Info", callback_data="m_meta"),
         InlineKeyboardButton("💬 Subs", callback_data="m_subs"),
         InlineKeyboardButton("🔊 Audio", callback_data="m_audio")],
        [InlineKeyboardButton("⏱ Fix Delay", callback_data="dl_open")],
        [InlineKeyboardButton("⏳ +1 Hour", callback_data="m_ext"),
         InlineKeyboardButton("🗑 Delete", callback_data="m_del")],
    ])

async def send_lib_menu(client: Client, chat_id: int, user_id: int) -> None:
    if lib_get(user_id):
        await staged_send(client, chat_id, "menu", lib_menu_text(user_id), lib_menu_keyboard(), os.environ.get("TEXT_TEMP_MENU", "⏳ Loading your menu......"))

# ---------------- progress helper for simple ffmpeg jobs ----------------
async def simple_progress(message: Message, title: str, current: float, total: float, start: float,
                          job: Optional[Dict[str, Any]] = None) -> None:
    now = time.time()
    if getattr(message, "_last_update_time", 0) and (now - message._last_update_time) < 3:
        return
    message._last_update_time = now
    pct = min(current / total * 100, 100) if total > 0 else 0
    bar = "█" * math.floor(pct / 10) + "░" * (10 - math.floor(pct / 10))
    if job is not None:
        elapsed = max(0.0, now - start)
        average_rate = current / elapsed if elapsed > 0 and current > 0 else 0.0
        eta = int(max(0.0, total - current) / average_rate) if total > 0 and average_rate > 0 else 0
        job.update(stage=title, progress=pct, elapsed=elapsed, current_secs=current,
                   total_secs=total, eta_seconds=eta, updated_at=now)
    await safe_edit(
        message,
        f"⚙️ **{title}**\n\n[{bar}] `{pct:.1f}%`\n🕒 **Elapsed:** {time_formatter(now - start)}",
        cancel=True,
    )

async def run_ffmpeg_job(cmd: list, job: Dict[str, Any], status_msg: Message, title: str, total_secs: float) -> Tuple[bool, str]:
    """Runs ffmpeg (with -progress pipe:1) under the cancel button."""
    logger.info("Running FFmpeg Command: " + " ".join(cmd))
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    job["proc"] = process
    tail = {"text": ""}

    async def drain():
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            tail["text"] = (tail["text"] + chunk.decode("utf-8", errors="ignore"))[-4000:]

    drain_task = asyncio.create_task(drain())
    start = time.time()
    status_msg._last_update_time = 0
    try:
        job.update(stage=title, progress=0.0, elapsed=0.0, current_secs=0.0,
                   total_secs=float(total_secs or 0), updated_at=start)
        await simple_progress(status_msg, title, 0, total_secs, start, job)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", errors="ignore").strip()
            if s.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    cur = int(s.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                await simple_progress(status_msg, title, cur, total_secs, start, job)
        await process.wait()
        await drain_task
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        job["proc"] = None

    if job.get("cancelled"):
        return False, "cancelled"
    if process.returncode != 0:
        logger.error("FFmpeg failed:\n" + tail["text"][-2500:])
        return False, "ffmpeg_error"
    return True, "ok"

# ---------------- metadata ----------------
async def probe_raw(path: Path) -> Dict[str, Any]:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", "-show_chapters", str(path)]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await process.communicate()
    try:
        return json.loads(out.decode("utf-8"))
    except Exception:
        return {}

def _kbps(v: Any) -> str:
    n = _to_int(v)
    return f"{round(n / 1000)} kbps" if n else "?"

def format_metadata(raw: Dict[str, Any], name: str, size: int) -> str:
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])
    lines = [
        "📋 **Media Info**\n",
        f"📄 `{name}`",
        f"📦 {humanbytes(size)} • {str(fmt.get('format_name', '?')).split(',')[0]} • "
        f"⏱ {fmt_hms(float(fmt.get('duration') or 0))} • {_kbps(fmt.get('bit_rate'))}",
    ]
    title = (fmt.get("tags") or {}).get("title") or (fmt.get("tags") or {}).get("TITLE")
    if title:
        lines.append(f"🏷 Title: `{title}`")

    vids = [s for s in streams if s.get("codec_type") == "video"]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    if vids:
        lines.append("\n🎞 **Video**")
        for s in vids[:3]:
            fps = fmt_fps(_to_fps(s.get("avg_frame_rate", "0")))
            hdr = " • HDR" if "smpte2084" in str(s.get("color_transfer", "")) or "arib" in str(s.get("color_transfer", "")) else ""
            lines.append(
                f"• {s.get('codec_name', '?').upper()} {s.get('profile', '')} • {s.get('width')}×{s.get('height')} • "
                f"{fps} fps • {s.get('pix_fmt', '')} • {_kbps(s.get('bit_rate'))}{hdr}"
            )
    if auds:
        lines.append(f"\n🔊 **Audio ({len(auds)})**")
        for i, s in enumerate(auds[:20], 1):
            tl = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
            ch = CHANNEL_LABEL.get(_to_int(s.get("channels")), str(s.get("channels", "?")))
            br = _kbps(s.get("bit_rate") or tl.get("bps"))
            flags = " [default]" if (s.get("disposition") or {}).get("default") else ""
            lang = LANG_NAMES.get(tl.get("language", "")[:3], tl.get("language", "und").upper())
            ttl = f' "{tl["title"]}"' if tl.get("title") else ""
            lines.append(f"{i}. {lang} • {s.get('codec_name', '?').upper()} {ch} • {br} • {s.get('sample_rate', '?')} Hz{flags}{ttl}")
    if subs:
        lines.append(f"\n💬 **Subtitles ({len(subs)})**")
        for i, s in enumerate(subs[:30], 1):
            tl = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
            disp = s.get("disposition") or {}
            flags = (" [forced]" if disp.get("forced") else "") + (" [default]" if disp.get("default") else "")
            lang = LANG_NAMES.get(tl.get("language", "")[:3], tl.get("language", "und").upper())
            ttl = f' "{tl["title"]}"' if tl.get("title") else ""
            lines.append(f"{i}. {lang} • {s.get('codec_name', '?')}{flags}{ttl}")
    chapters = raw.get("chapters", [])
    if chapters:
        lines.append(f"\n🔖 Chapters: {len(chapters)}")
    text = "\n".join(lines)
    return text if len(text) <= 3900 else text[:3880] + "\n…"

# ---------------- subtitle / audio extraction plans ----------------
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "subviewer", "microdvd", "eia_608"}
AUDIO_EXT = {
    "aac": "m4a", "ac3": "ac3", "eac3": "eac3", "dts": "dts", "mp3": "mp3", "opus": "opus",
    "flac": "flac", "truehd": "thd", "vorbis": "ogg", "pcm_s16le": "wav", "alac": "m4a",
}

def sub_extract_plan(track: Dict[str, Any]) -> Tuple[Optional[str], list]:
    codec = track["codec"]
    if codec in TEXT_SUB_CODECS:
        return "srt", ["-c:s", "srt"]
    if codec == "hdmv_pgs_subtitle":
        return "sup", ["-c:s", "copy"]
    return None, []          # image based (VobSub / DVB) cannot be converted here

def subs_menu(info: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    rows = []
    for n, t in enumerate(info["subs"][:40]):
        ext, _ = sub_extract_plan(t)
        lang = LANG_NAMES.get(t["lang"][:3], (t["lang"] or "und").upper())
        label = f"{n + 1}. {lang} • {t['codec']}" + (f" → .{ext}" if ext else " (not available)")
        rows.append([InlineKeyboardButton(("💬 " if ext else "🚫 ") + label, callback_data=f"xs:{n}" if ext else "noop")])
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="m_back")])
    text = "💬 **Subtitle tracks**\nChoose one to extract:" if info["subs"] else \
           "💬 This video has no subtitle tracks."
    return text, InlineKeyboardMarkup(rows)

def audio_menu(info: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    rows = []
    for n, t in enumerate(info["audio"][:40]):
        lang = LANG_NAMES.get(t["lang"][:3], (t["lang"] or "und").upper())
        ch = CHANNEL_LABEL.get(t["channels"], str(t["channels"]))
        br = f" • {round(t['bitrate'] / 1000)}k" if t["bitrate"] else ""
        rows.append([InlineKeyboardButton(
            f"🔊 {n + 1}. {lang} • {t['codec'].upper()} {ch}{br} → .{AUDIO_EXT.get(t['codec'], 'mka')}",
            callback_data=f"xa:{n}")])
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="m_back")])
    text = "🔊 **Audio tracks**\nChoose one to extract (copied without quality loss):" if info["audio"] else \
           "🔊 This video has no audio tracks."
    return text, InlineKeyboardMarkup(rows)

# ---------------- soft-embed an SRT ----------------
_TL_RE = re.compile(r"(\d+:\d{2}:\d{2}[,.]\d{1,6})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,6})")

def shift_time_line(line: str, shift_ms: int) -> Optional[str]:
    """Shifts one 'start --> end' line. Returns None when the cue ends before 0 after the shift."""
    m = _TL_RE.search(line)
    if not m:
        return line
    s_ms, e_ms = srt_time_to_ms(m.group(1)) + shift_ms, srt_time_to_ms(m.group(2)) + shift_ms
    if e_ms <= 0:
        return None
    return f"{_srt_ts(max(s_ms, 0))} --> {_srt_ts(e_ms)}"

def clean_srt_for_soft_embed(src: Path, dst: Path, shift_ms: int = 0) -> int:
    """Soft subtitle tracks must be plain text: drop ASS override tags ({\\an8}...), turn \\N into a newline."""
    content = open(src, "r", encoding="utf-8", errors="ignore").read().replace("\r\n", "\n").replace("\r", "\n")
    out, idx = [], 1
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        t_idx = next((i for i, l in enumerate(lines) if "-->" in l), -1)
        if t_idx == -1:
            continue
        text = "\n".join(lines[t_idx + 1:])
        text = re.sub(r"\{[^}]*\}", "", text).replace("\\N", "\n").replace("\\n", "\n")
        text = "\n".join(l.strip() for l in text.split("\n") if l.strip())
        tl = shift_time_line(lines[t_idx].strip(), shift_ms)
        if text and tl:
            out.append(f"{idx}\n{tl}\n{text}")
            idx += 1
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    return idx - 1

def shift_srt_file(src: Path, dst: Path, shift_ms: int) -> int:
    """Same SRT, every time shifted (tags / styling kept), re-indexed."""
    content = open(src, "r", encoding="utf-8", errors="ignore").read().replace("\r\n", "\n").replace("\r", "\n")
    out, idx = [], 1
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        t_idx = next((i for i, l in enumerate(lines) if "-->" in l), -1)
        if t_idx == -1 or len(lines) <= t_idx + 1:
            continue
        tl = shift_time_line(lines[t_idx].strip(), shift_ms)
        if tl:
            out.append(f"{idx}\n{tl}\n" + "\n".join(lines[t_idx + 1:]))
            idx += 1
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    return idx - 1

def build_embed_cmd(src: Path, srt: Path, out: Path, info: Dict[str, Any], ext: str,
                    keep_audio=None, keep_subs=None) -> list:
    n_aud = len(info["audio"])
    audio_idx = list(range(n_aud)) if keep_audio is None else [i for i in sorted(keep_audio) if 0 <= i < n_aud]
    is_mkv = ext == ".mkv"
    cand = range(len(info["subs"])) if keep_subs is None else sorted(keep_subs)
    old = [i for i in cand if 0 <= i < len(info["subs"]) and (is_mkv or info["subs"][i]["codec"] in TEXT_SUB_CODECS)]
    n_old = len(old)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
           "-i", str(src), "-i", str(srt), "-map", "0:v"]
    for i in audio_idx:
        cmd += ["-map", f"0:a:{i}"]
    for i in old:
        cmd += ["-map", f"0:s:{i}"]
    if is_mkv:
        cmd += ["-map", "0:t?"]                                  # attached fonts
    cmd += ["-map", "1:0", "-dn", "-c", "copy"]
    cmd += [f"-c:s:{n_old}", "srt"] if is_mkv else ["-c:s", "mov_text"]
    cmd += [f"-metadata:s:s:{n_old}", "language=sin", f"-metadata:s:s:{n_old}", "title=Sinhala",
            f"-disposition:s:{n_old}", "default"]
    for k in range(n_old):                                       # only the new Sinhala track stays "default"
        cmd += [f"-disposition:s:{k}", "-default"]
    if not is_mkv:
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out))
    return cmd

# ---------------- upload helper (retries + cancel) ----------------
async def upload_result(client: Client, chat_id: int, path: Path, send_name: str, caption: str,
                        status_msg: Message, job: Dict[str, Any], width: int = 0, height: int = 0,
                        duration: int = 0, thumb: Optional[Path] = None, reply_markup=None) -> bool:
    for attempt in range(1, 4):
        if job["cancelled"]:
            return False
        try:
            up_time = time.time()
            status_msg._last_update_time = 0
            pargs = ("Uploading to Telegram…", status_msg, up_time, client, job)
            if SEND_AS == "video":
                sent = await client.send_video(
                    chat_id=chat_id, video=str(path), file_name=send_name, duration=duration,
                    width=width, height=height, thumb=str(thumb) if thumb else None,
                    caption=caption, parse_mode=ParseMode.DISABLED, supports_streaming=True,
                    reply_markup=reply_markup, progress=progress_bar, progress_args=pargs)
            else:
                sent = await client.send_document(
                    chat_id=chat_id, document=str(path), file_name=send_name,
                    thumb=str(thumb) if thumb else None, caption=caption, parse_mode=ParseMode.DISABLED,
                    reply_markup=reply_markup, progress=progress_bar, progress_args=pargs)
            return bool(sent) and not job["cancelled"]
        except FloodWait as e:
            await asyncio.sleep(getattr(e, "value", getattr(e, "x", 5)) + 1)
        except Exception as e:
            logger.error(f"Upload attempt {attempt} failed: {e}")
            if attempt == 3:
                raise
            await asyncio.sleep(5 * attempt)
    return False


# ---------------------- output filename confirmation ----------------------
FILE_AUTO_SEND_SECONDS = max(1, int(os.environ.get("FILE_AUTO_SEND_SECONDS", "300")))
PENDING_DELIVERIES: Dict[str, Dict[str, Any]] = {}
USER_PENDING_RENAME: Dict[int, str] = {}
PRENAME_REQUESTS: Dict[str, Dict[str, Any]] = {}
USER_PRENAME: Dict[int, str] = {}

def file_name_keyboard(token: str, retry: bool = False) -> InlineKeyboardMarkup:
    first = InlineKeyboardButton("⏭ Send Now" if not retry else "🔁 Retry Upload", callback_data=f"file:send:{token}")
    return InlineKeyboardMarkup([[first, InlineKeyboardButton("✏️ Rename", callback_data=f"file:rename:{token}")],
                                 [InlineKeyboardButton("🗑 Discard", callback_data=f"file:cancel:{token}")]])

def sanitize_output_filename(proposed: str, original_name: str) -> Optional[str]:
    ext = Path(original_name).suffix or ".mkv"
    base = (proposed or "").replace("\\", "/").split("/")[-1].strip()
    if base.lower().endswith(ext.lower()):
        base = base[:-len(ext)]
    else:
        suffix = Path(base).suffix.lower()
        if suffix in VIDEO_EXTS:
            base = base[:-len(suffix)]
    base = re.sub(r'[\x00-\x1f<>:"/\\|?*`]', "_", base)
    base = re.sub(r"\s+", " ", base).strip(" ._-\t")[:120]
    if not base:
        return None
    return base + ext

def output_name_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Keep Original Name", callback_data=f"name:skip:{token}")]])

async def ask_output_name(client: Client, chat_id: int, user_id: int, default_name: str,
                          job: Dict[str, Any]) -> None:
    """Ask during encode start; the same prompt is reused for delivery if no name is supplied."""
    token = secrets.token_hex(5)
    ext = Path(default_name).suffix
    stem = htmlmod.escape(Path(default_name).stem)
    message = await client.send_message(
        chat_id,
        "📝 <b>Choose a name for the output file</b> (optional)\n\n"
        f"Original name: <code>{stem}</code>\n"
        f"The extension <code>{htmlmod.escape(ext)}</code> is added automatically. A name you send is used right away; "
        "you can also rename it after the output is ready.\n"
        "⏭ Skip keeps the original name.",
        parse_mode=ParseMode.HTML, reply_markup=output_name_keyboard(token))
    PRENAME_REQUESTS[token] = {"user_id": int(user_id), "default_name": default_name,
                               "message": message, "created": time.time(), "accepting": True,
                               "chosen_name": None, "skipped": False}
    USER_PRENAME[int(user_id)] = token
    job["pre_name_token"] = token

async def present_file_name_choice(
    user_id: int, chat_id: int, path: Path, default_name: str, caption: str, status_msg: Message,
    width: int = 0, height: int = 0, duration: int = 0, thumb: Optional[Path] = None,
    reply_markup=None, show_menu: bool = True,
) -> str:
    token = secrets.token_hex(5)
    pre_token = USER_PRENAME.pop(int(user_id), None)
    pre = PRENAME_REQUESTS.pop(pre_token, None) if pre_token else None
    immediate_send = bool(pre and (pre.get("chosen_name") or pre.get("skipped")))
    delivery_status = pre.get("message") if pre and pre.get("message") else status_msg
    if pre and pre.get("chosen_name"):
        default_name = pre["chosen_name"]
    if pre and delivery_status is not status_msg:
        await delete_status_message(status_msg)
    PENDING_DELIVERIES[token] = {
        "user_id": int(user_id), "chat_id": int(chat_id), "path": Path(path),
        "default_name": default_name, "chosen_name": (pre or {}).get("chosen_name"),
        "caption": caption, "status": delivery_status,
        "width": int(width), "height": int(height), "duration": int(duration),
        "thumb": Path(thumb) if thumb else None, "reply_markup": reply_markup,
        "created": time.time(), "auto_send_at": time.time() + (0 if immediate_send else FILE_AUTO_SEND_SECONDS),
        "show_menu": bool(show_menu), "sending": False,
        "history_job_id": (JOBS.get(int(user_id)) or {}).get("dashboard_id"),
    }
    shown_name = htmlmod.escape(Path(default_name).name)
    if immediate_send:
        await safe_edit(delivery_status,
            f"✅ <b>Video ready</b>\n📄 Name: <code>{shown_name}</code>\n📤 Sending now…",
            reply_markup=None, parse_mode=ParseMode.HTML)
        spawn(deliver_pending_when_idle(token))
    else:
        await safe_edit(delivery_status,
            "✅ <b>Your video is ready</b>\n\n"
            f"📄 File name: <code>{shown_name}</code>\n"
            "Tap <b>Rename</b> and send a new name. The extension stays the same, "
            f"and if you do nothing it is sent automatically after {max(1, round(FILE_AUTO_SEND_SECONDS / 60))} min.",
            reply_markup=file_name_keyboard(token), parse_mode=ParseMode.HTML)
        spawn(auto_send_pending_delivery(token))
    return token

async def auto_send_pending_delivery(token: str) -> None:
    """Keep the encoded output and send with its current name after the user's five-minute window."""
    await asyncio.sleep(FILE_AUTO_SEND_SECONDS)
    while True:
        entry = PENDING_DELIVERIES.get(token)
        if not entry or entry.get("sending"):
            return
        uid = int(entry["user_id"])
        if uid not in JOBS:
            await start_pending_delivery(bot, uid, token)
            return
        # If the user starts another operation while the delivery is waiting, send as soon as it is idle.
        await asyncio.sleep(30)

async def deliver_pending_when_idle(token: str) -> None:
    """Deliver a name chosen before encode as soon as the processing job releases the user lock."""
    while token in PENDING_DELIVERIES:
        entry = PENDING_DELIVERIES[token]
        uid = int(entry["user_id"])
        if uid not in JOBS:
            await start_pending_delivery(bot, uid, token, entry.get("chosen_name"))
            return
        await asyncio.sleep(1)

def caption_for_delivery(caption: str, send_name: str) -> str:
    """Show the final user-selected filename in the Telegram caption, keeping the existing details."""
    lines = (caption or "").splitlines()
    details = lines[1:] if lines else []
    return "\n".join([f"📄 {Path(send_name).name}", *details])[:1024]

async def start_pending_delivery(client: Client, user_id: int, token: str, chosen_name: Optional[str] = None) -> bool:
    entry = PENDING_DELIVERIES.get(token)
    if not entry or entry.get("user_id") != int(user_id):
        await client.send_message(user_id, "⚠️ This output is no longer available. Process again if needed.")
        return False
    if entry.get("sending"):
        return False
    job = begin_job(user_id, "delivery")
    if not job:
        await client.send_message(entry["chat_id"], "⏳ Another job is running. Choose again when it finishes.")
        return False
    entry["sending"] = True
    send_name = chosen_name or entry.get("chosen_name") or entry["default_name"]
    entry["chosen_name"] = send_name
    sent_ok = False
    try:
        await safe_edit(entry["status"], f"📤 Uploading <b>{htmlmod.escape(Path(send_name).name)}</b> to Telegram…", cancel=True,
                        parse_mode=ParseMode.HTML)
        await send_event_sticker(client, entry["chat_id"], "upload", job)
        sent = await upload_result(client, entry["chat_id"], entry["path"], send_name,
                                   caption_for_delivery(entry["caption"], send_name),
                                   entry["status"], job, entry["width"], entry["height"], entry["duration"],
                                   entry.get("thumb"), entry.get("reply_markup"))
        await cleanup_event_sticker(entry["chat_id"], job, "upload")
        if sent:
            sent_ok = True
            try:
                delivered_size = Path(entry["path"]).stat().st_size
            except OSError:
                delivered_size = 0
            dashboard_history_update_output(user_id, entry.get("history_job_id"), send_name, delivered_size)
            await send_event_sticker(client, entry["chat_id"], "success", job)
            PENDING_DELIVERIES.pop(token, None)
            if USER_PENDING_RENAME.get(user_id) == token:
                USER_PENDING_RENAME.pop(user_id, None)
            _unlink(entry.get("path"))
            _unlink(entry.get("thumb"))
            await delete_status_message(entry.get("status"))
            return True
        await send_event_sticker(client, entry["chat_id"], "error", job)
        await safe_edit(entry["status"], "❌ Upload failed. Try again or discard the output.",
                        reply_markup=file_name_keyboard(token, retry=True))
        return False
    except Exception as e:
        await send_event_sticker(client, entry["chat_id"], "error", job)
        logger.error(f"Deferred output upload failed for user {user_id}: {e}", exc_info=True)
        await safe_edit(entry["status"], "❌ Upload failed. Try again or discard the output.",
                        reply_markup=file_name_keyboard(token, retry=True))
        return False
    finally:
        await cleanup_event_sticker(entry["chat_id"], job, "upload")
        entry["sending"] = False
        await finish_job(client, entry["chat_id"], user_id, show_menu=entry["show_menu"] and sent_ok)

async def file_delivery_callback(client: Client, callback: CallbackQuery, action: str, token: str, user_id: int) -> None:
    entry = PENDING_DELIVERIES.get(token)
    if not entry or entry.get("user_id") != int(user_id):
        await callback.answer("This output is no longer available.", show_alert=True)
        return
    if action == "rename":
        USER_PENDING_RENAME[user_id] = token
        original_stem = htmlmod.escape(Path(entry["default_name"]).stem)
        original_ext = htmlmod.escape(Path(entry["default_name"]).suffix)
        await safe_edit(entry["status"],
            "✏️ <b>Send the new file name</b>\n\n"
            f"Original name: <code>{original_stem}</code>\n"
            f"The extension <code>{original_ext}</code> is added automatically. Copy the name above, edit it and send it back.\n"
            "If you send nothing, the original name is used after 5 min.\n"
            "Upload starts as soon as you send the name.",
            reply_markup=file_name_keyboard(token), parse_mode=ParseMode.HTML)
        await callback.answer()
    elif action == "send":
        await callback.answer("Starting upload…")
        spawn(start_pending_delivery(client, user_id, token))
    elif action == "cancel":
        PENDING_DELIVERIES.pop(token, None)
        if USER_PENDING_RENAME.get(user_id) == token:
            USER_PENDING_RENAME.pop(user_id, None)
        _unlink(entry.get("path"))
        _unlink(entry.get("thumb"))
        await safe_edit(entry["status"], "🗑 Temporary output discarded.")
        await callback.answer("Discarded")

async def handle_pending_rename_input(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    pre_token = USER_PRENAME.get(user_id)
    pre = PRENAME_REQUESTS.get(pre_token) if pre_token else None
    if pre and pre.get("accepting") and pre.get("user_id") == int(user_id):
        new_name = sanitize_output_filename(message.text or "", pre["default_name"])
        if not new_name:
            await message.reply_text("⚠️ Send a valid file name. The extension is added automatically.")
            return True
        pre["chosen_name"], pre["accepting"] = new_name, False
        current_job = JOBS.get(user_id)
        if current_job:
            current_job["selected_output_name"] = new_name
        await safe_edit(pre["message"],
                        f"✅ Name saved: <code>{htmlmod.escape(new_name)}</code>\n"
                        "It will be sent with this name as soon as the video is ready.",
                        reply_markup=None, parse_mode=ParseMode.HTML)
        return True
    token = USER_PENDING_RENAME.get(user_id)
    if not token:
        return False
    entry = PENDING_DELIVERIES.get(token)
    if not entry:
        USER_PENDING_RENAME.pop(user_id, None)
        await message.reply_text("⚠️ The output to rename is no longer available.")
        return True
    new_name = sanitize_output_filename(message.text or "", entry["default_name"])
    if not new_name:
        await message.reply_text("⚠️ Send a valid name (the extension is kept).")
        return True
    entry["chosen_name"] = new_name
    USER_PENDING_RENAME.pop(user_id, None)
    await safe_edit(entry["status"],
                    f"✅ Name saved: <code>{htmlmod.escape(new_name)}</code>\n📤 Sending now…",
                    reply_markup=None, parse_mode=ParseMode.HTML)
    spawn(deliver_pending_when_idle(token))
    return True

async def pre_encode_name_callback(callback: CallbackQuery, token: str, user_id: int) -> None:
    pending = PRENAME_REQUESTS.get(token)
    if not pending or int(pending.get("user_id", 0)) != int(user_id) or not pending.get("accepting"):
        await callback.answer("This name choice is no longer available.", show_alert=True)
        return
    pending["accepting"], pending["skipped"] = False, True
    job = JOBS.get(int(user_id))
    if job:
        job["selected_output_name"] = None
    await safe_edit(pending["message"],
                    f"⏭ Original name kept: <code>{htmlmod.escape(Path(pending['default_name']).name)}</code>\n"
                    "It will be sent with this name as soon as the video is ready.",
                    reply_markup=None, parse_mode=ParseMode.HTML)
    await callback.answer("Original name kept")


# ------------------------------------------------------------------------------
# SYNC CHECKER: English original vs Sinhala, matched by TIME (not by cue numbers)
# ------------------------------------------------------------------------------
import bisect
import secrets
import statistics

SYNC_TOL_START = float(os.environ.get("SYNC_TOL_START", "0.7"))   # seconds
SYNC_TOL_END = float(os.environ.get("SYNC_TOL_END", "1.2"))
SYNC_STAGES = ["Reading files", "Timing offset", "Matching", "Classifying", "Report"]

AD_RE = re.compile(
    r"(www\.|https?://|\.com\b|\.lk\b|\.net\b|\.org\b|t\.me/|telegram|@\w{3,}|subtitles?\s+(by|from)|subs?\s+by|"
    r"synced?\s+(by|and)|opensubtitles|translated\s+by|translation\s+by|join\s+us|follow\s+us|subscribe|"
    r"whatsapp|facebook|instagram|donate|advertise|"
    r"උපසිරැසි\s*ගැන්වීම|ගැන්වීම|ටෙලිග්‍රෑම්|ටෙලිග්රෑම්|චැනල්|ෆේස්බුක්|වෙබ්\s*අඩවිය|අපගේ\s*චැනල්)", re.I)
SDH_RE = re.compile(r"^\s*[\[\(][^\]\)]{0,60}[\]\)]\s*:?\s*$")
MUSIC_RE = re.compile(r"^[\s♪♫#*\-–—.]+$")

class SyncCancelled(Exception):
    pass

def plain_text(raw: str) -> str:
    t = re.sub(r"\{[^}]*\}", "", raw)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("\\N", " ").replace("\\n", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", t).strip()

def is_sdh(text: str) -> bool:
    return bool(text) and (bool(SDH_RE.match(text)) or bool(MUSIC_RE.match(text)))

def parse_srt_cues(path: Path, stats: Optional[Dict[str, int]] = None) -> list:
    content = ""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            content = open(path, "r", encoding=enc).read()
            break
        except UnicodeDecodeError:
            continue
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    rx = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,6})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,6})")
    cues = []
    skipped = 0
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        found = False
        for i, l in enumerate(lines):
            m = rx.search(l)
            if not m:
                continue
            found = True
            g = m.groups()
            s = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int((g[3] + "00")[:3]) / 1000
            e = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int((g[7] + "00")[:3]) / 1000
            raw = "\n".join(lines[i + 1:])
            txt = plain_text(raw)
            if txt:
                cues.append({"s": s, "e": max(e, s + 0.1), "raw": raw, "text": txt, "positioned": "{\\" in raw})
            break
        if not found and lines:
            skipped += 1
    if stats is not None:
        stats["skipped"] = skipped
    cues.sort(key=lambda c: c["s"])
    for n, c in enumerate(cues):
        c["n"] = n + 1
    return cues

def estimate_offset(en: list, si: list) -> Tuple[float, float]:
    """Most common (sinhala start - english start) difference. Returns (offset seconds, confidence 0..1)."""
    en_starts = [c["s"] for c in en]
    sample = si if len(si) <= 1500 else si[:: max(len(si) // 1500, 1)]
    best_off, conf = 0.0, 0.0
    for window in (60.0, 900.0):
        votes: Dict[int, int] = {}
        for c in sample:
            lo = bisect.bisect_left(en_starts, c["s"] - window)
            hi = bisect.bisect_right(en_starts, c["s"] + window)
            for k in range(lo, hi):
                b = round((c["s"] - en_starts[k]) / 0.1)
                votes[b] = votes.get(b, 0) + 1
        if not votes:
            continue
        b, v = max(((b, sum(votes.get(b + j, 0) for j in range(-2, 3))) for b in votes), key=lambda x: x[1])
        best_off, conf = b * 0.1, min(v / max(len(sample), 1), 1.0)
        if conf >= 0.25:
            break
    # refine to millisecond accuracy with the median of the close differences
    close = []
    for c in sample:
        k = bisect.bisect_left(en_starts, c["s"] - best_off)
        for kk in (k - 1, k):
            if 0 <= kk < len(en_starts):
                d = c["s"] - en_starts[kk]
                if abs(d - best_off) < 0.6:
                    close.append(d)
    if close:
        best_off = statistics.median(close)
    return best_off, conf

def offset_profile(en: list, si: list, g_off: float) -> list:
    """Local offset every ~30 cues so drift / jumps (cut scenes, fps change) become visible."""
    en_starts = [c["s"] for c in en]
    pts = []
    for i in range(0, len(si), 30):
        chunk = si[i:i + 30]
        res = []
        for c in chunk:
            k = bisect.bisect_left(en_starts, c["s"] - g_off)
            for kk in (k - 1, k):
                if 0 <= kk < len(en_starts):
                    d = c["s"] - g_off - en_starts[kk]
                    if abs(d) < 4:
                        res.append(d)
        if len(res) >= 5:
            pts.append([round(chunk[len(chunk) // 2]["s"], 2), round(g_off + statistics.median(res), 3)])
    if len(pts) >= 3:       # light 3-point median smoothing
        sm = [pts[0]]
        for j in range(1, len(pts) - 1):
            sm.append([pts[j][0], statistics.median([pts[j - 1][1], pts[j][1], pts[j + 1][1]])])
        sm.append(pts[-1])
        pts = sm
    return pts

def make_offset_fn(profile: list, g_off: float):
    if not profile:
        return lambda t: g_off
    xs = [p[0] for p in profile]
    ys = [p[1] for p in profile]
    def f(t: float) -> float:
        if t <= xs[0]:
            return ys[0]
        if t >= xs[-1]:
            return ys[-1]
        k = bisect.bisect_right(xs, t)
        x0, x1, y0, y1 = xs[k - 1], xs[k], ys[k - 1], ys[k]
        return y0 + (y1 - y0) * ((t - x0) / (x1 - x0) if x1 > x0 else 0)
    return f

def _cue_dict(c: dict) -> dict:
    return {"n": c["n"], "s": round(c["s"], 3), "e": round(c["e"], 3), "text": c["text"][:400]}

async def analyze_sync(en: list, si: list, state: Dict[str, Any], job: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    def check():
        if job and job.get("cancelled"):
            raise SyncCancelled()

    def stage(i: int, pct: float = 0.0):
        state["stage"] = i
        state["progress"] = round((i + pct) / len(SYNC_STAGES) * 100, 1)
        if job is not None:
            stage_name = SYNC_STAGES[min(i, len(SYNC_STAGES) - 1)]
            job["progress"] = state["progress"]
            job["stage"] = f"Subtitle sync — {stage_name}"
            job["status_text"] = f"Sync check: {stage_name}"
            job["updated_at"] = time.time()

    c_ = state["counters"]
    # ---- stage 1: offset
    stage(1)
    await asyncio.sleep(0)
    g_off, conf = estimate_offset(en, si)
    if conf < 0.12:
        g_off = 0.0
    profile = offset_profile(en, si, g_off) if conf >= 0.12 else []
    off = make_offset_fn(profile, g_off)
    state["offset"] = {"global": round(g_off, 3), "confidence": round(conf, 2)}
    check()

    # ---- stage 2: matching (union-find over overlapping cues)
    stage(2)
    n, m = len(si), len(en)
    parent = list(range(n + m))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    en_starts = [c["s"] for c in en]
    PAD = 0.3
    linked_si = [False] * n
    linked_en = [False] * m
    for i, c in enumerate(si):
        o = off(c["s"])
        a_s, a_e = c["s"] - o, c["e"] - o
        c["as"], c["ae"] = a_s, a_e
        k = max(bisect.bisect_left(en_starts, a_s - 12), 0)
        while k < m and en[k]["s"] <= a_e + PAD:
            e = en[k]
            if e["e"] >= a_s - PAD:
                ov = min(a_e, e["e"]) - max(a_s, e["s"])
                if ov > 0.12 or abs(a_s - e["s"]) <= 0.5:
                    union(i, n + k)
                    linked_si[i] = linked_en[k] = True
            k += 1
        if i % 150 == 0:
            c_["matched"] = sum(linked_si[: i + 1])
            c_["extra"] = (i + 1) - c_["matched"]
            stage(2, i / max(n, 1))
            check()
            await asyncio.sleep(0)

    # ---- stage 3: classify components
    stage(3)
    comps: Dict[int, Dict[str, list]] = {}
    for i in range(n):
        comps.setdefault(find(i), {"si": [], "en": []})["si"].append(i)
    for k in range(m):
        comps.setdefault(find(n + k), {"si": [], "en": []})["en"].append(k)

    items = []
    stats = {"ok": 0, "timing": 0, "missing": 0, "extra": 0, "merged": 0, "split": 0,
             "ads": 0, "sdh": 0, "overlap": 0, "fast": 0}

    # a Sinhala line and an English line that overlap nothing but sit close together are most likely
    # the SAME line shifted by a few seconds -> report them as one timing problem, not as missing + extra
    lone_si = [i for comp in comps.values() if comp["si"] and not comp["en"] for i in comp["si"]]
    lone_en = sorted((k for comp in comps.values() if comp["en"] and not comp["si"] for k in comp["en"]),
                     key=lambda k: en[k]["s"])
    lone_starts = [en[k]["s"] for k in lone_en]
    cand = []
    for i in lone_si:
        c = si[i]
        if AD_RE.search(c["text"]):
            continue
        lo = bisect.bisect_left(lone_starts, c["as"] - 6)
        hi = bisect.bisect_right(lone_starts, c["as"] + 6)
        for p in range(lo, hi):
            k = lone_en[p]
            if AD_RE.search(en[k]["text"]) or is_sdh(en[k]["text"]):
                continue
            cand.append((abs(c["as"] - en[k]["s"]), i, k))
    cand.sort()
    used_i, used_k = set(), set()
    for _, i, k in cand:
        if i in used_i or k in used_k:
            continue
        used_i.add(i)
        used_k.add(k)
        c, e = si[i], en[k]
        stats["timing"] += 1
        items.append({"type": "timing", "kind": "shifted", "t": e["s"], "si": [_cue_dict(c)], "en": [_cue_dict(e)],
                      "ds": round(c["as"] - e["s"], 2), "de": round(c["ae"] - e["e"], 2), "tags": []})

    for cid, comp in comps.items():
        S = [si[i] for i in sorted(comp["si"]) if i not in used_i]
        E = [en[k] for k in sorted(comp["en"]) if k not in used_k]
        if not S and not E:
            continue
        if S and not E:
            for c in S:
                ad = bool(AD_RE.search(c["text"]))
                stats["ads" if ad else "extra"] += 1
                items.append({"type": "extra", "t": c["s"], "si": [_cue_dict(c)], "en": [],
                              "tags": ["ad"] if ad else []})
            continue
        if E and not S:
            for e in E:
                tag = "ad" if AD_RE.search(e["text"]) else ("sdh" if is_sdh(e["text"]) else None)
                if tag:
                    stats["sdh" if tag == "sdh" else "ads"] += 1
                else:
                    stats["missing"] += 1
                items.append({"type": "missing", "t": e["s"], "si": [], "en": [_cue_dict(e)],
                              "tags": [tag] if tag else []})
            continue
        s0, s1 = min(c["as"] for c in S), max(c["ae"] for c in S)
        e0, e1 = min(c["s"] for c in E), max(c["e"] for c in E)
        ds, de = s0 - e0, s1 - e1
        kind = "match" if len(S) == 1 and len(E) == 1 else ("merged" if len(S) == 1 else ("split" if len(E) == 1 else "complex"))
        loosen = 0.0 if kind == "match" else 0.3
        bad = abs(ds) > SYNC_TOL_START + loosen or abs(de) > SYNC_TOL_END + loosen
        if kind in ("merged", "split", "complex"):
            stats["merged" if kind == "merged" else "split"] += 1
        typ = "timing" if bad else ("ok" if kind == "match" else kind)
        if typ == "timing":
            stats["timing"] += 1
        elif typ == "ok":
            stats["ok"] += 1
        items.append({"type": typ, "kind": kind, "t": e0, "si": [_cue_dict(c) for c in S], "en": [_cue_dict(c) for c in E],
                      "ds": round(ds, 2), "de": round(de, 2), "tags": []})

    c_["matched"] = stats["ok"] + stats["timing"] + stats["merged"] + stats["split"]
    c_["missing"], c_["extra"], c_["timing"] = stats["missing"], stats["extra"] + stats["ads"], stats["timing"]
    check()
    await asyncio.sleep(0)

    # ---- extra checks inside the Sinhala file
    prev = None
    for c in si:
        if c["positioned"]:
            continue
        if prev and c["s"] < prev["e"] - 0.05:
            stats["overlap"] += 1
            items.append({"type": "overlap", "t": c["s"], "si": [_cue_dict(prev), _cue_dict(c)], "en": [], "tags": []})
        dur = c["e"] - c["s"]
        cps = len(c["text"]) / dur if dur > 0 else 99
        if cps > 24 and len(c["text"]) > 12:
            stats["fast"] += 1
            items.append({"type": "fast", "t": c["s"], "si": [_cue_dict(c)], "en": [], "cps": round(cps, 1), "tags": []})
        prev = c

    # ---- stage 4: report
    stage(4)
    items.sort(key=lambda x: x["t"])
    for idx, it in enumerate(items):
        it["id"] = idx
    keep_ok = [it for it in items if it["type"] == "ok"]
    if len(keep_ok) > 6000:
        drop = set(id(x) for x in keep_ok[6000:])
        items = [it for it in items if id(it) not in drop]

    en_eff = sum(1 for e in en if not (AD_RE.search(e["text"]) or is_sdh(e["text"])))
    matched_en = sum(1 for k in range(m) if linked_en[k])
    coverage = min(matched_en / max(m, 1), 1.0)
    real_missing = stats["missing"]
    cov_eff = 1.0 - real_missing / max(en_eff, 1)
    issues_tim = stats["timing"] / max(c_["matched"], 1)
    reasons = []
    if abs(g_off) >= 0.3:
        reasons.append(f"Sinhala {'late' if g_off > 0 else 'early'} by {abs(g_off):.2f}s on average (global offset).")
    if len(profile) >= 3 and abs(profile[-1][1] - profile[0][1]) > 0.5:
        reasons.append(f"Offset changes from {profile[0][1]:+.2f}s to {profile[-1][1]:+.2f}s (drift / fps / cut mismatch).")
    if real_missing:
        reasons.append(f"{real_missing} English line(s) have no Sinhala line.")
    if stats["extra"] or stats["ads"]:
        reasons.append(f"{stats['extra']} extra Sinhala line(s) + {stats['ads']} ad/credit-like line(s) without English.")
    if stats["timing"]:
        reasons.append(f"{stats['timing']} line(s) are off by more than {SYNC_TOL_START}s.")
    if conf < 0.12:
        reasons.append("No consistent timing relation was found between the two files — are they the same movie/cut?")

    if conf < 0.12:
        level = "bad"
    elif cov_eff >= 0.95 and issues_tim <= 0.05 and stats["extra"] <= max(3, 0.03 * n):
        level = "good"
    elif cov_eff >= 0.85 and issues_tim <= 0.15:
        level = "warn"
    else:
        level = "bad"
    title = {"good": "Matches well", "warn": "Needs review", "bad": "Mismatch"}[level]

    report = {
        "meta": {"en_name": state.get("en_name", ""), "si_name": state.get("si_name", ""),
                 "en_count": m, "si_count": n,
                 "duration": round(max(en[-1]["e"] if en else 0, si[-1]["e"] if si else 0), 1),
                 "generated": time.strftime("%Y-%m-%d %H:%M:%S")},
        "verdict": {"level": level, "title": title, "reasons": reasons,
                    "coverage": round(cov_eff * 100, 1), "raw_coverage": round(coverage * 100, 1)},
        "stats": stats,
        "offset": {"global": round(g_off, 3), "confidence": round(conf, 2), "profile": profile},
        "tolerances": {"start": SYNC_TOL_START, "end": SYNC_TOL_END},
        "items": items,
    }
    stage(5)
    state["progress"] = 100.0
    return report


# ------------------------------------------------------------------------------
# SHARED UI KIT FOR ALL HTML PAGES (inline SVG icons, no emoji, works inside the Telegram Web App)
# ------------------------------------------------------------------------------
ICON_SPRITE = r"""<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="i-file" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></symbol>
<symbol id="i-film" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2.5"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/></symbol>
<symbol id="i-audio" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></symbol>
<symbol id="i-sub" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 15h4M15 15h2M7 11h2M13 11h4"/></symbol>
<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol>
<symbol id="i-drive" viewBox="0 0 24 24"><path d="M22 12H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><path d="M6 16h.01M10 16h.01"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m22 4-10 10.01-3-3"/></symbol>
<symbol id="i-tick" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7.5"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-zap" viewBox="0 0 24 24"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></symbol>
<symbol id="i-merge" viewBox="0 0 24 24"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></symbol>
<symbol id="i-scissors" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.12 15.88M14.47 14.48 20 20M8.12 8.12 12 12"/></symbol>
<symbol id="i-tag" viewBox="0 0 24 24"><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><path d="M7 7h.01"/></symbol>
<symbol id="i-bookmark" viewBox="0 0 24 24"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></symbol>
<symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></symbol>
<symbol id="i-chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-cpu" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></symbol>
<symbol id="i-expand" viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/></symbol>
<symbol id="i-gauge" viewBox="0 0 24 24"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></symbol>
<symbol id="i-star" viewBox="0 0 24 24"><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></symbol>
<symbol id="i-flag" viewBox="0 0 24 24"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/></symbol>
<symbol id="i-sync" viewBox="0 0 24 24"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></symbol>
<symbol id="i-pulse" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></symbol>
<symbol id="i-minus" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/></symbol>
<symbol id="i-plus" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/></symbol>
<symbol id="i-mega" viewBox="0 0 24 24"><path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/></symbol>
<symbol id="i-layers" viewBox="0 0 24 24"><path d="m12 2 10 5-10 5L2 7l10-5z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/></symbol>
<symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></symbol>
<symbol id="i-list" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></symbol>
<symbol id="i-box" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.27 6.96 8.73 5.05 8.73-5.05M12 22.08V12"/></symbol>
<symbol id="i-arrows" viewBox="0 0 24 24"><path d="M7 7h13M16 3l4 4-4 4M17 17H4M8 13l-4 4 4 4"/></symbol>
<symbol id="i-eye" viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></symbol>
<symbol id="i-image" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2.5"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></symbol>
<symbol id="i-type" viewBox="0 0 24 24"><path d="M4 7V4h16v3M9 20h6M12 4v16"/></symbol>
<symbol id="i-hash" viewBox="0 0 24 24"><path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/></symbol>
</defs></svg>"""

BASE_CSS = r"""
:root{--bg:#0b1020;--card:rgba(255,255,255,.055);--card2:rgba(255,255,255,.085);--line:rgba(255,255,255,.10);--text:#eaf0ff;--muted:#8d9bbd;
--accent:#6c8cff;--accent2:#22d3ee;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;--info:#38bdf8;--vio:#a78bfa;--org:#fb923c;--shadow:0 10px 30px rgba(0,0,0,.28)}
@media (prefers-color-scheme:light){:root{--bg:#f2f5fc;--card:rgba(255,255,255,.88);--card2:rgba(30,45,110,.07);--line:rgba(25,40,100,.12);--text:#111a33;--muted:#5b6990;--accent:#4a68f2;--accent2:#0891b2;--shadow:0 8px 24px rgba(40,60,130,.10)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI","Noto Sans Sinhala","Iskoola Pota",Roboto,sans-serif;min-height:100vh;
background:radial-gradient(900px 420px at 92% -8%,color-mix(in srgb,var(--accent) 24%,transparent),transparent 70%),radial-gradient(700px 420px at -8% 4%,color-mix(in srgb,var(--accent2) 16%,transparent),transparent 70%),var(--bg);background-attachment:fixed;padding-bottom:42px}
.wrap{max-width:960px;margin:0 auto;padding:16px 14px 0}
.ic{width:1.15em;height:1.15em;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.2em;flex:none}
.hero{position:relative;overflow:hidden;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 26%,var(--card)),var(--card));border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow);animation:rise .5s ease both}
.hero:after{content:"";position:absolute;right:-60px;top:-60px;width:180px;height:180px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--accent2) 35%,transparent),transparent 70%)}
.hero h1{font-size:18px;margin:0 0 4px;line-height:1.3;word-break:break-word;position:relative;z-index:1}.hero .sub{position:relative;z-index:1}
.hero .row{display:flex;gap:14px;align-items:center;position:relative;z-index:1}
.logo{width:52px;height:52px;border-radius:16px;display:grid;place-items:center;font-size:26px;color:#fff;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 20px color-mix(in srgb,var(--accent) 45%,transparent);flex:none}
.sub{color:var(--muted);font-size:12.5px;word-break:break-all}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;position:relative;z-index:1}
.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 11px;border-radius:99px;background:var(--card2);border:1px solid var(--line);font-size:12.5px;font-weight:600}
.chip .ic{color:var(--accent2)}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 15px;margin:12px 0;box-shadow:var(--shadow);backdrop-filter:blur(8px);animation:rise .5s ease both}
.sec{display:flex;align-items:center;gap:10px;margin:22px 2px 8px;font-weight:700;font-size:15px;letter-spacing:.2px}
.sec .badge-ic{width:30px;height:30px;border-radius:10px;display:grid;place-items:center;color:var(--c,var(--accent));background:color-mix(in srgb,var(--c,var(--accent)) 16%,transparent)}
.sec small{margin-left:auto;color:var(--muted);font-weight:600;font-size:12px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kv div{background:var(--card2);border-radius:13px;padding:9px 11px;min-width:0}
.kv span{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
.kv b{display:block;font-size:14px;margin-top:2px;word-break:break-word}
.pill{display:inline-flex;gap:6px;align-items:center;padding:6px 12px;border-radius:99px;font-weight:700;font-size:13px;border:1px solid transparent}
.pill.good{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
.pill.warn{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
.pill.bad{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
.pill.run{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent);border-color:color-mix(in srgb,var(--accent) 40%,transparent)}
.tag{display:inline-flex;gap:4px;align-items:center;padding:3px 9px;border-radius:99px;font-size:11.5px;font-weight:700;background:var(--card2);color:var(--muted)}
.tag.ok{color:var(--ok)}.tag.warn{color:var(--warn)}.tag.info{color:var(--info)}.tag.vio{color:var(--vio)}.tag.acc{color:var(--accent)}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
button{font:inherit}
"""

BASE_JS = r"""
var $=function(s,r){return (r||document).querySelector(s)}, $$=function(s,r){return [].slice.call((r||document).querySelectorAll(s))};
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function ic(n,cls){return '<svg class="ic '+(cls||'')+'"><use href="#i-'+n+'"/></svg>'}
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
function theme(){try{var tg=window.Telegram&&Telegram.WebApp;if(!tg)return;tg.ready();tg.expand();var p=tg.themeParams||{},r=document.documentElement.style;
 if(p.bg_color)r.setProperty('--bg',p.bg_color);if(p.text_color)r.setProperty('--text',p.text_color);if(p.hint_color)r.setProperty('--muted',p.hint_color);
 if(p.button_color)r.setProperty('--accent',p.button_color);}catch(e){}}
setTimeout(theme,60);setTimeout(theme,700);
"""

def _compose(template: str) -> str:
    return (template.replace("__ICONS__", ICON_SPRITE).replace("__BASE_CSS__", BASE_CSS).replace("__BASE_JS__", BASE_JS))

PAGES: Dict[str, Dict[str, Any]] = {}      # generated HTML pages served to the Telegram Web App (metadata etc.)

def register_page(html: str) -> str:
    token = secrets.token_urlsafe(10)
    PAGES[token] = {"html": html, "created": time.time()}
    return token

async def page_view(request: web.Request) -> web.Response:
    rec = PAGES.get(request.match_info["token"])
    if not rec:
        return web.Response(text="<h3>Page expired</h3>", content_type="text/html", status=404)
    return web.Response(text=rec["html"], content_type="text/html", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------
# SYNC REPORT UI (one self-contained HTML file; also served live to the Telegram Web App)
# ------------------------------------------------------------------------------
REPORT_HTML = r'''<!doctype html>
<html lang="si"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Sync Report</title>
<script src="https://telegram.org/js/telegram-web-app.js" async></script>
<style>__BASE_CSS__
.stages{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 4px}.stg{display:inline-flex;gap:5px;align-items:center;font-size:12px;padding:5px 10px;border-radius:99px;background:var(--card2);color:var(--muted);font-weight:600}
.stg.on{background:var(--accent);color:#fff}.stg.done{color:var(--ok)}
.bar{height:10px;border-radius:99px;background:var(--card2);overflow:hidden}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .4s}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px;cursor:pointer;display:flex;gap:11px;align-items:center;transition:transform .15s,border-color .15s;animation:rise .5s ease both}
.stat:hover{transform:translateY(-2px);border-color:var(--c,var(--accent))}.stat .bi{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-size:19px;color:var(--c,var(--accent));background:color-mix(in srgb,var(--c,var(--accent)) 16%,transparent);flex:none}
.stat b{display:block;font-size:20px;line-height:1.1}.stat span{color:var(--muted);font-size:12px}
.vd{display:flex;gap:14px;align-items:center}.ring{width:76px;height:76px;flex:none}.ring text{font:700 17px system-ui;fill:var(--text)}
.reasons{margin:8px 0 0;padding:0;list-style:none}.reasons li{display:flex;gap:8px;align-items:flex-start;color:var(--muted);font-size:13.5px;margin:5px 0}.reasons .ic{color:var(--warn);margin-top:3px}
canvas{width:100%;display:block;border-radius:12px;background:var(--card2)}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-top:8px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.tabs{display:flex;gap:7px;overflow-x:auto;padding:6px 0 10px;position:sticky;top:0;z-index:5;scrollbar-width:none;margin:0 -2px;backdrop-filter:blur(10px)}.tabs::-webkit-scrollbar{display:none}
.tab{display:inline-flex;gap:6px;align-items:center;white-space:nowrap;padding:8px 13px;border-radius:99px;background:var(--card);border:1px solid var(--line);color:var(--text);font-size:13px;font-weight:600;cursor:pointer}
.tab.on{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#fff}.tab em{font-style:normal;opacity:.85;font-size:12px}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:2px 0 10px}
.search{position:relative;flex:1;min-width:170px}.search .ic{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted)}
input[type=search]{width:100%;padding:10px 12px 10px 36px;border-radius:13px;border:1px solid var(--line);background:var(--card);color:var(--text);font:inherit}
label.chk{font-size:13px;color:var(--muted);display:flex;gap:7px;align-items:center}
.item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c,var(--muted));border-radius:15px;padding:11px 13px;margin:9px 0;animation:rise .35s ease both}
.item.t-timing{--c:var(--warn)}.item.t-missing{--c:var(--bad)}.item.t-extra{--c:var(--info)}.item.t-merged,.item.t-split{--c:var(--vio)}.item.t-ok{--c:var(--ok)}.item.t-overlap,.item.t-fast{--c:var(--org)}
.meta{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-bottom:7px;font-size:12px;color:var(--muted)}
.meta .ty{display:inline-flex;gap:5px;align-items:center;color:var(--c);font-weight:700}
.time{font-variant-numeric:tabular-nums;background:var(--card2);padding:2px 9px;border-radius:8px}
.ln{display:flex;gap:9px;margin:4px 0;align-items:flex-start}.ln b{flex:0 0 28px;font-size:10.5px;color:var(--muted);padding-top:4px;letter-spacing:.6px}
.ln div{flex:1;word-break:break-word}.ln small{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;padding-top:3px}
.ln.en div{color:var(--muted)}.ln.si div{font-weight:650}
button.more{width:100%;padding:12px;border-radius:14px;border:1px solid var(--line);background:var(--card);color:var(--accent);font-weight:700;margin:8px 0;cursor:pointer}
.empty{text-align:center;color:var(--muted);padding:30px 10px}.empty .ic{font-size:30px;color:var(--ok);display:block;margin:0 auto 8px}
.blocks{display:flex;flex-wrap:wrap;gap:7px;margin:0 0 8px}.blk{display:inline-flex;gap:6px;align-items:center;font-size:12px;padding:5px 10px;border-radius:10px;background:var(--card2);cursor:pointer;font-weight:600}
</style></head><body>__ICONS__<div class="wrap">
<div class="hero"><div class="row"><div class="logo"><svg class="ic"><use href="#i-sync"/></svg></div>
 <div style="min-width:0"><h1>Subtitle Sync Report</h1><div class="sub" id="names"></div></div></div>
 <div class="chips" id="hchips"></div></div>

<div class="card" id="live"><div class="bar"><i id="pbar"></i></div><div class="stages" id="stages"></div><div class="grid" id="livec"></div></div>

<div id="main" style="display:none">
 <div class="card" id="verdict"></div>
 <div class="grid" id="stats"></div>
 <div class="sec" style="--c:var(--accent2)"><span class="badge-ic"><svg class="ic"><use href="#i-pulse"/></svg></span>Timeline<small>tap to jump</small></div>
 <div class="card"><canvas id="tl" height="60"></canvas><div class="legend" id="lg"></div></div>
 <div id="profcard"><div class="sec" style="--c:var(--vio)"><span class="badge-ic"><svg class="ic"><use href="#i-clock"/></svg></span>Sync offset over time<small id="profnote"></small></div>
 <div class="card"><canvas id="prof" height="92"></canvas></div></div>
 <div class="sec" style="--c:var(--accent)"><span class="badge-ic"><svg class="ic"><use href="#i-list"/></svg></span>Lines</div>
 <div class="tabs" id="tabs"></div>
 <div class="tools"><div class="search"><svg class="ic"><use href="#i-search"/></svg><input type="search" id="q" placeholder="Search text or time"></div>
  <label class="chk"><input type="checkbox" id="hide" checked> Hide SDH / ads (English)</label></div>
 <div class="blocks" id="blocks"></div><div id="list"></div>
</div>
<div class="sub" style="text-align:center;margin-top:20px" id="foot"></div>
</div>
<script id="report-data" type="application/json">__REPORT__</script>
<script>
(function(){
__BASE_JS__
var STATE_URL=__STATE_URL__, REPORT=null, TAB='issues', PAGE=80, shown=PAGE;
var TY={timing:['clock','Timing','--warn'],missing:['minus','Missing','--bad'],extra:['plus','Extra','--info'],merged:['merge','Merged','--vio'],split:['scissors','Split','--vio'],
 overlap:['layers','Overlap','--org'],fast:['zap','Too fast','--org'],ok:['check','Matched','--ok']};
function fmt(t,ms){t=Math.max(t,0);var h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;
 return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+(ms?s.toFixed(1).padStart(4,'0'):String(Math.floor(s)).padStart(2,'0'))}
function sg(v){return (v>0?'+':'')+v.toFixed(2)+'s'}
function ring(p,col){var r=15.9,c=2*Math.PI*r,d=c*p/100;return '<svg class="ring" viewBox="0 0 36 36"><circle cx="18" cy="18" r="'+r+'" fill="none" stroke="var(--card2)" stroke-width="3.4"/>'+
 '<circle cx="18" cy="18" r="'+r+'" fill="none" stroke="'+col+'" stroke-width="3.4" stroke-linecap="round" stroke-dasharray="'+d+' '+c+'" transform="rotate(-90 18 18)"/>'+
 '<text x="18" y="21.5" text-anchor="middle" style="font-size:8.6px">'+Math.round(p)+'%</text></svg>'}
function names(a,b){return esc(a)+' '+ic('arrows')+' '+esc(b)}
function renderLive(st){
 $('#names').innerHTML=names(st.en_name||'',st.si_name||'');
 $('#pbar').style.width=(st.progress||0)+'%';
 $('#stages').innerHTML=(st.stages||[]).map(function(n,i){return '<span class="stg '+(i<st.stage?'done':i==st.stage?'on':'')+'">'+(i<st.stage?ic('tick'):'')+n+'</span>'}).join('');
 var c=st.counters||{};
 $('#livec').innerHTML=[['English cues',c.en,'file','--accent'],['Sinhala cues',c.si,'sub','--accent2'],['Matched',c.matched,'check','--ok'],['Missing',c.missing,'minus','--bad'],['Extra',c.extra,'plus','--info'],['Timing',c.timing,'clock','--warn']]
  .map(function(a){return '<div class="stat" style="--c:var('+a[3].slice(0)+')"><div class="bi">'+ic(a[2])+'</div><div><b>'+(a[1]==null?'-':a[1])+'</b><span>'+a[0]+'</span></div></div>'}).join('');
 if(st.error){$('#livec').innerHTML='<div class="empty">'+ic('alert')+esc(st.error)+'</div>'}
}
function counts(){var c={};REPORT.items.forEach(function(i){c[i.type]=(c[i.type]||0)+1});return c}
function visible(it){
 if(TAB==='issues'){if(['timing','missing','extra','overlap','fast'].indexOf(it.type)<0)return false}
 else if(TAB==='mergesplit'){if(it.type!=='merged'&&it.type!=='split')return false}
 else if(TAB==='all'){}else if(it.type!==TAB)return false;
 if($('#hide').checked&&it.type==='missing'&&(it.tags||[]).length)return false;
 var q=$('#q').value.trim().toLowerCase();
 if(q){var hay=(it.si.concat(it.en)).map(function(c){return c.text}).join(' ').toLowerCase()+' '+fmt(it.t,1);if(hay.indexOf(q)<0)return false}
 return true}
function cue(l,c){return '<div class="ln '+l.toLowerCase()+'"><b>'+l+'</b><div>'+esc(c.text)+'</div><small>'+fmt(c.s,1)+'</small></div>'}
function tg(cls,t,i){return '<span class="tag '+cls+'">'+(i?ic(i):'')+t+'</span>'}
function card(it){
 var ty=TY[it.type]||['info','',''],b='',T=REPORT.tolerances;
 if(it.ds!==undefined)b+=tg(Math.abs(it.ds)>T.start?'warn':'ok','start '+sg(it.ds))+tg(Math.abs(it.de)>T.end?'warn':'ok','end '+sg(it.de));
 if(it.kind==='merged')b+=tg('vio','1 SI / '+it.en.length+' EN','merge');
 if(it.kind==='split')b+=tg('vio',it.si.length+' SI / 1 EN','scissors');
 if(it.kind==='shifted')b+=tg('warn','shifted line','arrows');
 if(it.cps)b+=tg('warn',it.cps+' chars/s','zap');
 (it.tags||[]).forEach(function(t){b+=tg('info',t.toUpperCase(),t==='ad'?'mega':'tag')});
 var rows=it.en.map(function(c){return cue('EN',c)}).join('')+it.si.map(function(c){return cue('SI',c)}).join('');
 return '<div class="item t-'+it.type+'" id="it'+it.id+'"><div class="meta"><span class="ty">'+ic(ty[0])+ty[1]+'</span><span class="time">'+fmt(it.t,1)+'</span>'+b+'</div>'+rows+'</div>'}
function renderList(){
 var vis=REPORT.items.filter(visible);
 var html=vis.slice(0,shown).map(card).join('');
 if(!vis.length)html='<div class="empty">'+ic('check')+'Nothing to show here</div>';
 if(vis.length>shown)html+='<button class="more" id="more">Show more ('+(vis.length-shown)+' left)</button>';
 $('#list').innerHTML=html;var m=$('#more');if(m)m.onclick=function(){shown+=PAGE;renderList()};
 renderBlocks()}
function renderBlocks(){
 var items=REPORT.items.filter(function(i){return i.type===(TAB==='extra'?'extra':'missing')&&!(i.tags&&i.tags.length)});
 var out=[],cur=null;
 items.forEach(function(i){var e=(i.en[0]||i.si[0]).e;if(cur&&i.t-cur.e<8){cur.e=e;cur.n++}else{cur={s:i.t,e:e,n:1};out.push(cur)}});
 out=out.filter(function(b){return b.n>=3});
 $('#blocks').innerHTML=(TAB==='missing'||TAB==='extra'||TAB==='issues')?out.map(function(b){return '<span class="blk" data-t="'+b.s+'">'+ic(TAB==='extra'?'plus':'minus')+fmt(b.s)+' - '+fmt(b.e)+' ('+b.n+' lines)</span>'}).join(''):'';
 $$('.blk').forEach(function(e){e.onclick=function(){jump(parseFloat(e.dataset.t))}})}
function renderTabs(){
 var c=counts(),def=[['issues','alert','Issues',(c.timing||0)+(c.missing||0)+(c.extra||0)+(c.overlap||0)+(c.fast||0)],['timing','clock','Timing',c.timing],['missing','minus','Missing',c.missing],['extra','plus','Extra',c.extra],
  ['mergesplit','merge','Merged / Split',(c.merged||0)+(c.split||0)],['overlap','layers','Overlap',c.overlap],['fast','zap','Fast',c.fast],['ok','check','Matched',c.ok],['all','list','All',REPORT.items.length]];
 $('#tabs').innerHTML=def.filter(function(d){return d[3]||d[0]==='issues'}).map(function(d){return '<div class="tab '+(TAB===d[0]?'on':'')+'" data-k="'+d[0]+'">'+ic(d[1])+d[2]+'<em>'+(d[3]||0)+'</em></div>'}).join('');
 $$('.tab').forEach(function(e){e.onclick=function(){TAB=e.dataset.k;shown=PAGE;renderTabs();renderList()}})}
function jump(t){TAB='issues';shown=1e9;renderTabs();renderList();
 var best=null,bd=1e18;REPORT.items.forEach(function(i){if(visible(i)){var d=Math.abs(i.t-t);if(d<bd){bd=d;best=i}}});
 if(best){var el=document.getElementById('it'+best.id);if(el){el.scrollIntoView({behavior:'smooth',block:'center'});el.style.outline='2px solid '+css('--accent');setTimeout(function(){el.style.outline=''},1800)}}}
function sizeCanvas(c){var r=window.devicePixelRatio||1,w=c.clientWidth,h=parseInt(c.getAttribute('height'));c.width=w*r;c.height=h*r;var x=c.getContext('2d');x.scale(r,r);return[x,w,h]}
function drawTimeline(){
 var c=$('#tl'),a=sizeCanvas(c),x=a[0],w=a[1],h=a[2],D=Math.max(REPORT.meta.duration,1);
 x.clearRect(0,0,w,h);
 REPORT.items.forEach(function(i){if(i.type==='ok'){x.fillStyle=css('--ok');x.globalAlpha=.3;x.fillRect(i.t/D*w,h-14,Math.max(1,w/D*2),14)}});x.globalAlpha=1;
 var lanes={missing:[0,'--bad'],timing:[1,'--warn'],extra:[2,'--info'],overlap:[1,'--org'],fast:[1,'--org'],merged:[3,'--vio'],split:[3,'--vio']};
 REPORT.items.forEach(function(i){var l=lanes[i.type];if(!l||(i.tags&&i.tags.length&&i.type==='missing'))return;x.fillStyle=css(l[1]);x.fillRect(i.t/D*w,5+l[0]*9,Math.max(2,w/D*3),7)});
 c.onclick=function(e){var r=c.getBoundingClientRect();jump((e.clientX-r.left)/r.width*D)};
 $('#lg').innerHTML=[['--bad','Missing'],['--warn','Timing'],['--info','Extra'],['--vio','Merged / Split'],['--ok','Matched']].map(function(a){return '<span><i style="background:'+css(a[0])+'"></i>'+a[1]+'</span>'}).join('')}
function drawProfile(){
 var p=REPORT.offset.profile||[];
 if(p.length<2){$('#profcard').style.display='none';return}
 var c=$('#prof'),a=sizeCanvas(c),x=a[0],w=a[1],h=a[2],D=Math.max(REPORT.meta.duration,1);
 var ys=p.map(function(q){return q[1]}),mn=Math.min.apply(null,ys.concat([0])),mx=Math.max.apply(null,ys.concat([0])),pad=Math.max((mx-mn)*.2,.3);mn-=pad;mx+=pad;
 var Y=function(v){return h-10-(v-mn)/(mx-mn)*(h-20)};x.clearRect(0,0,w,h);
 x.strokeStyle=css('--muted');x.globalAlpha=.45;x.setLineDash([4,4]);x.beginPath();x.moveTo(0,Y(0));x.lineTo(w,Y(0));x.stroke();x.setLineDash([]);x.globalAlpha=1;
 var g=x.createLinearGradient(0,0,w,0);g.addColorStop(0,css('--accent'));g.addColorStop(1,css('--accent2'));x.strokeStyle=g;x.lineWidth=2.4;x.lineJoin='round';x.beginPath();
 p.forEach(function(q,i){var px=q[0]/D*w;i?x.lineTo(px,Y(q[1])):x.moveTo(px,Y(q[1]))});x.stroke();
 x.fillStyle=css('--muted');x.font='11px system-ui';x.fillText(mx.toFixed(1)+'s',8,13);x.fillText(mn.toFixed(1)+'s',8,h-5);
 var d=p[p.length-1][1]-p[0][1];$('#profnote').textContent=Math.abs(d)>.5?'drift '+sg(d):'stable'}
function renderReport(r){
 REPORT=r;$('#live').style.display='none';$('#main').style.display='block';
 $('#names').innerHTML=names(r.meta.en_name,r.meta.si_name);
 var v=r.verdict,s=r.stats,o=r.offset.global,col={good:'var(--ok)',warn:'var(--warn)',bad:'var(--bad)'}[v.level];
 $('#hchips').innerHTML='<span class="chip">'+ic('file')+'EN '+r.meta.en_count+' cues</span><span class="chip">'+ic('sub')+'SI '+r.meta.si_count+' cues</span><span class="chip">'+ic('clock')+'Offset '+sg(o)+'</span>'+
  '<span class="pill '+v.level+'">'+ic({good:'check',warn:'alert',bad:'x'}[v.level])+v.title+'</span>';
 $('#verdict').innerHTML='<div class="vd">'+ring(v.coverage,col)+'<div style="min-width:0"><b style="font-size:16px">'+({good:'Looks good',warn:'Needs review',bad:'Does not match well'}[v.level])+'</b>'+
  '<div class="sub">Coverage of English dialogue by the Sinhala file</div></div></div>'+
  (v.reasons.length?'<ul class="reasons">'+v.reasons.map(function(t){return '<li>'+ic('alert')+'<span>'+esc(t)+'</span></li>'}).join('')+'</ul>':'<div class="sub" style="margin-top:8px">No problems found.</div>')+
  (r.meta.skipped?'<div class="sub" style="margin-top:8px">'+ic('alert')+' Could not parse '+r.meta.skipped+' block(s) in the files.</div>':'');
 var cards=[['Coverage',v.coverage+'%','check',v.level==='good'?'--ok':v.level==='warn'?'--warn':'--bad','issues'],['Matched',s.ok+s.merged+s.split+s.timing,'merge','--ok','ok'],['Missing',s.missing,'minus',s.missing?'--bad':'--ok','missing'],
  ['Extra',s.extra,'plus',s.extra?'--info':'--ok','extra'],['Ads / credits',s.ads,'mega','--info','extra'],['Timing off',s.timing,'clock',s.timing?'--warn':'--ok','timing'],['Offset',sg(o),'sync',Math.abs(o)>=.3?'--warn':'--ok','issues']];
 $('#stats').innerHTML=cards.map(function(a){return '<div class="stat" data-k="'+a[4]+'" style="--c:var('+a[3]+')"><div class="bi">'+ic(a[2])+'</div><div><b>'+a[1]+'</b><span>'+a[0]+'</span></div></div>'}).join('');
 $$('.stat[data-k]').forEach(function(e){e.onclick=function(){TAB=e.dataset.k;shown=PAGE;renderTabs();renderList();$('#tabs').scrollIntoView({behavior:'smooth'})}});
 $('#foot').textContent='Generated '+r.meta.generated+'  |  tolerance start '+r.tolerances.start+'s / end '+r.tolerances.end+'s';
 renderTabs();renderList();drawTimeline();drawProfile();
 $('#q').oninput=function(){shown=PAGE;renderList()};$('#hide').onchange=function(){shown=PAGE;renderList()};
 window.addEventListener('resize',function(){drawTimeline();drawProfile()})}
function poll(){fetch(STATE_URL,{cache:'no-store'}).then(function(r){return r.json()}).then(function(st){
  if(st.status==='done'&&st.report){renderReport(st.report)}else{renderLive(st);setTimeout(poll,900)}}).catch(function(){setTimeout(poll,2000)})}
var el=document.getElementById('report-data'),emb=null;try{emb=JSON.parse(el.textContent)}catch(e){}
if(emb){renderReport(emb)}else if(STATE_URL){renderLive({stages:['Parse','Offset','Match','Classify','Report'],stage:0,progress:0,counters:{}});poll()}
else{$('#live').innerHTML='<div class="empty">No data</div>'}
})();
</script></body></html>'''

def render_report_html(report: Optional[Dict[str, Any]], state_url: Optional[str] = None) -> str:
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/") if report else "null"
    return _compose(REPORT_HTML).replace("__REPORT__", payload).replace("__STATE_URL__", json.dumps(state_url))

def new_sync_state(en_name: str, si_name: str, en_n: int, si_n: int) -> Dict[str, Any]:
    return {
        "status": "running", "stage": 0, "stages": SYNC_STAGES, "progress": 0.0, "error": "",
        "en_name": en_name, "si_name": si_name, "report": None,
        "counters": {"en": en_n, "si": si_n, "matched": 0, "missing": 0, "extra": 0, "timing": 0},
    }

async def sync_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    rec = SYNC_REPORTS.get(token)
    if not rec:
        return web.Response(text="<h3>Report expired or not found</h3>", content_type="text/html", status=404)
    st = rec["state"]
    html = render_report_html(st["report"] if st["status"] == "done" else None, f"/sync/{token}/state")
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

async def sync_state(request: web.Request) -> web.Response:
    rec = SYNC_REPORTS.get(request.match_info["token"])
    if not rec:
        return web.json_response({"status": "error", "error": "expired"}, status=404)
    return web.json_response(rec["state"], dumps=lambda o: json.dumps(o, ensure_ascii=False),
                             headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------------------
# METADATA PAGE (pretty HTML, opens in the Telegram Web App) – replaces the ugly JSON dump
# ------------------------------------------------------------------------------
def _mbps(v: Any) -> str:
    n = _to_int(v)
    if not n:
        return "-"
    return f"{n / 1_000_000:.2f} Mbps" if n >= 1_000_000 else f"{round(n / 1000)} kbps"

def _aspect(w: int, h: int, dar: str) -> str:
    if dar and dar not in ("0:1", "N/A"):
        return dar
    if not w or not h:
        return "-"
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"

CODEC_PRETTY = {
    "h264": "H.264 / AVC", "hevc": "HEVC / H.265", "vp9": "VP9", "av1": "AV1", "mpeg4": "MPEG-4 Part 2", "mpeg2video": "MPEG-2",
    "aac": "AAC (LC)", "ac3": "AC-3 (Dolby Digital)", "eac3": "E-AC-3 (Dolby Digital Plus)", "dts": "DTS", "truehd": "Dolby TrueHD",
    "mp3": "MP3", "opus": "Opus", "flac": "FLAC", "vorbis": "Vorbis", "alac": "Apple Lossless",
    "subrip": "SubRip (SRT)", "ass": "ASS / SSA", "ssa": "SSA", "mov_text": "MP4 Timed Text", "webvtt": "WebVTT",
    "hdmv_pgs_subtitle": "PGS (Blu-ray image)", "dvd_subtitle": "VobSub (DVD image)", "dvb_subtitle": "DVB (image)",
}

def build_meta_report(raw: Dict[str, Any], name: str, size: int) -> Dict[str, Any]:
    fmt = raw.get("format", {}) or {}
    streams = raw.get("streams", []) or []
    dur = float(fmt.get("duration") or 0)
    ftags = {str(k): str(v) for k, v in (fmt.get("tags") or {}).items()}

    def tl(s):
        return {str(k).lower(): str(v) for k, v in (s.get("tags") or {}).items()}

    video, audio, subs, adv = [], [], [], []
    a_sum = sum(_to_int(x.get("bit_rate")) or _to_int((x.get("tags") or {}).get("BPS")) for x in streams if x.get("codec_type") == "audio")
    for s in streams:
        ctype, t, disp = s.get("codec_type"), tl(s), s.get("disposition") or {}
        if ctype == "video" and not disp.get("attached_pic"):
            ct = str(s.get("color_transfer", ""))
            hdr = "HDR10 (PQ)" if "smpte2084" in ct else ("HLG" if "arib" in ct else "SDR")
            depth = s.get("bits_per_raw_sample") or ("10" if "10" in str(s.get("pix_fmt", "")) else "8")
            rows = [
                ["Codec", CODEC_PRETTY.get(s.get("codec_name"), str(s.get("codec_long_name") or s.get("codec_name", "-")))],
                ["Profile", f"{s.get('profile', '-')}" + (f" @ L{int(s['level']) / 10:.1f}" if s.get("level") not in (None, 0, -99) else "")],
                ["Resolution", f"{s.get('width')} x {s.get('height')}"],
                ["Aspect ratio", _aspect(_to_int(s.get("width")), _to_int(s.get("height")), str(s.get("display_aspect_ratio", "")))],
                ["Frame rate", f"{fmt_fps(_to_fps(s.get('avg_frame_rate', '0')))} fps"],
                ["Bit rate", _mbps(s.get("bit_rate") or t.get("bps")) if (s.get("bit_rate") or t.get("bps")) else
                 (("~" + _mbps(max(_to_int(fmt.get("bit_rate")) - a_sum, 0))) if _to_int(fmt.get("bit_rate")) else "-")],
                ["Pixel format", str(s.get("pix_fmt", "-"))],
                ["Bit depth", f"{depth} bit"],
                ["Dynamic range", hdr],
                ["Colour", " / ".join(x for x in (s.get("color_space"), s.get("color_range")) if x) or "Unspecified"],
                ["Scan", str(s.get("field_order", "progressive")).replace("unknown", "progressive")],
            ]
            if s.get("nb_frames") not in (None, "N/A"):
                rows.append(["Frames", str(s.get("nb_frames"))])
            video.append({"title": "Video", "rows": rows,
                          "flags": ["default"] if disp.get("default") else []})
        elif ctype == "audio":
            ch = _to_int(s.get("channels"))
            br = _to_int(s.get("bit_rate")) or _to_int(t.get("bps")) or _to_int(t.get("bps-eng"))
            rows = [
                ["Codec", CODEC_PRETTY.get(s.get("codec_name"), str(s.get("codec_long_name") or s.get("codec_name", "-")))],
                ["Channels", f"{CHANNEL_LABEL.get(ch, ch)}  ({ch} ch)"],
                ["Sample rate", f"{s.get('sample_rate', '-')} Hz"],
                ["Bit rate", _mbps(br)],
            ]
            if s.get("profile"):
                rows.insert(1, ["Profile", str(s["profile"])])
            if s.get("bits_per_raw_sample") not in (None, "N/A", "0"):
                rows.append(["Bit depth", f"{s.get('bits_per_raw_sample')} bit"])
            lang = lang_name(t.get("language", ""))
            audio.append({"title": lang, "subtitle": t.get("title", ""), "rows": rows,
                          "code": (t.get("language") or "und")[:3].upper(),
                          "flags": [f for f, on in (("default", disp.get("default")), ("forced", disp.get("forced"))) if on]})
        elif ctype == "subtitle":
            codec = str(s.get("codec_name", ""))
            ext, _ = sub_extract_plan({"codec": codec})
            kind = "Text" if codec in TEXT_SUB_CODECS else "Image based"
            rows = [["Codec", CODEC_PRETTY.get(codec, codec or "-")], ["Type", kind],
                    ["Extract as", f".{ext}" if ext else "not possible"]]
            subs.append({"title": lang_name(t.get("language", "")), "subtitle": t.get("title", ""), "rows": rows,
                         "code": (t.get("language") or "und")[:3].upper(), "text": codec in TEXT_SUB_CODECS,
                         "flags": [f for f, on in (("default", disp.get("default")), ("forced", disp.get("forced")),
                                                   ("sdh", disp.get("hearing_impaired"))) if on]})
        # advanced: every field of every stream as a plain table
        flat = []
        for k, v in s.items():
            if isinstance(v, dict):
                flat += [[f"{k}.{kk}", str(vv)] for kk, vv in v.items()]
            else:
                flat.append([k, str(v)])
        adv.append({"title": f"Stream #{s.get('index')}  -  {ctype}  ({s.get('codec_name', '')})", "rows": flat})
    adv.insert(0, {"title": "Container", "rows": [[k, str(v)] for k, v in fmt.items() if not isinstance(v, dict)]})

    chapters = []
    for c in raw.get("chapters", []) or []:
        chapters.append({"title": (c.get("tags") or {}).get("title", f"Chapter {len(chapters) + 1}"),
                         "start": fmt_hms(float(c.get("start_time") or 0))})

    return {
        "file": {"name": name, "size": humanbytes(size), "container": str(fmt.get("format_long_name") or fmt.get("format_name", "-")),
                 "duration": fmt_hms(dur), "bitrate": _mbps(fmt.get("bit_rate")), "streams": len(streams),
                 "title": ftags.get("title") or ftags.get("TITLE", "")},
        "video": video, "audio": audio, "subs": subs, "chapters": chapters,
        "tags": [[k, v] for k, v in ftags.items()], "adv": adv,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

META_HTML = r'''<!doctype html>
<html lang="si"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Media Info</title>
<script src="https://telegram.org/js/telegram-web-app.js" async></script>
<style>__BASE_CSS__
.track{display:flex;gap:12px;align-items:flex-start}
.lang{flex:none;min-width:46px;height:46px;padding:0 6px;border-radius:14px;display:grid;place-items:center;font-weight:800;font-size:12.5px;letter-spacing:.5px;color:#fff;background:linear-gradient(135deg,var(--c,var(--accent)),color-mix(in srgb,var(--c,var(--accent)) 55%,#000))}
.track h3{margin:0;font-size:15.5px;line-height:1.3}.track .t{color:var(--muted);font-size:12.5px;word-break:break-word}
.flags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.card .kv{margin-top:12px}
details{background:var(--card);border:1px solid var(--line);border-radius:16px;margin:9px 0;overflow:hidden}
summary{cursor:pointer;list-style:none;padding:12px 14px;font-weight:650;font-size:13.5px;display:flex;gap:9px;align-items:center}summary::-webkit-details-marker{display:none}
summary .ic:last-child{margin-left:auto;transition:transform .2s;color:var(--muted)}details[open] summary .ic:last-child{transform:rotate(180deg)}
table{width:100%;border-collapse:collapse;font-size:12.5px}td{padding:6px 14px;border-top:1px solid var(--line);vertical-align:top;word-break:break-word}td:first-child{color:var(--muted);width:42%}
.chap{display:flex;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}.chap:first-child{border-top:0}
.chap time{font-variant-numeric:tabular-nums;background:var(--card2);padding:2px 9px;border-radius:8px;font-size:12.5px;color:var(--accent2);font-weight:700}
</style></head><body>__ICONS__<div class="wrap">
<div class="hero"><div class="row"><div class="logo"><svg class="ic"><use href="#i-film"/></svg></div>
 <div style="min-width:0"><h1 id="fname"></h1><div class="sub" id="ftitle"></div></div></div><div class="chips" id="chips"></div></div>
<div id="out"></div>
<div class="sub" style="text-align:center;margin-top:22px" id="foot"></div></div>
<script id="meta-data" type="application/json">__META__</script>
<script>
(function(){
__BASE_JS__
var M=JSON.parse(document.getElementById('meta-data').textContent);
function kv(rows){return '<div class="kv">'+rows.map(function(r){return '<div><span>'+esc(r[0])+'</span><b>'+esc(r[1])+'</b></div>'}).join('')+'</div>'}
function sec(icon,color,title,count){return '<div class="sec" style="--c:var('+color+')"><span class="badge-ic">'+ic(icon)+'</span>'+title+(count!=null?'<small>'+count+'</small>':'')+'</div>'}
function flag(f){var m={default:['star','ok'],forced:['flag','warn'],sdh:['audio','info']}[f]||['tag','acc'];return '<span class="tag '+m[1]+'">'+ic(m[0])+f+'</span>'}
var F=M.file;document.getElementById('fname').textContent=F.name;document.getElementById('ftitle').textContent=(F.title?F.title+'  |  ':'')+F.container;
document.getElementById('chips').innerHTML=[['clock',F.duration],['drive',F.size],['pulse',F.bitrate],['layers',F.streams+' streams']]
 .map(function(c){return '<span class="chip">'+ic(c[0])+esc(c[1])+'</span>'}).join('');
var h='';
if(M.video.length){h+=sec('film','--accent','Video',M.video.length);M.video.forEach(function(v){
 h+='<div class="card"><div class="track"><div class="lang" style="--c:var(--accent)">'+ic('film')+'</div><div><h3>'+esc(v.rows[0][1])+'</h3><div class="t">'+esc(v.rows[2][1])+'  |  '+esc(v.rows[4][1])+'</div></div></div>'+kv(v.rows)+'</div>'})}
if(M.audio.length){h+=sec('audio','--ok','Audio',M.audio.length);M.audio.forEach(function(a){
 h+='<div class="card"><div class="track"><div class="lang" style="--c:var(--ok)">'+esc(a.code)+'</div><div style="min-width:0"><h3>'+esc(a.title)+'</h3>'+(a.subtitle?'<div class="t">'+esc(a.subtitle)+'</div>':'')+
 '<div class="flags"><span class="tag acc">'+esc(a.rows[0][1].split('(')[0])+'</span>'+a.flags.map(flag).join('')+'</div></div></div>'+kv(a.rows)+'</div>'})}
if(M.subs.length){h+=sec('sub','--vio','Subtitles',M.subs.length);M.subs.forEach(function(a){
 h+='<div class="card"><div class="track"><div class="lang" style="--c:var(--vio)">'+esc(a.code)+'</div><div style="min-width:0"><h3>'+esc(a.title)+'</h3>'+(a.subtitle?'<div class="t">'+esc(a.subtitle)+'</div>':'')+
 '<div class="flags"><span class="tag '+(a.text?'ok':'warn')+'">'+ic(a.text?'type':'image')+(a.text?'Text':'Image')+'</span>'+a.flags.map(flag).join('')+'</div></div></div>'+kv(a.rows)+'</div>'})}
if(M.chapters.length){h+=sec('bookmark','--warn','Chapters',M.chapters.length)+'<div class="card">'+M.chapters.map(function(c){return '<div class="chap"><time>'+esc(c.start)+'</time><span>'+esc(c.title)+'</span></div>'}).join('')+'</div>'}
if(M.tags.length){h+=sec('tag','--info','Tags')+'<div class="card">'+kv(M.tags)+'</div>'}
h+=sec('cpu','--org','All fields','advanced');
M.adv.forEach(function(g){h+='<details><summary>'+ic('hash')+esc(g.title)+ic('chev')+'</summary><table>'+g.rows.map(function(r){return '<tr><td>'+esc(r[0])+'</td><td>'+esc(r[1])+'</td></tr>'}).join('')+'</table></details>'});
document.getElementById('out').innerHTML=h;document.getElementById('foot').textContent='Generated '+M.generated;
})();
</script></body></html>'''

def render_meta_html(meta: Dict[str, Any]) -> str:
    payload = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    return _compose(META_HTML).replace("__META__", payload)


# ------------------------------------------------------------------------------
# TRACK NAMES (by language) + TRACK SELECTION BEFORE EVERY HARDCODE / EMBED
# ------------------------------------------------------------------------------
import html as htmlmod

LANG2TO3 = {"en": "eng", "hi": "hin", "ta": "tam", "te": "tel", "si": "sin", "ml": "mal", "kn": "kan", "ja": "jpn",
            "ko": "kor", "ur": "urd", "bn": "ben", "mr": "mar", "pa": "pan", "gu": "guj", "fr": "fra", "es": "spa",
            "de": "deu", "ru": "rus", "zh": "zho", "ar": "ara", "th": "tha", "it": "ita", "pt": "por", "tr": "tur"}

def lang_name(code: str) -> str:
    c = (code or "").strip().lower()
    c3 = LANG2TO3.get(c, c[:3])
    name = LANG_NAMES.get(c3)
    if name:
        return name.title()
    return c3.upper() if c3 and c3 != "und" else "Unknown"

def safe_filename(s: str, limit: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip() or "track"

def _audio_stem(t: Dict[str, Any]) -> str:
    title = (t["title"] or "").strip()
    if title and re.search(r"\d", title):
        return safe_filename(title)
    base = title or lang_name(t["lang"])
    codec = AUDIO_CODEC_LABEL.get(t["codec"], t["codec"].upper())
    ch = CHANNEL_LABEL.get(t["channels"], f"{t['channels']}.0" if t["channels"] else "")
    parts = [base, f"{codec}{ch}".strip()]
    if t["bitrate"]:
        parts.append(f"{round(t['bitrate'] / 1000)}kbps")
    return safe_filename(" ".join(p for p in parts if p))

def _sub_stem(t: Dict[str, Any]) -> str:
    title = (t["title"] or "").strip()
    base = title if title and title.lower() not in ("subrip", "srt", "ass", "ssa") else lang_name(t["lang"])
    if t.get("forced") and "forced" not in base.lower():
        base += " (Forced)"
    if t.get("sdh") and "sdh" not in base.lower():
        base += " (SDH)"
    return safe_filename(base)

def track_stems(kind: str, tracks: list) -> list:
    """File name stems built from the track itself (language / title), made unique: 'English', 'English 2' ..."""
    stems = [(_audio_stem(t) if kind == "a" else _sub_stem(t)) for t in tracks]
    seen: Dict[str, int] = {}
    out = []
    for s_ in stems:
        seen[s_] = seen.get(s_, 0) + 1
        out.append(s_ if stems.count(s_) == 1 else f"{s_} {seen[s_]}")
    return out

# ---------------- selection UI ----------------
def tracks_view(info: Dict[str, Any], ext: str, keep_a: set, keep_s: set, action: str) -> Tuple[str, InlineKeyboardMarkup]:
    E = htmlmod.escape
    title = {"hardcode": "Hardcode", "embed": "Embed Sub"}.get(action, action)
    lines = [f"<b>Track selection</b>  -  {title}",
             f"Container: <b>{ext.upper().lstrip('.')}</b>"]
    if ext != ".mkv" and any(t["codec"] not in TEXT_SUB_CODECS for t in info["subs"]):
        lines.append("<i>MP4 / MOV cannot hold image subtitles (PGS / VobSub); they are skipped.</i>")
    rows = []
    if info["audio"]:
        lines.append("\n<b>Audio</b>")
        for i, t in enumerate(info["audio"][:30]):
            ch = CHANNEL_LABEL.get(t["channels"], str(t["channels"]))
            br = f" - {round(t['bitrate'] / 1000)} kbps" if t["bitrate"] else ""
            ttl = f'  "{E(t["title"])}"' if t["title"] else ""
            on = i in keep_a
            lines.append(f"{'[x]' if on else '[ ]'} A{i + 1}  {E(lang_name(t['lang']))} - {t['codec'].upper()} {ch}{br}{ttl}")
            rows.append([InlineKeyboardButton(f"{'✅' if on else '⬜'} A{i + 1} {lang_name(t['lang'])} {t['codec'].upper()} {ch}",
                                              callback_data=f"tk:a:{i}")])
    if info["subs"]:
        lines.append("\n<b>Subtitles</b>")
        for i, t in enumerate(info["subs"][:30]):
            flags = "".join(f" [{f}]" for f, on_ in (("forced", t.get("forced")), ("default", t.get("default")), ("sdh", t.get("sdh"))) if on_)
            ttl = f'  "{E(t["title"])}"' if t["title"] else ""
            on = i in keep_s
            lines.append(f"{'[x]' if on else '[ ]'} S{i + 1}  {E(lang_name(t['lang']))} - {E(t['codec'])}{flags}{ttl}")
            rows.append([InlineKeyboardButton(f"{'✅' if on else '⬜'} S{i + 1} {lang_name(t['lang'])} {t['codec']}",
                                              callback_data=f"tk:s:{i}")])
    lines.append("\nChoose the tracks to keep (✅ = keep, ⬜ = remove).")
    bulk = []
    if info["audio"]:
        bulk += [InlineKeyboardButton("All audio", callback_data="tk:aa"), InlineKeyboardButton("No audio", callback_data="tk:an")]
    if info["subs"]:
        bulk += [InlineKeyboardButton("All subs", callback_data="tk:sa"), InlineKeyboardButton("No subs", callback_data="tk:sn")]
    if bulk:
        rows.append(bulk[:2])
        if len(bulk) > 2:
            rows.append(bulk[2:])
    rows.append([InlineKeyboardButton("▶ Continue", callback_data="tk:go"), InlineKeyboardButton("✖ Cancel", callback_data="tk:x")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)

async def ask_tracks(client: Client, chat_id: int, user_id: int, spec: Dict[str, Any], message: Optional[Message] = None) -> None:
    lib, sess = lib_get(user_id), USER_DATA.get(user_id)
    if not lib or not sess:
        return
    info = lib["info"]
    if not info["audio"] and not info["subs"]:
        sess["tk"] = {"audio": set(), "subs": set(), "spec": spec}
        await start_after_tracks(client, chat_id, user_id, sess)
        return
    keep_a = sess.get("keep_audio")
    keep_a = set(range(len(info["audio"]))) if keep_a is None else set(keep_a)
    keep_s = set(range(len(info["subs"]))) if spec["type"] == "embed" else set()
    sess["tk"] = {"audio": keep_a, "subs": keep_s, "spec": spec}
    text, kb = tracks_view(info, choose_container(lib["name"]), keep_a, keep_s, spec["type"])
    if message is not None:
        await safe_edit(message, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await client.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def start_after_tracks(client: Client, chat_id: int, user_id: int, sess: Dict[str, Any]) -> None:
    tk = sess.get("tk")
    if not tk:
        return
    spec = tk["spec"]
    sess["keep_audio"], sess["keep_subs"] = set(tk["audio"]), set(tk["subs"])
    job = begin_job(user_id, spec["type"])
    if not job:
        await client.send_message(chat_id, "⏳ **A job is already running.**")
        return
    job["outcome"] = "failed"
    job["stage"] = "Getting ready"
    retry_spec = {"kind": spec["type"], "start": int(spec.get("start", 0)), "end": spec.get("end"),
                  "speed": sess.get("speed", DEFAULT_SPEED), "add_watermark": bool(sess.get("add_watermark")),
                  "keep_audio": sorted(sess.get("keep_audio") or []),
                  "keep_subs": sorted(sess.get("keep_subs") or []), "delay_ms": int(sess.get("delay_ms", 0))}
    dashboard_history_set_retry(job, retry_spec)
    lib = lib_get(user_id)
    if lib and spec["type"] in ("hardcode", "embed"):
        ext = choose_container(lib["name"])
        default_name = (ve.build_output_filename(lib["name"], ext, ve.get_name_settings(user_id))
                        if spec["type"] == "hardcode" else Path(lib["name"]).stem + ext)
        job["default_output_name"] = default_name
        web_name = str(sess.pop("next_output_name", "") or "").strip()
        if web_name:
            selected = sanitize_output_filename(web_name, default_name)
            if selected:
                job["selected_output_name"] = selected
                try:
                    await client.send_message(chat_id, f"✅ Name chosen in the Web App: <code>{htmlmod.escape(selected)}</code>",
                                              parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        if not job.get("selected_output_name"):
            try:
                await ask_output_name(client, chat_id, user_id, default_name, job)
            except Exception as e:
                logger.warning(f"Could not show optional output-name prompt: {e}")
    if spec["type"] == "embed":
        spawn(embed_job(client, chat_id, user_id, sess, job))
    else:
        spawn(process_job(client, chat_id, user_id, sess, spec.get("start", 0), spec.get("end"), job))

async def tracks_callback(client: Client, callback: CallbackQuery, data: str, user_id: int) -> None:
    sess, lib = USER_DATA.get(user_id), lib_get(user_id)
    if not sess or not lib or not sess.get("tk"):
        await callback.answer("⚠️ Session expired.", show_alert=True)
        return
    tk, info = sess["tk"], lib["info"]
    if data == "tk:x":
        sess.pop("tk", None)
        await safe_edit(callback.message, "✖ Cancelled.")
        await callback.answer()
        return
    if data == "tk:go":
        msg = callback.message
        n_a, n_s = len(tk["audio"]), len(tk["subs"])
        await safe_edit(msg, f"✅ Tracks: audio {n_a}, subtitles {n_s}. Starting...")
        await start_after_tracks(client, msg.chat.id, user_id, sess)
        await callback.answer()
        return
    parts = data.split(":")
    if parts[1] in ("a", "s"):
        key, i = ("audio" if parts[1] == "a" else "subs"), int(parts[2])
        tk[key] ^= {i}
    elif parts[1] == "aa":
        tk["audio"] = set(range(len(info["audio"])))
    elif parts[1] == "an":
        tk["audio"] = set()
    elif parts[1] == "sa":
        tk["subs"] = set(range(len(info["subs"])))
    elif parts[1] == "sn":
        tk["subs"] = set()
    text, kb = tracks_view(info, choose_container(lib["name"]), tk["audio"], tk["subs"], tk["spec"]["type"])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()


# ------------------------------------------------------------------------------
# DELAY FIX: live preview of the video's own subtitle vs. the user's SRT, button-driven offset, demo clips
# ------------------------------------------------------------------------------
DELAY_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(ms|s)?\s*$", re.I)
DELAY_MAX_MS = 600_000
HMS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")

def fmt_delay(ms: int) -> str:
    return f"{ms / 1000:+.2f}s"

def parse_delay_text(text: str) -> Optional[int]:
    m = DELAY_RE.match(text or "")
    if not m:
        return None
    val, unit = float(m.group(1)), (m.group(2) or "").lower()
    if not unit:
        unit = "s" if ("." in m.group(1) or abs(val) <= 120) else "ms"
    ms = round(val) if unit == "ms" else round(val * 1000)
    return max(-DELAY_MAX_MS, min(DELAY_MAX_MS, ms))

def hms_ms(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(int(t * 1000 + 0.5), 3_600_000)
    m, rem = divmod(rem, 60_000)
    return f"{h:02d}:{m:02d}:{rem / 1000:06.3f}"

def delay_preview_html(user_cues: list, ref_cues: Optional[list], ref_label: str, delay_ms: int, page: int, per: int = 5) -> str:
    E = htmlmod.escape
    d = delay_ms / 1000
    u = [c for c in user_cues if not c["positioned"]]
    def line(c, shift=0.0):
        txt = c["text"] if len(c["text"]) <= 44 else c["text"][:43] + "…"
        return f"<code>{hms_ms(c['s'] + shift)}</code> {E(txt)}"
    out = [f"<b>Delay Fix</b>   <code>{fmt_delay(delay_ms)}</code>"]
    first_ref = first_user = None
    if ref_cues:
        total = len(ref_cues)
        a = page * per
        shown_ref = ref_cues[a:a + per]
        out.append(f"Reference: <b>{E(ref_label)}</b>  -  lines {a + 1}-{a + len(shown_ref)} of {total}")
        out.append("\n<b>VIDEO subtitle (reference)</b>")
        out += [line(c) for c in shown_ref] or ["(end of track)"]
        if shown_ref:
            t0, t1 = shown_ref[0]["s"] - 2.5, shown_ref[-1]["e"] + 2.5
            shown_user = [c for c in u if t0 <= c["s"] + d <= t1][: per + 2]
            first_ref = shown_ref[0]["s"]
        else:
            shown_user = []
    else:
        out.append("<i>This video has no text subtitle track - test with the Demo clip.</i>")
        a = page * per
        shown_user = u[a:a + per]
    out.append("\n<b>YOUR SRT (delay applied)</b>")
    out += [line(c, d) for c in shown_user] or ["(no lines in this time range - change the time)"]
    if shown_user:
        first_user = shown_user[0]["s"] + d
    if first_ref is not None and first_user is not None:
        out.append(f"\nDifference between the first lines: <b>{first_user - first_ref:+.2f}s</b>  (both should start together)")
    out.append("\n<i>Type an exact value: +1.25  |  -0.4s  |  1500ms</i>")
    return "\n".join(out)

def delay_keyboard(dl: Dict[str, Any], info: Dict[str, Any]) -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    rows = [
        [B("-10s", callback_data="dl:d:-10000"), B("-1s", callback_data="dl:d:-1000"),
         B("-0.5s", callback_data="dl:d:-500"), B("-0.1s", callback_data="dl:d:-100")],
        [B("+0.1s", callback_data="dl:d:100"), B("+0.5s", callback_data="dl:d:500"),
         B("+1s", callback_data="dl:d:1000"), B("+10s", callback_data="dl:d:10000")],
        [B("🎯 Auto detect", callback_data="dl:auto"), B("↺ Reset", callback_data="dl:reset")],
    ]
    refs = [i for i, t in enumerate(info["subs"]) if t["codec"] in TEXT_SUB_CODECS][:8]
    if refs:
        btns = [B(("● " if dl.get("ref") == i else "") + f"S{i + 1} {(info['subs'][i]['lang'] or 'und')[:3].upper()}",
                  callback_data=f"dl:ref:{i}") for i in refs]
        for k in range(0, len(btns), 4):
            rows.append(btns[k:k + 4])
        rows.append([B("◀ Prev", callback_data="dl:pg:-1"), B("Next ▶", callback_data="dl:pg:1")])
    rows.append([B("🎬 Demo clip", callback_data="dl:demo"), B("💾 Get SRT", callback_data="dl:srt")])
    rows.append([B("✅ Apply", callback_data="dl:apply"), B("✖ Close", callback_data="dl:close")])
    return InlineKeyboardMarkup(rows)

def demo_keyboard(dl: Dict[str, Any]) -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    ln = dl.get("demo_len", 60)
    return InlineKeyboardMarkup([
        [B(f"{p}%", callback_data=f"dl:dm:{p}") for p in (5, 25, 50, 75, 90)],
        [B(("✓ " if ln == s else "") + f"{s}s", callback_data=f"dl:dl:{s}") for s in (30, 60, 90)],
        [B("✏️ Custom time", callback_data="dl:dc"), B("◀ Back", callback_data="dl:back")],
    ])

def demo_result_keyboard() -> InlineKeyboardMarkup:
    B = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [B("🔥 Hardcode Full", callback_data="dl:full"), B("🧩 Embed Full", callback_data="dl:embedfull")],
        [B("⏱ Fix Delay", callback_data="dl_open"), B("🎬 New Demo", callback_data="dl:demo")],
    ])

async def dl_edit(client: Client, dl: Dict[str, Any], text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await client.edit_message_text(dl["chat"], dl["msg"], text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except RPCError:
        pass

def ref_label_of(info: Dict[str, Any], n: Optional[int]) -> str:
    if n is None:
        return ""
    t = info["subs"][n]
    return f"S{n + 1} {lang_name(t['lang'])} ({t['codec']})"

async def dl_render(client: Client, user_id: int) -> None:
    sess, lib = USER_DATA.get(user_id), lib_get(user_id)
    if not sess or not lib or not sess.get("dl"):
        return
    dl, info = sess["dl"], lib["info"]
    ref = (lib.get("refs") or {}).get(dl.get("ref")) if dl.get("ref") is not None else None
    text = delay_preview_html(sess["dl_cues"], ref, ref_label_of(info, dl.get("ref")), dl["delay"], dl.get("page", 0))
    await dl_edit(client, dl, text, delay_keyboard(dl, info))

async def load_ref_cues(lib: Dict[str, Any], n: int, job: Dict[str, Any], status_msg: Message) -> Optional[list]:
    cache = lib.setdefault("refs", {})
    if n in cache:
        return cache[n]
    out = OUTPUT_DIR / f"ref_{time.time_ns()}.srt"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
           "-i", lib["path"], "-map", f"0:s:{n}", "-c:s", "srt", str(out)]
    ok, _ = await run_ffmpeg_job(cmd, job, status_msg, "Reading subtitle track...", lib["info"]["duration"])
    cues = parse_srt_cues(out) if ok and out.exists() else None
    _unlink(out)
    if cues is not None:
        cache[n] = cues
    return cues

def pick_default_ref(info: Dict[str, Any]) -> Optional[int]:
    texts = [i for i, t in enumerate(info["subs"]) if t["codec"] in TEXT_SUB_CODECS]
    if not texts:
        return None
    for i in texts:
        if info["subs"][i]["lang"][:3] == "eng" and not info["subs"][i].get("forced"):
            return i
    return texts[0]

async def dl_open(client: Client, message: Message, user_id: int) -> None:
    sess, lib = USER_DATA.get(user_id), lib_get(user_id)
    chat_id = message.chat.id
    if not lib:
        await client.send_message(chat_id, "⚠️ The video is not on the server. Send it again.")
        return
    if not sess or not sess.get("srt_path"):
        USER_DATA.setdefault(user_id, {})["pending"] = "delay"
        await client.send_message(chat_id, "📄 Please send the **`.srt` subtitle file**. Fix Delay opens automatically after it arrives.")
        return
    job = begin_job(user_id, "ref")
    if not job:
        await client.send_message(chat_id, "⏳ **A job is already running.**")
        return
    try:
        sess["dl_cues"] = parse_srt_cues(sess["srt_path"])
        dl = sess["dl"] = {"delay": int(sess.get("delay_ms", 0)), "ref": pick_default_ref(lib["info"]), "page": 0,
                           "chat": chat_id, "msg": message.id, "demo_len": 60, "active": True}
        if dl["ref"] is not None:
            if await load_ref_cues(lib, dl["ref"], job, message) is None:
                dl["ref"] = None
        await dl_render(client, user_id)
    finally:
        record_job_finish(user_id)

def ensure_dl(sess: Dict[str, Any], lib: Dict[str, Any], message: Message) -> Dict[str, Any]:
    if not sess.get("dl_cues") and sess.get("srt_path"):
        sess["dl_cues"] = parse_srt_cues(sess["srt_path"])
    dl = sess.get("dl")
    if not dl:
        dl = sess["dl"] = {"delay": int(sess.get("delay_ms", 0)), "ref": pick_default_ref(lib["info"]), "page": 0,
                           "chat": message.chat.id, "msg": message.id, "demo_len": 60, "active": False}
    return dl

async def start_demo(client: Client, user_id: int, start_secs: float) -> None:
    sess, lib = USER_DATA.get(user_id), lib_get(user_id)
    if not sess or not lib or not sess.get("dl"):
        return
    dl, dur = sess["dl"], lib["info"]["duration"]
    length = dl.get("demo_len", 60)
    start = max(0.0, min(start_secs, max(dur - length, 0)))
    job = begin_job(user_id, "demo")
    if not job:
        await client.send_message(dl["chat"], "⏳ **A job is already running.**")
        return
    sess["delay_ms"] = dl["delay"]                    # the demo always uses the delay you are looking at
    dl["await_demo"] = False
    spawn(process_job(client, dl["chat"], user_id, sess, int(start), int(min(start + length, dur)), job, True))

async def delay_callback(client: Client, callback: CallbackQuery, data: str, user_id: int) -> None:
    sess, lib = USER_DATA.get(user_id), lib_get(user_id)
    if not sess or not lib or not sess.get("srt_path"):
        await callback.answer("⚠️ Session expired.", show_alert=True)
        return
    dl = ensure_dl(sess, lib, callback.message)
    if data not in ("dl:full", "dl:embedfull") and not data.startswith(("dl:dm", "dl:dl", "dl:dc")):
        pass
    parts = data.split(":")
    cmd = parts[1] if len(parts) > 1 else ""
    info = lib["info"]
    dl["chat"], dl["msg"] = callback.message.chat.id, callback.message.id       # always edit the message that was tapped

    if cmd == "d":
        dl["delay"] = max(-DELAY_MAX_MS, min(DELAY_MAX_MS, dl["delay"] + int(parts[2])))
        await dl_render(client, user_id)
    elif cmd == "reset":
        dl["delay"] = 0
        await dl_render(client, user_id)
    elif cmd == "pg":
        dl["page"] = max(0, dl.get("page", 0) + int(parts[2]))
        await dl_render(client, user_id)
    elif cmd == "ref":
        n = int(parts[2])
        job = begin_job(user_id, "ref")
        if not job:
            await callback.answer("⏳ A job is already running.", show_alert=True)
            return
        try:
            if await load_ref_cues(lib, n, job, callback.message) is not None:
                dl["ref"], dl["page"] = n, 0
        finally:
            record_job_finish(user_id)
        await dl_render(client, user_id)
    elif cmd == "auto":
        ref = (lib.get("refs") or {}).get(dl.get("ref"))
        if not ref:
            await callback.answer("No reference subtitle track.", show_alert=True)
            return
        g, conf = estimate_offset(ref, [c for c in sess["dl_cues"] if not c["positioned"]])
        if conf < 0.1:
            await callback.answer("No matching pattern found (maybe a different cut?).", show_alert=True)
            return
        dl["delay"] = max(-DELAY_MAX_MS, min(DELAY_MAX_MS, round(-g * 1000)))
        await callback.answer(f"Auto: {fmt_delay(dl['delay'])}  (confidence {conf * 100:.0f}%)")
        await dl_render(client, user_id)
        return
    elif cmd == "apply":
        sess["delay_ms"] = dl["delay"]
        dl["active"] = False
        B = InlineKeyboardButton
        await safe_edit(
            callback.message,
            f"✅ <b>Delay applied:</b> <code>{fmt_delay(dl['delay'])}</code>\nApplies to both Hardcode and Embed.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [B("🔥 Hardcode", callback_data="m_hc"), B("🧩 Embed", callback_data="m_embed")],
                [B("🎬 Demo clip", callback_data="dl:demo"), B("⏱ Fix Again", callback_data="dl_open")],
                [B("📂 Menu", callback_data="m_back")],
            ]))
    elif cmd == "close":
        dl["active"] = False
        await safe_edit(callback.message, lib_menu_text(user_id), reply_markup=lib_menu_keyboard())
    elif cmd == "srt":
        path = OUTPUT_DIR / f"delayed_{user_id}_{time.time_ns()}.srt"
        shift_srt_file(Path(sess["srt_path"]), path, dl["delay"])
        try:
            await client.send_document(callback.message.chat.id, str(path), file_name=f"{Path(sess.get('srt_name') or 'subtitle.srt').stem}.delay{dl['delay']}ms.srt",
                                       caption=f"Delay {fmt_delay(dl['delay'])} applied", parse_mode=ParseMode.DISABLED)
        finally:
            _unlink(path)
    elif cmd == "demo":
        dl["active"] = True
        await safe_edit(
            callback.message,
            f"<b>Demo clip</b>\nHardcode a short part of the video with delay <code>{fmt_delay(dl['delay'])}</code> to test it.\n"
            f"Length: <b>{dl.get('demo_len', 60)}s</b>  -  choose the start point:",
            parse_mode=ParseMode.HTML, reply_markup=demo_keyboard(dl))
    elif cmd == "dl":
        dl["demo_len"] = int(parts[2])
        await safe_edit(callback.message, f"<b>Demo clip</b>\nDelay <code>{fmt_delay(dl['delay'])}</code>\nLength: <b>{dl['demo_len']}s</b>  -  choose the start point:",
                        parse_mode=ParseMode.HTML, reply_markup=demo_keyboard(dl))
    elif cmd == "dm":
        await callback.answer("Making demo...")
        await start_demo(client, user_id, info["duration"] * int(parts[2]) / 100)
        return
    elif cmd == "dc":
        dl["await_demo"] = True
        await client.send_message(callback.message.chat.id, "Send the start time  (example: `00:42:10`)")
    elif cmd == "back":
        await dl_render(client, user_id)
    elif cmd in ("full", "embedfull"):
        sess["delay_ms"] = dl["delay"]
        await ask_tracks(client, callback.message.chat.id, user_id,
                         {"type": "hardcode", "start": 0, "end": None} if cmd == "full" else {"type": "embed"})
    await callback.answer()


# ------------------------------------------------------------------------------
# 7. PYROGRAM BOT HANDLERS & CALLBACKS
# ------------------------------------------------------------------------------
KNOWN_COMMANDS = ["start", "cancel", "setwm", "wm", "fonttest", "setfont", "fonts", "style", "intro", "sync", "menu", "delete", "stickers", "done", "webapp", "me", "account", "user", "users", "credit", "app_on", "app_off"]

def time_prompt_keyboard(speed: str = DEFAULT_SPEED) -> InlineKeyboardMarkup:
    def lbl(key: str, name: str) -> str:
        return f"{name} ✅" if speed == key else name
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Full Video", callback_data="get_full")],
        [InlineKeyboardButton(lbl("fast", "⚡ Fast"), callback_data="speed_fast"),
         InlineKeyboardButton(lbl("balanced", "⚖ Balanced"), callback_data="speed_balanced"),
         InlineKeyboardButton(lbl("best", "💎 Best"), callback_data="speed_best")],
        [InlineKeyboardButton(lbl("auto", "🤖 Auto"), callback_data="speed_auto"),
         InlineKeyboardButton(lbl("hq", "🧪 High Quality"), callback_data="speed_hq"),
         InlineKeyboardButton(lbl("lossless", "♾ Lossless"), callback_data="speed_lossless")],
    ])

def time_prompt_text(header: str, duration: float = 0) -> str:
    length = f"🎞 **Video length:** `{fmt_hms(duration)}`\n\n" if duration else ""
    return (
        f"{header}\n\n{length}"
        "⏱ **Send the time range you want.**\n"
        "Example: `00:00:00 - 00:02:00`\n\n"
        "🎬 **Tap Full Video below for the whole video.**\n\n"
        "⚡ **Fast** — quickest result.\n"
        "⚖ **Balanced** — speed and quality together.\n"
        "💎 **Best** — highest standard quality.\n"
        "🤖 **Auto** — picks the best mode for your video.\n"
        "🧪 **High Quality** — output file can be large.\n"
        "♾ **Lossless** — largest file and takes longer."
    )

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    webapp_on = WEBAPP_ENABLED and PUBLIC_URL.startswith("https://")
    register_webapp_chat(message.chat.id)
    welcome_text = (
        "🎬 **Video Control Panel**\n"
        "_Your video and Sinhala subtitle work in one place._\n\n"
        "**Get started**\n"
        f"• Send the `.srt` subtitle and the video here{', or upload from the Web App below' if webapp_on else ''}.\n"
        "• Choose 🔥 Hardcode or 🧩 Embed, then pick the audio / subtitle tracks.\n"
        "• While it encodes you can name the output file; the extension is kept.\n\n"
        "**More tools**\n"
        "📋 Info  •  💬 Subs  •  🔊 Audio  •  ⏱ Fix Delay / Demo\n"
        "🎨 `/style` — subtitle look and preview\n"
        "🔤 `/fonts` — upload, list and delete your fonts\n"
        "🧩 `/stickers` — save your own bot stickers\n"
        "🔄 `/sync` — English / Sinhala subtitle timing report\n"
        "🏷 `/setwm name` — watermark  •  📝 `/intro` — intro subtitle\n"
        "❌ `/cancel` — stop the running job\n\n"
        f"🕒 Your uploaded video is kept on the server for {KEEP_MINUTES} minutes.\n"
        f"🏷 Watermark: `{get_wm_text(message.from_user.id)}`"
    )
    if not webapp_on:
        welcome_text += "\n\nℹ️ The Web App is currently off."
    rows = []
    if webapp_on:
        rows.append([InlineKeyboardButton("🚀 Open App", web_app=WebAppInfo(url=f"{PUBLIC_URL}/dashboard"))])
    rows.append([InlineKeyboardButton("🎨 Style", callback_data="style:open"),
                 InlineKeyboardButton("🔤 Fonts", callback_data="font:manager")])
    await staged_send(client, message.chat.id, "welcome", welcome_text, InlineKeyboardMarkup(rows),
                      os.environ.get("TEXT_TEMP_START", "⏳ System starting......"))
    try:
        await client.set_chat_menu_button(chat_id=message.chat.id, menu_button=current_webapp_menu_button())
    except Exception as e:
        logger.warning("Could not update Telegram Web App menu button: %s", type(e).__name__)

@bot.on_message(filters.command(["webapp", "app_on", "app_off"]) & filters.private)
async def webapp_toggle_handler(client: Client, message: Message):
    global WEBAPP_ENABLED
    user_id = message.from_user.id if message.from_user else 0
    if not _bot_admin_ok(user_id):
        await message.reply_text("You are not allowed to use this command.")
        return
    parts = (message.text or "").split()
    command = parts[0].split("@", 1)[0].lstrip("/").lower() if parts else "webapp"
    action = (
        "on" if command == "app_on" else
        "off" if command == "app_off" else
        (parts[1].lower() if len(parts) > 1 else "status")
    )
    if action not in ("on", "off", "status"):
        await message.reply_text("Usage: `/webapp on`, `/webapp off`, `/webapp status` (or `/app_on`, `/app_off`).")
        return
    if action == "status":
        state = "ON" if WEBAPP_ENABLED else "OFF"
        note = "Also check that PUBLIC_URL is HTTPS." if WEBAPP_ENABLED and not PUBLIC_URL.startswith("https://") else ""
        await message.reply_text(f"Frame Studio Web App: **{state}**\n{note}")
        return
    WEBAPP_ENABLED = action == "on"
    register_webapp_chat(message.chat.id)
    save_webapp_control()
    changed, failed = await sync_webapp_menu_buttons(client)
    if WEBAPP_ENABLED:
        text = "✅ Web App is ON. Bot menu buttons updated."
        if not PUBLIC_URL.startswith("https://"):
            text += "\n⚠️ Set PUBLIC_URL to a public HTTPS address and restart the bot."
    else:
        text = "⛔ Web App is OFF. `/dashboard` and its API now return 404, and Mini App menu buttons were removed."
    text += f"\nMenu update: {changed} done, {failed} failed."
    text += "\nThe Main Mini App entry in the BotFather profile cannot be hidden by a command. To hide it fully, disable the Main Mini App in BotFather."
    await message.reply_text(text)

REACT_ON_MESSAGES = os.environ.get("REACT_ON_MESSAGES", "1") != "0"

REACTION_POOL = os.environ.get("REACTION_EMOJIS", "").replace(",", " ").split() or \
    ["👍", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "💯", "⚡", "👀", "😎", "🏆", "🤓", "🫡", "😍", "👌", "🤗"]
REACTION_BIG = os.environ.get("REACTION_BIG", "1") != "0"
_REACT_WARNED = {"n": 0}

async def _react(client: Client, chat_id: int, message_id: int, emoji: str = "") -> None:
    """Random (big, animated) reaction. Bots must use the Bot API method setMessageReaction."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for pick in random.sample(REACTION_POOL, min(3, len(REACTION_POOL))):
                async with session.post(url, json={
                    "chat_id": int(chat_id), "message_id": int(message_id),
                    "reaction": [{"type": "emoji", "emoji": pick}], "is_big": REACTION_BIG,
                }) as response:
                    result = await response.json(content_type=None)
                if result.get("ok"):
                    return
                err = str(result.get("description", ""))
                if _REACT_WARNED["n"] < 5:
                    _REACT_WARNED["n"] += 1
                    logger.warning("Reaction %s failed: %s", pick, err[:160])
                if "reaction" not in err.lower():
                    return
    except Exception as e:
        logger.debug("Reaction error: %s", e)

@bot.on_message(filters.private & (filters.text | filters.video | filters.document), group=-2)
async def react_to_message(client: Client, message: Message):
    """Instant reaction = 'the bot got it'. Never blocks or stops the real handler."""
    if message.from_user:
        access_register(message.from_user)
    if not REACT_ON_MESSAGES or not message.from_user:
        return
    text = message.text or ""
    emoji = "⚡" if text.startswith("/") else ("👀" if (message.video or message.document) else "👍")
    task = asyncio.ensure_future(_react(client, message.chat.id, message.id, emoji))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)

@bot.on_message(filters.command("stickers") & filters.private)
async def sticker_status_handler(client: Client, message: Message):
    uid = message.from_user.id
    if not _bot_admin_ok(uid):
        await message.reply_text("⛔ You are not allowed to change the bot stickers.")
        return
    STICKER_WIZ.pop(uid, None)
    await message.reply_text(sticker_panel_text(), reply_markup=sticker_panel_keyboard())

@bot.on_message(filters.command("done") & filters.private)
async def sticker_done_handler(client: Client, message: Message):
    uid = message.from_user.id
    if uid in STICKER_WIZ:
        STICKER_WIZ[uid]["i"] = len(STICKER_WIZ[uid]["queue"])
        await wiz_show(client, uid)
    else:
        await message.reply_text("ℹ️ Nothing to finish. Send /stickers to set stickers.")

@bot.on_message(filters.sticker & filters.private)
async def sticker_collect_handler(client: Client, message: Message):
    uid = message.from_user.id
    wiz = STICKER_WIZ.get(uid)
    if not wiz or not _bot_admin_ok(uid) or wiz["i"] >= len(wiz["queue"]):
        return
    sticker, event = message.sticker, wiz["queue"][wiz["i"]]
    STICKER_STORE.setdefault("events", {})[event] = [
        {"id": sticker.file_id, "u": sticker.file_unique_id, "emoji": sticker.emoji or ""}]
    STICKER_STORE["owner"] = uid
    save_sticker_store()
    wiz["i"] += 1
    await wiz_show(client, uid, f"✅ Saved for {STICKER_LABEL[event]}")

@bot.on_message(filters.document & filters.private, group=-1)
async def sticker_backup_import_handler(client: Client, message: Message):
    """Send stickers_backup.json / sticker_store.json to the bot to restore every sticker and setting."""
    name = (message.document.file_name or "").lower()
    if not (name.endswith(".json") and "sticker" in name) or not _bot_admin_ok(message.from_user.id):
        return
    try:
        data = await client.download_media(message, in_memory=True)
        raw_obj = json.loads(bytes(data.getbuffer()).decode("utf-8"))
        restored = _migrate_store(raw_obj)
        if isinstance(raw_obj.get("access"), dict) and raw_obj["access"].get("users"):
            ACCESS_DB.update(raw_obj["access"])
            _save_access()
        elif not any(restored["events"].values()):
            raise ValueError("no stickers in this file")
        STICKER_STORE.update(restored)
        STICKER_STORE["owner"] = message.from_user.id
        save_sticker_store()
        await message.reply_text("✅ **Stickers restored from the backup file.**\n\n" + sticker_panel_text(),
                                 reply_markup=sticker_panel_keyboard())
    except Exception as e:
        await message.reply_text(f"❌ Could not read the backup file: {str(e)[:150]}")
    message.stop_propagation()

@bot.on_message(filters.command("setwm") & filters.private)
async def setwm_handler(client: Client, message: Message):
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not clean_wm_text(parts[1]):
        await message.reply_text(
            f"🏷 **Current watermark:** `{get_wm_text(message.from_user.id)}`\n\n"
            "To change it: `/setwm Your Text`  (max 40 characters)"
        )
        return
    new_text = clean_wm_text(parts[1])
    set_wm_text(message.from_user.id, new_text)
    await message.reply_text(f"✅ **Watermark updated:** `{new_text}`")

@bot.on_message(filters.command("wm") & filters.private)
async def wm_handler(client: Client, message: Message):
    await message.reply_text(
        f"🏷 **Current watermark:** `{get_wm_text(message.from_user.id)}`\n"
        "To change it: `/setwm Your Text`"
    )

@bot.on_message(filters.command("style") & filters.private)
async def style_handler(client: Client, message: Message):
    await message.reply_text(style_panel_text(message.from_user.id), reply_markup=style_panel_keyboard())

@bot.on_message(filters.command("fonts") & filters.private)
async def font_manager_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await message.reply_text(font_manager_text(user_id), reply_markup=font_manager_keyboard(user_id))

@bot.on_message(filters.command("intro") & filters.private)
async def intro_handler(client: Client, message: Message):
    parts = (message.text or "").split()
    duration = 9.0
    if len(parts) > 1:
        try:
            duration = float(parts[1])
        except ValueError:
            pass
    duration = min(max(duration, 5.0), 30.0)

    path = OUTPUT_DIR / f"intro_{message.from_user.id}_{time.time_ns()}.srt"
    path.write_text(build_intro_srt(get_wm_text(message.from_user.id), duration), encoding="utf-8")
    try:
        await client.send_document(
            chat_id=message.chat.id, document=str(path), file_name="intro_snippet.srt",
            caption=(
                "📝 Paste this at the very top of your SRT (before the first subtitle).\n"
                "✏️ Change Movie Name, WWW.YOURSITE.COM, @YourChannel (delete any line you do not need).\n"
                f"⏱ The intro lasts {duration:g} seconds. The first subtitle must start after it.\n"
                "Change the length: /intro 12"
            ),
            parse_mode=ParseMode.DISABLED
        )
    finally:
        if path.exists():
            path.unlink()

@bot.on_message(filters.command("fonttest") & filters.private)
async def fonttest_handler(client: Client, message: Message):
    user_id = message.from_user.id
    files = sinhala_font_files(user_id)
    if not files:
        await message.reply_text("⚠️ **No Sinhala-capable font was found.** Add one with /fonts.")
        return
    status = await message.reply_text("🔤 **Making font previews…**")
    current_style = ve.get_user_style(user_id)
    current_family = current_style.get("font") or SUB_FONT_FAMILY
    for i, f in enumerate(files, 1):
        family = font_family_of(f, f.stem)
        png = OUTPUT_DIR / f"fonttest_{i}_{time.time_ns()}.png"
        try:
            preview_style = dict(current_style, font=family)
            if await render_font_sample(family, png, preview_style):
                mark = "  ✅ In use now" if family == current_family else ""
                await client.send_photo(
                    chat_id=message.chat.id, photo=str(png),
                    caption=f"{i}. {family}  ({f.name}){mark}",
                    parse_mode=ParseMode.DISABLED
                )
        except Exception as e:
            logger.error(f"Font test failed for {f.name}: {e}")
        finally:
            if png.exists():
                png.unlink()
    await status.edit_text(
        "✅ **Font previews are ready.** Choose the one that shows Sinhala letters and vowel signs correctly:\n"
        "`/setfont 2`  (number or name)"
    )

@bot.on_message(filters.command("setfont") & filters.private)
async def setfont_handler(client: Client, message: Message):
    user_id = message.from_user.id
    parts = (message.text or "").split(None, 1)
    files = sinhala_font_files(user_id)
    chosen = resolve_font_choice(parts[1], files) if len(parts) > 1 else None
    if not chosen:
        current = ve.get_user_style(user_id).get("font") or SUB_FONT_FAMILY
        await message.reply_text(
            f"🔤 **Current font:** `{current}`\n\n"
            "Preview fonts: `/fonttest`\nChoose: `/setfont 2`"
        )
        return
    style = ve.get_user_style(user_id)
    style["font"] = font_family_of(chosen, chosen.stem)
    ve.save_user_style(user_id, style)
    await message.reply_text(f"✅ **Your subtitle font was changed:** `{style['font']}`\nThis applies only to your account.")

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, message: Message):
    user_id = message.from_user.id
    pending_token = USER_PENDING_RENAME.pop(user_id, None)
    if pending_token and pending_token in PENDING_DELIVERIES:
        await message.reply_text("ℹ️ Name entry stopped. Use the prompt below to send with the old name or discard the output.")
        return
    if request_cancel(user_id):
        await message.reply_text("❌ **Stopping the running job…**")
    elif user_id in SYNC_WAIT:
        SYNC_WAIT.pop(user_id, None)
        await message.reply_text("❌ **Sync check stopped.**")
    else:
        sess = USER_DATA.get(user_id)
        if sess:
            sess.pop("pending", None)
        await message.reply_text("ℹ️ **No job is running.**")

@bot.on_message(filters.command("menu") & filters.private)
async def menu_handler(client: Client, message: Message):
    if lib_get(message.from_user.id):
        await send_lib_menu(client, message.chat.id, message.from_user.id)
    else:
        await staged_send(client, message.chat.id, "menu", "📂 **No file is ready.** Send a video first.", None,
                          os.environ.get("TEXT_TEMP_MENU", "⏳ Loading your menu......"))

@bot.on_message(filters.command("delete") & filters.private)
async def delete_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in JOBS:
        await message.reply_text("⏳ **A job is running.** Send /cancel before deleting files.")
        return
    had = lib_get(user_id) is not None
    lib_remove(user_id)
    await message.reply_text("🗑 **Your video / file is successfully deleted.**" if had else "ℹ️ There is no file to delete.")

@bot.on_message(filters.command("sync") & filters.private)
async def sync_command(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in JOBS:
        await message.reply_text("⏳ **A job is running.** Please wait for it to finish, or send /cancel.")
        return
    SYNC_WAIT[user_id] = {"stage": "en"}
    await message.reply_text(
        "🔄 **Subtitle Sync Check**\n\n"
        "1️⃣ Send the original **English** `.srt` file.\n"
        "2️⃣ Then send the **Sinhala** `.srt` file.\n\n"
        "Lines are matched by **time**, not by number (ads, extra / missing lines and timing shifts).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sync_cancel")]])
    )

# ---------------------------- document / video receive ----------------------------
VIDEO_EXTS = (".mkv", ".mp4", ".mov", ".avi", ".ts", ".m4v", ".webm")

@bot.on_message(filters.document & filters.private)
async def handle_document(client: Client, message: Message):
    user_id = message.from_user.id
    file_name = message.document.file_name or ""
    low = file_name.lower()

    if low.endswith(".srt"):
        if user_id in SYNC_WAIT:
            await handle_sync_file(client, message)
        else:
            await handle_srt_upload(client, message)
        return

    if low.endswith((".ttf", ".otf")):
        if message.document.file_size and message.document.file_size > FONT_MAX_UPLOAD_BYTES:
            await message.reply_text(f"⚠️ **The font file is too large.** Maximum is {FONT_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            return
        if len(uploaded_font_files(user_id)) >= FONT_MAX_FILES_PER_USER:
            await message.reply_text(f"⚠️ **You reached the font limit ({FONT_MAX_FILES_PER_USER}).** Remove one with /fonts first.")
            return
        ext = Path(file_name).suffix.lower()
        stem = re.sub(r"[^\w.-]+", "_", Path(file_name).stem, flags=re.UNICODE).strip("._-")[:70] or "font"
        safe_name = f"u{int(user_id)}_{stem}{ext}"
        dest = FONTS_DIR / safe_name
        suffix = 2
        while dest.exists():
            dest = FONTS_DIR / f"u{int(user_id)}_{stem}_{suffix}{ext}"
            suffix += 1
        try:
            await client.download_media(message, file_name=str(dest))
            if not dest.exists() or dest.stat().st_size < 10000 or dest.stat().st_size > FONT_MAX_UPLOAD_BYTES:
                raise ValueError("Font size is invalid")
            family, supports_sinhala = _font_scan(dest)
            if not supports_sinhala:
                raise ValueError("This font has no Sinhala letters or is not a valid font.")
            install_font_file(dest)
            await message.reply_text(
                f"✅ **Font added:** `{family}`\n"
                "Use /style to change the look, or /fonts to see or remove your fonts.",
                reply_markup=font_manager_keyboard(user_id),
            )
        except Exception as e:
            _unlink(dest)
            _unlink(Path.home() / ".fonts" / dest.name)
            logger.warning(f"Font upload rejected for user {user_id}: {e}")
            await message.reply_text(f"❌ Could not add the font: {str(e)[:180]}")
        return

    mime = message.document.mime_type or ""
    if "video" in mime or low.endswith(VIDEO_EXTS):
        await store_video(client, message)

@bot.on_message(filters.video & filters.private)
async def handle_video(client: Client, message: Message):
    await store_video(client, message)

async def handle_srt_upload(client: Client, message: Message):
    user_id = message.from_user.id
    file_name = message.document.file_name or "subtitle.srt"
    status_msg = await message.reply_text("📥 **Receiving the subtitle file…**")

    raw_srt = DOWNLOAD_DIR / f"raw_{user_id}_{file_name}"
    fixed_srt = OUTPUT_DIR / f"fixed_{user_id}_{file_name}"

    st_time = time.time()
    stage_stickers = {"sticker_events": set(), "sticker_messages": {}}
    await send_event_sticker(client, message.chat.id, "download", stage_stickers)
    try:
        await client.download_media(
            message, file_name=str(raw_srt), progress=progress_bar,
            progress_args=("Receiving subtitle file…", status_msg, st_time)
        )
    finally:
        await cleanup_event_sticker(message.chat.id, stage_stickers, "download")

    await status_msg.edit_text("🛠 **Fixing subtitle errors and renumbering…**")
    stats = sanitize_and_reindex_srt(raw_srt, fixed_srt)

    await status_msg.edit_text("📤 **Sending the fixed subtitle file…**")
    caption_text = (
        "✅ **Subtitle file fixed**\n\n"
        f"🔹 **Total subtitle lines:** `{stats['total_subs']}`\n"
        f"🔹 **Empty lines removed:** `{stats['removed_empty']}`\n"
        "🔹 **Numbering:** reset from 1"
    )
    await send_event_sticker(client, message.chat.id, "upload", stage_stickers)
    try:
        await client.send_document(
            chat_id=message.chat.id, document=str(fixed_srt), caption=caption_text,
            reply_to_message_id=message.id
        )
    finally:
        await cleanup_event_sticker(message.chat.id, stage_stickers, "upload")

    sess = USER_DATA.setdefault(user_id, {})
    if sess.get("srt_path") and str(sess["srt_path"]) != str(fixed_srt):
        _unlink(sess["srt_path"])
        _unlink(sess.get("raw_srt_path"))
    pending = sess.pop("pending", None)
    sess.update({
        "srt_path": fixed_srt, "raw_srt_path": raw_srt, "srt_name": file_name,
        "add_watermark": sess.get("add_watermark", False), "speed": sess.get("speed", DEFAULT_SPEED),
    })

    if srt_has_intro(fixed_srt):
        gap_line = "🎞 Intro included ✅\n\n"
    else:
        first = srt_first_cue_secs(fixed_srt)
        if first is not None and first >= 10:
            gap_line = f"🎬 First subtitle at `{fmt_hms(first)}`. There is room for an intro ✅ (`/intro`)\n\n"
        elif first is not None:
            gap_line = f"ℹ️ First subtitle at `{fmt_hms(first)}`. Not enough room for an intro.\n\n"
        else:
            gap_line = ""

    if lib_get(user_id):
        await status_msg.edit_text(
            "✅ **Subtitle fixed and ready.**\n\n" + gap_line + "The video is on the server. If the subtitle does not match the video, use **Fix Delay**.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏱ Fix Delay", callback_data="dl_open")]]))
        if pending == "hardcode":
            await show_watermark_prompt(client, message.chat.id, user_id)
        elif pending == "embed":
            await ask_tracks(client, message.chat.id, user_id, {"type": "embed"})
        elif pending == "delay":
            m_ = await client.send_message(message.chat.id, "⏱ Fix Delay...")
            await dl_open(client, m_, user_id)
        else:
            await send_lib_menu(client, message.chat.id, user_id)
    else:
        await status_msg.edit_text(
            "✅ **Subtitle fixed and ready.**\n\n" + gap_line +
            "Now send the **video** for this subtitle (it is kept on the server)."
        )

def original_file_name(message: Message) -> str:
    """The exact file name (with extension) of the file the user sent."""
    name, mime = None, ""
    if getattr(message, "document", None):
        name, mime = message.document.file_name, message.document.mime_type or ""
    elif getattr(message, "video", None):
        name, mime = message.video.file_name, message.video.mime_type or ""
    if name:
        return name
    return f"video_{message.id}{'.mkv' if 'matroska' in mime else '.mp4'}"

def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)

# ------------------------------------------------------------------------------
# ACCESS CONTROL  -  free trial, admin approval, credits, premium video length
#   * everyone: FREE_HARDCODE hardcode + FREE_EMBED embed, videos up to DEFAULT_MAX_MINUTES
#   * more than that needs the admin: credits (1 per hardcode / embed) and a longer length limit
#   * admins (ADMIN_IDS / WEBAPP_ADMIN_IDS / STICKER_ADMIN_IDS) have no limits
#   * if no admin is configured, access control stays OFF (so nobody is locked out)
# ------------------------------------------------------------------------------
ACCESS_ON = os.environ.get("ACCESS_CONTROL", "1") != "0"
FREE_HARDCODE = int(os.environ.get("FREE_HARDCODE", "1") or 0)
FREE_EMBED = int(os.environ.get("FREE_EMBED", "1") or 0)
DEFAULT_MAX_MINUTES = int(os.environ.get("DEFAULT_MAX_MINUTES", "30") or 30)
APPROVE_CREDITS = int(os.environ.get("APPROVE_CREDITS", "5") or 0)
APPROVE_MINUTES = int(os.environ.get("APPROVE_MINUTES", "60") or 60)
REQUEST_COOLDOWN = 60
LIMIT_CHOICES = (30, 60, 90, 120, 150, 9999)            # minutes; 9999 = "3h+" (no limit)
LIMIT_SHORT = {30: "30m", 60: "1h", 90: "1.5h", 120: "2h", 150: "2.5h", 9999: "3h+"}
_access_backup_task: Dict[str, Any] = {"t": None}


def admin_ids() -> set:
    return _env_int_set("ADMIN_IDS") | WEBAPP_ADMIN_IDS | STICKER_ADMIN_IDS


def is_admin(user_id: int) -> bool:
    return int(user_id) in admin_ids()


def access_active() -> bool:
    return ACCESS_ON and bool(admin_ids())


def _save_access() -> None:
    for folder in _sticker_store_dirs():
        try:
            tmp = folder / "access_db.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ACCESS_DB, f, ensure_ascii=False)
            os.replace(tmp, folder / "access_db.json")
        except Exception as e:
            logger.warning("Could not save users in %s: %s", folder, e)


def load_access_db() -> None:
    for folder in _sticker_store_dirs():
        try:
            with open(folder / "access_db.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("users"), dict):
                ACCESS_DB.update(data)
                logger.info("Loaded %d users", len(ACCESS_DB["users"]))
                return
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning("Could not read users file: %s", e)


async def _delayed_access_backup() -> None:
    await asyncio.sleep(60)
    _access_backup_task["t"] = None
    admins = sorted(admin_ids())
    if admins:
        await backup_sticker_store(bot, admins[0])


def touch_access() -> None:
    """Save now, and pin a fresh backup (about a minute after the last change) so nothing is lost on restart."""
    _save_access()
    if admin_ids() and (_access_backup_task["t"] is None or _access_backup_task["t"].done()):
        task = asyncio.ensure_future(_delayed_access_backup())
        _access_backup_task["t"] = task
        BACKGROUND_TASKS.add(task)
        task.add_done_callback(BACKGROUND_TASKS.discard)


def _acc(user_id: int) -> Dict[str, Any]:
    users = ACCESS_DB.setdefault("users", {})
    key = str(int(user_id))
    if key not in users:
        users[key] = {"name": "", "username": "", "credits": 0, "max_min": DEFAULT_MAX_MINUTES, "free_hc": 0,
                      "free_em": 0, "done_hc": 0, "done_em": 0, "blocked": False, "approved": False,
                      "requested": 0, "joined": time.time()}
    return users[key]


def access_register(user) -> None:
    if not user:
        return
    users = ACCESS_DB.setdefault("users", {})
    name = " ".join(x for x in (user.first_name, user.last_name) if x)[:40]
    username = user.username or ""
    is_new = str(user.id) not in users
    u = _acc(user.id)
    if is_new or u.get("name") != name or u.get("username") != username:
        u["name"], u["username"] = name, username
        _save_access()


def fmt_limit(minutes: int) -> str:
    if minutes >= 9999:
        return "3+ hours (no limit)"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:g} hour" + ("" if hours == 1 else "s")


def _length_text(u: Dict[str, Any], duration: float) -> str:
    return (f"⏱ **Video too long.**\nYour limit: **{fmt_limit(u['max_min'])}** • this video: **{fmt_hms(duration)}**.\n"
            "Ask the admin for premium access to use longer videos.")


def access_length_check(user_id: int, duration: float) -> Tuple[bool, str]:
    if not access_active() or is_admin(user_id):
        return True, ""
    u = _acc(user_id)
    if u["blocked"]:
        return False, "⛔ **Access blocked.** Please contact the admin."
    if duration and duration > u["max_min"] * 60 + 5:
        return False, _length_text(u, duration)
    return True, ""


def access_check(user_id: int, kind: str, duration: float) -> Tuple[bool, str]:
    """May this user start a hardcode ('hc') / embed ('em') now?"""
    ok, text = access_length_check(user_id, duration)
    if not ok or not access_active() or is_admin(user_id):
        return ok, text
    u = _acc(user_id)
    cap, used = (FREE_HARDCODE, u["free_hc"]) if kind == "hc" else (FREE_EMBED, u["free_em"])
    if used < cap or u["credits"] > 0:
        return True, ""
    return False, ("💳 **No credits left.**\nYour free hardcode / embed is used up.\n"
                   "Ask the admin for approval and credits to continue.")


def access_commit(user_id: int, kind: str) -> None:
    """Called when an output is ready: uses the free trial first, then one credit."""
    if not access_active() or is_admin(user_id):
        return
    u = _acc(user_id)
    cap, used_key = (FREE_HARDCODE, "free_hc") if kind == "hc" else (FREE_EMBED, "free_em")
    if u[used_key] < cap:
        u[used_key] += 1
    elif u["credits"] > 0:
        u["credits"] -= 1
    u["done_" + kind] += 1
    touch_access()


def user_account_text(user_id: int) -> str:
    if is_admin(user_id):
        return "👑 **Admin account**\n\nNo limits. Use /users to manage users."
    u = _acc(user_id)
    free_hc, free_em = max(0, FREE_HARDCODE - u["free_hc"]), max(0, FREE_EMBED - u["free_em"])
    status = "⛔ Blocked" if u["blocked"] else ("⭐ Premium" if (u["credits"] > 0 or u["max_min"] > DEFAULT_MAX_MINUTES) else "🆓 Free")
    return (f"👤 **Your Account**\n\nStatus: {status}\n💳 Credits: **{u['credits']}**\n"
            f"🆓 Free left: hardcode **{free_hc}** • embed **{free_em}**\n"
            f"⏱ Max video length: **{fmt_limit(u['max_min'])}**\n"
            f"📊 Done: hardcode {u['done_hc']} • embed {u['done_em']}")


def request_keyboard(show_me: bool = True) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("📨 Request access", callback_data="acc:req")]
    if show_me:
        row.append(InlineKeyboardButton("👤 My account", callback_data="acc:me"))
    return InlineKeyboardMarkup([row])


async def deny_message(client: Client, chat_id: int, text: str) -> None:
    await client.send_message(chat_id, text, reply_markup=request_keyboard())


def card_text(user_id: int) -> str:
    u = _acc(user_id)
    who = (u["name"] or "Unknown") + (f" (@{u['username']})" if u.get("username") else "")
    if u["blocked"]:
        status = "⛔ Blocked"
    elif u["approved"]:
        status = "✅ Approved"
    elif u["requested"]:
        status = "📨 Waiting for approval"
    else:
        status = "🆓 Free"
    return (f"🧾 **User Card**\n\n👤 {who}\n🆔 `{user_id}`\nStatus: {status}\n"
            f"💳 Credits: **{u['credits']}**\n⏱ Max video: **{fmt_limit(u['max_min'])}**\n"
            f"🆓 Free used: hardcode {u['free_hc']}/{FREE_HARDCODE} • embed {u['free_em']}/{FREE_EMBED}\n"
            f"📊 Done: hardcode {u['done_hc']} • embed {u['done_em']}")


def card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    B, u = InlineKeyboardButton, _acc(user_id)
    lim = [B(("✓ " if u["max_min"] == m else "") + LIMIT_SHORT[m], callback_data=f"acc:lim:{user_id}:{m}") for m in LIMIT_CHOICES]
    return InlineKeyboardMarkup([
        [B("💳 −1", callback_data=f"acc:cr:{user_id}:-1"), B("💳 +1", callback_data=f"acc:cr:{user_id}:1"),
         B("💳 +5", callback_data=f"acc:cr:{user_id}:5"), B("💳 +10", callback_data=f"acc:cr:{user_id}:10")],
        lim[:3], lim[3:],
        [B("✅ Approve", callback_data=f"acc:ap:{user_id}"),
         B("🔓 Unblock" if u["blocked"] else "⛔ Block", callback_data=f"acc:bl:{user_id}")],
        [B("🔄 Reset free", callback_data=f"acc:rf:{user_id}"), B("🗑 Zero credits", callback_data=f"acc:zc:{user_id}")],
        [B("🔃 Refresh", callback_data=f"acc:card:{user_id}"), B("📋 Users", callback_data="acc:list")],
    ])


async def notify_user(client: Client, user_id: int, text: str) -> None:
    try:
        await client.send_message(int(user_id), text)
    except Exception:
        pass


def users_list_markup() -> Tuple[str, InlineKeyboardMarkup]:
    users = ACCESS_DB.get("users", {})
    ordered = sorted(users.items(), key=lambda kv: (-(kv[1].get("requested", 0) if not kv[1].get("approved") else 0),
                                                    -kv[1].get("joined", 0)))[:20]
    if not ordered:
        return "📋 **Users**\n\nNo users yet.", InlineKeyboardMarkup([[InlineKeyboardButton("🔃 Refresh", callback_data="acc:list")]])
    lines, rows = ["📋 **Users** (waiting requests first)", ""], []
    for n, (uid, u) in enumerate(ordered, 1):
        pending = u.get("requested") and not u.get("approved") and not u.get("blocked")
        lines.append(f"{n}. {'📨 ' if pending else ''}{u.get('name') or 'Unknown'} • `{uid}` • 💳{u.get('credits', 0)}")
        rows.append([InlineKeyboardButton(f"{n}. {(u.get('name') or 'Unknown')[:16]} 💳{u.get('credits', 0)}",
                                          callback_data=f"acc:card:{uid}")])
    rows.append([InlineKeyboardButton("🔃 Refresh", callback_data="acc:list")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def access_callback(client: Client, callback: CallbackQuery, data: str, uid: int) -> None:
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    msg = callback.message

    async def show(text, markup):
        try:
            await msg.edit_text(text, reply_markup=markup)
        except RPCError:
            pass

    if action == "me":
        await callback.answer()
        await client.send_message(msg.chat.id, user_account_text(uid), reply_markup=request_keyboard(False))
        return
    if action == "req":
        u = _acc(uid)
        if is_admin(uid) or not access_active():
            await callback.answer("No request is needed.", show_alert=True)
            return
        if u["blocked"]:
            await callback.answer("Your access is blocked. Please contact the admin.", show_alert=True)
            return
        if time.time() - u["requested"] < REQUEST_COOLDOWN:
            await callback.answer("Your request was already sent. Please wait for the admin.", show_alert=True)
            return
        u["requested"] = time.time()
        touch_access()
        for admin in sorted(admin_ids()):
            try:
                await client.send_message(admin, "📨 **Access request**\n\n" + card_text(uid), reply_markup=card_keyboard(uid))
            except Exception:
                pass
        await callback.answer("Request sent. You will get a message when the admin replies.", show_alert=True)
        return
    # ---- everything below is admin only ----
    if not is_admin(uid):
        await callback.answer("Admin only.", show_alert=True)
        return
    if action == "list":
        text, markup = users_list_markup()
        await show(text, markup)
        await callback.answer()
        return
    target = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
    if not target:
        await callback.answer()
        return
    u = _acc(target)
    note = ""
    if action == "cr":
        u["credits"] = max(0, u["credits"] + int(parts[3]))
        if int(parts[3]) > 0:
            u["approved"], u["blocked"] = True, False
        note = f"💳 Your credits are now **{u['credits']}**."
    elif action == "lim":
        u["max_min"] = int(parts[3])
        note = f"⏱ Your max video length is now **{fmt_limit(u['max_min'])}**."
    elif action == "ap":
        u["approved"], u["blocked"] = True, False
        u["credits"] += APPROVE_CREDITS
        u["max_min"] = max(u["max_min"], APPROVE_MINUTES)
        note = (f"✅ **You are approved!**\n💳 Credits: **{u['credits']}**\n"
                f"⏱ Max video length: **{fmt_limit(u['max_min'])}**")
    elif action == "bl":
        u["blocked"] = not u["blocked"]
        note = "⛔ Your access was blocked by the admin." if u["blocked"] else "🔓 Your access is open again."
    elif action == "rf":
        u["free_hc"] = u["free_em"] = 0
        note = "🆓 Your free hardcode / embed was reset."
    elif action == "zc":
        u["credits"] = 0
        note = "💳 Your credits were set to **0**."
    touch_access()
    await show(card_text(target), card_keyboard(target))
    await callback.answer("Saved")
    if note:
        await notify_user(client, target, note)


@bot.on_message(filters.command(["me", "account"]) & filters.private)
async def account_handler(client: Client, message: Message):
    access_register(message.from_user)
    limited = access_active() and not is_admin(message.from_user.id)
    await message.reply_text(user_account_text(message.from_user.id),
                             reply_markup=request_keyboard(False) if limited else None)


@bot.on_message(filters.command("users") & filters.private)
async def users_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    text, markup = users_list_markup()
    await message.reply_text(text, reply_markup=markup)


@bot.on_message(filters.command("user") & filters.private)
async def user_card_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply_text("Usage: `/user 123456789`  (the user's Telegram ID)\nOr open /users and pick a person.")
        return
    target = int(parts[1])
    await message.reply_text(card_text(target), reply_markup=card_keyboard(target))


@bot.on_message(filters.command("credit") & filters.private)
async def credit_handler(client: Client, message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit() or not parts[2].lstrip("-").isdigit():
        await message.reply_text("Usage: `/credit 123456789 5`  (add 5 credits; use -2 to remove)")
        return
    target, delta = int(parts[1]), int(parts[2])
    u = _acc(target)
    u["credits"] = max(0, u["credits"] + delta)
    if delta > 0:
        u["approved"], u["blocked"] = True, False
    touch_access()
    await message.reply_text(card_text(target), reply_markup=card_keyboard(target))
    await notify_user(client, target, f"💳 Your credits are now **{u['credits']}**.")


load_access_db()
if ACCESS_ON and not admin_ids():
    logger.warning("Access control is OFF: set ADMIN_IDS (your Telegram user ID) to turn it on.")


FAST_DL_STREAMS = max(1, int(os.environ.get("TG_DOWNLOAD_STREAMS", "8") or 8))
FAST_DL_MIN_BYTES = 20 * 1024 * 1024


async def fast_download(client: Client, message: Message, dest: str, progress=None, progress_args: tuple = (),
                        job: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Several 1 MiB chunk streams at once (instead of one chunk per round-trip). Returns the path, or None
    (cancelled / failed) so the caller can fall back to the normal download."""
    media = message.video or message.document or message.audio
    size = int(getattr(media, "file_size", 0) or 0)
    if size < FAST_DL_MIN_BYTES or not hasattr(client, "stream_media"):
        return None
    chunk = 1024 * 1024
    total_chunks = math.ceil(size / chunk)
    streams = max(1, min(FAST_DL_STREAMS, int(os.environ.get("TG_TRANSMISSIONS", "6") or 6), total_chunks))
    per = math.ceil(total_chunks / streams)
    tmp = dest + ".part"
    loop = asyncio.get_running_loop()
    done = {"n": 0}
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.ftruncate(fd, size)

        async def worker(first: int, count: int) -> None:
            pos = first * chunk
            async for part in client.stream_media(message, limit=count, offset=first):
                if job is not None and job.get("cancelled"):
                    return
                await loop.run_in_executor(None, os.pwrite, fd, part, pos)
                pos += len(part)
                done["n"] += len(part)
                if progress:
                    await progress(min(done["n"], size), size, *progress_args)

        ranges = [(i * per, min(per, total_chunks - i * per)) for i in range(streams) if i * per < total_chunks]
        await asyncio.gather(*(worker(a, c) for a, c in ranges))
    except Exception as e:
        logger.warning("Fast download failed, using the normal download: %s", e)
        os.close(fd)
        _unlink(Path(tmp))
        return None
    os.close(fd)
    if (job is not None and job.get("cancelled")) or done["n"] != size:
        _unlink(Path(tmp))
        return None
    os.replace(tmp, dest)
    return dest


async def store_video(client: Client, message: Message) -> None:
    """Downloads the video ONCE and keeps it on the server so many jobs can reuse it."""
    user_id = message.from_user.id
    ok_len, why_len = access_length_check(user_id, getattr(message.video or message.document, "duration", 0) or 0)
    if not ok_len:                                   # rejected before any download starts
        await deny_message(client, message.chat.id, why_len)
        return
    job = begin_job(user_id, "download")
    if not job:
        await message.reply_text("⏳ **A job is running.** Please wait for it to finish, or send /cancel.")
        return

    media = message.video or message.document
    name = original_file_name(message)
    size = getattr(media, "file_size", 0) or 0
    status = await message.reply_text("📥 **Receiving your video…**", reply_markup=CANCEL_KB)
    await send_event_sticker(client, message.chat.id, "download", job)
    dest = None
    try:
        # Keep the previous valid source until this upload has downloaded and probed successfully.
        if size and shutil.disk_usage(LIBRARY_DIR).free < size * 1.3 + 300 * 1024 * 1024:
            job["outcome"], job["error"] = "failed", "insufficient storage"
            await send_event_sticker(client, message.chat.id, "error", job)
            await safe_edit(status, "❌ **Not enough space on the server.** Use /delete to remove old files, then try again.")
            return
        dest = LIBRARY_DIR / f"{user_id}_{message.id}{Path(name).suffix or '.mp4'}"
        dl_args = ("Receiving video…", status, time.time(), client, job)
        path = await fast_download(client, message, str(dest), progress_bar, dl_args, job)
        if not path and not job["cancelled"]:
            path = await client.download_media(message, file_name=str(dest), progress=progress_bar, progress_args=dl_args)
        await cleanup_event_sticker(message.chat.id, job, "received")
        await cleanup_event_sticker(message.chat.id, job, "download")
        if job["cancelled"] or not path:
            if not job["cancelled"]:
                job["outcome"], job["error"] = "failed", "download failed"
                await send_event_sticker(client, message.chat.id, "error", job)
            _unlink(dest)
            await safe_edit(status, "❌ Video download stopped." if job["cancelled"] else "❌ Could not download the video. Please try again.")
            return

        await safe_edit(status, "🔍 **Checking video details…**", cancel=True)
        info = await probe_video(Path(path))
        raw = await probe_raw(Path(path))
        ok_len, why_len = access_length_check(user_id, info.get("duration") or 0)
        if not ok_len:                               # documents have no duration until probed
            _unlink(Path(path))
            await delete_status_message(status)
            await deny_message(client, message.chat.id, why_len)
            return
        old = lib_get(user_id)
        if old:
            lib_remove(user_id, remove_srt=False)
        entry = {"path": str(path), "name": name, "size": Path(path).stat().st_size, "info": info, "raw": raw,
                 "added": time.time(), "expires": time.time() + KEEP_MINUTES * 60}
        LIBRARY[user_id] = entry
        persist_library_entry(user_id, entry)
        for k_ in ("keep_audio", "keep_subs", "dl", "dl_cues", "delay_ms", "tk"):
            (USER_DATA.get(user_id) or {}).pop(k_, None)
        await delete_status_message(status)
        await send_lib_menu(client, message.chat.id, user_id)
    except Exception as e:
        job["outcome"], job["error"] = "failed", str(e)[:500]
        await send_event_sticker(client, message.chat.id, "error", job)
        logger.error(f"store_video failed: {e}", exc_info=True)
        _unlink(dest)
        await safe_edit(status, "❌ **Could not check the new video.** An older working video, if any, was not deleted. Try again as an MP4 / MKV file.")
    finally:
        await cleanup_job_stage_stickers(message.chat.id, job)
        record_job_finish(user_id)

async def show_watermark_prompt(client: Client, chat_id: int, user_id: int) -> None:
    wm_text = get_wm_text(user_id)
    kbd = InlineKeyboardMarkup([[
        InlineKeyboardButton("✨ With Watermark", callback_data="wm_yes"),
        InlineKeyboardButton("❌ No Watermark", callback_data="wm_no"),
    ]])
    await client.send_message(
        chat_id,
        f"🏷 **Add the `{wm_text}` watermark to the video?**\n"
        "_(Use `/setwm` to change the text)_",
        reply_markup=kbd
    )

# ---------------------------- callbacks ----------------------------
async def need_srt(client: Client, callback: CallbackQuery, user_id: int, action: str) -> None:
    USER_DATA.setdefault(user_id, {})["pending"] = action
    await client.send_message(
        callback.message.chat.id,
        "📄 Please send the **`.srt` subtitle file**. It continues automatically after it arrives."
    )

async def library_callback(client: Client, callback: CallbackQuery, data: str, user_id: int) -> None:
    chat_id = callback.message.chat.id
    lib = lib_get(user_id)
    if not lib:
        await callback.answer("⚠️ File expired or was removed. Send the video again.", show_alert=True)
        return
    sess = USER_DATA.get(user_id) or {}

    if data == "m_back":
        await safe_edit(callback.message, lib_menu_text(user_id), reply_markup=lib_menu_keyboard())
    elif data == "m_ext":
        lib["expires"] += 3600
        await safe_edit(callback.message, lib_menu_text(user_id), reply_markup=lib_menu_keyboard())
        await callback.answer("⏳ 1 hour added", show_alert=False)
        return
    elif data == "m_meta":
        meta = build_meta_report(lib["raw"], lib["name"], lib["size"])
        page = render_meta_html(meta)
        kb = None
        if PUBLIC_URL:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Info", web_app=WebAppInfo(url=f"{PUBLIC_URL}/page/{register_page(page)}"))]])
        path = OUTPUT_DIR / f"info_{user_id}_{time.time_ns()}.html"
        path.write_text(page, encoding="utf-8")
        f = meta["file"]
        try:
            await client.send_document(
                chat_id, str(path), file_name=f"{Path(lib['name']).stem}.info.html", reply_markup=kb, parse_mode=ParseMode.DISABLED,
                caption=sc("Media info") + f"\n{lib['name']}\n" + sc(f"Length {f['duration']}  |  Video {len(meta['video'])}  |  Audio {len(meta['audio'])}  |  Subtitles {len(meta['subs'])}"))
        finally:
            _unlink(path)
    elif data == "m_subs":
        text, kb = subs_menu(lib["info"])
        await safe_edit(callback.message, text, reply_markup=kb)
    elif data == "m_audio":
        text, kb = audio_menu(lib["info"])
        await safe_edit(callback.message, text, reply_markup=kb)
    elif data == "m_hc":
        if not sess.get("srt_path"):
            await need_srt(client, callback, user_id, "hardcode")
        else:
            await show_watermark_prompt(client, chat_id, user_id)
    elif data == "m_embed":
        if not sess.get("srt_path"):
            await need_srt(client, callback, user_id, "embed")
        else:
            if user_id in JOBS:
                await callback.answer("⏳ A job is already running.", show_alert=True)
                return
            await ask_tracks(client, chat_id, user_id, {"type": "embed"})
    elif data == "m_del":
        await safe_edit(
            callback.message,
            "🗑 **Delete your video and SRT file?**",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, delete", callback_data="m_del_ok"),
                InlineKeyboardButton("↩️ No", callback_data="m_back"),
            ]])
        )
    elif data == "m_del_ok":
        if user_id in JOBS:
            await callback.answer("⏳ A job is running. Send /cancel first.", show_alert=True)
            return
        lib_remove(user_id)
        await safe_edit(callback.message, "🗑 **Your video / file is successfully deleted.**")
    elif data.startswith(("xs:", "xa:")):
        kind, n = data[:2], int(data.split(":")[1])
        job = begin_job(user_id, "extract")
        if not job:
            await callback.answer("⏳ A job is already running.", show_alert=True)
            return
        spawn(extract_job(client, chat_id, user_id, kind, n, job))
    await callback.answer()

@bot.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    data = callback.data or ""
    user_id = callback.from_user.id

    if data.startswith("acc:"):
        try:
            await access_callback(client, callback, data, user_id)
        except Exception as e:
            logger.error(f"Access action failed: {e}", exc_info=True)
            await callback.answer("Could not complete the action.", show_alert=True)
        return
    if data.startswith("stk:"):
        try:
            await sticker_callback(client, callback, data, user_id)
        except Exception as e:
            logger.error(f"Sticker action failed: {e}", exc_info=True)
            await callback.answer("Could not complete the action.", show_alert=True)
        return
    if data.startswith(("style:", "font:")):
        try:
            if await style_font_callback(client, callback, data, user_id):
                return
        except Exception as e:
            logger.error(f"Style/font action failed for user {user_id}: {e}", exc_info=True)
            await callback.answer("Could not complete the action.", show_alert=True)
            return
    if data.startswith("file:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            await file_delivery_callback(client, callback, parts[1], parts[2], user_id)
        else:
            await callback.answer("This button is not valid.", show_alert=True)
        return
    if data.startswith("name:skip:"):
        await pre_encode_name_callback(callback, data.rsplit(":", 1)[1], user_id)
        return

    if data == "cancel_job":
        if request_cancel(user_id):
            await callback.answer("❌ Stopping…")
        else:
            await callback.answer("No job is running.", show_alert=True)
        return
    if data == "noop":
        await callback.answer("Image-based subtitles (PGS / VobSub) cannot be extracted as SRT.", show_alert=True)
        return
    if data == "sync_cancel":
        SYNC_WAIT.pop(user_id, None)
        await safe_edit(callback.message, "❌ **Sync check stopped.**")
        await callback.answer()
        return
    if data == "dl_open":
        await dl_open(client, callback.message, user_id)
        await callback.answer()
        return
    if data.startswith("dl:"):
        await delay_callback(client, callback, data, user_id)
        return
    if data.startswith("tk:"):
        await tracks_callback(client, callback, data, user_id)
        return
    if data.startswith("m_") or data.startswith(("xs:", "xa:")):
        await library_callback(client, callback, data, user_id)
        return

    session = USER_DATA.get(user_id)
    lib = lib_get(user_id)
    if not session or "srt_path" not in session or not lib:
        await callback.answer("⚠️ Your session expired. Send the SRT file and the video again.", show_alert=True)
        return
    duration = lib["info"]["duration"]

    if data == "wm_yes":
        session["add_watermark"] = True
        await callback.message.edit_text(
            time_prompt_text("✅ **Watermark is ON!**", duration),
            reply_markup=time_prompt_keyboard(session.get("speed", DEFAULT_SPEED)))
    elif data == "wm_no":
        session["add_watermark"] = False
        await callback.message.edit_text(
            time_prompt_text("❌ **Watermark is OFF.**", duration),
            reply_markup=time_prompt_keyboard(session.get("speed", DEFAULT_SPEED)))
    elif data in ("speed_fast", "speed_balanced", "speed_best", "speed_auto", "speed_hq", "speed_lossless"):
        new_speed = data[len("speed_"):]
        if session.get("speed", DEFAULT_SPEED) != new_speed:
            session["speed"] = new_speed
            try:
                await callback.message.edit_reply_markup(time_prompt_keyboard(new_speed))
            except RPCError:
                pass
    elif data == "get_full":
        if user_id in JOBS:
            await callback.answer("⏳ A job is already running.", show_alert=True)
            return
        await ask_tracks(client, callback.message.chat.id, user_id, {"type": "hardcode", "start": 0, "end": None},
                         message=callback.message)
    await callback.answer()

@bot.on_message(filters.text & filters.private & ~filters.command(KNOWN_COMMANDS))
async def handle_time_and_process(client: Client, message: Message):
    user_id = message.from_user.id
    if await handle_pending_rename_input(client, message):
        return
    session = USER_DATA.get(user_id)

    if not session or "srt_path" not in session or not lib_get(user_id):
        await message.reply_text("ℹ️ **Send the subtitle file and the video first.** Send /menu for more options.")
        return
    if user_id in JOBS:
        await message.reply_text("⏳ **A job is already running.** To stop it send /cancel.")
        return

    text = (message.text or "").strip()
    dl = session.get("dl")
    if dl and dl.get("active"):
        hm = HMS_RE.match(text)
        if dl.get("await_demo") and hm:
            await start_demo(client, user_id, int(hm.group(1)) * 3600 + int(hm.group(2)) * 60 + int(hm.group(3)))
            return
        ms = parse_delay_text(text)
        if ms is not None:
            dl["delay"] = ms
            await dl_render(client, user_id)
            try:
                await message.delete()
            except RPCError:
                pass
            return
    match = re.match(r"^(\d{1,2}:\d{2}:\d{2})\s*-\s*(\d{1,2}:\d{2}:\d{2})$", text)
    if not match:
        await message.reply_text(
            "⚠️ **Wrong time format.** Send it like this:\n`00:00:00 - 00:02:00`\n\n"
            "Or tap the **Full Video** button.",
            reply_markup=time_prompt_keyboard(session.get("speed", DEFAULT_SPEED))
        )
        return

    def parse_secs(t):
        h, m, s = map(int, t.split(":"))
        return h * 3600 + m * 60 + s

    s_secs, e_secs = parse_secs(match.group(1)), parse_secs(match.group(2))
    if e_secs <= s_secs:
        await message.reply_text("⚠️ **The end time must be after the start time.**")
        return
    await ask_tracks(client, message.chat.id, user_id, {"type": "hardcode", "start": s_secs, "end": e_secs})

# ---------------------------- jobs ----------------------------
async def finish_job(client: Client, chat_id: int, user_id: int, show_menu: bool = True) -> None:
    job = JOBS.get(int(user_id))
    if job and job.get("pre_name_token"):
        pre_token = job.get("pre_name_token")
        pending = PRENAME_REQUESTS.pop(pre_token, None)
        if USER_PRENAME.get(int(user_id)) == pre_token:
            USER_PRENAME.pop(int(user_id), None)
        if pending and job.get("outcome") != "completed":
            try:
                await safe_edit(pending["message"], "❌ Name selection ended because encoding did not finish.", reply_markup=None)
            except Exception:
                pass
    await cleanup_job_stage_stickers(chat_id, job)
    record_job_finish(user_id)
    lib_touch(user_id)
    if show_menu:
        try:
            await send_lib_menu(client, chat_id, user_id)
        except Exception as e:
            logger.error(f"menu send failed: {e}")

async def extract_job(client: Client, chat_id: int, user_id: int, kind: str, n: int, job: Dict[str, Any]) -> None:
    lib = lib_get(user_id)
    if not lib:
        job["outcome"], job["error"] = "failed", "Source video is no longer available"
        try:
            await client.send_message(chat_id, "❌ <b>File not found</b>\nUpload the source video again and retry.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        record_job_finish(user_id)
        return
    info, stem, src = lib["info"], Path(lib["name"]).stem, lib["path"]
    out = None
    status = await client.send_message(chat_id, "⏳ <b>Preparing the track</b>\nYou can follow the progress here.", reply_markup=CANCEL_KB, parse_mode=ParseMode.HTML)
    await send_event_sticker(client, chat_id, "extract", job)
    try:
        if kind == "xs":
            track = info["subs"][n]
            ext, args = sub_extract_plan(track)
            cmd_map = ["-map", f"0:s:{n}"] + args
            title = "Extracting subtitle track…"
        else:
            track = info["audio"][n]
            ext = AUDIO_EXT.get(track["codec"], "mka")
            cmd_map = ["-map", f"0:a:{n}", "-c", "copy", "-vn"]
            title = "Extracting audio track…"
        stem_ = track_stems("s" if kind == "xs" else "a", info["subs"] if kind == "xs" else info["audio"])[n]
        out = OUTPUT_DIR / f"extract_{user_id}_{time.time_ns()}.{ext}"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:1",
               "-i", src, *cmd_map, str(out)]
        ok, reason = await run_ffmpeg_job(cmd, job, status, title, info["duration"])
        await cleanup_event_sticker(chat_id, job, "extract")
        if not ok:
            job["outcome"], job["error"] = ("cancelled" if reason == "cancelled" else "failed"), str(reason)
            if reason != "cancelled": await send_event_sticker(client, chat_id, "error", job)
            await safe_edit(status, "❌ **Job stopped.**" if reason == "cancelled"
                            else "❌ **Something went wrong while extracting the track.** Please try again.")
            return
        await safe_edit(status, "📤 <b>Sending the file to you</b>", cancel=True, parse_mode=ParseMode.HTML)
        await send_event_sticker(client, chat_id, "upload", job)
        status._last_update_time = 0
        output_name = f"{stem_}.{ext}"
        output_size = out.stat().st_size
        job["output_name"], job["output_size"] = output_name, output_size
        dashboard_history_update_output(user_id, job.get("dashboard_id"), output_name, output_size)
        label = "Subtitle track" if kind == "xs" else "Audio track"
        caption = sc(f"<b>✅ {label} extracted successfully</b>\n"
                   f"📄 <code>{htmlmod.escape(output_name[:180])}</code>\n"
                   f"📦 {humanbytes(output_size)}")
        sent = await client.send_document(
            chat_id, str(out), file_name=output_name, caption=caption, parse_mode=ParseMode.HTML,
            progress=progress_bar, progress_args=("Uploading file...", status, time.time(), client, job)
        )
        await cleanup_event_sticker(chat_id, job, "upload")
        if sent and not job["cancelled"]:
            job["outcome"] = "completed"
            await send_event_sticker(client, chat_id, "success", job)
            await delete_status_message(status)
        else:
            job["outcome"] = "cancelled" if job["cancelled"] else "failed"
            if not job["cancelled"]:
                job["error"] = "output upload failed"
                await send_event_sticker(client, chat_id, "error", job)
            await safe_edit(status, "❌ **Job stopped.**" if job["cancelled"] else "❌ Could not send the output.")
    except Exception as e:
        job["outcome"], job["error"] = "failed", str(e)[:500]
        await send_event_sticker(client, chat_id, "error", job)
        logger.error(f"extract_job failed: {e}", exc_info=True)
        await safe_edit(status, "❌ <b>Something went wrong while preparing the file.</b> Please try again.", parse_mode=ParseMode.HTML)
    finally:
        _unlink(out)
        await finish_job(client, chat_id, user_id, show_menu=False)

async def embed_job(client: Client, chat_id: int, user_id: int, session: Dict[str, Any], job: Dict[str, Any]) -> None:
    lib = lib_get(user_id)
    srt = session.get("srt_path")
    if not lib or not srt:
        record_job_finish(user_id)
        return
    ok_acc, why_acc = access_check(user_id, "em", float((lib.get("info") or {}).get("duration") or 0))
    if not ok_acc:
        record_job_finish(user_id)
        await deny_message(client, chat_id, why_acc)
        return
    info, video_path = lib["info"], Path(lib["path"])
    ext = choose_container(lib["name"])
    default_send_name = Path(lib["name"]).stem + ext
    stamp = int(time.time())
    clean = OUTPUT_DIR / f"embed_{user_id}_{stamp}.srt"
    out = OUTPUT_DIR / f"embedded_{user_id}_{stamp}{ext}"
    thumb = OUTPUT_DIR / f"thumb_{user_id}_{stamp}.jpg"
    status = await client.send_message(chat_id, "🧩 **Adding subtitles to the video…**", reply_markup=CANCEL_KB)
    await send_event_sticker(client, chat_id, "encoding", job)
    keep_output_for_delivery = False
    try:
        if shutil.disk_usage(OUTPUT_DIR).free < lib["size"] * 1.15 + 200 * 1024 * 1024:
            await safe_edit(status, "❌ Not enough disk space on the server.")
            return
        if clean_srt_for_soft_embed(Path(srt), clean, int(session.get("delay_ms", 0))) == 0:
            await safe_edit(status, "❌ No embeddable subtitles in the SRT.")
            return
        cmd = build_embed_cmd(video_path, clean, out, info, ext, session.get("keep_audio"), session.get("keep_subs"))
        ok, reason = await run_ffmpeg_job(cmd, job, status, "Adding subtitles to the video…", info["duration"])
        await cleanup_event_sticker(chat_id, job, "encoding")
        if not ok:
            if reason != "cancelled": await send_event_sticker(client, chat_id, "error", job)
            await safe_edit(status, "❌ **Job stopped.**" if reason == "cancelled"
                            else "❌ **Something went wrong while adding subtitles.**")
            return
        await safe_edit(status, "🖼 Making the preview image…", cancel=True)
        has_thumb = await generate_thumbnail(out, thumb, info["duration"])
        caption = build_caption(lib["name"], info["height"], vlabel_of(info), info["duration"],
                                build_plan(info, ext, keep_audio=session.get("keep_audio"))["audio_labels"])
        job.update(outcome="completed", stage="Output ready", progress=100.0,
                   output_name=str(job.get("selected_output_name") or default_send_name),
                   output_size=out.stat().st_size)
        await present_file_name_choice(
            user_id, chat_id, out, str(job.get("selected_output_name") or default_send_name), caption, status,
            info["width"], info["height"], int(info["duration"]), thumb if has_thumb else None,
            show_menu=True,
        )
        access_commit(user_id, "em")
        keep_output_for_delivery = True
    except Exception as e:
        job["outcome"], job["error"] = "failed", str(e)[:500]
        await send_event_sticker(client, chat_id, "error", job)
        logger.error(f"embed_job failed: {e}", exc_info=True)
        await safe_edit(status, f"❌ **Something went wrong while processing the video.** Send the file again and retry.")
    finally:
        for p in (clean,):
            _unlink(p)
        if not keep_output_for_delivery:
            _unlink(out)
            _unlink(thumb)
        await finish_job(client, chat_id, user_id, show_menu=not keep_output_for_delivery)

async def process_job(
    client: Client, chat_id: int, user_id: int, session: Dict[str, Any],
    start_secs: int, end_secs: Optional[int], job: Dict[str, Any], demo: bool = False
):
    """Hardcode. end_secs=None means: the full video. Uses the video already stored on the server."""
    lib = lib_get(user_id)
    if not lib or not session.get("srt_path"):
        record_job_finish(user_id)
        await client.send_message(chat_id, "⚠️ **Video or SRT file not found.** Please send them again.")
        return
    if not demo:
        ok_acc, why_acc = access_check(user_id, "hc", float((lib.get("info") or {}).get("duration") or 0))
        if not ok_acc:
            record_job_finish(user_id)
            await deny_message(client, chat_id, why_acc)
            return

    video_path, orig_name, info = Path(lib["path"]), lib["name"], lib["info"]
    srt_path = session["srt_path"]
    wm_text = get_wm_text(user_id) if session.get("add_watermark") else None
    out_ext = ".mp4" if demo else choose_container(orig_name)
    default_send_name = (Path(orig_name).stem + ".demo.mp4") if demo else ve.build_output_filename(
        orig_name, out_ext, ve.get_name_settings(user_id))
    delay_ms = int(session.get("delay_ms", 0))

    stamp = int(time.time())
    output_video_path = OUTPUT_DIR / f"rendered_{user_id}_{stamp}{out_ext}"
    thumb_path = OUTPUT_DIR / f"thumb_{user_id}_{stamp}.jpg"
    status_msg = await client.send_message(chat_id, "🔍 **Checking video details…**", reply_markup=CANCEL_KB)
    await send_event_sticker(client, chat_id, "encoding", job)
    keep_output_for_delivery = False

    try:
        total = info["duration"]
        if total > 0 and start_secs >= total:
            await safe_edit(status_msg, f"⚠️ **The start time is beyond the video length.**\n🎞 Video length: `{fmt_hms(total)}`")
            return

        if total > 0:
            remaining = total - start_secs
            if end_secs is None or (end_secs - start_secs) >= remaining - 0.5:
                clip_secs, use_t = remaining, False
            else:
                clip_secs, use_t = float(end_secs - start_secs), True
        else:
            if end_secs is None:
                await safe_edit(status_msg, "❌ **Could not read the video length.** Please give a time range.")
                return
            clip_secs, use_t = float(end_secs - start_secs), True

        selected_mode = "fast" if demo else session.get("speed", DEFAULT_SPEED)
        plan = build_plan(info, out_ext, selected_mode, session.get("keep_audio"),
                          session.get("keep_subs"), demo=demo, clip_secs=clip_secs)
        est_bytes = qe.estimate_output_bytes(plan["policy"], info, clip_secs, plan["a_total"])
        est_encode_secs = qe.estimate_encode_seconds(plan["policy"], info, clip_secs)
        if not demo:
            report = qe.analysis_report(info, plan["policy"], est_secs=est_encode_secs, est_bytes=est_bytes)
            await safe_edit(status_msg, premium_report_html(report), cancel=True, parse_mode=ParseMode.HTML)
        if est_bytes > TG_MAX_UPLOAD:
            await safe_edit(
                status_msg,
                "⚠️ **The output video may exceed the Telegram file limit (2 GB).**\n\n"
                f"📦 Estimate: `{humanbytes(est_bytes)}`\n"
                "Split the video into time ranges and process them one by one.\n"
                "Example: `00:00:00 - 01:30:00` and `01:30:00 - 03:00:00`"
            )
            return
        if shutil.disk_usage(OUTPUT_DIR).free < est_bytes * 1.2 + 200 * 1024 * 1024:
            await safe_edit(status_msg, "❌ **Not enough space on the server.** Choose a shorter time range.")
            return

        if ENCODE_SEM.locked():
            await safe_edit(status_msg, "⏳ **Another video is being processed.** Please wait for your turn…", cancel=True)
        while True:                                  # waiting in the queue stays cancellable
            try:
                await asyncio.wait_for(ENCODE_SEM.acquire(), timeout=2)
                break
            except asyncio.TimeoutError:
                if job["cancelled"]:
                    await safe_edit(status_msg, "❌ **Job stopped.**")
                    return
        try:
            if job["cancelled"]:
                await safe_edit(status_msg, "❌ **Job stopped.**")
                return
            await safe_edit(status_msg, "⚙️ **Started processing your video…**", cancel=True)
            subtitle_style = ve.get_user_style(user_id)
            if not subtitle_style.get("font"):
                subtitle_style["font"] = SUB_FONT_FAMILY
            # Keep the generated ASS until verification completes; lossless checks need it.
            job["keep_ass"] = True
            ok, reason = await hardcode_video_ffmpeg(
                video_path=video_path, srt_path=srt_path, output_path=output_video_path, info=info, plan=plan,
                start_secs=start_secs, clip_secs=clip_secs, use_t=use_t, wm_text=wm_text,
                status_msg=status_msg, job=job, delay_ms=delay_ms, style=subtitle_style,
            )
            await cleanup_event_sticker(chat_id, job, "encoding")
        finally:
            ENCODE_SEM.release()

        if not ok:
            job["outcome"] = "cancelled" if reason == "cancelled" else "failed"
            job["error"] = reason
            if reason != "cancelled": await send_event_sticker(client, chat_id, "error", job)
            msg = {
                "cancelled": "❌ **Job stopped.**",
                "stalled": "❌ **Video processing stalled.** Please try again.",
            }.get(reason, "❌ **Something went wrong while processing the video.**")
            await safe_edit(status_msg, msg)
            return

        if not demo and qe.QUALITY_VERIFY:
            await send_event_sticker(client, chat_id, "quality", job)
            try:
                out_info, checks = await ve.verify_output(
                    video_path, info, output_video_path, plan, start_secs, clip_secs,
                    Path(srt_path), delay_ms, job,
                )
                quality_report = qe.comparison_report(
                    info, out_info, plan["policy"], checks, elapsed=0,
                    src_bytes=(int(lib.get("size", 0) * clip_secs / info["duration"])
                               if info.get("duration", 0) > 0 else 0),
                    out_bytes=output_video_path.stat().st_size,
                )
                await client.send_message(chat_id, quality_report, parse_mode=ParseMode.DISABLED)
            except Exception as verify_error:
                logger.warning(f"Post-encode quality verification failed: {verify_error}", exc_info=True)
                await client.send_message(chat_id, "⚠️ Could not check the output quality. The video file was created successfully.")
            finally:
                await cleanup_event_sticker(chat_id, job, "quality")

        await safe_edit(status_msg, "🖼 Making the video preview and details...", cancel=True)
        width, height, duration_val = await get_video_metadata(output_video_path)
        has_thumb = await generate_thumbnail(output_video_path, thumb_path, duration_val or clip_secs)

        if demo:
            caption = (f"Demo  {fmt_hms(start_secs)} - {fmt_hms(start_secs + clip_secs)}\n"
                       f"Delay {delay_ms / 1000:+.2f}s  |  hardcoded preview")
        else:
            caption = ve.build_caption(
                orig_name, height, plan["vlabel"], duration_val or clip_secs, plan["audio_labels"],
                title=ve.build_display_title(orig_name, ve.get_name_settings(user_id)) + " සිංහල උපසිරසි සමග",
            )
        if not demo:
            job.update(outcome="completed", stage="Output ready", progress=100.0,
                       output_name=str(job.get("selected_output_name") or default_send_name),
                       output_size=output_video_path.stat().st_size)
        await present_file_name_choice(
            user_id, chat_id, output_video_path,
            str(job.get("selected_output_name") or default_send_name), caption, status_msg,
            width, height, duration_val or int(clip_secs), thumb_path if has_thumb else None,
            reply_markup=demo_result_keyboard() if demo else None, show_menu=not demo,
        )
        if not demo:
            access_commit(user_id, "hc")
        keep_output_for_delivery = True

    except Exception as e:
        job["outcome"], job["error"] = "failed", str(e)[:500]
        await send_event_sticker(client, chat_id, "error", job)
        logger.error(f"Error during processing: {e}", exc_info=True)
        await safe_edit(status_msg, "❌ **Processing failed.** Your original video is still in the server library; try again with /menu or the Mini App.")
    finally:
        if not keep_output_for_delivery:
            _unlink(output_video_path)
            _unlink(thumb_path)
        vf_info = job.pop("vf_info", None)
        job.pop("keep_ass", None)
        if vf_info and vf_info.get("ass"):
            _unlink(Path(vf_info["ass"]))
        await finish_job(client, chat_id, user_id, show_menu=(not demo and not keep_output_for_delivery))

# ---------------------------- sync checker flow ----------------------------
async def handle_sync_file(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    st = SYNC_WAIT.get(user_id)
    if not st:
        return
    name = message.document.file_name or "subtitle.srt"
    path = DOWNLOAD_DIR / f"sync_{user_id}_{time.time_ns()}.srt"
    await client.download_media(message, file_name=str(path))
    pstats: Dict[str, int] = {}
    cues = parse_srt_cues(path, pstats)
    if len(cues) < 3:
        _unlink(path)
        await message.reply_text("⚠️ No subtitles could be read from this SRT file. Please send it again.")
        return

    if st["stage"] == "en":
        st.update(stage="si", en_path=path, en_name=name, en_cues=cues, en_skipped=pstats.get("skipped", 0))
        await message.reply_text(
            f"✅ **English:** `{name}`\n🔹 Cues: `{len(cues)}`  •  ⏱ `{fmt_hms(cues[0]['s'])}` → `{fmt_hms(cues[-1]['e'])}`\n\n"
            "2️⃣ Now send the **Sinhala** `.srt` file.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sync_cancel")]])
        )
        return

    job = begin_job(user_id, "sync")
    if not job:
        _unlink(path)
        await message.reply_text("⏳ A job is already running.")
        return
    SYNC_WAIT.pop(user_id, None)
    spawn(run_sync(client, message.chat.id, user_id, st, path, name, cues, pstats.get("skipped", 0), job))

def sync_status_text(state: Dict[str, Any]) -> str:
    c = state["counters"]
    pct = state["progress"]
    bar = "█" * math.floor(pct / 10) + "░" * (10 - math.floor(pct / 10))
    stage = SYNC_STAGES[min(state["stage"], len(SYNC_STAGES) - 1)]
    return (
        "🔄 **Sync Check — Live**\n\n"
        f"[{bar}] `{pct:.0f}%`  •  {stage} ({min(state['stage'] + 1, len(SYNC_STAGES))}/{len(SYNC_STAGES)})\n\n"
        f"📄 English lines `{c['en']}`  •  Sinhala lines `{c['si']}`\n"
        f"✅ Matched `{c['matched']}`   🕳 Missing `{c['missing']}`\n"
        f"📎 Extra `{c['extra']}`   ⏱ Timing `{c['timing']}`"
    )

def sync_keyboard(token: str, finished: bool) -> InlineKeyboardMarkup:
    rows = []
    if PUBLIC_URL:
        label = "🔍 Open Report" if finished else "🔴 Live View"
        rows.append([InlineKeyboardButton(label, web_app=WebAppInfo(url=f"{PUBLIC_URL}/sync/{token}"))])
    if not finished:
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")])
    return InlineKeyboardMarkup(rows) if rows else None

async def run_sync(client: Client, chat_id: int, user_id: int, st: Dict[str, Any], si_path: Path,
                   si_name: str, si_cues: list, si_skipped: int, job: Dict[str, Any]) -> None:
    en_path, en_cues = st["en_path"], st["en_cues"]
    token = secrets.token_urlsafe(10)
    state = new_sync_state(st["en_name"], si_name, len(en_cues), len(si_cues))
    SYNC_REPORTS[token] = {"state": state, "created": time.time(), "user": user_id}
    status = await client.send_message(chat_id, sync_status_text(state), reply_markup=sync_keyboard(token, False))
    await send_event_sticker(client, chat_id, "sync", job)
    html_path = OUTPUT_DIR / f"sync_{user_id}_{time.time_ns()}.html"

    async def updater():
        last = ""
        while state["status"] == "running":
            await asyncio.sleep(2)
            txt = sync_status_text(state)
            if txt != last:
                last = txt
                await safe_edit(status, txt, reply_markup=sync_keyboard(token, False))

    upd = asyncio.create_task(updater())
    try:
        report = await analyze_sync(en_cues, si_cues, state, job)
        report["meta"]["skipped"] = st.get("en_skipped", 0) + si_skipped
        state["report"], state["status"] = report, "done"
        upd.cancel()

        v, s = report["verdict"], report["stats"]
        icon = {"good": "✅", "warn": "⚠️", "bad": "❌"}[v["level"]]
        verdict_si = {"good": "Good match", "warn": "Needs review", "bad": "Mismatch found"}.get(v["level"], v["title"])
        summary = (
            f"{icon} **Sync Report — {verdict_si}** (`{v['coverage']}%`)\n\n"
            f"📄 English lines `{report['meta']['en_count']}`  •  Sinhala lines `{report['meta']['si_count']}`\n"
            f"✅ Matched `{s['ok'] + s['merged'] + s['split'] + s['timing']}`  🕳 Missing `{s['missing']}`  "
            f"📎 Extra `{s['extra']}`  📣 Ads `{s['ads']}`  ⏱ Timing `{s['timing']}`\n"
            f"↔️ Average time shift `{report['offset']['global']:+.2f}s`"
        )
        if v["reasons"]:
            summary += "\n\n" + "\n".join("• " + r for r in v["reasons"][:5])
        await safe_edit(status, summary, reply_markup=sync_keyboard(token, True))

        html_path.write_text(render_report_html(report, None), encoding="utf-8")
        report_name = f"sync_report_{Path(si_name).stem}.html"
        job["output_name"], job["output_size"] = report_name, html_path.stat().st_size
        dashboard_history_update_output(user_id, job.get("dashboard_id"), report_name, html_path.stat().st_size)
        await send_event_sticker(client, chat_id, "upload", job)
        await client.send_document(
            chat_id, str(html_path), file_name=report_name,
            caption=sc("📄 Sync report (HTML) — open it in a browser or the Telegram Web App."),
            parse_mode=ParseMode.DISABLED
        )
        await cleanup_event_sticker(chat_id, job, "upload")
    except SyncCancelled:
        state["status"], state["error"] = "error", "cancelled"
        job["outcome"], job["error"] = "cancelled", "cancelled"
        upd.cancel()
        await safe_edit(status, "❌ **Sync check stopped.**")
    except Exception as e:
        job["outcome"], job["error"] = "failed", str(e)[:500]
        logger.error(f"run_sync failed: {e}", exc_info=True)
        state["status"], state["error"] = "error", str(e)[:200]
        upd.cancel()
        await safe_edit(status, "❌ Something went wrong during the sync check. Check the SRT files and try again.")
    finally:
        await cleanup_job_stage_stickers(chat_id, job)
        for p in (en_path, si_path, html_path):
            _unlink(p)
        record_job_finish(user_id)


# ------------------------------------------------------------------------------
# TELEGRAM MINI APP: authenticated dashboard API and existing-workflow adapters
# ------------------------------------------------------------------------------
def validate_webapp_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    if not init_data or len(init_data) > 8192 or not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            return None
        supplied_hash = values.pop("hash", "")
        # For bot-token HMAC validation, Telegram's data-check-string contains every
        # received field except hash. Keep the optional `signature` field here.
        if not supplied_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        actual = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, supplied_hash):
            return None
        auth_date = int(values.get("auth_date", "0"))
        if auth_date > time.time() + 60 or time.time() - auth_date > DASHBOARD_AUTH_MAX_AGE_SECONDS:
            return None
        user = json.loads(values.get("user", "{}"))
        uid = int(user.get("id", 0))
        return {"id": uid, "first_name": str(user.get("first_name", ""))[:80]} if uid > 0 else None
    except Exception:
        return None

def dashboard_request_user(request: web.Request) -> int:
    raw = request.headers.get("X-Telegram-Init-Data", "")
    auth = request.headers.get("Authorization", "")
    if not raw and auth.startswith("tma "):
        raw = auth[4:]
    user = validate_webapp_init_data(raw)
    if not user:
        if not raw:
            reason = "missing_init_data"
        elif not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
            reason = "bot_token_not_configured"
        else:
            reason = "invalid_hash_format_or_expired_init_data"
        logger.warning("Mini App authentication rejected (%s; initData_length=%d)", reason, len(raw))
        raise web.HTTPUnauthorized(text=(
            "Telegram Mini App session was rejected. Reopen the app from this bot's /start Open App button or chat Menu. "
            "If it still fails, confirm the deployed BOT_TOKEN belongs to this same bot and that its BotFather Mini App URL "
            "and PUBLIC_URL point to the exact HTTPS /dashboard address."
        ))
    return int(user["id"])

def dashboard_json(payload: Dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

async def dashboard_payload(request: web.Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Expected JSON")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Expected a JSON object")
    return payload

def dashboard_safe_error(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(?:/home/|/tmp/|/mnt/|/workspace/|/app/)[^\s<>\"']+", "[server path]", text)
    text = re.sub(r"(?i)[A-Z]:\\[^\s<>\"']+", "[server path]", text)
    return text[:300]

async def dashboard_page(request: web.Request) -> web.Response:
    page = DASHBOARD_DIR / "dist" / "index.html"
    if not page.is_file():
        page = DASHBOARD_DIR / "index.html"
    if not page.is_file():
        return web.Response(text="Mini App build is missing. Build webapp/frontend and deploy webapp/dist with main.py.", status=503)
    gzip_page = page.with_name(page.name + ".gz")
    use_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower() and gzip_page.is_file()
    served_page = gzip_page if use_gzip else page
    stat = page.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-{"gzip" if use_gzip else "identity"}"'
    headers = {
        "Cache-Control": "private, no-cache, must-revalidate",
        "ETag": etag,
        "Vary": "Accept-Encoding",
        "X-Content-Type-Options": "nosniff",
        "Content-Type": "text/html; charset=utf-8",
    }
    if use_gzip:
        headers["Content-Encoding"] = "gzip"
    if etag in {value.strip() for value in request.headers.get("If-None-Match", "").split(",")}:
        return web.Response(status=304, headers=headers)
    return web.FileResponse(served_page, headers=headers)

async def dashboard_asset(request: web.Request) -> web.Response:
    name = request.match_info.get("asset", "")
    root = (DASHBOARD_DIR / "dist" / "assets").resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})

def dashboard_media_signature(kind: str, uid: int, key: str, expiry: int, path: Path) -> str:
    payload = f"{kind}:{int(uid)}:{key}:{int(expiry)}:{path.resolve()}".encode("utf-8")
    return hmac.new(BOT_TOKEN.encode("utf-8"), payload, hashlib.sha256).hexdigest()

def dashboard_media_url(uid: int, kind: str, key: str, path: Path) -> str:
    # Stable for the current six-hour bucket so one-second state polling never restarts video playback.
    expiry = (int(time.time()) // 21600 + 1) * 21600
    signature = dashboard_media_signature(kind, uid, key, expiry, path)
    return f"/dashboard/media/{kind}/{int(uid)}/{key}/{expiry}/{signature}"

async def dashboard_media(request: web.Request) -> web.StreamResponse:
    try:
        kind = str(request.match_info.get("kind", ""))
        uid = int(request.match_info.get("uid", "0"))
        key = str(request.match_info.get("key", ""))
        expiry = int(request.match_info.get("expiry", "0"))
        signature = str(request.match_info.get("signature", ""))
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    if expiry < int(time.time()) or expiry > int(time.time()) + 21600 + 3600:
        raise web.HTTPForbidden(text="Preview link expired")
    path = None
    if kind == "library" and key == "video":
        lib = lib_get(uid)
        if lib: path = Path(lib.get("path", ""))
    elif kind == "delivery":
        entry = PENDING_DELIVERIES.get(key)
        if entry and int(entry.get("user_id", -1)) == uid:
            path = Path(entry.get("path", ""))
    if not path or not path.is_file():
        raise web.HTTPNotFound()
    expected = dashboard_media_signature(kind, uid, key, expiry, path)
    if not hmac.compare_digest(expected, signature):
        raise web.HTTPForbidden(text="Invalid preview link")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": mime, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"})

def dashboard_public_media(info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
        "duration": float(info.get("duration") or 0), "fps": float(info.get("fps") or 0),
        "codec": str(info.get("v_codec") or "unknown"),
        "audio": [{"index": i, "codec": str(t.get("codec") or "?"), "lang": str(t.get("lang") or "und"),
                   "channels": int(t.get("channels") or 0), "default": bool(t.get("default"))}
                  for i, t in enumerate(info.get("audio", []))],
        "subs": [{"index": i, "codec": str(t.get("codec") or "?"), "lang": str(t.get("lang") or "und"),
                  "title": str(t.get("title") or "")[:80], "default": bool(t.get("default")),
                  "extractable": bool(sub_extract_plan(t)[0])}
                 for i, t in enumerate(info.get("subs", []))],
    }

async def dashboard_state(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    lib = lib_get(uid)
    session = USER_DATA.get(uid) or {}
    video = None
    if lib:
        video = {"name": str(lib.get("name") or "video"), "size": int(lib.get("size") or 0),
                 "expires_at": float(lib.get("expires") or 0), "media": dashboard_public_media(lib["info"]),
                 "preview_url": dashboard_media_url(uid, "library", "video", Path(lib["path"]))}
    srt_path = Path(session["srt_path"]) if session.get("srt_path") else None
    subtitle = {"name": str(session.get("srt_name") or "subtitle.srt"),
                "size": srt_path.stat().st_size if srt_path and srt_path.is_file() else 0,
                "modified": srt_path.stat().st_mtime_ns if srt_path and srt_path.is_file() else 0,
                "ready": bool(srt_path and srt_path.is_file())}
    job = JOBS.get(uid)
    active = None
    if job:
        started = float(job.get("started_at", time.time()))
        pre = PRENAME_REQUESTS.get(job.get("pre_name_token"))
        active = {"id": job.get("dashboard_id"), "kind": job.get("kind"), "stage": job.get("stage", "processing"),
                  "status": job.get("status_text", ""), "progress": float(job.get("progress", 0)),
                  "elapsed": max(0, time.time() - started), "eta": int(job.get("eta_seconds", 0)),
                  "current_bytes": int(job.get("current_bytes", 0)), "total_bytes": int(job.get("total_bytes", 0)),
                  "can_cancel": True, "can_set_name": bool(pre and pre.get("accepting"))}
    elif uid in SYNC_WAIT:
        waiting = SYNC_WAIT[uid]
        stage = "Waiting for the Sinhala SRT file" if waiting.get("stage") == "si" else "Waiting for the first SRT file"
        active = {"id": None, "kind": "sync_wait", "stage": stage,
                  "status": "Both SRT files are needed for the sync check.", "progress": 0,
                  "elapsed": 0, "eta": 0, "current_bytes": 0, "total_bytes": 0,
                  "can_cancel": True, "can_set_name": False}
    pending_delivery = None
    pending_rows = [(token, value) for token, value in PENDING_DELIVERIES.items()
                    if int(value.get("user_id", -1)) == uid]
    if pending_rows:
        token, value = max(pending_rows, key=lambda item: float(item[1].get("created", 0)))
        delivery_path = Path(value.get("path", ""))
        pending_delivery = {"name": str(value.get("chosen_name") or value.get("default_name") or "video"),
                            "auto_send_at": float(value.get("auto_send_at", 0)),
                            "size": delivery_path.stat().st_size if delivery_path.is_file() else 0,
                            "can_set_name": True, "sending": bool(value.get("sending")),
                            "preview_url": dashboard_media_url(uid, "delivery", token, delivery_path) if delivery_path.is_file() else ""}
    history, daily = [], []
    stats = {"total": 0, "completed": 0, "failed": 0, "cancelled": 0, "today": 0}
    try:
        with dashboard_db_connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY started_at DESC LIMIT 60", (uid,)).fetchall()
            for row in rows:
                rec = dict(row)
                try:
                    retry_spec = json.loads(rec.get("retry_spec") or "{}")
                except Exception:
                    retry_spec = {}
                rec.pop("user_id", None); rec.pop("retry_spec", None)
                rec["elapsed"] = round(float(rec.get("elapsed") or 0), 1)
                retry_kind = retry_spec.get("kind")
                retry_index = retry_spec.get("track_index")
                retry_extractable = False
                if lib and retry_kind in ("audio_extract", "subtitle_extract"):
                    tracks = lib["info"].get("audio", []) if retry_kind == "audio_extract" else lib["info"].get("subs", [])
                    try:
                        idx = int(retry_index)
                        retry_extractable = 0 <= idx < len(tracks) and (retry_kind == "audio_extract" or bool(sub_extract_plan(tracks[idx])[0]))
                    except Exception:
                        retry_extractable = False
                rec["can_retry"] = bool((retry_kind in ("hardcode", "embed") and lib and subtitle["ready"]) or retry_extractable)
                rec["error"] = dashboard_safe_error(rec.get("error"))
                history.append(rec)
            today = time.strftime("%Y-%m-%d", time.localtime())
            totals = db.execute("SELECT status,COUNT(*) n FROM jobs WHERE user_id=? GROUP BY status", (uid,)).fetchall()
            stats["total"] = sum(int(r["n"]) for r in totals)
            for r in totals:
                key = r["status"] if r["status"] in ("completed", "failed", "cancelled") else None
                if key: stats[key] = int(r["n"])
            stats["today"] = int(db.execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND date(started_at,'unixepoch','localtime')=?",
                                             (uid, today)).fetchone()[0])
            counts = db.execute("SELECT date(started_at,'unixepoch','localtime') day,COUNT(*) n FROM jobs WHERE user_id=? AND started_at>=? GROUP BY day ORDER BY day",
                                (uid, time.time() - 6 * 86400)).fetchall()
            daily = [{"day": str(r["day"]), "count": int(r["n"])} for r in counts]
    except Exception as e:
        logger.warning(f"Dashboard state history read failed: {e}")
    fonts = [{"name": (p.name[len(f"u{uid}_"):] if p.name.startswith(f"u{uid}_") else p.name),
              "family": font_family_of(p, p.stem), "owned": p.name.startswith(f"u{uid}_"),
              "size": p.stat().st_size} for p in sinhala_font_files(uid)]
    storage = int((lib or {}).get("size", 0)) + int(subtitle["size"])
    storage += sum(int(x["size"]) for x in fonts if x["owned"])
    storage += sum(Path(d.get("path", "")).stat().st_size for d in PENDING_DELIVERIES.values()
                   if int(d.get("user_id", -1)) == uid and Path(d.get("path", "")).is_file())
    return dashboard_json({"ok": True, "video": video, "subtitle": subtitle, "active_job": active,
                           "pending_delivery": pending_delivery,
                           "history": history, "stats": stats, "daily": daily, "storage_bytes": storage,
                           "fonts": fonts, "style": ve.get_user_style(uid), "watermark": get_wm_text(uid),
                           "limits": {"video_bytes": DASHBOARD_UPLOAD_MAX_BYTES, "srt_bytes": DASHBOARD_SRT_MAX_BYTES,
                                      "font_bytes": FONT_MAX_UPLOAD_BYTES},
                           "public_url": PUBLIC_URL})

SRT_PREVIEW_MAX_CUES = 5000
SRT_PREVIEW_TIME_RE = re.compile(
    r"^\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})(?:\s+.*)?$"
)

def srt_preview_cues(path: Path) -> list:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    def ms(g1, g2, g3, g4):
        return ((int(g1) * 60 + int(g2)) * 60 + int(g3)) * 1000 + int(str(g4).ljust(3, "0")[:3])
    cues, i = [], 0
    while i < len(lines) and len(cues) < SRT_PREVIEW_MAX_CUES:
        match = SRT_PREVIEW_TIME_RE.match(lines[i])
        if not match:
            i += 1
            continue
        groups = match.groups()
        start, end = ms(*groups[:4]), ms(*groups[4:])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            if SRT_PREVIEW_TIME_RE.match(lines[i]):
                break
            body.append(lines[i])
            i += 1
        content = re.sub(r"<[^>]*>", "", "\n".join(body)).strip()
        if end > start and content:
            cues.append({"start": start, "end": end, "text": content[:1200]})
    return cues

async def dashboard_subtitle_cues(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    session = USER_DATA.get(uid) or {}
    path = Path(session["srt_path"]) if session.get("srt_path") else None
    if not path or not path.is_file():
        raise web.HTTPNotFound(text="No subtitle file is currently available")
    stat = path.stat()
    cache_key = f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
    cached = session.get("_dashboard_preview_cues")
    if not cached or cached.get("key") != cache_key:
        cached = {"key": cache_key, "cues": srt_preview_cues(path)}
        session["_dashboard_preview_cues"] = cached
    return dashboard_json({"ok": True, "cues": cached["cues"], "truncated": len(cached["cues"]) >= SRT_PREVIEW_MAX_CUES})

async def _save_dashboard_upload(request: web.Request, uid: int, allowed: set, limit: int, folder: Path):
    reader = await request.multipart()
    saved = None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "kind" and not part.filename:
            await part.text()
            continue
        if not part.filename:
            await part.release()
            continue
        name = part.filename.replace("\\", "/").split("/")[-1].strip() or "upload"
        ext = Path(name).suffix.lower()
        if ext not in allowed:
            raise web.HTTPBadRequest(text="Unsupported file type")
        effective_limit = min(limit, DASHBOARD_SRT_MAX_BYTES) if ext == ".srt" else limit
        if saved:
            raise web.HTTPBadRequest(text="Upload one file at a time")
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / f".web_{uid}_{uuid.uuid4().hex}{ext}"
        total = 0
        try:
            with temp.open("wb") as out:
                while True:
                    chunk = await part.read_chunk(size=1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > effective_limit:
                        raise web.HTTPRequestEntityTooLarge(max_size=effective_limit, actual_size=total)
                    out.write(chunk)
            if total <= 0:
                raise web.HTTPBadRequest(text="Empty upload")
            saved = (temp, name, total)
        except Exception:
            _unlink(temp)
            raise
    if not saved:
        raise web.HTTPBadRequest(text="No file was uploaded")
    return saved

async def dashboard_upload(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if uid in JOBS:
        raise web.HTTPConflict(text="Wait until the current job finishes before uploading")
    try:
        stage_stickers = {"sticker_events": set(), "sticker_messages": {}}
        await send_event_sticker(bot, uid, "download", stage_stickers)
        try:
            temp, name, size = await _save_dashboard_upload(
                request, uid, set(VIDEO_EXTS) | {".srt"}, DASHBOARD_UPLOAD_MAX_BYTES, DOWNLOAD_DIR)
        finally:
            await cleanup_event_sticker(uid, stage_stickers, "received")
            await cleanup_event_sticker(uid, stage_stickers, "download")
        ext = Path(name).suffix.lower()
        if ext == ".srt":
            if size > DASHBOARD_SRT_MAX_BYTES:
                _unlink(temp)
                raise web.HTTPRequestEntityTooLarge(max_size=DASHBOARD_SRT_MAX_BYTES, actual_size=size)
            fixed = DOWNLOAD_DIR / f"fixed_{uid}_web_{uuid.uuid4().hex}.srt"
            stats = sanitize_and_reindex_srt(temp, fixed)
            if stats.get("total_subs", 0) < 1:
                _unlink(temp); _unlink(fixed)
                raise web.HTTPBadRequest(text="No valid subtitle cues found")
            session = USER_DATA.setdefault(uid, {})
            old = session.get("srt_path")
            old_raw = session.get("raw_srt_path")
            session.update(srt_path=fixed, raw_srt_path=temp, srt_name=name,
                           speed=session.get("speed", DEFAULT_SPEED), add_watermark=session.get("add_watermark", False))
            if old and str(old) != str(fixed): _unlink(old)
            if old_raw and str(old_raw) != str(temp): _unlink(old_raw)
            return dashboard_json({"ok": True, "kind": "subtitle", "name": name,
                                   "size": size, "cues": int(stats["total_subs"])})
        final = LIBRARY_DIR / f"{uid}_web_{uuid.uuid4().hex}{ext}"
        try:
            info = await probe_video(temp)
            raw = await probe_raw(temp)
            temp.replace(final)
            old = lib_get(uid)
            if old:
                lib_remove(uid, remove_srt=False)
            entry = {"path": str(final), "name": name, "size": final.stat().st_size,
                     "info": info, "raw": raw, "added": time.time(), "expires": time.time() + KEEP_MINUTES * 60}
            LIBRARY[uid] = entry
            persist_library_entry(uid, entry)
            session = USER_DATA.setdefault(uid, {})
            for key in ("keep_audio", "keep_subs", "dl", "dl_cues", "delay_ms", "tk"):
                session.pop(key, None)
            return dashboard_json({"ok": True, "kind": "video", "name": name, "size": final.stat().st_size,
                                   "media": dashboard_public_media(info)})
        except Exception:
            _unlink(temp); _unlink(final)
            raise
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dashboard upload failed for user {uid}: {e}", exc_info=True)
        raise web.HTTPBadRequest(text="Upload could not be processed; check that the file is valid")

async def dashboard_sync_start(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if uid in JOBS or uid in SYNC_WAIT:
        raise web.HTTPConflict(text="A job or sync setup is already active")
    SYNC_WAIT[uid] = {"stage": "en"}
    return dashboard_json({"ok": True, "stage": "en", "message": "Upload the English SRT first"}, 202)

async def dashboard_sync_upload(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    st = SYNC_WAIT.get(uid)
    if not st:
        raise web.HTTPConflict(text="Start a subtitle sync check first")
    if uid in JOBS:
        raise web.HTTPConflict(text="A job is already running")
    temp = None
    try:
        stage_stickers = {"sticker_events": set(), "sticker_messages": {}}
        await send_event_sticker(bot, uid, "download", stage_stickers)
        try:
            temp, name, size = await _save_dashboard_upload(
                request, uid, {".srt"}, DASHBOARD_SRT_MAX_BYTES, DOWNLOAD_DIR)
        finally:
            await cleanup_event_sticker(uid, stage_stickers, "download")
        stats: Dict[str, int] = {}
        cues = parse_srt_cues(temp, stats)
        if len(cues) < 3:
            _unlink(temp)
            raise web.HTTPBadRequest(text="At least 3 valid subtitle cues are required")
        skipped = int(stats.get("skipped", 0))
        if st.get("stage") == "en":
            st.update(stage="si", en_path=temp, en_name=name, en_cues=cues, en_skipped=skipped)
            return dashboard_json({"ok": True, "stage": "si", "name": name,
                                   "size": size, "cues": len(cues)}, 202)
        if st.get("stage") != "si" or not st.get("en_path"):
            _unlink(temp)
            raise web.HTTPConflict(text="The first English subtitle is missing; restart sync")
        job = begin_job(uid, "sync")
        if not job:
            _unlink(temp)
            raise web.HTTPConflict(text="A job is already running")
        SYNC_WAIT.pop(uid, None)
        job.update(stage="Starting the sync check…", progress=0.0, outcome="failed")
        spawn(run_sync(bot, uid, uid, st, temp, name, cues, skipped, job))
        return dashboard_json({"ok": True, "stage": "running", "job_id": job.get("dashboard_id"),
                               "name": name, "size": size, "cues": len(cues)}, 202)
    except web.HTTPException:
        raise
    except Exception as exc:
        _unlink(temp)
        logger.error("Dashboard sync upload failed for user %s: %s", uid, exc, exc_info=True)
        raise web.HTTPBadRequest(text="Could not read this SRT file")

def dashboard_track_selection(raw: Any, count: int) -> set:
    if raw is None:
        return set(range(count))
    if not isinstance(raw, list) or len(raw) > 100:
        raise web.HTTPBadRequest(text="Invalid track selection")
    try:
        values = {int(x) for x in raw}
    except Exception:
        raise web.HTTPBadRequest(text="Invalid track selection")
    if any(x < 0 or x >= count for x in values):
        raise web.HTTPBadRequest(text="Track index out of range")
    return values

async def dashboard_start_job(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if uid in JOBS or uid in SYNC_WAIT:
        raise web.HTTPConflict(text="A job is already running")
    payload = await dashboard_payload(request)
    kind = str(payload.get("kind", "hardcode"))
    lib = lib_get(uid)
    session = USER_DATA.get(uid) or {}
    if not lib:
        raise web.HTTPConflict(text="Upload a video first")
    info = lib["info"]
    if kind in ("audio_extract", "subtitle_extract"):
        track_list = info.get("audio", []) if kind == "audio_extract" else info.get("subs", [])
        try:
            track_index = int(payload.get("track_index", -1))
        except Exception:
            raise web.HTTPBadRequest(text="Invalid track index")
        if track_index < 0 or track_index >= len(track_list):
            raise web.HTTPBadRequest(text="Track index out of range")
        if kind == "subtitle_extract" and not sub_extract_plan(track_list[track_index])[0]:
            raise web.HTTPBadRequest(text="This image-based subtitle track cannot be extracted by the current bot")
        extract_kind = "xa" if kind == "audio_extract" else "xs"
        job = begin_job(uid, extract_kind)
        if not job:
            raise web.HTTPConflict(text="A job is already running")
        job["stage"] = "Extracting audio track" if extract_kind == "xa" else "Extracting subtitle track"
        job["outcome"] = "failed"
        dashboard_history_set_retry(job, {"kind": kind, "track_index": track_index})
        spawn(extract_job(bot, uid, uid, extract_kind, track_index, job))
        return dashboard_json({"ok": True, "job_id": job.get("dashboard_id"), "message": "Extraction started"}, 202)
    if kind not in ("hardcode", "embed"):
        raise web.HTTPBadRequest(text="Unsupported operation")
    if not session.get("srt_path") or not Path(session["srt_path"]).is_file():
        raise web.HTTPConflict(text="Upload an SRT subtitle first")
    audio = dashboard_track_selection(payload.get("audio_tracks"), len(info.get("audio", [])))
    subs = dashboard_track_selection(payload.get("subtitle_tracks"), len(info.get("subs", []))) if kind == "embed" else set()
    speed = str(payload.get("mode", DEFAULT_SPEED))
    if speed not in {"fast", "balanced", "best", "auto", "hq", "lossless"}:
        raise web.HTTPBadRequest(text="Unsupported existing mode")
    try:
        delay_ms = int(payload.get("delay_ms", session.get("delay_ms", 0)))
    except Exception:
        raise web.HTTPBadRequest(text="Invalid subtitle delay")
    session.update(speed=speed, add_watermark=bool(payload.get("watermark", session.get("add_watermark", False))),
                   delay_ms=max(-DELAY_MAX_MS, min(DELAY_MAX_MS, delay_ms)), keep_audio=audio, keep_subs=subs)
    session["next_output_name"] = str(payload.get("output_name", ""))[:160].strip()
    if kind == "hardcode":
        try:
            start = max(0, int(payload.get("start_seconds", 0)))
            end = None if payload.get("full", True) else int(payload.get("end_seconds", 0))
        except Exception:
            raise web.HTTPBadRequest(text="Invalid time range")
        if end is not None and end <= start:
            raise web.HTTPBadRequest(text="End time must be after start time")
        if info.get("duration") and start >= info["duration"]:
            raise web.HTTPBadRequest(text="Start time is beyond video duration")
        if end is not None and info.get("duration"):
            end = min(end, int(info["duration"]))
        spec = {"type": "hardcode", "start": start, "end": end}
    else:
        spec = {"type": "embed"}
    session["tk"] = {"audio": audio, "subs": subs, "spec": spec}
    await start_after_tracks(bot, uid, uid, session)
    if uid not in JOBS:
        raise web.HTTPConflict(text="The job could not be started")
    return dashboard_json({"ok": True, "job_id": JOBS[uid].get("dashboard_id"), "message": "Job started"}, 202)

async def dashboard_cancel_job(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    waiting = SYNC_WAIT.pop(uid, None)
    if waiting is not None:
        _unlink(waiting.get("en_path"))
        return dashboard_json({"ok": True, "message": "Sync setup cancelled"}, 202)
    if not request_cancel(uid):
        return dashboard_json({"ok": False, "message": "No active job"}, 409)
    return dashboard_json({"ok": True, "message": "Cancellation requested"}, 202)

async def dashboard_set_output_name(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    payload = await dashboard_payload(request)
    action = str(payload.get("action", "set"))
    if action not in ("set", "skip"):
        raise web.HTTPBadRequest(text="Invalid name action")
    pre_token = USER_PRENAME.get(uid)
    pre = PRENAME_REQUESTS.get(pre_token) if pre_token else None
    if pre and pre.get("accepting"):
        if action == "skip":
            pre["accepting"], pre["skipped"] = False, True
            selected = pre["default_name"]
            if uid in JOBS: JOBS[uid]["selected_output_name"] = None
        else:
            selected = sanitize_output_filename(str(payload.get("name", "")), pre["default_name"])
            if not selected: raise web.HTTPBadRequest(text="Enter a valid output name")
            pre["chosen_name"], pre["accepting"] = selected, False
            if uid in JOBS: JOBS[uid]["selected_output_name"] = selected
        await safe_edit(pre["message"],
                        f"✅ Name saved: <code>{htmlmod.escape(selected)}</code>\n"
                        "It will be sent with this name as soon as the video is ready.",
                        reply_markup=None, parse_mode=ParseMode.HTML)
        return dashboard_json({"ok": True, "name": selected})
    pending_token = USER_PENDING_RENAME.get(uid)
    entry = PENDING_DELIVERIES.get(pending_token) if pending_token else None
    if not entry:
        rows = [(t, e) for t, e in PENDING_DELIVERIES.items() if int(e.get("user_id", -1)) == uid]
        if rows: pending_token, entry = max(rows, key=lambda x: float(x[1].get("created", 0)))
    if not entry or int(entry.get("user_id", -1)) != uid:
        raise web.HTTPConflict(text="There is no output waiting for a name")
    selected = entry["default_name"] if action == "skip" else sanitize_output_filename(str(payload.get("name", "")), entry["default_name"])
    if not selected: raise web.HTTPBadRequest(text="Enter a valid output name")
    entry["chosen_name"] = selected
    USER_PENDING_RENAME.pop(uid, None)
    await safe_edit(entry["status"], f"✅ Name saved: <code>{htmlmod.escape(selected)}</code>\n📤 Sending now…",
                    reply_markup=None, parse_mode=ParseMode.HTML)
    spawn(deliver_pending_when_idle(pending_token))
    return dashboard_json({"ok": True, "name": selected})

async def dashboard_cancel_delivery(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    rows = [(t, e) for t, e in PENDING_DELIVERIES.items() if int(e.get("user_id", -1)) == uid]
    if not rows: return dashboard_json({"ok": False, "message": "No output is waiting"}, 404)
    token, entry = max(rows, key=lambda x: float(x[1].get("created", 0)))
    if entry.get("sending"): raise web.HTTPConflict(text="Upload is already running")
    PENDING_DELIVERIES.pop(token, None); USER_PENDING_RENAME.pop(uid, None)
    _unlink(entry.get("path")); _unlink(entry.get("thumb"))
    await safe_edit(entry["status"], "🗑 Temporary output discarded from the Mini App.", reply_markup=None)
    return dashboard_json({"ok": True})

async def dashboard_retry_job(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if uid in JOBS:
        raise web.HTTPConflict(text="A job is already running")
    payload = await dashboard_payload(request)
    job_id = str(payload.get("job_id", ""))
    with dashboard_db_connect() as db:
        row = db.execute("SELECT retry_spec FROM jobs WHERE job_id=? AND user_id=?", (job_id, uid)).fetchone()
    if not row:
        raise web.HTTPNotFound(text="Job not found")
    try:
        spec = json.loads(row["retry_spec"] or "{}")
    except Exception:
        spec = {}
    lib, session = lib_get(uid), USER_DATA.get(uid)
    if spec.get("kind") in ("audio_extract", "subtitle_extract"):
        if not lib:
            raise web.HTTPConflict(text="Required video is no longer available")
        extract_kind = "xa" if spec["kind"] == "audio_extract" else "xs"
        tracks = lib["info"].get("audio", []) if extract_kind == "xa" else lib["info"].get("subs", [])
        try:
            track_index = int(spec.get("track_index", -1))
        except Exception:
            track_index = -1
        if track_index < 0 or track_index >= len(tracks):
            raise web.HTTPConflict(text="The selected track is no longer available")
        if extract_kind == "xs" and not sub_extract_plan(tracks[track_index])[0]:
            raise web.HTTPConflict(text="This subtitle track cannot be extracted by the current bot")
        job = begin_job(uid, extract_kind)
        if not job:
            raise web.HTTPConflict(text="A job is already running")
        job["stage"] = "Re-extracting audio track" if extract_kind == "xa" else "Re-extracting subtitle track"
        job["outcome"] = "failed"
        dashboard_history_set_retry(job, spec)
        spawn(extract_job(bot, uid, uid, extract_kind, track_index, job))
        return dashboard_json({"ok": True, "job_id": job.get("dashboard_id")}, 202)
    if spec.get("kind") not in ("hardcode", "embed"):
        raise web.HTTPConflict(text="This job cannot be retried")
    if not lib or not session or not session.get("srt_path") or not Path(session["srt_path"]).is_file():
        raise web.HTTPConflict(text="Required source or subtitle file is no longer available")
    info = lib["info"]
    session.update(speed=spec.get("speed", DEFAULT_SPEED), add_watermark=bool(spec.get("add_watermark")),
                   delay_ms=int(spec.get("delay_ms", 0)),
                   keep_audio=dashboard_track_selection(spec.get("keep_audio"), len(info.get("audio", []))),
                   keep_subs=dashboard_track_selection(spec.get("keep_subs"), len(info.get("subs", []))))
    session["tk"] = {"audio": session["keep_audio"], "subs": session["keep_subs"],
                      "spec": {"type": spec["kind"], "start": int(spec.get("start", 0)), "end": spec.get("end")}}
    await start_after_tracks(bot, uid, uid, session)
    if uid not in JOBS:
        raise web.HTTPConflict(text="Retry could not be started")
    return dashboard_json({"ok": True, "job_id": JOBS[uid].get("dashboard_id")}, 202)

async def dashboard_update_style(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    payload = await dashboard_payload(request)
    style = ve.get_user_style(uid)
    updates = payload.get("style", {})
    if not isinstance(updates, dict): raise web.HTTPBadRequest(text="Invalid style")
    allowed_fonts = {font_family_of(p, p.stem) for p in sinhala_font_files(uid)}
    numeric = {"size": (18, 120), "outline": (0, 8), "shadow": (0, 6),
               "margin_h": (0, 300), "margin_v": (0, 300), "spacing": (-5, 30)}
    for key, limits in numeric.items():
        if key in updates:
            try: value = float(updates[key]) if key in ("outline", "shadow", "spacing") else int(updates[key])
            except Exception: raise web.HTTPBadRequest(text="Invalid numeric style value")
            value = max(limits[0], min(limits[1], value))
            style[key] = round(value, 2) if key == "spacing" else (round(value, 1) if key in ("outline", "shadow") else int(value))
    if "alignment" in updates:
        try: align = int(updates["alignment"])
        except Exception: raise web.HTTPBadRequest(text="Invalid alignment")
        if align not in (1, 2, 3, 7, 8, 9): raise web.HTTPBadRequest(text="Invalid alignment")
        style["alignment"] = align
    if "bold" in updates: style["bold"] = bool(updates["bold"])
    for key in ("primary", "outline_c"):
        if key in updates:
            color = str(updates[key]).lstrip("#").upper()
            if not re.fullmatch(r"[0-9A-F]{6}", color): raise web.HTTPBadRequest(text="Invalid color")
            style[key] = f"#{color}"
    if "font" in updates:
        family = str(updates["font"])
        if family and family not in allowed_fonts: raise web.HTTPForbidden(text="That font is not available to this account")
        if family:
            style["font"] = family
        else:
            style.pop("font", None)
    ve.save_user_style(uid, style)
    return dashboard_json({"ok": True, "style": ve.get_user_style(uid)})

async def dashboard_style_preview(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    style = ve.get_user_style(uid)
    family = str(style.get("font") or SUB_FONT_FAMILY)
    png = OUTPUT_DIR / f"web_style_preview_{uid}_{uuid.uuid4().hex}.png"
    try:
        if not await render_font_sample(family, png, style):
            raise web.HTTPBadRequest(text="Subtitle preview could not be rendered; check the selected font and FFmpeg/libass")
        data = png.read_bytes()
        return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    finally:
        _unlink(png)

async def dashboard_intro(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    payload = await dashboard_payload(request)
    try:
        duration = float(payload.get("duration", 9.0))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid intro duration")
    duration = min(30.0, max(5.0, duration))
    content = build_intro_srt(get_wm_text(uid), duration)
    return web.Response(text=content, content_type="application/x-subrip", charset="utf-8",
                        headers={"Content-Disposition": 'attachment; filename="intro_snippet.srt"', "Cache-Control": "no-store"})

async def dashboard_update_watermark(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    text = clean_wm_text(str((await dashboard_payload(request)).get("text", "")))
    set_wm_text(uid, text)
    return dashboard_json({"ok": True, "watermark": get_wm_text(uid)})

async def dashboard_upload_font(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if len(uploaded_font_files(uid)) >= FONT_MAX_FILES_PER_USER:
        raise web.HTTPConflict(text="Your font upload limit has been reached")
    temp = None
    try:
        temp, name, size = await _save_dashboard_upload(request, uid, {".ttf", ".otf"}, FONT_MAX_UPLOAD_BYTES, DOWNLOAD_DIR)
        family, supports = _font_scan(temp)
        if size < 10000 or not supports:
            _unlink(temp)
            raise web.HTTPBadRequest(text="Font file is invalid or does not support Sinhala")
        base = re.sub(r"[^\w.-]+", "_", Path(name).stem, flags=re.UNICODE).strip("._")[:70] or "font"
        final = FONTS_DIR / f"u{uid}_{base}_{uuid.uuid4().hex[:6]}{Path(name).suffix.lower()}"
        temp.replace(final)
        install_font_file(final)
        return dashboard_json({"ok": True, "font": {"name": final.name, "family": family, "size": size}})
    except web.HTTPException:
        if temp: _unlink(temp)
        raise
    except Exception as e:
        if temp: _unlink(temp)
        logger.error(f"Dashboard font upload failed for user {uid}: {e}", exc_info=True)
        raise web.HTTPBadRequest(text="Font could not be installed")

async def dashboard_delete_font(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    name = str((await dashboard_payload(request)).get("name", ""))
    path = next((p for p in uploaded_font_files(uid)
                 if p.name[len(f"u{uid}_"):] == name), None)
    if not path:
        raise web.HTTPNotFound(text="Font not found for this account")
    family = font_family_of(path, path.stem)
    _unlink(path); _unlink(Path.home() / ".fonts" / path.name)
    subprocess.run(["fc-cache", "-f", str(FONTS_DIR)], capture_output=True, check=False)
    subprocess.run(["fc-cache", "-f", str(Path.home() / ".fonts")], capture_output=True, check=False)
    style = ve.get_user_style(uid)
    if style.get("font") == family:
        style.pop("font", None); ve.save_user_style(uid, style)
    return dashboard_json({"ok": True})

async def dashboard_delete_library(request: web.Request) -> web.Response:
    uid = dashboard_request_user(request)
    if uid in JOBS:
        raise web.HTTPConflict(text="Wait until the current job finishes before deleting its source")
    scope = str((await dashboard_payload(request)).get("scope", "all"))
    session = USER_DATA.get(uid) or {}
    if scope not in ("video", "subtitle", "all"): raise web.HTTPBadRequest(text="Invalid delete scope")
    if scope in ("video", "all") and lib_get(uid): lib_remove(uid, remove_srt=(scope == "all"))
    if scope in ("subtitle", "all"):
        _unlink(session.get("srt_path")); _unlink(session.get("raw_srt_path"))
        session.pop("srt_path", None); session.pop("raw_srt_path", None); session.pop("srt_name", None)
    return dashboard_json({"ok": True})

# ------------------------------------------------------------------------------
# MAIN ASYNC RUNNER ENTRY POINT
# ------------------------------------------------------------------------------
BOT_COMMANDS = [
    BotCommand("start", "Open the control panel"),
    BotCommand("menu", "Your video menu"),
    BotCommand("cancel", "Stop the running job"),
    BotCommand("delete", "Delete your video and files"),
    BotCommand("style", "Subtitle style and preview"),
    BotCommand("fonts", "Manage subtitle fonts"),
    BotCommand("fonttest", "Preview all fonts"),
    BotCommand("setfont", "Choose a font"),
    BotCommand("setwm", "Set watermark text"),
    BotCommand("wm", "Show watermark text"),
    BotCommand("intro", "Make an intro subtitle"),
    BotCommand("sync", "Subtitle sync check"),
    BotCommand("stickers", "Set bot stickers"),
    BotCommand("done", "Finish sticker setup"),
    BotCommand("webapp", "Web App on / off / status"),
    BotCommand("me", "Your account, credits and limits"),
    BotCommand("users", "Admin: user list"),
    BotCommand("user", "Admin: open a user card"),
    BotCommand("credit", "Admin: add credits"),
]

load_sticker_store()

async def main():
    logger.info("Initializing Health Check Web Server...")
    await start_web_server()
    logger.info("Starting Pyrogram Bot Client...")
    await bot.start()
    await sync_webapp_menu_buttons(bot)
    await restore_sticker_store(bot)
    try:
        await bot.set_bot_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)
    _packs = os.environ.get("STICKER_LOAD_PACKS", "auto").strip().lower()
    if _packs == "always" or (_packs == "auto" and not store_has_stickers()):
        await load_sticker_pack()
    else:
        logger.info("Public sticker packs skipped (your own stickers are used)")

    await restore_library()
    spawn(library_janitor())
    spawn(sticker_janitor())
    logger.info("Bot is fully operational and online!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
        sys.exit(0)
