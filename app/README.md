# Life OS v3 — `app/`

Переписанное ядро: FastAPI-сервер, React-SPA, Telegram-бот на aiogram, воркер
медиапайплайна и движок Obsidian. Старый контур (`src/`, `dashboard/`) остаётся
рабочим и не пересекается с этим каталогом.

```
app/
  server/     FastAPI + SQLite: API, раздача медиа, WebSocket, сидер демо-данных
  web/        SPA: React 19 + TypeScript + Vite, без внешних UI-библиотек
  bot/        Telegram-бот (aiogram): приём текста и медиа
  worker/     Очередь задач: транскрибация, сегменты, конспекты
  tests/      pytest: 272 теста на момент 3.0.0-alpha
  qa_report/  скриншоты и отчёт приёмки
```

## Что нужно

- Python 3.9+ (проверено на 3.9.6 и 3.13).
- Node.js 18+ и npm — только для сборки SPA.
- Ничего, кроме `requirements.txt`: всё остальное — стандартная библиотека.

## Запуск сервера

```bash
cd app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# демо-данные: 120 дней, 2 курса, привычки и цели (идемпотентно)
python3 -m app.server.seeder --db ../data/lifeos-v3.db --days 120

# короткие WAV для плеера курсов (необязательно, для демо)
python3 ../tools/make_demo_media.py --db ../data/lifeos-v3.db

cd ..   # из корня проекта, чтобы импорт app.* работал
LIFEOS_DB_PATH="$PWD/data/lifeos-v3.db" python3 -m uvicorn app.server.main:app \
  --host 127.0.0.1 --port 8787
```

Открыть `http://127.0.0.1:8787`. Если SPA ещё не собрана, сервер отдаст JSON на `/`.

## Сборка SPA

```bash
cd app/web
npm install
npm run dev      # разработка: Vite на 5173, /api,/ws,/media проксируются на 8787
npm run build    # production-сборка в app/web/dist (её и отдаёт сервер)
```

## Бот и воркер

```bash
# токен и chat_id читаются из config/config.json или переменных LIFEOS_TELEGRAM_TOKEN
python3 -m app.bot.main

# воркер: берёт задачи из таблицы jobs (транскрибация и сегменты)
python3 -m app.worker.main
```

## Полезные эндпоинты

| Путь | Назначение |
| --- | --- |
| `GET /api/overview` | обзор дня: КПИ, повестка, инсайт |
| `GET /api/sections/{name}?days=30` | универсальный раздел: `sleep`, `mood`, `journal`, `nutrition`, `workouts`, `body`, `habits`, `goals`, `today`, `analytics`, `settings` |
| `GET /api/journal` | записи с поиском (`?q=`) и пагинацией |
| `GET /api/courses`, `/api/courses/{id}`, `/api/lessons/{id}` | курсы, уроки, таймкоды, прогресс |
| `POST /api/lessons/{id}/progress` | позиция плеера, автозакрытие урока |
| `POST /api/quick`, `POST /api/water` | быстрый ввод |
| `POST /api/habits/{id}/mark`, `POST /api/habits`, `POST /api/goals`, `PATCH /api/goals/{id}` | привычки и цели |
| `GET /media/{lesson_id}` | медиа с Range (`bytes=0-999` → 206) |
| `WS /ws` | события изменений |
| `GET /api/health/weight?days=90&window=7` | ряд веса, EMA, скорость кг/нед и прогноз даты цели |
| `GET /api/health/composition` | жир/мышцы/вода: текущее и дельты за 7/30/90 дней |
| `GET /api/analytics/correlations?days=90` | корреляции сон↔настроение, вода↔настроение, тренировки↔вес |

### Контракты аналитики (v3)

`/api/health/weight` добавляет к ряду и EMA:

```json
{"current": 79.8, "ema": 79.9, "delta": -1.6, "target_kg": 78.0, "rate_per_week": -0.35,
 "forecast": {"status": "ok", "date": "2027-01-15", "weeks": 9.5, "rate_per_week": -0.35,
              "target_kg": 78.0, "current": 79.8}}
```

`forecast.status`: `ok` (есть дата), `reached` (цель достигнута), `flat` (тренда
нет), `insufficient_data` (< 5 замеров за 30 дней), `no_target` (цель по весу
не задана), `away_from_target` (движение идёт от цели). В не-`ok` статусах
`date` и `weeks` — `null`, UI не показывает выдуманную дату.

`/api/health/composition` отдаёт `items[]` с `deltas {"7","30","90"}` и
`counts`; если замеров в окне меньше двух, дельта `null` («мало данных»).

Корреляции считаются только при `n ≥ 14` и `|r| ≥ 0.3`, иначе возвращается
статус `insufficient_data` / `no_variance` / `weak` и понятный текст.

## Тесты

```bash
python3 -m pytest app/tests -q          # 272 теста
/usr/bin/python3 -m py_compile app/server/api/*.py   # синтаксис 3.9
cd app/web && npm run build             # tsc + vite
```

## Демо-данные

Сидер детерминирован (`--seed 42`) и идемпотентен: повторный запуск не
дублирует записи. Удалить демо — `python3 -m app.server.seeder --reset --db …`.
Файлы БД и медиа живут в `data/` и в git не попадают.

## PWA

Собранная SPA устанавливается как приложение: `manifest.webmanifest`, иконки
192/512 (генератор `tools/gen_pw_icons.py`, чистый Python без Pillow), service
worker `sw.js` кеширует хешированные ассеты, а оболочку (`index.html`) всегда
берёт из сети — иначе после деплоя браузер держит старую сборку.
