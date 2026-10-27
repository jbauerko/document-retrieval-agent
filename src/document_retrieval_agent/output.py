"""Save run traces, structured results, and plain-text email drafts."""

import json
from datetime import UTC, datetime
from pathlib import Path


def build_reply(result: dict, body: str) -> dict:
    matter = result.get("matter")
    if result["status"] == "needs_input":
        subject = "A detail needed for your document request"
    elif matter:
        document_type = result["document_type"] or "Document request"
        subject = f"{matter['matter_number']} — {document_type}"
    else:
        subject = "Your document request"
    return {
        "subject": subject,
        "body": body.strip(),
        "attachments": [result["archive"]] if result.get("archive") else [],
    }


def log_event(directory: Path, event: str, **details) -> None:
    entry = {"time": datetime.now(UTC).isoformat(), "event": event, **details}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def save_result(directory: Path, result: dict, body: str) -> None:
    reply = build_reply(result, body)
    result["reply"] = reply
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (directory / "reply.txt").write_text(
        f"Subject: {reply['subject']}\n\n{reply['body']}\n", encoding="utf-8"
    )
