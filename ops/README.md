# ops — службы Life OS на этом маке

Здесь лежат шаблоны автозапуска (launchd) и короткие инструкции. Всё ставится
из корня проекта, права администратора не нужны.

| Что | Команда | Файлы |
|---|---|---|
| Бот (Telegram) | `bash tools/bot-service.sh install` | `ops/com.lifeos.bot.plist`, `tools/bot_launcher.py` |
| Бэкап базы | `bash deploy/install-backup-agent.sh` | `ops/com.lifeos.backup.plist`, `tools/backup.sh` |
| Агент (v0.x) | `./install.sh` | `ops/com.lifeos.agent.plist` |

## Бот: поставить на автозапуск

1. Впиши токен от @BotFather в `config/config.json` → `telegram.token`
   (файл личный, в git не попадает; образец — `config/config.example.json`).
2. В корне проекта:

```bash
bash tools/bot-service.sh install
```

Скрипт соберёт `~/Library/LaunchAgents/com.lifeos.bot.plist`, поднимет службу и
скажет pid бота. Дальше бот живёт сам:

- поднимается после перезагрузки мака и при входе в систему;
- перезапускается, если упал (не чаще раза в минуту);
- не даёт маку заснуть, пока работает (`caffeinate`);
- пишет `data/logs/bot.log` (ротация 1 МБ × 3) и `data/logs/bot.launchd.log`.

Токена нет? Скрипт ничего не поставит и скажет одной строкой: «токен не задан —
создай бота в @BotFather и впиши token…». Трейсбека не будет.

Полезные варианты той же команды:

```bash
bash tools/bot-service.sh install --dry-run     # собрать plist, ничего не запускать
bash tools/bot-service.sh install --token-in-plist  # вписать токен прямо в plist
```

`--token-in-plist` нужен редко: по умолчанию токен остаётся только в
`config/config.json`, а служба читает его оттуда при старте.

## Бот: проверить, что он онлайн

```bash
bash tools/bot-service.sh status
```

Код возврата `0` — бот работает, `1` — нет (удобно для скриптов). В выводе:
состояние службы, pid процесса, есть ли токен, какая база, последние строки лога
и итог «бот онлайн». Дополнительно:

- `bash tools/bot-service.sh logs 50` — последние 50 строк лога бота;
- написать боту в Telegram любое сообщение: ответ = он живой;
- `/backup` в боте — получить файл базы в чат.

## Бот: остановить, перезапустить, снять

```bash
bash tools/bot-service.sh stop       # остановить (вернётся при следующем входе)
bash tools/bot-service.sh start      # запустить снова
bash tools/bot-service.sh restart    # перезапустить (например, после смены токена)
bash tools/bot-service.sh uninstall  # снять с автозапуска, база и копии целы
```

Запуск в текущем окне без службы (для отладки): `bash tools/run-bot.sh`.
Второй экземпляр не поднимется — бот держит замок `data/bot.lock`.

## Бот: напоминания и ритуалы

Бот сам пишет утром (09:00), вечером (22:00), о делах со временем (за 30 минут)
и о привычках (20:00). Проверить, что расписание живёт:

```bash
grep "напоминания:" data/logs/bot.log | tail -1     # что загружено при старте
grep "напоминание отправлено" data/logs/bot.log | tail -5
```

Если напоминаний нет — проверь `/reminders` в Telegram (покажет ли расписание и
не выключено ли оно), затем `/unmute`. Тихие часы 23:00–08:00 — это норма, а не
поломка; жёсткие события (`resource='must'`) ночью приходят.

Тихо выключить всё: `/mute`. Вернуть: `/unmute`. Сдвинуть время:
`/reminders morning 08:30`, `/reminders evening 21:30`.

## Где что искать

| Что | Где |
|---|---|
| plist службы бота | `~/Library/LaunchAgents/com.lifeos.bot.plist` |
| лог бота | `data/logs/bot.log` (ротация 1 МБ × 3) |
| служебный вывод launchd | `data/logs/bot.launchd.log` |
| база | `data/lifeos-v3.db` (путь — `paths.db` в конфиге) |
| копии базы | `data/backups/` |
| токен | только `config/config.json` — не в plist и не в логах |

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `status` пишет «не поставлена» | `bash tools/bot-service.sh install` |
| «токен не задан» | вписать `telegram.token` в `config/config.json`, затем `install` |
| в логе `401 Unauthorized` | токен отозван: новый от @BotFather → `config/config.json` → `restart` |
| «бот уже запущен» | рядом живёт второй экземпляр: `pkill -f app.bot.main`, затем `start` |
| «Operation not permitted» в логе | служба запущена не из Python.app: проверь `LIFEOS_PYTHON` (`bash tools/bot-service.sh status` печатает путь python) |
| бот молчит в Telegram | `bash tools/bot-service.sh status`, затем `restart`; проверить `allowed_chat_id` в конфиге — бот отвечает только владельцу |

Системный лог launchd на случай странностей:
`log show --last 5m --predicate 'process == "launchd"' | grep lifeos`.
