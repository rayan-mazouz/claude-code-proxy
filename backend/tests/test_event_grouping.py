import json
import uuid
from datetime import datetime, timedelta, timezone

from app.utils.postgres import ProxyEventDb
from app.utils.postgres.base import SessionFactory


def test_request_pages_keep_lifecycle_together_and_respect_filters(client, admin_headers):
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        for index, request_id in enumerate(["req_old", "req_new"]):
            for step, kind in enumerate(["request.received", "account.attempt", "response.returned"]):
                db.add(
                    ProxyEventDb(
                        id=uuid.uuid4(),
                        request_id=request_id,
                        event_type=kind,
                        created_at=now + timedelta(seconds=index * 10 + step),
                        metadata_json=json.dumps({"model": "gpt-6.1-sol", "operation": "inference"}),
                    )
                )
        db.commit()
    page = client.get("/api/v1/events?group_by=request&limit=1", headers=admin_headers).json()
    assert page["total"] == 2 and len(page["events"]) == 3
    assert {e["request_id"] for e in page["events"]} == {"req_new"}
    next_page = client.get("/api/v1/events?group_by=request&limit=1&offset=1", headers=admin_headers).json()
    assert {e["request_id"] for e in next_page["events"]} == {"req_old"}
    assert len(next_page["events"]) == 3
    filtered = client.get("/api/v1/events?group_by=request&model=gpt-6.1-sol&event_type=account.attempt", headers=admin_headers).json()
    assert filtered["total"] == 2 and len(filtered["events"]) == 6
    # Expansion uses the existing request-id endpoint to fetch every sub-event.
    details = client.get("/api/v1/events?request_id=req_new", headers=admin_headers).json()
    assert details["total"] == 3 and len(details["events"]) == 3
    assert client.get("/api/v1/events?group_by=request&model=unknown", headers=admin_headers).json()["total"] == 0
    # Other grouping views retain the original event-based pagination.
    raw = client.get("/api/v1/events?limit=1", headers=admin_headers).json()
    assert raw["total"] == 6 and len(raw["events"]) == 1


def test_deleted_user_keeps_event_name_snapshot(client, admin_headers):
    from app.utils import events
    from app.utils.postgres import UserDb

    created = client.post("/api/v1/users", headers=admin_headers, json={"name": "event-name-snapshot"}).json()["user"]
    uid = uuid.UUID(created["id"])
    events.record_event("request.received", "req_snapshot", user_id=uid)
    assert client.delete("/api/v1/users/" + str(uid), headers=admin_headers).status_code == 204
    rows = client.get("/api/v1/events?request_id=req_snapshot", headers=admin_headers).json()["events"]
    assert rows[0]["user_id"] == str(uid)
    assert rows[0]["metadata"]["user_name"] == "event-name-snapshot"
    assert rows[0]["metadata"]["user_deleted"] is True
    with SessionFactory() as db:
        assert db.get(UserDb, uid) is None
        db.add(ProxyEventDb(id=uuid.uuid4(), request_id="req_legacy_deleted", event_type="request.received", user_id=uid))
        db.commit()
    rows = client.get("/api/v1/events?request_id=req_legacy_deleted", headers=admin_headers).json()["events"]
    assert rows[0]["metadata"]["user_name"] == "Deleted user"


def test_stream_terminal_outcome_overrides_http_200(client, admin_headers):
    with SessionFactory() as db:
        for rid, terminal in [("req_ok", "response.completed"), ("req_bad", "response.failed"), ("req_pending", None)]:
            db.add(
                ProxyEventDb(
                    id=uuid.uuid4(),
                    request_id=rid,
                    event_type="response.returned",
                    metadata_json=json.dumps({"request_outcome": "pending"}),
                    status_code=200,
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
            )
            if terminal:
                db.add(ProxyEventDb(id=uuid.uuid4(), request_id=rid, event_type=terminal, status_code=200))
        db.commit()
    for rid, expected in [("req_ok", "succeeded"), ("req_bad", "failed"), ("req_pending", "pending")]:
        rows = client.get("/api/v1/events", params={"request_id": rid}, headers=admin_headers).json()["events"]
        assert all(e["metadata"]["request_outcome"] == expected for e in rows)


def test_legacy_http_success_does_not_require_retained_usage(client, admin_headers):
    with SessionFactory() as db:
        db.add(ProxyEventDb(id=uuid.uuid4(), request_id="req_deleted_success", user_id=uuid.uuid4(), event_type="response.returned", status_code=200))
        db.commit()
    row = client.get("/api/v1/events?request_id=req_deleted_success", headers=admin_headers).json()["events"][0]
    assert row["metadata"]["user_name"] == "Deleted user"
    assert row["metadata"]["request_outcome"] == "succeeded"
