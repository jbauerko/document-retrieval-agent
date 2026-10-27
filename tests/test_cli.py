import json
import sys
from io import StringIO

import pytest

from document_retrieval_agent import cli


class InputStream(StringIO):
    def __init__(self, text="", *, terminal=False):
        super().__init__(text)
        self.terminal = terminal

    def isatty(self):
        return self.terminal


@pytest.fixture
def configured_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOCUMENT_RETRIEVAL_AGENT_API_BASE", raising=False)
    monkeypatch.delenv("DOCUMENT_RETRIEVAL_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class ClarifyModel:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            return {"role": "assistant", "content": "Which document category would you like?"}

    model = ClarifyModel()
    monkeypatch.setattr(cli, "ChatModel", lambda *args: model)
    return model


@pytest.mark.parametrize("terminal", [False, True])
def test_one_shot_clarification_never_reads_follow_up(
    tmp_path, monkeypatch, configured_cli, terminal
):
    def unexpected_prompt(*args):
        pytest.fail("Noninteractive requests must not prompt for follow-up input")

    monkeypatch.setattr("builtins.input", unexpected_prompt)
    monkeypatch.setattr(sys, "stdin", InputStream(terminal=terminal))
    monkeypatch.setattr(
        sys, "argv", ["document-retrieval-agent", "Get documents for M12205", "--no-input"]
    )
    assert cli.main() == 0
    result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text())
    assert result["status"] == "needs_input"
    assert result["reply"]["body"] == "Which document category would you like?"
    assert result["reply"]["attachments"] == []
    assert configured_cli.calls == 1


def test_piped_ambiguous_request_returns_without_prompting(tmp_path, monkeypatch, configured_cli):
    monkeypatch.setattr(sys, "stdin", InputStream("Hi, get the documents for M12205\n"))
    monkeypatch.setattr(sys, "argv", ["document-retrieval-agent"])
    assert cli.main() == 0
    result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text())
    assert result["status"] == "needs_input"
    assert configured_cli.calls == 1


def test_eof_during_clarification_exits_with_question_saved(tmp_path, monkeypatch, configured_cli):
    def end_input(*args):
        raise EOFError

    monkeypatch.setattr(sys, "stdin", InputStream(terminal=True))
    monkeypatch.setattr("builtins.input", end_input)
    monkeypatch.setattr(sys, "argv", ["document-retrieval-agent", "Get documents for M12205"])
    assert cli.main() == 0
    result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text())
    assert result["status"] == "needs_input"
    assert configured_cli.calls == 1


@pytest.mark.parametrize("source", ["argument", "stdin", "file"])
def test_empty_requests_do_not_call_model(tmp_path, monkeypatch, configured_cli, source):
    argv = ["document-retrieval-agent"]
    monkeypatch.setattr(sys, "stdin", InputStream("  \n"))
    if source == "argument":
        argv.append("  ")
    elif source == "file":
        path = tmp_path / "empty.txt"
        path.write_text("  \n")
        argv.extend(["--request-file", str(path)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert configured_cli.calls == 0
    assert not (tmp_path / "runs").exists()


def test_conflicting_sources_fail_before_model(tmp_path, monkeypatch, configured_cli):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "document-retrieval-agent",
            "M12205 exhibits",
            "--request-file",
            str(tmp_path / "request.txt"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert configured_cli.calls == 0
