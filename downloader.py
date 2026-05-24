#!/usr/bin/env python3
"""
downloader.py — Скачивание Reels через yt-dlp с поддержкой Instagram cookies
"""

import os
import asyncio
import tempfile
import yt_dlp


def _build_cookies_file(tmpdir: str) -> str | None:
    """
    Создаёт файл cookies.txt из переменной окружения INSTAGRAM_SESSIONID.
    Возвращает путь к файлу или None если переменная не задана.
    """
    session_id = os.environ.get("INSTAGRAM_SESSIONID", "").strip()
    if not session_id:
        return None

    cookies_path = os.path.join(tmpdir, "cookies.txt")
    # Netscape cookies format — именно его понимает yt-dlp
    cookies_content = (
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{}\n"
    ).format(session_id)

    with open(cookies_path, "w") as f:
        f.write(cookies_content)

    return cookies_path


async def download_video(url: str, output_dir: str) -> str:
    """Скачивает видео по URL, возвращает путь к файлу"""

    output_template = os.path.join(output_dir, "reel.%(ext)s")
    cookies_file = _build_cookies_file(output_dir)

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        # Заголовок браузера чтобы не выглядеть ботом
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

    loop = asyncio.get_event_loop()

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _download)

    # Находим скачанный файл
    for f in os.listdir(output_dir):
        if f.startswith("reel"):
            return os.path.join(output_dir, f)

    raise FileNotFoundError("Видео не удалось скачать")
