import os
import time
import json
import base64
import random
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HORDE_KEY = os.getenv("HORDE_KEY", "").strip() or "0000000000"  # anonymous allowed but lowest priority

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Put it in .env as BOT_TOKEN=...")

# Pollinations (fast)
POLL_IMAGE = "https://image.pollinations.ai/prompt/"
POLL_TEXT = "https://text.pollinations.ai/"
POLL_MODELS_URL = "https://image.pollinations.ai/models"

# AI Horde (free, can be slower due to queue)
HORDE_BASE = "https://aihorde.net/api"
HORDE_ASYNC = f"{HORDE_BASE}/v2/generate/async"
HORDE_CHECK = f"{HORDE_BASE}/v2/generate/check"
HORDE_STATUS = f"{HORDE_BASE}/v2/generate/status"
HORDE_MODELS = f"{HORDE_BASE}/v2/status/models"  # list active models (huge) :contentReference[oaicite:2]{index=2}

CLIENT_AGENT = "tg-image-bot:1.0 (free)"

MENU = ReplyKeyboardMarkup(
    [["🎨 Generate", "⚙️ Settings", "ℹ️ Help"]],
    resize_keyboard=True,
    is_persistent=True,
)

WAIT_PROMPT = 1

STYLE_PRESETS = {
    "none": "",
    "realistic": "photorealistic, natural lighting, high detail, 35mm, sharp focus",
    "anime": "anime style, clean lineart, vibrant colors, studio quality, detailed background",
    "logo": "minimal logo, vector style, flat design, clean geometry, centered composition",
    "pixel": "pixel art, 16-bit, retro game style, limited palette, crisp pixels",
}

PROVIDERS = ["pollinations", "aihorde"]


@dataclass
class UserSettings:
    width: int = 1024
    height: int = 1024
    style: str = "none"
    provider: str = "pollinations"   # pollinations | aihorde
    model: str | None = None         # pollinations model (optional)
    horde_model: str | None = None   # aihorde model name (optional)


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> UserSettings:
    if "settings" not in context.user_data:
        context.user_data["settings"] = UserSettings().__dict__
    return UserSettings(**context.user_data["settings"])


def save_settings(context: ContextTypes.DEFAULT_TYPE, s: UserSettings) -> None:
    context.user_data["settings"] = s.__dict__


def settings_text(s: UserSettings) -> str:
    return (
        "⚙️ Settings\n\n"
        f"• Provider: {s.provider}\n"
        f"• Size: {s.width}×{s.height}\n"
        f"• Style: {s.style}\n"
        f"• Pollinations model: {s.model or 'default'}\n"
        f"• AI Horde model: {s.horde_model or 'auto'}\n"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Provider", callback_data="set:provider")],
        [InlineKeyboardButton("📐 Size", callback_data="set:size")],
        [InlineKeyboardButton("🎭 Style", callback_data="set:style")],
        [InlineKeyboardButton("🧩 Pollinations Model", callback_data="set:poll_model")],
        [InlineKeyboardButton("🧩 AI Horde Model", callback_data="set:horde_model")],
    ])


def provider_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for p in PROVIDERS:
        label = ("✅ " if p == current else "") + p
        rows.append([InlineKeyboardButton(label, callback_data=f"provider:{p}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])
    return InlineKeyboardMarkup(rows)


def size_keyboard(w: int, h: int) -> InlineKeyboardMarkup:
    presets = [(512, 512), (768, 768), (1024, 1024), (1024, 768), (768, 1024)]
    rows, row = [], []
    for pw, ph in presets:
        label = f"{pw}×{ph}"
        if (pw, ph) == (w, h):
            label = "✅ " + label
        row.append(InlineKeyboardButton(label, callback_data=f"size:{pw}x{ph}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])
    return InlineKeyboardMarkup(rows)


def style_keyboard(current: str) -> InlineKeyboardMarkup:
    keys = ["none", "realistic", "anime", "logo", "pixel"]
    rows = []
    for i in range(0, len(keys), 2):
        r = []
        for k in keys[i:i+2]:
            label = ("✅ " if k == current else "") + k
            r.append(InlineKeyboardButton(label, callback_data=f"style:{k}"))
        rows.append(r)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])
    return InlineKeyboardMarkup(rows)


def gen_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Generate", callback_data="gen:go"),
         InlineKeyboardButton("✨ Enhance", callback_data="gen:enhance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="gen:cancel")],
    ])


def after_send_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Regenerate", callback_data="gen:regen")]])


def build_prompt(user_prompt: str, s: UserSettings) -> str:
    style_suffix = STYLE_PRESETS.get(s.style, "")
    return f"{user_prompt}, {style_suffix}".strip(", ") if style_suffix else user_prompt


def build_pollinations_url(prompt: str, s: UserSettings) -> str:
    encoded = quote(prompt, safe="")
    base = f"{POLL_IMAGE}{encoded}"
    q = [
        f"width={max(16, min(s.width, 2048))}",
        f"height={max(16, min(s.height, 2048))}",
        f"seed={random.randint(1, 2_000_000_000)}",
        "nologo=true",
    ]
    if s.model:
        q.append(f"model={quote(s.model, safe='')}")
    return base + "?" + "&".join(q)


async def enhance_prompt(prompt: str) -> str | None:
    try:
        instruction = (
            "Rewrite this into a strong text-to-image prompt. "
            "Add useful visual details, lighting, camera/composition, but keep it concise. "
            f"Prompt: {prompt}"
        )
        url = f"{POLL_TEXT}{quote(instruction, safe='')}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return (await resp.text()).strip()
    except Exception:
        return None


async def fetch_poll_models() -> list[str] | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(POLL_MODELS_URL) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception:
        return None


async def fetch_horde_models() -> list[str] | None:
    # Output is large; we show only first ~20 in UI
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(HORDE_MODELS) as resp:
                resp.raise_for_status()
                data = await resp.json()

        names: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    for k in ("name", "model", "model_name", "id"):
                        if item.get(k):
                            names.append(str(item[k]))
                            break
        return [n for n in names if n][:200]
    except Exception:
        return None


async def aihorde_generate_image(prompt: str, s: UserSettings) -> bytes:
    """
    AI Horde flow:
      1) POST /v2/generate/async -> returns id
      2) poll GET /v2/generate/check/{id} until done
      3) GET /v2/generate/status/{id} -> generations[0].img (base64 or url)
    stablehordeapi-py docs show this structure. :contentReference[oaicite:3]{index=3}
    """
    headers = {"apikey": HORDE_KEY, "Client-Agent": CLIENT_AGENT}
    payload = {
        "prompt": prompt,
        "params": {
            "width": max(64, min(s.width, 2048)),
            "height": max(64, min(s.height, 2048)),
            "steps": 25,
            "cfg_scale": 7,
            "sampler_name": "k_euler_a",
        },
        "nsfw": False,
        "censor_nsfw": True,
        "n": 1,
    }
    if s.horde_model:
        payload["models"] = [s.horde_model]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        async with session.post(HORDE_ASYNC, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            r = await resp.json()
            gen_id = r.get("id")
            if not gen_id:
                raise RuntimeError(f"AI Horde bad response: {r}")

        # poll
        deadline = time.time() + 160
        while True:
            if time.time() > deadline:
                raise TimeoutError("AI Horde queue timeout. Try again or switch to Pollinations (fast).")

            async with session.get(f"{HORDE_CHECK}/{gen_id}", headers=headers) as resp:
                resp.raise_for_status()
                chk = await resp.json()

            done_val = chk.get("done")
            done = (done_val is True) or (done_val == 1) or (done_val == "1")

            if done:
                break
            await asyncio_sleep(1.2)

        async with session.get(f"{HORDE_STATUS}/{gen_id}", headers=headers) as resp:
            resp.raise_for_status()
            st = await resp.json()

        gens = st.get("generations") or []
        if not gens:
            raise RuntimeError(f"No generations returned: {st}")

        img_field = gens[0].get("img")
        if not img_field:
            raise RuntimeError(f"Missing image field: {gens[0]}")

        # Sometimes it's base64, sometimes it's a URL (if r2 is enabled in some clients). :contentReference[oaicite:4]{index=4}
        if isinstance(img_field, str) and img_field.startswith("http"):
            async with session.get(img_field) as rimg:
                rimg.raise_for_status()
                return await rimg.read()

        return base64.b64decode(img_field.encode("utf-8"))


async def asyncio_sleep(sec: float) -> None:
    # tiny helper (keeps imports minimal)
    import asyncio
    await asyncio.sleep(sec)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings(context)
    save_settings(context, s)
    await update.message.reply_text(
        "Hi! Use the buttons.\n\n"
        "🎨 Generate → send prompt → tap Generate\n"
        "⚙️ Settings → provider/size/style\n",
        reply_markup=MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ Help\n\n"
        "• Tap 🎨 Generate\n"
        "• Send prompt\n"
        "• Tap ✅ Generate (or ✨ Enhance)\n\n"
        "Tip: Provider=Pollinations is fastest. AI Horde is free but can queue.",
        reply_markup=MENU,
    )


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    txt = (update.message.text or "").strip()

    if txt == "🎨 Generate":
        await update.message.reply_text("📝 Send your prompt now.", reply_markup=MENU)
        return WAIT_PROMPT

    if txt == "⚙️ Settings":
        s = get_settings(context)
        await update.message.reply_text(settings_text(s), reply_markup=MENU)
        await update.message.reply_text("Change settings:", reply_markup=settings_keyboard())
        return ConversationHandler.END

    if txt == "ℹ️ Help":
        await help_cmd(update, context)
        return ConversationHandler.END

    # if user just types prompt
    context.user_data["pending_prompt"] = txt
    await update.message.reply_text("Got it. Generate or enhance?", reply_markup=gen_action_keyboard())
    return ConversationHandler.END


async def prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text("Send a text prompt 🙂", reply_markup=MENU)
        return WAIT_PROMPT

    context.user_data["pending_prompt"] = prompt
    await update.message.reply_text("Prompt saved. Generate or enhance?", reply_markup=gen_action_keyboard())
    return ConversationHandler.END


async def gen_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data

    if action == "gen:cancel":
        context.user_data.pop("pending_prompt", None)
        await q.edit_message_text("Cancelled.")
        return

    if action == "gen:regen":
        prompt = context.user_data.get("last_prompt")
        if not prompt:
            await q.edit_message_text("No previous prompt. Tap 🎨 Generate.")
            return
        context.user_data["pending_prompt"] = prompt
        action = "gen:go"

    prompt = context.user_data.get("pending_prompt")
    if not prompt:
        await q.edit_message_text("No prompt found. Tap 🎨 Generate.")
        return

    if action == "gen:enhance":
        await q.edit_message_text("✨ Enhancing prompt...")
        improved = await enhance_prompt(prompt)
        if not improved:
            await q.edit_message_text("Couldn’t enhance right now. Try ✅ Generate.")
            return
        context.user_data["pending_prompt"] = improved
        await q.edit_message_text(
            f"✨ Enhanced prompt:\n\n{improved}\n\nNow tap ✅ Generate.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Generate", callback_data="gen:go")],
                [InlineKeyboardButton("❌ Cancel", callback_data="gen:cancel")],
            ])
        )
        return

    if action == "gen:go":
        s = get_settings(context)
        final_prompt = build_prompt(prompt, s)
        context.user_data["last_prompt"] = prompt

        chat = q.message.chat
        await q.edit_message_text(f"🎨 Generating… ({s.provider})")
        await chat.send_action(ChatAction.UPLOAD_PHOTO)

        try:
            if s.provider == "pollinations":
                url = build_pollinations_url(final_prompt, s)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        img = await resp.read()
                caption = f"✅ Pollinations\nPrompt: {prompt}\nStyle: {s.style} | {s.width}×{s.height}"
                await chat.send_photo(photo=img, caption=caption, reply_markup=after_send_keyboard())

            else:
                img = await aihorde_generate_image(final_prompt, s)
                caption = f"✅ AI Horde\nPrompt: {prompt}\nStyle: {s.style} | {s.width}×{s.height}"
                await chat.send_photo(photo=img, caption=caption, reply_markup=after_send_keyboard())

        except Exception as e:
            await chat.send_message(
                "❌ Generation failed.\n"
                f"Error: {e}\n\n"
                "Try:\n• switch Provider (Settings)\n• smaller size\n• simpler prompt"
            )
        return


async def settings_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    s = get_settings(context)

    if q.data == "set:back":
        await q.edit_message_text("Change settings:", reply_markup=settings_keyboard())
        return

    if q.data == "set:provider":
        await q.edit_message_text("🧠 Choose provider:", reply_markup=provider_keyboard(s.provider))
        return

    if q.data.startswith("provider:"):
        s.provider = q.data.split(":", 1)[1]
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    if q.data == "set:size":
        await q.edit_message_text("📐 Choose size:", reply_markup=size_keyboard(s.width, s.height))
        return

    if q.data.startswith("size:"):
        w_str, h_str = q.data.split(":", 1)[1].split("x", 1)
        s.width, s.height = int(w_str), int(h_str)
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    if q.data == "set:style":
        await q.edit_message_text("🎭 Choose style:", reply_markup=style_keyboard(s.style))
        return

    if q.data.startswith("style:"):
        s.style = q.data.split(":", 1)[1]
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    if q.data == "set:poll_model":
        await q.edit_message_text("🧩 Loading Pollinations models…")
        models = await fetch_poll_models()
        if not models:
            await q.edit_message_text("Couldn’t load Pollinations models.", reply_markup=settings_keyboard())
            return
        top = models[:18]
        rows, row = [], []
        for m in top:
            label = ("✅ " if s.model == m else "") + m
            row.append(InlineKeyboardButton(label[:30], callback_data=f"pollmodel:{m}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(("✅ default" if s.model is None else "default"),
                                          callback_data="pollmodel:__default__")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])
        await q.edit_message_text("🧩 Choose Pollinations model:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if q.data.startswith("pollmodel:"):
        m = q.data.split(":", 1)[1]
        s.model = None if m == "__default__" else m
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    if q.data == "set:horde_model":
        await q.edit_message_text("🧩 Loading AI Horde models… (list is big)")
        models = await fetch_horde_models()
        if not models:
            await q.edit_message_text("Couldn’t load AI Horde models.", reply_markup=settings_keyboard())
            return
        top = models[:18]
        rows, row = [], []
        for m in top:
            label = ("✅ " if s.horde_model == m else "") + m
            row.append(InlineKeyboardButton(label[:30], callback_data=f"hordemodel:{m}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(("✅ auto" if s.horde_model is None else "auto"),
                                          callback_data="hordemodel:__auto__")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])
        await q.edit_message_text("🧩 Choose AI Horde model:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if q.data.startswith("hordemodel:"):
        m = q.data.split(":", 1)[1]
        s.horde_model = None if m == "__auto__" else m
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(gen_callbacks, pattern=r"^gen:"))
    app.add_handler(CallbackQueryHandler(settings_callbacks, pattern=r"^(set:|provider:|size:|style:|pollmodel:|hordemodel:)"))

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
        states={WAIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_received)]},
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
