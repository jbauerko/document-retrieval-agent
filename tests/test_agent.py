import json
import zipfile

import pytest

from document_retrieval_agent.agent import run_agent
from document_retrieval_agent.browser import parse_counts
from document_retrieval_agent.tools import CATEGORIES, Tools, safe_filename


class FakeBrowser:
    def __init__(self, count=12, fail=None):
        self.count = count
        self.fail = fail
        self.download_calls = []

    async def search_matter(self, matter_number):
        counts = dict.fromkeys(CATEGORIES, 0)
        counts["Other Documents"] = self.count
        return {
            "matter_number": matter_number,
            "metadata": {"title": "Test matter"},
            "counts": counts,
        }

    async def list_documents(self, document_type):
        return [{"id": str(i), "title": f"Document {i}"} for i in range(self.count)]

    async def download_document(self, document_id, directory):
        self.download_calls.append(document_id)
        if document_id == self.fail:
            raise ValueError("Download unavailable")
        path = directory / f"{document_id}.pdf"
        path.write_bytes(b"%PDF-test-content")
        return {"path": str(path)}


async def prepare(tmp_path, count=12, fail=None):
    tools = Tools(FakeBrowser(count, fail), tmp_path)
    await tools.search_matter("M12205")
    listing = await tools.list_documents("Other Documents")
    return tools, listing["documents"]


async def test_limit_idempotency_and_zip(tmp_path):
    tools, documents = await prepare(tmp_path)
    assert len(documents) == 10
    with pytest.raises(ValueError, match="document ID"):
        await tools.download_document("11")
    for document in documents:
        await tools.download_document(document["id"])
    await tools.download_document("0")
    assert len(tools.browser.download_calls) == 10
    result = await tools.create_archive()
    assert result["status"] == "complete"
    assert result["total_files"] == 12
    assert result["downloaded_count"] == 10
    with zipfile.ZipFile(result["archive"]) as archive:
        assert len(archive.namelist()) == 10
        assert archive.testzip() is None
    with pytest.raises(ValueError, match="One matter"):
        await tools.search_matter("M12383")
    with pytest.raises(ValueError, match="One document category"):
        await tools.list_documents("Exhibits")


async def test_partial_downloads_are_reported(tmp_path):
    tools, documents = await prepare(tmp_path, 3, fail="1")
    await tools.download_document("0")
    with pytest.raises(ValueError, match="Attempt all"):
        await tools.create_archive()
    with pytest.raises(ValueError, match="unavailable"):
        await tools.download_document("1")
    await tools.download_document("2")
    result = await tools.create_archive()
    assert result["status"] == "partial"
    assert result["downloaded_count"] == 2
    assert "1" in result["failures"]
    with zipfile.ZipFile(result["archive"]) as archive:
        assert archive.namelist() == ["0.pdf", "2.pdf"]


async def test_empty_category_and_failed_download_are_distinct(tmp_path):
    tools, _ = await prepare(tmp_path / "empty", 0)
    assert (await tools.create_archive())["status"] == "complete"
    tools, _ = await prepare(tmp_path / "failed", 1, fail="0")
    with pytest.raises(ValueError):
        await tools.download_document("0")
    with pytest.raises(ValueError, match="No successful"):
        await tools.create_archive()


def call(name, **args):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


class ScriptedModel:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.seen_tool_errors = []

    async def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            self.seen_tool_errors.append(json.loads(messages[-1]["content"]))
        return next(self.messages)


async def test_agent_recovers_from_invalid_tool_and_writes_artifacts(tmp_path):
    model = ScriptedModel(
        [
            call("execute_shell", command="bad"),
            call("search_matter", matter_number="M12205"),
            call("list_documents", document_type="Other Documents"),
            call("download_document", document_id="0"),
            call("create_archive"),
            {"role": "assistant", "content": "Found one file. ZIP is ready."},
        ]
    )
    tools = Tools(FakeBrowser(1), tmp_path)
    result, reply = await run_agent("Other Documents for M12205", model, tools)
    assert result["status"] == "complete"
    assert model.seen_tool_errors[0]["ok"] is False
    assert json.loads((tmp_path / "result.json").read_text())["downloaded_count"] == 1
    assert result["reply"]["body"] == reply
    assert result["reply"]["subject"] == "M12205 — Other Documents"
    assert result["reply"]["attachments"] == [result["archive"]]
    assert (tmp_path / "reply.txt").read_text().split("\n\n", 1)[1].strip() == reply
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 6


async def test_clarification_does_not_access_browser(tmp_path):
    model = ScriptedModel([{"role": "assistant", "content": "Which document type?"}])
    result, _ = await run_agent("M12205", model, Tools(None, tmp_path))
    assert result["status"] == "needs_input"
    assert result["archive"] is None
    assert result["reply"]["attachments"] == []


async def test_budget_stops_loop_and_saves_failure(tmp_path):
    model = ScriptedModel([call("bad"), call("bad"), call("bad")])
    result, _ = await run_agent("request", model, Tools(None, tmp_path), max_calls=2)
    assert result["status"] == "failed"
    assert "budget" in result["error"]
    assert (tmp_path / "result.json").exists()


async def test_premature_final_answer_cannot_mark_retrieval_complete(tmp_path):
    model = ScriptedModel(
        [
            call("search_matter", matter_number="M12205"),
            {"role": "assistant", "content": "Done."},
            {"role": "assistant", "content": "Done."},
        ]
    )
    result, reply = await run_agent(
        "Other Documents for M12205", model, Tools(FakeBrowser(1), tmp_path)
    )
    assert result["status"] == "incomplete"
    assert reply.startswith("I couldn't complete this request, so no ZIP is ready.")
    assert result["reply"]["attachments"] == []


async def test_timeout_saves_failure(tmp_path):
    import asyncio

    class SlowModel:
        async def complete(self, messages, tools):
            await asyncio.sleep(10)

    result, _ = await run_agent("request", SlowModel(), Tools(None, tmp_path), timeout=0.01)
    assert result["status"] == "failed"
    assert "time limit" in result["error"]


def test_counts_require_every_category_and_support_commas():
    text = "\n".join(f"{category} - {i:,}" for i, category in enumerate(CATEGORIES, 1000))
    assert parse_counts(text)["Other Documents"] == 1002
    with pytest.raises(ValueError, match="live count"):
        parse_counts("Exhibits - 1")


@pytest.mark.parametrize("name", ["../../file.pdf", r"C:\tmp\file.pdf", "..", "", "a\x00b.pdf"])
def test_download_names_are_safe(name):
    filename = safe_filename(name)
    assert filename and filename not in {".", ".."}
    assert not any(character in filename for character in ["/", "\\", "\x00"])


@pytest.mark.parametrize("input_mode", ["argument", "file", "stdin", "prompt", "clarification"])
def test_cli_request_to_zip_and_draft(tmp_path, monkeypatch, input_mode):
    """Exercise CLI, HTTP model client, agent loop and artifacts together, offline."""
    import sys
    from io import StringIO

    import httpx

    from document_retrieval_agent import cli
    from document_retrieval_agent.model import ChatModel

    steps = [
        call("search_matter", matter_number="M12205"),
        call("list_documents", document_type="Other Documents"),
        call("download_document", document_id="0"),
        call("download_document", document_id="1"),
        call("create_archive"),
        {
            "role": "assistant",
            "content": "Hi User, Test matter has 2 Other Documents and "
            "2 files total. Both documents are available in the ZIP.",
        },
    ]
    original_request = "Hi Agent, could you get the other docs for m 12205? Thanks!"
    if input_mode == "clarification":
        original_request = "Could you get the documents for m 12205?"
        steps.insert(0, {"role": "assistant", "content": "Which document category would you like?"})
    replies = iter(steps)
    last_messages = []

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == "gemini-3.5-flash-lite"
        assert request.headers["Authorization"] == "Bearer test-key"
        last_messages[:] = payload["messages"]
        return httpx.Response(200, json={"choices": [{"message": next(replies)}]})

    class Browser(FakeBrowser):
        def __init__(self, **kwargs):
            super().__init__(count=2)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.chdir(tmp_path)
    for name in [
        "DOCUMENT_RETRIEVAL_AGENT_MODEL",
        "DOCUMENT_RETRIEVAL_AGENT_API_BASE",
        "DOCUMENT_RETRIEVAL_AGENT_API_KEY",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=test-key\n")
    argv = ["document-retrieval-agent"]
    if input_mode in {"argument", "clarification"}:
        argv.append(original_request)
    elif input_mode == "file":
        path = tmp_path / "request.txt"
        path.write_text(original_request)
        argv.extend(["--request-file", str(path)])
    stream = StringIO(original_request if input_mode == "stdin" else "")
    stream.isatty = lambda: input_mode in {"prompt", "clarification"}
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(
        "builtins.input",
        lambda _: "Other docs, please" if input_mode == "clarification" else original_request,
    )
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(cli, "UARBBrowser", Browser)
    monkeypatch.setattr(
        cli,
        "ChatModel",
        lambda model, base, key: ChatModel(
            model, base, key, transport=httpx.MockTransport(respond)
        ),
    )
    assert cli.main() == 0
    run = next((tmp_path / "runs").iterdir())
    result = json.loads((run / "result.json").read_text())
    assert result["status"] == "complete"
    assert result["downloaded_count"] == 2
    assert result["request"] == original_request
    assert result["reply"]["body"].startswith("Hi User,")
    assert (run / "reply.txt").read_text().startswith("Subject: M12205 — Other Documents\n\n")
    user_messages = [m["content"] for m in last_messages if m["role"] == "user"]
    assert user_messages[0] == original_request
    if input_mode == "clarification":
        assert user_messages == [original_request, "Other docs, please"]
        assert sum(m["role"] == "system" for m in last_messages) == 1
    with zipfile.ZipFile(result["archive"]) as archive:
        assert archive.namelist() == ["0.pdf", "1.pdf"]


async def test_follow_up_reuses_request_and_conversation(tmp_path):
    model = ScriptedModel(
        [
            {"role": "assistant", "content": "Which document category would you like?"},
            call("search_matter", matter_number="M12205"),
            call("list_documents", document_type="Other Documents"),
            call("download_document", document_id="0"),
            call("create_archive"),
            {"role": "assistant", "content": "Hi, your ZIP for M12205 is ready."},
        ]
    )
    tools = Tools(FakeBrowser(1), tmp_path)
    history = []
    first, _ = await run_agent(
        "Please get documents for M12205", model, tools, conversation=history
    )
    assert first["status"] == "needs_input"
    assert tools.browser.download_calls == []
    final, _ = await run_agent("Other docs, please", model, tools, conversation=history)
    assert final["status"] == "complete"
    assert final["request"] == "Please get documents for M12205"
    assert sum(m["role"] == "system" for m in history) == 1
    assert [m["content"] for m in history if m["role"] == "user"] == [
        "Please get documents for M12205",
        "Other docs, please",
    ]
    assert final["reply"]["attachments"] == [final["archive"]]


async def test_partial_reply_only_attaches_successful_files(tmp_path):
    model = ScriptedModel(
        [
            call("search_matter", matter_number="M12205"),
            call("list_documents", document_type="Other Documents"),
            call("download_document", document_id="0"),
            call("download_document", document_id="1"),
            call("create_archive"),
            {"role": "assistant", "content": "Hi, one of the two downloads was unavailable."},
        ]
    )
    result, _ = await run_agent(
        "Other docs for M12205", model, Tools(FakeBrowser(2, fail="1"), tmp_path)
    )
    assert result["status"] == "partial"
    assert "couldn't complete" in result["reply"]["body"]
    with zipfile.ZipFile(result["reply"]["attachments"][0]) as archive:
        assert archive.namelist() == ["0.pdf"]
