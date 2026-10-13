# Path: app/utils/security/dependencies.py
# Description: FastAPI dependencies for admin sessions and proxy-user authentication.

import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils.postgres import ApiKeyDb, DashboardMemberDb, UserDb, get_db

from .keys import hash_key
from .permissions import has_permission, normalize_permissions, required_permission, verify_password
from .tokens import decode_admin_token, owner_session_version, session_version, verify_admin_credentials

# Verified against when a login names no existing member, so an unknown username costs the same scrypt work as a
# wrong password and response timing does not reveal which usernames exist.
_DUMMY_PASSWORD_HASH = "scrypt$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _same_version(claims: dict, expected: str) -> bool:
    presented = claims.get("sv")
    return isinstance(presented, str) and hmac.compare_digest(presented.encode(), expected.encode())


@dataclass(frozen=True)
class AdminPrincipal:
    subject: str
    member_id: Optional[uuid.UUID]
    role: str
    permissions: frozenset[str]


def _principal_from_claims(claims: dict, db: Session) -> Optional[AdminPrincipal]:
    member_id = claims.get("member_id")
    if not member_id:
        if claims.get("sub") == get_settings().ADMIN_USERNAME and _same_version(claims, owner_session_version()):
            subject = str(claims.get("sub"))
            return AdminPrincipal(subject, None, "owner", frozenset({"*"}))
        return None
    try:
        member = db.get(DashboardMemberDb, uuid.UUID(str(member_id)))
    except (ValueError, TypeError):
        return None
    if member is None or not member.active or not _same_version(claims, session_version(member.password_hash)):
        return None
    try:
        values = json.loads(member.permissions_json or "[]")
        permissions = normalize_permissions(values)
    except (TypeError, ValueError):
        permissions = frozenset()
    return AdminPrincipal(member.username, member.id, "member", permissions)


def require_admin_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),  # noqa: B008
) -> AdminPrincipal:
    """Authenticate a dashboard member and enforce the route permission."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_admin_token(token)
    principal = _principal_from_claims(claims, db) if claims is not None else None
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin session")
    required = required_permission(request.url.path, request.method)
    if not has_permission(principal.permissions, required):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing permission: {required}")
    return principal


def require_admin(principal: AdminPrincipal = Depends(require_admin_principal)) -> str:  # noqa: B008
    """Compatibility dependency returning the authenticated dashboard subject."""
    return principal.subject


def authenticate_dashboard_member(username: str, password: str, db: Session) -> Optional[DashboardMemberDb]:
    """Return an active database member when credentials are valid."""
    member = db.query(DashboardMemberDb).filter(DashboardMemberDb.username == username.strip().lower()).first()
    if member is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if not verify_password(password, member.password_hash) or not member.active:
        return None
    return member


def is_root_credentials(username: str, password: str) -> bool:
    return verify_admin_credentials(username, password)


def authenticate_user(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    db: Session = Depends(get_db),  # noqa: B008
) -> ApiKeyDb:
    """FastAPI dependency for the proxy path: resolve the calling API key (and thus its user), or raise 401.

    Claude Code sends the key as `Authorization: Bearer <key>` (ANTHROPIC_AUTH_TOKEN) or `x-api-key: <key>`
    (ANTHROPIC_API_KEY); both are accepted, as is a bare token in the Authorization header. The returned ApiKeyDb
    carries `user_id`, which the proxy uses to attribute usage.
    """
    presented: Optional[str] = None
    if authorization:
        parts = authorization.split(" ", 1)
        presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()
    elif x_api_key:
        presented = x_api_key.strip()

    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (Authorization: Bearer ... or x-api-key)",
        )

    key = db.query(ApiKeyDb).filter(ApiKeyDb.key_hash == hash_key(presented)).first()
    if key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if not key.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key is disabled")

    user = db.query(UserDb).filter(UserDb.id == key.user_id).first()
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is disabled")

    return key
