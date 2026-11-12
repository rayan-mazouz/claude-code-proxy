import json
import uuid
from datetime import datetime, timedelta, timezone

from app.utils.postgres import ProxyEventDb, UsageRecordDb
from app.utils.postgres.base import SessionFactory


def test_request_history_counts_percentiles_filters_and_bundle(client, admin_headers):
    users = [client.post("/api/v1/users", headers=admin_headers, json={"name": name}).json()["user"] for name in ("reader-one", "reader-two")]
    ids = [uuid.UUID(u["id"]) for u in users]
    with SessionFactory() as db:

        def add(uid, model, hour, size, reported=True):
            row = dict(
                id=uuid.uuid4(),
                user_id=uid,
                model=model,
                input_tokens=size,
                output_tokens=size * 2,
                tokens_per_second=20 if size else None,
                created_at=datetime(2026, 10, 1, hour, tzinfo=timezone.utc),
            )
            if hasattr(UsageRecordDb, "cache_creation_input_tokens"):
                row.update(cache_creation_input_tokens=0, cache_read_input_tokens=0, usage_reported=reported)
            db.add(UsageRecordDb(**row))

        for size in (10, 100, 1000, 0):
            add(ids[0], "model-a", 1, size)
        add(ids[0], "model-b", 1, 50)
        add(ids[1], "model-a", 1, 9999)
        add(ids[0], "inactive-model", 5, 500)
        db.commit()
    params = {"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T04:00:00Z", "user_id": str(ids[0]), "model": "model-a"}
    r = client.get("/api/v1/stats/activity", headers=admin_headers, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    point = body["points"][1]
    assert point["requests_by_model"] == {"model-a": 4}
    assert point["requests_by_user"] == {str(ids[0]): 4}
    assert body["user_names"] == {str(ids[0]): "reader-one"}
    assert point["input_distribution"] == {"samples": 3, "p10": 28.0, "median": 100.0, "p90": 820.0}
    assert point["output_distribution"] == {"samples": 3, "p10": 56.0, "median": 200.0, "p90": 1640.0}
    assert body["points"][0]["input_distribution"] is None
    assert body["points"][0]["requests_by_model"] == {}
    # Dropdown choices ignore the selected model so users can switch between active models.
    lookup = client.get("/api/v1/lookups", headers=admin_headers, params=params)
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["models"] == ["model-a", "model-b"]
    bundle = client.get("/api/v1/stats/dashboard", headers=admin_headers, params=params)
    assert bundle.status_code == 200, bundle.text
    data = bundle.json()
    assert "distributions" not in data  # Removed panel must not add percentile-query work.
    assert data["activity"] == body
    assert data["overview"]["requests"] == 4
    assert data["model_mix"] is not None and data["thinking_level_mix"] is not None
    assert data["lookups"]["models"] == ["model-a", "model-b"]
    unfiltered = client.get(
        "/api/v1/stats/activity", headers=admin_headers, params={k: v for k, v in params.items() if k not in ("user_id", "model")}
    ).json()
    assert unfiltered["points"][1]["requests_by_model"] == {"model-a": 5, "model-b": 1}
    assert unfiltered["points"][1]["requests_by_user"] == {str(ids[0]): 5, str(ids[1]): 1}
    daily = client.get("/api/v1/stats/activity", headers=admin_headers, params={**params, "end": "2026-10-04T00:00:00Z"}).json()
    assert daily["granularity"] == "day"
    assert daily["points"][0]["input_distribution"]["median"] == 100
    assert daily["points"][1]["input_distribution"] is None


def test_event_request_group_count_and_page_boundaries(client, admin_headers):
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        for req in range(3):
            for step, kind in enumerate(("request.received", "response.completed")):
                db.add(
                    ProxyEventDb(
                        id=uuid.uuid4(),
                        request_id=f"req_{req}",
                        event_type=kind,
                        created_at=now + timedelta(seconds=req * 2 + step),
                        metadata_json=json.dumps({}),
                    )
                )
        db.commit()
    params = {"group_by": "request", "limit": 1}
    first = client.get("/api/v1/events", headers=admin_headers, params=params)
    assert first.status_code == 200, first.text
    second = client.get("/api/v1/events", headers=admin_headers, params={**params, "offset": 1})
    assert second.status_code == 200, second.text
    assert first.json()["total"] == 3
    assert len(first.json()["events"]) == 2
    assert {r["request_id"] for r in first.json()["events"]}.isdisjoint({r["request_id"] for r in second.json()["events"]})
    beyond = client.get("/api/v1/events", headers=admin_headers, params={**params, "offset": 99})
    assert beyond.status_code == 200, beyond.text
    assert beyond.json()["events"] == [] and beyond.json()["total"] == first.json()["total"]
