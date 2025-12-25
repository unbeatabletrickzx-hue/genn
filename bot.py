import os
import time
import json
import random
import asyncio
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("8386172336:AAH2vExx-jc_2HO9ddQqWOwulTQr_sBgiF0", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Put it in .env as BOT_TOKEN=...")

# Pollinations legacy endpoints (free/no key in most cases)
POLLINATIONS_IMAGE_BASE = "https://image.pollinations.ai/prompt/"
POLLINATIONS_MODELS_URL = "https://image.pollinations.ai/models"

# Simple per-user cooldown to avoid spam
COOLDOWN_SECONDS = 6
_last_used: dict[int, float] = {}

@dataclass
class ImgArgs:
    prompt: str
    width: int = 1024
    height: int = 1024
    seed: int | None = None
    model: str | None = None

def parse_img_args(text: str) -> ImgArgs | None:
    """
    Parses:
      "/img a cat --w 768 --h 768 --seed 42 --model flux"
    Also works if text is just a prompt (no /img).
    """
    if not text:
        return None

    parts = text.strip().split()
    if parts and parts[0].lower() == "/img":
        parts = parts[1:]

    if not parts:
        return None

    width, height, seed, model = 1024, 1024, None, None
    prompt_tokens = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok in ("--w", "--width") and i + 1 < len(parts):
            try:
                width = int(parts[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if tok in ("--h", "--height") and i + 1 < len(parts):
            try:
                height = int(parts[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if tok == "--seed" and i + 1 < len(parts):
            try:
                seed = int(parts[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if tok == "--model" and i + 1 < len(parts):
            model = parts[i + 1]
            i += 2
            continue

        prompt_tokens.append(tok)
        i += 1

    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        return None

    # Keep dimensions in a sensible range (Pollinations examples often recommend 16..2048-ish)
    width = max(16, min(width, 2048))
    height = max(16, min(height, 2048))

    return ImgArgs(prompt=prompt, width=width, height=height, seed=seed, model=model)

def build_pollinations_url(args: ImgArgs) -> str:
    # Prompt must be URL encoded
    encoded_prompt = quote(args.prompt, safe="")
    url = f"{POLLINATIONS_IMAGE_BASE}{encoded_prompt}"

    # Add query params
    q = []
    q.append(f"width={args.width}")
    q.append(f"height={args.height}")

    # Use a random seed if none provided (helps avoid always getting same cached image)
    seed = args.seed if args.seed is not None else random.randint(1, 2_000_000_000)
    q.append(f"seed={seed}")

    if args.model:
        q.append(f"model={quote(args.model, safe='')}")

    # Some implementations accept nologo; harmless if ignored
    q.append("nologo=true")

    return url + "?" + "&".join(q)

async def download_bytes(url: str, timeout_s: int = 60) -> bytes:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_used.get(user_id, 0.0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_used[user_id] = now
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a prompt and I’ll generate an image with Pollinations.\n\n"
        "Examples:\n"
        "• /img cyberpunk cat astronaut\n"
        "• /img watercolor sunset over mountains --w 768 --h 768\n"
        "• (or just send text) 'a dragon made of flowers'\n\n"
        "Extra:\n"
        "• /models → list available models"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)

async def models_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(POLLINATIONS_MODELS_URL) as resp:
                resp.raise_for_status()
                data = await resp.text()

        # Models endpoint may return JSON array; try to parse
        try:
            models = json.loads(data)
            if isinstance(models, list):
                msg = "Available models:\n" + "\n".join(f"• {m}" for m in models[:80])
                if len(models) > 80:
                    msg += f"\n… and {len(models)-80} more"
            else:
                msg = f"Models response:\n{data[:3500]}"
        except json.JSONDecodeError:
            msg = f"Models response:\n{data[:3500]}"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Couldn’t fetch models right now. Error: {e}")

async def img_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id and not cooldown_ok(user_id):
        await update.message.reply_text("Slow down a bit 🙂 Try again in a few seconds.")
        return

    text = update.message.text or ""
    args = parse_img_args(text)
    if not args:
        await update.message.reply_text("Usage: /img <prompt> [--w N --h N --seed N --model NAME]")
        return

    url = build_pollinations_url(args)

    await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)

    try:
        # Download and re-upload = more reliable than sending URL directly
        img_bytes = await download_bytes(url, timeout_s=90)
        caption = f"Prompt: {args.prompt}\n{args.width}×{args.height}" + (f" | model={args.model}" if args.model else "")
        await update.message.reply_photo(photo=img_bytes, caption=caption)
    except Exception as e:
        await update.message.reply_text(
            "Generation failed. Try a different prompt or smaller size.\n"
            f"Error: {e}\n"
            f"URL used: {url}"
        )

async def text_as_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Treat any normal text as a prompt
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id and not cooldown_ok(user_id):
        await update.message.reply_text("Slow down a bit 🙂 Try again in a few seconds.")
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    args = ImgArgs(prompt=text)
    url = build_pollinations_url(args)

    await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)

    try:
        img_bytes = await download_bytes(url, timeout_s=90)
        await update.message.reply_photo(photo=img_bytes, caption=f"Prompt: {args.prompt}")
    except Exception as e:
        await update.message.reply_text(f"Generation failed. Error: {e}")

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CommandHandler("img", img_cmd))

    # Any other text becomes a prompt
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_as_prompt))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
