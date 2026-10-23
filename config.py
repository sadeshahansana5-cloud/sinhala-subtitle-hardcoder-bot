"""
==============================================================================
 config.py  -  ONE place to control the whole bot
==============================================================================
 Edit the values in SETTINGS below. Nothing else needs to be touched.

 How it works
   * Every value here is applied as a setting BEFORE the bot starts, so main.py,
     video_engine.py and quality_engine.py all pick it up automatically.
   * Leave a value as "" to use the built-in default of the bot.
   * Secrets (API_ID / API_HASH / BOT_TOKEN): leave them "" and keep them in
     your Hugging Face Space "Secrets" (recommended).
   * FORCE_CONFIG = False  ->  a Hugging Face "Variable" / env value with the same
                               name wins over this file.
     FORCE_CONFIG = True   ->  this file always wins.
 Values are written as text ("1", "0", "true", "fast" ...) exactly like env values.
==============================================================================
"""
import os

FORCE_CONFIG = False

SETTINGS = {
    # -------------------------------------------- 0. SPEED PROFILE (video encoding)
    # standard = your presets below.  turbo = about 2x faster hardcode: faster presets with a lower CRF
    # (16) so the picture looks the same. The file is a little bigger (the source-bitrate cap still applies).
    "SPEED_PROFILE": "standard",     # standard | turbo

    # ------------------------------------------------------------------ 1. LOGS
    "LOG_LEVEL": "INFO",             # DEBUG | INFO | WARNING | ERROR   (WARNING = quiet)
    "LOG_TO_FILE": "1",              # 1 = also write the log to LOG_FILE, 0 = console only
    "LOG_FILE": "bot.log",
    "LOG_LIBRARIES_LEVEL": "WARNING",  # pyrogram / aiohttp noise: WARNING hides the connection spam

    # ------------------------------------------------------------ 2. BOT LOGIN
    "API_ID": "",                    # keep in HF Secrets
    "API_HASH": "",                  # keep in HF Secrets
    "BOT_TOKEN": "",                 # keep in HF Secrets
    "PORT": "7860",                  # health-check web server port (Hugging Face = 7860)

    # ----------------------------------------- 2b. ADMINS, FREE TRIAL, CREDITS, LIMITS
    "ADMIN_IDS": "",                 # your Telegram user ID(s): a,b,c  - REQUIRED to turn access control on
    "ACCESS_CONTROL": "1",           # 1 = on (needs ADMIN_IDS), 0 = everyone unlimited
    "FREE_HARDCODE": "1",            # free hardcodes per new user
    "FREE_EMBED": "1",               # free embeds per new user
    "DEFAULT_MAX_MINUTES": "30",     # longest video a normal user may upload
    "APPROVE_CREDITS": "5",          # credits given by the "Approve" button on a user card
    "APPROVE_MINUTES": "60",         # video length given by the "Approve" button

    # ------------------------------------------------- 3. WEB APP / MINI APP
    "WEBAPP_ENABLED": "false",       # true = Mini App on at start. You can also use /webapp on|off
    "PUBLIC_URL": "",                # public https address of the Space (auto-detected on HF)
    "WEBAPP_ADMIN_IDS": "",          # Telegram user IDs allowed to use /webapp, /stickers  (a,b,c)
    "DASHBOARD_DB_PATH": "",         # history database path ("" = next to the code)
    "DASHBOARD_UPLOAD_MAX_GB": "1.9",
    "DASHBOARD_SRT_MAX_MB": "20",
    "DASHBOARD_AUTH_MAX_AGE_SECONDS": "86400",

    # ------------------------------------------------------------- 4. STICKERS
    "STICKER_ADMIN_IDS": "",         # who may run /stickers ("" = WEBAPP_ADMIN_IDS, or everyone)
    "STICKER_BACKUP_CHAT_ID": "",    # your Telegram user ID: restore from the pinned backup after a wipe
    "STICKER_STORE_DIR": "",         # folder for sticker_store.json ("" = /data if it exists, else code folder)
    "STICKER_WAIT_SECONDS": "6",     # a step waits at most this long for its sticker
    "STICKER_MIN_INTERVAL": "0.8",   # minimum gap between two stickers of one step
    "STICKER_LOAD_PACKS": "auto",    # auto = only when you saved no stickers | always | never
    "STICKER_PACK_NAMES": "",        # public packs used only as a fallback ("" = built-in)
    "STICKER_TTL_WELCOME": "6",      # seconds until the /start sticker is deleted (0 = keep)
    "STICKER_TTL_MENU": "6",
    "STICKER_TTL_SUCCESS": "10",
    "STICKER_TTL_ERROR": "10",
    "TEXT_TEMP_START": "⏳ System starting......",   # temp message shown before the /start sticker
    "TEXT_TEMP_MENU": "⏳ Loading your menu......",

    # ------------------------------------------------------------- 5. REACTIONS
    "REACT_ON_MESSAGES": "1",        # 1 = bot reacts to your messages, 0 = off
    "REACTION_EMOJIS": "👍 🔥 🥰 👏 😁 🎉 🤩 💯 ⚡ 👀 😎 🏆 🤓 🫡 😍 👌 🤗",   # random pick, separate by space
    "REACTION_BIG": "1",             # 1 = big animated reaction

    # ----------------------------------------------------------------- 6. FONTS
    "SUB_FONT_FAMILY": "",           # "" = the font you put in the fonts/ folder
    "WM_FONT_FAMILY": "",            # "" = same font
    "SUB_FONT_SIZE": "48",
    "WM_FONT_SIZE": "28",
    "FONT_MAX_UPLOAD_MB": "12",      # per font file a user may upload
    "FONT_MAX_FILES_PER_USER": "20",

    # ------------------------------------------------------------ 7. WATERMARK
    "WATERMARK_TEXT": "Sadesha.Sub",
    "WATERMARK_COLOR": "#87CEFA",
    "WATERMARK_ALIGN": "1",          # 1 bottom-left 2 bottom-center 3 bottom-right 7/8/9 = top
    "WATERMARK_MARGIN": "8",

    # ------------------------------------------------- 8. ENCODING (speed vs size)
    # Faster presets = quicker but slightly lower quality per size. Change only if you accept that.
    "ENCODE_CODEC": "auto",          # auto | h264 | hevc
    "DEFAULT_SPEED": "fast",         # fast | balanced | best   (mode used when none is chosen)
    "ENCODE_CRF": "18",              # lower = better quality (h264)
    "ENCODE_CRF_HEVC": "22",
    "ENCODE_PRESET": "medium",       # "Best" mode, h264
    "ENCODE_PRESET_BAL": "faster",   # "Balanced" mode, h264
    "ENCODE_PRESET_FAST": "veryfast",  # "Fast" mode, h264
    "ENCODE_PRESET_HEVC": "fast",
    "ENCODE_PRESET_HEVC_BAL": "veryfast",
    "ENCODE_PRESET_HEVC_FAST": "superfast",

    # ------------------------------------------------------ 9. QUALITY ENGINE
    "QUALITY_VERIFY": "1",           # 1 = check the output after encoding (0 = skip, saves time)
    "QUALITY_METRICS": "1",          # 1 = SSIM / PSNR sampling in that check
    "QUALITY_AUTO_TIME_BUDGET_MIN": "120",
    "QUALITY_HQ_PRESET_X264": "medium",
    "QUALITY_HQ_CRF_X264": "14",
    "QUALITY_HQ_PRESET_X265": "medium",
    "QUALITY_HQ_CRF_X265": "16",
    "QUALITY_HQ_PRESET_AV1": "6",
    "QUALITY_HQ_CRF_AV1": "22",
    "QUALITY_HQ_MAX_FACTOR": "0",
    "QUALITY_LOSSLESS_PRESET_X264": "veryfast",
    "QUALITY_LOSSLESS_PRESET_X265": "fast",
    "QUALITY_LOSSLESS_CHECK_SECS": "10",

    # ------------------------------------------------- 10. LIMITS AND TIMERS
    "MAX_PARALLEL_JOBS": "1",        # videos processed at the same time
    "STALL_TIMEOUT": "900",          # seconds without progress before a job is stopped
    "KEEP_MINUTES": "180",           # how long an uploaded video stays on the server
    "FILE_AUTO_SEND_SECONDS": "300", # auto-send the result if no name is sent
    "SEND_AS": "video",              # video | document
    "TG_MAX_UPLOAD_GB": "1.95",
    "TG_TRANSMISSIONS": "8",         # parallel Telegram transfers (needs TgCrypto, see requirements.txt)
    "TG_DOWNLOAD_STREAMS": "8",      # parallel chunk streams for one big download (max = TG_TRANSMISSIONS)
    "SYNC_TOL_START": "0.7",         # /sync matching tolerance (seconds)
    "SYNC_TOL_END": "1.2",
}


# What SPEED_PROFILE = "turbo" changes (everything else stays as written above).
TURBO = {
    "ENCODE_PRESET": "veryfast",        # Best      (was medium)
    "ENCODE_PRESET_BAL": "veryfast",    # Balanced  (was faster)
    "ENCODE_PRESET_FAST": "superfast",  # Fast      (was veryfast)
    "ENCODE_CRF": "16",                 # lower CRF keeps the look of the slower presets
    "ENCODE_PRESET_HEVC": "veryfast",
    "ENCODE_PRESET_HEVC_BAL": "veryfast",
    "ENCODE_CRF_HEVC": "20",
}


def _put(key: str, text: str) -> None:
    if FORCE_CONFIG or key not in os.environ:
        os.environ[key] = text


def apply() -> None:
    """Push SETTINGS into the environment (called automatically when this file is imported)."""
    turbo = str(SETTINGS.get("SPEED_PROFILE", "standard")).strip().lower() == "turbo"
    if turbo:
        for key, value in TURBO.items():
            _put(key, value)
    for key, value in SETTINGS.items():
        text = "" if value is None else str(value).strip()
        if text == "" or key == "SPEED_PROFILE" or (turbo and key in TURBO):
            continue
        _put(key, text)


apply()
