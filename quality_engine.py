"""
quality_engine.py - QUALITY & ENCODING POLICY ENGINE (stdlib only, no Telegram code)

Holds every encoding-quality setting (existing ENCODE_* environment variables are kept 1:1) and the policy
helpers that decide *how* a video is encoded. video_engine.py asks this module for a policy; this module never
imports video_engine.py / main.py (no circular imports).
"""
import os
import re
import math
import shutil
import logging
import subprocess
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("SRT_Hardcoder_Bot")

ENCODE_CODEC = os.environ.get("ENCODE_CODEC", "auto")               # auto = same codec as the source, or h264 / hevc

ENCODE_PRESET_HEVC = os.environ.get("ENCODE_PRESET_HEVC", "fast")           # HEVC Best quality

ENCODE_CRF_HEVC = os.environ.get("ENCODE_CRF_HEVC", "22")           # x265 CRF 22 ~ x264 CRF 18

ENCODE_PRESET_FAST = os.environ.get("ENCODE_PRESET_FAST", "veryfast")             # H.264 Fast

ENCODE_PRESET_BAL = os.environ.get("ENCODE_PRESET_BAL", "faster")                 # H.264 Balanced

ENCODE_PRESET_HEVC_FAST = os.environ.get("ENCODE_PRESET_HEVC_FAST", "superfast")  # HEVC Fast

ENCODE_PRESET_HEVC_BAL = os.environ.get("ENCODE_PRESET_HEVC_BAL", "veryfast")     # HEVC Balanced

DEFAULT_SPEED = os.environ.get("DEFAULT_SPEED", "fast")                           # fast | balanced | best

ENCODE_PRESET = os.environ.get("ENCODE_PRESET", "medium")            # H.264 Best quality

ENCODE_CRF = os.environ.get("ENCODE_CRF", "18")                     # quality (lower = better)

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

def preset_for(speed: str, hevc: bool) -> str:
    if hevc:
        return {"fast": ENCODE_PRESET_HEVC_FAST, "balanced": ENCODE_PRESET_HEVC_BAL}.get(speed, ENCODE_PRESET_HEVC)
    return {"fast": ENCODE_PRESET_FAST, "balanced": ENCODE_PRESET_BAL}.get(speed, ENCODE_PRESET)


# ==============================================================================
# NEW: QUALITY MODES, CODEC-AWARE POLICY, BITRATE PROTECTION, AUTO SELECTION, REPORTS
# ==============================================================================
# Existing modes (kept exactly as before):   fast | balanced | best   -> "source-size guard": CRF + max bitrate = source
# New modes:                                 hq (HIGH_QUALITY) | lossless | auto
#   hq        quality-driven CRF, NO bitrate cap (file may be larger than the source)
#   lossless  genuine lossless encoder mode (x265 lossless=1 / x264 qp 0 / libaom lossless) - very large files
#   auto      reads the source (codec, bits per pixel, resolution, CPU) and picks one of the modes above
MODES = ("fast", "balanced", "best", "auto", "hq", "lossless")
LEGACY_MODES = ("fast", "balanced", "best")

QUALITY_HQ_PRESET_X265 = os.environ.get("QUALITY_HQ_PRESET_X265", "medium")
QUALITY_HQ_CRF_X265 = os.environ.get("QUALITY_HQ_CRF_X265", "16")
QUALITY_HQ_PRESET_X264 = os.environ.get("QUALITY_HQ_PRESET_X264", "medium")
QUALITY_HQ_CRF_X264 = os.environ.get("QUALITY_HQ_CRF_X264", "14")
QUALITY_HQ_PRESET_AV1 = os.environ.get("QUALITY_HQ_PRESET_AV1", "6")           # SVT-AV1 preset (0 slow .. 13 fast)
QUALITY_HQ_CRF_AV1 = os.environ.get("QUALITY_HQ_CRF_AV1", "22")
QUALITY_HQ_MAX_FACTOR = float(os.environ.get("QUALITY_HQ_MAX_FACTOR", "0"))    # 0 = no bitrate cap in HQ mode
QUALITY_LOSSLESS_PRESET_X265 = os.environ.get("QUALITY_LOSSLESS_PRESET_X265", "fast")
QUALITY_LOSSLESS_PRESET_X264 = os.environ.get("QUALITY_LOSSLESS_PRESET_X264", "veryfast")
QUALITY_AUTO_TIME_BUDGET_MIN = float(os.environ.get("QUALITY_AUTO_TIME_BUDGET_MIN", "120"))
QUALITY_VERIFY = os.environ.get("QUALITY_VERIFY", "1") == "1"                  # post-encode checks
QUALITY_METRICS = os.environ.get("QUALITY_METRICS", "1") == "1"                # SSIM / PSNR sampling
QUALITY_LOSSLESS_CHECK_SECS = float(os.environ.get("QUALITY_LOSSLESS_CHECK_SECS", "10"))

MODE_INFO = {
    "fast": ("⚡ Fast", "Fastest. File never larger than the source (size guard)."),
    "balanced": ("⚖ Balanced", "Quality / speed balance. Size guard like Fast."),
    "best": ("💎 Best", "Best quality that still fits the source size (size guard, slower)."),
    "auto": ("🤖 Auto", "Reads the source and picks the mode itself."),
    "hq": ("🧪 High Quality", "Minimum visible loss. NO bitrate cap - file can be larger than the source."),
    "lossless": ("♾ Lossless", "Genuine lossless encode. Huge files, very slow."),
}


class PolicyError(Exception):
    """The requested mode cannot be honoured safely (e.g. no lossless-capable encoder)."""


# ---------------- encoder capabilities (cached) ----------------
_ENC_CACHE: Dict[str, Any] = {}

def _run(cmd: List[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""

def available_encoders() -> set:
    if "all" not in _ENC_CACHE:
        names = set()
        for ln in _run(["ffmpeg", "-hide_banner", "-encoders"]).splitlines():
            m = re.match(r"^\s*V\S*\s+(\S+)", ln)
            if m:
                names.add(m.group(1))
        _ENC_CACHE["all"] = names
    return _ENC_CACHE["all"]

def encoder_available(name: str) -> bool:
    return name in available_encoders()

def encoder_pix_fmts(name: str) -> set:
    key = "pf:" + name
    if key not in _ENC_CACHE:
        out = _run(["ffmpeg", "-hide_banner", "-h", f"encoder={name}"])
        m = re.search(r"Supported pixel formats:\s*(.+)", out)
        _ENC_CACHE[key] = set(m.group(1).split()) if m else set()
    return _ENC_CACHE[key]

def metric_filters_available() -> Dict[str, bool]:
    if "filters" not in _ENC_CACHE:
        out = _run(["ffmpeg", "-hide_banner", "-filters"])
        _ENC_CACHE["filters"] = {n: bool(re.search(rf"\s{n}\s", out)) for n in ("ssim", "psnr", "libvmaf")}
    return _ENC_CACHE["filters"]


# ---------------- source bitrate protection ----------------
def source_bitrate_info(info: Dict[str, Any]) -> Tuple[int, str]:
    """
    (video bits/s, reliability). The container bitrate is NEVER used as if it were the video stream bitrate:
      stream     the video stream's own bit_rate field
      tag        mkvmerge BPS statistics tag of the video stream
      derived    container bitrate minus the audio tracks (approximation)
      estimated  file size / duration minus the audio tracks (approximation)
      unknown    nothing usable -> callers must not impose a cap
    """
    if info.get("v_bitrate", 0) > 0:
        return info["v_bitrate"], (info.get("v_bitrate_src") or "stream")
    a_total = sum((t["bitrate"] or 128_000) for t in info.get("audio", []))
    if info.get("fmt_bitrate", 0) > 0:
        return max(info["fmt_bitrate"] - a_total, 300_000), "derived"
    if info.get("duration", 0) > 0 and info.get("size", 0) > 0:
        return max(int(info["size"] * 8 / info["duration"]) - a_total, 300_000), "estimated"
    return 0, "unknown"


# ---------------- helpers ----------------
def _hms(secs: float) -> str:
    secs = int(max(secs, 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _fps_txt(fps: float) -> str:
    return f"{fps:.3f}".rstrip("0").rstrip(".") if fps else "?"

def _bytes_txt(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"

def codec_key_of(codec_name: str) -> str:
    return {"hevc": "hevc", "h264": "h264", "av1": "av1"}.get(codec_name, "")

def cpu_count() -> int:
    try:
        return max(os.cpu_count() or 2, 1)
    except Exception:
        return 2


# ---------------- codec / pixel format / colour policy ----------------
def pick_codec(info: Dict[str, Any], ext: str, mode: str) -> Tuple[str, List[str]]:
    """Returns (codec key hevc|h264|av1, notes). Never converts everything to HEVC."""
    notes: List[str] = []
    src = codec_key_of(info.get("v_codec", ""))
    forced = ENCODE_CODEC if ENCODE_CODEC in ("h264", "hevc", "av1") else ""
    if forced:
        key = forced
        notes.append(f"Codec forced by ENCODE_CODEC={forced}.")
    elif src == "av1":
        if mode in ("hq", "lossless"):
            key = "av1"
            notes.append("AV1 source: AV1 kept.")
        else:
            key = "h264"                       # legacy size-guarded modes keep their old behaviour
            notes.append("AV1 source: size-guarded modes use H.264 (use High Quality / Lossless to keep AV1).")
    elif src in ("hevc", "h264"):
        key = src
    else:
        # unknown / legacy codec (vp9, mpeg4, mpeg2, prores ...): H.264 unless the source needs >8 bit or non-4:2:0
        needs_hevc = info.get("bit_depth", 8) > 8 or info.get("chroma", "420") != "420"
        key = "hevc" if (needs_hevc and mode in ("hq", "lossless")) else "h264"
        notes.append(f"Source codec '{info.get('v_codec', '?')}' cannot be kept; using {'HEVC' if key == 'hevc' else 'H.264'}.")
    if key == "av1":
        enc_ok = encoder_available("libaom-av1") if mode == "lossless" else (encoder_available("libsvtav1") or encoder_available("libaom-av1"))
        if not enc_ok:
            key = "hevc" if encoder_available("libx265") else "h264"
            notes.append("No suitable AV1 encoder in this FFmpeg build; fell back to " + ("HEVC." if key == "hevc" else "H.264."))
    if key == "hevc" and not encoder_available("libx265"):
        key = "h264"
        notes.append("libx265 not available; fell back to H.264.")
    return key, notes

def encoder_for(key: str, mode: str) -> str:
    if key == "hevc":
        return "libx265"
    if key == "av1":
        if mode == "lossless":
            return "libaom-av1"
        return "libsvtav1" if encoder_available("libsvtav1") else "libaom-av1"
    return "libx264"

def pick_pix_fmt(info: Dict[str, Any], encoder: str, preserve: bool, legacy_hevc: bool) -> Tuple[str, List[str]]:
    """preserve=True keeps the source pixel format / bit depth whenever the encoder supports it."""
    notes: List[str] = []
    src = info.get("pix_fmt") or "yuv420p"
    if not preserve:                                           # legacy rule (unchanged behaviour)
        if legacy_hevc and info.get("bit_depth", 8) >= 10:
            return "yuv420p10le", notes
        return "yuv420p", notes
    supported = encoder_pix_fmts(encoder)
    if not supported or src in supported:
        return src, notes
    depth, chroma = info.get("bit_depth", 8), info.get("chroma", "420")
    for cand in (f"yuv{chroma}p{'' if depth <= 8 else str(depth) + 'le'}", "yuv420p10le" if depth > 8 else "yuv420p"):
        if cand in supported:
            notes.append(f"{src} is not supported by {encoder}; using {cand}.")
            return cand, notes
    notes.append(f"{src} is not supported by {encoder}; using yuv420p (bit depth / chroma reduced).")
    return "yuv420p", notes

def color_args(info: Dict[str, Any]) -> List[str]:
    """Explicit colour tags so primaries / transfer / matrix / range survive the re-encode."""
    args: List[str] = []
    for opt, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                     ("-colorspace", "color_space"), ("-color_range", "color_range")):
        v = info.get(key, "")
        if v and v not in ("unknown", "unspecified"):
            args += [opt, v]
    return args


# ---------------- policy ----------------
class EncodePolicy:
    def __init__(self, **kw):
        self.mode = kw.get("mode", "fast")
        self.requested = kw.get("requested", self.mode)
        self.label = kw.get("label", "")
        self.codec = kw.get("codec", "h264")
        self.encoder = kw.get("encoder", "libx264")
        self.vargs: List[str] = kw.get("vargs", [])
        self.preset = kw.get("preset", "")
        self.crf = kw.get("crf")
        self.cap_bps: Optional[int] = kw.get("cap_bps")
        self.pix_fmt = kw.get("pix_fmt", "yuv420p")
        self.lossless = kw.get("lossless", False)
        self.notes: List[str] = kw.get("notes", [])
        self.warnings: List[str] = kw.get("warnings", [])
        self.vlabel = kw.get("vlabel", "H.264")
        self.bitrate_reliability = kw.get("bitrate_reliability", "unknown")
        self.source_bps = kw.get("source_bps", 0)

    def summary(self) -> str:
        rate = "lossless" if self.lossless else (f"CRF {self.crf}" if self.crf is not None else "-")
        cap = f", cap {self.cap_bps / 1e6:.2f} Mbps" if self.cap_bps else ", no bitrate cap"
        return f"{self.label} | {self.encoder} {self.preset} | {rate}{cap} | {self.pix_fmt}"


def decide_policy(info: Dict[str, Any], ext: str, mode: str, requested: Optional[str] = None) -> EncodePolicy:
    """
    Single place that turns (source analysis + mode) into encoder arguments.
    fast / balanced / best reproduce the previous encoder settings exactly; hq / lossless / auto are new.
    """
    requested = requested or mode
    if mode == "auto":
        mode, auto_notes = auto_select(info, info.get("clip_secs") or info.get("duration", 0), cpu_count(), ext)
    else:
        auto_notes = []
    if mode not in MODES:
        mode = "fast"
    key, notes = pick_codec(info, ext, mode)
    enc = encoder_for(key, mode)
    warnings: List[str] = []
    notes = auto_notes + notes
    bps, rel = source_bitrate_info(info)
    legacy = mode in LEGACY_MODES
    mp4_like = ext in (".mp4", ".m4v", ".mov")
    pf, pf_notes = pick_pix_fmt(info, enc, preserve=not legacy, legacy_hevc=(key == "hevc"))
    notes += pf_notes
    ten_bit = pf.endswith("10le") or pf.endswith("12le")
    label = MODE_INFO.get(requested if requested in MODE_INFO else mode, MODE_INFO[mode])[0]
    if requested == "auto":
        label = f"🤖 Auto -> {MODE_INFO[mode][0]}"
    preset = crf = None
    cap: Optional[int] = None
    vargs: List[str] = []
    lossless = False

    if legacy:
        speed = mode
        if key == "hevc":
            preset = {"fast": ENCODE_PRESET_HEVC_FAST, "balanced": ENCODE_PRESET_HEVC_BAL}.get(speed, ENCODE_PRESET_HEVC)
            crf = ENCODE_CRF_HEVC
        else:
            preset = {"fast": ENCODE_PRESET_FAST, "balanced": ENCODE_PRESET_BAL}.get(speed, ENCODE_PRESET)
            crf = ENCODE_CRF
        if rel == "unknown":
            notes.append("Source video bitrate unknown: no bitrate cap applied (CRF only).")
        else:
            cap = bps
            if rel in ("derived", "estimated"):
                notes.append(f"Source video bitrate is {rel} (container / file size based), used only as a size guard.")
    elif mode == "hq":
        if key == "hevc":
            preset, crf = QUALITY_HQ_PRESET_X265, QUALITY_HQ_CRF_X265
        elif key == "av1":
            preset, crf = QUALITY_HQ_PRESET_AV1, QUALITY_HQ_CRF_AV1
        else:
            preset, crf = QUALITY_HQ_PRESET_X264, QUALITY_HQ_CRF_X264
        if QUALITY_HQ_MAX_FACTOR > 0 and bps:
            cap = int(bps * QUALITY_HQ_MAX_FACTOR)
            notes.append(f"HQ bitrate ceiling = {QUALITY_HQ_MAX_FACTOR}x source (QUALITY_HQ_MAX_FACTOR).")
        warnings.append("High Quality has no size guard: the file can be larger than the source and slower to encode.")
    else:                                                      # lossless
        lossless = True
        if key == "av1" and enc != "libaom-av1":
            raise PolicyError("Lossless AV1 needs libaom-av1 (not present in this FFmpeg build).")
        if enc not in available_encoders():
            raise PolicyError(f"Encoder {enc} is not available in this FFmpeg build; lossless mode cannot run.")
        preset = QUALITY_LOSSLESS_PRESET_X265 if key == "hevc" else (QUALITY_LOSSLESS_PRESET_X264 if key == "h264" else "6")
        warnings.append("Lossless output can be 5-30x larger than the source and encodes very slowly.")
        warnings.append("Subtitles change the affected pixels: lossless preserves the subtitle-rendered frames, not the original un-subtitled ones.")
        if key == "h264":
            warnings.append("Lossless H.264 uses the High 4:4:4 Predictive profile; many players / phones cannot play it.")

    # ---- build the encoder arguments ----
    if key == "hevc":
        xp = ["log-level=error"] + (["lossless=1"] if lossless else [])
        vargs = ["-c:v", "libx265", "-preset", preset]
        if crf is not None and not lossless:
            vargs += ["-crf", str(crf)]
        if cap:
            vargs += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
        vargs += ["-pix_fmt", pf, "-x265-params", ":".join(xp)]
        if ten_bit:
            vargs += ["-profile:v", "main10"]
        if mp4_like:
            vargs += ["-tag:v", "hvc1"]
    elif key == "av1":
        if enc == "libaom-av1":
            vargs = ["-c:v", "libaom-av1", "-cpu-used", "6", "-pix_fmt", pf]
            vargs += ["-crf", "0", "-b:v", "0", "-aom-params", "lossless=1"] if lossless else ["-crf", str(crf), "-b:v", "0"]
        else:
            vargs = ["-c:v", "libsvtav1", "-preset", str(preset), "-crf", str(crf), "-pix_fmt", pf]
    else:
        vargs = ["-c:v", "libx264", "-preset", preset]
        if lossless:
            vargs += ["-qp", "0"]
        else:
            vargs += ["-crf", str(crf)]
            if cap:
                vargs += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
        vargs += ["-pix_fmt", pf]
    vargs += color_args(info)

    return EncodePolicy(
        mode=mode, requested=requested, label=label, codec=key, encoder=enc, vargs=vargs, preset=str(preset),
        crf=None if lossless else crf, cap_bps=cap, pix_fmt=pf, lossless=lossless, notes=notes, warnings=warnings,
        vlabel={"hevc": "HEVC", "h264": "H.264", "av1": "AV1"}[key], bitrate_reliability=rel, source_bps=bps,
    )


# ---------------- speed / size estimates ----------------
# megapixel-frames per second per CPU core (measured on x264/x265 at ~720p, rough for the rest)
_SPEED_TABLE = {
    "libx264": {"ultrafast": 40, "superfast": 30, "veryfast": 19, "faster": 16, "fast": 15, "medium": 14, "slow": 8, "slower": 4},
    "libx265": {"ultrafast": 14, "superfast": 12.8, "veryfast": 11.2, "faster": 11, "fast": 9.5, "medium": 6, "slow": 3, "slower": 1.5},
    "libsvtav1": {"4": 4, "5": 5, "6": 8, "7": 11, "8": 14, "10": 25},
    "libaom-av1": {"6": 0.8},
}

def estimate_encode_seconds(policy: EncodePolicy, info: Dict[str, Any], clip_secs: float, cpus: Optional[int] = None) -> float:
    cpus = cpus or cpu_count()
    mp = max(info.get("width", 1280) * info.get("height", 720), 1) / 1e6
    fps = info.get("fps") or 24.0
    base = _SPEED_TABLE.get(policy.encoder, {}).get(str(policy.preset), 10.0)
    if policy.lossless:
        base *= 0.6 if policy.encoder == "libx265" else 1.0
    eff_cores = min(cpus, 8) * 0.85
    return (clip_secs * fps * mp) / max(base * eff_cores, 0.05) * 1.1

def estimate_output_bytes(policy: EncodePolicy, info: Dict[str, Any], clip_secs: float, audio_bps: int) -> int:
    """Rough size forecast used for the 2 GB Telegram check and the warnings (NOT a promise)."""
    w, h, fps = info.get("width", 1280), info.get("height", 720), info.get("fps") or 24.0
    px_rate = w * h * fps
    if policy.cap_bps:
        v_bps = policy.cap_bps
    elif policy.lossless:
        v_bps = px_rate * (3.5 if info.get("bit_depth", 8) <= 8 else 4.4)
    else:                                                       # hq / uncapped
        bpp = {"hevc": 0.07, "h264": 0.12, "av1": 0.05}.get(policy.codec, 0.1)
        v_bps = max(policy.source_bps * 1.6 if policy.source_bps else 0, px_rate * bpp)
    return int((v_bps + audio_bps) * clip_secs / 8)


# ---------------- AUTO: analyse the source and choose a mode ----------------
_CODEC_EFFICIENCY = {"h264": 1.0, "hevc": 1.6, "av1": 2.0, "vp9": 1.5, "mpeg4": 0.6, "mpeg2video": 0.5}

def auto_select(info: Dict[str, Any], clip_secs: float, cpus: int, ext: str = ".mkv") -> Tuple[str, List[str]]:
    """
    Picks a mode from how well the source is compressed (effective bits per pixel, H.264-equivalent):
      very low / low   -> best     (cannot add detail; the size guard avoids bloating the file)
      medium           -> balanced / best
      high / very high -> hq       (a cap would starve the encode)
    and falls back to faster modes when the estimated encode time exceeds QUALITY_AUTO_TIME_BUDGET_MIN.
    """
    reasons: List[str] = []
    bps, rel = source_bitrate_info(info)
    px_rate = max(info.get("width", 1280) * info.get("height", 720) * (info.get("fps") or 24.0), 1)
    if rel == "unknown" or not bps:
        mode = "balanced"
        reasons.append("Source bitrate unknown -> Balanced (CRF based, no cap).")
    else:
        eff = bps / px_rate * _CODEC_EFFICIENCY.get(info.get("v_codec", ""), 1.0)
        if eff < 0.05:
            mode, tier = "best", "heavily compressed"
        elif eff < 0.10:
            mode, tier = "best", "moderately compressed"
        elif eff < 0.20:
            mode, tier = "hq", "high bitrate"
        else:
            mode, tier = "hq", "very high bitrate / remux grade"
        reasons.append(f"Source: {tier} (effective {eff:.3f} bits/pixel, bitrate {rel}).")
        if mode == "best":
            reasons.append("Source cannot gain detail -> size-guarded Best (no bloat).")
        else:
            reasons.append("Rich source -> High Quality without a bitrate cap.")
    # time budget: demote when the encode would take too long on this machine
    order = {"hq": ["hq", "best", "balanced", "fast"], "best": ["best", "balanced", "fast"],
             "balanced": ["balanced", "fast"], "fast": ["fast"]}[mode]
    for cand in order:
        try:
            pol = decide_policy(dict(info, clip_secs=clip_secs), ext, cand, requested=cand)
        except PolicyError:
            continue
        est_min = estimate_encode_seconds(pol, info, clip_secs, cpus) / 60
        if est_min <= QUALITY_AUTO_TIME_BUDGET_MIN or cand == "fast":
            if cand != mode:
                reasons.append(f"{MODE_INFO[mode][0]} would take ~{estimate_encode_seconds(decide_policy(dict(info, clip_secs=clip_secs), ext, mode, requested=mode), info, clip_secs, cpus) / 60:.0f} min on {cpus} CPU(s) "
                               f"(budget {QUALITY_AUTO_TIME_BUDGET_MIN:.0f} min) -> {MODE_INFO[cand][0]}.")
            mode = cand
            reasons.append(f"Estimated encode time ~{est_min:.0f} min ({cpus} CPU).")
            break
    return mode, reasons


# ---------------- reports (plain text, monospace) ----------------
def _plain(text: str) -> str:
    """Drop emoji / symbols so the monospace box stays aligned."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x00-\x7f]+", "", text)).strip()

def _wrap_rows(rows: List[str], limit: int = 54) -> List[str]:
    out: List[str] = []
    for r in rows:
        while len(r) > limit:
            cut = r.rfind(" ", 0, limit)
            cut = cut if cut > 10 else limit
            out.append(r[:cut])
            r = "  " + r[cut:].strip()
        out.append(r)
    return out

def box(title: str, rows: List[str]) -> str:
    rows = _wrap_rows(rows)
    width = max([len(r) for r in rows] + [len(title) + 6, 36])          # inner text width
    top = f"╭─ {title} " + "─" * (width - len(title) - 1) + "╮"
    body = "\n".join(f"│ {r.ljust(width)} │" for r in rows)
    return f"{top}\n{body}\n╰" + "─" * (width + 2) + "╯"

def audio_summary(info: Dict[str, Any]) -> str:
    if not info.get("audio"):
        return "none"
    t = info["audio"][0]
    extra = f" +{len(info['audio']) - 1}" if len(info["audio"]) > 1 else ""
    return f"{(t.get('lang') or 'und').upper()} {t.get('codec', '').upper()}{extra}"

def analysis_report(info: Dict[str, Any], policy: EncodePolicy, mode_note: str = "", est_secs: float = 0, est_bytes: int = 0) -> str:
    bps, rel = source_bitrate_info(info)
    rows = [
        f"Codec       : {_codec_txt(info.get('v_codec'))}  ({info.get('profile') or 'profile ?'})",
        f"Resolution  : {info.get('width')} x {info.get('height')}",
        f"Frame Rate  : {_fps_txt(info.get('fps', 0))} FPS",
        f"Bit Depth   : {info.get('bit_depth', 8)}-bit  ({info.get('pix_fmt') or '?'})",
        f"Colour      : {info.get('hdr', 'SDR')}  {info.get('color_space') or ''} {info.get('color_range') or ''}".rstrip(),
        f"Video rate  : {bps / 1e6:.2f} Mbps  [{rel}]" if bps else "Video rate  : unknown",
        f"Audio       : {audio_summary(info)}  ({len(info.get('audio', []))} track(s))",
        f"Subtitles   : {len(info.get('subs', []))} embedded",
        "",
        f"Mode        : {_plain(policy.label)}",
        f"Encoder     : {policy.encoder} / {policy.preset}" + ("  (lossless)" if policy.lossless else f"  CRF {policy.crf}"),
        f"Bitrate cap : {policy.cap_bps / 1e6:.2f} Mbps" if policy.cap_bps else "Bitrate cap : none",
        f"Output px   : {policy.pix_fmt}",
    ]
    if est_secs:
        rows.append(f"Est. time   : ~{est_secs / 60:.0f} min")
    if est_bytes:
        rows.append(f"Est. size   : ~{_bytes_txt(est_bytes)}")
    return box("VIDEO SOURCE ANALYSIS", rows)

def _ok(flag: Optional[bool], good: str = "PRESERVED", bad: str = "CHANGED", unknown: str = "NOT VERIFIED") -> str:
    return unknown if flag is None else (good if flag else bad)

_CODEC_NAME = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "mpeg4": "MPEG-4", "mpeg2video": "MPEG-2"}

def _codec_txt(c: str) -> str:
    return _CODEC_NAME.get(c, (c or "?").upper())

def comparison_report(src: Dict[str, Any], out: Dict[str, Any], policy: EncodePolicy, checks: Dict[str, Any],
                      elapsed: float = 0.0, src_bytes: int = 0, out_bytes: int = 0) -> str:
    """Source vs output box. 'Visual Quality' is only claimed when a metric was actually measured."""
    rows = [
        f"Resolution  : {src.get('width')} x {src.get('height')}  ->  {out.get('width')} x {out.get('height')}",
        f"Frame rate  : {_fps_txt(src.get('fps', 0))}  ->  {_fps_txt(out.get('fps', 0))}",
        f"Codec       : {_codec_txt(src.get('v_codec'))}  ->  {_codec_txt(out.get('v_codec'))}",
        f"Pixel fmt   : {src.get('pix_fmt') or '?'}  ->  {out.get('pix_fmt') or '?'}  ({out.get('bit_depth', 8)}-bit)",
        f"Colour      : {src.get('hdr', 'SDR')}  ->  {out.get('hdr', 'SDR')}",
        f"Audio tracks: {checks.get('audio_expected', len(src.get('audio', [])))}  ->  {len(out.get('audio', []))}",
        f"Mode        : {_plain(policy.label)}",
        f"Encoder     : {policy.encoder} {policy.preset}" + (" lossless" if policy.lossless else f" CRF {policy.crf}")
        + (f", cap {policy.cap_bps / 1e6:.2f} Mbps" if policy.cap_bps else ", no cap"),
    ]
    if src_bytes and out_bytes:
        rows.append(f"File size   : ~{_bytes_txt(src_bytes)} (src clip est.)  ->  {_bytes_txt(out_bytes)}")
    if elapsed:
        rows.append(f"Encode time : {_hms(elapsed)}")
    rows.append("")
    rows += [
        f"Resolution: {_ok(checks.get('resolution_ok'))}",
        f"Frame Rate: {_ok(checks.get('fps_ok'))}",
        f"Codec: {_ok(checks.get('codec_same'), 'MATCHED', 'CHANGED')}",
        f"Bit depth: {_ok(checks.get('bitdepth_ok'))}",
        f"Output complete: {_ok(checks.get('complete'), 'YES', 'NO')}",
        f"Playable: {_ok(checks.get('playable'), 'YES', 'NO')}",
        f"A/V sync: {_ok(checks.get('av_sync_ok'), 'OK', 'DRIFT')}",
        f"Audio tracks: {_ok(checks.get('audio_ok'), 'OK', 'MISMATCH')}",
        f"Subtitle: {checks.get('subtitle_text', 'NOT VERIFIED')}",
    ]
    if policy.lossless:
        rows.append(f"Lossless: {checks.get('lossless_text', 'NOT VERIFIED')}")
    rows.append(f"Visual Quality: {checks.get('quality_text', 'NOT VERIFIED')}")
    return box("SOURCE / OUTPUT REPORT", rows)

