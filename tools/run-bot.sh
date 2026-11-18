#!/bin/bash
# Запуск Telegram-бота Life OS в этом окне — одной командой.
#
#   bash tools/run-bot.sh
#
# Для постоянной работы бот ставится службой: deploy/install-bot-agent.sh
# (тогда он сам поднимается после перезагрузки и держит мак бодрым).
#
# Если токена нет, бот не запустится и напишет, что именно вписать в
# config/config.json. Второй экземпляр поверх работающего не поднимется:
# его остановит замок data/bot.lock.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}
cd "$ROOT"

PYTHON=${LIFEOS_PYTHON:-}
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
  else
    printf 'ОШИБКА: не найден python3. Установите Python 3 или задайте LIFEOS_PYTHON.\n' >&2
    exit 1
  fi
fi

# Без файла config/config.json настройки по умолчанию смотрят на data/lifeos.db,
# а живая база v3 — data/lifeos-v3.db. Чтобы случайно не открыть пустую базу,
# подставляем её явно (ровно как в docs и в службе).
if [ -z "${LIFEOS_DB_PATH:-}" ] && [ ! -f "$ROOT/config/config.json" ] && [ -f "$ROOT/data/lifeos-v3.db" ]; then
  export LIFEOS_DB_PATH="$ROOT/data/lifeos-v3.db"
fi

export LIFEOS_ROOT="${LIFEOS_ROOT:-$ROOT}"

# Отметка «меня запустили вручную»: по ней служба com.lifeos.bot при старте
# поймёт, что мешает именно ручной экземпляр, и аккуратно заменит его собой
# (иначе было бы 409 Conflict и «моргание» бота).
mkdir -p "$ROOT/data"
printf '%s' "$$" > "$ROOT/data/bot-manual.pid"

exec "$PYTHON" -m app.bot.main "$@"
