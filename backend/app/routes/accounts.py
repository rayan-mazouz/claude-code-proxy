# Path: app/routes/accounts.py
# Description: Pooled subscription account management -- OAuth add flow, listing with live quota, quota refresh, and
#              enable / disable / delete. Admin-only.

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import config
from app.logger import get_logger
from app.utils import crypto, egress, notifications, oauth, provider_health, rotation, security, usage, warmup
from app.utils.models.api import (
    Account,
    AccountResponse,
    AccountStatus,
    BulkAccountPriorityRequest,
    EgressTargetInfo,
    ListAccountsResponse,
    ListEgressTargetsResponse,
    OAuthCompleteRequest,
    OAuthStartResponse,
    ReauthCompleteRequest,
    ReorderAccountsRequest,
    UpdateAccountRequest,
)
from app.utils.postgres import AccountDb, UsageRecordDb, get_db

# Get the logger
logger = get_logger()

router = APIRouter(tags=["Accounts"], prefix="/accounts")


@router.get("/egress-targets", response_model=ListEgressTargetsResponse)
def list_egress_targets(
    _: str = Depends(security.require_admin),  # noqa: B008
) -> ListEgressTargetsResponse:
    """List safe outbound-path metadata for the account selector."""
    return ListEgressTargetsResponse(targets=[EgressTargetInfo(**target.public_dict()) for target in egress.get_pool().targets()])


def _account_egress_target(account: AccountDb) -> egress.EgressTarget:
    return egress.get_pool().resolve(account.egress_target_id)


def _next_priority(db: Session) -> int:
    highest = db.query(AccountDb).order_by(AccountDb.priority.desc()).first()
    return (highest.priority if highest is not None else 0) + 1


def _build_account(db: Session, account: AccountDb) -> Account:
    return Account.from_db(
        account,
        usage.spend_usage(db, account_id=account.id),
        usage.spend_usage(db, account_id=account.id, month_to_date=True),
    )


def _build_accounts(db: Session, accounts: list[AccountDb]) -> list[Account]:
    """Build account cards with one batched spend aggregation."""
    spend = usage.spend_usage_rollups(db, UsageRecordDb.account_id, [account.id for account in accounts])
    return [Account.from_db(account, *spend.get(account.id, (0.0, 0.0))) for account in accounts]


def _move_to_priority(db: Session, account: AccountDb, requested: int) -> None:
    ordered = (
        db.query(AccountDb)
        .filter(AccountDb.id != account.id)
        .order_by(AccountDb.priority.asc(), AccountDb.created_at.asc(), AccountDb.id.asc())
        .all()
    )
    ordered.insert(min(max(requested - 1, 0), len(ordered)), account)
    for position, candidate in enumerate(ordered, start=1):
        candidate.priority = position


@router.get(
    "",
    response_model=ListAccountsResponse,
    responses={
        200: {"description": "Accounts retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def list_accounts(
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListAccountsResponse:
    """List every pooled account in request-routing priority order."""
    accounts = db.query(AccountDb).order_by(AccountDb.priority.asc(), AccountDb.created_at.asc()).all()
    changed = any(rotation.normalize_expired_cooldown(account) for account in accounts)
    if changed:
        db.commit()
    return ListAccountsResponse(accounts=_build_accounts(db, accounts))


@router.put("/priorities/bulk", response_model=ListAccountsResponse)
def bulk_set_account_priority(
    request: BulkAccountPriorityRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListAccountsResponse:
    """Assign one priority to multiple accounts atomically without renumbering others."""
    if len(request.account_ids) != len(set(request.account_ids)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Account IDs must be unique")
    selected = db.query(AccountDb).filter(AccountDb.id.in_(request.account_ids)).with_for_update().all()
    selected_ids = {account.id for account in selected}
    requested_ids = set(request.account_ids)
    if selected_ids != requested_ids:
        missing = requested_ids - selected_ids
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Account(s) not found: {', '.join(map(str, missing))}")
    now = datetime.now(timezone.utc)
    for account in selected:
        account.priority = request.priority
        account.updated_at = now
    db.commit()
    ordered = db.query(AccountDb).order_by(AccountDb.priority.asc(), AccountDb.created_at.asc()).all()
    return ListAccountsResponse(accounts=_build_accounts(db, ordered))


@router.put("/priorities", response_model=ListAccountsResponse)
def reorder_accounts(
    request: ReorderAccountsRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListAccountsResponse:
    """Atomically replace the complete account priority order."""
    accounts = db.query(AccountDb).all()
    requested = request.account_ids
    if len(requested) != len(set(requested)) or set(requested) != {account.id for account in accounts}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Priority order must contain every account exactly once.",
        )
    by_id = {account.id: account for account in accounts}
    now = datetime.now(timezone.utc)
    for priority, account_id in enumerate(requested, start=1):
        by_id[account_id].priority = priority
        by_id[account_id].updated_at = now
    db.commit()
    ordered = [by_id[account_id] for account_id in requested]
    return ListAccountsResponse(accounts=_build_accounts(db, ordered))


@router.post(
    "/oauth/start",
    response_model=OAuthStartResponse,
    responses={
        200: {"description": "Authorization URL generated"},
        401: {"description": "Admin authentication required"},
    },
)
def start_oauth(_: str = Depends(security.require_admin)) -> OAuthStartResponse:  # noqa: B008
    """Begin adding an account: return the authorize URL to open and the PKCE verifier to pass back on completion."""
    verifier, challenge = oauth.generate_pkce()
    return OAuthStartResponse(authorize_url=oauth.build_authorize_url(verifier, challenge), verifier=verifier)


@router.post(
    "/oauth/complete",
    response_model=AccountResponse,
    responses={
        200: {"description": "Account added successfully"},
        400: {"description": "OAuth token exchange failed"},
        401: {"description": "Admin authentication required"},
    },
)
def complete_oauth(
    request: OAuthCompleteRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Finish adding an account from the pasted `code#state` value, enriching email and quota best-effort."""
    try:
        target = egress.get_pool().resolve(None)
        tokens = oauth.exchange_code(request.code, request.verifier, egress_target=target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"OAuth token exchange failed: {exc}") from exc

    access_token = tokens["access_token"]

    # Best-effort enrichment of email + subscription tier from the OAuth profile.
    email = None
    tier = None
    try:
        profile = oauth.fetch_profile(access_token, egress_target=target)
        email = oauth.extract_email(profile)
        tier = oauth.extract_tier(profile)
    except Exception:  # noqa: BLE001
        pass

    now = datetime.now(timezone.utc)
    account = AccountDb(
        id=uuid.uuid4(),
        label=request.label,
        account_email=email,
        tier=tier,
        access_token_enc=crypto.encrypt(access_token),
        refresh_token_enc=crypto.encrypt(tokens["refresh_token"]),
        expires_at=tokens["expires_at"],
        status=AccountStatus.ACTIVE,
        five_hour_rotation_threshold=config.DEFAULT_FIVE_HOUR_ROTATION_THRESHOLD,
        weekly_rotation_threshold=config.DEFAULT_WEEKLY_ROTATION_THRESHOLD,
        rotation_threshold=config.DEFAULT_ROTATION_THRESHOLD,
        cooldown_seconds=config.DEFAULT_COOLDOWN_SECONDS,
        max_failover_attempts=config.DEFAULT_MAX_FAILOVER_ATTEMPTS,
        egress_target_id=target.id,
        priority=_next_priority(db),
        created_at=now,
        updated_at=now,
    )

    limit_reached = False
    try:
        limit_reached = rotation.apply_usage_probe(account, oauth.fetch_usage(access_token, egress_target=target))
        provider_health.mark_success(account)
    except Exception as exc:  # noqa: BLE001
        provider_health.mark_failure(account, exc)

    db.add(account)
    db.commit()
    try:
        notifications.enqueue_account_added(db, account, config.get_settings().FRONTEND_ORIGIN)
        if limit_reached:
            notifications.enqueue_account_hard_limit(db, account, config.get_settings().FRONTEND_ORIGIN)
        else:
            notifications.enqueue_account_threshold(db, account, config.get_settings().FRONTEND_ORIGIN)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Could not queue notifications for newly added account")

    logger.info(f"Added account '{account.label}'")
    return AccountResponse(account=_build_account(db, account))


@router.post(
    "/{account_id}/oauth/complete",
    response_model=AccountResponse,
    responses={
        200: {"description": "Account re-authenticated"},
        400: {"description": "OAuth token exchange failed"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
    },
)
def reauth_account(
    account_id: uuid.UUID,
    request: ReauthCompleteRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Re-authenticate an existing account: swap in fresh OAuth tokens (e.g. after a refresh token expired).

    Reuses the same /oauth/start flow as adding an account; here the pasted code updates the account in place,
    clears any cooldown, re-activates it, and re-probes tier/email/quota best-effort. The label is unchanged.
    """
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    rotation.normalize_expired_cooldown(account)

    try:
        target = _account_egress_target(account)
        tokens = oauth.exchange_code(request.code, request.verifier, egress_target=target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"OAuth token exchange failed: {exc}") from exc

    access_token = tokens["access_token"]

    account.access_token_enc = crypto.encrypt(access_token)
    account.refresh_token_enc = crypto.encrypt(tokens["refresh_token"])
    account.expires_at = tokens["expires_at"]
    account.status = AccountStatus.ACTIVE
    account.cooldown_until = None
    account.provider_health = provider_health.ProviderHealth.UNKNOWN
    account.provider_health_code = None
    account.provider_health_message = None
    account.provider_health_failure_count = 0

    # Best-effort refresh of email + tier from the new token's profile.
    try:
        profile = oauth.fetch_profile(access_token, egress_target=target)
        account.account_email = oauth.extract_email(profile) or account.account_email
        account.tier = oauth.extract_tier(profile) or account.tier
    except Exception:  # noqa: BLE001
        pass

    limit_reached = False
    try:
        limit_reached = rotation.apply_usage_probe(account, oauth.fetch_usage(access_token, egress_target=target))
        provider_health.mark_success(account)
    except Exception as exc:  # noqa: BLE001
        provider_health.mark_failure(account, exc)

    account.updated_at = datetime.now(timezone.utc)
    if limit_reached:
        notifications.enqueue_account_hard_limit(db, account, config.get_settings().FRONTEND_ORIGIN)
    else:
        notifications.enqueue_account_threshold(db, account, config.get_settings().FRONTEND_ORIGIN)
    db.commit()

    logger.info(f"Re-authenticated account '{account.label}'")
    return AccountResponse(account=_build_account(db, account))


@router.put(
    "/{account_id}",
    response_model=AccountResponse,
    responses={
        200: {"description": "Account updated"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
    },
)
def update_account(
    account_id: uuid.UUID,
    request: UpdateAccountRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Update an account's editable label and rotation policy."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    rotation.normalize_expired_cooldown(account)

    if request.label is not None:
        label = request.label.strip()
        account.label = label

    pool = egress.get_pool()
    if "egress_target_id" in request.model_fields_set:
        target_id = request.egress_target_id.strip() if request.egress_target_id else pool.default_target().id
        if not pool.has_target(target_id):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Egress target '{target_id}' is not configured or enabled",
            )
        account.egress_target_id = target_id
    elif account.egress_target_id is None:
        account.egress_target_id = pool.default_target().id

    # Rotation policy: apply each value the client actually sent. These columns are NOT NULL, so a null/omitted
    # field leaves the current value untouched rather than clearing it.
    if request.five_hour_rotation_threshold is not None:
        account.five_hour_rotation_threshold = request.five_hour_rotation_threshold
    if request.weekly_rotation_threshold is not None:
        account.weekly_rotation_threshold = request.weekly_rotation_threshold
    if request.rotation_threshold is not None:
        account.rotation_threshold = request.rotation_threshold
        account.five_hour_rotation_threshold = request.rotation_threshold
        account.weekly_rotation_threshold = request.rotation_threshold
    else:
        account.rotation_threshold = min(account.five_hour_rotation_threshold, account.weekly_rotation_threshold)
    if request.authenticated_override is not None:
        account.authenticated_override = request.authenticated_override
    if request.warmup_enabled is not None:
        if account.warmup_enabled != request.warmup_enabled:
            account.warmup_next_at = None
        account.warmup_enabled = request.warmup_enabled
    if request.cooldown_seconds is not None:
        account.cooldown_seconds = request.cooldown_seconds
    if request.max_failover_attempts is not None:
        account.max_failover_attempts = request.max_failover_attempts
    if request.priority is not None:
        # Priority levels are intentionally non-unique; editing one account
        # must not renumber every other account in the pool.
        account.priority = request.priority

    account.updated_at = datetime.now(timezone.utc)
    notifications.enqueue_account_threshold(db, account, config.get_settings().FRONTEND_ORIGIN)
    db.commit()

    logger.info(f"Updated account '{account.label}'")
    return AccountResponse(account=_build_account(db, account))


@router.post(
    "/{account_id}/refresh-quota",
    response_model=AccountResponse,
    responses={
        200: {"description": "Quota refreshed"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
        502: {"description": "Quota probe failed"},
    },
)
def refresh_quota(
    account_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Refresh an account's quota via the zero-spend usage probe."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    # Getting a fresh token can itself fail if the stored refresh token was revoked/expired. That path runs before the
    # probe, so guard it separately -- otherwise the upstream OAuth rejection escapes as a raw 500 instead of an
    # actionable message. The remedy is the account's "Re-authenticate" action.
    try:
        target = _account_egress_target(account)
        access_token = rotation.ensure_fresh_token(db, account, egress_target=target)
    except provider_health.ProviderReauthenticationRequired as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        provider_health.persist_failure(db, account.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Token refresh failed temporarily.") from exc

    try:
        rotation.apply_usage_probe(account, oauth.fetch_usage(access_token, egress_target=target))
        provider_health.mark_success(account)
    except oauth.httpx.HTTPStatusError as exc:
        provider_health.persist_failure(db, account.id, exc)
        # A 429 here means Anthropic's usage endpoint is rate-limiting -- it does NOT mean the account is out of
        # quota (inference still works). Already retried with backoff in fetch_usage; report it as a soft, transient
        # condition rather than a raw upstream error string.
        if exc.response.status_code == 429:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Anthropic's usage endpoint is rate-limiting right now; quota refreshes automatically and "
                "will catch up shortly. Try again in a minute.",
            ) from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Quota probe failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        provider_health.persist_failure(db, account.id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Quota probe failed temporarily.") from exc

    # Refresh email + subscription tier too (best-effort; never fail the quota refresh over it).
    try:
        profile = oauth.fetch_profile(access_token, egress_target=target)
        account.tier = oauth.extract_tier(profile) or account.tier
        account.account_email = oauth.extract_email(profile) or account.account_email
    except Exception:  # noqa: BLE001
        pass

    account.updated_at = datetime.now(timezone.utc)
    notifications.enqueue_account_threshold(db, account, config.get_settings().FRONTEND_ORIGIN)
    db.commit()
    return AccountResponse(account=_build_account(db, account))


@router.post("/{account_id}/warmup", response_model=AccountResponse)
def warmup_account(
    account_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Start this account's five-hour provider window immediately."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    try:
        warmup.warm_account(db, account)
    except warmup.WarmupNotEligible as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except warmup.WarmupBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except warmup.WarmupFailed as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return AccountResponse(account=_build_account(db, account))


@router.post(
    "/{account_id}/disable",
    response_model=AccountResponse,
    responses={
        200: {"description": "Account disabled"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
    },
)
def disable_account(
    account_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Park an account (e.g. after a ban) so rotation skips it."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account.status = AccountStatus.DISABLED
    account.cooldown_until = None
    account.warmup_next_at = None
    account.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Disabled account '{account.label}'")
    return AccountResponse(account=_build_account(db, account))


@router.post(
    "/{account_id}/enable",
    response_model=AccountResponse,
    responses={
        200: {"description": "Account enabled"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
    },
)
def enable_account(
    account_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> AccountResponse:
    """Return a parked account to the rotation pool."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account.status = AccountStatus.ACTIVE
    account.cooldown_until = None
    account.warmup_next_at = None
    account.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Enabled account '{account.label}'")
    return AccountResponse(account=_build_account(db, account))


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Account deleted"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Account not found"},
    },
)
def delete_account(
    account_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Remove an account without renumbering the remaining priority lanes."""
    account = db.query(AccountDb).filter(AccountDb.id == account_id).first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    db.query(UsageRecordDb).filter(UsageRecordDb.account_id == account_id).update({UsageRecordDb.account_id: None}, synchronize_session=False)
    db.delete(account)
    db.flush()
    # Priority is a lane, not a unique rank. Deleting an account must not
    # renumber unrelated accounts or silently change their routing policy.
    db.commit()

    logger.info(f"Deleted account '{account.label}'")
