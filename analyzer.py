#!/usr/bin/env python3
"""
analyzer.py — Транскрипция (Groq Whisper) + Универсальный анализ (Claude + Perplexity)

Пайплайн:
  1. extract_audio + transcribe  → текст
  2. extract_structured_notes    → структурированный конспект (JSON)
  3. research_topic              → веб-исследование (Perplexity via OpenRouter)
  4. generate_claude_prompt      → готовый промпт для Claude Code
  5. refine_content              → правки по запросу пользователя
  6. chat_with_claude            → многоходовой чат
  7. analyze_image_in_chat       → анализ фото в диалоге
  8. generate_chat_summary       → конспект диалога для Obsidian
"""

import os
import re
import json
import asyncio
import subprocess
import requests
from datetime import datetime
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
    """Транскрипция через Groq Whisper. Принимает mp3/ogg/wav/m4a.

    Telegram голосовые приходят как OGG/Opus. Groq требует правильный mime-type.
    Явно указываем audio/ogg для .ogg файлов чтобы Whisper не отвергал их.
    """
    ext      = os.path.splitext(audio_path)[1].lower()
    filename = os.path.basename(audio_path)

    # Явный mime-type для Groq Whisper — без него OGG/Opus иногда отвергается
    MIME_TYPES = {
        ".ogg":  "audio/ogg",
        ".opus": "audio/ogg",
        ".mp3":  "audio/mpeg",
        ".mp4":  "audio/mp4",
        ".m4a":  "audio/mp4",
        ".wav":  "audio/wav",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }
    mime = MIME_TYPES.get(ext, "audio/ogg")

    # Пробуем large-v3, при ошибке (лимит/quota) — fallback на turbo (быстрее, чуть дешевле)
    for whisper_model in ("whisper-large-v3", "whisper-large-v3-turbo"):
        try:
            with open(audio_path, "rb") as f:
                result = groq_client.audio.transcriptions.create(
                    file=(filename, f, mime),
                    model=whisper_model,
                    response_format="text",
                    language="ru",  # подсказка — основной язык русский (повышает точность)
                )
            return result.strip() if result else ""
        except Exception as e:
            err = str(e).lower()
            # Rate limit или quota → пробуем следующую модель
            if "rate" in err or "quota" in err or "limit" in err or "429" in err:
                continue
            raise  # другая ошибка — поднимаем
    return ""  # обе модели недоступны


def _call_openrouter(messages: list, model="anthropic/claude-sonnet-4-6", max_tokens=3000) -> str:
    """Вызов OpenRouter с каскадным fallback по моделям.

    Стратегия при ошибке:
      1. Пробуем основную модель (sonnet-4-6)
      2. При 402/429/5xx → fallback на haiku-4-5 (в 10x дешевле)
      3. При повторной ошибке → поднимаем исключение (обрабатывается выше)
    """
    # Каскад моделей: основная → дешёвая резервная
    FALLBACK_MODELS = {
        "anthropic/claude-sonnet-4-6": "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4.5": "anthropic/claude-haiku-4-5",
        "anthropic/claude-opus-4-5":   "anthropic/claude-sonnet-4-6",
    }

    def _do_request(use_model: str) -> str:
        resp = requests.post(OR_URL, headers=OR_HEADERS, json={
            "model": use_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }, timeout=90)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    try:
        return _do_request(model)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        # 402 = нет баланса, 429 = rate limit, 5xx = сервер недоступен
        if status in (402, 429) or status >= 500:
            fallback = FALLBACK_MODELS.get(model)
            if fallback and fallback != model:
                return _do_request(fallback)
        raise


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

    Хранилища (ТОЧНЫЕ существующие папки):
      Бизнес QSNera:  Клиенты | Задачи | Отчёты | Маркетинг | Сайт
      Цифровой мозг:  Brain | Система | Саморазвитие | Работа над собой
      Личная жизнь:   Цели | Дневник
    """
    # Разрешённые папки — только реально существующие в репозиториях, без emoji!
    # qsnera-vault → Бизнес QSNera
    # digital-brain-vault → Цифровой мозг + Личная жизнь (нет отдельного repo)
    ALLOWED_FOLDERS = {
        "Бизнес QSNera": ["Клиенты", "Задачи", "Отчёты", "Маркетинг", "Сайт"],
        "Цифровой мозг": ["Brain", "Система", "Саморазвитие", "Работа над собой"],
        "Личная жизнь":  ["Brain", "Саморазвитие", "Работа над собой"],
    }
    DEFAULT_VAULT  = "Бизнес QSNera"
    DEFAULT_FOLDER = "Задачи"

    prompt = f"""Ты помощник для организации заметок в Obsidian для Родиона Яланского.
Родион — основатель студии Axiom:Void (веб-разработка и цифровые продукты: Void:Form / Axiom:Core / The Nexus / Absolute Zero).

ЗАМЕТКА:
{text}

СУЩЕСТВУЮЩАЯ структура хранилищ (используй ТОЛЬКО эти папки, без эмодзи!):
- "Бизнес QSNera" папки: "Клиенты", "Задачи", "Отчёты", "Маркетинг", "Сайт"
- "Цифровой мозг" папки: "Brain", "Система", "Саморазвитие", "Работа над собой"
- "Личная жизнь" папки: "Brain", "Саморазвитие", "Работа над собой"

Правила классификации:
- Клиенты, проекты, встречи, объекты → Бизнес QSNera / Клиенты
- Задачи для выполнения (код, сайт, автоматизация) → Бизнес QSNera / Задачи, type: task
- Маркетинг, контент, идеи для постов → Бизнес QSNera / Маркетинг
- Сайт, дизайн, UI → Бизнес QSNera / Сайт
- Личные цели, планы, мечты → Личная жизнь / Саморазвитие
- Дневник, личные мысли, эмоции → Личная жизнь / Работа над собой
- Технические знания, инструкции → Цифровой мозг / Система
- Саморазвитие, обучение, книги → Цифровой мозг / Саморазвитие
- Рефлексия, самоанализ → Цифровой мозг / Работа над собой
- Идеи, концепции для мозгового центра → Цифровой мозг / Brain

⚠️ ВАЖНО: folder должен быть ТОЧНО из списка выше. Нельзя придумывать новые папки!

Верни ТОЛЬКО валидный JSON без комментариев:
{{
  "vault": "название хранилища",
  "folder": "точное название папки из списка выше (без эмодзи!)",
  "title": "короткое название заметки (до 50 символов, без эмодзи)",
  "type": "note"
}}

Если текст — это задача (что-то нужно сделать, код, автоматизация), верни type: "task"."""

    # Haiku: классификация — простая задача, JSON из 4 полей (~15x дешевле sonnet).
    # При ошибке API (нет баланса, rate limit) → fallback: сохранить в Задачи/ без AI.
    try:
        response = _call_openrouter(
            [{"role": "user", "content": prompt}],
            model="anthropic/claude-haiku-4-5",
            max_tokens=200,
        )
        text_r = response.strip()
        if text_r.startswith("```"):
            text_r = re.sub(r"^```\w*\n?", "", text_r)
            text_r = re.sub(r"\n?```$", "", text_r.rstrip())
        try:
            result = json.loads(text_r)
        except Exception:
            m = re.search(r'\{.*\}', text_r, re.DOTALL)
            result = None
            if m:
                try:
                    result = json.loads(m.group())
                except Exception:
                    pass
    except Exception:
        # OpenRouter недоступен (нет баланса, rate limit, сеть) →
        # заметка сохраняется в Задачи/ — Родион увидит и разберёт вручную
        result = None

    if result is None:
        result = {"vault": DEFAULT_VAULT, "folder": DEFAULT_FOLDER, "title": text[:50], "type": "note"}

    # ── Жёсткая валидация: folder должен существовать в vault ──
    vault  = result.get("vault", DEFAULT_VAULT)
    folder = result.get("folder", DEFAULT_FOLDER)

    # Очищаем emoji из названий на случай если Claude всё же добавил их
    vault  = re.sub(r'[\U0001F000-\U0001FFFF☀-⟿⌀-⏿]', '', vault).strip()
    folder = re.sub(r'[\U0001F000-\U0001FFFF☀-⟿⌀-⏿]', '', folder).strip()

    if vault not in ALLOWED_FOLDERS:
        vault = DEFAULT_VAULT

    if folder not in ALLOWED_FOLDERS.get(vault, []):
        folder = DEFAULT_FOLDER

    result["vault"]  = vault
    result["folder"] = folder

    # Очищаем emoji из заголовка
    title = result.get("title", text[:50])
    title = re.sub(r'[\U0001F000-\U0001FFFF☀-⟿⌀-⏿]', '', title).strip()
    result["title"] = title[:60] if title else text[:50]

    return result


# ─── Task Preprocessor ───────────────────────────────────────────────────────

def preprocess_task(raw_text: str) -> str:
    """
    Переводит разговорный текст Родиона в чёткую инженерную задачу для Claude Code.
    Haiku: дёшево, достаточно для препроцессинга.
    При ошибке API возвращает оригинал без изменений.
    """
    SYSTEM = """Ты — переводчик с "разговорного языка" на "язык задач для Claude Code".

Контекст системы Axiom:Void:
- Родион — основатель студии Axiom:Void (веб-разработка, Void:Form / Axiom:Core / The Nexus / Absolute Zero)
- Сайт: ~/Desktop/premium-tiling-website (vanilla HTML/CSS/JS, index.html, axiom-void.dev)
- Telegram бот: ~/Desktop/qsnera-reels-bot/ (Python: bot.py, analyzer.py)
- Vault Бизнес: ~/vaults/AxiomVoid/ | Задачи/ | Отчёты/ | Маркетинг/ | Сайт/ | Клиенты/
- Vault Техника: ~/vaults/Цифровой мозг/ | Brain/ | Система/ | Саморазвитие/
- Vault Личное: ~/vaults/Личная жизнь/
- Агенты: ~/.claude/agents/ | Секреты: ~/.claude/.env
- Отчёты: "Отчёт - Название.md" (именно такой формат)

Алгоритм преобразования:
1. Убери мусор: "э", "ну", "короче", "типа", "вот", повторы, паузы, незаконченные мысли
2. Выдели суть: Глагол (Действие) + Объект + конкретный путь к файлу/папке
3. Угадай путь по контексту:
   - упоминает сайт/дизайн/страницу → ~/Desktop/premium-tiling-website
   - упоминает бота/telegram → ~/Desktop/qsnera-reels-bot/
   - упоминает заметки/задачи/отчёты → ~/vaults/AxiomVoid/
   - упоминает агентов/скрипты → ~/.claude/agents/
4. Если задача касается файлов или git — добавь в конце:
   Требования: git add -A, git pull --rebase перед push, папки vault БЕЗ emoji, коммит с префиксом feat:/fix:/refactor:

Верни ТОЛЬКО оптимизированный текст задачи. Без вводных слов. Просто сам текст."""

    try:
        response = _call_openrouter(
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": raw_text},
            ],
            model="anthropic/claude-haiku-4-5",
            max_tokens=500,
        )
        optimized = response.strip()
        return optimized if len(optimized) > 10 else raw_text
    except Exception:
        return raw_text  # fallback: вернуть оригинал


# ─── Claude Chat ──────────────────────────────────────────────────────────────

def chat_with_claude(messages: list, system_prompt: str = None, max_tokens: int = 2000) -> str:
    """
    Многоходовой чат с Claude через OpenRouter.
    messages: [{"role": "user"/"assistant", "content": "..."}]
    system_prompt: системный промпт — добавляется первым сообщением, не хранится в истории
    """
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)
    return _call_openrouter(full_messages, max_tokens=max_tokens)


def analyze_image_in_chat(image_base64: str, question: str, chat_history: list = None) -> tuple:
    """
    Анализирует изображение в рамках диалога с Claude Vision (OpenRouter).
    Возвращает (ответ: str, user_message: dict — для добавления в историю).
    """
    content = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
        },
        {
            "type": "text",
            "text": question or "Что ты видишь на этом изображении? Опиши подробно."
        }
    ]
    user_message = {"role": "user", "content": content}

    full_messages = list(chat_history or []) + [user_message]

    for vision_model in ("anthropic/claude-sonnet-4-6", "anthropic/claude-haiku-4-5"):
        try:
            resp = requests.post(OR_URL, headers=OR_HEADERS, json={
                "model": vision_model,
                "messages": full_messages,
                "max_tokens": 2000,
            }, timeout=90)
            resp.raise_for_status()
            response = resp.json()["choices"][0]["message"]["content"]
            return response, user_message
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (402, 429) or status >= 500:
                continue
            raise
    raise RuntimeError("Все модели Vision недоступны (нет баланса или rate limit)")


def generate_chat_summary(chat_history: list, chat_date: str = None) -> str:
    """
    Генерирует структурированный конспект диалога с Claude.
    Возвращает текст в формате Markdown.
    """
    if not chat_history:
        return "# Пустой диалог\n\n*Сообщений не было.*"

    date_str = chat_date or datetime.now().strftime("%Y-%m-%d %H:%M")

    # Формируем читаемый текст диалога
    convo_parts = []
    for msg in chat_history:
        role = "👤 **Родион**" if msg["role"] == "user" else "🤖 **Claude**"
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            has_image = False
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        has_image = True
            text = " ".join(filter(None, text_parts))
            if has_image:
                text = f"[📷 Фото] {text}" if text else "[📷 Фото]"
            content = text

        if content:
            convo_parts.append(f"{role}: {content}")

    convo_text = "\n\n".join(convo_parts)

    # Обрезаем если слишком длинный (лимит контекста)
    if len(convo_text) > 12000:
        convo_text = convo_text[:12000] + "\n\n_...[диалог обрезан для краткости]..._"

    msg_count = sum(1 for m in chat_history if m["role"] == "user")

    summary_prompt = f"""Создай структурированный конспект следующего диалога Родиона с Claude.

ДИАЛОГ (дата: {date_str}, сообщений от пользователя: {msg_count}):
---
{convo_text}
---

Напиши конспект строго в Markdown. Включи все разделы:

## 🎯 Тема разговора
[1-2 предложения о чём был разговор]

## 💡 Ключевые идеи и решения
[маркированный список важных моментов, идей, выводов]

## ✅ Задачи и следующие шаги
[конкретные действия по итогам — если обсуждались]

## 📚 Полезная информация
[факты, советы, данные из разговора — если есть]

---

## 📝 Полный диалог

{convo_text}

---
*Сохранено: {date_str} · Сообщений: {msg_count}*"""

    return _call_openrouter([{"role": "user", "content": summary_prompt}], max_tokens=4000)
