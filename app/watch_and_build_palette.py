import os
import re
import time
import json
import shutil
import configparser
from datetime import datetime
from collections import Counter
from itertools import combinations

import subprocess

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont
from skimage.measure import label, regionprops
from skimage.color import rgb2lab

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib import colors as mcolors
from scipy.optimize import minimize


IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff")


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_makedirs(p: str):
    os.makedirs(p, exist_ok=True)


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def read_config(path: str):
    cfg = configparser.ConfigParser()
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing config.ini at: {path}")
    cfg.read(path)

    # Required
    input_dir = cfg.get("paths", "input_dir")
    output_dir = cfg.get("paths", "output_dir")

    # Optional archive_dir (preferred because you mounted /mnt/archive)
    archive_dir = cfg.get("paths", "archive_dir", fallback="").strip()

    # Watch
    poll_seconds = cfg.getint("watch", "poll_seconds", fallback=20)
    require_pairs = cfg.getboolean("watch", "require_pairs", fallback=True)
    marked_suffix = cfg.get("watch", "marked_suffix", fallback="_x")

    # Diff
    threshold = cfg.getint("diff", "threshold", fallback=35)

    # Outputs
    build_palettes = cfg.getboolean("outputs", "build_palettes", fallback=True)
    palette_include_variants = cfg.getboolean("outputs", "palette_include_variants", fallback=True)
    include_originals_in_pdf = cfg.getboolean("outputs", "include_originals_in_pdf", fallback=True)
    include_bb_in_pdf = cfg.getboolean("outputs", "include_bb_in_pdf", fallback=True)

    # Legacy (kept for compatibility; V2 writes per-base PDFs)
    combined_pdf_name = cfg.get("outputs", "combined_pdf_name", fallback="combined_report.pdf")

    # Optional archive folder name under input (fallback mode)
    archive_folder_name = cfg.get("archive", "archive_folder_name", fallback="archive")

    # V2 toggles (defaults: minimal non-visual outputs)
    write_swatches_json = cfg.getboolean("outputs", "write_swatches_json", fallback=False)
    write_mix_json = cfg.getboolean("outputs", "write_mix_json", fallback=False)

    # Drive upload (optional)
    drive_enabled = cfg.getboolean("drive", "enabled", fallback=False)
    drive_remote = cfg.get("drive", "remote", fallback="palette_drive").strip()
    drive_mode = cfg.get("drive", "mode", fallback="full_folder").strip()

    # V2 mix config
    mix_step_pct = cfg.getfloat("mix", "step_pct", fallback=2.5)
    max_pigments = cfg.getint("mix", "max_pigments", fallback=4)
    allow_black_fallback = cfg.getboolean("mix", "allow_black_fallback", fallback=True)
    deltae_fallback_threshold = cfg.getfloat("mix", "black_fallback_deltae", fallback=6.0)

    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "archive_dir": archive_dir,
        "archive_folder_name": archive_folder_name,
        "poll_seconds": poll_seconds,
        "require_pairs": require_pairs,
        "marked_suffix": marked_suffix,
        "threshold": threshold,
        "build_palettes": build_palettes,
        "palette_include_variants": palette_include_variants,
        "include_originals_in_pdf": include_originals_in_pdf,
        "include_bb_in_pdf": include_bb_in_pdf,
        "combined_pdf_name": combined_pdf_name,
        "write_swatches_json": write_swatches_json,
        "write_mix_json": write_mix_json,
        "drive_enabled": drive_enabled,
        "drive_remote": drive_remote,
        "drive_mode": drive_mode,
        "mix_step_pct": mix_step_pct,
        "max_pigments": max_pigments,
        "allow_black_fallback": allow_black_fallback,
        "black_fallback_deltae": deltae_fallback_threshold,
    }


def resolve_archive_dir(cfg):
    """
    Priority:
      1) [paths].archive_dir (e.g., /mnt/archive) if set
      2) /mnt/in/<archive_folder_name> if it exists
      3) /mnt/out/_processed_inputs
    """
    input_dir = cfg["input_dir"]
    output_dir = cfg["output_dir"]

    # 1) explicit archive_dir
    if cfg.get("archive_dir"):
        safe_makedirs(cfg["archive_dir"])
        return cfg["archive_dir"]

    # 2) archive folder inside input
    candidate = os.path.join(input_dir, cfg.get("archive_folder_name", "archive"))
    if os.path.isdir(candidate):
        return candidate

    # 3) fallback
    fallback = os.path.join(output_dir, "_processed_inputs")
    safe_makedirs(fallback)
    return fallback


def load_paints_from_ini(path: str):
    """Load paint definitions from INI.

    Supports:
      [paints]
      Titanium White = #FFFFFF

    Optional:
      [paints.enabled]
      Titanium White = true|false

    Returns dict[name] = HEX (uppercase).
    """
    if not os.path.exists(path):
        return {}

    p = configparser.ConfigParser()
    p.optionxform = str  # preserve paint names
    try:
        p.read(path)
    except Exception:
        return {}

    if not p.has_section("paints"):
        return {}

    enabled = {}
    if p.has_section("paints.enabled"):
        for k, v in p.items("paints.enabled"):
            enabled[k.strip()] = str(v).strip().lower() in ("1", "true", "yes", "on")

    paints = {}
    for name, hexv in p.items("paints"):
        n = name.strip()
        v = str(hexv).strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", v):
            continue
        if enabled and not enabled.get(n, True):
            continue
        paints[n] = v.upper()

    return paints


# =========================
# Pigments (single source of truth)
# =========================

PIGMENTS_HEX = {
    "Titanium White": "#FFFFFF",
    "Yellow Ochre": "#DFAF2C",
    "Burnt Sienna": "#E97451",
    "Burnt Umber": "#8A3324",
    "Paynes Gray": "#536878",
    "Ivory Black": "#231F20",
}

CUSTOM_PIGMENT_COLORS = {
    # used for pie chart color chips
    "Titanium White": "#f8f8f6",
    "Yellow Ochre": "#c5a059",
    "Burnt Sienna": "#b05c26",
    "Burnt Umber": "#5c4033",
    "Paynes Gray": "#4d5d69",
    "Ivory Black": "#1c1c1c",
}


# =========================
# Color helpers
# =========================

def hex_color(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def get_dominant_colors(image: Image.Image, num_colors=1):
    image = image.convert("RGB")
    image = image.resize((max(1, image.width // 2), max(1, image.height // 2)))
    pixels = np.array(image).reshape((-1, 3))
    color_counts = Counter(map(tuple, pixels))
    most_common = color_counts.most_common(num_colors)
    return [(hex_color(color), count) for color, count in most_common]


def load_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def compare_images(marked_path, orig_path, out_dir, base_label, threshold=35, write_swatches_json=False):
    """Bounding boxes / labels tuned per Preview 6."""

    marked = Image.open(marked_path).convert("RGB")
    orig = Image.open(orig_path).convert("RGB")

    if marked.size != orig.size:
        raise ValueError(f"Size mismatch: {marked_path} vs {orig_path}")

    w, h = orig.size

    diff = ImageChops.difference(marked, orig)
    diff_gray = diff.convert("L")
    diff_np = np.array(diff_gray)
    diff_np = np.where(diff_np > threshold, 255, 0).astype(np.uint8)

    labeled_img = label(diff_np, connectivity=2)
    regions = regionprops(labeled_img)

    img_with_boxes = orig.copy()
    draw = ImageDraw.Draw(img_with_boxes)

    # label sizing (print-friendly) — includes hex
    font_px = clamp(int(min(w, h) * 0.022), 14, 40)
    stroke_w = clamp(int(font_px * 0.18), 2, 6)
    font = load_font(font_px)

    # thin dynamic bbox
    box_w = clamp(int(min(w, h) * 0.0015), 1, 4)

    # keep tiny regions (minimal filtering) + small pad
    min_area = 5
    pad = clamp(int(min(w, h) * 0.0025), 2, 12)

    kept = [r for r in regions if r.area >= min_area]

    swatches = []
    for i, region in enumerate(kept):
        minr, minc, maxr, maxc = region.bbox
        left, top, right, bottom = minc, minr, maxc, maxr

        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(w, right + pad)
        bottom = min(h, bottom + pad)

        swatch_crop = orig.crop((left, top, right, bottom))
        dominant = get_dominant_colors(swatch_crop, 1)[0][0]

        draw.rectangle([left, top, right, bottom], outline="white", width=box_w)

        text = f"{i+1}: {dominant}"
        y = max(0, top - font_px - 4)
        x = left
        try:
            tb = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
            tw = tb[2] - tb[0]
            x = clamp(x, 0, max(0, w - tw - 2))
        except Exception:
            pass

        draw.text(
            (x, y),
            text,
            font=font,
            fill=(255, 255, 255),
            stroke_width=stroke_w,
            stroke_fill=(0, 0, 0),
        )

        swatches.append({"label": base_label, "swatch": i + 1, "color": dominant})

    safe_makedirs(out_dir)
    swatch_json_path = os.path.join(out_dir, f"{base_label}_colorswatches.json")
    bb_image_path = os.path.join(out_dir, f"{base_label}_bb.jpg")

    if write_swatches_json:
        with open(swatch_json_path, "w", encoding="utf-8") as f:
            json.dump(swatches, f, indent=4)

    img_with_boxes.save(bb_image_path, quality=95)
    return swatch_json_path, bb_image_path, swatches


# =========================
# Mixing solver V2 (ΔE in Lab, max pigments, 2.5% rounding, black fallback)
# =========================

def rgb01_to_lab(rgb01: np.ndarray) -> np.ndarray:
    """rgb01 shape (3,) -> lab shape (3,)"""
    arr = np.clip(rgb01.reshape((1, 1, 3)), 0, 1)
    lab = rgb2lab(arr)
    return lab.reshape((3,))


def deltae76(lab1: np.ndarray, lab2: np.ndarray) -> float:
    return float(np.linalg.norm(lab1 - lab2))


def mix_rgb01(weights: np.ndarray, pigment_rgbs: np.ndarray) -> np.ndarray:
    # weights (k,), pigment_rgbs (k,3)
    return np.clip(np.sum(weights.reshape((-1, 1)) * pigment_rgbs, axis=0), 0, 1)


def round_weights_to_step(weights: np.ndarray, step_pct: float) -> np.ndarray:
    """Round weights to nearest step_pct (e.g. 2.5%) and re-normalize to sum=1."""
    step = step_pct / 100.0
    if step <= 0:
        w = np.clip(weights, 0, 1)
        s = float(np.sum(w))
        return (w / s) if s > 0 else w

    w = np.clip(weights, 0, 1)
    if float(np.sum(w)) <= 0:
        w = np.ones_like(w) / len(w)

    # normalize
    w = w / float(np.sum(w))

    # quantize
    q = np.round(w / step) * step

    # ensure at least one step for any nonzero component
    # (avoid all zeros)
    if float(np.sum(q)) == 0:
        q[np.argmax(w)] = step

    # adjust to sum exactly 1.0 by fixing the largest component
    total = float(np.sum(q))
    diff = 1.0 - total
    j = int(np.argmax(q))
    q[j] = np.clip(q[j] + diff, 0, 1)

    # if rounding caused tiny negative, fix by re-normalizing
    q = np.clip(q, 0, 1)
    s = float(np.sum(q))
    if s <= 0:
        q = np.ones_like(q) / len(q)
    else:
        q = q / s

    return q


def solve_mix_for_target(
    target_hex: str,
    step_pct: float = 2.5,
    max_pigments: int = 4,
    allow_black_fallback: bool = True,
    black_fallback_deltae: float = 6.0,
):
    target_rgb = np.array(mcolors.hex2color(target_hex), dtype=float)
    target_lab = rgb01_to_lab(target_rgb)

    # build pigment arrays
    pigment_items = [(k, np.array(mcolors.hex2color(v), dtype=float)) for k, v in PIGMENTS_HEX.items()]

    def run_search(allowed_names):
        allowed = [(n, rgb) for (n, rgb) in pigment_items if n in allowed_names]
        best = None

        # search combos size 2..max_pigments
        for k in range(2, min(max_pigments, len(allowed)) + 1):
            for combo in combinations(allowed, k):
                names = [x[0] for x in combo]
                rgbs = np.stack([x[1] for x in combo], axis=0)

                def obj(w):
                    w = np.clip(np.array(w, dtype=float), 0, 1)
                    s = float(np.sum(w))
                    if s <= 0:
                        w = np.ones((k,), dtype=float) / k
                    else:
                        w = w / s
                    mixed_rgb = mix_rgb01(w, rgbs)
                    mixed_lab = rgb01_to_lab(mixed_rgb)
                    return deltae76(mixed_lab, target_lab)

                x0 = np.ones((k,), dtype=float) / k
                bounds = [(0, 1) for _ in range(k)]
                cons = {"type": "eq", "fun": lambda w: float(np.sum(w)) - 1.0}

                try:
                    res = minimize(obj, x0, bounds=bounds, constraints=[cons])
                    w = np.array(res.x, dtype=float)
                except Exception:
                    w = x0

                w = np.clip(w, 0, 1)
                if float(np.sum(w)) <= 0:
                    w = x0
                else:
                    w = w / float(np.sum(w))

                # round to step
                wq = round_weights_to_step(w, step_pct=step_pct)

                mixed_rgb = mix_rgb01(wq, rgbs)
                mixed_lab = rgb01_to_lab(mixed_rgb)
                de = deltae76(mixed_lab, target_lab)

                if best is None or de < best["deltaE"]:
                    best = {
                        "pigments": names,
                        "weights": wq,
                        "mixed_rgb": mixed_rgb,
                        "mixed_hex": mcolors.to_hex(mixed_rgb),
                        "deltaE": float(de),
                    }

        return best

    # primary: exclude Ivory Black
    primary_allowed = [
        "Titanium White",
        "Yellow Ochre",
        "Burnt Sienna",
        "Burnt Umber",
        "Paynes Gray",
    ]

    best = run_search(primary_allowed)
    used_black = False

    if allow_black_fallback and (best is None or best["deltaE"] > black_fallback_deltae):
        fallback_allowed = primary_allowed + ["Ivory Black"]
        best2 = run_search(fallback_allowed)
        if best2 is not None and (best is None or best2["deltaE"] < best["deltaE"]):
            best = best2
            used_black = ("Ivory Black" in best["pigments"])

    if best is None:
        # emergency fallback
        best = {
            "pigments": ["Titanium White"],
            "weights": np.array([1.0]),
            "mixed_rgb": np.array([1.0, 1.0, 1.0]),
            "mixed_hex": "#ffffff",
            "deltaE": 999.0,
        }

    # convert to percentages dict
    perc = {n: float(w * 100.0) for n, w in zip(best["pigments"], best["weights"]) if w > 0}

    # sort by descending weight
    perc = dict(sorted(perc.items(), key=lambda kv: kv[1], reverse=True))

    return {
        "color_percentages": perc,
        "mixed_color_hex": best["mixed_hex"],
        "deltaE": float(best["deltaE"]),
        "used_black": bool(used_black),
    }


def build_mix_data(swatches_list, cfg):
    mix = {"entries": []}
    for s in swatches_list:
        hex_col = s["color"]
        mix_data = solve_mix_for_target(
            hex_col,
            step_pct=float(cfg.get("mix_step_pct", 2.5)),
            max_pigments=int(cfg.get("max_pigments", 4)),
            allow_black_fallback=bool(cfg.get("allow_black_fallback", True)),
            black_fallback_deltae=float(cfg.get("black_fallback_deltae", 6.0)),
        )
        mix["entries"].append(
            {
                "Hex Color": hex_col,
                "Color Percentages": mix_data["color_percentages"],
                "Mixed Color Hex": mix_data["mixed_color_hex"],
                "DeltaE": mix_data["deltaE"],
                "Used Ivory Black": mix_data["used_black"],
            }
        )
    return mix


# =========================
# Palette builder (kept as-is from your combined script)
# =========================

def hex_to_rgb01(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def rgb01_to_hex(rgb01):
    return "#{:02x}{:02x}{:02x}".format(*(int(max(0, min(1, x)) * 255) for x in rgb01))


def mix_colors(hex1, hex2, ratio):
    r1, g1, b1 = hex_to_rgb01(hex1)
    r2, g2, b2 = hex_to_rgb01(hex2)
    mixed = (r1 * (1 - ratio) + r2 * ratio, g1 * (1 - ratio) + g2 * ratio, b1 * (1 - ratio) + b2 * ratio)
    return rgb01_to_hex(mixed)


def generate_palette(base_color, include_variants=True):
    if not include_variants:
        # Base-only palette: single swatch
        return [[base_color]], ["Base"]

    pigments = {
        "Titanium White": "#ffffff",
        "Yellow Ochre": "#c79b46",
        "Burnt Sienna": "#8a4b2d",
        "Burnt Umber": "#5a3a31",
        "Ivory Black": "#1c1c1c",
        "Paynes Gray": "#404f5c",
    }

    base_variants = {
        "Base": base_color,
    }

    if include_variants:
        base_variants.update({
            "Warm": mix_colors(base_color, pigments["Yellow Ochre"], 0.3),
            "Cool": mix_colors(base_color, pigments["Paynes Gray"], 0.3),
            "Shadow": mix_colors(base_color, pigments["Ivory Black"], 0.4),
            "Highlight": mix_colors(base_color, pigments["Titanium White"], 0.4),
        })

    variant_keys = list(base_variants.keys())
    shadow_ratios = [0.15, 0.10, 0.05]
    highlight_ratios = [0.15, 0.10, 0.05]
    rows, cols = 7, 5
    swatch_colors = []

    for row in range(rows):
        row_colors = []
        for key in variant_keys:
            base_hex = base_variants[key]
            if row < 3:
                ratio = shadow_ratios[row]
                row_colors.append(mix_colors(base_hex, pigments["Ivory Black"], ratio))
            elif row == 3:
                row_colors.append(base_hex)
            else:
                ratio = highlight_ratios[row - 4]
                row_colors.append(mix_colors(base_hex, pigments["Titanium White"], ratio))
        swatch_colors.append(row_colors)

    return swatch_colors, variant_keys


def draw_palette_image(swatch_colors, variant_keys, out_path):
    rows = len(swatch_colors)
    cols = len(variant_keys)
    fig, ax = plt.subplots(figsize=(cols * 2.2, rows * 1.5))

    for row in range(rows):
        for col in range(cols):
            color = swatch_colors[row][col]
            y = rows - 1 - row
            ax.add_patch(plt.Rectangle((col, y), 1, 1, color=color))
            ax.add_patch(plt.Rectangle((col + 0.15, y + 0.4), 0.7, 0.2, color="white", zorder=2))
            ax.text(col + 0.5, y + 0.5, color, ha="center", va="center", fontsize=7, color="black", zorder=3)
            if row == 3:
                ax.text(col + 0.5, y + 0.8, variant_keys[col], ha="center", va="bottom", fontsize=9, weight="bold", color="white")

    ax.set_xlim(0, cols)
    ax.set_ylim(0, rows)
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, format="jpeg", bbox_inches="tight")
    plt.close(fig)


def build_palette_for_hex(hex_color_str, out_dir, include_variants=True):
    safe_makedirs(out_dir)
    base = hex_color_str.lstrip("#").lower()
    img_path = os.path.join(out_dir, f"{base}_palette-swatches.jpeg")

    swatch_colors, variant_keys = generate_palette(f"#{base}", include_variants=include_variants)
    draw_palette_image(swatch_colors, variant_keys, img_path)
    return img_path


def add_fullpage_image_to_pdf(pdf: PdfPages, image_path: str, title: str):
    img = Image.open(image_path).convert("RGB")
    fig, ax = plt.subplots(figsize=(8.5, 11))
    ax.imshow(img)
    ax.axis("off")
    fig.suptitle(title, fontsize=14, y=0.95)
    pdf.savefig(fig)
    plt.close(fig)


def save_pie_chart_image(percentages, hex_color, out_path, title):
    labels = list(percentages.keys())
    sizes = list(percentages.values())
    colors = [CUSTOM_PIGMENT_COLORS.get(label, "#cccccc") for label in labels]

    fig, ax = plt.subplots(figsize=(8.5, 11))
    pie_ax = fig.add_axes([0.1, 0.25, 0.8, 0.6])
    pie_ax.pie(
        sizes,
        labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=140,
        textprops={"fontsize": 10},
    )
    pie_ax.axis("equal")
    fig.suptitle(f"{title} — {hex_color}", fontsize=16, weight="bold", y=0.92)

    plt.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def process_one_pair(stem, orig_path, marked_path, cfg):
    """V2: write outputs directly under output_dir/<stem>/"""
    out_dir = os.path.join(cfg["output_dir"], stem)
    safe_makedirs(out_dir)

    report_path = os.path.join(out_dir, f"{stem}_report.pdf")

    # Store originals in output folder too (for reference)
    stored_orig = os.path.join(out_dir, os.path.basename(orig_path))
    stored_marked = os.path.join(out_dir, os.path.basename(marked_path))
    shutil.copy2(orig_path, stored_orig)
    shutil.copy2(marked_path, stored_marked)

    # bounding boxes + swatches
    _swatch_json_path, bb_image_path, swatches_list = compare_images(
        marked_path=marked_path,
        orig_path=orig_path,
        out_dir=out_dir,
        base_label=stem,
        threshold=cfg["threshold"],
        write_swatches_json=cfg.get("write_swatches_json", False),
    )

    mix_data = build_mix_data(swatches_list, cfg)

    # optional mix json
    if cfg.get("write_mix_json", False):
        mix_json_path = os.path.join(out_dir, f"{stem}_mix.json")
        with open(mix_json_path, "w", encoding="utf-8") as f:
            json.dump(mix_data, f, indent=4)

    charts_dir = os.path.join(out_dir, "charts")
    safe_makedirs(charts_dir)

    # Write PDF per-base
    with PdfPages(report_path) as pdf:
        if cfg["include_bb_in_pdf"]:
            add_fullpage_image_to_pdf(pdf, bb_image_path, title=f"{stem} — Bounding Boxes")

        entries = mix_data.get("entries", [])
        for idx, entry in enumerate(entries, start=1):
            percentages = entry.get("Color Percentages", {})
            hex_color = entry.get("Hex Color", f"#{idx}")
            if not percentages:
                continue

            chart_img_path = os.path.join(charts_dir, f"{stem}_pie_{idx:03d}.png")
            save_pie_chart_image(percentages, hex_color, chart_img_path, title=f"{stem} Mix {idx}")
            add_fullpage_image_to_pdf(pdf, chart_img_path, title=f"{stem} — Pie Chart {idx}")

            if cfg["build_palettes"]:
                palettes_dir = os.path.join(out_dir, "palettes")
                safe_makedirs(palettes_dir)
                palette_img = build_palette_for_hex(hex_color, palettes_dir, include_variants=cfg.get("palette_include_variants", True))
                add_fullpage_image_to_pdf(pdf, palette_img, title=f"{stem} — Palette for {hex_color}")

        if cfg["include_originals_in_pdf"]:
            add_fullpage_image_to_pdf(pdf, stored_orig, title=f"{stem} — Original")
            add_fullpage_image_to_pdf(pdf, stored_marked, title=f"{stem} — Marked")

    return report_path


# =========================
# Drive upload (rclone)
# =========================

def folder_signature(root: str) -> float:
    """Return a single float signature based on max mtime under root."""
    max_m = 0.0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fn))
                if st.st_mtime > max_m:
                    max_m = st.st_mtime
            except FileNotFoundError:
                pass
    return max_m


def load_drive_state(stem_dir: str) -> dict:
    p = os.path.join(stem_dir, '.drive_upload.json')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_drive_state(stem_dir: str, state: dict) -> None:
    p = os.path.join(stem_dir, '.drive_upload.json')
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def drive_upload(cfg: dict, stems: list[str]):
    """Upload results to Google Drive via rclone.

    Config keys:
      - drive_enabled (bool)
      - drive_remote (str) e.g. palette_drive
      - drive_mode (full_folder|pdf_only)

    Called after processing a batch of pairs (so we only upload when everything is finished).
    """

    if not cfg.get("drive_enabled", False):
        return

    remote = cfg.get("drive_remote", "palette_drive")
    mode = (cfg.get("drive_mode", "full_folder") or "full_folder").lower()
    out_root = cfg["output_dir"]

    if mode not in ("full_folder", "pdf_only"):
        print(f"⚠️ drive: unknown mode={mode}; expected full_folder|pdf_only")
        return

    for stem in stems:
        # skip unchanged uploads
        stem_dir = os.path.join(out_root, stem)
        sig_now = folder_signature(stem_dir)
        st = load_drive_state(stem_dir)
        sig_last = float(st.get("folder_mtime", 0.0) or 0.0)
        if sig_now <= sig_last:
            print(f"☁️ drive upload: skip unchanged {stem}")
            continue
        if mode == "pdf_only":
            src = os.path.join(out_root, stem, f"{stem}_report.pdf")
            dest = f"{remote}:"
            cmd = ["rclone", "copy", src, dest]
        else:
            src = os.path.join(out_root, stem)
            dest = f"{remote}:{stem}"
            cmd = ["rclone", "copy", src, dest]

        print("☁️ drive upload:", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print("❌ drive upload failed:", stem)
            if res.stdout:
                print(res.stdout.strip())
            if res.stderr:
                print(res.stderr.strip())
        else:
            print("✅ drive upload ok:", stem)
            save_drive_state(stem_dir, {"folder_mtime": sig_now, "ts": time.time()})


def find_ready_pairs(input_dir, marked_suffix="_x", require_pairs=True):
    files = [f for f in os.listdir(input_dir) if f.lower().endswith(IMG_EXTS)]
    lower_map = {f.lower(): f for f in files}

    pairs = []
    seen_stems = set()

    for f in files:
        name, ext = os.path.splitext(f)
        if name.endswith(marked_suffix):
            continue

        stem = name
        if stem in seen_stems:
            continue

        marked_candidate_same = f"{stem}{marked_suffix}{ext}"
        marked_path = None
        if marked_candidate_same.lower() in lower_map:
            marked_path = os.path.join(input_dir, lower_map[marked_candidate_same.lower()])
        else:
            for e in IMG_EXTS:
                cand = f"{stem}{marked_suffix}{e}"
                if cand.lower() in lower_map:
                    marked_path = os.path.join(input_dir, lower_map[cand.lower()])
                    break

        orig_path = os.path.join(input_dir, f)

        if require_pairs:
            if marked_path and os.path.exists(orig_path):
                pairs.append((stem, orig_path, marked_path))
                seen_stems.add(stem)
        else:
            pairs.append((stem, orig_path, marked_path))
            seen_stems.add(stem)

    return sorted(pairs, key=lambda x: x[0].lower())


def move_processed_inputs(pair, archive_dir):
    safe_makedirs(archive_dir)
    stem, orig_path, marked_path = pair

    for p in [orig_path, marked_path]:
        if p and os.path.exists(p):
            dest = os.path.join(archive_dir, os.path.basename(p))
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base}_{now_stamp()}{ext}"
            shutil.move(p, dest)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.environ.get("CONFIG_PATH", os.path.join(script_dir, "color_palette_config.ini"))
    cfg = read_config(cfg_path)

    # Optional: override pigments from config.ini so the worker matches the GUI.
    paints = load_paints_from_ini(cfg_path)
    if paints:
        global PIGMENTS_HEX, CUSTOM_PIGMENT_COLORS
        PIGMENTS_HEX = paints
        # default chip colors to the same hex values
        CUSTOM_PIGMENT_COLORS = dict(paints)
        print(f"🎨 Loaded {len(paints)} paint(s) from [paints] in config.")

    input_dir = cfg["input_dir"]
    output_dir = cfg["output_dir"]
    safe_makedirs(input_dir)
    safe_makedirs(output_dir)

    archive_dir = resolve_archive_dir(cfg)
    print(f"Archiving processed inputs to: {archive_dir}")

    print("=== Watcher started ===")
    print(f"Input:   {input_dir}")
    print(f"Output:  {output_dir}")
    print(f"Poll:    {cfg['poll_seconds']} seconds")
    print(f"Suffix:  {cfg['marked_suffix']}  (example: image_x.jpg)")
    print("V2: per-base output folder + per-base PDF: <output_dir>/<base>/<base>_report.pdf")
    print(f"V2 mix: ΔE(Lab), step={cfg['mix_step_pct']}%, max_pigments={cfg['max_pigments']}, black_fallback={cfg['allow_black_fallback']}")

    while True:
        try:
            pairs = find_ready_pairs(
                input_dir=input_dir,
                marked_suffix=cfg["marked_suffix"],
                require_pairs=cfg["require_pairs"],
            )

            if not pairs:
                time.sleep(cfg["poll_seconds"])
                continue

            print(f"\n📦 Found {len(pairs)} pair(s).")

            processed_stems = []
            for stem, orig_path, marked_path in pairs:
                print(f"🔧 Processing: {stem}")
                report_path = process_one_pair(stem, orig_path, marked_path, cfg)
                print(f"✅ Report written: {report_path}")

                move_processed_inputs((stem, orig_path, marked_path), archive_dir)
                processed_stems.append(stem)

            # after this batch, optionally upload to Drive
            if processed_stems:
                drive_upload(cfg, processed_stems)

        except Exception as e:
            print(f"❌ Error: {e}")

        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
