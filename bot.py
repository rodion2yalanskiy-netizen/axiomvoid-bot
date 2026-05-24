#!/usr/bin/env python3
"""
Reels Research Bot — анализирует Instagram Reels и формирует промпт для Claude Code.

Пайплайн:
  Reel → транскрипция → конспект → веб-исследование → промпт → ✅/✏️
  При ✅ — задача автоматически отправляется в Claude Code через GitHub vault
  При ✏️ — цикл доработки пока результат не устроит
"""

import os
import logging
import asyncio
import base64
import tempfile
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
    format_notes_telegram
)
from downloader import download_video

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")   # для отправки в Claude Code
VAULT_REPO     = "rodion2yalanskiy-netizen/qsnera-vault"

# ─── Состояние сессий пользователей ─────────────────────────────────────────
# user_sessions[user_id] = {
#   "state": "confirming" | "editing",
#   "notes": dict,
#   "research": str,
#   "prompt": str,
# }
user_sessions: dict = {}


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить в Claude Code", callback_data=f"confirm:{user_id}"),
        InlineKeyboardButton("✏️ Доработать", callback_data=f"edit:{user_id}"),
    ]])


# ─── Отправка задачи в Claude Code через GitHub ──────────────────────────────

def send_to_claude_code(topic: str, prompt_text: str, notes: dict) -> bool:
    """
    Создаёт файл-задачу в GitHub vault → vault-sync подхватит → Claude Code выполнит.
    Требует GITHUB_TOKEN в переменных окружения Railway.
    """
    if not GITHUB_TOKEN:
        return False

    import re, urllib.parse
    safe_name = re.sub(r'[^\w\s\-]', '', topic)[:50].strip()
    filename  = f"Reel - {safe_name} ({datetime.now().strftime('%H%M')}).md"
    path      = urllib.parse.quote(f"🤖 AI Задачи/{filename}")

    content = f"""---
type: task
tool: code
priority: normal
date: {datetime.now().strftime('%Y-%m-%d')}
source: reels-bot
topic: {topic}
---

# {topic}

{prompt_text}
"""

    resp = requests.put(
        f"https://api.github.com/repos/{VAULT_REPO}/contents/{path}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"},
        json={
            "message": f"Reel task: {safe_name}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main"
        },
        timeout=30
    )
    return resp.status_code in (200, 201)


# ─── Хендлеры ────────────────────────────────────────────────────────────────

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Reels Research Bot*\n\n"
        "Пришли мне:\n"
        "• Ссылку на Instagram Reel\n"
        "• Или видео напрямую (пересланное из Instagram)\n\n"
        "Я проанализирую контент, проверю через интернет и сформирую "
        "готовый промпт для Claude Code.\n\n"
        "Команды:\n"
        "/test — диагностика всех компонентов",
        parse_mode="Markdown"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает Instagram-ссылки и правки пользователя в режиме editing"""
    message = update.message
    text    = (message.text or "").strip()
    user_id = update.effective_user.id

    # ── Режим редактирования: пользователь описывает что изменить ──
    session = user_sessions.get(user_id, {})
    if session.get("state") == "editing":
        progress = await message.reply_text("⏳ Вношу правки...")
        try:
            loop = asyncio.get_event_loop()
            new_notes, new_prompt = await loop.run_in_executor(
                None, refine_content,
                session["notes"], session["research"], session["prompt"], text
            )
            user_sessions[user_id].update({
                "notes": new_notes,
                "prompt": new_prompt,
                "state": "confirming"
            })

            summary = format_notes_telegram(new_notes, session["research"])
            prompt_preview = new_prompt[:1500] + "..." if len(new_prompt) > 1500 else new_prompt

            await progress.edit_text(summary, parse_mode="Markdown")
            await message.reply_text(
                f"📄 *Промпт для Claude Code:*\n\n{prompt_preview}",
                parse_mode="Markdown",
                reply_markup=confirm_keyboard(user_id)
            )
        except Exception as e:
            logger.error(f"refine error: {e}")
            await progress.edit_text(f"❌ Ошибка при доработке: {e}")
        return

    # ── Обычный режим: ждём Instagram-ссылку ──
    if not any(x in text for x in ["instagram.com/reel", "instagram.com/p/", "instagr.am"]):
        return

    url      = text.split()[0]
    progress = await message.reply_text("⬇️ Скачиваю Reel...")

    try:
        loop = asyncio.get_event_loop()

        # Шаг 1: Скачать
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = await download_video(url, tmpdir)

            # Шаг 2: Транскрибировать
            await progress.edit_text("🎤 Транскрибирую аудио...")
            audio_path    = await loop.run_in_executor(None, extract_audio, video_path)
            transcript    = await loop.run_in_executor(None, transcribe, audio_path)

            # Шаг 3: Извлечь конспект
            await progress.edit_text("🔍 Анализирую контент...")
            notes = await loop.run_in_executor(None, extract_structured_notes, transcript)

            # Шаг 4: Веб-исследование
            await progress.edit_text("🌐 Исследую тему через интернет...")
            research = await loop.run_in_executor(None, research_topic, notes)

            # Шаг 5: Сформировать промпт
            await progress.edit_text("📝 Формирую промпт для Claude Code...")
            claude_prompt = await loop.run_in_executor(
                None, generate_claude_prompt, notes, research
            )

        # Сохраняем сессию
        user_sessions[user_id] = {
            "state": "confirming",
            "notes": notes,
            "research": research,
            "prompt": claude_prompt,
        }

        # Отправляем конспект
        summary = format_notes_telegram(notes, research)
        try:
            await progress.edit_text(summary, parse_mode="Markdown")
        except Exception:
            await progress.edit_text(summary)

        # Отправляем промпт + кнопки
        prompt_preview = claude_prompt[:1500] + "..." if len(claude_prompt) > 1500 else claude_prompt
        try:
            await message.reply_text(
                f"📄 *Промпт для Claude Code:*\n\n{prompt_preview}",
                parse_mode="Markdown",
                reply_markup=confirm_keyboard(user_id)
            )
        except Exception:
            await message.reply_text(
                f"📄 Промпт для Claude Code:\n\n{prompt_preview}",
                reply_markup=confirm_keyboard(user_id)
            )

    except Exception as e:
        logger.error(f"handle_text error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает видео напрямую (без ссылки на Instagram)"""
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
            file       = await context.bot.get_file(video.file_id)
            await file.download_to_drive(video_path)

            await progress.edit_text("🎤 Транскрибирую аудио...")
            audio_path = await loop.run_in_executor(None, extract_audio, video_path)
            transcript = await loop.run_in_executor(None, transcribe, audio_path)

            await progress.edit_text("🔍 Анализирую контент...")
            notes = await loop.run_in_executor(None, extract_structured_notes, transcript)

            await progress.edit_text("🌐 Исследую тему...")
            research = await loop.run_in_executor(None, research_topic, notes)

            await progress.edit_text("📝 Формирую промпт...")
            claude_prompt = await loop.run_in_executor(None, generate_claude_prompt, notes, research)

        user_sessions[user_id] = {
            "state": "confirming",
            "notes": notes,
            "research": research,
            "prompt": claude_prompt,
        }

        summary = format_notes_telegram(notes, research)
        try:
            await progress.edit_text(summary, parse_mode="Markdown")
        except Exception:
            await progress.edit_text(summary)

        prompt_preview = claude_prompt[:1500] + "..." if len(claude_prompt) > 1500 else claude_prompt
        try:
            await message.reply_text(
                f"📄 *Промпт для Claude Code:*\n\n{prompt_preview}",
                parse_mode="Markdown",
                reply_markup=confirm_keyboard(user_id)
            )
        except Exception:
            await message.reply_text(
                f"📄 Промпт:\n\n{prompt_preview}",
                reply_markup=confirm_keyboard(user_id)
            )

    except Exception as e:
        logger.error(f"handle_video error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия ✅ и ✏️"""
    query   = update.callback_query
    await query.answer()

    data    = query.data or ""
    parts   = data.split(":", 1)
    action  = parts[0]
    user_id = int(parts[1]) if len(parts) > 1 else update.effective_user.id

    session = user_sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия истекла. Отправь Reel заново.")
        return

    if action == "confirm":
        # Убираем кнопки
        await query.edit_message_reply_markup(None)

        topic  = session["notes"].get("topic", "Reel задача")
        prompt = session["prompt"]

        if GITHUB_TOKEN:
            # Отправляем в vault → Claude Code подхватит автоматически
            loop    = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None, send_to_claude_code, topic, prompt, session["notes"]
            )
            if success:
                await query.message.reply_text(
                    f"✅ *Задача отправлена в Claude Code!*\n\n"
                    f"📌 Тема: *{topic}*\n\n"
                    f"Файл создан в `🤖 AI Задачи/`. "
                    f"Через 5-10 минут появится отчёт в Obsidian.",
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text(
                    f"⚠️ Не удалось создать задачу автоматически.\n\n"
                    f"Скопируй и вставь промпт в Claude Code вручную:",
                )
                await query.message.reply_text(prompt)
        else:
            # GITHUB_TOKEN не настроен — показываем промпт для копирования
            await query.message.reply_text(
                f"📋 *Промпт готов — скопируй в Claude Code:*",
                parse_mode="Markdown"
            )
            await query.message.reply_text(prompt)

        user_sessions.pop(user_id, None)

    elif action == "edit":
        # Переходим в режим редактирования
        await query.edit_message_reply_markup(None)
        user_sessions[user_id]["state"] = "editing"
        await query.message.reply_text(
            "✏️ Что изменить или добавить?\n\n"
            "Напиши своими словами — я обновлю конспект и промпт."
        )


async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика всех компонентов"""
    msg = await update.message.reply_text("🔧 Диагностика...")
    TEST_URL = "https://www.instagram.com/reel/DV1joYJjCz0/"
    results  = []

    await msg.edit_text("🔧 1/3: Скачиваю тестовый Reel...")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = await download_video(TEST_URL, tmpdir)
            size_mb    = round(os.path.getsize(video_path) / 1024 / 1024, 1)
            results.append(f"✅ Скачивание: OK ({size_mb} MB)")

            await msg.edit_text("🔧 2/3: Извлекаю аудио (ffmpeg)...")
            try:
                loop       = asyncio.get_event_loop()
                audio_path = await loop.run_in_executor(None, extract_audio, video_path)
                results.append("✅ ffmpeg: OK")

                await msg.edit_text("🔧 3/3: Транскрипция (Groq Whisper)...")
                try:
                    text = await loop.run_in_executor(None, transcribe, audio_path)
                    results.append(f"✅ Groq Whisper: OK ({len(text)} символов)")
                except Exception as e:
                    results.append(f"❌ Groq Whisper: {e}")
            except Exception as e:
                results.append(f"❌ ffmpeg: {e}")
    except Exception as e:
        results.append(f"❌ Скачивание: {e}")

    github_status = "✅ GITHUB_TOKEN настроен" if GITHUB_TOKEN else "⚠️ GITHUB_TOKEN не задан (отправка в Claude Code недоступна)"
    results.append(github_status)

    report = "📊 *Диагностика:*\n\n" + "\n".join(results)
    await msg.edit_text(report, parse_mode="Markdown")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("Конфликт polling — другой экземпляр ещё жив")
    elif isinstance(err, NetworkError):
        logger.warning(f"Сетевая ошибка: {err}")
    else:
        logger.error(f"Ошибка: {err}")


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_error_handler(handle_error)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("test",  handle_test))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Reels Research Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
