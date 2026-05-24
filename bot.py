#!/usr/bin/env python3
"""
QSNera Reels Bot — анализирует Instagram Reels через Telegram
Пересылай Reels → получай отчёт + промпт для саморазвития
"""

import os
import logging
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from analyzer import analyze_reel
from downloader import download_video

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]


def make_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Изучить подробнее", callback_data="study"),
            InlineKeyboardButton("📋 Сохранить", callback_data="save"),
        ],
        [InlineKeyboardButton("❌ Отклонить", callback_data="dismiss")]
    ])


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает видео пересланное в чат (Reel как файл)"""
    message = update.message
    video = message.video or message.document
    if not video:
        return

    progress = await message.reply_text("⏳ Получаю видео...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = f"{tmpdir}/reel.mp4"
            file = await context.bot.get_file(video.file_id)
            await file.download_to_drive(video_path)

            await progress.edit_text("🎵 Транскрибирую аудио через Whisper...")
            report = await analyze_reel(video_path)

        try:
            await progress.edit_text(report, reply_markup=make_keyboard(), parse_mode="Markdown")
        except Exception:
            # Fallback без Markdown если есть спецсимволы
            await progress.edit_text(report, reply_markup=make_keyboard())

    except Exception as e:
        logger.error(f"Ошибка обработки видео: {e}")
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ссылки на Instagram Reels"""
    message = update.message
    text = message.text or ""

    # Только Instagram ссылки
    if not any(x in text for x in ["instagram.com/reel", "instagram.com/p/", "instagr.am"]):
        return

    url = text.strip().split()[0]
    progress = await message.reply_text("⬇️ Скачиваю Reel...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = await download_video(url, tmpdir)

            await progress.edit_text("🎵 Транскрибирую аудио через Whisper...")
            report = await analyze_reel(video_path)

        try:
            await progress.edit_text(report, reply_markup=make_keyboard(), parse_mode="Markdown")
        except Exception:
            # Fallback без Markdown если есть спецсимволы
            await progress.edit_text(report, reply_markup=make_keyboard())

    except Exception as e:
        logger.error(f"Ошибка обработки ссылки: {e}")
        await progress.edit_text(f"❌ Ошибка: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок"""
    query = update.callback_query
    await query.answer()

    actions = {
        "study": "🧠 Отлично! Создай задачу в AI Задачи для углублённого изучения.",
        "save":  "✅ Сохранено! Отчёт остаётся в истории чата.",
        "dismiss": "❌ Отклонено."
    }

    msg = actions.get(query.data, "")
    await query.edit_message_reply_markup(None)
    if msg:
        await query.message.reply_text(msg)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я *QSNera Reels Bot*.\n\n"
        "Пришли мне:\n"
        "• Видео Reel (пересланное из Instagram)\n"
        "• Ссылку на Instagram Reel\n\n"
        "И я сделаю анализ с ключевыми инсайтами для QSNera! 🎯\n\n"
        "Команды:\n"
        "/test — проверить что всё работает",
        parse_mode="Markdown"
    )


async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика всех компонентов"""
    msg = await update.message.reply_text("🔧 Запускаю диагностику...")
    TEST_URL = "https://www.instagram.com/reel/DV1joYJjCz0/"
    results = []

    # Шаг 1: yt-dlp
    await msg.edit_text("🔧 Шаг 1/3: Скачиваю тестовый Reel...")
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            from downloader import download_video
            video_path = await download_video(TEST_URL, tmpdir)
            results.append(f"✅ Скачивание: OK ({round(os.path.getsize(video_path)/1024/1024, 1)} MB)")

            # Шаг 2: ffmpeg
            await msg.edit_text("🔧 Шаг 2/3: Извлекаю аудио...")
            try:
                import subprocess, tempfile as tf
                audio = video_path.replace(".mp4", "_test.mp3")
                subprocess.run(["ffmpeg", "-i", video_path, "-vn", "-acodec", "mp3",
                                "-ar", "16000", "-ac", "1", "-b:a", "64k",
                                audio, "-y", "-loglevel", "quiet"], check=True)
                results.append(f"✅ ffmpeg: OK")

                # Шаг 3: Groq Whisper
                await msg.edit_text("🔧 Шаг 3/3: Транскрибирую...")
                try:
                    from analyzer import transcribe
                    text = transcribe(audio)
                    results.append(f"✅ Groq Whisper: OK ({len(text)} символов)")
                except Exception as e:
                    results.append(f"❌ Groq Whisper: {e}")
            except Exception as e:
                results.append(f"❌ ffmpeg: {e}")
    except Exception as e:
        results.append(f"❌ Скачивание: {e}")

    report = "📊 *Результаты диагностики:*\n\n" + "\n".join(results)
    await msg.edit_text(report, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("test", handle_test))

    # Видео файлы
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    # Instagram ссылки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Кнопки
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 QSNera Reels Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
