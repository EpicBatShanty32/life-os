#!/usr/bin/env bash
# ============================================================================
# Life OS — снятие агента с автозапуска (macOS)
#
#   bash uninstall.sh           выгрузить агента и удалить LaunchAgent;
#                               данные (база, логи, конфиг) СОХРАНЯЮТСЯ
#   bash uninstall.sh --purge   то же + удалить ~/Library/Application Support/LifeOS
#                               и ~/Library/Logs/LifeOS (конфиг с токеном, база, логи)
#   bash uninstall.sh --dry-run показать действия, ничего не меняя
#   bash uninstall.sh --help    справка
#
# Совместим с /bin/bash 3.2 (macOS).
# ============================================================================
set -euo pipefail

: "${HOME:?HOME не задан — запусти скрипт из обычного терминала}"

LABEL="com.lifeos.agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

APP_DIR="${LIFEOS_APP_DIR:-$HOME/Library/Application Support/LifeOS}"
LOG_DIR="${LIFEOS_LOG_DIR:-$HOME/Library/Logs/LifeOS}"
PLIST_DIR="${LIFEOS_PLIST_DIR:-$HOME/Library/LaunchAgents}"
PLIST_DST="$PLIST_DIR/$LABEL.plist"
LOG_FILE="$LOG_DIR/agent.log"

PURGE=0
DRY_RUN=0

if [ -t 1 ]; then
  C_BOLD=$'\033[1m'
  C_RED=$'\033[31m'
  C_YELLOW=$'\033[33m'
  C_RESET=$'\033[0m'
else
  C_BOLD=""
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

on_error() {
  local code=$?
  printf '\n%s  ✖ Шаг не выполнен (код %s).%s\n\n' "$C_RED" "$code" "$C_RESET" >&2
}
trap on_error ERR

usage() {
  cat <<'EOF'
Life OS — снять агент с автозапуска.

Запуск:
  bash uninstall.sh            выгрузить агента, удалить LaunchAgent;
                               данные (конфиг, база, логи) сохраняются
  bash uninstall.sh --purge    то же + удалить данные:
                               ~/Library/Application Support/LifeOS (конфиг с токеном,
                               база SQLite) и ~/Library/Logs/LifeOS
  bash uninstall.sh --dry-run  показать действия, ничего не меняя
  bash uninstall.sh --help     эта справка

Перед удалением сделай копию конфига, если токен нужен: он лежит в
~/Library/Application Support/LifeOS/config.json
EOF
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --purge) PURGE=1 ;;
      --dry-run) DRY_RUN=1 ;;
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

# Защита от удаления не того каталога: --purge работает только с путями,
# которые заканчиваются на /LifeOS и не являются домашним каталогом.
assert_safe_purge_target() {
  local target="$1"
  case "$target" in
    "" | "/" | "$HOME" | "$HOME/")
      die "Отказ: небезопасный путь для удаления: '$target'"
      ;;
    */LifeOS)
      return 0
      ;;
    *)
      die "Отказ: '$target' не похож на каталог LifeOS — удалять не буду."
      ;;
  esac
}

wait_for_bootout() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if ! launchctl list "$LABEL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

unload_agent() {
  step "Останавливаю агента"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] launchctl bootout gui/$(id -u)/$LABEL"
    say "[dry-run] launchctl unload \"$PLIST_DST\" (запасной способ)"
    return 0
  fi

  if command -v launchctl >/dev/null 2>&1; then
    local domain
    domain="gui/$(id -u)"
    launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || true
    if ! wait_for_bootout; then
      # Старый способ для древних версий macOS.
      launchctl unload "$PLIST_DST" >/dev/null 2>&1 || true
      wait_for_bootout || warn "агент всё ещё виден в launchctl — проверь: launchctl list | grep $LABEL"
    fi
    say "агент выгружен"
  else
    warn "launchctl не найден — это не macOS? Пропускаю остановку."
  fi
}

remove_plist() {
  step "Удаляю LaunchAgent"
  if [ -f "$PLIST_DST" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      say "[dry-run] rm -f \"$PLIST_DST\""
    else
      rm -f "$PLIST_DST"
      say "удалён: $PLIST_DST"
    fi
  else
    say "файла нет: $PLIST_DST"
  fi
}

purge_data() {
  step "Удаляю данные (--purge)"
  assert_safe_purge_target "$APP_DIR"
  assert_safe_purge_target "$LOG_DIR"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "[dry-run] rm -rf \"$APP_DIR\""
    say "[dry-run] rm -rf \"$LOG_DIR\""
    return 0
  fi

  if [ -d "$APP_DIR" ]; then
    rm -rf "$APP_DIR"
    say "удалён: $APP_DIR"
  else
    say "нет каталога: $APP_DIR"
  fi

  if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    say "удалён: $LOG_DIR"
  else
    say "нет каталога: $LOG_DIR"
  fi
}

keep_data_note() {
  step "Данные сохранены"
  say "Программа и данные: $APP_DIR"
  say "Логи:               $LOG_DIR"
  say "Чтобы удалить и их — запусти: bash uninstall.sh --purge"
  if [ -f "$APP_DIR/config/config.json" ]; then
    say "Токен бота остался в: $APP_DIR/config/config.json (не удаляй его без нужды)."
  fi
}

print_result() {
  step "Готово"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "Режим --dry-run: ничего не изменено."
  elif [ "$PURGE" -eq 1 ]; then
    say "Агент снят с автозапуска, данные удалены."
  else
    say "Агент снят с автозапуска. Данные на месте — можно установить заново:"
    say "  bash \"$SCRIPT_DIR/install.sh\""
  fi
}

main() {
  parse_args "$@"

  printf '\n%sLife OS — удаление агента%s\n' "$C_BOLD" "$C_RESET"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '%s  Режим: --dry-run (ничего не меняется)%s\n' "$C_YELLOW" "$C_RESET"
  fi
  if [ "$PURGE" -eq 1 ]; then
    printf '%s  Режим: --purge (данные будут удалены)%s\n' "$C_YELLOW" "$C_RESET"
  fi

  unload_agent
  remove_plist

  if [ "$PURGE" -eq 1 ]; then
    purge_data
  else
    keep_data_note
  fi

  print_result
}

main "$@"
