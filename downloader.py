#!/usr/bin/env python3
"""
downloader.py — Скачивание Reels через yt-dlp
"""

import os
import asyncio
import yt_dlp


async def download_video(url: str, output_dir: str) -> str:
    """Скачивает видео по URL, возвращает путь к файлу"""

    output_template = os.path.join(output_dir, "reel.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }

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
