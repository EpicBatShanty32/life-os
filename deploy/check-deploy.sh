#!/usr/bin/env bash
# ============================================================================
# Life OS v3 — статическая проверка артефактов deploy/.
#
# Что делает: проверяет то, что можно проверить БЕЗ docker, без сети и без
# запуска чего-либо: разбор YAML, набор сервисов и томов, инструкции
# Dockerfile, директивы Caddyfile, секции systemd-юнитов, покрытие
# переменных .env.example, отсутствие секретов в файлах.
#
# Что НЕ делает: не собирает образы, не валидирует Caddyfile движком Caddy,
# не проверяет юниты через systemd-analyze. Это записано в DEPLOY_GUIDE §10
# как «не проверено».
#
# Использование:  bash deploy/check-deploy.sh
# Код возврата:   0 — все проверки прошли, 1 — есть провалы.
# Совместим с bash 3.2 (macOS): без ассоциативных массивов и mapfile.
# ============================================================================
set -uo pipefail

# Флаги: --live дополнительно ИСПОЛНЯЕТ healthcheck-команды из compose
# (компиляция python-проб + прогон bot/worker на временной SQLite).
LIVE=0
for _arg in "$@"; do
  case "$_arg" in
    --live) LIVE=1 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Неизвестный аргумент: %s (есть --live, --help)\n' "$_arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
cd "$ROOT" || exit 1

PASS=0
FAIL=0
WARN=0

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'
  C_B=$'\033[1m'; C_R=$'\033[0m'
else
  C_OK=""; C_BAD=""; C_WARN=""; C_B=""; C_R=""
fi

ok()      { printf '  %s✔%s %s\n' "$C_OK" "$C_R" "$*"; PASS=$((PASS + 1)); }
bad()     { printf '  %s✖%s %s\n' "$C_BAD" "$C_R" "$*"; FAIL=$((FAIL + 1)); }
warn()    { printf '  %s!%s %s\n' "$C_WARN" "$C_R" "$*"; WARN=$((WARN + 1)); }
section() { printf '\n%s▸ %s%s\n' "$C_B" "$*" "$C_R"; }

COMPOSE="deploy/docker-compose.prod.yml"
ENV_EXAMPLE="deploy/.env.example"
CADDY="deploy/Caddyfile"

# ---------------------------------------------------------------------------
section "1. Файлы на месте"
# ---------------------------------------------------------------------------
for f in "$COMPOSE" "$CADDY" deploy/Dockerfile.api deploy/Dockerfile.bot \
         deploy/Dockerfile.web deploy/nginx-web.conf deploy/lifeos.service \
         deploy/lifeos-bot.service "$ENV_EXAMPLE" deploy/check-deploy.sh \
         .dockerignore DEPLOY_GUIDE.md; do
  if [ -s "$f" ]; then
    ok "$f ($(wc -l < "$f" | tr -d ' ') строк)"
  else
    bad "$f — нет или пустой"
  fi
done

# ---------------------------------------------------------------------------
section "2. YAML разбирается (docker-compose.prod.yml)"
# ---------------------------------------------------------------------------
PARSER="none"
if python3 -c 'import yaml' >/dev/null 2>&1; then
  PARSER="python"
elif command -v ruby >/dev/null 2>&1; then
  PARSER="ruby"
fi

parse_compose_services() {
  case "$PARSER" in
    python)
      python3 - "$COMPOSE" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted((data.get("services") or {}).keys())))
PY
      ;;
    ruby)
      ruby -ryaml -e 'd = YAML.load_file(ARGV[0]); puts((d["services"] || {}).keys.sort.join(" "))' "$COMPOSE"
      ;;
    *)
      return 1
      ;;
  esac
}

parse_compose_volumes() {
  case "$PARSER" in
    python)
      python3 - "$COMPOSE" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(sorted((data.get("volumes") or {}).keys())))
PY
      ;;
    ruby)
      ruby -ryaml -e 'd = YAML.load_file(ARGV[0]); puts((d["volumes"] || {}).keys.sort.join(" "))' "$COMPOSE"
      ;;
    *)
      return 1
      ;;
  esac
}

if [ "$PARSER" = "none" ]; then
  warn "ни PyYAML, ни ruby — YAML не распарсен (только грепы ниже)"
else
  SERVICES="$(parse_compose_services 2>&1)"
  VOLUMES="$(parse_compose_volumes 2>&1)"
  if [ -n "$SERVICES" ]; then
    ok "парсер: $PARSER; сервисы: $SERVICES"
  else
    bad "YAML не разобрался ($PARSER): $SERVICES"
    SERVICES=""
  fi
  if [ -n "$VOLUMES" ]; then
    ok "тома: $VOLUMES"
  fi

  # --- сервисы ровно по спеке ---------------------------------------------
  for want in lifeos-web lifeos-api lifeos-bot lifeos-worker telegram-bot-api caddy; do
    case " $SERVICES " in
      *" $want "*) ok "сервис на месте: $want" ;;
      *) bad "нет сервиса: $want" ;;
    esac
  done
  COUNT="$(printf '%s\n' $SERVICES | wc -l | tr -d ' ')"
  if [ "$COUNT" = "6" ]; then
    ok "сервисов ровно 6"
  else
    bad "сервисов $COUNT, а по ТЗ должно быть 6"
  fi

  # --- тома: ровно data, media, vault, caddy_data -------------------------
  for want in data media vault caddy_data; do
    case " $VOLUMES " in
      *" $want "*) ok "том на месте: $want" ;;
      *) bad "нет тома: $want" ;;
    esac
  done
  VCOUNT="$(printf '%s\n' $VOLUMES | wc -l | tr -d ' ')"
  if [ "$VCOUNT" = "4" ]; then
    ok "томов ровно 4"
  else
    warn "томов $VCOUNT (в ТЗ 4: data, media, vault, caddy_data; лишние объясните в отчёте)"
  fi
fi

# ---------------------------------------------------------------------------
section "3. Compose: эксплуатационные требования"
# ---------------------------------------------------------------------------
n_restart="$(grep -c 'restart: unless-stopped' "$COMPOSE" || true)"
if [ "$n_restart" -ge 6 ]; then
  ok "restart: unless-stopped — $n_restart (все сервисы)"
else
  bad "restart: unless-stopped только у $n_restart сервисов"
fi

n_health="$(grep -c 'healthcheck:' "$COMPOSE" || true)"
if [ "$n_health" -ge 5 ]; then
  ok "healthcheck — $n_health сервисов (у telegram-bot-api намеренно нет)"
else
  warn "healthcheck только у $n_health сервисов (см. комментарий в файле)"
fi

n_dep="$(grep -c 'depends_on:' "$COMPOSE" || true)"
if [ "$n_dep" -ge 4 ]; then
  ok "depends_on — $n_dep сервисов"
else
  bad "depends_on только у $n_dep сервисов"
fi

if grep -q 'condition: service_healthy' "$COMPOSE"; then
  ok "есть ожидание service_healthy (а не только «запущен»)"
else
  warn "нигде не используется service_healthy"
fi

if grep -q 'no-new-privileges:true' "$COMPOSE"; then
  ok "security_opt no-new-privileges"
else
  warn "нет no-new-privileges"
fi

if grep -q 'cap_drop:' "$COMPOSE" && grep -q '\- ALL' "$COMPOSE"; then
  ok "cap_drop: ALL у прикладных сервисов"
else
  warn "нет cap_drop: ALL"
fi

# --- секретов в файле быть не должно ---------------------------------------
if grep -Eq '[0-9]{8,10}:[A-Za-z0-9_-]{30,}' "$COMPOSE"; then
  bad "в compose похоже на настоящий токен бота — вынести в .env"
else
  ok "в compose нет токенов вида 123456789:AA..."
fi
if grep -Eq 'LIFEOS_TOKEN:\s*["'"'"']?[A-Za-z0-9]{24,}' "$COMPOSE"; then
  bad "в compose захардкожен LIFEOS_TOKEN"
else
  ok "LIFEOS_TOKEN берётся только из .env (\${LIFEOS_TOKEN:?...})"
fi

# ---------------------------------------------------------------------------
section "4. Dockerfile'ы"
# ---------------------------------------------------------------------------
check_dockerfile() {
  local f="$1" want_user="$2"
  if grep -Eq '^FROM ' "$f"; then
    ok "$f: есть FROM ($(grep -E '^FROM ' "$f" | head -1 | tr -s ' '))"
  else
    bad "$f: нет FROM"
  fi
  if grep -Eq '^USER ' "$f"; then
    ok "$f: USER $(grep -E '^USER ' "$f" | head -1 | awk '{print $2}') (не работает от root)"
  else
    bad "$f: нет USER — образ запустится от root"
  fi
  if grep -q 'type=cache' "$f"; then
    ok "$f: кэш зависимостей (--mount=type=cache)"
  else
    warn "$f: нет cache-mount"
  fi
  if grep -q 'no-new-privileges\|LIFEOS_UID' "$f"; then
    ok "$f: фиксированный непривилегированный uid"
  fi
}

check_dockerfile deploy/Dockerfile.api 10001
check_dockerfile deploy/Dockerfile.bot 10001
check_dockerfile deploy/Dockerfile.web 101

if grep -q 'FROM python:3.13-slim' deploy/Dockerfile.api; then
  ok "Dockerfile.api: python:3.13-slim (как в ТЗ)"
else
  warn "Dockerfile.api: базовый образ отличается от python:3.13-slim"
fi
if grep -q 'FROM node:24-alpine' deploy/Dockerfile.web; then
  ok "Dockerfile.web: node:24-alpine (multi-stage)"
else
  warn "Dockerfile.web: базовый образ отличается от node:24-alpine"
fi
for df in deploy/Dockerfile.api deploy/Dockerfile.bot deploy/Dockerfile.web; do
  if [ "$(grep -c '^FROM ' "$df")" -ge 2 ]; then
    ok "$df: multi-stage ($(grep -c '^FROM ' "$df") этапа)"
  else
    bad "$df: не multi-stage (нужно 2 этапа: сборка + runtime)"
  fi
done
if grep -q 'nginxinc/nginx-unprivileged' deploy/Dockerfile.web; then
  ok "Dockerfile.web: nginx-unprivileged (non-root, :8080)"
else
  warn "Dockerfile.web: nginx не unprivileged"
fi

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
section "4b. Интеграция с кодом app/ (сдача идёт параллельно)"
# ---------------------------------------------------------------------------
if [ -f app/web/package.json ]; then
  if grep -q '"build"' app/web/package.json; then
    ok "app/web/package.json: скрипт build есть"
  else
    bad "app/web/package.json: нет скрипта build — Dockerfile.web не соберётся"
  fi
  if grep -q 'npm run build' deploy/Dockerfile.web; then
    ok "Dockerfile.web вызывает npm run build (совпадает с package.json)"
  else
    bad "Dockerfile.web и package.json расходятся в команде сборки"
  fi
  if [ -f app/web/package-lock.json ]; then
    ok "app/web/package-lock.json есть — сборка воспроизводима (npm ci)"
  else
    warn "app/web/package-lock.json нет — Dockerfile.web уйдёт в npm install"
  fi
else
  warn "app/web/package.json ещё нет (Фаза 5) — сверка сборки невозможна"
fi

for src in app/server app/worker app/bot app/web; do
  if [ -d "$src" ]; then
    ok "есть $src"
  else
    warn "нет $src — соответствующая Фаза ещё не сдана (ожидаемо до сборки)"
  fi
done

if [ -f app/server/settings.py ]; then
  if grep -q 'LIFEOS_TELEGRAM_TOKEN' app/server/settings.py && \
     grep -q 'LIFEOS_VAULT' app/server/settings.py; then
    ok "settings.py читает LIFEOS_TELEGRAM_TOKEN / LIFEOS_VAULT — compose передаёт оба имени"
  else
    warn "имена переменных в settings.py изменились — сверьте x-lifeos-common-env"
  fi
  if grep -q 'LIFEOS_TELEGRAM_API_BASE\|LIFEOS_API_BASE' app/server/settings.py; then
    ok "settings.py умеет TELEGRAM_API_BASE — локальный Bot API заработает"
  else
    warn "settings.py не читает TELEGRAM_API_BASE: бот пойдёт в api.telegram.org (лимит 20 МБ), см. DEPLOY_GUIDE §10"
  fi
fi

section "5. Caddyfile"
# ---------------------------------------------------------------------------
check_caddy() {
  local pattern="$1" msg="$2"
  if grep -Eq -- "$pattern" "$CADDY"; then
    ok "$msg"
  else
    bad "$msg — не найдено в Caddyfile"
  fi
}
check_caddy 'encode zstd gzip' 'сжатие zstd + gzip'
check_caddy 'Strict-Transport-Security' 'HSTS'
check_caddy 'X-Content-Type-Options' 'X-Content-Type-Options: nosniff'
check_caddy 'Referrer-Policy' 'Referrer-Policy'
check_caddy 'request_body' 'ограничение тела запроса (request_body)'
check_caddy 'max_size \{\$MEDIA_MAX_UPLOAD' 'лимит под видео из .env (MEDIA_MAX_UPLOAD)'
check_caddy 'reverse_proxy (@[a-z]+ )?lifeos-api:8787' 'проксирование на lifeos-api:8787'
check_caddy 'reverse_proxy lifeos-web:8080' 'проксирование статики на lifeos-web:8080'
check_caddy 'path /api/\*' 'маршрут /api/*'
check_caddy '@ws path /ws' 'маршрут WebSocket /ws'
check_caddy '\{\$LIFEOS_DOMAIN\}' 'домен из переменной LIFEOS_DOMAIN'
check_caddy 'handle_errors' 'свой ответ на 5xx'
if grep -q 'handle_path /api' "$CADDY"; then
  bad "handle_path /api обрезал бы префикс — API получил бы /overview вместо /api/overview"
else
  ok "префикс /api не обрезается"
fi

# ---------------------------------------------------------------------------
section "6. systemd-юниты"
# ---------------------------------------------------------------------------
check_unit() {
  local f="$1"
  for sec in '[Unit]' '[Service]' '[Install]'; do
    if grep -qF "$sec" "$f"; then
      ok "$f: секция $sec"
    else
      bad "$f: нет секции $sec"
    fi
  done
  if grep -q '^ExecStart=' "$f"; then
    ok "$f: ExecStart есть"
  else
    bad "$f: нет ExecStart"
  fi
  if grep -q '^EnvironmentFile=' "$f"; then
    ok "$f: переменные из EnvironmentFile (без секретов в юните)"
  else
    bad "$f: нет EnvironmentFile"
  fi
  if grep -q '^Restart=' "$f"; then
    ok "$f: $(grep -E '^Restart=' "$f")"
  else
    bad "$f: нет Restart"
  fi
  if grep -q 'NoNewPrivileges=yes' "$f" && grep -q 'ProtectSystem=strict' "$f"; then
    ok "$f: hardening (NoNewPrivileges, ProtectSystem=strict)"
  else
    warn "$f: неполный hardening"
  fi
  if grep -q 'ReadWritePaths=' "$f"; then
    ok "$f: ReadWritePaths ограничивает запись"
  else
    warn "$f: нет ReadWritePaths при ProtectSystem=strict"
  fi
  if grep -Eq 'StartLimit' "$f"; then
    if awk '/^\[Unit\]/{u=1} /^\[Service\]/{u=0} u && /^StartLimit/' "$f" | grep -q .; then
      ok "$f: StartLimit* в [Unit] (с systemd 230+ только там)"
    else
      bad "$f: StartLimit* вне [Unit] — systemd проигнорирует"
    fi
  fi
}
check_unit deploy/lifeos.service
check_unit deploy/lifeos-bot.service

# ---------------------------------------------------------------------------
section "7. Переменные: compose/Caddy требуют — .env.example знает"
# ---------------------------------------------------------------------------
# compose: ${VAR} и ${VAR:-default}; комментарии не считаем
collect_vars() {
  grep -v '^[[:space:]]*#' "$1" 2>/dev/null \
    | grep -oE '\$\{[A-Z][A-Z0-9_]*' \
    | sed 's/^\${//' | sort -u
}

# Caddyfile: {$VAR} и {$VAR:default}
collect_caddy_vars() {
  grep -v '^[[:space:]]*#' "$1" 2>/dev/null \
    | grep -oE '\{\$[A-Z][A-Z0-9_]*' \
    | sed 's/^{\$//' | sort -u
}

MISSING=0
for v in $(collect_vars "$COMPOSE") $(collect_caddy_vars "$CADDY"); do
  case "$v" in
    MEDIA_MAX_UPLOAD|LIFEOS_DOMAIN|ACME_EMAIL|LIFEOS_TAG|LIFEOS_TZ|\
    LIFEOS_TOKEN|ADMIN_TELEGRAM_ID|TELEGRAM_BOT_TOKEN|TELEGRAM_API_ID|\
    TELEGRAM_API_HASH|TELEGRAM_API_BASE|LIFEOS_LLM_BASE_URL|\
    LIFEOS_LLM_MODEL|LIFEOS_LLM_API_KEY|LIFEOS_LLM_ENABLED|WHISPER_MODEL|\
    FFMPEG_BIN|WITH_WHISPER|LIFEOS_VAULT_WRITE|OBSIDIAN_VAULT_PATH|VAR)
      if grep -Eq "^${v}=" "$ENV_EXAMPLE"; then
        ok "$v — есть в .env.example"
      else
        bad "$v — используется, но не описан в .env.example"
        MISSING=$((MISSING + 1))
      fi
      ;;
    *)
      warn "$v — переменная не из известного набора, проверьте .env.example вручную"
      ;;
  esac
done

# Все переменные .env.example должны быть либо описаны в compose/Caddy,
# либо явно помечены как «для systemd/бэкапов» (иначе это мёртвый груз).
for v in $(grep -oE '^[A-Z][A-Z0-9_]*=' "$ENV_EXAMPLE" | sed 's/=$//'); do
  if grep -q "${v}" "$COMPOSE" || grep -q "${v}" "$CADDY"; then
    :
  else
    case "$v" in
      LIFEOS_DATA_DIR|LIFEOS_MEDIA_DIR|LIFEOS_VAULT_DIR|OBSIDIAN_VAULT_PATH|\
TELEGRAM_API_BASE|BACKUP_DIR|BACKUP_KEEP|WHISPER_MODEL)
        : ;; # нужны systemd-варианту и скрипту бэкапа — это нормально
      *)
        warn "$v описан в .env.example, но нигде не используется"
        ;;
    esac
  fi
done
[ "$MISSING" -eq 0 ] && ok "все переменные из compose/Caddy описаны в .env.example"

if grep -Eq 'CHANGE_ME' "$ENV_EXAMPLE"; then
  ok ".env.example содержит плейсхолдеры CHANGE_ME (секретов нет)"
else
  warn "в .env.example нет CHANGE_ME — проверьте, что там нет настоящих секретов"
fi

# ---------------------------------------------------------------------------
section "8. Гигиена секретов и статики"
# ---------------------------------------------------------------------------
if grep -Eq '^deploy/\.env$' .gitignore; then
  ok ".gitignore игнорирует deploy/.env"
else
  bad ".gitignore не игнорирует deploy/.env — секреты уедут в git"
fi
if [ -e deploy/.env ]; then
  PERM="$(ls -l deploy/.env | awk '{print $1}')"
  if [ "$PERM" = "-rw-------" ]; then
    ok "deploy/.env существует и закрыт (600)"
  else
    warn "deploy/.env существует с правами $PERM — нужно chmod 600"
  fi
else
  ok "deploy/.env отсутствует (в репозитории только шаблон)"
fi

if grep -Eq '^(\.git|data/|deploy/\.env)$' .dockerignore; then
  ok ".dockerignore исключает .git, data/ и deploy/.env из контекста сборки"
else
  warn ".dockerignore может пропустить секреты в build-context"
fi

if grep -q 'try_files $uri $uri/ /index.html' deploy/nginx-web.conf; then
  ok "nginx: SPA-fallback (прямые ссылки и «на экран Домой» не дают 404)"
else
  bad "nginx: нет SPA-fallback"
fi
if grep -q 'listen      8080' deploy/nginx-web.conf; then
  ok "nginx: слушает 8080 (unprivileged-образ)"
else
  warn "nginx: порт не 8080"
fi
if grep -q 'location = /sw.js' deploy/nginx-web.conf && \
   grep -q 'no-store' deploy/nginx-web.conf; then
  ok "nginx: sw.js и index.html без кэша (PWA обновляется)"
else
  warn "nginx: кэш service worker не отключён"
fi

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
if [ "$LIVE" = "1" ]; then
  section "9. --live: healthcheck-команды из compose исполняются"
  TMPHC="$(mktemp -d "${TMPDIR:-/tmp}/lifeos-hc.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$TMPHC'" EXIT

  dumped=0
  case "$PARSER" in
    ruby)
      ruby -ryaml -rjson -e '
        d = YAML.load_file(ARGV[0]); o = {}
        d["services"].each { |n, s| t = s.dig("healthcheck", "test"); o[n] = t[1] if t }
        File.write(ARGV[1], JSON.generate(o))
      ' "$COMPOSE" "$TMPHC/hc.json" && dumped=1
      ;;
    python)
      python3 - "$COMPOSE" "$TMPHC/hc.json" <<'PYEXTRACT' && dumped=1
import json, sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
o = {k: v["healthcheck"]["test"][1]
     for k, v in d["services"].items()
     if (v.get("healthcheck") or {}).get("test")}
json.dump(o, open(sys.argv[2], "w", encoding="utf-8"))
PYEXTRACT
      ;;
  esac

  if [ "$dumped" = "1" ]; then
    # Проверяем ровно те строки, что лежат в compose. python -> python3:
    # в контейнере python:3.13-slim команда называется python, на macOS — python3.
    LIVE_OUT="$(python3 - "$TMPHC/hc.json" "$TMPHC" <<'PYRUN' 2>&1
import json, os, sqlite3, subprocess, sys

tests = json.load(open(sys.argv[1], encoding="utf-8"))
tmp = sys.argv[2]
PY_BIN = "python3"          # локальное имя: в образе это python


def code_of(cmd):
    marker = "python -c "
    if not cmd.startswith(marker):
        return None
    body = cmd[len(marker):].strip()
    if body.startswith('"') and body.endswith('"'):
        body = body[1:-1]
    return body


def make_db(path, with_jobs=True):
    c = sqlite3.connect(path)
    if with_jobs:
        c.execute("create table jobs (id integer primary key, status text default 'queued')")
        c.execute("insert into jobs default values")
    c.commit()
    c.close()


def run(code, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.run([PY_BIN, "-c", code], capture_output=True, text=True,
                       env=env, timeout=30)
    tail = (p.stdout + p.stderr).strip()
    return p.returncode, (tail.splitlines()[-1] if tail else "")


for name in ("lifeos-api", "lifeos-bot", "lifeos-worker"):
    cmd = tests.get(name)
    if not cmd:
        print("FAIL %s: healthcheck не найден в compose" % name)
        continue
    code = code_of(cmd)
    if code is None:
        print("WARN %s: не python-проба (wget?) — локально не исполняем" % name)
        continue
    try:
        compile(code, "<%s>" % name, "exec")
    except SyntaxError as exc:
        print("FAIL %s: python-код пробы не компилируется: %s" % (name, exc))
        continue
    print("OK %s: код пробы компилируется" % name)

    if name == "lifeos-api":
        # Позитивный прогон требует живого сервера на 8787 (DEPLOY_GUIDE §2.5).
        continue

    db = os.path.join(tmp, "%s.db" % name)
    make_db(db)
    rc, out = run(code, {"LIFEOS_DB_PATH": db, "TELEGRAM_BOT_TOKEN": "42:test",
                         "LIFEOS_DATA_DIR": tmp})
    if rc == 0:
        print("OK %s: rc=0 на живой SQLite (%s)" % (name, out[:40]))
    else:
        print("FAIL %s: rc=%s на живой SQLite: %s" % (name, rc, out[:60]))

# негативные прогоны: проба обязана падать, а не молча зеленеть
bot = code_of(tests.get("lifeos-bot", ""))
if bot:
    rc, _ = run(bot, {"LIFEOS_DB_PATH": os.path.join(tmp, "bot.db"),
                      "TELEGRAM_BOT_TOKEN": "", "LIFEOS_DATA_DIR": tmp})
    print(("FAIL lifeos-bot: без токена проба НЕ упала (ложное «healthy»)"
           if rc == 0 else "OK lifeos-bot: без токена rc!=0 (ловит пустой .env)"))

worker = code_of(tests.get("lifeos-worker", ""))
if worker:
    empty = os.path.join(tmp, "empty.db")
    make_db(empty, with_jobs=False)
    rc, _ = run(worker, {"LIFEOS_DB_PATH": empty})
    print(("FAIL lifeos-worker: на БД без таблицы jobs проба НЕ упала"
           if rc == 0 else "OK lifeos-worker: БД без jobs даёт rc!=0"))
PYRUN
)"

    while IFS= read -r line; do
      case "$line" in
        OK\ *)   ok "${line#OK }" ;;
        WARN\ *) warn "${line#WARN }" ;;
        FAIL\ *) bad "${line#FAIL }" ;;
        "")      ;;
        *)       printf '    %s\n' "$line" ;;
      esac
    done <<< "$LIVE_OUT"
  else
    warn "--live: не удалось извлечь healthcheck'и (нужен ruby или PyYAML)"
  fi
fi

printf '\n%sИтог:%s %s✔%s %s  %s✖%s %s  %s!%s %s\n' \
  "$C_B" "$C_R" "$C_OK" "$C_R" "$PASS" "$C_BAD" "$C_R" "$FAIL" "$C_WARN" "$C_R" "$WARN"

if [ "$FAIL" -eq 0 ]; then
  printf '%sСтатическая проверка пройдена.%s Сборка и запуск НЕ проверялись: docker в среде нет.\n' \
    "$C_OK" "$C_R"
  exit 0
else
  printf '%sЕсть провалы — см. ✖ выше.%s\n' "$C_BAD" "$C_R"
  exit 1
fi
