# Life OS v3 — фаза 4: FastAPI, медиа с Range и аналитика

- **Статус:** готово
- **Дата:** 2026-11-17
- **Коммит волны:** c45bb2a, ac7d663, `4d093d9`
- **Требование:** MASTER_SPEC §2.2 и §2.5, волны `t7` (API) и «аналитика/корреляции»

## API

`app/server/main.py` (230 строк): фабрика `create_app()`, lifespan с миграциями, CORS только для localhost, auth-middleware, кеш-политика, раздача собранной SPA из `app/web/dist`, JSON-заглушка если SPA не собрана.

| Роутер | Строк | Эндпоинты |
|---|---:|---|
| `api/overview.py` | 409 | `GET /api/overview` — 6 KPI, повестка, вода, вес, «продолжить курс» |
| `api/journal.py` | 107 | `GET /api/journal?limit&type&q` — FTS5-поиск |
| `api/health.py` | 181 | `GET /api/health`, `/api/health/weight` (EMA + прогноз), `/api/health/composition` |
| `api/courses.py` | 178 | `GET /api/courses`, `/api/courses/{id}`, `/api/lessons/{id}`, `POST /api/lessons/{id}/progress` |
| `api/sections.py` | 500 | `GET /api/sections/{name}` — единая форма для 11 экранов; `POST /api/habits/{id}/mark`, `/api/habits`, `/api/goals`, `PATCH /api/goals/{id}` |
| `api/quick.py` | 187 | `POST /api/quick`, `/api/water` |
| `api/settings.py` | 69 | `GET/PUT /api/settings` — только безопасные поля |
| `api/analytics.py` | 40 | `GET /api/analytics/correlations` |
| `media.py` | 231 | `GET /media/{lesson_id}`: `Accept-Ranges`, `206`, точный `Content-Range`, один диапазон, чанки 64 КБ, защита от traversal |
| `ws.py` | 176 | `WS /ws` + in-process брокер: `entry.created`, `job.progress`, `sync.updated` |

## Аналитика

`api/aggregates.py` (552 строки) — чистые функции без HTTP:

- `ema_series` — α = 2/(окно+1), окна 7/14;
- `pearson` и `build_correlations` — пары сон↔настроение, вода↔настроение, тренировки↔вес; пороги `n ≥ 14`, `|r| ≥ 0.3`; честные статусы `ok`, `weak`, `insufficient_data`, `no_variance`;
- `linear_trend` + `weight_forecast` — МНК по последним 30 дням, скорость кг/нед, дата цели; статусы `ok`, `flat`, `insufficient_data`, `no_target`, `away_from_target`, `reached` (дата не выдумывается);
- `composition_deltas` — жир/мышцы/вода, дельты за 7/30/90, `None` при нехватке замеров.

## Чем проверено

```text
python3 -m pytest app/tests -q
271 passed, 15 subtests passed
```

- `test_e2e_api.py` (15): реальный ASGI-клиент — обзор, разделы, журнал, быстрый ввод, курсы, прогресс урока, `/media` Range `206`, отсутствие секретов, WebSocket-рукопожатие.
- `test_sections.py` (27): форма ответа всех разделов, привычки, цели.
- `test_analytics.py` (14): EMA, Пирсон, тренд, прогноз, дельты и корреляции на синтетике с известным ответом; эндпоинты на временной базе.
- `test_auth_pwa.py` (17): 401 без токена, cookie/header, публичный health, traversal → 404, статика и PWA.

## Что осталось

- Множественные Range-диапазоны (`multipart/byteranges`) сознательно не поддерживаются — браузерному плееру не нужны, ответ `416`.
- Прогноз опирается на линейный тренд; при «ступеньках» веса он честно показывает статус, а не дату.
