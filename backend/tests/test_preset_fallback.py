"""Fallback access inherits from presets without losing explicit user choices."""

from sqlalchemy import text

from app.scripts.migrate import sync_canonical_schema
from app.utils.postgres.base import engine


def test_fallback_inheritance_override_reload_and_clear(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Fallback enabled", "fallback_enabled": True}).json()
    inherited = client.post("/api/v1/users", headers=admin_headers, json={"name": "inherited", "preset_id": preset["id"]}).json()["user"]
    assert inherited["fallback_enabled"] is True
    assert inherited["preset_overrides"] == []
    opted_out = client.post(
        "/api/v1/users", headers=admin_headers, json={"name": "opt-out", "preset_id": preset["id"], "fallback_enabled": False}
    ).json()["user"]
    assert opted_out["fallback_enabled"] is False
    assert opted_out["preset_overrides"] == ["fallback_enabled"]
    path = f"/api/v1/users/{inherited['id']}"
    explicit = client.put(path, headers=admin_headers, json={"fallback_enabled": True}).json()["user"]
    assert explicit["preset_overrides"] == ["fallback_enabled"]
    changed = client.put(f"/api/v1/presets/{preset['id']}", headers=admin_headers, json={"name": "Fallback disabled", "fallback_enabled": False})
    assert changed.status_code == 200, changed.text
    users = {u["name"]: u for u in client.get("/api/v1/users", headers=admin_headers).json()["users"]}
    assert users["inherited"]["fallback_enabled"] is True
    assert users["opt-out"]["fallback_enabled"] is False
    cleared = client.delete(path + "/preset-overrides/fallback_enabled", headers=admin_headers).json()["user"]
    assert cleared["fallback_enabled"] is False
    assert cleared["preset_overrides"] == []


def test_preset_fallback_changes_propagate_to_inheriting_users(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Inherited fallback"}).json()
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "follows-preset", "preset_id": preset["id"]}).json()["user"]
    assert user["fallback_enabled"] is False
    response = client.put(f"/api/v1/presets/{preset['id']}", headers=admin_headers, json={"name": "Inherited fallback", "fallback_enabled": True})
    assert response.status_code == 200, response.text
    stored = client.get("/api/v1/users", headers=admin_headers).json()["users"][0]
    assert stored["fallback_enabled"] is True
    assert stored["preset_overrides"] == []


def test_migration_preserves_existing_fallback_access_and_is_idempotent(client, admin_headers):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Legacy preset"}).json()
    allowed = client.post(
        "/api/v1/users", headers=admin_headers, json={"name": "legacy-allowed", "preset_id": preset["id"], "fallback_enabled": True}
    ).json()["user"]
    client.post("/api/v1/users", headers=admin_headers, json={"name": "legacy-blocked", "preset_id": preset["id"]})
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET preset_overrides_json = '[]' WHERE id = :id"), {"id": allowed["id"]})
        connection.execute(text("ALTER TABLE presets DROP COLUMN fallback_enabled"))
    sync_canonical_schema()
    sync_canonical_schema()
    users = {u["name"]: u for u in client.get("/api/v1/users", headers=admin_headers).json()["users"]}
    assert users["legacy-allowed"]["fallback_enabled"] is True
    assert users["legacy-allowed"]["preset_overrides"] == ["fallback_enabled"]
    assert users["legacy-blocked"]["fallback_enabled"] is False
    assert users["legacy-blocked"]["preset_overrides"] == []
