"""Тесты Obsidian-слоя Life OS v3 (app/server/vault.py).

Проверяется: идемпотентная запись дневника с маркерами ``<!-- lifeos:entry:ID -->``,
разбор дневной заметки, сохранение пользовательских секций при перезаписи,
уроки курсов (фронтматтер, конспект, таймкоды, «Мои заметки»), таблицы
здоровья (вес, сон, тренировки), синхронизация из БД и защита от записи за
пределы vault (``..``, абсолютный путь, симлинк наружу).

Все тесты работают во временных каталогах (``tmp_path`` = tempfile): реальный
vault Obsidian ``~/Documents/чисто лайфик обсидиан/…`` не читается и не
изменяется, рабочая ``data/lifeos.db`` тоже не затрагивается.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.server.db import add_entry, add_metric, init_db
from app.server.settings import load_settings
from app.server.vault import (
    GENERATED_END,
    GENERATED_START,
    HEALTH_SLEEP,
    HEALTH_WEIGHT,
    HEALTH_WORKOUTS,
    NOTES_HEADING,
    TABLE_END,
    TABLE_START,
    DailyEntry,
    Vault,
    VaultDateError,
    VaultDisabledError,
    VaultError,
    VaultPathError,
    format_duration,
    parse_day,
    slugify,
)

DAY = "2026-11-17"
OTHER_DAY = "2026-11-16"


# --------------------------------------------------------------------------
# Фикстуры и помощники
# --------------------------------------------------------------------------


@pytest.fixture()
def vault_root(tmp_path: Path) -> Path:
    """Корень временного vault: кириллица и пробел в имени каталога."""
    root = tmp_path / "vault Obsidian"
    root.mkdir()
    return root


@pytest.fixture()
def conn(tmp_path: Path):
    """Отдельная БД v3 во временном каталоге (реальная data/lifeos.db не тронута)."""
    connection = init_db(tmp_path / "lifeos.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def vault(vault_root: Path, conn: sqlite3.Connection) -> Vault:
    return Vault(vault_root, conn, write=True)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count(text: str, needle: str) -> int:
    return text.count(needle)


def _lines_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


def _entry(entry_id: int, kind: str, text: str, ts: str = "2026-11-17T09:15:00") -> dict:
    return {"id": entry_id, "ts": ts, "kind": kind, "text": text}


def _seed_course(
    connection: sqlite3.Connection,
    title: str = "Python с нуля",
    category: str = "Программирование",
    lessons: int = 2,
) -> tuple[int, list[int]]:
    """Курс с уроками; возвращает ``(course_id, lesson_ids)``."""
    with connection:
        cursor = connection.execute(
            "INSERT INTO courses(title, category) VALUES (?, ?)", (title, category)
        )
        course_id = int(cursor.lastrowid or 0)
        lesson_ids: list[int] = []
        for number in range(1, lessons + 1):
            cursor = connection.execute(
                "INSERT INTO lessons(course_id, idx, title, duration_s) VALUES (?, ?, ?, ?)",
                (course_id, number, "Урок %d — введение" % number, 600.0 + number),
            )
            lesson_ids.append(int(cursor.lastrowid or 0))
    return course_id, lesson_ids


def _seed_segments(
    connection: sqlite3.Connection,
    lesson_id: int,
    items: list[tuple[float, float, str]],
) -> None:
    with connection:
        for start_s, end_s, text in items:
            connection.execute(
                "INSERT INTO segments(lesson_id, start_s, end_s, text) VALUES (?, ?, ?, ?)",
                (lesson_id, start_s, end_s, text),
            )


def _write_day_template(root: Path, body: str | None = None) -> None:
    """Шаблон дня в vault — как у пользователя Obsidian (Templater-плейсхолдеры)."""
    template = root / "Шаблоны" / "Шаблон - День.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(body or "---\ntags:\n  - дневник\n---\n# {{title}}\n\n## Мой шаблон\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Безопасность путей: ничего за пределы vault
# --------------------------------------------------------------------------


def test_resolve_blocks_absolute_and_parent_traversal(vault: Vault, vault_root: Path) -> None:
    for bad in ("/etc/passwd", "../escape.md", "a/../../b.md", "..", "./../out.md"):
        with pytest.raises(VaultPathError):
            vault.resolve(bad)
    assert issubclass(VaultPathError, ValueError)
    assert not (vault_root.parent / "escape.md").exists()
    assert not (vault_root.parent / "out.md").exists()


def test_resolve_keeps_cyrillic_and_spaces_inside_root(vault: Vault, vault_root: Path) -> None:
    path = vault.resolve("01 Daily", "2026-11-17.md")
    assert path == vault_root / "01 Daily" / "2026-11-17.md"
    assert path.parent.parent == vault_root
    lesson = vault.resolve("Courses", "Python с нуля", "01 - Введение.md")
    assert lesson.parent.parent == vault_root / "Courses"


def test_resolve_rejects_symlink_escape(vault: Vault, vault_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "вне vault"
    outside.mkdir()
    (vault_root / "Health").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultPathError):
        vault.resolve("Health", HEALTH_WEIGHT)
    with pytest.raises(VaultPathError):
        vault.write_health_table(HEALTH_WEIGHT, ["Дата"], [])
    assert not (outside / HEALTH_WEIGHT).exists()
    assert list(outside.iterdir()) == []


def test_daily_path_validates_date(vault: Vault, vault_root: Path) -> None:
    assert vault.daily_path(DAY) == vault_root / "01 Daily" / ("%s.md" % DAY)
    for bad in ("17.11.2026", "2026-13-01", "2026-02-30", "", None):
        with pytest.raises(VaultDateError):
            vault.daily_path(bad)
    assert not (vault_root / "01 Daily").exists()


def test_health_path_and_lesson_path_stay_inside(vault: Vault, vault_root: Path) -> None:
    with pytest.raises(VaultPathError):
        vault.health_path("../evil.md")
    with pytest.raises(VaultPathError):
        vault.health_path("../../etc/passwd")

    lesson = {"id": 1, "idx": 3, "title": "../../Урок: с / слэшем", "duration_s": 60.0}
    path = vault.lesson_path("Python / с нуля", lesson)
    assert path.parent.parent == vault_root / "Courses"
    assert path.parent.name == "Python с нуля"
    assert path.name.endswith(".md")
    assert ".." not in path.name and "/" not in path.name
    assert path.resolve().is_relative_to(vault_root.resolve())


def test_no_default_root_into_real_vault() -> None:
    # У Vault нет корня по умолчанию: путь в живой Obsidian передаёт вызывающий
    # код (из Settings), случайно «включить» реальный vault нельзя.
    with pytest.raises(TypeError):
        Vault()  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Дневник: создание, маркеры, идемпотентность
# --------------------------------------------------------------------------


def test_ensure_daily_creates_note_once(vault: Vault, vault_root: Path) -> None:
    path = vault.ensure_daily(DAY)
    assert path == vault_root / "01 Daily" / ("%s.md" % DAY)

    text = _text(path)
    assert "# %s" % DAY in text
    assert "## Заметки" in text
    assert "lifeos:entry:" not in text

    path.write_text(text + "\nправка пользователя\n", encoding="utf-8")
    assert vault.ensure_daily(DAY) == path
    assert "правка пользователя" in _text(path)


def test_daily_note_uses_template_from_vault(vault_root: Path, conn: sqlite3.Connection) -> None:
    _write_day_template(vault_root)
    vault = Vault(vault_root, conn, write=True, template_path="Шаблоны/Шаблон - День.md")

    path = vault.ensure_daily(DAY)
    text = _text(path)
    assert "# %s" % DAY in text
    assert "## Мой шаблон" in text
    assert "{{" not in text


def test_write_entry_creates_note_with_marker(vault: Vault, vault_root: Path) -> None:
    assert vault.write_entry(_entry(7, "food", "Овсянка на завтрак")) is True

    path = vault_root / "01 Daily" / ("%s.md" % DAY)
    text = _text(path)
    assert "## Еда (агент)" in text
    assert "- 09:15 — Овсянка на завтрак <!-- lifeos:entry:7 -->" in text
    assert vault.has_entry(DAY, 7) is True
    assert vault.has_entry(DAY, 8) is False


def test_repeated_write_of_same_entry_does_not_duplicate(vault: Vault, vault_root: Path) -> None:
    entry = _entry(7, "food", "Овсянка на завтрак")
    assert vault.write_entry(entry) is True

    path = vault_root / "01 Daily" / ("%s.md" % DAY)
    before = _text(path)
    assert vault.write_entry(entry) is False
    assert _text(path) == before
    assert _count(_text(path), "<!-- lifeos:entry:7 -->") == 1


def test_changed_entry_updates_bullet_in_place(vault: Vault, vault_root: Path) -> None:
    vault.write_entry(_entry(7, "food", "Овсянка"))
    vault.write_entry(_entry(8, "food", "Творог"))
    assert vault.write_entry(_entry(7, "food", "Овсянка с ягодами")) is True

    text = _text(vault_root / "01 Daily" / ("%s.md" % DAY))
    marked = _lines_with(text, "<!-- lifeos:entry:")
    assert len([line for line in marked if "lifeos:entry:7" in line]) == 1
    assert "- 09:15 — Овсянка с ягодами <!-- lifeos:entry:7 -->" in text
    assert "- 09:15 — Овсянка <!--" not in text
    # Порядок записей сохранён: 7 остаётся выше 8.
    assert marked.index([line for line in marked if "entry:7" in line][0]) == 0


def test_entries_are_grouped_by_kind(vault: Vault, vault_root: Path) -> None:
    vault.write_entry(_entry(1, "food", "Суп"))
    vault.write_entry(_entry(2, "training", "Силовая"))
    vault.write_entry(_entry(3, "journal", "Мысль"))
    vault.write_entry(_entry(4, "неизвестная", "Что-то"))

    text = _text(vault_root / "01 Daily" / ("%s.md" % DAY))
    for heading in ("## Еда (агент)", "## Тренировка (агент)", "## Мысли (агент)"):
        assert heading in text
    unknown_entries = [entry for entry in vault.parse_daily(DAY) if entry.marker_id == "4"]
    assert unknown_entries[0].section == "Мысли (агент)"


def test_parse_daily_reads_markers(vault: Vault) -> None:
    vault.write_entry(_entry(7, "food", "Овсянка"))
    vault.write_entry(_entry(8, "training", "Силовая", ts="2026-11-17T18:40:00"))

    entries = vault.parse_daily(DAY)
    assert [entry.marker_id for entry in entries] == ["7", "8"]
    first = entries[0]
    assert isinstance(first, DailyEntry)
    assert (first.kind, first.time, first.text) == ("food", "09:15", "Овсянка")
    assert first.section == "Еда (агент)"
    assert first.marker == "<!-- lifeos:entry:7 -->"
    assert vault.parse_daily(OTHER_DAY) == []


def test_parse_day_ignores_handwritten_bullets() -> None:
    text = "\n".join(
        [
            "# %s" % DAY,
            "",
            "## Мысли",
            "- 10:00 — сам записал руками",
            "- без маркера",
            "- 12:00 — размеченная <!-- lifeos:entry:12 -->",
        ]
    )
    parsed = parse_day(text)
    assert [entry.marker_id for entry in parsed] == ["12"]
    assert parsed[0].text == "размеченная"
    # Свой заголовок пользователя («## Мысли») не привязан к категории:
    # kind выводится только из служебных разделов агента.
    assert parsed[0].section == "Мысли"
    assert parsed[0].kind == ""


def test_user_sections_survive_agent_rewrites(vault: Vault, vault_root: Path) -> None:
    path = vault.ensure_daily(DAY)
    original = _text(path)
    user_block = "\n## Мой раздел\n\n- купил хлеб\n- позвонил маме\n"
    path.write_text(original + user_block, encoding="utf-8")

    vault.write_entry(_entry(1, "food", "Овсянка"))
    vault.write_entry(_entry(2, "journal", "Мысль"))
    vault.write_entry(_entry(1, "food", "Овсянка с ягодами"))

    text = _text(path)
    assert "## Мой раздел" in text
    assert "- купил хлеб" in text
    assert "- позвонил маме" in text
    # Пользовательский текст идёт до наших разделов и не переупорядочивается.
    assert text.index("## Мой раздел") < text.index("<!-- lifeos:entry:1 -->")


def test_user_text_above_markers_is_preserved(vault: Vault, vault_root: Path) -> None:
    path = vault.ensure_daily(DAY)
    path.write_text(
        _text(path).replace("## Заметки", "## Заметки\n\nВажное дело на сегодня."),
        encoding="utf-8",
    )

    vault.write_entry(_entry(3, "tasks", "Позвонить в клинику"))
    text = _text(path)
    assert "Важное дело на сегодня." in text
    assert "<!-- lifeos:entry:3 -->" in text


def test_daily_frontmatter_is_not_touched(vault: Vault, vault_root: Path) -> None:
    path = vault.ensure_daily(DAY)
    path.write_text(
        _text(path).replace("tags:\n  - дневник", "tags:\n  - дневник\naliases:\n  - День"),
        encoding="utf-8",
    )
    vault.write_entry(_entry(1, "food", "Суп"))
    assert "aliases:\n  - День" in _text(path)


def test_daily_notes_are_per_date(vault: Vault, vault_root: Path) -> None:
    vault.write_entry(_entry(1, "food", "Овсянка", ts="2026-11-17T09:15:00"))
    vault.write_entry(_entry(2, "food", "Сырники", ts="2026-11-16T09:00:00"))

    names = sorted(path.name for path in (vault_root / "01 Daily").iterdir())
    assert names == ["2026-11-16.md", "2026-11-17.md"]
    assert [entry.text for entry in vault.parse_daily(OTHER_DAY)] == ["Сырники"]
    assert [entry.text for entry in vault.parse_daily(DAY)] == ["Овсянка"]


def test_write_requires_existing_vault_root(tmp_path: Path) -> None:
    root = tmp_path / "нет такого vault"
    vault = Vault(root, write=True)
    with pytest.raises(VaultError):
        vault.ensure_daily(DAY)
    with pytest.raises(VaultError):
        vault.write_health_table(HEALTH_SLEEP, ["Дата", "Сон, ч"], [])
    assert not root.exists()


def test_entry_text_cannot_forge_service_marker(vault: Vault, vault_root: Path) -> None:
    vault.write_entry(
        _entry(10, "journal", "вставка <!-- lifeos:entry:99 --> и ещё <!-- незакрытый")
    )
    text = _text(vault_root / "01 Daily" / ("%s.md" % DAY))
    assert "<!-- lifeos:entry:99 -->" not in text
    assert vault.has_entry(DAY, 99) is False
    assert [entry.marker_id for entry in vault.parse_daily(DAY)] == ["10"]


def test_entry_without_time_round_trips(vault: Vault) -> None:
    vault.write_entry({"id": 5, "date": DAY, "kind": "journal", "text": "Запись без времени"})

    entries = vault.parse_daily(DAY)
    assert len(entries) == 1
    assert entries[0].time == ""
    assert entries[0].text == "Запись без времени"


def test_write_entry_requires_id_and_mapping(vault: Vault) -> None:
    with pytest.raises(VaultError):
        vault.write_entry({"date": DAY, "text": "без id"})
    with pytest.raises(VaultError):
        vault.write_entry("просто строка")


def test_write_daily_counts_only_changes(vault: Vault) -> None:
    entries = [
        _entry(1, "food", "Овсянка"),
        _entry(2, "food", "Суп"),
        _entry(1, "food", "Овсянка"),
    ]
    assert vault.write_daily(DAY, entries) == 2
    assert vault.write_daily(DAY, entries) == 0


def test_remove_entry_keeps_other_content(vault: Vault, vault_root: Path) -> None:
    vault.write_entry(_entry(1, "food", "Овсянка"))
    vault.write_entry(_entry(2, "food", "Суп"))
    path = vault_root / "01 Daily" / ("%s.md" % DAY)
    path.write_text(_text(path) + "\n## Мой раздел\n\nважное\n", encoding="utf-8")

    assert vault.remove_entry(DAY, 1) is True
    text = _text(path)
    assert "lifeos:entry:1" not in text
    assert "lifeos:entry:2" in text
    assert "## Мой раздел" in text
    assert vault.remove_entry(DAY, 1) is False


def test_readonly_vault_refuses_writes(vault_root: Path, conn: sqlite3.Connection) -> None:
    writable = Vault(vault_root, conn, write=True)
    writable.write_entry(_entry(1, "food", "Овсянка"))

    readonly = Vault(vault_root, conn, write=False)
    assert readonly.read_text(readonly.daily_path(DAY)) != ""
    assert len(readonly.parse_daily(DAY)) == 1

    with pytest.raises(VaultDisabledError):
        readonly.write_entry(_entry(2, "food", "Суп"))
    with pytest.raises(VaultDisabledError):
        readonly.write_entry(_entry(1, "food", "Другой текст"))
    with pytest.raises(VaultDisabledError):
        Vault(vault_root / "новый", conn, write=False).ensure_daily(DAY)
    assert _count(_text(vault_root / "01 Daily" / ("%s.md" % DAY)), "entry:2") == 0


# --------------------------------------------------------------------------
# Дневник: синхронизация из БД
# --------------------------------------------------------------------------


def _add_entry(conn: sqlite3.Connection, kind: str, text: str, ts: str) -> int:
    with conn:
        return add_entry(conn, kind, text, ts=ts)


def test_sync_daily_writes_entries_from_db(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    _add_entry(conn, "training", "Силовая", "2026-11-17T18:40:00")

    assert vault.sync_daily(DAY) == 2
    entries = vault.parse_daily(DAY)
    assert [entry.text for entry in entries] == ["Овсянка", "Силовая"]
    assert [entry.time for entry in entries] == ["09:15", "18:40"]
    assert "## Тренировка (агент)" in _text(vault_root / "01 Daily" / ("%s.md" % DAY))


def test_sync_daily_is_idempotent(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    assert vault.sync_daily(DAY) == 1

    path = vault_root / "01 Daily" / ("%s.md" % DAY)
    before = _text(path)
    mtime = path.stat().st_mtime_ns
    assert vault.sync_daily(DAY) == 0
    assert _text(path) == before
    assert path.stat().st_mtime_ns == mtime


def test_sync_daily_only_changed_entries(vault: Vault, conn: sqlite3.Connection) -> None:
    first = _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    _add_entry(conn, "food", "Суп", "2026-11-17T13:00:00")
    vault.sync_daily(DAY)

    with conn:
        conn.execute("UPDATE entries SET text = ? WHERE id = ?", ("Овсянка с ягодами", first))
    assert vault.sync_daily(DAY) == 1
    assert vault.sync_daily(DAY) == 0

    entries = vault.parse_daily(DAY)
    assert [entry.text for entry in entries] == ["Овсянка с ягодами", "Суп"]


def test_sync_daily_ignores_other_days(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    _add_entry(conn, "food", "Сырники", "2026-11-16T09:00:00")

    assert vault.sync_daily(DAY) == 1
    assert not (vault_root / "01 Daily" / ("%s.md" % OTHER_DAY)).exists()
    assert [entry.text for entry in vault.parse_daily(DAY)] == ["Овсянка"]


def test_sync_daily_prune_removes_vanished_rows(vault: Vault, conn: sqlite3.Connection) -> None:
    first = _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    _add_entry(conn, "food", "Суп", "2026-11-17T13:00:00")
    vault.sync_daily(DAY)
    # Маркер, которого в БД нет: ручная вставка помеченного пункта.
    path = vault.daily_path(DAY)
    path.write_text(_text(path) + "- 10:00 — ручной <!-- lifeos:entry:777 -->\n", encoding="utf-8")

    with conn:
        conn.execute("DELETE FROM entries WHERE id = ?", (first,))
    assert vault.sync_daily(DAY) == 0  # без prune ничего не удаляем
    assert vault.has_entry(DAY, first) is True

    # prune=True снимает оба пункта без строк в БД: удалённую запись id=1
    # и ручной маркер 777; запись id=2 остаётся.
    assert vault.sync_daily(DAY, prune=True) == 2
    assert vault.has_entry(DAY, 777) is False
    assert vault.has_entry(DAY, first) is False
    assert [entry.text for entry in vault.parse_daily(DAY)] == ["Суп"]


def test_sync_daily_keeps_user_sections(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    vault.sync_daily(DAY)
    path = vault_root / "01 Daily" / ("%s.md" % DAY)
    path.write_text(_text(path) + "\n## Итоги дня\n\nСпокойный день.\n", encoding="utf-8")

    _add_entry(conn, "journal", "Мысль", "2026-11-17T20:00:00")
    assert vault.sync_daily(DAY) == 1
    text = _text(path)
    assert "## Итоги дня" in text and "Спокойный день." in text
    assert "<!-- lifeos:entry:" in text


def test_sync_daily_can_use_conn_argument(vault_root: Path, conn: sqlite3.Connection) -> None:
    _add_entry(conn, "food", "Овсянка", "2026-11-17T09:15:00")
    vault = Vault(vault_root, write=True)
    assert vault.sync_daily(DAY, conn=conn) == 1
    assert [entry.text for entry in vault.parse_daily(DAY)] == ["Овсянка"]


def test_sync_without_connection_raises(vault_root: Path) -> None:
    vault = Vault(vault_root, write=True)
    with pytest.raises(VaultError):
        vault.sync_daily(DAY)
    with pytest.raises(VaultError):
        vault.sync_course(1)


# --------------------------------------------------------------------------
# Курсы: фронтматтер, конспект, таймкоды, «Мои заметки»
# --------------------------------------------------------------------------


def test_sync_course_creates_lesson_notes(
    vault: Vault, vault_root: Path, conn: sqlite3.Connection
) -> None:
    course_id, lesson_ids = _seed_course(conn, lessons=2)
    _seed_segments(
        conn,
        lesson_ids[0],
        [(0.0, 12.0, "Первая мысль."), (75.0, 90.0, "Вторая мысль.")],
    )

    assert vault.sync_course(course_id) == 2

    first = vault_root / "Courses" / "Python с нуля" / "01 - Урок 1 — введение.md"
    second = vault_root / "Courses" / "Python с нуля" / "02 - Урок 2 — введение.md"
    assert first.is_file() and second.is_file()

    text = _text(first)
    assert text.startswith("---\n")
    assert "course: Python с нуля" in text
    assert "lesson: 1" in text
    assert "duration:" in text
    assert "tags:\n  - курс\n  - Программирование" in text
    assert "# 01 — Урок 1 — введение" in text
    assert "## Конспект" in text
    assert "## Таймкоды" in text
    assert "- [00:00] Первая мысль." in text
    assert "- [01:15] Вторая мысль." in text
    assert NOTES_HEADING in text
    assert text.index(GENERATED_START) < text.index(GENERATED_END) < text.index(NOTES_HEADING)
    assert "\n\n" + GENERATED_END in text


def test_sync_course_is_idempotent(vault: Vault, conn: sqlite3.Connection) -> None:
    course_id, _ = _seed_course(conn, lessons=1)
    assert vault.sync_course(course_id) == 1

    path = vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})
    before = _text(path)
    mtime = path.stat().st_mtime_ns
    assert vault.sync_course(course_id) == 0
    assert _text(path) == before
    assert path.stat().st_mtime_ns == mtime


def test_lesson_user_notes_are_never_overwritten(vault: Vault, conn: sqlite3.Connection) -> None:
    course_id, lesson_ids = _seed_course(conn, lessons=1)
    vault.sync_course(course_id)
    path = vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})

    text = _text(path).replace(NOTES_HEADING, NOTES_HEADING + "\n\nМоё важное наблюдение.")
    path.write_text(text, encoding="utf-8")

    _seed_segments(conn, lesson_ids[0], [(120.0, 130.0, "Третья мысль.")])
    assert vault.sync_course(course_id) == 1

    updated = _text(path)
    assert "Моё важное наблюдение." in updated
    assert "- [02:00] Третья мысль." in updated
    assert _count(updated, GENERATED_START) == 1


def test_lesson_user_preamble_survives_resync(vault: Vault, conn: sqlite3.Connection) -> None:
    course_id, lesson_ids = _seed_course(conn, lessons=1)
    vault.sync_course(course_id)
    path = vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})

    heading = "# 01 — Урок 1 — введение"
    text = _text(path).replace(
        heading, heading + "\n\nЗачем мне этот урок: хочу автоматизировать отчёты.", 1
    )
    path.write_text(text, encoding="utf-8")

    _seed_segments(conn, lesson_ids[0], [(30.0, 40.0, "Новая мысль.")])
    assert vault.sync_course(course_id) == 1

    updated = _text(path)
    assert "Зачем мне этот урок: хочу автоматизировать отчёты." in updated
    assert updated.index("Зачем мне этот урок") < updated.index(GENERATED_START)
    assert "- [00:30] Новая мысль." in updated
    assert updated.index(GENERATED_START) < updated.index(NOTES_HEADING)


def test_manually_created_lesson_file_gets_generated_block(
    vault: Vault, conn: sqlite3.Connection
) -> None:
    course_id, _ = _seed_course(conn, lessons=1)
    path = vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})
    path.parent.mkdir(parents=True)
    path.write_text("# 01 — Урок 1 — введение\n\nМои мысли до синка.\n", encoding="utf-8")

    assert vault.sync_course(course_id) == 1
    text = _text(path)
    assert "Мои мысли до синка." in text
    assert "course: Python с нуля" in text
    assert GENERATED_START in text and NOTES_HEADING in text

    # Второй проход не должен «переставлять» пользовательский текст.
    assert vault.sync_course(course_id) == 0
    assert _text(path) == text


def test_lesson_frontmatter_keeps_user_keys(vault: Vault, conn: sqlite3.Connection) -> None:
    course_id, _ = _seed_course(conn, lessons=1)
    vault.sync_course(course_id)
    path = vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})

    text = _text(path).replace("course:", "cssclasses:\n  - курс\ncourse:", 1)
    path.write_text(text, encoding="utf-8")

    with conn:
        conn.execute(
            "UPDATE lessons SET duration_s = 3725 WHERE course_id = ? AND idx = 1", (course_id,)
        )
    assert vault.sync_course(course_id) == 1

    updated = _text(path)
    assert "cssclasses:\n  - курс" in updated
    assert "duration:" in updated
    assert _count(updated, "duration:") == 1


def test_lesson_notes_and_transcript_theses(
    vault: Vault, vault_root: Path, conn: sqlite3.Connection
) -> None:
    course_id, lesson_ids = _seed_course(conn, lessons=1)
    with conn:
        conn.execute(
            "INSERT INTO transcripts(lesson_id, lang, text) VALUES (?, 'ru', ?)",
            (lesson_ids[0], "Первое предложение. Второе предложение! Третье?"),
        )
        conn.execute(
            "INSERT INTO notes(lesson_id, ts, text) VALUES (?, ?, ?)",
            (lesson_ids[0], "2026-11-17T10:00:00", "Заметка из БД"),
        )
    assert vault.sync_course(course_id) == 1

    text = _text(vault_root / "Courses" / "Python с нуля" / "01 - Урок 1 — введение.md")
    assert "- Первое предложение." in text
    assert "- Второе предложение!" in text
    assert "- 2026-11-17 10:00 — Заметка из БД" in text


def test_lesson_timecodes_unique_and_ordered(vault: Vault, conn: sqlite3.Connection) -> None:
    course_id, lesson_ids = _seed_course(conn, lessons=1)
    _seed_segments(
        conn,
        lesson_ids[0],
        [(0.0, 5.0, "Повтор."), (90.0, 95.0, "Повтор."), (3600.0, 3610.0, "Час.")],
    )
    vault.sync_course(course_id)

    text = _text(
        vault.lesson_path("Python с нуля", {"idx": 1, "title": "Урок 1 — введение"})
    )
    assert _count(text, "- [00:00] Повтор.") == 1
    assert "- [01:30] Повтор." in text
    assert "- [1:00:00] Час." in text


def test_lesson_path_index_is_two_digits(vault: Vault) -> None:
    path = vault.lesson_path("Курс", {"id": 1, "idx": 7, "title": "Тема"})
    assert path.name == "07 - Тема.md"
    path = vault.lesson_path("Курс", {"id": 1, "idx": 12, "title": "Тема: с двоеточием"})
    assert path.name == "12 - Тема с двоеточием.md"


def test_sync_course_missing_course_raises(vault: Vault) -> None:
    with pytest.raises(VaultError):
        vault.sync_course(999)


def test_sync_course_readonly_raises(vault_root: Path, conn: sqlite3.Connection) -> None:
    course_id, _ = _seed_course(conn, lessons=1)
    readonly = Vault(vault_root, conn, write=False)
    with pytest.raises(VaultDisabledError):
        readonly.sync_course(course_id)
    assert not (vault_root / "Courses").exists()


# --------------------------------------------------------------------------
# Здоровье: таблицы замеров
# --------------------------------------------------------------------------


def _seed_health(conn: sqlite3.Connection) -> None:
    with conn:
        add_metric(conn, "weight", 80.4, date="2026-11-16")
        add_metric(conn, "fat", 21.3, date="2026-11-16")
        add_metric(conn, "weight", 80.0, date="2026-11-15")
        add_metric(conn, "sleep", 7.2, date="2026-11-15")
        add_metric(conn, "sleep", 6.5, date="2026-11-16")
        add_entry(conn, "workout", "Силовая: присед, жим", ts="2026-11-16T18:00:00")


def test_sync_health_creates_three_tables(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _seed_health(conn)
    assert vault.sync_health(today=DAY) == 3

    health = vault_root / "Health"
    for name in (HEALTH_WEIGHT, HEALTH_SLEEP, HEALTH_WORKOUTS):
        assert (health / name).is_file(), name

    weight = _text(health / HEALTH_WEIGHT)
    assert "# Вес и состав тела" in weight
    assert "| Дата | Вес, кг | Жир, % | Мышцы, кг | Вода, % |" in weight
    assert "| 2026-11-15 | 80 | — | — | — |" in weight
    assert "| 2026-11-16 | 80.4 | 21.3 | — | — |" in weight
    assert weight.index("2026-11-15") < weight.index("2026-11-16")

    sleep = _text(health / HEALTH_SLEEP)
    assert "| Дата | Сон, ч |" in sleep
    assert "| 2026-11-15 | 7.2 |" in sleep

    workouts = _text(health / HEALTH_WORKOUTS)
    assert "| Дата | Тренировка |" in workouts
    assert "| 2026-11-16 | Силовая: присед, жим |" in workouts


def test_health_tables_use_window(vault: Vault, conn: sqlite3.Connection) -> None:
    with conn:
        add_metric(conn, "weight", 81.0, date="2026-01-01")
        add_metric(conn, "weight", 80.4, date="2026-11-16")
    vault.sync_health(today=DAY, days=7)

    text = _text(vault.health_path(HEALTH_WEIGHT))
    assert "2026-11-16" in text
    assert "2026-01-01" not in text


def test_sync_health_is_idempotent(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _seed_health(conn)
    vault.sync_health(today=DAY)

    paths = [vault_root / "Health" / name for name in (HEALTH_WEIGHT, HEALTH_SLEEP, HEALTH_WORKOUTS)]
    before = [_text(path) for path in paths]
    mtimes = [path.stat().st_mtime_ns for path in paths]

    assert vault.sync_health(today=DAY) == 0
    assert [_text(path) for path in paths] == before
    assert [path.stat().st_mtime_ns for path in paths] == mtimes


def test_health_table_keeps_user_text(vault: Vault, vault_root: Path, conn: sqlite3.Connection) -> None:
    _seed_health(conn)
    vault.sync_health(today=DAY)
    path = vault_root / "Health" / HEALTH_WEIGHT

    text = _text(path)
    text = text.replace("# Вес и состав тела", "# Вес и состав тела\n\nЦель: 79 кг к январю.")
    text += "\n## Наблюдения\n\n- Больше белка.\n"
    path.write_text(text, encoding="utf-8")

    with conn:
        add_metric(conn, "muscle", 37.1, date="2026-11-17")
    assert vault.sync_health(today=DAY) == 1

    updated = _text(path)
    assert "Цель: 79 кг к январю." in updated
    assert "- Больше белка." in updated
    assert "| 2026-11-17 | — | — | 37.1 | — |" in updated
    assert _count(updated, TABLE_START) == 1
    assert _count(updated, TABLE_END) == 1


def test_health_table_appends_to_existing_note_without_markers(vault: Vault) -> None:
    path = vault.health_path(HEALTH_SLEEP)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Сон\n\nМой ручной текст до таблицы.\n", encoding="utf-8")

    assert vault.write_health_table(HEALTH_SLEEP, ["Дата", "Сон, ч"], [["2026-11-16", 7.2]]) is True
    text = _text(path)
    assert "Мой ручной текст до таблицы." in text
    assert TABLE_START in text and TABLE_END in text
    assert "| 2026-11-16 | 7.2 |" in text

    # Повторная перерисовка меняет блок, а не добавляет вторую таблицу.
    assert vault.write_health_table(HEALTH_SLEEP, ["Дата", "Сон, ч"], [["2026-11-16", 6.8]]) is True
    updated = _text(path)
    assert "Мой ручной текст до таблицы." in updated
    assert _count(updated, TABLE_START) == 1
    assert "6.8" in updated and "7.2" not in updated


def test_health_table_without_markers_is_created(vault: Vault) -> None:
    assert vault.write_health_table(HEALTH_WORKOUTS, ["Дата", "Тренировка"], []) is True
    text = _text(vault.health_path(HEALTH_WORKOUTS))
    assert text.startswith("# Тренировки\n")
    assert TABLE_START in text and TABLE_END in text


def test_health_cells_are_sanitized(vault: Vault) -> None:
    assert (
        vault.write_health_table(
            HEALTH_WORKOUTS,
            ["Дата", "Тренировка"],
            [["2026-11-16", "жим | присед\nвторая строка"]],
        )
        is True
    )
    text = _text(vault.health_path(HEALTH_WORKOUTS))
    assert "жим \\| присед вторая строка" in text


def test_sync_health_readonly_raises(vault_root: Path, conn: sqlite3.Connection) -> None:
    _seed_health(conn)
    readonly = Vault(vault_root, conn, write=False)
    with pytest.raises(VaultDisabledError):
        readonly.sync_health(today=DAY)
    assert not (vault_root / "Health").exists()


def test_write_health_table_requires_headers(vault: Vault) -> None:
    with pytest.raises(VaultError):
        vault.write_health_table(HEALTH_SLEEP, [], [])


# --------------------------------------------------------------------------
# Настройки: путь, режим записи, каталоги
# --------------------------------------------------------------------------


def _settings(root: Path, obsidian: dict | None = None):
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    payload = {"obsidian": obsidian if obsidian is not None else {}}
    (config / "config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return load_settings(env={}, root=root)


def test_from_settings_disabled_vault_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"vault_path": ""})
    with pytest.raises(VaultDisabledError):
        Vault.from_settings(settings)


def test_from_settings_builds_readonly_vault(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    settings = _settings(tmp_path, {"vault_path": str(vault_dir), "enabled": False})

    vault = Vault.from_settings(settings)
    assert vault.root == vault_dir
    assert vault.write_enabled is False
    with pytest.raises(VaultDisabledError):
        vault.ensure_daily(DAY)
    assert not (vault_dir / "01 Daily").exists()


def test_from_settings_uses_configured_folders(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Шаблоны").mkdir()
    (vault_dir / "Шаблоны" / "День.md").write_text("# {{title}}\n\n## Своё\n", encoding="utf-8")
    settings = _settings(
        tmp_path,
        {
            "vault_path": str(vault_dir),
            "enabled": True,
            "daily_folder": "Дневник",
            "courses_folder": "Учёба",
            "health_folder": "Здоровье",
            "template_path": "Шаблоны/День.md",
        },
    )

    vault = Vault.from_settings(settings)
    assert vault.write_enabled is True
    path = vault.ensure_daily(DAY)
    assert path == vault_dir / "Дневник" / ("%s.md" % DAY)
    assert "## Своё" in _text(path)
    assert vault.course_dir("Курс") == vault_dir / "Учёба" / "Курс"
    assert vault.health_path(HEALTH_SLEEP) == vault_dir / "Здоровье" / HEALTH_SLEEP


# --------------------------------------------------------------------------
# Вспомогательные форматы
# --------------------------------------------------------------------------


def test_format_duration() -> None:
    assert format_duration(0) == "00:00"
    assert format_duration(59.6) == "01:00"
    assert format_duration(754) == "12:34"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(-5) == "00:00"
    assert format_duration(None) == "00:00"
    assert format_duration("abc") == "00:00"


def test_slugify_keeps_cyrillic_and_removes_forbidden() -> None:
    assert slugify("Python с нуля") == "Python с нуля"
    assert slugify("Курс: часть 1/2") == "Курс часть 1 2"
    assert slugify("  ..  ") == "Без названия"
    assert slugify(None) == "Без названия"
    assert slugify("x" * 200).__len__() <= 80
    assert "/" not in slugify("../../etc/passwd")


def test_tempfile_directory_is_isolated() -> None:
    # Явная проверка контракта «тесты только на tempfile»: всё, что пишет
    # Vault, живёт внутри временного каталога и исчезает вместе с ним.
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "vault"
        root.mkdir()
        connection = init_db(base / "lifeos.db")
        try:
            add_entry(connection, "food", "Овсянка", ts="2026-11-17T09:15:00")
            connection.commit()
            vault = Vault(root, connection, write=True)
            assert vault.sync_daily(DAY) == 1
            assert (root / "01 Daily" / ("%s.md" % DAY)).is_file()
            names = {path.name for path in base.iterdir()}
            assert "vault" in names
            assert {name for name in names if name.startswith("lifeos.db")} == {
                "lifeos.db",
                "lifeos.db-shm",
                "lifeos.db-wal",
            }
            assert sorted(path.name for path in root.iterdir()) == ["01 Daily"]
        finally:
            connection.close()
        assert base.exists()
    assert not Path(directory).exists()


def test_vault_works_with_pathlib_and_str_roots(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    assert Vault(str(root)).root == root
    assert os.fspath(Vault(root).root) == str(root)
