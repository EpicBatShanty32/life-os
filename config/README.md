# config — настройки Life OS

| Файл | Что это |
|---|---|
| `config.example.json` | образец: все поля с пустыми секретами. Его можно читать и копировать, в git он лежит |
| `config.json` | твои настоящие настройки. Создаётся из образца, в git **не** попадает (`.gitignore`), права 600 |

```bash
cp config/config.example.json config/config.json
chmod 600 config/config.json
```

## telegram — бот

| Поле | Что вписать |
|---|---|
| `token` | **токен от @BotFather.** Это пароль бота: в git, логи и переписку он не попадает. Пока поле пустое, бот не запускается и пишет «токен не задан — создай бота в @BotFather и впиши token» |
| `allowed_chat_id` | твой Telegram ID (число). Бот отвечает только ему: `0` — не отвечает никому. Узнать ID: написать боту @userinfobot |
| `api_base` | адрес Bot API. Обычный — `https://api.telegram.org`; локальный сервер — свой адрес (тогда лимит файла 2 ГБ вместо 50 МБ) |

Как скопировать токен: Telegram → @BotFather → `/mybots` → твой бот → **API
Token**. Потерялся или отозван — там же `/revoke` и новый токен; после замены
токена в `config.json` перезапусти бота: `bash tools/bot-service.sh restart`.

Токен живёт только здесь: в `~/Library/LaunchAgents/com.lifeos.bot.plist` его
нет (служба берёт его из этого файла при старте). Исключение — явный флаг
`bash tools/bot-service.sh install --token-in-plist`.

## paths — где лежат данные

| Поле | По умолчанию | Что это |
|---|---|---|
| `db` | `data/lifeos.db` | файл базы SQLite. Для v3/v4 — `data/lifeos-v3.db` |
| `logs` | `data/logs` | логи: `bot.log` (бот, ротация 1 МБ × 3), `backup.log` |
| `media` | `data/media` | фото и файлы из Telegram |
| `dashboard_out` | `dashboard/index.html` | статический дашборд v0.x |
| `web_dist` | `app/web/dist` | собранная веб-морда |

Относительные пути считаются от корня проекта. Служба бота получает
`LIFEOS_DB_PATH` из поля `paths.db`, поэтому менять базу достаточно здесь.

## obsidian, llm, schedule, app

- `obsidian` — запись в личный vault (по умолчанию выключена: `enabled: false`).
- `llm` — «умный» режим: `enabled`, `api_key`; без ключа бот работает на правилах.
  Ключ берётся из `llm.api_key`, иначе из переменной окружения `DDD_API_KEY`
  (или `LIFEOS_LLM_API_KEY`). «Сколько думать»: `reasoning_effort` (общий,
  по умолчанию `xhigh`), `parse_effort` (разбор фразы, `medium`),
  `answer_effort` (ответ о данных, `xhigh`). Пустая строка — не отправлять
  подсказку провайдеру. Модель подключается вторым слоем: явные фразы
  разбирают правила, свободные — модель, при любой ошибке — тихий откат.
- `schedule` — утренний, дневной, вечерний ритуал и недельный итог (время `ЧЧ:ММ`).
- `app` — часовой пояс, язык, тихие часы (`quiet_hours`), порт веб-сервера.

## Что важно помнить

- `config.json` — личный файл: не коммитить, не пересылать, не вставлять в чаты.
- Проверить, что секреты не утекают: `bash check.sh` (проверяет и образец, и git).
- После правки `config.json` бота перезапустить: `bash tools/bot-service.sh restart`.
