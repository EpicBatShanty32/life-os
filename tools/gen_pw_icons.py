#!/usr/bin/env python3
"""Генератор иконок PWA для Life OS без внешних библиотек.

Рисует плоскую иконку (фиолетовый квадрат со скруглением, белая «L» и точка)
и пишет настоящие PNG через ``zlib``: Pillow не нужен, работает на стоковом
Python 3.9+. Иконки кладутся в ``app/web/public`` и попадают в сборку Vite.

Запуск::

    python3 tools/gen_pw_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "public"

#: Палитра из tokens.css — светлый Life OS.
VIOLET = (161, 139, 183)
VIOLET_DARK = (139, 116, 163)
WHITE = (255, 255, 255)
MINT = (174, 222, 194)

#: Размеры иконок: (имя, сторона, доля внутреннего отступа).
SIZES = (
    ("icon-192.png", 192, 0.0),
    ("icon-512.png", 512, 0.0),
    ("icon-512-maskable.png", 512, 0.22),
    ("apple-touch-icon-180.png", 180, 0.0),
)


def _radius(size: int) -> int:
    return int(size * 0.22)


def _inside_round_rect(x: float, y: float, size: int, radius: int) -> bool:
    if radius <= 0:
        return 0 <= x < size and 0 <= y < size
    left, right = radius, size - radius
    top, bottom = radius, size - radius
    if left <= x < right or top <= y < bottom:
        return 0 <= x < size and 0 <= y < size
    center_x = left if x < left else right
    center_y = top if y < top else bottom
    dx = x - center_x
    dy = y - center_y
    return dx * dx + dy * dy <= radius * radius


def _row_bytes(size: int, y: int, inset: int, mark_color: tuple[int, int, int]) -> bytes:
    """Пиксели одной строки: фон, скругление, «L» и точка."""
    inner = size - 2 * inset
    radius = _radius(inner)
    body = inner * 0.11  # толщина штриха «L»
    stem_x0 = inset + inner * 0.33
    stem_x1 = stem_x0 + body
    stem_y0 = inset + inner * 0.26
    stem_y1 = inset + inner * 0.74
    foot_x1 = inset + inner * 0.68
    dot_cx = inset + inner * 0.70
    dot_cy = inset + inner * 0.30
    dot_r = inner * 0.075

    data = bytearray()
    for x in range(size):
        px = x + 0.5
        py = y + 0.5
        if not _inside_round_rect(px - inset, py - inset, inner, radius):
            data.extend((0, 0, 0, 0))
            continue
        color = VIOLET if py < inset + inner * 0.55 else VIOLET_DARK
        # Вертикальная стойка «L».
        if stem_x0 <= px <= stem_x1 and stem_y0 <= py <= stem_y1:
            color = mark_color
        # Нижняя перекладина.
        elif stem_y1 - body <= py <= stem_y1 and stem_x0 <= px <= foot_x1:
            color = mark_color
        # Точка-акцент.
        else:
            dx = px - dot_cx
            dy = py - dot_cy
            if dx * dx + dy * dy <= dot_r * dot_r:
                color = MINT
        data.extend((color[0], color[1], color[2], 255))
    return bytes(data)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_png(path: Path, size: int, inset_ratio: float) -> None:
    inset = int(size * inset_ratio)
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # фильтр строки: None
        raw.extend(_row_bytes(size, y, inset, WHITE))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", header)
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, inset_ratio in SIZES:
        target = PUBLIC_DIR / name
        write_png(target, size, inset_ratio)
        print("%-28s %4d×%-4d %6d Б" % (name, size, size, target.stat().st_size))


if __name__ == "__main__":
    main()
