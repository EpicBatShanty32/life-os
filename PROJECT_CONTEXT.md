# PROJECT_CONTEXT — что нужно знать каждому в команде

## 1. Где что лежит

- Проект: `/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent/`
  (= `project_root`; все относительные пути считаются от него).
- ТЗ: `ТЗ.md`, контракты: `INTERFACES.md`, план: `PLAN.md`.
- Obsidian пользователя (живая база, НЕ трогать в тестах):
  `/Users/user/Documents/чисто лайфик обсидиан/чисто лайфик`
  - `01 Daily/YYYY-MM-DD.md` — дневные заметки (шаблон `Шаблоны/Шаблон - День.md`:
    `## Главное`, `## Сделал`, `## Смотрел и читал`, `## Мысли`).
  - `Проекты/`, `Заметки/`, `08 Дневник/`, `00 Старт/`.
  - Бэкап базы: `/Users/user/Documents/чисто лайфик обсидиан/Бэкап базы 2026-11-15 перед настройкой`
- Python: `/usr/bin/python3` = 3.13, внешних пакетов нет (только stdlib).
- DSH-окружение: работаем через bash; macOS 27.2.1; таймзона Europe/Moscow.
- LLM для агента: SAMP gateway `https://api.samp.team/v1` (OpenAI-compatible),
  ключ `DDD_API_KEY` лежит в `~/.dsh/.credentials.yaml` (строка `refs: { DDD_API_KEY: sk-... }`),
  dnst не задан в env — `install.sh` подхватит его сам. Модель: `deepseek-v4.1-flash-1017`
  (проверено 2026-11-16: works with tools/stream). `glm-5.3-prime` бывает болен — не использовать.

## 2. Что уже сделано Lead'ом

- OCR и палитры дизайн-скриншотов пользователя (`tools/ocr.js`, `tools/palette.py`).
- ТЗ, INTERFACES, этот файл, PLAN.
- Решение: бот = Telegram Bot API (long polling) + launchd; LLM опционален
  (scripted fallback); дашборд — self-contained HTML без CDN.

## 3. Дизайн-референсы пользователя (важно для dash)

Пользователь прислал 3 скриншота-референса. Выводы OCR:

- **«Life OS by SoloRise»** (главный референс): личная система-дашборд.
  Карточки: Habit tracker, Money/Budget tracker, Goals (progress bars,
  «75%», «3/4 milestones», «60%», «$6,000 / $10,000»), Wellness — Metrics
  dashboard, Sleep tracker (Avg Sleep 7h 30m), Exercise tracker (Workout Days 417,
  streak 14 days, 85% this week), Daily Journal (Mood: Energetic, Focus: High),
  Notes/Quick Notes, Content Planner, календарь месяца и Scheduled.
- Второй скриншот: страница Employee Management System — чистая табличная вёрстка,
  много воздуха, тонкие разделители, мягкие бейджи отделов.
- Третий: маленькая карточка с фиолетовым акцентом (`#a18bb7`).

**Design language (обязательно для dash):**
- Светлая тема, фон почти белый `#F6F7FB` / `#FBFBFD`, карточки `#FFFFFF`,
  радиус 18–20 px, мягкая тень `0 8px 24px rgba(23,24,28,.06)`, рамка 1 px
  `rgba(23,24,28,.05)`.
- Текст `#17181C`, вторичный `#7A7D87`, третичный `#A9ACB6`.
- Акценты пастельные: фиолетовый `#A18BB7` (основной), мятный `#AEDEC2`,
  персиковый `#F2C9B6`, голубой `#B9CBE8`. Заливки акцентов 10–14 %.
- Типографика: системный стек (`-apple-system, "SF Pro Display", "Helvetica Neue"`),
  заголовки 600–700, цифры крупные, подписи 12–13 px, letter-spacing -0.01em.
- Вёрстка: сетка 12 колонок, gap 20 px, карточки разной высоты (bento), заголовок
  с приветствием и датой; KPI-чипы сверху; графики — аккуратные SVG без мусора.
- Никакого «AI slop»: без стоковых градиентов, без неона, без тяжёлых теней,
  без внешних шрифтов (офлайн!). Скругления и воздух — главное.

## 4. Окружение: факты и ограничения

- Telegram Bot API доступен из сети (проверено curl: шлюз отвечает JSON). Бот-токена
  пока НЕТ — пользователь создаст через @BotFather; до этого тесты на моке.
- launchd: `~/Library/LaunchAgents`, `launchctl bootstrap gui/$UID ...` или `load -w`.
  Установка в `~/Library/Application Support/LifeOS`.
- Песочница DSH может запрещать запись вне workspace: тесты — только temp-vault
  внутри `tests/tmp/` или `/tmp`; боевая запись в Obsidian — из launchd.
- `tgsend` (UI-скрипт в Избранное) существует, но для агента не используется:
  двусторонний диалог надёжнее через Bot API.
- DSH-сессии в фоне не живут — фон даёт launchd; поэтому бот — отдельный python-процесс.
- Часовой пояс Europe/Moscow (UTC+3, без перехода на летнее время).

## 5. Технические ограничения команды

- Только Python stdlib (urllib, sqlite3, json, zoneinfo, html, http.server для мока).
- Никаких pip install, npm, CDN, внешних картинок.
- Все модули импортируются как `from src import config` при запуске из project_root:
  в `src/__init__.py` пусто; запуск `python3 -m src.main` или `python3 src/main.py`
  (main.py добавляет project_root в sys.path). Тесты запускаются из project_root:
  `python3 -m unittest discover tests`.
- Логи пишутся в `data/logs/` (в тестах — temp). Никаких секретов в логах.
- Время в UTC ISO с оффсетом (`2026-11-16T14:02:11+03:00`).
