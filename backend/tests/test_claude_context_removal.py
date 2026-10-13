"""Claude does not expose or enforce the Codex-only context policy."""

import json

from sqlalchemy import text

from app.scripts.migrate import sync_canonical_schema
from app.utils.postgres.base import engine


def test_claude_user_and_preset_responses_have_no_extended_context(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Claude preset", "fallback_enabled": True}).json()
    assert "allow_extended_context" not in preset
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "Devasheesh", "preset_id": preset["id"]}).json()["user"]
    assert "allow_extended_context" not in user
    assert user["preset_overrides"] == []
    assert user["fallback_enabled"] is True
    updated = client.put(f"/api/v1/users/{user['id']}", headers=admin_headers, json={"fallback_enabled": False}).json()["user"]
    assert updated["preset_overrides"] == ["fallback_enabled"]
    assert updated["fallback_enabled"] is False


def test_migration_removes_only_obsolete_context_markers(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Legacy Claude"}).json()
    user = client.post(
        "/api/v1/users", headers=admin_headers, json={"name": "devasheesh", "preset_id": preset["id"], "fallback_enabled": True}
    ).json()["user"]
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users ADD COLUMN allow_extended_context BOOLEAN NOT NULL DEFAULT FALSE"))
        connection.execute(text("ALTER TABLE presets ADD COLUMN allow_extended_context BOOLEAN NOT NULL DEFAULT FALSE"))
        connection.execute(
            text("UPDATE users SET allow_extended_context = TRUE, preset_overrides_json = :overrides WHERE id = :id"),
            {"overrides": json.dumps(["allow_extended_context", "fallback_enabled"]), "id": user["id"]},
        )
    # Even before cleanup, the API filters retired fields out of the UI data.
    listed = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert listed["preset_overrides"] == ["fallback_enabled"]
    sync_canonical_schema()
    sync_canonical_schema()
    with engine.connect() as connection:
        marker = connection.execute(text("SELECT preset_overrides_json FROM users WHERE id = :id"), {"id": user["id"]}).scalar_one()
        # Do not drop columns underneath the old slot during a live rollout.
        assert connection.execute(text("SELECT allow_extended_context FROM users WHERE id = :id"), {"id": user["id"]}).scalar_one() is True
    assert json.loads(marker) == ["fallback_enabled"]
    listed = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert listed["fallback_enabled"] is True
    assert "allow_extended_context" not in listed
