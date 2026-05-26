#!/usr/bin/env python3
"""
QSNera AI Bot — два режима в одном:
  1. 📝 ЗАМЕТКИ: любой текст → Claude классифицирует → предлагает vault/папку → сохраняет в Obsidian
  2. 🎬 REELS:  Instagram ссылка/видео → транскрипция → анализ → промпт → Claude Code

Obsidian структура:
  Бизнес QSNera:  Клиенты/ | Задачи/ | Отчёты/ | Маркетинг/ | Сайт/
  Цифровой мозг:  Brain/ | API Ключи.md
  Личная жизнь:   Цели.md | Дневник/ | Автономный доход/
"""

import os, re, logging, asyncio, base64, tempfile, subprocess
from datetime import datetime

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from analyzer import (
    extract_audio, transcribe,
    extract_structured_notes, research_topic,
    generate_claude_prompt, refine_content,
    format_notes_telegram, classify_note,
    chat_with_claude, analyze_image_in_chat, generate_chat_summary,
)
from downloader import download_video

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
ADMIN_CHAT_ID  = int(os.environ.get("ADMIN_CHAT_ID", "0") or "0")
RAILWAY_TOKEN  = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_SVC_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENV_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

# Репозитории для каждого vault'а
VAULT_REPOS = {
    "Бизнес QSNera": "rodion2yalanskiy-netizen/qsnera-vault",
    "Цифровой мозг": "rodion2yalanskiy-netizen/qsnera-vault",  # пока один repo
    "Личная жизнь":  "rodion2yalanskiy-netizen/qsnera-vault",
}
DEFAULT_REPO = "rodion2yalanskiy-netizen/qsnera-vault"

# ─── Сессии ─────────────────────────────────────────────────────────────────
# state: "reel_confirming" | "reel_editing" | "note_confirming" | "note_editing"
#        "agent_selecting" | "note_editing_folder" | "claude_chat"
user_sessions: dict = {}

# Кеш списка отчётов для inline-кнопок (user_id -> list of file dicts)
report_cache: dict = {}

# ─── Системный промпт для чата ────────────────────────────────────────────────
SYSTEM_PROMPT_CHAT = (
    "Ты — AI-помощник Родиона Яланского, владельца студии QSNera "
    "(укладка премиум-плитки, мрамор, натуральный камень, ручная работа).\n"
    "Помогай с бизнес-вопросами, анализом фото и видео плитки/дизайна, "
    "идеями для контента, техническими задачами.\n"
    "Отвечай по-русски, кратко и конкретно. "
    "Если тебе присылают фото плитки или дизайна интерьера — анализируй профессионально. "
    "Диалог ведётся через Telegram."
)

# ─── Агенты ──────────────────────────────────────────────────────────────────
AGENTS = {
    "code": {
        "btn":   "🤖 Claude Code",
        "title": "Claude Code (Mac)",
        "desc":  "Код, файлы, скрипты, анализ через Claude на твоём Mac",
        "time":  "~2–10 мин",
        "icon":  "🤖",
    },
    "openrouter": {
        "btn":   "🧠 Анализ / Текст",
        "title": "OpenRouter — Claude Sonnet",
        "desc":  "Текст, анализ, написание, структурирование через облако",
        "time":  "~1–3 мин",
        "icon":  "🧠",
    },
    "vision": {
        "btn":   "👁 Анализ фото",
        "title": "Vision — Claude Opus",
        "desc":  "Анализ изображений, скриншотов, фото плитки или дизайна",
        "time":  "~2–5 мин",
        "icon":  "👁",
    },
    "images": {
        "btn":   "🎨 Генерация",
        "title": "Images — FLUX",
        "desc":  "Генерация изображений по описанию (дизайн, визуализации)",
        "time":  "~3–7 мин",
        "icon":  "🎨",
    },
    "antigravity": {
        "btn":   "🌟 Antigravity",
        "title": "Antigravity — Gemini",
        "desc":  "Задача передаётся в Antigravity (Gemini) на Mac",
        "time":  "~2–5 мин",
        "icon":  "🌟",
    },
}


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def reel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить в Claude Code", callback_data=f"reel_confirm:{user_id}"),
        InlineKeyboardButton("✏️ Доработать",             callback_data=f"reel_edit:{user_id}"),
    ]])

def agent_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора агента для выполнения задачи."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(AGENTS["code"]["btn"],        callback_data=f"agt:{user_id}:code"),
            InlineKeyboardButton(AGENTS["openrouter"]["btn"],  callback_data=f"agt:{user_id}:openrouter"),
        ],
        [
            InlineKeyboardButton(AGENTS["vision"]["btn"],      callback_data=f"agt:{user_id}:vision"),
            InlineKeyboardButton(AGENTS["images"]["btn"],      callback_data=f"agt:{user_id}:images"),
        ],
        [
            InlineKeyboardButton(AGENTS["antigravity"]["btn"], callback_data=f"agt:{user_id}:antigravity"),
            InlineKeyboardButton("✏️ Изменить задачу",         callback_data=f"agt:{user_id}:edit"),
        ],
    ])

def note_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Сохранить",       callback_data=f"note_save:{user_id}"),
        InlineKeyboardButton("✏️ Изменить текст",  callback_data=f"note_edit:{user_id}"),
        InlineKeyboardButton("📂 Другая папка",    callback_data=f"note_folder:{user_id}"),
    ]])


# ─── GitHub API: создать файл в vault ───────────────────────────────────────

def github_create_file(repo: str, path: str, content: str, message: str) -> bool:
    """Создаёт или обновляет файл в GitHub репозитории."""
    if not GITHUB_TOKEN:
        return False
    import urllib.parse
    encoded_path = urllib.parse.quote(path, safe="/")
    url = f"https://api.github.com/repos/{repo}/contents/{encoded_path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"}

    # Проверяем существует ли файл (нужен sha для обновления)
    sha = None
    check = requests.get(url, headers=headers, timeout=10)
    if check.status_code == 200:
        sha = check.json().get("sha")

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": "main"
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, headers=headers, json=payload, timeout=30)
    return resp.status_code in (200, 201)


def save_note_to_obsidian(vault: str, folder: str, title: str, content: str) -> bool:
    """Сохраняет заметку в нужный vault через GitHub API."""
    repo      = VAULT_REPOS.get(vault, DEFAULT_REPO)
    safe_title = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', title, flags=re.UNICODE)[:60].strip()
    date_str   = datetime.now().strftime("%Y-%m-%d")
    filename   = f"{safe_title}.md"
    path       = f"{folder}/{filename}" if folder else filename

    note_content = f"""---
title: {title}
date: {date_str}
source: telegram-bot
vault: {vault}
---

{content}
"""
    return github_create_file(repo, path, note_content, f"note: {safe_title}")


def github_get_reports(limit: int = 7) -> list:
    """Получает список последних отчётов из папки Отчёты/ на GitHub."""
    import urllib.parse
    if not GITHUB_TOKEN:
        return []
    folder = "Отчёты"
    url = f"https://api.github.com/repos/{DEFAULT_REPO}/contents/{urllib.parse.quote(folder)}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        files = [f for f in resp.json() if isinstance(f, dict) and f.get("type") == "file" and f["name"].endswith(".md")]
        files.sort(key=lambda x: x["name"], reverse=True)
        return files[:limit]
    except Exception as e:
        logger.warning(f"github_get_reports error: {e}")
        return []


def github_get_file_content(path: str) -> str:
    """Загружает содержимое файла из GitHub (декодирует из base64)."""
    import urllib.parse
    if not GITHUB_TOKEN:
        return ""
    url = f"https://api.github.com/repos/{DEFAULT_REPO}/contents/{urllib.parse.quote(path)}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return ""
        raw_content = resp.json().get("content", "")
        return base64.b64decode(raw_content).decode("utf-8")
    except Exception as e:
        logger.warning(f"github_get_file_content error: {e}")
        return ""


def send_task_to_claude_code(title: str, task_text: str, tool: str = "code") -> bool:
    """Создаёт задачу для указанного агента в папке Задачи/. Устаревший интерфейс — используй create_task."""
    ok, _ = create_task(title, task_text, tool)
    return ok


def create_task(title: str, task_text: str, tool: str = "code") -> tuple:
    """Создаёт задачу в Задачи/ с указанным tool. Возвращает (success: bool, github_path: str)."""
    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', title, flags=re.UNICODE)[:50].strip()
    filename  = f"{safe_name} ({datetime.now().strftime('%H%M')}).md"
    path      = f"Задачи/{filename}"

    content = f"""---
type: task
tool: {tool}
status: delegated
task_name: {safe_name}
date: {datetime.now().strftime('%Y-%m-%d')}
source: telegram-bot
---

# {title}

{task_text}
"""
    ok = github_create_file(DEFAULT_REPO, path, content, f"task({tool}): {safe_name}")
    return ok, path


def verify_task(path: str, expected_tool: str, expected_title: str) -> dict:
    """Перечитывает задачу с GitHub и проверяет соответствие запросу."""
    import urllib.parse, time
    time.sleep(1)  # Небольшая пауза — GitHub иногда кеширует
    url = f"https://api.github.com/repos/{DEFAULT_REPO}/contents/{urllib.parse.quote(path)}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code} — файл не найден на GitHub"}

        raw = base64.b64decode(resp.json().get("content", "")).decode("utf-8")
        actual_tool = ""
        actual_task = ""
        actual_status = ""
        for line in raw.split("\n"):
            if line.startswith("tool:"):
                actual_tool = line.split(":", 1)[1].strip()
            elif line.startswith("task_name:"):
                actual_task = line.split(":", 1)[1].strip()
            elif line.startswith("status:"):
                actual_status = line.split(":", 1)[1].strip()

        return {
            "ok": True,
            "tool_match":   actual_tool == expected_tool,
            "actual_tool":  actual_tool,
            "actual_task":  actual_task,
            "actual_status": actual_status,
            "file_size":    len(raw),
            "path":         path,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Команды ─────────────────────────────────────────────────────────────────

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _save_owner_chat_id(update.effective_user.id)
    await update.message.reply_text(
        "👋 *QSNera AI Bot*\n\n"
        "Я умею:\n\n"
        "📝 *Создавать заметки* — просто напиши что угодно текстом\n"
        "🎤 *Голосовые* — говори голосом, пойму и классифицирую\n"
        "📷 *Анализировать фото* — пришли фото плитки или дизайна\n"
        "🎬 *Анализировать Reels* — пришли ссылку на Instagram\n"
        "⚡ *Задача агенту* — /задача + выбор агента\n"
        "💬 *Диалог с Claude* — /чат для многоходового разговора\n"
        "🤖 *Все агенты* — /агенты\n"
        "📋 *Читать отчёты* — /отчёты\n\n"
        "Примеры:\n"
        "• _«Встреча с клиентом Петровым, хочет мрамор»_ → заметка в Клиенты/\n"
        "• _«Задача: напиши коммерческое предложение»_ → выбор агента → отчёт сюда\n"
        "• [фото плитки] → анализ материала + сохранение\n"
        "• instagram.com/reel/... → анализ + промпт для агента\n\n"
        "Команды: /задача /чат /агенты /отчёты /help /myid",
        parse_mode="Markdown"
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "*Заметки:* напиши текст → бот предложит куда сохранить\n"
        "*Фото:* пришли фото плитки/интерьера → анализ Claude Vision\n"
        "*Задача агенту:* `/задача что сделать` → выбери агента\n"
        "  или напиши текст с _«задача:»_ / _«сделай:»_\n"
        "*Диалог:* `/чат` — Claude помнит весь разговор\n"
        "  пришли фото прямо в чат — анализирует в контексте\n"
        "  /стоп — завершить и сохранить конспект в Obsidian\n"
        "*Все агенты:* `/агенты` — описание + кнопки\n"
        "*Отчёты:* `/отчёты` — последние отчёты прямо здесь\n"
        "*Reels:* пришли ссылку instagram.com/reel/...\n\n"
        "*Хранилища:*\n"
        "🏢 Бизнес QSNera — клиенты, задачи, сайт, маркетинг\n"
        "🧠 Цифровой мозг — техника, знания\n"
        "🏠 Личная жизнь — цели, дневник\n\n"
        "/myid — твой Telegram ID\n"
        "/test — диагностика системы",
        parse_mode="Markdown"
    )

async def handle_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ADMIN_CHAT_ID
    user_id = update.effective_user.id
    ADMIN_CHAT_ID = user_id
    logger.info(f"ADMIN_CHAT_ID={user_id}")
    _save_chat_id_to_railway(user_id)
    await update.message.reply_text(
        f"🆔 Твой Telegram ID: `{user_id}`\n✅ Уведомления о задачах настроены.",
        parse_mode="Markdown"
    )


# ─── Основной обработчик текста ──────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text    = (message.text or "").strip()
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})

    # ── Режим чата с Claude ──
    if session.get("state") == "claude_chat":
        await _handle_claude_chat(message, text, user_id, session)
        return

    # ── Режим правки Reels ──
    if session.get("state") == "reel_editing":
        await _handle_reel_refinement(message, text, user_id, session)
        return

    # ── Режим правки заметки ──
    if session.get("state") == "note_editing":
        await _handle_note_text_edit(message, text, user_id, session)
        return

    # ── Режим смены папки ──
    if session.get("state") == "note_editing_folder":
        await _handle_note_folder_edit(message, text, user_id, session)
        return

    # ── Instagram ссылка → режим Reels ──
    if any(x in text for x in ["instagram.com/reel", "instagram.com/p/", "instagr.am"]):
        await _process_reel(message, text.split()[0], user_id)
        return

    # ── Любой другой текст → режим заметок ──
    await _process_note(message, text, user_id)


async def _process_note(message, text: str, user_id: int):
    """Классифицирует текст и предлагает сохранить как заметку."""
    if len(text) < 3:
        return

    progress = await message.reply_text("🤔 Разбираюсь куда сохранить...")
    try:
        loop = asyncio.get_event_loop()
        classification = await loop.run_in_executor(None, classify_note, text)

        vault    = classification.get("vault", "Бизнес QSNera")
        folder   = classification.get("folder", "Задачи")
        title    = classification.get("title", text[:50])
        note_type = classification.get("type", "note")  # "note" или "task"

        user_sessions[user_id] = {
            "state":   "note_confirming",
            "text":    text,
            "vault":   vault,
            "folder":  folder,
            "title":   title,
            "type":    note_type,
        }

        vault_emoji = {"Бизнес QSNera": "🏢", "Цифровой мозг": "🧠", "Личная жизнь": "🏠"}.get(vault, "📁")
        preview = text[:200] + ("..." if len(text) > 200 else "")

        if note_type == "task":
            # Задача → показываем выбор агента
            user_sessions[user_id]["state"] = "agent_selecting"
            await progress.edit_text(
                f"⚡ *Задача распознана*\n\n"
                f"📄 *{title}*\n"
                f"_{preview}_\n\n"
                f"*Выбери агента-исполнителя:*",
                parse_mode="Markdown",
                reply_markup=agent_keyboard(user_id)
            )
        else:
            # Заметка → обычный флоу
            await progress.edit_text(
                f"📝 *Заметка*\n\n"
                f"📁 *{vault_emoji} {vault}* / `{folder}/`\n"
                f"📄 *{title}*\n\n"
                f"_{preview}_",
                parse_mode="Markdown",
                reply_markup=note_keyboard(user_id)
            )
    except Exception as e:
        logger.error(f"classify error: {e}")
        await progress.edit_text(f"❌ Ошибка классификации: {e}")


async def _handle_note_folder_edit(message, text: str, user_id: int, session: dict):
    """Пользователь вводит новую папку в формате 'Vault / folder'."""
    try:
        if "/" in text:
            parts = [p.strip() for p in text.split("/", 1)]
            vault  = parts[0] if parts[0] else session["vault"]
            folder = parts[1] if len(parts) > 1 else session["folder"]
        else:
            vault  = session["vault"]
            folder = text.strip()
        user_sessions[user_id].update({"vault": vault, "folder": folder, "state": "note_confirming"})
        vault_emoji = {"Бизнес QSNera": "🏢", "Цифровой мозг": "🧠", "Личная жизнь": "🏠"}.get(vault, "📁")
        await message.reply_text(
            f"📂 Папка обновлена\n\n"
            f"📁 *{vault_emoji} {vault}* / `{folder}/`\n"
            f"📄 *{session['title']}*",
            parse_mode="Markdown",
            reply_markup=note_keyboard(user_id)
        )
    except Exception as e:
        await message.reply_text(f"❌ Ошибка: {e}")


async def _handle_note_text_edit(message, text: str, user_id: int, session: dict):
    """Пользователь правит текст или папку заметки."""
    user_sessions[user_id].update({"text": text, "state": "note_confirming"})
    vault   = session["vault"]
    folder  = session["folder"]
    title   = session["title"]
    vault_emoji = {"Бизнес QSNera": "🏢", "Цифровой мозг": "🧠", "Личная жизнь": "🏠"}.get(vault, "📁")
    await message.reply_text(
        f"✏️ Текст обновлён\n\n"
        f"📁 *{vault_emoji} {vault}* / `{folder}/`\n"
        f"📄 *{title}*",
        parse_mode="Markdown",
        reply_markup=note_keyboard(user_id)
    )


# ─── Reels pipeline ──────────────────────────────────────────────────────────

async def _process_reel(message, url: str, user_id: int):
    progress = await message.reply_text("⬇️ Скачиваю Reel...")
    try:
        loop = asyncio.get_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path    = await download_video(url, tmpdir)
            await progress.edit_text("🎤 Транскрибирую аудио...")
            audio_path    = await loop.run_in_executor(None, extract_audio, video_path)
            transcript    = await loop.run_in_executor(None, transcribe, audio_path)
            await progress.edit_text("🔍 Анализирую контент...")
            notes         = await loop.run_in_executor(None, extract_structured_notes, transcript)
            await progress.edit_text("🌐 Исследую тему...")
            research      = await loop.run_in_executor(None, research_topic, notes)
            await progress.edit_text("📝 Формирую промпт...")
            claude_prompt = await loop.run_in_executor(None, generate_claude_prompt, notes, research)

        user_sessions[user_id] = {
            "state": "reel_confirming", "notes": notes,
            "research": research, "prompt": claude_prompt,
        }
        summary = format_notes_telegram(notes, research)
        try:
            await progress.edit_text(summary, parse_mode="Markdown")
        except Exception:
            await progress.edit_text(summary)

        preview = claude_prompt[:1500] + ("..." if len(claude_prompt) > 1500 else "")
        try:
            await message.reply_text(f"📄 *Промпт:*\n\n{preview}", parse_mode="Markdown", reply_markup=reel_keyboard(user_id))
        except Exception:
            await message.reply_text(f"📄 Промпт:\n\n{preview}", reply_markup=reel_keyboard(user_id))
    except Exception as e:
        logger.error(f"reel error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка: {e}")


async def _handle_reel_refinement(message, text: str, user_id: int, session: dict):
    progress = await message.reply_text("⏳ Вношу правки...")
    try:
        loop = asyncio.get_event_loop()
        new_notes, new_prompt = await loop.run_in_executor(
            None, refine_content, session["notes"], session["research"], session["prompt"], text
        )
        user_sessions[user_id].update({"notes": new_notes, "prompt": new_prompt, "state": "reel_confirming"})
        summary = format_notes_telegram(new_notes, session["research"])
        preview = new_prompt[:1500] + ("..." if len(new_prompt) > 1500 else "")
        await progress.edit_text(summary, parse_mode="Markdown")
        await message.reply_text(f"📄 *Промпт:*\n\n{preview}", parse_mode="Markdown", reply_markup=reel_keyboard(user_id))
    except Exception as e:
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовое сообщение → ffmpeg конвертация → Groq Whisper → обработка как текст."""
    message = update.message
    user_id = update.effective_user.id
    voice   = message.voice or message.audio

    progress = await message.reply_text("🎤 Слушаю...")
    try:
        file = await context.bot.get_file(voice.file_id)
        loop = asyncio.get_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Скачиваем как .ogg (Telegram Voice → Opus codec)
            ogg_path = f"{tmpdir}/voice.ogg"
            await file.download_to_drive(ogg_path)

            await progress.edit_text("🔄 Конвертирую...")
            # ogg (Opus) → mp3 через ffmpeg — Groq лучше понимает mp3
            mp3_path = await loop.run_in_executor(None, extract_audio, ogg_path)

            await progress.edit_text("🔍 Транскрибирую...")
            transcript = await loop.run_in_executor(None, transcribe, mp3_path)

        if not transcript or len(transcript.strip()) < 2:
            await progress.edit_text("❌ Не удалось распознать речь. Говори чётче или попробуй ещё раз.")
            return

        # Показываем что распознали + обрабатываем
        await progress.edit_text(f"📝 *Распознано:*\n_{transcript}_", parse_mode="Markdown")

        session = user_sessions.get(user_id, {})
        if session.get("state") == "claude_chat":
            await _handle_claude_chat(message, transcript, user_id, session)
        else:
            await _process_note(message, transcript, user_id)

    except Exception as e:
        logger.error(f"voice error: {e}", exc_info=True)
        err_text = str(e)
        if "ffmpeg" in err_text.lower():
            await progress.edit_text("❌ ffmpeg не найден на сервере. Обратись к разработчику.")
        elif "groq" in err_text.lower() or "api" in err_text.lower():
            await progress.edit_text("❌ Ошибка распознавания (Groq API). Попробуй позже.")
        else:
            await progress.edit_text(f"❌ Ошибка: {err_text[:200]}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото напрямую → анализ через Claude Vision."""
    message = update.message
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    caption = (message.caption or "").strip()

    progress = await message.reply_text("🔍 Анализирую фото...")
    try:
        # Скачиваем фото (берём наибольшее)
        photo   = message.photo[-1]
        file    = await context.bot.get_file(photo.file_id)
        f_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(bytes(f_bytes)).decode()

        question = caption if caption else "Что на этом фото? Опиши профессионально — особенно если это плитка, мрамор, камень или интерьерный дизайн."

        loop = asyncio.get_event_loop()

        # В режиме чата — добавляем в историю
        if session.get("state") == "claude_chat":
            history = session.get("history", [])
            answer, user_msg = await loop.run_in_executor(
                None, analyze_image_in_chat, img_b64, question, history
            )
            history.append(user_msg)
            history.append({"role": "assistant", "content": answer})
            user_sessions[user_id]["history"] = history
            await progress.edit_text(answer[:4000])
        else:
            # Вне чата — разовый анализ
            answer, _ = await loop.run_in_executor(
                None, analyze_image_in_chat, img_b64, question, []
            )
            # Предлагаем сохранить как заметку или задачу
            user_sessions[user_id] = {
                "state":  "note_confirming",
                "text":   f"[Анализ фото]\n{answer}",
                "vault":  "Бизнес QSNera",
                "folder": "Клиенты",
                "title":  f"Фото анализ {datetime.now().strftime('%d.%m %H:%M')}",
                "type":   "note",
            }
            await progress.edit_text(
                f"📷 *Анализ фото:*\n\n{answer[:3500]}",
                parse_mode="Markdown",
                reply_markup=note_keyboard(user_id)
            )
    except Exception as e:
        logger.error(f"photo error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка анализа фото: {e}")


async def _handle_claude_chat(message, text: str, user_id: int, session: dict):
    """Многоходовой диалог с Claude."""
    history = session.get("history", [])

    # Добавляем команду выхода
    if text.lower() in ("/стоп", "/stop", "/выход", "стоп", "выход"):
        # Сохраняем конспект
        if history:
            await message.reply_text("📝 Сохраняю конспект диалога...")
            loop = asyncio.get_event_loop()
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            summary = await loop.run_in_executor(None, generate_chat_summary, history, date_str)
            title   = f"Диалог {datetime.now().strftime('%d.%m %H:%M')}"
            user_sessions[user_id] = {
                "state":  "note_confirming",
                "text":   summary,
                "vault":  "Цифровой мозг",
                "folder": "Brain",
                "title":  title,
                "type":   "note",
            }
            await message.reply_text(
                f"💾 *Сохранить конспект диалога?*\n📁 Цифровой мозг / Brain / `{title}.md`",
                parse_mode="Markdown",
                reply_markup=note_keyboard(user_id)
            )
        else:
            user_sessions.pop(user_id, None)
            await message.reply_text("👋 Диалог завершён.")
        return

    progress = await message.reply_text("💭 Думаю...")
    try:
        history.append({"role": "user", "content": text})
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(
            None, chat_with_claude, history, SYSTEM_PROMPT_CHAT, 2000
        )
        history.append({"role": "assistant", "content": answer})
        # Ограничиваем историю (последние 20 сообщений)
        if len(history) > 20:
            history = history[-20:]
        user_sessions[user_id]["history"] = history

        msg_count = len([m for m in history if m["role"] == "user"])
        await progress.edit_text(
            f"{answer[:3900]}\n\n_Сообщение {msg_count} · /стоп чтобы завершить_",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"chat error: {e}")
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/чат — начать диалог с Claude."""
    user_id = update.effective_user.id
    text    = " ".join(context.args).strip() if context.args else ""

    user_sessions[user_id] = {
        "state":   "claude_chat",
        "history": [],
    }

    if text:
        # Если сразу дал вопрос — обрабатываем
        await handle_text(update, context)
    else:
        await update.message.reply_text(
            "🤖 *Диалог с Claude*\n\n"
            "Задавай вопросы — я помню контекст всего разговора.\n"
            "Можешь присылать фото плитки для анализа.\n\n"
            "_/стоп_ — завершить и сохранить конспект в Obsidian",
            parse_mode="Markdown"
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видео напрямую → анализ как Reel."""
    message = update.message
    video   = message.video or message.document
    if not video:
        return
    user_id  = update.effective_user.id
    progress = await message.reply_text("⏳ Получаю видео...")
    try:
        loop = asyncio.get_event_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = f"{tmpdir}/reel.mp4"
            file = await context.bot.get_file(video.file_id)
            await file.download_to_drive(video_path)
            await progress.edit_text("🎤 Транскрибирую...")
            audio_path    = await loop.run_in_executor(None, extract_audio, video_path)
            transcript    = await loop.run_in_executor(None, transcribe, audio_path)
            await progress.edit_text("🔍 Анализирую...")
            notes         = await loop.run_in_executor(None, extract_structured_notes, transcript)
            await progress.edit_text("🌐 Исследую...")
            research      = await loop.run_in_executor(None, research_topic, notes)
            await progress.edit_text("📝 Формирую промпт...")
            claude_prompt = await loop.run_in_executor(None, generate_claude_prompt, notes, research)

        user_sessions[user_id] = {
            "state": "reel_confirming", "notes": notes,
            "research": research, "prompt": claude_prompt,
        }
        summary = format_notes_telegram(notes, research)
        try:
            await progress.edit_text(summary, parse_mode="Markdown")
        except Exception:
            await progress.edit_text(summary)
        preview = claude_prompt[:1500] + ("..." if len(claude_prompt) > 1500 else "")
        try:
            await message.reply_text(f"📄 *Промпт:*\n\n{preview}", parse_mode="Markdown", reply_markup=reel_keyboard(user_id))
        except Exception:
            await message.reply_text(f"📄 Промпт:\n\n{preview}", reply_markup=reel_keyboard(user_id))
    except Exception as e:
        logger.error(f"video error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка: {e}")


# ─── Callback handler ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data or ""

    # ── Выбор агента ──────────────────────────────────────────────────────────
    if data.startswith("agt:"):
        # Формат: agt:{user_id}:{tool}
        parts_agt = data.split(":", 2)
        uid  = int(parts_agt[1]) if len(parts_agt) > 1 else update.effective_user.id
        tool = parts_agt[2] if len(parts_agt) > 2 else "code"
        session = user_sessions.get(uid, {})

        if not session or session.get("state") not in ("agent_selecting", "note_confirming"):
            await query.message.reply_text("⚠️ Сессия истекла. Напиши задачу снова.")
            return

        # Пользователь нажал «Изменить задачу»
        if tool == "edit":
            user_sessions[uid]["state"] = "note_editing"
            await query.edit_message_reply_markup(None)
            await query.message.reply_text("✏️ Напиши исправленный текст задачи:")
            return

        agent_info = AGENTS.get(tool, AGENTS["code"])
        title      = session.get("title", "Задача")
        task_text  = session.get("text",  "")

        # Убираем кнопки, показываем прогресс
        await query.edit_message_reply_markup(None)
        msg = await query.message.reply_text(
            f"{agent_info['icon']} Отправляю задачу в *{agent_info['title']}*...",
            parse_mode="Markdown"
        )

        # Создаём задачу
        loop = asyncio.get_event_loop()
        ok, gh_path = await loop.run_in_executor(None, create_task, title, task_text, tool)

        if not ok:
            await msg.edit_text("❌ Ошибка создания задачи. Проверь GitHub Token.")
            return

        # Верификация — перечитываем файл с GitHub
        await msg.edit_text(f"{agent_info['icon']} Задача создана. Проверяю соответствие...")
        v = await loop.run_in_executor(None, verify_task, gh_path, tool, title)

        if v.get("ok"):
            tool_icon   = "✅" if v["tool_match"] else "⚠️"
            status_icon = "✅" if v["actual_status"] == "delegated" else "⚠️"
            verify_lines = [
                f"\n*Проверка соответствия:*",
                f"{tool_icon} Агент: `{v['actual_tool']}` {'✓ совпадает' if v['tool_match'] else '≠ ожидалось ' + tool}",
                f"📄 Задача: `{v['actual_task']}`",
                f"{status_icon} Статус: `{v['actual_status']}`",
                f"📦 Размер файла: {v['file_size']} байт",
            ]
            verify_text = "\n".join(verify_lines)

            await msg.edit_text(
                f"✅ *Задача отправлена!*\n\n"
                f"📌 *{title}*\n"
                f"{agent_info['icon']} Агент: *{agent_info['title']}*\n"
                f"⏱ Ожидаемое время: {agent_info['time']}\n"
                f"📱 Отчёт придёт сюда автоматически\n"
                f"{verify_text}",
                parse_mode="Markdown"
            )
        else:
            await msg.edit_text(
                f"✅ *Задача отправлена* (верификация недоступна)\n\n"
                f"📌 *{title}*\n"
                f"{agent_info['icon']} *{agent_info['title']}*\n"
                f"⚠️ Проверка: {v.get('error', 'нет данных')}\n"
                f"⏱ {agent_info['time']}",
                parse_mode="Markdown"
            )

        user_sessions.pop(uid, None)
        return

    # ── Просмотр отчёта (не требует активной сессии) ──
    if data.startswith("rpt_view:"):
        idx = int(data.split(":")[1])
        uid = update.effective_user.id
        reports = report_cache.get(uid, [])
        if not reports or idx >= len(reports):
            await query.message.reply_text("⚠️ Список устарел. Используй /отчёты снова.")
            return
        report = reports[idx]
        await query.message.reply_text("📖 Загружаю отчёт...")
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, github_get_file_content, report["path"])
        if not raw:
            await query.message.reply_text("❌ Не удалось загрузить отчёт.")
            return
        # Убираем frontmatter
        body = raw
        if raw.startswith("---"):
            fm_parts = raw.split("---", 2)
            if len(fm_parts) >= 3:
                body = fm_parts[2].strip()
        name = report["name"].replace("Отчёт - ", "").replace("Отчёт: ", "").replace(".md", "")
        out = f"📋 *{name}*\n\n{body}"
        if len(out) > 4000:
            out = out[:3950] + "\n\n_...обрезано — смотри полный отчёт в Obsidian_"
        try:
            await query.message.reply_text(out, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(out)
        return

    parts   = data.split(":", 1)
    action  = parts[0]
    user_id = int(parts[1]) if len(parts) > 1 else update.effective_user.id
    session = user_sessions.get(user_id)

    if not session:
        await query.message.reply_text("⚠️ Сессия истекла. Напиши снова.")
        return

    # ── Reels callbacks ──
    if action == "reel_confirm":
        await query.edit_message_reply_markup(None)
        topic  = session["notes"].get("topic", "Reel задача")
        prompt = session["prompt"]
        if GITHUB_TOKEN:
            loop    = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, send_task_to_claude_code, topic, prompt)
            if success:
                await query.message.reply_text(
                    f"✅ *Задача отправлена в Claude Code!*\n📌 *{topic}*\n\nПоявится в Obsidian через 5–10 мин.",
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text("⚠️ Ошибка отправки. Промпт для ручного использования:")
                await query.message.reply_text(prompt)
        else:
            await query.message.reply_text("📋 *Промпт для Claude Code:*", parse_mode="Markdown")
            await query.message.reply_text(prompt)
        user_sessions.pop(user_id, None)

    elif action == "reel_edit":
        await query.edit_message_reply_markup(None)
        user_sessions[user_id]["state"] = "reel_editing"
        await query.message.reply_text("✏️ Что изменить? Напиши своими словами.")

    # ── Note callbacks ──
    elif action == "note_save":
        await query.edit_message_reply_markup(None)
        vault  = session["vault"]
        folder = session["folder"]
        title  = session["title"]
        text   = session["text"]
        note_type = session.get("type", "note")

        loop = asyncio.get_event_loop()
        if note_type == "task":
            success = await loop.run_in_executor(None, send_task_to_claude_code, title, text)
            if success:
                await query.message.reply_text(
                    f"⚡ *Задача создана для Claude Code!*\n📌 *{title}*\n\nПоявится в Obsidian через 5–10 мин.",
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text("❌ Ошибка создания задачи.")
        else:
            success = await loop.run_in_executor(None, save_note_to_obsidian, vault, folder, title, text)
            vault_emoji = {"Бизнес QSNera": "🏢", "Цифровой мозг": "🧠", "Личная жизнь": "🏠"}.get(vault, "📁")
            if success:
                await query.message.reply_text(
                    f"✅ *Заметка сохранена!*\n\n{vault_emoji} {vault}\n📂 `{folder}/{title}.md`\n\nПоявится в Obsidian через ~5 мин.",
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text("❌ Ошибка сохранения. Нет GITHUB_TOKEN?")
        user_sessions.pop(user_id, None)

    elif action == "note_edit":
        await query.edit_message_reply_markup(None)
        user_sessions[user_id]["state"] = "note_editing"
        await query.message.reply_text("✏️ Напиши исправленный текст заметки:")

    elif action == "note_folder":
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            "📂 *Куда сохранить?* Напиши в формате:\n\n"
            "`Бизнес QSNera / Клиенты`\n"
            "`Личная жизнь / Дневник`\n"
            "`Цифровой мозг / Brain`",
            parse_mode="Markdown"
        )
        user_sessions[user_id]["state"] = "note_editing_folder"


# ─── Тест ────────────────────────────────────────────────────────────────────

async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg     = await update.message.reply_text("🔧 Диагностика...")
    results = []

    # GitHub
    if GITHUB_TOKEN:
        r = requests.get(f"https://api.github.com/repos/{DEFAULT_REPO}", headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=10)
        results.append("✅ GitHub API" if r.status_code == 200 else f"❌ GitHub: {r.status_code}")
    else:
        results.append("❌ GITHUB_TOKEN не задан")

    results.append("✅ ADMIN_CHAT_ID настроен" if ADMIN_CHAT_ID else "⚠️ ADMIN_CHAT_ID не задан (нет уведомлений)")

    # Reels test
    await msg.edit_text("🔧 Тестирую Reels...")
    TEST_URL = "https://www.instagram.com/reel/DV1joYJjCz0/"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = await download_video(TEST_URL, tmpdir)
            size_mb = round(os.path.getsize(video_path) / 1024 / 1024, 1)
            results.append(f"✅ Reels скачивание: OK ({size_mb} MB)")
            loop = asyncio.get_event_loop()
            audio_path = await loop.run_in_executor(None, extract_audio, video_path)
            results.append("✅ ffmpeg: OK")
            text = await loop.run_in_executor(None, transcribe, audio_path)
            results.append(f"✅ Groq Whisper: OK ({len(text)} символов)")
    except Exception as e:
        results.append(f"❌ Reels: {e}")

    await msg.edit_text("📊 *Диагностика:*\n\n" + "\n".join(results), parse_mode="Markdown")


async def handle_agents_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/агенты — показывает список всех агентов и что они умеют."""
    user_id = update.effective_user.id
    lines = ["🤖 *AI Агенты системы QSNera*\n"]
    for tool, a in AGENTS.items():
        lines.append(
            f"{a['icon']} *{a['title']}*\n"
            f"  _{a['desc']}_\n"
            f"  ⏱ {a['time']}\n"
        )
    lines.append("Чтобы отправить задачу конкретному агенту — используй /задача или напиши _«задача: ...»_")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=agent_keyboard(user_id)
    )


async def handle_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/отчёты — показывает последние отчёты из Obsidian прямо в Telegram."""
    user_id = update.effective_user.id
    msg = await update.message.reply_text("📋 Загружаю отчёты из Obsidian...")

    loop = asyncio.get_event_loop()
    reports = await loop.run_in_executor(None, github_get_reports)

    if not reports:
        await msg.edit_text(
            "📭 *Отчётов пока нет*\n\n"
            "Создай задачу через /задача или Reels — отчёт придёт автоматически.",
            parse_mode="Markdown"
        )
        return

    report_cache[user_id] = reports

    buttons = []
    for i, r in enumerate(reports):
        name = r["name"].replace("Отчёт: ", "").replace(".md", "")
        if len(name) > 38:
            name = name[:35] + "..."
        buttons.append([InlineKeyboardButton(f"📄 {name}", callback_data=f"rpt_view:{i}")])

    await msg.edit_text(
        "📋 *Последние отчёты:*\nНажми чтобы прочитать прямо здесь:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )


async def handle_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/задача [текст] — показывает выбор агента для выполнения задачи."""
    text = " ".join(context.args).strip() if context.args else ""
    user_id = update.effective_user.id

    if not text:
        await update.message.reply_text(
            "⚡ *Создать задачу для агента:*\n\n"
            "Укажи задачу после команды:\n"
            "`/задача Проанализируй конкурентов в укладке плитки`\n\n"
            "Или используй /агенты чтобы узнать что умеет каждый агент.",
            parse_mode="Markdown"
        )
        return

    title = text.strip().split("\n")[0][:60].strip()
    user_sessions[user_id] = {
        "state":      "agent_selecting",
        "text":       text,
        "vault":      "Бизнес QSNera",
        "folder":     "Задачи",
        "title":      title,
        "type":       "task",
    }

    await update.message.reply_text(
        f"⚡ *Задача готова*\n\n"
        f"📄 *{title}*\n\n"
        f"*Выбери агента-исполнителя:*",
        parse_mode="Markdown",
        reply_markup=agent_keyboard(user_id)
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    from telegram.error import Conflict, NetworkError, TimedOut
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("Конфликт polling")
        return
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Сетевая ошибка (временная): {err}")
        return
    # Критическая ошибка — логируем и сразу отправляем в Telegram
    tb = "".join(traceback.format_exception(None, err, err.__traceback__))
    logger.error(f"Критическая ошибка: {tb}")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ *Бот: критическая ошибка*\n\n```\n{tb[:3000]}\n```",
            parse_mode="Markdown",
        )
    except Exception:
        pass  # Если Telegram тоже недоступен — не падаем рекурсивно


# ─── Вспомогательные ─────────────────────────────────────────────────────────

def _save_owner_chat_id(chat_id: int):
    try:
        import json, pathlib
        pathlib.Path("/tmp/bot_owner.json").write_text(json.dumps({"owner_chat_id": chat_id}))
    except Exception:
        pass

def _save_chat_id_to_railway(chat_id: int) -> bool:
    if not RAILWAY_TOKEN or not RAILWAY_SVC_ID or not RAILWAY_ENV_ID:
        return False
    try:
        mutation = "mutation UpsertVariables($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }"
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": mutation, "variables": {"input": {
                "serviceId": RAILWAY_SVC_ID, "environmentId": RAILWAY_ENV_ID,
                "variables": {"ADMIN_CHAT_ID": str(chat_id)}
            }}}, timeout=10
        )
        return resp.status_code == 200
    except Exception:
        return False


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(handle_error)
    app.add_handler(CommandHandler("start",   handle_start))
    app.add_handler(CommandHandler("help",    handle_help))
    app.add_handler(CommandHandler("myid",    handle_myid))
    app.add_handler(CommandHandler("test",    handle_test))
    app.add_handler(CommandHandler("отчёты",  handle_reports))
    app.add_handler(CommandHandler("reports", handle_reports))
    app.add_handler(CommandHandler("задача",  handle_task_command))
    app.add_handler(CommandHandler("task",    handle_task_command))
    app.add_handler(CommandHandler("агенты",  handle_agents_info))
    app.add_handler(CommandHandler("agents",  handle_agents_info))
    app.add_handler(CommandHandler("чат",     handle_chat_command))
    app.add_handler(CommandHandler("chat",    handle_chat_command))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🤖 QSNera AI Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
