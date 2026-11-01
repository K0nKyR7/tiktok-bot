import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp
import imageio_ffmpeg
import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaPhoto
from dotenv import load_dotenv

# Загрузка переменных окружения
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env файле или переменных окружения!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("media_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Регулярные выражения для поиска ссылок
TIKTOK_URL_REGEX = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+"
)
YOUTUBE_URL_REGEX = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)[a-zA-Z0-9_\-]+"
)

# Максимальный размер файла для отправки через Telegram Bot API (50 МБ)
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024


def to_absolute_url(url: Optional[str]) -> Optional[str]:
    """Преобразует относительный путь TikWM в полный URL."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://www.tikwm.com{url}"


async def fetch_tiktok_data(url: str) -> Optional[dict]:
    """Запрашивает данные видео через TikWM API."""
    api_url = "https://www.tikwm.com/api/"
    params = {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=headers) as session:
        try:
            async with session.post(api_url, data=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0:
                        return data.get("data")
                    else:
                        logger.warning(f"TikWM вернул сообщение: {data.get('msg')}")
        except Exception as e:
            logger.error(f"Ошибка запроса к TikWM API: {e}")
    return None


def download_youtube_video(url: str, output_dir: str):
    """Синхронная функция скачивания YouTube-видео через yt-dlp."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ydl_opts = {
        'ffmpeg_location': ffmpeg_exe,
        'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'max_filesize': MAX_TELEGRAM_FILE_SIZE,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info.get("id")
        expected_file = os.path.join(output_dir, f"{video_id}.mp4")
        if not os.path.exists(expected_file):
            for f in os.listdir(output_dir):
                if f.startswith(str(video_id)):
                    expected_file = os.path.join(output_dir, f)
                    break
        return info, expected_file


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Привет!**\n\n"
        "Я умею скачивать видео из **TikTok** и **YouTube** (включая Shorts)!\n\n"
        "Отправь мне ссылку:\n"
        "• **TikTok** (видео без водяного знака и фото-карусели)\n"
        "• **YouTube / Shorts** (видео до 50 МБ)",
        parse_mode=ParseMode.MARKDOWN,
    )


# --- Обработчик ссылок TikTok ---
@dp.message(F.text.regexp(TIKTOK_URL_REGEX))
async def handle_tiktok_link(message: types.Message):
    match = TIKTOK_URL_REGEX.search(message.text)
    if not match:
        return

    tiktok_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю из TikTok, пожалуйста, подождите...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        data = await fetch_tiktok_data(tiktok_url)

        if not data:
            await status_msg.edit_text("❌ Не удалось найти видео TikTok. Возможно, аккаунт приватный или видео удалено.")
            return

        title = data.get("title") or ""
        clean_title = (title[:180] + "...") if len(title) > 180 else title
        caption = f"🎬 {clean_title}" if clean_title else "🎬 TikTok видео"

        # 1. Фото-слайдшоу
        images = data.get("images")
        if images and isinstance(images, list):
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_PHOTO)
            media_group = [
                InputMediaPhoto(media=to_absolute_url(img_url), caption=caption if i == 0 else "")
                for i, img_url in enumerate(images[:10])
            ]
            await message.reply_media_group(media=media_group)
            await status_msg.delete()
            return

        # 2. Видео без водяного знака
        raw_video_url = data.get("play") or data.get("hdplay")
        video_url = to_absolute_url(raw_video_url)

        if not video_url:
            await status_msg.edit_text("❌ Не найдена ссылка на скачивание без водяного знака.")
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=headers) as session:
            async with session.get(video_url) as resp:
                if resp.status == 200:
                    video_bytes = await resp.read()
                    video_file = BufferedInputFile(video_bytes, filename="tiktok_video.mp4")
                    await message.reply_video(video=video_file, caption=caption)
                else:
                    await message.reply_video(video=video_url, caption=caption)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка при обработке TikTok ссылки: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Произошла ошибка при скачивании из TikTok.")


# --- Обработчик ссылок YouTube / Shorts ---
@dp.message(F.text.regexp(YOUTUBE_URL_REGEX))
async def handle_youtube_link(message: types.Message):
    match = YOUTUBE_URL_REGEX.search(message.text)
    if not match:
        return

    yt_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю видео из YouTube, пожалуйста, подождите...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Скачиваем видео в отдельном потоке, чтобы не блокировать бота
            info, file_path = await asyncio.to_thread(download_youtube_video, yt_url, tmp_dir)

            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Не удалось скачать видео с YouTube.")
                return

            file_size = os.path.getsize(file_path)
            if file_size > MAX_TELEGRAM_FILE_SIZE:
                await status_msg.edit_text(
                    f"❌ Видео слишком большое ({file_size // (1024 * 1024)} МБ).\n"
                    "Telegram разрешает ботам отправлять файлы размером не более 50 МБ."
                )
                return

            title = info.get("title") or "YouTube video"
            clean_title = (title[:180] + "...") if len(title) > 180 else title
            caption = f"🎬 {clean_title}"

            video_file = FSInputFile(file_path, filename=f"youtube_{info.get('id')}.mp4")
            await message.reply_video(video=video_file, caption=caption)
            await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка при обработке YouTube ссылки: {e}", exc_info=True)
        err_msg = str(e)
        if "File is larger than max_filesize" in err_msg:
            await status_msg.edit_text("❌ Размер этого видео превышает лимит Telegram (50 МБ).")
        else:
            await status_msg.edit_text("⚠️ Не удалось скачать видео с YouTube. Проверьте ссылку или доступность ролика.")


async def start_health_check_server():
    """Запускает мини веб-сервер для Render Web Service."""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Bot is running!")

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер проверки здоровья запущен на порту {port}")


async def main():
    logger.info("Запуск бота...")
    await bot.delete_webhook(drop_pending_updates=True)

    if os.getenv("PORT"):
        await start_health_check_server()

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
