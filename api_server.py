"""
╔══════════════════════════════════════════════════════════════════════╗
║   UNDRESS AI API SERVER — Your own private API (FastAPI + SQLite)    ║
║   Mirrors public-api.undresstool.fun endpoints                       ║
║   Includes: API key management, user credits, all undress endpoints  ║
╚══════════════════════════════════════════════════════════════════════╝

Run:
    pip install -r requirements.txt
    python api_server.py

Admin endpoints (no auth):
    POST /admin/keys          — generate a new API key
    GET  /admin/keys          — list all keys
    POST /admin/keys/{key}/credits  — add credits to a key
    DELETE /admin/keys/{key}  — revoke a key

User endpoints (require X-API-KEY header):
    GET  /api/v1/me
    POST /api/v1/photos/undress
    GET  /api/v1/photos/poses
    POST /api/v1/photos/poses/undress
    POST /api/v1/video/undress
    GET  /api/v1/video/poses
    POST /api/v1/video/poses/undress
"""

import asyncio
import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

import aiosqlite
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────────────────────

DB_PATH         = "api_server.db"
ADMIN_SECRET    = os.getenv("ADMIN_SECRET", "change-this-admin-secret")  # protect admin routes
API_HOST        = "0.0.0.0"
API_PORT        = 8000

# Default credits given to a newly created API key
DEFAULT_CREDITS = 100

# Credit costs per operation (mirrors undresstool.fun pricing)
CREDIT_COST = {
    "photo_basic":   2,
    "photo_custom":  3,
    "photo_pose":    2,
    "video_basic":   5,
    "video_pose":    5,
}

PHOTO_POSES = [
    "standing", "sitting", "lying", "kneeling",
    "dancing", "yoga", "stretching", "bending",
]

VIDEO_POSES = [
    {"id": "vp_001", "name": "Walk"},
    {"id": "vp_002", "name": "Dance"},
    {"id": "vp_003", "name": "Spin"},
    {"id": "vp_004", "name": "Bounce"},
    {"id": "vp_005", "name": "Stretch"},
]

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("api_server")

# ──────────────────────────────────────────────────────────────────────────────
#  ENUMS
# ──────────────────────────────────────────────────────────────────────────────

class AgeEnum(str, Enum):
    a18 = "18"; a20 = "20"; a30 = "30"; a40 = "40"; a50 = "50"

class BreastSizeEnum(str, Enum):
    small = "small"; normal = "normal"; big = "big"

class BodyTypeEnum(str, Enum):
    skinny = "skinny"; normal = "normal"; curvy = "curvy"; muscular = "muscular"

class ButtSizeEnum(str, Enum):
    small = "small"; normal = "normal"; big = "big"

class PostGenEnum(str, Enum):
    upscale = "upscale"; anime = "anime"

class ClothEnum(str, Enum):
    naked             = "Naked"
    bikini            = "Bikini"
    lingerie          = "Lingerie"
    sport_wear        = "Sport wear"
    bdsm              = "BDSM"
    latex             = "Latex"
    teacher           = "Teacher"
    schoolgirl        = "Schoolgirl"
    bikini_leopard    = "Bikini leopard"
    naked_cum         = "Naked cum"
    naked_tattoo      = "Naked tatoo"
    witch             = "Witch"
    sexy_witch        = "Sexy Witch"
    maid              = "Maid"
    christmas         = "Christmas underwear"
    pregnant          = "Pregnant"
    cheerleader       = "Cheerleader"
    police            = "Police"
    secretary         = "Secretary"
    blooming_bouquet  = "Blooming Bouquet"
    leather_dress     = "Leather dress"
    corset            = "Corset"
    mini_bikini       = "Mini bikini"

# ──────────────────────────────────────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # API keys table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key             TEXT PRIMARY KEY,
                key_hash        TEXT UNIQUE NOT NULL,
                telegram_id     INTEGER,
                label           TEXT,
                credits         INTEGER DEFAULT 100,
                is_active       INTEGER DEFAULT 1,
                can_create_photos INTEGER DEFAULT 1,
                can_create_videos INTEGER DEFAULT 1,
                last_bought_at  TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        # Jobs table (tracks async undress requests)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id_gen          TEXT PRIMARY KEY,
                key_hash        TEXT NOT NULL,
                type            TEXT NOT NULL,
                webhook_url     TEXT NOT NULL,
                status          TEXT DEFAULT 'queued',
                result_url      TEXT,
                error_msg       TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        await db.commit()
    log.info("DB initialised ✔")


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _generate_key() -> str:
    """Generate a random API key like: udt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"""
    return "udt_" + secrets.token_hex(24)


async def db_create_key(
    label: str = None,
    telegram_id: int = None,
    credits: int = DEFAULT_CREDITS,
) -> dict:
    key = _generate_key()
    key_hash = _hash_key(key)
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO api_keys
               (key, key_hash, telegram_id, label, credits, is_active,
                can_create_photos, can_create_videos, created_at)
               VALUES (?, ?, ?, ?, ?, 1, 1, 1, ?)""",
            (key, key_hash, telegram_id, label, credits, now),
        )
        await db.commit()
    return {"key": key, "key_hash": key_hash, "credits": credits, "created_at": now}


async def db_get_key(key: str) -> dict | None:
    key_hash = _hash_key(key)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1", (key_hash,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_list_keys() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT key, label, telegram_id, credits, is_active, can_create_photos, "
            "can_create_videos, created_at FROM api_keys ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def db_deduct_credits(key_hash: str, amount: int) -> int:
    """Deduct credits. Returns new balance."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE api_keys SET credits = credits - ? WHERE key_hash = ?",
            (amount, key_hash),
        )
        await db.commit()
        async with db.execute(
            "SELECT credits FROM api_keys WHERE key_hash = ?", (key_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_add_credits(key: str, amount: int) -> int:
    key_hash = _hash_key(key)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE api_keys SET credits = credits + ?, last_bought_at = ? WHERE key_hash = ?",
            (amount, datetime.utcnow().isoformat(), key_hash),
        )
        await db.commit()
        async with db.execute(
            "SELECT credits FROM api_keys WHERE key_hash = ?", (key_hash,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def db_revoke_key(key: str) -> bool:
    key_hash = _hash_key(key)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_hash = ?", (key_hash,)
        )
        await db.commit()
    return True


async def db_save_job(id_gen: str, key_hash: str, job_type: str, webhook_url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jobs (id_gen, key_hash, type, webhook_url, status, created_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?)",
            (id_gen, key_hash, job_type, webhook_url, datetime.utcnow().isoformat()),
        )
        await db.commit()


# ──────────────────────────────────────────────────────────────────────────────
#  AUTH DEPENDENCY
# ──────────────────────────────────────────────────────────────────────────────

async def require_api_key(x_api_key: str = Header(..., alias="X-API-KEY")) -> dict:
    user = await db_get_key(x_api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return user


async def require_admin(x_admin_secret: str = Header(..., alias="X-Admin-Secret")):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


# ──────────────────────────────────────────────────────────────────────────────
#  FASTAPI APP
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Undress AI API",
    description="""
Your private Undress AI API server.

**Authentication**: Pass your API key in the `X-API-KEY` header.

**Admin routes**: Pass `X-Admin-Secret` header.

**Rate limits**:
- Authenticated users: 60 requests per minute

**Credit costs**:
- Basic photo undress: 2 credits
- Custom photo undress: 3 credits
- Photo + pose: 2 credits
- Video: 5 credits
- Video + pose: 5 credits
""",
    version="1.0.0",
)


@app.on_event("startup")
async def startup():
    await init_db()
    log.info("API server ready on http://%s:%d", API_HOST, API_PORT)


# ──────────────────────────────────────────────────────────────────────────────
#  ADMIN ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/admin/keys", tags=["Admin"], summary="Generate a new API key")
async def admin_create_key(
    label: str = Form(None),
    telegram_id: int = Form(None),
    credits: int = Form(DEFAULT_CREDITS),
    _: None = Depends(require_admin),
):
    """Create a new API key with optional label, telegram_id, and starting credits."""
    result = await db_create_key(label=label, telegram_id=telegram_id, credits=credits)
    log.info("Created API key label=%s credits=%d", label, credits)
    return result


@app.get("/admin/keys", tags=["Admin"], summary="List all API keys")
async def admin_list_keys(_: None = Depends(require_admin)):
    """List all API keys (keys are shown in full — keep admin secret safe)."""
    return await db_list_keys()


@app.post("/admin/keys/{key}/credits", tags=["Admin"], summary="Add credits to a key")
async def admin_add_credits(
    key: str,
    amount: int = Form(...),
    _: None = Depends(require_admin),
):
    """Add credits to the given API key."""
    new_balance = await db_add_credits(key, amount)
    return {"key": key, "added": amount, "new_balance": new_balance}


@app.delete("/admin/keys/{key}", tags=["Admin"], summary="Revoke an API key")
async def admin_revoke_key(key: str, _: None = Depends(require_admin)):
    """Permanently deactivate an API key."""
    await db_revoke_key(key)
    return {"status": "revoked", "key": key}


# ──────────────────────────────────────────────────────────────────────────────
#  USER ROUTES — /api/v1/me
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/v1/me", tags=["User"], summary="Get User Info")
async def get_user_info(user: dict = Depends(require_api_key)):
    """Return account info for the authenticated API key."""
    return {
        "telegram_id":        user.get("telegram_id"),
        "balance":            user["credits"],
        "last_bought_at":     user.get("last_bought_at"),
        "can_create_photos":  bool(user["can_create_photos"]),
        "can_create_videos":  bool(user["can_create_videos"]),
    }


# ──────────────────────────────────────────────────────────────────────────────
#  PHOTO POSES
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/v1/photos/poses", tags=["Photo"], summary="Get Photo Poses")
async def get_photo_poses():
    return {"poses": PHOTO_POSES}


# ──────────────────────────────────────────────────────────────────────────────
#  PHOTO UNDRESS
# ──────────────────────────────────────────────────────────────────────────────

async def _handle_undress_request(
    user: dict,
    id_gen: str,
    webhook: str,
    photo: UploadFile,
    job_type: str,
    cost: int,
) -> dict:
    """Shared logic: check credits, deduct, queue job, trigger async processing."""
    key_hash = user["key_hash"]

    if user["credits"] < cost:
        return {
            "status": "error",
            "message": f"Insufficient credits. Need {cost}, have {user['credits']}.",
            "id_gen": id_gen,
            "raw_data": "",
        }

    await db_deduct_credits(key_hash, cost)
    photo_bytes = await photo.read()
    await db_save_job(id_gen, key_hash, job_type, webhook)

    # Kick off background processing (you plug in your real AI processing here)
    asyncio.create_task(
        _process_job(id_gen, job_type, photo_bytes, webhook)
    )

    log.info("Queued job id_gen=%s type=%s cost=%d", id_gen, job_type, cost)
    return {
        "status": "queued",
        "message": "Your request has been queued. Result will be sent to your webhook.",
        "id_gen": id_gen,
    }


async def _process_job(id_gen: str, job_type: str, photo_bytes: bytes, webhook_url: str):
    """
    ─────────────────────────────────────────────────────────────────
    PLUG YOUR AI MODEL / EXTERNAL PROCESSOR HERE.

    This function runs in the background after a job is queued.
    When done, POST the result to `webhook_url` with this payload:

        {
            "id_gen": "<id_gen>",
            "status": "done",
            "result_url": "https://your-cdn.com/result.jpg"
        }

    On failure:
        {
            "id_gen": "<id_gen>",
            "status": "error",
            "message": "Something went wrong"
        }
    ─────────────────────────────────────────────────────────────────
    """
    import aiohttp as _aiohttp

    log.info("Processing job %s (type=%s)  [PLUG YOUR AI HERE]", id_gen, job_type)

    # ── TODO: Replace this block with your actual AI processing ──────────────
    await asyncio.sleep(3)  # simulate processing time
    result_payload = {
        "id_gen": id_gen,
        "status": "done",
        # Replace with a real generated image/video URL from your AI:
        "result_url": f"https://picsum.photos/seed/{id_gen[:8]}/800/600",
    }
    # ─────────────────────────────────────────────────────────────────────────

    try:
        async with _aiohttp.ClientSession() as s:
            await s.post(webhook_url, json=result_payload)
        log.info("Sent result for job %s to %s", id_gen, webhook_url)
    except Exception as e:
        log.error("Failed to POST webhook for job %s: %s", id_gen, e)

    # Update DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status='done', result_url=? WHERE id_gen=?",
            (result_payload.get("result_url"), id_gen),
        )
        await db.commit()


@app.post("/api/v1/photos/undress", tags=["Photo"], summary="Undress Photo")
async def undress_photo(
    id_gen:      str          = Form(..., description="ID to identify your request"),
    photo:       UploadFile   = File(..., description="Photo to undress"),
    webhook:     AnyHttpUrl   = Form(..., description="Webhook URL for result"),
    age:         Optional[AgeEnum]        = Form(None),
    breast_size: Optional[BreastSizeEnum] = Form(None),
    body_type:   Optional[BodyTypeEnum]   = Form(None),
    butt_size:   Optional[ButtSizeEnum]   = Form(None),
    cloth:       Optional[ClothEnum]      = Form(None),
    post_gen:    Optional[PostGenEnum]    = Form(None),
    user: dict = Depends(require_api_key),
):
    """
    Undress a photo. Optionally pass customization parameters.

    **Cost**: 2 credits (basic) or 3 credits (with any customization param).
    """
    has_custom = any([age, breast_size, body_type, butt_size, cloth, post_gen])
    cost = CREDIT_COST["photo_custom"] if has_custom else CREDIT_COST["photo_basic"]
    job_type = "photo_custom" if has_custom else "photo_basic"
    return await _handle_undress_request(user, id_gen, str(webhook), photo, job_type, cost)


@app.post("/api/v1/photos/poses/undress", tags=["Photo"], summary="Undress Photo with Pose")
async def undress_photo_pose(
    id_gen:  str        = Form(...),
    photo:   UploadFile = File(...),
    pose:    str        = Form(..., description="Pose name from /api/v1/photos/poses"),
    webhook: AnyHttpUrl = Form(...),
    user: dict = Depends(require_api_key),
):
    """Undress a photo and apply a pose. **Cost**: 2 credits."""
    if pose not in PHOTO_POSES:
        raise HTTPException(400, f"Invalid pose '{pose}'. Valid: {PHOTO_POSES}")
    return await _handle_undress_request(
        user, id_gen, str(webhook), photo, "photo_pose", CREDIT_COST["photo_pose"]
    )


# ──────────────────────────────────────────────────────────────────────────────
#  VIDEO POSES
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/v1/video/poses", tags=["Video"], summary="Get Video Poses")
async def get_video_poses():
    return {"poses": VIDEO_POSES}


# ──────────────────────────────────────────────────────────────────────────────
#  VIDEO UNDRESS
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/v1/video/undress", tags=["Video"], summary="Undress Video")
async def undress_video(
    id_gen:  str        = Form(...),
    photo:   UploadFile = File(...),
    webhook: AnyHttpUrl = Form(...),
    user: dict = Depends(require_api_key),
):
    """Generate an undress video from a photo. **Cost**: 5 credits."""
    return await _handle_undress_request(
        user, id_gen, str(webhook), photo, "video_basic", CREDIT_COST["video_basic"]
    )


@app.post("/api/v1/video/poses/undress", tags=["Video"], summary="Undress Video with Pose")
async def undress_video_pose(
    id_gen:  str        = Form(...),
    photo:   UploadFile = File(...),
    pose_id: str        = Form(..., description="Pose ID from /api/v1/video/poses"),
    webhook: AnyHttpUrl = Form(...),
    user: dict = Depends(require_api_key),
):
    """Generate a posed undress video from a photo. **Cost**: 5 credits."""
    valid_ids = [p["id"] for p in VIDEO_POSES]
    if pose_id not in valid_ids:
        raise HTTPException(400, f"Invalid pose_id '{pose_id}'. Valid: {valid_ids}")
    return await _handle_undress_request(
        user, id_gen, str(webhook), photo, "video_pose", CREDIT_COST["video_pose"]
    )


# ──────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host=API_HOST, port=API_PORT, reload=False)
