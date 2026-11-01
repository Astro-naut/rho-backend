"""Admin auth via X-Admin-Key header (constant-time compare)."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> str:
    settings = get_settings()
    expected = settings.admin_api_key.encode("utf-8")
    got = (x_admin_key or "").encode("utf-8")
    if not x_admin_key or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")
    return "admin"
