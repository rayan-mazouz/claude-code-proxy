"""Flush final SSE data and distinguish unknown metrics from reported zero."""

from app.utils import usage


def test_final_usage_without_newline_is_preserved():
    accumulator = usage.StreamUsageAccumulator()
    accumulator.feed(b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":15}}}\n\n')
    accumulator.feed(b'data: {"type":"message_delta","usage":{"output_tokens":20}}')
    result = accumulator.result()
    assert result.input_tokens == 15
    assert result.output_tokens == 20
    assert result.usage_reported is True


def test_reported_zero_is_distinct_from_absent_usage():
    assert usage.usage_from_json({"usage": {"input_tokens": 0, "output_tokens": 0}}).usage_reported
    assert not usage.usage_from_json({"type": "error"}).usage_reported


def test_protocol_completion_survives_transport_cancellation(client, admin_headers, seed_account, make_user):
    import asyncio

    import httpx
    import respx

    from app.utils.postgres import ProxyEventDb
    from app.utils.postgres.base import SessionFactory

    class CompletedThenCanceled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":1}}}\n\n'
            yield b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            yield b'data: {"type":"message_delta","usage":{"output_tokens":1}}\n\n'
            yield b'data: {"type":"message_stop"}\n\n'
            raise asyncio.CancelledError()

    seed_account("completed-cancel")
    key = make_user("completed-cancel-user")
    with respx.mock:
        respx.route(host="testserver").pass_through()
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=CompletedThenCanceled(),
            )
        )
        client.post(
            "/api/v1/messages",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "test"}],
                "stream": True,
                "max_tokens": 10,
            },
        )
    with SessionFactory() as db:
        types = [row.event_type for row in db.query(ProxyEventDb).all()]
    assert "response.completed" in types
    assert "response.failed" not in types
