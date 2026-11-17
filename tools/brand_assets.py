#!/usr/bin/env python3
"""Собирает ассеты бренда Life OS из исходного PNG-логотипа.

Только стандартная библиотека: PNG читается и пишется вручную (RGBA8),
уменьшение делается через ``sips`` (macOS), поэтому скрипт не тянет Pillow.

Что делает:
* обрезает прозрачные поля вокруг лепестка;
* пишет ``logo.png`` (мастер-версия), ``icon-192/512`` и maskable-версию;
* делает ``apple-touch-icon-180.png`` с непрозрачным фоном (iOS не любит альфу);
* делает ``favicon-32.png``.

Запуск::

    python3 tools/brand_assets.py \
        --src "/path/to/logo.png" --out app/web/public
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import zlib
from pathlib import Path

__all__ = ["crop_alpha_bbox", "load_png", "save_png", "build"]

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _read_chunks(data: bytes):
    if not data.startswith(PNG_SIG):
        raise ValueError("не PNG")
    pos = len(PNG_SIG)
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        yield kind, body
        pos += 12 + length
        if kind == b"IEND":
            break


def load_png(path: str | Path) -> tuple[int, int, bytes, int]:
    """Возвращает (width, height, rgba_bytes, bit_depth). Только 8 бит."""
    raw = Path(path).read_bytes()
    width = height = 0
    bit_depth = 8
    color_type = 6
    idat = bytearray()
    for kind, body in _read_chunks(raw):
        if kind == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat.extend(body)
        elif kind == b"tRNS":
            raise ValueError("tRNS (палитра) не поддерживается")
    if bit_depth != 8:
        raise ValueError("поддерживается только 8 бит на канал, получено %d" % bit_depth)
    if color_type not in (2, 6):
        raise ValueError("поддерживается RGB/RGBA, получено color type %d" % color_type)
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    decoded = zlib.decompress(bytes(idat))
    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = decoded[pos]
        pos += 1
        line = bytearray(decoded[pos : pos + stride])
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        else:
            raise ValueError("неизвестный фильтр %d" % filter_type)
        prev = line
        base = y * width * 4
        if channels == 4:
            out[base : base + width * 4] = line
        else:
            for x in range(width):
                out[base + x * 4 : base + x * 4 + 3] = line[x * 3 : x * 3 + 3]
                out[base + x * 4 + 3] = 255
    return width, height, bytes(out), bit_depth


def save_png(path: str | Path, width: int, height: int, rgba: bytes, *, background: tuple[int, int, int] | None = None) -> None:
    """Пишет RGBA8 PNG; ``background`` композит альфу на непрозрачный фон."""
    rows = bytearray()
    prev = bytes(width * 4)
    for y in range(height):
        base = y * width * 4
        if background is None:
            line = bytes(rgba[base : base + width * 4])
        else:
            buf = bytearray()
            br, bg_, bb = background
            for x in range(width):
                r, g, b, a = rgba[base + x * 4 : base + x * 4 + 4]
                af = a / 255.0
                buf.extend(
                    (
                        int(r * af + br * (1 - af)),
                        int(g * af + bg_ * (1 - af)),
                        int(b * af + bb * (1 - af)),
                        255,
                    )
                )
            line = bytes(buf)
        # Адаптивный фильтр: минимизируем сумму |signed byte| (эвристика PNG).
        sub = bytes((line[i] - (line[i - 4] if i >= 4 else 0)) & 0xFF for i in range(len(line)))
        up = bytes((line[i] - prev[i]) & 0xFF for i in range(len(line)))
        best_type, best = 0, line
        best_cost = sum(b if b < 128 else 256 - b for b in line)
        for ftype, candidate in ((1, sub), (2, up)):
            cost = sum(b if b < 128 else 256 - b for b in candidate)
            if cost < best_cost:
                best_type, best, best_cost = ftype, candidate, cost
        rows.append(best_type)
        rows.extend(best)
        prev = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    data = PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    Path(path).write_bytes(data)


def crop_alpha_bbox(width: int, height: int, rgba: bytes, threshold: int = 6) -> tuple[int, int, int, int]:
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        base = y * width * 4
        for x in range(width):
            if rgba[base + x * 4 + 3] > threshold:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0:
        raise ValueError("изображение полностью прозрачное")
    return left, top, right + 1, bottom + 1


def crop(width: int, height: int, rgba: bytes, box: tuple[int, int, int, int]) -> tuple[int, int, bytes]:
    left, top, right, bottom = box
    w, h = right - left, bottom - top
    out = bytearray(w * h * 4)
    for y in range(h):
        src = ((top + y) * width + left) * 4
        out[y * w * 4 : (y + 1) * w * 4] = rgba[src : src + w * 4]
    return w, h, bytes(out)


def add_padding(width: int, height: int, rgba: bytes, ratio: float) -> tuple[int, int, bytes]:
    """Квадрат с прозрачными полями ``ratio`` от размера картинки."""
    side = int(max(width, height) * (1 + ratio * 2))
    out = bytearray(side * side * 4)
    ox, oy = (side - width) // 2, (side - height) // 2
    for y in range(height):
        src = y * width * 4
        dst = ((oy + y) * side + ox) * 4
        out[dst : dst + width * 4] = rgba[src : src + width * 4]
    return side, side, bytes(out)


def resize(src: Path, dst: Path, size: int) -> None:
    subprocess.run(
        ["sips", "-z", str(size), str(size), str(src), "--out", str(dst)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build(src: Path, out_dir: Path) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height, rgba, _ = load_png(src)
    box = crop_alpha_bbox(width, height, rgba)
    cw, ch, cropped = crop(width, height, rgba, box)

    master = out_dir / "logo.png"
    save_png(master, cw, ch, cropped)

    square = out_dir / "_logo-square.png"
    sw, sh, squared = add_padding(cw, ch, cropped, ratio=0.08)
    save_png(square, sw, sh, squared)

    sizes = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "favicon-32.png": 32,
    }
    for name, size in sizes.items():
        resize(square, out_dir / name, size)

    # maskable: логотип занимает ~72% площади, остальное — фон
    mask_side = 512
    inner = 368
    small = out_dir / "_logo-mask-small.png"
    resize(square, out_dir / "_logo-mask-src.png", inner)
    mw, mh, mr, _ = load_png(out_dir / "_logo-mask-src.png")
    canvas = bytearray(mask_side * mask_side * 4)
    ox, oy = (mask_side - mw) // 2, (mask_side - mh) // 2
    for y in range(mh):
        dst = ((oy + y) * mask_side + ox) * 4
        canvas[dst : dst + mw * 4] = mr[y * mw * 4 : (y + 1) * mw * 4]
    save_png(out_dir / "icon-512-maskable.png", mask_side, mask_side, bytes(canvas), background=(246, 247, 251))

    # apple-touch: непрозрачный фон, чтобы iOS не заливал чёрным
    apple = out_dir / "_logo-apple.png"
    resize(square, apple, 180)
    aw, ah, ar, _ = load_png(apple)
    save_png(out_dir / "apple-touch-icon-180.png", aw, ah, ar, background=(246, 247, 251))

    for tmp in ("_logo-square.png", "_logo-mask-small.png", "_logo-mask-src.png", "_logo-apple.png"):
        (out_dir / tmp).unlink(missing_ok=True)

    small.unlink(missing_ok=True)
    return {"source": f"{width}x{height}", "trimmed": f"{cw}x{ch}", "bbox": box, "out": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Собрать ассеты бренда Life OS из PNG-логотипа")
    parser.add_argument("--src", required=True)
    parser.add_argument("--out", default="app/web/public")
    args = parser.parse_args(argv)
    result = build(Path(args.src), Path(args.out))
    for key, value in result.items():
        print("%s: %s" % (key, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
