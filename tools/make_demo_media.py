#!/usr/bin/env python3
"""Демо-медиа для уроков Life OS: короткие WAV, чтобы плеер было чем проверить.

Чистый stdlib: ``wave`` + ``math``. Файлы кладутся в ``data/media/demo`` и
прописываются в ``lessons.media_path`` вместе с ``duration_s``. Данные
демонстрационные, в git не попадают (``data/`` в .gitignore).

Запуск::

    python3 tools/make_demo_media.py --db data/lifeos-v3.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import struct
import wave
from pathlib import Path

RATE = 8000
SECONDS = 90
#: Простые аккорды, чтобы разные уроки звучали по-разному.
CHORDS = ((220.0, 277.2, 329.6), (246.9, 311.1, 370.0), (196.0, 246.9, 293.7))


def render(path: Path, chord: tuple[float, float, float], seconds: int = SECONDS) -> None:
    """Пишет мягкий аккорд с плавным появлением и затуханием."""
    frames = bytearray()
    total = RATE * seconds
    for index in range(total):
        moment = index / RATE
        envelope = min(1.0, moment / 1.2, (seconds - moment) / 2.0)
        envelope = max(0.0, envelope) ** 1.5
        value = sum(math.sin(2 * math.pi * freq * moment) for freq in chord) / len(chord)
        sample = int(max(-1.0, min(1.0, value * envelope * 0.35)) * 32767)
        frames.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(bytes(frames))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Демо-медиа для уроков Life OS")
    parser.add_argument("--db", default="data/lifeos-v3.db", help="файл БД")
    parser.add_argument("--dir", default="data/media/demo", help="каталог для WAV")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        parser.error("нет файла БД: %s" % db_path)

    target_dir = Path(args.dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path)
    try:
        lessons = connection.execute(
            "SELECT id, title FROM lessons ORDER BY course_id, idx"
        ).fetchall()
        if not lessons:
            print("В базе нет уроков — сначала запустите сидер.")
            return 1
        for position, (lesson_id, title) in enumerate(lessons):
            name = "lesson-%02d.wav" % int(lesson_id)
            path = target_dir / name
            render(path, CHORDS[position % len(CHORDS)])
            row = connection.execute(
                "SELECT duration_s FROM lessons WHERE id = ?", (int(lesson_id),)
            ).fetchone()
            old_duration = float(row[0]) if row and row[0] else 0.0
            if old_duration > 0:
                # Позиция просмотра сохраняет долю просмотра, а не секунды.
                connection.execute(
                    "UPDATE lesson_progress SET "
                    " position_s = round(min(position_s, :old) * :new / :old, 1),"
                    " watched_s = round(min(watched_s, :old) * :new / :old, 1)"
                    " WHERE lesson_id = :lesson",
                    {"old": old_duration, "new": float(SECONDS), "lesson": int(lesson_id)},
                )
            if old_duration > 0:
                # Субтитры и конспект живут в тех же долях, что и позиция плеера.
                connection.execute(
                    "UPDATE segments SET "
                    " start_s = round(min(start_s, :old) * :new / :old, 1),"
                    " end_s = round(max(min(end_s, :old) * :new / :old, 0.5), 1)"
                    " WHERE lesson_id = :lesson",
                    {"old": old_duration, "new": float(SECONDS), "lesson": int(lesson_id)},
                )
            connection.execute(
                "UPDATE lessons SET media_path = ?, duration_s = ? WHERE id = ?",
                (str(path), float(SECONDS), int(lesson_id)),
            )
            print("%-26s %-40s %d Б" % (name, str(title)[:40], path.stat().st_size))
        connection.commit()
    finally:
        connection.close()
    print("Готово: демо-медиа в %s" % target_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
