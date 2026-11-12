import json
from datetime import datetime, timezone

from app.utils import rotation


def test_anthropic_429_body_persists_authoritative_reset(seed_account):
    account_id = seed_account("hard-limit")
    from app.utils.postgres import AccountDb
    from app.utils.postgres.base import SessionFactory

    with SessionFactory() as db:
        account = db.get(AccountDb, account_id)
        reached, reset_at, _scoped = rotation.apply_rate_limit_body(
            account,
            json.dumps({"type": "error", "error": {"type": "rate_limit_error", "resets_in_seconds": 3600}}).encode(),
        )
        assert reached == "rate_limit_error"
        assert account.session_used_pct == 1.0
        assert reset_at is not None
        assert 3590 <= (reset_at - datetime.now(timezone.utc)).total_seconds() <= 3600
