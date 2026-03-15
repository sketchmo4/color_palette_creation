import os
import re
import time
import configparser
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

IN_DIR = Path(os.environ.get("IN_DIR", "/mnt/in"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/mnt/out"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/color_palette_config.ini"))

SAFE_BASE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

DEFAULT_PAINTS: Dict[str, str] = {
    "Titanium White": "#FFFFFF",
    "Yellow Ochre": "#DFAF2C",
    "Burnt Sienna": "#E97451",
    "Burnt Umber": "#8A3324",
    "Paynes Gray": "#536878",
    "Ivory Black": "#231F20",
}

app = FastAPI(title="Color Palette Creation UI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def auto_base() -> str:
    return datetime.now().strftime("input_%Y%m%d_%H%M%S")


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


@app.post("/upload")
def upload(
    base: Optional[str] = Form(default=None),
    original: UploadFile = File(...),
    marked: UploadFile = File(...),
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

    return {"ok": True, "base": b, "original": str(dest_orig), "marked": str(dest_mark)}


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
