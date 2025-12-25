import os
import time
import json
import random
import base64
from io import BytesIO
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
from PIL import Image
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
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
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Put it in .env as BOT_TOKEN=...")

# ---------- Providers ----------
# Pollinations (legacy URL style). Repo also shows pollinations.ai/p/<prompt> examples. :contentReference[oaicite:2]{index=2}
POLL_IMAGE_LEGACY = "https://image.pollinations.ai/prompt/"
POLL_IMAGE_ALT = "https://pollinations.ai/p/"  # fallback style shown in repo
POLL_MODELS_URL = "https://image.pollinations.ai/models"
POLL_TEXT_URL = "https://text.pollinations.ai/"

# Craiyon (free endpoint shown in Postman docs) :contentReference[oaicite:3]{index=3}
CRAIYON_GENERATE_URL = "https://backend.craiyon.com/generate"

# ---------- UX ----------
MENU = ReplyKeyboardMarkup(
    [["🎨 Generate", "⚙️ Settings", "ℹ️ Help"]],
    resize_keyboard=True,
    is_persistent=True,
)

# Conversation states
WAIT_PROMPT = 1
WAIT_ENHANCE_PROMPT = 2

# Basic cooldown (avoid spam / rate limit issues)
COOLDOWN_SECONDS = 6
_last_used: dict[int, float] = {}


@dataclass
class UserSettings:
    provider: str = "pollinations"  # "pollinations" | "craiyon"
    width: int = 1024
    height: int = 1024
    poll_model: str | None = None  # e.g. "flux"
    output_count: int = 1          # 1 or 4 (Pollinations always 1; Craiyon sends N from its 9 results)


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> UserSettings:
    d = context.user_data
    if "settings" not in d:
        d["settings"] = UserSettings().__dict__
    return UserSettings(**d["settings"])


def save_settings(context: ContextTypes.DEFAULT_TYPE, s: UserSettings) -> None:
    context.user_data["settings"] = s.__dict__


def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_used.get(user_id, 0.0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_used[user_id] = now
    return True


# ---------- Pollinations helpers ----------
def build_pollinations_url(prompt: str, s: UserSettings) -> str:
    encoded = quote(prompt, safe="")
    url = f"{POLL_IMAGE_LEGACY}{encoded}"
    q = [
        f"width={max(16, min(s.width, 2048))}",
        f"height={max(16, min(s.height, 2048))}",
        f"seed={random.randint(1, 2_000_000_000)}",
        "nologo=true",
    ]
    if s.poll_model:
        q.append(f"model={quote(s.poll_model, safe='')}")
    return url + "?" + "&".join(q)


async def fetch_pollinations_models() -> list[str] | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(POLL_MODELS_URL) as resp:
                resp.raise_for_status()
                data = await resp.text()
        parsed = json.loads(data)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        return None
    return None


async def enhance_prompt_with_pollinations(prompt: str) -> str | None:
    """
    Uses Pollinations text endpoint (simple URL) to rewrite prompt.
    """
    try:
        q = quote(
            "Rewrite this as a high-quality text-to-image prompt. "
            "Keep it concise, add important visual details, lighting, style, and composition. "
            f"Prompt: {prompt}",
            safe=""
        )
        url = f"{POLL_TEXT_URL}{q}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return (await resp.text()).strip()
    except Exception:
        return None


# ---------- Craiyon helpers ----------
async def craiyon_generate(prompt: str) -> list[bytes]:
    """
    Returns list of PNG bytes (converted from Craiyon base64/webp).
    """
    payload = {"prompt": prompt}
    timeout = aiohttp.ClientTimeout(total=180)  # can be slow
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(CRAIYON_GENERATE_URL, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

    images = data.get("images", [])
    out: list[bytes] = []

    for b64 in images:
        if not isinstance(b64, str):
            continue
        # Sometimes base64 may include prefix; be robust
        if "," in b64:
            b64 = b64.split(",")[-1]
        raw = base64.b64decode(b64)

        # Craiyon often outputs WEBP. Convert to PNG for Telegram photo upload.
        im = Image.open(BytesIO(raw))
        buf = BytesIO()
        im.convert("RGBA").save(buf, format="PNG")
        out.append(buf.getvalue())

    return out


# ---------- UI builders ----------
def settings_text(s: UserSettings) -> str:
    prov = "Pollinations" if s.provider == "pollinations" else "Craiyon"
    model = s.poll_model if s.poll_model else "default"
    return (
        "⚙️ Settings\n\n"
        f"• Provider: {prov}\n"
        f"• Size: {s.width}×{s.height}\n"
        f"• Pollinations model: {model}\n"
        f"• Output count (Craiyon): {s.output_count}\n"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Provider", callback_data="set:provider")],
        [InlineKeyboardButton("📐 Size", callback_data="set:size")],
        [InlineKeyboardButton("🧩 Pollinations model", callback_data="set:model")],
        [InlineKeyboardButton("🖼 Output count", callback_data="set:count")],
        [InlineKeyboardButton("⬅️ Back", callback_data="set:back")],
    ])


def provider_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ " if current == "pollinations" else "") + "Pollinations",
                                 callback_data="provider:pollinations"),
            InlineKeyboardButton(("✅ " if current == "craiyon" else "") + "Craiyon",
                                 callback_data="provider:craiyon"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="set:back")],
    ])


def size_keyboard(w: int, h: int) -> InlineKeyboardMarkup:
    presets = [
        (512, 512), (768, 768), (1024, 1024),
        (1024, 768), (768, 1024),
        (1536, 1024), (1024, 1536),
    ]
    rows = []
    row = []
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


def count_keyboard(current: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ " if current == 1 else "") + "1 image", callback_data="count:1"),
            InlineKeyboardButton(("✅ " if current == 4 else "") + "4 images", callback_data="count:4"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="set:back")],
    ])


def prompt_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Generate", callback_data="prompt:go"),
            InlineKeyboardButton("✨ Enhance", callback_data="prompt:enhance"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="prompt:cancel")],
    ])


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings(context)
    await update.message.reply_text(
        "Hi! Use the menu buttons below.\n\n"
        "🎨 Generate → type your prompt → tap Generate.\n"
        "⚙️ Settings → choose provider/size/model.\n",
        reply_markup=MENU
    )
    # Keep settings in sync
    save_settings(context, s)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ Help\n\n"
        "1) Tap 🎨 Generate\n"
        "2) Send your prompt (example: “cinematic cyberpunk cat, rainy street, neon reflections”)\n"
        "3) Tap ✅ Generate (or ✨ Enhance)\n\n"
        "Tips:\n"
        "• If Craiyon is slow, try Pollinations.\n"
        "• If you want specific look, set Pollinations model in Settings.\n",
        reply_markup=MENU
    )


async def on_menu_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    if user_id and not cooldown_ok(user_id):
        await update.message.reply_text("Slow down a bit 🙂 Try again in a few seconds.", reply_markup=MENU)
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 Send your prompt now.\n\nExample:\n"
        "“A futuristic Indian street market at night, neon lights, cinematic, ultra detailed”",
        reply_markup=MENU
    )
    return WAIT_PROMPT


async def on_menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_settings(context)
    await update.message.reply_text(settings_text(s), reply_markup=MENU)
    await update.message.reply_text("Choose what to change:", reply_markup=settings_keyboard())


async def on_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = (update.message.text or "").strip()
    if not prompt:
        await update.message.reply_text("Send a text prompt 🙂", reply_markup=MENU)
        return WAIT_PROMPT

    context.user_data["pending_prompt"] = prompt

    s = get_settings(context)
    prov = "Pollinations" if s.provider == "pollinations" else "Craiyon"
    await update.message.reply_text(
        f"Prompt saved.\n\nProvider: {prov}\nSize: {s.width}×{s.height}\n\nWhat next?",
        reply_markup=prompt_action_keyboard()
    )
    return ConversationHandler.END


async def on_prompt_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    action = q.data
    if action == "prompt:cancel":
        context.user_data.pop("pending_prompt", None)
        await q.edit_message_text("Cancelled.")
        return ConversationHandler.END

    prompt = context.user_data.get("pending_prompt")
    if not prompt:
        await q.edit_message_text("No prompt found. Tap 🎨 Generate again.")
        return ConversationHandler.END

    if action == "prompt:enhance":
        await q.edit_message_text("✨ Enhancing prompt...")
        enhanced = await enhance_prompt_with_pollinations(prompt)
        if not enhanced:
            await q.edit_message_text("Couldn’t enhance right now. Try again or generate directly.")
            return ConversationHandler.END

        context.user_data["pending_prompt"] = enhanced
        await q.edit_message_text(
            "✨ Enhanced prompt:\n\n"
            f"{enhanced}\n\n"
            "Tap ✅ Generate to create the image.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Generate", callback_data="prompt:go")],
                [InlineKeyboardButton("❌ Cancel", callback_data="prompt:cancel")],
            ])
        )
        return ConversationHandler.END

    if action == "prompt:go":
        # Generate now
        s = get_settings(context)
        await q.edit_message_text("🎨 Generating...")

        chat = q.message.chat
        await chat.send_action(ChatAction.UPLOAD_PHOTO)

        try:
            if s.provider == "pollinations":
                url = build_pollinations_url(prompt, s)
                # Download bytes (re-upload to Telegram)
                timeout = aiohttp.ClientTimeout(total=90)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        img = await resp.read()

                caption = f"✅ Pollinations\n{prompt}\n{s.width}×{s.height}" + (f" | model={s.poll_model}" if s.poll_model else "")
                await chat.send_photo(photo=img, caption=caption)

            else:
                # Craiyon returns 9 images; we send 1 or 4 (your setting)
                imgs = await craiyon_generate(prompt)
                if not imgs:
                    raise RuntimeError("Craiyon returned no images")

                n = 1 if s.output_count <= 1 else 4
                chosen = imgs[:n]

                if len(chosen) == 1:
                    await chat.send_photo(photo=chosen[0], caption=f"✅ Craiyon\n{prompt}")
                else:
                    media = []
                    for i, b in enumerate(chosen):
                        bio = BytesIO(b)
                        bio.name = f"img{i}.png"
                        media.append(InputMediaPhoto(media=bio))
                    await chat.send_media_group(media=media)
                    await chat.send_message(f"✅ Craiyon\n{prompt}")

        except Exception as e:
            await chat.send_message(
                "❌ Generation failed.\n"
                f"Error: {e}\n\n"
                "Try:\n"
                "• switching provider in Settings\n"
                "• using a smaller size\n"
                "• re-trying with a simpler prompt"
            )

        return ConversationHandler.END

    return ConversationHandler.END


# ---------- Settings callbacks ----------
async def on_settings_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    s = get_settings(context)

    if q.data == "set:back":
        await q.edit_message_text("Choose what to change:", reply_markup=settings_keyboard())
        return

    if q.data == "set:provider":
        await q.edit_message_text("🧠 Choose provider:", reply_markup=provider_keyboard(s.provider))
        return

    if q.data == "set:size":
        await q.edit_message_text("📐 Choose size:", reply_markup=size_keyboard(s.width, s.height))
        return

    if q.data == "set:count":
        await q.edit_message_text("🖼 Craiyon output count:", reply_markup=count_keyboard(s.output_count))
        return

    if q.data == "set:model":
        # Show Pollinations models (fetch live if possible)
        await q.edit_message_text("🧩 Loading Pollinations models...")
        models = await fetch_pollinations_models()
        if not models:
            await q.edit_message_text(
                "Couldn’t load models right now.\n\n"
                "You can still use the default model, or try again later.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="set:back")]])
            )
            return

        # Keep it short in UI
        top = models[:18]
        rows = []
        row = []
        for m in top:
            label = ("✅ " if s.poll_model == m else "") + m
            row.append(InlineKeyboardButton(label[:30], callback_data=f"model:{m}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append([InlineKeyboardButton(("✅ default" if s.poll_model is None else "default"),
                                          callback_data="model:__default__")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="set:back")])

        await q.edit_message_text("🧩 Choose Pollinations model:", reply_markup=InlineKeyboardMarkup(rows))
        return

    # provider set
    if q.data.startswith("provider:"):
        s.provider = q.data.split(":", 1)[1]
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    # size set
    if q.data.startswith("size:"):
        val = q.data.split(":", 1)[1]
        w_str, h_str = val.split("x", 1)
        s.width, s.height = int(w_str), int(h_str)
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    # count set
    if q.data.startswith("count:"):
        s.output_count = int(q.data.split(":", 1)[1])
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return

    # model set
    if q.data.startswith("model:"):
        m = q.data.split(":", 1)[1]
        s.poll_model = None if m == "__default__" else m
        save_settings(context, s)
        await q.edit_message_text(settings_text(s), reply_markup=settings_keyboard())
        return


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Menu router: when user taps the menu buttons.
    """
    txt = (update.message.text or "").strip()
    if txt == "🎨 Generate":
        return await on_menu_generate(update, context)
    if txt == "⚙️ Settings":
        await on_menu_settings(update, context)
        return ConversationHandler.END
    if txt == "ℹ️ Help":
        await help_cmd(update, context)
        return ConversationHandler.END

    # If user just types randomly, treat as “quick generate”:
    # save as pending prompt and show action buttons.
    context.user_data["pending_prompt"] = txt
    s = get_settings(context)
    prov = "Pollinations" if s.provider == "pollinations" else "Craiyon"
    await update.message.reply_text(
        f"Prompt received.\nProvider: {prov}\n\nGenerate now or enhance?",
        reply_markup=prompt_action_keyboard()
    )
    return ConversationHandler.END


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Callbacks for settings + prompt actions
    app.add_handler(CallbackQueryHandler(on_settings_callbacks, pattern=r"^(set:|provider:|size:|count:|model:)"))
    app.add_handler(CallbackQueryHandler(on_prompt_actions, pattern=r"^prompt:"))

    # Conversation for waiting prompt after tapping Generate
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text)],
        states={
            WAIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt_received)],
            WAIT_ENHANCE_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt_received)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
