import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, InputMediaPhoto
from dotenv import load_dotenv

# Загрузка переменных окружения из каталога скрипта
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env файле или переменных окружения!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("tiktok_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Регулярное выражение для ссылок TikTok
TIKTOK_URL_REGEX = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+"
)


def to_absolute_url(url: Optional[str]) -> Optional[str]:
    """Преобразует относительный путь TikWM в полный URL."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://www.tikwm.com{url}"


async def fetch_tiktok_data(url: str) -> Optional[dict]:
    """Запрашивает данные видео через публичный API TikWM."""
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


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Привет!**\n\n"
        "Отправь мне ссылку на видео или фото-карусель из **TikTok**, "
        "и я пришлю тебе его **без водяного знака**!\n\n"
        "Поддерживаются ссылки формата:\n"
        "• `https://vm.tiktok.com/...`\n"
        "• `https://vt.tiktok.com/...`\n"
        "• `https://www.tiktok.com/@user/video/...`",
        parse_mode=ParseMode.MARKDOWN,
    )


@dp.message(F.text.regexp(TIKTOK_URL_REGEX))
async def handle_tiktok_link(message: types.Message):
    match = TIKTOK_URL_REGEX.search(message.text)
    if not match:
        return

    tiktok_url = match.group(0)
    status_msg = await message.reply("⏳ Загружаю, пожалуйста, подождите...")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        data = await fetch_tiktok_data(tiktok_url)

        if not data:
            await status_msg.edit_text("❌ Не удалось найти видео. Возможно, аккаунт приватный или видео удалено.")
            return

        title = data.get("title") or ""
        clean_title = (title[:180] + "...") if len(title) > 180 else title
        caption = f"🎬 {clean_title}" if clean_title else "🎬 Видео без водяного знака"

        # 1. Фото-слайдшоу (галерея изображений)
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

        # Скачиваем ролик
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=headers) as session:
            async with session.get(video_url) as resp:
                if resp.status == 200:
                    video_bytes = await resp.read()
                    video_file = BufferedInputFile(video_bytes, filename="tiktok_video.mp4")
                    await message.reply_video(video=video_file, caption=caption)
                else:
                    logger.warning(f"Прямое скачивание вернуло {resp.status}, пробуем отправить URL")
                    await message.reply_video(video=video_url, caption=caption)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки: {e}", exc_info=True)
        await status_msg.edit_text("⚠️ Произошла ошибка при скачивании. Попробуйте еще раз позже.")


async def start_health_check_server():
    """Запускает мини веб-сервер для проверки доступности (Render Web Service)."""
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

    # Если бот запущен на хостинге (Render передает переменную PORT), поднимаем HTTP-порт
    if os.getenv("PORT"):
        await start_health_check_server()

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
