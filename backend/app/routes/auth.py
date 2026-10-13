# Path: app/routes/auth.py
# Description: Admin authentication route -- issues a session JWT for the dashboard.

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.logger import get_logger
from app.utils import security
from app.utils.models.api import AuthProfileResponse, LoginRequest, TokenResponse
from app.utils.postgres import get_db

# Get the logger
logger = get_logger()

router = APIRouter(tags=["Admin Auth"], prefix="/auth")


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        200: {"description": "Authenticated; session token returned"},
        401: {"description": "Invalid username or password"},
        429: {"description": "Too many failed attempts; retry after the Retry-After delay"},
    },
)
def login(request: LoginRequest, http_request: Request, db: Session = Depends(get_db)) -> TokenResponse:  # noqa: B008
    """Authenticate the break-glass owner or a database-backed team member."""
    source = security.client_address(http_request)
    retry_after = security.login_retry_after(source, request.username)
    if retry_after is not None:
        logger.warning("Dashboard login throttled for source %s", source)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if security.is_root_credentials(request.username, request.password):
        security.record_login_success(source, request.username)
        token = security.issue_admin_token(subject=request.username, role="owner", permissions=["*"])
        logger.info("Dashboard owner logged in")
        return TokenResponse(token=token)

    member = security.authenticate_dashboard_member(request.username, request.password, db)
    if member is None:
        security.record_login_failure(source, request.username)
        logger.info("Dashboard login failed for source %s", source)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    security.record_login_success(source, request.username)
    member.last_login_at = datetime.now(timezone.utc)
    db.commit()
    values = json.loads(member.permissions_json or "[]")
    permissions = security.normalize_permissions(values)
    token = security.issue_admin_token(
        subject=member.username,
        member_id=str(member.id),
        role="member",
        permissions=sorted(permissions),
        version=security.session_version(member.password_hash),
    )
    logger.info("Dashboard team member logged in")
    return TokenResponse(token=token)


@router.get("/me", response_model=AuthProfileResponse)
def profile(principal=Depends(security.require_admin_principal)) -> AuthProfileResponse:  # noqa: B008
    return AuthProfileResponse(
        username=principal.subject,
        permissions=sorted(principal.permissions),
        root="*" in principal.permissions,
    )
