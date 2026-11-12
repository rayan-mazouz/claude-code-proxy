import uuid
from datetime import datetime, timedelta, timezone

from app.utils import archive
from app.utils.postgres import UsageRecordDb, UserDb
from app.utils.postgres.base import SessionFactory


def test_archive_listing_filters_pages_and_recognizes_response_only_capture(client, admin_headers, make_user, monkeypatch):
    class Store:
        async def find_body_key(self, event_id, created_at, filename):
            return "body" if event_id == str(captured) and filename == "response.json.gz" else None

    captured = uuid.uuid4()
    monkeypatch.setattr(archive, "get_archive_store", lambda: Store())
    make_user("archive-user")
    now = datetime.now(timezone.utc)
    with SessionFactory() as db:
        user_id = db.query(UserDb).filter(UserDb.name == "archive-user").one().id
        for index, (rid, model, code) in enumerate([(f"req_{captured.hex}", "test-a", 200), ("legacy", "test-b", 503)]):
            values = dict(
                id=uuid.uuid4(),
                user_id=user_id,
                model=model,
                input_tokens=20,
                output_tokens=5,
                request_id=rid,
                status_code=code,
                created_at=now + timedelta(seconds=index),
            )
            for field in ("cached_input_tokens", "cache_write_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                if hasattr(UsageRecordDb, field):
                    values[field] = 0
            if hasattr(UsageRecordDb, "request_mode"):
                values["request_mode"] = "standard"
            db.add(UsageRecordDb(**values))
        db.commit()
    assert client.get("/api/v1/requests").status_code == 401
    response = client.get("/api/v1/requests", headers=admin_headers, params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response.json()["items"][0]["request_id"] == "legacy"
    assert not response.json()["items"][0]["archive_available"]
    next_page = client.get("/api/v1/requests", headers=admin_headers, params={"limit": 1, "offset": 1}).json()
    assert next_page["items"][0]["archive_available"]
    assert next_page["items"][0]["event_id"] == str(captured)
    for params in (
        {"model": "test-a"},
        {"status_code": 200},
        {"search": captured.hex},
        {"user_id": str(user_id), "end": (now + timedelta(milliseconds=500)).isoformat()},
    ):
        data = client.get("/api/v1/requests", headers=admin_headers, params=params).json()
        assert data["total"] == 1
        assert data["items"][0]["request_id"] == f"req_{captured.hex}"
