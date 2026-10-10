import asyncio
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from shazamio import Shazam
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from job_queue import MediaJobQueue
from media_providers import build_provider_manager, public_error_message


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_SEND_MB = int(os.getenv("MAX_SEND_MB", "45"))
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "1200"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "480"))
RECOGNITION_SECONDS = int(os.getenv("RECOGNITION_SECONDS", "28"))
CONCURRENT_UPDATES = int(os.getenv("CONCURRENT_UPDATES", "4"))
SEARCH_RESULTS = int(os.getenv("SEARCH_RESULTS", "18"))
SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "6"))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()
REQUIRED_CHANNEL_NAME = os.getenv("REQUIRED_CHANNEL_NAME", "Bizning kanal").strip() or "Bizning kanal"


DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("uz_navo")

MEDIA_MANAGER = build_provider_manager(VIDEO_HEIGHT, MAX_DURATION_SECONDS)
JOB_QUEUE = MediaJobQueue()

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
}


def allowed_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in ALLOWED_HOSTS
    except Exception:
        return False


def first_supported_url(text: str | None) -> str | None:
    if not text:
        return None
    for match in URL_RE.findall(text):
        url = match.rstrip(").,]>")
        if allowed_url(url):
            return url
    return None


def normalize_media_url(url: str) -> str:
    """Normal YouTube watch URL'dan playlist/radio parametrlarini olib tashlaydi."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            qs = parse_qs(parsed.query)
            video_id = (qs.get("v") or [None])[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/")[0]
            if video_id:
                return f"https://youtu.be/{video_id}"
    except Exception:
        pass

    return url


def readable_size(path: Path) -> str:
    mb = path.stat().st_size / (1024 * 1024)
    return f"{mb:.1f} MB"


def duration_text(value) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return "--:--"
    if seconds <= 0:
        return "--:--"
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def media_candidates(workdir: Path) -> list[Path]:
    ignore_suffixes = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}
    return [
        p for p in workdir.iterdir()
        if p.is_file() and p.suffix.lower() not in ignore_suffixes
    ]


def extract_sample_sync(
    src: Path,
    dst: Path,
    start_seconds: float = 4.0,
    duration_seconds: int | None = None,
) -> None:
    duration_seconds = duration_seconds or RECOGNITION_SECONDS
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(max(0.0, start_seconds)),
        "-i", str(src),
        "-t", str(duration_seconds),
        "-vn", "-ac", "1", "-ar", "44100",
        "-acodec", "libmp3lame", "-b:a", "96k", str(dst),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1200:] or "FFmpeg sample extraction failed")


async def extract_sample(
    src: Path,
    dst: Path,
    start_seconds: float = 4.0,
    duration_seconds: int | None = None,
) -> None:
    await asyncio.to_thread(
        extract_sample_sync,
        src,
        dst,
        start_seconds,
        duration_seconds,
    )


def convert_to_mp3_sync(src: Path, dst: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-acodec", "libmp3lame", "-b:a", "128k", str(dst),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1200:] or "FFmpeg MP3 conversion failed")


async def convert_to_mp3(src: Path, dst: Path) -> None:
    await asyncio.to_thread(convert_to_mp3_sync, src, dst)


EFFECTS = {
    "slowrev": (
        "🌙 Slowed + Reverb",
        "asetrate=44100*0.90,aresample=44100,"
        "aecho=0.8:0.88:60|120:0.35|0.20,alimiter=limit=0.95"
    ),
    "sped": (
        "⚡ Sped Up",
        "asetrate=44100*1.12,aresample=44100,alimiter=limit=0.95"
    ),

    # Strong low-end boost for headphones/speakers.
    "bass": (
        "🔊 Bass Booster",
        "highpass=f=28,"
        "equalizer=f=45:t=q:w=1.0:g=5,"
        "equalizer=f=70:t=q:w=1.0:g=9,"
        "equalizer=f=110:t=q:w=1.0:g=7,"
        "equalizer=f=180:t=q:w=1.1:g=3,"
        "volume=1.04,alimiter=limit=0.92"
    ),

    # Slow left/right movement for a classic 8D headphone effect.
    "8d": (
        "🎧 8D",
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "apulsator=hz=0.10:amount=0.92,"
        "alimiter=limit=0.94"
    ),

    # Hall-style ambience. This is a simulated hall/reverb effect.
    "concert": (
        "🏟 Concert Hall",
        "highpass=f=55,lowpass=f=15500,"
        "aecho=0.78:0.86:45|90|180|310:0.34|0.26|0.18|0.11,"
        "volume=0.96,alimiter=limit=0.94"
    ),

    # Car-audio inspired EQ: firm sub/low bass, clear mids and crisp highs.
    "carrozeria": (
        "🚘 Carrozeria",
        "highpass=f=30,"
        "equalizer=f=55:t=q:w=1.0:g=6,"
        "equalizer=f=95:t=q:w=1.0:g=5,"
        "equalizer=f=220:t=q:w=1.1:g=2,"
        "equalizer=f=1000:t=q:w=1.2:g=-1,"
        "equalizer=f=3200:t=q:w=1.0:g=2.5,"
        "equalizer=f=8500:t=q:w=1.0:g=3,"
        "volume=1.03,alimiter=limit=0.92"
    ),

    # Punch around the kick/bass-guitar region without huge sub-bass.
    "midbass": (
        "🥁 Mid Bass",
        "highpass=f=38,"
        "equalizer=f=75:t=q:w=1.1:g=3,"
        "equalizer=f=110:t=q:w=0.9:g=7,"
        "equalizer=f=160:t=q:w=0.9:g=6,"
        "equalizer=f=230:t=q:w=1.0:g=3,"
        "volume=1.03,alimiter=limit=0.93"
    ),

    "night": (
        "🌃 Nightcore",
        "asetrate=44100*1.20,aresample=44100,alimiter=limit=0.95"
    ),
    "deep": (
        "🌑 Deep / Low Pitch",
        "asetrate=44100*0.86,aresample=44100,alimiter=limit=0.95"
    ),
    "echo": (
        "🏛 Echo",
        "aecho=0.8:0.88:70|140:0.40|0.22,alimiter=limit=0.95"
    ),
    "vocal": (
        "🎤 Vocal Boost",
        "highpass=f=120,lowpass=f=11000,"
        "equalizer=f=1800:t=q:w=1.2:g=4,"
        "equalizer=f=3500:t=q:w=1.2:g=3,"
        "alimiter=limit=0.95"
    ),

    "p919_flat": (
        "🚘 P919 FLAT",
        "anull"
    ),
    "p919_sbass": (
        "🔊 P919 SUPER BASS",
        "equalizer=f=50:t=q:w=1.0:g=10,"
        "equalizer=f=80:t=q:w=1.0:g=8,"
        "equalizer=f=125:t=q:w=1.0:g=5,"
        "equalizer=f=200:t=q:w=1.0:g=2,"
        "alimiter=limit=0.86"
    ),
    "p919_powerful": (
        "💥 P919 POWERFUL",
        "equalizer=f=50:t=q:w=1.0:g=6,"
        "equalizer=f=80:t=q:w=1.0:g=5,"
        "equalizer=f=125:t=q:w=1.0:g=3,"
        "equalizer=f=200:t=q:w=1.0:g=2,"
        "equalizer=f=315:t=q:w=1.0:g=1,"
        "equalizer=f=1250:t=q:w=1.0:g=1,"
        "equalizer=f=2000:t=q:w=1.0:g=2,"
        "equalizer=f=3150:t=q:w=1.0:g=3,"
        "equalizer=f=5000:t=q:w=1.0:g=3,"
        "equalizer=f=8000:t=q:w=1.0:g=2,"
        "alimiter=limit=0.88"
    ),
    "p919_natural": (
        "🌿 P919 NATURAL",
        "equalizer=f=50:t=q:w=1.0:g=2,"
        "equalizer=f=80:t=q:w=1.0:g=2,"
        "equalizer=f=125:t=q:w=1.0:g=1,"
        "equalizer=f=315:t=q:w=1.0:g=-1,"
        "equalizer=f=500:t=q:w=1.0:g=-1,"
        "equalizer=f=1250:t=q:w=1.0:g=1,"
        "equalizer=f=2000:t=q:w=1.0:g=2,"
        "equalizer=f=3150:t=q:w=1.0:g=2,"
        "equalizer=f=5000:t=q:w=1.0:g=1,"
        "equalizer=f=8000:t=q:w=1.0:g=1,"
        "alimiter=limit=0.92"
    ),
    "p919_vocal": (
        "🎤 P919 VOCAL",
        "equalizer=f=50:t=q:w=1.0:g=-3,"
        "equalizer=f=80:t=q:w=1.0:g=-2,"
        "equalizer=f=125:t=q:w=1.0:g=-1,"
        "equalizer=f=200:t=q:w=1.0:g=1,"
        "equalizer=f=315:t=q:w=1.0:g=2,"
        "equalizer=f=500:t=q:w=1.0:g=3,"
        "equalizer=f=800:t=q:w=1.0:g=4,"
        "equalizer=f=1250:t=q:w=1.0:g=4,"
        "equalizer=f=2000:t=q:w=1.0:g=3,"
        "equalizer=f=3150:t=q:w=1.0:g=2,"
        "equalizer=f=5000:t=q:w=1.0:g=1,"
        "alimiter=limit=0.90"
    ),
    "p919_loud": (
        "🎚 P919 LOUDNESS",
        "equalizer=f=100:t=q:w=0.65:g=10,"
        "equalizer=f=10000:t=q:w=0.75:g=6.5,"
        "alimiter=limit=0.84"
    ),
    "p919_studio": (
        "🎙 P919 STUDIO",
        "aecho=0.82:0.72:22|44|82:0.18|0.12|0.08,"
        "highpass=f=45,lowpass=f=17500,alimiter=limit=0.94"
    ),
    "p919_club": (
        "🪩 P919 CLUB",
        "equalizer=f=80:t=q:w=0.9:g=4,"
        "equalizer=f=125:t=q:w=1.0:g=3,"
        "aecho=0.80:0.78:38|76|122:0.25|0.15|0.09,"
        "alimiter=limit=0.91"
    ),
    "p919_hall": (
        "🏟 P919 CONCERT HALL",
        "highpass=f=45,lowpass=f=16500,"
        "aecho=0.78:0.84:46|92|184|320:0.34|0.25|0.17|0.10,"
        "alimiter=limit=0.92"
    ),
    "p919_dome": (
        "🏛 P919 DOME",
        "highpass=f=42,lowpass=f=16000,"
        "aecho=0.76:0.88:70|145|290|470:0.32|0.24|0.16|0.09,"
        "alimiter=limit=0.91"
    ),
    "p919_bbe": (
        "✨ P919 BBE-style",
        "equalizer=f=2500:t=q:w=1.0:g=1.5,"
        "equalizer=f=5000:t=q:w=1.0:g=2.5,"
        "equalizer=f=10000:t=q:w=0.9:g=3.5,"
        "highpass=f=28,alimiter=limit=0.92"
    ),
    "p919_oct1": (
        "🌀 P919 OCTAVER 1",
        "anull"
    ),
    "p919_oct2": (
        "🌀 P919 OCTAVER 2",
        "anull"
    ),
}


def apply_audio_effect_sync(src: Path, dst: Path, effect_key: str) -> None:
    if effect_key in {"p919_oct1", "p919_oct2"}:
        wet = "0.22" if effect_key == "p919_oct1" else "0.38"
        filter_complex = (
            "[0:a]asplit=2[orig][pitch];"
            "[pitch]rubberband=pitch=0.5,lowpass=f=180[low];"
            f"[orig][low]amix=inputs=2:weights='1 {wet}':normalize=0,"
            "alimiter=limit=0.90[out]"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-acodec", "libmp3lame", "-b:a", "128k",
            str(dst),
        ]
    else:
        if effect_key not in EFFECTS:
            raise ValueError("Noma'lum effekt.")
        _label, audio_filter = EFFECTS[effect_key]
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-vn",
            "-af", audio_filter,
            "-acodec", "libmp3lame", "-b:a", "128k",
            str(dst),
        ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1600:] or "FFmpeg effect processing failed")


async def apply_audio_effect(src: Path, dst: Path, effect_key: str) -> None:
    await asyncio.to_thread(apply_audio_effect_sync, src, dst, effect_key)


async def recognize_song(path: Path) -> dict | None:
    shazam = Shazam()
    result = await shazam.recognize(str(path))
    track = result.get("track") if isinstance(result, dict) else None
    if not track:
        return None

    images = track.get("images") or {}
    return {
        "title": track.get("title") or "Noma'lum",
        "artist": track.get("subtitle") or "Noma'lum ijrochi",
        "url": track.get("url"),
        "cover": images.get("coverarthq") or images.get("coverart"),
    }


async def recognize_media_fast(
    src: Path,
    workdir: Path,
    duration: float | None = None,
) -> dict | None:
    first = workdir / "shazam_1.mp3"
    await extract_sample(src, first, 4.0, RECOGNITION_SECONDS)
    song = await recognize_song(first)
    if song:
        return song

    if duration and duration > 70:
        second = workdir / "shazam_2.mp3"
        middle = max(8.0, (float(duration) / 2.0) - (RECOGNITION_SECONDS / 2.0))
        await extract_sample(src, second, middle, RECOGNITION_SECONDS)
        return await recognize_song(second)

    return None


def song_text(song: dict | None) -> str:
    if not song:
        return (
            "❌ Musiqa aniqlanmadi.\n\n"
            "Maslahat: musiqa ovozi balandroq bo'lgan 10–30 soniyalik parcha yuboring."
        )

    text = (
        "🎵 Musiqa topildi!\n\n"
        f"🎤 Ijrochi: {song['artist']}\n"
        f"🎶 Nomi: {song['title']}"
    )
    if song.get("url"):
        text += f"\n🔗 Shazam: {song['url']}"
    return text


async def send_song_result(message, song: dict | None) -> None:
    caption = song_text(song)
    cover = song.get("cover") if song else None
    if cover:
        try:
            await message.reply_photo(photo=cover, caption=caption)
            return
        except TelegramError:
            pass
    await message.reply_text(caption, disable_web_page_preview=True)


# -------------------------
# Media providers: metadata/search/download
# -------------------------

def get_info_sync(url: str) -> dict:
    if not allowed_url(url):
        raise ValueError("Faqat YouTube va Instagram havolalari qo'llanadi.")
    return MEDIA_MANAGER.get_info(normalize_media_url(url))


async def get_info(url: str) -> dict:
    return await asyncio.to_thread(get_info_sync, url)


def search_youtube_sync(query: str) -> list[dict]:
    return MEDIA_MANAGER.search(query, SEARCH_RESULTS)


async def search_youtube(query: str) -> list[dict]:
    return await asyncio.to_thread(search_youtube_sync, query)


def download_video_sync(url: str, workdir: Path) -> tuple[Path, dict]:
    if not allowed_url(url):
        raise ValueError("Faqat YouTube va Instagram havolalari qo'llanadi.")
    return MEDIA_MANAGER.download_video(normalize_media_url(url), workdir)


async def download_video(url: str, workdir: Path) -> tuple[Path, dict]:
    return await asyncio.to_thread(download_video_sync, url, workdir)


def download_audio_sync(url: str, workdir: Path) -> tuple[Path, dict]:
    if not allowed_url(url):
        raise ValueError("Faqat YouTube va Instagram havolalari qo'llanadi.")
    return MEDIA_MANAGER.download_audio(normalize_media_url(url), workdir)


async def download_audio(url: str, workdir: Path) -> tuple[Path, dict]:
    return await asyncio.to_thread(download_audio_sync, url, workdir)


# -------------------------
# Inline UI / cache
# -------------------------

def remember_item(context: ContextTypes.DEFAULT_TYPE, item: dict) -> str:
    items = context.user_data.setdefault("media_items", {})
    token = secrets.token_hex(4)
    items[token] = item
    while len(items) > 20:
        first_key = next(iter(items))
        items.pop(first_key, None)
    return token


def media_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Video", callback_data=f"m:v:{token}"),
            InlineKeyboardButton("🎧 Audio", callback_data=f"m:a:{token}"),
        ],
        [
            InlineKeyboardButton("🎵 Musiqani aniqlash", callback_data=f"m:s:{token}"),
            InlineKeyboardButton("🎛 Effektlar", callback_data=f"m:e:{token}"),
        ],
        [InlineKeyboardButton("❌ Yopish", callback_data="close")],
    ])


def effects_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚘 DEH-P919 DSP", callback_data=f"p919:{token}")],
        [
            InlineKeyboardButton("🔊 Bass Booster", callback_data=f"fx:bass:{token}"),
            InlineKeyboardButton("🥁 Mid Bass", callback_data=f"fx:midbass:{token}"),
        ],
        [
            InlineKeyboardButton("🏟 Concert Hall", callback_data=f"fx:concert:{token}"),
            InlineKeyboardButton("🎧 8D", callback_data=f"fx:8d:{token}"),
        ],
        [
            InlineKeyboardButton("🚘 Carrozeria EQ", callback_data=f"fx:carrozeria:{token}"),
            InlineKeyboardButton("🌙 Slowed + Reverb", callback_data=f"fx:slowrev:{token}"),
        ],
        [
            InlineKeyboardButton("⚡ Sped Up", callback_data=f"fx:sped:{token}"),
            InlineKeyboardButton("🌃 Nightcore", callback_data=f"fx:night:{token}"),
        ],
        [
            InlineKeyboardButton("🌑 Deep", callback_data=f"fx:deep:{token}"),
            InlineKeyboardButton("🏛 Echo", callback_data=f"fx:echo:{token}"),
        ],
        [InlineKeyboardButton("🎤 Vocal Boost", callback_data=f"fx:vocal:{token}")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data=f"fxback:{token}")],
    ])


def p919_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔊 SUPER BASS", callback_data=f"fx:p919_sbass:{token}"),
            InlineKeyboardButton("💥 POWERFUL", callback_data=f"fx:p919_powerful:{token}"),
        ],
        [
            InlineKeyboardButton("🌿 NATURAL", callback_data=f"fx:p919_natural:{token}"),
            InlineKeyboardButton("🎤 VOCAL", callback_data=f"fx:p919_vocal:{token}"),
        ],
        [
            InlineKeyboardButton("🎙 STUDIO", callback_data=f"fx:p919_studio:{token}"),
            InlineKeyboardButton("🪩 CLUB", callback_data=f"fx:p919_club:{token}"),
        ],
        [
            InlineKeyboardButton("🏟 CONCERT HALL", callback_data=f"fx:p919_hall:{token}"),
            InlineKeyboardButton("🏛 DOME", callback_data=f"fx:p919_dome:{token}"),
        ],
        [
            InlineKeyboardButton("🌀 OCTAVER 1", callback_data=f"fx:p919_oct1:{token}"),
            InlineKeyboardButton("🌀 OCTAVER 2", callback_data=f"fx:p919_oct2:{token}"),
        ],
        [
            InlineKeyboardButton("✨ BBE-style", callback_data=f"fx:p919_bbe:{token}"),
            InlineKeyboardButton("🎚 LOUDNESS", callback_data=f"fx:p919_loud:{token}"),
        ],
        [InlineKeyboardButton("➖ FLAT", callback_data=f"fx:p919_flat:{token}")],
        [InlineKeyboardButton("⬅️ Effektlarga qaytish", callback_data=f"p919back:{token}")],
    ])

def card_caption(item: dict) -> str:
    title = item.get("title") or "Media"
    uploader = item.get("uploader") or item.get("channel") or ""
    duration = duration_text(item.get("duration"))
    text = f"🎞 {title}"
    if uploader:
        text += f"\n👤 {uploader}"
    if duration != "--:--":
        text += f"\n⏱ {duration}"
    text += "\n\nYuklab olish formatlari ↓"
    return text[:1024]


async def send_media_card(message, context: ContextTypes.DEFAULT_TYPE, item: dict) -> None:
    token = remember_item(context, item)
    markup = media_keyboard(token)
    caption = card_caption(item)
    thumbnail = item.get("thumbnail")

    if thumbnail:
        try:
            await message.reply_photo(photo=thumbnail, caption=caption, reply_markup=markup)
            return
        except TelegramError:
            pass
    await message.reply_text(caption, reply_markup=markup, disable_web_page_preview=True)


def search_markup(page: int, total: int) -> InlineKeyboardMarkup:
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, total)
    nums = []
    row = []
    for idx in range(start, end):
        row.append(InlineKeyboardButton(str(idx - start + 1), callback_data=f"pick:{idx}"))
        if len(row) == 3:
            nums.append(row)
            row = []
    if row:
        nums.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}"))
    nav.append(InlineKeyboardButton("❌", callback_data="close"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{page+1}"))
    nums.append(nav)
    return InlineKeyboardMarkup(nums)


def search_text_block(query: str, results: list[dict], page: int) -> str:
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(results))
    lines = [f"🔍 {query}\n"]
    for pos, item in enumerate(results[start:end], start=1):
        title = item.get("title") or "Noma'lum"
        duration = duration_text(item.get("duration"))
        lines.append(f"{pos}. {title} {duration}")
    if len(results) > SEARCH_PAGE_SIZE:
        pages = (len(results) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
        lines.append(f"\n📄 {page + 1}/{pages}")
    return "\n".join(lines)[:4000]


async def render_search(message, context: ContextTypes.DEFAULT_TYPE, page: int, edit: bool = False) -> None:
    results = context.user_data.get("search_results") or []
    query = context.user_data.get("search_query") or "Qidiruv"
    if not results:
        if edit:
            await message.edit_text("❌ Qidiruv natijalari eskirgan. Qayta yozib qidiring.")
        else:
            await message.reply_text("❌ Qidiruv natijalari topilmadi.")
        return

    max_page = max(0, (len(results) - 1) // SEARCH_PAGE_SIZE)
    page = max(0, min(page, max_page))
    context.user_data["search_page"] = page
    text = search_text_block(query, results, page)
    markup = search_markup(page, len(results))
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)



# -------------------------
# Majburiy obuna
# -------------------------

def subscription_url() -> str:
    if REQUIRED_CHANNEL_URL:
        return REQUIRED_CHANNEL_URL
    if REQUIRED_CHANNEL.startswith("@"):
        return f"https://t.me/{REQUIRED_CHANNEL[1:]}"
    return ""


def subscription_keyboard() -> InlineKeyboardMarkup:
    rows = []
    join_url = subscription_url()
    if join_url:
        rows.append([
            InlineKeyboardButton("📢 Kanalga obuna bo'lish", url=join_url)
        ])
    rows.append([
        InlineKeyboardButton("✅ Obunani tekshirish", callback_data="checksub")
    ])
    return InlineKeyboardMarkup(rows)


async def get_subscription_state(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool | None:
    """
    True  -> obuna bor
    False -> obuna yo'q
    None  -> kanal/bot sozlamasida xato
    """
    if not REQUIRED_CHANNEL:
        return True

    try:
        member = await context.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL,
            user_id=user_id,
        )
    except TelegramError as exc:
        logger.warning("Subscription check failed: %s", exc)
        return None

    status = str(getattr(member, "status", "")).lower()

    if status in {"creator", "administrator", "member"}:
        return True

    # Restricted foydalanuvchi kanal a'zosi bo'lib qolgan bo'lishi mumkin.
    if status == "restricted":
        return bool(getattr(member, "is_member", False))

    return False


async def send_subscription_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit_callback: bool = False,
) -> None:
    text = (
        "🔒 Botdan foydalanish uchun avval kanalimizga obuna bo'ling.\n\n"
        f"📢 Kanal: {REQUIRED_CHANNEL_NAME}\n\n"
        "Obuna bo'lgach, «✅ Obunani tekshirish» tugmasini bosing."
    )
    markup = subscription_keyboard()

    query = update.callback_query
    if edit_callback and query and query.message:
        try:
            await query.message.edit_text(text, reply_markup=markup)
            return
        except TelegramError:
            pass

    message = update.effective_message
    if message:
        await message.reply_text(
            text,
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not REQUIRED_CHANNEL:
        return True

    user = update.effective_user
    if not user:
        return False

    state = await get_subscription_state(context, user.id)

    if state is True:
        return True

    if state is None:
        message = update.effective_message
        if message:
            await message.reply_text(
                "⚠️ Obunani tekshirib bo'lmadi.\n"
                "Bot egasi kanal sozlamalarini yoki botning kanaldagi admin huquqini tekshirishi kerak."
            )
        return False

    await send_subscription_prompt(update, context)
    return False



# -------------------------
# Commands / messages
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_subscription(update, context):
        return

    text = (
        "👋 Salom! Men Uz Navo botman.\n\n"
        "🔎 Qo'shiq nomi, ijrochi yoki matndan parcha yozing — YouTube'dan qidiraman.\n"
        "🎙 Voice/audio/video yuboring — ichidagi musiqani aniqlayman.\n"
        "🔗 YouTube/Instagram link yuboring — Video / Audio / Shazam / Effekt tugmalari chiqadi.\n"
        "🎛 Bass Booster, 8D, Slowed+Reverb va maxsus Pioneer Carrozzeria DEH-P919 DSP menyusi mavjud.\n\n"
        "Buyruqlar:\n"
        "/start — boshlash\n"
        "/help — yordam\n"
        "/id — chat ID\n\n"
        "⚠️ Faqat yuklab olishga ruxsatingiz bo'lgan kontentdan foydalaning."
    )
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_subscription(update, context):
        return
    await update.effective_message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def recognize_telegram_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if not await require_subscription(update, context):
        return

    attachment = None
    suffix = ".bin"
    is_video = False

    if message.audio:
        attachment = message.audio
        suffix = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
    elif message.voice:
        attachment = message.voice
        suffix = ".ogg"
    elif message.video:
        attachment = message.video
        suffix = Path(message.video.file_name or "video.mp4").suffix or ".mp4"
        is_video = True
    elif message.video_note:
        attachment = message.video_note
        suffix = ".mp4"
        is_video = True
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("audio/"):
            attachment = message.document
            suffix = Path(message.document.file_name or "audio.bin").suffix or ".bin"
        elif mime.startswith("video/"):
            attachment = message.document
            suffix = Path(message.document.file_name or "video.bin").suffix or ".bin"
            is_video = True

    if not attachment:
        return

    if is_video and not ffmpeg_exists():
        await message.reply_text("❌ FFmpeg topilmadi.")
        return

    status = await message.reply_text("🔎 Musiqani qidiryapman...")
    try:
        with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
            workdir = Path(td)
            src = workdir / f"telegram_media{suffix}"
            tg_file = await attachment.get_file()
            await tg_file.download_to_drive(custom_path=src)
            media_duration = getattr(attachment, "duration", None)
            song = await recognize_media_fast(src, workdir, media_duration)
        await status.delete()
        await send_song_result(message, song)
    except Exception as exc:
        logger.exception("Telegram media recognition failed")
        await status.edit_text(f"❌ Xatolik: {type(exc).__name__}: {exc}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    url = first_supported_url(message.text or message.caption)
    if not url:
        return
    if not await require_subscription(update, context):
        return

    status = await message.reply_text("⚡ Ma'lumot olinmoqda...")
    try:
        info = await get_info(url)
        item = {
            "url": normalize_media_url(url),
            "id": info.get("id"),
            "title": info.get("title") or "Media",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        }
        await status.delete()
        await send_media_card(message, context, item)
    except Exception as exc:
        logger.exception("URL metadata failed")
        await status.edit_text(public_error_message(exc, "info"))


async def search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    if first_supported_url(message.text):
        return
    if update.effective_chat.type in ("group", "supergroup"):
        return
    if not await require_subscription(update, context):
        return

    query = " ".join(message.text.strip().split())
    if len(query) < 2:
        await message.reply_text("🔎 Qo'shiq nomi yoki matndan kamida 2 ta belgi yozing.")
        return

    status = await message.reply_text("🔎 Qidiryapman...")
    try:
        results = await search_youtube(query)
        if not results:
            await status.edit_text("❌ Hech narsa topilmadi.")
            return
        context.user_data["search_query"] = query
        context.user_data["search_results"] = results
        context.user_data["search_page"] = 0
        await status.delete()
        await render_search(message, context, 0, edit=False)
    except Exception as exc:
        logger.exception("Search failed")
        await status.edit_text(public_error_message(exc, "search"))


async def enqueue_media_job(
    *,
    chat_id: int,
    status_message_id: int,
    action: str,
    item: dict,
    effect_key: str | None = None,
) -> bool:
    """Redis mavjud bo'lsa media ishini alohida workerga topshiradi. Redis ishlamasa False qaytaradi."""
    if not JOB_QUEUE.enabled:
        return False
    try:
        await JOB_QUEUE.enqueue({
            "chat_id": chat_id,
            "status_message_id": status_message_id,
            "action": action,
            "url": item.get("url"),
            "item": {
                "url": item.get("url"),
                "id": item.get("id"),
                "title": item.get("title"),
                "uploader": item.get("uploader"),
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnail"),
            },
            "effect_key": effect_key,
        })
        return True
    except Exception:
        logger.exception("Redis queue unavailable; inline fallback will be used")
        return False


# -------------------------
# Callback buttons
# -------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    data = query.data or ""

    if data == "checksub":
        if not REQUIRED_CHANNEL:
            await query.answer("Majburiy obuna sozlanmagan.", show_alert=True)
            return

        state = await get_subscription_state(context, update.effective_user.id)
        if state is True:
            await query.answer("✅ Obuna tasdiqlandi!", show_alert=False)
            try:
                await query.message.edit_text(
                    "✅ Obuna tasdiqlandi!\n\n"
                    "Endi botdan foydalanishingiz mumkin. Qo'shiq nomini yozing, "
                    "audio/video yuboring yoki YouTube/Instagram link jo'nating."
                )
            except TelegramError:
                pass
            return

        if state is None:
            await query.answer(
                "⚠️ Tekshiruv ishlamadi. Bot kanalga admin qilinganini tekshiring.",
                show_alert=True,
            )
            return

        await query.answer(
            "❌ Hali kanalga obuna bo'lmagansiz.",
            show_alert=True,
        )
        return

    if REQUIRED_CHANNEL:
        state = await get_subscription_state(context, update.effective_user.id)
        if state is not True:
            if state is None:
                await query.answer(
                    "⚠️ Obuna tekshiruvi ishlamayapti. Bot egasi sozlamalarni tekshirishi kerak.",
                    show_alert=True,
                )
            else:
                await query.answer(
                    "🔒 Avval kanalga obuna bo'ling.",
                    show_alert=True,
                )
                await send_subscription_prompt(update, context)
            return

    if data == "close":
        await query.answer()
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return

    if data.startswith("page:"):
        await query.answer()
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await render_search(query.message, context, page, edit=True)
        return

    if data.startswith("pick:"):
        await query.answer()
        try:
            idx = int(data.split(":", 1)[1])
            results = context.user_data.get("search_results") or []
            item = results[idx]
        except (ValueError, IndexError):
            await query.answer("Natija eskirgan. Qayta qidiring.", show_alert=True)
            return
        await send_media_card(query.message, context, item)
        return

    if data.startswith("p919back:"):
        token = data.split(":", 1)[1]
        item = (context.user_data.get("media_items") or {}).get(token)
        await query.answer()
        if not item:
            await query.answer("Bu tugma eskirgan. Qayta qidiring.", show_alert=True)
            return
        try:
            await query.message.edit_reply_markup(reply_markup=effects_keyboard(token))
        except TelegramError:
            pass
        return

    if data.startswith("p919:"):
        token = data.split(":", 1)[1]
        item = (context.user_data.get("media_items") or {}).get(token)
        await query.answer()
        if not item:
            await query.answer("Bu tugma eskirgan. Qayta qidiring.", show_alert=True)
            return
        try:
            await query.message.edit_reply_markup(reply_markup=p919_keyboard(token))
        except TelegramError:
            await query.message.reply_text(
                "🚘 DEH-P919 DSP rejimini tanlang:",
                reply_markup=p919_keyboard(token),
            )
        return

    if data.startswith("fxback:"):
        token = data.split(":", 1)[1]
        item = (context.user_data.get("media_items") or {}).get(token)
        await query.answer()
        if not item:
            await query.answer("Bu tugma eskirgan. Qayta qidiring.", show_alert=True)
            return
        try:
            await query.message.edit_reply_markup(reply_markup=media_keyboard(token))
        except TelegramError:
            pass
        return

    if data.startswith("fx:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Noto'g'ri effekt tugmasi.", show_alert=True)
            return
        _, effect_key, token = parts
        item = (context.user_data.get("media_items") or {}).get(token)
        if not item or effect_key not in EFFECTS:
            await query.answer("Bu tugma eskirgan. Qayta qidiring.", show_alert=True)
            return

        await query.answer()
        if not ffmpeg_exists():
            await query.message.reply_text("❌ FFmpeg topilmadi.")
            return

        url = item.get("url")
        label = EFFECTS[effect_key][0]
        status = await query.message.reply_text(f"🎛 {label} tayyorlanyapti...")
        if await enqueue_media_job(
            chat_id=query.message.chat_id,
            status_message_id=status.message_id,
            action="effect",
            item=item,
            effect_key=effect_key,
        ):
            return
        try:
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
                workdir = Path(td)
                audio_path, info = await download_audio(url, workdir)
                effect_path = workdir / f"effect_{effect_key}.mp3"
                await status.edit_text(f"🎚 {label} qo'llanmoqda...")
                await apply_audio_effect(audio_path, effect_path, effect_key)

                if effect_path.stat().st_size > MAX_SEND_MB * 1024 * 1024:
                    await status.edit_text(
                        f"⚠️ Tayyor audio {readable_size(effect_path)}. Telegram limiti uchun juda katta."
                    )
                    return

                title = (info.get("title") or item.get("title") or "Audio")
                performer = (info.get("uploader") or item.get("uploader") or "")
                await status.edit_text(f"📤 {label} yuborilyapti ({readable_size(effect_path)})...")
                with effect_path.open("rb") as fh:
                    await query.message.reply_audio(
                        audio=fh,
                        title=f"{title[:48]} [{label.replace(chr(127769), '').strip()[:14]}]"[:64],
                        performer=performer[:64] or None,
                        caption=f"🎛 Effekt: {label}\n🎵 {title}"[:1024],
                        read_timeout=180,
                        write_timeout=180,
                    )
            await status.delete()
        except Exception as exc:
            logger.exception("Effect processing failed")
            await status.edit_text(public_error_message(exc, "effect"))
        return

    if not data.startswith("m:"):
        await query.answer()
        return

    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer("Noto'g'ri tugma.", show_alert=True)
        return

    _, action, token = parts
    item = (context.user_data.get("media_items") or {}).get(token)
    if not item:
        await query.answer("Bu tugma eskirgan. Linkni yoki qidiruvni qayta yuboring.", show_alert=True)
        return

    await query.answer()
    url = item.get("url")
    if not url:
        await query.message.reply_text("❌ Media URL topilmadi.")
        return

    if action == "e":
        try:
            await query.message.edit_reply_markup(reply_markup=effects_keyboard(token))
        except TelegramError:
            await query.message.reply_text("🎛 Effektni tanlang:", reply_markup=effects_keyboard(token))
        return

    if action == "v":
        status = await query.message.reply_text("⬇️ Video yuklanyapti...")
        if await enqueue_media_job(
            chat_id=query.message.chat_id,
            status_message_id=status.message_id,
            action="video",
            item=item,
        ):
            return
        try:
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
                workdir = Path(td)
                media_path, info = await download_video(url, workdir)
                if media_path.stat().st_size > MAX_SEND_MB * 1024 * 1024:
                    await status.edit_text(
                        f"⚠️ Video {readable_size(media_path)}. Telegram limiti uchun juda katta."
                    )
                    return
                await status.edit_text(f"📤 Video yuborilyapti ({readable_size(media_path)})...")
                try:
                    await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_VIDEO)
                    with media_path.open("rb") as fh:
                        await query.message.reply_video(
                            video=fh,
                            caption=(info.get("title") or item.get("title") or "Video")[:1024],
                            supports_streaming=True,
                            read_timeout=120,
                            write_timeout=120,
                        )
                except TelegramError:
                    with media_path.open("rb") as fh:
                        await query.message.reply_document(
                            document=fh,
                            caption=(info.get("title") or item.get("title") or "Video")[:1024],
                            read_timeout=120,
                            write_timeout=120,
                        )
            await status.delete()
        except Exception as exc:
            logger.exception("Video download failed")
            await status.edit_text(public_error_message(exc, "video"))
        return

    if action == "a":
        status = await query.message.reply_text("🎧 Audio yuklanyapti...")
        if await enqueue_media_job(
            chat_id=query.message.chat_id,
            status_message_id=status.message_id,
            action="audio",
            item=item,
        ):
            return
        try:
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
                workdir = Path(td)
                audio_path, info = await download_audio(url, workdir)
                send_path = audio_path
                if audio_path.suffix.lower() not in {".mp3", ".m4a"}:
                    await status.edit_text("🎧 Audio tayyorlanyapti...")
                    send_path = workdir / "audio.mp3"
                    await convert_to_mp3(audio_path, send_path)
                if send_path.stat().st_size > MAX_SEND_MB * 1024 * 1024:
                    await status.edit_text(
                        f"⚠️ Audio {readable_size(send_path)}. Telegram limiti uchun juda katta."
                    )
                    return
                await status.edit_text(f"📤 Audio yuborilyapti ({readable_size(send_path)})...")
                with send_path.open("rb") as fh:
                    await query.message.reply_audio(
                        audio=fh,
                        title=(info.get("title") or item.get("title") or "Audio")[:64],
                        performer=(info.get("uploader") or item.get("uploader") or "")[:64] or None,
                        read_timeout=120,
                        write_timeout=120,
                    )
            await status.delete()
        except Exception as exc:
            logger.exception("Audio download failed")
            await status.edit_text(public_error_message(exc, "audio"))
        return

    if action == "s":
        if not ffmpeg_exists():
            await query.message.reply_text("❌ FFmpeg topilmadi.")
            return
        status = await query.message.reply_text("🔎 Musiqa aniqlanyapti...")
        if await enqueue_media_job(
            chat_id=query.message.chat_id,
            status_message_id=status.message_id,
            action="recognize",
            item=item,
        ):
            return
        try:
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
                workdir = Path(td)
                audio_path, info = await download_audio(url, workdir)
                song = await recognize_media_fast(audio_path, workdir, info.get("duration"))
            await status.delete()
            await send_song_result(query.message, song)
        except Exception as exc:
            logger.exception("URL recognition failed")
            await status.edit_text(public_error_message(exc, "recognize"))
        return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi. .env fayl yarating va BOT_TOKEN=... yozing.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(CONCURRENT_UPDATES)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .media_write_timeout(120)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))

    media_filter = (
        filters.AUDIO
        | filters.VOICE
        | filters.VIDEO
        | filters.VIDEO_NOTE
        | filters.Document.AUDIO
        | filters.Document.VIDEO
    )
    app.add_handler(MessageHandler(media_filter, recognize_telegram_media))

    # URL birinchi guruhda; oddiy matn ikkinchi guruhda qidiruvga ketadi.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_text), group=1)

    app.add_error_handler(error_handler)
    logger.info("Bot ishga tushdi.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
