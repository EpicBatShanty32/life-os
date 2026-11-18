#!/bin/bash
# Проверка бэкапа Life OS: «а из этой копии точно можно восстановиться?»
#
#   tools/restore-check.sh [ФАЙЛ] [--dir PATH] [--quiet]
#
# Что делает по шагам:
#   1. берёт указанный файл или самый свежий из data/backups/;
#   2. разворачивает его во временный каталог (оригинал не трогает);
#   3. открывает копию и проверяет PRAGMA integrity_check;
#   4. пробует записать в копию одну служебную таблицу — файл должен быть
#      не только читаемым, но и рабочим;
#   5. считает строки в ключевых таблицах (entries, metrics, tasks, goals,
#      habits, transactions, books, checkins, jobs, inbox_items, spheres,
#      projects, finance_accounts, courses);
#   6. печатает «бэкап годен» и сводку или «бэкап НЕ годен» с причиной.
#
# Коды выхода: 0 — годен, 1 — не годен (или файл не найден), 2 — неверные
# аргументы. Временный каталог удаляется всегда; токенов скрипт не читает.

set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}

FILE=""
BACKUP_DIR=${LIFEOS_BACKUP_DIR:-$ROOT/data/backups}
QUIET=0

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)   [ $# -ge 2 ] || { echo "после --dir нужен путь" >&2; exit 2; }; BACKUP_DIR=$2; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Неизвестный аргумент: $1 (--help покажет справку)" >&2; exit 2 ;;
    *)  FILE=$1; shift ;;
  esac
done

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

command -v sqlite3 >/dev/null 2>&1 || { printf 'ОШИБКА: не найден sqlite3\n' >&2; exit 1; }

if [ -z "$FILE" ]; then
  FILE=$(ls -1 "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db 2>/dev/null | sort -r | head -n 1 || true)
  [ -n "$FILE" ] || { printf 'ОШИБКА: в %s нет ни одной копии (сначала tools/backup.sh)\n' "$BACKUP_DIR" >&2; exit 1; }
fi

if [ ! -f "$FILE" ]; then
  printf 'ОШИБКА: файла нет: %s\n' "$FILE" >&2
  exit 1
fi
if [ ! -s "$FILE" ]; then
  printf 'бэкап НЕ годен: файл пустой (%s)\n' "$FILE" >&2
  exit 1
fi

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/lifeos-restore.XXXXXX") || { printf 'ОШИБКА: не удалось создать временный каталог\n' >&2; exit 1; }
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT INT TERM

WORK=$TMP_DIR/work.db
cp "$FILE" "$WORK" || { printf 'бэкап НЕ годен: не удалось скопировать файл\n' >&2; exit 1; }
# Если копия снята не нашим скриптом и рядом лежит -wal, переносим и его —
# иначе последние записи потеряются.
if [ -f "$FILE-wal" ]; then cp "$FILE-wal" "$WORK-wal" 2>/dev/null || true; fi
chmod u+w "$WORK" 2>/dev/null || true

reason=""
integrity=$(sqlite3 "$WORK" "PRAGMA integrity_check;" 2>/dev/null | head -n 1 || true)
if [ "$integrity" != "ok" ]; then
  reason="integrity_check: ${integrity:-файл не открывается как база SQLite}"
fi

tables=0
if [ -z "$reason" ]; then
  tables=$(sqlite3 "$WORK" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo 0)
  if [ "${tables:-0}" -lt 1 ] 2>/dev/null; then
    reason="в файле нет ни одной таблицы"
  fi
fi

if [ -z "$reason" ]; then
  if ! sqlite3 "$WORK" "CREATE TABLE _restore_probe(x INTEGER); DROP TABLE _restore_probe;" >/dev/null 2>&1; then
    reason="копия открывается только для чтения — запись в неё невозможна"
  fi
fi

fk_violations=0
if [ -z "$reason" ]; then
  fk_violations=$(sqlite3 "$WORK" "PRAGMA foreign_key_check;" 2>/dev/null | wc -l | tr -d ' ')
fi

if [ -n "$reason" ]; then
  printf 'бэкап НЕ годен: %s\n' "$reason" >&2
  printf 'Файл: %s\n' "$FILE" >&2
  exit 1
fi

# --- считаем строки по ключевым таблицам ----------------------------------

KEY_TABLES="entries metrics tasks goals habits transactions books checkins jobs inbox_items spheres projects finance_accounts courses"
total=0
rows_report=""
for table in $KEY_TABLES; do
  exists=$(sqlite3 "$WORK" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='$table';" 2>/dev/null || echo 0)
  if [ "${exists:-0}" = "1" ]; then
    count=$(sqlite3 "$WORK" "SELECT count(*) FROM \"$table\";" 2>/dev/null || echo 0)
    total=$((total + count))
    rows_report="$rows_report  $table: $count
"
  else
    rows_report="$rows_report  $table: нет (старая схема)
"
  fi
done

size=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE" 2>/dev/null || echo 0)
human=$(awk -v b="$size" 'BEGIN { if (b >= 1048576) printf "%.1f МБ", b/1048576; else if (b >= 1024) printf "%.0f КБ", b/1024; else printf "%d Б", b }')

say "Проверяю копию: $FILE"
say "$rows_report"
say "Таблиц: $tables · строк в ключевых таблицах: $total · целостность: ok · размер: $human"
if [ "${fk_violations:-0}" != "0" ]; then
  say "Замечание: ссылок на несуществующие строки — $fk_violations (данные стоит проверить)."
fi
printf 'бэкап годен\n'
exit 0
