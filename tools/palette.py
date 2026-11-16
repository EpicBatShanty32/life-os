#!/usr/bin/env python3
"""Extract dominant colors + basic layout stats from design screenshots."""
import sys
from PIL import Image

def analyze(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    small = im.resize((w // 2, h // 2))
    q = small.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    colors = []
    for cnt, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        colors.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "share": round(cnt / total, 3)})
    # brightness map: 4x8 grid average luminance
    grid = []
    for gy in range(8):
        row = []
        for gx in range(4):
            box = im.crop((w * gx // 4, h * gy // 8, w * (gx + 1) // 4, h * (gy + 1) // 8))
            px = box.resize((1, 1)).getpixel((0, 0))
            lum = int(0.299 * px[0] + 0.587 * px[1] + 0.114 * px[2])
            row.append(lum)
        grid.append(row)
    return {"size": [w, h], "colors": colors, "luma_grid": grid}

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(p)
        print(analyze(p))
