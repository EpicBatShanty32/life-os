#!/bin/bash
# Горячий бэкап базы Life OS (SQLite) — бот при этом продолжает работать.
#
#   tools/backup.sh [--db PATH] [--dir PATH] [--keep N] [--no-rotate] [--quiet]
#
# Что делает:
#   * снимает копию через sqlite3 ".backup" — это штатный онлайн-бэкап SQLite:
#     открытые транзакции не рвутся, бот продолжает писать;
#   * кладёт копию в data/backups/YYYY-MM-DD-HH.db (одна копия на час);
#   * проверяет копию через PRAGMA integrity_check — негодная не попадает
#     в архив и удаляется;
#   * делает копию самодостаточной (journal_mode=DELETE), чтобы у файла не
#     было спутников -wal/-shm и его можно было просто вернуть на место;
#   * ротация: остаются 14 последних копий плюс по одной самой свежей за
#     каждый более старый месяц. Последние 14 не удаляются никогда;
#   * пишет строку в data/logs/backup.log.
#
# Где база: --db, потом $LIFEOS_DB_PATH, потом paths.db из
# config/config.json, потом data/lifeos-v3.db, потом data/lifeos.db.
# Установщик службы (deploy/install-backup-agent.sh) закрепляет путь в plist,
# чтобы расписание не зависело от окружения.
#
# Коды выхода: 0 — копия создана (в том числе когда другой бэкап уже идёт),
# 1 — ошибка. Токенов скрипт не читает и не пишет.

set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}

DB=""
BACKUP_DIR=${LIFEOS_BACKUP_DIR:-$ROOT/data/backups}
KEEP=${LIFEOS_BACKUP_KEEP:-14}
ROTATE=1
QUIET=0

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --db)    [ $# -ge 2 ] || { echo "после --db нужен путь" >&2; exit 2; }; DB=$2; shift 2 ;;
    --dir)   [ $# -ge 2 ] || { echo "после --dir нужен путь" >&2; exit 2; }; BACKUP_DIR=$2; shift 2 ;;
    --keep)  [ $# -ge 2 ] || { echo "после --keep нужно число" >&2; exit 2; }; KEEP=$2; shift 2 ;;
    --no-rotate) ROTATE=0; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный аргумент: $1 (--help покажет справку)" >&2; exit 2 ;;
  esac
done

case "$KEEP" in
  ''|*[!0-9]*) echo "--keep: нужно целое число, получено '$KEEP'" >&2; exit 2 ;;
esac
[ "$KEEP" -ge 1 ] || { echo "--keep: нужно хотя бы 1" >&2; exit 2; }

LOG_DIR=${LIFEOS_LOGS_DIR:-$ROOT/data/logs}
mkdir -p "$BACKUP_DIR" "$LOG_DIR" 2>/dev/null || true
LOG=$LOG_DIR/backup.log

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG" 2>/dev/null || true; }
die() { log "ошибка: $*"; printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || die "не найден sqlite3 (он входит в macOS; проверьте PATH)"
[ -d "$BACKUP_DIR" ] || die "не удалось создать каталог для копий: $BACKUP_DIR"

# --- где база -------------------------------------------------------------

resolve_db() {
  if [ -n "$DB" ]; then
    case "$DB" in /*) printf '%s\n' "$DB" ;; *) printf '%s\n' "$ROOT/$DB" ;; esac
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

DB_PATH=$(resolve_db) || die "база не найдена (укажите --db PATH или LIFEOS_DB_PATH)"
[ -f "$DB_PATH" ] || die "файл базы не найден: $DB_PATH"
[ -r "$DB_PATH" ] || die "нет прав на чтение базы: $DB_PATH"

# Первые 16 байт любого SQLite-файла — "SQLite format 3\0". Так мы не сделаем
# «бэкап» пустого файла, если бот упал посреди создания базы.
header=$(head -c 16 "$DB_PATH" 2>/dev/null || true)
[ "$header" = "SQLite format 3" ] || die "это не похоже на базу SQLite: $DB_PATH"

# --- параллельный запуск --------------------------------------------------

LOCK_DIR=$BACKUP_DIR/.backup.lock
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -d "$LOCK_DIR" ] && find "$LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null | grep -q .; then
    # Замок древнее 30 минут — значит прошлый бэкап умер, не убрав его.
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "пропуск: другой бэкап уже идёт (замок $LOCK_DIR)"
    say "бэкап уже идёт — этот запуск пропущен"
    exit 0
  fi
fi

STAMP=$(date '+%Y-%m-%d-%H')
DEST=$BACKUP_DIR/$STAMP.db
TMP=$BACKUP_DIR/.$STAMP.tmp.$$
cleanup() { rm -f "$TMP" "$TMP-wal" "$TMP-shm" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# --- копия ----------------------------------------------------------------

# ".backup 'файл'" — онлайн-бэкап SQLite: берёт согласованный снимок базы,
# не мешая записи. Кавычки внутри dot-команды допускаются.
sqlite3 "$DB_PATH" ".backup '$TMP'" >/dev/null 2>&1 \
  || die "sqlite3 не смог снять копию с $DB_PATH"

[ -s "$TMP" ] || die "копия получилась пустой: $TMP"

# Сворачиваем WAL в файл и выключаем WAL у копии: бэкап должен быть одним
# самодостаточным файлом, без -wal/-shm рядом.
sqlite3 "$TMP" "PRAGMA journal_mode=DELETE;" >/dev/null 2>&1 || true

integrity=$(sqlite3 "$TMP" "PRAGMA integrity_check;" 2>/dev/null | head -n 1 || true)
if [ "$integrity" != "ok" ]; then
  die "копия не прошла PRAGMA integrity_check (${integrity:-нет ответа}), в архив не попала"
fi

tables=$(sqlite3 "$TMP" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo 0)
[ "${tables:-0}" -gt 0 ] 2>/dev/null || die "в копии нет ни одной таблицы — это не база Life OS"

chmod 600 "$TMP" 2>/dev/null || true
mv -f "$TMP" "$DEST" || die "не удалось положить копию на место: $DEST"
chmod 600 "$DEST" 2>/dev/null || true

size=$(stat -f%z "$DEST" 2>/dev/null || stat -c%s "$DEST" 2>/dev/null || echo 0)
[ "${size:-0}" -gt 0 ] 2>/dev/null || die "копия весит 0 байт: $DEST"

# --- ротация --------------------------------------------------------------

removed=0
if [ "$ROTATE" -eq 1 ]; then
  files=()
  for f in "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db; do
    [ -f "$f" ] || continue
    files+=("$(basename "$f")")
  done
  if [ "${#files[@]}" -gt 0 ]; then
    sorted=($(printf '%s\n' "${files[@]}" | sort -r))
    seen_months=" "
    index=0
    for name in "${sorted[@]}"; do
      index=$((index + 1))
      month=${name:0:7}
      if [ "$index" -le "$KEEP" ]; then
        seen_months="$seen_months$month "
        continue
      fi
      case "$seen_months" in
        # Месяц уже представлен более свежей копией — эту можно удалять.
        *" $month "*) if rm -f "$BACKUP_DIR/$name"; then removed=$((removed + 1)); fi ;;
        # Первая (самая свежая) копия месяца — оставляем как месячный архив.
        *) seen_months="$seen_months$month " ;;
      esac
    done
  fi
fi

# --- итог -----------------------------------------------------------------

human=$(awk -v b="$size" 'BEGIN { if (b >= 1048576) printf "%.1f МБ", b/1048576; else if (b >= 1024) printf "%.0f КБ", b/1024; else printf "%d Б", b }')
log "ок: $DEST ($size B, integrity=ok, tables=$tables, удалено=$removed, db=$DB_PATH)"
say "бэкап готов: $DEST · $human · целостность ok · удалено старых: $removed"
exit 0
