#!/usr/bin/env python3
"""Deterministic image resize utility built on Pillow.

Principles:
- Never overwrite an input file; outputs are named <stem>_<W>x<H>.<ext>.
- Always re-open the saved file to verify actual dimensions.
- Use Image.draft() to downscale-decode large JPEGs when the target is smaller,
  cutting decode time and memory by several-fold.
- Print one human-readable line per file, then a compact single-line JSON array.

Supported modes:
  fit      keep aspect ratio, scale into target box (no crop, no pad)
  exact    stretch to exact target size (may distort)
  cover    scale to fill target box, center-crop the overflow
  contain  scale into target box, pad the remainder (default white)
"""

import argparse
import concurrent.futures
import io
import json
import os
import sys
import threading

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.stderr.write("error: Pillow is not installed in this interpreter. "
                     "Install it with: pip install Pillow\n")
    sys.exit(2)

# Optional HEIC/HEIF support (iPhone photos).
HEIF_SUPPORTED = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    pass

HEIF_EXTS = {".heic", ".heif"}

EXT_FOR = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "bmp": ".bmp",
           "tiff": ".tiff", "gif": ".gif"}
EXT_TO_FMT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
              ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF",
              ".heic": "JPEG", ".heif": "JPEG"}


def parse_args():
    p = argparse.ArgumentParser(description="Deterministic image resize (Pillow).")
    p.add_argument("inputs", nargs="+", help="input image file(s)")
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--percent", type=float, default=None,
                   help="scale to a percentage of original size, e.g. 50")
    p.add_argument("--scale", type=float, default=None,
                   help="scale factor, e.g. 2 or 0.5")
    p.add_argument("--mode", choices=["fit", "exact", "cover", "contain"],
                   default=None)
    p.add_argument("--format", dest="fmt",
                   choices=["keep", "jpeg", "png", "webp"], default="keep")
    p.add_argument("--quality", type=int, default=85,
                   help="JPEG/WebP quality (default 85)")
    p.add_argument("--max-size", dest="max_size", default=None,
                   help="target max file size, e.g. 500KB / 10MB "
                        "(plain number is treated as KB; JPEG/WebP only)")
    p.add_argument("--outdir", default=None,
                   help="output directory (default: same as input)")
    p.add_argument("--pad-color", default="#FFFFFF",
                   help="contain-mode background color (default white)")
    p.add_argument("--jobs", type=int, default=1,
                   help="parallel workers for batch input (default 1 = serial)")
    return p.parse_args()


def parse_size(s):
    """Parse a size string like '10MB', '500KB', '512K', '10M', or '200'
    (plain number = KB). Returns bytes."""
    if s is None:
        return None
    t = str(s).strip().upper()
    try:
        if t.endswith("MB"):
            return int(round(float(t[:-2]) * 1024 * 1024))
        if t.endswith("KB"):
            return int(round(float(t[:-2]) * 1024))
        if t.endswith("M"):
            return int(round(float(t[:-1]) * 1024 * 1024))
        if t.endswith("K"):
            return int(round(float(t[:-1]) * 1024))
        if t.endswith("B"):
            return int(round(float(t[:-1])))
        return int(round(float(t) * 1024))  # plain number = KB
    except ValueError:
        raise SystemExit(f"error: invalid --max-size value: {s!r} "
                         "(use e.g. 500KB, 10MB, or 200 for 200KB)")


def resolve_target(w, h, args):
    """Return (tw, th, factor_based) for a source of size (w, h).
    Raise SystemExit on bad input."""
    if args.scale is not None or args.percent is not None:
        if args.width is not None or args.height is not None:
            raise SystemExit("error: --scale/--percent cannot be combined "
                             "with --width/--height")
        factor = args.scale if args.scale is not None else args.percent / 100.0
        if factor <= 0:
            raise SystemExit("error: scale factor must be positive")
        return max(1, round(w * factor)), max(1, round(h * factor)), True
    if args.width is not None and args.height is not None:
        if args.width <= 0 or args.height <= 0:
            raise SystemExit("error: width and height must be positive")
        return args.width, args.height, False
    if args.width is not None:
        if args.width <= 0:
            raise SystemExit("error: width must be positive")
        return args.width, max(1, round(h * args.width / w)), False
    if args.height is not None:
        if args.height <= 0:
            raise SystemExit("error: height must be positive")
        return max(1, round(w * args.height / h)), args.height, False
    raise SystemExit("error: specify one of --width/--height/--percent/--scale")


def resolve_mode(args):
    if args.mode:
        return args.mode
    # Defaults: both explicit dims -> exact; single dim / factor -> fit.
    if args.width is not None and args.height is not None:
        return "exact"
    return "fit"


def apply_mode(img, mode, tw, th, pad_color, factor_based=False):
    """Return (image, note_list). Image is RGB/RGBA as needed."""
    notes = []
    src_w, src_h = img.size
    if mode == "exact":
        out = img.resize((tw, th), Image.LANCZOS)
    elif mode == "cover":
        out = ImageOps.fit(img, (tw, th), method=Image.LANCZOS, centering=(0.5, 0.5))
        notes.append("center-cropped to fill")
    elif mode == "contain":
        out = ImageOps.pad(img, (tw, th), method=Image.LANCZOS,
                           color=pad_color, centering=(0.5, 0.5))
        notes.append("padded to fit")
    else:  # fit
        src_ar = src_w / src_h
        tgt_ar = tw / th
        ar_preserved = abs(tgt_ar - src_ar) / src_ar < 0.01
        if factor_based or ar_preserved:
            out = img.resize((tw, th), Image.LANCZOS)
        else:
            out = img.copy()
            out.thumbnail((tw, th), Image.LANCZOS)
            if out.size != (tw, th):
                notes.append("kept aspect ratio (fits within target)")
                if out.size == (src_w, src_h):
                    notes.append("source already smaller than target; not "
                                 "enlarged (use --scale to upscale)")
    if out.size[0] > src_w or out.size[1] > src_h:
        notes.append("upscaled: interpolation adds no new detail")
    return out, notes


def save_with_target_size(img, path, fmt, quality, max_size_bytes):
    """Save img; if max_size_bytes set (JPEG/WebP), binary-search quality."""
    if max_size_bytes is not None and fmt in ("JPEG", "WEBP"):
        limit = max_size_bytes
        best = None
        lo, hi = 20, min(95, max(20, quality))
        while lo <= hi:
            mid = (lo + hi) // 2
            buf = io.BytesIO()
            img.save(buf, format=fmt, quality=mid)
            data = buf.getvalue()
            if len(data) <= limit:
                best = (mid, data)
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            buf = io.BytesIO()
            img.save(buf, format=fmt, quality=20)
            best = (20, buf.getvalue())
            with open(path, "wb") as f:
                f.write(best[1])
            return False, best[0]
        with open(path, "wb") as f:
            f.write(best[1])
        return len(best[1]) <= limit, best[0]
    if fmt in ("JPEG", "WEBP"):
        img.save(path, format=fmt, quality=quality)
        return None, quality
    # PNG/GIF/etc. have no "quality" knob; report None.
    img.save(path, format=fmt)
    return None, None


def process(path, args, claimed, lock):
    """Process one image; return a result record dict."""
    if not os.path.isfile(path):
        return {"input": path, "error": "file not found"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in EXT_TO_FMT:
        return {"input": path, "error": f"unsupported type {ext}"}
    if ext in HEIF_EXTS and not HEIF_SUPPORTED:
        return {"input": path,
                "error": ("HEIC/HEIF needs the optional 'pillow-heif' package "
                          "(pip install pillow-heif); or convert to JPG first "
                          "(e.g. with the resize skill / system Preview export)")}

    try:
        img = Image.open(path)
    except Exception as e:
        return {"input": path, "error": f"cannot open: {e}"}

    src_w, src_h = img.size
    # Account for EXIF orientation so the target aspect ratio matches what the
    # user actually sees (portrait shots stored landscape).
    try:
        orientation = img.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    rotated = orientation in (5, 6, 7, 8)
    disp_w, disp_h = (src_h, src_w) if rotated else (src_w, src_h)

    notes = []
    animated = getattr(img, "is_animated", False)
    if animated:
        notes.append("animated image: only first frame processed")
        img.seek(0)
        img = img.copy()

    try:
        tw, th, factor_based = resolve_target(disp_w, disp_h, args)
    except SystemExit:
        raise  # parameter error -> abort the whole run

    # draft(): downscale-decode large JPEGs when the target is smaller. The
    # final resize to (tw, th) still produces the exact dimensions, so draft
    # only speeds up decoding and lowers memory (several-fold for big photos).
    draft_target = (th, tw) if rotated else (tw, th)
    if (not animated) and img.format == "JPEG" and img.mode in ("RGB", "L") \
            and (draft_target[0] < src_w or draft_target[1] < src_h):
        img.draft("RGB", draft_target)
    img.load()

    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # "before" is the display size (EXIF-corrected), captured BEFORE draft()
    # downsampled the decode — it must reflect the true original dimensions.
    before = [disp_w, disp_h]
    mode = resolve_mode(args)
    out, mode_notes = apply_mode(img, mode, tw, th, args.pad_color,
                                 factor_based=factor_based)
    notes.extend(mode_notes)

    # Output format.
    if args.fmt == "keep":
        fmt = EXT_TO_FMT[ext]
        if ext in HEIF_EXTS:
            out_ext = ".jpg"  # cannot write HEIF back; convert to JPEG
            notes.append("HEIC/HEIF input converted to JPEG")
        else:
            out_ext = ext  # preserve the original extension (.tif, .jpeg, ...)
    else:
        fmt = args.fmt.upper()
        out_ext = EXT_FOR[args.fmt]

    # Channel handling.
    has_alpha = out.mode in ("RGBA", "LA", "PA") or \
        (out.mode == "P" and "transparency" in out.info)
    if fmt == "JPEG":
        if has_alpha:
            bg = Image.new("RGB", out.size, args.pad_color)
            rgba = out.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            out = bg
            notes.append("alpha flattened onto white background")
        elif out.mode != "RGB":
            src_mode = img.mode
            out = out.convert("RGB")
            notes.append(f"converted {src_mode} to RGB for JPEG")
    elif fmt == "WEBP" and out.mode not in ("RGBA", "RGB"):
        out = out.convert("RGBA" if has_alpha else "RGB")

    # Output path: <stem>_<W>x<H><ext>, never equal to input. Dimensions come
    # from the REAL output size (fit mode can land on different dimensions).
    stem = os.path.splitext(os.path.basename(path))[0]
    aw, ah = out.size
    out_name = f"{stem}_{aw}x{ah}{out_ext}"
    out_dir = args.outdir or os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, out_name)
    if os.path.abspath(out_path) == os.path.abspath(path):
        out_path = os.path.join(out_dir, f"{stem}_{aw}x{ah}_new{out_ext}")
    # Avoid colliding with another output produced earlier in this run
    # (e.g. same-stem inputs from different dirs scaled to identical size).
    with lock:
        base, fext = os.path.splitext(out_path)
        counter = 2
        while os.path.abspath(out_path) in claimed:
            out_path = f"{base}_{counter}{fext}"
            counter += 1
        claimed.add(os.path.abspath(out_path))

    max_size_bytes = parse_size(args.max_size)
    size_ok, used_q = save_with_target_size(out, out_path, fmt,
                                            args.quality, max_size_bytes)

    max_size_unsupported = max_size_bytes is not None and fmt not in ("JPEG", "WEBP")
    if max_size_unsupported:
        notes.append(f"--max-size ignored: {fmt} output is lossless; "
                     "add --format webp (or jpeg) to enforce the size cap")
        sys.stderr.write(
            f"warning: --max-size has no effect on {fmt} output for "
            f"{os.path.basename(path)}; add --format webp to enforce it\n")

    # Verify by re-reading the saved file.
    with Image.open(out_path) as check:
        after = list(check.size)
        actual_fmt = check.format

    rec = {
        "input": path,
        "output": out_path,
        "before": before,
        "after": after,
        "mode": mode,
        "format": actual_fmt,
        "input_bytes": os.path.getsize(path),
        "output_bytes": os.path.getsize(out_path),
    }
    if used_q is not None:
        rec["quality"] = used_q
    final_size_ok = None
    if max_size_bytes is not None:
        rec["target_max"] = args.max_size
        rec["target_max_bytes"] = max_size_bytes
        if max_size_unsupported:
            final_size_ok = False
        elif size_ok is not None:
            final_size_ok = bool(size_ok)
        else:
            final_size_ok = os.path.getsize(out_path) <= max_size_bytes
        rec["size_ok"] = final_size_ok
    if notes:
        rec["notes"] = notes
    size_note = ""
    if final_size_ok is False:
        size_note = " [WARN: could not reach target file size]"
    print(f"{os.path.basename(path)}: {before[0]}x{before[1]} -> "
          f"{os.path.basename(out_path)} {after[0]}x{after[1]} "
          f"({rec['input_bytes']/1024:.0f}KB -> {rec['output_bytes']/1024:.0f}KB) "
          f"mode={mode} fmt={actual_fmt}{size_note}")
    return rec


def main():
    args = parse_args()
    if args.jobs is not None and args.jobs < 1:
        raise SystemExit("error: --jobs must be >= 1")

    results = [None] * len(args.inputs)
    claimed = set()
    lock = threading.Lock()

    if args.jobs > 1 and len(args.inputs) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            future_map = {ex.submit(process, path, args, claimed, lock): i
                          for i, path in enumerate(args.inputs)}
            for fut in concurrent.futures.as_completed(future_map):
                i = future_map[fut]
                try:
                    results[i] = fut.result()
                except SystemExit as e:
                    raise
    else:
        for i, path in enumerate(args.inputs):
            results[i] = process(path, args, claimed, lock)

    errors = [r for r in results if r and "error" in r]
    print(json.dumps(results, ensure_ascii=False, separators=(",", ":")))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
