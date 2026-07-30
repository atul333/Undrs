"""
╔══════════════════════════════════════════════════════════════════╗
║         UNDRESS AI BOT — powered by undresstool.fun API          ║
║         Framework : aiogram 3.x + aiohttp + aiosqlite            ║
╚══════════════════════════════════════════════════════════════════╝

Config:
  BOT_TOKEN        — Telegram bot token from @BotFather
  UNDRESS_API_KEY  — API key from undresstool.fun
  WEBHOOK_BASE_URL — Your public HTTPS URL (e.g. https://abc.ngrok.io)
  WEBHOOK_PORT     — Local port for the embedded webhook HTTP server
"""

import asyncio
import logging
import uuid
from datetime import datetime

import aiohttp
import aiosqlite
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  (edit these before running)
# ──────────────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = "YOUR_TELEGRAM_BOT_TOKEN"
OWN_API_KEY: str = "udt_your_generated_key_here"   # from: python api_server.py → POST /admin/keys
OWN_API_URL: str = "http://localhost:8000"          # your api_server.py base URL
WEBHOOK_BASE_URL: str = "https://your-public-domain.com"  # public HTTPS for undress results
WEBHOOK_PORT: int = 8888

API_BASE = OWN_API_URL  # bot calls YOUR server
WEBHOOK_PATH = "/undress-webhook"

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("undress_bot")

# ──────────────────────────────────────────────────────────────────────────────
#  ENUMS (mirrors the API)
# ──────────────────────────────────────────────────────────────────────────────

AGE_OPTIONS       = ["18", "20", "30", "40", "50"]
BREAST_OPTIONS    = ["small", "normal", "big"]
BODY_OPTIONS      = ["skinny", "normal", "curvy", "muscular"]
BUTT_OPTIONS      = ["small", "normal", "big"]
POST_GEN_OPTIONS  = ["upscale", "anime"]
CLOTH_OPTIONS     = [
    "Naked", "Bikini", "Lingerie", "Sport wear", "BDSM", "Latex",
    "Teacher", "Schoolgirl", "Bikini leopard", "Naked cum", "Naked tatoo",
    "Witch", "Sexy Witch", "Maid", "Christmas underwear", "Pregnant",
    "Cheerleader", "Police", "Secretary", "Blooming Bouquet",
    "Leather dress", "Corset", "Mini bikini",
]

MODES = {
    "basic":       "🖼 Basic Photo (2 credits)",
    "custom":      "✨ Custom Photo (3 credits)",
    "photo_pose":  "🎭 Photo + Pose (2 credits)",
    "video":       "🎬 Video (5 credits)",
    "video_pose":  "🎬🎭 Video + Pose (5 credits)",
}

# ──────────────────────────────────────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────────────────────────────────────

DB_PATH = "undress_jobs.db"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id_gen      TEXT PRIMARY KEY,
                chat_id     INTEGER NOT NULL,
                message_id  INTEGER,
                mode        TEXT,
                status      TEXT DEFAULT 'pending',
                result_url  TEXT,
                error_msg   TEXT,
                created_at  TEXT
            )
        """)
        await db.commit()
    log.info("DB ready ✔")


async def save_job(id_gen: str, chat_id: int, message_id: int, mode: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO jobs (id_gen, chat_id, message_id, mode, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (id_gen, chat_id, message_id, mode, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_job(id_gen: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id_gen = ?", (id_gen,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_job(id_gen: str, status: str, result_url: str = None, error_msg: str = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status=?, result_url=?, error_msg=? WHERE id_gen=?",
            (status, result_url, error_msg, id_gen),
        )
        await db.commit()


# ──────────────────────────────────────────────────────────────────────────────
#  API CLIENT
# ──────────────────────────────────────────────────────────────────────────────

def _api_headers() -> dict:
    return {"X-API-KEY": OWN_API_KEY}


async def api_get_me() -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{API_BASE}/api/v1/me", headers=_api_headers()) as r:
            return await r.json()


async def api_get_photo_poses() -> list:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{API_BASE}/api/v1/photos/poses", headers=_api_headers()) as r:
            data = await r.json()
            return data.get("poses", [])


async def api_get_video_poses() -> list:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{API_BASE}/api/v1/video/poses", headers=_api_headers()) as r:
            data = await r.json()
            return data.get("poses", [])


async def api_undress_photo(
    photo_bytes: bytes,
    filename: str,
    id_gen: str,
    webhook: str,
    age=None, breast_size=None, body_type=None, butt_size=None, cloth=None, post_gen=None,
) -> dict:
    form = aiohttp.FormData()
    form.add_field("id_gen", id_gen)
    form.add_field("webhook", webhook)
    form.add_field("photo", photo_bytes, filename=filename, content_type="image/jpeg")
    if age:         form.add_field("age", age)
    if breast_size: form.add_field("breast_size", breast_size)
    if body_type:   form.add_field("body_type", body_type)
    if butt_size:   form.add_field("butt_size", butt_size)
    if cloth:       form.add_field("cloth", cloth)
    if post_gen:    form.add_field("post_gen", post_gen)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_BASE}/api/v1/photos/undress",
            data=form, headers=_api_headers()
        ) as r:
            return await r.json()


async def api_undress_photo_pose(
    photo_bytes: bytes, filename: str, id_gen: str, webhook: str, pose: str
) -> dict:
    form = aiohttp.FormData()
    form.add_field("id_gen", id_gen)
    form.add_field("webhook", webhook)
    form.add_field("pose", pose)
    form.add_field("photo", photo_bytes, filename=filename, content_type="image/jpeg")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_BASE}/api/v1/photos/poses/undress",
            data=form, headers=_api_headers()
        ) as r:
            return await r.json()


async def api_undress_video(
    photo_bytes: bytes, filename: str, id_gen: str, webhook: str
) -> dict:
    form = aiohttp.FormData()
    form.add_field("id_gen", id_gen)
    form.add_field("webhook", webhook)
    form.add_field("photo", photo_bytes, filename=filename, content_type="image/jpeg")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_BASE}/api/v1/video/undress",
            data=form, headers=_api_headers()
        ) as r:
            return await r.json()


async def api_undress_video_pose(
    photo_bytes: bytes, filename: str, id_gen: str, webhook: str, pose_id: str
) -> dict:
    form = aiohttp.FormData()
    form.add_field("id_gen", id_gen)
    form.add_field("webhook", webhook)
    form.add_field("pose_id", pose_id)
    form.add_field("photo", photo_bytes, filename=filename, content_type="image/jpeg")
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{API_BASE}/api/v1/video/poses/undress",
            data=form, headers=_api_headers()
        ) as r:
            return await r.json()


# ──────────────────────────────────────────────────────────────────────────────
#  KEYBOARD HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _chunk(lst: list, n: int) -> list:
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def mode_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in MODES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mode:{key}")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def options_keyboard(prefix: str, options: list, skip: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=o, callback_data=f"{prefix}:{o}") for o in chunk]
        for chunk in _chunk(options, 3)
    ]
    footer = []
    if skip:
        footer.append(InlineKeyboardButton(text="⏭ Skip", callback_data=f"{prefix}:skip"))
    footer.append(InlineKeyboardButton(text="❌ Cancel", callback_data="cancel"))
    rows.append(footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pose_keyboard(prefix: str, poses) -> InlineKeyboardMarkup:
    """Works for both str list (photo) and dict list (video)."""
    rows = []
    if poses and isinstance(poses[0], dict):
        items = [(p["id"], p["name"]) for p in poses]
    else:
        items = [(p, p) for p in poses]
    for chunk in _chunk(items, 2):
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"{prefix}:{pid}")
            for pid, label in chunk
        ])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────────────────────────────────────────────────────────────────────────
#  FSM STATES
# ──────────────────────────────────────────────────────────────────────────────

class UndressFlow(StatesGroup):
    waiting_mode    = State()
    custom_age      = State()
    custom_breast   = State()
    custom_body     = State()
    custom_butt     = State()
    custom_cloth    = State()
    custom_post_gen = State()
    pick_photo_pose = State()
    pick_video_pose = State()


# ──────────────────────────────────────────────────────────────────────────────
#  BOT REFERENCE (set during startup)
# ──────────────────────────────────────────────────────────────────────────────

bot: Bot = None


# ──────────────────────────────────────────────────────────────────────────────
#  ROUTER + HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

router = Router()

WELCOME = (
    "👋 <b>Welcome to Undress AI Bot!</b>\n\n"
    "🔞 <i>For adults only. Use responsibly.</i>\n\n"
    "<b>Commands:</b>\n"
    "📷 Send any photo → choose a generation mode\n"
    "/balance — check your API credits\n"
    "/poses_photo — list available photo poses\n"
    "/poses_video — list available video poses\n"
    "/help — show this message again\n\n"
    "<b>Modes &amp; Credit costs:</b>\n"
    "• 🖼 Basic Photo — 2 credits\n"
    "• ✨ Custom Photo — 3 credits\n"
    "• 🎭 Photo + Pose — 2 credits\n"
    "• 🎬 Video — 5 credits\n"
    "• 🎬🎭 Video + Pose — 5 credits"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await msg.answer(WELCOME, parse_mode=ParseMode.HTML)


@router.message(Command("balance"))
async def cmd_balance(msg: Message) -> None:
    try:
        data = await api_get_me()
        text = (
            "💰 <b>Account Info</b>\n\n"
            f"🪙 Balance: <b>{data['balance']} credits</b>\n"
            f"🖼 Can create photos: {'✅' if data['can_create_photos'] else '❌'}\n"
            f"🎬 Can create videos: {'✅' if data['can_create_videos'] else '❌'}"
        )
        if data.get("last_bought_at"):
            text += f"\n🕐 Last purchase: {data['last_bought_at'][:10]}"
    except Exception as e:
        text = f"❌ Error fetching balance: {e}"
    await msg.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("poses_photo"))
async def cmd_poses_photo(msg: Message) -> None:
    try:
        poses = await api_get_photo_poses()
        if not poses:
            await msg.answer("No photo poses available right now.")
            return
        text = "🎭 <b>Available Photo Poses:</b>\n\n" + "\n".join(f"• {p}" for p in poses)
        await msg.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")


@router.message(Command("poses_video"))
async def cmd_poses_video(msg: Message) -> None:
    try:
        poses = await api_get_video_poses()
        if not poses:
            await msg.answer("No video poses available right now.")
            return
        text = "🎬 <b>Available Video Poses:</b>\n\n" + "\n".join(
            f"• [{p['id']}] {p['name']}" for p in poses
        )
        await msg.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.answer(f"❌ Error: {e}")


@router.message(F.photo)
async def handle_photo(msg: Message, state: FSMContext) -> None:
    await state.clear()
    best = msg.photo[-1]
    await state.update_data(file_id=best.file_id, chat_id=msg.chat.id)
    await state.set_state(UndressFlow.waiting_mode)
    await msg.answer(
        "📸 Photo received!\n\n<b>Select generation mode:</b>",
        reply_markup=mode_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "cancel")
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("❌ Cancelled. Send a new photo whenever you're ready.")
    await cb.answer()


@router.callback_query(UndressFlow.waiting_mode, F.data.startswith("mode:"))
async def on_mode_selected(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":", 1)[1]
    await state.update_data(mode=mode, params={})
    await cb.answer()

    if mode == "basic":
        await cb.message.edit_text("⚙️ Submitting basic undress…")
        await _submit(cb.message, state)

    elif mode == "custom":
        await state.set_state(UndressFlow.custom_age)
        await cb.message.edit_text(
            "✨ Custom mode — Step 1/6\n\n<b>Select Age:</b>",
            reply_markup=options_keyboard("age", AGE_OPTIONS),
            parse_mode=ParseMode.HTML,
        )

    elif mode == "photo_pose":
        await cb.message.edit_text("⏳ Loading photo poses…")
        try:
            poses = await api_get_photo_poses()
            await state.set_state(UndressFlow.pick_photo_pose)
            await cb.message.edit_text(
                "🎭 <b>Pick a Pose:</b>",
                reply_markup=pose_keyboard("pp", poses),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await cb.message.edit_text(f"❌ Could not load poses: {e}")
            await state.clear()

    elif mode == "video":
        await cb.message.edit_text("⚙️ Submitting video generation…")
        await _submit(cb.message, state)

    elif mode == "video_pose":
        await cb.message.edit_text("⏳ Loading video poses…")
        try:
            poses = await api_get_video_poses()
            await state.set_state(UndressFlow.pick_video_pose)
            await cb.message.edit_text(
                "🎬 <b>Pick a Video Pose:</b>",
                reply_markup=pose_keyboard("vp", poses),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await cb.message.edit_text(f"❌ Could not load poses: {e}")
            await state.clear()


# ── Custom flow ───────────────────────────────────────────────────────────────

@router.callback_query(UndressFlow.custom_age, F.data.startswith("age:"))
async def on_custom_age(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["age"] = val
    await state.update_data(params=params)
    await state.set_state(UndressFlow.custom_breast)
    await cb.answer()
    await cb.message.edit_text(
        "✨ Custom mode — Step 2/6\n\n<b>Select Breast Size:</b>",
        reply_markup=options_keyboard("breast", BREAST_OPTIONS),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UndressFlow.custom_breast, F.data.startswith("breast:"))
async def on_custom_breast(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["breast_size"] = val
    await state.update_data(params=params)
    await state.set_state(UndressFlow.custom_body)
    await cb.answer()
    await cb.message.edit_text(
        "✨ Custom mode — Step 3/6\n\n<b>Select Body Type:</b>",
        reply_markup=options_keyboard("body", BODY_OPTIONS),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UndressFlow.custom_body, F.data.startswith("body:"))
async def on_custom_body(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["body_type"] = val
    await state.update_data(params=params)
    await state.set_state(UndressFlow.custom_butt)
    await cb.answer()
    await cb.message.edit_text(
        "✨ Custom mode — Step 4/6\n\n<b>Select Butt Size:</b>",
        reply_markup=options_keyboard("butt", BUTT_OPTIONS),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UndressFlow.custom_butt, F.data.startswith("butt:"))
async def on_custom_butt(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["butt_size"] = val
    await state.update_data(params=params)
    await state.set_state(UndressFlow.custom_cloth)
    await cb.answer()
    await cb.message.edit_text(
        "✨ Custom mode — Step 5/6\n\n<b>Select Clothing Style:</b>",
        reply_markup=options_keyboard("cloth", CLOTH_OPTIONS),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UndressFlow.custom_cloth, F.data.startswith("cloth:"))
async def on_custom_cloth(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["cloth"] = val
    await state.update_data(params=params)
    await state.set_state(UndressFlow.custom_post_gen)
    await cb.answer()
    await cb.message.edit_text(
        "✨ Custom mode — Step 6/6\n\n<b>Post Processing:</b>",
        reply_markup=options_keyboard("pgen", POST_GEN_OPTIONS),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(UndressFlow.custom_post_gen, F.data.startswith("pgen:"))
async def on_custom_post_gen(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    params = data.get("params", {})
    if val != "skip":
        params["post_gen"] = val
    await state.update_data(params=params)
    await cb.answer()
    await cb.message.edit_text("⚙️ Submitting custom undress…")
    await _submit(cb.message, state)


# ── Pose selections ───────────────────────────────────────────────────────────

@router.callback_query(UndressFlow.pick_photo_pose, F.data.startswith("pp:"))
async def on_photo_pose(cb: CallbackQuery, state: FSMContext) -> None:
    pose = cb.data.split(":", 1)[1]
    await state.update_data(pose=pose)
    await cb.answer()
    await cb.message.edit_text("⚙️ Submitting photo + pose…")
    await _submit(cb.message, state)


@router.callback_query(UndressFlow.pick_video_pose, F.data.startswith("vp:"))
async def on_video_pose(cb: CallbackQuery, state: FSMContext) -> None:
    pose_id = cb.data.split(":", 1)[1]
    await state.update_data(pose_id=pose_id)
    await cb.answer()
    await cb.message.edit_text("⚙️ Submitting video + pose…")
    await _submit(cb.message, state)


# ──────────────────────────────────────────────────────────────────────────────
#  SUBMIT JOB
# ──────────────────────────────────────────────────────────────────────────────

async def _submit(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    file_id = data.get("file_id")
    chat_id = data.get("chat_id") or msg.chat.id
    mode    = data.get("mode", "basic")
    params  = data.get("params", {})
    pose    = data.get("pose")
    pose_id = data.get("pose_id")

    id_gen  = str(uuid.uuid4())
    webhook = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"

    try:
        file = await bot.get_file(file_id)
        buf  = await bot.download_file(file.file_path)
        photo_bytes = buf.read()
    except Exception as e:
        await msg.edit_text(f"❌ Could not download photo: {e}")
        return

    await save_job(id_gen, chat_id, msg.message_id, mode)

    try:
        if mode == "basic":
            result = await api_undress_photo(photo_bytes, "photo.jpg", id_gen, webhook)
        elif mode == "custom":
            result = await api_undress_photo(photo_bytes, "photo.jpg", id_gen, webhook, **params)
        elif mode == "photo_pose":
            result = await api_undress_photo_pose(photo_bytes, "photo.jpg", id_gen, webhook, pose)
        elif mode == "video":
            result = await api_undress_video(photo_bytes, "photo.jpg", id_gen, webhook)
        elif mode == "video_pose":
            result = await api_undress_video_pose(photo_bytes, "photo.jpg", id_gen, webhook, pose_id)
        else:
            result = {"status": "error", "message": "Unknown mode"}
    except Exception as e:
        await msg.edit_text(f"❌ API error: {e}")
        await update_job(id_gen, "error", error_msg=str(e))
        return

    status = result.get("status", "")
    if status in ("ok", "queued", "success", "pending"):
        await msg.edit_text(
            f"⏳ <b>Processing…</b>\n\nJob ID: <code>{id_gen}</code>\n"
            "You'll receive the result here automatically once it's ready.",
            parse_mode=ParseMode.HTML,
        )
    else:
        err = result.get("message", "Unknown error")
        await msg.edit_text(
            f"❌ API rejected the request:\n<code>{err}</code>",
            parse_mode=ParseMode.HTML,
        )
        await update_job(id_gen, "error", error_msg=err)


# ──────────────────────────────────────────────────────────────────────────────
#  WEBHOOK SERVER — receives results from undresstool.fun
# ──────────────────────────────────────────────────────────────────────────────

async def webhook_handler(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    log.info("Webhook payload: %s", payload)

    id_gen     = payload.get("id_gen")
    status     = payload.get("status", "")
    result_url = payload.get("result_url") or payload.get("url")
    message    = payload.get("message", "")

    if not id_gen:
        return web.Response(status=400, text="Missing id_gen")

    job = await get_job(id_gen)
    if not job:
        log.warning("Unknown job id_gen=%s", id_gen)
        return web.Response(status=404, text="Job not found")

    chat_id = job["chat_id"]
    mode    = job.get("mode", "basic")

    if status in ("done", "success", "ok", "completed") and result_url:
        await update_job(id_gen, "done", result_url=result_url)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(result_url) as resp:
                    file_data    = await resp.read()
                    content_type = resp.content_type or ""

            is_video = mode in ("video", "video_pose") or "video" in content_type
            caption = (
                f"✅ <b>Result ready!</b>\n"
                f"🆔 Job: <code>{id_gen}</code>\n"
                f"🎨 Mode: {MODES.get(mode, mode)}"
            )

            if is_video:
                await bot.send_video(
                    chat_id,
                    BufferedInputFile(file_data, filename="result.mp4"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_photo(
                    chat_id,
                    BufferedInputFile(file_data, filename="result.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            log.error("Failed to send result to %s: %s", chat_id, e)
            await bot.send_message(
                chat_id,
                f"✅ Result ready! <a href='{result_url}'>Click to view</a>",
                parse_mode=ParseMode.HTML,
            )
    else:
        err = message or "Unknown processing error"
        await update_job(id_gen, "error", error_msg=err)
        await bot.send_message(
            chat_id,
            f"❌ <b>Processing failed</b>\n<code>{err}</code>",
            parse_mode=ParseMode.HTML,
        )

    return web.Response(status=200, text="OK")


def build_webhook_app() -> web.Application:
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, webhook_handler)
    return app


# ──────────────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    global bot

    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start embedded aiohttp webhook receiver
    wh_app = build_webhook_app()
    runner = web.AppRunner(wh_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=WEBHOOK_PORT)
    await site.start()
    log.info("Webhook server on port %d  ✔  path: %s", WEBHOOK_PORT, WEBHOOK_PATH)
    log.info("Full webhook URL: %s%s", WEBHOOK_BASE_URL, WEBHOOK_PATH)

    log.info("Starting Telegram bot polling…")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
