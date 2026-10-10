from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import TelegramError

from bot import (
    BOT_TOKEN,
    DOWNLOAD_DIR,
    EFFECTS,
    MAX_SEND_MB,
    apply_audio_effect,
    convert_to_mp3,
    download_audio,
    download_video,
    ffmpeg_exists,
    readable_size,
    recognize_media_fast,
    song_text,
)
from job_queue import MediaJobQueue
from media_providers import ProviderTemporaryError, public_error_message

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("uz_navo.worker")

WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "2")))
JOB_RETRIES = max(0, int(os.getenv("JOB_RETRIES", "1")))


async def edit_status(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except TelegramError:
        pass


async def delete_status(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


def too_large(path: Path) -> bool:
    return path.stat().st_size > MAX_SEND_MB * 1024 * 1024


async def send_cached(bot: Bot, chat_id: int, cached: dict) -> bool:
    file_id = cached.get("file_id")
    kind = cached.get("kind")
    if not file_id:
        return False
    try:
        if kind == "audio":
            await bot.send_audio(chat_id=chat_id, audio=file_id, caption=cached.get("caption"))
        elif kind == "video":
            await bot.send_video(chat_id=chat_id, video=file_id, caption=cached.get("caption"), supports_streaming=True)
        elif kind == "document":
            await bot.send_document(chat_id=chat_id, document=file_id, caption=cached.get("caption"))
        else:
            return False
        return True
    except TelegramError:
        return False


async def process_job(bot: Bot, queue: MediaJobQueue, job: dict) -> None:
    action = str(job.get("action") or "")
    url = str(job.get("url") or "")
    chat_id = int(job["chat_id"])
    status_id = int(job["status_message_id"])
    item = job.get("item") or {}
    effect_key = job.get("effect_key")

    if action in {"audio", "video", "effect"}:
        cache_key = queue.media_cache_key(action, url, str(effect_key or ""))
        cached = await queue.cache_get(cache_key)
        if cached and await send_cached(bot, chat_id, cached):
            await delete_status(bot, chat_id, status_id)
            logger.info("Cache hit for %s", action)
            return

    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as td:
        workdir = Path(td)

        if action == "audio":
            await edit_status(bot, chat_id, status_id, "🎧 Audio yuklanyapti...")
            audio_path, info = await download_audio(url, workdir)
            send_path = audio_path
            if audio_path.suffix.lower() not in {".mp3", ".m4a"}:
                await edit_status(bot, chat_id, status_id, "🎧 Audio tayyorlanyapti...")
                send_path = workdir / "audio.mp3"
                await convert_to_mp3(audio_path, send_path)
            if too_large(send_path):
                await edit_status(bot, chat_id, status_id, f"⚠️ Audio {readable_size(send_path)}. Telegram limiti uchun juda katta.")
                return
            await edit_status(bot, chat_id, status_id, f"📤 Audio yuborilyapti ({readable_size(send_path)})...")
            with send_path.open("rb") as fh:
                msg = await bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    title=(info.get("title") or item.get("title") or "Audio")[:64],
                    performer=(info.get("uploader") or item.get("uploader") or "")[:64] or None,
                    read_timeout=180,
                    write_timeout=180,
                )
            if msg.audio:
                await queue.cache_set(cache_key, {"kind": "audio", "file_id": msg.audio.file_id})
            await delete_status(bot, chat_id, status_id)
            return

        if action == "video":
            await edit_status(bot, chat_id, status_id, "⬇️ Video yuklanyapti...")
            media_path, info = await download_video(url, workdir)
            if too_large(media_path):
                await edit_status(bot, chat_id, status_id, f"⚠️ Video {readable_size(media_path)}. Telegram limiti uchun juda katta.")
                return
            caption = (info.get("title") or item.get("title") or "Video")[:1024]
            await edit_status(bot, chat_id, status_id, f"📤 Video yuborilyapti ({readable_size(media_path)})...")
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            try:
                with media_path.open("rb") as fh:
                    msg = await bot.send_video(
                        chat_id=chat_id,
                        video=fh,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                    )
                if msg.video:
                    await queue.cache_set(cache_key, {"kind": "video", "file_id": msg.video.file_id, "caption": caption})
            except TelegramError:
                with media_path.open("rb") as fh:
                    msg = await bot.send_document(
                        chat_id=chat_id,
                        document=fh,
                        caption=caption,
                        read_timeout=180,
                        write_timeout=180,
                    )
                if msg.document:
                    await queue.cache_set(cache_key, {"kind": "document", "file_id": msg.document.file_id, "caption": caption})
            await delete_status(bot, chat_id, status_id)
            return

        if action == "effect":
            if effect_key not in EFFECTS:
                raise RuntimeError("Noto‘g‘ri effekt")
            if not ffmpeg_exists():
                raise RuntimeError("FFmpeg topilmadi")
            label = EFFECTS[effect_key][0]
            await edit_status(bot, chat_id, status_id, f"🎛 {label} tayyorlanyapti...")
            audio_path, info = await download_audio(url, workdir)
            effect_path = workdir / f"effect_{effect_key}.mp3"
            await edit_status(bot, chat_id, status_id, f"🎚 {label} qo‘llanmoqda...")
            await apply_audio_effect(audio_path, effect_path, effect_key)
            if too_large(effect_path):
                await edit_status(bot, chat_id, status_id, f"⚠️ Tayyor audio {readable_size(effect_path)}. Telegram limiti uchun juda katta.")
                return
            title = info.get("title") or item.get("title") or "Audio"
            performer = info.get("uploader") or item.get("uploader") or ""
            caption = f"🎛 Effekt: {label}\n🎵 {title}"[:1024]
            await edit_status(bot, chat_id, status_id, f"📤 {label} yuborilyapti ({readable_size(effect_path)})...")
            with effect_path.open("rb") as fh:
                msg = await bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    title=f"{title[:48]} [{label[:14]}]"[:64],
                    performer=performer[:64] or None,
                    caption=caption,
                    read_timeout=180,
                    write_timeout=180,
                )
            if msg.audio:
                await queue.cache_set(cache_key, {"kind": "audio", "file_id": msg.audio.file_id, "caption": caption})
            await delete_status(bot, chat_id, status_id)
            return

        if action == "recognize":
            if not ffmpeg_exists():
                raise RuntimeError("FFmpeg topilmadi")
            await edit_status(bot, chat_id, status_id, "🔎 Musiqa aniqlanyapti...")
            audio_path, info = await download_audio(url, workdir)
            song = await recognize_media_fast(audio_path, workdir, info.get("duration"))
            await delete_status(bot, chat_id, status_id)
            text = song_text(song)
            cover = song.get("cover") if song else None
            if cover:
                try:
                    await bot.send_photo(chat_id=chat_id, photo=cover, caption=text)
                    return
                except TelegramError:
                    pass
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
            return

        raise RuntimeError(f"Noma'lum job action: {action}")


async def consumer(index: int, bot: Bot, queue: MediaJobQueue) -> None:
    logger.info("Worker consumer %s started", index)
    while True:
        try:
            job = await queue.dequeue(timeout=5)
            if not job:
                continue
            try:
                await process_job(bot, queue, job)
            except Exception as exc:
                logger.exception("Job failed: %s", job.get("job_id"))
                attempt = int(job.get("attempt", 0))
                if isinstance(exc, ProviderTemporaryError) and attempt < JOB_RETRIES:
                    job["attempt"] = attempt + 1
                    await asyncio.sleep(min(10, 2 ** (attempt + 1)))
                    await queue.requeue(job)
                    continue
                await edit_status(
                    bot,
                    int(job["chat_id"]),
                    int(job["status_message_id"]),
                    public_error_message(exc, str(job.get("action") or "media")),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker loop error")
            await asyncio.sleep(2)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi")
    queue = MediaJobQueue()
    if not queue.enabled:
        raise RuntimeError("Worker uchun REDIS_URL kerak")
    if not await queue.ping():
        raise RuntimeError("Redis bilan ulanish amalga oshmadi")

    async with Bot(token=BOT_TOKEN) as bot:
        tasks = [asyncio.create_task(consumer(i + 1, bot, queue)) for i in range(WORKER_CONCURRENCY)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await queue.close()


if __name__ == "__main__":
    asyncio.run(main())
