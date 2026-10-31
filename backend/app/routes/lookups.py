"""Small, non-sensitive lookup lists used by analytics filters."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.utils import security
from app.utils.models.api import TimeRange
from app.utils.postgres import UsageRecordDb, UserDb, get_db

router = APIRouter(tags=["Dashboard lookups"], prefix="/lookups")


@router.get("/users")
def list_user_lookups(
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Return only identity fields needed by overview and event filters."""
    users = db.query(UserDb.id, UserDb.name).order_by(UserDb.name.asc()).all()
    return {"users": [{"id": str(user_id), "name": name} for user_id, name in users]}


@router.get("/models")
def list_model_lookups(
    rng: TimeRange = Depends(TimeRange.get_request),
    _: str = Depends(security.require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Only models with recorded activity in the selected range/user scope."""
    query = db.query(UsageRecordDb.model)
    if rng.start is not None:
        query = query.filter(UsageRecordDb.created_at >= rng.start)
    if rng.end is not None:
        query = query.filter(UsageRecordDb.created_at < rng.end)
    if rng.user_id is not None:
        query = query.filter(UsageRecordDb.user_id == rng.user_id)
    return {"models": [row.model for row in query.distinct().order_by(UsageRecordDb.model).all()]}


@router.get("")
def analytics_lookups(
    rng: TimeRange = Depends(TimeRange.get_request),
    _: str = Depends(security.require_admin),
    db: Session = Depends(get_db),
) -> dict:
    return {**list_user_lookups(_=_, db=db), **list_model_lookups(rng=rng, _=_, db=db)}
