# Axiom:Void Bot — Claude Code Instructions

## 🚀 Начало каждой сессии

1. Прочитай `~/.claude/projects/-Users-rodionyalanskiy/memory/BRAIN-INDEX.md`
2. Прочитай все файлы памяти из индекса (полные пути ниже)
3. Сообщи: **"Память загружена: [список тем]"**
4. После этого жди задачу

### Пути к файлам памяти
```
ИНДЕКС:    ~/.claude/projects/-Users-rodionyalanskiy/memory/BRAIN-INDEX.md
ПРАВИЛО:   ~/.claude/projects/-Users-rodionyalanskiy/memory/memory-rule.md
БОТ:       ~/.claude/projects/-Users-rodionyalanskiy/memory/telegram-bot-knowledge.md
ПРОЕКТ:    ~/.claude/projects/-Users-rodionyalanskiy/memory/qsnera-project.md
ЭКОСИСТ:   ~/.claude/projects/-Users-rodionyalanskiy/memory/ecosystem-setup.md
PIPELINE:  ~/.claude/projects/-Users-rodionyalanskiy/memory/ai-pipeline.md
GUARDIAN:  ~/.claude/projects/-Users-rodionyalanskiy/memory/vault-guardian.md
```

---

## 📁 Этот проект

**Репозиторий:** `rodion2yalanskiy-netizen/qsnera-reels-bot`
**Railway:** автодеплой при push в `main` (~2–3 мин)

| Файл | Назначение |
|------|-----------|
| `bot.py` | Хендлеры, меню, сессии, GitHub API |
| `analyzer.py` | classify_note, preprocess_task, transcribe, Vision |
| `downloader.py` | Скачивание Instagram Reels |

---

## ⚡ Правила работы с этим проектом

- `git add -A` — всегда (emoji в именах папок)
- `git pull --rebase` перед push
- Редактировать файлы через **Read/Edit** инструменты (TCC блокирует shell cp на Desktop)
- Синтаксис проверять: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
- Push в main → Railway автодеплой → тест через 3 мин

---

## 📝 После каждого финального ответа

1. Обнови `telegram-bot-knowledge.md` — добавь новое, мёрдж дублей
2. Обнови `BRAIN-INDEX.md` — дата + описание
3. Добавь в ответ блок: `📝 Память обновлена: [файл] — [что изменено]`
