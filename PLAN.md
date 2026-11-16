# PLAN — команда, задачи, порядок работ

## Роли

| Кто | Зона | Файлы (write scope) |
|---|---|---|
| **Lead** | ТЗ, контракты, интеграция, приёмка, установка | `ТЗ.md`, `PLAN.md`, `INTERFACES.md`, `PROJECT_CONTEXT.md`, финальные правки |
| **bot-core** | каркас бота: конфиг, Telegram-клиент, SQLite, планировщик, цикл | `src/main.py`, `src/config.py`, `src/telegram.py`, `src/storage.py`, `src/scheduler.py` |
| **brain** | LLM-клиент, промпты, извлечение данных | `src/llm.py`, `src/prompts.py`, `src/parsing.py` |
| **vault** | Obsidian: дневник, цели, зеркало | `src/obsidian.py` |
| **dash** | дашборд: данные + HTML/CSS/JS | `src/dashboard_data.py`, `dashboard/` |
| **qa** | тесты, мок Telegram, e2e | `tests/`, `src/mock_telegram.py` |
| **ops** | установка, launchd, README | `install.sh`, `uninstall.sh`, `ops/`, `README.md`, `config/config.example.json` |

## Задачи (доска team_task_*)

- T1 `bot-core` — каркас и цикл бота (scope `src/main.py src/config.py src/telegram.py
  src/storage.py src/scheduler.py`). Зависимостей нет; опирается на INTERFACES.
- T2 `brain` — `src/llm.py`, `src/prompts.py`, `src/parsing.py`.
- T3 `vault` — `src/obsidian.py`.
- T4 `dash` — `src/dashboard_data.py` + `dashboard/`.
- T5 `ops` — install/uninstall/plist/README/config.example.
- T6 `qa` — мок-сервер + unit/e2e тесты; **blocked_by T1..T4**.
- T7 Lead — интеграция: связать модули, прогнать e2e, починить стыки, собрать демо-дашборд.
- T8 Lead — установка: launchd, реальный бот (после получения токена от пользователя).

## Порядок

1. Lead: ТЗ/INTERFACES/CONTEXT — готово.
2. Параллельно: T1, T2, T3, T4, T5 (разные файлы, пересечений нет).
3. После T1–T4: T6 (qa) пишет и гоняет тесты, шлёт баг-репорты владельцам.
4. Lead: T7 интеграция + прогон e2e, затем демо-дашборд пользователю.
5. Пользователь присылает токен → T8 установка и живая проверка.

## Правила

- Каждый агент: взять задачу с доски (claim), читать INTERFACES + PROJECT_CONTEXT,
  писать только свои файлы, гонять свои тесты, отчитываться Lead'у.
- Контракты менять нельзя; если что-то не сходится — сообщение Lead'у.
- Готовность: код + ручная проверка + отчёт (что сделано, как проверено, риски).
- Чужие файлы не редактировать. Общие файлы (`ТЗ.md` и т.п.) — только Lead.
