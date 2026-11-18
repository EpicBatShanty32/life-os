#!/usr/bin/env bash
# ============================================================================
# check.sh — быстрая самопроверка проекта Life OS.
#
# Запуск из папки проекта:
#   bash check.sh
#   ./check.sh
#
# Что проверяется:
#   1. python3 доступен (и какой версии);
#   2. compileall src — синтаксис всех модулей;
#   3. unit-тесты tests/ (если папка есть);
#   4. bash -n install.sh и uninstall.sh;
#   5. plutil -lint ops/com.lifeos.agent.plist;
#   6. json.tool config/config.example.json + пустые token / api_key / chat_id;
#   7. в файлах проекта нет секретов (sk-…, токены ботов);
#   8. в git-репозитории секреты и данные под .gitignore и не попали в индекс.
#
# Код выхода: 0 — всё чисто, 1 — есть ошибки.
# Совместим с /bin/bash 3.2 (macOS): без ассоциативных массивов и mapfile.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

if [ -t 1 ]; then
  C_OK=$'\033[32m'
  C_BAD=$'\033[31m'
  C_DIM=$'\033[2m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_OK=""
  C_BAD=""
  C_DIM=""
  C_BOLD=""
  C_RESET=""
fi

PASSED=0
FAILED=0
SKIPPED=0

# Временная папка для вывода проверок; удаляется на любом выходе.
TMP="$(mktemp -d "${TMPDIR:-/tmp}/lifeos-check.XXXXXX")"
OUT="$TMP/out.txt"
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# ---------------------------------------------------------------------------
# Вывод и учёт результатов
# ---------------------------------------------------------------------------
ok() {
  PASSED=$((PASSED + 1))
  printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$1"
}

bad() {
  FAILED=$((FAILED + 1))
  printf '  %s✗%s %s\n' "$C_BAD" "$C_RESET" "$1"
}

skip() {
  SKIPPED=$((SKIPPED + 1))
  printf '  %s– %s (пропущено)%s\n' "$C_DIM" "$1" "$C_RESET"
}

# run_check "описание" команда [аргументы...]
# Ошибка не прерывает прогон: печатаем хвост вывода и идём дальше.
run_check() {
  local label="$1"
  shift
  if "$@" >"$OUT" 2>&1; then
    ok "$label"
    return 0
  fi
  bad "$label"
  if [ -s "$OUT" ]; then
    tail -n 20 "$OUT" | sed 's/^/      /'
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Вспомогательные проверки (вызываются через run_check)
# ---------------------------------------------------------------------------

# В образце конфига не должно быть реальных токенов, ключей и chat_id.
check_example_config() {
  local cfg="config/config.example.json"
  if ! grep -qE '"token"[[:space:]]*:[[:space:]]*""' "$cfg"; then
    echo "telegram.token не пустой — в примере не должно быть токена"
    return 1
  fi
  if ! grep -qE '"api_key"[[:space:]]*:[[:space:]]*""' "$cfg"; then
    echo "llm.api_key не пустой — в примере не должно быть ключа"
    return 1
  fi
  if ! grep -qE '"allowed_chat_id"[[:space:]]*:[[:space:]]*0([[:space:]]|,|$)' "$cfg"; then
    echo "telegram.allowed_chat_id должен быть 0"
    return 1
  fi
  return 0
}

# Файлы проекта без личных данных, логов и кэша — то, где ищем секреты.
# node_modules сторонних пакетов не проверяем: это чужой код, а строки
# вроде «-webkit-mask-box-image» ложно срабатывают на шаблон «sk-…».
build_file_list() {
  find . \
    -path ./.git -prune -o \
    -path ./data -prune -o \
    -path ./tests/tmp -prune -o \
    -name node_modules -prune -o \
    -name __pycache__ -prune -o \
    -type f \
    ! -name '*.pyc' \
    ! -name '*.log' \
    ! -name '.DS_Store' \
    ! -path './config/config.json' \
    -print
}

# Реальные секреты: ключи sk-… и токены Telegram-ботов (8–10 цифр : 30+ символов).
# Плейсхолдеры из документации вида 1234567890:AAHxxxx… секретом не считаются.
check_no_secrets() {
  local list="$TMP/files.txt"
  local hits="$TMP/secret-hits.txt"
  local tokens="$TMP/token-hits.txt"
  local f

  build_file_list > "$list"
  : > "$hits"
  : > "$tokens"

  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # Слева должен быть не-словесный символ: иначе идентификаторы сценариев
    # с префиксом sk- в тестах ложно считаются ключом.
    grep -HnE '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}' "$f" >> "$hits" 2>/dev/null || true
  done < "$list"

  while IFS= read -r f; do
    [ -n "$f" ] || continue
    grep -HnE '[0-9]{8,10}:[A-Za-z0-9_-]{30,}' "$f" >> "$tokens" 2>/dev/null || true
  done < "$list"
  # Плейсхолдер из документации Telegram и такой же в тестах — не секрет.
  grep -vE '[Xx]{8,}|123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw' "$tokens" >> "$hits" 2>/dev/null || true

  if [ -s "$hits" ]; then
    echo "найдены секреты (файл:строка):"
    head -n 5 "$hits" | sed 's/^/  /'
    return 1
  fi
  return 0
}

# Секреты и генерируемые данные должны быть под .gitignore.
check_git_ignores() {
  local p
  local missing=0
  for p in config/config.json data/lifeos.db dashboard/index.html dashboard/data.json \
           demo/index.html demo/data.json tests/tmp/probe .DS_Store agent.log; do
    if ! git check-ignore -q -- "$p"; then
      echo "не игнорируется: $p"
      missing=1
    fi
  done
  return "$missing"
}

# В индексе git не должно быть личных данных и сгенерированных страниц.
check_git_index() {
  local bad
  bad="$(git ls-files | grep -E '^(config/config\.json|data/|dashboard/index\.html|dashboard/data\.json|demo/index\.html|demo/data\.json|tests/tmp/)' || true)"
  if [ -n "$bad" ]; then
    echo "в индексе лишние файлы:"
    printf '%s\n' "$bad" | head -n 5 | sed 's/^/  /'
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Заголовок
# ---------------------------------------------------------------------------
VERSION_STR=""
if [ -f VERSION ]; then
  VERSION_STR="$(tr -d '[:space:]' < VERSION)"
fi
[ -n "$VERSION_STR" ] || VERSION_STR="без версии"

printf '%sLife OS %s — проверка проекта%s\n' "$C_BOLD" "$VERSION_STR" "$C_RESET"
printf '%s%s%s\n' "$C_DIM" "$ROOT" "$C_RESET"

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
PY_BIN="${LIFEOS_PYTHON:-}"
if [ -z "$PY_BIN" ]; then
  PY_BIN="$(command -v python3 2>/dev/null || true)"
fi

HAVE_PY=0
if [ -n "$PY_BIN" ] && [ -x "$PY_BIN" ]; then
  HAVE_PY=1
  PY_VER="$("$PY_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
  ok "python3 ${PY_VER:-?} ($PY_BIN)"
else
  bad "python3 не найден — задай LIFEOS_PYTHON=/путь/к/python3"
fi

# ---------------------------------------------------------------------------
# 2. Синтаксис модулей
# ---------------------------------------------------------------------------
# PYTHONPYCACHEPREFIX — байткод идёт в temp, а не в src/ и не в кэш Apple
# (у /usr/bin/python3 это ~/Library/Caches/com.apple.python, в песочнице запрещён).
if [ "$HAVE_PY" -eq 1 ] && [ -d src ]; then
  run_check "compileall src" env PYTHONPYCACHEPREFIX="$TMP/pycache" "$PY_BIN" -m compileall -q src
else
  skip "compileall src — нет python3 или папки src"
fi

# ---------------------------------------------------------------------------
# 3. Тесты
# ---------------------------------------------------------------------------
if [ "$HAVE_PY" -eq 1 ] && [ -d tests ]; then
  run_check "unit-тесты (unittest discover -s tests)" "$PY_BIN" -m unittest discover -s tests
elif [ -d tests ]; then
  skip "unit-тесты — нет python3"
else
  skip "unit-тесты — нет папки tests"
fi

# ---------------------------------------------------------------------------
# 4. Синтаксис установщиков (bash 3.2)
# ---------------------------------------------------------------------------
for script in install.sh uninstall.sh; do
  if [ -f "$script" ]; then
    run_check "bash -n $script" bash -n "$script"
  else
    skip "bash -n $script — файла нет"
  fi
done

# ---------------------------------------------------------------------------
# 5. LaunchAgent plist
# ---------------------------------------------------------------------------
PLIST="ops/com.lifeos.agent.plist"
if [ ! -f "$PLIST" ]; then
  skip "plutil -lint $PLIST — файла нет"
elif ! command -v plutil >/dev/null 2>&1; then
  skip "plutil -lint $PLIST — plutil не найден (не macOS)"
else
  run_check "plutil -lint $PLIST" plutil -lint "$PLIST"
fi

# ---------------------------------------------------------------------------
# 6. Пример конфига: валидный JSON и никаких секретов внутри
# ---------------------------------------------------------------------------
CFG="config/config.example.json"
if [ ! -f "$CFG" ]; then
  skip "json.tool $CFG — файла нет"
elif [ "$HAVE_PY" -eq 0 ]; then
  skip "json.tool $CFG — нет python3"
else
  run_check "json.tool $CFG" "$PY_BIN" -m json.tool "$CFG"
  run_check "пример конфига без секретов (token/api_key/chat_id пустые)" check_example_config
fi

# ---------------------------------------------------------------------------
# 7. Секреты в файлах проекта
# ---------------------------------------------------------------------------
run_check "секретов в проекте нет (sk-…, токены ботов)" check_no_secrets

# ---------------------------------------------------------------------------
# 8. Git: игнор секретов и данных
# ---------------------------------------------------------------------------
if git rev-parse --git-dir >/dev/null 2>&1; then
  run_check ".gitignore: секреты и данные исключены" check_git_ignores
  run_check "git: секреты и данные не в индексе" check_git_index
else
  skip "git — не репозиторий (git init ещё не сделан)"
fi

# ---------------------------------------------------------------------------
# Итог
# ---------------------------------------------------------------------------
TOTAL=$((PASSED + FAILED))
printf '\n'

if [ "$FAILED" -eq 0 ]; then
  printf '%sPASS%s  %d проверок, ошибок нет' "$C_OK$C_BOLD" "$C_RESET" "$TOTAL"
  if [ "$SKIPPED" -gt 0 ]; then
    printf ' (пропущено: %d)' "$SKIPPED"
  fi
  printf '\n'
  exit 0
fi

printf '%sFAIL%s  %d из %d проверок не прошли' "$C_BAD$C_BOLD" "$C_RESET" "$FAILED" "$TOTAL"
if [ "$SKIPPED" -gt 0 ]; then
  printf ' (пропущено: %d)' "$SKIPPED"
fi
printf '\n'
exit 1
