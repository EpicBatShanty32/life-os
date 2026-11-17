"""Obsidian-слой Life OS v3: чтение и идемпотентная запись Markdown-заметок.

Модуль обслуживает три вида заметок в vault пользователя:

* ``01 Daily/YYYY-MM-DD.md`` — дневник. Каждая запись из БД живёт в отдельном
  пункте с маркером ``<!-- lifeos:entry:ID -->``: повторная запись того же ID
  обновляет пункт на месте, а не добавляет второй. Пользовательский текст вне
  размеченных пунктов (и вне служебных разделов) при перезаписи сохраняется
  дословно.
* ``Courses/<Курс>/<NN - Урок>.md`` — урок: фронтматтер (``course``,
  ``lesson``, ``duration``, ``tags``), управляемый блок
  ``<!-- lifeos:generated:start --> … <!-- lifeos:generated:end -->``
  с конспектом, таймкодами и заметками из БД, а ниже — раздел
  «Мои заметки», который код не трогает никогда.
* ``Health/*.md`` — таблицы замеров (вес и состав тела, сон, тренировки)
  между маркерами ``<!-- lifeos:table:start -->`` / ``end``.

Границы безопасности: любой путь строится только от корня vault. Абсолютный
путь, ``..`` или симлинк, ведущий наружу, дают :class:`VaultPathError`, и на
диск ничего не пишется. По умолчанию модуль ничего не знает о живой базе
Obsidian: корень передаёт вызывающий код (обычно из ``Settings.vault_path``
через :meth:`Vault.from_settings`), а ``write=False`` делает объект
read-only — тогда никакие операции записи не выполняются.

Таблицы здоровья — витрина для чтения человеком: даты ``YYYY-MM-DD``,
числа с точкой и одним знаком после запятой. Обратный импорт из них не
предусмотрен (источник истины — SQLite).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "CATEGORY_SECTIONS",
    "CONSPECT_HEADING",
    "DEFAULT_SECTION",
    "ENTRY_MARKER",
    "GENERATED_END",
    "GENERATED_START",
    "HEALTH_SLEEP",
    "HEALTH_WEIGHT",
    "HEALTH_WORKOUTS",
    "NOTES_HEADING",
    "DailyEntry",
    "Vault",
    "VaultDateError",
    "VaultDisabledError",
    "VaultError",
    "VaultPathError",
    "format_duration",
    "parse_day",
    "slugify",
]

logger = logging.getLogger("lifeos.vault")

# ---------------------------------------------------------------------------
# Формат заметок (MASTER_SPEC.md §2.1)
# ---------------------------------------------------------------------------

ENTRY_MARKER = "lifeos:entry:"
GENERATED_START = "<!-- lifeos:generated:start -->"
GENERATED_END = "<!-- lifeos:generated:end -->"
TABLE_START = "<!-- lifeos:table:start -->"
TABLE_END = "<!-- lifeos:table:end -->"

CONSPECT_HEADING = "## Конспект"
TIMECODES_HEADING = "## Таймкоды"
DB_NOTES_HEADING = "## Заметки (Life OS)"
NOTES_HEADING = "## Мои заметки"

DEFAULT_DAILY_FOLDER = "01 Daily"
DEFAULT_COURSES_FOLDER = "Courses"
DEFAULT_HEALTH_FOLDER = "Health"
DEFAULT_TEMPLATE_PATH = "Шаблоны/Шаблон - День.md"

HEALTH_WEIGHT = "Вес и состав тела.md"
HEALTH_SLEEP = "Сон.md"
HEALTH_WORKOUTS = "Тренировки.md"

#: Категория записи (``entries.kind``) → заголовок раздела в дневной заметке.
CATEGORY_SECTIONS = {
    "food": "Еда (агент)",
    "meal": "Еда (агент)",
    "nutrition": "Еда (агент)",
    "training": "Тренировка (агент)",
    "workout": "Тренировка (агент)",
    "sport": "Тренировка (агент)",
    "tasks": "Задачи (агент)",
    "task": "Задачи (агент)",
    "todo": "Задачи (агент)",
    "goals": "Цели (агент)",
    "goal": "Цели (агент)",
    "mood": "Мысли (агент)",
    "journal": "Мысли (агент)",
    "note": "Мысли (агент)",
    "thought": "Мысли (агент)",
    "other": "Мысли (агент)",
    "sleep": "Тело (агент)",
    "water": "Тело (агент)",
    "body": "Тело (агент)",
    "weight": "Тело (агент)",
    "health": "Тело (агент)",
}

#: Раздел по умолчанию для неизвестной категории.
DEFAULT_SECTION = "Мысли (агент)"

#: Обратный разбор: заголовок раздела → категория (для :func:`parse_day`).
SECTION_KINDS = {
    "Еда (агент)": "food",
    "Тренировка (агент)": "training",
    "Задачи (агент)": "tasks",
    "Цели (агент)": "goals",
    "Мысли (агент)": "journal",
    "Тело (агент)": "body",
}

#: Метрики, из которых собирается таблица «Вес и состав тела».
WEIGHT_METRICS: tuple[tuple[str, str], ...] = (
    ("weight", "Вес, кг"),
    ("fat", "Жир, %"),
    ("muscle", "Мышцы, кг"),
    ("body_water", "Вода, %"),
)

SLEEP_METRIC = "sleep"
WORKOUT_KINDS = ("workout", "training", "sport")

#: Максимальная длина текста записи в заметке (в БД текст хранится целиком).
ENTRY_TEXT_LIMIT = 4000

_TMP_PREFIX = ".lifeos-tmp-"

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}$")
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SECTION_HEADING_RE = re.compile(r"^#{1,2}\s")
_MARKER_NAME_RE = re.compile(r"(?i)lifeos:(entry|goals|generated|table):")
_MARKER_ESCAPES = (("<!--", "&lt;!--"), ("-->", "--&gt;"))

#: Строка-пункт с маркером записи: «- 14:02 — текст <!-- lifeos:entry:12 -->».
_ENTRY_LINE_RE = re.compile(
    r"^\s*[-*]\s+"
    r"(?:(?P<time>\d{1,2}:\d{2})\s+[—-]\s+)?"
    r"(?P<text>.*?)"
    r"\s*<!--\s*lifeos:entry:(?P<id>[^<>\s]+)\s*-->\s*$"
)


class VaultError(RuntimeError):
    """Базовая ошибка vault-слоя: данные, БД или состояние диска."""


class VaultPathError(VaultError, ValueError):
    """Путь уводит за пределы vault (абсолютный путь, ``..`` или симлинк)."""


class VaultDateError(VaultError, ValueError):
    """Дата не в формате ``YYYY-MM-DD`` или не существует."""


class VaultDisabledError(VaultError):
    """Операция записи запрещена: vault выключен или открыт только для чтения."""


# ---------------------------------------------------------------------------
# Разбор Markdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyEntry:
    """Запись, вычитанная из дневной заметки.

    ``text`` — текст пункта без маркера, ``time`` — время ``HH:MM`` (пустая
    строка, если пункт добавлен без времени), ``section`` — ближайший
    заголовок ``## …`` выше пункта.
    """

    marker_id: str
    kind: str
    section: str
    time: str
    text: str

    @property
    def marker(self) -> str:
        """HTML-маркер этой записи в том виде, в каком он лежит в файле."""
        return "<!-- %s%s -->" % (ENTRY_MARKER, self.marker_id)


def parse_day(text: str) -> list[DailyEntry]:
    """Возвращает все размеченные записи из текста дневной заметки.

    Строки без маркера (пользовательский текст, заголовки, таблицы) в
    результат не попадают: :meth:`Vault.parse_daily` читает только то, что
    писала программа, и не пытается угадать смысл свободного текста.
    """
    entries: list[DailyEntry] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if _SECTION_HEADING_RE.match(stripped):
                section = stripped.lstrip("#").strip()
            continue
        match = _ENTRY_LINE_RE.match(line)
        if match is None:
            continue
        entries.append(
            DailyEntry(
                marker_id=match.group("id"),
                kind=SECTION_KINDS.get(section, ""),
                section=section,
                time=match.group("time") or "",
                text=(match.group("text") or "").strip(),
            )
        )
    return entries


def format_duration(seconds: Any) -> str:
    """Секунды → ``MM:SS`` (или ``H:MM:SS`` для уроков длиннее часа)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def slugify(value: Any, default: str = "Без названия") -> str:
    """Имя файла/каталога из произвольной строки (кириллица сохраняется)."""
    text = unicodedata.normalize("NFC", str(value or "")).replace("\u00a0", " ")
    text = _UNSAFE_NAME_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > 80:
        text = text[:80].rstrip(" .")
    if not text or text in (".", ".."):
        return default
    return text


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _is_inside(root: Path, candidate: Path) -> bool:
    """Лежит ли ``candidate`` внутри ``root`` после разрешения симлинков."""
    try:
        resolved_root = Path(root).resolve()
        resolved = Path(candidate).resolve()
    except (OSError, RuntimeError):  # pragma: no cover - экзотическая ФС
        return False
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _sanitize_marker_id(raw: Any) -> str:
    """ID для HTML-маркера: только безопасные символы."""
    token = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(raw)).strip("-.")
    return token or "0"


def _sanitize_text(text: Any) -> str:
    """Убирает из пользовательского текста подделку служебных комментариев.

    Без этого запись с текстом ``<!-- lifeos:entry:7 -->`` могла бы «занять»
    чужой маркер и скрыть настоящую запись, а незакрытый ``<!--`` ломал бы
    отображение заметки в Obsidian.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    for token, replacement in _MARKER_ESCAPES:
        text = text.replace(token, replacement)
    return _MARKER_NAME_RE.sub(lambda m: "lifeos&#58;%s&#58;" % m.group(1), text)


def _one_line(text: Any, limit: int = ENTRY_TEXT_LIMIT) -> str:
    """Схлопывает переводы строк и длинные простыни в один пункт списка."""
    value = _sanitize_text(text)
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def _cell(value: Any, default: str = "—") -> str:
    """Ячейка Markdown-таблицы: без переводов строк и без ломки колонок."""
    text = " ".join(_sanitize_text(value).split())
    if not text:
        return default
    return text.replace("|", "\\|")


def _number(value: Any) -> str:
    """Число для таблицы: один знак после точки, без хвостового ``.0``."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    text = "%.1f" % num
    return text[:-2] if text.endswith(".0") else text


def _validate_date(value: Any) -> str:
    """Строгая проверка даты ``YYYY-MM-DD`` (иначе — имя файла поедет)."""
    text = str(value or "")
    if not _DATE_RE.match(text):
        raise VaultDateError("Дата должна быть в формате YYYY-MM-DD: %r" % (value,))
    try:
        _date.fromisoformat(text)
    except ValueError as exc:
        raise VaultDateError("Несуществующая дата: %r" % (value,)) from exc
    return text


def _split_frontmatter(lines: list[str]) -> tuple[list[str], list[str]]:
    """Делит текст на строки фронтматтера (вместе с ``---``) и тело."""
    stripped = [line.strip() for line in lines]
    if not stripped or stripped[0] != "---":
        return [], lines
    for index in range(1, len(stripped)):
        if stripped[index] == "---":
            return lines[: index + 1], lines[index + 1 :]
    return [], lines


def _yaml_scalar(value: Any) -> str:
    """Минимальное YAML-значение: строка в кавычках, если иначе неоднозначно."""
    text = " ".join(str(value).split())
    if not text:
        return '""'
    if re.search(r'[:#\[\]{}",]', text):
        return '"%s"' % text.replace('"', '\\"')
    return text


def _frontmatter_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [_yaml_scalar(item) for item in value if str(item).strip()]
        if not items:
            return ["%s: []" % key]
        return ["%s:" % key] + ["  - %s" % item for item in items]
    return ["%s: %s" % (key, _yaml_scalar(value))]


def _merge_frontmatter(front: list[str], values: Mapping[str, Any]) -> list[str]:
    """Переписывает известные ключи фронтматтера, сохраняя остальные.

    Пользовательские ключи (``aliases``, ``cssclasses``, свои поля) остаются
    на месте: мы заменяем только те строки, чьи ключи перечислены в
    ``values``, а недостающие дописываем в конец блока.
    """
    if not front:
        block = ["---"]
        for key, value in values.items():
            block.extend(_frontmatter_lines(key, value))
        block.append("---")
        return block
    body = front[1:-1]
    kept: list[str] = []
    skip = False
    for line in body:
        if line[:1] not in (" ", "\t", "-"):
            key = line.split(":", 1)[0].strip().lower()
            skip = key in values
        if not skip:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    for key, value in values.items():
        kept.extend(_frontmatter_lines(key, value))
    return ["---"] + kept + ["---"]


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class Vault:
    """Заметки Life OS v3 поверх Markdown-файлов Obsidian.

    ``root`` — корень vault (обычно ``Settings.vault_path``). ``conn`` —
    необязательное соединение SQLite для методов ``sync_*``; его можно
    передавать и в каждый вызов параметром ``conn``.

    ``write=False`` переводит объект в режим только-чтение: чтение работает,
    а методы записи бросают :class:`VaultDisabledError` до любого обращения к
    диску — вызывающий код проверяет :attr:`write_enabled` заранее.
    """

    def __init__(
        self,
        root: str | Path,
        conn: sqlite3.Connection | None = None,
        *,
        daily_folder: str = DEFAULT_DAILY_FOLDER,
        courses_folder: str = DEFAULT_COURSES_FOLDER,
        health_folder: str = DEFAULT_HEALTH_FOLDER,
        template_path: str = DEFAULT_TEMPLATE_PATH,
        write: bool = True,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.root = Path(root).expanduser()
        self.conn = conn
        self.daily_folder = str(daily_folder or DEFAULT_DAILY_FOLDER)
        self.courses_folder = str(courses_folder or DEFAULT_COURSES_FOLDER)
        self.health_folder = str(health_folder or DEFAULT_HEALTH_FOLDER)
        self.template_path = str(template_path or "")
        self.write_enabled = bool(write)
        self.logger = logger_ or logger

    # -- сборка из настроек ------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any, conn: sqlite3.Connection | None = None) -> "Vault":
        """Собирает vault из ``Settings`` (путь, флаг записи, шаблон дня)."""
        path = getattr(settings, "vault_path", None)
        if path is None:
            raise VaultDisabledError(
                "Obsidian выключен: задайте obsidian.vault_path в config/config.json "
                "или переменную окружения LIFEOS_VAULT"
            )
        config = getattr(settings, "config", None) or {}
        obsidian = config.get("obsidian") if isinstance(config, dict) else None
        obsidian = obsidian if isinstance(obsidian, dict) else {}
        template = obsidian.get("template_path") or DEFAULT_TEMPLATE_PATH
        return cls(
            path,
            conn,
            daily_folder=obsidian.get("daily_folder") or DEFAULT_DAILY_FOLDER,
            courses_folder=obsidian.get("courses_folder") or DEFAULT_COURSES_FOLDER,
            health_folder=obsidian.get("health_folder") or DEFAULT_HEALTH_FOLDER,
            template_path=str(template),
            write=bool(getattr(settings, "vault_write", False)),
        )

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return "Vault(root=%r, write=%r)" % (str(self.root), self.write_enabled)

    # -- пути --------------------------------------------------------------

    def resolve(self, *parts: Any) -> Path:
        """Путь внутри vault; всё, что выводит наружу, — :class:`VaultPathError`."""
        cleaned: list[str] = []
        for part in parts:
            text = str(part)
            if not text.strip():
                continue
            cleaned.append(text)
        if not cleaned:
            raise VaultPathError("Пустой путь: нечего разрешать внутри vault")
        relative = Path(*cleaned)
        if relative.is_absolute():
            raise VaultPathError("Абсолютный путь запрещён внутри vault: %s" % relative)
        if ".." in relative.parts:
            raise VaultPathError("Переход вверх по каталогу запрещён: %s" % relative)
        target = self.root.joinpath(*relative.parts)
        if not _is_inside(self.root, target):
            raise VaultPathError("Путь выходит за пределы vault: %s" % relative)
        return target

    def daily_path(self, date: Any) -> Path:
        """``01 Daily/YYYY-MM-DD.md``."""
        return self.resolve(self.daily_folder, "%s.md" % _validate_date(date))

    def course_dir(self, course_title: Any) -> Path:
        """Каталог курса ``Courses/<Курс>``."""
        return self.resolve(self.courses_folder, slugify(course_title, "Курс"))

    def lesson_path(self, course_title: Any, lesson: Mapping[str, Any]) -> Path:
        """``Courses/<Курс>/<NN - Урок>.md`` (NN — ``lessons.idx``)."""
        idx = _as_int(_row_get(lesson, "idx"), 0)
        name = "%02d - %s.md" % (idx, slugify(_row_get(lesson, "title"), "Урок"))
        return self.resolve(self.courses_folder, slugify(course_title, "Курс"), name)

    def health_path(self, name: str) -> Path:
        """``Health/<Имя>.md``: имя проверяется на traversal так же строго."""
        return self.resolve(self.health_folder, name)

    # -- чтение ------------------------------------------------------------

    def read_text(self, path: Path) -> str:
        """Текст файла или пустая строка, если его нет."""
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise VaultError("Не удалось прочитать %s: %s" % (path, exc)) from exc

    def parse_daily(self, date: Any) -> list[DailyEntry]:
        """Все размеченные записи дневной заметки за дату."""
        return parse_day(self.read_text(self.daily_path(date)))

    def has_entry(self, date: Any, entry_id: Any) -> bool:
        """Есть ли уже в заметке пункт с таким ID (без записи на диск)."""
        marker_id = _sanitize_marker_id(entry_id)
        return _marker_regex(marker_id).search(self.read_text(self.daily_path(date))) is not None

    # -- дневник -----------------------------------------------------------

    def ensure_daily(self, date: Any) -> Path:
        """Создаёт дневную заметку из шаблона (или каркаса), если её нет."""
        path = self.daily_path(date)
        if path.is_file():
            return path
        self._require_write("создать дневную заметку")
        self._atomic_write(path, self._render_daily(str(date)))
        return path

    def write_entry(self, entry: Mapping[str, Any]) -> bool:
        """Пишет одну запись в дневную заметку; ``True`` — файл изменился.

        Идемпотентно по маркеру ``<!-- lifeos:entry:ID -->``: повторный вызов
        с тем же текстом ничего не меняет и возвращает ``False``, а с
        изменённым текстом обновляет пункт на месте.
        """
        if isinstance(entry, (str, bytes)) or entry is None:
            raise VaultError("Запись должна быть словарём или строкой БД, получено %r" % (type(entry),))
        marker_id = _sanitize_marker_id(_entry_id(entry))
        date = _entry_date(entry)
        section = self.section_for_category(_entry_kind(entry))
        bullet = self._render_bullet(entry, marker_id)

        path = self.daily_path(date)
        if not path.is_file():
            path = self.ensure_daily(date)
        content = self.read_text(path)
        updated = _upsert_block(content, section, marker_id, bullet)
        if updated == content:
            return False
        self._require_write("записать запись id=%s" % marker_id)
        self._atomic_write(path, updated)
        return True

    def write_daily(self, date: Any, entries: Iterable[Mapping[str, Any]]) -> int:
        """Пишет пачку записей за дату; возвращает число изменённых пунктов."""
        changed = 0
        for entry in entries:
            if self.write_entry(entry):
                changed += 1
        return changed

    def remove_entry(self, date: Any, entry_id: Any) -> bool:
        """Удаляет пункт с маркером (для ``prune``); ``True`` — файл изменился."""
        marker_id = _sanitize_marker_id(entry_id)
        path = self.daily_path(date)
        content = self.read_text(path)
        if not content:
            return False
        pattern = _marker_regex(marker_id)
        kept = [line for line in content.splitlines() if not pattern.search(line)]
        updated = "\n".join(kept)
        if content.endswith("\n") and not updated.endswith("\n"):
            updated += "\n"
        if updated == content:
            return False
        self._require_write("удалить запись id=%s" % marker_id)
        self._atomic_write(path, updated)
        return True

    def section_for_category(self, kind: Any) -> str:
        """Заголовок раздела для категории записи."""
        key = str(kind or "").strip().lower()
        return CATEGORY_SECTIONS.get(key, DEFAULT_SECTION)

    # -- курсы -------------------------------------------------------------

    def write_lesson(
        self,
        course: Mapping[str, Any],
        lesson: Mapping[str, Any],
        *,
        summary: Sequence[str] = (),
        timecodes: Sequence[tuple[Any, Any]] = (),
        notes: Sequence[tuple[Any, Any]] = (),
    ) -> bool:
        """Пишет Markdown-урока; ``True`` — файл изменился.

        Управляемый блок (конспект, таймкоды, заметки из БД) перерисовывается
        целиком. Пользовательский текст вокруг него — вступление между
        заголовком урока и блоком и всё ниже раздела «Мои заметки» —
        сохраняется дословно.
        """
        self._require_write("записать урок")
        course_title = str(_row_get(course, "title") or _row_get(course, "course") or "Курс")
        path = self.lesson_path(course_title, lesson)
        existing = self.read_text(path)
        front, body = _split_frontmatter(existing.splitlines())

        lesson_idx = _as_int(_row_get(lesson, "idx"), 0)
        lesson_title = str(_row_get(lesson, "title") or "Урок")
        tags = ["курс"]
        category = str(_row_get(course, "category") or "").strip()
        if category:
            tags.append(category)
        front = _merge_frontmatter(
            front,
            {
                "course": course_title,
                "lesson": lesson_idx,
                "title": lesson_title,
                "duration": format_duration(_row_get(lesson, "duration_s")),
                "tags": tags,
            },
        )

        preamble = _lesson_preamble(body)
        tail = _lesson_tail(body)
        if not any(line.strip() == NOTES_HEADING for line in tail):
            if tail:
                tail.append("")
            tail.extend([NOTES_HEADING, ""])

        generated = _render_generated(summary, timecodes, notes)
        heading = "# %02d — %s" % (lesson_idx, lesson_title)
        parts = ["\n".join(front), "", heading]
        if preamble:
            parts.extend(["", "\n".join(preamble)])
        parts.extend(["", generated])
        if tail:
            parts.extend(["", "\n".join(tail)])
        text = "\n".join(parts).rstrip("\n") + "\n"
        if text == existing:
            return False
        self._atomic_write(path, text)
        return True

    # -- здоровье ----------------------------------------------------------

    def write_health_table(
        self,
        name: str,
        headers: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        title: str | None = None,
    ) -> bool:
        """Перерисовывает таблицу замеров в ``Health/<name>.md``.

        Меняется только блок между ``<!-- lifeos:table:start -->`` и
        ``<!-- lifeos:table:end -->``; остальной текст файла сохраняется.
        """
        self._require_write("записать таблицу здоровья")
        headers = [str(header) for header in headers]
        if not headers:
            raise VaultError("У таблицы здоровья должен быть хотя бы один столбец")
        lines = [
            "| %s |" % " | ".join(_cell(header) for header in headers),
            "| %s |" % " | ".join("---" for _ in headers),
        ]
        for row in rows:
            cells = [_cell(value) for value in row]
            cells = (cells + ["—"] * len(headers))[: len(headers)]
            lines.append("| %s |" % " | ".join(cells))
        block = [TABLE_START] + lines + [TABLE_END]

        path = self.health_path(name)
        existing = self.read_text(path)
        if existing.strip():
            updated = _replace_block(existing, TABLE_START, TABLE_END, block)
        else:
            heading = "# %s" % (title or Path(name).stem)
            updated = "\n".join([heading, "", *block]) + "\n"
        if updated == existing:
            return False
        self._atomic_write(path, updated)
        return True

    # -- синхронизация из БД ----------------------------------------------

    def sync_daily(self, date: Any, *, conn: sqlite3.Connection | None = None, prune: bool = False) -> int:
        """Переносит записи за дату из БД в дневную заметку.

        Возвращает число изменённых пунктов: повторный вызов на неизменной
        БД отдаёт ``0`` и не трогает файлы. ``prune=True`` дополнительно
        удаляет размеченные пункты, которых в БД больше нет (по умолчанию
        выключено — ручные файлы не чистим без явной просьбы). На vault
        только для чтения бросает :class:`VaultDisabledError`.
        """
        self._require_write("синхронизировать дневник")
        date_str = _validate_date(date)
        rows = self._fetch_all(
            conn,
            "SELECT id, ts, kind, text FROM entries WHERE date = ? ORDER BY ts, id",
            (date_str,),
        )
        changed = 0
        for row in rows:
            if self.write_entry(_row_to_entry(row, date_str)):
                changed += 1
        if prune:
            keep = {_sanitize_marker_id(_row_get(row, "id")) for row in rows}
            for entry in self.parse_daily(date_str):
                if entry.marker_id not in keep and self.remove_entry(date_str, entry.marker_id):
                    changed += 1
        return changed

    def sync_course(self, course_id: Any, *, conn: sqlite3.Connection | None = None) -> int:
        """Пишет/обновляет все уроки курса; возвращает число изменённых файлов.

        На vault только для чтения бросает :class:`VaultDisabledError`;
        несуществующий курс — :class:`VaultError`.
        """
        self._require_write("синхронизировать курс")
        course = self._fetch_one(
            conn,
            "SELECT id, title, category FROM courses WHERE id = ?",
            (_as_int(course_id, 0),),
        )
        if course is None:
            raise VaultError("Курс id=%r не найден" % (course_id,))
        lessons = self._fetch_all(
            conn,
            "SELECT id, idx, title, duration_s FROM lessons WHERE course_id = ? ORDER BY idx, id",
            (_as_int(_row_get(course, "id"), 0),),
        )
        changed = 0
        for lesson in lessons:
            lesson_id = _as_int(_row_get(lesson, "id"), 0)
            segments = self._fetch_all(
                conn,
                "SELECT start_s, end_s, text FROM segments WHERE lesson_id = ? ORDER BY start_s, id",
                (lesson_id,),
            )
            notes = self._fetch_all(
                conn,
                "SELECT ts, text FROM notes WHERE lesson_id = ? ORDER BY ts, id",
                (lesson_id,),
            )
            transcript = self._fetch_one(
                conn,
                "SELECT text FROM transcripts WHERE lesson_id = ? ORDER BY lang",
                (lesson_id,),
            )
            theses = self._lesson_theses(_row_get(transcript, "text") if transcript else "", segments)
            timecodes = [
                (_row_get(segment, "start_s"), _row_get(segment, "text")) for segment in segments
            ]
            note_rows = [(_row_get(note, "ts"), _row_get(note, "text")) for note in notes]
            if self.write_lesson(course, lesson, summary=theses, timecodes=timecodes, notes=note_rows):
                changed += 1
        return changed

    def sync_health(
        self,
        *,
        days: int = 120,
        conn: sqlite3.Connection | None = None,
        today: Any = None,
    ) -> int:
        """Обновляет три файла здоровья по последним ``days`` дням.

        ``Вес и состав тела`` — из ``metrics`` (weight/fat/muscle/body_water),
        ``Сон`` — из ``metrics.kind='sleep'``, ``Тренировки`` — из записей
        журнала с видом workout/training/sport. На vault только для чтения
        бросает :class:`VaultDisabledError`.
        """
        self._require_write("синхронизировать файлы здоровья")
        frame_days = max(1, _as_int(days, 120))
        last = _validate_date(today) if today else _today()
        first = _shift_date(last, -frame_days + 1)

        metrics = self._fetch_all(
            conn,
            "SELECT date, kind, value FROM metrics "
            "WHERE date >= ? AND date <= ? AND kind IN (%s) ORDER BY date"
            % ", ".join("?" for _ in WEIGHT_METRICS + ((SLEEP_METRIC, "Сон, ч"),)),
            (first, last) + tuple(kind for kind, _ in WEIGHT_METRICS) + (SLEEP_METRIC,),
        )
        by_date: dict[str, dict[str, float]] = {}
        sleep: dict[str, float] = {}
        for row in metrics:
            date_str = str(_row_get(row, "date"))
            kind = str(_row_get(row, "kind"))
            value = _row_get(row, "value")
            if kind == SLEEP_METRIC:
                sleep[date_str] = _as_float(value)
            else:
                by_date.setdefault(date_str, {})[kind] = _as_float(value)

        weight_rows = [
            [date_str] + [_number(values.get(kind, "")) for kind, _ in WEIGHT_METRICS]
            for date_str, values in sorted(by_date.items())
        ]
        sleep_rows = [[date_str, _number(hours)] for date_str, hours in sorted(sleep.items())]

        workouts = self._fetch_all(
            conn,
            "SELECT ts, kind, text FROM entries "
            "WHERE date >= ? AND date <= ? AND kind IN (%s) ORDER BY ts, id"
            % ", ".join("?" for _ in WORKOUT_KINDS if _),
            (first, last) + tuple(kind for kind in WORKOUT_KINDS if kind),
        )
        workout_rows = [
            [str(_row_get(row, "ts"))[:10], _one_line(_row_get(row, "text"), 200)] for row in workouts
        ]

        changed = 0
        if self.write_health_table(
            HEALTH_WEIGHT,
            ["Дата"] + [header for _, header in WEIGHT_METRICS],
            weight_rows,
            title="Вес и состав тела",
        ):
            changed += 1
        if self.write_health_table(HEALTH_SLEEP, ["Дата", "Сон, ч"], sleep_rows, title="Сон"):
            changed += 1
        if self.write_health_table(
            HEALTH_WORKOUTS, ["Дата", "Тренировка"], workout_rows, title="Тренировки"
        ):
            changed += 1
        return changed

    # -- внутреннее --------------------------------------------------------

    def _render_daily(self, date: str) -> str:
        """Каркас дня: из шаблона vault, если он есть, иначе встроенный."""
        template = ""
        if self.template_path:
            try:
                template = self.read_text(self.resolve(self.template_path))
            except VaultPathError:
                logger.warning("Шаблон дня вне vault — использую каркас: %s", self.template_path)
                template = ""
        if not template.strip():
            template = _DAILY_SKELETON
        return _substitute_placeholders(template, date)

    def _render_bullet(self, entry: Any, marker_id: str) -> str:
        moment = _entry_time_label(entry)
        text = _one_line(_entry_text(entry)) or "(пустая запись)"
        prefix = "%s — " % moment if moment else ""
        return "- %s%s <!-- %s%s -->" % (prefix, text, ENTRY_MARKER, marker_id)

    @staticmethod
    def _lesson_theses(transcript: Any, segments: Sequence[Any], limit: int = 5) -> list[str]:
        """Тезисы конспекта: первые фразы расшифровки (или фрагментов)."""
        source = str(transcript or "").strip()
        if source:
            pieces = _sentences(source)
        else:
            pieces = []
            for segment in segments:
                pieces.extend(_sentences(str(_row_get(segment, "text") or "")))
        unique: list[str] = []
        for piece in pieces:
            text = _one_line(piece, 220)
            if text and text not in unique:
                unique.append(text)
            if len(unique) >= limit:
                break
        return unique

    def _require_write(self, action: str) -> None:
        if not self.write_enabled:
            raise VaultDisabledError(
                "Vault открыт только для чтения — нельзя %s (obsidian.write / LIFEOS_VAULT_WRITE)"
                % action
            )

    def _atomic_write(self, path: Path, text: str) -> None:
        """Пишет файл через временный файл в том же каталоге (атомарно)."""
        if not self.root.is_dir():
            raise VaultError(
                "Корень vault не найден: %s — запись отменена (проверьте obsidian.vault_path)"
                % self.root
            )
        if not _is_inside(self.root, path):
            raise VaultPathError("Запись вне vault запрещена: %s" % path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / ("%s%d%s.tmp" % (_TMP_PREFIX, os.getpid(), path.name))
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            raise VaultError("Не удалось записать %s: %s" % (path, exc)) from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - гонка с другим процессом
                    pass
        self.logger.debug("Записан %s", path)

    def _connection(self, conn: sqlite3.Connection | None) -> sqlite3.Connection:
        connection = conn if conn is not None else self.conn
        if connection is None:
            raise VaultError("Нужно соединение с БД: передайте conn в Vault или в метод")
        return connection

    def _fetch_all(
        self, conn: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()
    ) -> list[Any]:
        return self._connection(conn).execute(sql, params).fetchall()

    def _fetch_one(
        self, conn: sqlite3.Connection | None, sql: str, params: tuple[Any, ...] = ()
    ) -> Any:
        return self._connection(conn).execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# Формат: каркас дня и подстановки
# ---------------------------------------------------------------------------

_DAILY_SKELETON = """---
tags:
  - дневник
---
# {{title}}

## Главное

## Заметки
"""


def _substitute_placeholders(text: str, date_str: str) -> str:
    """Подставляет ``{{date}}``, ``{{title}}`` и ``{{date:FMT}}`` в шаблон."""
    year, month, day = date_str.split("-")
    display = "%s.%s.%s" % (day, month, year)

    def replace(match: re.Match) -> str:
        name = match.group(1) or ""
        fmt = match.group(2) or ""
        if name == "title":
            return date_str
        if not fmt:
            return date_str
        return (
            fmt.replace("YYYY", year).replace("YY", year[2:]).replace("MM", month).replace("DD", day)
        )

    return re.sub(r"\{\{\s*(date|title)?\s*(?::([^}]*))?\}\}", replace, text)


def _today() -> str:
    return _date.today().isoformat()


def _shift_date(date_str: str, days: int) -> str:
    return (_date.fromisoformat(date_str) + timedelta(days=days)).isoformat()


def _sentences(text: str) -> list[str]:
    """Делит текст на фразы по точке/восклицанию/вопросу и переводу строки."""
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text)
    return [part.strip(" -•\t") for part in parts if part.strip()]


# ---------------------------------------------------------------------------
# Формат: пункты дневника
# ---------------------------------------------------------------------------


def _marker_regex(marker_id: str) -> "re.Pattern[str]":
    return re.compile(r"<!--\s*%s%s\s*-->" % (re.escape(ENTRY_MARKER), re.escape(marker_id)))


def _upsert_block(content: str, section: str, marker_id: str, bullet: str) -> str:
    """Ставит пункт в раздел: обновляет существующий или добавляет новый.

    Возвращает новый текст файла. Строки, не относящиеся к нашему пункту,
    остаются на месте — пользовательские правки не затираются.
    """
    trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    pattern = _marker_regex(marker_id)

    for index, line in enumerate(lines):
        if pattern.search(line):
            if line == bullet:
                return content
            lines[index] = bullet
            return _join_lines(lines, trailing_newline)

    lines = _insert_into_section(lines, section, bullet)
    return _join_lines(lines, trailing_newline)


def _join_lines(lines: list[str], trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


def _heading_line(section: str) -> str:
    """Заголовок раздела ``## …`` из названия категории."""
    text = section.strip()
    if not text:
        return "## " + DEFAULT_SECTION
    return text if text.startswith("#") else "## " + text


def _insert_into_section(lines: list[str], section: str, bullet: str) -> list[str]:
    """Дописывает пункт в конец раздела; раздела нет — создаёт его."""
    heading = _heading_line(section)
    index = next((i for i, line in enumerate(lines) if line.strip() == heading), None)

    if index is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend([heading, "", bullet])
        return lines

    end = len(lines)
    for position in range(index + 1, len(lines)):
        if _SECTION_HEADING_RE.match(lines[position]):
            end = position
            break
    insert_at = end
    while insert_at > index + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    previous = lines[insert_at - 1] if insert_at else ""
    if previous.strip() and not previous.lstrip().startswith(("- ", "* ")):
        lines.insert(insert_at, "")
        insert_at += 1
    lines.insert(insert_at, bullet)
    return lines


def _replace_block(content: str, start: str, end: str, block: Sequence[str]) -> str:
    """Заменяет блок между маркерами; маркеров нет — добавляет блок в конец."""
    trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    start_index = next((i for i, line in enumerate(lines) if line.strip() == start), None)
    end_index = None
    if start_index is not None:
        for position in range(start_index + 1, len(lines)):
            if lines[position].strip() == end:
                end_index = position
                break
    if start_index is not None and end_index is not None:
        updated = lines[:start_index] + list(block) + lines[end_index + 1 :]
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        updated = lines + list(block)
    return _join_lines(updated, trailing_newline)


# ---------------------------------------------------------------------------
# Формат: уроки
# ---------------------------------------------------------------------------


def _lesson_preamble(body: Sequence[str]) -> list[str]:
    """Пользовательский текст между заголовком урока и управляемым блоком.

    Obsidian-пользователь часто пишет пару строк сразу под ``# NN — Урок``:
    такие строки не принадлежат Life OS и при перерисовке сохраняются на
    своём месте. Если управляемого блока ещё нет, вступления тоже нет —
    весь остаток файла считает пользовательским :func:`_lesson_tail`.
    """
    lines = list(body)
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == GENERATED_START), None
    )
    if start is None:
        return []
    heading = next(
        (index for index in range(start) if lines[index].lstrip().startswith("# ")), None
    )
    preamble = lines[(heading + 1 if heading is not None else 0) : start]
    while preamble and not preamble[0].strip():
        preamble.pop(0)
    while preamble and not preamble[-1].strip():
        preamble.pop()
    return preamble


def _lesson_tail(body: Sequence[str]) -> list[str]:
    """Пользовательская часть урока: всё, что после управляемого блока."""
    lines = list(body)
    start = None
    end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == GENERATED_START and start is None:
            start = index
        elif stripped == GENERATED_END and start is not None:
            end = index
            break
    if end is not None:
        tail = lines[end + 1 :]
    else:
        tail = list(lines)
        if tail and tail[0].lstrip().startswith("# "):
            tail = tail[1:]
    while tail and not tail[0].strip():
        tail.pop(0)
    while tail and not tail[-1].strip():
        tail.pop()
    return tail


def _render_generated(
    summary: Sequence[str],
    timecodes: Sequence[tuple[Any, Any]],
    notes: Sequence[tuple[Any, Any]],
) -> str:
    """Управляемый блок урока: конспект, таймкоды, заметки из БД."""
    theses = _unique(_one_line(item, 400) for item in summary)
    codes = _unique(
        "[%s] %s" % (format_duration(start_s), _one_line(text, 400))
        for start_s, text in timecodes
    )
    rendered_notes = _unique(
        ("%s — %s" % (_note_time(ts), _one_line(text, 400))) for ts, text in notes
    )

    sections: list[list[str]] = [
        [CONSPECT_HEADING, *("- %s" % thesis for thesis in theses)],
        [TIMECODES_HEADING, *("- %s" % code for code in codes)],
    ]
    if rendered_notes:
        sections.append([DB_NOTES_HEADING, *("- %s" % note for note in rendered_notes)])

    lines = [GENERATED_START, ""]
    for index, section in enumerate(sections):
        if index:
            lines.append("")
        lines.extend(section)
    lines.extend(["", GENERATED_END])
    return "\n".join(lines)


def _note_time(ts: Any) -> str:
    text = str(ts or "").replace("T", " ")
    return text[:16] if text else "—"


def _unique(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------------------
# Преобразование записей БД
# ---------------------------------------------------------------------------


def _entry_id(entry: Any) -> Any:
    for key in ("id", "entry_id", "entryId"):
        value = _row_get(entry, key)
        if value is not None:
            return value
    raise VaultError("У записи нет id: маркер идемпотентности построить не из чего")


def _entry_date(entry: Any) -> str:
    explicit = _row_get(entry, "date")
    if explicit:
        return _validate_date(explicit)
    ts = str(_row_get(entry, "ts") or "")
    if len(ts) >= 10:
        return _validate_date(ts[:10])
    raise VaultError("У записи нет даты (date или ts): %r" % (dict(entry),))


def _entry_kind(entry: Any) -> str:
    for key in ("kind", "category", "slot"):
        value = _row_get(entry, key)
        if value:
            return str(value)
    return ""


def _entry_text(entry: Any) -> str:
    for key in ("text", "title", "raw"):
        value = _row_get(entry, key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _entry_time_label(entry: Any) -> str:
    """Время пункта: явное ``time`` или ``HH:MM`` из ``ts``.

    Пустая строка означает «времени нет»: пункт пишется без префикса, чтобы
    :func:`parse_day` вернул исходный текст записи, а не заглушку времени.
    """
    explicit = str(_row_get(entry, "time") or "").strip()
    if _TIME_RE.match(explicit):
        return explicit
    ts = str(_row_get(entry, "ts") or "")
    match = re.search(r"[T ](\d{2}:\d{2})", ts)
    return match.group(1) if match else ""


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError):  # pragma: no cover - защита от чужой строки
        return None


def _row_to_entry(row: Any, date_str: str) -> dict[str, Any]:
    return {
        "id": _row_get(row, "id"),
        "ts": _row_get(row, "ts"),
        "date": date_str,
        "kind": _row_get(row, "kind"),
        "text": _row_get(row, "text"),
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
