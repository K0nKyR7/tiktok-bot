import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
import imageio_ffmpeg
import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
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

COOKIES_FILE = Path(__file__).resolve().parent / "cookies.txt"
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="⚡️ Статус бота")],
            [KeyboardButton(text="ℹ️ О боте")]
        ],
        resize_keyboard=True
    )


def get_ffmpeg_path() -> Optional[str]:
    """Находит ffmpeg и гарантирует права на выполнение (особенно для Linux/Render)."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            try:
                st = os.stat(exe)
                os.chmod(exe, st.st_mode | 0o111)
            except Exception:
                pass
            return exe
    except Exception as e:
        logger.warning(f"Не удалось получить ffmpeg из imageio: {e}")
    return None


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
    """Скачивание YouTube-видео через yt-dlp с cookies."""
    ffmpeg_exe = get_ffmpeg_path()
    ydl_opts = {
        'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/18/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

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


def download_youtube_audio(video_id: str, output_dir: str):
    """Скачивание аудиодорожки с YouTube через cookies."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ffmpeg_exe = get_ffmpeg_path()
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/18/best',
        'outtmpl': os.path.join(output_dir, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    if COOKIES_FILE.exists():
        ydl_opts['cookiefile'] = str(COOKIES_FILE)
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        expected_file = os.path.join(output_dir, f"{video_id}.m4a")
        if not os.path.exists(expected_file):
            for f in os.listdir(output_dir):
                if f.startswith(str(video_id)):
                    expected_file = os.path.join(output_dir, f)
                    break
        return info, expected_file


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать!**\n\n"
        "Я бот для скачивания медиа из **TikTok** и **YouTube**!\n\n"
        "📌 **Что я умею:**\n"
        "• 🎬 **TikTok** — видео без водяного знака и фото-карусели\n"
        "• 🎥 **YouTube & Shorts** — видео высокого качества со звуком\n"
        "• 🎵 **Аудио** — скачивание треков и музыки в один клик\n\n"
        "Просто отправь мне ссылку!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(),
    )


# --- Кнопки главного меню ---
@dp.message(F.text == "📖 Инструкция")
async def btn_instruction(message: types.Message):
    await message.answer(
        "📖 **Инструкция по использованию:**\n\n"
        "1. Скопируйте ссылку на видео в приложении или браузере:\n"
        "   • TikTok (кнопка «Поделиться» ➔ «Ссылка»)\n"
        "   • YouTube или Shorts (кнопка «Поделиться»)\n"
        "2. Отправьте ссылку боту в этот чат.\n"
        "3. Бот сразу обработает её и пришлет чистое видео!\n\n"
        "💡 Под каждым видео будет кнопка **«🎵 Скачать аудио»** для сохранения трека.",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "⚡️ Статус бота")
async def btn_status(message: types.Message):
    start_t = time.time()
    msg = await message.answer("🔄 Проверка связи...")
    ping = round((time.time() - start_t) * 1000)
    await msg.edit_text(
        "🟢 **Бот работает стабильно 24/7!**\n\n"
        f"⚡️ Отклик сервера: **{ping} мс**\n"
        "☁️ Хостинг: **Render Cloud**\n"
        "🛡 Загрузка: **TikTok + YouTube**\n"
        "📦 Макс. размер: **50 МБ**",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "ℹ️ О боте")
async def btn_about(message: types.Message):
    await message.answer(
        "ℹ️ **О боте:**\n\n"
        "🤖 Бот скачивает ролики без водяных знаков и рекламы.\n"
        "🚀 Быстрая загрузка благодаря облачному серверу.\n"
        "✨ Поддерживает фото-карусели, обычные видео, Shorts и музыку.",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Обработчик ссылок TikTok ---
@dp.message(F.text.regexp(TIKTOK_URL_REGEX))
async def handle_tiktok_link(message: types.Message):
    match = TIKTOK_URL_REGEX.search(message.text)
    if not match:
        return

    tiktok_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю из TikTok...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        data = await fetch_tiktok_data(tiktok_url)

        if not data:
            await status_msg.edit_text("❌ Не удалось найти видео TikTok. Возможно, оно приватное или удалено.")
            return

        title = data.get("title") or ""
        clean_title = (title[:180] + "...") if len(title) > 180 else title
        caption = f"🎬 {clean_title}" if clean_title else "🎬 TikTok видео"
        video_id = data.get("id", "")

        # Инлайн-кнопки под видео
        buttons = []
        if data.get("music"):
            buttons.append([InlineKeyboardButton(text="🎵 Скачать аудио", callback_data=f"tta:{video_id}")])
        buttons.append([InlineKeyboardButton(text="🔗 Открыть оригинал", url=tiktok_url)])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        # 1. Фото-карусель
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
            await status_msg.edit_text("❌ Не найдена ссылка на скачивание.")
            return

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=headers) as session:
            async with session.get(video_url) as resp:
                if resp.status == 200:
                    video_bytes = await resp.read()
                    video_file = BufferedInputFile(video_bytes, filename="tiktok_video.mp4")
                    await message.reply_video(video=video_file, caption=caption, reply_markup=keyboard)
                else:
                    await message.reply_video(video=video_url, caption=caption, reply_markup=keyboard)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка при обработке TikTok: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Ошибка при скачивании из TikTok. Попробуйте еще раз.")


# --- Обработчик ссылок YouTube / Shorts ---
@dp.message(F.text.regexp(YOUTUBE_URL_REGEX))
async def handle_youtube_link(message: types.Message):
    match = YOUTUBE_URL_REGEX.search(message.text)
    if not match:
        return

    yt_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю видео из YouTube, подождите...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as tmp_dir:
            info, file_path = await asyncio.to_thread(download_youtube_video, yt_url, tmp_dir)

            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Не удалось найти или скачать видео.")
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
            vid_id = info.get("id", "")

            # Инлайн-кнопки под видео
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎵 Скачать только аудио", callback_data=f"yta:{vid_id}")],
                    [InlineKeyboardButton(text="🔗 Открыть на YouTube", url=yt_url)]
                ]
            )

            video_file = FSInputFile(file_path, filename=f"youtube_{vid_id}.mp4")
            await message.reply_video(video=video_file, caption=caption, reply_markup=keyboard)
            await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка при обработке YouTube: {e}", exc_info=True)
        await status_msg.edit_text(f"⚠️ Ошибка: {str(e)[:160]}")


# --- Callback: Скачивание аудио TikTok ---
@dp.callback_query(F.data.startswith("tta:"))
async def callback_tiktok_audio(callback: CallbackQuery):
    video_id = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Скачиваю аудио...")

    try:
        data = await fetch_tiktok_data(f"https://www.tiktok.com/@tiktok/video/{video_id}")
        if not data or not data.get("music"):
            await callback.message.reply("❌ Аудиодорожка не найдена.")
            return

        audio_url = to_absolute_url(data.get("music"))
        music_info = data.get("music_info") or {}
        track_title = music_info.get("title") or "Audio"
        author = music_info.get("author") or "TikTok"

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(audio_url) as resp:
                if resp.status == 200:
                    audio_bytes = await resp.read()
                    audio_file = BufferedInputFile(audio_bytes, filename=f"{track_title}.mp3")
                    await callback.message.reply_audio(
                        audio=audio_file,
                        title=track_title,
                        performer=author
                    )
    except Exception as e:
        logger.error(f"Ошибка скачивания TikTok аудио: {e}")
        await callback.message.reply("⚠️ Не удалось загрузить аудио.")


# --- Callback: Скачивание аудио YouTube ---
@dp.callback_query(F.data.startswith("yta:"))
async def callback_youtube_audio(callback: CallbackQuery):
    video_id = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Скачиваю аудио...")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            info, file_path = await asyncio.to_thread(download_youtube_audio, video_id, tmp_dir)
            if file_path and os.path.exists(file_path):
                title = info.get("title") or "Audio"
                channel = info.get("uploader") or "YouTube"
                audio_file = FSInputFile(file_path, filename=f"{title}.m4a")
                await callback.message.reply_audio(
                    audio=audio_file,
                    title=title[:60],
                    performer=channel[:60]
                )
    except Exception as e:
        logger.error(f"Ошибка скачивания YouTube аудио: {e}")
        await callback.message.reply("⚠️ Не удалось загрузить аудиодорожку.")


async def start_health_check_server():
    """Запускает веб-сервер для Render Web Service."""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Bot is running! Version: v4-yt-fixed")

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {port}")


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
