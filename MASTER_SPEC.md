# LIFE OS v3.0 — MASTER_SPEC (рабочая версия для агентов)

Источник: ТЗ владельца от 2026-11-17 (36 агентов, 7 фаз, 12 часов). Здесь оно переписано так, чтобы агенты могли исполнять его без домыслов: что строим, где лежит, что считается готовым, что физически недоступно в этой среде.

## 0. Реальность среды (читать до начала работ)

| Есть | Нет |
|---|---|
| Python 3.13 (`python3`) с fastapi 0.162, uvicorn 0.54, pydantic 2.13, aiogram 3.31, httpx 0.28 | Docker / docker-compose |
| Python 3.9 (`/usr/bin/python3`) — только stdlib (его использует launchd-бот v0.x) | ffmpeg / ffprobe |
| Node 24 + npm 11, npx | faster-whisper / whisper, redis, celery, duckdb, pandas, numpy, PIL, jinja2, watchdog |
| SQLite с FTS5 в обеих версиях Python | публичный домен, сервер, DNS |

Отсюда правила:
1. **Docker-стек не запускаем** — файлы `deploy/` пишем и валидируем статически, в отчёте честно помечаем «не проверено, docker отсутствует».
2. **Транскрибация** — интерфейс `Transcriber` с двумя реализациями: `WhisperTranscriber` (если установлен faster-whisper и ffmpeg) и `NullTranscriber` (возвращает статус `pending_toolchain`). Пайплайн не падает, а показывает в UI «нужен ffmpeg/whisper».
3. **Аналитика** — на stdlib (`statistics`, `sqlite3`); DuckDB не вводим.
4. **Redis/очереди** — заменяем таблицей `jobs` в SQLite + воркер-процессом (простой цикл с `SELECT ... WHERE status='queued'`).
5. **React** собираем Vite'ом, раздаём статикой из FastAPI (`app/web/dist`), сборка — `npm ci && npm run build` в `app/web`.

## 1. Целевая архитектура

```
telegram-life-agent/
  app/                      # новый Life OS v3 (Python 3.13)
    server/                 # FastAPI
      main.py               # сборка приложения, статика, lifespan
      settings.py           # конфиг, пути, секреты из env/файла
      db.py                 # SQLite: схема, миграции, FTS5, WAL
      models.py             # pydantic-схемы API
      vault.py              # Obsidian: чтение/запись, идемпотентные маркеры
      media.py              # приём файлов, метаданные, Range-раздача
      analytics.py          # EMA веса, корреляции, недельный отчёт
      jobs.py               # очередь задач в SQLite + воркер
      transcribe.py         # Transcriber: whisper | null
      api/                  # роутеры: overview, journal, health, courses, analytics, settings
      ws.py                 # WebSocket-хаб событий
    bot/                    # aiogram 3.x: приём сообщений, медиа, интент-роутер
    worker/                 # фоновой процесс: jobs -> транскрибация/конспект
    web/                    # React SPA (Vite): sidebar, pages, player
    tests/                  # pytest: API, БД, vault, analytics, bot-хендлеры
  deploy/                   # docker-compose.prod.yml, Caddyfile, systemd (артефакты, не проверены)
  data/                     # lifeos.db, media/, кэш
  studio/                   # память студии
  src/, dashboard/, demo/   # v0.x — не трогаем до переключения
```

## 2. Модули и критерии приёмки

### 2.1 Фундамент (Фаза 1)
- SQLite-схема v3: `entries` (журнал), `metrics` (вес/жир/мышцы/вода/сон/настроение/вода, EAV с типом и датой), `habits`, `habit_marks`, `goals`, `courses`, `lessons`, `lesson_progress`, `transcripts`, `segments` (таймкоды), `notes`, `jobs`, `settings_kv`. Индексы по дате, FTS5 по `entries.text` и `segments.text`.
- `vault.py`: идемпотентная запись `01 Daily/YYYY-MM-DD.md` (маркер `<!-- lifeos:entry:ID -->`), `Courses/<Курс>/<Урок>.md`, `Health/*.md`; чтение существующих заметок без потери ручных правок.
- Seeder: 120 дней правдоподобных данных (вес 81.4→79.8 с шумом, сон 6.2–8.0, вода, тренировки 3/нед, 2 демо-курса по 6 уроков с конспектами).
- Приёмка: `python3 -m pytest app/tests -q` зелёный; повторный запуск seeder'а не дублирует данные.

### 2.2 FastAPI (Фаза 4)
- `GET /api/overview`, `/api/journal?limit&type&q`, `/api/health/weight?days`, `/api/courses`, `/api/courses/{id}`, `/api/lessons/{id}` (конспект+сегменты), `/api/analytics/correlations`, `/api/settings`, `POST /api/quick` (быстрая запись), `POST /api/water`.
- `GET /media/{lesson_id}` — отдача файла с `Accept-Ranges: bytes`, `206`, `Content-Range`; проверка на traversal.
- `WS /ws` — события `entry.created`, `job.progress`, `sync.updated`.
- Приёмка: pytest-тесты на каждый роутер, Range-тест (запрос `bytes=100-200` → 206 и точный размер), OpenAPI генерируется.

### 2.3 Telegram-бот v3 (Фаза 2–3)
- aiogram 3.x, whitelist по `ADMIN_TELEGRAM_ID`, команды `/start /help /stats`.
- Интент-роутер: свободный текст → LLM (если ключ есть) →结构化 запись; фото/видео/документ → медиа-пайплайн; голос → сохраняется, транскрибация через адаптер.
- Медиа: сохраняем оригинал в `data/media/<course>/<lesson>/`, метаданные в БД, job на транскрибацию/конспект; при отсутствии toolchain статус `pending_toolchain` виден в UI.
- Приёмка: тесты хендлеров с фейковым API aiogram (без сети), один ручной smoke — только если владелец даст токен.

### 2.4 React SPA (Фаза 5)
- Layout: Sidebar 3.0 (4 контура, сворачивание 260↔68px, бейджи, `⌘K`-палитра, `+ Быстрая запись`, футер со статусом синка и темой).
- Страницы: Обзор (bento, 6 KPI + виджеты воды/веса/«продолжить курс»), Курсы (витрина + плеер с таймкодами и поиском по субтитрам), Тело и Весы (EMA-график, состав тела, замеры), Сегодня, Цели, Привычки, Сон, Тренировки, Питание, Журнал, Аналитика, Настройки.
- Стиль: палитра `#F6F7FB / #FFFFFF / #17181C / #7A7D87 / #A18BB7 / #AEDEC2 / #F2C9B6 / #B9CBE8`, радиусы 18–20, системные шрифты, без CDN.
- Приёмка: `npm run build` без ошибок, bundle < 1.5 МБ gzip, все маршруты рендерятся с моковыми данными, `prefers-reduced-motion` уважается.

### 2.5 Аналитика (Фаза 4)
- EMA веса (α по окну 7/14), прогноз даты цели, дельта жир/мышцы за 7/30/90.
- Корреляции: сон↔энергия, вода↔настроение, тренировки↔вес — только при n≥14 и |r|≥0.3, иначе «мало данных».
- Приёмка: юнит-тесты на синтетических рядах с известным ответом.

### 2.6 PWA, безопасность, деплой (Фаза 6)
- manifest + service-worker (offline-оболочка), мобильная нижняя навигация.
- Auth: токен владельца в cookie + `X-LifeOS-Token` для API; всё остальное 401. Никаких паролей в открытом виде в репозитории.
- `deploy/docker-compose.prod.yml`, `Caddyfile`, `DEPLOY_GUIDE.md` — артефакты с пометкой «не проверено».

## 3. Правила для агентов

1. Один файл — один владелец. Не редактировать чужую зону; нужна правка — писать владельцу в задаче.
2. Никаких заглушек без маркера: `TODO` запрещён, допустим `NotImplementedError` с текстом «нужен toolchain: ffmpeg».
3. Все внешние вызовы — с таймаутом и обработкой ошибок; логи без секретов.
4. Тесты — до сдачи: `python3 -m pytest app/tests -q` (3.13) и, если файл попадает в старый проект, `python3 -m unittest discover tests` (3.9).
5. Русский язык в UI и документации. Код и имена — латиницей.
6. Скриншоты не запрашивать: среда текстовая (glm-5.3 падает на картинках), проверки — через DOM-текст, HTTP-коды, тесты.
7. Каждая фаза заканчивается отчётом в `studio/REPORT-v3-phaseN.md` и обновлением `studio/TASK_LEDGER.md`.

## 4. Порядок фаз (что зависит от чего)

1. Ф1 фундамент: `db.py`, `models.py`, `vault.py`, seeder → без него ничего не работает.
2. Ф4 API: читает БД, отдаёт наружу; стартует после Ф1.
3. Ф2–Ф3 бот и медиа: пишет в БД и vault; стартует после Ф1, независимо от API.
4. Ф5 SPA: может верстаться на моках параллельно API, но интеграция — после Ф4.
5. Ф6–Ф7: PWA, авторизация, деплой-артефакты, e2e — после Ф4 и Ф5.

## 5. Что считается «готово» для владельца

- `bash install.sh` поднимает v3 (или v0.x, если v3 ещё не готов) и печатает адрес дашборда.
- Дашборд открывается на `http://127.0.0.1:8787`, показывает реальные данные БД, все разделы кликаются.
- Бот принимает текст/фото/видео и складывает в БД/vault; при отсутствии toolchain честно сообщает, чего не хватает.
- Тесты и `./check.sh` зелёные; в `studio/REPORT-v3-*.md` — что сделано, чем проверено, что осталось.
