"""Persist user override intent independently from the preset's current value."""

import pytest

from app.utils import presets


@pytest.mark.parametrize("field", list(presets.POLICY_COLUMNS))
def test_explicit_same_value_override_survives_reload_and_preset_edit(client, admin_headers, field):
    preset = client.post("/api/v1/presets", headers=admin_headers, json={"name": "Baseline"}).json()
    user = client.post("/api/v1/users", headers=admin_headers, json={"name": "override-reload", "preset_id": preset["id"]}).json()["user"]
    path = f"/api/v1/users/{user['id']}"
    updated = client.put(path, headers=admin_headers, json={field: user[field]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["user"]["preset_overrides"] == [field]

    def reloaded():
        return next(u for u in client.get("/api/v1/users", headers=admin_headers).json()["users"] if u["id"] == user["id"])

    assert reloaded()["preset_overrides"] == [field]
    payload = {key: preset[key] for key in presets.POLICY_COLUMNS}
    payload["name"] = "Renamed baseline"
    response = client.put(f"/api/v1/presets/{preset['id']}", headers=admin_headers, json=payload)
    assert response.status_code == 200, response.text
    assert reloaded()["preset_overrides"] == [field]
    assert reloaded()[field] == user[field]
    cleared = client.delete(f"{path}/preset-overrides/{field}", headers=admin_headers)
    assert cleared.status_code == 200, cleared.text
    assert reloaded()["preset_overrides"] == []
