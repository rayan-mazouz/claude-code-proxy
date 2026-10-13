# Path: app/utils/security/tokens.py
# Description: Admin session JWT issue/verify and constant-time credential checking.

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.config import get_settings

# Get the settings
settings = get_settings()

# Claims every dashboard session must carry; tokens missing any of them are rejected.
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "sv"]


def _constant_time_equals(left: str, right: str) -> bool:
    # hmac.compare_digest rejects non-ASCII str input, so compare the UTF-8 bytes instead.
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_admin_credentials(username: str, password: str) -> bool:
    """Constant-time check of the break-glass owner credentials configured in the environment."""
    user_ok = _constant_time_equals(username, settings.ADMIN_USERNAME)
    pass_ok = _constant_time_equals(password, settings.ADMIN_PASSWORD)
    return user_ok and pass_ok


def session_version(credential: str) -> str:
    """Derive a short, non-reversible fingerprint of the credential a session was issued for.

    The fingerprint is embedded in every session token and re-checked on each request, so changing a password
    (the owner's ADMIN_PASSWORD or a member's stored hash) invalidates every session issued before the change.
    """
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), credential.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def owner_session_version() -> str:
    return session_version(f"owner:{settings.ADMIN_USERNAME}:{settings.ADMIN_PASSWORD}")


def issue_admin_token(
    *,
    subject: Optional[str] = None,
    member_id: Optional[str] = None,
    role: str = "owner",
    permissions: Optional[list[str]] = None,
    version: Optional[str] = None,
) -> str:
    """Issue a signed JWT for a dashboard member or the break-glass owner."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject or settings.ADMIN_USERNAME,
        "role": "admin",
        "member_id": member_id,
        "member_role": role,
        "permissions": permissions if permissions is not None else ["*"],
        "sv": version if version is not None else owner_session_version(),
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_admin_token(token: str) -> Optional[dict]:
    """Decode and verify an admin session token. Returns the claims if valid and admin-scoped, else None."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("role") != "admin":
        return None
    return payload
