"""Conversation and tool execution for a single document retrieval session."""

import asyncio
import json
from collections.abc import Callable

from .model import Model
from .output import log_event, save_result
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, Tools

COMPLETION_REMINDER = (
    "Retrieval is unfinished. Complete the listed downloads and create "
    "the archive, or clearly explain the blocking error."
)


class AgentSession:
    """Keep request history and tools together while the user answers clarifications."""

    def __init__(
        self,
        model: Model,
        tools: Tools,
        *,
        max_calls: int = 30,
        timeout: float = 600,
        progress: Callable[[str], None] | None = None,
        conversation: list[dict] | None = None,
    ):
        self.model = model
        self.tools = tools
        self.max_calls = max_calls
        self.timeout = timeout
        self.progress = progress
        self.messages = conversation if conversation is not None else []
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})
        self.calls_used = 0
        self.completion_reminded = False

    async def respond(self, request: str) -> tuple[dict, str]:
        """Answer one user message and save its result, including partial failures."""
        self.messages.append({"role": "user", "content": request})
        self.calls_used = 0
        self.completion_reminded = False
        reply, error = "", None
        try:
            async with asyncio.timeout(self.timeout):
                reply = await self._run_loop()
        except TimeoutError:
            error = f"Run exceeded its {self.timeout:g}-second time limit"
        except Exception as exc:
            error = str(exc)

        result, reply = self._build_result(reply, error)
        save_result(self.tools.output, result, reply)
        return result, reply

    async def _run_loop(self) -> str:
        for _ in range(self.max_calls + 3):
            message = await self.model.complete(self.messages, TOOL_SCHEMAS)
            self.messages.append(message)
            calls = message.get("tool_calls") or []
            if calls:
                for call in calls:
                    await self._execute_tool(call)
                continue

            reply = message.get("content")
            if not isinstance(reply, str) or not reply.strip():
                raise RuntimeError("Model returned neither tool calls nor a reply")
            if self._needs_completion_reminder():
                self.messages.append({"role": "user", "content": COMPLETION_REMINDER})
                self.completion_reminded = True
                continue
            log_event(self.tools.output, "reply", text=reply)
            return reply
        raise RuntimeError("Agent turn budget exhausted")

    async def _execute_tool(self, call: dict) -> None:
        self.calls_used += 1
        if self.calls_used > self.max_calls:
            raise RuntimeError(f"Tool-call budget exhausted ({self.max_calls})")
        function = call["function"]
        name, arguments = function["name"], function["arguments"]
        if self.progress:
            self.progress(name)
        try:
            result = await self.tools.execute(name, json.loads(arguments))
            outcome = {"ok": True, "result": result}
        except Exception as exc:
            # Tool failures go back to the model so it can retry or explain them.
            outcome = {"ok": False, "error": str(exc)}
        log_event(self.tools.output, "tool", name=name, arguments=arguments, output=outcome)
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(outcome),
            }
        )

    def _needs_completion_reminder(self) -> bool:
        return bool(self.tools.matter and not self.tools.archive and not self.completion_reminded)

    def _build_result(self, reply: str, error: str | None) -> tuple[dict, str]:
        result = self.tools.result()
        if error:
            result["error"] = error
            result["status"] = "partial" if self.tools.downloads else "failed"
            reply = f"The request could not be completed: {error}\n"
        elif not self.tools.matter:
            result["status"] = "needs_input" if self.calls_used == 0 else "incomplete"

        if not error and result["status"] in {"incomplete", "partial"}:
            if result["archive"]:
                notice = (
                    f"I downloaded {result['downloaded_count']} file(s), but couldn't complete "
                    "the full request. The ZIP contains only the successful downloads.\n\n"
                )
            else:
                notice = "I couldn't complete this request, so no ZIP is ready.\n\n"
            reply = notice + reply

        result["request"] = next(m["content"] for m in self.messages if m["role"] == "user")
        result["tool_calls"] = self.calls_used
        if hasattr(self.model, "usage"):
            result["usage"] = self.model.usage
        return result, reply


async def run_agent(
    request: str,
    model: Model,
    tools: Tools,
    *,
    max_calls: int = 30,
    timeout: float = 600,
    progress: Callable[[str], None] | None = None,
    conversation: list[dict] | None = None,
) -> tuple[dict, str]:
    """Convenience entry point for callers that only need to submit one message."""
    session = AgentSession(
        model,
        tools,
        max_calls=max_calls,
        timeout=timeout,
        progress=progress,
        conversation=conversation,
    )
    return await session.respond(request)
