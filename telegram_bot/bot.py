import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
IN_DIR = Path(os.environ.get("IN_DIR", "/mnt/in"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/mnt/out"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/state.json"))

SAFE_BASE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def auto_base() -> str:
    return datetime.now().strftime("input_%Y%m%d_%H%M%S")


def load_state() -> Dict[str, dict]:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


@dataclass
class Pending:
    base: str
    step: str  # 'orig' or 'marked'
    orig_ext: Optional[str] = None


def get_pending(chat_id: int) -> Optional[Pending]:
    st = load_state()
    item = st.get(str(chat_id))
    if not item:
        return None
    return Pending(**item)


def set_pending(chat_id: int, p: Optional[Pending]) -> None:
    st = load_state()
    if p is None:
        st.pop(str(chat_id), None)
    else:
        st[str(chat_id)] = {
            "base": p.base,
            "step": p.step,
            "orig_ext": p.orig_ext,
        }
    save_state(st)


async def cmd_palette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    base = None
    if context.args:
        base = context.args[0].strip()
        if not SAFE_BASE_RE.match(base):
            await update.message.reply_text(
                "Base name must be 1-64 chars: letters/numbers/_/-. Example: /palette example"
            )
            return
    else:
        base = auto_base()

    set_pending(chat_id, Pending(base=base, step="orig", orig_ext=None))
    await update.message.reply_text(
        f"Palette intake started. Base: {base}\n\nSend ORIGINAL image (as a *document* preferred).",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_pending(chat_id, None)
    await update.message.reply_text("Cancelled. Send /palette to start again.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_pending(chat_id)
    if not p:
        await update.message.reply_text("No active intake. Send /palette to start.")
        return
    await update.message.reply_text(f"Pending base={p.base} step={p.step}")


def _ext_from_filename(name: Optional[str]) -> str:
    if not name:
        return ".jpg"
    ext = Path(name).suffix.lower()
    return ext if ext else ".jpg"


async def _download_to(update: Update, context: ContextTypes.DEFAULT_TYPE, dest: Path) -> None:
    msg = update.message
    doc = msg.document
    if doc:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
        return
    if msg.photo:
        # pick largest
        ph = msg.photo[-1]
        tg_file = await context.bot.get_file(ph.file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
        return
    raise ValueError("No supported attachment")


async def _watch_and_notify(chat_id: int, base: str, context: ContextTypes.DEFAULT_TYPE):
    report = OUT_DIR / base / f"{base}_report.pdf"
    drive_state = OUT_DIR / base / ".drive_upload.json"

    # Wait for report
    t0 = time.time()
    while time.time() - t0 < 60 * 30:
        if report.exists():
            break
        await asyncio.sleep(2)

    if not report.exists():
        await context.bot.send_message(chat_id, f"⚠️ Timed out waiting for report for {base}.")
        return

    # Wait for drive upload state if drive is enabled; we can't reliably know enabled here,
    # so we watch for the state file for a while.
    t1 = time.time()
    while time.time() - t1 < 60 * 30:
        if drive_state.exists():
            try:
                data = json.loads(drive_state.read_text("utf-8"))
                ts = data.get("ts")
                if ts:
                    dt = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
                    await context.bot.send_message(chat_id, f"✅ {base} processed and uploaded to Drive ({dt}).")
                else:
                    await context.bot.send_message(chat_id, f"✅ {base} processed and uploaded to Drive.")
            except Exception:
                await context.bot.send_message(chat_id, f"✅ {base} processed and Drive upload state detected.")
            return
        await asyncio.sleep(3)

    # If no drive state appears, still notify processing done.
    await context.bot.send_message(
        chat_id,
        f"✅ {base} processed. (No Drive upload confirmation seen yet — check drive settings/logs.)",
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = get_pending(chat_id)
    if not p:
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # Determine extension from filename if document; otherwise default to .jpg
    filename = None
    if update.message.document:
        filename = update.message.document.file_name
    ext = _ext_from_filename(filename)

    IN_DIR.mkdir(parents=True, exist_ok=True)

    if p.step == "orig":
        dest_final = IN_DIR / f"{p.base}{ext}"
        dest_tmp = IN_DIR / f".{p.base}{ext}.tmp"
        await _download_to(update, context, dest_tmp)
        dest_tmp.replace(dest_final)
        set_pending(chat_id, Pending(base=p.base, step="marked", orig_ext=ext))
        await update.message.reply_text(
            f"Got ORIGINAL. Now send MARKED image for base {p.base} (will be saved as {p.base}_x{ext})."
        )
        return

    if p.step == "marked":
        use_ext = p.orig_ext or ext
        dest_final = IN_DIR / f"{p.base}_x{use_ext}"
        dest_tmp = IN_DIR / f".{p.base}_x{use_ext}.tmp"
        await _download_to(update, context, dest_tmp)
        dest_tmp.replace(dest_final)
        set_pending(chat_id, None)
        await update.message.reply_text(
            f"✅ Queued: {p.base}\nI'll notify you when Drive upload is confirmed.")
        # background watcher
        asyncio.create_task(_watch_and_notify(chat_id, p.base, context))
        return


async def main():
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("palette", cmd_palette))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))

    app.add_handler(MessageHandler(filters.Document.IMAGE | filters.PHOTO, handle_media))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # run forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
