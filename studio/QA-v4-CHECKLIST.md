# Life OS v4 — чек-лист независимой приёмки (t14)

- **Кто:** qa (независимый QA), задача t14.
- **Статус:** подготовлено 2026-11-17 23:35 MSK; приёмка ещё не проводилась — ждём t14.
- **Источники:** `studio/HANDOFF-OPUS-5.5.md` §10–16, §20; `studio/DESIGN-v4.md`
  §0–6; `MASTER_SPEC.md`; прошлый отчёт `studio/REPORT-v3-qa.md`.
- **Правила:** код не правим; живую базу `data/lifeos-v3.db` не трогаем; сервер —
  только на копии БД (порт 8790, пустая БД — 8791); артефакты — в
  `studio/shots-v4/` и отчёт `studio/REPORT-v4-qa.md`; в git не коммитим.
- **Уровни:** P0 — без этого v4 не сдаётся (Definition of Done §16); P1 —
  требования DESIGN §5–6 и сквозных функций; P2 — замечания, не блокируют.
- **Вердикт:** `pass` только при 0 открытых P0 и 0 P1 (или явно отложенных
  капитаном с записью); любой P0/P1 → `needs_revision` с файлом/селектором и
  воспроизведением.

## 0. Инструменты — проверены до t14 (2026-11-17 23:26–23:35 MSK)

| Инструмент | Команда | Факт |
|---|---|---|
| pytest | `cd app && python3 -m pytest tests -q` | **293 passed, 15 subtests**, 0 failed, 2.28 s (снято 23:26 на рабочем дереве, схема v3 уже была в `db.py`; цель v4 — ≥300) |
| tsc | `cd app/web && npx tsc -p tsconfig.json --noEmit` | exit 0 |
| vite build | `npx vite build --outDir /tmp/qa-lifeos-v4/dist-check --emptyOutDir` | exit 0, 71 модуль; gzip JS+CSS **111 413 Б** |
| check.sh | `bash check.sh` | **PASS 11 проверок**, 0 ошибок |
| deploy | `bash deploy/check-deploy.sh` | **121 ✔, 0 ✖, 0 !** |
| миграция v2→v3 | копия `data/lifeos-v3.db.bak-1794947129` (user_version=2) → `init_db` | user_version=**3**, 41 таблица (вкл. FTS), все новые таблицы на месте |
| сервер 8790 | `LIFEOS_DB_PATH=/tmp/qa-lifeos-v4/qa-8790.db python3 -m uvicorn app.server.main:app --host 127.0.0.1 --port 8790 --log-level warning` | `/api/health` 200 за 18.8 ms, SPA 200 |
| Playwright CLI | `npx playwright@1.49.1 screenshot --viewport-size=... --wait-for-timeout=2500 URL file` | 1440×1000 светлая и тёмная (`--color-scheme=dark`), 390×844 — ок; средняя яркость 241 vs 38 (тема реально меняется) |
| agent-browser | `~/.local/bin/agent-browser 0.43.0 --session <s> --executable-path ~/Library/Caches/ms-playwright/chromium_headless_shell-1148/chrome-mac/headless_shell` | open/snapshot -i/click @eN/type/console/errors/screenshot/vitals/a11y работают; `press Meta+k` открывает палитру |
| axe-аудит | `agent-browser a11y --tags wcag2a,wcag2aa` | база v3 (Обзор): 1 serious (color-contrast, 62 узла), 0 critical, 2 incomplete (aria-prohibited-attr, color-contrast 18) |
| sqlite3 / Pillow | `/usr/bin/sqlite3` 3.54; bundled Python + Pillow 12.3 | есть; Pillow — для проверки скриншотов (размер/яркость) |

Ограничения: docker в среде нет (`check-deploy.sh` это учитывает); живой сервер
владельца на 8787 не трогаем; все записи QA идут только в `/tmp/qa-lifeos-v4/`.

## 1. P0 — Definition of Done (handoff §16)

- [ ] 1.1 `cd app && python3 -m pytest tests -q` → **≥ 300 passed, 0 failed**.
  Приложить хвост вывода и число. На каждый новый модуль — happy path +
  валидация + пустые состояния (проверить наличие тестов по именам).
- [ ] 1.2 `cd app/web && npm run build` → PASS (tsc 0 ошибок). Сумма gzip
  `dist/assets/*.js` + `dist/assets/*.css` **≤ 250 КБ**. Команда:
  `find app/web/dist/assets -type f \( -name '*.js' -o -name '*.css' \) -exec sh -c 'gzip -9c "$1" | wc -c' _ {} \; | awk '{s+=$1} END {print s}'`.
- [ ] 1.3 `bash check.sh` → **11/11 PASS**; `bash deploy/check-deploy.sh` →
  **0 провалов** (кроме docker-строк, если появятся, — с пояснением).
- [ ] 1.4 Живой smoke на копии реальной базы: v2-бэкап → `init_db` → v3;
  все новые эндпоинты из §2 отвечают 200; SPA открывается. Повторный
  `init_db`/старт сервера идемпотентен, данные не теряются (сверить count
  `entries`, `habits`, `habit_marks`, `courses` до/после).
- [ ] 1.5 `python3 -m app.server.seeder --db <temp>.db --days 120` дважды →
  счётчики ключевых таблиц одинаковые, дублей нет, `source='telegram'` и
  пользовательские строки не тронуты.
- [ ] 1.6 Скриншоты в `studio/shots-v4/`: 10 экранов × {1440, 390} ×
  {light, dark} = 40 файлов; размеры и непустоту проверить (см. §7).
- [ ] 1.7 Ручной UX-чек-лист: клавиатурный путь «добавить задачу → отметить →
  чек-ин → итоги»; ⌘K находит сущности; фильтры сохраняются в URL (§6).
- [ ] 1.8 Отчёт `studio/REPORT-v4-qa.md`: что проверено, вывод команд, что не
  сделано и почему, риски, вердикт P0/P1/P2.
- [ ] 1.9 Definition of Done модуля (для каждого нового модуля): данные → API
  с тестами → экран → пустое состояние → мобильная версия → тёмная тема →
  скриншот.

## 2. P0 — API-контракт (handoff §12)

Все ответы — JSON без обёртки; ошибки `{"detail": "..."}` 4xx. Без
`LIFEOS_API_TOKEN` auth выключен; новые эндпоинты — под `/api/*`.

- [ ] 2.1 Сферы: `GET/POST/PATCH/DELETE /api/spheres`; архив при DELETE,
  если используется.
- [ ] 2.2 Компас: `GET /api/compass?date=` → `spheres, goals[], antiGoals`;
  у цели `progressPct` считается как доля закрытых вех; если `progress>0` —
  ручной. CRUD целей, вех, анти-целей.
- [ ] 2.3 Дела: `GET /api/tasks` c фильтрами `date/view/from/to/sphereId/
  projectId/resource/status/listKind/q`; `POST/PATCH/DELETE`; `complete`
  возвращает `{task,next?}`; повторяющаяся задача создаёт следующее вхождение;
  невалидное правило не роняет запрос. `POST /api/tasks/{id}/reschedule`.
- [ ] 2.4 Инбокс: `GET /api/inbox?status=`, `POST`, `POST /{id}/convert`
  (`task|project|personal|book|delete`) — элемент уходит из инбокса,
  `DELETE`.
- [ ] 2.5 Проекты: список/создание/карточка `{project, steps, notes, tasks}`,
  PATCH, DELETE (409 при наличии задач), этапы, заметки/ссылки.
- [ ] 2.6 Библиотека: полки `reading|queue|done|dropped|all`, `GET /api/books/{id}`
  = `{book, insights, quotes, actions}`, progress, insights/quotes,
  `POST /api/book-actions/{id}/to-task` создаёт задачу и связывает с книгой;
  `GET /api/learning`, `PATCH /api/courses/{id}`.
- [ ] 2.7 Финансы: `GET /api/finance/summary?month=` (income/expense/balance,
  byCategory, budgets, daily); транзакции CRUD; деньги: в запросе `amount`
  рубли, округление до копеек, в ответе `amount` + `amountKopecks` (int).
  Проверить `POST 1250.5` → `125050` коп. Счета/категории/бюджеты.
- [ ] 2.8 Личное: `GET /api/personal/summary`, `POST /api/personal/checkin`
  (upsert по дате), `GET/POST /api/personal/review`, `GET /reviews`.
- [ ] 2.9 Аналитика: `GET /api/analytics/summary?days=` (energy/spheres/
  reading/finance/habits/tasks/correlations); старый `/api/analytics/correlations`
  не изменён.
- [ ] 2.10 Поиск: `GET /api/search?q=&limit=` → `items[{kind,id,title,
  snippet,route}]`, kind ∈ task/project/book/quote/goal/inbox/note; пустой q
  не падает.
- [ ] 2.11 Экспорт: `GET /api/export?format=json|md` → файл,
  `Content-Disposition: attachment`, JSON парсится, MD содержит заголовки.
- [ ] 2.12 Старые API не сломаны: `/api/overview`, `/api/sections/{name}`,
  `/api/settings`, `/api/health`, `/api/analytics/correlations`, `/api/courses*`,
  `/api/journal`, `/media/*` — 200 и прежние ключи; auth-тесты зелёные.
- [ ] 2.13 Auth: при `LIFEOS_API_TOKEN` `/api/*` (кроме `/api/health`) без
  токена → 401/403, с `X-LifeOS-Token` → 200 (проверить на временном
  перезапуске 8790, затем вернуть без токена).

## 3. P0/P1 — фронтенд v4 и DESIGN §5–6

### 3.1 Оболочка и экраны
- [ ] 3.1.1 Сайдбар 248/64 px, hairline, без тени; лепесток 30 px, «Life OS» +
  «личный контур», **без версии/сборки**; низ: статус-точка + «Синхронизировано»,
  переключатель темы из трёх иконок 26 px без подписей, «Настройки» строкой.
- [ ] 3.1.2 Верхняя полоса 56 px: хлебная крошка, поиск ⌘K, одна акцентная
  кнопка «Быстрая запись». Акцент один на экран.
- [ ] 3.1.3 Все экраны §16.6 существуют и живые на демо-данных: Обзор, Компас,
  Сегодня, Проекты, Библиотека, книга (drawer), Финансы, Личное, Итоги,
  Аналитика; дополнительно Инбокс, Неделя. Старые (Курсы/Тело/Сон/Тренировки/
  Питание/Журнал/Настроение/Настройки) функциональны.
- [ ] 3.1.4 Примитивы из DESIGN §3 есть и используются (`Card`, `Section`,
  `Segmented`, `Button`, `Field`, `ProgressBar`, `EmptyState`, `Sheet`,
  `Table`, `Chip`, `Kpi`); карточки без тени, радиус 14.
- [ ] 3.1.5 Адаптив 390/768/1024/1440: нет горизонтального переполнения
  (`document.documentElement.scrollWidth <= innerWidth + 1`), на 390 — нижняя
  навигация 5 пунктов + «Ещё» (шторка).

### 3.2 Состояния (DESIGN §5)
- [ ] 3.2.1 Пустые состояния слева, с действием («Добавить первую задачу →»),
  а не «Нет данных» — на пустой БД (сервер 8791) по всем новым экранам.
- [ ] 3.2.2 Ошибка блока не валит экран: при обрыве одного API соседние блоки
  живут, в блоке «Не удалось загрузить · Повторить» (проверить
  `agent-browser network route ... --abort` + reload).
- [ ] 3.2.3 Загрузка — скелетон 2–3 строки при первом рендере, без спиннеров.
- [ ] 3.2.4 Действия оптимистичные: чекбокс/статус меняется до ответа сервера;
  при ошибке — тост снизу и откат.
- [ ] 3.2.5 Клавиатура: весь путь без мыши; Enter — сохранить, Esc — закрыть;
  фокус видимый и возвращается после закрытия слоя; `role="dialog"` +
  `aria-modal`; `aria-live="polite"` у тостов; label полей связан через `id`.
- [ ] 3.2.6 Контраст: axe color-contrast — не хуже базы v3 (0 critical;
  serious-узлы перечислить; на новых экранах основной текст ≥ 4.5:1).
- [ ] 3.2.7 Тёмная тема полноценная: `data-theme="dark"`, фон `#121211`,
  скриншоты, contrast-аудит; без «светлых островов».

### 3.3 Запреты (DESIGN §6)
- [ ] 3.3.1 Нет инлайн-hex в компонентах/страницах: `grep -rnoE '#[0-9a-fA-F]{3,8}' app/web/src/components app/web/src/pages` — пусто.
- [ ] 3.3.2 Нет эмодзи-иконок в UI (эмодзи допустимы только как данные привычки).
- [ ] 3.3.3 Нет «сборка», номера версии, внутренних меток в интерфейсе:
  `grep -rn "сборка\|Сборка\|Ф5\|3\.0\.0-alpha\|v3 ·" app/web/src` — пусто
  (допустимо только «Настройки → О программе»).
- [ ] 3.3.4 Нет градиентных «блобов», стеклянных карточек, центрированных hero;
  тени только у всплывающих слоёв.
- [ ] 3.3.5 Числа в таблицах/KPI — `font-variant-numeric: tabular-nums`
  (проверить computed style у `.kpi__value`/таблицы).
- [ ] 3.3.6 Шрифты/иконки/аналитика — только локальные, без CDN.

## 4. P1 — сквозные функции

- [ ] 4.1 ⌘K: дебаунс ~120 мс, отмена запросов, группы по типу, Enter —
  переход; ищет задачи/проекты/книги/цитаты/заметки/цели/инбокс.
- [ ] 4.2 Фильтры списков сохраняются в URL-хэше; после reload состояние
  фильтров восстанавливается.
- [ ] 4.3 Экспорт JSON/MD из Настроек работает (кнопка + эндпоинт).
- [ ] 4.4 PWA: `/manifest.webmanifest`, `/sw.js`, `/offline.html`, иконки
  192/512/maskable — 200; `start_url`, `display=standalone`; кэш API не сломан.
- [ ] 4.5 Производительность: первый экран ≤1.5 с (`agent-browser vitals`:
  TTFB/FCP/LCP), API локально p95 < 50 мс (20 замеров curl), отклик действий
  < 100 мс и оптимистичность (см. 3.2.4).
- [ ] 4.6 Версия: `app/__init__.py` → `4.0.0-alpha`, запись в `CHANGELOG.md`;
  `VERSION` (v1-контур) не переиспользован.
- [ ] 4.7 Python 3.9-совместимость синтаксиса:
  `/usr/bin/python3 -m py_compile app/server/*.py app/server/api/*.py app/bot/*.py app/worker/*.py`
  → exit 0.
- [ ] 4.8 Секреты/данные: `git status` без `.db`/медиа/секретов; check.sh
  зелёный; живая база не изменена QA (сверить shasum до/после); Obsidian не
  тронут.
- [ ] 4.9 Reduced motion: при `prefers-reduced-motion: reduce` переходы
  0.01ms (выборочно computed style).

## 5. P2 — наблюдения (не блокируют)

- [ ] 5.1 axe incomplete (`aria-prohibited-attr` на `.streak`, contrast-
  incomplete) — перечислить, если осталось.
- [ ] 5.2 Демо-медиа: аудио курсов без `tools/make_demo_media.py` даёт 404 —
  известное, не дефект кода.
- [ ] 5.3 Прочие мелочи (типографика, отступы, пустые подписи) — списком.

## 6. Пустая БД и сценарии ошибок (дополнительно к 3.2)

```bash
cd /Users/user/Documents/deepseek-harness/default-workspace/life-os
# пустая БД: файла нет — миграции создадут схему v3
LIFEOS_DB_PATH=/tmp/qa-lifeos-v4/empty.db python3 -m uvicorn app.server.main:app \
  --host 127.0.0.1 --port 8791 --log-level warning
```
- [ ] 6.1 Все новые экраны на пустой БД: пустые состояния с действиями,
  без падений и без «NaN».
- [ ] 6.2 Ошибка блока: `agent-browser network route "**/api/tasks*" --abort`,
  reload → блок с «Повторить», остальные живы.
- [ ] 6.3 Невалидный ввод: сумма `abc` в финансах, пустое название задачи,
  плохое правило повторения — внятная ошибка у поля, сервер не 500.

## 7. Скриншоты и их проверка

Матрица: `studio/shots-v4/<screen>-<1440|390>-<light|dark>.png`, обязательные
10 экранов по DoD: `overview, compass, today, projects, library, book, finance,
personal, review, analytics` (книга — drawer через agent-browser); бонусом
`week, inbox` (ещё 8 файлов).

```bash
cd /Users/user/Documents/deepseek-harness/default-workspace/life-os
SHOTS=studio/shots-v4
npx playwright@1.49.1 screenshot --viewport-size="1440,1000" \
  --wait-for-timeout=2500 "http://127.0.0.1:8790/#/compass" \
  "$SHOTS/compass-1440-light.png"
npx playwright@1.49.1 screenshot --viewport-size="390,844" \
  --wait-for-timeout=2500 --color-scheme=dark "http://127.0.0.1:8790/#/compass" \
  "$SHOTS/compass-390-dark.png"
```
- [ ] 7.1 ≥ 40 файлов для 10 обязательных экранов, размеры 1440×1000 / 390×844
  (`sips -g pixelWidth`).
- [ ] 7.2 Непустые: размер > 30 КБ; светлая/тёмная отличаются средней
  яркостью (Pillow: light > 180, dark < 90).
- [ ] 7.3 На скриншотах нет обрезанных блоков/горизонтального скролла
  (визуальная сверка + 3.1.5).

## 8. Что приложить к отчёту

- [ ] Хвост `pytest -q` (число passed) и `npm run build` (exit 0), сумма gzip.
- [ ] `check.sh` (11/11) и `check-deploy.sh` (0 ✖).
- [ ] Таблица API-smoke (эндпоинт → код → ключи/ошибки).
- [ ] Таблица axe по экранам (critical/serious/incomplete) и vitals.
- [ ] Список файлов скриншотов + проверка размеров/яркости.
- [ ] `git status --porcelain` и `shasum data/lifeos-v3.db` до/после QA.
- [ ] Выводы P0/P1/P2 и вердикт; риски и что осталось.

## 9. Воспроизведение приёмки (последовательность)

```bash
cd /Users/user/Documents/deepseek-harness/default-workspace/life-os
cp data/lifeos-v3.db /tmp/qa-lifeos-v4/qa-8790.db          # копия, не живая база
cd app && python3 -m pytest tests -q                        # ≥300 passed
cd ../web && npm run build                                  # tsc + vite
cd ../.. && bash check.sh && bash deploy/check-deploy.sh
# сервер на копии (порт 8790), затем smoke API + скриншоты + axe;
# перед t14 сервер перезапустить, чтобы подхватить актуальный Python-код
# (статика dist читается с диска — перезапуск для неё не нужен)
# миграция v2→v3 на копии бэкапа, сид дважды на temp, пустая БД на 8791
```
