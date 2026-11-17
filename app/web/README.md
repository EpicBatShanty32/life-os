# Life OS v3 — React SPA (`app/web`)

Каркас дашборда Life OS: Vite + React 19 + TypeScript, собственный хеш-роутер,
дизайн-система на CSS-переменных. Без сторонних UI-библиотек, CDN, внешних шрифтов
и сетевых запросов — только локальные ассеты.

## Запуск

```bash
cd app/web
npm install --no-audit --no-fund
npm run dev        # http://127.0.0.1:5173, /api и /ws проксируются на 127.0.0.1:8787
npm run build      # typecheck + production-сборка в dist/
npm run preview    # статическая раздача dist/ на 127.0.0.1:4173
```

`vite.config.ts`: `base: '/'`, proxy `/api`, `/ws`, `/media` → `http://127.0.0.1:8787`.
FastAPI (Ф4) раздаёт содержимое `app/web/dist` как статику.

## Маршруты

| Хеш | Раздел |
|---|---|
| `#/` | Обзор (bento-сетка, KPI, виджеты) |
| `#/today` | Сегодня |
| `#/goals` | Цели |
| `#/habits` | Привычки |
| `#/body` | Тело и весы |
| `#/sleep` | Сон |
| `#/workouts` | Тренировки |
| `#/nutrition` | Питание |
| `#/courses` | Курсы |
| `#/journal` | Журнал |
| `#/mood` | Настроение |
| `#/analytics` | Аналитика |
| `#/settings` | Настройки |

Разделы, кроме «Обзора», — осознанные заглушки: отдельный компонент на маршрут,
заголовок и явный текст «Раздел в работе». Пустых страниц нет.

## Структура

```
src/
  api/
    types.ts      типы контракта /api
    client.ts     fetch с таймаутом, нормализация, фолбэк на моки, useOverview()
    mock.ts       демо-данные на 14 дней (вес, активность, план, курс)
  components/
    Sidebar.tsx   Sidebar 3.0: 4 контура, свёртка 260↔68, бейджи, тултипы, тема, синк
    Topbar.tsx    хлебные крошки, дата, поиск, быстрая запись, источник данных
    CommandPalette.tsx  ⌘K / Ctrl+K: поиск по разделам и команды, клавиатура
    QuickEntryDialog.tsx  модалка быстрой записи (Esc, focus-trap)
    MobileNav.tsx нижняя навигация из 5 пунктов
    Sparkline.tsx SVG-спарклайн без библиотек
    KpiCard.tsx   KPI-карточка с прогрессом/трендом
    Icon.tsx      набор inline-SVG иконок и логотип
    ThemeSwitch.tsx / SyncStatus.tsx / Toaster.tsx
  lib/
    router.ts     хеш-роутер и реестр маршрутов
    theme.ts      light/dark/auto, prefers-color-scheme, localStorage
    storage.ts    безопасная обёртка localStorage
    format.ts     даты, числа, русские склонения
    focus.ts      focus-trap для модалок
    toast.ts      мини-стор уведомлений
  pages/          Overview + по компоненту на каждый раздел
  styles/
    tokens.css    палитра, радиусы, тени, тёмная тема, reduced-motion
    base.css      reset, типографика, доступность
    layout.css    каркас, сайдбар, топбар, ⌘K, диалог, тосты, адаптив
    pages.css     bento, KPI, виджеты, заглушки, адаптив
```

## Палитра и тема

`#F6F7FB` фон, `#FFFFFF` поверхность, `#17181C` текст, `#7A7D87` вторичный,
`#A9ACB6` третичный; акценты `#A18BB7`, `#AEDEC2`, `#F2C9B6`, `#B9CBE8`.
Радиусы 18–20 px, системные шрифты, мягкие тени.

Тема: светлая / тёмная / авто. Выбор хранится в `localStorage['lifeos.theme']`,
применяется до первой отрисовки (инлайн-скрипт в `index.html`), тёмная — через
`[data-theme="dark"]`. Авто следит за `prefers-color-scheme` в реальном времени.
`prefers-reduced-motion: reduce` отключает переходы и анимации.

Свёрнутый сайдбар: `localStorage['lifeos.sidebar.collapsed']`, ширина 260 → 68 px,
подписи скрываются, навигация получает тултипы; на ширине ≤ 900 px сайдбар уступает
место нижней навигации из пяти пунктов (Обзор, Сегодня, Тело и весы, Курсы, Аналитика).

## ⌘K / Ctrl+K

Поиск по всем 13 разделам (словари синонимов в `src/nav.ts`) и быстрые команды:
быстрая запись, вода +250 мл, смена темы, свёртка меню, обновление данных.
Управление: `↑` `↓` `Home` `End`, `Enter`, `Esc`; активный пункт объявляется через
`aria-activedescendant`, фокус возвращается на элемент, открывший палитру.

## Контракт данных

Экран «Обзор» читает `GET /api/overview`. Ответ — объект (допускается обёртка
`{ "data": { ... } }`). Ожидаемая форма:

```json
{
  "date": "2026-11-17",
  "kpis": {
    "streak": { "days": 12, "bestDays": 21 },
    "checkins": { "completed": 4, "total": 6 },
    "workouts": { "week": 3, "target": 4 },
    "sleep": { "lastHours": 7.4, "avg7Hours": 7.1 },
    "water": { "ml": 1250, "targetMl": 2000 },
    "mood": { "score": 7, "max": 10, "label": "спокойно", "avg7": 6.8 }
  },
  "weight": {
    "currentKg": 79.8,
    "targetKg": 76,
    "delta30Kg": -1.4,
    "series": [{ "date": "2026-11-04", "value": 80.4 }]
  },
  "course": {
    "id": "sleep-recovery",
    "title": "Сон и восстановление",
    "lessonTitle": "Урок 6 · Как считать циклы сна",
    "doneLessons": 5,
    "totalLessons": 12,
    "nextLessonId": "lesson-06"
  },
  "agenda": [
    { "id": "a1", "title": "Растяжка", "meta": "10 минут", "done": true, "kind": "habit" }
  ],
  "activity": [{ "date": "2026-11-17", "label": "вт", "minutes": 46 }],
  "insight": { "title": "Сон → энергия", "text": "…", "footnote": "r = 0,42 · n = 21" },
  "sync": { "state": "ok", "at": "2026-11-17T00:26:00+03:00" },
  "nav": { "today": 3, "habits": 2, "goals": 2, "courses": 1 }
}
```

Клиент терпим к `snake_case` (`best_days`, `target_ml`, `avg7_hours` и т.п.) и
неполным вложенным полям. Если `/api/overview` недоступен, отвечает не JSON или
контракт неполный (нет `kpis`/`weight`) — включается мок `src/api/mock.ts`, а UI
честно помечает источник: чип «Демо», футер сайдбара «Демо-данные», плашка на «Обзоре».

Действия:

- `POST /api/water` `{ "ml": 250, "date": "2026-11-17" }` — кнопки +250/+500 мл.
  При ошибке прибавка сохраняется локально (`lifeos.demo.water.<дата>`) и не теряется.
- `POST /api/quick` `{ "text": "…", "type": "note" | "metric" | "event" }` — быстрая запись.
  При ошибке запись остаётся в локальном демо-буфере `lifeos.demo.quick-entries`.

Токен владельца (Ф6) читается из `localStorage['lifeos.token']` и уходит заголовком
`X-LifeOS-Token`, если он задан.

## Доступность

- Семантические `header/nav/main/aside`, `aria-current="page"`, `aria-label` у иконок.
- Модалки: `role="dialog"`, `aria-modal`, focus-trap, `Esc`, возврат фокуса.
- KPI-прогресс — `role="progressbar"`; график — `role="img"` с текстовым описанием.
- Видимый фокус (`:focus-visible`), ссылка «Перейти к содержимому», `lang="ru"`.
- `prefers-reduced-motion` и контрастные токены для тёмной темы.

## Проверки

```bash
npm install --no-audit --no-fund
npm run build
test -f dist/index.html && echo OK
grep -c "https\?://" dist/assets/*.js
```

Внешних загрузок нет: единственные `http(s)://` в бандле — пространства имён W3C
(`http://www.w3.org/...`) и ссылки на ошибки React в тексте сообщений; ни один URL
не используется для `fetch`, `import`, шрифтов или картинок.
