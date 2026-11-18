#!/bin/bash
# Служба автобэкапа базы Life OS (launchd, ярлык com.lifeos.backup):
# поставить / проверить / снять.
#
#   deploy/install-backup-agent.sh                 поставить (сразу делает копию)
#   deploy/install-backup-agent.sh --status        что со службой и копиями
#   deploy/install-backup-agent.sh --uninstall     снять службу
#   deploy/install-backup-agent.sh --db PATH       какой базе делать копии
#
# Служба делает копию каждый день в 04:00 и ещё один раз при загрузке мака.
# Остановить бота для этого не нужно: sqlite3 ".backup" работает на живой базе.
# Копии лежат в data/backups, журнал — data/logs/backup.log.
#
# Путь к базе закрепляется в plist, чтобы расписание не зависело от того,
# из какого каталога и с какими переменными запущен скрипт.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}
LABEL=com.lifeos.backup
PLIST_SRC=$ROOT/ops/com.lifeos.backup.plist
PLIST_DST=$HOME/Library/LaunchAgents/$LABEL.plist
LOG_DIR=${LIFEOS_LOGS_DIR:-$ROOT/data/logs}
BACKUP_DIR=${LIFEOS_BACKUP_DIR:-$ROOT/data/backups}
UID_NUMBER=$(id -u)
DOMAIN=gui/$UID_NUMBER

ACTION=install
DB_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --status) ACTION=status ;;
    --uninstall) ACTION=uninstall ;;
    --db) [ $# -ge 2 ] || { echo "после --db нужен путь" >&2; exit 2; }; DB_ARG=$2; shift ;;
    -h|--help|install) ACTION=install ;;
    *) echo "Неизвестный аргумент: $1 (--status, --uninstall, --db PATH)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }

resolve_db() {
  if [ -n "$DB_ARG" ]; then
    case "$DB_ARG" in /*) printf '%s\n' "$DB_ARG" ;; *) printf '%s\n' "$ROOT/$DB_ARG" ;; esac
    return 0
  fi
  if [ -n "${LIFEOS_DB_PATH:-}" ]; then
    case "$LIFEOS_DB_PATH" in
      /*) printf '%s\n' "$LIFEOS_DB_PATH" ;;
      *)  printf '%s\n' "$ROOT/$LIFEOS_DB_PATH" ;;
    esac
    return 0
  fi
  if [ -f "$ROOT/config/config.json" ] && command -v plutil >/dev/null 2>&1; then
    cfg_db=$(plutil -extract paths.db raw -o - "$ROOT/config/config.json" 2>/dev/null || true)
    if [ -n "$cfg_db" ]; then
      case "$cfg_db" in /*) printf '%s\n' "$cfg_db" ;; *) printf '%s\n' "$ROOT/$cfg_db" ;; esac
      return 0
    fi
  fi
  if [ -f "$ROOT/data/lifeos-v3.db" ]; then printf '%s\n' "$ROOT/data/lifeos-v3.db"; return 0; fi
  if [ -f "$ROOT/data/lifeos.db" ]; then printf '%s\n' "$ROOT/data/lifeos.db"; return 0; fi
  return 1
}


detect_python() {
  # launchd получает доступ к «Документам» только если задание запущено из
  # Python.app (официальная сборка Python.framework). Поэтому ищем именно его.
  candidate=${LIFEOS_PYTHON:-}
  if [ -z "$candidate" ] && command -v python3 >/dev/null 2>&1; then
    candidate=$(command -v python3)
  fi
  if [ -n "$candidate" ]; then
    resolved=$(cd "$(dirname "$candidate")" && pwd -P)/$(basename "$candidate")
    case "$resolved" in
      *Python.framework*) printf '%s\n' "$candidate"; return 0 ;;
    esac
  fi
  for app in /Library/Frameworks/Python.framework/Versions/*/Resources/Python.app/Contents/MacOS/Python; do
    if [ -x "$app" ]; then printf '%s\n' "$app"; return 0; fi
  done
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return 0; fi
  return 1
}

case "$ACTION" in
  install)
    [ -f "$PLIST_SRC" ] || { echo "ОШИБКА: нет шаблона $PLIST_SRC" >&2; exit 1; }
    [ -x "$ROOT/tools/backup.sh" ] || { echo "ОШИБКА: нет $ROOT/tools/backup.sh" >&2; exit 1; }
    DB_PATH=$(resolve_db) || { echo "ОШИБКА: база не найдена — укажите --db PATH" >&2; exit 1; }
    [ -f "$DB_PATH" ] || { echo "ОШИБКА: файла базы нет: $DB_PATH" >&2; exit 1; }

    PYTHON_BIN=$(detect_python) || {
      echo "ОШИБКА: не найден python3. Установите Python с python.org или задайте LIFEOS_PYTHON." >&2
      exit 1
    }
    say "Python для службы: $PYTHON_BIN"

    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR" "$BACKUP_DIR"
    escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
    sed -e "s|__PYTHON__|$(escape "$PYTHON_BIN")|g" \
        -e "s|__REPO__|$(escape "$ROOT")|g" \
        -e "s|__LOG_DIR__|$(escape "$LOG_DIR")|g" \
        -e "s|__DB_PATH__|$(escape "$DB_PATH")|g" \
        -e "s|__BACKUP_DIR__|$(escape "$BACKUP_DIR")|g" \
        "$PLIST_SRC" > "$PLIST_DST"
    if ! plutil -lint "$PLIST_DST" >/dev/null 2>&1; then
      echo "ОШИБКА: получился некорректный plist: $PLIST_DST" >&2
      exit 1
    fi
    chmod 600 "$PLIST_DST" 2>/dev/null || true

    if [ -n "$DB_ARG" ] || [ -n "${LIFEOS_DB_PATH:-}" ]; then
      export LIFEOS_DB_PATH="$DB_PATH"
    fi

    say "База: $DB_PATH"
    say "Копии: $BACKUP_DIR"
    say ""
    say "Пробная копия прямо сейчас:"
    "$ROOT/tools/backup.sh" --db "$DB_PATH" --dir "$BACKUP_DIR" || {
      echo "ОШИБКА: пробный бэкап не получился — службу не ставлю" >&2
      exit 1
    }
    say ""

    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    sleep 1
    if ! launchctl bootstrap "$DOMAIN" "$PLIST_DST" >/dev/null 2>&1; then
      launchctl load -w "$PLIST_DST" >/dev/null 2>&1 || true
    fi
    sleep 1
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      say "Служба $LABEL поставлена: копия каждый день в 04:00 и при загрузке мака."
    else
      say "ВНИМАНИЕ: служба не отметилась в launchd. Проверьте: launchctl print $DOMAIN/$LABEL"
    fi
    say "Проверить: deploy/install-backup-agent.sh --status"
    ;;

  status)
    say "Служба $LABEL ($DOMAIN):"
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/state = |pid = |last exit code = /{print "  " $0}'
    else
      say "  не установлена — поставьте: deploy/install-backup-agent.sh"
    fi
    say ""
    say "Копии в $BACKUP_DIR:"
    if ls -1t "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db >/dev/null 2>&1; then
      ls -lht "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db 2>/dev/null | awk 'NR<=6 {print "  " $9 "  " $5}'
      total=$(ls -1 "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db 2>/dev/null | wc -l | tr -d ' ')
      say "  всего: $total"
    else
      say "  пока нет"
    fi
    say ""
    if [ -f "$LOG_DIR/backup.log" ]; then
      say "— последние строки $LOG_DIR/backup.log:"
      tail -n 6 "$LOG_DIR/backup.log" | sed 's/^/  /'
    fi
    if [ -f "$LOG_DIR/backup.launchd.log" ]; then
      say "— последние строки $LOG_DIR/backup.launchd.log:"
      tail -n 6 "$LOG_DIR/backup.launchd.log" | sed 's/^/  /'
    fi
    ;;

  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
    rm -f "$PLIST_DST"
    say "Служба $LABEL снята. Копии и журналы не тронуты."
    say "Сделать копию вручную: tools/backup.sh"
    ;;
esac
