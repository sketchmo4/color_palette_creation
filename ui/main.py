import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

IN_DIR = Path(os.environ.get("IN_DIR", "/mnt/in"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/mnt/out"))

SAFE_BASE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

app = FastAPI(title="Color Palette Creation UI")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def auto_base() -> str:
    return datetime.now().strftime("input_%Y%m%d_%H%M%S")


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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "in_dir": str(IN_DIR),
            "out_dir": str(OUT_DIR),
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
