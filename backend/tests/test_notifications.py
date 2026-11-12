"""Notification settings, template safety, deduplication, and Telegram delivery tests."""

from datetime import datetime
from uuid import uuid4

import respx
from httpx import Response

from app.utils import crypto, notifications, provider_health
from app.utils.models.api import ProviderHealth
from app.utils.postgres import (
    AccountDb,
    NotificationChannelDb,
    NotificationDeliveryDb,
    NotificationRuleDb,
    ProxyEventDb,
    UserDb,
)
from app.utils.postgres.base import SessionFactory


def _configure_telegram(client, admin_headers, *, enabled=True):
    response = client.put(
        "/api/v1/notifications/telegram",
        headers=admin_headers,
        json={
            "enabled": enabled,
            "bot_token": "123456:test-token",
            "chat_id": "-100987654321",
            "message_thread_id": 42,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_notification_settings_are_empty_but_include_event_catalog(client, admin_headers):
    response = client.get("/api/v1/notifications", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["telegram"]["configured"] is False
    assert body["telegram"]["enabled"] is False
    assert {rule["event_type"] for rule in body["rules"]} == set(notifications.EVENTS)
    assert next(rule for rule in body["rules"] if rule["event_type"] == "account_added")["enabled"] is True
    authentication_rule = next(rule for rule in body["rules"] if rule["event_type"] == "account_authentication_expired")
    assert authentication_rule["title"] == "Account authentication expired"
    assert {"authentication_code", "authentication_reason"}.issubset(authentication_rule["variables"])
    quota_rule = next(rule for rule in body["rules"] if rule["event_type"] == "account_quota_threshold")
    assert {
        "limiting_window",
        "five_hour_usage_percent",
        "five_hour_reset_at",
        "weekly_usage_percent",
        "weekly_reset_at",
    }.issubset(quota_rule["variables"])
    assert next(rule for rule in body["rules"] if rule["event_type"] == "user_rate_limit")["enabled"] is False
    assert body["telegram"]["timezone"] == "Asia/Kolkata"


def test_report_periods_use_completed_ist_calendar_windows():
    from zoneinfo import ZoneInfo

    local_now = datetime(2026, 8, 22, 6, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    daily_start, daily_end, daily_key = notifications._report_period("daily_usage_report", local_now)
    weekly_start, weekly_end, weekly_key = notifications._report_period("weekly_usage_report", local_now)
    monthly_start, monthly_end, monthly_key = notifications._report_period("monthly_usage_report", local_now)

    assert (daily_start.date().isoformat(), daily_end.date().isoformat(), daily_key) == ("2026-08-21", "2026-08-22", "daily:2026-08-21")
    assert (weekly_start.date().isoformat(), weekly_end.date().isoformat(), weekly_key) == ("2026-08-10", "2026-08-17", "weekly:2026-08-10")
    assert (monthly_start.date().isoformat(), monthly_end.date().isoformat(), monthly_key) == ("2026-07-01", "2026-08-01", "monthly:2026-07")
    assert notifications.validate_timezone("Asia/Kolkata") == "Asia/Kolkata"


def test_account_status_command_scopes_and_formats_saved_timezone(client, admin_headers, seed_account):
    from datetime import timezone

    from app.utils.models.api import AccountStatus, ProviderHealth
    from app.utils.postgres import AccountDb

    assert notifications._STATUS_COMMAND.fullmatch("/claude status all") is not None
    assert notifications._STATUS_COMMAND.fullmatch("/claude_code_proxy_status all") is None
    assert notifications._STATUS_COMMAND.fullmatch("claude-code-proxy-status all") is None

    usable_id = seed_account(
        "usable-account",
        session_used_pct=0.25,
        weekly_used_pct=0.40,
        rotation_threshold=1.0,
        priority=1,
    )
    available_id = seed_account(
        "available-account",
        session_used_pct=0.50,
        weekly_used_pct=0.60,
        rotation_threshold=1.0,
        priority=2,
    )
    exhausted_id = seed_account(
        "exhausted-account",
        session_used_pct=1.0,
        weekly_used_pct=0.80,
        rotation_threshold=1.0,
        priority=3,
    )
    disabled_id = seed_account(
        "disabled-account",
        session_used_pct=0.10,
        weekly_used_pct=0.20,
        rotation_threshold=1.0,
        priority=4,
    )
    with SessionFactory() as db:
        usable = db.query(AccountDb).filter(AccountDb.id == usable_id).one()
        usable.account_email = "usable-account@example.com"
        usable.provider_health = ProviderHealth.HEALTHY
        usable.session_reset_at = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        usable.weekly_reset_at = datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)
        available = db.query(AccountDb).filter(AccountDb.id == available_id).one()
        available.account_email = "available-account@example.com"
        available.provider_health = ProviderHealth.DEGRADED
        exhausted = db.query(AccountDb).filter(AccountDb.id == exhausted_id).one()
        exhausted.account_email = "exhausted-account@example.com"
        exhausted.provider_health = ProviderHealth.HEALTHY
        disabled = db.query(AccountDb).filter(AccountDb.id == disabled_id).one()
        disabled.account_email = "disabled-account@example.com"
        disabled.status = AccountStatus.DISABLED
        db.commit()

        usable_message = "\n".join(notifications.account_status_messages(db, "usable", "Asia/Kolkata"))
        available_message = "\n".join(notifications.account_status_messages(db, "available", "Asia/Kolkata"))
        all_message = "\n".join(notifications.account_status_messages(db, "all", "Asia/Kolkata"))

    assert "usable-account@example.com" in usable_message
    assert "5-hour remaining: 75.0%" in usable_message
    assert "Weekly remaining: 60.0%" in usable_message
    assert "5-hour rotation threshold: 100.0%" in usable_message
    assert "Weekly rotation threshold: 100.0%" in usable_message
    assert "5-hour reset: 2026-08-25 05:30 IST" in usable_message
    assert "Weekly reset: 2026-08-29 05:30 IST" in usable_message
    assert "reset: Unknown" not in available_message
    assert "Status:" not in usable_message
    assert "usage consumed:" not in usable_message
    assert "available-account" not in usable_message
    assert "available-account@example.com" in available_message
    assert "exhausted-account@example.com" in available_message
    assert "Status: Rotation threshold reached (5-hour)" in available_message
    assert "Available in pool: 3" in available_message
    assert "disabled-account" not in available_message
    assert "disabled-account@example.com" in all_message


@respx.mock
def test_telegram_status_command_is_registered_polled_and_replied_to(client, admin_headers, seed_account):
    from app.utils.models.api import ProviderHealth
    from app.utils.postgres import AccountDb

    _configure_telegram(client, admin_headers)
    account_id = seed_account(
        "command-account",
        session_used_pct=0.40,
        weekly_used_pct=0.50,
        rotation_threshold=1.0,
    )
    with SessionFactory() as db:
        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.account_email = "command-account@example.com"
        account.provider_health = ProviderHealth.HEALTHY
        db.commit()

    set_commands = respx.post("https://api.telegram.org/bot123456:test-token/setMyCommands").mock(
        return_value=Response(200, json={"ok": True, "result": True})
    )
    get_updates = respx.post("https://api.telegram.org/bot123456:test-token/getUpdates").mock(
        side_effect=[
            Response(200, json={"ok": True, "result": []}),
            Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 91,
                            "message": {
                                "message_id": 12,
                                "message_thread_id": 42,
                                "chat": {"id": -100987654321},
                                "text": "/claude status usable",
                            },
                        }
                    ],
                },
            ),
        ]
    )
    send_message = respx.post("https://api.telegram.org/bot123456:test-token/sendMessage").mock(
        return_value=Response(200, json={"ok": True, "result": {}})
    )
    offsets: dict[str, int] = {}
    with SessionFactory() as db:
        assert notifications.poll_telegram_commands_once(db, offsets) == 0
        assert notifications.poll_telegram_commands_once(db, offsets) == 1

    assert set_commands.called
    assert '"command":"claude"' in set_commands.calls[0].request.content.decode()
    assert get_updates.call_count == 2
    payload = send_message.calls[0].request.content.decode()
    assert "command-account@example.com" in payload
    assert '"message_thread_id":42' in payload
    assert '"message_id":12' in payload


def test_telegram_token_is_encrypted_and_never_returned(client, admin_headers):
    body = _configure_telegram(client, admin_headers)

    assert body["telegram"]["configured"] is True
    assert "bot_token" not in body["telegram"]
    with SessionFactory() as db:
        channel = db.query(NotificationChannelDb).one()
        assert channel.secret_enc != "123456:test-token"
        assert crypto.decrypt(channel.secret_enc) == "123456:test-token"
        assert channel.destination == "-100987654321"
        assert channel.thread_id == 42


def test_notifier_installed_is_queued_only_once(client, admin_headers):
    _configure_telegram(client, admin_headers)
    for enabled in (False, True):
        response = client.put(
            "/api/v1/notifications/telegram",
            headers=admin_headers,
            json={"enabled": enabled, "chat_id": "-100987654321", "message_thread_id": 42},
        )
        assert response.status_code == 200, response.text

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "notifier_installed").all()
        assert len(deliveries) == 1
        assert "notifier installed" in deliveries[0].message.lower()


def test_account_added_includes_email_and_only_upgrades_the_stock_template(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("email-included")
    with SessionFactory() as db:
        rule = db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type == "account_added").one()
        rule.template = notifications.LEGACY_DEFAULT_TEMPLATES["account_added"][0]
        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.account_email = "email-included@example.com"
        db.commit()

        assert notifications.enqueue_account_added(db, account) == 1
        db.commit()

    with SessionFactory() as db:
        delivery = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_added").one()
        rule = db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type == "account_added").one()
        assert "Email: email-included@example.com" in delivery.message
        assert "5-hour usage: 0%" in delivery.message
        assert "Weekly usage: 0%" in delivery.message
        assert rule.template == notifications.EVENTS["account_added"].default_template

        rule.template = "Custom account-added message for {{ account_label }}"
        db.commit()

    custom_account_id = seed_account("custom-template")
    with SessionFactory() as db:
        account = db.query(AccountDb).filter(AccountDb.id == custom_account_id).one()
        assert notifications.enqueue_account_added(db, account) == 1
        db.commit()

        rule = db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type == "account_added").one()
        assert rule.template == "Custom account-added message for {{ account_label }}"


def test_unknown_template_variable_is_rejected(client, admin_headers):
    _configure_telegram(client, admin_headers)

    response = client.put(
        "/api/v1/notifications/rules/account_added",
        headers=admin_headers,
        json={"enabled": True, "template": "Hello {{ secret_token }}", "cooldown_seconds": 0},
    )

    assert response.status_code == 422
    assert "secret_token" in response.json()["detail"]


def test_threshold_event_is_rendered_and_deduplicated(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("nearly-full", session_used_pct=0.97, weekly_used_pct=0.20, rotation_threshold=0.95)

    with SessionFactory() as db:
        from app.utils.postgres import AccountDb

        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.account_email = "nearly-full@example.com"
        assert notifications.enqueue_account_threshold(db, account) == 1
        assert notifications.enqueue_account_threshold(db, account) == 0
        db.commit()

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_quota_threshold").all()
        assert len(deliveries) == 1
        assert deliveries[0].event_type == "account_quota_threshold"
        assert "nearly-full" in deliveries[0].message
        assert "nearly-full@example.com" in deliveries[0].message
        assert "Limiting window: 5-hour" in deliveries[0].message
        assert "5-hour usage: 97.0%" in deliveries[0].message
        assert "Weekly usage: 20.0%" in deliveries[0].message


def test_hard_limit_supersedes_threshold_at_100_percent(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account(
        "exhausted-account",
        session_used_pct=1.0,
        weekly_used_pct=0.40,
        rotation_threshold=1.0,
    )

    with SessionFactory() as db:
        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        assert notifications.enqueue_account_threshold(db, account) == 0
        assert notifications.enqueue_account_hard_limit(db, account) == 1
        assert notifications.enqueue_account_threshold(db, account) == 0
        db.commit()

    with SessionFactory() as db:
        assert db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_quota_threshold").count() == 0
        assert db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_hard_limit").count() == 1


def test_hard_limit_suppresses_an_already_queued_threshold(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("hard-after-threshold", session_used_pct=0.97, weekly_used_pct=0.40, rotation_threshold=0.95)

    with SessionFactory() as db:
        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        assert notifications.enqueue_account_threshold(db, account) == 1
        assert notifications.enqueue_account_hard_limit(db, account) == 1
        db.commit()

    with SessionFactory() as db:
        threshold = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_quota_threshold").one()
        assert threshold.status == "suppressed"
        assert db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_hard_limit").count() == 1


def test_exhausted_503_alerts_for_one_user_and_respects_cooldown(client, admin_headers, make_user):
    _configure_telegram(client, admin_headers)
    make_user("single-503")

    with SessionFactory() as db:
        rule = db.query(NotificationRuleDb).filter(NotificationRuleDb.event_type == "request_capacity_exhausted").one()
        rule.enabled = True
        user = db.query(UserDb).filter(UserDb.name == "single-503").one()
        db.add(
            ProxyEventDb(
                id=uuid4(),
                created_at=notifications.utcnow(),
                request_id="req_single_503",
                user_id=user.id,
                event_type="request.exhausted",
                status_code=503,
                message="No capacity",
            )
        )
        db.flush()
        assert notifications.enqueue_capacity_exhausted(db) == 1
        assert notifications.enqueue_capacity_exhausted(db) == 0
        db.commit()

    with SessionFactory() as db:
        delivery = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "request_capacity_exhausted").one()
        assert "HTTP 503" in delivery.message
        assert "Affected users: 1" in delivery.message


def test_threshold_event_deduplicates_reset_timestamp_drift(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("drifting-reset", session_used_pct=1.0, weekly_used_pct=0.2, rotation_threshold=0.95)

    with SessionFactory() as db:
        from datetime import datetime, timedelta, timezone

        from app.utils.postgres import AccountDb

        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.session_reset_at = datetime(2026, 8, 21, 22, 53, 10, tzinfo=timezone.utc)
        assert notifications.enqueue_account_threshold(db, account) == 1
        account.session_reset_at += timedelta(seconds=8)
        assert notifications.enqueue_account_threshold(db, account) == 0
        db.commit()

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_quota_threshold").all()
        assert len(deliveries) == 1


def test_weekly_exhaustion_prevents_new_five_hour_threshold_alerts(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account(
        "weekly-blocked",
        session_used_pct=0.96,
        weekly_used_pct=1.0,
        rotation_threshold=0.95,
    )

    with SessionFactory() as db:
        from datetime import timedelta, timezone

        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.session_reset_at = datetime(2026, 8, 25, 5, 0, tzinfo=timezone.utc)
        account.weekly_reset_at = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
        assert notifications.enqueue_account_threshold(db, account) == 1

        # A fresh five-hour window cannot make the account usable while the
        # weekly window remains exhausted, so do not send another alert.
        account.session_used_pct = 1.0
        account.session_reset_at += timedelta(hours=5)
        assert notifications.enqueue_account_threshold(db, account) == 0
        db.commit()

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_quota_threshold").all()
        assert len(deliveries) == 1


def test_authentication_expired_is_queued_once_per_reauthentication_cycle(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("expired-auth")

    with SessionFactory() as db:
        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        account.account_email = "expired-auth@example.com"
        provider_health.mark_success(account)
        db.commit()

    with SessionFactory() as db:
        assert provider_health.persist_failure(db, account_id, provider_health.reauthentication_error()) == ProviderHealth.REAUTH_REQUIRED
        assert provider_health.persist_failure(db, account_id, provider_health.reauthentication_error()) == ProviderHealth.REAUTH_REQUIRED

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_authentication_expired").all()
        assert len(deliveries) == 1
        assert "expired-auth@example.com" in deliveries[0].message
        assert "Authentication is no longer valid" in deliveries[0].message

        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        provider_health.mark_success(account)
        db.commit()
        assert provider_health.persist_failure(db, account_id, provider_health.reauthentication_error()) == ProviderHealth.REAUTH_REQUIRED

    with SessionFactory() as db:
        deliveries = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_authentication_expired").all()
        assert len(deliveries) == 2
        assert len({delivery.event_key for delivery in deliveries}) == 2


@respx.mock
def test_send_test_uses_saved_destination_and_topic(client, admin_headers):
    _configure_telegram(client, admin_headers)
    route = respx.post("https://api.telegram.org/bot123456:test-token/sendMessage").mock(return_value=Response(200, json={"ok": True}))

    response = client.post("/api/v1/notifications/telegram/test", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    payload = route.calls[0].request.content.decode()
    assert '"chat_id":"-100987654321"' in payload
    assert '"message_thread_id":42' in payload


@respx.mock
def test_failed_delivery_is_retried_without_exposing_token(client, admin_headers, seed_account):
    _configure_telegram(client, admin_headers)
    account_id = seed_account("full", session_used_pct=1.0, weekly_used_pct=1.0, rotation_threshold=0.95)
    respx.post("https://api.telegram.org/bot123456:test-token/sendMessage").mock(
        return_value=Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
    )

    with SessionFactory() as db:
        from app.utils.postgres import AccountDb

        account = db.query(AccountDb).filter(AccountDb.id == account_id).one()
        notifications.enqueue_account_hard_limit(db, account)
        db.commit()
        assert notifications.deliver_pending_once(db) is True
        assert notifications.deliver_pending_once(db) is True

    with SessionFactory() as db:
        delivery = db.query(NotificationDeliveryDb).filter(NotificationDeliveryDb.event_type == "account_hard_limit").one()
        channel = db.query(NotificationChannelDb).one()
        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert "chat not found" in delivery.last_error
        assert "test-token" not in delivery.last_error
        assert channel.last_error == delivery.last_error
