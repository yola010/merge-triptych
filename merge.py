#!/usr/bin/env python3
"""Deterministic image merge utility built on Pillow.

Principles:
- Order = command-line order (never re-sorted by filename).
- Never overwrite; default output is <first_stem>_merge.<ext>.
- Row layouts (two_h / three_h / two_v / long) require equal-sized images; a
  clear warning is printed and recorded in JSON when sizes differ.
- Grid layouts (four / six / nine) accept mismatched sizes and center each
  image in a uniform cell, separated by a uniform white gap (default 1 mm).
  The center cell (odd x odd grids only) is contain-fitted by default (aspect
  ratio preserved, centered on white); --center-fill stretch force-fills it.
- Output format is inferred from the inputs (all-JPEG -> JPEG q90, all-PNG ->
  PNG, all-WEBP -> WEBP, mixed -> PNG); override with --format/--quality.
- Print one human-readable line, then a compact single-line JSON summary.
"""

import argparse
import json
import os
import sys

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.stderr.write("error: Pillow is not installed in this interpreter. "
                     "Install it with: pip install Pillow\n")
    sys.exit(2)

# Optional HEIC/HEIF support (iPhone photos). Register the reader if the
# optional dependency is present; otherwise print a clear guidance instead of
# a bare decode error.
HEIF_SUPPORTED = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    pass

HEIF_EXTS = {".heic", ".heif"}
JPEG_EXTS = {".jpg", ".jpeg"}
PNG_EXTS = {".png"}
WEBP_EXTS = {".webp"}

FORMAT_EXTS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}

# preset -> (rows, cols, required_count or None for "long")
PRESETS = {
    "two_h":   (1, 2, 2),   # two-across (left/right)
    "three_h": (1, 3, 3),   # three-across (triple screen)
    "two_v":   (2, 1, 2),   # two-stacked (top/bottom)
    "long":    (None, 1, None),  # long image: N stacked vertically
    "four":    (2, 2, 4),   # 2x2 grid
    "six":     (2, 3, 6),   # 3x2 grid
    "nine":    (3, 3, 9),   # 3x3 grid
}


def parse_args():
    p = argparse.ArgumentParser(description="Deterministic image merge (Pillow).")
    p.add_argument("inputs", nargs="+", help="input image file(s), in order")
    p.add_argument("--preset", choices=sorted(PRESETS), default="three_h",
                   help="fixed merge layout (default three_h)")
    p.add_argument("--gap-mm", type=float, default=1.0,
                   help="gap between images in mm (default 1.0, white)")
    p.add_argument("--dpi", type=float, default=None,
                   help="dots per inch for mm->px (default: image metadata else 96)")
    p.add_argument("--format", dest="fmt",
                   choices=["keep", "jpeg", "png", "webp"], default="keep",
                   help="output format (default keep: inferred from inputs)")
    p.add_argument("--quality", type=int, default=90,
                   help="JPEG/WebP quality (default 90)")
    p.add_argument("--center-fill", choices=["stretch", "contain"],
                   default="contain",
                   help="center-cell handling in grids (default contain: "
                        "aspect preserved, centered)")
    p.add_argument("--verbose", action="store_true",
                   help="include per-image placement details in JSON")
    p.add_argument("--output", default=None,
                   help="output file (default <first_stem>_merge.<ext>)")
    return p.parse_args()


def classify_ext(ext):
    """Map a lowercase extension to a format class for inference."""
    if ext in JPEG_EXTS or ext in HEIF_EXTS:
        return "jpeg"
    if ext in PNG_EXTS:
        return "png"
    if ext in WEBP_EXTS:
        return "webp"
    return "other"


def infer_output(args, input_paths):
    """Return (fmt, out_ext) where fmt in {"JPEG","PNG","WEBP"}."""
    if args.fmt != "keep":
        return args.fmt.upper(), FORMAT_EXTS[args.fmt]
    classes = {classify_ext(os.path.splitext(p)[1].lower()) for p in input_paths}
    if classes <= {"jpeg"}:
        return "JPEG", ".jpg"
    if classes <= {"png"}:
        return "PNG", ".png"
    if classes <= {"webp"}:
        return "WEBP", ".webp"
    # Mixed / other -> lossless PNG fallback.
    return "PNG", ".png"


def flatten(img):
    """Return (RGB_image, had_alpha). Any transparency is composited over white."""
    if img.mode in ("RGBA", "LA", "PA") or \
            (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg, True
    if img.mode != "RGB":
        return img.convert("RGB"), False
    return img, False


def load_images(paths, notes):
    """Open, EXIF-transpose, first-frame-only, and flatten each image.

    Returns a list of (path, image) in the original order.
    """
    images = []
    any_alpha = False
    for path in paths:
        if not os.path.isfile(path):
            raise SystemExit("error: file not found: %s" % path)
        ext = os.path.splitext(path)[1].lower()
        if ext in HEIF_EXTS and not HEIF_SUPPORTED:
            raise SystemExit(
                "error: %s is HEIC/HEIF. Install the optional 'pillow-heif' "
                "package (pip install pillow-heif), or convert it to JPG first "
                "(e.g. with the resize skill or the system Preview export)."
                % path)
        img = Image.open(path)
        img.load()
        if getattr(img, "is_animated", False):
            notes.append("animated image: only first frame processed (%s)"
                         % os.path.basename(path))
            img.seek(0)
            img = img.copy()
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        rgb, had_alpha = flatten(img)
        any_alpha = any_alpha or had_alpha
        images.append((path, rgb))
    if any_alpha:
        notes.append("transparent image(s) composited onto white background")
    return images


def layout(real, rows, cols, gap):
    """Return (canvas_w, canvas_h, cell_w, cell_h, strip).

    strip is "h" for a single horizontal row, "v" for a single vertical
    column, or None for a uniform grid.
    """
    if rows == 1:
        max_h = max(i.height for _, i in real)
        total_w = sum(i.width for _, i in real) + gap * (len(real) - 1)
        return total_w, max_h, None, None, "h"
    if cols == 1:
        max_w = max(i.width for _, i in real)
        total_h = sum(i.height for _, i in real) + gap * (len(real) - 1)
        return max_w, total_h, None, None, "v"
    cell_w = max(i.width for _, i in real)
    cell_h = max(i.height for _, i in real)
    canvas_w = cell_w * cols + gap * (cols - 1)
    canvas_h = cell_h * rows + gap * (rows - 1)
    return canvas_w, canvas_h, cell_w, cell_h, None


def main():
    args = parse_args()
    notes = []
    images = load_images(args.inputs, notes)

    n = len(images)
    preset_rows, preset_cols, required = PRESETS[args.preset]
    if args.preset == "long":
        rows, cols = n, 1
    else:
        rows, cols = preset_rows, preset_cols

    expected = rows * cols
    if n < expected:
        notes.append("preset needs %d images, got %d; empty cells filled white"
                     % (expected, n))
        images = images + [None] * (expected - n)
    elif n > expected:
        notes.append("preset needs %d images, got %d; extra images ignored"
                     % (expected, n))
        images = images[:expected]

    real = [e for e in images if e is not None]

    # 联排（单行 / 单列）要求图片尺寸一致；不一致则提醒用户。
    is_row_or_col = (rows == 1 or cols == 1)
    if is_row_or_col and len(real) >= 2:
        sizes = [tuple(i.size) for _, i in real]
        if len(set(sizes)) > 1:
            warn = ("联排要求图片尺寸一致，检测到不一致尺寸 %s；"
                    "已按居中白底对齐处理" % sizes)
            notes.append(warn)
            print("[WARN] " + warn)

    # 1 mm -> px at a fixed, documented DPI (default 96 = screen standard,
    # 1 mm ~ 3.78 px -> rounded to 4 px). Override with --dpi for print.
    dpi = args.dpi if args.dpi else 96.0
    gap = max(0, round(args.gap_mm * dpi / 25.4))

    canvas_w, canvas_h, cell_w, cell_h, strip = layout(real, rows, cols, gap)

    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")

    items = []
    if strip == "h":
        x = 0
        for entry in images:
            if entry is None:
                continue
            path, img = entry
            y = (canvas_h - img.height) // 2
            canvas.paste(img, (x, y))
            items.append({"input": os.path.basename(path),
                          "box": [x, y, img.width, img.height]})
            x += img.width + gap
    elif strip == "v":
        y = 0
        for entry in images:
            if entry is None:
                continue
            path, img = entry
            x = (canvas_w - img.width) // 2
            canvas.paste(img, (x, y))
            items.append({"input": os.path.basename(path),
                          "box": [x, y, img.width, img.height]})
            y += img.height + gap
    else:
        # 宫格（四/六/九）：贴边对齐。边缘格贴向中心十字线（1mm 白隙处），
        # 中心格（仅奇数×奇数宫格存在）默认 contain 居中（等比、不变形），
        # --center-fill stretch 则拉伸填满。图片尺寸在 contain 下不改宽高比。
        mid_h = (cols - 1) / 2.0
        mid_v = (rows - 1) / 2.0
        for r in range(rows):
            for c in range(cols):
                idx = r * cols + c
                entry = images[idx]
                if entry is None:
                    continue
                path, img = entry
                x0 = c * (cell_w + gap)
                y0 = r * (cell_h + gap)
                if r == mid_v and c == mid_h:
                    # 中心格：默认 contain 居中（等比缩放到恰放进整格）。
                    if args.center_fill == "stretch":
                        img = img.resize((cell_w, cell_h), Image.LANCZOS)
                        x, y = x0, y0
                        align = "fill"
                    else:
                        scale = min(cell_w / img.width, cell_h / img.height)
                        nw = max(1, round(img.width * scale))
                        nh = max(1, round(img.height * scale))
                        img = img.resize((nw, nh), Image.LANCZOS)
                        x = x0 + (cell_w - nw) // 2
                        y = y0 + (cell_h - nh) // 2
                        align = "center"
                else:
                    # 水平：左半贴右、右半贴左、中列居中
                    if c < mid_h:
                        x = x0 + (cell_w - img.width)
                        ha = "right"
                    elif c > mid_h:
                        x = x0
                        ha = "left"
                    else:
                        x = x0 + (cell_w - img.width) // 2
                        ha = "center"
                    # 垂直：上半贴下、下半贴上、中行居中
                    if r < mid_v:
                        y = y0 + (cell_h - img.height)
                        va = "bottom"
                    elif r > mid_v:
                        y = y0
                        va = "top"
                    else:
                        y = y0 + (cell_h - img.height) // 2
                        va = "center"
                    align = ha + "-" + va
                canvas.paste(img, (x, y))
                items.append({"input": os.path.basename(path),
                              "box": [x, y, img.width, img.height],
                              "align": align})
        notes.append("宫格贴边对齐：边缘格贴向中心十字线，中心格%s"
                     % ("拉伸填满" if args.center_fill == "stretch"
                        else "contain 居中（不变形）"))

    # Output format & path: <first_stem>_merge.<ext>, never equal to any input.
    fmt, out_ext = infer_output(args, args.inputs)
    first = args.inputs[0]
    stem = os.path.splitext(os.path.basename(first))[0]
    in_dir = os.path.dirname(os.path.abspath(first))
    if args.output:
        out_path = args.output
    else:
        out_path = os.path.join(in_dir, stem + "_merge" + out_ext)
    if not os.path.splitext(out_path)[1]:
        out_path += out_ext
    out_path = os.path.abspath(out_path)
    if any(out_path == os.path.abspath(i) for i in args.inputs):
        out_path = os.path.join(os.path.dirname(out_path),
                                stem + "_merge_new" + out_ext)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if fmt in ("JPEG", "WEBP"):
        canvas.save(out_path, format=fmt, quality=args.quality)
    else:
        canvas.save(out_path, format=fmt)

    result = {
        "output": out_path,
        "size": [canvas_w, canvas_h],
        "preset": args.preset,
        "format": fmt,
        "quality": args.quality if fmt in ("JPEG", "WEBP") else None,
        "gap_mm": args.gap_mm,
        "gap_px": gap,
        "dpi": dpi,
        "count": len(real),
    }
    if args.verbose:
        result["items"] = items
    if notes:
        result["notes"] = notes

    print("%d images -> %s %dx%d (preset=%s, gap=%dpx, %s)"
          % (len(real), os.path.basename(out_path), canvas_w, canvas_h,
             args.preset, gap, fmt))
    print(json.dumps([result], ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
