import json
import zipfile

import pytest

from document_retrieval_agent.agent import run_agent
from document_retrieval_agent.retrieval.documents import (
    CATEGORIES,
    METADATA_FIELDS,
    parse_counts,
    safe_filename,
)
from document_retrieval_agent.retrieval.tools import Tools

METADATA = dict.fromkeys(METADATA_FIELDS)
METADATA["title"] = "Test matter"


class FakeBrowser:
    site_url = "https://example.test"

    def __init__(self, count=12, fail=None):
        self.count = count
        self.fail = fail
        self.download_calls = []
        self.generation = 0

    async def read_text(self):
        counts = dict.fromkeys(CATEGORIES, 0)
        counts["Other Documents"] = self.count
        return "M12205\nTest matter\n" + "\n".join(
            f"{key} - {value}" for key, value in counts.items()
        )

    async def observe_page(self):
        self.generation += 1
        return {
            "snapshot_id": self.generation,
            "url": self.site_url,
            "text": await self.read_text(),
            "elements": [],
            "rows": [{"text": f"{i} Document {i} Public"} for i in range(self.count)],
        }

    async def open_site(self):
        return await self.observe_page()

    async def click(self, element_ref):
        if element_ref == "category":
            target = {"label": f"Other Documents - {self.count}", "context": ""}
        else:
            document_id = element_ref.removeprefix("row_")
            target = {
                "label": "GO GET IT",
                "context": f"{document_id} Document {document_id} Public",
            }
        return {"target": target, "observation": await self.observe_page()}

    async def download(self, element_ref, directory, document_id):
        self.download_calls.append(document_id)
        if document_id == self.fail:
            raise ValueError("Download unavailable")
        path = directory / f"{document_id}.pdf"
        path.write_bytes(b"%PDF-test-content")
        return {"path": str(path), "observation": await self.observe_page()}


async def prepare(tmp_path, count=12, fail=None):
    tools = Tools(FakeBrowser(count, fail), tmp_path)
    await tools.open_site("M12205", "Other Documents")
    await tools.record_matter(METADATA.copy())
    await tools.click("category")
    documents = [{"id": str(i), "title": f"Document {i}"} for i in range(min(count, 10))]
    await tools.record_documents(documents)
    return tools, documents


async def fetch_document(tools, document_id):
    await tools.click(f"row_{document_id}")
    return (await tools.download("file", document_id))["download"]


async def test_limit_idempotency_and_zip(tmp_path):
    tools, documents = await prepare(tmp_path)
    assert len(documents) == 10
    with pytest.raises(ValueError, match="document ID"):
        await fetch_document(tools, "11")
    for document in documents:
        await fetch_document(tools, document["id"])
    await fetch_document(tools, "0")
    assert len(tools.browser.download_calls) == 10
    result = await tools.create_archive()
    assert result["status"] == "complete"
    assert result["total_files"] == 12
    assert result["downloaded_count"] == 10
    with zipfile.ZipFile(result["archive"]) as archive:
        assert len(archive.namelist()) == 10
        assert archive.testzip() is None
    with pytest.raises(ValueError, match="One matter"):
        await tools.open_site("M12383", "Other Documents")
    with pytest.raises(ValueError, match="one document category"):
        await tools.open_site("M12205", "Exhibits")


async def test_partial_downloads_are_reported(tmp_path):
    tools, documents = await prepare(tmp_path, 3, fail="1")
    await fetch_document(tools, "0")
    with pytest.raises(ValueError, match="Attempt all"):
        await tools.create_archive()
    with pytest.raises(ValueError, match="unavailable"):
        await fetch_document(tools, "1")
    await fetch_document(tools, "2")
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
        await fetch_document(tools, "0")
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
            call("open_site", matter_number="M12205", document_type="Other Documents"),
            call("record_matter", metadata=METADATA),
            call("click", element_ref="category"),
            call("record_documents", documents=[{"id": "0", "title": "Document 0"}]),
            call("click", element_ref="row_0"),
            call("download", element_ref="file", document_id="0"),
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
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == result["tool_calls"] + 1


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
            call("open_site", matter_number="M12205", document_type="Other Documents"),
            call("record_matter", metadata=METADATA),
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
    from document_retrieval_agent.agent.model import ChatModel

    steps = [
        call("open_site", matter_number="M12205", document_type="Other Documents"),
        call("record_matter", metadata=METADATA),
        call("click", element_ref="category"),
        call(
            "record_documents",
            documents=[{"id": "0", "title": "Document 0"}, {"id": "1", "title": "Document 1"}],
        ),
        call("click", element_ref="row_0"),
        call("download", element_ref="file", document_id="0"),
        call("click", element_ref="row_1"),
        call("download", element_ref="file", document_id="1"),
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
    monkeypatch.setattr(cli, "BrowserSession", Browser)
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
            call("open_site", matter_number="M12205", document_type="Other Documents"),
            call("record_matter", metadata=METADATA),
            call("click", element_ref="category"),
            call("record_documents", documents=[{"id": "0", "title": "Document 0"}]),
            call("click", element_ref="row_0"),
            call("download", element_ref="file", document_id="0"),
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
            call("open_site", matter_number="M12205", document_type="Other Documents"),
            call("record_matter", metadata=METADATA),
            call("click", element_ref="category"),
            call(
                "record_documents",
                documents=[{"id": "0", "title": "Document 0"}, {"id": "1", "title": "Document 1"}],
            ),
            call("click", element_ref="row_0"),
            call("download", element_ref="file", document_id="0"),
            call("click", element_ref="row_1"),
            call("download", element_ref="file", document_id="1"),
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


async def test_model_values_must_match_observed_matter_and_rows(tmp_path):
    tools = Tools(FakeBrowser(2), tmp_path)
    await tools.open_site("M12205", "Other Documents")
    with pytest.raises(ValueError, match="copied from observed"):
        await tools.record_matter({**METADATA, "title": "Invented title"})
    await tools.record_matter(METADATA)
    await tools.click("category")
    with pytest.raises(ValueError, match="not found"):
        await tools.record_documents(
            [
                {"id": "99", "title": "Invented document"},
                {"id": "1", "title": "Document 1"},
            ]
        )
    with pytest.raises(ValueError, match="row order"):
        await tools.record_documents(
            [
                {"id": "1", "title": "Document 1"},
                {"id": "0", "title": "Document 0"},
            ]
        )
    assert tools.listed is False


async def test_download_must_follow_the_matching_document_row(tmp_path):
    tools, _ = await prepare(tmp_path, 2)
    with pytest.raises(ValueError, match="row control"):
        await tools.download("file", "0")
    await tools.click("row_1")
    with pytest.raises(ValueError, match="row control"):
        await tools.download("file", "0")
    assert tools.browser.download_calls == []


async def test_only_latest_observation_stays_in_model_context(tmp_path):
    history = []
    model = ScriptedModel(
        [
            call("open_site", matter_number="M12205", document_type="Other Documents"),
            call("observe_page"),
            {"role": "assistant", "content": "Unable to continue."},
        ]
    )
    await run_agent(
        "Other docs for M12205", model, Tools(FakeBrowser(1), tmp_path), conversation=history
    )
    results = [json.loads(m["content"])["result"] for m in history if m["role"] == "tool"]
    assert "retired" in results[0]["observation"]
    assert "text" not in results[0]["observation"]
    assert "text" in results[1]["observation"]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "text" in events[0]["output"]["result"]["observation"]


class TaskBrowser(FakeBrowser):
    def __init__(self, count=2, fail=None):
        super().__init__(count, fail)
        self.dialog = None
        self.searches = []

    async def search_matter(self, matter_number):
        self.searches.append(matter_number)
        return METADATA.copy()

    async def category_documents(self, category, count):
        return [{"id": str(i), "title": f"Document {i}"} for i in range(min(count, 10))]

    async def observe_page(self):
        observation = await super().observe_page()
        observation["dialog"] = "Download Files" if self.dialog is not None else None
        observation["elements"] = (
            [
                {
                    "ref": "file",
                    "tag": "div",
                    "role": "button",
                    "label": "attachment.pdf",
                    "context": "",
                },
                {"ref": "close", "tag": "button", "label": "Close", "context": ""},
            ]
            if self.dialog is not None
            else [
                {
                    "ref": f"row_{i}",
                    "tag": "button",
                    "label": "GO GET IT",
                    "context": f"{i} Document {i}",
                }
                for i in range(self.count)
            ]
        )
        return observation

    async def click(self, element_ref):
        if element_ref == "close":
            self.dialog = None
            return {
                "target": {"label": "Close", "context": ""},
                "observation": await self.observe_page(),
            }
        if element_ref.startswith("row_"):
            self.dialog = element_ref.removeprefix("row_")
        return await super().click(element_ref)


async def test_task_tools_download_individually_with_compact_results(tmp_path):
    browser = TaskBrowser()
    tools = Tools(browser, tmp_path)
    matter = await tools.execute(
        "search_matter", {"matter_number": "M12205", "document_type": "Other Documents"}
    )
    assert matter["matter"]["matter_number"] == "M12205"
    listing = await tools.execute("open_category", {"category": "Other Documents"})
    assert len(listing["documents"]) == 2
    assert browser.download_calls == []  # The model still has to choose each download.
    for document in listing["documents"]:
        result = await tools.execute("download_document", {"document_id": document["id"]})
        assert "observation" not in result
        assert browser.dialog is None
    await tools.download_document("0")
    assert browser.download_calls == ["0", "1"]
    assert "observation" not in matter
    assert "observation" not in listing
    assert (await tools.create_archive())["status"] == "complete"


async def test_task_download_failure_can_resume_after_browser_recovery(tmp_path):
    tools = Tools(TaskBrowser(fail="0"), tmp_path)
    await tools.search_matter("M12205", "Other Documents")
    await tools.open_category("Other Documents")
    with pytest.raises(ValueError, match="unavailable"):
        await tools.download_document("0")
    assert "0" in tools.failures
    # The model can inspect and close the dialog, then choose to retry the task.
    assert (await tools.observe_page())["observation"]["dialog"]
    await tools.click("close")
    tools.browser.fail = None
    await tools.download_document("0")
    await tools.download_document("1")
    assert tools.failures == {}
    assert (await tools.create_archive())["status"] == "complete"


async def test_task_scope_and_empty_category(tmp_path):
    tools = Tools(TaskBrowser(count=0), tmp_path)
    with pytest.raises(ValueError, match="Search the matter"):
        await tools.open_category("Other Documents")
    await tools.search_matter("M12205", "Other Documents")
    with pytest.raises(ValueError, match="requested category"):
        await tools.open_category("Exhibits")
    assert (await tools.open_category("Other Documents"))["documents"] == []
    assert (await tools.create_archive())["status"] == "complete"


async def test_agent_can_switch_from_task_failure_to_browser_recovery(tmp_path):
    class RecoveringBrowser(TaskBrowser):
        async def click(self, element_ref):
            if element_ref == "close":
                self.fail = None
            return await super().click(element_ref)

    model = ScriptedModel(
        [
            call("search_matter", matter_number="M12205", document_type="Other Documents"),
            call("open_category", category="Other Documents"),
            call("download_document", document_id="0"),
            call("observe_page"),
            call("click", element_ref="close"),
            call("download_document", document_id="0"),
            call("create_archive"),
            {"role": "assistant", "content": "Your ZIP is ready."},
        ]
    )
    browser = RecoveringBrowser(count=1, fail="0")
    result, _ = await run_agent("Other docs for M12205", model, Tools(browser, tmp_path))
    assert result["status"] == "complete"
    assert browser.download_calls == ["0", "0"]
    assert any(not outcome["ok"] for outcome in model.seen_tool_errors)
