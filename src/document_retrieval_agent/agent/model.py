"""A small HTTP client for tool-calling, OpenAI-compatible model endpoints."""

import asyncio
from typing import Protocol

import httpx

DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
MISSING_API_KEY_MESSAGE = (
    "Set GEMINI_API_KEY in .env, or DOCUMENT_RETRIEVAL_AGENT_API_KEY for another provider"
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class Model(Protocol):
    """The only model operation the agent needs; test models implement it too."""

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


def retry_delay(response: httpx.Response) -> float:
    """Allow a rate-limit window to reset while keeping waits bounded."""
    try:
        default = "60" if response.status_code == 429 else "1"
        return min(60, max(1, float(response.headers.get("retry-after", default))))
    except ValueError:
        return 1


class ChatModel:
    def __init__(
        self, model=DEFAULT_MODEL, api_base=DEFAULT_API_BASE, api_key="", *, transport=None
    ):
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        if not self.api_key:
            raise ValueError(MISSING_API_KEY_MESSAGE)
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto"}
        async with httpx.AsyncClient(timeout=90, transport=self.transport) as client:
            response = await self._post_with_retry(client, payload)
        return self._read_completion(response)

    async def _post_with_retry(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        for attempt in range(2):
            last_attempt = attempt == 1
            try:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
            except httpx.TransportError as exc:
                if last_attempt:
                    raise RuntimeError(
                        "Could not reach the model endpoint; check network access"
                    ) from exc
                await asyncio.sleep(1)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and not last_attempt:
                await asyncio.sleep(retry_delay(response))
                continue
            if response.is_error:
                detail = response.text.replace(self.api_key, "[redacted]")[:800]
                raise RuntimeError(f"Model endpoint returned HTTP {response.status_code}: {detail}")
            return response
        raise RuntimeError("Model request attempts exhausted")

    def _read_completion(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Expected an assistant message")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Model endpoint returned an invalid chat-completion response"
            ) from exc
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise RuntimeError(
                f"Model stopped before completing its answer: {choice['finish_reason']}"
            )
        self._record_usage(body.get("usage") or {})
        # Preserve provider fields, including Gemini thought signatures, for the next turn.
        return message

    def _record_usage(self, usage: dict) -> None:
        for key in self.usage:
            value = usage.get(key)
            if isinstance(value, int):
                self.usage[key] += value
