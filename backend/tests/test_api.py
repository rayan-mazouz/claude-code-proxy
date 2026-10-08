# Path: tests/test_api.py
# Description: API tests for admin auth, user management, and the proxy path (non-streaming, streaming, failover).

import json
import uuid

import httpx
import respx

ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"


def test_presets_inherit_and_preserve_user_overrides(client, admin_headers):
    preset = client.post(
        "/api/v1/presets",
        headers=admin_headers,
        json={
            "name": "Research",
            "allowed_models": ["claude-sonnet-4-6"],
            "allowed_thinking_levels": ["high"],
            "allowed_thinking_modes": ["enabled"],
            "model_thinking_levels": {"claude-sonnet-4-6": ["high"]},
        },
    )
    assert preset.status_code == 201, preset.text
    preset_id = preset.json()["id"]
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "preset-user", "preset_id": preset_id})
    assert user.status_code == 201, user.text
    user_id = user.json()["user"]["id"]
    assert user.json()["user"]["allowed_models"] == ["claude-sonnet-4-6"]
    assert user.json()["user"]["allowed_thinking_modes"] == ["enabled"]
    assert user.json()["user"]["preset_overrides"] == []

    override = client.put(f"/api/v1/users/{user_id}", headers=admin_headers, json={"allowed_thinking_levels": ["low"]})
    assert override.status_code == 200, override.text
    assert override.json()["user"]["preset_overrides"] == ["allowed_thinking_levels"]

    changed = client.put(
        f"/api/v1/presets/{preset_id}",
        headers=admin_headers,
        json={"name": "Research", "allowed_models": ["claude-haiku-4-5"], "allowed_thinking_levels": ["max"], "allowed_thinking_modes": ["adaptive"]},
    )
    assert changed.status_code == 200, changed.text
    listed = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert listed["allowed_models"] == ["claude-haiku-4-5"]
    assert listed["allowed_thinking_levels"] == ["low"]
    reset = client.delete(f"/api/v1/users/{user_id}/preset-overrides/allowed_thinking_levels", headers=admin_headers)
    assert reset.status_code == 200
    assert reset.json()["user"]["allowed_thinking_levels"] == ["max"]
    assert reset.json()["user"]["preset_overrides"] == []
    assert client.delete(f"/api/v1/presets/{preset_id}", headers=admin_headers).status_code == 409


def test_user_policy_edit_is_atomic_with_preset_assignment(client, admin_headers):
    first = client.post("/api/v1/presets", headers=admin_headers, json={"name": "First", "allowed_thinking_modes": ["disabled"]}).json()
    second = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Second", "allowed_thinking_modes": ["enabled"]}).json()
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "atomic", "preset_id": first["id"]}).json()["user"]
    path = f"/api/v1/users/{user['id']}"
    failed = client.put(path, headers=admin_headers, json={"preset_id": str(uuid.uuid4()), "name": "changed"})
    assert failed.status_code == 404
    assert client.get("/api/v1/users", headers=admin_headers).json()["users"][0]["name"] == "atomic"
    changed = client.put(path, headers=admin_headers, json={"preset_id": second["id"], "name": "changed", "allowed_thinking_modes": ["disabled"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["user"]["preset_id"] == second["id"]
    assert changed.json()["user"]["preset_overrides"] == ["allowed_thinking_modes"]
    reset = client.put(path, headers=admin_headers, json={"clear_preset_overrides": ["allowed_thinking_modes"]})
    assert reset.status_code == 200, reset.text
    assert reset.json()["user"]["allowed_thinking_modes"] == ["enabled"]


def test_schema_sync_preserves_users_without_a_preset(client, admin_headers):
    from app.scripts.migrate import sync_canonical_schema

    first = client.post("/api/v1/users", headers=admin_headers, json={"name": "first", "allowed_thinking_levels": ["low"]})
    second = client.post("/api/v1/users", headers=admin_headers, json={"name": "second", "allowed_thinking_levels": ["max"]})
    assert first.status_code == second.status_code == 201
    sync_canonical_schema()
    users = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    presets = client.get("/api/v1/presets", headers=admin_headers).json()["presets"]
    assert len(presets) == 1 and presets[0]["name"] == "Current configuration"
    assert {user["name"]: user["allowed_thinking_levels"] for user in users} == {"first": ["low"], "second": ["max"]}
    assert all(user["preset_id"] is None for user in users)
    assert all(user["preset_overrides"] == [] for user in users)


def test_per_model_thinking_mode_from_preset_is_enforced(client, admin_headers):
    preset = client.post(
        "/api/v1/presets",
        headers=admin_headers,
        json={"name": "Enabled only", "model_thinking_modes": {"claude-sonnet-4-6": ["enabled"]}},
    ).json()
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "mode-user", "preset_id": preset["id"]}).json()["user"]
    secret = client.post(f"/api/v1/users/{user['id']}/keys", headers=admin_headers, json={"label": "test"}).json()["secret"]
    response = client.post(
        "/api/v1/messages",
        headers={"x-api-key": secret},
        json={"model": "claude-sonnet-4-6", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 403
    assert "not allowed for model" in response.text


def test_preset_routes_use_user_policy_permissions():
    from app.utils.security.permissions import required_permission

    assert required_permission("/api/v1/presets", "GET") == "proxy_users:read"
    assert required_permission("/api/v1/presets", "POST") == "proxy_users:write"
    assert required_permission("/api/v1/users/123/preset-overrides/allowed_models", "DELETE") == "proxy_users:write"


def test_login_success_and_failure(client, admin_password):
    ok = client.post("/api/v1/auth/login", json={"username": "admin", "password": admin_password})
    assert ok.status_code == 200
    assert ok.json()["token"]

    bad = client.post("/api/v1/auth/login", json={"username": "admin", "password": "definitely-wrong"})
    assert bad.status_code == 401


def test_admin_routes_require_auth(client):
    assert client.get("/api/v1/users").status_code == 401
    assert client.get("/api/v1/accounts").status_code == 401
    assert client.get("/api/v1/stats/overview").status_code == 401
    assert client.get("/api/v1/stats/thinking-level-mix").status_code == 401


def test_user_crud_and_key_management(client, admin_headers):
    created = client.post("/api/v1/users", headers=admin_headers, json={"name": "alice"})
    assert created.status_code == 201
    user_id = created.json()["user"]["id"]
    assert created.json()["user"]["key_count"] == 0
    assert created.json()["user"]["allowed_thinking_levels"] == ["low", "medium", "high", "max"]

    updated = client.put(f"/api/v1/users/{user_id}", headers=admin_headers, json={"name": "alice-2"})
    assert updated.status_code == 200
    assert updated.json()["user"]["name"] == "alice-2"

    # One user can hold multiple keys; each secret is returned exactly once.
    k1 = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "laptop"})
    k2 = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "ci"})
    assert k1.status_code == 201 and k2.status_code == 201
    assert k1.json()["secret"].startswith("usr_")
    assert k1.json()["secret"] != k2.json()["secret"]

    keys = client.get(f"/api/v1/users/{user_id}/keys", headers=admin_headers).json()["keys"]
    assert len(keys) == 2
    assert client.get("/api/v1/users", headers=admin_headers).json()["users"][0]["key_count"] == 2

    # Deleting one key leaves the other.
    assert client.delete(f"/api/v1/users/{user_id}/keys/{keys[0]['id']}", headers=admin_headers).status_code == 204
    assert len(client.get(f"/api/v1/users/{user_id}/keys", headers=admin_headers).json()["keys"]) == 1

    assert client.delete(f"/api/v1/users/{user_id}", headers=admin_headers).status_code == 204
    assert client.get("/api/v1/users", headers=admin_headers).json()["total"] == 0


def test_proxy_requires_user_key(client):
    assert client.post("/api/v1/messages", json={}).status_code == 401


def test_account_priority_update_preserves_duplicate_priority_groups(client, admin_headers, seed_account):
    seed_account("first", priority=1)
    seed_account("second", priority=1)
    third = seed_account("third", priority=3)

    moved = client.put(
        f"/api/v1/accounts/{third}",
        headers=admin_headers,
        json={"priority": 1},
    )
    assert moved.status_code == 200
    accounts = client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]
    assert {account["label"]: account["priority"] for account in accounts} == {
        "first": 1,
        "second": 1,
        "third": 1,
    }


def test_account_delete_preserves_remaining_priority_lanes(client, admin_headers, seed_account):
    seed_account("delete-first", priority=1)
    middle = seed_account("delete-middle", priority=3)
    seed_account("delete-last", priority=3)

    response = client.delete(f"/api/v1/accounts/{middle}", headers=admin_headers)

    assert response.status_code == 204
    accounts = client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]
    assert {account["label"]: account["priority"] for account in accounts} == {
        "delete-first": 1,
        "delete-last": 3,
    }


def test_bulk_priority_assignment_updates_selected_accounts_and_users(client, admin_headers, seed_account):
    account_ids = [str(seed_account("bulk-a", priority=1)), str(seed_account("bulk-b", priority=2)), str(seed_account("keep", priority=3))]
    account_response = client.put(
        "/api/v1/accounts/priorities/bulk",
        headers=admin_headers,
        json={"account_ids": account_ids[:2], "priority": 4},
    )
    assert account_response.status_code == 200, account_response.text
    account_priorities = {item["label"]: item["priority"] for item in account_response.json()["accounts"]}
    assert account_priorities == {"bulk-a": 4, "bulk-b": 4, "keep": 3}

    users = [
        client.post("/api/v1/users", headers=admin_headers, json={"name": name}).json()["user"]["id"]
        for name in ("bulk-user-a", "bulk-user-b", "keep-user")
    ]
    user_response = client.put(
        "/api/v1/users/priorities/bulk",
        headers=admin_headers,
        json={"user_ids": users[:2], "priority": 3},
    )
    assert user_response.status_code == 200, user_response.text
    user_priorities = {item["name"]: item["priority"] for item in user_response.json()["users"]}
    assert user_priorities["bulk-user-a"] == 3
    assert user_priorities["bulk-user-b"] == 3
    assert user_priorities["keep-user"] == 1

    duplicate = client.put(
        "/api/v1/users/priorities/bulk",
        headers=admin_headers,
        json={"user_ids": [users[0], users[0]], "priority": 2},
    )
    assert duplicate.status_code == 422


def test_account_email_can_only_be_updated_by_oauth(client, admin_headers, seed_account):
    account_id = seed_account("oauth-owned-email")

    response = client.put(
        f"/api/v1/accounts/{account_id}",
        headers=admin_headers,
        json={"account_email": "manual@example.com"},
    )

    assert response.status_code == 422
    accounts = client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]
    account = next(item for item in accounts if item["id"] == str(account_id))
    assert account["account_email"] is None


def test_bulk_priority_update_is_atomic_and_complete(client, admin_headers, seed_account):
    first = seed_account("first", priority=1)
    second = seed_account("second", priority=2)
    third = seed_account("third", priority=3)
    response = client.put(
        "/api/v1/accounts/priorities",
        headers=admin_headers,
        json={"account_ids": [str(third), str(first), str(second)]},
    )
    assert response.status_code == 200
    assert [(account["label"], account["priority"]) for account in response.json()["accounts"]] == [
        ("third", 1),
        ("first", 2),
        ("second", 3),
    ]
    invalid = client.put(
        "/api/v1/accounts/priorities",
        headers=admin_headers,
        json={"account_ids": [str(first), str(first), str(second)]},
    )
    assert invalid.status_code == 422


@respx.mock
def test_proxy_nonstreaming_records_usage(client, admin_headers, seed_account, make_user):
    seed_account("a1")
    key = make_user("bob")

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 5}})
    )

    resp = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5", "messages": []})
    assert resp.status_code == 200

    usage = client.get("/api/v1/stats/usage", headers=admin_headers).json()
    assert usage["total"] == 1
    record = usage["items"][0]
    assert record["user_name"] == "bob"
    assert record["account_label"] == "a1"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5


@respx.mock
def test_proxy_streaming_relays_and_records(client, admin_headers, seed_account, make_user):
    seed_account("a1")
    key = make_user("carol")

    sse = (
        'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":20,"cache_read_input_tokens":3}}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
        "data: [DONE]\n\n"
    )

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse))

    resp = client.post(
        "/api/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "claude-opus-4-8",
            "stream": True,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        },
    )
    assert resp.status_code == 200
    assert "message_delta" in resp.text
    assert '"model":"claude-opus-4-8"' in resp.text
    assert '"model":"claude-sonnet-4-6"' not in resp.text

    record = client.get("/api/v1/stats/usage", headers=admin_headers).json()["items"][0]
    assert record["input_tokens"] == 23  # 20 input + 3 cache_read
    assert record["output_tokens"] == 42
    assert record["reasoning_level"] == "high"
    assert record["model"] == "claude-sonnet-4-6"


@respx.mock
def test_proxy_forwards_only_api_headers_upstream(client, seed_account, make_user):
    seed_account("header-account")
    key = make_user("header-user")

    respx.route(host="testserver").pass_through()
    route = respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )
    client.post(
        "/api/v1/messages",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Forwarded-For": "100.64.0.7",
            "X-Real-Ip": "100.64.0.7",
            "X-Forwarded-Host": "proxy.internal",
            "Cookie": "session=secret",
            "X-Debug-Client": "1",
            "anthropic-unlisted": "1",
            "anthropic-beta": "context-1m-2025-08-07",
            "x-stainless-os": "Linux",
            "x-app": "cli",
        },
        json={"model": "claude-haiku-4-5"},
    )

    sent = route.calls[0].request.headers
    for leaked in ("x-forwarded-for", "x-real-ip", "x-forwarded-host", "cookie", "x-api-key", "x-debug-client", "anthropic-unlisted"):
        assert leaked not in sent
    assert sent["authorization"] == "Bearer upstream-access-token"
    assert "context-1m-2025-08-07" in sent["anthropic-beta"]
    assert sent["x-stainless-os"] == "Linux"
    assert sent["x-app"] == "cli"


@respx.mock
def test_proxy_fails_over_on_429(client, admin_headers, seed_account, make_user):
    from app.utils.models.api import AccountStatus
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    first_id = seed_account("a1")
    seed_account("a2")
    key = make_user("dave")

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"retry-after": "1"},
                json={"error": {"message": "Rate limited by Anthropic"}},
            ),
            httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )

    resp = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert resp.status_code == 200
    with SessionFactory() as db:
        first = db.get(AccountDb, first_id)
        assert first.status == AccountStatus.COOLDOWN
        assert first.cooldown_until is not None


@respx.mock
def test_proxy_cools_down_capacity_account_and_immediately_fails_over(client, seed_account, make_user):
    from app.utils.models.api import AccountStatus
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    first_id = seed_account("capacity-first")
    seed_account("capacity-second")
    key = make_user("capacity-user")

    capacity = {
        "error": {
            "message": "Selected model is at capacity. Please try a different model.",
        }
    }
    respx.route(host="testserver").pass_through()
    route = respx.post(ANTHROPIC_MESSAGES).mock(
        side_effect=[
            httpx.Response(403, json=capacity),
            httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    headers = {"Authorization": f"Bearer {key}"}

    assert client.post("/api/v1/messages", headers=headers, json={"model": "claude-haiku-4-5"}).status_code == 200
    assert len(route.calls) == 2
    with SessionFactory() as db:
        first = db.get(AccountDb, first_id)
        assert first.status == AccountStatus.COOLDOWN
        assert first.cooldown_until is not None


@respx.mock
def test_proxy_fails_over_on_streamed_capacity_event(client, seed_account, make_user):
    seed_account("stream-capacity-first")
    seed_account("stream-capacity-second")
    key = make_user("stream-capacity-user")
    capacity_sse = 'data: {"type":"error","error":{"message":"Selected model is at capacity. Please try a different model."}}\n\n'

    respx.route(host="testserver").pass_through()
    route = respx.post(ANTHROPIC_MESSAGES).mock(
        side_effect=[
            httpx.Response(200, headers={"content-type": "text/event-stream"}, text=capacity_sse),
            httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    response = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert response.status_code == 200
    assert len(route.calls) == 2


@respx.mock
def test_model_text_mentioning_capacity_is_not_a_provider_failure(client, seed_account, make_user):
    from app.utils.models.api import AccountStatus
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    first_id = seed_account("chatty-first")
    seed_account("chatty-second")
    key = make_user("chatty-user")
    text = 'The cluster is at capacity; try a different model. Logged \\"type\\":\\"error\\" and response.failed.'
    sse = (
        'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-haiku-4-5","usage":{"input_tokens":5}}}\n\n'
        f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"text_delta","text":"{text}"}}}}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    message = {
        "type": "message",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": "The cluster is at capacity; response.failed was logged."}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    respx.route(host="testserver").pass_through()
    route = respx.post(ANTHROPIC_MESSAGES).mock(
        side_effect=[
            httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse),
            httpx.Response(200, json=message),
        ]
    )
    headers = {"Authorization": f"Bearer {key}"}

    streamed = client.post("/api/v1/messages", headers=headers, json={"model": "claude-haiku-4-5", "stream": True})
    assert streamed.status_code == 200
    assert "try a different model" in streamed.text
    assert streamed.text.count("message_stop") == 2  # event name + payload of the one real stop frame

    plain = client.post("/api/v1/messages", headers=headers, json={"model": "claude-haiku-4-5"})
    assert plain.status_code == 200
    assert plain.json()["content"][0]["text"] == message["content"][0]["text"]

    assert len(route.calls) == 2
    with SessionFactory() as db:
        assert db.get(AccountDb, first_id).status == AccountStatus.ACTIVE


def _streaming_response(*frames: str) -> httpx.Response:
    async def body():
        for frame in frames:
            yield frame.encode()

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def test_prefetch_starts_stream_when_output_mentions_capacity():
    import asyncio

    from app.routes.proxy import _prepare_candidate

    prepared = asyncio.run(
        _prepare_candidate(
            _streaming_response(
                'event: message_start\ndata: {"type":"message_start","message":{}}\n\n'
                'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"temporarily overloaded"}}\n\n',
            )
        )
    )
    assert prepared._proxy_capacity_error is False
    assert prepared._proxy_pre_output_failure is False


def test_prefetch_retries_an_error_frame_before_anything_is_sent():
    import asyncio

    from app.routes.proxy import _prepare_candidate

    prepared = asyncio.run(
        _prepare_candidate(
            _streaming_response(
                'event: message_start\ndata: {"type":"message_start","message":{}}\n\n'
                'event: error\ndata: {"type":"error","error":{"message":"Selected model is at capacity."}}\n\n',
            )
        )
    )
    assert prepared._proxy_capacity_error is True
    assert prepared._proxy_pre_output_failure is True


def test_midstream_error_frame_is_forwarded_not_replaced_with_a_stop():
    import asyncio

    from app.routes.proxy import _iter_sse_frames

    delta = 'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"partial"}}\n\n'
    error = 'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'

    async def collect():
        return [frame async for frame in _iter_sse_frames(_streaming_response(delta[:20], delta[20:] + error))]

    assert asyncio.run(collect()) == [delta.encode(), error.encode()]


@respx.mock
def test_refresh_quota_reports_rate_limit_cleanly(client, admin_headers, seed_account, monkeypatch):
    # A persistently rate-limited probe must return a friendly, actionable message -- not a raw httpx 429 string.
    from app.utils import oauth

    monkeypatch.setattr(oauth.time, "sleep", lambda _seconds: None)
    account_id = seed_account("rl")

    respx.route(host="testserver").pass_through()
    respx.get(oauth.config.OAUTH_USAGE_URL).mock(return_value=httpx.Response(429, json={}))

    resp = client.post(f"/api/v1/accounts/{account_id}/refresh-quota", headers=admin_headers)
    assert resp.status_code == 502
    detail = resp.json()["detail"].lower()
    assert "rate-limit" in detail
    assert "429" not in detail


@respx.mock
def test_refresh_quota_reports_expired_refresh_token_cleanly(client, admin_headers, seed_account):
    # An account whose access token has expired must refresh first; if the stored refresh token is rejected, that
    # must surface as a friendly 502 pointing at re-authentication -- never a raw 500.
    from app.utils import oauth

    account_id = seed_account("stale", expires_in_hours=-1)

    respx.route(host="testserver").pass_through()
    respx.post(oauth.config.OAUTH_TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))

    resp = client.post(f"/api/v1/accounts/{account_id}/refresh-quota", headers=admin_headers)
    assert resp.status_code == 502
    detail = resp.json()["detail"].lower()
    assert "re-authenticate" in detail
    account = next(item for item in client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"] if item["id"] == str(account_id))
    assert account["provider_health"] == "REAUTH_REQUIRED"
    assert account["provider_health_code"] == "oauth_refresh_rejected"
    assert account["provider_health_failure_count"] >= 1


def test_reauthentication_required_account_is_not_available(seed_account):
    from app.utils import rotation
    from app.utils.models.api import ProviderHealth
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    account_id = seed_account("broken")
    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        account.provider_health = ProviderHealth.REAUTH_REQUIRED
        assert rotation.is_available(account) is False


def test_account_label_update(client, admin_headers, seed_account):
    account_id = seed_account("orig")
    updated = client.put(f"/api/v1/accounts/{account_id}", headers=admin_headers, json={"label": "renamed"})
    assert updated.status_code == 200
    assert updated.json()["account"]["label"] == "renamed"

    labels = [a["label"] for a in client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]]
    assert labels == ["renamed"]


def test_account_labels_can_be_duplicated(client, admin_headers, seed_account):
    first_id = seed_account("shared")
    second_id = seed_account("other")

    updated = client.put(f"/api/v1/accounts/{second_id}", headers=admin_headers, json={"label": "shared"})

    assert updated.status_code == 200
    accounts = client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"]
    assert [(account["id"], account["label"]) for account in accounts] == [
        (str(first_id), "shared"),
        (str(second_id), "shared"),
    ]


@respx.mock
def test_me_usage_reports_own_usage_and_pool(client, admin_headers, seed_account, make_user):
    seed_account("a1", session_used_pct=0.25, weekly_used_pct=0.4)
    key = make_user("erin")

    # Before any traffic: zeroed usage but the pool headroom is visible.
    me = client.get("/api/v1/me/usage", headers={"Authorization": f"Bearer {key}"})
    assert me.status_code == 200
    assert me.json()["user"] == "erin"
    assert me.json()["tokens_this_month"] == 0
    assert me.json()["pool"]["five_hour"]["used_pct"] == 0.25
    assert me.json()["pool"]["five_hour"]["known_account_count"] == 1

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 8, "output_tokens": 4}})
    )
    client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})

    me_after = client.get("/api/v1/me/usage", headers={"Authorization": f"Bearer {key}"}).json()
    assert me_after["tokens_this_month"] == 12
    assert me_after["requests_this_month"] == 1


def test_me_usage_aggregates_active_pool_and_earliest_reset(client, seed_account, make_user):
    from datetime import datetime, timedelta, timezone

    from app.utils.models.api import AccountStatus, ProviderHealth
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    first_id = seed_account("first", session_used_pct=0.2, weekly_used_pct=0.3)
    second_id = seed_account("second", session_used_pct=0.6, weekly_used_pct=0.5)
    seed_account("disabled", status=AccountStatus.DISABLED)
    exhausted_id = seed_account("exhausted", session_used_pct=0.95, rotation_threshold=0.9)
    reauth_id = seed_account("reauth", session_used_pct=0.1)
    unknown_id = seed_account("unknown", session_used_pct=0.0, weekly_used_pct=0.0)
    key = make_user("pool-user")

    now = datetime.now(timezone.utc)
    exhausted_reset = now + timedelta(hours=1)
    second_reset = now + timedelta(hours=2)
    weekly_exhausted_reset = now + timedelta(hours=2)
    weekly_second_reset = now + timedelta(hours=3)
    weekly_first_reset = now + timedelta(hours=6)
    with SessionFactory() as db:
        db.get(AccountDb, first_id).session_reset_at = now + timedelta(hours=4)
        db.get(AccountDb, second_id).session_reset_at = second_reset
        db.get(AccountDb, exhausted_id).session_reset_at = exhausted_reset
        db.get(AccountDb, first_id).weekly_reset_at = weekly_first_reset
        db.get(AccountDb, second_id).weekly_reset_at = weekly_second_reset
        db.get(AccountDb, exhausted_id).weekly_reset_at = weekly_exhausted_reset
        db.get(AccountDb, reauth_id).provider_health = ProviderHealth.REAUTH_REQUIRED
        db.get(AccountDb, unknown_id).session_used_pct = None
        db.get(AccountDb, unknown_id).weekly_used_pct = None
        db.commit()

    pool = client.get("/api/v1/me/usage", headers={"Authorization": f"Bearer {key}"}).json()["pool"]
    assert pool["account_count"] == 4
    assert pool["five_hour"]["known_account_count"] == 3
    assert pool["five_hour"]["unknown_account_count"] == 1
    assert pool["weekly"]["known_account_count"] == 3
    assert pool["weekly"]["unknown_account_count"] == 1
    assert pool["five_hour"]["used_pct"] == 0.583333
    assert pool["weekly"]["used_pct"] == 0.266667
    reset_values = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in pool["five_hour"]["reset_at"]]
    assert reset_values == [exhausted_reset, second_reset, now + timedelta(hours=4)]
    assert datetime.fromisoformat(pool["five_hour"]["next_reset_at"].replace("Z", "+00:00")) == exhausted_reset
    weekly_reset_values = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in pool["weekly"]["reset_at"]]
    assert weekly_reset_values == [weekly_exhausted_reset, weekly_second_reset, weekly_first_reset]
    assert datetime.fromisoformat(pool["weekly"]["next_reset_at"].replace("Z", "+00:00")) == weekly_exhausted_reset
    # API-key holders only see pool aggregates, never which Anthropic logins back the pool.
    assert "accounts" not in pool


def test_me_usage_requires_key(client):
    assert client.get("/api/v1/me/usage").status_code == 401


@respx.mock
def test_per_key_rate_limit(client, admin_headers, seed_account):
    seed_account("a1")
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "frank"}).json()["user"]["id"]
    key = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "k", "rate_limit_per_minute": 1}).json()["secret"]

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )

    first = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert first.status_code == 200
    second = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert second.status_code == 429


@respx.mock
def test_per_key_monthly_budget(client, admin_headers, seed_account):
    seed_account("a1")
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "grace"}).json()["user"]["id"]
    key = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "k", "monthly_token_budget": 5}).json()["secret"]

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 0}})
    )

    first = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert first.status_code == 200  # consumes 10 tokens (over the budget of 5)
    second = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})
    assert second.status_code == 403


def test_key_limits_crud(client, admin_headers):
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "heidi"}).json()["user"]["id"]
    created = client.post(
        f"/api/v1/users/{user_id}/keys",
        headers=admin_headers,
        json={"label": "k", "rate_limit_per_minute": 30, "monthly_token_budget": 1000},
    ).json()["api_key"]
    assert created["rate_limit_per_minute"] == 30
    assert created["monthly_token_budget"] == 1000

    client.put(
        f"/api/v1/users/{user_id}/keys/{created['id']}",
        headers=admin_headers,
        json={"rate_limit_per_minute": 0, "monthly_token_budget": 2000},
    )
    keys = client.get(f"/api/v1/users/{user_id}/keys", headers=admin_headers).json()["keys"]
    assert keys[0]["rate_limit_per_minute"] == 0
    assert keys[0]["monthly_token_budget"] == 2000


@respx.mock
def test_per_user_rate_limit_across_keys(client, admin_headers, seed_account):
    # The cap is per-user: it counts requests across ALL of the user's keys, not per key.
    seed_account("a1")
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "judy", "rate_limit_per_minute": 1}).json()["user"]["id"]
    key_a = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "a"}).json()["secret"]
    key_b = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "b"}).json()["secret"]

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )

    first = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key_a}"}, json={"model": "claude-haiku-4-5"})
    assert first.status_code == 200
    # Second request via a DIFFERENT key of the same user is still blocked by the per-user cap.
    second = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key_b}"}, json={"model": "claude-haiku-4-5"})
    assert second.status_code == 429


@respx.mock
def test_per_user_monthly_budget_across_keys(client, admin_headers, seed_account):
    seed_account("a1")
    user_id = client.post("/api/v1/users", headers=admin_headers, json={"name": "mallory", "monthly_token_budget": 5}).json()["user"]["id"]
    key_a = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "a"}).json()["secret"]
    key_b = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "b"}).json()["secret"]

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 0}})
    )

    first = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key_a}"}, json={"model": "claude-haiku-4-5"})
    assert first.status_code == 200  # consumes 10 tokens (over the budget of 5)
    # A DIFFERENT key of the same user is now blocked because the user's monthly budget is exhausted.
    second = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key_b}"}, json={"model": "claude-haiku-4-5"})
    assert second.status_code == 403


def test_user_limits_crud(client, admin_headers):
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "name": "niaj",
            "rate_limit_per_minute": 30,
            "rate_limit_per_hour": 120,
            "rate_limit_per_day": 1000,
            "monthly_token_budget": 1000,
        },
    ).json()["user"]
    assert created["rate_limit_per_minute"] == 30
    assert created["rate_limit_per_hour"] == 120
    assert created["rate_limit_per_day"] == 1000
    assert created["monthly_token_budget"] == 1000

    user_id = created["id"]
    client.put(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"rate_limit_per_minute": 0, "rate_limit_per_hour": 0, "rate_limit_per_day": 0, "monthly_token_budget": 2000},
    )
    user = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert user["rate_limit_per_minute"] == 0
    assert user["rate_limit_per_hour"] == 0
    assert user["rate_limit_per_day"] == 0
    assert user["monthly_token_budget"] == 2000


def test_user_can_override_preset_models_and_rewrites(client, admin_headers):
    preset = client.post(
        "/api/v1/presets",
        headers=admin_headers,
        json={"name": "Restricted", "allowed_models": ["claude-sonnet-4-6"]},
    ).json()
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "override-user", "preset_id": preset["id"]}).json()["user"]
    rewrites = {"claude-sonnet-4-6": "claude-haiku-4-5"}
    updated = client.put(
        f"/api/v1/users/{user['id']}",
        headers=admin_headers,
        json={"allowed_models": ["claude-haiku-4-5"], "model_overrides": rewrites},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["user"]["allowed_models"] == ["claude-haiku-4-5"]
    assert updated.json()["user"]["model_overrides"] == rewrites
    assert set(updated.json()["user"]["preset_overrides"]) == {"allowed_models", "model_overrides"}


def test_user_can_create_without_preset_and_detach_preserving_policy(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Baseline"}).json()
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"name": "direct-policy", "preset_id": None, "allowed_models": ["claude-sonnet-4-6"]},
    )
    assert created.status_code == 201, created.text
    user = created.json()["user"]
    assert user["preset_id"] is None
    assert user["allowed_models"] == ["claude-sonnet-4-6"]

    path = f"/api/v1/users/{user['id']}"
    assigned = client.put(path, headers=admin_headers, json={"preset_id": preset["id"]})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["user"]["preset_id"] == preset["id"]
    detached = client.put(
        path,
        headers=admin_headers,
        json={"preset_id": None, "allowed_models": ["claude-haiku-4-5"]},
    )
    assert detached.status_code == 200, detached.text
    assert detached.json()["user"]["preset_id"] is None
    assert detached.json()["user"]["preset_overrides"] == []
    assert detached.json()["user"]["allowed_models"] == ["claude-haiku-4-5"]
    from app.scripts.migrate import sync_canonical_schema

    sync_canonical_schema()
    after_restart = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert after_restart["preset_id"] is None
    assert after_restart["allowed_models"] == ["claude-haiku-4-5"]


@respx.mock
def test_user_hourly_and_daily_rate_limits_across_keys(client, admin_headers, seed_account):
    seed_account("a1")
    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )
    for field, expected_window in (("rate_limit_per_hour", 3600), ("rate_limit_per_day", 86400)):
        user = client.post("/api/v1/users", headers=admin_headers, json={"name": field, field: 1}).json()["user"]
        first_key = client.post(f"/api/v1/users/{user['id']}/keys", headers=admin_headers, json={"label": "first"}).json()["secret"]
        second_key = client.post(f"/api/v1/users/{user['id']}/keys", headers=admin_headers, json={"label": "second"}).json()["secret"]
        first = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {first_key}"}, json={"model": "claude-haiku-4-5"})
        assert first.status_code == 200, first.text
        blocked = client.post("/api/v1/messages", headers={"Authorization": f"Bearer {second_key}"}, json={"model": "claude-haiku-4-5"})
        assert blocked.status_code == 429, blocked.text
        assert 1 <= int(blocked.headers["Retry-After"]) <= expected_window
        from datetime import datetime, timedelta, timezone

        from app.utils.postgres import ProxyEventDb
        from app.utils.postgres.base import SessionFactory

        with SessionFactory() as db:
            db.query(ProxyEventDb).filter(
                ProxyEventDb.user_id == uuid.UUID(user["id"]),
                ProxyEventDb.event_type == "request.reserved",
            ).update({"created_at": datetime.now(timezone.utc) - timedelta(seconds=expected_window + 5)})
            db.commit()
        after_window = client.post(
            "/api/v1/messages",
            headers={"Authorization": f"Bearer {second_key}"},
            json={"model": "claude-haiku-4-5"},
        )
        assert after_window.status_code == 200, after_window.text


def test_user_extended_limits_and_model_overrides_crud(client, admin_headers):
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "name": "policy-user",
            "monthly_token_budget": 1000,
            "lifetime_token_budget": 5000,
            "monthly_spend_budget_usd": 12.5,
            "lifetime_spend_budget_usd": 50,
            "model_overrides": {" Claude-Opus-4-8 ": " CLAUDE-SONNET-4-6 "},
        },
    )
    assert created.status_code == 201
    user = created.json()["user"]
    assert user["lifetime_token_budget"] == 5000
    assert user["monthly_spend_budget_usd"] == 12.5
    assert user["lifetime_spend_budget_usd"] == 50
    assert user["model_overrides"] == {"claude-opus-4-8": "claude-sonnet-4-6"}

    cleared = client.put(
        f"/api/v1/users/{user['id']}",
        headers=admin_headers,
        json={
            "lifetime_token_budget": 0,
            "monthly_spend_budget_usd": 0,
            "lifetime_spend_budget_usd": 0,
            "model_overrides": {},
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["user"]["model_overrides"] == {}
    assert (
        client.post(
            "/api/v1/users",
            headers=admin_headers,
            json={"name": "invalid", "model_overrides": {"claude-sonnet-4-6": "claude-sonnet-4-6"}},
        ).status_code
        == 422
    )


def test_unknown_models_cannot_be_saved_to_claude_user_policy(client, admin_headers):
    for payload in (
        {"name": "unknown-source", "model_overrides": {"claude-imaginary": "claude-sonnet-5"}},
        {"name": "unknown-target", "model_overrides": {"claude-opus-4-8": "claude-imaginary"}},
    ):
        assert client.post("/api/v1/users", headers=admin_headers, json=payload).status_code == 422
    created = client.post("/api/v1/users", headers=admin_headers, json={"name": "valid-user"}).json()["user"]
    assert (
        client.put(
            f"/api/v1/users/{created['id']}",
            headers=admin_headers,
            json={"model_overrides": {"claude-opus-4-8": "claude-imaginary"}},
        ).status_code
        == 422
    )
    users = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    assert len(users) == 1 and users[0]["model_overrides"] == {}


@respx.mock
def test_unknown_model_never_reaches_claude_upstream(client, make_user):
    key = make_user("unknown-model-user")
    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(return_value=httpx.Response(200, json={}))
    headers = {"Authorization": f"Bearer {key}"}
    for path in ("/api/v1/messages", "/api/v1/messages/count_tokens"):
        response = client.post(path, headers=headers, json={"model": "claude-imaginary", "messages": []})
        assert response.status_code == 400, (path, response.text)
    assert upstream.call_count == 0


@respx.mock
def test_user_model_override_rewrites_upstream_request(client, admin_headers, seed_account):
    seed_account("override-account")
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"name": "override-user", "model_overrides": {"claude-opus-4-8": "claude-sonnet-4-6"}},
    ).json()["user"]
    key = client.post(f"/api/v1/users/{created['id']}/keys", headers=admin_headers, json={"label": "test-key"}).json()["secret"]
    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1, "output_tokens": 1}})
    )

    response = client.post(
        "/api/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "CLAUDE-OPUS-4-8", "messages": []},
    )
    assert response.status_code == 200
    assert json.loads(upstream.calls[0].request.content)["model"] == "claude-sonnet-4-6"
    assert response.json()["model"] == "CLAUDE-OPUS-4-8"


@respx.mock
def test_user_model_override_hides_effective_name_in_forwarded_error(client, admin_headers, seed_account):
    seed_account("override-error-account")
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"name": "override-error-user", "model_overrides": {"claude-opus-4-8": "claude-sonnet-4-6"}},
    ).json()["user"]
    key = client.post(f"/api/v1/users/{created['id']}/keys", headers=admin_headers, json={"label": "test-key"}).json()["secret"]
    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(
            422,
            json={"type": "error", "error": {"message": "Invalid request for model claude-sonnet-4-6"}},
        )
    )

    response = client.post(
        "/api/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "CLAUDE-OPUS-4-8", "messages": []},
    )

    assert response.status_code == 422
    assert json.loads(upstream.calls[0].request.content)["model"] == "claude-sonnet-4-6"
    assert response.json()["error"]["message"] == "Invalid request for model CLAUDE-OPUS-4-8"
    assert "claude-sonnet-4-6" not in response.text


@respx.mock
def test_user_lifetime_and_spend_limits_and_rollups(client, admin_headers, seed_account):
    account_id = seed_account("budget-account")
    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 0}})
    )
    for name, limits in (
        ("token-budget", {"lifetime_token_budget": 5}),
        ("monthly-spend", {"monthly_spend_budget_usd": 0.000001}),
        ("lifetime-spend", {"lifetime_spend_budget_usd": 0.000001}),
    ):
        created = client.post("/api/v1/users", headers=admin_headers, json={"name": name, **limits}).json()["user"]
        key = client.post(f"/api/v1/users/{created['id']}/keys", headers=admin_headers, json={"label": "test-key"}).json()["secret"]
        auth = {"Authorization": f"Bearer {key}"}
        assert client.post("/api/v1/messages", headers=auth, json={"model": "claude-haiku-4-5"}).status_code == 200
        assert client.post("/api/v1/messages", headers=auth, json={"model": "claude-haiku-4-5"}).status_code == 403
    assert upstream.call_count == 3

    users = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    assert all(user["total_spend_usd"] > 0 for user in users)
    assert all(user["monthly_spend_usd"] == user["total_spend_usd"] for user in users)
    account = next(item for item in client.get("/api/v1/accounts", headers=admin_headers).json()["accounts"] if item["id"] == str(account_id))
    assert account["total_spend_usd"] == round(sum(user["total_spend_usd"] for user in users), 6)
    assert account["monthly_spend_usd"] == account["total_spend_usd"]


@respx.mock
def test_per_user_allowed_thinking_levels(client, admin_headers, seed_account):
    seed_account("a1")
    created = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"name": "thinker", "allowed_thinking_levels": ["low", "medium"]},
    ).json()["user"]
    user_id = created["id"]
    assert created["allowed_thinking_levels"] == ["low", "medium"]
    key = client.post(f"/api/v1/users/{user_id}/keys", headers=admin_headers, json={"label": "test-key"}).json()["secret"]

    respx.route(host="testserver").pass_through()
    upstream = respx.post(ANTHROPIC_MESSAGES).mock(return_value=httpx.Response(200, json={"model": "claude-sonnet-4-6", "usage": {}}))
    auth = {"Authorization": f"Bearer {key}"}

    allowed = client.post(
        "/api/v1/messages",
        headers=auth,
        json={"model": "claude-sonnet-4-6", "output_config": {"effort": "medium"}},
    )
    assert allowed.status_code == 200
    assert upstream.call_count == 1

    blocked = client.post(
        "/api/v1/messages",
        headers=auth,
        json={"model": "claude-sonnet-4-6", "output_config": {"effort": "high"}},
    )
    assert blocked.status_code == 403
    assert "not allowed" in blocked.json()["detail"]
    assert upstream.call_count == 1

    implicit = client.post("/api/v1/messages", headers=auth, json={"model": "claude-sonnet-4-6"})
    assert implicit.status_code == 200
    assert upstream.call_count == 2

    updated = client.put(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"allowed_thinking_levels": ["high", "max"]},
    ).json()["user"]
    assert updated["allowed_thinking_levels"] == ["high", "max"]


def test_user_thinking_levels_reject_unknown_value(client, admin_headers):
    response = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"name": "invalid", "allowed_thinking_levels": ["ultra"]},
    )
    assert response.status_code == 422


@respx.mock
def test_stats_endpoints_reflect_usage(client, admin_headers, seed_account, make_user):
    seed_account("a1")
    key = make_user("ivan")

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 3, "output_tokens": 3, "cache_read_input_tokens": 3},
            },
        )
    )
    client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})

    overview = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert overview["total_accounts"] == 1
    assert overview["total_users"] == 1
    assert overview["total_keys"] == 1
    assert overview["requests"] == 1
    assert overview["tokens"] == 9
    assert overview["input_tokens"] == 6
    assert overview["output_tokens"] == 3
    assert overview["cached_input_tokens"] == 3
    assert overview["input_output_ratio"] == 2.0
    assert overview["cache_hit_rate"] == 0.5
    assert overview["input_rate_pct"] == 66.67
    assert overview["output_rate_pct"] == 33.33
    assert overview["cache_hit_rate_pct"] == 50.0
    assert "estimated_cost_usd" not in overview and "total_cost_usd" not in overview

    by_user = client.get("/api/v1/stats/by-user", headers=admin_headers).json()["users"]
    assert by_user[0]["user_name"] == "ivan"
    assert by_user[0]["tokens"] == 9

    act = client.get("/api/v1/stats/activity", headers=admin_headers).json()
    assert act["granularity"] == "day" and len(act["points"]) >= 1
    assert client.get("/api/v1/stats/hourly", headers=admin_headers).status_code == 200


@respx.mock
def test_events_and_model_mix_keep_routed_model_when_provider_reports_alias(
    client,
    admin_headers,
    seed_account,
    make_user,
):
    from app.utils.postgres import UsageRecordDb
    from app.utils.postgres.base import SessionFactory

    seed_account("model-alias-account")
    key = make_user("model-alias-user")
    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )
    )

    proxied = client.post(
        "/api/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "claude-sonnet-4-5", "messages": []},
    )
    assert proxied.status_code == 200, proxied.text

    with SessionFactory() as db:
        usage_row = db.query(UsageRecordDb).one()
        request_id = usage_row.request_id
        usage_row.model = "claude-haiku-4-5"
        db.commit()

    events_response = client.get(
        "/api/v1/events",
        headers=admin_headers,
        params={"request_id": request_id},
    )
    assert events_response.status_code == 200, events_response.text
    returned = next(event for event in events_response.json()["events"] if event["event_type"] == "response.returned")
    assert returned["metadata"]["model"] == "claude-sonnet-4-5"
    assert returned["metadata"]["requested_model"] == "claude-sonnet-4-5"
    assert returned["metadata"]["upstream_response_model"] == "claude-haiku-4-5"

    model_mix = client.get("/api/v1/stats/model-mix", headers=admin_headers)
    assert model_mix.status_code == 200, model_mix.text
    model_row = model_mix.json()["users"][0]["models"][0]
    assert model_row["model"] == "claude-sonnet-4-5"
    assert model_row["requests"] == 1
    assert model_row["input_tokens"] == 10
    assert model_row["output_tokens"] == 2
    assert model_row["average_tps"] > 0


def test_overview_aggregates_active_pool_capacity(client, admin_headers, seed_account):
    from datetime import datetime, timedelta, timezone

    from app.utils.models.api import AccountStatus, ProviderHealth
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    usable_ids = [
        seed_account("usable-1", session_used_pct=0.1, weekly_used_pct=0.2),
        seed_account("usable-2", session_used_pct=0.4, weekly_used_pct=0.3),
    ]
    disabled_id = seed_account(
        "disabled",
        status=AccountStatus.DISABLED,
        session_used_pct=0.0,
        weekly_used_pct=0.0,
    )
    exhausted_id = seed_account(
        "exhausted",
        session_used_pct=0.95,
        weekly_used_pct=0.2,
        rotation_threshold=0.9,
    )
    cooldown_id = seed_account(
        "cooldown",
        status=AccountStatus.COOLDOWN,
        session_used_pct=0.5,
        weekly_used_pct=0.2,
        rotation_threshold=0.4,
    )
    reauth_id = seed_account("reauth-required", session_used_pct=0.2, weekly_used_pct=0.1)

    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        db.query(AccountDb).filter(AccountDb.id.in_([*usable_ids, disabled_id, exhausted_id, cooldown_id])).update(
            {AccountDb.provider_health: ProviderHealth.HEALTHY}
        )
        db.query(AccountDb).filter(AccountDb.id == reauth_id).update({AccountDb.provider_health: ProviderHealth.REAUTH_REQUIRED})
        db.get(AccountDb, exhausted_id).session_reset_at = now + timedelta(hours=1)
        db.get(AccountDb, cooldown_id).weekly_reset_at = now + timedelta(hours=2)
        db.get(AccountDb, usable_ids[0]).session_reset_at = now + timedelta(hours=3)
        db.get(AccountDb, usable_ids[1]).weekly_reset_at = now + timedelta(hours=4)
        db.commit()

    overview = client.get("/api/v1/stats/overview", headers=admin_headers).json()
    assert overview["total_accounts"] == 6
    assert overview["active_accounts"] == 4
    assert overview["usable_accounts"] == 2
    assert overview["five_hour_average_pct"] == 48.75
    assert overview["weekly_average_pct"] == 22.5
    assert overview["pool_used_pct"] == 0.5125
    assert overview["pool_remaining_pct"] == 0.4875
    assert datetime.fromisoformat(overview["five_hour_reset_at"].replace("Z", "+00:00")) == now + timedelta(hours=1)
    assert datetime.fromisoformat(overview["weekly_reset_at"].replace("Z", "+00:00")) == now + timedelta(hours=2)


def test_user_scoped_stats_hide_unselected_users(client, admin_headers, make_user):
    make_user("alice")
    make_user("bob")
    users = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    alice_id = next(user["id"] for user in users if user["name"] == "alice")

    for path in (
        "/api/v1/stats/by-user",
        "/api/v1/stats/model-mix",
        "/api/v1/stats/thinking-level-mix",
    ):
        response = client.get(path, headers=admin_headers, params={"user_id": alice_id})
        assert response.status_code == 200
        assert [str(user["user_id"]) for user in response.json()["users"]] == [alice_id]


@respx.mock
def test_thinking_level_mix_groups_requests_by_user_and_level(client, admin_headers, seed_account, make_user):
    seed_account("a1")
    ivan_key = make_user("ivan")
    judy_key = make_user("judy")

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-sonnet-4-6", "usage": {"input_tokens": 8, "output_tokens": 5}})
    )

    def send(key, **body):
        response = client.post(
            "/api/v1/messages",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "claude-sonnet-4-6", "messages": [], **body},
        )
        assert response.status_code == 200, response.text

    send(ivan_key, output_config={"effort": "high"})
    send(ivan_key, output_config={"effort": "high"})
    send(ivan_key)
    send(judy_key, output_config={"effort": "low"})

    response = client.get("/api/v1/stats/thinking-level-mix", headers=admin_headers)
    assert response.status_code == 200
    by_name = {user["user_name"]: user for user in response.json()["users"]}
    assert by_name["ivan"]["total_requests"] == 3
    assert {level["level"]: level["requests"] for level in by_name["ivan"]["levels"]} == {
        "high": 2,
        "not_recorded": 1,
    }
    assert by_name["judy"]["total_requests"] == 1
    assert by_name["judy"]["levels"] == [{"level": "low", "requests": 1, "input_tokens": 8, "output_tokens": 5}]

    users = client.get("/api/v1/users", headers=admin_headers).json()["users"]
    ivan_id = next(user["id"] for user in users if user["name"] == "ivan")
    filtered = client.get(
        "/api/v1/stats/thinking-level-mix",
        headers=admin_headers,
        params={"user_id": ivan_id},
    ).json()["users"]
    assert sum(user["total_requests"] for user in filtered) == 3


@respx.mock
def test_stats_range_filter(client, admin_headers, seed_account, make_user):
    """start/end clamp the usage figures; a short window switches the activity series to hourly buckets."""
    from datetime import datetime, timedelta, timezone

    seed_account("a1")
    key = make_user("ivan")

    respx.route(host="testserver").pass_through()
    respx.post(ANTHROPIC_MESSAGES).mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5", "usage": {"input_tokens": 6, "output_tokens": 3}})
    )
    client.post("/api/v1/messages", headers={"Authorization": f"Bearer {key}"}, json={"model": "claude-haiku-4-5"})

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(minutes=30)).isoformat()

    def get(path, **params):
        return client.get(path, headers=admin_headers, params=params).json()

    # A window entirely in the future excludes the just-recorded request.
    empty = get("/api/v1/stats/overview", start=future)
    assert empty["requests"] == 0 and empty["tokens"] == 0
    assert empty["input_output_ratio"] is None and empty["cache_hit_rate"] == 0.0
    assert empty["input_rate_pct"] == 0.0 and empty["output_rate_pct"] == 0.0 and empty["cache_hit_rate_pct"] == 0.0
    # ...but the inventory counts are not range-filtered.
    assert empty["total_accounts"] == 1 and empty["total_keys"] == 1

    # A window covering now includes it.
    covered = get("/api/v1/stats/overview", start=past)
    assert covered["requests"] == 1 and covered["tokens"] == 9

    # The usage log and by-user respect the range too.
    assert get("/api/v1/stats/usage", start=future)["total"] == 0
    assert get("/api/v1/stats/usage", start=past)["total"] == 1
    busy = get("/api/v1/stats/by-user", start=future)["users"]
    assert busy[0]["user_name"] == "ivan" and busy[0]["tokens"] == 0  # user still listed, zeroed in window

    # A short window (<= 2 days) buckets the activity series by hour.
    short = get("/api/v1/stats/activity", start=recent, end=future)
    assert short["granularity"] == "hour"

    # An explicit end without a start means all time and includes the earliest request.
    all_time = get("/api/v1/stats/activity", end=future)
    assert sum(point["requests"] for point in all_time["points"]) == 1


def test_models_lists_what_the_key_may_use(client, admin_headers, make_user):
    key = make_user("model-lister")
    listed = client.get("/api/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert listed.status_code == 200, listed.text
    ids = [model["id"] for model in listed.json()["data"]]
    assert ids and all(model["type"] == "model" for model in listed.json()["data"])
    assert client.get("/api/v1/models").status_code == 401
