#!/usr/bin/env bash
# ============================================================================
# Life OS — установка агента на macOS
#
# Что делает install.sh:
#   1) копирует программу (src/, dashboard/, config/, README) в
#      ~/Library/Application Support/LifeOS;
#   2) создаёт config.json из config.example.json, если конфига ещё нет
#      (существующий конфиг НЕ перезаписывается), ставит права 600;
#   3) если в конфиге пуст llm.api_key — подхватывает DDD_API_KEY из
#      ~/.dsh/.credentials.yaml (ключ в вывод не печатается);
#   4) ставит LaunchAgent ~/Library/LaunchAgents/com.lifeos.agent.plist
#      (запуск при входе + перезапуск при падении);
#   5) запускает агента и печатает статус.
#
# Использование:
#   bash install.sh              установить/обновить и запустить
#   bash install.sh --reload     только перезапустить (после правки config.json)
#   bash install.sh --dry-run    показать, что будет сделано, ничего не меняя
#   bash install.sh --help       справка
#
# Служебные переменные окружения (нужны только для отладки/тестов):
#   LIFEOS_APP_DIR, LIFEOS_LOG_DIR, LIFEOS_PLIST_DIR, LIFEOS_CREDENTIALS,
#   LIFEOS_PYTHON
#
# Скрипт совместим с /bin/bash 3.2 (macOS): без ассоциативных массивов,
# без mapfile, без ${var,,}.
# ============================================================================
set -euo pipefail

: "${HOME:?HOME не задан — запусти скрипт из обычного терминала}"

LABEL="com.lifeos.agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

APP_DIR="${LIFEOS_APP_DIR:-$HOME/Library/Application Support/LifeOS}"
LOG_DIR="${LIFEOS_LOG_DIR:-$HOME/Library/Logs/LifeOS}"
PLIST_DIR="${LIFEOS_PLIST_DIR:-$HOME/Library/LaunchAgents}"
CREDENTIALS="${LIFEOS_CREDENTIALS:-$HOME/.dsh/.credentials.yaml}"

PLIST_TEMPLATE="$SCRIPT_DIR/ops/$LABEL.plist"
PLIST_DST="$PLIST_DIR/$LABEL.plist"
CONFIG_DIR="$APP_DIR/config"
CONFIG_JSON="$CONFIG_DIR/config.json"
CONFIG_EXAMPLE="$CONFIG_DIR/config.example.json"
LOG_FILE="$LOG_DIR/agent.log"

DRY_RUN=0
RELOAD_ONLY=0
STARTED=0
PYTHON=""
PYTHON_VERSION=""

# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_RED=$'\033[31m'
  C_YELLOW=$'\033[33m'
  C_RESET=$'\033[0m'
else
  C_BOLD=""
  C_DIM=""
  C_RED=""
  C_YELLOW=""
  C_RESET=""
fi

say() { printf '  %s\n' "$*"; }
step() { printf '\n%s▸ %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }
warn() { printf '%s  ! %s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2; }
die() {
  printf '\n%s  ✖ %s%s\n\n' "$C_RED" "$*" "$C_RESET" >&2
  exit 1
}

show_cmd() {
  local arg out=""
  for arg in "$@"; do
    out="$out$(printf '%q' "$arg") "
  done
  printf '    [dry-run] %s\n' "${out% }"
}

# run выполняет команду, а в режиме --dry-run только печатает её.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    show_cmd "$@"
  else
    "$@"
  fi
}

on_error() {
  local code=$?
  printf '\n%s  ✖ Шаг не выполнен (код %s). Установка прервана.%s\n\n' \
    "$C_RED" "$code" "$C_RESET" >&2
}
trap on_error ERR

usage() {
  cat <<'EOF'
Life OS — установка Telegram-агента (macOS).

Запуск:
  bash install.sh            установить (или обновить) и запустить агента
  bash install.sh --reload   только перезапустить (после правки config.json)
  bash install.sh --dry-run  показать план действий, ничего не меняя
  bash install.sh --help     эта справка

Что делает установка:
  1) копирует src/, dashboard/, config/, README в
     ~/Library/Application Support/LifeOS;
  2) создаёт config.json из config.example.json (существующий не трогает),
     ставит права 600; подхватывает llm.api_key из ~/.dsh/.credentials.yaml;
  3) ставит LaunchAgent ~/Library/LaunchAgents/com.lifeos.agent.plist
     (запуск при входе, перезапуск при падении);
  4) запускает агента и печатает статус.

После установки:
  * впиши telegram.token и telegram.allowed_chat_id в
    ~/Library/Application Support/LifeOS/config.json;
  * примени изменения: bash install.sh --reload;
  * лог:     ~/Library/Logs/LifeOS/agent.log
  * дашборд: open "$HOME/Library/Application Support/LifeOS/dashboard/index.html"

Снять с автозапуска: bash uninstall.sh          (данные сохраняются)
Удалить всё:         bash uninstall.sh --purge  (удалит конфиг, базу, логи)

Параметры для отладки (переменные окружения): LIFEOS_APP_DIR, LIFEOS_LOG_DIR,
LIFEOS_PLIST_DIR, LIFEOS_CREDENTIALS, LIFEOS_PYTHON.
EOF
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --reload) RELOAD_ONLY=1 ;;
      --help | -h)
        usage
        exit 0
        ;;
      *)
        usage >&2
        die "Неизвестный параметр: $1"
        ;;
    esac
    shift
  done
}

# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------
python_ok() {
  local candidate="$1"
  [ -n "$candidate" ] && [ -x "$candidate" ] || return 1
  "$candidate" -c 'import sys, json, sqlite3, urllib.request, zoneinfo, plistlib
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

# Выбирает рабочий python3. Помимо /usr/bin/python3 смотрим более новые
# интерпретаторы: код проекта разрабатывается и тестируется на "python3" из
# PATH (на этой машине /usr/local/bin/python3 = 3.13, а /usr/bin/python3 = 3.9).
resolve_python() {
  local candidate
  if [ -n "${LIFEOS_PYTHON:-}" ]; then
    python_ok "$LIFEOS_PYTHON" ||
      die "LIFEOS_PYTHON=$LIFEOS_PYTHON не работает или это Python < 3.9."
    PYTHON="$LIFEOS_PYTHON"
  else
    for candidate in \
      /usr/local/bin/python3 \
      /opt/homebrew/bin/python3 \
      /usr/bin/python3 \
      "$(command -v python3 2>/dev/null || true)"; do
      if python_ok "$candidate"; then
        PYTHON="$candidate"
        break
      fi
    done
    [ -n "$PYTHON" ] || die "Не найден рабочий python3 (нужен Python 3.9+).
  Установи Xcode Command Line Tools: xcode-select --install"
  fi
  PYTHON_VERSION="$("$PYTHON" -c 'import sys; print(".".join(str(x) for x in sys.version_info[:3]))')"
}

preflight() {
  step "Проверки"

  if [ "$(uname -s 2>/dev/null || true)" != "Darwin" ]; then
    warn "Это не macOS: launchd здесь не работает (dry-run допустим, установка — нет)."
  fi

  resolve_python
  say "Python: $PYTHON ($PYTHON_VERSION)"

  [ -f "$PLIST_TEMPLATE" ] ||
    die "Нет шаблона LaunchAgent: $PLIST_TEMPLATE"
  [ -f "$SCRIPT_DIR/config/config.example.json" ] ||
    die "Нет файла конфига: $SCRIPT_DIR/config/config.example.json"

  if [ ! -f "$SCRIPT_DIR/src/main.py" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "src/main.py ещё нет — проверка неполная (dry-run)."
    else
      die "Нет src/main.py — устанавливать нечего."
    fi
  fi
  if [ ! -d "$SCRIPT_DIR/dashboard" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "dashboard/ ещё нет — проверка неполная (dry-run)."
    else
      warn "Нет папки dashboard/ — дашборд не установится."
    fi
  fi

  "$PYTHON" -m json.tool "$SCRIPT_DIR/config/config.example.json" >/dev/null 2>&1 ||
    die "config/config.example.json — невалидный JSON."

  "$PYTHON" - "$PLIST_TEMPLATE" <<'PY' || die "ops/$LABEL.plist — невалидный plist-шаблон."
import plistlib
import sys

with open(sys.argv[1], "rb") as fh:
    data = plistlib.load(fh)

if data.get("Label") != "com.lifeos.agent":
    raise SystemExit("в plist неверный Label")
args = data.get("ProgramArguments") or []
if len(args) < 2 or args[-1] != "src/main.py":
    raise SystemExit("в plist ProgramArguments должен заканчиваться на src/main.py")
PY

  say "Проверки пройдены."
}

# ---------------------------------------------------------------------------
# Чтение конфига
# ---------------------------------------------------------------------------
# cfg_get "telegram.token" -> значение из config.json (строка) или "".
cfg_get() {
  "$PYTHON" -c '
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
except Exception:
    print("")
    raise SystemExit(0)

cur = cfg
for part in sys.argv[2].split("."):
    if not isinstance(cur, dict):
        cur = None
        break
    cur = cur.get(part)
print("" if cur is None else cur)
' "$CONFIG_JSON" "${1:-}"
}

# Что не заполнено: "token", "chat", "both" или пусто (всё готово).
config_problem() {
  local token chat
  token="$(cfg_get telegram.token || true)"
  chat="$(cfg_get telegram.allowed_chat_id || true)"
  case "$chat" in
    '' | *[!0-9]*) chat=0 ;;
  esac
  if [ -z "$token" ] && [ "$chat" -eq 0 ]; then
    printf 'both'
  elif [ -z "$token" ]; then
    printf 'token'
  elif [ "$chat" -eq 0 ]; then
    printf 'chat'
  fi
}

schedule_line() {
  "$PYTHON" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = json.load(fh)
sch = cfg.get("schedule") or {}
parts = []
for key, label in (("morning", "утро"), ("midday", "день"), ("evening", "вечер")):
    item = sch.get(key) or {}
    part = "%s %s" % (label, item.get("time", "\u2014"))
    if not item.get("enabled", True):
        part += " (выкл)"
    parts.append(part)
print(", ".join(parts))
' "$CONFIG_JSON"
}

# ---------------------------------------------------------------------------
# Шаги установки
# ---------------------------------------------------------------------------
ensure_dirs() {
  step "Каталоги"
  say "Приложение: $APP_DIR"
  say "Логи:       $LOG_DIR"
  say "LaunchAgent: $PLIST_DIR"
  run mkdir -p "$APP_DIR" "$CONFIG_DIR" "$PLIST_DIR" "$LOG_DIR" "$APP_DIR/data"
  if [ "$DRY_RUN" -eq 0 ]; then
    chmod 700 "$APP_DIR" "$CONFIG_DIR" "$LOG_DIR" 2>/dev/null || true
  fi
}

same_dir() {
  [ -d "$1" ] && [ -d "$2" ] || return 1
  [ "$(cd "$1" && pwd -P)" = "$(cd "$2" && pwd -P)" ]
}

copy_project() {
  step "Копирую программу"

  if same_dir "$SCRIPT_DIR" "$APP_DIR"; then
    say "Запуск из папки приложения — копирование не требуется."
  else
    run rm -rf "$APP_DIR/src"
    run mkdir -p "$APP_DIR/src"
    run cp -R "$SCRIPT_DIR/src/." "$APP_DIR/src/"
    say "код → $APP_DIR/src"

    if [ -d "$SCRIPT_DIR/dashboard" ]; then
      run mkdir -p "$APP_DIR/dashboard"
      run cp -R "$SCRIPT_DIR/dashboard/." "$APP_DIR/dashboard/"
      say "дашборд → $APP_DIR/dashboard"
    else
      warn "папки dashboard/ нет — пропускаю."
    fi

    if [ -d "$SCRIPT_DIR/ops" ]; then
      run mkdir -p "$APP_DIR/ops"
      run cp -R "$SCRIPT_DIR/ops/." "$APP_DIR/ops/"
    fi
    if [ -f "$SCRIPT_DIR/README.md" ]; then
      run cp -f "$SCRIPT_DIR/README.md" "$APP_DIR/README.md"
      run chmod 644 "$APP_DIR/README.md"
    fi
    if [ -f "$SCRIPT_DIR/install.sh" ]; then
      run cp -f "$SCRIPT_DIR/install.sh" "$APP_DIR/install.sh"
      run chmod 755 "$APP_DIR/install.sh"
    fi
    if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
      run cp -f "$SCRIPT_DIR/uninstall.sh" "$APP_DIR/uninstall.sh"
      run chmod 755 "$APP_DIR/uninstall.sh"
    fi
  fi

  # Карантин macOS: файлы, скачанные из интернета, launchd может не прочитать.
  if command -v xattr >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
      show_cmd xattr -dr com.apple.quarantine "$APP_DIR"
    else
      xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
    fi
  fi

  if [ "$DRY_RUN" -eq 0 ]; then
    if "$PYTHON" -m compileall -q "$APP_DIR/src" >/dev/null 2>&1; then
      say "проверка синтаксиса Python: ок"
    else
      warn "в src/ есть ошибки синтаксиса — агент может не запуститься."
    fi
  fi
}

ensure_config() {
  step "Конфиг"
  # Если install.sh запущен из папки приложения, пример уже на месте:
  # cp файла самого в себя на macOS завершается ошибкой "are identical".
  if [ "$SCRIPT_DIR/config/config.example.json" -ef "$CONFIG_EXAMPLE" ]; then
    say "config.example.json уже на месте."
  else
    run cp -f "$SCRIPT_DIR/config/config.example.json" "$CONFIG_EXAMPLE"
    run chmod 644 "$CONFIG_EXAMPLE"
  fi

  if [ -f "$CONFIG_JSON" ]; then
    say "config.json уже есть — не перезаписываю (правки сохраняются)."
  elif [ -f "$SCRIPT_DIR/config/config.json" ]; then
    run cp -p "$SCRIPT_DIR/config/config.json" "$CONFIG_JSON"
    say "config.json скопирован из проекта."
  else
    run cp -p "$CONFIG_EXAMPLE" "$CONFIG_JSON"
    say "config.json создан из config.example.json."
  fi

  run chmod 600 "$CONFIG_JSON"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$PYTHON" -m json.tool "$CONFIG_JSON" >/dev/null 2>&1 ||
      die "config.json — невалидный JSON. Исправь файл: $CONFIG_JSON"
  fi
}

# Вставляет ключ в llm.api_key атомарно (json, а не sed: в конфиге кириллица).
inject_llm_key() {
  "$PYTHON" - "$CONFIG_JSON" "$1" <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
key = (sys.argv[2] or "").strip().strip("\"'")
if not key:
    raise SystemExit(0)

try:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
except (OSError, ValueError) as exc:
    print("config.json не читается: %s" % exc, file=sys.stderr)
    raise SystemExit(1)

llm = cfg.get("llm")
if not isinstance(llm, dict):
    llm = {}
    cfg["llm"] = llm
if (llm.get("api_key") or "").strip():
    raise SystemExit(0)

llm["api_key"] = key

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".config-", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

setup_llm_key() {
  step "LLM-ключ"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "проверю llm.api_key; если пуст — подхвачу DDD_API_KEY из $CREDENTIALS"
    return 0
  fi

  local key_now key_found
  key_now="$(cfg_get llm.api_key || true)"
  if [ -n "$key_now" ]; then
    say "llm.api_key уже заполнен — не трогаю."
    return 0
  fi

  key_found=""
  if [ -f "$CREDENTIALS" ]; then
    key_found="$(grep -o 'DDD_API_KEY: sk-[^ }]*' "$CREDENTIALS" 2>/dev/null |
      head -n 1 | cut -d' ' -f2 || true)"
  fi

  if [ -z "$key_found" ]; then
    say "ключ не найден — LLM будет в скриптованном режиме (данные сохраняются)."
    return 0
  fi

  inject_llm_key "$key_found" || die "Не удалось записать llm.api_key в $CONFIG_JSON"
  run chmod 600 "$CONFIG_JSON"
  say "llm.api_key подхвачен из $(basename "$CREDENTIALS") (в конфиге, права 600)."
}

rotate_log_if_needed() {
  [ -f "$LOG_FILE" ] || return 0
  local size
  size="$(wc -c <"$LOG_FILE" | tr -d ' ')"
  if [ "$size" -gt 5242880 ]; then
    step "Ротация лога"
    say "agent.log больше 5 МБ — сохраняю в agent.log.1 и начинаю заново."
    run cp -p "$LOG_FILE" "$LOG_FILE.1"
    if [ "$DRY_RUN" -eq 0 ]; then
      : >"$LOG_FILE"
    fi
  fi
}

install_plist() {
  step "LaunchAgent"
  run mkdir -p "$PLIST_DIR"

  if [ "$DRY_RUN" -eq 1 ]; then
    show_cmd "$PYTHON" - "$PLIST_TEMPLATE" "$PLIST_DST" "$APP_DIR" "$LOG_DIR" "$PYTHON"
    say "    (сгенерирую plist подстановкой __APP_DIR__ / __LOG_DIR__)"
    return 0
  fi

  "$PYTHON" - "$PLIST_TEMPLATE" "$PLIST_DST" "$APP_DIR" "$LOG_DIR" "$PYTHON" <<'PY'
import os
import plistlib
import sys
import tempfile

src, dst, app_dir, log_dir, python = sys.argv[1:6]

with open(src, encoding="utf-8") as fh:
    text = fh.read()


def xml_escape(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


text = text.replace("__APP_DIR__", xml_escape(app_dir))
text = text.replace("__LOG_DIR__", xml_escape(log_dir))
data = plistlib.loads(text.encode("utf-8"))

prog = data.get("ProgramArguments") or []
if data.get("Label") != "com.lifeos.agent":
    raise SystemExit("plist: неверный Label")
if len(prog) < 2 or prog[-1] != "src/main.py":
    raise SystemExit("plist: ProgramArguments должен заканчиваться на src/main.py")
if python and python != "/usr/bin/python3" and prog[0] == "/usr/bin/python3":
    prog = [python] + prog[1:]
    data["ProgramArguments"] = prog

os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst) or ".", prefix=".com.lifeos.agent-", suffix=".plist")
try:
    with os.fdopen(fd, "wb") as fh:
        fh.write(plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False))
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY

  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST_DST" >/dev/null 2>&1 ||
      die "plist не прошёл проверку plutil: $PLIST_DST"
  fi
  say "plist: $PLIST_DST"
  say "интерпретатор: $PYTHON"
}

token_hint() {
  cat <<EOF

  ─── Как включить бота (нужен токен) ─────────────────────────────────
  1. Открой Telegram и найди @BotFather (у него галочка verified).
  2. Отправь ему /newbot, придумай имя и username (username — на "bot").
  3. Скопируй токен, который он пришлёт: 1234567890:AAH...
  4. Впиши его в конфиг:  "token": "1234567890:AAH..."
     Файл:   $CONFIG_JSON
     Открыть: open -e "$CONFIG_JSON"
  5. Узнай свой chat_id у @userinfobot (поле Id) и впиши в "allowed_chat_id".
  6. Примени:  bash install.sh --reload
  ─────────────────────────────────────────────────────────────────────
EOF
}

chat_hint() {
  cat <<EOF

  ─── Осталось вписать chat_id ────────────────────────────────────────
  Токен есть, но "allowed_chat_id" пустой (0) — бот не знает, кому писать.
  1. Напиши @userinfobot в Telegram — он пришлёт "Id: 123456789".
  2. Впиши это число в "allowed_chat_id" (без кавычек) в файле:
     $CONFIG_JSON
  3. Примени:  bash install.sh --reload
  ─────────────────────────────────────────────────────────────────────
EOF
}

restart_agent() {
  step "Запуск агента"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] launchctl bootout gui/$(id -u)/$LABEL (снять старый запуск, если был)"
    say "[dry-run] launchctl bootstrap gui/$(id -u) \"$PLIST_DST\""
    say "[dry-run] если не выйдет — launchctl load -w \"$PLIST_DST\""
    return 0
  fi

  command -v launchctl >/dev/null 2>&1 ||
    die "launchctl не найден — это не macOS?"

  local domain
  domain="gui/$(id -u)"
  launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || true
  sleep 1

  if launchctl bootstrap "$domain" "$PLIST_DST" >/dev/null 2>&1; then
    say "агент запущен (launchctl bootstrap)"
  elif launchctl load -w "$PLIST_DST" >/dev/null 2>&1; then
    say "агент запущен (launchctl load -w)"
  else
    warn "не удалось загрузить агент автоматически."
    say "Вручную: launchctl bootstrap $domain \"$PLIST_DST\""
    say "Если не выйдет — перелогинься и повтори: bash install.sh --reload"
  fi
}

start_or_hold() {
  local problem
  problem="$(config_problem)"

  if [ -z "$problem" ]; then
    restart_agent
    STARTED=1
    return 0
  fi

  step "Запуск агента"
  say "Пока не запускаю: в config.json не заполнено то, без чего бот не работает."
  if [ "$DRY_RUN" -eq 0 ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  fi
  if [ "$problem" = "token" ] || [ "$problem" = "both" ]; then
    token_hint
  fi
  if [ "$problem" = "chat" ]; then
    chat_hint
  fi
}

print_status() {
  step "Статус"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] launchctl list | grep $LABEL"
    return 0
  fi

  if launchctl list "$LABEL" >/dev/null 2>&1; then
    local list_line pid
    list_line="$(launchctl list | grep -F "$LABEL" || true)"
    if [ -n "$list_line" ]; then
      say "launchctl: $list_line"
      pid="$(printf '%s' "$list_line" | awk '{print $1}')"
      if [ -n "$pid" ] && [ "$pid" != "-" ]; then
        say "процесс работает (PID $pid)"
      else
        warn "процесс не запущен — смотри лог: $LOG_FILE"
      fi
    fi
  else
    warn "LaunchAgent не найден в launchctl (не установлен или не загружен)."
  fi

  if [ -s "$LOG_FILE" ]; then
    say "последние строки лога ($LOG_FILE):"
    tail -n 6 "$LOG_FILE" 2>/dev/null | sed 's/^/    /' || true
  else
    say "лог пока пуст: $LOG_FILE"
  fi
}

print_summary() {
  step "Итог"
  say "Программа:  $APP_DIR"
  say "Конфиг:     $CONFIG_JSON (права 600)"
  say "Логи:       $LOG_FILE"
  say "Дашборд:    $APP_DIR/dashboard/index.html"
  say "Проверка:   launchctl list | grep $LABEL"

  if [ "$DRY_RUN" -eq 0 ] && [ -f "$CONFIG_JSON" ]; then
    say "Расписание: $(schedule_line)"
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    say "Режим --dry-run: ничего не изменено."
  elif [ "$STARTED" -eq 1 ]; then
    say "Агент запущен: первое сообщение придёт по расписанию (утро/день/вечер)."
  else
    say "Агент установлен, но НЕ запущен: заполни config.json и выполни bash install.sh --reload"
  fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  printf '\n%sLife OS — установка агента%s\n' "$C_BOLD" "$C_RESET"
  printf '%s  macOS LaunchAgent: автозапуск и перезапуск при падении%s\n' "$C_DIM" "$C_RESET"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s  Режим: --dry-run (ничего не меняется)%s\n' "$C_YELLOW" "$C_RESET"
  fi

  preflight

  if [ "$RELOAD_ONLY" -eq 1 ]; then
    say "Режим: только перезапуск (--reload), файлы не копирую."
    if [ "$DRY_RUN" -eq 0 ]; then
      [ -f "$CONFIG_JSON" ] ||
        die "Конфиг не найден: $CONFIG_JSON
Сначала выполни полную установку: bash install.sh"
      [ -f "$APP_DIR/src/main.py" ] ||
        die "Программа не установлена в $APP_DIR
Сначала выполни полную установку: bash install.sh"
      "$PYTHON" -m json.tool "$CONFIG_JSON" >/dev/null 2>&1 ||
        die "config.json — невалидный JSON. Исправь файл: $CONFIG_JSON"
      chmod 600 "$CONFIG_JSON" 2>/dev/null || true
    fi
  fi

  ensure_dirs

  if [ "$RELOAD_ONLY" -eq 0 ]; then
    copy_project
    ensure_config
    setup_llm_key
  fi

  rotate_log_if_needed
  install_plist
  start_or_hold
  print_status
  print_summary
}

main "$@"
