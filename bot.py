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

# Регулярные выражения для поиска ссылок на 4 платформы
TIKTOK_URL_REGEX = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+"
)
YOUTUBE_URL_REGEX = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)[a-zA-Z0-9_\-]+"
)
INSTAGRAM_URL_REGEX = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([a-zA-Z0-9_\-]+)"
)
PINTEREST_URL_REGEX = re.compile(
    r"https?://(?:[a-zA-Z0-9_\-]+\.)?pinterest\.[a-z.]+/(?:pin/\d+|idea/\d+)|https?://pin\.it/[a-zA-Z0-9_\-]+"
)

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
    """Находит ffmpeg и гарантирует права на выполнение на Linux/Render."""
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


# ==========================================
# 1. TIKTOK
# ==========================================
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


# ==========================================
# 2. YOUTUBE (Мобильный Android InnerTube API)
# ==========================================
# 2. YOUTUBE (Мульти-стратегия с Android/iOS InnerTube и cookies)
# ==========================================
def download_youtube_video(url: str, output_dir: str):
    """
    Скачивание YouTube-видео:
    Попытка 1: Вариант 2 (Android/iOS API без загрузки webpage/configs - обход датацентрового бана)
    Попытка 2: Вариант 1 (cookies.txt, если сессия валидна)
    Попытка 3: Fallback Android/mweb
    """
    ffmpeg_exe = get_ffmpeg_path()
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    has_cookies = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 50

    strategies = [
        # Стратегия 1: Android + iOS InnerTube API с пропуском веб-страницы
        {
            "name": "mobile_innertube",
            "opts": {
                "format": "18/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios"],
                        "player_skip": ["webpage", "configs"],
                    }
                },
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                },
                "noplaylist": True,
            }
        },
    ]

    if has_cookies:
        strategies.append({
            "name": "cookies_txt",
            "opts": {
                "cookiefile": cookie_file,
                "format": "18/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                },
                "noplaylist": True,
            }
        })

    strategies.append({
        "name": "android_direct",
        "opts": {
            "format": "18/best[ext=mp4]/best",
            "extractor_args": {
                "youtube": {
                    "player_client": ["android"],
                    "player_skip": ["webpage", "configs"],
                }
            },
            "noplaylist": True,
        }
    })

    last_error = None
    for st in strategies:
        ydl_opts = {
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            **st["opts"]
        }
        if ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = ffmpeg_exe

        try:
            logger.info(f"Пробуем стратегию скачивания YouTube: {st['name']}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = info.get("id")
                for f in os.listdir(output_dir):
                    if str(video_id) in f:
                        return info, os.path.join(output_dir, f)
        except Exception as e:
            logger.warning(f"Стратегия YouTube '{st['name']}' завершилась ошибкой: {e}")
            last_error = e

    raise last_error or Exception("Не удалось скачать видео с YouTube.")


def download_youtube_audio(video_id: str, output_dir: str):
    """Скачивание аудиодорожки с YouTube через мобильный API или cookies."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ffmpeg_exe = get_ffmpeg_path()
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    has_cookies = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 50

    strategies = [
        {
            "name": "mobile_audio",
            "opts": {
                "format": "bestaudio[ext=m4a]/18/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios"],
                        "player_skip": ["webpage", "configs"],
                    }
                },
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                },
                "noplaylist": True,
            }
        }
    ]

    if has_cookies:
        strategies.append({
            "name": "cookies_audio",
            "opts": {
                "cookiefile": cookie_file,
                "format": "bestaudio[ext=m4a]/18/best",
                "noplaylist": True,
            }
        })

    last_error = None
    for st in strategies:
        ydl_opts = {
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            **st["opts"]
        }
        if ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = ffmpeg_exe

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                for f in os.listdir(output_dir):
                    if str(video_id) in f:
                        return info, os.path.join(output_dir, f)
        except Exception as e:
            logger.warning(f"Стратегия YouTube аудио '{st['name']}' завершилась ошибкой: {e}")
            last_error = e

    raise last_error or Exception("Не удалось скачать аудио с YouTube.")


# ==========================================
# 3. PINTEREST
# ==========================================
async def fetch_pinterest_media(url: str) -> Optional[dict]:
    """Парсинг видео или изображения из Pinterest (с разворачиванием pin.it)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            # 1. Прямое видео Pinterest CDN (720p/1080p)
            vids = re.findall(r'https?://v[0-9]?\.pinimg\.com/videos/[^\s"\'\\]+\.mp4', html)
            if vids:
                best_vid = vids[0]
                for v in vids:
                    if "720p" in v or "1080p" in v:
                        best_vid = v
                        break
                return {"type": "video", "url": best_vid}

            # 2. Оригинальное фото (originals)
            imgs = re.findall(r'https?://i\.pinimg\.com/originals/[^\s"\'\\]+\.(?:jpg|jpeg|png|webp)', html)
            if imgs:
                return {"type": "image", "url": imgs[0]}

            # 3. Резервный поиск через yt-dlp
            def extract_pin_ytdlp():
                opts = {"quiet": True, "no_warnings": True, "skip_download": True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info.get("url"):
                        return {"type": "video", "url": info.get("url")}
                return None

            res = await asyncio.to_thread(extract_pin_ytdlp)
            if res:
                return res

    except Exception as e:
        logger.error(f"Ошибка Pinterest: {e}")
    return None


# ==========================================
# 4. INSTAGRAM (vxinstagram + yt-dlp + vkr)
# ==========================================
async def fetch_instagram_media(url: str, shortcode: str) -> Optional[dict]:
    """Скачивание Instagram Reels / публикаций через надежные мульти-провайдеры."""

    # Способ 1: vxinstagram (проверенный генератор OpenGraph прямых ссылок)
    vx_endpoints = [
        f"https://www.vxinstagram.com/reel/{shortcode}/",
        f"https://www.vxinstagram.com/p/{shortcode}/",
    ]
    for vx_url in vx_endpoints:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "TelegramBot"}
            ) as session:
                async with session.get(vx_url) as r:
                    if r.status == 200:
                        html = await r.text()
                        # Ищем прямой URL видео в og:video или og:video:secure_url
                        vm = re.search(
                            r'<meta property=[\x22\x27]og:video(?::secure_url)?[\x22\x27] content=[\x22\x27]([^\x22\x27]+)[\x22\x27]',
                            html
                        )
                        if vm and vm.group(1).endswith(".mp4"):
                            logger.info(f"Instagram найден через vxinstagram: {vm.group(1)}")
                            return {"type": "video", "url": vm.group(1)}

                        # Если это публикация с фото
                        im = re.search(
                            r'<meta property=[\x22\x27]og:image[\x22\x27] content=[\x22\x27]([^\x22\x27]+)[\x22\x27]',
                            html
                        )
                        if im and not im.group(1).endswith(".mp4"):
                            logger.info(f"Instagram фото найдено через vxinstagram: {im.group(1)}")
                            return {"type": "image", "url": im.group(1)}
        except Exception as e:
            logger.warning(f"Ошибка vxinstagram ({vx_url}): {e}")

    # Способ 2: yt-dlp Instagram экстрактор
    def extract_ig_ytdlp():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("url"):
                return {"type": "video", "url": info.get("url")}
            formats = info.get("formats", [])
            if formats:
                return {"type": "video", "url": formats[-1].get("url")}
        return None

    try:
        res = await asyncio.to_thread(extract_ig_ytdlp)
        if res and res.get("url"):
            logger.info("Instagram найден через yt-dlp")
            return res
    except Exception as e:
        logger.warning(f"Ошибка yt-dlp instagram: {e}")

    # Способ 3: vkrdownloader API
    try:
        api_url = f"https://api.vkrdownloader.com/server?vkr={url}"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"}
        ) as session:
            async with session.get(api_url) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    downloads = data.get("data", {}).get("downloads", [])
                    for d in downloads:
                        if d.get("url"):
                            return {"type": "video", "url": d.get("url")}
    except Exception as e:
        logger.warning(f"Ошибка vkr: {e}")

    return None


# ==========================================
# ОБРАБОТЧИКИ КОМАНД И МЕНЮ
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать!**\n\n"
        "Я универсальный бот для скачивания медиа!\n\n"
        "📌 **Поддерживаемые платформы:**\n"
        "• 🎬 **TikTok** — видео без водяного знака и фото-карусели\n"
        "• 🎥 **YouTube & Shorts** — видео со звуком без рекламы\n"
        "• 📸 **Instagram** — Reels и публикации\n"
        "• 📌 **Pinterest** — видео и фото в высоком разрешении\n\n"
        "Просто отправь мне любую ссылку!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text == "📖 Инструкция")
async def btn_instruction(message: types.Message):
    await message.answer(
        "📖 **Инструкция по использованию:**\n\n"
        "1. Скопируйте ссылку на видео в любом приложении:\n"
        "   • **TikTok** (кнопка «Поделиться» ➔ «Ссылка»)\n"
        "   • **YouTube / Shorts** (кнопка «Поделиться»)\n"
        "   • **Instagram Reels** (кнопка «Поделиться» ➔ «Копировать ссылку»)\n"
        "   • **Pinterest** (кнопка «Поделиться» или `pin.it`)\n"
        "2. Отправьте ссылку боту.\n"
        "3. Бот сразу обработает её и пришлет медиафайл!\n\n"
        "💡 Для TikTok и YouTube под видео доступна кнопка **«🎵 Скачать аудио»**.",
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
        "🛡 Сервисы: **TikTok • YouTube • Instagram • Pinterest**\n"
        "📦 Макс. размер: **50 МБ**",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "ℹ️ О боте")
async def btn_about(message: types.Message):
    await message.answer(
        "ℹ️ **О боте:**\n\n"
        "🤖 Бот скачивает медиа без водяных знаков, рекламы и ограничений.\n"
        "🚀 Работает на автономном облачном сервере 24/7.\n"
        "✨ Поддерживает 4 популярные платформы, музыку и фото-карусели.",
        parse_mode=ParseMode.MARKDOWN
    )


# ==========================================
# ОБРАБОТЧИКИ ССЫЛОК
# ==========================================

# --- 1. TikTok ---
@dp.message(F.text.regexp(TIKTOK_URL_REGEX))
async def handle_tiktok(message: types.Message):
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

        buttons = []
        if data.get("music"):
            buttons.append([InlineKeyboardButton(text="🎵 Скачать аудио", callback_data=f"tta:{video_id}")])
        buttons.append([InlineKeyboardButton(text="🔗 Открыть в TikTok", url=tiktok_url)])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Фото-карусель
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

        # Видео без водяного знака
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


# --- 2. YouTube / Shorts ---
@dp.message(F.text.regexp(YOUTUBE_URL_REGEX))
async def handle_youtube(message: types.Message):
    match = YOUTUBE_URL_REGEX.search(message.text)
    if not match:
        return

    yt_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю из YouTube...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as tmp_dir:
            info, file_path = await asyncio.to_thread(download_youtube_video, yt_url, tmp_dir)

            if not file_path or not os.path.exists(file_path):
                await status_msg.edit_text("❌ Не удалось скачать видео.")
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


# --- 3. Instagram Reels / Posts ---
@dp.message(F.text.regexp(INSTAGRAM_URL_REGEX))
async def handle_instagram(message: types.Message):
    match = INSTAGRAM_URL_REGEX.search(message.text)
    if not match:
        return

    ig_url = match.group(0)
    shortcode = match.group(1)
    status_msg = await message.reply("⏳ Загружаю из Instagram...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        media = await fetch_instagram_media(ig_url, shortcode)

        if not media or not media.get("url"):
            await status_msg.edit_text("❌ Не удалось найти видео. Возможно, профиль приватный или публикация удалена.")
            return

        media_url = media["url"]
        caption = "🎬 Instagram Reel"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в Instagram", url=ig_url)]])

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=headers) as session:
            async with session.get(media_url) as resp:
                if resp.status == 200:
                    media_bytes = await resp.read()
                    if media.get("type") == "image":
                        photo_file = BufferedInputFile(media_bytes, filename="instagram.jpg")
                        await message.reply_photo(photo=photo_file, caption=caption, reply_markup=keyboard)
                    else:
                        video_file = BufferedInputFile(media_bytes, filename="instagram.mp4")
                        await message.reply_video(video=video_file, caption=caption, reply_markup=keyboard)
                else:
                    await message.reply_video(video=media_url, caption=caption, reply_markup=keyboard)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка Instagram: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Ошибка при скачивании из Instagram. Попробуйте еще раз.")


# --- 4. Pinterest Pins / Videos ---
@dp.message(F.text.regexp(PINTEREST_URL_REGEX))
async def handle_pinterest(message: types.Message):
    match = PINTEREST_URL_REGEX.search(message.text)
    if not match:
        return

    pin_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю из Pinterest...")

    try:
        media = await fetch_pinterest_media(pin_url)

        if not media or not media.get("url"):
            await status_msg.edit_text("❌ Не удалось извлечь медиа из Pinterest. Проверьте ссылку.")
            return

        media_url = media["url"]
        caption = "📌 Pinterest"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в Pinterest", url=pin_url)]])

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=headers) as session:
            async with session.get(media_url) as resp:
                if resp.status == 200:
                    media_bytes = await resp.read()
                    if media.get("type") == "image":
                        photo_file = BufferedInputFile(media_bytes, filename="pinterest.jpg")
                        await message.reply_photo(photo=photo_file, caption=caption, reply_markup=keyboard)
                    else:
                        video_file = BufferedInputFile(media_bytes, filename="pinterest.mp4")
                        await message.reply_video(video=video_file, caption=caption, reply_markup=keyboard)
                else:
                    await message.reply_video(video=media_url, caption=caption, reply_markup=keyboard)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка Pinterest: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Ошибка при скачивании из Pinterest.")


# ==========================================
# CALLBACK-ОБРАБОТЧИКИ АУДИО
# ==========================================
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
        logger.error(f"Ошибка TikTok аудио: {e}")
        await callback.message.reply("⚠️ Не удалось загрузить аудио.")


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
        logger.error(f"Ошибка YouTube аудио: {e}")
        await callback.message.reply("⚠️ Не удалось загрузить аудиодорожку.")


# ==========================================
# HEALTH CHECK СЕРВЕР (ДЛЯ RENDER)
# ==========================================
async def start_health_check_server():
    """Запускает веб-сервер для Render Web Service с диагностикой."""
    app = web.Application()

    async def handle_ping(request):
        return web.Response(text="Bot is running! Version: v8-ultimate-multi (TikTok, YouTube, Instagram, Pinterest)")

    async def handle_test_yt(request):
        url = request.query.get("url", "https://youtu.be/Uj0Qn32tER0")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                info, path = await asyncio.to_thread(download_youtube_video, url, tmp_dir)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                return web.json_response({
                    "status": "success",
                    "title": info.get("title"),
                    "size_mb": round(size_mb, 2),
                    "file": os.path.basename(path)
                })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_test_ig(request):
        url = request.query.get("url", "https://www.instagram.com/reels/DfI91GEqDeK/")
        m = INSTAGRAM_URL_REGEX.search(url)
        shortcode = m.group(1) if m else "DfI91GEqDeK"
        try:
            res = await fetch_instagram_media(url, shortcode)
            return web.json_response({"status": "success" if res else "not_found", "data": res})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/test-yt", handle_test_yt)
    app.router.add_get("/test-ig", handle_test_ig)

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
