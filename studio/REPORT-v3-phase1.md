# Life OS v3 — фаза 1: фундамент (`app/server`)

- **Статус:** готово
- **Дата:** 2026-11-17
- **Коммит волны:** 2fc2066 / `{{commit}}`
- **Требование:** MASTER_SPEC §2.1, волна `t5` на доске

## Что сделано

| Файл | Строк | Содержание |
|---|---:|---|
| `app/server/settings.py` | 622 | конфиг из env `LIFEOS_*` и `config/config.json` v0.x, пути, таймзона `Europe/Moscow`, секреты вне `repr` и `safe_dict()`, vault по умолчанию только на чтение |
| `app/server/db.py` | 658 | SQLite-схема v3: 13 таблиц (`entries`, `metrics`, `habits`, `habit_marks`, `goals`, `courses`, `lessons`, `lesson_progress`, `transcripts`, `segments`, `notes`, `jobs`, `settings_kv`), индексы по датам, FTS5 `entries_fts`/`segments_fts` с триггерами, WAL, `busy_timeout`, миграции v1→v2, `LegacySchemaError`/`SchemaVersionError` |
| `app/server/models.py` | 422 | pydantic-модели ответов API (обзор, разделы, курсы, настройки); контракт SPA — `app/web/src/api/types.ts` |

`entries.date` — генерируемая колонка `substr(ts, 1, 10)`, её нельзя перезаписать. База v0.x не перезаписывается: `migrate()` распознаёт чужую схему и требует новый файл.

## Чем проверено

```text
python3 -m pytest app/tests/test_db.py app/tests/test_settings.py -q
43 passed
/usr/bin/python3 -m pytest app/tests/test_db.py app/tests/test_settings.py -q
43 passed
```

Проверено: идемпотентность миграций, FTS5-поиск с русскими словами, WAL на файловой базе, отказ от чужой схемы, маскирование секретов, приоритет env над конфигом, разрешение относительных путей от корня проекта.

## Решения и отступления от буквы спека

- `app/server/analytics.py` разнесён на `api/aggregates.py` (чистые функции) и `api/analytics.py`/`api/health.py` (HTTP). Это позволяет тестировать EMA и корреляции без сервера.

## Что осталось

Нет. Фундамент не менялся после этой фазы, кроме добавления прогноза цели и дельт состава тела в фазу 4.
