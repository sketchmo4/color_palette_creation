import os
import re
import time
import configparser
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

import numpy as np
from matplotlib import colors as mcolors
from skimage.color import rgb2lab, lab2rgb
from scipy.optimize import minimize

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

IN_DIR = Path(os.environ.get("IN_DIR", "/mnt/in"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/mnt/out"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/color_palette_config.ini"))

SAFE_BASE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
SAFE_MASK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")



def rgb01_to_lab(rgb01: np.ndarray) -> np.ndarray:
    lab = rgb2lab(rgb01.reshape((1, 1, 3)))
    return lab.reshape((3,))


def deltae76(lab1: np.ndarray, lab2: np.ndarray) -> float:
    return float(np.linalg.norm(lab1 - lab2))


def mix_rgb01(weights: np.ndarray, pigment_rgbs: np.ndarray) -> np.ndarray:
    return np.clip(np.sum(weights.reshape((-1, 1)) * pigment_rgbs, axis=0), 0, 1)


def round_weights_to_step(weights: np.ndarray, step_pct: float) -> np.ndarray:
    step = step_pct / 100.0
    w = np.clip(weights, 0, 1)
    if float(np.sum(w)) <= 0:
        w = np.ones_like(w) / len(w)
    w = w / float(np.sum(w))

    if step > 0:
        q = np.round(w / step) * step
        if float(np.sum(q)) == 0:
            q[int(np.argmax(w))] = step
        total = float(np.sum(q))
        diff = 1.0 - total
        j = int(np.argmax(q))
        q[j] = np.clip(q[j] + diff, 0, 1)
        q = np.clip(q, 0, 1)
        s = float(np.sum(q))
        q = (q / s) if s > 0 else (np.ones_like(q) / len(q))
        return q

    return w


def normalize_hex(h: str) -> str:
    h = (h or '').strip()
    if not h:
        raise ValueError('empty hex')
    if not h.startswith('#'):
        h = '#' + h
    if re.match(r'^#[0-9a-fA-F]{3}$', h):
        h = '#' + ''.join([c * 2 for c in h[1:]])
    if not re.match(r'^#[0-9a-fA-F]{6}$', h):
        raise ValueError(f'invalid hex: {h}')
    return h.lower()


def combine_hexes_to_base(hex_codes: List[str], method: str = 'lab_mean') -> str:
    hex_norm = [normalize_hex(h) for h in hex_codes if (h or '').strip()]
    if not hex_norm:
        raise ValueError('need at least one hex')
    rgbs = np.array([mcolors.hex2color(h) for h in hex_norm], dtype=float)
    if method == 'lab_mean':
        labs = np.array([rgb01_to_lab(rgb) for rgb in rgbs], dtype=float)
        lab = np.mean(labs, axis=0)
        rgb = lab2rgb(lab.reshape((1, 1, 3))).reshape((3,))
        rgb = np.clip(rgb, 0, 1)
        return mcolors.to_hex(rgb)
    rgb = np.clip(np.mean(rgbs, axis=0), 0, 1)
    return mcolors.to_hex(rgb)


def solve_mix_for_target(target_hex: str, paints: Dict[str, str], step_pct: float, max_pigments: int) -> dict:
    target_rgb = np.array(mcolors.hex2color(target_hex), dtype=float)
    target_lab = rgb01_to_lab(target_rgb)

    pigment_items = [(k, np.array(mcolors.hex2color(v), dtype=float)) for k, v in paints.items()]

    best = None
    from itertools import combinations

    for k in range(2, min(max_pigments, len(pigment_items)) + 1):
        for combo in combinations(pigment_items, k):
            names = [x[0] for x in combo]
            rgbs = np.stack([x[1] for x in combo], axis=0)

            def obj(w):
                w = np.clip(np.array(w, dtype=float), 0, 1)
                s = float(np.sum(w))
                w = (w / s) if s > 0 else (np.ones((k,), dtype=float) / k)
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
            w = w / float(np.sum(w)) if float(np.sum(w)) > 0 else x0
            wq = round_weights_to_step(w, step_pct=step_pct)

            mixed_rgb = mix_rgb01(wq, rgbs)
            mixed_lab = rgb01_to_lab(mixed_rgb)
            de = deltae76(mixed_lab, target_lab)

            if best is None or de < best['deltaE']:
                best = {
                    'pigments': names,
                    'weights': wq,
                    'mixed_hex': mcolors.to_hex(mixed_rgb),
                    'deltaE': float(de),
                }

    if best is None:
        return {"color_percentages": {}, "mixed_hex": None, "deltaE": None}

    perc = {n: float(w * 100.0) for n, w in zip(best['pigments'], best['weights']) if w > 0}
    perc = dict(sorted(perc.items(), key=lambda kv: kv[1], reverse=True))
    return {"color_percentages": perc, "mixed_hex": best['mixed_hex'], "deltaE": best['deltaE']}

DEFAULT_PAINTS: Dict[str, str] = {
    "Titanium White": "#FFFFFF",
    "Yellow Ochre": "#DFAF2C",
    "Burnt Sienna": "#E97451",
    "Burnt Umber": "#8A3324",
    "Paynes Gray": "#536878",
    "Ivory Black": "#231F20",
}



def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text('utf-8'))
    except Exception:
        return {}


def compute_parts_from_local(local_perc: dict, target_perc: dict, total_parts: float = 20.0):
    """Return (start_local_parts, additions_parts, notes).

    We scale down the starting local pile so none of its pigment parts exceed the target.
    Then we only add pigments to reach the target mix.

    total_parts is the size of the final target pile in 'parts'.
    """
    # Normalize keys
    local = {str(k): float(v) for k,v in (local_perc or {}).items()}
    target = {str(k): float(v) for k,v in (target_perc or {}).items()}

    # Ensure all target pigments exist in local dict for ratios
    # Compute the maximum fraction of the local pile we can start with without overshooting any pigment.
    ratios = []
    for name, lpct in local.items():
        if lpct <= 0:
            continue
        tpct = float(target.get(name, 0.0))
        ratios.append(tpct / lpct)

    frac = min(ratios) if ratios else 0.0
    frac = max(0.0, min(1.0, frac))

    start_local_parts = total_parts * frac

    additions = {}
    for name, tpct in target.items():
        if tpct <= 0:
            continue
        t_parts = total_parts * (tpct / 100.0)
        l_parts = start_local_parts * (float(local.get(name, 0.0)) / 100.0)
        add = max(0.0, t_parts - l_parts)
        if add > 1e-6:
            additions[name] = add

    notes = []
    if frac < 1.0:
        notes.append(f"Start with {frac:.2f}× of the local mix (scaled down) to avoid overshooting some pigments.")
    if frac == 0.0:
        notes.append("Local mix has pigments not present in the target; starting local pile must be ~0 parts for a strict add-only path.")

    return start_local_parts, additions, notes

app = FastAPI(title="Color Palette Creation UI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def auto_base() -> str:
    return datetime.now().strftime("input_%Y%m%d_%H%M%S")




def read_ini() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH)
    return cfg


def write_ini(cfg: configparser.ConfigParser) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open('w', encoding='utf-8') as f:
        cfg.write(f)
def load_paints() -> Dict[str, str]:
    """Read paints from CONFIG_PATH if present, otherwise return defaults.

    Expected INI format:
      [paints]
      Titanium White = #FFFFFF
      ...

    Any invalid hex values are ignored.
    """
    paints = {}
    try:
        if CONFIG_PATH.exists():
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_PATH)
            if cfg.has_section("paints"):
                for name, hexv in cfg.items("paints"):
                    # configparser lowercases keys; preserve original-ish formatting
                    paint_name = name.strip()
                    v = hexv.strip()
                    if re.match(r"^#[0-9a-fA-F]{6}$", v):
                        paints[paint_name] = v.upper()
    except Exception:
        paints = {}

    return paints or DEFAULT_PAINTS


def safe_ext(filename: str) -> str:
    ext = Path(filename).suffix
    if not ext:
        raise HTTPException(400, "File must have an extension")
    return ext


def save_upload(dest: Path, up: UploadFile) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # stream to disk
    with dest.open("wb") as f:
        while True:
            chunk = up.file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    paints = load_paints()
    # keep stable order
    paint_items = sorted(paints.items(), key=lambda kv: kv[0].lower())
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "in_dir": str(IN_DIR),
            "out_dir": str(OUT_DIR),
            "config_path": str(CONFIG_PATH),
            "paint_items": paint_items,
        },
    )




@app.get("/paints", response_class=HTMLResponse)
def paints_page(request: Request):
    paints = load_paints()
    # preserve display order: if config has [paints], keep that file order; else alphabetical
    cfg = read_ini()
    items = []
    if cfg.has_section('paints'):
        for k, v in cfg.items('paints'):
            if re.match(r"^#[0-9a-fA-F]{6}$", str(v).strip()):
                items.append((k.strip(), str(v).strip().upper()))
    else:
        items = sorted(paints.items(), key=lambda kv: kv[0].lower())

    enabled = {}
    if cfg.has_section('paints.enabled'):
        for k, v in cfg.items('paints.enabled'):
            enabled[k.strip()] = str(v).strip().lower() in ('1','true','yes','on')

    return templates.TemplateResponse(request, 'paints.html', {
        'paint_items': items,
        'enabled': enabled,
        'config_path': str(CONFIG_PATH),
    })


@app.post("/paints")
def paints_save(
    request: Request,
    name: list[str] = Form(default=[]),
    hexv: list[str] = Form(default=[]),
    enabled: list[str] = Form(default=[]),
):
    # enabled list contains paint names that are checked
    checked = set(enabled)

    items = []
    for n, h in zip(name, hexv):
        n = (n or '').strip()
        h = (h or '').strip().upper()
        if not n:
            continue
        if not re.match(r"^#[0-9A-F]{6}$", h):
            raise HTTPException(400, f"Invalid hex for '{n}': {h}")
        items.append((n, h))

    if len(items) < 2:
        raise HTTPException(400, 'Need at least 2 paints enabled/defined')

    cfg = read_ini()
    if cfg.has_section('paints'):
        cfg.remove_section('paints')
    if cfg.has_section('paints.enabled'):
        cfg.remove_section('paints.enabled')

    cfg.add_section('paints')
    for n, h in items:
        cfg.set('paints', n, h)

    cfg.add_section('paints.enabled')
    for n, _h in items:
        cfg.set('paints.enabled', n, 'true' if n in checked else 'false')

    write_ini(cfg)

    return RedirectResponse(url='/paints', status_code=303)

@app.post("/upload")
def upload(
    base: Optional[str] = Form(default=None),
    original: UploadFile = File(...),
    marked: UploadFile = File(...),
    mask_type: str = Form(default="general"),
    custom_mask: str = Form(default=""),
):
    b = (base or "").strip()
    if b:
        if not SAFE_BASE_RE.match(b):
            raise HTTPException(400, "Base name must be 1-64 chars: letters/numbers/_/-")
    else:
        b = auto_base()

    ext_o = safe_ext(original.filename or "")
    ext_m = safe_ext(marked.filename or "")

    # If extensions differ, keep each extension (script can handle), but warn by using each ext.
    dest_orig = IN_DIR / f"{b}{ext_o}"
    dest_mark = IN_DIR / f"{b}_x{ext_m}"

    save_upload(dest_orig, original)
    save_upload(dest_mark, marked)

    # optional: sidecar metadata for the worker
    mt = (custom_mask or "").strip().lower() or (mask_type or "general").strip().lower()
    if not SAFE_MASK_RE.match(mt):
        mt = "general"
    meta_path = IN_DIR / f"{b}.meta.json"
    meta_path.write_text(json.dumps({"mask_type": mt}), encoding="utf-8")

    # Redirect to job page for status + auto-open
    return RedirectResponse(url=f"/jobs/{b}", status_code=303)




@app.get("/jobs/{base}", response_class=HTMLResponse)
def job_page(request: Request, base: str):
    if not SAFE_BASE_RE.match(base):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "job.html", {"base": base})


@app.get("/api/jobs/{base}/status")
def job_status(base: str):
    if not SAFE_BASE_RE.match(base):
        raise HTTPException(404)
    base_dir = OUT_DIR / base
    pdf = base_dir / f"{base}_report.pdf"
    drive_state = base_dir / ".drive_upload.json"

    charts_dir = base_dir / "charts"
    palettes_dir = base_dir / "palettes"

    charts = []
    if charts_dir.exists():
        charts = [p.name for p in charts_dir.iterdir() if p.is_file()]

    palettes = []
    if palettes_dir.exists():
        palettes = [p.name for p in palettes_dir.iterdir() if p.is_file()]

    uploaded = False
    if drive_state.exists():
        uploaded = True

    return {
        "base": base,
        "exists": base_dir.exists(),
        "pdf": pdf.exists(),
        "pdf_url": f"/runs/{base}/pdf" if pdf.exists() else None,
        "charts_count": len(charts),
        "palettes_count": len(palettes),
        "drive_uploaded": uploaded,
    }



@app.get("/local-color", response_class=HTMLResponse)
def local_color_page(request: Request):
    return templates.TemplateResponse(request, 'local_color.html', {
        'result': None,
        'error': None,
        'default_method': 'lab_mean',
    })


@app.post("/local-color", response_class=HTMLResponse)
def local_color_compute(
    request: Request,
    hexes: str = Form(default=''),
    method: str = Form(default='lab_mean'),
):
    try:
        hex_list = [h.strip() for h in (hexes or '').split(',') if h.strip()]
        base_hex = combine_hexes_to_base(hex_list, method=method)

        cfg = read_ini()
        # paints from config or default
        paints = load_paints()

        # enabled filtering
        if cfg.has_section('paints.enabled'):
            enabled = {k.strip(): str(v).strip().lower() in ('1','true','yes','on') for k,v in cfg.items('paints.enabled')}
            paints = {k:v for k,v in paints.items() if enabled.get(k, True)}

        step_pct = float(cfg.get('mix', 'step_pct', fallback='2.5')) if cfg.has_section('mix') else 2.5
        max_pigments = int(cfg.get('mix', 'max_pigments', fallback='4')) if cfg.has_section('mix') else 4

        mix = solve_mix_for_target(base_hex, paints=paints, step_pct=step_pct, max_pigments=max_pigments)

        result = {
            'input_hexes': hex_list,
            'base_hex': base_hex,
            'method': method,
            'mix': mix,
        }
        return templates.TemplateResponse(request, 'local_color.html', {
            'result': result,
            'error': None,
            'default_method': method,
            'hexes': ','.join(hex_list),
        })
    except Exception as e:
        return templates.TemplateResponse(request, 'local_color.html', {
            'result': None,
            'error': str(e),
            'default_method': method,
            'hexes': hexes,
        })



@app.get("/runs/{base}/skin-deltas", response_class=HTMLResponse)
def skin_deltas_page(request: Request, base: str):
    if not SAFE_BASE_RE.match(base):
        raise HTTPException(404)

    base_dir = OUT_DIR / base
    local_path = base_dir / f"{base}_local_skin.json"
    mix_path = base_dir / f"{base}_mix.json"

    local = load_json(local_path)
    mix = load_json(mix_path)

    local_mix = (local.get('mix') or {}).get('color_percentages') or {}

    rows = []
    entries = (mix.get('entries') or [])
    for i, entry in enumerate(entries, start=1):
        target_hex = entry.get('Hex Color')
        target_perc = entry.get('Color Percentages') or {}
        if not target_hex or not target_perc:
            continue

        start_local, adds, notes = compute_parts_from_local(local_mix, target_perc, total_parts=20.0)
        rows.append({
            'idx': i,
            'hex': target_hex,
            'start_local_parts': start_local,
            'adds': dict(sorted(adds.items(), key=lambda kv: kv[1], reverse=True)),
            'notes': notes,
        })

    return templates.TemplateResponse(request, 'skin_deltas.html', {
        'base': base,
        'local_path_exists': local_path.exists(),
        'mix_path_exists': mix_path.exists(),
        'local': local,
        'rows': rows,
    })


@app.get("/runs/{base}/local-skin-pie.png")
def local_skin_pie_png(base: str):
    if not SAFE_BASE_RE.match(base):
        raise HTTPException(404)

    base_dir = OUT_DIR / base
    local_path = base_dir / f"{base}_local_skin.json"
    local = load_json(local_path)
    perc = (local.get('mix') or {}).get('color_percentages') or {}
    if not perc:
        raise HTTPException(404, 'No local skin mix found')

    # Use paint colors from config for chips
    paints = load_paints()

    labels = list(perc.keys())
    sizes = list(perc.values())
    colors = [paints.get(l, '#cccccc') for l in labels]

    out_path = base_dir / f"{base}_local_skin_pie.png"

    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=140, textprops={'fontsize': 10})
    ax.axis('equal')
    fig.suptitle(f"{base} — Skin local mix", fontsize=14, weight='bold')
    fig.savefig(out_path, dpi=160, bbox_inches='tight')
    plt.close(fig)

    return FileResponse(str(out_path), media_type='image/png', filename=out_path.name)



@app.get("/runs/{base}/mask/{mask_type}/deltas", response_class=HTMLResponse)
def mask_deltas_page(request: Request, base: str, mask_type: str):
    if not SAFE_BASE_RE.match(base) or not SAFE_MASK_RE.match(mask_type):
        raise HTTPException(404)

    base_dir = OUT_DIR / base
    local_path = base_dir / f"{base}_local_{mask_type}.json"
    mix_path = base_dir / f"{base}_mix.json"

    local = load_json(local_path)
    mix = load_json(mix_path)

    local_mix = (local.get('mix') or {}).get('color_percentages') or {}

    rows = []
    entries = (mix.get('entries') or [])
    for i, entry in enumerate(entries, start=1):
        target_hex = entry.get('Hex Color')
        target_perc = entry.get('Color Percentages') or {}
        if not target_hex or not target_perc:
            continue

        start_local, adds, notes = compute_parts_from_local(local_mix, target_perc, total_parts=20.0)
        rows.append({
            'idx': i,
            'hex': target_hex,
            'start_local_parts': start_local,
            'adds': dict(sorted(adds.items(), key=lambda kv: kv[1], reverse=True)),
            'notes': notes,
        })

    return templates.TemplateResponse(request, 'mask_deltas.html', {
        'base': base,
        'mask_type': mask_type,
        'local_path_exists': local_path.exists(),
        'mix_path_exists': mix_path.exists(),
        'local': local,
        'rows': rows,
    })


@app.get("/runs/{base}/mask/{mask_type}/local-pie.png")
def mask_local_pie_png(base: str, mask_type: str):
    if not SAFE_BASE_RE.match(base) or not SAFE_MASK_RE.match(mask_type):
        raise HTTPException(404)

    base_dir = OUT_DIR / base
    local_path = base_dir / f"{base}_local_{mask_type}.json"
    local = load_json(local_path)
    perc = (local.get('mix') or {}).get('color_percentages') or {}
    if not perc:
        raise HTTPException(404, f'No local {mask_type} mix found')

    paints = load_paints()
    labels = list(perc.keys())
    sizes = list(perc.values())
    colors = [paints.get(l, '#cccccc') for l in labels]

    out_path = base_dir / f"{base}_local_{mask_type}_pie.png"

    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=140, textprops={'fontsize': 10})
    ax.axis('equal')
    fig.suptitle(f"{base} — {mask_type} local mix", fontsize=14, weight='bold')
    fig.savefig(out_path, dpi=160, bbox_inches='tight')
    plt.close(fig)

    return FileResponse(str(out_path), media_type='image/png', filename=out_path.name)

@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    bases = []
    if OUT_DIR.exists():
        for p in OUT_DIR.iterdir():
            if p.is_dir():
                pdf = p / f"{p.name}_report.pdf"
                bases.append(
                    {
                        "base": p.name,
                        "pdf": pdf.name if pdf.exists() else None,
                        "mtime": p.stat().st_mtime,
                    }
                )
    bases.sort(key=lambda x: x["mtime"], reverse=True)
    return templates.TemplateResponse(request, "runs.html", {"bases": bases})


@app.get("/runs/{base}/pdf")
def run_pdf(base: str):
    if not SAFE_BASE_RE.match(base):
        raise HTTPException(404)
    pdf = OUT_DIR / base / f"{base}_report.pdf"
    if not pdf.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(str(pdf), media_type="application/pdf", filename=pdf.name)
