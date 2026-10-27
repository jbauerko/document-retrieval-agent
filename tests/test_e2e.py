"""Opt-in actual model + actual website + actual CLI verification."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("DOCUMENT_RETRIEVAL_AGENT_E2E_TEST") != "1",
    reason="Real model end-to-end test is opt-in",
)


def test_request_to_real_zip_and_draft(tmp_path):
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("DOCUMENT_RETRIEVAL_AGENT_API_KEY")):
        pytest.fail(
            "Configure GEMINI_API_KEY or DOCUMENT_RETRIEVAL_AGENT_API_KEY before running the live agent test"
        )
    command = [
        sys.executable,
        "-m",
        "document_retrieval_agent.cli",
        "Hi Agent, could you get the key docs for m 12205? Thanks!",
        "--output",
        str(tmp_path),
        "--timeout",
        "240",
    ]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=270)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    directory = next(tmp_path.iterdir())
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "complete"
    assert result["matter"]["matter_number"] == "M12205"
    assert result["document_type"] == "Key Documents"
    expected = min(10, result["matter"]["counts"]["Key Documents"])
    assert result["downloaded_count"] == expected
    with zipfile.ZipFile(result["archive"]) as archive:
        assert len(archive.namelist()) == expected
        assert archive.testzip() is None
        assert set(archive.namelist()) == {Path(d["path"]).name for d in result["downloads"]}
    reply = (directory / "reply.txt").read_text()
    assert "M12205" in reply
    assert "Key Documents" in reply
    assert reply.startswith("Subject: M12205 — Key Documents\n\n")
    assert result["reply"]["attachments"] == [result["archive"]]
    body = result["reply"]["body"].lower()
    for category in result["matter"]["counts"]:
        assert category.lower() in body
    for key in ["type", "category"]:
        value = result["matter"]["metadata"].get(key)
        if value:
            assert value.lower() in body
    assert "**" not in body
    assert result["tool_calls"] >= expected + 3
    assert (directory / "events.jsonl").is_file()
    print(f"Live agent artifacts: {directory}", flush=True)
