import asyncio
import gzip
import logging
import os
import re
import shutil
import tempfile
import time
import json
import urllib.parse
import urllib.request
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
            d = os.path.dirname(exe)
            std_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            target = os.path.join(d, std_name)
            if not os.path.exists(target):
                try:
                    shutil.copyfile(exe, target)
                except Exception:
                    pass
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return target if os.path.exists(target) else exe
    except Exception as e:
        logger.warning(f"Не удалось получить ffmpeg из imageio: {e}")
    return None


def setup_deno_path() -> Optional[str]:
    """Находит бинарник deno из pip-пакета deno или системы, делает исполняемым и добавляет в PATH."""
    system_deno = shutil.which("deno")
    if system_deno:
        return system_deno
    try:
        import deno
        deno_bin = deno.find_deno_bin()
        if deno_bin and os.path.exists(deno_bin):
            try:
                st = os.stat(deno_bin)
                os.chmod(deno_bin, st.st_mode | 0o755)
            except Exception:
                pass
            d = os.path.dirname(deno_bin)
            std_name = "deno.exe" if os.name == "nt" else "deno"
            target = os.path.join(d, std_name)
            if not os.path.exists(target):
                try:
                    shutil.copyfile(deno_bin, target)
                    os.chmod(target, 0o755)
                except Exception:
                    pass
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return target if os.path.exists(target) else deno_bin
    except Exception as e:
        logger.warning(f"Не удалось получить deno: {e}")
    return None


# Инициализируем PATH для ffmpeg и deno при старте
get_ffmpeg_path()
setup_deno_path()


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
def fetch_tiktok_via_curl_cffi(url: str) -> Optional[dict]:
    """Скачивание TikTok через TikWM с имитацией TLS Chrome 120 (curl_cffi) для 100% обхода Cloudflare WAF."""
    try:
        from curl_cffi import requests as cffi_requests
        encoded_url = urllib.parse.quote(url)
        endpoints = [
            f"https://www.tikwm.com/api/?url={encoded_url}&hd=1",
            f"https://tikwm.com/api/?url={encoded_url}&hd=1",
        ]
        for ep in endpoints:
            try:
                r = cffi_requests.get(ep, impersonate="chrome120", timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("code") == 0 and data.get("data"):
                        logger.info(f"TikWM curl_cffi успешно получил данные для TikTok {url}")
                        return data.get("data")
            except Exception as e:
                logger.warning(f"Ошибка TikWM curl_cffi ({ep}): {e}")
    except Exception as e:
        logger.warning(f"curl_cffi недоступен или ошибка: {e}")
    return None


async def fetch_tiktok_via_ssstik(url: str) -> Optional[dict]:
    """Скачивание TikTok через SSSTik API (обходит Cloudflare и блокировки датацентров Render)."""
    try:
        post_data = {
            "id": url,
            "locale": "en",
            "tt": 0
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "HX-Request": "true",
            "HX-Target": "target",
            "HX-Current-URL": "https://ssstik.io/en",
            "Origin": "https://ssstik.io",
            "Referer": "https://ssstik.io/en",
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers=headers) as session:
            async with session.post("https://ssstik.io/abc?url=dl", data=post_data) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    dl_urls = re.findall(r'href="([^"]+)"[^>]*class="[^"]*pure-button[^"]*"', html)
                    valid_urls = [u for u in dl_urls if u.startswith("http")]

                    photo_urls = re.findall(r'href="([^"]+)"[^>]*class="[^"]*(?:download_link|pure-button)[^"]*"[^>]*>(?:Download|Download photo|Photo)', html, re.IGNORECASE)
                    if not photo_urls:
                        photo_urls = [u for u in re.findall(r'<img[^>]+src="([^"]+)"', html) if "tikcdn" in u or "tiktok" in u]

                    title_m = re.search(r'<h2>([^<]+)</h2>', html) or re.search(r'<p class="maintext">([^<]+)</p>', html)
                    title = title_m.group(1).strip() if title_m else "TikTok video"

                    video_url = None
                    music_url = None
                    for u in valid_urls:
                        if "/m/" in u:
                            music_url = u
                        elif not video_url:
                            video_url = u

                    video_id = ""
                    if video_url:
                        id_m = re.search(r"/ssstik/(\d+)", video_url)
                        if id_m:
                            video_id = id_m.group(1)

                    if not video_id:
                        vid_m = re.search(r"/video/(\d+)", url) or re.search(r"(\d{15,22})", url)
                        if vid_m:
                            video_id = vid_m.group(1)

                    if video_url or photo_urls:
                        res = {
                            "id": video_id,
                            "title": title,
                            "play": video_url,
                            "hdplay": video_url,
                            "music": music_url,
                            "music_info": {
                                "title": "Original Sound",
                                "author": title,
                                "play": music_url,
                            }
                        }
                        if photo_urls and not video_url:
                            res["images"] = photo_urls
                        logger.info(f"SSSTik успешно скачал TikTok {url}: title={title[:30]}")
                        return res
    except Exception as e:
        logger.warning(f"Ошибка SSSTik: {e}")
    return None


async def fetch_tiktok_data(url: str) -> Optional[dict]:
    """Запрашивает данные видео через SSSTik + Aweme Mobile API + curl_cffi TikWM + yt-dlp."""
    # Способ 1: SSSTik (работает напрямую с датацентров Render без блокировок Cloudflare)
    try:
        ssstik_data = await fetch_tiktok_via_ssstik(url)
        if ssstik_data and (ssstik_data.get("play") or ssstik_data.get("images")):
            return ssstik_data
    except Exception as e:
        logger.warning(f"Ошибка SSSTik: {e}")

    # Способ 2: TikWM через curl_cffi (полная имитация TLS Chrome 120)
    try:
        cffi_data = await asyncio.to_thread(fetch_tiktok_via_curl_cffi, url)
        if cffi_data and (cffi_data.get("play") or cffi_data.get("images")):
            return cffi_data
    except Exception as e:
        logger.warning(f"Ошибка при вызове fetch_tiktok_via_curl_cffi: {e}")

    headers_browser = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }

    # Способ 2: Официальный мобильный API TikTok (Aweme Mobile API) с параметрами Android
    try:
        video_id = None
        vid_match = re.search(r"/video/(\d+)", url) or re.search(r"(\d{15,22})", url)
        if vid_match:
            video_id = vid_match.group(1)
        else:
            # Разрешаем редиректы коротких ссылок (vt.tiktok.com, vm.tiktok.com)
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(url, allow_redirects=True) as r:
                    final_url = str(r.url)
                    vid_match = re.search(r"/video/(\d+)", final_url) or re.search(r"(\d{15,22})", final_url)
                    if vid_match:
                        video_id = vid_match.group(1)

        if video_id:
            logger.info(f"Пробуем официальный TikTok Aweme Mobile API для video_id={video_id}...")
            ts = int(time.time())
            aweme_urls = [
                # US/Global
                f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}&version_name=26.1.3&version_code=2613&build_number=26.1.3&manifest_version_code=2613&update_version_code=2613&openudid=a1b2c3d4e5f60718&uuid=1234567890123456&_rticket={ts*1000}&ts={ts}&device_brand=Google&device_type=Pixel%207&device_platform=android&resolution=1080*2400&dpi=420&os_version=13&os_api=33&carrier_region=US&sys_region=US&region=US&app_name=musical_ly&app_language=en&language=en&channel=googleplay&ac=wifi&aid=1233",
                # Asia / CIS / International
                f"https://api22-normal-c-alisg.tiktokv.com/aweme/v1/feed/?aweme_id={video_id}&version_name=26.1.3&version_code=2613&build_number=26.1.3&manifest_version_code=2613&update_version_code=2613&openudid=a1b2c3d4e5f60718&uuid=1234567890123456&_rticket={ts*1000}&ts={ts}&device_brand=Google&device_type=Pixel%207&device_platform=android&resolution=1080*2400&dpi=420&os_version=13&os_api=33&carrier_region=SG&sys_region=SG&region=SG&app_name=musical_ly&app_language=en&language=en&channel=googleplay&ac=wifi&aid=1233",
            ]
            aweme_headers = {
                "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 13; en_US; Pixel 7)",
                "Accept-Encoding": "gzip",
            }
            for a_url in aweme_urls:
                try:
                    async with aiohttp.ClientSession(headers=aweme_headers, timeout=aiohttp.ClientTimeout(total=8)) as s:
                        async with s.get(a_url) as r:
                            if r.status == 200:
                                raw = await r.read()
                                if raw.startswith(b"\x1f\x8b"):
                                    raw = gzip.decompress(raw)
                                if not raw or not raw.strip():
                                    continue
                                feed_json = json.loads(raw.decode("utf-8", errors="ignore"))
                                aweme_list = feed_json.get("aweme_list", [])
                                if aweme_list:
                                    target_item = None
                                    for it in aweme_list:
                                        if str(it.get("aweme_id")) == str(video_id):
                                            target_item = it
                                            break
                                    if not target_item:
                                        continue

                                    desc = target_item.get("desc") or "TikTok video"

                                    # Фото-карусель
                                    images = []
                                    img_post = target_item.get("image_post_info")
                                    if img_post and img_post.get("images"):
                                        for img_obj in img_post.get("images", []):
                                            d_img = img_obj.get("display_image", {})
                                            u_list = d_img.get("url_list", [])
                                            if u_list:
                                                images.append(u_list[0])

                                    # Видео-ссылки (проверяем play_addr, download_addr, bit_rate)
                                    v_urls = []
                                    v_obj = target_item.get("video") or {}
                                    if v_obj.get("play_addr", {}).get("url_list"):
                                        v_urls.extend(v_obj["play_addr"]["url_list"])
                                    if v_obj.get("download_addr", {}).get("url_list"):
                                        v_urls.extend(v_obj["download_addr"]["url_list"])
                                    if v_obj.get("bit_rate"):
                                        for br in v_obj["bit_rate"]:
                                            br_urls = br.get("play_addr", {}).get("url_list", [])
                                            if br_urls:
                                                v_urls.extend(br_urls)

                                    m_obj = target_item.get("music") or {}
                                    m_play_info = m_obj.get("play_url") or {}
                                    m_url = m_play_info.get("uri")
                                    if not m_url and m_play_info.get("url_list"):
                                        m_url = m_play_info["url_list"][0]
                                    if not m_url and v_urls:
                                        m_url = v_urls[0]

                                    result = {
                                        "id": video_id,
                                        "title": desc,
                                        "play": v_urls[0] if v_urls else None,
                                        "hdplay": v_urls[-1] if len(v_urls) > 1 else (v_urls[0] if v_urls else None),
                                        "music": m_url,
                                        "music_info": {
                                            "title": m_obj.get("title") or "Original Sound",
                                            "author": m_obj.get("author") or target_item.get("author", {}).get("nickname") or "",
                                            "play": m_url,
                                        },
                                        "author": {
                                            "nickname": target_item.get("author", {}).get("nickname") or "",
                                            "unique_id": target_item.get("author", {}).get("unique_id") or "",
                                        }
                                    }
                                    if images:
                                        result["images"] = images
                                    if result.get("play") or result.get("images"):
                                        logger.info(f"TikTok Aweme Mobile API успешно извлек данные для video_id={video_id}")
                                        return result
                except Exception as ex:
                    logger.debug(f"Ошибка Aweme {a_url}: {ex}")
    except Exception as e:
        logger.warning(f"Ошибка Aweme Mobile API: {e}")

    # Способ 2: TikWM API через GET
    try:
        encoded_url = urllib.parse.quote(url)
        get_hosts = [
            f"https://www.tikwm.com/api/?url={encoded_url}&hd=1",
            f"https://tikwm.com/api/?url={encoded_url}&hd=1",
        ]
        for get_url in get_hosts:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers=headers_browser) as session:
                    async with session.get(get_url) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            if data.get("code") == 0 and data.get("data"):
                                logger.info(f"TikWM GET успешно получил данные для TikTok {url}")
                                return data.get("data")
            except Exception as e:
                logger.warning(f"Ошибка TikWM GET ({get_url}): {e}")
    except Exception as e:
        logger.warning(f"Ошибка TikWM: {e}")

    # Способ 3: Резерв через yt-dlp
    logger.info("Пробуем резервное получение TikTok через yt-dlp...")
    try:
        def extract_tt():
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.to_thread(extract_tt)
        if info:
            v_url = info.get("url")
            if not v_url and info.get("formats"):
                v_url = info["formats"][-1].get("url")
            if v_url:
                return {
                    "id": info.get("id", ""),
                    "title": info.get("title") or "TikTok video",
                    "play": v_url,
                    "music": v_url,
                    "music_info": {
                        "title": info.get("track") or info.get("title") or "Audio",
                        "author": info.get("artist") or info.get("uploader") or "TikTok"
                    }
                }
    except Exception as e:
        err_str = str(e)
        logger.warning(f"Ошибка резервного yt-dlp для TikTok: {err_str}")
        if "comfortable for some audiences" in err_str or "Log in for access" in err_str:
            return {"error": "age_restricted"}

    return None


# ==========================================
# 2. YOUTUBE (Мобильный Android InnerTube API)
# ==========================================
# 2. YOUTUBE (Мульти-стратегия с Android/iOS InnerTube и cookies)
# ==========================================
def download_youtube_via_external(url: str, output_dir: str, is_audio: bool = False) -> Optional[tuple[dict, str]]:
    """Резервное скачивание через проверенные внешние API (Cobalt/Y2Mate), если все клиенты yt-dlp заблокированы датацентровым баном."""
    logger.info("Пробуем резервное внешнее скачивание YouTube через API...")
    m = YOUTUBE_URL_REGEX.search(url)
    video_id = url.split("v=")[-1].split("&")[0] if "v=" in url else url.split("/")[-1].split("?")[0]

    # 1. Попытка через Cobalt API mirrors
    cobalt_mirrors = [
        "https://taivideo.websites.com.vn/",
        "https://capi.3kh0.net/",
        "https://cobalt-backend.canine.tools/",
        "https://cobalt.api.scidsg.org/api/json",
        "https://api.qwk.app",
        "https://co.wuk.sh/api/json",
    ]
    payload = {
        "url": url,
        "downloadMode": "audio" if is_audio else "auto",
        "videoQuality": "720"
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for inst in cobalt_mirrors:
        try:
            req = urllib.request.Request(
                inst,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.loads(r.read().decode("utf-8"))
                direct_url = res.get("url")
                if direct_url:
                    ext = "m4a" if is_audio else "mp4"
                    out_file = os.path.join(output_dir, f"youtube_ext_{int(time.time())}.{ext}")
                    d_req = urllib.request.Request(direct_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(d_req, timeout=45) as dr, open(out_file, "wb") as f:
                        shutil.copyfileobj(dr, f)
                    if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                        logger.info(f"Успешно скачано через внешний Cobalt API {inst}")
                        info = {"title": res.get("filename") or "YouTube Media", "id": video_id, "uploader": "YouTube"}
                        return info, out_file
        except Exception as e:
            logger.debug(f"Ошибка внешнего API {inst}: {e}")
            continue

    # 2. Попытка через Y2Mate API
    try:
        y2_url = "https://www.y2mate.com/mates/analyzeV2/ajax"
        data = urllib.parse.urlencode({
            "k_query": f"https://www.youtube.com/watch?v={video_id}",
            "k_page": "home",
            "hl": "en",
            "q_auto": "0"
        }).encode("utf-8")
        y2_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.y2mate.com",
            "Referer": "https://www.y2mate.com/en949"
        }
        y2_req = urllib.request.Request(y2_url, data=data, headers=y2_headers)
        with urllib.request.urlopen(y2_req, timeout=10) as r:
            res_json = json.loads(r.read().decode("utf-8"))
            title = res_json.get("title", "YouTube Media")
            links = res_json.get("links", {})
            choice = None
            if is_audio:
                mp3_links = links.get("mp3", {})
                for k, v in mp3_links.items():
                    choice = v
                    break
            else:
                mp4_links = links.get("mp4", {})
                for k, v in mp4_links.items():
                    if v.get("q_text") in ["720p", "480p", "360p"]:
                        choice = v
                        break
                if not choice and mp4_links:
                    choice = list(mp4_links.values())[0]

            if choice and choice.get("k"):
                conv_url = "https://www.y2mate.com/mates/convertV2/index"
                c_data = urllib.parse.urlencode({"k": choice["k"], "vid": video_id}).encode("utf-8")
                c_req = urllib.request.Request(conv_url, data=c_data, headers=y2_headers)
                with urllib.request.urlopen(c_req, timeout=15) as cr:
                    c_res = json.loads(cr.read().decode("utf-8"))
                    dlink = c_res.get("dlink")
                    if dlink:
                        ext = "mp3" if is_audio else "mp4"
                        out_file = os.path.join(output_dir, f"y2_{int(time.time())}.{ext}")
                        dl_req = urllib.request.Request(dlink, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(dl_req, timeout=45) as dr, open(out_file, "wb") as f:
                            shutil.copyfileobj(dr, f)
                        if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                            logger.info("Успешно скачано через Y2Mate API")
                            info = {"title": title, "id": video_id, "uploader": "YouTube"}
                            return info, out_file
    except Exception as e:
        logger.debug(f"Ошибка Y2Mate API: {e}")

    return None


def download_youtube_video(url: str, output_dir: str):
    """
    Скачивание YouTube-видео:
    Попытка 1: web_embedded + remote_components ejs:github (обходит PO Token и датацентровые баны Google)
    Попытка 2: android_vr (InnerTube без жесткого PO Token)
    Попытка 3: mweb (мобильная веб-версия без жесткого PO Token)
    Попытка 4: web_safari (HLS без обязательного PO Token)
    Попытка 5: tv_samsung_pure
    Попытка 6: visionos_pure
    Попытка 7: android_direct
    Попытка 8: tv_samsung с cookies (если есть)
    Попытка 9: Резервное внешнее скачивание (Cobalt / Y2Mate API)
    """
    ffmpeg_exe = get_ffmpeg_path()
    setup_deno_path()
    proxy = os.getenv("YOUTUBE_PROXY")
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")

    # Поддержка куков из переменной окружения Render (YOUTUBE_COOKIES)
    env_cookies = os.getenv("YOUTUBE_COOKIES")
    if env_cookies:
        tmp_cookie = os.path.join(tempfile.gettempdir(), "yt_cookies_env.txt")
        try:
            with open(tmp_cookie, "w", encoding="utf-8") as f:
                f.write(env_cookies)
            cookie_file = tmp_cookie
        except Exception:
            pass

    has_cookies = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 50

    format_selector = (
        "bestvideo[filesize<=38M][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=720][filesize_approx<38M]+bestaudio/"
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=720]+bestaudio/"
        "18/best[ext=mp4]/best"
    )

    strategies = []

    # Если есть cookies (из файла cookies.txt или ENV YOUTUBE_COOKIES) - ставим их ПЕРВЫМИ!
    # Web cookies обходят датацентровые проверки Google на Render
    if has_cookies:
        strategies.extend([
            {
                "name": "default_cookies",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": format_selector,
                    "merge_output_format": "mp4",
                    "remote_components": ["ejs:github"],
                    "noplaylist": True,
                }
            },
            {
                "name": "mweb_cookies",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": format_selector,
                    "merge_output_format": "mp4",
                    "remote_components": ["ejs:github"],
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["mweb"],
                        }
                    },
                    "noplaylist": True,
                }
            },
            {
                "name": "web_embedded_cookies",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": format_selector,
                    "merge_output_format": "mp4",
                    "remote_components": ["ejs:github"],
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["web_embedded"],
                        }
                    },
                    "noplaylist": True,
                }
            },
        ])

    strategies.extend([
        # 1. Web Embedded с удаленными компонентами для решения JS-челленджей (обходит PO Token)
        {
            "name": "web_embedded",
            "opts": {
                "format": format_selector,
                "merge_output_format": "mp4",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_embedded"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 2. Android VR InnerTube клиент
        {
            "name": "android_vr",
            "opts": {
                "format": "bestvideo[filesize<=38M][ext=mp4]+bestaudio[ext=m4a]/18/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android_vr"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 3. MWeb клиент
        {
            "name": "mweb",
            "opts": {
                "format": "bestvideo[filesize<=38M][ext=mp4]+bestaudio[ext=m4a]/18/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["mweb"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 4. Web Safari (HLS потоки без строгой проверки PO Token)
        {
            "name": "web_safari",
            "opts": {
                "format": format_selector,
                "merge_output_format": "mp4",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_safari"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 5. TV Samsung без cookies
        {
            "name": "tv_samsung_pure",
            "opts": {
                "format": format_selector,
                "merge_output_format": "mp4",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["tv_samsung"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 6. VisionOS без cookies
        {
            "name": "visionos_pure",
            "opts": {
                "format": format_selector,
                "merge_output_format": "mp4",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["visionos"],
                    }
                },
                "noplaylist": True,
            }
        },
        # 7. Мобильный Android InnerTube
        {
            "name": "android_direct",
            "opts": {
                "format": "18/best[ext=mp4]/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android"],
                        "player_skip": ["webpage", "configs"],
                    }
                },
                "http_headers": {
                    "User-Agent": "com.google.android.youtube/19.10.37 (Linux; U; Android 14; en_US) gzip",
                },
                "noplaylist": True,
            }
        }
    ])

    strategy_errors = []
    for st in strategies:
        ydl_opts = {
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            **st["opts"]
        }
        if ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = ffmpeg_exe
        if proxy:
            ydl_opts["proxy"] = proxy

        try:
            logger.info(f"Пробуем стратегию скачивания YouTube: {st['name']}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = info.get("id")
                for f in os.listdir(output_dir):
                    if str(video_id) in f:
                        return info, os.path.join(output_dir, f)
        except Exception as e:
            msg = f"[{st['name']}]: {e}"
            logger.warning(f"Стратегия YouTube {msg}")
            strategy_errors.append(msg)

    # Резервный шаг: внешнее API, если все клиенты yt-dlp заблокированы
    ext_res = download_youtube_via_external(url, output_dir, is_audio=False)
    if ext_res:
        return ext_res

    raise Exception(" | ".join(strategy_errors) or "Не удалось скачать видео с YouTube.")


def ensure_mp3_file(input_path: str, ffmpeg_exe: Optional[str] = None) -> str:
    """Гарантирует, что аудиофайл сохранен именно как настоящий MP3 (192 kbps)."""
    if not input_path or not os.path.exists(input_path):
        return input_path
    if input_path.lower().endswith(".mp3"):
        return input_path

    mp3_path = os.path.splitext(input_path)[0] + ".mp3"
    ff = ffmpeg_exe or get_ffmpeg_path() or "ffmpeg"
    try:
        cmd = [
            ff, "-y",
            "-i", input_path,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            mp3_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
            try:
                os.remove(input_path)
            except Exception:
                pass
            return mp3_path
        else:
            logger.warning(f"ffmpeg conversion failed: {res.stderr.decode('utf-8', errors='ignore')}")
    except Exception as e:
        logger.warning(f"Ошибка ручной конвертации в MP3: {e}")
    return input_path


def download_youtube_audio(video_id: str, output_dir: str):
    """Скачивание аудиодорожки с YouTube с гарантированной конвертацией в MP3 192kbps."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ffmpeg_exe = get_ffmpeg_path()
    setup_deno_path()
    cookie_file = os.path.join(os.path.dirname(__file__), "cookies.txt")
    env_cookies = os.getenv("YOUTUBE_COOKIES")
    if env_cookies:
        tmp_cookie = os.path.join(tempfile.gettempdir(), "yt_cookies_env.txt")
        if os.path.exists(tmp_cookie):
            cookie_file = tmp_cookie

    has_cookies = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 50

    mp3_postprocessor = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]

    strategies = []
    if has_cookies:
        strategies.extend([
            {
                "name": "default_cookies_audio",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": "bestaudio/best",
                    "remote_components": ["ejs:github"],
                    "noplaylist": True,
                    "postprocessors": mp3_postprocessor,
                }
            },
            {
                "name": "mweb_cookies_audio",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": "bestaudio/best",
                    "remote_components": ["ejs:github"],
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["mweb"],
                        }
                    },
                    "noplaylist": True,
                    "postprocessors": mp3_postprocessor,
                }
            },
            {
                "name": "web_embedded_cookies_audio",
                "opts": {
                    "cookiefile": cookie_file,
                    "format": "bestaudio/best",
                    "remote_components": ["ejs:github"],
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["web_embedded"],
                        }
                    },
                    "noplaylist": True,
                    "postprocessors": mp3_postprocessor,
                }
            },
        ])

    strategies.extend([
        # 1. Web Embedded аудио
        {
            "name": "web_embedded_audio",
            "opts": {
                "format": "bestaudio/best",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_embedded"],
                    }
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        },
        # 2. Android VR аудио
        {
            "name": "android_vr_audio",
            "opts": {
                "format": "bestaudio/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android_vr"],
                    }
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        },
        # 3. MWeb аудио
        {
            "name": "mweb_audio",
            "opts": {
                "format": "bestaudio/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["mweb"],
                    }
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        },
        # 4. TV Samsung аудио
        {
            "name": "tv_samsung_audio",
            "opts": {
                "format": "bestaudio/best",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["tv_samsung"],
                    }
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        },
        # 5. VisionOS аудио
        {
            "name": "visionos_audio",
            "opts": {
                "format": "bestaudio/best",
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": ["visionos"],
                    }
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        },
        # 6. Android InnerTube
        {
            "name": "android_audio",
            "opts": {
                "format": "bestaudio/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android"],
                        "player_skip": ["webpage", "configs"],
                    }
                },
                "http_headers": {
                    "User-Agent": "com.google.android.youtube/19.10.37 (Linux; U; Android 14; en_US) gzip",
                },
                "noplaylist": True,
                "postprocessors": mp3_postprocessor,
            }
        }
    ])

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
                    if str(video_id) in f and f.lower().endswith(".mp3"):
                        return info, os.path.join(output_dir, f)
                for f in os.listdir(output_dir):
                    if str(video_id) in f:
                        final_path = ensure_mp3_file(os.path.join(output_dir, f), ffmpeg_exe)
                        return info, final_path
        except Exception as e:
            logger.warning(f"Стратегия YouTube аудио '{st['name']}' завершилась ошибкой: {e}")
            last_error = e

    # Резервный шаг: внешнее API для аудио
    ext_res = download_youtube_via_external(url, output_dir, is_audio=True)
    if ext_res:
        ext_info, ext_path = ext_res
        final_mp3 = ensure_mp3_file(ext_path, ffmpeg_exe)
        return ext_info, final_mp3

    raise last_error or Exception("Не удалось скачать аудио с YouTube.")


# ==========================================
# 3. PINTEREST (yt-dlp + ffmpeg для видео и фото)
# ==========================================
def download_pinterest_media(url: str, output_dir: str) -> dict:
    """
    Скачивание Pinterest (видео со звуком или фото максимального качества) через yt-dlp + ffmpeg.
    1. Автоматически разворачивает короткие ссылки pin.it.
    2. Извлекает метаданные пина через PinResource API Pinterest.
    3. Если есть видео — скачивает и склеивает со звуком через ffmpeg.
    4. Если это фото — находит оригинальное фото (без иконок/логотипов интерфейса).
    """
    ffmpeg_exe = get_ffmpeg_path()
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    if ffmpeg_exe:
        ydl_opts["ffmpeg_location"] = ffmpeg_exe

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        video_formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]

        title = info.get("title") or "Pinterest"
        clean_title = (title[:180] + "...") if len(title) > 180 else title

        if video_formats:
            # Есть видео - скачиваем
            ydl.download([url])
            video_id = info.get("id")
            for f in os.listdir(output_dir):
                if str(video_id) in f:
                    return {
                        "type": "video",
                        "file_path": os.path.join(output_dir, f),
                        "title": clean_title
                    }

        # Если фото - находим оригинальное изображение пина
        thumbnails = info.get("thumbnails", [])
        img_url = None
        if thumbnails:
            for t in reversed(thumbnails):
                t_url = t.get("url", "")
                if "originals" in t_url or "736x" in t_url:
                    img_url = t_url
                    break
            if not img_url:
                img_url = thumbnails[-1].get("url")

        if not img_url:
            img_url = info.get("thumbnail") or info.get("url")

        return {
            "type": "image",
            "url": img_url,
            "title": clean_title
        }


# ==========================================
# 4. INSTAGRAM (vxinstagram + yt-dlp + vkr)
# ==========================================
async def fetch_instagram_media(url: str, shortcode: str) -> Optional[dict]:
    """Скачивание Instagram Reels / публикаций через мульти-провайдеры (vxinstagram, yt-dlp, Cobalt, vkr)."""

    # Способ 1: vxinstagram / instagramez (OpenGraph провайдеры - мгновенное скачивание)
    embed_endpoints = [
        f"https://www.vxinstagram.com/reel/{shortcode}/",
        f"https://instagramez.com/reel/{shortcode}/",
        f"https://www.vxinstagram.com/p/{shortcode}/",
    ]
    for ep in embed_endpoints:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4),
                headers={"User-Agent": "TelegramBot"}
            ) as session:
                async with session.get(ep) as r:
                    if r.status == 200:
                        html = await r.text()
                        vm = re.search(
                            r'<meta property=[\x22\x27]og:video(?::secure_url)?[\x22\x27] content=[\x22\x27]([^\x22\x27]+)[\x22\x27]',
                            html
                        )
                        if vm and ".mp4" in vm.group(1):
                            logger.info(f"Instagram найден через {ep}: {vm.group(1)}")
                            return {"type": "video", "url": vm.group(1)}

                        im = re.search(
                            r'<meta property=[\x22\x27]og:image[\x22\x27] content=[\x22\x27]([^\x22\x27]+)[\x22\x27]',
                            html
                        )
                        if im and not im.group(1).endswith(".mp4"):
                            logger.info(f"Instagram фото найдено через {ep}: {im.group(1)}")
                            return {"type": "image", "url": im.group(1)}
        except Exception as e:
            logger.debug(f"Ошибка {ep}: {e}")

    # Способ 2: yt-dlp Instagram экстрактор с быстрым распознаванием 18+ / возрастного ограничения
    last_ig_error = ""
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
        last_ig_error = str(e).lower()
        logger.warning(f"Ошибка yt-dlp instagram: {e}")
        if "available to everyone" in last_ig_error or "certain audiences" in last_ig_error or "login" in last_ig_error or "requires authentication" in last_ig_error:
            logger.info(f"Instagram 18+ detected for {shortcode}")
            return {"error": "age_restricted"}

    # Способ 3: Cobalt mirrors для Instagram
    cobalt_mirrors = [
        "https://capi.3kh0.net/",
        "https://cobalt-backend.canine.tools/",
        "https://taivideo.websites.com.vn/",
    ]
    for cm in cobalt_mirrors:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                }
            ) as session:
                clean_ig_url = f"https://www.instagram.com/reel/{shortcode}/"
                async with session.post(cm, json={"url": clean_ig_url}) as r:
                    if r.status == 200:
                        c_data = await r.json(content_type=None)
                        if c_data.get("status") in ("classic", "stream", "redirect") and c_data.get("url"):
                            logger.info(f"Instagram найден через Cobalt ({cm})")
                            return {"type": "video", "url": c_data["url"]}
                        elif c_data.get("status") == "picker" and c_data.get("picker"):
                            p_item = c_data["picker"][0]
                            if p_item.get("url"):
                                return {"type": p_item.get("type", "video"), "url": p_item["url"]}
        except Exception as e:
            logger.debug(f"Ошибка Cobalt Instagram ({cm}): {e}")

    # Способ 4: vkrdownloader API
    try:
        api_url = f"https://api.vkrdownloader.com/server?vkr={url}"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
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
        logger.debug(f"Ошибка vkr: {e}")

    if "available to everyone" in last_ig_error or "certain audiences" in last_ig_error or "login" in last_ig_error or "requires authentication" in last_ig_error:
        return {"error": "age_restricted"}

    return None


# ==========================================
# ОБРАБОТЧИКИ КОМАНД И МЕНЮ
# ==========================================
@dp.message(F.text == "/id")
async def cmd_id(message: types.Message):
    await message.answer(
        f"🆔 **Ваш Telegram Chat ID:** `{message.chat.id}`\n"
        f"👤 **Пользователь:** {message.from_user.full_name} (@{message.from_user.username})",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать!**\n\n"
        "Я универсальный бот для скачивания медиа!\n\n"
        "📌 **Поддерживаемые платформы:**\n"
        "• 🎬 **TikTok** — видео без водяного знака, аудио (MP3) и фото-карусели\n"
        "• 📸 **Instagram** — Reels и публикации\n"
        "• 📌 **Pinterest** — видео со звуком и фото в высоком разрешении\n\n"
        "Просто отправь мне любую ссылку!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text == "📖 Инструкция")
async def btn_instruction(message: types.Message):
    await message.answer(
        "📖 **Инструкция по использованию:**\n\n"
        "1. Скопируйте ссылку в приложении:\n"
        "   • **TikTok** (кнопка «Поделиться» ➔ «Ссылка»)\n"
        "   • **Instagram Reels** (кнопка «Поделиться» ➔ «Копировать ссылку»)\n"
        "   • **Pinterest** (кнопка «Поделиться» или `pin.it`)\n"
        "2. Отправьте ссылку боту.\n"
        "3. Бот сразу обработает её и пришлет медиафайл!\n\n"
        "💡 Для TikTok под видео доступна кнопка **«🎵 Скачать аудио»** (чистый MP3).",
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
        "🛡 Сервисы: **TikTok • Instagram • Pinterest**\n"
        "📦 Макс. размер: **50 МБ**",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(F.text == "ℹ️ О боте")
async def btn_about(message: types.Message):
    await message.answer(
        "ℹ️ **О боте:**\n\n"
        "🤖 Бот скачивает медиа без водяных знаков, рекламы и ограничений.\n"
        "🚀 Работает на автономном облачном сервере 24/7.\n"
        "✨ Поддерживает TikTok, Instagram, Pinterest, аудиодорожки в MP3 и фото-карусели.",
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

        if isinstance(data, dict) and data.get("error") == "age_restricted":
            await status_msg.edit_text(
                "🔞 **Это видео в TikTok имеет возрастное ограничение (18+)** или скрыто автором.\n\n"
                "TikTok требует подтверждения возраста в приложении для его просмотра.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://ssstik.io/",
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
    await message.reply(
        "⚠️ **Скачивание с YouTube сейчас недоступно** из-за ограничений и блокировок платформы.\n\n"
        "✨ Пожалуйста, отправьте ссылку на **TikTok**, **Instagram** или **Pinterest** — они работают быстро и стабильно!",
        parse_mode=ParseMode.MARKDOWN
    )


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

        if isinstance(media, dict) and media.get("error") == "age_restricted":
            await status_msg.edit_text(
                "🔞 **Это видео в Instagram имеет возрастное ограничение (18+)** или доступно только авторизованным пользователям.\n\n"
                "Instagram блокирует анонимное скачивание такого контента.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        if not media or not media.get("url"):
            await status_msg.edit_text("❌ Не удалось найти видео. Возможно, профиль приватный или публикация удалена.")
            return

        media_url = media["url"]
        caption = "🎬 Instagram Reel"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в Instagram", url=ig_url)]])

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://ssstik.io/",
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
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)

        with tempfile.TemporaryDirectory() as tmp_dir:
            res = await asyncio.to_thread(download_pinterest_media, pin_url, tmp_dir)

            if not res:
                await status_msg.edit_text("❌ Не удалось извлечь медиа из Pinterest. Проверьте ссылку.")
                return

            caption = f"📌 {res.get('title') or 'Pinterest'}"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть в Pinterest", url=pin_url)]])

            if res.get("type") == "video":
                file_path = res["file_path"]
                file_size = os.path.getsize(file_path)
                if file_size > MAX_TELEGRAM_FILE_SIZE:
                    await status_msg.edit_text(
                        f"❌ Видео слишком большое ({file_size // (1024 * 1024)} МБ).\n"
                        "Telegram разрешает ботам отправлять файлы размером не более 50 МБ."
                    )
                    return

                video_file = FSInputFile(file_path, filename="pinterest_video.mp4")
                await message.reply_video(video=video_file, caption=caption, reply_markup=keyboard)
            else:
                img_url = res.get("url")
                if not img_url:
                    await status_msg.edit_text("❌ Не удалось найти изображение в пине.")
                    return

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=headers) as session:
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            photo_bytes = await resp.read()
                            photo_file = BufferedInputFile(photo_bytes, filename="pinterest.jpg")
                            await message.reply_photo(photo=photo_file, caption=caption, reply_markup=keyboard)
                        else:
                            await message.reply_photo(photo=img_url, caption=caption, reply_markup=keyboard)

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
        raw_title = music_info.get("title") or "Audio"
        raw_author = music_info.get("author") or "TikTok"
        clean_title = re.sub(r'[\\/*?:"<>|\r\n]', "", raw_title).strip() or "Audio"
        clean_author = re.sub(r'[\\/*?:"<>|\r\n]', "", raw_author).strip() or "TikTok"

        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = os.path.join(tmp_dir, "raw_audio")
            audio_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://ssstik.io/",
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=audio_headers) as session:
                async with session.get(audio_url) as resp:
                    if resp.status == 200:
                        with open(raw_path, "wb") as f:
                            f.write(await resp.read())

            if os.path.exists(raw_path) and os.path.getsize(raw_path) > 1000:
                mp3_path = ensure_mp3_file(raw_path)
                filename = f"{clean_title[:60]}.mp3"
                audio_file = FSInputFile(mp3_path, filename=filename)
                await callback.message.reply_audio(
                    audio=audio_file,
                    title=clean_title[:60],
                    performer=clean_author[:60]
                )
            else:
                await callback.message.reply("⚠️ Не удалось загрузить аудио.")
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
                raw_title = info.get("title") or "Audio"
                channel = info.get("uploader") or "YouTube"
                clean_title = re.sub(r'[\\/*?:"<>|\r\n]', "", raw_title).strip() or "Audio"
                clean_channel = re.sub(r'[\\/*?:"<>|\r\n]', "", channel).strip() or "YouTube"
                filename = f"{clean_title[:60]}.mp3"

                audio_file = FSInputFile(file_path, filename=filename)
                await callback.message.reply_audio(
                    audio=audio_file,
                    title=clean_title[:60],
                    performer=clean_channel[:60]
                )
            else:
                await callback.message.reply("❌ Не удалось сохранить аудиофайл.")
    except Exception as e:
        logger.error(f"Ошибка YouTube аудио: {e}")
        await callback.message.reply(f"⚠️ Не удалось загрузить аудиодорожку: {e}")


# ==========================================
# HEALTH CHECK СЕРВЕР (ДЛЯ RENDER)
# ==========================================
async def start_health_check_server():
    """Запускает веб-сервер для Render Web Service с диагностикой."""
    app = web.Application()

    async def handle_ping(request):
        cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
        c_exists = os.path.exists(cookie_path)
        c_size = os.path.getsize(cookie_path) if c_exists else 0
        deno_ok = bool(shutil.which("deno"))
        ffmpeg_ok = bool(get_ffmpeg_path())
        return web.Response(text=f"Bot v34-ssstik-prod-live | deno: {deno_ok} | ffmpeg: {ffmpeg_ok} | cookies: {c_exists} ({c_size}b)")

    async def handle_test_tt(request):
        url = request.query.get("url", "https://vt.tiktok.com/ZSpLa6XT8/")
        debug = {"url": url}

        # 0. Resolve redirect
        video_id = None
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=6)) as s:
                async with s.get(url, allow_redirects=True) as r:
                    debug["redirect_url"] = str(r.url)
                    vid = str(r.url).split("/video/")[1].split("?")[0] if "/video/" in str(r.url) else None
                    debug["video_id"] = vid
                    video_id = vid
        except Exception as e:
            debug["redirect_err"] = str(e)

        # 1. Direct Aweme test with full inspect
        debug["aweme_inspect"] = {}
        if video_id:
            ts = int(time.time())
            aweme_url = (
                f"https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/feed/"
                f"?aweme_id={video_id}&version_name=26.1.3&version_code=2613&build_number=26.1.3"
                f"&manifest_version_code=2613&update_version_code=2613&openudid=a1b2c3d4e5f60718"
                f"&uuid=1234567890123456&_rticket={ts*1000}&ts={ts}&device_brand=Google"
                f"&device_type=Pixel%207&device_platform=android&resolution=1080*2400&dpi=420"
                f"&os_version=13&os_api=33&carrier_region=US&sys_region=US&region=US"
                f"&app_name=musical_ly&app_language=en&language=en&channel=googleplay&ac=wifi&aid=1233"
            )
            headers = {
                "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 13; en_US; Pixel 7)",
                "Accept-Encoding": "gzip",
            }
            try:
                async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as s:
                    async with s.get(aweme_url) as r:
                        debug["aweme_inspect"]["status"] = r.status
                        debug["aweme_inspect"]["encoding"] = r.headers.get("Content-Encoding")
                        debug["aweme_inspect"]["content_type"] = r.headers.get("Content-Type")
                        if r.status == 200:
                            raw = await r.read()
                            debug["aweme_inspect"]["raw_len"] = len(raw)
                            debug["aweme_inspect"]["raw_head"] = repr(raw[:30])
                            decomp = raw
                            if raw.startswith(b"\x1f\x8b"):
                                decomp = gzip.decompress(raw)
                            debug["aweme_inspect"]["decomp_head"] = repr(decomp[:50])
                            feed_json = json.loads(decomp.decode("utf-8", errors="ignore"))
                            items = feed_json.get("aweme_list", [])
                            debug["aweme_inspect"]["items_count"] = len(items)
                            debug["aweme_inspect"]["item_ids"] = [str(it.get("aweme_id")) for it in items]
                            # Find matching
                            matched = [it for it in items if str(it.get("aweme_id")) == str(video_id)]
                            debug["aweme_inspect"]["found_target"] = bool(matched)
                            if matched:
                                v_obj = matched[0].get("video") or {}
                                debug["aweme_inspect"]["matched_desc"] = matched[0].get("desc")
                                debug["aweme_inspect"]["matched_play_urls"] = len(v_obj.get("play_addr", {}).get("url_list", []))
            except Exception as e:
                debug["aweme_inspect"]["err"] = repr(e)

        # 1.5 Direct cffi test
        debug["cffi_test"] = {}
        try:
            from curl_cffi import requests as cffi_req
            debug["cffi_test"]["import_ok"] = True
            encoded_url = urllib.parse.quote(url)
            ep = f"https://www.tikwm.com/api/?url={encoded_url}&hd=1"
            r = cffi_req.get(ep, impersonate="chrome120", timeout=10)
            debug["cffi_test"]["status"] = r.status_code
            debug["cffi_test"]["text_preview"] = r.text[:200]
            if r.status_code == 200:
                data = r.json()
                debug["cffi_test"]["code"] = data.get("code")
                debug["cffi_test"]["msg"] = data.get("msg")
        except Exception as e:
            debug["cffi_test"]["err"] = repr(e)

        # 2. Test full fetch_tiktok_data function
        try:
            full_data = await fetch_tiktok_data(url)
            debug["fetch_tiktok_data"] = {
                "success": bool(full_data and (full_data.get("play") or full_data.get("images"))),
                "title": full_data.get("title") if full_data else None,
                "has_play": bool(full_data.get("play")) if full_data else False,
                "has_music": bool(full_data.get("music")) if full_data else False,
                "images_count": len(full_data.get("images", [])) if full_data and full_data.get("images") else 0,
            }
        except Exception as e:
            debug["fetch_tiktok_data"] = {"err": repr(e)}

        return web.json_response(debug)

    async def handle_test_yt_audio(request):
        url = request.query.get("url", "https://youtu.be/rDskeI7Q58E")
        video_id = url.split("v=")[-1].split("&")[0] if "v=" in url else url.split("/")[-1].split("?")[0]
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                info, path = await asyncio.to_thread(download_youtube_audio, video_id, tmp_dir)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                return web.json_response({
                    "status": "success",
                    "title": info.get("title"),
                    "size_mb": round(size_mb, 2),
                    "file": os.path.basename(path)
                })
        except Exception as e:
            return web.json_response({
                "status": "error",
                "message": str(e)
            }, status=500)

    async def handle_test_yt(request):
        url = request.query.get("url", "https://youtu.be/rDskeI7Q58E")
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
            cookie_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
            c_exists = os.path.exists(cookie_path)
            c_size = os.path.getsize(cookie_path) if c_exists else 0
            return web.json_response({
                "status": "error",
                "message": str(e),
                "cookie_file_exists": c_exists,
                "cookie_file_size": c_size,
                "cookie_file_path": cookie_path
            }, status=500)

    async def handle_test_ig(request):
        url = request.query.get("url", "https://www.instagram.com/reels/DfI91GEqDeK/")
        m = INSTAGRAM_URL_REGEX.search(url)
        shortcode = m.group(1) if m else "DfI91GEqDeK"
        debug = {"url": url, "shortcode": shortcode}

        # Test vxinstagram
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "TelegramBot"}) as s:
                async with s.get(f"https://www.vxinstagram.com/reel/{shortcode}/") as r:
                    debug["vxinstagram_status"] = r.status
                    txt = await r.text()
                    vm = re.search(r'<meta property=[\x22\x27]og:video(?::secure_url)?[\x22\x27] content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', txt)
                    debug["vxinstagram_video"] = vm.group(1) if vm else None
        except Exception as e:
            debug["vxinstagram_err"] = str(e)

        # Test yt-dlp
        try:
            def ext_ig():
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
                    return y.extract_info(url, download=False)
            res = await asyncio.to_thread(ext_ig)
            debug["ytdlp_title"] = res.get("title")
            debug["ytdlp_url"] = bool(res.get("url"))
            debug["ytdlp_formats"] = len(res.get("formats", []))
        except Exception as e:
            debug["ytdlp_err"] = str(e)

        # Test full fetch_instagram_media
        try:
            ig_res = await fetch_instagram_media(url, shortcode)
            debug["fetch_instagram_media_res"] = ig_res
            debug["fetch_instagram_media_success"] = bool(ig_res and ig_res.get("url"))
        except Exception as e:
            debug["fetch_instagram_media_err"] = str(e)

        return web.json_response(debug)

    async def handle_test_pin(request):
        url = request.query.get("url", "https://pin.it/5lleYI7Rp")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                res = await asyncio.to_thread(download_pinterest_media, url, tmp_dir)
                if res.get("type") == "video":
                    size_mb = os.path.getsize(res["file_path"]) / (1024 * 1024)
                    return web.json_response({
                        "status": "success",
                        "type": "video",
                        "title": res.get("title"),
                        "size_mb": round(size_mb, 2)
                    })
                return web.json_response({
                    "status": "success",
                    "type": "image",
                    "title": res.get("title"),
                    "url": res.get("url")
                })
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_test_apis(request):
        test_url = request.query.get("url", "https://youtu.be/rDskeI7Q58E")
        video_id = "rDskeI7Q58E"
        results = {}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            # 1. Test Invidious instances
            invidious_mirrors = [
                "https://invidious.nerdvpn.de",
                "https://inv.nadeko.net",
                "https://invidious.jing.rocks",
                "https://vid.priv.au",
                "https://yt.artemislena.eu",
                "https://invidious.privacydev.net",
                "https://iv.melmac.space",
                "https://invidious.drgns.space",
                "https://yewtu.be",
                "https://invidious.flokinet.to",
                "https://iv.ggtyler.dev",
            ]
            for im in invidious_mirrors:
                try:
                    i_url = f"{im}/api/v1/videos/{video_id}"
                    async with session.get(i_url, timeout=aiohttp.ClientTimeout(total=4), ssl=False) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            fs = data.get("formatStreams", [])
                            results[f"invidious_{im}"] = {
                                "status": "ok",
                                "title": data.get("title"),
                                "formatStreams": len(fs),
                                "first_stream": fs[0].get("url")[:80] if fs else None
                            }
                        else:
                            results[f"invidious_{im}"] = {"status": "http_error", "code": r.status}
                except Exception as e:
                    results[f"invidious_{im}"] = {"status": "error", "error": str(e)}

            # 2. Test Piped instances
            piped_mirrors = [
                "https://pipedapi.kavin.rocks",
                "https://api.piped.privacydev.net",
                "https://piped-api.lunar.icu",
                "https://api-piped.mha.fi",
                "https://pipedapi.tokhmi.xyz"
            ]
            for pm in piped_mirrors:
                try:
                    p_url = f"{pm}/streams/{video_id}"
                    async with session.get(p_url, timeout=aiohttp.ClientTimeout(total=4), ssl=False) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            vs = data.get("videoStreams", [])
                            results[f"piped_{pm}"] = {
                                "status": "ok",
                                "title": data.get("title"),
                                "videoStreams": len(vs),
                                "first_stream": vs[0].get("url")[:80] if vs else None
                            }
                        else:
                            results[f"piped_{pm}"] = {"status": "http_error", "code": r.status}
                except Exception as e:
                    results[f"piped_{pm}"] = {"status": "error", "error": str(e)}

            # 3. Test Y2Mate
            try:
                y2_url = "https://www.y2mate.com/mates/analyzeV2/ajax"
                data = {
                    "k_query": f"https://www.youtube.com/watch?v={video_id}",
                    "k_page": "home",
                    "hl": "en",
                    "q_auto": "0"
                }
                async with session.post(
                    y2_url,
                    data=data,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Origin": "https://www.y2mate.com",
                        "Referer": "https://www.y2mate.com/en949"
                    },
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        res_json = await r.json(content_type=None)
                        links = res_json.get("links", {})
                        results["y2mate"] = {
                            "status": "ok",
                            "title": res_json.get("title"),
                            "has_mp4": "mp4" in links
                        }
                    else:
                        results["y2mate"] = {"status": "http_error", "code": r.status}
            except Exception as e:
                results["y2mate"] = {"status": "error", "error": str(e)}

            # 4. Test SaveFrom
            try:
                sf_url = "https://worker.sf-converter.com/savefrom.php"
                async with session.post(
                    sf_url,
                    data={"sf_url": test_url},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    results["savefrom"] = {"status": "ok" if r.status == 200 else "error", "code": r.status}
            except Exception as e:
                results["savefrom"] = {"status": "error", "error": str(e)}

        # 5. Test yt-dlp clients (android_vr, web_embedded, mweb)
        for cl in ["android_vr", "web_embedded", "mweb"]:
            try:
                ydl_opts = {
                    "format": "bestvideo[height<=720]+bestaudio/18/best",
                    "extractor_args": {"youtube": {"player_client": [cl]}},
                    "remote_components": ["ejs:github"] if cl == "web_embedded" else [],
                    "skip_download": True,
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(test_url, download=False)
                    results[f"ytdlp_{cl}"] = {
                        "status": "ok",
                        "title": info.get("title"),
                        "formats": len(info.get("formats", []))
                    }
            except Exception as e:
                results[f"ytdlp_{cl}"] = {"status": "error", "error": str(e)[:120]}

        return web.json_response(results)

    async def handle_send_test(request):
        target_chat = request.query.get("chat_id")
        text = request.query.get("text", "✅ Тестовое сообщение от бота: связь установлена!")
        if not target_chat:
            return web.json_response({"error": "missing chat_id"}, status=400)
        try:
            await bot.send_message(chat_id=int(target_chat), text=text)
            return web.json_response({"status": "sent", "chat_id": target_chat})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/test-yt", handle_test_yt)
    app.router.add_get("/test-yt-audio", handle_test_yt_audio)
    app.router.add_get("/test-tt", handle_test_tt)
    app.router.add_get("/test-ig", handle_test_ig)
    app.router.add_get("/test-pin", handle_test_pin)
    app.router.add_get("/test-apis", handle_test_apis)
    app.router.add_get("/send-test", handle_send_test)

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
