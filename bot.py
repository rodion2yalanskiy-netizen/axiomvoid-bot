#!/usr/bin/env python3
"""
Axiom:Void Bot — два режима в одном:
  1. 📝 ЗАМЕТКИ: любой текст → Claude классифицирует → предлагает vault/папку → сохраняет в Obsidian
  2. 🎬 REELS:  Instagram ссылка/видео → транскрипция → анализ → промпт → Claude Code

Obsidian структура (СУЩЕСТВУЮЩИЕ папки, без emoji!):
  Бизнес QSNera:  Клиенты/ | Задачи/ | Отчёты/ | Маркетинг/ | Сайт/
  Цифровой мозг:  Brain/ | Система/ | Саморазвитие/ | Работа над собой/
  Личная жизнь:   Brain/ | Саморазвитие/ | Работа над собой/
"""

import os, re, logging, asyncio, base64, tempfile, subprocess, json, time
from datetime import datetime
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
    TypeHandler, ApplicationHandlerStop,
)

from analyzer import (
    extract_audio, transcribe,
    extract_structured_notes, research_topic,
    generate_claude_prompt, refine_content,
    format_notes_telegram, classify_note, preprocess_task,
    chat_with_claude, analyze_image_in_chat, generate_chat_summary,
)
from downloader import download_video

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
# Убираем httpx/httpcore шум — они генерируют 80% Railway logs без пользы
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
ADMIN_CHAT_ID  = int(os.environ.get("ADMIN_CHAT_ID", "0") or "0")
# Иммутабельная копия из env — единственный источник истины для авторизации.
# Никогда не изменяется из входящих сообщений.
_OWNER_ID: int = ADMIN_CHAT_ID
RAILWAY_TOKEN  = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_SVC_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENV_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

# Fail-fast: без этих переменных бот не может работать
_missing = [name for name, val in [("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("ADMIN_CHAT_ID", ADMIN_CHAT_ID)] if not val]
if _missing:
    raise RuntimeError(f"Обязательные переменные окружения не заданы: {', '.join(_missing)} — добавь в Railway → Variables")

# CRM / Stripe
AXIOMVOID_REPO    = "rodion2yalanskiy-netizen/axiomvoid-vau"
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Репозитории для каждого vault'а
# ВАЖНО: каждый vault → свой repo, иначе бот создаёт папки в чужом vault'е!
VAULT_REPOS = {
    "Бизнес QSNera": "rodion2yalanskiy-netizen/qsnera-vault",
    "Цифровой мозг": "rodion2yalanskiy-netizen/digital-brain-vault",
    "Личная жизнь":  "rodion2yalanskiy-netizen/digital-brain-vault",  # нет отдельного repo, Brain как хаб
}
DEFAULT_REPO = "rodion2yalanskiy-netizen/qsnera-vault"

# Отображаемые имена vault'ов в UI (роутинг-ключи не меняем!)
VAULT_DISPLAY = {
    "Бизнес QSNera": "AxiomVoid",
    "Цифровой мозг": "Цифровой мозг",
    "Личная жизнь":  "Личная жизнь",
}

# ─── Сессии с TTL и персистентностью ─────────────────────────────────────────
# state: "reel_confirming" | "reel_editing" | "note_confirming" | "note_editing"
#        "agent_selecting" | "note_editing_folder" | "claude_chat"
SESSION_TTL  = 3600                              # 1 час — сессия живёт без активности
SESSION_FILE = Path(os.environ.get("SESSION_FILE_PATH", "/tmp/bot_sessions.json"))
# На Railway /tmp сбрасывается при деплое — задать SESSION_FILE_PATH в Variables
# для сохранения сессий. Пример: SESSION_FILE_PATH=/app/sessions.json

def _persist(data: dict):
    try:
        import tempfile
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {str(k): v for k, v in data.items()}, ensure_ascii=False
        ))
        os.replace(tmp, SESSION_FILE)
    except Exception:
        pass

def _load_sessions() -> dict:
    try:
        if SESSION_FILE.exists():
            raw = json.loads(SESSION_FILE.read_text())
            now = time.time()
            return {int(k): v for k, v in raw.items()
                    if now - v.get("_ts", 0) < SESSION_TTL}
    except Exception:
        pass
    return {}

class SessionStore(dict):
    """dict с автоматическим TTL и персистентностью."""

    def __setitem__(self, key, value):
        if isinstance(value, dict):
            value["_ts"] = time.time()
        super().__setitem__(key, value)
        _persist(self)

    def get(self, key, default=None):          # type: ignore[override]
        s = super().get(key)
        if s is None:
            return default
        if time.time() - s.get("_ts", 0) > SESSION_TTL:
            super().__delitem__(key)
            _persist(self)
            return default
        return s

    def pop(self, key, *args):
        result = super().pop(key, *args)
        _persist(self)
        return result

    def touch(self, key):
        """Обновляет _ts и сохраняет (вызывать после прямого изменения dict внутри)."""
        if key in self:
            self[key]["_ts"] = time.time()
            _persist(self)

# Загружаем при старте — сессии переживают crash/graceful restart
user_sessions: SessionStore = SessionStore(_load_sessions())

# Кеш списка отчётов для inline-кнопок (user_id -> list of file dicts)
report_cache: dict = {}
REPORT_CACHE_TTL = 300  # 5 минут

# ─── Системный промпт для чата ────────────────────────────────────────────────
SYSTEM_PROMPT_CHAT = (
    "Ты — AI-помощник Родиона Яланского, основателя студии Axiom:Void "
    "(веб-разработка и цифровые продукты: Void:Form / Axiom:Core / The Nexus / Absolute Zero).\n"
    "Помогай с бизнес-вопросами, анализом дизайна и кода, "
    "идеями для контента, техническими задачами.\n"
    "Отвечай по-русски, кратко и конкретно. "
    "Если присылают скриншоты, макеты или UI — анализируй профессионально. "
    "Диалог ведётся через Telegram."
)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def safe_md_truncate(text: str, limit: int = 4000) -> str:
    """Truncate to limit chars without leaving unclosed Telegram Markdown markers."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 5]
    last_space = cut.rfind(' ')
    if last_space > len(cut) // 2:
        cut = cut[:last_space]
    cut += "…"
    # Close triple-backtick code blocks before single backticks
    if cut.count('```') % 2 == 1:
        cut += '\n```'
    single_bt = cut.count('`') - 3 * cut.count('```')
    if single_bt % 2 == 1:
        cut += '`'
    if cut.count('*') % 2 == 1:
        cut += '*'
    if cut.count('_') % 2 == 1:
        cut += '_'
    return cut


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
        "desc":  "Анализ изображений, скриншотов, UI/UX макетов или дизайна",
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

def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню — 4 кнопки режимов работы."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Задача для Claude",  callback_data="menu:task"),
            InlineKeyboardButton("📝 Заметка",           callback_data="menu:note"),
        ],
        [
            InlineKeyboardButton("💬 Чат с Claude",      callback_data="menu:chat"),
            InlineKeyboardButton("📋 Отчёты",            callback_data="menu:reports"),
        ],
    ])


def reel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Отправить в Claude Code", callback_data=f"reel_confirm:{user_id}"),
        InlineKeyboardButton("✏️ Доработать",             callback_data=f"reel_edit:{user_id}"),
    ]])

def agent_keyboard(user_id: int, urgent: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура выбора агента для выполнения задачи."""
    prio_btn = "🔴 Срочно (сейчас)" if not urgent else "⚪ Обычный приоритет"
    prio_data = f"agt:{user_id}:set_urgent" if not urgent else f"agt:{user_id}:set_normal"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(AGENTS["code"]["btn"],        callback_data=f"agt:{user_id}:code"),
            InlineKeyboardButton(AGENTS["openrouter"]["btn"],  callback_data=f"agt:{user_id}:openrouter"),
        ],
        [
            InlineKeyboardButton("✏️ Изменить задачу",         callback_data=f"agt:{user_id}:edit"),
            InlineKeyboardButton(prio_btn,                     callback_data=prio_data),
        ],
    ])

def task_preview_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выбрать агента", callback_data=f"task_accept:{user_id}"),
        InlineKeyboardButton("✏️ Изменить",       callback_data=f"task_redo:{user_id}"),
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
    """Сохраняет заметку в нужный vault через GitHub API.
    Использует ТОЛЬКО существующие папки — проверяет реальную структуру репозиториев."""
    import re as _re

    # Разрешённые папки — только реально существующие в каждом репозитории!
    # qsnera-vault (Бизнес QSNera): Клиенты, Задачи, Отчёты, Маркетинг, Сайт
    # digital-brain-vault (Цифровой мозг + Личная жизнь): Brain, Система, Саморазвитие, Работа над собой
    ALLOWED_FOLDERS = {
        "Бизнес QSNera": ["Клиенты", "Задачи", "Отчёты", "Маркетинг", "Сайт"],
        "Цифровой мозг": ["Brain", "Система", "Саморазвитие", "Работа над собой"],
        "Личная жизнь":  ["Brain", "Саморазвитие", "Работа над собой"],  # в digital-brain-vault, нет отдельного repo
    }

    # Дефолтная папка для каждого vault'а
    VAULT_DEFAULTS = {
        "Бизнес QSNera": "Задачи",
        "Цифровой мозг": "Brain",
        "Личная жизнь":  "Brain",
    }

    # Очищаем emoji из vault и folder
    vault  = _re.sub(r'[\U0001F000-\U0001FFFF☀-⟿⌀-⏿]', '', vault).strip()
    folder = _re.sub(r'[\U0001F000-\U0001FFFF☀-⟿⌀-⏿]', '', folder).strip()

    # Валидируем — если vault неизвестен, сбрасываем на Бизнес QSNera
    if vault not in ALLOWED_FOLDERS:
        vault = "Бизнес QSNera"

    # Маппинг папок Личная жизнь → существующие папки в digital-brain-vault
    if vault == "Личная жизнь":
        folder_map = {"Цели": "Саморазвитие", "Дневник": "Работа над собой"}
        folder = folder_map.get(folder, "Brain")

    # Валидируем folder — если нет в списке, берём дефолт для vault'а
    if folder not in ALLOWED_FOLDERS.get(vault, []):
        folder = VAULT_DEFAULTS.get(vault, "Brain")

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


def github_get_file_content(path: str, repo: str = DEFAULT_REPO) -> str:
    """Загружает содержимое файла из GitHub (декодирует из base64)."""
    import urllib.parse
    if not GITHUB_TOKEN:
        return ""
    url = f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}"
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


def create_task(title: str, task_text: str, tool: str = "code", priority: str = "normal") -> tuple:
    """Создаёт задачу в Задачи/ с указанным tool и приоритетом. Возвращает (success: bool, github_path: str)."""
    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', title, flags=re.UNICODE)[:50].strip()
    filename  = f"{safe_name} ({datetime.now().strftime('%H%M')}).md"
    # Urgent задачи получают префикс для сортировки первыми (sort по имени файла)
    if priority == "urgent":
        filename = f"0_URGENT_{filename}"
    path      = f"Задачи/{filename}"

    content = f"""---
type: task
tool: {tool}
status: delegated
priority: {priority}
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


# ─── CRM: хелперы ─────────────────────────────────────────────────────────────

def create_client_file(name: str, email: str, project_type: str, budget: str) -> bool:
    """Создаёт файл клиента из шаблона в axiomvoid-vault."""
    date_str  = datetime.now().strftime("%Y-%m-%d")
    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', name, flags=re.UNICODE)[:60].strip()
    content = f"""---
tags: [клиент, crm]
status: lead
created: {date_str}
---

# {name}

## Связи
- [[Dashboard]]
- [[CRM-Обзор]]

## Контакты
- Email: {email}
- Телефон:
- Instagram:
- Город:

## Проект
- Тип: {project_type}
- Бюджет: {budget}
- Дедлайн:
- Статус: lead

## История общения
- {date_str}: добавлен через Telegram бот

## Файлы
- Бриф:
- Договор:
- Инвойс:

## Заметки

"""
    return github_create_file(AXIOMVOID_REPO, f"Клиенты/{safe_name}.md",
                               content, f"crm: новый клиент {safe_name}")


def update_crm_overview(name: str, status: str = "lead", budget: str = "—") -> bool:
    """Добавляет строку клиента в CRM-Обзор.md через GitHub API."""
    raw = github_get_file_content("Клиенты/CRM-Обзор.md", repo=AXIOMVOID_REPO)
    if not raw:
        return False

    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', name, flags=re.UNICODE)[:60].strip()
    date_str  = datetime.now().strftime("%Y-%m-%d")
    new_row   = f"| [[{safe_name}]] | {status} | {budget} | — |"

    # Вставляем строку перед первой пустой строкой после разделителя таблицы
    lines, result = raw.split("\n"), []
    in_active, in_table, inserted = False, False, False
    for line in lines:
        if "## Активные клиенты" in line:
            in_active = True
        if in_active and line.startswith("|---"):
            in_table = True
        if in_active and in_table and not line.startswith("|") and not inserted:
            result.append(new_row)
            inserted, in_active, in_table = True, False, False
        result.append(line)

    new_content = "\n".join(result)

    # Добавляем в раздел «Все клиенты»
    if "## Все клиенты" in new_content:
        new_content = new_content.rstrip() + f"\n- [[{safe_name}]] — {status}, -, {budget}\n"

    new_content = re.sub(r'updated: \d{4}-\d{2}-\d{2}', f'updated: {date_str}', new_content)
    return github_create_file(AXIOMVOID_REPO, "Клиенты/CRM-Обзор.md",
                               new_content, f"crm: добавлен {safe_name}")


def create_stripe_payment(client_name: str, amount_usd: float, description: str) -> dict:
    """Создаёт Stripe Checkout Session. Возвращает {url, id} или {error}."""
    if not STRIPE_SECRET_KEY:
        return {"error": "STRIPE_SECRET_KEY не задан — добавь в Railway Variables"}
    try:
        resp = requests.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data={
                "payment_method_types[]": "card",
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][product_data][name]": description[:500],
                "line_items[0][price_data][unit_amount]": str(int(amount_usd * 100)),
                "line_items[0][quantity]": "1",
                "mode": "payment",
                "success_url": "https://t.me/axiomvoidbot",
                "cancel_url": "https://t.me/axiomvoidbot",
                "metadata[client]": client_name[:500],
            },
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 200:
            return {"url": data["url"], "id": data["id"]}
        return {"error": data.get("error", {}).get("message", f"HTTP {resp.status_code}")}
    except Exception as e:
        return {"error": str(e)}


def record_invoice_in_client_file(client_name: str, amount_usd: float,
                                   description: str, payment_url: str, session_id: str) -> bool:
    """Дописывает инвойс в файл клиента в axiomvoid-vault."""
    safe_name = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', client_name, flags=re.UNICODE)[:60].strip()
    path      = f"Клиенты/{safe_name}.md"
    raw       = github_get_file_content(path, repo=AXIOMVOID_REPO)
    if not raw:
        return False
    date_str = datetime.now().strftime("%Y-%m-%d")
    block = (
        f"\n## Инвойс {date_str}\n"
        f"- Сумма: ${amount_usd:.0f}\n"
        f"- Услуга: {description}\n"
        f"- Ссылка: {payment_url}\n"
        f"- Stripe ID: {session_id}\n"
    )
    return github_create_file(AXIOMVOID_REPO, path,
                               raw.rstrip() + "\n" + block,
                               f"invoice: {safe_name} ${amount_usd:.0f}")


# ─── CRM: обработчики шагов /newclient ────────────────────────────────────────

def _nc_type_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Сайт",     callback_data=f"nc_type:{user_id}:сайт"),
            InlineKeyboardButton("🔄 Редизайн", callback_data=f"nc_type:{user_id}:редизайн"),
        ],
        [
            InlineKeyboardButton("📄 Лендинг",  callback_data=f"nc_type:{user_id}:лендинг"),
            InlineKeyboardButton("📦 Другое",   callback_data=f"nc_type:{user_id}:другое"),
        ],
    ])


async def _nc_handle_name(message, text: str, user_id: int, session: dict):
    user_sessions[user_id] = {**session, "state": "nc_email", "nc_name": text}
    await message.reply_text(
        f"✅ Имя: *{text}*\n\nШаг 2/4: Email клиента:",
        parse_mode="Markdown"
    )


async def _nc_handle_email(message, text: str, user_id: int, session: dict):
    user_sessions[user_id] = {**session, "state": "nc_type", "nc_email": text}
    await message.reply_text(
        f"✅ Email: *{text}*\n\nШаг 3/4: Тип проекта:",
        parse_mode="Markdown",
        reply_markup=_nc_type_keyboard(user_id)
    )


async def _nc_set_type(message, project_type: str, user_id: int, session: dict):
    user_sessions[user_id] = {**session, "state": "nc_budget", "nc_type": project_type}
    await message.reply_text(
        f"✅ Тип: *{project_type}*\n\nШаг 4/4: Бюджет (например: $1,500 или $3,000–5,000):",
        parse_mode="Markdown"
    )


async def _nc_handle_budget(message, text: str, user_id: int, session: dict):
    progress = await message.reply_text("⏳ Создаю клиента в CRM...")
    name         = session.get("nc_name", "")
    email        = session.get("nc_email", "")
    project_type = session.get("nc_type", "")
    budget       = text
    loop     = asyncio.get_running_loop()
    ok_file  = await loop.run_in_executor(None, create_client_file, name, email, project_type, budget)
    ok_crm   = await loop.run_in_executor(None, update_crm_overview, name, "lead", budget)
    user_sessions.pop(user_id, None)

    if ok_file:
        safe = re.sub(r'[^\w\s\-а-яёА-ЯЁ]', '', name, flags=re.UNICODE)[:60].strip()
        status_crm = "✅ обновлён" if ok_crm else "⚠️ не обновился (запусти crm-sync.sh)"
        await progress.edit_text(
            f"✅ *Клиент {name} добавлен в CRM!*\n\n"
            f"👤 Имя: {name}\n📧 Email: {email}\n"
            f"🏗 Проект: {project_type}\n💰 Бюджет: {budget}\n\n"
            f"📁 `Клиенты/{safe}.md`\n📊 CRM-Обзор: {status_crm}",
            parse_mode="Markdown"
        )
    else:
        await progress.edit_text("❌ Ошибка создания файла клиента. Проверь GITHUB_TOKEN.")


# ─── Invoice: обработчики шагов /invoice ──────────────────────────────────────

async def _inv_handle_client(message, text: str, user_id: int, session: dict):
    user_sessions[user_id] = {**session, "state": "inv_amount", "inv_client": text}
    await message.reply_text(
        f"✅ Клиент: *{text}*\n\nШаг 2/3: Сумма в USD (только цифра, например: 1500):",
        parse_mode="Markdown"
    )


async def _inv_handle_amount(message, text: str, user_id: int, session: dict):
    # Извлекаем число из строки
    digits = re.sub(r'[^\d.]', '', text)
    try:
        amount = float(digits)
    except ValueError:
        await message.reply_text("⚠️ Введи сумму числом, например: `1500` или `2,500`", parse_mode="Markdown")
        return
    user_sessions[user_id] = {**session, "state": "inv_desc", "inv_amount": amount}
    await message.reply_text(
        f"✅ Сумма: *${amount:,.0f}*\n\nШаг 3/3: Описание услуги (будет в инвойсе):",
        parse_mode="Markdown"
    )


async def _inv_handle_desc(message, text: str, user_id: int, session: dict):
    progress    = await message.reply_text("⏳ Создаю Stripe инвойс...")
    client_name = session.get("inv_client", "")
    amount_usd  = session.get("inv_amount", 0.0)
    description = text

    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, create_stripe_payment, client_name, amount_usd, description)

    if "error" in result:
        await progress.edit_text(f"❌ Ошибка Stripe: {result['error']}")
        user_sessions.pop(user_id, None)
        return

    pay_url    = result["url"]
    session_id = result["id"]

    # Записываем инвойс в файл клиента
    ok_obs = await loop.run_in_executor(
        None, record_invoice_in_client_file, client_name, amount_usd, description, pay_url, session_id
    )
    user_sessions.pop(user_id, None)

    obs_status = "✅ записан в Obsidian" if ok_obs else "⚠️ файл клиента не найден в CRM"
    await progress.edit_text(
        f"💳 *Инвойс создан!*\n\n"
        f"👤 Клиент: {client_name}\n"
        f"💰 Сумма: ${amount_usd:,.0f}\n"
        f"📝 Услуга: {description}\n\n"
        f"🔗 *Ссылка на оплату:*\n{pay_url}\n\n"
        f"📄 Obsidian: {obs_status}",
        parse_mode="Markdown"
    )


# ─── Авторизация — блокировка не-владельца ───────────────────────────────────

async def _guard_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs in handler group -1 before all others.
    Drops every update that doesn't come from _OWNER_ID.
    Raises ApplicationHandlerStop so PTB skips remaining handlers."""
    uid = update.effective_user.id if update.effective_user else None
    if uid != _OWNER_ID:
        raise ApplicationHandlerStop()

# ─── Команды ─────────────────────────────────────────────────────────────────

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _save_owner_chat_id(update.effective_user.id)
    await update.message.reply_text(
        "👋 *Axiom:Void Bot*\n\n"
        "Выбери что хочешь сделать — или просто напиши текст / пришли голосовое:\n\n"
        "📝 *Заметка* — любой текст или голос → Claude определит куда сохранить\n"
        "✅ *Задача* — напиши _«задача: ...»_ или нажми кнопку\n"
        "💬 *Чат* — многоходовой диалог с Claude\n"
        "📋 *Отчёты* — последние выполненные задачи\n"
        "🎬 *Reels* — пришли ссылку instagram.com/reel/...\n\n"
        "_Доп. команды:_ /агенты /статус /помощь /myid",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "*Заметки:* напиши текст → бот предложит куда сохранить\n"
        "*Фото:* пришли скриншот/макет/UI → анализ Claude Vision\n"
        "*Задача агенту:* `/задача что сделать` → выбери агента\n"
        "  или напиши текст с _«задача:»_ / _«сделай:»_\n"
        "*Диалог:* `/чат` — Claude помнит весь разговор\n"
        "  пришли фото прямо в чат — анализирует в контексте\n"
        "  /стоп — завершить и сохранить конспект в Obsidian\n"
        "*Все агенты:* `/агенты` — описание + кнопки\n"
        "*Отчёты:* `/отчёты` — последние отчёты прямо здесь\n"
        "*Reels:* пришли ссылку instagram.com/reel/...\n\n"
        "*Хранилища:*\n"
        "🏢 Бизнес QSNera — клиенты, задачи, маркетинг, сайт\n"
        "🧠 Цифровой мозг — Brain, Система, Саморазвитие, Работа над собой\n"
        "🏠 Личная жизнь — Brain, Саморазвитие, Работа над собой\n\n"
        "/статус — статус агентов, очередь задач, последние отчёты\n"
        "/myid — твой Telegram ID\n"
        "/test — полная диагностика системы",
        parse_mode="Markdown"
    )

async def handle_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает caller его Telegram ID. Не изменяет ADMIN_CHAT_ID."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"🆔 Твой Telegram ID: `{user_id}`",
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

    # ── Режим ввода задачи (через кнопку меню «Задача») ──
    if session.get("state") == "awaiting_task_text":
        user_sessions.pop(user_id, None)
        await _process_task_direct(message, text, user_id)
        return

    # ── Режим ввода заметки (через кнопку меню «Заметка») ──
    if session.get("state") == "awaiting_note_text":
        user_sessions.pop(user_id, None)
        await _process_note(message, text, user_id)
        return

    # ── Режим правки Reels ──
    if session.get("state") == "reel_editing":
        await _handle_reel_refinement(message, text, user_id, session)
        return

    # ── Режим правки заметки ──
    if session.get("state") == "note_editing":
        await _handle_note_text_edit(message, text, user_id, session)
        return

    # ── Режим правки задачи (после предпросмотра) ──
    if session.get("state") == "task_editing":
        await _process_task_edit(message, text, user_id, session)
        return

    # ── Режим смены папки ──
    if session.get("state") == "note_editing_folder":
        await _handle_note_folder_edit(message, text, user_id, session)
        return

    # ── CRM: /newclient шаги ──
    if session.get("state") == "nc_name":
        await _nc_handle_name(message, text, user_id, session)
        return
    if session.get("state") == "nc_email":
        await _nc_handle_email(message, text, user_id, session)
        return
    if session.get("state") == "nc_type":
        await _nc_set_type(message, text, user_id, session)
        return
    if session.get("state") == "nc_budget":
        await _nc_handle_budget(message, text, user_id, session)
        return

    # ── Invoice: /invoice шаги ──
    if session.get("state") == "inv_client":
        await _inv_handle_client(message, text, user_id, session)
        return
    if session.get("state") == "inv_amount":
        await _inv_handle_amount(message, text, user_id, session)
        return
    if session.get("state") == "inv_desc":
        await _inv_handle_desc(message, text, user_id, session)
        return

    # ── Instagram ссылка → режим Reels ──
    if any(x in text for x in ["instagram.com/reel", "instagram.com/p/", "instagr.am"]):
        await _process_reel(message, text.split()[0], user_id)
        return

    # ── Задача по prefix → прямо в task_preview, минуя classify_note ──
    _TASK_PREFIXES = ("задача:", "задача :", "task:", "сделай:", "выполни:")
    if any(text.lower().startswith(p) for p in _TASK_PREFIXES):
        await _process_task_direct(message, text, user_id)
        return

    # ── Любой другой текст → режим заметок ──
    await _process_note(message, text, user_id)


_VAGUE_PATTERNS = (
    "все проблемы", "всё исправь", "всё починить", "полный аудит",
    "разбери всё", "сделай всё", "реши всё", "исправь всё",
    "проверь всё", "all problems", "fix everything", "audit all",
)

async def _process_task_direct(message, text: str, user_id: int):
    """Задача с prefix «задача:» → прямо в task_preview, минуя classify_note."""
    # Защита от мега-задач — предупреждаем до генерации промпта
    text_lower = text.lower()
    is_vague = any(p in text_lower for p in _VAGUE_PATTERNS)
    if is_vague:
        await message.reply_text(
            "⚠️ *Задача слишком широкая*\n\n"
            "Такие задачи как «исправь все проблемы» или «сделай полный аудит» "
            "всегда заканчиваются таймаутом — Claude Code не успевает за 30 минут.\n\n"
            "*Сформулируй конкретнее, например:*\n"
            "• `задача: исправь ошибку X в файле Y`\n"
            "• `задача: подключи GitHub к Railway`\n"
            "• `задача: почини шаг 3 в daily-self-dev.sh`\n\n"
            "_Одна задача = одна конкретная цель._",
            parse_mode="Markdown"
        )
        return

    progress = await message.reply_text("🤖 Формирую структурированный промпт...")
    try:
        loop = asyncio.get_running_loop()
        optimized = await loop.run_in_executor(None, preprocess_task, text)

        # Извлекаем заголовок из первой строки (без prefix)
        _TASK_PREFIXES_LIST = ("задача:", "задача :", "task:", "сделай:", "выполни:")
        first_line = text.split("\n")[0].strip()
        for prefix in _TASK_PREFIXES_LIST:
            if first_line.lower().startswith(prefix):
                first_line = first_line[len(prefix):].strip()
                break
        title = first_line[:60] if first_line else f"Задача {datetime.now().strftime('%d.%m %H:%M')}"

        user_sessions[user_id] = {
            "state":    "task_preview",
            "text":     optimized,
            "raw_text": text,
            "vault":    "Бизнес QSNera",
            "folder":   "Задачи",
            "title":    title,
            "type":     "task",
        }

        raw_prev = text[:100] + ("..." if len(text) > 100 else "")
        opt_prev = optimized[:1200] + ("\n\n_...промпт продолжается_" if len(optimized) > 1200 else "")

        try:
            await progress.edit_text(
                f"📋 *Промпт для Claude Code сформирован:*\n\n"
                f"{opt_prev}\n\n"
                f"---\n_Исходник:_ `{raw_prev}`",
                parse_mode="Markdown",
                reply_markup=task_preview_keyboard(user_id)
            )
        except Exception:
            await progress.edit_text(
                f"📋 Промпт для Claude Code:\n\n{opt_prev}\n\n---\nИсходник: {raw_prev}",
                reply_markup=task_preview_keyboard(user_id)
            )
    except Exception as e:
        logger.error(f"_process_task_direct error: {e}", exc_info=True)
        await progress.edit_text(f"❌ Ошибка: {e}")


async def _process_note(message, text: str, user_id: int):
    """Классифицирует текст и предлагает сохранить как заметку."""
    if len(text) < 3:
        return

    progress = await message.reply_text("🤔 Разбираюсь куда сохранить...")
    try:
        loop = asyncio.get_running_loop()
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
            # Задача → препроцессинг через AI → показываем предпросмотр
            await progress.edit_text("🤖 Формирую структурированный промпт...")
            optimized = await loop.run_in_executor(None, preprocess_task, text)

            user_sessions[user_id].update({
                "state":    "task_preview",
                "text":     optimized,
                "raw_text": text,
            })

            raw_prev = text[:100] + ("..." if len(text) > 100 else "")
            # Показываем первые 1200 символов промпта — достаточно чтобы увидеть структуру
            opt_prev = optimized[:1200] + ("\n\n_...промпт продолжается_" if len(optimized) > 1200 else "")

            try:
                await progress.edit_text(
                    f"📋 *Промпт для Claude Code сформирован:*\n\n"
                    f"{opt_prev}\n\n"
                    f"---\n_Исходник:_ `{raw_prev}`",
                    parse_mode="Markdown",
                    reply_markup=task_preview_keyboard(user_id)
                )
            except Exception:
                # Если Markdown не прошёл — plain text
                await progress.edit_text(
                    f"📋 Промпт для Claude Code:\n\n{opt_prev}\n\n---\nИсходник: {raw_prev}",
                    reply_markup=task_preview_keyboard(user_id)
                )
        else:
            # Заметка → обычный флоу
            await progress.edit_text(
                f"📝 *Заметка*\n\n"
                f"📁 *{vault_emoji} {VAULT_DISPLAY.get(vault, vault)}* / `{folder}/`\n"
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
            f"📁 *{vault_emoji} {VAULT_DISPLAY.get(vault, vault)}* / `{folder}/`\n"
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
        f"📁 *{vault_emoji} {VAULT_DISPLAY.get(vault, vault)}* / `{folder}/`\n"
        f"📄 *{title}*",
        parse_mode="Markdown",
        reply_markup=note_keyboard(user_id)
    )


# ─── Reels pipeline ──────────────────────────────────────────────────────────

async def _process_reel(message, url: str, user_id: int):
    if not os.environ.get("GROQ_API_KEY"):
        await message.reply_text(
            "⚠️ Транскрипция Reels недоступна: GROQ\\_API\\_KEY не задан.",
            parse_mode="Markdown"
        )
        logger.warning("_process_reel: GROQ_API_KEY не задан, запрос отклонён")
        return
    progress = await message.reply_text("⬇️ Скачиваю Reel...")
    try:
        loop = asyncio.get_running_loop()
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


async def _process_task_edit(message, text: str, user_id: int, session: dict):
    """Пользователь исправил задачу → препроцессинг заново → предпросмотр."""
    progress = await message.reply_text("🤖 Обновляю...")
    loop = asyncio.get_running_loop()
    optimized = await loop.run_in_executor(None, preprocess_task, text)

    user_sessions[user_id].update({
        "state":    "task_preview",
        "text":     optimized,
        "raw_text": text,
    })

    raw_prev = text[:120] + ("..." if len(text) > 120 else "")
    opt_prev = optimized[:700] + ("..." if len(optimized) > 700 else "")

    await progress.edit_text(
        f"🤖 *Обновлённая задача:*\n\n"
        f"```\n{opt_prev}\n```\n\n"
        f"📝 _Исходный:_ {raw_prev}",
        parse_mode="Markdown",
        reply_markup=task_preview_keyboard(user_id)
    )


async def _handle_reel_refinement(message, text: str, user_id: int, session: dict):
    progress = await message.reply_text("⏳ Вношу правки...")
    try:
        loop = asyncio.get_running_loop()
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
    """Голосовое сообщение → Groq Whisper (OGG напрямую, без ffmpeg) → текст."""
    message = update.message
    user_id = update.effective_user.id
    voice   = message.voice or message.audio

    if not os.environ.get("GROQ_API_KEY"):
        await message.reply_text(
            "⚠️ Транскрипция голоса недоступна: GROQ\\_API\\_KEY не задан.",
            parse_mode="Markdown"
        )
        logger.warning("handle_voice: GROQ_API_KEY не задан, запрос отклонён")
        return

    progress = await message.reply_text("🎤 Слушаю...")
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        loop    = asyncio.get_event_loop()

        with tempfile.TemporaryDirectory() as tmpdir:
            ogg_path = f"{tmpdir}/voice.ogg"
            await tg_file.download_to_drive(ogg_path)

            size_kb = os.path.getsize(ogg_path) // 1024
            logger.info(f"voice: скачан {size_kb}KB → {ogg_path}")

            await progress.edit_text("🔍 Распознаю речь...")
            # Groq Whisper принимает OGG/Opus напрямую — ffmpeg не нужен
            transcript = await loop.run_in_executor(None, transcribe, ogg_path)
            logger.info(f"voice: transcript='{transcript[:80]}'")

        if not transcript or len(transcript.strip()) < 2:
            await progress.edit_text(
                "❌ Речь не распознана.\n\n"
                "Попробуй: говори чётче, поднеси телефон ближе ко рту."
            )
            return

        # Показываем расшифровку
        safe = transcript.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        await progress.edit_text(f"📝 *Распознано:*\n_{safe}_", parse_mode="Markdown")

        # Дальше — как обычный текст
        session = user_sessions.get(user_id, {})
        if session.get("state") == "claude_chat":
            await _handle_claude_chat(message, transcript, user_id, session)
        else:
            await _process_note(message, transcript, user_id)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"voice error:\n{tb}")
        # Отправляем полный стектрейс админу для диагностики
        if ADMIN_CHAT_ID and user_id != ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"🔴 voice error от {user_id}:\n```\n{tb[:1000]}\n```",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        await progress.edit_text(
            f"❌ Ошибка при распознавании:\n`{str(e)[:300]}`",
            parse_mode="Markdown"
        )


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

        question = caption if caption else "Что на этом фото? Опиши профессионально — особенно если это UI/UX макет, дизайн-система, скриншот сайта или интерфейс."

        loop = asyncio.get_running_loop()

        # В режиме чата — добавляем в историю
        if session.get("state") == "claude_chat":
            history = session.get("history", [])
            answer, user_msg = await loop.run_in_executor(
                None, analyze_image_in_chat, img_b64, question, history
            )
            history.append(user_msg)
            history.append({"role": "assistant", "content": answer})
            user_sessions[user_id]["history"] = history
            await progress.edit_text(safe_md_truncate(answer, 4000))
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
                f"📷 *Анализ фото:*\n\n{safe_md_truncate(answer, 3500)}",
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
            loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, chat_with_claude, history, SYSTEM_PROMPT_CHAT, 2000
        )
        history.append({"role": "assistant", "content": answer})
        # Ограничиваем историю (последние 20 сообщений)
        if len(history) > 20:
            history = history[-20:]
        user_sessions[user_id]["history"] = history
        user_sessions.touch(user_id)

        msg_count = len([m for m in history if m["role"] == "user"])
        await progress.edit_text(
            f"{safe_md_truncate(answer, 3900)}\n\n_Сообщение {msg_count} · /стоп чтобы завершить_",
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
            "Можешь присылать скриншоты, макеты, UI для анализа.\n\n"
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
    if not os.environ.get("GROQ_API_KEY"):
        await message.reply_text(
            "⚠️ Транскрипция видео недоступна: GROQ\\_API\\_KEY не задан.",
            parse_mode="Markdown"
        )
        logger.warning("handle_video: GROQ_API_KEY не задан, запрос отклонён")
        return
    progress = await message.reply_text("⏳ Получаю видео...")
    try:
        loop = asyncio.get_running_loop()
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

    # ── CRM: выбор типа проекта ──────────────────────────────────────────────
    if data.startswith("nc_type:"):
        parts_nc = data.split(":", 2)
        uid_nc   = int(parts_nc[1]) if len(parts_nc) > 1 else update.effective_user.id
        nc_type  = parts_nc[2] if len(parts_nc) > 2 else "другое"
        session_nc = user_sessions.get(uid_nc, {})
        if session_nc.get("state") == "nc_type":
            await query.edit_message_reply_markup(None)
            await _nc_set_type(query.message, nc_type, uid_nc, session_nc)
        return

    # ── Worker: Отмена / Одобрение инструментов ───────────────────────────────
    if data.startswith("worker_"):
        await _handle_worker_callback(query, data)
        return

    # ── Главное меню ─────────────────────────────────────────────────────────
    if data.startswith("menu:"):
        action = data.split(":")[1]
        uid = update.effective_user.id
        if action == "task":
            user_sessions[uid] = {"state": "awaiting_task_text"}
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(
                "✅ *Задача для Claude*\n\nОпиши задачу — я сформирую структурированный промпт.\n"
                "_Или напиши «задача: ...» в любой момент._",
                parse_mode="Markdown"
            )
        elif action == "note":
            user_sessions[uid] = {"state": "awaiting_note_text"}
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(
                "📝 *Заметка*\n\nНапиши что угодно — текстом или голосом.",
                parse_mode="Markdown"
            )
        elif action == "chat":
            user_sessions[uid] = {"state": "claude_chat", "history": []}
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(
                "💬 *Диалог с Claude*\n\nЗадавай вопросы — помню контекст разговора.\n"
                "_/стоп — завершить и сохранить конспект._",
                parse_mode="Markdown"
            )
        elif action == "reports":
            await query.edit_message_reply_markup(None)
            # Переиспользуем handle_reports логику
            loop = asyncio.get_running_loop()
            reports = await loop.run_in_executor(None, github_get_reports)
            if not reports:
                await query.message.reply_text("📭 Отчётов пока нет.")
                return
            report_cache[uid] = reports
            buttons = []
            for i, r in enumerate(reports):
                name = r["name"].replace("Отчёт: ", "").replace(".md", "")
                if len(name) > 38:
                    name = name[:35] + "..."
                buttons.append([InlineKeyboardButton(f"📄 {name}", callback_data=f"rpt_view:{i}")])
            await query.message.reply_text(
                "📋 *Последние отчёты:*\nНажми чтобы прочитать:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        return

    # ── Предпросмотр задачи: подтвердить ─────────────────────────────────────
    if data.startswith("task_accept:"):
        uid = int(data.split(":")[1])
        session = user_sessions.get(uid, {})
        if not session or session.get("state") != "task_preview":
            await query.message.reply_text("⚠️ Сессия истекла. Напиши задачу снова.")
            return
        user_sessions[uid]["state"] = "agent_selecting"
        user_sessions.touch(uid)
        await query.edit_message_text(
            f"⚡ *Задача подтверждена*\n\n"
            f"📄 *{session.get('title', 'Задача')}*\n\n"
            f"*Выбери агента-исполнителя:*",
            parse_mode="Markdown",
            reply_markup=agent_keyboard(uid)
        )
        return

    # ── Предпросмотр задачи: изменить ────────────────────────────────────────
    if data.startswith("task_redo:"):
        uid = int(data.split(":")[1])
        session = user_sessions.get(uid, {})
        if session:
            user_sessions[uid]["state"] = "task_editing"
            user_sessions.touch(uid)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            "✏️ Напиши исправленный вариант задачи\n"
            "_(или пришли голосовое)_",
            parse_mode="Markdown"
        )
        return

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
            user_sessions.touch(uid)
            await query.edit_message_reply_markup(None)
            await query.message.reply_text("✏️ Напиши исправленный текст задачи:")
            return

        # Пользователь переключил приоритет
        if tool in ("set_urgent", "set_normal"):
            new_prio = "urgent" if tool == "set_urgent" else "normal"
            user_sessions[uid]["priority"] = new_prio
            user_sessions.touch(uid)
            is_urgent = (new_prio == "urgent")
            prio_text = "🔴 *Срочно* — выполнится первым" if is_urgent else "⚪ Обычный приоритет"
            title_show = session.get("title", "Задача")
            await query.edit_message_text(
                f"⚡ *Задача готова*\n\n📄 *{title_show}*\n\n{prio_text}\n\n*Выбери агента-исполнителя:*",
                parse_mode="Markdown",
                reply_markup=agent_keyboard(uid, urgent=is_urgent)
            )
            return

        agent_info = AGENTS.get(tool, AGENTS["code"])
        title      = session.get("title", "Задача")
        task_text  = session.get("text",  "")
        priority   = session.get("priority", "normal")

        # Убираем кнопки, показываем прогресс
        prio_suffix = " 🔴" if priority == "urgent" else ""
        await query.edit_message_reply_markup(None)
        msg = await query.message.reply_text(
            f"{agent_info['icon']} Отправляю задачу в *{agent_info['title']}*{prio_suffix}...",
            parse_mode="Markdown"
        )

        # Создаём задачу
        loop = asyncio.get_running_loop()
        ok, gh_path = await loop.run_in_executor(None, create_task, title, task_text, tool, priority)

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

            prio_line = "🔴 *Приоритет: СРОЧНО* — выполнится первым\n" if priority == "urgent" else ""
            await msg.edit_text(
                f"✅ *Задача отправлена!*\n\n"
                f"📌 *{title}*\n"
                f"{agent_info['icon']} Агент: *{agent_info['title']}*\n"
                f"{prio_line}"
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
        loop = asyncio.get_running_loop()
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
            out = safe_md_truncate(out, 3950) + "\n\n_...обрезано — смотри полный отчёт в Obsidian_"
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
            loop    = asyncio.get_running_loop()
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

        loop = asyncio.get_running_loop()
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
                    f"✅ *Заметка сохранена!*\n\n{vault_emoji} {VAULT_DISPLAY.get(vault, vault)}\n📂 `{folder}/{title}.md`\n\nПоявится в Obsidian через ~5 мин.",
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


# ─── Worker callbacks (Отмена / Одобрение инструментов) ──────────────────────

WORKER_CONTROL = "/tmp/claude-worker-control"
import re as _re
_SAFE_ID = _re.compile(r'^[A-Za-z0-9_-]{1,64}$')

def _write_control(filename: str):
    import os
    base = WORKER_CONTROL
    os.makedirs(base, exist_ok=True)
    path = os.path.realpath(os.path.join(base, filename))
    # Containment check — путь должен оставаться внутри WORKER_CONTROL
    if not path.startswith(os.path.realpath(base) + os.sep):
        raise ValueError(f"Path traversal blocked: {filename}")
    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

async def _handle_worker_callback(query, data: str):
    """Обрабатывает кнопки от telegram-worker: отмена и одобрение инструментов."""
    await query.answer()

    # Только владелец бота может управлять воркером
    if query.from_user.id != ADMIN_CHAT_ID:
        await query.answer("Нет доступа", show_alert=True)
        return

    parts = data.split(":")

    if parts[0] == "worker_cancel" and len(parts) >= 2:
        task_id = parts[1]
        if not _SAFE_ID.match(task_id):
            await query.answer("Неверный ID", show_alert=True)
            return
        _write_control(f"cancel_{task_id}")
        await query.edit_message_reply_markup(None)
        await query.message.reply_text("🛑 Сигнал отмены отправлен. Задача остановится через несколько секунд.")

    elif parts[0] == "worker_approve" and len(parts) >= 3:
        task_id, tool = parts[1], parts[2]
        if not _SAFE_ID.match(task_id) or not _SAFE_ID.match(tool):
            await query.answer("Неверные параметры", show_alert=True)
            return
        _write_control(f"approve_{task_id}_{tool}")
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"✅ `{tool}` разрешён. Claude продолжает.", parse_mode="Markdown")

    elif parts[0] == "worker_deny" and len(parts) >= 3:
        task_id, tool = parts[1], parts[2]
        if not _SAFE_ID.match(task_id) or not _SAFE_ID.match(tool):
            await query.answer("Неверные параметры", show_alert=True)
            return
        _write_control(f"deny_{task_id}_{tool}")
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"❌ `{tool}` запрещён. Claude адаптируется.", parse_mode="Markdown")


# ─── Тест ────────────────────────────────────────────────────────────────────

async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/test — полная диагностика всех компонентов системы."""
    msg  = await update.message.reply_text("🔧 Диагностика (1/5)...")
    res  = []
    loop = asyncio.get_running_loop()

    # ── 1. ENV переменные ──
    import os as _os
    groq_key = _os.environ.get("GROQ_API_KEY", "")
    or_key   = _os.environ.get("OPENROUTER_API_KEY", "")
    res.append("*ENV переменные:*")
    res.append(f"{'✅' if GITHUB_TOKEN    else '❌'} GITHUB\\_TOKEN: {'OK' if GITHUB_TOKEN else 'ОТСУТСТВУЕТ'}")
    res.append(f"{'✅' if groq_key        else '❌'} GROQ\\_API\\_KEY: {'OK (' + groq_key[:8] + '...)' if groq_key else 'ОТСУТСТВУЕТ'}")
    res.append(f"{'✅' if or_key          else '❌'} OPENROUTER\\_API\\_KEY: {'OK' if or_key else 'ОТСУТСТВУЕТ'}")
    res.append(f"{'✅' if ADMIN_CHAT_ID   else '⚠️'} ADMIN\\_CHAT\\_ID: {ADMIN_CHAT_ID or 'не задан'}")

    # ── 2. GitHub API ──
    await msg.edit_text("🔧 Диагностика (2/5) GitHub...")
    try:
        r = requests.get(
            f"https://api.github.com/repos/{DEFAULT_REPO}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}, timeout=10
        )
        res.append(f"\n*GitHub API:* {'✅ OK' if r.status_code == 200 else '❌ ' + str(r.status_code)}")
    except Exception as e:
        res.append(f"\n*GitHub API:* ❌ {e}")

    # ── 3. Groq Whisper (тихий тест без скачивания) ──
    await msg.edit_text("🔧 Диагностика (3/5) Groq Whisper...")
    try:
        if not groq_key:
            raise ValueError("GROQ_API_KEY не задан")
        from groq import Groq as _Groq
        _client = _Groq(api_key=groq_key)
        # Создаём минимальный тихий ogg (1 байт заголовок OGG)
        # Вместо этого проверяем что клиент инициализируется
        res.append(f"*Groq Whisper:* ✅ клиент создан (key={groq_key[:8]}...)")
    except Exception as e:
        res.append(f"*Groq Whisper:* ❌ {e}")

    # ── 4. OpenRouter / Claude ──
    await msg.edit_text("🔧 Диагностика (4/5) OpenRouter...")
    try:
        if not or_key:
            raise ValueError("OPENROUTER_API_KEY не задан")
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {or_key}", "Content-Type": "application/json"},
            json={"model": "anthropic/claude-sonnet-4-6", "messages": [{"role":"user","content":"1+1=?"}], "max_tokens": 5},
            timeout=15
        )
        if r.status_code == 200:
            ans = r.json()["choices"][0]["message"]["content"]
            res.append(f"*OpenRouter:* ✅ ответ: '{ans}'")
        else:
            res.append(f"*OpenRouter:* ❌ HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        res.append(f"*OpenRouter:* ❌ {e}")

    # ── 5. ffmpeg (для Reels) ──
    await msg.edit_text("🔧 Диагностика (5/5) ffmpeg...")
    try:
        import subprocess as _sp
        r2 = _sp.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        ver = r2.stdout.split("\n")[0] if r2.returncode == 0 else "не найден"
        res.append(f"*ffmpeg:* {'✅ ' + ver[:40] if r2.returncode == 0 else '❌ не найден (Reels не работает)'}")
    except FileNotFoundError:
        res.append("*ffmpeg:* ❌ не установлен")
    except Exception as e:
        res.append(f"*ffmpeg:* ❌ {e}")

    text = "📊 *Диагностика системы:*\n\n" + "\n".join(res)
    await msg.edit_text(text, parse_mode="Markdown")


async def handle_agents_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/агенты — показывает список всех агентов и что они умеют."""
    user_id = update.effective_user.id
    lines = ["🤖 *AI Агенты системы Axiom:Void*\n"]
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

    loop = asyncio.get_running_loop()
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


async def handle_newclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newclient — пошаговое добавление нового клиента в CRM."""
    user_id = update.effective_user.id
    user_sessions[user_id] = {"state": "nc_name"}
    await update.message.reply_text(
        "➕ *Новый клиент в CRM*\n\n"
        "Шаг 1/4: Введи имя клиента или компании:",
        parse_mode="Markdown"
    )


async def handle_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/invoice — создать Stripe инвойс и отправить ссылку на оплату."""
    user_id = update.effective_user.id
    if not STRIPE_SECRET_KEY:
        await update.message.reply_text(
            "⚠️ *STRIPE_SECRET_KEY не настроен*\n\n"
            "Добавь переменную в Railway → Variables:\n`STRIPE_SECRET_KEY=sk_live_...`",
            parse_mode="Markdown"
        )
        return
    user_sessions[user_id] = {"state": "inv_client"}
    await update.message.reply_text(
        "💳 *Создать Stripe инвойс*\n\n"
        "Шаг 1/3: Введи имя клиента (должен быть в CRM):",
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
            "`/задача Проанализируй конкурентов в нише веб-разработки`\n\n"
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


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/статус — проверяет состояние всех локальных агентов через GitHub API."""
    msg = await update.message.reply_text("🔍 Проверяю статус агентов...")
    res = []

    # ── Читаем session-state.md из digital-brain-vault (там живёт Brain/) ──
    BRAIN_REPO = "rodion2yalanskiy-netizen/digital-brain-vault"
    state_raw = github_get_file_content("Brain/session-state.md", repo=BRAIN_REPO)
    if not state_raw:
        state_raw = github_get_file_content("Система/session-state.md", repo=BRAIN_REPO)
    if state_raw:
        # Берём первые значимые строки (убираем frontmatter)
        state_body = state_raw
        if state_raw.startswith("---"):
            parts_fm = state_raw.split("---", 2)
            if len(parts_fm) >= 3:
                state_body = parts_fm[2].strip()
        state_preview = state_body[:300].strip()
        if state_preview:
            res.append(f"\n🗂 *session-state:*\n```\n{state_preview}\n```")

    # ── Смотрим время последнего коммита local-agent через GitHub API ──
    try:
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
        # Последний коммит к Отчёты/ — показывает активность агента
        r = requests.get(
            f"https://api.github.com/repos/{DEFAULT_REPO}/commits",
            params={"path": "Отчёты", "per_page": 1},
            headers=headers, timeout=10
        )
        if r.status_code == 200 and r.json():
            last = r.json()[0]
            commit_dt = last["commit"]["committer"]["date"]   # ISO 8601 UTC
            author_msg = last["commit"]["message"][:60]
            # Парсим время
            from datetime import timezone
            dt = datetime.fromisoformat(commit_dt.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            diff_min = int((now_utc - dt).total_seconds() / 60)
            icon = "✅" if diff_min < 30 else ("⚠️" if diff_min < 120 else "❌")
            res.append(f"{icon} *local-agent:* последний отчёт {diff_min} мин назад")
            res.append(f"   _{author_msg}_")
        else:
            res.append("❓ *local-agent:* нет данных о последнем коммите")
    except Exception as e:
        res.append(f"❌ *local-agent:* ошибка проверки ({e})")

    # ── Количество задач в очереди ──
    try:
        r2 = requests.get(
            f"https://api.github.com/repos/{DEFAULT_REPO}/contents/%D0%97%D0%B0%D0%B4%D0%B0%D1%87%D0%B8",
            headers=headers, timeout=10
        )
        if r2.status_code == 200:
            files = [f for f in r2.json() if isinstance(f, dict) and f.get("name", "").endswith(".md")]
            res.append(f"\n📋 *Очередь задач:* {len(files)} файл(ов)")
        else:
            res.append(f"\n📋 *Очередь задач:* нет доступа (HTTP {r2.status_code})")
    except Exception as e:
        res.append(f"\n📋 *Очередь задач:* ошибка ({e})")

    # ── Последние отчёты ──
    try:
        r3 = requests.get(
            f"https://api.github.com/repos/{DEFAULT_REPO}/commits",
            params={"path": "Отчёты", "per_page": 3},
            headers=headers, timeout=10
        )
        if r3.status_code == 200 and r3.json():
            res.append("\n📄 *Последние выполненные задачи:*")
            for c in r3.json():
                msg_txt = c["commit"]["message"].replace("Local agent: ", "").replace(" task(s) completed [skip ci]", "")[:50]
                res.append(f"  • _{msg_txt}_")
    except Exception:
        pass

    # ── Статус бота ──
    res.append(f"\n🤖 *Бот:* ✅ работает")
    res.append(f"💬 *Активных сессий:* {len(user_sessions)}")
    res.append(f"🕐 *Время сервера:* {datetime.now().strftime('%H:%M %d.%m.%Y')}")

    text = "📊 *Статус системы Axiom:Void*\n\n" + "\n".join(res)
    await msg.edit_text(text, parse_mode="Markdown")


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
    # Единый guard: блокирует все апдейты от не-владельца (group=-1 выполняется первым).
    app.add_handler(TypeHandler(Update, _guard_owner), group=-1)
    app.add_handler(CommandHandler("start",     handle_start))
    app.add_handler(CommandHandler("menu",      handle_start))
    app.add_handler(CommandHandler("help",      handle_help))
    app.add_handler(CommandHandler("myid",      handle_myid))
    app.add_handler(CommandHandler("test",      handle_test))
    app.add_handler(CommandHandler("reports",   handle_reports))
    app.add_handler(CommandHandler("task",      handle_task_command))
    app.add_handler(CommandHandler("agents",    handle_agents_info))
    app.add_handler(CommandHandler("chat",      handle_chat_command))
    app.add_handler(CommandHandler("status",    handle_status))
    app.add_handler(CommandHandler("newclient", handle_newclient))
    app.add_handler(CommandHandler("invoice",   handle_invoice))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    if not os.environ.get("GROQ_API_KEY"):
        logger.warning("GROQ_API_KEY не задан — транскрипция голоса недоступна")
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.warning("OPENROUTER_API_KEY не задан — анализ через OpenRouter недоступен")
    logger.info("🤖 Axiom:Void Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

