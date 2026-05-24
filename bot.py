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

        await progress.edit_text(report, reply_markup=make_keyboard(), parse_mode="Markdown")

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
            await progress.edit_text("⬇️ Скачиваю Reel...")
            video_path = await download_video(url, tmpdir)

            await progress.edit_text("🎵 Транскрибирую аудио через Whisper...")
            report = await analyze_reel(video_path)

        await progress.edit_text(report, reply_markup=make_keyboard(), parse_mode="Markdown")

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
        "И я сделаю анализ с ключевыми инсайтами для QSNera! 🎯",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /start
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("start", handle_start))

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
