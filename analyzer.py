#!/usr/bin/env python3
"""
analyzer.py — Транскрипция (Groq Whisper) + Анализ (Claude via OpenRouter)
"""

import os
import asyncio
import tempfile
import subprocess
import requests
from groq import Groq

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

groq_client = Groq(api_key=GROQ_API_KEY)


def extract_audio(video_path: str) -> str:
    """Извлекает аудио из видео, возвращает путь к mp3"""
    # Работает с любым форматом видео (.mp4, .webm, etc.)
    base = os.path.splitext(video_path)[0]
    audio_path = base + ".mp3"
    result = subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "mp3",
        "-ar", "16000", "-ac", "1", "-b:a", "64k",
        audio_path, "-y", "-loglevel", "error"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg ошибка: {result.stderr[:300]}")
    return audio_path


def transcribe(audio_path: str) -> str:
    """Транскрибирует аудио через Groq Whisper (бесплатно)"""
    with open(audio_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("audio.mp3", f),
            model="whisper-large-v3",
            response_format="text"
        )
    return result.strip() if result else ""


def analyze(transcription: str) -> str:
    """Анализирует контент через Claude via OpenRouter"""

    if not transcription or len(transcription) < 10:
        transcription = "[Аудио отсутствует или неразборчиво — анализ по визуальному контексту]"

    prompt = f"""Ты — AI-аналитик для студии укладки плитки QSNera (Родион Яланский).

Проанализируй транскрипцию Instagram Reel и создай полезный отчёт.

ТРАНСКРИПЦИЯ:
{transcription}

Создай отчёт строго в этом формате:

📊 *АНАЛИЗ REELS*
━━━━━━━━━━━━━━━━━━

*Тема:* [основная тема одной строкой]

*Ключевые инсайты:*
1. [инсайт]
2. [инсайт]
3. [инсайт]

*Применимость для QSNera:*
✅ [что уже применяем или легко внедрить]
⚠️ [что требует ресурсов/времени]
🆕 [новые идеи для бизнеса]

*Оценка:* [X/10] — [одна строка вывода]

💡 *ПРОМПТ ДЛЯ САМОРАЗВИТИЯ:*
_"[конкретный промпт для углублённого изучения — начни с глагола]"_"""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://t.me/Improvement_for_mi_bot",
            "X-Title": "QSNera Reels Bot",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-sonnet-4-5",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def analyze_reel(video_path: str) -> str:
    """Полный пайплайн: видео → транскрипция → анализ"""
    loop = asyncio.get_event_loop()

    # Извлекаем аудио
    audio_path = await loop.run_in_executor(None, extract_audio, video_path)

    try:
        # Транскрибируем
        transcription = await loop.run_in_executor(None, transcribe, audio_path)

        # Анализируем
        report = await loop.run_in_executor(None, analyze, transcription)
        return report

    finally:
        if os.path.exists(audio_path):
            os.unlink(audio_path)
