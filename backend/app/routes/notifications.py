"""Admin routes for Telegram connection settings and notification event rules."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import config
from app.utils import crypto, notifications, security
from app.utils.models.api import (
    NotificationRule,
    NotificationSettingsResponse,
    TelegramSettings,
    TestNotificationResponse,
    UpdateNotificationRuleRequest,
    UpdateTelegramSettingsRequest,
)
from app.utils.postgres import NotificationChannelDb, NotificationRuleDb, get_db

router = APIRouter(tags=["Notifications"], prefix="/notifications")
settings = config.get_settings()


def _telegram_channel(db: Session) -> NotificationChannelDb | None:
    return db.query(NotificationChannelDb).filter(NotificationChannelDb.channel_type == notifications.TELEGRAM_CHANNEL_TYPE).first()


def _get_or_create_telegram(db: Session) -> NotificationChannelDb:
    channel = _telegram_channel(db)
    if channel is not None:
        return channel
    now = datetime.now(timezone.utc)
    channel = NotificationChannelDb(
        id=uuid.uuid4(),
        channel_type=notifications.TELEGRAM_CHANNEL_TYPE,
        name="Telegram",
        enabled=False,
        created_at=now,
        updated_at=now,
    )
    db.add(channel)
    db.flush()
    notifications.ensure_default_rules(db, channel)
    return channel


def _telegram_response(channel: NotificationChannelDb | None) -> TelegramSettings:
    if channel is None:
        return TelegramSettings(
            enabled=False,
            configured=False,
            chat_id=None,
            message_thread_id=None,
            timezone=notifications.DEFAULT_NOTIFICATION_TIMEZONE,
            last_success_at=None,
            last_error_at=None,
            last_error=None,
        )
    return TelegramSettings(
        enabled=channel.enabled,
        configured=bool(channel.secret_enc and channel.destination),
        chat_id=channel.destination,
        message_thread_id=channel.thread_id,
        timezone=channel.timezone or notifications.DEFAULT_NOTIFICATION_TIMEZONE,
        last_success_at=channel.last_success_at,
        last_error_at=channel.last_error_at,
        last_error=channel.last_error,
    )


def _rules_response(db: Session, channel: NotificationChannelDb | None) -> list[NotificationRule]:
    saved: dict[str, NotificationRuleDb] = {}
    if channel is not None:
        saved = {rule.event_type: rule for rule in db.query(NotificationRuleDb).filter(NotificationRuleDb.channel_id == channel.id).all()}
    result = []
    for event_type, definition in notifications.EVENTS.items():
        rule = saved.get(event_type)
        result.append(
            NotificationRule(
                event_type=event_type,
                title=definition.title,
                description=definition.description,
                enabled=rule.enabled if rule is not None else definition.default_enabled,
                template=rule.template if rule is not None else definition.default_template,
                default_template=definition.default_template,
                cooldown_seconds=rule.cooldown_seconds if rule is not None else definition.cooldown_seconds,
                variables=list(definition.variables),
            )
        )
    return result


def _settings_response(db: Session) -> NotificationSettingsResponse:
    channel = _telegram_channel(db)
    return NotificationSettingsResponse(telegram=_telegram_response(channel), rules=_rules_response(db, channel))


@router.get("", response_model=NotificationSettingsResponse)
def get_notification_settings(
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> NotificationSettingsResponse:
    return _settings_response(db)


@router.put("/telegram", response_model=NotificationSettingsResponse)
def update_telegram_settings(
    request: UpdateTelegramSettingsRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> NotificationSettingsResponse:
    channel = _get_or_create_telegram(db)
    was_installed = bool(channel.enabled and channel.secret_enc and channel.destination)
    if request.bot_token is not None:
        if ":" not in request.bot_token:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Telegram bot token is invalid")
        channel.secret_enc = crypto.encrypt(request.bot_token)
    if request.chat_id is not None:
        channel.destination = request.chat_id
    channel.thread_id = request.message_thread_id
    if request.timezone is not None:
        try:
            notifications.validate_timezone(request.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        channel.timezone = request.timezone
    if request.enabled and (not channel.secret_enc or not channel.destination):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Add a Telegram bot token and group/chat ID before enabling the channel.",
        )
    channel.enabled = request.enabled
    channel.updated_at = datetime.now(timezone.utc)
    notifications.ensure_default_rules(db, channel)
    if channel.enabled and channel.secret_enc and channel.destination and not was_installed:
        notifications.enqueue_notifier_installed(db, channel, settings.FRONTEND_ORIGIN)
    db.commit()
    return _settings_response(db)


@router.put("/rules/{event_type}", response_model=NotificationSettingsResponse)
def update_notification_rule(
    event_type: str,
    request: UpdateNotificationRuleRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> NotificationSettingsResponse:
    if event_type not in notifications.EVENTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification event not found")
    try:
        notifications.validate_template(event_type, request.template)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    channel = _get_or_create_telegram(db)
    rule = next(rule for rule in notifications.ensure_default_rules(db, channel) if rule.event_type == event_type)
    rule.enabled = request.enabled
    rule.template = request.template
    rule.cooldown_seconds = request.cooldown_seconds
    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _settings_response(db)


@router.post("/telegram/test", response_model=TestNotificationResponse)
def test_telegram(
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> TestNotificationResponse:
    channel = _telegram_channel(db)
    if channel is None or not channel.secret_enc or not channel.destination:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Save a bot token and destination first.")
    now = datetime.now(timezone.utc)
    try:
        notifications.send_telegram(
            channel,
            f"✅ Claude Code Proxy notifications connected\n\nTelegram delivery is working.\nDashboard: {settings.FRONTEND_ORIGIN}",
        )
    except Exception as exc:  # noqa: BLE001
        channel.last_error_at = now
        channel.last_error = str(exc)[:2000]
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    channel.last_success_at = now
    channel.last_error = None
    channel.updated_at = now
    db.commit()
    return TestNotificationResponse(delivered=True, detail="Test message delivered to Telegram.")
