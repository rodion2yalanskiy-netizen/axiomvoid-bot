#!/usr/bin/env python3
"""
QSNera AI Bot — два режима в одном:
  1. 📝 ЗАМЕТКИ: любой текст → Claude классифицирует → предлагает vault/папку → сохраняет в Obsidian
  2. 🎬 REELS:  Instagram ссылка/видео → транскрипция → анализ → промпт → Claude Code

Obsidian структура:
  Бизнес QSNera:  👥 Клиенты/ | 📝 Задачи/ | ✅ Отчёты/ | 💡 Маркетинг/ | 🏗 Сайт/
  Цифровой мозг:  🧠 Brain/ | 🔑 API Ключи.md
  Личная жизнь:   🎯 Цели.md | 📓 Дневник/ | 💰 Автономный доход/
"""

import os, re, logging, asyncio, base64, tempfile
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
    format_notes_telegram, classify_note
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
user_sessions: dict = {}


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def reel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить в Claude Code", callback_data=f"reel_confirm:{user_id}"),
        InlineKeyboardButton("✏️ Доработать",             callback_data=f"reel_edit:{user_id}"),
    ]])

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
    repo      = DEFAULT_REPO
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


def send_task_to_claude_code(title: str, task_text: str) -> bool:
    """Создаёт задачу для Claude Code в папке 📝 Задачи/."""
    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', title, flags=re.UNICODE)[:50].strip()
    filename  = f"{safe_name} ({datetime.now().strftime('%H%M')}).md"
    path      = f"📝 Задачи/{filename}"

    content = f"""---
type: task
tool: code
status: delegated
task_name: {safe_name}
date: {datetime.now().strftime('%Y-%m-%d')}
source: telegram-bot
---

# {title}

{task_text}
"""
    return github_create_file(DEFAULT_REPO, path, content, f"task: {safe_name}")


# ─── Команды ─────────────────────────────────────────────────────────────────

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _save_owner_chat_id(update.effective_user.id)
    await update.message.reply_text(
        "👋 *QSNera AI Bot*\n\n"
        "Я умею:\n\n"
        "📝 *Создавать заметки* — просто напиши что угодно текстом, я пойму куда сохранить\n"
        "🎬 *Анализировать Reels* — пришли ссылку на Instagram или само видео\n\n"
        "Примеры:\n"
        "• _«Встреча с клиентом Петровым, хочет мрамор»_ → заметка в Клиенты/\n"
        "• _«Идея: сделать видео про укладку мрамора»_ → заметка в Маркетинг/\n"
        "• instagram.com/reel/... → анализ Reel + промпт для Claude Code\n\n"
        "Команды: /help /myid /test",
        parse_mode="Markdown"
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "*Заметки:* просто напиши текст → бот предложит куда сохранить\n"
        "*Задача для Claude Code:* начни с «задача:» или «сделай:»\n"
        "*Reels:* пришли ссылку instagram.com/reel/...\n\n"
        "*Хранилища:*\n"
        "🏢 Бизнес QSNera — клиенты, задачи, сайт, маркетинг\n"
        "🧠 Цифровой мозг — техника, знания\n"
        "🏠 Личная жизнь — цели, дневник\n\n"
        "/myid — твой Telegram ID\n"
        "/test — диагностика",
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
        folder   = classification.get("folder", "📝 Задачи")
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
        type_label = "⚡ Задача для Claude Code" if note_type == "task" else "📝 Заметка"

        preview = text[:300] + ("..." if len(text) > 300 else "")

        await progress.edit_text(
            f"{type_label}\n\n"
            f"📁 *{vault_emoji} {vault}*\n"
            f"📂 `{folder}/`\n"
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
            "`Бизнес QSNera / 👥 Клиенты`\n"
            "`Личная жизнь / 📓 Дневник`\n"
            "`Цифровой мозг / 🧠 Brain`",
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


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("Конфликт polling")
    elif isinstance(err, NetworkError):
        logger.warning(f"Сетевая ошибка: {err}")
    else:
        logger.error(f"Ошибка: {err}")


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
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help",  handle_help))
    app.add_handler(CommandHandler("myid",  handle_myid))
    app.add_handler(CommandHandler("test",  handle_test))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("🤖 QSNera AI Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
