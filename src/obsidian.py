"""Life OS: интеграция с Obsidian (дневник, цели, зеркало данных).

Контракт: INTERFACES.md §7 (класс ``Vault``) и §13 (формат блоков), ТЗ §3.5.

Главные инварианты
------------------
* **Текст пользователя не теряется.** Дневная заметка только дополняется:
  существующие строки не меняются, новые пункты вставляются в конец нужного
  раздела (или в конец файла). Цели перегенерируются исключительно между
  служебными маркерами ``<!-- lifeos:goals:start -->`` /
  ``<!-- lifeos:goals:end -->``; всё, что вне маркеров, сохраняется.
* **Идемпотентность.** Каждая запись помечается
  ``<!-- lifeos:entry:ID -->``; если маркер уже есть — повторный
  :meth:`Vault.write_entry` возвращает ``False`` и ничего не пишет.
  Разделы «## Еда (агент)» и т.п. создаются один раз.
* **Безопасность вызовов.** Всё, что связано с диском, обёрнуто в
  ``try/except``: ошибки логируются в ``logging.getLogger("lifeos.vault")``,
  а наружу возвращается ``False``/``0``. Исключения ``TypeError`` /
  ``ProgrammerError`` бросаются только при явном нарушении контракта
  (например, ``entry`` — не ``dict``).
* **Атомарная запись.** Файлы переписываются через временный файл в том же
  каталоге + ``os.replace``, поэтому Obsidian никогда не видит наполовину
  записанный файл, а сбой посреди записи не портит заметку.

Кодировка — UTF-8, переводы строк — ``\n``, только стандартная библиотека.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # zoneinfo — stdlib с Python 3.9, но на всякий случай не падаем
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - экзотическая сборка Python
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger("lifeos.vault")

__all__ = [
    "Vault",
    "ProgrammerError",
    "CATEGORY_SECTIONS",
    "GOALS_START",
    "GOALS_END",
]

# ---------------------------------------------------------------------------
# Константы формата (INTERFACES.md §7, §13)
# ---------------------------------------------------------------------------

GOALS_START = "<!-- lifeos:goals:start -->"
GOALS_END = "<!-- lifeos:goals:end -->"

#: Категория записи → заголовок раздела в дневной заметке.
CATEGORY_SECTIONS = {
    "food": "Еда (агент)",
    "training": "Тренировка (агент)",
    "tasks": "Задачи (агент)",
    "goals": "Цели (агент)",
    "mood": "Мысли (агент)",
    "journal": "Мысли (агент)",
    "other": "Мысли (агент)",
    "sleep": "Тело (агент)",
    "water": "Тело (агент)",
}

#: Раздел по умолчанию для неизвестной/пустой категории.
DEFAULT_SECTION = "Мысли (агент)"

#: Статус цели (значения из БД) → русское название для таблицы.
STATUS_RU = {
    "active": "активна",
    "активна": "активна",
    "in_progress": "активна",
    "в работе": "активна",
    "pause": "пауза",
    "paused": "пауза",
    "on_hold": "пауза",
    "onhold": "пауза",
    "пауза": "пауза",
    "done": "готово",
    "complete": "готово",
    "completed": "готово",
    "готово": "готово",
    "failed": "провал",
    "fail": "провал",
    "dropped": "провал",
    "aborted": "провал",
    "провал": "провал",
}

DEFAULT_DAILY_FOLDER = "01 Daily"
DEFAULT_TEMPLATE_PATH = "Шаблоны/Шаблон - День.md"
DEFAULT_GOALS_PATH = "Проекты/Life OS/Цели.md"
DEFAULT_DATA_FOLDER = "Life OS/data"

#: Каркас дневной заметки, если шаблон не найден/не читается.
_SKELETON = (
    "---\n"
    "tags:\n"
    "  - дневник\n"
    "---\n"
    "# {display_date}\n"
    "\n"
    "## Главное\n"
    "\n"
    "## Сделал\n"
    "\n"
    "## Смотрел и читал\n"
    "\n"
    "## Мысли\n"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_H2_RE = re.compile(r"^##(?!#)(?:\s|$)")
#: Плейсхолдеры Obsidian-шаблонов: ``{{date}}``, ``{{date:DD.MM.YYYY}}``,
#: ``{{title}}``, ``{{time}}``, ``{{time:HH:mm}}``.
_PLACEHOLDER_RE = re.compile(
    r"\{\{(date|time|title)(?::([^{}]*))?\}\}", re.IGNORECASE | re.UNICODE
)
#: Токены moment.js, которые понимает Obsidian в ``{{date:...}}``.
_MOMENT_RE = re.compile(r"dddd|ddd|YYYY|YY|MMMM|MMM|MM|M|DD|D|HH|H|mm|ss")

_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября",
    12: "декабря",
}
_WEEKDAYS_RU = {
    0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг",
    4: "пятница", 5: "суббота", 6: "воскресенье",
}

_MIRROR_ENTRIES_NAME = "entries.jsonl"
_MIRROR_GOALS_NAME = "goals.json"
_TMP_PREFIX = ".lifeos-tmp-"
_TMP_SUFFIX = ".tmp"

#: Максимальная длина текста записи в дневной заметке и в JSONL-зеркале.
#: В БД текст хранится целиком (его использует LLM); в Obsidian 250-килобайтные
#: строки ломают таблицы/поиск и раздувают файл, поэтому режем до 4000 символов.
ENTRY_TEXT_LIMIT = 4000

#: Служебные HTML-комментарии, которые пользователь не должен уметь подделать.
#: Экранируем «<»/«>» и двоеточия в имени маркера: строка «<!-- lifeos:entry:2 -->»
#: в тексте записи больше не выглядит для _marker_regex как настоящий маркер.
_MARKER_ESCAPES = (
    ("<!--", "&lt;!--"),
    ("-->", "--&gt;"),
)
_MARKER_NAME_RE = re.compile(r"(?i)lifeos:(entry|goals):")


class ProgrammerError(TypeError):
    """Явное нарушение контракта вызова (неверный тип/формат аргументов).

    Наследуется от :class:`TypeError`, чтобы вызывающий код мог ловить одним
    ``except TypeError``. Все прочие проблемы (диск, кодировка, данные)
    наружу не бросаются — логируются и превращаются в ``False``/``0``.
    """


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _as_bool(value, default: bool) -> bool:
    """Мягкое приведение конфиг-значения к bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "да", "вкл"):
            return True
        if text in ("0", "false", "no", "off", "нет", "выкл", ""):
            return False
    return default


def _moment_format(fmt: str, moment: datetime) -> str:
    """Форматирует дату по подмножеству формата moment.js (как Obsidian).

    Поддержаны токены ``YYYY``, ``YY``, ``MMMM``, ``MMM``, ``MM``, ``M``,
    ``DD``, ``D``, ``dddd``, ``ddd``, ``HH``, ``H``, ``mm``, ``ss``.
    Неизвестные символы остаются как есть.
    """

    def repl(match: re.Match) -> str:
        token = match.group(0)
        if token == "YYYY":
            return "%04d" % moment.year
        if token == "YY":
            return "%02d" % (moment.year % 100)
        if token == "MMMM":
            return _MONTHS_RU.get(moment.month, "")
        if token == "MMM":
            return _MONTHS_RU.get(moment.month, "")[:3]
        if token == "MM":
            return "%02d" % moment.month
        if token == "M":
            return str(moment.month)
        if token == "DD":
            return "%02d" % moment.day
        if token == "D":
            return str(moment.day)
        if token == "dddd":
            return _WEEKDAYS_RU.get(moment.weekday(), "")
        if token == "ddd":
            return _WEEKDAYS_RU.get(moment.weekday(), "")[:2]
        if token == "HH":
            return "%02d" % moment.hour
        if token == "H":
            return str(moment.hour)
        if token == "mm":
            return "%02d" % moment.minute
        if token == "ss":
            return "%02d" % moment.second
        return token

    try:
        return _MOMENT_RE.sub(repl, fmt)
    except Exception:  # pragma: no cover - защита от экзотических форматов
        return fmt


def _parse_ts(value) -> Optional[datetime]:
    """Разбирает ISO-строку времени; ``None``, если не получилось."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    # Терпимость к «почти ISO»: 2026-11-16 14:02 / 2026-11-16T14:02
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        for candidate in (text, text[:19]):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _number_from(value) -> Optional[float]:
    """Число из значения цели («60000», «60 000», «60,5», «200к») или None.

    Единицы измерения/суффикс отбрасываются: для процента прогресса важно
    отношение current/target, а одинаковый суффикс на него не влияет.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\u00a0", " ").replace(" ", "").replace("_", "")
    if not text:
        return None
    text = text.replace(",", ".")
    match = re.match(
        r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:[%¥₽€$]|[а-яёa-z.]+)?$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _clean_cell(value, default: str = "—") -> str:
    """Безопасная ячейка markdown-таблицы: без переводов строк, «|» и маркеров."""
    if value is None:
        return default
    if isinstance(value, bool):
        return "да" if value else "нет"
    text = " ".join(str(value).split())
    if not text:
        return default
    text = _sanitize_user_text(text)
    return text.replace("|", "\\|")


def _sanitize_marker_id(raw) -> str:
    """ID для HTML-маркера: только безопасные символы."""
    token = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(raw)).strip("-.")
    return token or "0"


def _sanitize_user_text(text: str) -> str:
    """Экранирует служебные HTML-комментарии в пользовательском тексте.

    Нужно, чтобы текст записи/цели не мог подделать маркер идемпотентности
    (``<!-- lifeos:entry:N -->``) и подавить чужую запись, а также не создавал
    незакрытые HTML-комментарии в заметке.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    for token, replacement in _MARKER_ESCAPES:
        text = text.replace(token, replacement)
    return _MARKER_NAME_RE.sub(
        lambda match: "lifeos&#58;%s&#58;" % match.group(1), text
    )


def _truncate_text(text: str, limit: int = ENTRY_TEXT_LIMIT) -> str:
    """Обрезает длинный текст до ``limit`` символов, добавляя «…»."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class Vault:
    """Работа с живой Obsidian-базой: дневник, цели, зеркало JSONL/JSON.

    Параметры
    ---------
    cfg:
        Полный конфиг приложения (``ТЗ.md`` §6). Используется секция
        ``cfg["obsidian"]``: ``enabled``, ``vault_path``, ``daily_folder``,
        ``template_path``, ``goals_path``, ``data_folder``, ``write_mirror``.
        ``cfg["_root"]`` — база для относительного ``vault_path``.
    storage:
        Необязательный объект ``Storage`` (duck-typing): нужны методы
        ``goals()``, ``entries_unwritten()``, ``mark_written(ids)``.
    """

    # ------------------------------------------------------------------ init
    def __init__(self, cfg: dict, storage=None) -> None:
        if not isinstance(cfg, dict):
            raise TypeError("Vault: cfg должен быть dict")
        obsidian = cfg.get("obsidian") or {}
        if not isinstance(obsidian, dict):
            raise TypeError("Vault: cfg['obsidian'] должен быть dict")

        self.cfg = cfg
        self.storage = storage
        self._root = self._resolve_root(cfg)
        self.vault_path: Optional[Path] = self._init_vault_path(obsidian)
        self.daily_folder = self._str_setting(obsidian, "daily_folder", DEFAULT_DAILY_FOLDER)
        self.template_path = self._str_setting(obsidian, "template_path", DEFAULT_TEMPLATE_PATH)
        self.goals_path = self._str_setting(obsidian, "goals_path", DEFAULT_GOALS_PATH)
        self.data_folder = self._str_setting(obsidian, "data_folder", DEFAULT_DATA_FOLDER)
        self.write_mirror_enabled = _as_bool(obsidian.get("write_mirror"), True)

        # Корень vault — существующая папка. Не создаём его сами: опечатка в
        # obsidian.vault_path иначе приведёт к «фантомной» базе, куда молча
        # уедут все записи. Подпапки (01 Daily, Проекты/…) внутри готовой базы
        # создавать можно.
        self._vault_configured = _as_bool(obsidian.get("enabled"), True)
        self._vault_issue_logged = False
        vault_ready = self._check_vault_root(log_issue=self._vault_configured)
        self.enabled = self._vault_configured and vault_ready
        if self._vault_configured and not vault_ready and self.vault_path is not None:
            self._vault_issue_logged = True

        app = cfg.get("app") or {}
        timezone_name = app.get("timezone") if isinstance(app, dict) else None
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            timezone_name = "Europe/Moscow"
        self.timezone_name = timezone_name.strip()
        self._tz = None
        if ZoneInfo is not None:
            try:
                self._tz = ZoneInfo(self.timezone_name)
            except Exception:
                logger.warning(
                    "Неизвестный часовой пояс %r — используется системный", self.timezone_name
                )

        if self.vault_path is None:
            logger.warning(
                "obsidian.vault_path не задан — запись в Obsidian отключена"
            )

    @staticmethod
    def _resolve_root(cfg: dict) -> Path:
        raw = cfg.get("_root")
        if isinstance(raw, (str, os.PathLike)) and str(raw).strip():
            return Path(os.path.abspath(os.path.expanduser(str(raw))))
        return Path(os.path.abspath(os.getcwd()))

    def _init_vault_path(self, obsidian: dict) -> Optional[Path]:
        raw = obsidian.get("vault_path")
        if isinstance(raw, os.PathLike):
            raw = str(raw)
        if not isinstance(raw, str) or not raw.strip():
            return None
        path = Path(os.path.expanduser(raw.strip()))
        if not path.is_absolute():
            path = self._root / path
        return Path(os.path.abspath(str(path)))

    def _check_vault_root(self, log_issue: bool = False) -> bool:
        """Есть ли корень vault и является ли он папкой (ничего не создаёт)."""
        path = self.vault_path
        if path is None:
            return False
        try:
            if path.is_dir():
                return True
            exists = path.exists()
        except OSError as exc:
            if log_issue:
                logger.error(
                    "Не удалось проверить папку Obsidian %s: %s — запись отключена",
                    path,
                    exc,
                )
            return False
        if log_issue:
            if exists:
                logger.error(
                    "obsidian.vault_path %s существует, но это не папка — "
                    "запись в Obsidian отключена (путь не будет создан)",
                    path,
                )
            else:
                logger.error(
                    "obsidian.vault_path %s не найден — запись в Obsidian "
                    "отключена, пока папка базы не появится. Создайте vault в "
                    "Obsidian или исправьте obsidian.vault_path в конфиге; "
                    "подпапки внутри существующей базы создаются автоматически.",
                    path,
                )
        return False

    def _require_vault(self, action: str) -> bool:
        """Проверка перед записью: корень vault существует и включён в конфиге.

        Если папка появилась после старта агента — запись включается сама,
        без перезапуска (пользователь мог открыть Obsidian позже).
        """
        if not self._vault_configured:
            logger.debug(
                "Obsidian выключен (obsidian.enabled=false) — %s пропущено", action
            )
            return False
        if self._check_vault_root(log_issue=False):
            if not self.enabled:
                logger.info(
                    "Папка Obsidian найдена (%s) — запись включена", self.vault_path
                )
                self.enabled = True
            return True
        if not self._vault_issue_logged:
            self._vault_issue_logged = True
            self._check_vault_root(log_issue=True)
        logger.debug(
            "Папка Obsidian недоступна (%s) — %s пропущено", self.vault_path, action
        )
        return False

    @staticmethod
    def _str_setting(obsidian: dict, key: str, default: str) -> str:
        value = obsidian.get(key)
        if isinstance(value, os.PathLike):
            value = str(value)
        if isinstance(value, str):
            return value.strip() or default
        if value is not None:
            logger.warning(
                "obsidian.%s должен быть строкой — используется %r", key, default
            )
        return default

    # -------------------------------------------------------------- утилиты
    def _now(self) -> datetime:
        """Текущее локальное время (Europe/Moscow по умолчанию)."""
        if self._tz is not None:
            try:
                return datetime.now(self._tz)
            except Exception:  # pragma: no cover
                pass
        return datetime.now().astimezone()

    def _vault_abs(self, value: str) -> Path:
        """Путь внутри vault (daily_folder, goals_path и т.п.) → абсолютный."""
        path = Path(os.path.expanduser(str(value)))
        if path.is_absolute():
            return Path(os.path.abspath(str(path)))
        base = self.vault_path if self.vault_path is not None else self._root
        return Path(os.path.abspath(str(base / path)))

    def _daily_path(self, date_str: str) -> Path:
        return self._vault_abs(self.daily_folder) / ("%s.md" % date_str)

    @staticmethod
    def _validate_date(value) -> str:
        """Проверяет/нормализует дату ``YYYY-MM-DD``."""
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, _date):
            return value.strftime("%Y-%m-%d")
        if not isinstance(value, str):
            raise TypeError("date должен быть строкой в формате YYYY-MM-DD")
        text = value.strip()
        if not _DATE_RE.match(text):
            raise ProgrammerError(
                "date должен быть в формате YYYY-MM-DD, получено %r" % (value,)
            )
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ProgrammerError("date содержит некорректную дату %r" % (value,)) from exc
        return text

    def daily_path(self, date: str) -> Path:
        """Абсолютный путь дневной заметки (без создания файла)."""
        return self._daily_path(self._validate_date(date))

    def section_for_category(self, category) -> str:
        """Название раздела для категории записи."""
        key = category.strip().lower() if isinstance(category, str) else ""
        return CATEGORY_SECTIONS.get(key, DEFAULT_SECTION)

    # ------------------------------------------------------------ шаблоны
    def _template_file(self) -> Path:
        return self._vault_abs(self.template_path)

    def _render_template(self, date_str: str) -> str:
        """Текст новой дневной заметки: шаблон с подстановками или каркас."""
        template_file = self._template_file()
        text = None
        try:
            text = template_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info(
                "Шаблон дневной заметки не найден (%s) — используется каркас",
                template_file,
            )
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Не удалось прочитать шаблон %s (%s) — используется каркас",
                template_file,
                exc,
            )
        display_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        if text is None:
            text = _SKELETON.format(display_date=display_date)
        return self._substitute_placeholders(text, date_str)

    def _substitute_placeholders(self, text: str, date_str: str) -> str:
        """Подставляет ``{{date}}``, ``{{date:...}}``, ``{{title}}``, ``{{time}}``."""
        try:
            moment_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:  # pragma: no cover - date уже провалидирована
            moment_date = self._now()
        moment_now = self._now()

        def repl(match: re.Match) -> str:
            kind = match.group(1).lower()
            fmt = match.group(2)
            try:
                if kind == "title":
                    # {{title}} в Obsidian — имя заметки, для daily это дата.
                    return date_str
                if kind == "date":
                    if fmt:
                        return _moment_format(fmt, moment_date)
                    return date_str
                if kind == "time":
                    if fmt:
                        return _moment_format(fmt, moment_now)
                    return moment_now.strftime("%H:%M")
            except Exception:  # pragma: no cover - подстановка не должна ронять
                logger.exception("Ошибка подстановки плейсхолдера %r", match.group(0))
                return match.group(0)
            return match.group(0)

        return _PLACEHOLDER_RE.sub(repl, text)

    # -------------------------------------------------------- дневная заметка
    def ensure_daily(self, date: str) -> Path:
        """Возвращает путь дневной заметки, создавая её при необходимости.

        Если файла нет — он создаётся из ``template_path`` (UTF-8) с
        подстановкой ``{{date:DD.MM.YYYY}}``, ``{{date}}``, ``{{title}}``;
        если шаблона нет — минимальный каркас. Существующий файл не трогается.
        """
        date_str = self._validate_date(date)
        path = self._daily_path(date_str)
        if not self._require_vault("создание дневной заметки"):
            return path
        if path.exists():
            return path
        try:
            # Подпапка внутри существующего vault — можно (корень уже проверен).
            path.parent.mkdir(parents=True, exist_ok=True)
            content = self._render_template(date_str)
            if content and not content.endswith("\n"):
                content += "\n"
            try:
                with path.open("x", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            except FileExistsError:
                # Параллельный процесс успел создать заметку — не перезаписываем.
                return path
            logger.info("Создана дневная заметка %s", path.name)
        except Exception:
            logger.exception("Не удалось создать дневную заметку %s", path)
        return path

    # --------------------------------------------------------------- записи
    def _entry_date(self, entry: dict) -> str:
        """Дата записи: поле date → из ts → сегодняшняя (локальная)."""
        raw_date = entry.get("date")
        if isinstance(raw_date, str) and _DATE_RE.match(raw_date.strip()):
            candidate = raw_date.strip()
            try:
                datetime.strptime(candidate, "%Y-%m-%d")
                return candidate
            except ValueError:
                logger.warning("Некорректная дата записи %r — беру из ts", raw_date)
        moment = _parse_ts(entry.get("ts"))
        if moment is not None:
            return moment.strftime("%Y-%m-%d")
        logger.warning("У записи нет корректной даты/ts — использую сегодняшнюю")
        return self._now().strftime("%Y-%m-%d")

    def _entry_time(self, entry: dict) -> str:
        """Время пункта ``HH:MM`` из ts (в локальном поясе) или текущее."""
        moment = _parse_ts(entry.get("ts"))
        if moment is None:
            return self._now().strftime("%H:%M")
        if moment.tzinfo is not None and self._tz is not None:
            try:
                moment = moment.astimezone(self._tz)
            except Exception:  # pragma: no cover
                pass
        return moment.strftime("%H:%M")

    def _entry_marker_id(self, entry: dict) -> str:
        """ID для маркера идемпотентности.

        Для записей из БД — ``id``. Если id нет (вызов до сохранения в БД),
        строится детерминированный маркер по содержимому, чтобы запись всё
        равно не дублировалась и не терялась.
        """
        raw_id = entry.get("id")
        if raw_id is not None:
            return _sanitize_marker_id(raw_id)
        seed = "|".join(
            str(entry.get(key) or "")
            for key in ("date", "ts", "category", "title", "text", "raw")
        )
        digest = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]
        # Это не ошибка: scheduler в проде всегда передаёт id. Держим в debug,
        # чтобы не засорять лог на каждый вызов без id.
        logger.debug("Запись без id — маркер будет noid-%s", digest)
        return "noid-%s" % digest

    @staticmethod
    def _marker_regex(marker_id: str) -> "re.Pattern[str]":
        return re.compile(r"<!--\s*lifeos:entry:%s\s*-->" % re.escape(marker_id))

    @staticmethod
    def _bullet_text(entry: dict) -> str:
        """Человекочитаемый текст пункта (одна строка, без подделки маркера)."""
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            text = entry.get("title")
        if text is None:
            text = ""
        elif not isinstance(text, str):
            text = str(text)
        normalized = " ".join(text.split())
        # Экранируем «<!--»/«-->» и имя маркера, чтобы пользовательский текст
        # не мог подавить чужую запись. Затем режем гигантские простыни.
        normalized = _sanitize_user_text(normalized)
        normalized = _truncate_text(normalized)
        return normalized or "(пустая запись)"

    def _format_bullet(self, entry: dict, marker_id: str) -> str:
        return "- %s — %s <!-- lifeos:entry:%s -->" % (
            self._entry_time(entry),
            self._bullet_text(entry),
            marker_id,
        )

    def _marker_present(self, entry: dict) -> bool:
        """Есть ли маркер записи в её дневной заметке (без записи)."""
        try:
            date_str = self._entry_date(entry)
            marker_id = self._entry_marker_id(entry)
            path = self._daily_path(date_str)
            if not path.is_file():
                return False
            content = path.read_text(encoding="utf-8")
            return bool(self._marker_regex(marker_id).search(content))
        except Exception:
            logger.exception("Не удалось проверить маркер записи")
            return False

    @staticmethod
    def _append_to_section(content: str, heading: str, bullet: str) -> str:
        """Добавляет пункт в раздел, не меняя остальной текст.

        * раздел уже есть → пункт в конец раздела (перед следующим ``## ``);
        * раздела нет → раздел создаётся в конце файла.

        Возвращает новый текст файла; исходные строки сохраняются дословно.
        """
        if not content.endswith("\n"):
            content += "\n"
        lines = content.split("\n")
        heading_norm = heading.strip()

        section_index = -1
        for index, line in enumerate(lines):
            if _H2_RE.match(line) and line.strip() == heading_norm:
                section_index = index
                break

        if section_index < 0:
            # Раздела нет — создаём его в самом конце файла. Пустая строка
            # после заголовка — как в шаблоне дневной заметки.
            body = content
            if body.strip():
                separator = "" if body.endswith("\n\n") else "\n"
                return body + separator + heading_norm + "\n\n" + bullet + "\n"
            return heading_norm + "\n\n" + bullet + "\n"

        # Ищем начало следующего заголовка второго уровня.
        next_heading = len(lines) - 1  # последний элемент — "" от финального "\n"
        for index in range(section_index + 1, len(lines)):
            if _H2_RE.match(lines[index]):
                next_heading = index
                break

        # Вставляем пункт после последней содержательной строки раздела,
        # не съедая пустые строки, которые отделяют раздел от следующего.
        insert_at = next_heading
        while insert_at > section_index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1

        # Раздел был пуст (сразу следующий «## »): отделяем пункт от
        # заголовка пустой строкой, чтобы Markdown не слипался.
        if insert_at == section_index + 1:
            lines.insert(insert_at, "")
            insert_at += 1

        lines.insert(insert_at, bullet)

        # И отделяем следующий заголовок от пункта, если пустой строки не было.
        if insert_at + 1 < len(lines) and _H2_RE.match(lines[insert_at + 1]):
            lines.insert(insert_at + 1, "")
        return "\n".join(lines)

    def write_entry(self, entry: dict) -> bool:
        """Дописывает запись в дневную заметку.

        ``entry`` — формат INTERFACES.md §12 (нужен ``id`` из БД).
        Возвращает ``True``, если пункт добавлен; ``False`` — если уже был
        (идемпотентность) или при любой ошибке записи.
        """
        if not isinstance(entry, dict):
            raise TypeError("write_entry: entry должен быть dict")
        try:
            if not self._require_vault("запись в дневник"):
                return False

            date_str = self._entry_date(entry)
            marker_id = self._entry_marker_id(entry)
            daily = self.ensure_daily(date_str)
            if daily is None or not daily.is_file():
                logger.error(
                    "Дневная заметка недоступна — запись id=%s не добавлена", marker_id
                )
                return False

            try:
                content = daily.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.error(
                    "Не удалось прочитать дневную заметку %s: %s", daily.name, exc
                )
                return False

            if self._marker_regex(marker_id).search(content):
                logger.debug(
                    "Запись id=%s уже есть в %s — пропуск", marker_id, daily.name
                )
                return False

            category = entry.get("category")
            section = "## " + self.section_for_category(category)
            bullet = self._format_bullet(entry, marker_id)
            new_content = self._append_to_section(content, section, bullet)
            if new_content == content:
                logger.warning(
                    "Запись id=%s не добавлена: файл не изменился", marker_id
                )
                return False

            self._atomic_write(daily, new_content)
            logger.info(
                "Запись id=%s добавлена в раздел «%s» (%s)",
                marker_id,
                section[3:],
                daily.name,
            )
            return True
        except Exception:
            logger.exception("Ошибка записи entry в Obsidian")
            return False

    # ----------------------------------------------------------------- цели
    def _load_goals(self) -> Optional[list]:
        """Цели из storage; ``None`` — если источник недоступен."""
        if self.storage is None:
            logger.debug("storage не передан — цели недоступны")
            return None
        getter = getattr(self.storage, "goals", None)
        if not callable(getter):
            logger.warning("У storage нет метода goals() — цели недоступны")
            return None
        try:
            goals = getter()
        except Exception:
            logger.exception("storage.goals() завершился ошибкой")
            return None
        if goals is None:
            return []
        if not isinstance(goals, (list, tuple)):
            logger.warning("storage.goals() вернул не список — цели пропущены")
            return None
        result = [goal for goal in goals if isinstance(goal, dict)]
        if len(result) != len(goals):
            logger.warning("Часть целей имеет неверный формат — пропущена")
        return result

    def _goal_title(self, goal: dict) -> str:
        raw = goal.get("title")
        title = " ".join(str(raw).split()) if raw is not None else ""
        if not title:
            return "—"
        # Маркеры goals/entry в названии цели — тоже пользовательский ввод:
        # экранируем, чтобы не сломать служебный блок [[...]].
        title = _sanitize_user_text(title)
        return "[[%s]]" % title.replace("|", "\\|")

    def _goal_progress(self, goal: dict) -> str:
        current = goal.get("current")
        target = goal.get("target")
        current_number = _number_from(current)
        target_number = _number_from(target)
        if (
            current_number is not None
            and target_number is not None
            and target_number != 0
        ):
            percent = current_number / target_number * 100.0
            rounded = round(percent)
            if abs(percent - rounded) < 0.05:
                return "%d%%" % rounded
            return "%.1f%%" % percent
        current_text = _clean_cell(current, default="")
        target_text = _clean_cell(target, default="")
        if current_text and target_text:
            return "%s / %s" % (current_text, target_text)
        return current_text or "—"

    def _goal_deadline(self, goal: dict) -> str:
        deadline = goal.get("deadline")
        if deadline is None:
            return "—"
        text = " ".join(str(deadline).split())
        if not text:
            return "—"
        match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$", text)
        if match:
            return match.group(1)
        return _clean_cell(text)

    def _goal_status(self, goal: dict) -> str:
        status = " ".join(str(goal.get("status") or "").split())
        if not status:
            return "активна"
        return STATUS_RU.get(status.strip().lower(), _clean_cell(status))

    def _render_goals_table(self, goals: list) -> str:
        lines = [
            "| Цель | Прогресс | Срок | Статус |",
            "|---|--:|---|---|",
        ]
        for goal in goals:
            # Порядок колонок: Цель | Прогресс | Срок | Статус.
            lines.append(
                "| %s | %s | %s | %s |"
                % (
                    self._goal_title(goal),
                    self._goal_progress(goal),
                    self._goal_deadline(goal),
                    self._goal_status(goal),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _replace_goals_block(content: str, table: str) -> str:
        """Заменяет содержимое между маркерами goals, сохраняя остальной текст."""
        block = "%s\n%s\n%s" % (GOALS_START, table, GOALS_END)
        start = content.find(GOALS_START)
        if start >= 0:
            end = content.find(GOALS_END, start + len(GOALS_START))
            if end >= 0:
                new_content = (
                    content[:start] + block + content[end + len(GOALS_END):]
                )
                if not new_content.endswith("\n"):
                    new_content += "\n"
                return new_content
            logger.warning(
                "В файле целей есть %s без закрывающего маркера — блок будет собран заново",
                GOALS_START,
            )
        elif GOALS_END in content:
            logger.warning(
                "В файле целей есть закрывающий маркер без открывающего — блок будет собран заново"
            )

        # Валидной пары маркеров нет: убираем осиротевшие служебные маркеры
        # (это наши комментарии, не текст пользователя) и дописываем блок
        # в конец файла, сохраняя всё остальное содержимое.
        cleaned_lines = [
            line
            for line in content.split("\n")
            if line.strip() not in (GOALS_START, GOALS_END)
        ]
        cleaned = "\n".join(cleaned_lines)
        if cleaned.strip():
            if not cleaned.endswith("\n"):
                cleaned += "\n"
            separator = "" if cleaned.endswith("\n\n") else "\n"
            return cleaned + separator + block + "\n"
        return block + "\n"

    def rewrite_goals(self) -> bool:
        """Перегенерирует таблицу целей в ``goals_path`` между маркерами.

        Файл/маркеры создаются при необходимости. Текст вне маркеров
        сохраняется. Возвращает ``False`` при ошибке или если storage
        недоступен.
        """
        try:
            if not self._require_vault("обновление целей"):
                return False
            goals = self._load_goals()
            if goals is None:
                logger.warning("Цели не обновлены: нет доступа к storage.goals()")
                return False

            path = self._vault_abs(self.goals_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.error("Не удалось прочитать файл целей %s: %s", path, exc)
                    return False
            else:
                content = "# Цели Life OS\n"

            table = self._render_goals_table(goals)
            new_content = self._replace_goals_block(content, table)
            if new_content == content and path.exists():
                logger.debug("Таблица целей не изменилась")
                return True
            self._atomic_write(path, new_content)
            logger.info(
                "Таблица целей обновлена (%d шт.) в %s", len(goals), path.name
            )
            return True
        except Exception:
            logger.exception("Не удалось обновить цели в Obsidian")
            return False

    # ---------------------------------------------------------------- зеркало
    @staticmethod
    def _mirror_entry(entry: dict) -> dict:
        """Запись для entries.jsonl: metrics всегда dict, JSON-сериализуемо.

        Длинные текстовые поля (text/title/raw) обрезаются до
        :data:`ENTRY_TEXT_LIMIT`: в БД остаётся полный текст для LLM, а
        зеркало/дневник не превращаются в 250-килобайтные строки.
        """
        data = dict(entry)
        for key in ("text", "title", "raw"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = _truncate_text(value)
        metrics = data.get("metrics")
        if isinstance(metrics, str):
            try:
                parsed = json.loads(metrics) if metrics.strip() else {}
            except (ValueError, TypeError):
                parsed = {}
            data["metrics"] = parsed if isinstance(parsed, dict) else {}
        elif metrics is None:
            data["metrics"] = {}
        elif not isinstance(metrics, dict):
            data["metrics"] = {}
        return data

    def _mirror_folder(self) -> Path:
        return self._vault_abs(self.data_folder)

    def write_mirror(self, entries: list) -> None:
        """Полностью перезаписывает machine-readable зеркало данных.

        Пишет ``<data_folder>/entries.jsonl`` и ``<data_folder>/goals.json``
        атомарно (временный файл + ``os.replace``). Если ``write_mirror``
        выключен в конфиге — ничего не делает. Ошибки только логируются.
        """
        if not isinstance(entries, (list, tuple)):
            raise TypeError("write_mirror: entries должен быть list")
        try:
            if not self._require_vault("запись зеркала"):
                return
            if not self.write_mirror_enabled:
                logger.debug("write_mirror=false — зеркало не пишется")
                return
            folder = self._mirror_folder()
            folder.mkdir(parents=True, exist_ok=True)

            try:
                lines = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    payload = self._mirror_entry(entry)
                    lines.append(
                        json.dumps(payload, ensure_ascii=False, default=str)
                    )
                text = "\n".join(lines) + ("\n" if lines else "")
                self._atomic_write(folder / _MIRROR_ENTRIES_NAME, text)
            except Exception:
                logger.exception("Не удалось записать %s", _MIRROR_ENTRIES_NAME)

            goals = self._load_goals()
            if goals is not None:
                try:
                    text = (
                        json.dumps(goals, ensure_ascii=False, indent=2, default=str)
                        + "\n"
                    )
                    self._atomic_write(folder / _MIRROR_GOALS_NAME, text)
                except Exception:
                    logger.exception("Не удалось записать %s", _MIRROR_GOALS_NAME)
        except Exception:
            logger.exception("Ошибка write_mirror")

    # ------------------------------------------------------------- синхронизация
    def sync_pending(self) -> int:
        """Дописывает в Obsidian все несохранённые записи из БД.

        ``storage.entries_unwritten()`` → :meth:`write_entry` →
        ``storage.mark_written(ids)``. Возвращает число записей, которые
        гарантированно оказались в дневной заметке. Ошибки логируются и не
        бросаются; неудачные записи остаются с ``written=0`` для повтора.
        """
        try:
            if not self._require_vault("синхронизация записей"):
                return 0
            if self.storage is None:
                logger.debug("storage не передан — синхронизация невозможна")
                return 0
            getter = getattr(self.storage, "entries_unwritten", None)
            if not callable(getter):
                logger.warning("У storage нет entries_unwritten() — синхронизация пропущена")
                return 0
            try:
                pending = getter()
            except Exception:
                logger.exception("storage.entries_unwritten() завершился ошибкой")
                return 0
            if pending is None:
                return 0
            if not isinstance(pending, (list, tuple)):
                logger.warning("entries_unwritten() вернул не список — пропуск")
                return 0

            written_count = 0
            written_ids = []
            for entry in pending:
                if not isinstance(entry, dict):
                    logger.warning("Пропущена запись неверного формата")
                    continue
                entry_id = entry.get("id")
                try:
                    added = self.write_entry(entry)
                    if not added:
                        # False — либо дубль (маркер уже в заметке), либо ошибка.
                        # Во втором случае запись оставляем для повтора.
                        if not self._marker_present(entry):
                            logger.error(
                                "Запись id=%s не попала в Obsidian — оставлена для повтора",
                                entry_id,
                            )
                            continue
                        logger.info(
                            "Запись id=%s уже была в заметке — помечаю written", entry_id
                        )
                    written_count += 1
                    if isinstance(entry_id, int) and not isinstance(entry_id, bool):
                        written_ids.append(entry_id)
                    elif isinstance(entry_id, str) and entry_id.strip().isdigit():
                        written_ids.append(int(entry_id.strip()))
                except (TypeError, ProgrammerError):
                    logger.warning("Запись id=%s пропущена из-за неверных данных", entry_id)
                except Exception:
                    logger.exception("Ошибка синхронизации записи id=%s", entry_id)

            if written_ids:
                marker = getattr(self.storage, "mark_written", None)
                if callable(marker):
                    try:
                        marker(written_ids)
                    except Exception:
                        logger.exception("storage.mark_written() завершился ошибкой")
                else:
                    logger.warning("У storage нет mark_written() — флаги не обновлены")
            return written_count
        except Exception:
            logger.exception("Ошибка sync_pending")
            return 0

    # ------------------------------------------------------------ файловые IO
    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """Атомарная запись текста: temp в том же каталоге + ``os.replace``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX, dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # Сохраняем права исходного файла (mkstemp создаёт 0600).
            try:
                if path.exists():
                    os.chmod(tmp_name, path.stat().st_mode & 0o7777)
            except OSError:  # pragma: no cover - права не критичны
                pass
            os.replace(tmp_name, str(path))
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:  # pragma: no cover
                pass
