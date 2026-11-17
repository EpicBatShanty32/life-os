# Life OS — передача проекта (handoff для Opus 5.5)

Дата: 2026-11-17. Автор: ведущий агент сессии Life OS (DeepSeek Harness).

Это единственный документ, который нужен, чтобы войти в проект без остального
контекста. Сначала прочитай разделы 1–5 (что есть), затем 6–8 (что не так и
куда идём), затем 9–14 (что именно строить). В конце (раздел 21) — готовое
сообщение, которое можно вставить в чат.

---

## 1. Продукт в одном абзаце

**Life OS** — личная операционная система одного человека: Telegram-бот, куда
пишут мысли, воду, сон, тренировки и планы, плюс локальный веб-дашборд, который
показывает день, тело, знания и аналитику. Всё крутится на Mac владельца,
данные лежат в SQLite, часть записей зеркалируется в Obsidian. Живого бота
пока не запускали: нет токена. Дашборд работает.

Проект прошёл путь v1 (бот на stdlib) → v3 (FastAPI + React SPA). Сейчас
нужен **v4**: дизайн уровня студии + стратегический и рабочий контур
(цели, проекты, задачи, книги, финансы, рефлексия).

---

## 2. Пользователь

- Не программист. Общается по-русски, коротко. Ценит «просто работает» и
  внешний вид; технические детали не читает.
- Один владелец, один Mac (macOS 27.2.1, 16 ГБ, Europe/Moscow).
- Obsidian-база: `/Users/user/Documents/чисто лайфик обсидиан/чисто лайфик`.
  **Не трогать без явной просьбы.** Тесты пишут только во временные каталоги.
- Личные задачи и данные владельца — не выдумывать и не перезаписывать.

---

## 3. Что уже работает (факты, проверено)

### 3.1. Репозиторий

```
/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent
```

Git: ветка `main`, последний коммит `7534148` («docs: скриншоты живого
дашборда…»). Рабочее дерево почти чистое; не закоммичены новые ассеты бренда
(см. 8) и `tools/brand_assets.py`.

### 3.2. Два контура (важно не перепутать)

| Контур | Где | Что это | Статус |
|---|---|---|---|
| **v1** | `src/`, `dashboard/`, `install.sh`, `ops/`, `tests/` | старый бот на stdlib + self-contained HTML-дашборд + launchd | рабочий, 150 unit-тестов, не трогаем без нужды |
| **v3** | `app/` | FastAPI + SQLite + React 19 SPA + aiogram-бот + воркер + Obsidian-движок | текущий продукт, 272 pytest |

Новые фичи делаются **в `app/`**. Старый контур не удалять.

### 3.3. Цифры (последняя независимая проверка)

- `app/tests`: **272 passed**, 0 failed (pytest, Python 3.13; совместимость
  с 3.9 проверяется отдельно через `/usr/bin/python3`).
- `check.sh`: **11/11 PASS**.
- `deploy/check-deploy.sh`: **121 проверка, 0 провалов**.
- SPA: сборка PASS, **≈112,8 КБ gzip** (бюджет 1,5 МБ).
- Живой smoke: все API 200, SPA отдаётся, Range-медиа 206.
- Скриншоты приёмки: `app/qa_report/screenshots/` и `studio/screenshots/`.

### 3.4. Что реально готово в SPA

- **Обзор** (`/`) — 6 KPI, серия 120 дней, вода, вес, курс, повестка. Реальная.
- **Тело и весы** (`/body`) — EMA-график, состав тела, прогноз цели. Реальная.
- **Аналитика** (`/analytics`) — корреляции, переключатель 30/90/180. Реальная.
- **Курсы** (`/courses`) — плеер, субтитры-таймкоды, поиск по транскрипту,
  прогресс. Реальная.
- **Настройки** (`/settings`) — токен бота, тема, данные. Реальная.
- **Сегодня, Цели, Привычки, Сон, Тренировки, Питание, Журнал, Настроение** —
  работают через универсальную заготовку `SectionPage` (данные из
  `/api/sections/{name}`), то есть функционально живы, но выглядят как один и
  тот же шаблон. Их надо заменить настоящими экранами.

### 3.5. Бэкенд v3 (что уже есть)

`app/server/`:

- `main.py` — фабрика FastAPI, auth по `LIFEOS_API_TOKEN` (пусто = localhost
  без auth), CORS только localhost, раздача `app/web/dist`, WebSocket.
- `db.py` — SQLite WAL + `foreign_keys=ON` + FTS5, миграции по
  `PRAGMA user_version` (`SCHEMA_VERSION = 2`), FTS-таблицы `entries_fts`,
  `segments_fts`.
- `api/` — `overview.py`, `health.py` (вес и состав тела), `aggregates.py`,
  `analytics.py` (корреляции), `courses.py`, `journal.py`, `quick.py`,
  `sections.py`, `settings.py`, `deps.py`.
- `seeder.py` — детерминированные демо-данные на 120 дней (seed 42),
  идемпотентно, помечены `source='demo'`.
- `media.py` — отдача аудио с Range.
- `jobs.py`, `ws.py`, `vault.py`, `transcribe.py` — воркер и Obsidian.

`app/bot/` — aiogram-бот: текст и медиа, LLM через SAMP-шлюз, fallback на
скрипты. `app/worker/` — очередь транскрибации.

### 3.6. Запущенный дашборд

Сейчас на `http://127.0.0.1:8787` работает uvicorn с демо-базой
`data/lifeos-v3.db` (120 дней, 2 курса). Процесс отвязан от сессии.

---

## 4. Как запустить и проверить

Из корня проекта:

```bash
# 1. Сервер (из корня, чтобы импорт app.* работал)
cd /Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent
LIFEOS_DB_PATH="$PWD/data/lifeos-v3.db" python3 -m uvicorn app.server.main:app \
  --host 127.0.0.1 --port 8787 --log-level warning

# 2. Сборка SPA
cd app/web
npm run build          # tsc + vite → app/web/dist; сервер отдаёт dist

# 3. Тесты бэкенда (из app/)
cd /Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent/app
python3 -m pytest tests -q

# 4. Проверки проекта (из корня)
bash check.sh
bash deploy/check-deploy.sh

# 5. Демо-данные (идемпотентно; можно на временную БД)
python3 -m app.server.seeder --db data/lifeos-v3.db --days 120
python3 tools/make_demo_media.py --db data/lifeos-v3.db   # аудио для курсов
```

Python: `python3` в PATH = 3.13 (`/Library/Frameworks/Python.framework/...`) с
установленными зависимостями. `/usr/bin/python3` = 3.9.6 — используется только
для проверки совместимости синтаксиса (у него нет fastapi). Node 24.22.1,
npm 11.19.1.

**Перед любой миграцией БД** сделай бэкап:

```bash
cp data/lifeos-v3.db "data/lifeos-v3.db.bak-$(date +%s)"
```

---

## 5. Где что лежит

```
app/
  server/        FastAPI + SQLite: API, миграции, сидер, медиа, WS
  web/           React 19 + TS + Vite, БЕЗ внешних UI-библиотек
    src/pages/   страницы (Overview, Body, Analytics, Courses, Settings + заготовки)
    src/components/  Sidebar, Topbar, ThemeSwitch, CommandPalette, Toaster…
    src/styles/  tokens.css, base.css, layout.css, pages.css, …
    public/      иконки, manifest, sw.js, offline.html, логотип (см. 8)
  bot/           aiogram-бот
  worker/        воркер задач
  tests/         pytest
  qa_report/     скриншоты и отчёт приёмки
brand/           logo-source.png (исходник логотипа от владельца)
tools/           brand_assets.py, make_demo_media.py, ocr.js, palette.py
data/            lifeos-v3.db (демо), media/
studio/          память проекта: отчёты фаз, план, QA, архитектура
```

Полезные документы (читать по необходимости, не все сразу):
`app/README.md`, `INTERFACES.md`, `ТЗ.md`, `MASTER_SPEC.md`,
`studio/ARCHITECTURE_MAP.md`, `studio/REPORT-v3-phase*.md`,
`studio/REPORT-v3-qa.md`.

---

## 6. Задача v4 (что просит владелец)

Дословно суть: «это уже рабочий прототип, но дизайн палится как ИИ-слоп;
используй логотип; низ сайдбара выглядит плохо; расширь аналитику и базу
знаний — добавь финтрекер, цели и т.д.».

Три направления:

1. **Дизайн уровня студии** — убрать «ИИ-слопность», сделать узнаваемый
   характер, использовать присланный логотип, переделать низ сайдбара.
2. **Функциональные модули** — Компас (стратегия и цели), Дела (инбокс,
   проекты, задачи по ресурсу), Библиотека (книги, цитаты, обучение), Личное
   (энергия, привычки, итоги недели), Финансы (транзакции, бюджеты), сводная
   Аналитика.
3. **Сквозное** — глобальный поиск ⌘K, фильтры, экспорт, уведомления,
   адаптив/PWA, отклик <100 мс.

Целевая версия: **v4.0.0-alpha**. Старые API и страницы не ломать.

---

## 7. Дизайн: что именно не так и как надо

### 7.1. Диагноз «ИИ-слопа»

Сейчас интерфейс собран из типовых решений, по которым сразу видно
сгенерированный UI:

- одинаковые белые карточки с одинаковой тенью и радиусом 18–20 px на всех
  экранах;
- пастельные плашки-«пилюли» и эмодзи там, где нужна типографика;
- крупные заголовки без иерархии, много воздуха при этом плотные списки;
- акцентный фиолетовый размазан по интерфейсу (кнопки, ссылки, активные
  состояния одного оттенка);
- подписи-капсы с трекингом везде, включая места, где они не нужны;
- блок темы в сайдбаре как «сегмент-контрол» с текстом и версией сборки;
- скругления и тени не несут смысла: всё выглядит «мягким» одинаково.

### 7.2. Желаемый характер

Рабочая метафора: **тихая студия / бумажный ежедневник с точной сеткой**.
Не «дашборд из шаблона», а инструмент, к которому возвращаются каждый день.

Правила, которые надо соблюсти:

- **Иерархия важнее декора.** Максимум 3 уровня текста на экран: заголовок
  экрана, заголовок блока, тело. Подписи — 12–13 px, только где помогают.
- **Один акцент.** Фиолетовый (`--accent-violet`) — только для активного
  состояния/фокуса и одной главной кнопки на экран. Остальное — нейтральные
  поверхности, линии и типографика. Пастель (mint/peach/blue) — только
  маркеры категорий и мини-графики, никогда не заливка больших блоков.
- **Тени почти не нужны.** Разделяй слои линиями (`--border`), фоном
  (`--surface-soft`) и отступами. Тень — только у всплывающих слоёв
  (поповер, палитра, drawer).
- **Радиусы по смыслу:** 8–10 px у полей и строк, 14–16 px у карточек,
  20 px — только у больших контейнеров. Не всё подряд 20 px.
- **Сетка 4/8 px.** Вертикальный ритм 8/12/16/24/32/48. Контент — 12 колонок,
  bento только на Обзоре и Аналитике.
- **Цифры — моноширинные** (`--font-num`), табличные. KPI: число крупно,
  подпись мелко, дельта — знаком и цветом только если это хорошо/плохо.
- **Пустые состояния** — не «Нет данных», а что сделать, чтобы появились.
- **Микроанимации** 120–180 мс, только opacity/transform, уважать
  `prefers-reduced-motion`.
- **Контраст** — WCAG AA (4.5:1 для текста). Фокус — видимый, не только
  `outline: none`.

Токены уже есть в `app/web/src/styles/tokens.css` (палитра, тени, радиусы,
типографика, тёмная тема). Их можно и нужно развивать, но не ломать
существующие страницы.

### 7.3. Низ сайдбара (конкретная жалоба + скриншот)

Сейчас внизу: строка синхронизации, затем крупный сегмент-переключатель
«Светлая / Тёмная / Авто» с подписями, затем строка «Настройки» с шестерёнкой,
затем серый текст «Life OS v3 · сборка Ф5».

Владельцу не нравится: выглядит как настройки из конструктора, «сборка Ф5»
торчит в интерфейсе, сегмент слишком тяжёлый.

Как надо:

- Убрать текст «Life OS v3 · сборка Ф5» из сайдбара совсем (версию показывать
  только в «Настройки → О программе»).
- Тема — компактный переключатель **только из иконок** (☀ ☾ ◐) высотой
  ~28 px, три равных сегмента, без подписей, с тултипами и `aria-label`.
  Активный — заливка `--surface` + тонкая линия, без тяжёлой тени.
- «Настройки» — обычная строка навигации (как другие пункты), а не отдельный
  крупный блок; шестерёнка 18 px.
- Строку синхронизации оставить, но тише: точка статуса 6 px + текст 12 px,
  без рамки и фона; при ошибке — цвет `--danger` и тултип.
- В свёрнутом сайдбаре: иконка темы циклически переключает режим, настройки —
  иконка с тултипом.
- Сверху сайдбара: логотип-лепесток 34 px (см. 8), рядом «Life OS» и
  подпись «личный контур» без версии.

### 7.4. Скиллы, которые стоит загрузить

Если доступны: `frontend-design`, `high-end-visual-design`, `minimalist-ui`,
`redesign-existing-projects`, `web-design-guidelines`, `data-viz`,
`apple-design`. Они дают язык и правила, но финальные решения — за тобой:
дизайн должен быть цельным, а не «набором советов».

---

## 8. Логотип и айдентика

Владелец прислал логотип: **стеклянный лавандовый лепесток** (градиент
фиолетовый → белый → голубой, прозрачный фон). Лежит в репозитории:

```
brand/logo-source.png            # исходник, 1262×1246, RGBA
app/web/public/logo.png          # обрезанный мастер, 739×807
app/web/public/logo-256.png      # для UI/сайдбара
app/web/public/icon-192.png
app/web/public/icon-512.png
app/web/public/icon-512-maskable.png   # 512, фон #F6F7FB
app/web/public/apple-touch-icon-180.png
app/web/public/favicon-32.png
```

Скрипт сборки ассетов: `tools/brand_assets.py` (stdlib + `sips`), запуск:

```bash
python3 tools/brand_assets.py --src brand/logo-source.png --out app/web/public
```

Что сделать:

- заменить текущий SVG-«LogoMark» (три белые палочки на градиентном квадрате)
  на изображение лепестка; в сайдбаре — 34 px, в мобильной шапке — 28 px;
- обновить `index.html`: `favicon-32.png`, `apple-touch-icon-180.png`,
  убрать data-URI SVG-иконку;
- обновить `manifest.webmanifest`: иконки с новыми файлами, `theme_color`,
  `background_color` — под финальную палитру;
- продумать, как лепесток живёт на тёмной теме (не терять свечение, не
  «выжигать» глаз): допустим лёгкий drop-shadow/подложка-градиент 1–2 %;
- **не растягивать** логотип, не перекрашивать, не добавлять текст внутрь.

---

## 9. Информационная архитектура v4

Левое меню — группы, внутри до 4 пунктов. Мобильная нижняя навигация — 5
пунктов + «Ещё» (шторка со всеми разделами).

```
Обзор                /            (главная, bento)
Компас               /compass     (сферы, год/квартал/месяц, анти-цели)
Дела
  Сегодня            /today       (жёсткие события + гибкие задачи по ресурсу)
  Неделя             /week        (канбан / календарная сетка, переключатель)
  Инбокс             /inbox       (парковка идей, триаж)
  Проекты            /projects    (карточки, чек-листы, заметки, ссылки)
База знаний
  Библиотека         /library     (полки книг, карточка книги, цитаты)
  Курсы              /courses     (существующая страница + метаданные)
  Журнал             /journal     (записи из бота/Obsidian)
Здоровье
  Тело и весы        /body
  Сон                /sleep
  Тренировки         /workouts
  Питание            /nutrition
  Привычки           /habits
  Настроение         /mood
Финансы              /finance     (обзор месяца, транзакции, бюджеты)
Личное
  Ресурс             /personal    (чек-ин энергии, личные списки)
  Итоги              /review      (мастер недели/месяца)
Аналитика            /analytics   (сводная: энергия, сферы, чтение, деньги)
Настройки            /settings
```

Маршрут — hash (`#/compass`), как сейчас. Детальные карточки (книга, проект)
открываются как **drawer/боковая панель поверх списка**, а не отдельным
маршрутом — чтобы не терять контекст и работать быстрее.

---

## 10. Модули и требования

Приоритеты: **P0** — без этого v4 не сдаётся; **P1** — очень желательно;
**P2** — если останется время.

### 10.1. Компас (`/compass`) — P0

Блок долгосрочного видения.

- **Сферы жизни**: настраиваемый список. По умолчанию: Работа и проекты,
  Обучение и развитие, Личное и семья, Здоровье и ресурс, Финансы и быт.
  У каждой — цвет-маркер (из палитры токенов) и иконка. CRUD.
- **Горизонты**: год (1–2 цели на сферу) → квартал (измеримые результаты) →
  месяц (3–5 вех). Цель может иметь родителя (месячная → квартальная →
  годовая). Прогресс: процент закрытых вех; можно переопределить вручную.
- **Анти-цели**: отдельные списки на месяц и квартал — «от чего сознательно
  отказываюсь». Пример: «Не брать больше 2 проектов параллельно», «Не
  проверять рабочие чаты после 20:00».
- **Визуализация**: карточка цели — сфера (цветная точка), название,
  горизонт, дедлайн, прогресс-бар, вехи. Сводка сверху: сколько целей на
  горизонте, средний прогресс, цели без движения (>14 дней).
- Переключатель горизонтов — сегмент без лишних подписей.

### 10.2. Дела — P0

**Инбокс** (`/inbox`): единый список неразобранного, добавление в одно поле
(Enter — сохранить, остаться в поле). Для каждого элемента действия:
«В задачи на сегодня», «В проект», «В личное», «В книги/идеи», «Удалить».
После действия элемент исчезает из инбокса.

**Проекты** (`/projects`): карточка проекта — название, сфера, связь с целью,
дедлайн, статус (Идея / В работе / На паузе / Завершён), «результат» одним
абзацем, чек-лист этапов с прогрессом. Внутри проекта: заметки, ссылки
(URL + подпись), связанные задачи. Фильтры по сфере/статусу.

**Планировщик** (`/today`, `/week`):

- Жёсткие задачи (события) — дата + интервал времени (`10:00–11:30`).
- Гибкие задачи по ресурсу: 🔴 Must-do (максимум 2–3 в день — предупреждать,
  не блокировать), 🟡 «важно, если будет время», 🟢 «на низком ресурсе»
  (рутина). Ресурс — обязательное поле, по умолчанию 🟡.
- Представления: список на день, канбан по статусам, календарная сетка
  недели. Переключатель — сегмент.
- Быстрое добавление — ≤2 клика или горячая клавиша (например `N`),
  из любого раздела через ⌘K/кнопку.
- Повторяющиеся задачи: ежедневно / по дням недели / раз в месяц.
  Завершение создаёт следующее вхождение.
- Фильтры: сфера, проект, ресурс, дедлайн, список (работа/личное), тег.

### 10.3. Библиотека (`/library`) — P0

**Книги**, полки:

- «Читаю сейчас» — предупреждение, если больше 3 книг.
- «В очереди» — с приоритетом (перетаскивание или стрелки).
- «Прочитано» — архив с оценкой 1–5 и датой завершения.
- «Отложено / не зашло».

Карточка книги: название, автор, категория (проф. литература / художка /
нон-фикшн), страницы (всего/прочитано, прогресс), «Зачем я взял эту книгу»
(запрос на чтение), **3 главные мысли** (структурированный список),
**копилка цитат** (текст + страница), **действие на внедрение** с кнопкой
«Превратить в задачу» → создаёт задачу в Делах и связывает её с книгой.

**Обучение** (вкладка внутри Библиотеки, курсы остаются на `/courses`):
организатор, прогресс по урокам, дедлайн доступа, конспект, привязка к
проекту/цели.

### 10.4. Личное и рефлексия — P0/P1

**Личные дела** (`/personal`): списки с тегами — Покупки, Дом и уют, Семья и
близкие, Здоровье и врачи, Досуг. Отделены от рабочих дедлайнов; любую задачу
можно перенести в план на конкретный день.

**Чек-ин ресурса**: раз в день — энергия 1–5 + тег состояния (В потоке,
Спокойно, Усталость, Перегруз) + заметка. Повторный чек-ин в тот же день
перезаписывает.

**Привычки**: 3–5 опорных (сон 7+, прогулка, чтение, спорт, отдых без
экрана), сетка выполнения за неделю и месяц, серии. Использовать существующие
`habits`/`habit_marks`, добавить цвет/сферу.

**Итоги недели/месяца** (`/review`): пошаговый мастер.

1. Факты (показывает система): задач закрыто/всего, страниц прочитано,
   средняя энергия за 7 дней, % привычек.
2. 3 главные победы (текст).
3. Что не успелось — автоматический список незавершённых задач с кнопками
   «На следующую неделю» / «Отменить без вины».
4. Фокус и забота о себе на следующую неделю.
5. Сохранить итог (история итогов — список за прошлые периоды).

### 10.5. Финансы (`/finance`) — P0

- Счета (наличные/карта/накопления/брокер) — только для группировки и
  стартового баланса, без интеграций.
- Категории доходов/расходов с цветами; дефолтный набор.
- Транзакции: дата, сумма, тип, категория, счёт, заметка. Добавление — быстрое,
  одной формой, сумма в рублях (хранить в копейках, целыми).
- Обзор месяца: доход, расход, разница; расход по категориям (горизонтальные
  бары, доли); дневная динамика; бюджеты: лимит на категорию и факт,
  прогресс с предупреждением при >80 % и >100 %.
- Фильтры: месяц, категория, тип, поиск по заметке.
- **Никаких** графиков «на весь экран» и пончиков из 20 сегментов: только
  читаемые бары и один тренд.

### 10.6. Аналитика (`/analytics`) — P1

Сводная страница: серия энергии за 30/90/180 дней, баланс сфер (задачи по
сферам — закрытые/всего), читательская статистика (страницы, книги), деньги
(доход/расход/тренд), привычки (%), корреляции (существующий блок). Каждая
карточка — с честной подписью: «мало данных» только если данных правда мало;
при слабой связи — «связи выше порога нет (r=…, n=…)».

### 10.7. Сквозные функции — P0/P1

- **Глобальный поиск ⌘K**: мгновенный (дебаунс 120 мс, отмена запросов).
  Ищет по задачам, проектам, книгам, цитатам, заметкам, целям, инбоксу.
  Результаты сгруппированы по типу, Enter — переход, ⌘K — открыть/закрыть.
  Серверный эндпоинт `/api/search?q=`.
- **Умные фильтры** на списках (сфера/ресурс/дедлайн/проект/тег), состояние
  фильтров — в URL-хэше, чтобы ссылку можно было сохранить.
- **Уведомления** (P2): утренний дайджест задач и вечернее напоминание
  чек-ина — через бота, только если токен задан. Без токена — выключены и
  честно подписаны.
- **Экспорт** (P1): JSON всей базы и Markdown-выгрузка; PDF — через
  print-стили браузера (без новых зависимостей). Кнопка в Настройках.
- **Адаптив**: mobile-first для быстрых действий (добавить в инбокс, отметить
  привычку, записать цитату, чек-ин), desktop — многоколоночный дашборд.
- **PWA**: установка, оффлайн-оболочка (уже есть `sw.js`), не ломать кэш API.

---

## 11. Модель данных v4

`SCHEMA_VERSION = 3`, миграция № 3. Правила: ISO-даты (`YYYY-MM-DD`), метки
времени `YYYY-MM-DDTHH:MM:SSZ`, деньги — **целые копейки**, неделя начинается
с понедельника, часовой пояс Europe/Moscow.

Ниже — целевая схема. Порядок таблиц важен для FK.

```sql
-- Сферы жизни
CREATE TABLE spheres (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  color TEXT NOT NULL DEFAULT '#A18BB7',
  icon TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- goals уже есть: title, target, current, unit, deadline, status, created
ALTER TABLE goals ADD COLUMN sphere_id INTEGER REFERENCES spheres(id);
ALTER TABLE goals ADD COLUMN horizon TEXT NOT NULL DEFAULT 'year';   -- year|quarter|month
ALTER TABLE goals ADD COLUMN parent_id INTEGER REFERENCES goals(id) ON DELETE SET NULL;
ALTER TABLE goals ADD COLUMN why TEXT NOT NULL DEFAULT '';
ALTER TABLE goals ADD COLUMN progress INTEGER NOT NULL DEFAULT 0;    -- 0..100, ручной оверрайд

CREATE TABLE goal_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  due TEXT,
  sort INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE anti_goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL CHECK (period IN ('month','quarter')),
  text TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Проекты
CREATE TABLE projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sphere_id INTEGER REFERENCES spheres(id) ON DELETE SET NULL,
  goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
  deadline TEXT,
  status TEXT NOT NULL DEFAULT 'idea'
    CHECK (status IN ('idea','active','paused','done')),
  outcome TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated TEXT
);

CREATE TABLE project_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE project_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note','link')),
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Задачи
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  sphere_id INTEGER REFERENCES spheres(id) ON DELETE SET NULL,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
  list_kind TEXT NOT NULL DEFAULT 'work' CHECK (list_kind IN ('work','personal')),
  tags TEXT NOT NULL DEFAULT '',              -- «Покупки,Дом» через запятую
  due_date TEXT,
  start_at TEXT,                              -- только для жёстких событий
  end_at TEXT,
  resource TEXT NOT NULL DEFAULT 'important'
    CHECK (resource IN ('must','important','low')),
  status TEXT NOT NULL DEFAULT 'todo'
    CHECK (status IN ('todo','done','cancelled')),
  recurrence TEXT NOT NULL DEFAULT '',        -- '', 'daily', 'weekly:1,3,5', 'monthly:15'
  notes TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  done_at TEXT
);

CREATE TABLE inbox_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','processed')),
  converted_type TEXT NOT NULL DEFAULT '',
  converted_id INTEGER,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Библиотека
CREATE TABLE books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'nonfiction'
    CHECK (category IN ('pro','fiction','nonfiction')),
  pages_total INTEGER NOT NULL DEFAULT 0,
  pages_read INTEGER NOT NULL DEFAULT 0,
  shelf TEXT NOT NULL DEFAULT 'queue'
    CHECK (shelf IN ('reading','queue','done','dropped')),
  rating INTEGER,
  why TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  finished_at TEXT,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated TEXT
);

CREATE TABLE book_insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL
    REFERENCES books(id) ON DELETE CASCADE, text TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE book_quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL
    REFERENCES books(id) ON DELETE CASCADE, text TEXT NOT NULL,
  page INTEGER, created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE book_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER NOT NULL
    REFERENCES books(id) ON DELETE CASCADE, text TEXT NOT NULL,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Курсы уже есть (courses/lessons/…): добавить метаданные
ALTER TABLE courses ADD COLUMN organizer TEXT NOT NULL DEFAULT '';
ALTER TABLE courses ADD COLUMN deadline TEXT;
ALTER TABLE courses ADD COLUMN notes TEXT NOT NULL DEFAULT '';
ALTER TABLE courses ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE courses ADD COLUMN goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL;

-- Финансы
CREATE TABLE finance_accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'card' CHECK (kind IN ('cash','card','savings','broker')),
  currency TEXT NOT NULL DEFAULT 'RUB',
  balance_start_kopecks INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE finance_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
  color TEXT NOT NULL DEFAULT '#A18BB7',
  sort INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  UNIQUE (name, kind)
);

CREATE TABLE transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0),
  kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
  category_id INTEGER REFERENCES finance_categories(id) ON DELETE SET NULL,
  account_id INTEGER REFERENCES finance_accounts(id) ON DELETE SET NULL,
  note TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE budgets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES finance_categories(id) ON DELETE CASCADE,
  month TEXT NOT NULL,                       -- 'YYYY-MM'
  limit_kopecks INTEGER NOT NULL,
  UNIQUE (category_id, month)
);

-- Ресурс и рефлексия
CREATE TABLE checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL UNIQUE,
  energy INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 5),
  mood TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL CHECK (period IN ('week','month')),
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  wins TEXT NOT NULL DEFAULT '',
  misses TEXT NOT NULL DEFAULT '',
  focus TEXT NOT NULL DEFAULT '',
  care TEXT NOT NULL DEFAULT '',
  facts_json TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (period, start_date)
);

-- Привычки уже есть (habits/habit_marks): добавить маркер
ALTER TABLE habits ADD COLUMN sphere_id INTEGER REFERENCES spheres(id) ON DELETE SET NULL;
ALTER TABLE habits ADD COLUMN color TEXT NOT NULL DEFAULT '#A18BB7';
```

Индексы (обязательно): `tasks(due_date,status)`, `tasks(project_id)`,
`tasks(sphere_id)`, `tasks(list_kind)`, `transactions(ts)`,
`transactions(category_id)`, `goal_steps(goal_id)`, `project_steps(project_id)`,
`books(shelf)`, `inbox_items(status)`, `checkins(date)`, `reviews(period,start_date)`.

FTS не расширяем: поиск через `LIKE` по индексированным полям — на локальной
базе это <10 мс при десятках тысяч строк.

---

## 12. API v4 (контракт)

Все ответы — JSON без обёртки; ошибки — `{"detail": "..."}` с кодом 4xx.
Использовать существующие зависимости (`api/deps.py`) и стиль остальных
роутеров. Все новые эндпоинты — под `/api/*`, требуют тот же auth (если задан
`LIFEOS_API_TOKEN`).

Стиль имён: **camelCase в JSON**, snake_case в Python-моделях не обязателен;
важнее стабильность. Деньги: в запросе `amount` — число рублей
(`1250.5`), сервер округляет до копеек; в ответе — `amount` (рубли) и
`amountKopecks` (int).

### 12.1. Сферы и компас

```
GET    /api/spheres                        → {items:[{id,name,color,icon,sort,archived}]}
POST   /api/spheres                        {name,color?,icon?}
PATCH  /api/spheres/{id}
DELETE /api/spheres/{id}                   (архив, если используется)

GET    /api/compass?date=YYYY-MM-DD
  → {spheres:[...],
     goals:[{id,title,horizon,sphereId,sphere:{id,name,color},parentId,
             deadline,status,progressPct,why,
             steps:[{id,title,done,due,sort}],
             counts:{stepsDone,stepsTotal,tasksDone,tasksTotal}}],
     antiGoals:{month:[{id,text,active}],quarter:[...]}}

POST   /api/goals        {title,horizon?,sphereId?,parentId?,why?,deadline?,target?,unit?}
PATCH  /api/goals/{id}   (любые поля, включая status и progress)
DELETE /api/goals/{id}
POST   /api/goals/{id}/steps      {title,due?}
PATCH  /api/goal-steps/{id}       {title?,done?}
DELETE /api/goal-steps/{id}

GET    /api/anti-goals?period=month|quarter
POST   /api/anti-goals   {period,text}
PATCH  /api/anti-goals/{id} {text?,active?}
DELETE /api/anti-goals/{id}
```

Прогресс цели: если `progress > 0` — ручной; иначе доля закрытых вех; если вех
нет — доля закрытых связанных задач.

### 12.2. Задачи, инбокс, проекты

```
GET  /api/tasks?date=&view=day|week|kanban|calendar&from=&to=
     &sphereId=&projectId=&resource=&status=&listKind=&q=
  → {items:[{id,title,sphereId,sphereName,projectId,projectName,goalId,
             listKind,tags:[...],dueDate,startAt,endAt,resource,status,
             recurrence,notes,doneAt,created}],
     counts:{must,important,low,done,total}}

POST /api/tasks {title,dueDate?,startAt?,endAt?,resource?,sphereId?,
                 projectId?,goalId?,listKind?,tags?:string|string[],notes?,recurrence?}
PATCH /api/tasks/{id}
POST  /api/tasks/{id}/complete   {done:true|false}   → {task,next?:task}
POST  /api/tasks/{id}/reschedule {dueDate}
DELETE /api/tasks/{id}           (status='cancelled')

GET  /api/inbox?status=new|processed|all
POST /api/inbox {text}
POST /api/inbox/{id}/convert {to:"task"|"project"|"personal"|"book"|"delete", payload?:{}}
DELETE /api/inbox/{id}

GET  /api/projects?status=&sphereId=&q=
POST /api/projects {name,sphereId?,goalId?,deadline?,status?,outcome?,description?}
GET  /api/projects/{id}  → {project, steps:[...], notes:[...], tasks:{items,counts}}
PATCH /api/projects/{id}
DELETE /api/projects/{id} (если нет задач — удалить; иначе 409)
POST /api/projects/{id}/steps {title}
PATCH /api/project-steps/{id} {title?,done?}
DELETE /api/project-steps/{id}
POST /api/projects/{id}/notes {kind:"note"|"link",title?,body?,url?}
DELETE /api/project-notes/{id}
```

Повторяющиеся задачи: при `complete` для `recurrence` создаётся следующая
задача (дата = следующее вхождение после текущей). Если правило невалидно —
не падать, просто не создавать.

### 12.3. Библиотека

```
GET  /api/books?shelf=reading|queue|done|dropped|all&q=
POST /api/books {title,author?,category?,pagesTotal?,shelf?,why?,priority?}
GET  /api/books/{id} → {book, insights, quotes, actions}
PATCH /api/books/{id}
DELETE /api/books/{id}
POST /api/books/{id}/progress {pagesRead}

POST /api/books/{id}/insights {text}
PATCH /api/book-insights/{id} {text?}
DELETE /api/book-insights/{id}
POST /api/books/{id}/quotes {text,page?}
DELETE /api/book-quotes/{id}
POST /api/books/{id}/actions {text}
POST /api/book-actions/{id}/to-task {dueDate?} → {task,action}

GET  /api/learning → {courses:[{id,title,organizer,deadline,projectId,goalId,
       notes,lessonsTotal,lessonsDone,pct,cover,category}]}
PATCH /api/courses/{id} {organizer?,deadline?,projectId?,goalId?,notes?}
```

### 12.4. Финансы

```
GET /api/finance/summary?month=YYYY-MM
  → {month,income,expense,balance,
     byCategory:[{id,name,color,kind,sum,share}],
     budgets:[{categoryId,name,limit,spent,pct,over}],
     daily:[{date,income,expense}]}

GET    /api/finance/transactions?month=&kind=&categoryId=&q=&limit=100
POST   /api/finance/transactions {date,amount,kind,categoryId?,accountId?,note?}
PATCH  /api/finance/transactions/{id}
DELETE /api/finance/transactions/{id}

GET/POST/PATCH /api/finance/accounts
GET/POST/PATCH /api/finance/categories
GET    /api/finance/budgets?month=
PUT    /api/finance/budgets {categoryId,month,limit}
```

### 12.5. Личное и итоги

```
GET  /api/personal/summary?date=YYYY-MM-DD
  → {date, checkin:{date,energy,mood,note}|null,
     habits:[{id,name,color,targetPerWeek,streak,marks:{'YYYY-MM-DD':1}|[]}],
     tasks:{must,important,low,done,total},
     reading:{pagesToday,pagesWeek},
     energy7:[{date,value}]}

POST /api/personal/checkin {date,energy,mood?,note?}   (upsert по дате)

GET  /api/personal/review?period=week|month&start=YYYY-MM-DD
  → {period,startDate,endDate,existing?:{},facts:{tasksDone,tasksTotal,
     pagesRead,energyAvg,habitsPct}, openTasks:[{id,title,dueDate}]}
POST /api/personal/review {period,startDate,wins,misses,focus,care}
GET  /api/personal/reviews?limit=12
```

### 12.6. Аналитика, поиск, экспорт

```
GET /api/analytics/summary?days=30
  → {energy:{series,avg}, spheres:[{id,name,color,done,total,share}],
     reading:{pages,booksDone}, finance:{income,expense,balance,series},
     habits:{pct,series}, tasks:{done,total},
     correlations:[ ...как /api/analytics/correlations ]}

GET /api/search?q=...&limit=20
  → {items:[{kind:"task"|"project"|"book"|"quote"|"goal"|"inbox"|"note",
             id,title,snippet,route}]}

GET /api/export?format=json|md   → файл (Content-Disposition: attachment)
```

`/api/analytics/correlations` и остальные существующие эндпоинты не менять
(только дополнять поля).

---

## 13. Фронтенд v4

Ограничения:

- React 19 + TypeScript strict, **без** внешних UI-библиотек и Tailwind.
  Стили — CSS-переменные + обычные CSS-файлы (как сейчас).
- Данные — через `src/api/client.ts` (fetch + AbortController + таймаут) и
  хуки в духе существующих (`useOverview`, `useSection`). Никакого Redux;
  локальное состояние + `useState`/`useReducer`.
- Все действия — **оптимистичные**: сначала обновить UI, потом отправить
  запрос, при ошибке откатить и показать тост. Никаких полноэкранных
  спиннеров; скелетоны только при первой загрузке.
- Формы — управляемые, валидация на месте, ошибки у поля, Enter — сохранить,
  Esc — отмена. Модалки/панели — с focus-trap и возвратом фокуса.
- Новые страницы — отдельные файлы `src/pages/*.tsx`, стили —
  `src/styles/*.css`, общие примитивы — `src/components/` (`Card`, `Sheet`,
  `Segmented`, `Field`, `ProgressBar`, `EmptyState`, `SearchPalette`).
- Регистрация маршрутов: один файл, чтобы не конфликтовать при параллельной
  работе (например `src/pages/registry.ts` + `App.tsx`).
- Компонентные классы — по BEM-подобной схеме, как в существующих файлах.
- Никаких инлайн-hex в компонентах: только токены/`var(--…)`.
- Иконки — текущий набор `Icon.tsx`; новые добавлять туда же (16/18/20 px,
  stroke 1.6–1.75), не тянуть icon-паки.

---

## 14. Демо-данные

Нужны, чтобы все новые экраны были живыми сразу после установки.

- Детерминированно (`random.Random(42)`), идемпотентно, с `--reset` только
  для демо-строк (не трогать `source='telegram'` и пользовательские записи).
- 120 дней: сферы, 8–10 целей по горизонтам с вехами, 2–3 анти-цели,
  4–6 проектов с этапами и заметками, ~120 задач (закрытые/открытые,
  жёсткие и гибкие), 12 книг (2 читаю, 4 в очереди, 4 прочитано, 2 отложено)
  с цитатами и инсайтами, 2 курса (уже есть), ~180 транзакций по 8–10
  категориям и 3 счетам, 3 бюджета, 90 чек-инов энергии, 2 годовых итога.
- Временные метки — вокруг «сегодня», чтобы графики выглядели живыми.

Команда: `python3 -m app.server.seeder --db data/lifeos-v3.db --days 120`.

---

## 15. Нефункциональные требования

- **Скорость**: первый экран ≤1,5 с локально; переключение разделов и
  чекбоксы — мгновенно (<100 мс), без ожидания сети. API локально p95 <50 мс.
- **Надёжность**: нет необработанных исключений в API; ошибка одного блока не
  валит страницу (как сейчас в Body: `Promise.allSettled`).
- **Доступность**: семантические лендмарки, подписи у полей, клавиатура,
  видимый фокус, контраст AA, `aria-live` для тостов, reduced motion.
- **Тёмная тема** — полноценная, не «инверсия на скорую руку».
- **Адаптив**: 390 px (телефон), 768, 1024, 1440. Ничего не обрезается.
- **Данные**: SQLite WAL, бэкап перед миграцией, экспорт JSON/MD.
- **Безопасность**: только localhost по умолчанию; токен не логировать и не
  коммитить; никаких внешних запросов из SPA (без CDN/шрифтов/аналитики).
- **HTTP**: существующий auth не ломать; новые эндпоинты — под тем же
  контролем.

---

## 16. Тесты и Definition of Done

Требования к сдаче (все пункты — проверяемые):

1. `cd app && python3 -m pytest tests -q` — **≥ 300 passed**, 0 failed.
   На каждый новый модуль: happy path + валидация + пустые состояния.
2. `cd app/web && npm run build` — PASS; `tsc` без ошибок; bundle gzip
   ≤ 250 КБ (суммарно JS+CSS, не считая медиа).
3. `bash check.sh` — 11/11 PASS; `bash deploy/check-deploy.sh` — 0 провалов.
4. Живой smoke на временной БД: миграция v2→v3 проходит на копии реальной
   базы, все новые эндпоинты отвечают 200, SPA открывается.
5. `python3 -m app.server.seeder --days 120` — идемпотентно (два запуска не
   плодят дубли).
6. Скриншоты: 1440 и 390 px, светлая и тёмная тема, ключевые экраны
   (Обзор, Компас, Сегодня, Проекты, Библиотека, книга, Финансы, Личное,
   Итоги, Аналитика) — в `studio/shots-v4/`.
7. Ручной чек-лист UX: с клавиатуры можно пройти весь день (добавить задачу →
   отметить → чек-ин → итоги); ⌘K находит сущности; фильтры сохраняются в URL.
8. Отчёт: что сделано, что не сделано и почему, как проверял, риски.

**Definition of Done модуля**: данные → API (с тестами) → экран → пустое
состояние → мобильная версия → тёмная тема → скриншот.

---

## 17. Что нельзя делать

- Не трогать `src/`, `install.sh`, `ops/`, `tests/` (v1-контур) без явной
  необходимости; бот v3 не переписывать с нуля.
- Не менять формат существующих API-ответов, на которые опираются страницы
  (`/api/overview`, `/api/sections/*`, `/api/settings`, `/api/health/*`,
  `/api/analytics/correlations`, `/api/courses*`).
- Не писать в Obsidian-базу владельца из тестов и сидера; только tempdir.
- Не добавлять зависимости без нужды (никаких UI-китов, графических
  библиотек, ORM, Docker).
- Не коммитить секреты, `.db`, медиа, личные данные.
- Не обещать в интерфейсе того, чего нет: без токена бот «выключен», а не
  «синхронизирован».
- Не ставить в интерфейс «сборка Ф5» и прочие внутренние метки.
- Не использовать фиолетовые градиенты-«блобы», эмодзи-иконки, стеклянные
  карточки, центрированные hero и прочие маркеры шаблонного ИИ-дизайна.

---

## 18. Риски и открытые вопросы

1. **Токен бота** не выдан. Всё, что зависит от Telegram (живые уведомления,
   дайджесты), делать за флагом и с честной подписью; интерфейс не должен
   падать без токена.
2. **PDF-отчёт**: без новых зависимостей — печатная версия страницы
   (`@media print`). Если нужен «настоящий» PDF — согласовать зависимость.
3. **Регулярные задачи**: не строить сложный RRULE; хватит daily /
   weekly:1,3,5 / monthly:15. Некорректное правило — игнорировать, не падать.
4. **Оффлайн-синхронизация**: не изобретать очередь мутаций в v4; PWA-кэш
   только для оболочки, действия требуют локального сервера.
5. **Многопользовательность/авторизация по email**: продукт однопользовательский
   и локальный; не строить OAuth. Если нужен код-пароль — отдельным решением.
6. **Старые заготовки** (`SectionPage`) можно переиспользовать для
   второстепенных разделов (Сон, Питание, Тренировки), но главные экраны
   должны быть настоящими.
7. **Данные владельца**: демо-база — «жизнь на 120 дней», её можно менять
   свободно; реальные записи появятся только после запуска бота.

---

## 19. Рекомендуемый порядок работ

1. **Фундамент** — миграция 3 + модели/хелперы + сидер; прогнать миграцию на
   копии живой базы; тесты схемы.
2. **Бренд и оболочка** — логотип, favicon/manifest, сайдбар (низ), токены,
   примитивы (`Card`, `Sheet`, `Segmented`, `ProgressBar`, `EmptyState`),
   ⌘K-палитра на `/api/search`.
3. **Компас** — сферы/цели/вехи/анти-цели: API → экран.
4. **Дела** — инбокс → задачи → проекты; виды день/неделя; повторения.
5. **Библиотека** — полки → карточка книги → цитаты/инсайты → «в задачу»;
   метаданные курсов.
6. **Финансы** — счета/категории/транзакции/бюджеты → обзор месяца.
7. **Личное** — чек-ин, привычки, личные списки, мастер итогов.
8. **Аналитика** — сводка, энергии, сферы, чтение, деньги.
9. **Полировка** — адаптив, тёмная тема, пустые состояния, a11y,
   производительность, скриншоты, отчёт.

Каждый шаг — отдельный коммит с внятным сообщением.

---

## 20. Приложение: подводные камни, уже пойманные

- Python 3.9 обязателен как минимум для синтаксиса: никаких `match`, `X | Y`
  в аннотациях, `dict[str, int]` только под `from __future__ import
  annotations`. Проверка: `/usr/bin/python3 -m py_compile`.
- SQLite без FTS5 роняет миграцию — это ожидаемо и проверено тестом.
- `entries.date` — генерируемая колонка, её нельзя вставлять/обновлять.
- `PRAGMA user_version` — единственный источник версии схемы; миграции
  идемпотентны.
- Сервер отдаёт `app/web/dist`; после `npm run build` перезапуск не нужен,
  статика читается с диска. Изменения Python-кода — только с перезапуском
  uvicorn.
- Auth: если задан `LIFEOS_API_TOKEN`, все `/api/*` (кроме `/api/health`) и
  `/media/*` требуют `X-LifeOS-Token` или cookie `lifeos_token`; WebSocket —
  ещё `?token=`.
- Медиа: `GET /media/{lesson_id}` умеет Range (206) — плеер курсов на этом
  держится.
- SPA: тема применяется до первой отрисовки скриптом в `index.html`; не
  сломать, иначе будет вспышка светлого экрана.
- Сайдбар: свёрнутость хранится в `localStorage`; в свёрнутом виде у пунктов
  тултипы — не потерять.
- Бот v1 (stdlib) и бот v3 (aiogram) — разные вещи; live-токен нужен только
  второму.
- Демо-курсы требуют `tools/make_demo_media.py` для звука; без него плеер
  живёт, но аудио 404 (это не баг кода).
- Версии в репозитории расходятся намеренно: `VERSION` = `0.1.2` — это
  установленный старый контур v0.x, а код `app/` — `3.0.0-alpha` в
  `app/__init__.py` (его и отдаёт `/api/health`). Для v4 поднять версию в
  `app/__init__.py` и `CHANGELOG.md`, `VERSION` не переиспользовать.
- Логотип и новые иконки уже собраны, но SPA ещё не пересобрана: после
  `npm run build` они появятся в `app/web/dist`. Исходник — `brand/logo-source.png`.
- Живой демо-сервер на 8787 отвязан от сессий; если он мешает, его можно
  остановить (`lsof -ti tcp:8787 | xargs kill`) и поднять заново командой из
  раздела 4. База при этом не пострадает.
- Сборка SPA: `npm run build` запускает `tsc` — типы обязательны, `any`
  не приветствуется.
- Тесты пишут только во временные каталоги (`tmp_path`), никогда — в
  `data/` или Obsidian.
- Проверка секретов — `check.sh`; не ломать её эвристики.

---

## 21. Готовое сообщение для Opus 5.5

Скопируй в чат вместе с этим файлом:

> Ты — ведущий инженер и дизайнер проекта **Life OS**. Прочитай
> `studio/HANDOFF-OPUS-5.5.md` целиком — там контекст, что уже работает,
> дизайн-направление, схема данных, контракт API и Definition of Done.
>
> Проект: `/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent`.
> Стек: FastAPI + SQLite (WAL, миграции) + React 19/TS/Vite без UI-библиотек;
> Python 3.9-совместимый синтаксис; только локальный сервер на 127.0.0.1:8787.
>
> Начни с запуска и осмотра: подними сервер на демо-базе
> (`data/lifeos-v3.db`), собери SPA, открой `http://127.0.0.1:8787`, прогони
> `pytest app/tests -q` и `bash check.sh`. Сделай бэкап БД перед миграцией.
>
> Что сделать, по порядку:
> 1) бренд: логотип-лепесток (`brand/logo-source.png` → ассеты уже собраны
>    `tools/brand_assets.py`), favicon/manifest/PWA, сайдбар — убрать
>    «Светлая/Тёмная/Авто» с подписями и «сборка Ф5», сделать компактный
>    переключатель темы из иконок и аккуратный низ сайдбара;
> 2) убрать «ИИ-слоп»: один акцент, тени только у всплывающих слоёв, сетка
>    4/8 px, моноширинные цифры, честные пустые состояния, тёмная тема,
>    адаптив 390/768/1440;
> 3) миграция схемы v3 (`SCHEMA_VERSION = 3`) — сферы, цели/вехи/анти-цели,
>    инбокс, проекты, задачи, книги/цитаты, финансы, чек-ины, итоги; сидер
>    демо-данных на 120 дней;
> 4) API и экраны по контракту из разделов 10–12: Компас, Дела (сегодня,
>    неделя, инбокс, проекты), Библиотека, Финансы, Личное/Итоги, сводная
>    Аналитика, ⌘K-поиск, экспорт JSON/MD;
> 5) проверки: ≥300 pytest, сборка без ошибок tsc, `check.sh`, живой smoke на
>    копии базы, скриншоты 1440/390 в светлой и тёмной теме, отчёт.
>
> Жёсткие ограничения: не трогать Obsidian-базу владельца и `src/` (v1);
> не менять формат существующих API; без новых тяжёлых зависимостей и без
> CDN; секреты не коммитить; живого Telegram-бота не обещать — токена нет.
> Действия в интерфейсе — оптимистичные, отклик <100 мс, первый экран ≤1,5 с.
>
> Сдай результат так: список сделанного, скриншоты, вывод тестов/сборки,
> что осталось и какие решения принял. Каждый модуль — отдельным коммитом.
