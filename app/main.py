"""Rho v2 FastAPI app — Batch 2: decisions, Bolna dial, webhooks, rewards."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.admin_routes import router as admin_router
from app.auth import require_admin
from app.config import get_settings
from app.db import close_pool, get_pool
from app.ingest import (
    get_referral_code,
    ingest_event,
    opt_out,
    upsert_profile,
)
from app.migrate import apply_migrations
from app.models import EventIn, OptOutOut, ProfileIn, ProfileOut, ReferralCodeOut, TickOut
from app.rate_limit import TokenBucket
from app.team_phones import seed_team_phones_from_env
from app.call_context import call_preview
from app.webhooks import handle_bolna_webhook, verify_webhook_secret
from app.worker import force_evaluate_user, run_tick, worker_loop

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rho")

# CORS: fixed allow list + regex for localhost / 127.0.0.1 any port
_CORS_ORIGINS = [
    "https://astro-naut.github.io",
    "https://rho-for-bolna.vercel.app",
]
_CORS_ORIGIN_REGEX = r"https://astro-naut\.github\.io|https://rho-for-bolna\.vercel\.app|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Migrations on startup (sync via thread); pool stays lazy until first request/tick.
    try:
        applied = await apply_migrations()
        if applied:
            log.info("migrations applied: %s", applied)
    except Exception:
        log.exception("migration failed on startup")
        raise

    # Seed team phones (needs pool)
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            n = await seed_team_phones_from_env(conn)
            if n:
                log.info("seeded %s team phones from env", n)

    worker_task = None
    stop_event = None
    if settings.worker_enabled:
        import asyncio

        stop_event = asyncio.Event()
        worker_task = asyncio.create_task(worker_loop(stop_event), name="rho-worker")

    yield

    if stop_event:
        stop_event.set()
    if worker_task:
        try:
            await worker_task
        except Exception:
            pass
    await close_pool()


app = FastAPI(title="Rho v2", version="0.2.1", lifespan=lifespan)

_settings_boot = None
try:
    _settings_boot = get_settings()
    extra = _settings_boot.cors_extra_list()
except Exception:
    extra = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS + extra,
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router)

_limiter: Optional[TokenBucket] = None


def _get_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = TokenBucket(s.rate_limit_capacity, s.rate_limit_refill_per_sec)
    return _limiter


async def _check_body_and_rate(request: Request) -> None:
    settings = get_settings()
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > settings.max_body_bytes:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "body too large")
        except ValueError:
            pass
    # Peek body size
    body = await request.body()
    if len(body) > settings.max_body_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "body too large")
    # stash for downstream — FastAPI caches request body after read in Starlette
    client = request.client.host if request.client else "unknown"
    if not _get_limiter().allow(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")


@app.get("/health")
async def health():
    """Cheap liveness — no DB."""
    return {"ok": True, "service": "rho-v2"}


@app.api_route("/internal/tick", methods=["GET", "POST"], response_model=TickOut)
async def internal_tick(secret: str = Query(...)):
    settings = get_settings()
    expected = settings.tick_secret.encode("utf-8")
    got = (secret or "").encode("utf-8")
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid tick secret")
    result = await run_tick()
    return TickOut(**{k: result[k] for k in TickOut.model_fields if k in result})


@app.post("/events")
async def post_events(request: Request):
    await _check_body_and_rate(request)
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(400, "invalid json") from e
    try:
        body = EventIn.model_validate(data)
    except Exception as e:
        raise HTTPException(422, str(e)) from e
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            try:
                result = await ingest_event(
                    conn,
                    user_id=body.user_id,
                    stage=body.stage,
                    event=body.event,
                    ts_raw=body.ts,
                    ref_code=body.ref_code,
                )
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
    return result


@app.post("/users/{user_id}/profile", response_model=ProfileOut)
async def post_profile(user_id: str, request: Request):
    await _check_body_and_rate(request)
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(400, "invalid json") from e
    try:
        body = ProfileIn.model_validate(data)
    except Exception as e:
        raise HTTPException(422, str(e)) from e
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await upsert_profile(
                conn,
                user_id,
                phone=body.phone,
                language=body.language,
                consent=body.consent,
                name=body.name,
                ref_code=body.ref_code,
            )
    return ProfileOut(**result)


@app.get("/users/{user_id}/referral-code", response_model=ReferralCodeOut)
async def referral_code(user_id: str):
    pool = await get_pool()
    async with pool.connection() as conn:
        # 404 if no profile (no user row or no referral_code yet)
        cur = await conn.execute(
            "SELECT id, referral_code FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        if not row or not row["referral_code"]:
            raise HTTPException(404, "no profile / referral code")
        return ReferralCodeOut(user_id=user_id, referral_code=row["referral_code"])


@app.post("/users/{user_id}/optout", response_model=OptOutOut)
async def post_optout(user_id: str, request: Request):
    await _check_body_and_rate(request)
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await opt_out(conn, user_id)
    return OptOutOut(**result)




@app.get("/users/{user_id}/call-preview")
async def get_call_preview(user_id: str):
    """Return agent_env, agent_id_configured, playbook, user_data — no call placed."""
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            return await call_preview(conn, user_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e


@app.post("/webhooks/bolna")
async def bolna_webhook(
    request: Request,
    secret: Optional[str] = Query(default=None),
):
    header_secret = request.headers.get("X-Rho-Webhook-Secret")
    if not verify_webhook_secret(header_secret, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook secret")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(400, "invalid json") from e
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected object")
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await handle_bolna_webhook(conn, payload)
    return result


@app.post("/dev/trigger/{user_id}")
async def dev_trigger(user_id: str, _: None = Depends(require_admin)):
    """DEV only: force-evaluate open stall (filters except score threshold)."""
    settings = get_settings()
    if not settings.dev:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return await force_evaluate_user(user_id)


# Explicit OPTIONS for sendBeacon / preflight friendliness (CORSMiddleware also handles this).
@app.options("/{full_path:path}")
async def options_preflight(full_path: str):
    return PlainTextResponse("", status_code=204)
