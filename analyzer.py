#!/usr/bin/env python3
"""
analyzer.py — Транскрипция (Groq Whisper) + Универсальный анализ (Claude + Perplexity)

Пайплайн:
  1. extract_audio + transcribe  → текст
  2. extract_structured_notes    → структурированный конспект (JSON)
  3. research_topic              → веб-исследование (Perplexity via OpenRouter)
  4. generate_claude_prompt      → готовый промпт для Claude Code
  5. refine_content              → правки по запросу пользователя
"""

import os
import re
import json
import asyncio
import subprocess
import requests
from groq import Groq

GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

groq_client = Groq(api_key=GROQ_API_KEY)

OR_URL     = "https://openrouter.ai/api/v1/chat/completions"
OR_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}


# ─── Низкоуровневые утилиты ──────────────────────────────────────────────────

def extract_audio(video_path: str) -> str:
    """Извлекает аудио из видео любого формата → mp3"""
    base = os.path.splitext(video_path)[0]
    audio_path = base + ".mp3"
    result = subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vn", "-acodec", "mp3",
        "-ar", "16000", "-ac", "1", "-b:a", "64k",
        audio_path, "-y", "-loglevel", "error"
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg: {result.stderr[:300]}")
    return audio_path


def transcribe(audio_path: str) -> str:
    """Транскрипция через Groq Whisper"""
    with open(audio_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=("audio.mp3", f),
            model="whisper-large-v3",
            response_format="text"
        )
    return result.strip() if result else ""


def _call_openrouter(messages: list, model="anthropic/claude-sonnet-4-5", max_tokens=3000) -> str:
    resp = requests.post(OR_URL, headers=OR_HEADERS, json={
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ─── Шаг 1: Структурированный конспект ──────────────────────────────────────

def extract_structured_notes(transcript: str) -> dict:
    """
    Извлекает ВСЮ полезную информацию из транскрипции — без привязки к теме.
    Возвращает dict с полями topic, summary, steps, tools, technologies,
    commands, services, key_details, search_query.
    """
    if not transcript or len(transcript) < 10:
        transcript = "[Речь отсутствует или неразборчива]"

    prompt = f"""Проанализируй транскрипцию Instagram Reel. Извлеки ВСЮ полезную информацию — \
независимо от темы (технологии, бизнес, лайфстайл, обучение — любая тема).

ТРАНСКРИПЦИЯ:
{transcript}

Верни ТОЛЬКО валидный JSON (без markdown, без пояснений):
{{
  "topic": "краткая тема (до 10 слов)",
  "summary": "суть за 2-3 предложения",
  "steps": ["шаг 1", "шаг 2"],
  "tools": ["инструмент 1"],
  "technologies": ["технология 1"],
  "commands": ["команда/инструкция 1"],
  "services": ["сервис/платформа 1"],
  "key_details": ["важная деталь 1"],
  "search_query": "поисковый запрос для углублённого изучения"
}}

Если поле пустое — оставь []. Не добавляй комментарии вне JSON."""

    response = _call_openrouter([{"role": "user", "content": prompt}])

    # Вычищаем JSON из ответа
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.rstrip())
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    # Fallback
    return {
        "topic": "Анализ Reel",
        "summary": transcript[:300],
        "steps": [], "tools": [], "technologies": [],
        "commands": [], "services": [], "key_details": [],
        "search_query": "информация из видео"
    }


# ─── Шаг 2: Веб-исследование ────────────────────────────────────────────────

def research_topic(notes: dict) -> str:
    """
    Веб-исследование через Perplexity/Sonar (OpenRouter).
    Fallback: Claude с базой знаний.
    """
    topic        = notes.get("topic", "")
    search_query = notes.get("search_query", topic)
    tools        = ", ".join(notes.get("tools", []) + notes.get("technologies", []))
    steps        = "; ".join(notes.get("steps", []))

    prompt = f"""Тема из Reel: {topic}
Инструменты/технологии: {tools or "не указаны"}
Шаги из Reel: {steps or "не указаны"}
Поисковый запрос: {search_query}

Проведи исследование и найди:
1. Как это работает на практике (реальный опыт, форумы, Reddit, GitHub)
2. Подводные камни и частые ошибки, которых нет в Reel
3. Актуальность информации — устарела ли
4. Лучшие альтернативы или улучшения
5. Ссылки на ключевые ресурсы (документация, репозитории, статьи)

Ответь структурированно, кратко, на русском."""

    # Пробуем Perplexity с реальным поиском
    for model in ("perplexity/sonar", "perplexity/sonar-pro"):
        try:
            return _call_openrouter(
                [{"role": "user", "content": prompt}],
                model=model,
                max_tokens=2000
            )
        except Exception:
            continue

    # Fallback: Claude без веб-поиска
    fallback = f"""На основе своих знаний расскажи о теме: "{search_query}"
Инструменты: {tools}

Структурируй:
- Подводные камни и ошибки
- Актуальность и альтернативы
- Практические советы
- Полезные ресурсы

Кратко, на русском."""
    return _call_openrouter([{"role": "user", "content": fallback}])


# ─── Шаг 3: Генерация промпта для Claude Code ────────────────────────────────

def generate_claude_prompt(notes: dict, research: str) -> str:
    """Формирует готовый промпт для выполнения в Claude Code"""

    notes_text = json.dumps(notes, ensure_ascii=False, indent=2)

    prompt = f"""На основе анализа Reel и веб-исследования сформируй готовый промпт для Claude Code.

ДАННЫЕ ИЗ REEL:
{notes_text}

РЕЗУЛЬТАТЫ ВЕБ-ИССЛЕДОВАНИЯ:
{research}

Напиши промпт в формате:

## 🎯 Задача
[чёткое описание — что нужно сделать]

## 📦 Что установить
```bash
[команды установки — если применимо]
```

## 📋 Шаги реализации
[нумерованный список пошаговых действий]

## 🗂 Структура проекта
[файлы и папки — если применимо]

## ⚙️ Конфигурация и настройки
[ключевые параметры, переменные окружения — если применимо]

## ✅ Ожидаемый результат
[что должно получиться на выходе]

---
Если тема не требует кода — адаптируй формат: убери секции про установку/структуру, \
добавь план действий / список ресурсов / чек-лист.

Промпт должен быть самодостаточным — готов к прямому использованию без пояснений."""

    return _call_openrouter([{"role": "user", "content": prompt}], max_tokens=2000)


# ─── Шаг 5 (Edit): Доработка по запросу пользователя ────────────────────────

def refine_content(notes: dict, research: str, current_prompt: str, correction: str) -> tuple[dict, str]:
    """
    Пересматривает конспект и промпт с учётом правки пользователя.
    Возвращает (обновлённые notes, обновлённый промпт).
    """
    notes_json = json.dumps(notes, ensure_ascii=False, indent=2)

    prompt = f"""Пользователь хочет внести правки в анализ Reel.

ТЕКУЩИЙ КОНСПЕКТ (JSON):
{notes_json}

ТЕКУЩИЙ ПРОМПТ:
{current_prompt}

ПРАВКА ПОЛЬЗОВАТЕЛЯ:
{correction}

ВЕБ-ИССЛЕДОВАНИЕ (для контекста):
{research[:500]}...

Обнови конспект и промпт с учётом правки. Верни ТОЛЬКО валидный JSON:
{{
  "notes": {{ ...обновлённый конспект со всеми полями... }},
  "prompt": "...обновлённый промпт..."
}}"""

    response = _call_openrouter([{"role": "user", "content": prompt}], max_tokens=3000)

    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.rstrip())

    try:
        data = json.loads(text)
        return data.get("notes", notes), data.get("prompt", current_prompt)
    except Exception:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                return data.get("notes", notes), data.get("prompt", current_prompt)
            except Exception:
                pass
    return notes, current_prompt


# ─── Форматирование для Telegram ─────────────────────────────────────────────

def format_notes_telegram(notes: dict, research: str) -> str:
    """Форматирует конспект + исследование для отправки в Telegram"""
    parts = []

    parts.append(f"📋 *Конспект Reel*\n\n🎯 *Тема:* {notes.get('topic', '—')}")

    if notes.get("summary"):
        parts.append(f"\n📝 *Суть:*\n{notes['summary']}")

    if notes.get("steps"):
        steps = "\n".join(f"{i+1}\\. {s}" for i, s in enumerate(notes["steps"]))
        parts.append(f"\n📌 *Шаги:*\n{steps}")

    tools = notes.get("tools", []) + notes.get("technologies", [])
    if tools:
        parts.append(f"\n🛠 *Инструменты/технологии:* {', '.join(tools)}")

    if notes.get("commands"):
        cmds = "\n".join(f"• `{c}`" for c in notes["commands"])
        parts.append(f"\n⚙️ *Команды:*\n{cmds}")

    if notes.get("services"):
        parts.append(f"\n🔗 *Сервисы/платформы:* {', '.join(notes['services'])}")

    if notes.get("key_details"):
        details = "\n".join(f"• {d}" for d in notes["key_details"])
        parts.append(f"\n💡 *Важные детали:*\n{details}")

    if research:
        # Обрезаем если слишком длинный
        r = research if len(research) < 1500 else research[:1500] + "..."
        parts.append(f"\n\n🔍 *Проверено на практике:*\n{r}")

    return "\n".join(parts)


# ─── Классификация заметок для Obsidian ──────────────────────────────────────

def classify_note(text: str) -> dict:
    """
    Определяет куда сохранить заметку в Obsidian.
    Возвращает: vault, folder, title, type ("note" | "task")

    Хранилища:
      Бизнес QSNera:  Клиенты | Задачи | Отчёты | Маркетинг | Сайт
      Цифровой мозг:  Brain | Знания
      Личная жизнь:   Цели | Дневник | Автономный доход
    """
    prompt = f"""Ты помощник для организации заметок в Obsidian для Родиона Яланского.
Родион — владелец студии QSNera (укладка премиум-плитки, натуральный камень, мрамор).

ЗАМЕТКА:
{text}

Структура хранилищ:
- "Бизнес QSNera" папки: "Клиенты", "Задачи", "Отчёты", "Маркетинг", "Сайт"
- "Цифровой мозг" папки: "Brain", "Знания"
- "Личная жизнь" папки: "Цели", "Дневник", "Автономный доход"

Правила:
- Клиенты, проекты, встречи → Бизнес QSNera / Клиенты
- Задачи для выполнения (код, сайт, автоматизация) → Бизнес QSNera / Задачи, type: task
- Маркетинг, контент, идеи для постов → Бизнес QSNera / Маркетинг
- Сайт, дизайн → Бизнес QSNera / Сайт
- Цели, планы на будущее → Личная жизнь / Цели
- Дневник, личные мысли → Личная жизнь / Дневник
- Технические знания, изучение → Цифровой мозг / Знания

Верни ТОЛЬКО валидный JSON без комментариев:
{{
  "vault": "название хранилища",
  "folder": "папка (без эмодзи, точное название из структуры выше)",
  "title": "короткое название заметки (до 50 символов)",
  "type": "note"
}}

Если текст — это задача (что-то нужно сделать, код, автоматизация), верни type: "task"."""

    response = _call_openrouter([{"role": "user", "content": prompt}], max_tokens=200)

    text_r = response.strip()
    if text_r.startswith("```"):
        text_r = re.sub(r"^```\w*\n?", "", text_r)
        text_r = re.sub(r"\n?```$", "", text_r.rstrip())
    try:
        return json.loads(text_r)
    except Exception:
        m = re.search(r'\{.*\}', text_r, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass

    # Fallback
    return {
        "vault": "Бизнес QSNera",
        "folder": "Задачи",
        "title": text[:50],
        "type": "note"
    }
