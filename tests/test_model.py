import json

import httpx
import pytest

from document_retrieval_agent.agent.model import ChatModel


async def test_model_endpoint_and_provider_fields_are_preserved():
    message = {"role": "assistant", "content": "Ready", "extra_content": {"signature": "test"}}

    def respond(request):
        assert str(request.url) == "https://example.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "small-model"
        assert payload["tools"] == []
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            },
        )

    model = ChatModel(
        "small-model",
        "https://example.test/v1/",
        "test-key",
        transport=httpx.MockTransport(respond),
    )
    assert await model.complete([], []) == message
    assert model.usage == {"prompt_tokens": 20, "completion_tokens": 3}


async def test_missing_key_fails_without_network():
    with pytest.raises(ValueError, match="Set GEMINI_API_KEY"):
        await ChatModel().complete([], [])


async def test_http_errors_redact_credentials():
    def respond(request):
        return httpx.Response(401, text="Invalid key secret-value")

    model = ChatModel(api_key="secret-value", transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match="HTTP 401") as exc:
        await model.complete([], [])
    assert "secret-value" not in str(exc.value)


async def test_transient_error_is_retried_once(monkeypatch):
    calls = 0

    async def no_sleep(delay):
        pass

    monkeypatch.setattr("document_retrieval_agent.agent.model.asyncio.sleep", no_sleep)

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Ready"}}]}
        )

    model = ChatModel(api_key="test-key", transport=httpx.MockTransport(respond))
    assert (await model.complete([], []))["content"] == "Ready"
    assert calls == 2


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": None}]}])
async def test_malformed_provider_responses_fail_clearly(body):
    model = ChatModel(
        api_key="test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    with pytest.raises(RuntimeError, match="invalid chat-completion"):
        await model.complete([], [])


async def test_repeated_server_errors_stop_after_two_attempts(monkeypatch):
    calls = 0

    async def no_sleep(delay):
        pass

    monkeypatch.setattr("document_retrieval_agent.agent.model.asyncio.sleep", no_sleep)

    def respond(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="Temporarily unavailable")

    model = ChatModel(api_key="test-key", transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        await model.complete([], [])
    assert calls == 2


@pytest.mark.parametrize("reason", ["length", "content_filter"])
async def test_incomplete_model_answers_are_rejected(reason):
    body = {
        "choices": [
            {"message": {"role": "assistant", "content": "Partial reply"}, "finish_reason": reason}
        ]
    }
    model = ChatModel(
        api_key="test-key", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    with pytest.raises(RuntimeError, match="stopped before completing"):
        await model.complete([], [])


def test_rate_limit_retry_allows_window_to_reset():
    from document_retrieval_agent.agent.model import retry_delay

    assert retry_delay(httpx.Response(429)) == 60
    assert retry_delay(httpx.Response(429, headers={"retry-after": "13"})) == 13
    assert retry_delay(httpx.Response(503)) == 1
    assert retry_delay(httpx.Response(429, headers={"retry-after": "120"})) == 60
