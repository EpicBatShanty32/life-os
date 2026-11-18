# Life OS v3 — развёртывание

Два пути: **(А)** VPS с Docker Compose и настоящим доменом, **(Б)** свой мини-сервер
(или этот Mac) без открытых портов через Tailscale / Cloudflare Tunnel.
Вход в обоих — по токену владельца, с iPhone ставится «на экран Домой» как PWA.

> **Главное про честность.** Файлы в `deploy/` написаны по `MASTER_SPEC.md`
> (§0 «Реальность среды», §1, §2, §6) и проверены **только статически**:
> синтаксис YAML, наличие сервисов/томов, инструкции Dockerfile, директивы
> Caddyfile, секции systemd-юнитов, покрытие переменных `.env.example`.
> Ни один контейнер, сертификат и юнит здесь не запускался — docker и systemd
> в этой среде отсутствуют. Подробный список — §10.

---

## 1. Что понадобится

| Сценарий | Минимум | Домен | Открытые порты |
|---|---|---|---|
| А — VPS | 2 vCPU, 2 ГБ RAM, 20 ГБ SSD, Ubuntu 24.04 | нужен | 80, 443 (TCP+UDP для HTTP/3) |
| Б — мини-сервер | любой Linux x86/ARM или этот Mac | не нужен | никаких (туннель исходящий) |

Для сборки образов нужно ~3 ГБ диска сверх образов (node_modules + слой pip).
Если включить `WITH_WHISPER=true`, добавьте ~1.5 ГБ на образ и ~1 ГБ на модель.

Код Life OS v3 сдаётся параллельно. На момент написания `app/` уже появился
частично — Фаза 1: `app/server/settings.py`, `db.py`, `models.py`, каркас
`app/web` (vite), `app/tests`. Ещё нет: `app/server/main.py` (точка входа API),
`app/bot/main.py`, `app/worker/`, `app/web/dist` (сборка SPA). Поэтому
`docker compose build` сейчас упадёт на `COPY app/server`/`COPY app/bot` —
это ожидаемо, а не дефект артефактов.
Сначала `python3 -m pytest app/tests -q` и живой `uvicorn`, потом деплой.

---

## 2. Путь А — VPS с Docker Compose

### 2.1 Сервер и DNS

1. Создайте VPS (Ubuntu 24.04) и добавьте свой SSH-ключ.
2. В DNS у регистратора: `A` запись `lifeos.example.ru → IP сервера`.
   Если есть IPv6 — добавьте `AAAA`. Проверьте **до** первого старта:

```bash
dig +short lifeos.example.ru      # должен вернуть IP сервера
```

Caddy выпускает сертификат Let's Encrypt сам, но только когда DNS уже работает:
иначе первый старт получит ошибку ACME, и повтор будет с задержкой.

### 2.2 Docker на сервере

На чистой Ubuntu:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"     # затем перелогиниться
docker compose version              # нужен v2.20+ (для ${VAR:?} и healthcheck)
```

Docker здесь **не проверен** — в среде сборки его нет (см. §9.1).

### 2.3 Код и `.env`

```bash
sudo mkdir -p /srv/lifeos && sudo chown "$USER": /srv/lifeos
git clone <ваш-репозиторий> /srv/lifeos && cd /srv/lifeos

cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
openssl rand -hex 32                 # вставьте в LIFEOS_TOKEN
$EDITOR deploy/.env
```

Заполнить обязательно (compose упадёт с понятным текстом, если пропущено):
`LIFEOS_DOMAIN`, `ACME_EMAIL`, `LIFEOS_TOKEN`, `ADMIN_TELEGRAM_ID`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`.
Остальное — по желанию: LLM (`LIFEOS_LLM_*`), транскрибация (`WITH_WHISPER`).

Про имена: в `.env` используются короткие имена из ТЗ (`TELEGRAM_BOT_TOKEN`,
`ADMIN_TELEGRAM_ID`, `LIFEOS_VAULT_DIR`), а `app/server/settings.py` читает
`LIFEOS_TELEGRAM_TOKEN`, `LIFEOS_ADMIN_TELEGRAM_ID`, `LIFEOS_VAULT`.
Compose передаёт **оба** набора (блок `x-lifeos-common-env`), поэтому в
контейнере находятся любые. Заполнять `.env` нужно по коротким именам.

Токен владельца — это **единственный** пароль системы. Он живёт в `LIFEOS_TOKEN`,
в репозиторий не попадает (`.gitignore` → `deploy/.env`).

### 2.4 Сборка и запуск

```bash
cd /srv/lifeos
docker compose -f deploy/docker-compose.prod.yml up -d --build
docker compose -f deploy/docker-compose.prod.yml ps
```

Перед первым стартом прогоните валидацию конфига (Caddy её умеет без запуска
прокси; это единственный способ проверить синтаксис без моего участия):

```bash
docker compose -f deploy/docker-compose.prod.yml run --rm --entrypoint caddy caddy \
  validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Ожидаемый порядок сервисов: `telegram-bot-api` → `lifeos-api` (healthy) →
`lifeos-bot`, `lifeos-worker`, `lifeos-web` → `caddy`.
Первый старт Caddy занимает 10–60 секунд: ACME-запрос и выпуск сертификата.

Логи:

```bash
docker compose -f deploy/docker-compose.prod.yml logs -f caddy lifeos-api
```

### 2.5 Проверка после запуска

```bash
# 1. Сертификат и редирект
curl -sI https://lifeos.example.ru | head -5

# 2. Заголовки безопасности (должны быть все три)
curl -sI https://lifeos.example.ru | grep -Ei 'strict-transport|x-content-type|referrer-policy'

# 3. API отвечает 401 без токена — это правильно (MASTER_SPEC §2.6)
curl -s -o /dev/null -w '%{http_code}\n' https://lifeos.example.ru/api/overview

# 4. С токеном — 200 и JSON
curl -s -H "X-LifeOS-Token: $LIFEOS_TOKEN" https://lifeos.example.ru/api/overview | head -c 300

# 5. Сжатие
curl -sI -H 'Accept-Encoding: zstd, gzip' https://lifeos.example.ru/ | grep -i content-encoding

# 6. Лимит тела: 8 МБ на JSON-роут (ожидается 413)
head -c 9000000 /dev/zero | curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST --data-binary @- -H "X-LifeOS-Token: $LIFEOS_TOKEN" \
  https://lifeos.example.ru/api/quick
```

---

## 3. Путь Б — домашний мини-сервер, без открытых портов

Тот же код, но без Docker: systemd-юниты `deploy/lifeos.service` и
`deploy/lifeos-bot.service`, доступ через туннель. Порты в интернет **не**
пробрасываются — роутер трогать не нужно.

### 3.1 Что поднять на машине (Linux)

```bash
# пользователь и каталоги
sudo useradd --system --home /srv/lifeos --shell /usr/sbin/nologin lifeos
sudo mkdir -p /srv/lifeos/{app,data,media,vault} /etc/lifeos
sudo chown -R lifeos:lifeos /srv/lifeos
sudo chmod 750 /srv/lifeos

# код
sudo -u lifeos git clone <репозиторий> /srv/lifeos/app
sudo -u lifeos python3 -m venv /srv/lifeos/.venv
sudo -u lifeos /srv/lifeos/.venv/bin/pip install -U pip
# если app/requirements.txt уже сдан (его ещё нет на 2026-11-17):
sudo -u lifeos /srv/lifeos/.venv/bin/pip install -r /srv/lifeos/app/app/requirements.txt 2>/dev/null \
  || sudo -u lifeos /srv/lifeos/.venv/bin/pip install \
       'fastapi~=0.162' 'uvicorn[standard]~=0.54' 'pydantic~=2.13' 'httpx~=0.28' \
       'aiogram~=3.31' 'python-multipart~=0.0.20'

# сборка SPA (Node 24). package-lock.json пока не закоммичен — npm ci упадёт,
# поэтому install; после появления lock-файла используйте npm ci.
cd /srv/lifeos/app/app/web && sudo -u lifeos npm install && sudo -u lifeos npm run build

# переменные: те же имена, что в deploy/.env.example
sudo install -m 600 -o lifeos -g lifeos deploy/.env.example /etc/lifeos/lifeos.env
sudo -u lifeos $EDITOR /etc/lifeos/lifeos.env
```

Правки в `/etc/lifeos/lifeos.env` для этого пути: `LIFEOS_DATA_DIR=/srv/lifeos/data`,
`LIFEOS_MEDIA_DIR=/srv/lifeos/media`, `LIFEOS_VAULT_DIR=/srv/lifeos/vault`,
`TELEGRAM_API_BASE` — пусто (облачный Telegram) или `http://127.0.0.1:8081`.

```bash
sudo cp deploy/lifeos.service deploy/lifeos-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifeos.service lifeos-bot.service
systemctl status lifeos.service --no-pager
journalctl -u lifeos.service -f
```

**Если «мини-сервер» — это этот Mac**, systemd нет. Варианты: (1) Linux-ВМ
(Ubuntu в UTM/Parallels) и всё выше; (2) тот же набор руками:
`nohup`/`launchd` с `ExecStart`-эквивалентом
`/srv/lifeos/.venv/bin/uvicorn app.server.main:app --host 127.0.0.1 --port 8787`
(образец юнита — `ops/com.lifeos.agent.plist` от v0.x). Юниты из `deploy/`
на macOS не проверялись.

### 3.2 Туннель: Tailscale (проще, только для своих устройств)

```bash
# Linux
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh

# публикуем локальный API по HTTPS с сертификатом Tailscale
tailscale serve --bg 8787
tailscale serve status
```

Адрес будет вида `https://miniserver.tailXXXX.ts.net` — он уже с валидным
сертификатом (нужен включённый HTTPS в админке Tailscale и MagicDNS).
Синтаксис `serve` менялся между версиями: если `--bg 8787` не принимается,
посмотрите `tailscale serve --help` и используйте
`tailscale serve --bg https / http://127.0.0.1:8787`.

Плюс: снаружи портов нет, доступ только с устройств в вашем tailnet.
Минус: на iPhone нужен установленный Tailscale.

### 3.3 Туннель: Cloudflare Tunnel (доступ из любого браузера)

```bash
brew install cloudflared            # на macOS; на Linux — пакет из репозитория Cloudflare
cloudflared tunnel login
cloudflared tunnel create lifeos
cloudflared tunnel route dns lifeos lifeos.example.ru
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: lifeos
credentials-file: /home/user/.cloudflared/<uuid>.json
ingress:
  - hostname: lifeos.example.ru
    service: http://127.0.0.1:8787
  - service: http_status:404
```

```bash
sudo cloudflared service install    # автозапуск
cloudflared tunnel run lifeos        # или проверить вручную
```

Домен должен быть на Cloudflare (бесплатный план подходит). Дополнительно можно
включить **Cloudflare Access** (Zero Trust → Applications) — тогда перед Life OS
будет второй барьер: почта/OTP. Это не заменяет `LIFEOS_TOKEN`, а дополняет.

Туннель отдаёт только API; статику SPA раздаёт uvicorn из `app/web/dist`
(MASTER_SPEC §0.5) — отдельный nginx для пути Б не нужен.

---

## 4. Obsidian-vault: том или bind-mount

По умолчанию в `deploy/docker-compose.prod.yml` vault — именованный том
`lifeos-vault` (внутри Docker, снаружи не виден). Чтобы писать в **живую**
базу Obsidian, замените в сервисах `lifeos-api`, `lifeos-bot`, `lifeos-worker`:

```yaml
    volumes:
      - data:/srv/lifeos/data
      - media:/srv/lifeos/media
      - ${OBSIDIAN_VAULT_PATH}:/srv/lifeos/vault   # вместо vault:/srv/lifeos/vault
```

и раскомментируйте `OBSIDIAN_VAULT_PATH` в `deploy/.env`. Путь на хосте должен
быть доступен на запись пользователю с uid **10001** (это uid внутри образа):

```bash
sudo chown -R 10001:10001 "/Users/you/Documents/Obsidian/Мой vault"
```

Для systemd-варианта то же самое делает `ReadWritePaths=/srv/lifeos/vault`
в юните: если база лежит вне `/srv/lifeos`, добавьте её путь в эту строку,
иначе запись упадёт с `Permission denied` (ProtectSystem=strict).

---

## 5. iPhone: PWA «на экран Домой»

1. Откройте `https://lifeos.example.ru` **в Safari** (не в Chrome — на iOS
   «на экран Домой» умеет только Safari).
2. Введите `LIFEOS_TOKEN` в поле входа.
3. «Поделиться» → **«На экран „Домой“»**. Иконка появится как у приложения,
   запуск — без адресной строки.
4. Проверьте: приложение открывается на весь экран, нижняя навигация
   (MASTER_SPEC §2.6) видна, данные грузятся.

Требования, без которых PWA на iPhone не работает:

| Что | Почему |
|---|---|
| HTTPS с валидным сертификатом | без него service worker и установка недоступны |
| `manifest.webmanifest` + иконки 180×180 и 512×512 | иначе «Домой» поставит скриншот |
| cookie сессии `Secure; SameSite=Lax` (или `None` для iframe) | Safari блокирует insecure-cookie |
| `index.html` и `sw.js` без кэша | иначе после обновления остаётся старая оболочка (это уже настроено в `deploy/nginx-web.conf`) |

После обновления на сервере на iPhone: закрыть приложение из переключателя,
открыть заново. Если оболочка всё равно старая — удалить иконку и добавить снова.
Push-уведомления на iOS требуют 16.4+ и установки на экран Домой.

---

## 6. Вход по токену владельца

Как это устроено в v3 (MASTER_SPEC §2.6):

- Браузер один раз отправляет `LIFEOS_TOKEN` на `POST /api/auth/login`,
  сервер ставит `HttpOnly; Secure; SameSite=Lax` cookie.
- API принимает также заголовок `X-LifeOS-Token` — для `curl` и скриптов.
- Всё остальное без токена — `401`. Паролей в открытом виде в репозитории нет.
- Смена `LIFEOS_TOKEN` + перезапуск `lifeos-api` = выход на всех устройствах.

Проверка «правда ли защищено» с чужой машины:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://lifeos.example.ru/api/journal   # 401
curl -s -o /dev/null -w '%{http_code}\n' https://lifeos.example.ru/api/journal \
  -H "X-LifeOS-Token: $LIFEOS_TOKEN"                                             # 200
```

Не храните токен в истории shell: `export LIFEOS_TOKEN=...` живёт до закрытия
терминала, а `~/.zsh_history` — нет. Лучше подставлять из `deploy/.env`:
`export LIFEOS_TOKEN=$(grep ^LIFEOS_TOKEN deploy/.env | cut -d= -f2)`.

---

## 7. Бэкапы

### 7.1 Что бэкапить

| Что | Где лежит | Как часто | Почему важно |
|---|---|---|---|
| `lifeos.db` (+ `-wal`, `-shm`) | том `data` | ежедневно | весь журнал, метрики, курсы, настройки |
| `media/` | том `media` | раз в неделю (инкрементально) | оригиналы видео/аудио, их не восстановить |
| `vault/` | том `vault` / ваша Obsidian-база | ежедневно | заметки; при bind-mount это ваша живая база |
| `caddy_data` | том `caddy_data` | раз в месяц | сертификаты и ACME-аккаунт (иначе новая выдача) |
| `.env` | `deploy/.env` | при изменении | без него не поднимется ничего |

Тома `vault`/`media` не дублируйте в cloud-хранилище руками: у Obsidian своя
синхронизация (iCloud/Obsidian Sync) — убедитесь, что она не пишет в тот же
файл, пока Life OS пишет туда же (v3 пишет идемпотентно по маркерам, но
конфликты синка всё равно возможны).

### 7.2 Куда

- внешний диск / NAS (`BACKUP_DIR=/Volumes/Backup/lifeos`);
- вторая машина по `rsync -a`;
- объектное хранилище через `rclone` (Backblaze B2, S3, Яндекс.Диск);
- **правило 3-2-1**: две копии на разных носителях, одна — вне дома.

Шифрование: `age` или `gpg` — база содержит личные записи о здоровье.
`caddy_data` считать секретом: там приватные ключи TLS.

### 7.3 Скрипт бэкапа (SQLite — только через `.backup`)

Копировать `lifeos.db` через `cp` при работающем приложении нельзя: получите
битую БД (WAL). Правильно — `sqlite3 .backup` или `VACUUM INTO`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /srv/lifeos
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="${BACKUP_DIR:-/srv/backup}/lifeos-$STAMP"
mkdir -p "$OUT"

# 1. База: горячий бэкап из контейнера (путь А)
docker compose -f deploy/docker-compose.prod.yml exec -T lifeos-api \
  python -c "import sqlite3; src=sqlite3.connect('/srv/lifeos/data/lifeos.db'); \
             dst=sqlite3.connect('/srv/lifeos/data/backup-snapshot.db'); src.backup(dst); dst.close(); src.close()"
docker compose -f deploy/docker-compose.prod.yml cp \
  lifeos-api:/srv/lifeos/data/backup-snapshot.db "$OUT/lifeos.db"
docker compose -f deploy/docker-compose.prod.yml exec -T lifeos-api \
  rm -f /srv/lifeos/data/backup-snapshot.db

# 2. Медиа и vault (архивом, чтобы не плодить тысячи файлов)
docker run --rm -v lifeos-media:/src:ro -v "$OUT":/out alpine \
  tar -czf /out/media.tar.gz -C /src .
docker run --rm -v lifeos-vault:/src:ro -v "$OUT":/out alpine \
  tar -czf /out/vault.tar.gz -C /src .

# 3. Конфиг (секрет — шифруем)
tar -czf "$OUT/env.tar.gz" -C /srv/lifeos deploy/.env

# 4. Сертификаты
docker run --rm -v lifeos-caddy-data:/src:ro -v "$OUT":/out alpine \
  tar -czf /out/caddy_data.tar.gz -C /src .

# 5. Отпечаток для проверки целостности
( cd "$OUT" && shasum -a 256 ./* > SHA256SUMS )

# 6. Ротация: оставить последние BACKUP_KEEP архивов
KEEP="${BACKUP_KEEP:-14}"
ls -1dt "${BACKUP_DIR:-/srv/backup}"/lifeos-* | tail -n +$((KEEP+1)) | xargs -r rm -rf
```

Положите в cron: `17 4 * * * /srv/lifeos/ops/backup.sh >> /var/log/lifeos-backup.log 2>&1`.
Для пути Б тот же скрипт, но без `docker compose` — прямой
`sqlite3 /srv/lifeos/data/lifeos.db ".backup '$OUT/lifeos.db'"` и `tar` по каталогам.

### 7.4 Восстановление

```bash
cd /srv/lifeos
docker compose -f deploy/docker-compose.prod.yml down          # остановить всё

# 1. Проверить, что архив цел
shasum -a 256 -c /path/to/lifeos-20261117-041700/SHA256SUMS

# 2. Вернуть базу (том создаётся первым старт��м api)
docker compose -f deploy/docker-compose.prod.yml up -d lifeos-api
docker compose -f deploy/docker-compose.prod.yml cp \
  /path/to/lifeos-.../lifeos.db lifeos-api:/srv/lifeos/data/lifeos.db
docker compose -f deploy/docker-compose.prod.yml exec -T lifeos-api \
  chown 10001:10001 /srv/lifeos/data/lifeos.db
docker compose -f deploy/docker-compose.prod.yml exec -T lifeos-api \
  python -c "import sqlite3; c=sqlite3.connect('/srv/lifeos/data/lifeos.db'); \
             print(c.execute('pragma integrity_check').fetchone()); \
             print(c.execute('select count(*) from entries').fetchone())"

# 3. Медиа и vault
docker run --rm -v lifeos-media:/dst -v /path/to/backup:/src alpine \
  sh -c 'rm -rf /dst/* && tar -xzf /src/media.tar.gz -C /dst'
docker run --rm -v lifeos-vault:/dst -v /path/to/backup:/src alpine \
  sh -c 'rm -rf /dst/* && tar -xzf /src/vault.tar.gz -C /dst'

# 4. Поднять всё
docker compose -f deploy/docker-compose.prod.yml up -d
```

После восстановления откройте дашборд и проверьте три вещи: последняя запись
журнала на месте, «Тело и Весы» показывает график, курс открывается и играет.
`pragma integrity_check` должен вернуть `ok`.

**Бэкап, который никогда не восстанавливали, бэкапом не считается.** Раз в
квартал прогоняйте §7.4 на тестовой копии.

---

## 8. Обновление и откат

### 8.1 Обновление (путь А)

```bash
cd /srv/lifeos
./ops/backup.sh                                  # 1. бэкап ДО обновления
git fetch && git log --oneline HEAD..origin/main # 2. что меняется
git pull --ff-only
docker compose -f deploy/docker-compose.prod.yml build --pull
docker compose -f deploy/docker-compose.prod.yml up -d
docker compose -f deploy/docker-compose.prod.yml ps
```

Перед `up -d` прочитайте `CHANGELOG.md`: если в релизе есть миграции БД,
откат кода без отката базы может не сработать (схема уже новая).

Если миграции нет — минимизируйте простой: `up -d --no-deps lifeos-api`
заменит только API, SPA и бот продолжат работать.

### 8.2 Откат

| Что откатываем | Как |
|---|---|
| Код и образы | `git checkout <прежний-коммит> && docker compose ... build && up -d` |
| Образы без git | `LIFEOS_TAG=2026-11-10 docker compose ... up -d` (если тег сохраняли) |
| База | §7.4 (остановить → подменить `lifeos.db` → поднять) |
| Только конфиг | вернуть `deploy/.env` из бэкапа, `up -d` |

Правило: **сначала бэкап, потом обновление**. Откат без бэкапа базы — это
только откат кода; данные, записанные новой версией, могут стать нечитаемыми.

Для пути Б: `sudo systemctl stop lifeos.service lifeos-bot.service`,
`git checkout <коммит>`, `pip install -r`, `npm run build`, `systemctl start`.
Юнит при падении перезапускается сам (`Restart=on-failure`, лимит 10 падений
за минуту — защита от бесконечного цикла).

---

## 9. Чего нет в этой среде и как поставить

Проверено 2026-11-17 на этой машине (macOS 27.2.1, arm64, 117 ГиБ свободно):
`docker` — нет, `ffmpeg` — нет, `faster-whisper` — нет, PyYAML — нет.
Homebrew **есть**: `/opt/homebrew/bin/brew` (6.0.21).

### 9.1 Docker

Ни один контейнер из `deploy/` здесь не запускался. Варианты установки:

```bash
# A. Docker Desktop (графическое приложение, самый простой путь)
brew install --cask docker
open -a Docker            # дальше «Accept», и ждать кита в строке меню

# B. Без GUI: colima + CLI (легче, меньше памяти)
brew install colima docker docker-compose
colima start --cpu 2 --memory 4 --disk 30
docker compose version

# C. Linux-сервер (путь А)
curl -fsSL https://get.docker.com | sudo sh
```

Проверка: `docker run --rm hello-world`. Если печатает «Hello from Docker!» —
можно переходить к §2.4. До этого `docker compose build` запускать бессмысленно.

### 9.2 ffmpeg

**Политика с 2026-11-18: на личном маке расшифровка выключена.** В конфиге
`transcribe.engine = "off"` (значение по умолчанию), а `ffmpeg` и
`faster-whisper` с этой машины удалены. Голосовое сообщение сохраняется как
файл, задача получает `pending_toolchain` с кодом `transcribe_disabled`, и бот
честно отвечает «расшифровка выключена на этом устройстве» — он больше не
предлагает «поставить ffmpeg».

Считать whisper на маке ресурсы не тратятся: правильный путь — отдельная
машина или сервер (путь А, `WITH_WHISPER=true` в `deploy/Dockerfile.api`).
Ниже — как поставить инструменты **осознанно**, на той машине, где расшифровка
действительно должна жить; после установки включите `transcribe.engine = "local"`.

Нужен для транскрибации (извлечение аудио из видео), нарезки и метаданных.
Без него пайплайн **не падает**: `NullTranscriber` ставит статус
`pending_toolchain`, UI честно пишет «нужен ffmpeg/whisper» (MASTER_SPEC §0.2).

```bash
brew install ffmpeg
ffmpeg -version | head -1     # проверить
```

Если brew недоступен (например, Linux без пакета или старый macOS) — статическая
сборка с <https://evermeet.cx/ffmpeg/> (macOS) или
<https://johnvansickle.com/ffmpeg/> (Linux):

```bash
# macOS: распаковать в ~/bin и добавить в PATH
mkdir -p ~/bin && cd ~/bin
curl -L -o ffmpeg.zip https://evermeet.cx/ffmpeg/getrelease/zip
unzip -o ffmpeg.zip && rm ffmpeg.zip
chmod +x ffmpeg && ./ffmpeg -version | head -1
export PATH="$HOME/bin:$PATH"     # и в ~/.zshrc, чтобы жило после перезапуска
```

**Оговорка для этого Mac:** evermeet.cx собирает под x86_64. На arm64 бинарь
запустится только через Rosetta 2 (`softwareupdate --install-rosetta`), будет
медленнее и может не найти системные кодеки. На arm64 правильный путь — `brew`.
Homebrew ещё и подтянет зависимости (`libvpx`, `x264`, `fdk-aac`) корректно
под arm64.

### 9.3 faster-whisper в отдельном venv

Только для машины, которой разрешено считать расшифровку (`transcribe.engine = "local"`).
Не ставим в системный Python: тянет `ctranslate2`, `onnxruntime`, `tokenizers`
(~1 ГБ с зависимостями) и может конфликтовать с системными пакетами.
На личном маке эти пакеты удалены — ставить обратно нужно осознанно.

```bash
cd /srv/lifeos          # или ~/lifeos
python3 -m venv .venv
.venv/bin/pip install -U pip wheel
.venv/bin/pip install faster-whisper

# проверка импорта без загрузки модели (быстро)
.venv/bin/python -c "import faster_whisper, ctranslate2; \
print('faster-whisper', faster_whisper.__version__, '| ctranslate2', ctranslate2.__version__)"
```

Дальше укажите в `deploy/.env` путь к venv-питону (для systemd-варианта
`ExecStart=/srv/lifeos/.venv/bin/python -m app.bot.main` уже так и написан),
а в Docker — соберите образ с транскрибацией:

```bash
docker compose -f deploy/docker-compose.prod.yml build --build-arg WITH_WHISPER=true lifeos-api
```

### 9.4 Модель распознавания (~1 ГБ)

Модель скачивается при первом запуске транскрибации и кэшируется:

```bash
df -h /            # на этой машине 117 ГиБ свободно — места хватает
.venv/bin/python - <<'PY'
from faster_whisper import WhisperModel
m = WhisperModel("small", device="cpu", compute_type="int8", download_root="/srv/lifeos/data/whisper")
print("модель загружена, устройство:", m.model.device)
PY
```

Размеры (сжатый CTranslate2-кэш): `tiny` ~75 МБ, `base` ~145 МБ, `small` ~490 МБ,
`medium` ~1.5 ГБ, `large-v3` ~3 ГБ. В `deploy/.env` это `WHISPER_MODEL` и
`WHISPER_MODEL_PATH` (по умолчанию `/srv/lifeos/data/whisper` — попадает в бэкап
тома `data`; при нехватке места вынесите кэш наружу).

Важно: **модель в репозиторий не кладём** — только скачивание в рантайме.

### 9.5 Чего ещё нет

- **Публичного домена и DNS** — путь А без него не заработает (ACME не выпустит
  сертификат). Путь Б обходится без домена через Tailscale.
- **Токена бота и `chat_id`** — без них бот не стартует; это блокер из
  `studio/TASK_LEDGER.md` (T8).
- **`systemd`** — юниты в `deploy/` написаны, но `systemd-analyze verify` не
  запускался: на macOS его нет.

---

## 10. Что проверено, а что нет

### Проверено (командой, воспроизводимо)

Прогнать у себя: `bash deploy/check-deploy.sh` → 119 проверок, `--live` → 126.
Результат на 2026-11-17: **126 ✔, 0 ✖, 2 !** (YAML валидирован через ruby/psych,
потому что PyYAML в среде нет). Два предупреждения — не дефекты деплоя, а
известные пробелы сдаваемого кода: нет `app/worker/`; `settings.py` не читает
`TELEGRAM_API_BASE`. Число проверок растёт по мере сдачи `app/` — ориентируйтесь
на `✖ 0` и на текст предупреждений, а не на цифру.

| Проверка | Чем | Результат |
|---|---|---|
| YAML `docker-compose.prod.yml` парсится | `ruby -ryaml -e 'YAML.load_file(...)'` (PyYAML нет) | объект, 6 сервисов, 4 тома |
| Сервисы и тома по ТЗ | `deploy/check-deploy.sh` | имена совпадают со спекой |
| Healthcheck'и из compose **исполняются** | `--live` (bot/worker — прогон на временной SQLite, api — только компиляция) + ручной прогон api против живого сервера на 8787 | api: `rc=0`, вывод `HTTP 404` при живом сервере; `rc=1` (ConnectionRefused) при убитом. bot/worker: `rc=0` на временной SQLite с таблицей `jobs`; `rc=1` без токена и без таблицы |
| Инструкции Dockerfile | `deploy/check-deploy.sh` | два этапа `FROM` во всех трёх, `USER` non-root (10001/101), cache-mount pip/npm |
| Директивы Caddyfile | `deploy/check-deploy.sh` | `encode`, HSTS/nosniff/Referrer-Policy, `request_body`, 3 upstream'а |
| Секции systemd-юнитов | `deploy/check-deploy.sh` | `[Unit]/[Service]/[Install]`, `ExecStart`, hardening |
| Переменные `.env` | `deploy/check-deploy.sh` | все `${VAR}` из compose и Caddyfile есть в `.env.example` |
| Синтаксис shell | `bash -n deploy/check-deploy.sh` | rc=0 |
| Табы в YAML (запрещены) | сканирование файла | 0 строк с табами |
| Баланс `{}` в Caddyfile и nginx | подсчёт без комментариев | 14/14 и 10/10 |
| `COPY`-источники в Dockerfile'ах | сверка с деревом | `app/server`, `app/web` есть; `app/worker`, `app/bot`, `app/web/dist` нет — это и есть ожидаемое падение сборки |
| `app/web/package.json` читается | чтение файла | скрипт `build` = `typecheck && vite build`, Vite 7, React 19 — совпадает с `npm run build` в Dockerfile.web |
| `package-lock.json` | `ls app/web` | появился в ходе работы (Фаза 5) → `npm ci` в Dockerfile.web теперь основной путь, fallback остаётся страховкой |
| Раскладка файлов | `ls deploy/` | 10 файлов + `DEPLOY_GUIDE.md` в корне |

### Не проверено (честно)

1. **Сборка и запуск контейнеров** — docker в среде нет; `docker compose build`,
   `up -d`, healthcheck'и и ACME-выпуск не выполнялись ни разу.
2. **Caddy**: `caddy validate` не запускался (нет бинаря). Синтаксис
   `request_body`, `route`, `handle_errors`, HTTP/3, zstd — по документации
   Caddy 2.8, а не по фактическому прогону.
3. **systemd-юниты**: `systemd-analyze verify` недоступен (macOS). Ключи
   `ProtectSystem`, `ReadWritePaths`, `UMask`, `StartLimit*` взяты по man-страницам.
4. **Локальный Bot API Server** (`aiogram/telegram-bot-api`): команда
   `--api-id/--api-hash/--local/--http-port/--dir` и absence-of-healthcheck
   выведены из документации/чужого опыта, не из запуска. Файлы >2 ГБ,
   миграция с облачного API и лимиты — не проверялись.
5. **Интеграция с кодом приложения — главный риск.** Найдено при сверке с
   `app/server/settings.py` (Фаза 1, читается только чтением):
   - имена переменных расходятся с ТЗ (`LIFEOS_TELEGRAM_TOKEN` vs
     `TELEGRAM_BOT_TOKEN`, `LIFEOS_ADMIN_TELEGRAM_ID` vs `ADMIN_TELEGRAM_ID`,
     `LIFEOS_VAULT` vs `LIFEOS_VAULT_DIR`) — обойдено передачей обоих наборов;
   - `telegram.api_base` в `settings.py` берётся **только из
     `config/config.json`**, переменной окружения для него нет. Значит
     `TELEGRAM_API_BASE=http://telegram-bot-api:8081` сейчас не подействует,
     и бот пойдёт в облачный `api.telegram.org` (лимит 20 МБ) — локальный Bot
     API Server будет простаивать. Закрывается либо поддержкой env в Фазе 2,
     либо монтированием `config/config.json` с `telegram.api_base`;
   - имена модулей (`app.server.main:app`, `app.bot.main`, `app.worker.main`)
     взяты из `MASTER_SPEC.md` §1 и ещё не подтверждены файлами;
   - неизвестно, пишет ли API в текущий каталог (тогда понадобится
     `read_only: true` + tmpfs, сейчас не выставлено).
6. **Работа с iPhone/PWA** — браузера в этой среде нет; манифест, service
   worker и cookie-политика будут проверяемы только на устройстве.

---

## 11. Диагностика

| Симптом | Причина | Что делать |
|---|---|---|
| Caddy падает, в логе `no such host` | DNS не указывает на сервер | `dig +short $LIFEOS_DOMAIN`, поправить A-запись, `up -d caddy` |
| `acme: challenge failed` | порт 80 занят/закрыт firewall | открыть 80/443, `ufw status`, `docker compose logs caddy` |
| `lifeos-api` вечно `starting`, unhealthy | БД не смонтирована или нет прав | `docker compose logs lifeos-api`, `docker compose exec lifeos-api ls -la /srv/lifeos/data` |
| `permission denied` на `/srv/lifeos/data` | uid тома ≠ 10001 | `docker compose exec -u 0 lifeos-api chown -R 10001:10001 /srv/lifeos` |
| Бот молчит, лог `Unauthorized` | токен или локальный Bot API | `curl "http://127.0.0.1:8081/bot<TOKEN>/getMe"`, сверить `TELEGRAM_API_BASE` |
| Бот видит только 20 МБ | `TELEGRAM_API_BASE` не задан | указать `http://telegram-bot-api:8081` и перезапустить бота |
| Воркер не берёт задачи | таблица `jobs` пустая или статусы другие | `sqlite3 data/lifeos.db 'select status,count(*) from jobs group by 1'` |
| iPhone показывает старую версию | кэш service worker | закрыть приложение, переустановить иконку; проверить `Cache-Control` на `sw.js` |
| `413` при загрузке видео | лимит Caddy | увеличить `MEDIA_MAX_UPLOAD` (и перезапустить `caddy`) |
| Всё разом 502 | упал `lifeos-web` или `lifeos-api` | `docker compose ps`, затем `logs --tail=100` |

Полезные команды:

```bash
docker compose -f deploy/docker-compose.prod.yml ps
docker compose -f deploy/docker-compose.prod.yml logs --tail=200 -f lifeos-api
docker compose -f deploy/docker-compose.prod.yml exec lifeos-api env | grep LIFEOS_
docker compose -f deploy/docker-compose.prod.yml exec caddy caddy validate --config /etc/caddy/Caddyfile
docker stats --no-stream
```

---

## 12. Файлы деплоя

| Файл | Назначение |
|---|---|
| `deploy/docker-compose.prod.yml` | 6 сервисов, 4 тома, healthcheck, `.env` без секретов |
| `deploy/Dockerfile.api` | python:3.13-slim, два этапа (venv → runtime), non-root 10001, кэш pip, опция `WITH_WHISPER` |
| `deploy/Dockerfile.bot` | aiogram 3.31, два этапа, non-root 10001, кэш pip |
| `deploy/Dockerfile.web` | node:24-alpine → nginx-unprivileged (multi-stage, кэш npm) |
| `deploy/nginx-web.conf` | статика SPA, SPA-fallback, кэш ассетов, `no-store` для `index.html`/`sw.js` |
| `deploy/Caddyfile` | 443 + auto-HTTPS, gzip/zstd, HSTS/nosniff/Referrer-Policy, лимит тела под видео |
| `deploy/lifeos.service` | API под systemd: 127.0.0.1:8787, hardening, `Restart=on-failure` |
| `deploy/lifeos-bot.service` | бот под systemd: `Restart=always`, тот же `lifeos.env` |
| `deploy/.env.example` | все переменные с комментариями и плейсхолдерами |
| `deploy/check-deploy.sh` | статическая проверка без docker; `--live` дополнительно исполняет healthcheck-команды из compose |
| `.dockerignore` | секреты/база/`.git` не попадают в build-context |
