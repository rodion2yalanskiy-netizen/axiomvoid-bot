#!/usr/bin/env python3
"""
downloader.py — Скачивание Reels через yt-dlp + instaloader (резервный)
"""

import os
import re
import asyncio
import requests
import yt_dlp


def _extract_shortcode(url: str) -> str | None:
    """Извлекает shortcode из Instagram URL"""
    m = re.search(r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _build_cookies_file(tmpdir: str) -> str | None:
    """Создаёт cookies.txt из INSTAGRAM_SESSIONID env var"""
    session_id = os.environ.get("INSTAGRAM_SESSIONID", "").strip()
    if not session_id:
        return None
    cookies_path = os.path.join(tmpdir, "cookies.txt")
    with open(cookies_path, "w") as f:
        f.write(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{session_id}\n"
        )
    return cookies_path


def _download_via_ytdlp(url: str, output_dir: str) -> str | None:
    """Попытка скачать через yt-dlp. Возвращает путь или None при ошибке."""
    output_template = os.path.join(output_dir, "reel.%(ext)s")
    cookies_file = _build_cookies_file(output_dir)

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            )
        },
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        for f in os.listdir(output_dir):
            if f.startswith("reel"):
                return os.path.join(output_dir, f)
    except Exception:
        pass
    return None


def _download_via_instaloader(url: str, output_dir: str) -> str | None:
    """Резервный метод: instaloader → прямая CDN ссылка → requests."""
    shortcode = _extract_shortcode(url)
    if not shortcode:
        return None

    try:
        import instaloader
        L = instaloader.Instaloader()

        # Логин если заданы credentials
        username = os.environ.get("INSTAGRAM_USERNAME", "").strip()
        password = os.environ.get("INSTAGRAM_PASSWORD", "").strip()
        if username and password:
            L.login(username, password)

        post = instaloader.Post.from_shortcode(L.context, shortcode)
        video_url = post.video_url
        if not video_url:
            return None

        # Скачиваем CDN видео напрямую
        out_path = os.path.join(output_dir, "reel.mp4")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            )
        }
        resp = requests.get(video_url, headers=headers, timeout=60, stream=True)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
        return out_path

    except Exception:
        return None


async def download_video(url: str, output_dir: str) -> str:
    """
    Скачивает видео по URL, возвращает путь к файлу.
    Сначала пробует yt-dlp, при неудаче — instaloader.
    """
    loop = asyncio.get_event_loop()

    # Попытка 1: yt-dlp
    result = await loop.run_in_executor(None, _download_via_ytdlp, url, output_dir)
    if result:
        return result

    # Попытка 2: instaloader (обходит блокировку серверных IP)
    result = await loop.run_in_executor(None, _download_via_instaloader, url, output_dir)
    if result:
        return result

    raise RuntimeError(
        "Не удалось скачать Reel. Instagram требует авторизацию с этого сервера.\n"
        "Попробуй: перешли видео из Instagram напрямую в бот (кнопка Поделиться → Telegram)."
    )
