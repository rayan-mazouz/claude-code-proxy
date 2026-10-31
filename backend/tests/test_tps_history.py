"""TPS history averages measured requests without turning missing samples into zeros."""

import uuid
from datetime import datetime, timezone

import pytest

from app.utils.postgres import UsageRecordDb
from app.utils.postgres.base import SessionFactory


def test_activity_model_tps_buckets_filters_and_missing_measurements(client, admin_headers):
    user_ids = []
    for name in ("history-one", "history-two"):
        response = client.post("/api/v1/users", headers=admin_headers, json={"name": name})
        assert response.status_code == 201, response.text
        user_ids.append(uuid.UUID(response.json()["user"]["id"]))
    with SessionFactory() as db:

        def add(user, model, hour, tps):
            values = dict(
                id=uuid.uuid4(),
                user_id=user,
                model=model,
                input_tokens=100,
                output_tokens=200,
                tokens_per_second=tps,
                created_at=datetime(2026, 10, 1, hour, tzinfo=timezone.utc),
            )
            # Both proxies share analytics but store provider-specific cache columns.
            if hasattr(UsageRecordDb, "cache_creation_input_tokens"):
                values.update(cache_creation_input_tokens=0, cache_read_input_tokens=0)
            db.add(UsageRecordDb(**values))

        add(user_ids[0], "model-a", 1, 20)
        add(user_ids[0], "model-a", 1, 40)
        add(user_ids[0], "model-a", 1, None)
        add(user_ids[0], "model-a", 1, 0)
        add(user_ids[0], "model-b", 1, 80)
        add(user_ids[1], "model-a", 1, 120)
        add(user_ids[0], "model-a", 4, 999)  # Outside the requested range.
        db.commit()
    params = dict(start="2026-10-01T00:00:00Z", end="2026-10-01T04:00:00Z", user_id=str(user_ids[0]))
    response = client.get("/api/v1/stats/activity", headers=admin_headers, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["granularity"] == "hour"
    assert len(body["points"]) == 4
    assert body["points"][1]["model_tps"] == {"model-a": 30.0, "model-b": 80.0}
    assert body["points"][0]["model_tps"] == {}
    assert body["points"][2]["model_tps"] == {}
    filtered = client.get("/api/v1/stats/activity", headers=admin_headers, params={**params, "model": "model-b"}).json()
    assert filtered["points"][1]["model_tps"] == {"model-b": 80.0}
    daily = client.get("/api/v1/stats/activity", headers=admin_headers, params={**params, "end": "2026-10-04T00:00:00Z"}).json()
    assert daily["granularity"] == "day"
    assert daily["points"][0]["model_tps"]["model-a"] == pytest.approx((20 + 40 + 999) / 3, abs=0.01)
    assert daily["points"][1]["model_tps"] == {}
