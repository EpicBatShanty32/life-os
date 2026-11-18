#!/bin/bash
# Служба бота Life OS (launchd, ярлык com.lifeos.bot): поставить на автозапуск,
# запустить, остановить, проверить, посмотреть логи, снять.
#
#   bash tools/bot-service.sh install      поставить службу и запустить бота
#   bash tools/bot-service.sh install --dry-run  только собрать plist, не запускать
#   bash tools/bot-service.sh status       бот онлайн? (код 0 — да, 1 — нет)
#   bash tools/bot-service.sh start        запустить (служба уже поставлена)
#   bash tools/bot-service.sh stop         остановить до следующего входа/старта
#   bash tools/bot-service.sh restart      перезапустить (после смены токена и т.п.)
#   bash tools/bot-service.sh logs [N]     последние N строк лога бота (по умолчанию 20)
#   bash tools/bot-service.sh uninstall    снять с автозапуска (база и копии целы)
#
# Что даёт служба:
#   * бот живёт без открытого терминала и без сессии агента;
#   * поднимается сам при входе в систему (RunAtLoad + plist в ~/Library/LaunchAgents);
#   * перезапускается после падения (KeepAlive SuccessfulExit=false, не чаще раза в минуту);
#   * не даёт маку заснуть, пока бот нужен (caffeinate -i -s);
#   * пишет data/logs/bot.log с ротацией 1 МБ × 3; служебный вывод launchd —
#     data/logs/bot.launchd.log;
#   * токен остаётся только в config/config.json: в plist его нет.
#     Исключение — явный флаг install --token-in-plist (см. ниже).
#
# Токен: если он не задан, скрипт ничего не ставит и печатает, что вписать.
# Второй экземпляр не запускается: бот держит замок data/bot.lock.
#
# Пути можно подменить переменными LIFEOS_ROOT, LIFEOS_LOGS_DIR, LIFEOS_PYTHON.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}
LABEL=com.lifeos.bot
PLIST_SRC=$ROOT/ops/com.lifeos.bot.plist
PLIST_DST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG_DIR=${LIFEOS_LOGS_DIR:-$ROOT/data/logs}
UID_NUMBER=$(id -u)
DOMAIN=gui/$UID_NUMBER
LAUNCHD_LOG=$LOG_DIR/bot.launchd.log
BOT_LOG=$LOG_DIR/bot.log
TOKEN_HINT="токен не задан — создай бота в @BotFather и впиши token в config/config.json → telegram.token"

say() { printf '%s\n' "$*"; }
blank() { printf '\n'; }
err() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Аргументы
# ---------------------------------------------------------------------------
ACTION=install
TOKEN_IN_PLIST=0
DRY_RUN=0
LOG_LINES=20

while [ $# -gt 0 ]; do
  case "$1" in
    install|start|stop|restart|status|logs|uninstall)
      ACTION=$1 ;;
    --status) ACTION=status ;;
    --uninstall) ACTION=uninstall ;;
    --start) ACTION=start ;;
    --stop) ACTION=stop ;;
    --token-in-plist) TOKEN_IN_PLIST=1 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    -h|--help|help) ACTION=help ;;
    [0-9]*) LOG_LINES=$1 ;;
    *) err "Неизвестный аргумент: $1"; err "Команды: install | start | stop | restart | status | logs | uninstall"; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------

detect_python() {
  # launchd получает доступ к «Документам» только когда задание запущено из
  # Python.app (официальная сборка Python.framework): /bin/bash и
  # /usr/bin/python3 в «Документы» не пускают. Поэтому ищем именно Python.app.
  candidate=${LIFEOS_PYTHON:-}
  if [ -z "$candidate" ] && command -v python3 >/dev/null 2>&1; then
    candidate=$(command -v python3)
  fi
  if [ -n "$candidate" ]; then
    case "$candidate" in
      *Python.framework*) printf '%s\n' "$candidate"; return 0 ;;
    esac
  fi
  for app in /Library/Frameworks/Python.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python; do
    if [ -x "$app" ]; then printf '%s\n' "$app"; return 0; fi
  done
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
  return 1
}

PYTHON_BIN=$(detect_python) || PYTHON_BIN=""

have_python() { [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; }

# Печатает "есть" / "нет (причина)", не показывая сам токен.
token_state() {
  if [ -n "${LIFEOS_TELEGRAM_TOKEN:-}" ]; then printf 'есть (переменная окружения)'; return 0; fi
  if [ ! -f "$ROOT/config/config.json" ]; then printf 'нет (файла config/config.json нет)'; return 0; fi
  if ! have_python; then printf 'не проверить (нет python)'; return 0; fi
  "$PYTHON_BIN" - "$ROOT/config/config.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print("не прочитать config/config.json: %s" % exc)
    sys.exit(0)
telegram = data.get("telegram") or {}
token = str(telegram.get("token") or "").strip()
print("есть (config/config.json → telegram.token)" if token else "нет")
PY
}

# Куда смотрит база: из config/config.json (paths.db), иначе живая база v3.
db_path() {
  if [ -n "${LIFEOS_DB_PATH:-}" ]; then printf '%s' "$LIFEOS_DB_PATH"; return 0; fi
  if have_python && [ -f "$ROOT/config/config.json" ]; then
    "$PYTHON_BIN" - "$ROOT" <<'PY' 2>/dev/null && return 0
import json, os, sys
root = os.path.abspath(sys.argv[1])
raw = ""
try:
    data = json.load(open(os.path.join(root, "config", "config.json"), encoding="utf-8"))
    raw = str(((data.get("paths") or {}).get("db") or "")).strip()
except Exception:
    pass
if raw:
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    print(os.path.realpath(path))
PY
  fi
  for candidate in "$ROOT/data/lifeos-v3.db" "$ROOT/data/lifeos.db"; do
    if [ -f "$candidate" ]; then printf '%s' "$candidate"; return 0; fi
  done
  printf '%s' "$ROOT/data/lifeos-v3.db"
}

service_field() {
  # Значение поля из `launchctl print` (пустая строка, если службы нет).
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk -v key="$1" '
    $0 ~ "^[[:space:]]*" key " = " { sub("^[[:space:]]*" key " = ", ""); print; exit }'
}

service_pid() { service_field pid | tr -dc '0-9'; }

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

pid_alive() { [ -n "${1:-}" ] && ps -p "$1" >/dev/null 2>&1; }

bot_python_processes() {
  # Процессы самого бота (без обёртки caffeinate): "pid ppid команда".
  for candidate in $(pgrep -f "app.bot.main|tools/bot_launcher.py" 2>/dev/null || true); do
    command=$(ps -p "$candidate" -o command= 2>/dev/null || true)
    case "$command" in
      *caffeinate*) continue ;;
    esac
    parent=$(ps -p "$candidate" -o ppid= 2>/dev/null | tr -d ' ' || true)
    printf '%s %s %s\n' "$candidate" "${parent:-0}" "$command"
  done
}

foreign_bot_processes() {
  # Процессы бота, не относящиеся к службе: pid, чей pid не совпадает с pid
  # службы и чей родитель — не она. Пусто — посторонних нет.
  service=$1
  bot_python_processes | awk -v svc="$service" 'svc != "" && ($1 == svc || $2 == svc) { next } { print }'
}

render_plist() {
  # Собирает установочный plist из шаблона ops/com.lifeos.bot.plist.
  # Подстановка и проверка — в python: значения могут содержать &, <, >,
  # а токен не должен мелькать в аргументах командной строки.
  [ -f "$PLIST_SRC" ] || { err "ОШИБКА: нет шаблона $PLIST_SRC"; exit 1; }
  have_python || { err "ОШИБКА: не найден python3. Поставьте Python с python.org или задайте LIFEOS_PYTHON."; exit 1; }

  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  db=$(db_path)
  set +e
  "$PYTHON_BIN" - "$PLIST_SRC" "$PLIST_DST" "$PYTHON_BIN" "$ROOT" "$LOG_DIR" "$db" "$TOKEN_IN_PLIST" <<'PY'
import json
import os
import sys

src, dst, python_bin, root, log_dir, db_path, token_flag = sys.argv[1:8]


def escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


token_line = ""
if token_flag == "1":
    token = os.environ.get("LIFEOS_TELEGRAM_TOKEN", "").strip()
    if not token:
        try:
            with open(os.path.join(root, "config", "config.json"), encoding="utf-8") as fh:
                token = str(((json.load(fh).get("telegram") or {}).get("token")) or "").strip()
        except Exception:
            token = ""
    if not token:
        sys.exit(4)
    token_line = "\t\t<key>LIFEOS_TELEGRAM_TOKEN</key>\n\t\t<string>%s</string>" % escape(token)

with open(src, encoding="utf-8") as fh:
    text = fh.read()
for key, value in (
    ("__PYTHON__", escape(python_bin)),
    ("__REPO__", escape(root)),
    ("__LOG_DIR__", escape(log_dir)),
    ("__DB_PATH__", escape(db_path)),
    # Строка токена в шаблоне — комментарий-заглушка: без неё plist-шаблон
    # остаётся валидным и его можно проверять plutil -lint.
    ("<!-- __TOKEN_LINE__ -->", token_line),
):
    text = text.replace(key, value)
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    if [ "$status" -eq 4 ]; then
      err "ОШИБКА: --token-in-plist, а токена нет. Впишите его в config/config.json → telegram.token."
    else
      err "ОШИБКА: не удалось собрать plist из $PLIST_SRC"
    fi
    exit 1
  fi

  if ! plutil -lint "$PLIST_DST" >/dev/null 2>&1; then
    err "ОШИБКА: получился некорректный plist: $PLIST_DST"
    exit 1
  fi
  # Токен (если его всё же вписали в plist) не должен быть доступен другим.
  chmod 600 "$PLIST_DST" 2>/dev/null || true
}

reload_service() {
  # Перечитать plist: снять старую службу и поднять заново.
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  sleep 1
  if ! launchctl bootstrap "$DOMAIN" "$PLIST_DST" >/dev/null 2>&1; then
    launchctl load -w "$PLIST_DST" >/dev/null 2>&1 || true
  fi
}

wait_for_pid() {
  # Ждёт появления процесса службы (до 10 секунд), печатает pid или пусто.
  i=0
  while [ "$i" -lt 40 ]; do
    pid=$(service_pid)
    if [ -n "$pid" ] && pid_alive "$pid"; then printf '%s' "$pid"; return 0; fi
    sleep 0.25
    i=$((i + 1))
  done
  return 1
}

tail_file() {
  file=$1
  lines=${2:-8}
  [ -f "$file" ] || return 0
  say "— последние строки $file:"
  tail -n "$lines" "$file" 2>/dev/null | sed 's/^/  /'
}

token_line_for_install() {
  state=$(token_state)
  case "$state" in
    есть*) say "Токен: $state" ;;
    *)
      say "Токен: $state"
      blank
      err "$TOKEN_HINT"
      err "Потом запустите снова: bash tools/bot-service.sh install"
      exit 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
do_install() {
  token_line_for_install

  db=$(db_path)
  if [ ! -f "$db" ]; then
    say "Внимание: базы $db пока нет — бот создаст новую при первом запуске."
  fi

  render_plist
  say "plist: $PLIST_DST"
  say "Python службы: $PYTHON_BIN"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "Сухой прогон: plist собран, служба не тронута."
    exit 0
  fi

  reload_service
  if pid=$(wait_for_pid); then
    say "Служба поднята: $LABEL, процесс бота pid $pid."
  else
    say "Служба поставлена, процесс пока не отмечен — смотрим лог."
  fi
  blank
  say "Логи: $BOT_LOG (данные бота), $LAUNCHD_LOG (служебный вывод)."
  say "Проверить: bash tools/bot-service.sh status"
  blank
  tail_file "$LAUNCHD_LOG" 6
}

# ---------------------------------------------------------------------------
# start / stop / restart
# ---------------------------------------------------------------------------
do_start() {
  if [ ! -f "$PLIST_DST" ]; then
    err "Служба ещё не поставлена. Сначала: bash tools/bot-service.sh install"
    exit 1
  fi
  token_line_for_install
  render_plist
  if is_loaded; then
    launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || reload_service
  else
    reload_service
  fi
  if pid=$(wait_for_pid); then
    say "Бот запущен службой: pid $pid. Смотреть: bash tools/bot-service.sh status"
    return 0
  fi
  err "Процесс не поднялся за 10 секунд. Лог: $LAUNCHD_LOG"
  tail_file "$LAUNCHD_LOG" 10
  return 1
}

do_stop() {
  if ! is_loaded; then
    say "Служба $LABEL и не была запущена."
    return 0
  fi
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
  # Даём процессу бота выйти по SIGTERM: он закрывает соединение с Telegram
  # аккуратно, без «моргания» у владельца.
  i=0
  while [ "$i" -lt 40 ]; do
    remaining=$(bot_python_processes || true)
    [ -z "$remaining" ] && break
    sleep 0.25
    i=$((i + 1))
  done
  if [ -n "${remaining:-}" ]; then
    err "Служба снята, но процесс бота ещё жив:"
    printf '%s\n' "$remaining" | sed 's/^/  /'
    err "Остановите вручную: pkill -f app.bot.main"
    return 1
  fi
  say "Бот остановлен. Сам поднимется при следующем входе в систему."
  say "Запустить сейчас: bash tools/bot-service.sh start"
}

do_restart() {
  if ! is_loaded; then
    do_start
    return $?
  fi
  token_line_for_install
  render_plist
  launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || { reload_service; }
  if pid=$(wait_for_pid); then
    say "Бот перезапущен: pid $pid."
    return 0
  fi
  err "После перезапуска процесса нет. Лог: $LAUNCHD_LOG"
  tail_file "$LAUNCHD_LOG" 10
  return 1
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
do_status() {
  say "Служба $LABEL ($DOMAIN)"
  if is_loaded; then
    say "  поставлена: $PLIST_DST"
    say "  состояние:  $(service_field state)"
    pid=$(service_pid)
    if [ -n "$pid" ]; then
      if pid_alive "$pid"; then
        say "  процесс:    pid $pid (жив)"
      else
        say "  процесс:    pid $pid (уже завершился)"
      fi
    else
      say "  процесс:    нет"
    fi
    code=$(service_field "last exit code")
    case "$code" in
      ""|*[!0-9]*) : ;;  # "(never exited)" и пусто — сказать нечего
      *) say "  код выхода: $code (0 — чисто, другое — падал)" ;;
    esac
    runs=$(service_field runs)
    [ -n "$runs" ] && say "  запусков:   $runs"
  elif [ -f "$PLIST_DST" ]; then
    say "  поставлена: $PLIST_DST"
    say "  состояние:  не запущена (остановлена вручную)"
  else
    say "  не поставлена — команда: bash tools/bot-service.sh install"
  fi
  say "  токен:      $(token_state)"
  say "  база:       $(db_path)"
  say "  логи:       $BOT_LOG"
  say "              $LAUNCHD_LOG"

  running=0
  if pid=$(service_pid) && [ -n "$pid" ] && pid_alive "$pid"; then running=1; fi

  blank
  if [ -f "$BOT_LOG" ]; then
    tail_file "$BOT_LOG" 5
  else
    say "— лога бота ещё нет: он появится при первом запуске."
  fi
  blank
  extra=$(foreign_bot_processes "${pid:-}" || true)
  if [ "$running" -eq 1 ]; then
    if [ -n "$extra" ]; then
      say "Итог: служба работает, но рядом живёт второй экземпляр бота —"
      printf '%s\n' "$extra" | sed 's/^/  /'
      say "  Telegram отдаёт сообщения только одному поллеру: лишний стоит остановить."
      return 1
    fi
    say "Итог: бот онлайн — служба держит его, сообщения в Telegram доходят."
    return 0
  fi
  say "Итог: бот не работает."
  say "  поднять: bash tools/bot-service.sh start"
  say "  если падает: посмотрите $LAUNCHD_LOG и $BOT_LOG"
  return 1
}

# ---------------------------------------------------------------------------
# logs / uninstall / help
# ---------------------------------------------------------------------------
do_logs() {
  [ -f "$BOT_LOG" ] || { say "Лога ещё нет: $BOT_LOG"; return 0; }
  tail -n "$LOG_LINES" "$BOT_LOG"
}

do_uninstall() {
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
  rm -f "$PLIST_DST"
  say "Служба $LABEL снята: бот больше не поднимается сам."
  say "База, копии и логи не тронуты."
  say "Запускать вручную: bash tools/run-bot.sh"
}

do_help() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

case "$ACTION" in
  install) do_install ;;
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  status) do_status ;;
  logs) do_logs ;;
  uninstall) do_uninstall ;;
  help) do_help ;;
esac
