# Path: app/routes/stats.py
# Description: Dashboard statistics -- overview counts, the paginated usage log, and activity analytics
#              (daily/hourly series, hour-of-day distribution, per-user rollups). All accept an optional
#              [start, end) range over UsageRecord.created_at; each endpoint applies a sensible default
#              when a bound is omitted. Admin-only.

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends
from sqlalchemy import JSON, and_, cast, func, select
from sqlalchemy.orm import Session

from app import pricing
from app.logger import get_logger
from app.utils import rotation, security, usage
from app.utils.models.api import (
    AccountStatus,
    ActivityPoint,
    ActivityResponse,
    ByUserResponse,
    DistributionResponse,
    HourlyPoint,
    HourlyResponse,
    HourlyUser,
    HourlyUserSlice,
    ListUsageRequest,
    ListUsageResponse,
    OverviewResponse,
    PercentileBreakdown,
    ProviderHealth,
    ThinkingLevelMixResponse,
    ThinkingLevelSlice,
    TimeRange,
    UsageRecord,
    UserThinkingLevelMix,
    UserUsage,
)
from app.utils.models.api.stats import ModelMixResponse, ModelSlice, UserModelMix
from app.utils.postgres import AccountDb, AnthropicFallbackDb, ApiKeyDb, ProxyEventDb, UsageRecordDb, UserDb, get_db

# Get the logger
logger = get_logger()

router = APIRouter(tags=["Stats"], prefix="/stats")

NOT_RECORDED_THINKING_LEVEL = "not_recorded"


def _tokens_sum():
    return usage.token_sum_expr()


def _routed_model_expr():
    """Use the routed model for presentation while retaining provider labels for accounting."""
    event_model = (
        select(func.json_extract_path_text(cast(ProxyEventDb.metadata_json, JSON), "model"))
        .where(
            ProxyEventDb.request_id == UsageRecordDb.request_id,
            ProxyEventDb.event_type == "response.returned",
        )
        .order_by(ProxyEventDb.created_at.desc())
        .limit(1)
        .correlate(UsageRecordDb)
        .scalar_subquery()
    )
    return func.coalesce(event_model, UsageRecordDb.model)


def _scope_clauses(
    start: Optional[datetime],
    end: Optional[datetime],
    user_id: Optional[uuid.UUID] = None,
    model: Optional[str] = None,
) -> List:
    """WHERE clauses scoping the stats queries: the [start, end) window (each bound optional) plus an optional
    user filter. count_tokens calls are no longer recorded so no model-null exclusion is needed.
    """
    clauses: List = []
    if start is not None:
        clauses.append(UsageRecordDb.created_at >= start)
    if end is not None:
        clauses.append(UsageRecordDb.created_at < end)
    if user_id is not None:
        clauses.append(UsageRecordDb.user_id == user_id)
    if model:
        clauses.append(UsageRecordDb.model == model.strip())
    return clauses


@router.get(
    "/overview",
    response_model=OverviewResponse,
    responses={
        200: {"description": "Overview retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def overview(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> OverviewResponse:
    """Headline account/user/key counts plus tokens, request volume, and API-equivalent value over the range.

    With no range the usage figures default to month-to-date. The account/user/key counts are pool inventory and
    are never range-filtered.
    """
    now = datetime.now(timezone.utc)
    start, end = rng.start, rng.end
    if start is None and end is None:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    clauses = _scope_clauses(start, end, rng.user_id, rng.model)

    requests = db.query(UsageRecordDb).filter(*clauses).count()
    average_tps_value = db.query(func.avg(UsageRecordDb.tokens_per_second)).filter(*clauses).scalar()

    # Per-model token sums over the range -> 4-way breakdown + API-equivalent cost (priced per model).
    rows = (
        db.query(
            UsageRecordDb.model,
            UsageRecordDb.fallback_provider_id,
            func.coalesce(func.sum(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.cache_creation_input_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.cache_read_input_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.cache_creation_5m_input_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.cache_creation_1h_input_tokens), 0),
            func.coalesce(func.sum(UsageRecordDb.billed_cost_usd), 0.0),
        )
        .filter(*clauses)
        .group_by(UsageRecordDb.model, UsageRecordDb.fallback_provider_id)
        .all()
    )

    in_tok = out_tok = cached_in_tok = 0
    cost = 0.0
    for model, fallback_provider_id, mi, mo, mcw, mcr, m5m, m1h, billed_cost in rows:
        mi, mo, mcw, mcr, m5m, m1h = int(mi), int(mo), int(mcw), int(mcr), int(m5m), int(m1h)
        in_tok += mi  # already includes cache tokens (recorded that way)
        out_tok += mo
        cached_in_tok += max(0, mcr)
        if fallback_provider_id is not None:
            cost += float(billed_cost or 0.0)
        else:
            cost += pricing.cost_usd(
                model=model,
                input_tokens=mi - mcw - mcr,  # strip cache portions — they're priced at their own rates below
                output_tokens=mo,
                cache_read_input_tokens=mcr,
                cache_creation_5m_input_tokens=m5m,
                cache_creation_1h_input_tokens=m1h,
                cache_creation_input_tokens=mcw,
            )

    accounts = db.query(AccountDb).all()
    # Dashboard inventory counts every enabled account with valid authentication,
    # including accounts in a temporary cooldown. Immediate routing eligibility is
    # reported separately through usable_accounts.
    active_accounts = [
        account for account in accounts if account.status != AccountStatus.DISABLED and account.provider_health != ProviderHealth.REAUTH_REQUIRED
    ]
    usable_accounts = [account for account in accounts if rotation.is_usable(account, now)]

    def average_window_pct(field: str) -> float:
        values = [getattr(account, field) for account in active_accounts if getattr(account, field) is not None]
        return round(sum(values) / len(values) * 100.0, 2) if values else 0.0

    def earliest_reset_at(field: str) -> datetime | None:
        resets: list[datetime] = []
        for account in active_accounts:
            reset_at = getattr(account, field)
            if reset_at is None:
                continue
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            if reset_at > now:
                resets.append(reset_at)
        return min(resets, default=None)

    # A weekly-exhausted account has no usable five-hour capacity even when
    # the provider's cached five-hour field is stale or lower. Count it as
    # fully used so the pool average reflects the capacity users can actually
    # consume.
    five_hour_values = []
    for account in active_accounts:
        if account.weekly_used_pct is not None and account.weekly_used_pct >= 1.0:
            five_hour_values.append(1.0)
            continue
        if account.session_used_pct is not None:
            five_hour_values.append(account.session_used_pct)
    five_hour_average_pct = round(sum(five_hour_values) / len(five_hour_values) * 100.0, 2) if five_hour_values else 0.0
    weekly_average_pct = average_window_pct("weekly_used_pct")
    monthly_average_pct = average_window_pct("monthly_used_pct")
    five_hour_reset_at = earliest_reset_at("session_reset_at")
    weekly_reset_at = earliest_reset_at("weekly_reset_at")
    monthly_reset_at = earliest_reset_at("monthly_reset_at")
    if active_accounts:
        pool_used_pct = round(
            sum(max(0.0, min(1.0, rotation.account_load(account))) for account in active_accounts) / len(active_accounts),
            6,
        )
        pool_remaining_pct = round(1.0 - pool_used_pct, 6)
    else:
        pool_used_pct = 0.0
        pool_remaining_pct = 0.0

    total_flow = in_tok + out_tok
    input_rate_pct = round(in_tok / total_flow * 100.0, 2) if total_flow > 0 else 0.0
    output_rate_pct = round(out_tok / total_flow * 100.0, 2) if total_flow > 0 else 0.0
    cache_hit_rate = min(cached_in_tok, in_tok) / in_tok if in_tok > 0 else 0.0

    return OverviewResponse(
        total_accounts=len(accounts),
        active_accounts=len(active_accounts),
        usable_accounts=len(usable_accounts),
        five_hour_average_pct=five_hour_average_pct,
        weekly_average_pct=weekly_average_pct,
        monthly_average_pct=monthly_average_pct,
        five_hour_reset_at=five_hour_reset_at,
        weekly_reset_at=weekly_reset_at,
        monthly_reset_at=monthly_reset_at,
        pool_used_pct=pool_used_pct,
        pool_remaining_pct=pool_remaining_pct,
        total_users=db.query(UserDb).count(),
        active_users=db.query(UserDb).filter(UserDb.active.is_(True)).count(),
        total_keys=db.query(ApiKeyDb).count(),
        tokens=in_tok + out_tok,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_input_tokens=cached_in_tok,
        input_output_ratio=round(in_tok / out_tok, 4) if out_tok > 0 else None,
        cache_hit_rate=round(cache_hit_rate, 6),
        input_rate_pct=input_rate_pct,
        output_rate_pct=output_rate_pct,
        cache_hit_rate_pct=round(cache_hit_rate * 100.0, 2),
        api_equivalent_cost_usd=round(cost, 2),
        requests=requests,
        average_tps=round(float(average_tps_value), 2) if average_tps_value is not None else None,
    )


def _usage_rows(db: Session, rng: TimeRange, limit: Optional[int] = None, offset: int = 0):
    """Usage-log rows (newest first) joined to user/key/account labels, filtered to the range."""
    query = (
        db.query(UsageRecordDb, UserDb.name, ApiKeyDb.label, AccountDb.label, AnthropicFallbackDb.label)
        .outerjoin(UserDb, UsageRecordDb.user_id == UserDb.id)
        .outerjoin(ApiKeyDb, UsageRecordDb.api_key_id == ApiKeyDb.id)
        .outerjoin(AccountDb, UsageRecordDb.account_id == AccountDb.id)
        .outerjoin(AnthropicFallbackDb, UsageRecordDb.fallback_provider_id == AnthropicFallbackDb.id)
        .filter(*_scope_clauses(rng.start, rng.end, rng.user_id, rng.model))
        .order_by(UsageRecordDb.created_at.desc())
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


@router.get(
    "/usage",
    response_model=ListUsageResponse,
    responses={
        200: {"description": "Usage log retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def usage_log(
    request: ListUsageRequest = Depends(ListUsageRequest.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListUsageResponse:
    """Request log over the range, newest first, each row carrying the user name, key label, and account label."""
    rng = TimeRange(start=request.start, end=request.end, user_id=request.user_id, model=request.model)
    total = db.query(UsageRecordDb).filter(*_scope_clauses(rng.start, rng.end, rng.user_id, rng.model)).count()
    rows = _usage_rows(db, rng, limit=request.limit, offset=request.offset)
    items = [
        UsageRecord.from_db(record, user_name, key_label, account_label, fallback_label)
        for record, user_name, key_label, account_label, fallback_label in rows
    ]
    return ListUsageResponse(items=items, total=total, limit=request.limit, offset=request.offset)


@router.get(
    "/activity",
    response_model=ActivityResponse,
    responses={
        200: {"description": "Activity series retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def activity(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ActivityResponse:
    """Requests and tokens bucketed over the range, zero-filled. Short windows (<=2 days) bucket by hour, longer by day."""

    def _utc(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    end = _utc(rng.end) if rng.end is not None else now
    if rng.start is not None:
        start = _utc(rng.start)
    elif rng.end is not None:
        # An explicit end with no start is the dashboard's "All time" range. Anchor the
        # series at the first matching request instead of silently truncating it to 30 days.
        earliest = db.query(func.min(UsageRecordDb.created_at)).filter(*_scope_clauses(None, end, rng.user_id, rng.model)).scalar()
        start = _utc(earliest) if earliest is not None else end - timedelta(days=1)
    else:
        start = end - timedelta(days=30)
    if end <= start:
        end = start + timedelta(days=1)

    # Pick a granularity so a short window isn't collapsed into one or two bars.
    by_hour = (end - start) <= timedelta(days=2)
    granularity = "hour" if by_hour else "day"
    unit = timedelta(hours=1) if by_hour else timedelta(days=1)

    def floor(dt: datetime) -> datetime:
        if by_hour:
            return dt.replace(minute=0, second=0, microsecond=0)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    bucket_col = func.date_trunc(granularity, UsageRecordDb.created_at)
    scope = _scope_clauses(start, end, rng.user_id, rng.model)
    measured = [UsageRecordDb.input_tokens + UsageRecordDb.output_tokens > 0]
    measured.append(UsageRecordDb.usage_reported.is_not(False))
    # Compute all three percentiles together: one ordered-set transition per
    # token column, alongside volume totals in the same bucket scan.
    rows = (
        db.query(
            bucket_col.label("bucket"),
            _tokens_sum().label("tokens"),
            func.count(UsageRecordDb.id).label("requests"),
            func.count(UsageRecordDb.id).filter(*measured).label("samples"),
            func.percentile_cont([0.1, 0.5, 0.9]).within_group(UsageRecordDb.input_tokens).filter(*measured).label("input_quantiles"),
            func.percentile_cont([0.1, 0.5, 0.9]).within_group(UsageRecordDb.output_tokens).filter(*measured).label("output_quantiles"),
        )
        .filter(*scope)
        .group_by(bucket_col)
        .all()
    )
    # Counts and TPS share the same model/bucket grouping. Missing TPS stays
    # null while request counts still include requests without measured usage.
    model_rows = (
        db.query(
            bucket_col.label("bucket"),
            UsageRecordDb.model,
            func.count(UsageRecordDb.id).label("requests"),
            func.avg(UsageRecordDb.tokens_per_second).filter(UsageRecordDb.tokens_per_second > 0).label("average_tps"),
        )
        .filter(*scope)
        .group_by(bucket_col, UsageRecordDb.model)
        .all()
    )
    user_rows = (
        db.query(bucket_col.label("bucket"), UsageRecordDb.user_id, UserDb.name, func.count(UsageRecordDb.id).label("requests"))
        .outerjoin(UserDb, UsageRecordDb.user_id == UserDb.id)
        .filter(*scope)
        .group_by(bucket_col, UsageRecordDb.user_id, UserDb.name)
        .all()
    )

    def key(value: datetime) -> datetime:
        return floor(value).replace(tzinfo=None)

    by_bucket = {key(row.bucket): row for row in rows}
    model_counts, tps_by_bucket = {}, {}
    for row in model_rows:
        model_counts.setdefault(key(row.bucket), {})[row.model] = int(row.requests)
        if row.average_tps is not None:
            tps_by_bucket.setdefault(key(row.bucket), {})[row.model] = round(float(row.average_tps), 2)
    user_counts, user_names = {}, {}
    for row in user_rows:
        uid = str(row.user_id)
        user_counts.setdefault(key(row.bucket), {})[uid] = int(row.requests)
        user_names[uid] = row.name or "Deleted user"

    def distribution_values(row, side):
        if row is None or not row.samples:
            return None
        quantiles = getattr(row, f"{side}_quantiles")
        return {"samples": int(row.samples), "p10": float(quantiles[0]), "median": float(quantiles[1]), "p90": float(quantiles[2])}

    points = []
    cursor = floor(start)
    while cursor < end:
        bucket_key = cursor.replace(tzinfo=None)
        row = by_bucket.get(bucket_key)
        points.append(
            ActivityPoint(
                ts=cursor.astimezone(timezone.utc).isoformat(),
                requests=int(row.requests) if row is not None else 0,
                tokens=int(row.tokens or 0) if row is not None else 0,
                model_tps=tps_by_bucket.get(bucket_key, {}),
                requests_by_model=model_counts.get(bucket_key, {}),
                requests_by_user=user_counts.get(bucket_key, {}),
                input_distribution=distribution_values(row, "input"),
                output_distribution=distribution_values(row, "output"),
            )
        )
        cursor += unit
    return ActivityResponse(granularity=granularity, points=points, user_names=user_names)


@router.get(
    "/hourly",
    response_model=HourlyResponse,
    responses={
        200: {"description": "Hour-of-day distribution retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def hourly(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> HourlyResponse:
    """Request and token totals bucketed by hour of day (0..23, UTC) over the range -- shows peak hours.

    Includes a per-user breakdown so the frontend can render stacked bars.
    """
    hour_col = func.extract("hour", UsageRecordDb.created_at)
    clauses = _scope_clauses(rng.start, rng.end, rng.user_id, rng.model)

    totals = (
        db.query(hour_col.label("hour"), _tokens_sum().label("tokens"), func.count(UsageRecordDb.id).label("requests"))
        .filter(*clauses)
        .group_by(hour_col)
        .all()
    )
    by_hour = {int(row.hour): (int(row.tokens or 0), int(row.requests or 0)) for row in totals}

    per_user_rows = (
        db.query(
            hour_col.label("hour"),
            UserDb.id.label("user_id"),
            UserDb.name.label("user_name"),
            func.count(UsageRecordDb.id).label("requests"),
        )
        .join(UserDb, UsageRecordDb.user_id == UserDb.id)
        .filter(*clauses)
        .group_by(hour_col, UserDb.id, UserDb.name)
        .all()
    )

    user_hour: Dict[Tuple[int, str], HourlyUserSlice] = {}
    seen_users: Dict[str, str] = {}
    for row in per_user_rows:
        h = int(row.hour)
        uid = str(row.user_id)
        seen_users[uid] = row.user_name
        user_hour[(h, uid)] = HourlyUserSlice(user_id=row.user_id, user_name=row.user_name, requests=int(row.requests or 0))

    user_roster = sorted(seen_users.items(), key=lambda kv: kv[1])

    points = []
    for hour in range(24):
        tokens, requests = by_hour.get(hour, (0, 0))
        slices = [user_hour[(hour, uid)] for uid, _ in user_roster if (hour, uid) in user_hour]
        points.append(HourlyPoint(hour=hour, requests=requests, tokens=tokens, by_user=slices))

    users = [HourlyUser(user_id=uuid.UUID(uid), user_name=name) for uid, name in user_roster]
    return HourlyResponse(points=points, users=users)


@router.get(
    "/by-user",
    response_model=ByUserResponse,
    responses={
        200: {"description": "Per-user usage retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def by_user(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ByUserResponse:
    """Per-user totals (tokens, requests, last activity) over the range, busiest first.

    The range condition lives in the JOIN (not WHERE) so every user is still listed when no user filter is selected,
    with sums limited to the window. A user filter narrows the roster itself to prevent unrelated zero-usage users.
    """
    join_on = and_(UsageRecordDb.user_id == UserDb.id, *_scope_clauses(rng.start, rng.end, rng.user_id, rng.model))
    query = db.query(
        UserDb.id.label("user_id"),
        UserDb.name.label("user_name"),
        _tokens_sum().label("tokens"),
        func.count(UsageRecordDb.id).label("requests"),
        func.max(UsageRecordDb.created_at).label("last_used_at"),
        func.coalesce(func.sum(UsageRecordDb.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageRecordDb.output_tokens), 0).label("output_tokens"),
        func.avg(UsageRecordDb.tokens_per_second).label("average_tps"),
    ).outerjoin(UsageRecordDb, join_on)
    if rng.user_id is not None:
        query = query.filter(UserDb.id == rng.user_id)
    rows = query.group_by(UserDb.id, UserDb.name).order_by(_tokens_sum().desc()).all()

    users = [
        UserUsage(
            user_id=row.user_id,
            user_name=row.user_name,
            requests=int(row.requests or 0),
            tokens=int(row.tokens or 0),
            input_tokens=int(row.input_tokens or 0),
            output_tokens=int(row.output_tokens or 0),
            last_used_at=row.last_used_at,
        )
        for row in rows
    ]
    return ByUserResponse(users=users)


@router.get(
    "/distributions",
    response_model=DistributionResponse,
    responses={
        200: {"description": "Token distribution retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def distributions(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> DistributionResponse:
    """Percentile breakdown (min, P25, P50, P75, P90, P95, max) of per-request input and output tokens over the range."""
    clauses = _scope_clauses(rng.start, rng.end, rng.user_id, rng.model)

    row = (
        db.query(
            func.count(UsageRecordDb.id).label("total"),
            func.coalesce(func.min(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.percentile_cont(0.25).within_group(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.percentile_cont(0.50).within_group(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.percentile_cont(0.75).within_group(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.percentile_cont(0.90).within_group(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.percentile_cont(0.95).within_group(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.max(UsageRecordDb.input_tokens), 0),
            func.coalesce(func.min(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.percentile_cont(0.25).within_group(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.percentile_cont(0.50).within_group(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.percentile_cont(0.75).within_group(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.percentile_cont(0.90).within_group(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.percentile_cont(0.95).within_group(UsageRecordDb.output_tokens), 0),
            func.coalesce(func.max(UsageRecordDb.output_tokens), 0),
        )
        .filter(*clauses)
        .one()
    )

    total = int(row[0])
    zero = PercentileBreakdown(min=0, p25=0, p50=0, p75=0, p90=0, p95=0, max=0)
    if total == 0:
        return DistributionResponse(input_tokens=zero, output_tokens=zero, total_requests=0)

    return DistributionResponse(
        input_tokens=PercentileBreakdown(
            min=int(row[1]),
            p25=int(row[2]),
            p50=int(row[3]),
            p75=int(row[4]),
            p90=int(row[5]),
            p95=int(row[6]),
            max=int(row[7]),
        ),
        output_tokens=PercentileBreakdown(
            min=int(row[8]),
            p25=int(row[9]),
            p50=int(row[10]),
            p75=int(row[11]),
            p90=int(row[12]),
            p95=int(row[13]),
            max=int(row[14]),
        ),
        total_requests=total,
    )


@router.get(
    "/model-mix",
    response_model=ModelMixResponse,
    response_model_exclude_none=True,
    responses={
        200: {"description": "Per-user model breakdown retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def model_mix(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ModelMixResponse:
    """Per-user model breakdown (requests, input/output tokens) over the range, busiest model first per user."""
    routed_model = _routed_model_expr()
    scope = _scope_clauses(rng.start, rng.end, rng.user_id)
    if rng.model:
        scope.append(routed_model == rng.model.strip())
    join_on = and_(UsageRecordDb.user_id == UserDb.id, *scope)
    query = db.query(
        UserDb.id.label("user_id"),
        UserDb.name.label("user_name"),
        routed_model.label("model"),
        func.count(UsageRecordDb.id).label("requests"),
        func.coalesce(func.sum(UsageRecordDb.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageRecordDb.output_tokens), 0).label("output_tokens"),
        func.avg(UsageRecordDb.tokens_per_second).label("average_tps"),
    ).outerjoin(UsageRecordDb, join_on)
    if rng.user_id is not None:
        query = query.filter(UserDb.id == rng.user_id)
    rows = query.group_by(UserDb.id, UserDb.name, routed_model).order_by(UserDb.name, func.count(UsageRecordDb.id).desc()).all()

    users_map: dict = {}
    for row in rows:
        uid = str(row.user_id)
        if uid not in users_map:
            users_map[uid] = UserModelMix(
                user_id=row.user_id,
                user_name=row.user_name,
                total_requests=0,
                models=[],
            )
        reqs = int(row.requests or 0)
        if row.model is not None and reqs > 0:
            users_map[uid].total_requests += reqs
            users_map[uid].models.append(
                ModelSlice(
                    model=row.model,
                    requests=reqs,
                    input_tokens=int(row.input_tokens or 0),
                    output_tokens=int(row.output_tokens or 0),
                    average_tps=round(float(getattr(row, "average_tps", None)), 2) if getattr(row, "average_tps", None) is not None else None,
                )
            )

    users = sorted(users_map.values(), key=lambda u: u.total_requests, reverse=True)
    return ModelMixResponse(users=users)


@router.get(
    "/thinking-level-mix",
    response_model=ThinkingLevelMixResponse,
    responses={
        200: {"description": "Per-user thinking-level breakdown retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def thinking_level_mix(
    rng: TimeRange = Depends(TimeRange.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ThinkingLevelMixResponse:
    """Per-user thinking-level breakdown over the range, busiest level first per user.

    Requests created before thinking metadata was captured, or requests without an explicit
    thinking setting, are grouped as ``not_recorded`` so the breakdown always reconciles with
    the user's total request count.
    """
    join_on = and_(UsageRecordDb.user_id == UserDb.id, *_scope_clauses(rng.start, rng.end, rng.user_id, rng.model))
    level_col = func.coalesce(UsageRecordDb.reasoning_level, NOT_RECORDED_THINKING_LEVEL)
    query = db.query(
        UserDb.id.label("user_id"),
        UserDb.name.label("user_name"),
        level_col.label("level"),
        func.count(UsageRecordDb.id).label("requests"),
        func.coalesce(func.sum(UsageRecordDb.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageRecordDb.output_tokens), 0).label("output_tokens"),
    ).outerjoin(UsageRecordDb, join_on)
    if rng.user_id is not None:
        query = query.filter(UserDb.id == rng.user_id)
    rows = query.group_by(UserDb.id, UserDb.name, level_col).order_by(UserDb.name, func.count(UsageRecordDb.id).desc(), level_col).all()

    users_map: dict = {}
    for row in rows:
        uid = str(row.user_id)
        if uid not in users_map:
            users_map[uid] = UserThinkingLevelMix(
                user_id=row.user_id,
                user_name=row.user_name,
                total_requests=0,
                levels=[],
            )
        reqs = int(row.requests or 0)
        if reqs > 0:
            users_map[uid].total_requests += reqs
            users_map[uid].levels.append(
                ThinkingLevelSlice(
                    level=row.level,
                    requests=reqs,
                    input_tokens=int(row.input_tokens or 0),
                    output_tokens=int(row.output_tokens or 0),
                )
            )

    users = sorted(users_map.values(), key=lambda u: u.total_requests, reverse=True)
    return ThinkingLevelMixResponse(users=users)


# Bound analytics concurrency per worker. Each read gets its own SQLAlchemy
# session; inference credentials, routing state, and write paths are untouched.
_dashboard_reads = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dashboard-read")


def _dashboard_read(compute, rng, principal):
    from app.utils.postgres.base import SessionFactory

    with SessionFactory() as session:
        return compute(rng=rng, _=principal, db=session)


@router.get("/dashboard")
def dashboard_analytics(
    rng: TimeRange = Depends(TimeRange.get_request),
    _: str = Depends(security.require_admin),
) -> dict:
    """One round trip with bounded, independent, read-only analytics work."""
    from app.routes.lookups import analytics_lookups

    computations = {
        "overview": overview,
        "activity": activity,
        "hourly": hourly,
        "lookups": analytics_lookups,
        "distributions": distributions,
        "model_mix": model_mix,
        "thinking_level_mix": thinking_level_mix,
    }
    futures = {name: _dashboard_reads.submit(_dashboard_read, compute, rng, _) for name, compute in computations.items()}
    result = {}
    for name, future in futures.items():
        try:
            result[name] = future.result()
        except Exception:
            if name in {"overview", "activity", "hourly", "lookups"}:
                raise
            # Preserve the prior optional-panel behavior.
            logger.exception("Optional dashboard analytics unavailable: %s", name)
            result[name] = None
    return result
