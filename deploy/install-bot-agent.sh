#!/bin/bash
# Старое имя для службы бота. Вся работа — в tools/bot-service.sh.
#
#   deploy/install-bot-agent.sh             = bash tools/bot-service.sh install
#   deploy/install-bot-agent.sh --status    = bash tools/bot-service.sh status
#   deploy/install-bot-agent.sh --uninstall = bash tools/bot-service.sh uninstall
#
# Оставлено, чтобы старые ссылки в документации продолжали работать.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=${LIFEOS_ROOT:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)}

case "${1:-}" in
  --status) shift; set -- status "$@" ;;
  --uninstall) shift; set -- uninstall "$@" ;;
  --start) shift; set -- start "$@" ;;
  --stop) shift; set -- stop "$@" ;;
  "") set -- install ;;
esac

exec bash "$ROOT/tools/bot-service.sh" "$@"
