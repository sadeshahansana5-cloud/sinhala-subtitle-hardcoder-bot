"""
video_engine.py - VIDEO PROCESSING ENGINE

FFmpeg / FFprobe integration: probing, codec-aware encode plans, subtitle (SRT -> ASS) rendering and hardcoding,
audio / subtitle track selection and copying, embedding, extraction, metadata, subtitle sync analysis, fonts and
file naming. Quality *policy* lives in quality_engine.py. Telegram-specific progress output is reached only through
the small `UI` hook below (main.py installs the real implementation), so this module has no Telegram dependency.
"""
import os
import re
import sys
import time
import math
import json
import bisect
import shutil
import asyncio
import logging
import secrets
import statistics
import subprocess
import unicodedata
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import quality_engine as qe
from quality_engine import (
    ENCODE_CODEC, ENCODE_PRESET_HEVC, ENCODE_CRF_HEVC, ENCODE_PRESET_FAST, ENCODE_PRESET_BAL,
    ENCODE_PRESET_HEVC_FAST, ENCODE_PRESET_HEVC_BAL, DEFAULT_SPEED, ENCODE_PRESET, ENCODE_CRF,
    source_video_bitrate, preset_for,
)

logger = logging.getLogger("SRT_Hardcoder_Bot")
Message = Any          # Telegram message objects are only passed through to the UI hook, never inspected


class _NullUI:
    """Progress / status hooks. main.py replaces `UI` with the Telegram implementation."""
    async def status(self, message, text, cancel=False, **kwargs):
        logger.info(text)

    async def progress(self, message, current_secs, total_secs, start_time, quality_line, extra=None):
        pass

    async def simple(self, message, title, current, total, start):
        pass


UI = _NullUI()

DEFAULT_WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "Sadesha.Sub")

WATERMARK_COLOR = os.environ.get("WATERMARK_COLOR", "#87CEFA")      # light blue

WATERMARK_ALIGN = os.environ.get("WATERMARK_ALIGN", "1")            # 1 = bottom-left, 7 = top-left

WATERMARK_MARGIN = os.environ.get("WATERMARK_MARGIN", "8")          # distance from the screen corner

SUB_FONT_SIZE = os.environ.get("SUB_FONT_SIZE", "48")               # subtitle size (was 44)

WM_FONT_SIZE = os.environ.get("WM_FONT_SIZE", "28")

STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "900"))         # kill ffmpeg if stuck (seconds)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "outputs"
FONTS_DIR = BASE_DIR / "fonts"

for d in [DOWNLOAD_DIR, OUTPUT_DIR, FONTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------------
# SYSTEM FONT REGISTRATION
#   Only the font you put in fonts/ is used.
#   Abhaya Libre / Noto / M PLUS are no longer bundled or downloaded.
# ------------------------------------------------------------------------------
FONT_CONFIGS = []        # nothing is downloaded - the font you put in fonts/ is used
FONT_LABELS = {}
# Microsoft fonts such as Iskoola Pota (iskpota.ttf) / Nirmala UI are proprietary: they cannot be bundled or
# downloaded here - send the .ttf you own to the bot and it is installed like any other font.

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

def refresh_font_cache() -> None:
    user_fonts = Path.home() / ".fonts"
    user_fonts.mkdir(parents=True, exist_ok=True)
    for f in FONTS_DIR.glob("*.[to]tf"):
        try:
            shutil.copy(f, user_fonts / f.name)
        except Exception:
            pass
    subprocess.run(["fc-cache", "-f", str(user_fonts)], check=False, capture_output=True)
    subprocess.run(["fc-cache", "-f", str(FONTS_DIR)], check=False, capture_output=True)

def download_font_entry(entry: Dict[str, Any]) -> bool:
    """Blocking download of one catalog font (call through asyncio.to_thread)."""
    path = FONTS_DIR / entry["name"]
    for url in entry["urls"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) > 10000:
                path.write_bytes(data)
                refresh_font_cache()
                return True
        except Exception as e:
            logger.warning(f"Font download failed {url}: {e}")
    return False

def font_catalog_status() -> list:
    """[(entry, installed?, label)] for every built-in (downloadable) font."""
    return [(e, font_ok(e["name"]), FONT_LABELS.get(e["name"], e["name"])) for e in FONT_CONFIGS if e["name"] in FONT_LABELS]

def font_ok(name: str) -> bool:
    p = FONTS_DIR / name
    return p.exists() and p.stat().st_size > 10000

def setup_system_fonts():
    """Download fonts with mirrors/fallbacks and force update the Linux fontconfig cache."""
    user_fonts = Path.home() / ".fonts"
    user_fonts.mkdir(parents=True, exist_ok=True)

    for cfg in FONT_CONFIGS:
        font_path = FONTS_DIR / cfg["name"]
        user_font_path = user_fonts / cfg["name"]

        if not font_ok(cfg["name"]):
            logger.info(f"Downloading {cfg['name']}...")
            downloaded = False
            for url in cfg["urls"]:
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=60) as response:
                        data = response.read()
                    if len(data) > 10000:
                        with open(font_path, 'wb') as out_file:
                            out_file.write(data)
                        shutil.copy(font_path, user_font_path)
                        logger.info(f"Font downloaded successfully: {cfg['name']}")
                        downloaded = True
                        break
                except Exception as e:
                    logger.warning(f"Failed download from {url}: {e}")

            if not downloaded:
                logger.error(f"All Font Download URLs failed for {cfg['name']}!")

    try:
        subprocess.run(["fc-cache", "-fv", str(user_fonts)], check=False)
        subprocess.run(["fc-cache", "-fv", str(FONTS_DIR)], check=False)
        logger.info("System fc-cache reloaded successfully!")
    except Exception as e:
        logger.error(f"Error running fc-cache: {e}")

setup_system_fonts()

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

def clean_wm_text(text: str) -> str:
    text = re.sub(r"[{}\\\r\n]", "", text or "").strip()
    return text[:40]

def humanbytes(size: float) -> str:
    if not size:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size) < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

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

def read_subtitle_text(path: Path) -> Tuple[str, str, list]:
    """Reads a subtitle file with encoding detection. Returns (text, encoding name, warnings)."""
    raw = Path(path).read_bytes()
    warnings: list = []
    enc = None
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    elif raw[:200].count(b"\x00") > 20:                          # BOM-less UTF-16
        enc = "utf-16-le" if raw[1:2] == b"\x00" else "utf-16-be"
    text = None
    if enc:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            enc = None
    if text is None:
        try:
            text, enc = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, enc = raw.decode("cp1252", errors="replace"), "cp1252 (fallback)"
            warnings.append("File is not UTF-8; decoded as Windows-1252. Re-save the subtitle as UTF-8 if letters look wrong.")
    if "\ufffd" in text:
        warnings.append("Some characters could not be decoded (replacement characters found).")
    sinhala = sum(1 for ch in text if "\u0d80" <= ch <= "\u0dff")
    latin_ext = sum(1 for ch in text if "\u00c0" <= ch <= "\u00ff")
    if sinhala == 0 and len(text) > 200 and latin_ext / max(len(text), 1) > 0.12:
        warnings.append("Looks like a LEGACY (non-Unicode) Sinhala font encoding - the letters will render wrongly. Convert it to Unicode first.")
    return text, enc, warnings

def detect_subtitle_format(path: Path) -> str:
    """srt | vtt | ass | unknown - by content, not only by extension."""
    try:
        head, _, _ = read_subtitle_text(path)
    except Exception:
        return "unknown"
    h = head[:2000].lstrip("\ufeff")
    if h.startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in h or "[V4+ Styles]" in h or "[V4 Styles]" in h:
        return "ass"
    if re.search(r"\d+:\d{2}:\d{2}[,.]\d{1,3}\s*-->", h):
        return "srt"
    return "unknown"

async def convert_subtitle_to_srt(src: Path, dst: Path) -> bool:
    """VTT / ASS / SSA -> SRT through ffmpeg (ASS styling is lost; text and timing are kept)."""
    rc, _, _ = await _capture(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src), "-c:s", "srt", str(dst)], 90)
    return rc == 0 and dst.exists() and dst.stat().st_size > 0

def sanitize_and_reindex_srt(input_path: Path, output_path: Path) -> dict:
    content, encoding, enc_warnings = read_subtitle_text(input_path)

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

    return {"total_subs": index - 1, "removed_empty": total_removed, "encoding": encoding, "warnings": enc_warnings}

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

STYLE_DEFAULT: Dict[str, Any] = {
    "font": None, "size": int(SUB_FONT_SIZE), "primary": "#FFFFFF", "outline_c": "#000000", "outline": 3.0,
    "shadow": 1.5, "shadow_c": "#000000", "alignment": 2, "margin_v": 50, "margin_h": 60, "bold": True, "spacing": 0.0,
}

STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "Classic White": {"primary": "#FFFFFF", "outline_c": "#000000", "outline": 3.0, "shadow": 1.5, "size": 48, "bold": True},
    "Yellow Cinema": {"primary": "#FFD400", "outline_c": "#000000", "outline": 3.0, "shadow": 2.0, "size": 48, "bold": True},
    "Big Bold": {"primary": "#FFFFFF", "outline_c": "#000000", "outline": 3.5, "shadow": 2.0, "size": 58, "bold": True},
    "Soft Shadow": {"primary": "#FFFFFF", "outline_c": "#1E1E1E", "outline": 1.2, "shadow": 3.0, "size": 48, "bold": True},
    "Minimal": {"primary": "#FFFFFF", "outline_c": "#000000", "outline": 2.0, "shadow": 0.0, "size": 42, "bold": False},
    "Cyan Pop": {"primary": "#5EE7FF", "outline_c": "#00334D", "outline": 3.0, "shadow": 1.5, "size": 50, "bold": True},
    "Top Caption": {"alignment": 8, "margin_v": 40},
}

STYLE_FILE = BASE_DIR / "style_settings.json"

def _load_styles() -> Dict[str, Dict[str, Any]]:
    try:
        with open(STYLE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

STYLES: Dict[str, Dict[str, Any]] = _load_styles()

def merge_style(style: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    st = dict(STYLE_DEFAULT)
    st.update({k: v for k, v in (style or {}).items() if k in STYLE_DEFAULT and v is not None or (k == "font" and k in (style or {}))})
    return st

def get_user_style(user_id: int) -> Dict[str, Any]:
    return merge_style(STYLES.get(str(user_id)))

def save_user_style(user_id: int, style: Dict[str, Any]) -> None:
    STYLES[str(user_id)] = {k: style[k] for k in STYLE_DEFAULT if k in style}
    try:
        with open(STYLE_FILE, "w", encoding="utf-8") as f:
            json.dump(STYLES, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save style settings: {e}")

def style_font_family(style: Optional[Dict[str, Any]] = None) -> str:
    return (merge_style(style).get("font")) or SUB_FONT_FAMILY

def matrix_for(info: Dict[str, Any]) -> str:
    """libass colour matrix of the subtitle script so coloured text matches the video's colour space."""
    cs = info.get("color_space", "")
    if "2020" in cs or info.get("hdr", "SDR") != "SDR":
        return "None"
    rng = "PC" if info.get("color_range") == "pc" else "TV"
    if cs == "bt709" or info.get("height", 0) >= 720:
        return f"{rng}.709"
    return f"{rng}.601"

def default_style_line(family: str, style: Optional[Dict[str, Any]] = None) -> str:
    st = merge_style(style)
    bold = -1 if st["bold"] else 0
    back = "&H80" + ass_colour(st["shadow_c"], "00")[4:]
    return (
        f"Style: Default,{family},{int(st['size'])},{ass_colour(st['primary'], '00')},&H000000FF,"
        f"{ass_colour(st['outline_c'], '00')},{back},{bold},0,0,0,100,100,{st['spacing']:g},0,1,"
        f"{float(st['outline'])},{float(st['shadow'])},{int(st['alignment'])},{int(st['margin_h'])},{int(st['margin_h'])},{int(st['margin_v'])},1"
    )

def convert_srt_to_ass(
    srt_path: Path,
    ass_path: Path,
    watermark_text: Optional[str] = None,
    offset_ms: int = 0,
    clip_len_ms: Optional[int] = None,
    shift_ms: int = 0,
    style: Optional[Dict[str, Any]] = None,
    matrix: str = "TV.601",
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
        "Title: Sinhala Hardcoded Subtitles\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"YCbCr Matrix: {matrix}\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + default_style_line(style_font_family(style), style) + "\n"
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
        formatted_text = formatted_text.replace("\\fnSINH", f"\\fn{SUB_FONT_FAMILY}")

        dialogues.append(
            f"Dialogue: 0,{ms_to_ass_time(start_ms)},{ms_to_ass_time(end_ms)},Default,,0,0,0,,{formatted_text}"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(dialogues) + "\n")

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
        "v_codec": "", "v_bitrate": 0, "v_bitrate_src": "", "pix_fmt": "", "audio": [], "subs": [], "has_audio": False,
        "bit_depth": 8, "chroma": "420", "color_space": "", "color_primaries": "", "color_transfer": "", "color_range": "",
        "sar": "", "dar": "", "profile": "", "field_order": "", "rotation": 0, "nb_frames": 0, "hdr": "SDR",
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
                pf_ = info["pix_fmt"]
                info["bit_depth"] = _to_int(st.get("bits_per_raw_sample")) or (12 if "12" in pf_ else 10 if "10" in pf_ else 8)
                info["chroma"] = "444" if "444" in pf_ else "422" if "422" in pf_ else "420"
                for k_, f_ in (("color_space", "color_space"), ("color_primaries", "color_primaries"),
                               ("color_transfer", "color_transfer"), ("color_range", "color_range"),
                               ("sar", "sample_aspect_ratio"), ("dar", "display_aspect_ratio"),
                               ("profile", "profile"), ("field_order", "field_order")):
                    v_ = str(st.get(f_, "") or "")
                    info[k_] = "" if v_ in ("unknown", "N/A", "0:1") else v_
                ct_ = info["color_transfer"]
                info["hdr"] = "HDR10" if "smpte2084" in ct_ else ("HLG" if "arib" in ct_ else "SDR")
                info["nb_frames"] = _to_int(st.get("nb_frames"))
                for sd_ in st.get("side_data_list", []) or []:
                    if "rotation" in sd_:
                        info["rotation"] = _to_int(sd_.get("rotation"))
                info["v_bitrate"] = _to_int(st.get("bit_rate"))
                info["v_bitrate_src"] = "stream" if info["v_bitrate"] else ""
                if not info["v_bitrate"]:
                    tl_ = {str(k).lower(): v for k, v in (st.get("tags") or {}).items()}
                    info["v_bitrate"] = _to_int(tl_.get("bps")) or _to_int(tl_.get("bps-eng"))
                    info["v_bitrate_src"] = "tag" if info["v_bitrate"] else ""
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

def fmt_hms(secs: float) -> str:
    secs = int(max(secs, 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def fmt_fps(fps: float) -> str:
    return f"{fps:.3f}".rstrip("0").rstrip(".") if fps else "?"

SUPPORTED_EXT = (".mkv", ".mp4", ".mov", ".m4v")

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

def build_plan(info: Dict[str, Any], ext: str, speed: str = DEFAULT_SPEED, keep_audio=None, keep_subs=None,
               demo: bool = False, clip_secs: Optional[float] = None) -> Dict[str, Any]:
    """Everything needed to keep the original quality: codec policy, bitrate rule and the CHOSEN audio / subtitle tracks.
    speed = fast | balanced | best (size-guarded, as before)  or  auto | hq | lossless (see quality_engine.py)."""
    speed = "fast" if demo else (speed if speed in qe.MODES else "fast")
    policy = qe.decide_policy(dict(info, clip_secs=clip_secs or info.get("duration", 0)), ext, speed, requested=speed)
    vargs, vlabel = policy.vargs, policy.vlabel
    v_br = policy.cap_bps or policy.source_bps or source_video_bitrate(info)

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
        "ext": ext, "faststart": ext in (".mp4", ".m4v", ".mov"), "speed": policy.mode,
        "policy": policy, "requested_mode": speed,
    }

def _title_from_stem(stem: str) -> str:
    stem = re.sub(r"(?i)\b([hx])\.(26[45])\b", "\\1\x00\\2", stem)
    # a dot is kept only for short numbers like 5.1 / 2.0 (so "2026.720p" still becomes "2026 720p")
    stem = re.sub(r"\.(?!(?<=\d\.)\d{1,2}(?!\d))", " ", stem)
    stem = stem.replace("_", " ").replace("\x00", ".")
    return re.sub(r"\s+", " ", stem).strip()

def make_title(filename: str) -> str:
    """'Movie.Name.2026.720p.WEB-DL.mkv' -> 'Movie Name 2026 720p WEB-DL' (5.1 / H.264 kept intact)."""
    return _title_from_stem(Path(filename).stem)

_JUNK_TAIL = {"esub", "esubs", "msub", "msubs", "sub", "subs", "hc", "hardsub", "dual", "audio", "multi", "multisub", "org", "dubbed"}

def clean_release_name(filename: str) -> str:
    """
    'Jujutsu Kaisen S01E09 ... 720p WEBRip x265 HEVC [ Hindi AAC 2.0 ~ 128kbps + English ] ESub ~ VijayRaj ~ mkvAnime.mkv'
      -> 'Jujutsu Kaisen S01E09 ... 720p WEBRip x265 HEVC'
    Removes [..] / {..} groups, (..) groups (a (2026) year stays), everything after a '~', release-group suffixes
    ('x264-GRP') and trailing words like ESub / Dual Audio.
    """
    stem = Path(filename).stem if Path(filename).suffix.lower() in (".mkv", ".mp4", ".mov", ".m4v", ".avi", ".ts", ".webm") else filename
    s_ = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", stem)
    s_ = re.sub(r"\((?!\s*(?:19|20)\d{2}\s*\))[^)]*\)", " ", s_)
    s_ = s_.split("~")[0]
    s_ = _title_from_stem(s_)
    s_ = re.sub(r"(?i)\b(x26[45]|h\.?26[45]|hevc|avc|av1|aac\d?(?:\.\d)?|ddp?\d(?:\.\d)?|dts|truehd)-[A-Za-z0-9]+\s*$", r"\1", s_)
    tokens = s_.split()
    while tokens and tokens[-1].lower().strip("-+&.,") in _JUNK_TAIL | {"", "+", "-", "&"}:
        tokens.pop()
    out = " ".join(tokens).strip(" -_.+")
    return out or _title_from_stem(Path(filename).stem)

NAME_DEFAULT: Dict[str, Any] = {"clean": True, "prefix": "", "suffix": "සිංහල උපසිරැසි සමග"}
NAME_FILE = BASE_DIR / "name_settings.json"

def _load_name_settings() -> Dict[str, Dict[str, Any]]:
    try:
        with open(NAME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

NAME_SETTINGS: Dict[str, Dict[str, Any]] = _load_name_settings()

def get_name_settings(user_id: int) -> Dict[str, Any]:
    st = dict(NAME_DEFAULT)
    st.update(NAME_SETTINGS.get(str(user_id), {}))
    return st

def save_name_settings(user_id: int, **changes: Any) -> Dict[str, Any]:
    st = get_name_settings(user_id)
    st.update(changes)
    NAME_SETTINGS[str(user_id)] = st
    try:
        with open(NAME_FILE, "w", encoding="utf-8") as f:
            json.dump(NAME_SETTINGS, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not save name settings: {e}")
    return st

def build_display_title(filename: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """Title used for the caption AND the file name: [prefix] cleaned-name [suffix tag]."""
    st = dict(NAME_DEFAULT, **(settings or {}))
    base = clean_release_name(filename) if st["clean"] else _title_from_stem(Path(filename).stem)
    return " ".join(p for p in (st["prefix"].strip(), base, st["suffix"].strip()) if p)

def build_output_filename(filename: str, ext: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """Clean + Tag mode:  'Jujutsu Kaisen S01E09 ... 720p WEBRip x265 HEVC සිංහල උපසිරැසි සමග.mkv'
       Original mode (and no tags): the original name exactly, only the extension follows the container."""
    st = dict(NAME_DEFAULT, **(settings or {}))
    if not st["clean"] and not st["prefix"].strip() and not st["suffix"].strip():
        return Path(filename).stem + ext
    if st["clean"]:
        title = build_display_title(filename, st)
    else:                                         # original name + the user's tags
        title = " ".join(p for p in (st["prefix"].strip(), Path(filename).stem, st["suffix"].strip()) if p)
    title = safe_filename(title, 200)
    while len((title + ext).encode("utf-8")) > 240 and len(title) > 20:      # Telegram file-name byte limit
        title = title[:-4].rstrip()
    return title + ext

def build_caption(orig_name: str, height: int, vlabel: str, clip_secs: float, audio_labels: list,
                  title: Optional[str] = None) -> str:
    lines = [
        (title if title is not None else f"{make_title(orig_name)} සිංහල උපසිරසි සමග")[:400],
        f"🎬 {height}p {vlabel} ⏳ {fmt_hms(round(clip_secs))}",
    ]
    if audio_labels:
        lines.append("🔊 " + " , ".join(audio_labels))
    lines.append("💬 Subtitle - සිංහල උපසිරැසි සහිතව.")
    return "\n".join(lines)[:1024]

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
        shift_ms=int(delay_ms), style=style, matrix=matrix_for(info),
    )

    abs_output_path = output_path.resolve()
    ass_path_esc = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts_dir_esc = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
    vf_filter = f"ass='{ass_path_esc}':fontsdir='{fonts_dir_esc}':shaping=complex"
    job["vf_info"] = {"vf": vf_filter, "ass": str(ass_path)}

    # ---- keep the original quality: same resolution / fps / codec family, bitrate capped to the source ----
    v_br = plan["v_br"]

    # Input seeking (-ss) + stream-copied audio makes ffmpeg start the audio at the previous KEYFRAME while the
    # video starts exactly at -ss, so subtitles drift. For trimmed clips the audio is therefore cut first
    # (still a bit-exact copy) and used as a second input; the video then starts exactly at 0 like the audio.
    audio_cut = None
    if start_secs > 0 and (plan["audio_idx"] or plan["sub_idx"]):
        audio_cut = work_dir / f"audiocut_{time.time_ns()}.mkv"
        await UI.status(status_msg, "🎧 Cutting audio / subtitle tracks...", cancel=True)
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
    pol = plan["policy"]
    rate_txt = "lossless" if pol.lossless else f"CRF {pol.crf}"
    cap_txt = f"cap {pol.cap_bps / 1_000_000:.2f} Mbps" if pol.cap_bps else "no cap"
    quality_line = (
        f"📐 {info['width']}×{info['height']} • {fmt_fps(info['fps'])} fps • {plan['vlabel']} • {pol.pix_fmt}\n"
        f"⚙️ {pol.label} • {pol.encoder} {pol.preset} • {rate_txt} • {cap_txt}\n🔊 {a_desc}"
    )
    for w_ in pol.warnings[:2]:
        quality_line += f"\n⚠️ {w_}"

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
    await UI.progress(status_msg, 0, clip_secs, start_proc_time, quality_line)

    prog: Dict[str, str] = {}
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="ignore").strip()
            if "=" in line_str:
                k_, _, v_ = line_str.partition("=")
                prog[k_] = v_
            if line_str.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    current_secs = int(line_str.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                state["last_progress"] = time.time()
                if current_secs >= 0:
                    await UI.progress(status_msg, current_secs, clip_secs, start_proc_time, quality_line, extra=dict(prog))
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
        if ass_path.exists() and not job.get("keep_ass"):
            ass_path.unlink()
        _unlink(audio_cut)
        job["proc"] = None

    job["ffmpeg_stderr"] = stderr_tail["text"]
    if job.get("cancelled"):
        return False, "cancelled"
    if state["stalled"]:
        return False, "stalled"
    if process.returncode != 0:
        logger.error("FFmpeg process failed:\n" + stderr_tail["text"][-3000:])
        return False, "ffmpeg_error"

    ok = abs_output_path.exists() and abs_output_path.stat().st_size > 0
    return ok, "ok" if ok else "no_output"

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

FONT_SAMPLE_LINES = [
    "කි කී කු කූ කෘ කෙ කේ කො කෝ කෞ කා කැ කෑ කං",
    "ක්\u200dර ක්\u200dය ශ්\u200dරී ප්\u200dරශ්නය වෛද්\u200dය ක්\u200dෂ",
    "ඔබට සුබ දවසක් වේවා! 1080p",
]

def sinhala_font_files() -> list:
    """Font files in the fonts folder that support Sinhala."""
    found = []
    for p in sorted(FONTS_DIR.iterdir()):
        if p.suffix.lower() not in (".ttf", ".otf") or p.stat().st_size < 10000:
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

async def _run_quiet(cmd: list) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()

async def render_font_sample(family: str, out_png: Path, style: Optional[Dict[str, Any]] = None) -> bool:
    """Render the sample with the exact same ffmpeg + libass (complex shaping) pipeline as the real job."""
    ass_path = out_png.with_suffix(".ass")
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.601\nPlayResX: 1920\nPlayResY: 330\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + default_style_line(family, dict(style or {}, alignment=2, margin_v=50)) + "\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    text = "\\N".join(normalize_sinhala_text(l) for l in FONT_SAMPLE_LINES)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + f"Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{text}\n")

    ass_esc = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts_esc = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
    await _run_quiet([
        "ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x2b2b2b:s=1920x330:d=1",
        "-vf", f"ass='{ass_esc}':fontsdir='{fonts_esc}':shaping=complex",
        "-frames:v", "1", str(out_png),
    ])
    _unlink(ass_path)
    return out_png.exists() and out_png.stat().st_size > 0

async def render_style_preview(out_png: Path, style: Optional[Dict[str, Any]], video: Optional[Path] = None,
                               at_secs: float = 0.0, srt: Optional[Path] = None, delay_ms: int = 0,
                               matrix: str = "TV.709") -> bool:
    """One frame with the CURRENT style: a real video frame + the SRT cue active there, or sample text on a dark card."""
    st = merge_style(style)
    ass_path = out_png.with_suffix(".ass")
    if srt is not None and video is not None:
        convert_srt_to_ass(srt, ass_path, None, offset_ms=int(at_secs * 1000), shift_ms=int(delay_ms), style=st, matrix=matrix)
    else:
        text = "\\N".join(normalize_sinhala_text(l) for l in FONT_SAMPLE_LINES)
        header = (
            "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"YCbCr Matrix: {matrix}\nPlayResX: 1920\nPlayResY: 1080\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + default_style_line(style_font_family(st), st) + "\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            f"Dialogue: 0,0:00:00.00,9:59:59.99,Default,,0,0,0,,{text}\n")
        ass_path.write_text(header, encoding="utf-8")
    ass_esc = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:")
    fonts_esc = str(FONTS_DIR.resolve()).replace("\\", "/").replace(":", "\\:")
    vf = f"ass='{ass_esc}':fontsdir='{fonts_esc}':shaping=complex,scale=1280:-2"
    src = (["-ss", f"{max(at_secs, 0):.3f}", "-i", str(video)] if video is not None else
           ["-f", "lavfi", "-i", "gradients=s=1920x1080:c0=0x1d2b3a:c1=0x6b7a8f:d=1"])
    await _run_quiet(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error", *src,
                      "-vf", vf, "-frames:v", "1", str(out_png)])
    _unlink(ass_path)
    return out_png.exists() and out_png.stat().st_size > 0

def preview_time_for(srt_path: Optional[Path], duration: float, delay_ms: int = 0, frac: float = 0.25) -> float:
    """Middle of a real cue near `frac` of the film (so the preview shows genuine subtitle text)."""
    try:
        cues = [c for c in parse_srt_cues(srt_path) if not c["positioned"] and c["e"] - c["s"] >= 1.2] if srt_path else []
    except Exception:
        cues = []
    if not cues:
        return max(duration * frac, 0.0)
    target = duration * frac - delay_ms / 1000
    best = min(cues, key=lambda c: abs(c["s"] - target))
    return max((best["s"] + best["e"]) / 2 + delay_ms / 1000, 0.0)

def _unlink(p: Any) -> None:
    try:
        if p and Path(p).exists():
            Path(p).unlink()
    except Exception:
        pass

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
        await UI.simple(status_msg, title, 0, total_secs, start)
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
                await UI.simple(status_msg, title, cur, total_secs, start)
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
        "📋 **Metadata**\n",
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

SYNC_TOL_START = float(os.environ.get("SYNC_TOL_START", "0.7"))   # seconds

SYNC_TOL_END = float(os.environ.get("SYNC_TOL_END", "1.2"))

SYNC_STAGES = ["Parse", "Offset", "Match", "Classify", "Report"]

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


# ==============================================================================
# OUTPUT VERIFICATION (completeness, playability, A/V sync, subtitles, metrics, lossless frame check)
# ==============================================================================
async def _capture(cmd: list, timeout: float = 120) -> Tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode("utf-8", errors="ignore"), err.decode("utf-8", errors="ignore")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -9, "", "timeout"
    except Exception as e:
        return -1, "", str(e)

async def decode_check(path: Path, duration: float) -> Tuple[bool, str]:
    """Decode a few seconds at the start / middle / end. Any decoder error means the file is not safely playable."""
    errors = []
    points = [0.0] if duration < 8 else [0.0, duration / 2, max(duration - 3.0, 0)]
    for t in points:
        rc, _, err = await _capture(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.2f}", "-t", "2",
                                     "-i", str(path), "-map", "0:v:0", "-f", "null", "-"], 90)
        if rc != 0 or err.strip():
            errors.append(err.strip()[:160] or f"rc={rc}")
    return (not errors), "; ".join(errors[:2])

_BAND = {"bottom": "crop=iw:ih*0.30:0:ih*0.70", "top": "crop=iw:ih*0.30:0:0",
         "mid": "crop=iw:ih*0.30:0:ih*0.35"}

async def _psnr(src: Path, s_ts: float, out: Path, o_ts: float, band: str) -> Optional[float]:
    crop = _BAND[band]
    fc = f"[0:v]{crop},setpts=PTS-STARTPTS[a];[1:v]{crop},setpts=PTS-STARTPTS[b];[a][b]psnr"
    rc, _, err = await _capture(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{s_ts:.3f}", "-i", str(src),
                                 "-ss", f"{o_ts:.3f}", "-i", str(out), "-frames:v", "1", "-filter_complex", fc,
                                 "-f", "null", "-"], 60)
    m = re.search(r"average:(\S+)", err)
    if not m:
        return None
    return 100.0 if m.group(1) == "inf" else float(m.group(1))

def sample_cue_times(srt_path: Path, start_secs: float, clip_secs: float, delay_ms: int = 0) -> Tuple[list, Optional[float]]:
    """Up to 3 cue mid-points inside the clip (clip time) and one quiet moment without any cue."""
    try:
        cues = [c for c in parse_srt_cues(srt_path) if not c["positioned"]]
    except Exception:
        return [], None
    d = delay_ms / 1000
    inside = [(c["s"] + d - start_secs, c["e"] + d - start_secs) for c in cues]
    inside = [(a, b) for a, b in inside if a >= 0.2 and b <= clip_secs - 0.2 and b - a >= 1.0]
    picks = []
    if inside:
        for frac in (0.2, 0.5, 0.8):
            a, b = inside[min(int(len(inside) * frac), len(inside) - 1)]
            t = (a + b) / 2
            if all(abs(t - p) > 1 for p in picks):
                picks.append(t)
    return picks, None

async def subtitle_presence(src: Path, out: Path, start_secs: float, times: list, band: str = "bottom") -> Tuple[Optional[int], int]:
    """Heuristic: in the subtitle band the output must differ MORE from the source than in the opposite (control) band."""
    control = "top" if band != "top" else "bottom"
    hits = 0
    for t in times:
        pb = await _psnr(src, start_secs + t, out, t, band)
        pc = await _psnr(src, start_secs + t, out, t, control)
        if pb is None or pc is None:
            return None, len(times)
        if pb < pc - 2.5 or (pb < 60 and pc >= 90):
            hits += 1
    return hits, len(times)

async def ssim_psnr_samples(src: Path, out: Path, start_secs: float, clip_secs: float, band_control: str = "top") -> Optional[Tuple[float, float, int]]:
    """Mean SSIM / PSNR over up to 3 short windows, measured on the region subtitles normally do not touch."""
    crop = {"top": "crop=iw:ih*0.55:0:0", "bottom": "crop=iw:ih*0.55:0:ih*0.45"}[band_control]
    win = min(6.0, max(clip_secs / 4, 1.0))
    spots = [0.1, 0.5, 0.9] if clip_secs > 20 else [0.3]
    ss_vals, ps_vals = [], []
    for f in spots:
        a = max(min(clip_secs * f, clip_secs - win - 0.1), 0.0)
        fc = (f"[0:v]{crop},setpts=PTS-STARTPTS[a];[1:v]{crop},setpts=PTS-STARTPTS[b];[a][b]ssim;")
        rc, _, err = await _capture(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start_secs + a:.3f}", "-t", f"{win:.2f}", "-i", str(src),
                                     "-ss", f"{a:.3f}", "-t", f"{win:.2f}", "-i", str(out), "-filter_complex", fc.rstrip(";"),
                                     "-f", "null", "-"], 120)
        m = re.search(r"All:([0-9.]+)", err)
        if m:
            ss_vals.append(float(m.group(1)))
        fc2 = fc.replace("ssim", "psnr").rstrip(";")
        rc, _, err = await _capture(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start_secs + a:.3f}", "-t", f"{win:.2f}", "-i", str(src),
                                     "-ss", f"{a:.3f}", "-t", f"{win:.2f}", "-i", str(out), "-filter_complex", fc2, "-f", "null", "-"], 120)
        m = re.search(r"average:(\S+)", err)
        if m:
            ps_vals.append(100.0 if m.group(1) == "inf" else float(m.group(1)))
    if not ss_vals:
        return None
    return sum(ss_vals) / len(ss_vals), (sum(ps_vals) / len(ps_vals) if ps_vals else 0.0), len(ss_vals)

async def lossless_frame_check(src: Path, vf: str, pix_fmt: str, out: Path, start_secs: float, seconds: float) -> Tuple[Optional[bool], int]:
    """Frame-level proof: md5 of the subtitle-rendered source frames == md5 of the decoded output frames."""
    def hashes(txt: str) -> list:
        return [ln.split(",")[-1].strip() for ln in txt.splitlines() if ln and not ln.startswith("#")]
    ref_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start_secs > 0:
        ref_cmd += ["-ss", f"{start_secs:.3f}"]
    ref_cmd += ["-t", f"{seconds:.2f}", "-i", str(src), "-map", "0:v:0", "-vf", f"{vf},format={pix_fmt}", "-f", "framemd5", "-"]
    out_cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-t", f"{seconds:.2f}", "-i", str(out),
               "-map", "0:v:0", "-vf", f"format={pix_fmt}", "-f", "framemd5", "-"]
    rc1, o1, _ = await _capture(ref_cmd, 300)
    rc2, o2, _ = await _capture(out_cmd, 300)
    h1, h2 = hashes(o1), hashes(o2)
    if rc1 != 0 or rc2 != 0 or not h1 or not h2:
        return None, 0
    n = min(len(h1), len(h2))
    return (h1[:n] == h2[:n] and abs(len(h1) - len(h2)) <= 1), n

async def verify_output(src_path: Path, src_info: Dict[str, Any], out_path: Path, plan: Dict[str, Any], start_secs: float,
                        clip_secs: float, srt_path: Optional[Path], delay_ms: int, job: Dict[str, Any],
                        sub_band: str = "bottom") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Runs every post-encode check. Returns (output probe info, checks dict for quality_engine.comparison_report)."""
    out_info = await probe_video(out_path)
    raw = await probe_raw(out_path)
    pol = plan["policy"]
    checks: Dict[str, Any] = {"audio_expected": len(plan["audio_idx"])}
    checks["complete"] = out_info["duration"] > 0 and abs(out_info["duration"] - clip_secs) <= max(1.5, clip_secs * 0.01)
    checks["resolution_ok"] = (out_info["width"], out_info["height"]) == (src_info["width"], src_info["height"])
    checks["fps_ok"] = abs((out_info["fps"] or 0) - (src_info["fps"] or 0)) <= 0.01 * max(src_info["fps"] or 1, 1)
    checks["codec_same"] = out_info["v_codec"] == src_info["v_codec"]
    checks["bitdepth_ok"] = out_info["bit_depth"] == src_info["bit_depth"]
    checks["audio_ok"] = len(out_info["audio"]) == checks["audio_expected"]
    # A/V sync from the stream start times / durations of the first video and first audio stream
    try:
        vs = next(x for x in raw.get("streams", []) if x.get("codec_type") == "video")
        a_streams = [x for x in raw.get("streams", []) if x.get("codec_type") == "audio"]
        if a_streams:
            as_ = a_streams[0]
            vstart, astart = float(vs.get("start_time") or 0), float(as_.get("start_time") or 0)
            vd, ad = _stream_duration(vs), _stream_duration(as_)
            ok = abs(vstart - astart) < 0.25 and (not (vd and ad) or abs(vd - ad) < 0.8)
            checks["av_sync_ok"] = ok
        else:
            checks["av_sync_ok"] = None
    except Exception:
        checks["av_sync_ok"] = None
    play_ok, play_err = await decode_check(out_path, out_info["duration"])
    checks["playable"] = play_ok
    checks["decode_errors"] = play_err
    errs = (job.get("ffmpeg_stderr") or "").strip()
    checks["ffmpeg_warnings"] = len(errs.splitlines()) if errs else 0

    if srt_path is not None and qe.metric_filters_available().get("psnr"):
        times, _ = sample_cue_times(srt_path, start_secs, clip_secs, delay_ms)
        if times:
            hits, total = await subtitle_presence(src_path, out_path, start_secs, times, sub_band)
            checks["subtitle_text"] = ("NOT VERIFIED" if hits is None else
                                       (f"DETECTED ({hits}/{total} sampled cues)" if hits else f"NOT DETECTED (0/{total})"))
        else:
            checks["subtitle_text"] = "NOT VERIFIED (no sample cue in this clip)"
    if qe.QUALITY_METRICS and qe.metric_filters_available().get("ssim"):
        res = await ssim_psnr_samples(src_path, out_path, start_secs, clip_secs, "top" if sub_band != "top" else "bottom")
        if res:
            psnr_txt = ">99 dB" if res[1] >= 99.9 else f"{res[1]:.1f} dB"
            checks["quality_text"] = f"SSIM {res[0]:.4f} | PSNR {psnr_txt} ({res[2]} sample(s), subtitle-free area)"
    if pol.lossless and job.get("vf_info"):
        secs = min(qe.QUALITY_LOSSLESS_CHECK_SECS, clip_secs)
        ok, n = await lossless_frame_check(src_path, job["vf_info"]["vf"], pol.pix_fmt, out_path, start_secs, secs)
        checks["lossless_text"] = ("NOT VERIFIED" if ok is None else
                                   (f"VERIFIED ({n} frames bit-identical to the rendered source)" if ok else "FAILED - frames differ"))
    return out_info, checks

