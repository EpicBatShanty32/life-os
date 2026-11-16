# SECURITY_AUDIT — Life OS

**Срез:** 2026-11-16 16:50 MSK · рабочее дерево
`/Users/user/Documents/deepseek-harness/default-workspace/telegram-life-agent`
(git `b40194d` + незакоммиченные правки команды; строки проверены на этом срезе).
**Код не правился.** Единственный записанный файл — этот отчёт.
**Метод:** чтение всех модулей, `install.sh`, `uninstall.sh`, plist, конфига, README,
`dashboard/template.html`; статический grep; запуск тестов/`compileall`; генерация
дашборда с XSS-пейлоадами; временная установка с подставным `launchctl`; проверка
TLS; тесты pid-lock/SIGTERM, read-only vault, SQL-инъекций, таймаута LLM.

---

## 0. Вердикт коротко

**P0 нет.** Ставить на Mac **можно условно**, но перед первым боевым запуском нужно
закрыть три P1 (ниже) или применить обходные условия из §8. Базовая архитектура
аккуратная: SQL параметризован, XSS в дашборде нет, TLS/UA в порядке, pid-lock,
SIGTERM, WAL, read-only vault и таймаут LLM работают. Главные риски — утечка токена
в лог при опечатке в `api_base`, лог-спам launchd при фатальной ошибке конфига и
несоответствие README фактической отправке текстов в сторонний LLM.

| Приоритет | Кол-во | Суть |
|---|---:|---|
| P0 | 0 | Блокирующих «нельзя ставить» не найдено |
| P1 | 3 | токен в логе; KeepAlive-спам; приватность/LLM в README |
| P2 | 12 | права data, схема URL, guard install, containment vault, offset, и т.д. |

---

## 1. Чеклист

Легенда: ✅ — проверено, замечаний нет; ⚠️ — работает, но есть риск/отклонение;
❌ — требование не выполнено.

### 1.1 Секреты

| Проверка | Статус | Доказательство |
|---|---|---|
| `config.json` после установки 600 | ✅ | Временная установка: `-rw------- config.json`; `install.sh:398`, `install.sh:476`, `install.sh:720` |
| `data/` 700 | ⚠️ | Фактически `drwxr-xr-x` (755), БД `-rw-r--r--`; `install.sh:311-314` создаёт `data`, но `chmod 700` только `APP_DIR/CONFIG_DIR/LOG_DIR`. Родитель `APP_DIR` — 700, поэтому практически закрыто, но требование не выполнено |
| Токен/ключ нет в README/plist/install | ✅ | `README.md:31,76` — только `1234567890:AAHxxx…`; plist не содержит токена; `install.sh` печатает только подсказки/пути |
| Токен/ключ нет в git | ✅ | `git ls-files` не содержит `config/config.json`, `data/`, `dashboard/index.html`, `demo/index.html`; `git log -p --all` — реальных `sk-…`/токенов нет; `.gitignore:10,14,19,24` |
| `TelegramError`/`repr` без токена | ✅ | `src/telegram.py:116-117` — `token=***`; тесты 401/429/409/500 и network error: подстрока токена отсутствует |
| LLM не логирует ключ/тело | ✅ | `src/llm.py:211-212` — repr без ключа; `src/llm.py:268-273` — только код/тип ошибки; тест с `sk-SECRET…` в логе ключа нет |
| Нет утечки токена в лог ни при каком конфиге | ❌ | **P1-1:** при `api_base` без схемы токен попадает в `agent.log` целиком. Воспроизведено |
| Ключ не печатается install.sh | ✅ | `install.sh:466-477`: ключ читается в переменную, пишется в config, в вывод не попадает; проверено с тестовым `sk-TESTKEY…` |
| Ключ не виден в `ps` | ⚠️ | `install.sh:475` передаёт ключ аргументом Python-процессу на мгновение; другой локальный пользователь теоретически может увидеть `ps` |
| Секреты не в текущем `agent.log` | ✅ | `data/logs/agent.log` — grep по `sk-`/`bot<digits>:` пусто |

### 1.2 SQL

| Проверка | Статус | Доказательство |
|---|---|---|
| Нет f-string/`%`/`.format` в `execute` | ✅ | `grep -rnE "execute\((f[\"']|.*%|.*\.format\()" src` — пусто; единственный sink `Storage._execute` (`src/storage.py:145-152`) |
| Все пользовательские значения — через `?` | ✅ | `src/storage.py:175-178,186-189,211-225,230-233,253-256,266-269,271-275,283-286,288-292,310-313,357-358,362-365,376-378,394-396` |
| Динамический SQL только из констант | ✅ | `mark_written` — только `?` (`253-256`); `upsert_goal` — имена колонок из `_GOAL_FIELDS` (`316-330`, `350-353`) |
| Инъекция не выполняется | ✅ | Тест с `x'); DROP TABLE entries; --` в title/text/metrics/goal/habit/kv: таблицы целы, записи читаются, `DROP` не выполнен |
| `executescript(_SCHEMA)` без пользовательских данных | ✅ | `src/storage.py:24-76,141` — статическая схема |

### 1.3 Сеть и TLS

| Проверка | Статус | Доказательство |
|---|---|---|
| Только `api.telegram.org` и `api.samp.team` по умолчанию | ✅ | `src/config.py:38,51`; `src/telegram.py:83`; `src/llm.py:27`; других `urlopen` нет |
| Мок — только localhost | ✅ | `src/mock_telegram.py:86-92,173` — `127.0.0.1`, тестовый модуль |
| `User-Agent` фиксирован и не ломает TLS | ✅ | `src/telegram.py:140`; `src/llm.py:33,250`; TLS 1.3, сертификаты `api.telegram.org`/`samp.team` проверены; SAMP отвечает 401, не Cloudflare 1010 |
| TLS-проверка сертификата не отключена | ✅ | Нет `ssl._create_unverified_context`/`check_hostname=False`; `urlopen` использует default context |
| Схема URL ограничена HTTPS | ⚠️ | `src/config.py:171,182` проверяют только непустую строку; `http://` допустим → токен/ключ открытым текстом. **P2-2** |
| `--mock-url` не может увести токен на произвольный URL | ⚠️ | `src/main.py:242-243` — CLI-опция; доверенный запуск, но при ошибке оператора токен уйдёт на указанный хост. **P2-2** |

### 1.4 XSS в дашборде

| Проверка | Статус | Доказательство |
|---|---|---|
| `</` экранируется в JSON | ✅ | `src/dashboard_data.py:577-579`: `payload.replace("</", "<\\/")` |
| Данные вставляются через `textContent` | ✅ | `dashboard/template.html:644-647`: `el()` ставит `textContent`; `dashboard/template.html:617` — только JSON |
| Нет `innerHTML` с пользовательскими данными | ✅ | `grep`: все `innerHTML` — `""` либо статические `EMPTY_ICON`/SVG (`dashboard/template.html:756,938,1441`); пользовательский текст туда не попадает |
| Пейлоад `<script>alert(1)</script>` не исполняется | ✅ | Генерация с текущим шаблоном: в данных `</script` нет (`<\\/script`), в HTML нет сырого `<script>alert(1)</script>`; счётчик закрывающих `</script>` = 2 |
| Пейлоад `"><img src=x onerror=…>` не исполняется | ✅ | В HTML попадает только как JSON-строка; рендер идёт через `textContent`; `onerror` не становится атрибутом DOM |
| Имя приложения/дата экранируются | ✅ | `src/dashboard_data.py:595-598` — `_html.escape`; проверено `<img …>` в `app.name` → `&lt;img …&gt;` |
| `<!--<script>` не ломает парсер | ⚠️ | Экранируется только `</`, поэтому последовательность `<!--<script>` остаётся в `<script>`-блоке. Исполнения нет, но возможен перевод HTML-парсера в double-escaped state и поломка страницы. **P2-9** |
| Плейсхолдеры не портят текст пользователя | ⚠️ | `src/dashboard_data.py:593-605` заменяет `{{APP_NAME}}`/`{{GENERATED_AT}}` по всей странице уже после вставки JSON; сообщение «до `{{APP_NAME}}` после» отображается как «до Life OS после». **P2-8** |
| CSP/defense-in-depth | ⚠️ | CSP нет; для локального файла это не обязательно, но снижает последствия будущих ошибок. **P2-12** |

### 1.5 Пути, plist, install/uninstall

| Проверка | Статус | Доказательство |
|---|---|---|
| Даты/ID не дают path traversal | ✅ | `src/obsidian.py:407-424` — строгая `YYYY-MM-DD`; `_sanitize_marker_id` (`290-293`); тесты `../../etc/passwd`, `2026-11-16/../../x` → `ProgrammerError`, файлов вне vault нет |
| Пути из конфига не выходят за vault | ⚠️ | `src/obsidian.py:395-401` (`_vault_abs`) принимает абсолютные и `..`; тест `daily_folder="../../escape"` дал файл вне vault. Конфиг 600 и доверенный, но containment нет. **P2-4** |
| Кириллица/пробелы/`&&` в plist XML | ✅ | Временная установка с `HOME=/tmp/… & <тест>`: `xml_escape` (`install.sh:516-526`), `plistlib.loads` (`527`), `plutil -lint` — OK; `WorkingDirectory`/`StdOut` совпали с путями |
| Quoting в install.sh при пробелах/кириллице | ✅ | Установка из пути `project with spaces & <тест>` и `HOME` с пробелами/кириллицей: rc=0, файлы на месте; все подстановки в кавычках (`install.sh:311,330,335,384,499,504,605-615`) |
| `uninstall --purge` защищён | ✅ | `uninstall.sh:94-107` отказывает для пути не `*/LifeOS`; проверено: `.../not-lifeos` → rc=1, каталог не создан; точный `.../LifeOS` удаляется, соседний sentinel выжил (`uninstall.sh:158-181`) |
| `uninstall --purge` с кастомным суффиксом | ⚠️ | Guard требует ровно `*/LifeOS`; каталог `LifeOS & <тест>` purge не удаляет (безопасно, но пользователь не сможет очистить). **P2-3** |
| `install.sh` защищён от опасного `LIFEOS_APP_DIR` | ❌ | `install.sh:36` принимает любой путь; `copy_project` делает `rm -rf "$APP_DIR/src"` (`install.sh:328`). При `LIFEOS_APP_DIR="$HOME"` можно удалить `~/src`. **P2-3** |
| `ProgramArguments` / `WorkingDirectory` | ✅ | `ops/com.lifeos.agent.plist:14-20`; `/usr/bin/python3` заменяется на найденный (`install.sh:534-536`); относительный `src/main.py` разрешается от `WorkingDirectory` |
| `uninstall.sh` не удаляет данные без `--purge` | ✅ | `uninstall.sh:220-224`; проверено: config остался |
| `HOME` с пробелами | ✅ | Временные install/uninstall с `HOME=/tmp/lifeos … & <тест>/home` — rc=0, пути корректны |

### 1.6 Надёжность

| Проверка | Статус | Доказательство |
|---|---|---|
| Пустой токен не спамит лог | ❌ | **P1-2:** `main.py:273-286` пишет ERROR и `return 2`; plist `KeepAlive` (`ops/com.lifeos.agent.plist:23-24`) перезапускает каждые ~10 с. 3 запуска → 9 строк лога (INFO×2 + ERROR, плюс дубль stderr). При 401/`chat_id=0` то же |
| Pid-lock | ✅ | `src/main.py:65-105,371-378`; второй экземпляр rc=3, сообщение «уже запущен»; после SIGTERM pid-файл удалён |
| SIGTERM/SIGINT | ✅ | `src/main.py:380-391,451-460`; тест: rc=0, лог «Получен сигнал 15», БД закрыта, pid снят (задержка до ~2 с из-за long poll) |
| Ротация лога | ⚠️ | `RotatingFileHandler` 5 МБ×3 (`src/main.py:41-42,130-133`) + отдельная ротация в `install.sh:480-492`; plist пишет stderr в тот же `agent.log` (`plist:25-28`), а install.sh копирует/обнуляет файл — два независимых ротатора. **P2-10** |
| WAL и busy_timeout | ✅ | `src/storage.py:129-141`: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `timeout=10` |
| Read-only vault не теряет данные | ✅ | Тест: `chmod 500` на `01 Daily` → `write_entry=False`, `entries.written=0`, запись остаётся в SQLite, `sync_pending` вернул 0, `entries_unwritten` = 1 |
| LLM-таймаут | ✅ | `src/llm.py:192-194,258`; тест с сервером, спящим 4 с, `timeout=1` → `None` за 1.02 с, `TimeoutError` |
| LLM не блокирует цикл дольше таймаута | ⚠️ | Таймаут по умолчанию 60 с (`config.example.json:21`); вызовы из `scheduler.py:848,900` синхронные — до 60 с без getUpdates. **P2-6** |
| Смещение Telegram сохраняется | ❌ | **P2-5:** `offset` живёт только в памяти (`main.py:399,410,440`); в kv не пишется. После падения/рестарта возможна повторная выдача последнего батча и дубли записей/ответов |
| Сессия не теряет приглашение | ⚠️ | **P2-6:** `_start_session` сохраняет сессию и `last_slot` до `_send` (`scheduler.py:432-441`); если отправка упала, пользователь вопроса не увидел, а бот считает сессию активной |
| Двойной запуск не пишет в vault до lock | ⚠️ | `main.py:335-338` делает `sync_pending` до `PidLock.acquire` (`371-378`); второй процесс может успеть записать/пересобрать дашборд. **P2-7** |
| `--dry-run` не трогает данные | ⚠️ | `--dry-run` отключает только Telegram (`main.py:349-351`), но открывает БД, делает `sync_pending`, пишет vault и дашборд. README предлагает `--dry-run --once` как «проверку» (`README.md:302-307`). **P2-7** |
| Pid-файл устойчив к мусору | ⚠️ | `_pid_alive` (`main.py:50-62`) не ловит `OverflowError` для огромного pid; битый файл с `10**100` уронит запуск. **P2-11** |
| Ошибки в цикле ловятся | ✅ | `main.py:409-446` — TelegramError/Exception вокруг getUpdates и tick; scheduler.on_message в `_handle_updates` (`215-216`) |

### 1.7 Практика

| Проверка | Статус | Доказательство |
|---|---|---|
| `python3 -m compileall src` | ✅ | 3.13.14 — exit 0; `/usr/bin/python3` 3.9.6 — exit 0 (с `PYTHONPYCACHEPREFIX` вне sandbox) |
| Unit-тесты | ✅ | 150 тестов, OK на 3.13 и 3.9 |
| `bash -n install.sh`, `uninstall.sh` | ✅ | exit 0 |
| `plutil -lint ops/com.lifeos.agent.plist` | ✅ | OK |
| Grep-скрипты | ✅ | SQL-interpolation grep пусто; `innerHTML` — только статика; секреты в git/истории/README/plist — только плейсхолдеры |
| Документация Python | ⚠️ | `PROJECT_CONTEXT.md:14` утверждает `/usr/bin/python3` = 3.13; фактически 3.9.6. Installer это обходит (`/usr/local/bin/python3`), но текст неточен |

---

## 2. P1 — исправить до боевого запуска

### P1-1. Токен бота попадает в `agent.log` при опечатке в `telegram.api_base`

**Где:**
`src/config.py:171` — `api_base` проверяется только как непустая строка;
`src/telegram.py:120-121` — токен вставляется в URL;
`src/telegram.py:133-143` — `urllib.request.Request(...)` создаётся **до** `try`;
`src/main.py:435-437` — `logger.exception("Неожиданная ошибка при опросе Telegram: %s", exc)`;
`src/scheduler.py:254-266` — `self.log.warning("Telegram: отправка не удалась (%s)", exc)`.

**Воспроизведение (без реального токена):**

```json
{"telegram": {"token": "123456789:FAKE-TOKEN-for-audit",
              "allowed_chat_id": 42,
              "api_base": "api.telegram.org"}}
```

`python3 src/main.py --once` → в `agent.log`:

```
ERROR lifeos: Неожиданная ошибка при опросе Telegram:
unknown url type: 'api.telegram.org/bot123456789:FAKE-TOKEN-for-audit/getUpdates'
...
WARNING lifeos.scheduler: Telegram: отправка не удалась
(unknown url type: '.../bot123456789:FAKE-TOKEN-for-audit/sendMessage')
```

Токен также попадает в traceback (три вхождения в одном запуске). Причина —
`ValueError` из `urllib` не превращается в `TelegramError`: `Request` стоит до
`try` (`telegram.py:133`), а внешний `except Exception` логирует текст исключения.

**Фикс:**
1. В `config._validate` требовать `urlparse(api_base).scheme in {"http","https"}`,
   непустой `netloc`; для `llm.base_url` — то же (см. P2-2).
2. В `Telegram.__init__`/`call` ловить `ValueError` вокруг `Request`/`urlopen` и
   бросать `TelegramError("Некорректный telegram.api_base — проверьте схему и хост")`
   **без URL**; `scheduler._send` не логировать `str(exc)` от неизвестных исключений,
   только тип/код.
3. Регрессионный тест: токен-маркер + `api_base` без схемы → в логе маркера нет.

**Обход сейчас:** перед запуском убедиться, что `api_base` начинается с `https://`
(и `llm.base_url` — тоже). Это не отменяет фикс.

---

### P1-2. `KeepAlive` + фатальная ошибка конфига = лог-спам и перезапуск каждые ~10 с

**Где:**
`ops/com.lifeos.agent.plist:22-24` — `RunAtLoad` + `KeepAlive=true`;
`src/main.py:273-286` — при пустом `token`/`chat_id=0` пишет ERROR и `return 2`;
`src/main.py:421-430` — после трёх 401/403 `exit_code=1` и выход;
`src/main.py:109-144` — логгер пишет и в файл, и в stderr.

**Факт:** три запуска `--once` с пустым токеном дали 9 строк в `agent.log`
(2 INFO + 1 ERROR на запуск; ERROR ещё дублируется через stderr, который launchd
пишет в тот же файл). `launchd` с `KeepAlive=true` перезапускает процесс после
любого выхода, throttle по умолчанию ~10 с → ~26 тыс. строк/сутки, ротация 5 МБ
быстро забивается. То же при неверном/отозванном токене и при `chat_id=0`.
`install.sh` сам не запускает агент при пустом токене (`start_or_hold`,
`install.sh:619-640`), поэтому первый запуск чистый; проблема возникает после
падения/рестарта с плохим конфигом.

**Фикс (любой из):**
- `KeepAlive` заменить на `<dict><key>SuccessfulExit</key><false/></dict>`,
  а фатальные ошибки конфига завершать кодом 0 (тогда launchd не перезапускает),
  ошибки-краши — ненулевым; добавить `ThrottleInterval` 60.
- Либо в `main` при фатальной конфиг-ошибке не выходить, а спать с проверкой
  сигнала/перечитыванием конфига раз в N минут (не спамить).
- Развести `StandardErrorPath` и файл логгера (например, `agent.stderr.log`),
  чтобы не дублировать ERROR.

---

### P1-3. README обещает «данные никуда не уходят», а LLM отправляет тексты на `api.samp.team`

**Где:**
`README.md:7-8`: «Никакие данные, кроме сообщений в Telegram, никуда не уходят».
Фактически:
`src/scheduler.py:894-906` — свободный текст уходит в `self.llm.chat(...)`;
`src/parsing.py:672-676` — каждый ответ уходит в `json_chat` для извлечения;
`src/llm.py:234-259` — POST на `base_url` с `messages` (текст пользователя,
история диалога);
`config/config.example.json:16-22` — `llm.enabled=true`;
`install.sh:450-477` — установщик сам подставляет `DDD_API_KEY`, если он есть.

Для не-программиста это существенно: включённый по умолчанию LLM = передача
личных записей (еда, сон, настроение, мысли) стороннему шлюзу. Это не «утечка»
в смысле взлома, но неверное информирование о приватности.

**Фикс:**
- Исправить README: явно указать, что при `llm.enabled=true` тексты ответов и
  история идут в `api.samp.team`; без ключа/при `enabled=false` — только локально.
- Сделать LLM **opt-in** (по умолчанию `enabled=false`) либо спрашивать
  подтверждение при установке, если найден `DDD_API_KEY`.
- В `/status`/README показать текущий режим LLM.

---

## 3. P2 — желательно исправить

### P2-1. `data/` 755 и БД 644
`install.sh:311-314` создаёт `"$APP_DIR/data"`, но `chmod 700` применяет только к
`APP_DIR`, `CONFIG_DIR`, `LOG_DIR`. Факт: `data` — 755, `lifeos.db` — 644.
Родитель `APP_DIR` — 700, поэтому практически данные закрыты, но при смене прав
родителя/копировании каталога БД читаема. **Фикс:** `chmod 700 "$APP_DIR/data"`,
`umask 077` в install/plist (`Umask` key), `chmod 600` для `lifeos.db` после
создания (или `Storage` создаёт с 600).

### P2-2. Нет проверки схемы URL; `http://` допустим
`src/config.py:171,182` — только `_expect_str`. `http://api.telegram.org` отправит
токен открытым текстом; `http://api.samp.team` — ключ и тексты. **Фикс:** разрешать
`https` всегда, `http` — только для `127.0.0.1`/`localhost` (моки); проверять
`urlparse().netloc`; `--mock-url` — только loopback.

### P2-3. `install.sh` не защищает `LIFEOS_APP_DIR`
`install.sh:36` принимает любой путь, `copy_project` делает `rm -rf "$APP_DIR/src"`
(`install.sh:328`). При `LIFEOS_APP_DIR="$HOME"` можно удалить `~/src`. **Фикс:**
такой же guard, как в `uninstall.sh:94-107` (заканчивается на `/LifeOS`, не `$HOME`,
не `/`), либо разрешать только путь по умолчанию/явно подтверждённый.

### P2-4. Нет containment для путей Obsidian
`src/obsidian.py:395-401` (`_vault_abs`) принимает абсолютные пути и `..`;
`daily_folder="../../escape"` в тесте дал файл вне vault. Конфиг 600 и доверенный,
но случайная правка/копипаст может писать вне базы. **Фикс:** после `resolve()`
проверять `os.path.commonpath([target, vault]) == vault`; для `template_path`
разрешить чтение вне vault отдельным флагом.

### P2-5. `offset` Telegram не сохраняется
`src/main.py:399,410,440`: offset только в памяти. После падения/рестарта
`getUpdates` может повторно отдать последний батч → дубли `entries`/ответов.
**Фикс:** `kv_set("tg_offset", offset)` после обработки батча, загружать при старте;
дедуп по `update_id` (хранить последний).

### P2-6. Сессия и LLM: потеря приглашения и блокировка цикла
`src/scheduler.py:432-441` сохраняет сессию и `last_slot` **до** `_send`; если
отправка не удалась, пользователь не получил вопрос, а бот ждёт ответ. Плюс
`self.llm.chat` (`scheduler.py:848,900`) синхронный, таймаут 60 с — getUpdates
стоит. **Фикс:** отмечать `last_slot`/сессию после успешной отправки (или retry);
для интерактивных вызовов LLM — таймаут 10–15 с.

### P2-7. `--dry-run` и `--once` пишут в БД/vault; lock берётся поздно
`--dry-run` отключает только Telegram (`main.py:349-351`), но `sync_pending`
(`main.py:335-338`), `scheduler.tick` → `rewrite_goals`/`write_mirror` и
`save_answer` пишут в БД/vault. README предлагает `--dry-run --once` как проверку
(`README.md:302-307`). Кроме того, `sync_pending` выполняется **до** `PidLock.acquire`
(`main.py:371-378`), поэтому второй процесс может успеть записать. **Фикс:** в
`--dry-run` не писать vault/дашборд (или отдельный `--check`); pid-lock брать сразу
после конфига, до Storage/Vault.

### P2-8. Плейсхолдеры шаблона заменяются внутри JSON-данных
`src/dashboard_data.py:593-605` делает `page.replace` для `{{APP_NAME}}`,
`{{GENERATED_AT}}` и т.д. уже после вставки `{{LIFEOS_DATA}}`. Текст пользователя
`{{APP_NAME}}` превращается в имя приложения (в `data.json` остаётся правильно).
**Фикс:** сначала подставлять все плейсхолдеры шаблона, `{{LIFEOS_DATA}}` — последним;
или экранировать `{{` в payload.

### P2-9. `<script>`-контекст: экранируется только `</`
`src/dashboard_data.py:579` заменяет `</` → `<\/`. Пейлоад `<!--<script>` не
исполняется (нет `innerHTML`), но по HTML-спеке может включить double-escaped
state и «съесть» закрывающий `</script>`, сломав страницу. **Фикс:** сериализовать
JSON с `replace("<", "\\u003c")` (и при желании `>`/`&`), а не только `</`.

### P2-10. Две ротации одного лога
`src/main.py:130-133` — `RotatingFileHandler`; `install.sh:480-492` — копирование
и `: >`; `plist:25-28` — launchd пишет stderr в тот же файл. После ротации
`RotatingFileHandler` переименовывает файл, а launchd продолжает писать в старый
inode (`agent.log.1`), плюс возможны ложные срабатывания `tell()`. **Фикс:** один
механизм: либо Python-ротация + отдельный stderr, либо newsyslog/launchd; убрать
ручную ротацию из install.sh.

### P2-11. Pid-lock: порядок, гонка, мусор
`PidLock.acquire` (`main.py:73-94`) — check-then-write без `O_EXCL`; два старта
могут оба пройти. `_pid_alive` (`main.py:50-62`) не ловит `OverflowError` для
огромного pid из битого файла. **Фикс:** `os.open(path, O_CREAT|O_EXCL)` или
`fcntl.flock`; `int(raw)` ограничивать; ловить `OverflowError`.

### P2-12. Мелочи
- `install.sh:475` — ключ в argv Python на мгновение; передавать через stdin.
- `--dry-run` пишет полные тексты в лог (`main.py:173`), а `_handle_add_goal`
  логирует название цели (`scheduler.py:755`) — личные данные в `agent.log`;
  для launchd-логов это допустимо, но стоит предупредить/сократить.
- Нет CSP в шаблоне; для локального файла не критично, но `default-src 'none'`
  с inline-style/script — полезная страховка.
- `README`/`PROJECT_CONTEXT` утверждают Python 3.13 у `/usr/bin/python3`;
  фактически 3.9.6 (installer выбирает `/usr/local/bin/python3`). Поправить текст.

---

## 4. Что проверено положительно (детали)

### 4.1 Секреты
- `Telegram.__repr__` — `token=***` (`telegram.py:116-117`); `TelegramError`
  при 401/429/409/500 не содержит токена (тест с маркером).
- `LLM.__repr__` — без ключа (`llm.py:211-212`); при ошибке логируется только
  `http_401`/`URLError` (`llm.py:260-273`), тело ответа не логируется.
- `install.sh` при подстановке ключа печатает только «подхвачен из creds.yaml»,
  не сам ключ (тест с `sk-TESTKEY…`: в stdout ключа нет).
- `.gitignore` закрывает `config/config.json`, `data/`, `dashboard/index.html`,
  `demo/index.html`, `tests/tmp/`, `*.log`; `git ls-files` и история — без
  реальных секретов. В README только шаблон `1234567890:AAHxxxx…`.

### 4.2 SQL
Единственный путь к SQLite — `Storage._execute` (`storage.py:145-152`), все
значения передаются кортежем `params`. Динамические части:
`mark_written` — `",".join("?" ...)` (`253-256`); `upsert_goal` — имена колонок
из фиксированного `_GOAL_FIELDS` (`316-330`, `350-353`). Тест с
`x'); DROP TABLE entries; --` во всех полях: таблицы/записи целы, `DROP` не
выполнился (trace показывает литерал как данные, а не SQL).

### 4.3 XSS
Сгенерирован `dashboard/index.html` с записями:
`<script>alert(1)</script>`, `"><img src=x onerror=alert(1)>`,
`</script><svg onload=alert(1)>`, `<!--<script>alert(1)</script>-->`,
`<!--<script>`, `javascript:alert(1)`, `{{APP_NAME}}`; goal/habit/app.name — с
HTML. Результат:
- в `window.LIFEOS_DATA` нет `</script` (есть `<\/script`), закрывающих
  `</script>` в файле ровно 2 (шаблон);
- все данные рендерятся через `el()` → `textContent` (`template.html:644-647`);
- `innerHTML` — только `""`, `EMPTY_ICON` и статические SVG (`750,932,1434`);
- `app.name` в `<title>/<h1>/<footer>` — `&lt;img …&gt;` (`dashboard_data.py:595-598`);
- исполняемого инжекта нет. Остаётся P2-9 (`<!--<script>`) и P2-8 (плейсхолдеры).

### 4.4 Сеть/TLS
- `https://api.telegram.org` — HTTP 200, TLS 1.3, CN `api.telegram.org`;
- `https://api.samp.team/v1/models` — HTTP 401 `Missing API key` (не 403/1010),
  TLS 1.3, CN `samp.team`;
- `Telegram.call(getMe)` с фейковым токеном — 401, сообщение без токена;
- TLS-контекст — стандартный, без отключения проверки; фиксированный UA
  (`LifeOS/1.0`) не ломает handshake (заголовок идёт после TLS).

### 4.5 Надёжность (тесты)
- pid-lock: второй экземпляр rc=3; после SIGTERM первый rc=0, pid-файл удалён,
  в логе «Получен сигнал 15» и «Life OS остановлен».
- read-only vault: `write_entry=False`, `entries.written=0`, запись в SQLite
  осталась, `sync_pending=0`, `entries_unwritten=1`.
- LLM timeout: сервер спит 4 с, `timeout=1` → `None` за 1.02 с.
- `install.sh`/`uninstall.sh`: временная установка с путями, содержащими
  пробелы, кириллицу, `&`, `<`, `>`; `config.json` 600; plist корректно
  экранирован и распарсен; purge с точным `.../LifeOS` удаляет только его,
  соседний sentinel остаётся.
- `compileall` и 150 тестов — OK на 3.13.14 и 3.9.6; `bash -n` и `plutil -lint` — OK.

---

## 5. Итог: можно ли ставить на Mac

**Да, условно.** P0 нет; после закрытия P1-1/P1-2 (или обходных условий) и
исправления текста P1-3 система пригодна для личного использования на одном Mac.

Обязательные условия:

1. **Токен и chat_id.** В `config.json` — реальный `telegram.token` и
   `allowed_chat_id`; `api_base` — строго `https://api.telegram.org`.
   `llm.base_url` — строго `https://api.samp.team/v1`, если LLM включён.
2. **Права.** `config/config.json` — 600 (installer), `data/` — вручную
   `chmod 700 "$HOME/Library/Application Support/LifeOS/data"` до фикса P2-1;
   не делать каталог установки 755.
3. **Full Disk Access.** Дать его интерпретатору, который печатает installer
   (`/usr/local/bin/python3` на этой машине), иначе запись в
   `~/Documents/чисто лайфик обсидиан/...` получит `Operation not permitted`.
   Перед первым запуском — бэкап vault (он уже есть от 2026-11-15).
4. **LLM/приватность.** Если не хочешь отправлять тексты на `api.samp.team`,
   поставь `"llm": {"enabled": false}` и очисти `api_key`; бот будет работать
   в скриптованном режиме. Если оставляешь — знай, что ответы и история уходят
   на шлюз (P1-3).
5. **Эксплуатация.** Не запускай второй экземпляр вручную; не делай
   `--dry-run --once` на живой базе (он пишет vault, P2-7); не публикуй
   `agent.log` (в нём могут быть тексты/цели, а до фикса P1-1 — потенциально
   токен); после правки `config.json` — `bash install.sh --reload`.
6. **Проверка.** `launchctl list | grep lifeos` показывает PID; в логе нет
   `401/409`; `/status` отвечает; тестовая запись появилась в daily note,
   `entries.written=1`.

**Когда ставить не стоит:** если нужен «полностью офлайн» режим — только с
`llm.enabled=false`; если планируется несколько пользователей/чатов — текущая
модель одного `allowed_chat_id` и `_is_allowed` при `chat_id=0` (dev-режим,
`src/scheduler.py:624-630`) не подходит; если нельзя дать Full Disk Access —
запись в Obsidian будет только в БД (`written=0`), это не потеря, но и не
рабочий сценарий.

---

## 6. Приложение: команды проверки

```bash
# синтаксис/тесты
python3 -m compileall -q src                       # exit 0
PYTHONPYCACHEPREFIX=/tmp/pyc /usr/bin/python3 -m compileall -q src  # 3.9, exit 0
python3 -m unittest discover tests                 # 150 tests, OK
bash -n install.sh && bash -n uninstall.sh         # OK
plutil -lint ops/com.lifeos.agent.plist            # OK

# SQL: нет интерполяции
grep -rnE "execute\((f[\"']|.*%|.*\.format\()" src --include='*.py'   # пусто

# секреты: git/история/рабочее дерево
git ls-files | grep -E '^(config/config\.json|data/|dashboard/index\.html|demo/index\.html)'  # пусто
git log -p --all | grep -E 'sk-[A-Za-z0-9]{16,}|[0-9]{8,10}:[A-Za-z0-9_-]{30,}'             # пусто

# XSS: генерация с payload'ами (в temp, не в живой базе)
#   build_dashboard(cfg, storage) → dashboard/index.html
#   проверка: "</script" в JSON = False, "<\\/script" = True, textContent в шаблоне

# TLS
python3 -c '... urlopen("https://api.telegram.org") ...'        # 200, TLS 1.3
python3 -c '... urlopen("https://api.samp.team/v1/models") ...' # 401 Missing API key

# права после временной установки
stat -f '%Sp %N' "$APP/config/config.json" "$APP/data" "$APP/data/lifeos.db"
# -rw------- config.json; drwxr-xr-x data; -rw-r--r-- lifeos.db  → P2-1

# token leak (воспроизведение P1-1)
# config: api_base="api.telegram.org"; python3 src/main.py --once
# grep -c "bot<TOKEN>" data/logs/agent.log  → >0
```

**Ограничение аудита:** реальный `launchctl bootstrap` не выполнялся (чтобы не
ставить агент в систему); использовался подставной `launchctl` и временный
`HOME`. Поведение самого launchd с `KeepAlive` оценено по plist и документации,
а лог-спам воспроизведён запусками `main.py --once` (каждый запуск = один
рестарт).
