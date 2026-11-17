# Life OS v3 — независимая проверка QA (t3)

- **Статус:** готово к приёмке (блокеров P0/P1 нет; замечания P2 — ниже)
- **Дата/время:** 2026-11-17, 13:30–13:46 Europe/Moscow (UTC+3)
- **Коммит:** `ac7d663` + незакоммиченные изменения фронтенда t1/t2 (рабочее дерево)
- **Окружение:** macOS, Python 3.13.14 (`python3`) и 3.9.6 (`/usr/bin/python3`),
  Node v24.22.1 / npm 11.19.1, uvicorn (FastAPI), Chromium (dsh-browser)
- **Что проверял:** результат t1 («Тело»), t2 («Аналитика» + поиск по субтитрам)
  и проект целиком: сборка, тесты, check.sh, deploy-артефакты, живой smoke на
  временной БД, браузерный smoke ключевых страниц.
- **Что не трогал:** `data/lifeos-v3.db` (mtime 11:46 не изменился), порт 8787,
  исходники не правил, в git не коммитил, docker не запускал.

## 1. Сборка SPA — PASS

```text
cd app/web && npm run build     # tsc --noEmit + vite build
```

- `tsc -p tsconfig.json --noEmit` — без ошибок, exit 0.
- `vite build` — 71 модуль, 437 мс, exit 0.

| Файл | Размер | gzip |
|---|---:|---:|
| `dist/index.html` | 2,27 кБ | 1,13 кБ |
| `dist/assets/style-ByOdR65Z.css` | 59,07 кБ | 10,76 кБ |
| `dist/assets/index-Bu8uWgdm.js` | 331,95 кБ | 100,88 кБ |
| **Итого** | **393,3 кБ** | **≈112,8 кБ** (112 403 Б) |

Порог MASTER_SPEC §2.6 «bundle < 1,5 МБ gzip» выполнен с запасом ×13.
Артефакты `dist/`: `index.html` (есть `id="root"`, `<title>Life OS — личный дашборд…`),
`manifest.webmanifest` (1 111 Б, `lang=ru`, `display=standalone`, `start_url=./#/today`),
`sw.js` (2 783 Б), `offline.html` (1 596 Б), иконки 192/512/maskable + apple-touch.

## 2. Тесты — PASS

```text
python3 -m pytest app/tests -q
→ 271 passed, 15 subtests passed in 1.85s, exit 0 (0 failed)

/usr/bin/python3 -m py_compile app/server/*.py app/server/api/*.py app/bot/*.py app/worker/*.py
→ exit 0 (синтаксис Python 3.9 корректен)
```

Ожидание «271+ passed, 0 failed» выполнено ровно: 271 тест, 15 подтестов.

## 3. check.sh — PASS

```text
bash check.sh
→ PASS  11 проверок, ошибок нет, exit 0
```

Все 11 шагов ✓: python3, compileall src, unit-тесты (unittest discover),
`bash -n` install.sh/uninstall.sh, plutil plist, json.tool config,
пустые секреты в примере конфига, секретов в проекте нет, `.gitignore`,
git-индекс без секретов и данных.

## 4. deploy/check-deploy.sh — PASS

```text
bash deploy/check-deploy.sh
→ Итог: ✔ 121  ✖ 0  ! 0, exit 0
```

Статическая проверка артефактов deploy/ (compose, Dockerfile, Caddyfile,
systemd-юниты, .env.example, гигиена). Docker в среде нет — образы и запуск
не проверялись, это ожидаемо.

## 5. Живой smoke на временной БД — PASS

Данные: `TMP=$(mktemp -d)`, `python3 -m app.server.seeder --db "$TMP/qa.db" --days 120 --seed 42`
→ 1369 записей, 824 метрик, 4 привычки, 382 отметки, 3 цели, 2 курса,
12 уроков, 162 сегмента, 15 заметок, 5 прогрессов (БД 876 544 Б).
Сервер: `LIFEOS_DB_PATH="$TMP/qa.db" LIFEOS_API_TOKEN= python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8789`
(готов с первой попытки; порт 8789, живой 8787 не трогал). После проверок
uvicorn погашен, временная БД удалена.

| Запрос | Код | Проверка содержимого |
|---|---:|---|
| `GET /api/health` | 200 | `status=ok`, `db=ok`, `version=3.0.0.dev0` |
| `GET /api/overview` | 200 | `kpis` — 6 штук: streak/checkins/workouts/sleep/water/mood; `insight=null` (см. P2-1) |
| `GET /api/health/weight?days=90&window=7` | 200 | current 79,7 кг, EMA 79,84, 64 точки, `forecast.status=away_from_target` |
| `GET /api/health/composition` | 200 | 3 items: fat/muscle/body_water, у каждого `current`+`deltas` 7/30/90 |
| `GET /api/analytics/correlations?days=30\|90\|180` | 200 ×3 | 3 pairs: sleep↔mood, water↔mood, training↔weight; n=90/90/64, r=0,04, status=weak |
| `GET /api/sections/body?days=90&limit=50` | 200 | `empty=false`, 4 серии (weight/fat/muscle/water), `items=0` |
| `GET /api/courses` | 200 | 2 курса (Нутрициология, Психология и EMDR) |
| `GET /api/courses/1`, `GET /api/lessons/1` | 200 | детали и субтитры отдаются |
| `GET /media/1` + `Range: bytes=0-99` | 404 → **206** | см. P2-2: у уроков сидера `media_path=""`; после привязки `demo/lesson-13.wav` в **временной** БД — `206`, `Content-Range: bytes 0-99/1440044`, 100 Б, `audio/x-wav` |
| `GET /` | 200 | `text/html; charset=utf-8`, 2 272 Б, есть `id="root"`, заголовок SPA |

## 6. Браузерный smoke — PASS

Скриншоты (untracked, для истории): `app/qa_report/screenshots/v3-body.png`,
`v3-analytics.png`, `v3-courses.png`, `v3-overview.png`.

### `#/body` — «Тело и весы»
Title «Тело и весы — Life OS». Видны: «64 дня с весом», текущий вес **79,7 кг**,
EMA **79,9 кг** (окно 7), изменение за 30 дней **−0,3 кг**, цель **90,0 кг**,
прогноз темпа **−0,12 кг/нед**, блок «Прогноз цели» («Движение идёт от цели»),
SVG-график с `role="img"` и `aria-label` «График веса и EMA: 64 точки с 21.08
по 17.11, от 79,5 до 81,2 кг», «Состав тела» — жир 17,1 %, мышцы 35,4 кг,
вода 56,2 %, дельты 7/30/90. Секция «Замеры и записи» честно пишет «Пока нет
записей о теле — добавьте через бота или быструю запись» (у сидера `items=0`).
В тексте DOM нет `NaN`/`undefined`/`null`/`[object Object]`, упавших запросов нет.

### `#/analytics` — «Аналитика»
Title «Аналитика — Life OS». 3 карточки корреляций («Сон ↔ Настроение»,
«Вода ↔ Настроение», «Тренировки ↔ Вес») с r=0,04, n=90/90/64, статусом
«слабая связь», подпись «0 из 3 выше порога», «Корреляция не доказывает
причину», CTA «Смотреть вес и состав тела». Переключатель периода 30/90/180:
`aria-pressed` переключается, подпись меняется на «За 30 дней…», ошибок нет.

### `#/courses` — «Курсы» + поиск по субтитрам
2 курса, открытие курса → 6 уроков, открытие урока → конспект и поиск
(видимый label «Поиск по субтитрам», placeholder «Например: дыхание»).
Проверено вживую: «Найдено 12 из 12» → запрос `сон` → **2 из 12**,
`9:34` (таймкод) → **1 из 12**, `дыхание` → **0 из 12** + «Ничего не найдено» +
кнопка «Сбросить»; «Сбросить» → 12/12; переход «Следующий» на урок 2 —
поле поиска очищено, снова 13/13 (сброс при смене урока работает).
Урок без медиа честно сообщает «У урока нет медиафайла — доступен конспект
и таймкоды» (см. P2-2).

### Обзор (`#/`) — 6 KPI
Маршрут обзора в SPA — `#/` (см. P3-1): видны «Ключевые показатели» ровно
6 KPI — Стрик 120 дней, Чек-ины 6/6, Тренировки 3/3, Сон 7,4 ч, Вода 2 200 мл,
Настроение 3/10; карточки веса, курса, плана дня, активности. Виджет
«Инсайты» пишет «Пока мало данных для корреляций — нужно минимум 14 дней
наблюдений», хотя данных 90+ дней (дефект копирайта, P2-1).

## 7. Найденные проблемы

### P0 — нет
### P1 — нет

### P2-1. Обзор: «мало данных» вместо «связей выше порога нет»
`app/server/api/overview.py:371-384` (`_insight` возвращает инсайт только при
`status == "ok"`, слабые пары игнорирует) + `app/web/src/pages/Overview.tsx:511`
(фолбэк «Пока мало данных для корреляций — нужно минимум 14 дней наблюдений»).
На живых данных 90/64 дня и 3 слабые пары — фраза неверна и противоречит
«Аналитике», которая корректно пишет «0 из 3 выше порога».
**Предложение:** различать «недостаточно данных» и «связей выше порога нет»
(например, использовать статус `weak` с честной подписью).

### P2-2. Демо-сидер не привязывает медиа к урокам
`app/server/seeder.py:679-681` — уроки вставляются с `media_path=""`, хотя
файлы `data/media/demo/lesson-*.wav` в проекте есть. Из-за этого на свежей
демо-БД `GET /media/{id}` → 404, а плеер пишет «У урока нет медиафайла»;
Range-раздача проверена только после ручной привязки файла во временной БД
(206 + `Content-Range` работает корректно). Не блокер, но живой сценарий
«видео/аудио с перемоткой» на демо-данных не воспроизводится.
**Предложение:** прописать в сидере `media_path` для части уроков или
задокументировать это в README/сидере.

### P2-3. Рассогласование версий
`VERSION` = `0.1.2`, `app/__init__.py:10` = `3.0.0.dev0`, `app/web/package.json`
= `3.0.0`, `CHANGELOG.md` сверху `0.3.0-alpha`, `RELEASE_NOTES.md` — `0.1.2`;
`check.sh:190` печатает «Life OS 0.1.2 — проверка проекта». Для релиза v3.0
стоит привести `VERSION`/CHANGELOG/RELEASE_NOTES к 3.0.0.

### P3-1. Маршрута `#/overview` нет (это не дефект)
В задании значился `#/overview`, но в `app/web/src/lib/router.ts:12` маршрут
обзора — `path: '/'` (`#/`). Запрос `#/overview` корректно показывает
«Страница не найдена — Life OS», белого экрана нет. Проверял обзор на `#/`.

### P3-2. Покрытие статусов на демо-данных
Сидер детерминированно даёт только `weak`-корреляции (r=0,04) и
`forecast.status=away_from_target`, поэтому живьём не видны ветки `ok`/
дата прогноза и `insufficient_data`/`no_variance` — они покрыты юнит-тестами
t1/t2 на моках (в этом прогоне тесты зелёные). Замечание к тестовым данным,
не к коду.

## 8. Вердикт

**Готово к приёмке.** Сборка, typecheck, 271 тест, py_compile 3.9, check.sh
(11/11), deploy-проверка (121/0/0), живой API и все четыре проверенные
страницы работают без падений, белых экранов, 401 и `NaN`. Блокеров нет;
P2-1…P2-3 стоит разобрать до коммита/релиза (не влияют на работоспособность).

## 9. Воспроизведение

```bash
ROOT=/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent
cd "$ROOT/app/web" && npm run build
cd "$ROOT" && python3 -m pytest app/tests -q
/usr/bin/python3 -m py_compile app/server/*.py app/server/api/*.py app/bot/*.py app/worker/*.py
bash check.sh
bash deploy/check-deploy.sh
TMP=$(mktemp -d)
python3 -m app.server.seeder --db "$TMP/qa.db" --days 120 --seed 42
LIFEOS_DB_PATH="$TMP/qa.db" LIFEOS_API_TOKEN= python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8789
# браузер: http://127.0.0.1:8789/#/body, #/analytics, #/courses, #/
```

## Пост-фиксы после отчёта (Lead, 2026-11-17 13:55)

| Замечание | Что сделано | Проверка |
|---|---|---|
| P2-1 «мало данных» на Обзоре при 90 днях | `_insight` показывает самую заметную слабую пару (`r`, `n`, порог); фолбэк фронта переформулирован | `test_overview_insight_reports_weak_correlation`, `pytest app/tests` — 272 passed |
| P2-2 `media_path` пуст у сидера | это шаг демо, а не код: `python3 tools/make_demo_media.py --db …` прописан в `app/README.md`; Range проверен на привязанном файле | `206` + `Content-Range` |
| P2-3 версии расходятся | `app/__init__.py` = `3.0.0-alpha`, `CHANGELOG.md` = `[3.0.0-alpha]`; `VERSION` 0.1.2 — версия установленного контура v0.x (D-19) | `/api/health` отдаёт `"version":"3.0.0-alpha"` |
| P3 демо без «сильных» корреляций | принято как есть: ветки `ok`/дата покрыты 15 юнит-тестами на синтетике | `test_analytics.py` |
