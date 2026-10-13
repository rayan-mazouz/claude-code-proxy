"""Dashboard team-member management with explicit permission assignment."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils import security
from app.utils.models.api import (
    CreateDashboardMemberRequest,
    DashboardMember,
    DashboardMemberResponse,
    ListDashboardMembersResponse,
    UpdateDashboardMemberRequest,
)
from app.utils.postgres import DashboardMemberDb, get_db

router = APIRouter(tags=["Dashboard Team"], prefix="/team")


def _member_response(member: DashboardMemberDb) -> DashboardMember:
    return DashboardMember(
        id=member.id,
        username=member.username,
        permissions=sorted(_member_permissions(member)),
        active=member.active,
        created_at=member.created_at,
        last_login_at=member.last_login_at,
    )


def _member_permissions(member: DashboardMemberDb) -> frozenset[str]:
    try:
        return security.normalize_permissions(json.loads(member.permissions_json or "[]"))
    except (TypeError, ValueError):
        return frozenset()


def _require_delegable(principal, permissions: frozenset[str] | set[str] | list[str]) -> None:
    """A member may only grant, or manage someone holding, permissions they hold themselves.

    Without this rule anyone allowed to manage the team could create an account with every permission and use it
    to take over pooled accounts or API keys. The environment-configured owner holds the wildcard and is exempt.
    """
    if "*" in principal.permissions:
        return
    missing = sorted(set(permissions) - set(principal.permissions))
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot grant or manage permissions you do not hold: {', '.join(missing)}",
        )


def _validate_permissions(values: list[str]) -> list[str]:
    requested = {value.strip() for value in values if isinstance(value, str) and value.strip()}
    permissions = sorted(security.normalize_permissions(requested))
    unknown = sorted(value for value in requested if not security.normalize_permissions([value]))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown permission(s): {', '.join(unknown)}",
        )
    if not permissions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Select at least one permission",
        )
    return permissions


@router.get("/permissions")
def list_permissions(_: object = Depends(security.require_admin_principal)) -> dict:  # noqa: B008
    """Return the exact permission names available to assign."""
    return {"permissions": sorted(security.PERMISSIONS)}


@router.get("/members", response_model=ListDashboardMembersResponse)
def list_members(
    _: object = Depends(security.require_admin_principal),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListDashboardMembersResponse:
    members = db.query(DashboardMemberDb).order_by(DashboardMemberDb.created_at.asc()).all()
    return ListDashboardMembersResponse(members=[_member_response(member) for member in members])


@router.post("/members", response_model=DashboardMemberResponse, status_code=status.HTTP_201_CREATED)
def create_member(
    request: CreateDashboardMemberRequest,
    principal=Depends(security.require_admin_principal),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DashboardMemberResponse:
    username = request.username.strip().lower()
    if username == get_settings().ADMIN_USERNAME.strip().lower():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dashboard username is reserved")
    if db.query(DashboardMemberDb).filter(DashboardMemberDb.username == username).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dashboard username already exists")
    permissions = _validate_permissions(request.permissions)
    _require_delegable(principal, permissions)
    now = datetime.now(timezone.utc)
    member = DashboardMemberDb(
        id=uuid.uuid4(),
        username=username,
        password_hash=security.hash_password(request.password),
        permissions_json=json.dumps(permissions, separators=(",", ":")),
        active=True,
        created_at=now,
        updated_at=now,
    )
    db.add(member)
    db.commit()
    return DashboardMemberResponse(member=_member_response(member))


@router.put("/members/{member_id}", response_model=DashboardMemberResponse)
def update_member(
    member_id: uuid.UUID,
    request: UpdateDashboardMemberRequest,
    principal=Depends(security.require_admin_principal),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DashboardMemberResponse:
    member = db.get(DashboardMemberDb, member_id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard member not found")
    _require_delegable(principal, _member_permissions(member))
    if request.permissions is not None:
        permissions = _validate_permissions(request.permissions)
        _require_delegable(principal, permissions)
        member.permissions_json = json.dumps(permissions, separators=(",", ":"))
    if request.password is not None:
        member.password_hash = security.hash_password(request.password)
    if request.active is not None:
        if principal.member_id == member.id and request.active is False:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="You cannot disable your own account")
        member.active = request.active
    member.updated_at = datetime.now(timezone.utc)
    db.commit()
    return DashboardMemberResponse(member=_member_response(member))


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(
    member_id: uuid.UUID,
    principal=Depends(security.require_admin_principal),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    if principal.member_id == member_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="You cannot delete your own account")
    member = db.get(DashboardMemberDb, member_id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard member not found")
    _require_delegable(principal, _member_permissions(member))
    db.delete(member)
    db.commit()
